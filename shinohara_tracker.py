#!/usr/bin/env python3
"""篠原バズ collector.

リプ欄（または本文）に篠原のページへ飛ぶリンクがあるポストを
「篠原バズ」タブに収集する。

対象リンク（部分一致・小文字比較、短縮リンクは遷移先まで解決）:
  - xfans.tokyo/shinohara_kaede
  - myfans.jp/shinohara_personal

モード:
- 通常: X検索で対象URLを含むポスト（≒リプ）を拾い、
        その親ポスト（リプ先）を「篠原バズ」に保存
- SHINOHARA_BACKFILL=1: 「X収集」「実際伸びた投稿」タブの既存行を
        精査し、リプ欄に篠原リンクがある行を「篠原バズ」にコピー

DRY_RUN=1 でシートへの投稿をスキップして候補を表示するだけになる。
"""

import base64
import os
import re
import sys
import urllib.parse
from datetime import datetime

from playwright.sync_api import sync_playwright

import collector
from collector import (call_api, iter_tweet_results, parse_tweet,
                       _post_urls, _conversation_ids, _fetch_status_thread,
                       _resolve_chain, REDIRECT_HOSTS, JST)

TARGETS = ("xfans.tokyo/shinohara_kaede", "myfans.jp/shinohara_personal")
SEARCH_QUERIES = (
    'url:"shinohara_kaede" OR url:"shinohara_personal"',
    '"xfans.tokyo/shinohara_kaede" OR "myfans.jp/shinohara_personal"',
)
DRY_RUN = os.environ.get("DRY_RUN") == "1"
BACKFILL = os.environ.get("SHINOHARA_BACKFILL") == "1"
MAX_SAVE = int(os.environ.get("SHINOHARA_MAX_SAVE", "30"))
SEARCH_SCROLLS = int(os.environ.get("SHINOHARA_SEARCH_SCROLLS", "15"))
NAV_TIMEOUT = 60_000
XSTATUS_RE = re.compile(r"(?:x\.com|twitter\.com)/[^/]+/status/(\d+)")


def _hits_target(url, budget):
    """URLが篠原ページへ着地するか。短縮/誘導は遷移先まで解決。"""
    if any(t in url.lower() for t in TARGETS):
        return True
    host = urllib.parse.urlparse(url).netloc.lower()
    if budget[0] > 0 and any(h in host for h in REDIRECT_HOSTS):
        budget[0] -= 1
        try:
            hops, final, html, _ = _resolve_chain(url)
        except Exception:
            return False
        chain = " ".join(hops + [final]).lower() + " " + html
        return any(t in chain for t in TARGETS)
    return False


def _thread_has_target(results, tweet_id):
    """会話内（focal含む）のポストのURLに篠原リンクがあるか。"""
    budget = [8]
    conv = _conversation_ids(results, tweet_id)
    for tr in results:
        if tr.get("rest_id") not in conv:
            continue
        for u in _post_urls(tr.get("legacy") or {}):
            if XSTATUS_RE.search(u):
                continue
            if _hits_target(u, budget):
                return True
    return False


def _existing_ids():
    """篠原バズタブの既存status id集合。"""
    res = call_api(params="action=list&sheet=shinohara")
    ids = set()
    for row in res.get("rows") or []:
        m = re.search(r"/status/(\d+)", row.get("url") or "")
        if m:
            ids.add(m.group(1))
    return ids


def _scrape_search(page, query, captured):
    """X検索（最新タブ）を開いてSearchTimelineレスポンスを捕捉。"""
    def on_response(res):
        if "SearchTimeline" in res.url:
            try:
                captured.append(res.json())
            except Exception:
                pass

    page.on("response", on_response)
    q = urllib.parse.quote(query)
    page.goto(f"https://x.com/search?q={q}&src=typed_query&f=live",
              timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
    page.wait_for_timeout(5000)
    if "/i/flow/login" in page.url:
        raise RuntimeError("Xのログイン画面にリダイレクトされました。"
                           "state.jsonが無効です。")
    last_height = stale = 0
    for _ in range(SEARCH_SCROLLS):
        before = len(captured)
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(2000)
        height = page.evaluate("document.body.scrollHeight")
        if height == last_height and len(captured) == before:
            stale += 1
            if stale >= 3:
                break
        else:
            stale = 0
        last_height = height
    page.remove_listener("response", on_response)


def _collect_targets(page):
    """検索結果から「篠原リンクを含むポスト」を集め、
    保存対象（リプ→親ポストid / 単独→自分自身）のidセットを返す。"""
    targets = set()
    for query in SEARCH_QUERIES:
        captured = []
        try:
            _scrape_search(page, query, captured)
        except Exception as e:
            print(f"  search error ({query}): {e}", file=sys.stderr)
            continue
        hits = 0
        for body in captured:
            results = []
            iter_tweet_results(body, results)
            for tr in results:
                lg = tr.get("legacy") or {}
                urls = _post_urls(lg)
                budget = [4]
                if not any(_hits_target(u, budget) for u in urls):
                    continue
                hits += 1
                # リプへのリプでもルートポストを対象にする
                targets.add(lg.get("conversation_id_str")
                            or tr.get("rest_id"))
        print(f"  query {query!r}: {hits}件のリンク含有ポスト")
    return targets


def _save_row(page, tid, existing, shot=True):
    """status id のポストを詳細取得して篠原バズタブへ保存。
    returns: "saved" / "skip"（非該当・重複） / "fail"（取得・保存失敗）"""
    duser, did, results = _fetch_status_thread(
        page, f"https://x.com/i/status/{tid}")
    if not results:
        print(f"  {tid} 詳細取得できず（削除済み?）")
        return "fail"
    focal = next((tr for tr in results if tr.get("rest_id") == did), None)
    if not focal:
        print(f"  {tid} focalポストがレスポンスに無し")
        return "fail"
    if not _thread_has_target(results, did):
        print(f"  {tid} リプ欄に篠原リンク確認できず → スキップ")
        return "skip"
    t = parse_tweet(focal)
    if DRY_RUN:
        print(f"  [DRY_RUN] {t['url']} imp={t['impressions']:,}"
              f" text={t['text'][:40]!r}")
        return "saved"
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)
    shot_png = page.screenshot() if shot else None
    row = {
        "date": t["date"] or datetime.now(JST).strftime("%Y/%m/%d %H:%M"),
        "url": t["url"], "text": t["text"], "media": t["media"],
        "impressions": t["impressions"], "likes": t["likes"],
        "reposts": t["reposts"], "bookmarks": t["bookmarks"],
    }
    payload = {"target": "shinohara", "row": row}
    if shot_png:
        payload["imageBase64"] = base64.b64encode(shot_png).decode()
    res = call_api(payload=payload)
    if res.get("saved"):
        print(f"  saved No.{res.get('no')} {t['url']}"
              f" (imp {t['impressions']:,})")
        existing.add(did)
        return "saved"
    if res.get("duplicate"):
        existing.add(did)
        return "skip"
    print(f"  save failed {tid}: {res}", file=sys.stderr)
    return "fail"


def _launch(p):
    headful = os.environ.get("HEADFUL") == "1"
    args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    try:
        browser = p.chromium.launch(channel="chrome", headless=not headful,
                                    args=args)
    except Exception:
        browser = p.chromium.launch(headless=not headful, args=args)
    ctx = browser.new_context(
        storage_state=collector.STATE_PATH,
        viewport={"width": 700, "height": 900},
        locale="ja-JP", timezone_id="Asia/Tokyo")
    return browser, ctx.new_page()


def main():
    if not collector.APPS_SCRIPT_URL or not collector.TOKEN:
        sys.exit("APPS_SCRIPT_URL / APPS_SCRIPT_TOKEN が未設定です")

    existing = _existing_ids()
    print(f"篠原バズ 既存: {len(existing)}件"
          + ("（バックフィル）" if BACKFILL else "")
          + ("（DRY_RUN）" if DRY_RUN else ""))

    saved = 0
    with sync_playwright() as p:
        browser, page = _launch(p)
        if BACKFILL:
            # 既存収集行のリプ欄を精査してマッチ分をコピー
            rows = []
            for sheet in ("", "growth"):
                params = "action=list" + (f"&sheet={sheet}" if sheet else "")
                rows += (call_api(params=params).get("rows") or [])
            print(f"精査対象: {len(rows)}行")
            consec_fail = 0
            for i, row in enumerate(rows):
                m = re.search(r"/status/(\d+)", row.get("url") or "")
                if not m or m.group(1) in existing:
                    continue
                tid = m.group(1)
                status = "fail"
                for attempt in range(3):
                    try:
                        status = _save_row(page, tid, existing)
                    except Exception as e:
                        print(f"  [{i+1}/{len(rows)}] {tid} error: {e}",
                              file=sys.stderr)
                        status = "fail"
                        page.wait_for_timeout(5000 * (attempt + 1))
                        continue
                    break
                if status == "saved":
                    saved += 1
                if status == "fail":
                    consec_fail += 1
                    if consec_fail >= 15:
                        print("連続失敗のため中断（再実行してください）")
                        break
                else:
                    consec_fail = 0
                if saved >= MAX_SAVE:
                    print(f"保存上限 {MAX_SAVE}件に到達")
                    break
                page.wait_for_timeout(1500)
        else:
            targets = _collect_targets(page) - existing
            print(f"保存対象: {len(targets)}件")
            for tid in sorted(targets, key=int):
                try:
                    if _save_row(page, tid, existing) == "saved":
                        saved += 1
                except Exception as e:
                    print(f"  {tid} error: {e}", file=sys.stderr)
                if saved >= MAX_SAVE:
                    print(f"保存上限 {MAX_SAVE}件に到達")
                    break
                page.wait_for_timeout(1500)
        browser.close()

    print(f"完了: {saved}件を「篠原バズ」タブに保存"
          + ("（DRY_RUN）" if DRY_RUN else ""))


if __name__ == "__main__":
    main()
