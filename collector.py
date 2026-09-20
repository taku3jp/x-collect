#!/usr/bin/env python3
"""X posts collector.

1. Fetch config (target accounts / impression threshold / already-collected IDs)
   from the spreadsheet via Apps Script webhook.
2. Scrape each account's timeline with Playwright + X login session (state.json).
   Tweet metrics are taken from X's internal GraphQL responses (UserTweets /
   TweetDetail), not from DOM parsing.
3. For tweets above the threshold, open the permalink, take a screenshot and
   POST the row to Apps Script, which inserts it at the top of the sheet.
"""

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

JST = timezone(timedelta(hours=9))

APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "")
TOKEN = os.environ.get("APPS_SCRIPT_TOKEN", "")
STATE_PATH = os.environ.get("X_STATE_PATH", "state.json")
MAX_SCROLLS = int(os.environ.get("MAX_SCROLLS", "50"))
MAX_SHOTS = int(os.environ.get("MAX_SHOTS", "25"))
SCROLL_PAUSE = 2.0
DETAIL_PAUSE = 3.0
NAV_TIMEOUT = 60_000

# X内部APIのオペレーション名（URLに含まれる文字列で判定）
TIMELINE_OPS = ("HomeTimeline", "UserTweets", "UserMedia")

STATUS_RE = re.compile(r"/status/(\d+)")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(NoRedirect)


def call_api(payload=None, params=""):
    """Call Apps Script. Handles the 302 redirect Apps Script returns by
    re-sending the request (same method/body) to the redirect target."""
    url = APPS_SCRIPT_URL
    data = None
    if payload is not None:
        data = json.dumps({**payload, "token": TOKEN}).encode()
    elif params:
        url += ("&" if "?" in url else "?") + f"token={TOKEN}&" + params

    for _ in range(5):
        req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        if data:
            req.add_header("Content-Type", "text/plain")
        try:
            with _opener.open(req, timeout=120) as res:
                return json.loads(res.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307) and e.headers.get("Location"):
                url = e.headers["Location"]
                # Apps ScriptのPOSTは処理後に302を返す。リダイレクト先は
                # 結果を取りに行くだけなのでGETに切り替える
                data = None
                continue
            raise
    raise RuntimeError("Apps Script redirect limit exceeded")


def iter_tweet_results(obj, out):
    """Recursively collect tweet result dicts from a GraphQL response."""
    if isinstance(obj, dict):
        legacy = obj.get("legacy")
        if isinstance(legacy, dict) and legacy.get("full_text") and obj.get("rest_id"):
            # リポストのラッパーは除外（元ポストは内部で別途拾われるので
            # 参考リンクが常に元ポストのURLになる）
            if "retweeted_status_result" not in legacy:
                out.append(obj)
        for v in obj.values():
            iter_tweet_results(v, out)
    elif isinstance(obj, list):
        for v in obj:
            iter_tweet_results(v, out)


def parse_tweet(tr, fallback_user=""):
    legacy = tr.get("legacy") or {}
    user_results = ((tr.get("core") or {}).get("user_results") or {}).get("result") or {}
    screen_name = (user_results.get("legacy") or {}).get("screen_name") or \
        (user_results.get("core") or {}).get("screen_name") or fallback_user

    media_urls = []
    for m in ((legacy.get("extended_entities") or {}).get("media") or []):
        if m.get("type") == "photo":
            media_urls.append(m.get("media_url_https") or "")
        else:
            variants = [v for v in (m.get("video_info") or {}).get("variants", [])
                        if v.get("content_type") == "video/mp4"]
            if variants:
                media_urls.append(max(variants, key=lambda v: v.get("bitrate", 0))["url"])

    # キーワード判定用: 本文+展開済みURL+カードURLをまとめた文字列
    ext_urls = [u.get("expanded_url") or u.get("url") or ""
                for u in (legacy.get("entities") or {}).get("urls", [])]
    card = json.dumps(tr.get("card") or {})
    haystack = (legacy.get("full_text") or "") + " " + " ".join(ext_urls) + " " + card

    # X公式のセンシティブ判定（メディア・ポスト・投稿者のいずれか）
    media_list = (legacy.get("extended_entities") or {}).get("media") or \
        (legacy.get("entities") or {}).get("media") or []
    sensitive = bool(
        legacy.get("possibly_sensitive") or
        legacy.get("possibly_sensitive_editable") or
        (user_results.get("legacy") or {}).get("possibly_sensitive") or
        any(m.get("possibly_sensitive") for m in media_list)
    )
    haystack = haystack.lower()

    created = legacy.get("created_at") or ""
    try:
        dt = datetime.strptime(created, "%a %b %d %H:%M:%S %z %Y").astimezone(JST)
        date_str = dt.strftime("%Y/%m/%d %H:%M")
        sort_key = dt.timestamp()
    except ValueError:
        date_str, sort_key = created, 0.0

    return {
        "id": tr.get("rest_id") or "",
        "user": screen_name,
        "url": (f"https://x.com/{screen_name}/status/{tr.get('rest_id')}"
                if screen_name else f"https://x.com/i/web/status/{tr.get('rest_id')}"),
        "text": legacy.get("full_text") or "",
        "date": date_str,
        "sort_key": sort_key,
        "media": "\n".join(u for u in media_urls if u),
        "impressions": int((tr.get("views") or {}).get("count") or 0),
        "likes": int(legacy.get("favorite_count") or 0),
        "reposts": int(legacy.get("retweet_count") or 0),
        "replies": int(legacy.get("reply_count") or 0),
        "bookmarks": int(legacy.get("bookmark_count") or 0),
        "haystack": haystack,
        "sensitive": sensitive,
        "lang": legacy.get("lang") or "",
    }


def scrape_timeline(page, url, captured, for_you=False):
    """Open a timeline URL and collect tweet results from GraphQL responses."""
    def on_response(res):
        if any(op in res.url for op in TIMELINE_OPS):
            try:
                captured.append(res.json())
            except Exception:
                pass

    page.on("response", on_response)
    page.goto(url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)

    print(f"  url={page.url} title={page.title()!r}")
    if "/i/flow/login" in page.url:
        page.screenshot(path="debug_login.png")
        raise RuntimeError("Xのログイン画面にリダイレクトされました。state.jsonが無効です。")

    if for_you:
        # 「For you / おすすめ」タブを明示的に選択
        try:
            page.get_by_role("tab", name=re.compile("For you|おすすめ")).first.click(timeout=8000)
            page.wait_for_timeout(3000)
        except Exception:
            pass

    last_height = 0
    for i in range(MAX_SCROLLS):
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(int(SCROLL_PAUSE * 1000))
        height = page.evaluate("document.body.scrollHeight")
        if height == last_height:
            break
        last_height = height

    page.remove_listener("response", on_response)
    print(f"  scrolls={i+1} captured_responses={len(captured)}")


def get_tweet_detail(page, tweet, keywords):
    """Open the tweet permalink; capture TweetDetail JSON and screenshot.
    Also checks the author's own replies for affiliate links."""
    captured = []

    def on_response(res):
        if "TweetDetail" in res.url or "TweetResultByRestId" in res.url:
            try:
                captured.append(res.json())
            except Exception:
                pass

    page.on("response", on_response)
    page.goto(tweet["url"], timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    page.remove_listener("response", on_response)

    all_results = []
    for body in captured:
        iter_tweet_results(body, all_results)

    for tr in all_results:
        if tr.get("rest_id") == tweet["id"]:
            fresh = parse_tweet(tr, tweet["user"])
            tweet.update({k: fresh[k] for k in
                          ("impressions", "likes", "reposts", "replies",
                           "bookmarks", "text", "media", "date", "haystack",
                           "sensitive", "lang")})

    # 投稿者自身の返信にアフィリエイトリンクがあるかチェック
    tweet["reply_link"] = False
    for tr in all_results:
        lg = tr.get("legacy") or {}
        if lg.get("in_reply_to_status_id_str") != tweet["id"]:
            continue
        author = ((tr.get("core") or {}).get("user_results") or {}).get("result") or {}
        author_name = (author.get("legacy") or {}).get("screen_name") or \
            (author.get("core") or {}).get("screen_name")
        if author_name != tweet["user"]:
            continue
        urls = [u.get("expanded_url") or u.get("url") or ""
                for u in (lg.get("entities") or {}).get("urls", [])]
        rh = ((lg.get("full_text") or "") + " " + " ".join(urls)).lower()
        # 返信に外部URLがあり、キーワード指定があればその一致も必須
        if urls and (not keywords or any(k in rh for k in keywords)):
            tweet["reply_link"] = True
            break

    shot = None
    article = page.locator('article[data-testid="tweet"]').first
    try:
        # センシティブメディアの「表示」ボタンを自動クリック
        for btn in page.get_by_role("button", name=re.compile("表示|View")).all():
            try:
                btn.click(timeout=800)
            except Exception:
                pass
        article.scroll_into_view_if_needed(timeout=10_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        shot = article.screenshot(timeout=15_000)
    except Exception as e:
        print(f"  screenshot failed for {tweet['id']}: {e}", file=sys.stderr)
    return shot


def main():
    if not APPS_SCRIPT_URL or not TOKEN:
        sys.exit("APPS_SCRIPT_URL / APPS_SCRIPT_TOKEN が未設定です")

    config = call_api(params="action=config")
    if config.get("error"):
        sys.exit(f"設定の取得に失敗: {config['error']}")

    if not config.get("enabled", True):
        print("設定タブで収集がOFFになっています。終了します。")
        return

    threshold = int(config.get("threshold") or 300000)
    accounts = config.get("accounts") or []
    keywords = [str(k).lower() for k in (config.get("keywords") or [])]
    sensitive_only = bool(config.get("sensitiveOnly", True))
    ja_only = bool(config.get("jaOnly", True))
    existing = set(config.get("existingIds") or [])
    print(f"対象: {accounts} / 閾値: {threshold} / キーワード: {len(keywords)}件"
          f" / センシティブのみ: {sensitive_only} / 日本語のみ: {ja_only}"
          f" / 収集済み: {len(existing)}件")

    def kw_match(t):
        return any(k in t["haystack"] for k in keywords)

    def is_target(t, strict=True):
        if ja_only and t["lang"] != "ja":
            return False
        # 本文/リンクのキーワード一致はそのままアダアフィ判定
        if kw_match(t):
            return True
        if sensitive_only and t["sensitive"]:
            # 厳格モード: 投稿者自身の返信にアフィリエイトリンク必須
            # （TL収集段階では返信未確認なので緩く通し、詳細取得後に厳格判定）
            return (not strict) or bool(t.get("reply_link"))
        return not sensitive_only

    if not accounts:
        print("対象アカウント未指定 → おすすめTLのみ収集します")

    tweets = {}
    headful = os.environ.get("HEADFUL") == "1"
    launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    with sync_playwright() as p:
        try:
            # 本物のChromeを使う（コーデックあり→動画が再生/表示される）
            browser = p.chromium.launch(channel="chrome", headless=not headful,
                                        args=launch_args)
        except Exception:
            browser = p.chromium.launch(headless=not headful, args=launch_args)
        ctx = browser.new_context(
            storage_state=STATE_PATH,
            viewport={"width": 700, "height": 900},
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )
        page = ctx.new_page()

        captured = []
        # おすすめTL（For You）は常に収集
        print("[timeline] For You")
        try:
            scrape_timeline(page, "https://x.com/home", captured, for_you=True)
        except Exception as e:
            print(f"  error: {e}", file=sys.stderr)
        page.wait_for_timeout(2000)

        # 設定タブにアカウント指定があればプロフィールも巡回
        for account in accounts:
            print(f"[timeline] @{account}")
            try:
                scrape_timeline(page, f"https://x.com/{account}", captured)
            except Exception as e:
                print(f"  error: {e}", file=sys.stderr)
            page.wait_for_timeout(2000)

        for body in captured:
            results = []
            iter_tweet_results(body, results)
            for tr in results:
                t = parse_tweet(tr)
                if t["id"] and t["id"] not in tweets:
                    tweets[t["id"]] = t

        if not tweets:
            page.screenshot(path="debug_empty.png")
            Path("debug_empty.html").write_text(page.content())
            print("  0件 → debug_empty.png/html を保存")

        candidates = [t for t in tweets.values()
                      if t["impressions"] >= threshold and t["id"] not in existing
                      and is_target(t, strict=False)]
        candidates.sort(key=lambda t: t["sort_key"])  # 古い順→POSTで新しいのが上に来る
        print(f"発見: {len(tweets)}件 / 閾値以上かつ未収集: {len(candidates)}件")

        sent = 0
        for t in candidates[:MAX_SHOTS]:
            try:
                shot = get_tweet_detail(page, t, keywords)
            except Exception as e:
                print(f"  detail error {t['id']}: {e}", file=sys.stderr)
                shot = None

            if t["impressions"] < threshold:
                print(f"  skip (詳細で閾値未満): {t['url']}")
                continue
            if not is_target(t, strict=True):
                print(f"  skip (アフィリンクなし等): {t['url']}")
                continue

            row = {
                "date": t["date"], "url": t["url"], "text": t["text"],
                "media": t["media"], "impressions": t["impressions"],
                "likes": t["likes"], "reposts": t["reposts"],
                "bookmarks": t["bookmarks"],
            }
            payload = {"row": row}
            if shot:
                payload["imageBase64"] = base64.b64encode(shot).decode()

            try:
                res = call_api(payload=payload)
                if res.get("saved"):
                    print(f"  saved No.{res.get('no')} {t['url']} (imp {t['impressions']:,})")
                    sent += 1
                else:
                    print(f"  save failed {t['id']}: {res.get('error')}", file=sys.stderr)
            except Exception as e:
                print(f"  post error {t['id']}: {e}", file=sys.stderr)

            page.wait_for_timeout(int(DETAIL_PAUSE * 1000))

        browser.close()

    print(f"完了: {sent}件をスプレッドシートに保存")


if __name__ == "__main__":
    main()
