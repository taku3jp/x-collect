#!/usr/bin/env python3
"""X収集タブの既存行から、特定誘導先（例: myfans.jp/xxx）へ飛ぶ投稿を
再検証して削除するワンショットツール。

TARGET_CHANNEL 環境変数で誘導先を指定（部分一致・小文字比較）。
各投稿の詳細ページを開き、本文・会話内返信・本人リング先(1段)のURLを
リダイレクト解決まで行って誘導先を判定する。

判定は collector.BLOCKED_TARGETS と同じ意味だが、既存行の遡及精査用に
対象を環境変数で都度指定できるようにしたもの。
マッチした行は X収集タブから削除し、status idを設定タブF列(reject)に
記録して再収集を防ぐ。

使い方:
  TARGET_CHANNEL=myfans.jp/jukiya_erotame python cleanup_channel.py
  DRY_RUN=1 で削除せずマッチ行の表示だけ行う
"""

import os
import re
import sys
import urllib.parse

from playwright.sync_api import sync_playwright

import collector
from collector import (call_api, iter_tweet_results, _post_urls,
                       _resolve_chain, _conversation_ids,
                       _fetch_status_thread, _author_name,
                       REDIRECT_HOSTS)

TARGET = os.environ.get("TARGET_CHANNEL", "").strip().lower()
DRY_RUN = os.environ.get("DRY_RUN") == "1"
XSTATUS_RE = re.compile(r"(?:x\.com|twitter\.com)/[^/]+/status/\d+")


def _url_hits_target(url, budget):
    """URLがターゲットへ着地するか。短縮/誘導は遷移先まで解決。"""
    if TARGET in url.lower():
        return True
    host = urllib.parse.urlparse(url).netloc.lower()
    if budget[0] > 0 and any(h in host for h in REDIRECT_HOSTS):
        budget[0] -= 1
        try:
            hops, final, html, _ = _resolve_chain(url)
        except Exception:
            return False
        chain = " ".join(hops + [final]).lower() + " " + html
        return TARGET in chain
    return False


def _thread_hits_target(page, results, tweet_id, depth=0):
    """スレッド内（focal含む会話メンバー）のURLがターゲットへ飛ぶか。
    本人のx.comリング先は1段だけ本文URLを確認。"""
    budget = [8]
    conv = _conversation_ids(results, tweet_id)
    self_ring = []
    for tr in results:
        rid = tr.get("rest_id")
        if rid not in conv:
            continue
        lg = tr.get("legacy") or {}
        urls = _post_urls(lg)
        for u in urls:
            if XSTATUS_RE.search(u):
                if _author_name(tr) and rid != tweet_id:
                    self_ring.append(u)
                continue
            if _url_hits_target(u, budget):
                return True
    if depth == 0:
        for u in list(dict.fromkeys(self_ring))[:2]:
            try:
                _du, did, sub = _fetch_status_thread(page, u)
            except Exception:
                continue
            if not sub:
                continue
            dest = next((tr for tr in sub if tr.get("rest_id") == did), None)
            if dest and any(_url_hits_target(u2, budget)
                            for u2 in _post_urls(dest.get("legacy") or {})):
                return True
    return False


def main():
    if not collector.APPS_SCRIPT_URL or not collector.TOKEN:
        sys.exit("APPS_SCRIPT_URL / APPS_SCRIPT_TOKEN が未設定です")
    if not TARGET:
        sys.exit("TARGET_CHANNEL を指定してください（例: myfans.jp/jukiya_erotame）")

    res = call_api(params="action=list")
    rows = res.get("rows") or []
    print(f"対象行: {len(rows)}件 / ターゲット: {TARGET}"
          + ("（DRY_RUN）" if DRY_RUN else ""))
    if not rows:
        return

    headful = os.environ.get("HEADFUL") == "1"
    launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    hit_ids = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome",
                                        headless=not headful, args=launch_args)
        except Exception:
            browser = p.chromium.launch(headless=not headful, args=launch_args)
        ctx = browser.new_context(
            storage_state=collector.STATE_PATH,
            viewport={"width": 700, "height": 900},
            locale="ja-JP", timezone_id="Asia/Tokyo")
        page = ctx.new_page()

        for i, row in enumerate(rows):
            url = row.get("url") or ""
            m = re.search(r"/status/(\d+)", url)
            if not m:
                continue
            tid = m.group(1)
            try:
                _user, did, results = _fetch_status_thread(page, url)
            except Exception as e:
                print(f"  [{i+1}/{len(rows)}] {url} fetch error: {e}",
                      file=sys.stderr)
                continue
            if not results:
                print(f"  [{i+1}/{len(rows)}] {url} 詳細取得できず（削除済み?）")
                continue
            try:
                hit = _thread_hits_target(page, results, did)
            except Exception as e:
                print(f"  [{i+1}/{len(rows)}] {url} check error: {e}",
                      file=sys.stderr)
                continue
            if hit:
                hit_ids.append(tid)
                print(f"  [{i+1}/{len(rows)}] HIT No.{row.get('no')} {url}")
            page.wait_for_timeout(500)
        browser.close()

    print(f"マッチ: {len(hit_ids)}件 {hit_ids}")
    if hit_ids and not DRY_RUN:
        r1 = call_api(params="action=delete&ids=" + ",".join(hit_ids))
        print(f"削除: {r1}")
        r2 = call_api(params="action=reject&ids=" + ",".join(hit_ids))
        print(f"reject登録: {r2}")


if __name__ == "__main__":
    main()
