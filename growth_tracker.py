#!/usr/bin/env python3
"""Own-account growth tracker.

1. Fetch config from the spreadsheet via Apps Script webhook
   (growth accounts / impression threshold / already-recorded IDs).
2. Scrape each account's profile timeline with Playwright + X login session
   (state.json). Impressions come from UserTweets GraphQL responses.
3. For tweets that newly crossed the threshold, open the permalink, take a
   screenshot, POST to Apps Script (target=growth → 「実際伸びた投稿」 tab),
   then post a Slack notification via Incoming Webhook.

DRY_RUN=1 でシート・Slackへの投稿をスキップして候補を表示するだけになる。
"""

import base64
import json
import os
import re
import sys
import urllib.request
from datetime import datetime

from playwright.sync_api import sync_playwright

import collector
from collector import call_api, iter_tweet_results, parse_tweet

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
SHEET_URL = os.environ.get(
    "GROWTH_SHEET_URL",
    "https://docs.google.com/spreadsheets/d/"
    "1Hu4lhEesRQOJPB_puNHfYHerNSWdso3_LFC8uNGeJgs/edit#gid=832401486")
GROWTH_MAX_SCROLLS = int(os.environ.get("GROWTH_MAX_SCROLLS", "40"))
MAX_AGE_DAYS = int(os.environ.get("GROWTH_MAX_AGE_DAYS", "30"))
MAX_POSTS_PER_RUN = int(os.environ.get("GROWTH_MAX_POSTS", "30"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
NAV_TIMEOUT = 60_000


def scrape_profile(page, account, captured):
    """Open a profile and collect UserTweets responses while scrolling."""
    def on_response(res):
        # プロフィールTLのGraphQL op。X側の改修でUserTweets→
        # UserOriginalsTimelineへ切り替わっているため両方拾う
        if any(op in res.url for op in
               ("UserTweets", "UserOriginalsTimeline",
                "UserTweetsAndReplies", "UserMedia")):
            try:
                captured.append(res.json())
            except Exception:
                pass

    page.on("response", on_response)
    page.goto(f"https://x.com/{account}", timeout=NAV_TIMEOUT,
              wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    if "/i/flow/login" in page.url:
        raise RuntimeError("Xのログイン画面にリダイレクトされました。state.jsonが無効です。")

    # 直近ポストが閾値を超えるまで伸び続けるので毎回再スキャンする。
    # 高さが伸びず新規レスポンスも来ない状態が続いたら底とみなす。
    # 30日より古いポストまで潜ったら打ち切り（閾値超過はほぼ起きないため）
    last_height = 0
    stale = 0
    cutoff_ts = datetime.now().timestamp() - MAX_AGE_DAYS * 86400
    for i in range(GROWTH_MAX_SCROLLS):
        before = len(captured)
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(2000)
        height = page.evaluate("document.body.scrollHeight")

        # 取得済みツイートの最古日時が閾値より古ければ打ち切り
        oldest = None
        for body in captured:
            results = []
            iter_tweet_results(body, results)
            for tr in results:
                t = parse_tweet(tr)
                if t["sort_key"] and (oldest is None or t["sort_key"] < oldest):
                    oldest = t["sort_key"]
        if oldest and oldest < cutoff_ts:
            break

        if height == last_height and len(captured) == before:
            stale += 1
            if stale >= 4:
                break
            page.wait_for_timeout(2000)
        else:
            stale = 0
        last_height = height

    page.remove_listener("response", on_response)
    print(f"  @{account}: scrolls={i+1} captured={len(captured)}")


def take_screenshot(page, tweet):
    """Open the permalink and screenshot the target article only."""
    page.goto(tweet["url"], timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    article = page.locator(
        f'article[data-testid="tweet"]:has(a[href$="/{tweet["id"]}"])'
    ).first
    if not article.count():
        article = page.locator('article[data-testid="tweet"]').first
    for btn in article.get_by_role(
            "button", name=re.compile("表示|View")).all():
        try:
            btn.click(timeout=800)
        except Exception:
            pass
    try:
        article.evaluate("el => el.scrollIntoView({block: 'start'})")
    except Exception:
        article.scroll_into_view_if_needed(timeout=10_000)
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass
    try:
        article.locator("img, video").first.wait_for(
            state="visible", timeout=10_000)
        page.wait_for_function(
            """el => {
                const imgs = el.querySelectorAll('img');
                return imgs.length === 0 ||
                    [...imgs].every(i => i.naturalWidth > 0);
            }""",
            arg=article.element_handle(), timeout=10_000)
    except Exception:
        pass
    page.wait_for_timeout(500)
    return article.screenshot(timeout=15_000)


def notify_slack(tweet, no):
    if not SLACK_WEBHOOK_URL:
        print("  SLACK_WEBHOOK_URL 未設定 → Slack通知スキップ")
        return
    text = (f"伸びた投稿を検出（No.{no}）\n"
            f"{tweet['url']}\n"
            f"インプ {tweet['impressions']:,} / いいね {tweet['likes']:,}"
            f" / RT {tweet['reposts']:,} / 保存 {tweet['bookmarks']:,}\n"
            f"{tweet['text'][:120]}\n"
            f"スプシ: {SHEET_URL}")
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30).read()


def main():
    if not collector.APPS_SCRIPT_URL or not collector.TOKEN:
        sys.exit("APPS_SCRIPT_URL / APPS_SCRIPT_TOKEN が未設定です")

    config = call_api(params="action=config")
    if config.get("error"):
        sys.exit(f"設定の取得に失敗: {config['error']}")

    growth = config.get("growth") or {}
    if not growth.get("enabled", True):
        print("設定タブで自アカ監視がOFFになっています。終了します。")
        return

    threshold = int(growth.get("threshold") or 100000)
    accounts = growth.get("accounts") or []
    existing = set(growth.get("existingIds") or [])
    print(f"監視対象: {accounts} / インプ閾値: {threshold:,}"
          f" / 記録済み: {len(existing)}件")

    if not accounts:
        print("監視アカウント未指定（設定タブH5以降に@なしIDを入力）")
        return

    headful = os.environ.get("HEADFUL") == "1"
    launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=not headful,
                                        args=launch_args)
        except Exception:
            browser = p.chromium.launch(headless=not headful, args=launch_args)
        ctx = browser.new_context(
            storage_state=collector.STATE_PATH,
            viewport={"width": 700, "height": 900},
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )
        page = ctx.new_page()

        tweets = {}
        for account in accounts:
            captured = []
            try:
                scrape_profile(page, account, captured)
            except Exception as e:
                print(f"  @{account} error: {e}", file=sys.stderr)
                continue
            for body in captured:
                results = []
                iter_tweet_results(body, results)
                for tr in results:
                    t = parse_tweet(tr)
                    # 本人のポストだけ（TL内の他人ポスト・広告を除外）
                    if t["id"] and t["user"].lower() == account.lower() \
                            and t["id"] not in tweets:
                        tweets[t["id"]] = t

        candidates = [t for t in tweets.values()
                      if t["impressions"] >= threshold
                      and t["id"] not in existing]
        candidates.sort(key=lambda t: t["sort_key"])
        print(f"発見: {len(tweets)}件 / 閾値以上かつ未記録: {len(candidates)}件")

        sent = 0
        for t in candidates[:MAX_POSTS_PER_RUN]:
            if DRY_RUN:
                print(f"  [DRY_RUN] {t['url']} imp={t['impressions']:,}"
                      f" text={t['text'][:40]!r}")
                continue
            shot = None
            try:
                shot = take_screenshot(page, t)
            except Exception as e:
                print(f"  screenshot failed {t['id']}: {e}", file=sys.stderr)

            row = {
                "date": t["date"] or datetime.now(collector.JST)
                        .strftime("%Y/%m/%d %H:%M"),
                "url": t["url"], "text": t["text"],
                "media": t["media"], "impressions": t["impressions"],
                "likes": t["likes"], "reposts": t["reposts"],
                "bookmarks": t["bookmarks"],
            }
            payload = {"target": "growth", "row": row}
            if shot:
                payload["imageBase64"] = base64.b64encode(shot).decode()

            try:
                res = call_api(payload=payload)
                if res.get("saved"):
                    print(f"  saved No.{res.get('no')} {t['url']}"
                          f" (imp {t['impressions']:,})")
                    sent += 1
                    try:
                        notify_slack(t, res.get("no"))
                    except Exception as e:
                        print(f"  slack notify failed: {e}", file=sys.stderr)
                else:
                    print(f"  save failed {t['id']}: {res}", file=sys.stderr)
            except Exception as e:
                print(f"  post error {t['id']}: {e}", file=sys.stderr)

            page.wait_for_timeout(1000)

        browser.close()

    print(f"完了: {sent}件を「実際伸びた投稿」タブに保存"
          + ("（DRY_RUN）" if DRY_RUN else ""))


if __name__ == "__main__":
    main()
