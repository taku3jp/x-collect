"""スプシ行の各ポストを開き、返信/本文のアフィリンク着地が
BLOCKED_TARGETS（ジュキヤ系等）ならそのstatus idを列挙する監査スクリプト。
結果は /tmp/jukiya_hits.txt に追記していく。"""
import sys
sys.path.insert(0, "/Users/takumi/Documents/ChatGPT/X収集")
import collector
from playwright.sync_api import sync_playwright

ids = open("/tmp/idlist.txt").read().split()
hits = open("/tmp/jukiya_hits.txt", "a")

def url_blocked(u, keywords):
    lv = collector._aff_url_level(u, keywords)
    return lv < 0

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
    ctx = browser.new_context(storage_state="/Users/takumi/Documents/ChatGPT/X収集/state.json",
                              viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.goto("https://x.com/home", timeout=60000, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    keywords = list(collector.FANCLUB_DOMAINS)
    for i, pid in enumerate(ids):
        url = f"https://x.com/i/web/status/{pid}"
        captured = []

        def on_response(res):
            if "TweetDetail" in res.url or "TweetResultByRestId" in res.url:
                try:
                    captured.append(res.json())
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            for _ in range(3):
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(1500)
        except Exception as e:
            print(f"[{i}] {pid} nav err {e}", flush=True)
        page.remove_listener("response", on_response)

        results = []
        for b in captured:
            collector.iter_tweet_results(b, results)
        focal = next((tr for tr in results if tr.get("rest_id") == pid), None)
        fuser = collector._author_name(focal) if focal else None

        blocked = False
        hit_url = hit_who = ""
        seen = []
        for tr in results:
            who = "author" if collector._author_name(tr) == fuser else "other"
            for u in collector._post_urls(tr.get("legacy") or {}):
                if "x.com" in u or "twitter.com" in u:
                    continue
                if u in seen:
                    continue
                seen.append(u)
                lv = collector._aff_url_level(u, keywords)
                if lv < 0:
                    blocked, hit_url, hit_who = True, u, who
                    break
            if blocked:
                break
        if blocked:
            hits.write(f"{pid}\t{hit_url}\t{hit_who}\n")
            hits.flush()
            print(f"[{i}] {pid} BLOCKED via {hit_url} ({hit_who})", flush=True)
        print(f"[{i}/{len(ids)}] {pid} {'HIT' if blocked else 'ok'} ({len(results)} results)", flush=True)
    browser.close()
print("DONE")
