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
import urllib.request

from collector import (call_api, iter_tweet_results, parse_tweet,
                       _post_urls, _conversation_ids, _fetch_status_thread,
                       _resolve_chain, _RESOLVE_UA, REDIRECT_HOSTS, JST)

TARGETS = ("xfans.tokyo/shinohara_kaede", "myfans.jp/shinohara_personal")
TARGET_HANDLES = ("shinohara_kaede", "shinohara_personal")
# xfans公式短縮・myfans短縮・meta refresh系短縮を遷移先解決対象に追加
REDIRECTS = tuple(REDIRECT_HOSTS) + ("xfns.io", "x.fans", "mfns.jp",
                                   "b-short.link", "mfco.link")
# 投稿ページURL(/posts/<uuid>)は作者名を含まないため、
# ページをレンダリングして作者プロフィールリンクで判定する
CREATOR_HOSTS = ("xfans.tokyo", "myfans.jp")
_CTX = None
_CREATOR_CACHE = {}
_RENDER_BUDGET = [500]
_RENDER_EXHAUSTED = [False]
SEARCH_QUERIES = (
    'url:"shinohara_kaede" OR url:"shinohara_personal"',
    '"xfans.tokyo/shinohara_kaede" OR "myfans.jp/shinohara_personal"',
    '"shinohara_kaede" OR "shinohara_personal"',
)
DRY_RUN = os.environ.get("DRY_RUN") == "1"
BACKFILL = os.environ.get("SHINOHARA_BACKFILL") == "1"
MAX_SAVE = int(os.environ.get("SHINOHARA_MAX_SAVE", "30"))
START = int(os.environ.get("SHINOHARA_START", "0"))
SEARCH_SCROLLS = int(os.environ.get("SHINOHARA_SEARCH_SCROLLS", "15"))
NAV_TIMEOUT = 60_000
XSTATUS_RE = re.compile(r"(?:x\.com|twitter\.com)/[^/]+/status/(\d+)")


def _post_page_is_shinohara(url):
    """xfans/myfansの投稿ページを開き、篠原関連か判定。
    判定: 作者プロフィールリンクが篠原ハンドル、または
    ページのタイトル/本文に「篠原」が含まれる（コラボ投稿を拾うため）。
    結果はキャッシュ。判定不能（予算切れ/エラー）は None。"""
    key = url.split("?")[0].rstrip("/")
    if key in _CREATOR_CACHE:
        return _CREATOR_CACHE[key]
    if _CTX is None or _RENDER_BUDGET[0] <= 0:
        if not _RENDER_EXHAUSTED[0]:
            _RENDER_EXHAUSTED[0] = True
            print("    作者判定の上限到達。以降の投稿ページは未確認扱い")
        return None
    _RENDER_BUDGET[0] -= 1
    verdict = None
    pg = None
    try:
        pg = _CTX.new_page()
        pg.goto(url, wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(4000)
        # 年齢確認ゲート（myfans等）
        try:
            pg.get_by_text("はい", exact=True).click(timeout=2500)
            pg.wait_for_timeout(4000)
        except Exception:
            pass
        hrefs = pg.eval_on_selector_all(
            "a[href]", "els=>els.map(e=>e.getAttribute('href'))")
        if any(any(h in (x or "").lower()
                   for h in TARGET_HANDLES) for x in hrefs):
            verdict = True
        else:
            text = (pg.title() + " " + pg.inner_text("body")).lower()
            verdict = "篠原" in text
        print(f"    {url} → 作者/内容判定: "
              + ("篠原" if verdict else "非該当"))
    except Exception:
        pass
    finally:
        if pg:
            try:
                pg.close()
            except Exception:
                pass
    if verdict is not None:
        _CREATOR_CACHE[key] = verdict
    return verdict


def _resolve_dest(url, budget):
    """HTTPリダイレクトとmeta refreshを辿って最終URLを返す。
    returns (final_url, body_lower, ok)"""
    cur = url
    seen = set()
    for _ in range(5):
        if cur in seen:
            return cur, "", True
        seen.add(cur)
        if budget[0] <= 0:
            return None, None, False
        budget[0] -= 1
        try:
            req = urllib.request.Request(
                cur, headers={"User-Agent": _RESOLVE_UA})
            with urllib.request.urlopen(req, timeout=12) as res:
                final = res.geturl()
                ct = (res.headers.get("Content-Type") or "").lower()
                body = res.read(300_000).decode("utf-8", "ignore") \
                    if "html" in ct else ""
        except Exception:
            return None, None, False
        m = re.search(r'http-equiv=["\']?refresh["\']?[^>]*?url=([^"\'>\s]+)',
                      body, re.I)
        if m:
            cur = urllib.parse.urljoin(final, m.group(1))
            continue
        return final, body.lower(), True
    return cur, "", True


def _hits_target(url, budget):
    """URLが篠原コンテンツへ着地するか。短縮/誘導は遷移先まで解決。
    投稿ページ(/posts/<uuid>)はページを開いて判定する。
    True / False / None（判定不能）を返す。"""
    if any(t in url.lower() for t in TARGETS):
        return True
    final = url
    host = urllib.parse.urlparse(url).netloc.lower()
    if any(h in host for h in REDIRECTS):
        final, body, ok = _resolve_dest(url, budget)
        if not ok:
            return None
        if any(t in (final + " " + body).lower() for t in TARGETS):
            return True
    fhost = urllib.parse.urlparse(final).netloc.lower()
    fpath = urllib.parse.urlparse(final).path
    if any(h in fhost for h in CREATOR_HOSTS) and "/posts/" in fpath:
        return _post_page_is_shinohara(final)
    return False


def _status_link_is_target(page, tid):
    """リプ内の別ポストへのリンクを辿り、そのポスト内のリンクを判定。"""
    _, did, res2 = _fetch_status_thread(
        page, f"https://x.com/i/status/{tid}")
    focal = next((t for t in res2 if t.get("rest_id") == tid), None)
    if not focal:
        print(f"    リンク先ポスト {tid} 取得不可")
        return None
    budget = [6]
    unknown = False
    for u in _post_urls(focal.get("legacy") or {}):
        if XSTATUS_RE.search(u):
            continue
        r = _hits_target(u, budget)
        if r:
            print(f"    リンク先ポスト {tid} に篠原リンク検出")
            return True
        if r is None:
            unknown = True
    return None if unknown else False


def _thread_has_target(results, tweet_id, page=None):
    """会話内（focal含む）のポストのURLに篠原リンクがあるか。
    リプ内の別ポストへのリンクも1段だけ辿る。
    True / False / None（未確認のリンクが残った）を返す。"""
    budget = [8]
    x_follow = [3]
    unknown = False
    conv = _conversation_ids(results, tweet_id)
    for tr in results:
        if tr.get("rest_id") not in conv:
            continue
        for u in _post_urls(tr.get("legacy") or {}):
            m = XSTATUS_RE.search(u)
            if m:
                tid2 = m.group(1)
                if (tid2 in conv or x_follow[0] <= 0
                        or page is None):
                    continue
                x_follow[0] -= 1
                r = _status_link_is_target(page, tid2)
                if r:
                    return True
                if r is None:
                    unknown = True
                continue
            r = _hits_target(u, budget)
            if r:
                return True
            if r is None:
                unknown = True
    return None if unknown else False


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


def _fetch_thread_full(page, tid):
    """詳細ページを開き、返信欄も展開してTweetDetailを全捕捉。
    （_fetch_status_thread は初期レスポンスのみで、スクロールしないと
      出ない返信を取りこぼすため）"""
    captured = []

    def on_response(res):
        if "TweetDetail" in res.url or "TweetResultByRestId" in res.url:
            try:
                captured.append(res.json())
            except Exception:
                pass

    page.on("response", on_response)
    try:
        page.goto(f"https://x.com/i/status/{tid}", timeout=NAV_TIMEOUT,
                  wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        # 返信欄を展開（collector.get_tweet_detail と同じ要領）
        try:
            for _ in range(2):
                for b in page.get_by_text(
                        re.compile("返信をさらに表示|Show more replies")
                ).all():
                    try:
                        b.click(timeout=1500)
                    except Exception:
                        pass
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(1200)
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(1200)
        except Exception:
            pass
    except Exception:
        pass
    finally:
        page.remove_listener("response", on_response)
    results = []
    for body in captured:
        iter_tweet_results(body, results)
    return tid, results


def _save_row(page, tid, existing, shot=True):
    """status id のポストを詳細取得して篠原バズタブへ保存。
    returns: "saved" / "skip"（非該当・重複） / "fail"（取得・保存失敗）"""
    did, results = _fetch_thread_full(page, tid)
    if not results:
        print(f"  {tid} 詳細取得できず（削除済み?）")
        return "fail"
    focal = next((tr for tr in results if tr.get("rest_id") == did), None)
    if not focal:
        print(f"  {tid} focalポストがレスポンスに無し")
        return "fail"
    verdict = _thread_has_target(results, did, page)
    if verdict is None:
        print(f"  {tid} リンク判定未確認（再試行対象）")
        return "fail"
    if not verdict:
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


def _mark_checked(tid, pending):
    """精査済みidをバッファし、10件たまったら設定シートへ記録。"""
    pending.append(tid)
    if len(pending) >= 10:
        _flush_checked(pending)


def _flush_checked(pending):
    if pending and not DRY_RUN:
        try:
            call_api(params="action=markchecked&ids=" + ",".join(pending))
        except Exception as e:
            print(f"  markchecked error: {e}", file=sys.stderr)
    pending.clear()


def _checked_ids():
    """精査済みstatus id集合（設定シートL列）。"""
    try:
        res = call_api(params="action=config")
        return set(res.get("shinoharaCheckedIds") or [])
    except Exception as e:
        print(f"  config取得失敗: {e}", file=sys.stderr)
        return set()


def main():
    if not collector.APPS_SCRIPT_URL or not collector.TOKEN:
        sys.exit("APPS_SCRIPT_URL / APPS_SCRIPT_TOKEN が未設定です")

    existing = _existing_ids()
    # BACKFILL=1 は全行を再精査（checkedを無視）。
    # 通常実行は差分のみ（checkedをスキップ）。
    checked = set() if BACKFILL else _checked_ids()
    print(f"篠原バズ 既存: {len(existing)}件 精査済み: {len(checked)}件"
          + ("（全行再精査）" if BACKFILL else "（差分精査）")
          + ("（DRY_RUN）" if DRY_RUN else ""))

    saved = 0
    pending = []
    with sync_playwright() as p:
        browser, page = _launch(p)
        global _CTX
        _CTX = page.context
        if not BACKFILL:
            # 検索で篠原リンク含有ポストを直接拾う（補助経路）
            targets = _collect_targets(page) - existing
            print(f"検索ヒット保存対象: {len(targets)}件")
            for tid in sorted(targets, key=int):
                try:
                    if _save_row(page, tid, existing) == "saved":
                        saved += 1
                except Exception as e:
                    print(f"  {tid} error: {e}", file=sys.stderr)
                if saved >= MAX_SAVE:
                    break
                page.wait_for_timeout(1500)

        # 収集済み行のリプ欄を差分精査（メイン経路）
        rows = []
        for sheet in ("", "growth"):
            params = "action=list" + (f"&sheet={sheet}" if sheet else "")
            rows += (call_api(params=params).get("rows") or [])
        rows = rows[START:]
        print(f"精査対象: {len(rows)}行（checked/既存はスキップ）")
        consec_fail = 0
        for i, row in enumerate(rows):
            m = re.search(r"/status/(\d+)", row.get("url") or "")
            if not m or m.group(1) in existing or m.group(1) in checked:
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
                if consec_fail >= 30:
                    print("連続失敗のため中断（再実行してください）")
                    break
            else:
                consec_fail = 0
                _mark_checked(tid, pending)
            if saved >= MAX_SAVE:
                print(f"保存上限 {MAX_SAVE}件に到達")
                break
            page.wait_for_timeout(2000)
        _flush_checked(pending)
        browser.close()

    print(f"完了: {saved}件を「篠原バズ」タブに保存"
          + ("（DRY_RUN）" if DRY_RUN else ""))


if __name__ == "__main__":
    main()
