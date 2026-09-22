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
    for attempt in range(4):
        url = APPS_SCRIPT_URL
        data = None
        if payload is not None:
            data = json.dumps({**payload, "token": TOKEN}).encode()
        elif params:
            url += ("&" if "?" in url else "?") + f"token={TOKEN}&" + params

        try:
            for _ in range(5):
                req = urllib.request.Request(
                    url, data=data, method="POST" if data else "GET")
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
        except (urllib.error.HTTPError, urllib.error.URLError):
            if attempt < 3:
                time.sleep(15 * (attempt + 1))
                continue
            raise


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
    url_haystack = (" ".join(ext_urls) + " " + card).lower()
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
        "url_haystack": url_haystack,
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


def _reply_scan(all_results, tweet_id, user, keywords):
    """会話スレッド内のポストを走査（直接返信だけでなくネストも含む）
    returns: (キーワードURL返信あり, 本人外部リンク返信あり, リングURL一覧)"""
    kw_reply = self_link = False
    ring_urls = []
    for tr in all_results:
        lg = tr.get("legacy") or {}
        if tr.get("rest_id") == tweet_id:
            continue
        urls = [u.get("expanded_url") or u.get("url") or ""
                for u in (lg.get("entities") or {}).get("urls", [])]
        qs = (lg.get("quoted_status_permalink") or {}).get("expanded")
        if qs:
            urls.append(qs)
        if not urls:
            continue
        # キーワード一致はURLのみで判定（本文ワードでの誤検出を防ぐ）
        uh = " ".join(urls).lower()
        kw_hit = any(k in uh for k in keywords)
        author = ((tr.get("core") or {}).get("user_results") or {}).get("result") or {}
        name = (author.get("legacy") or {}).get("screen_name") or \
            (author.get("core") or {}).get("screen_name")
        if kw_hit:
            kw_reply = True
        if name == user:
            # 本人返信もキーワード一致必須（商業AV等を除外するため）
            if any("x.com" not in u and "twitter.com" not in u for u in urls) \
                    and kw_hit:
                self_link = True
        else:
            for u in urls:
                if re.search(r"(?:x\.com|twitter\.com)/[^/]+/status/\d+", u) \
                        and f"/{user}/" not in u:
                    ring_urls.append(u)
    return kw_reply, self_link, ring_urls


def _dest_is_affiliate(page, ring_urls, keywords):
    """リング返信のリンク先ポストを1段掘り、アフィリンク構造があるか確認。"""
    for u in ring_urls[:3]:
        m = re.search(r"(?:x\.com|twitter\.com)/([^/]+)/status/(\d+)", u)
        if not m:
            continue
        duser, did = m.group(1), m.group(2)
        captured = []

        def on_response(res):
            if "TweetDetail" in res.url or "TweetResultByRestId" in res.url:
                try:
                    captured.append(res.json())
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            page.goto(u, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
        except Exception:
            pass
        page.remove_listener("response", on_response)

        results = []
        for body in captured:
            iter_tweet_results(body, results)
        main = next((t for t in results if t.get("rest_id") == did), None)
        if not main:
            continue
        lg = main.get("legacy") or {}
        dest_urls = [u2.get("expanded_url") or u2.get("url") or ""
                     for u2 in (lg.get("entities") or {}).get("urls", [])]
        qs = (lg.get("quoted_status_permalink") or {}).get("expanded")
        if qs:
            dest_urls.append(qs)
        dh = " ".join(dest_urls).lower()
        if any(k in dh for k in keywords):
            return True
        kw, selfl, _ = _reply_scan(results, did, duser, keywords)
        if kw or selfl:
            return True
    return False


def get_tweet_detail(page, tweet, keywords):
    """Open the tweet permalink; capture TweetDetail JSON and screenshot.
    Also checks the reply section for affiliate links."""
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

    # 「さらに返信を表示」系の折りたたみを展開（スパム判定返信にアフィリンクが多い）
    try:
        for _ in range(3):
            btns = page.get_by_text(
                re.compile("返信をさらに表示|Show more replies")).all()
            if not btns:
                break
            for b in btns:
                try:
                    b.click(timeout=1500)
                except Exception:
                    pass
            page.wait_for_timeout(2500)
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(1500)
    except Exception:
        pass

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
                           "url_haystack", "sensitive", "lang")})

    # 返信欄のアフィリエイト構造をチェック
    # reply_link: 返信のキーワード一致URL、またはリング先ポストにアフィリンク
    # self_reply_link: 投稿者本人の返信にx.com以外の外部URL（bit.ly等の短縮含む）
    kw_reply, self_link, ring_urls = _reply_scan(
        all_results, tweet["id"], tweet["user"], keywords)
    tweet["reply_link"] = kw_reply
    tweet["self_reply_link"] = self_link

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

    # スクショ後にリング先を1段掘ってアフィリンク構造を確認
    if not tweet["reply_link"] and ring_urls:
        tweet["reply_link"] = _dest_is_affiliate(page, ring_urls, keywords)
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
        if not sensitive_only:
            # キーワードフィルタのみ運用（キーワード空なら全件）
            return not keywords or kw_match(t)
        # アダアフィ厳格モード:
        # TL段階は返信未確認なので全通し。詳細取得後に
        # 「返信欄のアフィリンク構造」or「本文リンクがアフィドメイン一致」のみ採用
        if not strict:
            return True
        return bool(t.get("reply_link") or t.get("self_reply_link") or
                    any(k in t["url_haystack"] for k in keywords))

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
        # センシティブ付きを優先（アダアフィ率が高い）、その中では古い順
        candidates.sort(key=lambda t: (not t["sensitive"], t["sort_key"]))
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
