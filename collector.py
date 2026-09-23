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
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

JST = timezone(timedelta(hours=9))

APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "")
TOKEN = os.environ.get("APPS_SCRIPT_TOKEN", "")
STATE_PATH = os.environ.get("X_STATE_PATH", "state.json")
MAX_SCROLLS = int(os.environ.get("MAX_SCROLLS", "300"))
MAX_SHOTS = int(os.environ.get("MAX_SHOTS", "120"))
SCROLL_PAUSE = 2.0
DETAIL_PAUSE = 1.0
NAV_TIMEOUT = 60_000

# X内部APIのオペレーション名（URLに含まれる文字列で判定）
TIMELINE_OPS = ("HomeTimeline", "UserTweets", "UserMedia")

STATUS_RE = re.compile(r"/status/(\d+)")

# 除外ドメイン: 商業AV＋漫画系。短縮/誘導リンクの遷移先に現れたら
# アフィ証拠に数えず、本人の導線にあれば投稿自体を除外
BLOCKED_DOMAINS = ("fanza", "dmm.co.jp", "dmm.com", "mgstage", "sokmil",
                   "duga", "digiket", "dlsite", "pixiv.net", "fanbox.cc",
                   "booth.pm")

# 同人ファンクラブ系プラットフォーム。証拠URLは最終的にこれらへ着地すること
FANCLUB_DOMAINS = ("myfans", "fantia", "onlyfans", "fansly", "fanvue",
                   "candfans", "fc2", "stripchat", "chaturbate", "xfans")

# 遷移先を確認する短縮・誘導・リンク集ドメイン（URL自体は着地とみなさない）
REDIRECT_HOSTS = (
    "x.gd", "is.gd", "v.gd", "bit.ly", "cutt.ly", "tinyurl.", "t.ly",
    "reurl.cc", "rb.gy", "shorturl.", "urx.", "p.tl", "ow.ly",
    "mfco.link", "loknote77", "videy.yt", "videi.in", "ho-zuki", "omg10",
    "lit.link", "linktr.ee", "potofu", "instabio", "bio.site",
    "campsite", "solo.to", "allmylinks", "linkr.")

# 200応答のHTML内リンクまで確認するリンク集ドメイン
AGGREGATOR_HOSTS = ("lit.link", "linktr.ee", "potofu", "instabio",
                    "bio.site", "campsite", "solo.to", "allmylinks", "linkr.")

# 生メディア直リンクのCDN。解決失敗時も証拠にしない（寄生botが貼り回すため）
CDN_MEDIA_HOSTS = ("videy.yt", "videi.in")

_RESOLVE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# URL -> (経由URL一覧, 最終URL, 最終HTML先頭, 応答あり)（実行中のキャッシュ）
_chain_cache = {}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Apps Scriptのリダイレクトはcookieを要求することがあるためCookieJar付き
_opener = urllib.request.build_opener(
    NoRedirect, urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


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
            for _ in range(8):
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

    media_types = [m.get("type") for m in
                   ((legacy.get("extended_entities") or {}).get("media") or [])]
    has_video = any(t in ("video", "animated_gif") for t in media_types)

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
    # カード説明文はURL証拠から除外（リンク先ページの宣伝文にfantia等が
    # 書かれているだけで誤検出するため）
    url_haystack = " ".join(ext_urls).lower()
    haystack = (legacy.get("full_text") or "") + " " + " ".join(ext_urls)

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
        "verified": bool(user_results.get("is_blue_verified") or
                         (user_results.get("verification") or {}).get("verified")),
        "has_video": has_video,
        "photo_urls": [m.get("media_url_https") or "" for m in media_list
                       if m.get("type") == "photo"],
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

    # XのTLは仮想化リスト（スクロールで上のポストが消える）なので
    # scrollHeightだけでは判定できない。「高さが伸びない」＋「新規
    # GraphQLレスポンスが来ない」が続いたら底 or レート制限とみなす。
    last_height = 0
    stale = 0
    for i in range(MAX_SCROLLS):
        before = len(captured)
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(int(SCROLL_PAUSE * 1000))
        height = page.evaluate("document.body.scrollHeight")
        if height == last_height and len(captured) == before:
            stale += 1
            if stale >= 5:
                print(f"  {stale}回連続で新規なし → 底/レート制限と判断")
                break
            page.wait_for_timeout(2500)  # 追加ロードをもう少し待つ
        else:
            stale = 0
        last_height = height

    page.remove_listener("response", on_response)
    print(f"  scrolls={i+1} captured_responses={len(captured)}")


def _resolve_chain(url, max_hops=4):
    """リダイレクトチェインをたどる。リンク集は最終HTMLも取得。
    returns (経由URL一覧, 最終URL, HTML先頭, HTTP応答があったか)"""
    if url in _chain_cache:
        return _chain_cache[url]
    hops = []
    cur = url
    html = ""
    responded = False
    for _ in range(max_hops):
        try:
            req = urllib.request.Request(cur, headers={"User-Agent": _RESOLVE_UA})
            with _opener.open(req, timeout=10) as res:
                responded = True
                ct = (res.headers.get("Content-Type") or "").lower()
                host = urllib.parse.urlparse(cur).netloc.lower()
                if "text/html" in ct and any(a in host for a in AGGREGATOR_HOSTS):
                    html = res.read(200_000).decode("utf-8", "ignore").lower()
                break
        except urllib.error.HTTPError as e:
            responded = True
            loc = e.headers.get("Location") \
                if e.code in (301, 302, 303, 307, 308) else None
            if not loc:
                break
            cur = urllib.parse.urljoin(cur, loc)
            hops.append(cur)
        except Exception:
            break
    _chain_cache[url] = (hops, cur, html, responded)
    return _chain_cache[url]


def _aff_url_level(url, keywords, resolve=True):
    """URLの同人アフィ証拠強度:
    2=ファンクラブ系に着地確認 / 1=キーワードドメイン一致（着地未確認）/
    0=非証拠 / -1=除外ドメイン（商業AV・漫画系）着地"""
    lu = url.lower()
    if any(d in lu for d in BLOCKED_DOMAINS):
        return -1
    if any(d in lu for d in FANCLUB_DOMAINS):
        return 2
    host = urllib.parse.urlparse(url).netloc.lower()
    if any(h in host for h in REDIRECT_HOSTS):
        if not resolve:
            # 解決枠切れ: キーワード一致のみ弱証拠
            return 1 if any(k in lu for k in keywords) else 0
        hops, final, html, ok = _resolve_chain(url)
        if not ok:
            if any(h in host for h in CDN_MEDIA_HOSTS):
                return 0
            return 1 if any(k in lu for k in keywords) else 0
        chain = " ".join(hops + [final]).lower() + " " + html
        if any(d in chain for d in BLOCKED_DOMAINS):
            return -1
        if any(d in chain for d in FANCLUB_DOMAINS):
            return 2
        # 生メディア直リンク（mp4等）は証拠にしない
        if re.search(r"\.(mp4|m3u8|webm|mov|ts|jpg|png)([?/#]|$)", final.lower()):
            return 0
        return 0  # 着地が同人系でない（寄生CDN等）は証拠にしない
    if any(k in lu for k in keywords):
        return 1
    return 0


def _image_is_manga(url):
    """画像URLを取得して漫画/イラストっぽいかピクセル解析で判定。
    白黒漫画ページ: 彩度≈0で色数が極端に少ない or 白地率が高い
    カラーイラスト: ベタ塗り率（隣接同色）が高く輪郭線が密
    写真: 色数・中間調が多くベタ塗り率が低い（センサーノイズ由来）"""
    try:
        import io
        from PIL import Image, ImageFilter
        u = url + ("&" if "?" in url else "?") + "name=small"
        req = urllib.request.Request(u, headers={"User-Agent": _RESOLVE_UA})
        with urllib.request.urlopen(req, timeout=15) as res:
            img = Image.open(io.BytesIO(res.read())).convert("RGB")
        img.thumbnail((240, 240))
        w, h = img.size
        n = w * h
        px = img.load()
        flat = white = sat_sum = 0
        colors = set()
        for y in range(h):
            for x in range(w):
                r, g, b = px[x, y]
                colors.add((r >> 3, g >> 3, b >> 3))
                sat_sum += max(r, g, b) - min(r, g, b)
                if r > 235 and g > 235 and b > 235:
                    white += 1
                if x < w - 1:
                    r2, g2, b2 = px[x + 1, y]
                    if abs(r - r2) < 8 and abs(g - g2) < 8 and abs(b - b2) < 8:
                        flat += 1
        sat = sat_sum / (255 * n)
        white /= n
        flat /= n
        edge = sum(1 for v in img.convert("L").filter(
            ImageFilter.FIND_EDGES).getdata() if v > 80) / n
        if sat <= 0.08 and (len(colors) <= 100 or white >= 0.35):
            return True
        return flat >= 0.50 and edge >= 0.18
    except Exception:
        return False


def _author_name(tr):
    u = ((tr.get("core") or {}).get("user_results") or {}).get("result") or {}
    return (u.get("legacy") or {}).get("screen_name") or \
        (u.get("core") or {}).get("screen_name")


def _post_urls(lg):
    urls = [u.get("expanded_url") or u.get("url") or ""
            for u in (lg.get("entities") or {}).get("urls", [])]
    qs = (lg.get("quoted_status_permalink") or {}).get("expanded")
    if qs:
        urls.append(qs)
    return urls


def _reply_scan(all_results, tweet_id, user, keywords):
    """会話スレッド内のポストを走査（直接返信だけでなくネストも含む）
    returns: (キーワードURL返信あり, 本人外部リンク返信あり,
              他人リングURL一覧, 本人リングURL一覧, 本人の商業AVリンクあり)"""
    kw_reply = self_link = author_blocked = False
    ring_urls = []
    self_ring = []
    budget = [8]  # 短縮URLの遷移先解決は最大8回/走査

    # 会話メンバーに限定（レスポンス内のおすすめ・広告など無関係ポストを除外）
    by_id = {tr.get("rest_id"): tr for tr in all_results}
    conv = {tweet_id}
    cur = by_id.get(tweet_id)
    while cur:  # 祖先チェーン
        pid = (cur.get("legacy") or {}).get("in_reply_to_status_id_str")
        if not pid or pid in conv:
            break
        conv.add(pid)
        cur = by_id.get(pid)
    changed = True
    while changed:  # focalへの返信＋ネスト返信を推移的に追加
        changed = False
        for tr in all_results:
            rid = tr.get("rest_id")
            if rid and rid not in conv:
                pid = (tr.get("legacy") or {}).get("in_reply_to_status_id_str")
                if pid in conv:
                    conv.add(rid)
                    changed = True

    for tr in all_results:
        lg = tr.get("legacy") or {}
        rid = tr.get("rest_id")
        if rid == tweet_id or rid not in conv:
            continue
        urls = _post_urls(lg)
        if not urls:
            continue
        name = _author_name(tr)
        # 各URLのアフィ証拠強度を判定（短縮は遷移先まで確認）
        ev_hit = False
        for u in urls:
            host = urllib.parse.urlparse(u).netloc.lower()
            resolve = budget[0] > 0 and \
                any(h in host for h in REDIRECT_HOSTS)
            if resolve:
                budget[0] -= 1
            lv = _aff_url_level(u, keywords, resolve=resolve)
            if lv < 0:
                # 本人ポストの商業AV/漫画リンクは投稿自体の除外理由
                if name == user:
                    author_blocked = True
                continue
            if lv > 0:
                ev_hit = True
        if name == user:
            for u in urls:
                if re.search(r"(?:x\.com|twitter\.com)/[^/]+/status/\d+", u):
                    self_ring.append(u)
            # 本人返信もURL証拠必須（外部URLのみ。x.comリンクはリング扱い）
            if ev_hit and any("x.com" not in u and "twitter.com" not in u
                              for u in urls):
                self_link = True
        else:
            for u in urls:
                if re.search(r"(?:x\.com|twitter\.com)/[^/]+/status/\d+", u) \
                        and f"/{user}/" not in u:
                    ring_urls.append(u)
        if ev_hit:
            kw_reply = True
    return kw_reply, self_link, ring_urls, self_ring, author_blocked


def _fetch_status_thread(page, url):
    """x.comポストURLを開きTweetDetailを捕捉。returns (user, id, tweet_results)"""
    m = re.search(r"(?:x\.com|twitter\.com)/([^/]+)/status/(\d+)", url)
    if not m:
        return None, None, []
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
        page.goto(url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
    except Exception:
        pass
    page.remove_listener("response", on_response)

    results = []
    for body in captured:
        iter_tweet_results(body, results)
    return duser, did, results


def _thread_has_commercial(results, author=None):
    """スレッド内のURLに除外ドメインがあればTrue（短縮は遷移先も確認）。
    author指定時はその作者のポストのみ対象（第三者スパムは除外根拠にしない）。"""
    budget = [6]
    for tr in results:
        if author and _author_name(tr) != author:
            continue
        for u in _post_urls(tr.get("legacy") or {}):
            if any(d in u.lower() for d in BLOCKED_DOMAINS):
                return True
            host = urllib.parse.urlparse(u).netloc.lower()
            if budget[0] > 0 and any(h in host for h in REDIRECT_HOSTS):
                budget[0] -= 1
                hops, final, html, _ = _resolve_chain(u)
                chain = " ".join(hops + [final]).lower() + " " + html
                if any(d in chain for d in BLOCKED_DOMAINS):
                    return True
    return False


def _self_ring_scan(page, ring_urls, keywords):
    """本人返信のx.comリンク先を1段掘る。
    アフィ証拠はリンク先ポスト本文のURLのみ（リンク先の返信欄まで掘ると
    バズ→別垢バズ→返信アフィの多段宣伝チェインを拾ってしまう）。
    商業AV除外はリンク先作者のポスト全体を対象。
    returns: (リンク先スレッドに商業AVあり, リンク先ポスト本文にアフィURLあり)"""
    commercial = affiliate = False
    for u in list(dict.fromkeys(ring_urls))[:2]:
        duser, did, results = _fetch_status_thread(page, u)
        if not results:
            continue
        if _thread_has_commercial(results, duser):
            commercial = True
        dest = next((tr for tr in results if tr.get("rest_id") == did), None)
        if dest and any(_aff_url_level(u2, keywords) >= 1
                        for u2 in _post_urls(dest.get("legacy") or {})):
            affiliate = True
    return commercial, affiliate


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
    page.wait_for_timeout(2500)

    # 「さらに返信を表示」系の折りたたみを展開（スパム判定返信にアフィリンクが多い）
    try:
        for _ in range(2):
            btns = page.get_by_text(
                re.compile("返信をさらに表示|Show more replies")).all()
            if not btns:
                break
            for b in btns:
                try:
                    b.click(timeout=1500)
                except Exception:
                    pass
            page.wait_for_timeout(1500)
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(1000)
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
                           "url_haystack", "sensitive", "lang", "verified",
                           "has_video")})
            # 本文リンクの証拠判定（短縮は遷移先まで確認。カード説明文は対象外）
            murls = _post_urls(tr.get("legacy") or {})
            mlv = [_aff_url_level(u, keywords) for u in murls]
            if any(v < 0 for v in mlv):
                tweet["commercial"] = True
            tweet["own_link_ok"] = any(v > 0 for v in mlv)

    # 返信欄のアフィリエイト構造をチェック
    # 証拠は「ファンクラブ系ドメインへの着地が確認できるURL」のみ
    # （寄生botが貼るvidei.in等のCDN直リンクは着地先が非ファンクラブなので弾く）
    # reply_link: 会話内返信のアフィURL、またはリング先ポストのアフィ構造
    # self_reply_link: 本人返信のアフィ外部URL
    # commercial: 本人ポスト/本人リング先に商業AV・漫画系リンク → 収集しない
    kw_reply, self_link, _ring, self_ring, author_blocked = _reply_scan(
        all_results, tweet["id"], tweet["user"], keywords)
    tweet["reply_link"] = kw_reply
    tweet["self_reply_link"] = self_link
    if author_blocked:
        tweet["commercial"] = True

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
            page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass
        page.wait_for_timeout(1000)
        shot = article.screenshot(timeout=15_000)
    except Exception as e:
        print(f"  screenshot failed for {tweet['id']}: {e}", file=sys.stderr)

    # 本人返信のx.comリンク先を1段掘る（商業AV着地なら除外、
    # リンク先ポスト本文にアフィURLがあれば証拠）。
    # 他人返信のリングは掘らない（寄生虫がバズ投稿に宣伝リンクを
    # 貼るだけなのでフォーカルポストの証拠にならない）
    if self_ring:
        commercial, aff = _self_ring_scan(page, self_ring, keywords)
        if commercial:
            tweet["commercial"] = True
        tweet["reply_link"] = tweet["reply_link"] or aff
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

    # 商業宣伝っぽい本文パターン（同人AVの釣り文には出ない表現）
    PROMO_RE = re.compile(
        r"発売中|配信開始|新作発売|サンプル動画|【[^】]{10,}】|予約受付|セール中"
    )
    # 漫画系ポストの本文パターン（「（1/4）」等の分割ページ表記含む）
    MANGA_RE = re.compile(
        r"漫画|コミック|単行本|試し読み|電子書籍|成年向け|DLsite|FANZA同人"
        r"|マンガ|同人誌|CG集|エロゲ|（\s*\d+\s*/\s*\d+\s*）|\(\s*\d+\s*/\s*\d+\s*\)"
    )

    def is_target(t, strict=True):
        if ja_only and t["lang"] != "ja":
            return False
        if t.get("verified"):
            return False  # 公式マーク付きアカウントは対象外（使い捨て垢のみ）
        if t.get("commercial"):
            return False  # 本人のリンク先が商業AV（FANZA/DMM/MGS等）
        if sensitive_only and PROMO_RE.search(t["text"]):
            return False  # 商業宣伝文パターンは対象外
        if sensitive_only and MANGA_RE.search(t["text"]):
            return False  # 漫画系ポストは対象外
        if not sensitive_only:
            # キーワードフィルタのみ運用（キーワード空なら全件）
            return not keywords or kw_match(t)
        # アダアフィ厳格モード:
        # TL段階は返信未確認なので全通し。詳細取得後に
        # 「返信欄のアフィリンク構造」or「本文リンクがアフィドメイン一致」のみ採用
        if not strict:
            return True
        return bool(t.get("reply_link") or t.get("self_reply_link") or
                    t.get("own_link_ok",
                          any(k in t["url_haystack"] for k in keywords)))

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

            # 画像のみポストは中身を解析して漫画/イラストを弾く
            if not t.get("has_video") and t.get("photo_urls"):
                shots = t["photo_urls"][:4]
                if sum(_image_is_manga(u) for u in shots) * 2 >= len(shots):
                    print(f"  skip (漫画/イラスト画像): {t['url']}")
                    continue

            row = {
                "date": datetime.now(JST).strftime("%Y/%m/%d %H:%M"),
                "url": t["url"], "text": t["text"],
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
