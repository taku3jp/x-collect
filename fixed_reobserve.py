#!/usr/bin/env python3
"""Fixed public-post re-observation (candidate).

Re-observes a fixed manifest of public X post URLs using the existing
Playwright browser stack and login state, and records only the public
metric counters of the requested post IDs. New-discovery collection,
spreadsheet/GAS access and auth handling are untouched.

Import-safe: this module uses only the standard library at import time.
Playwright is imported lazily inside the browser capture path, so the
pure manifest/row logic can be tested without playwright or network.

Constraints (see docs/fixed-reobservation.md):
- manifest version 1, strict validation; >100 URLs are rejected, never
  silently truncated
- only public metric fields leave this process: post text, media, raw
  responses, session material and response-URL query strings are never
  written to logs or artifacts
- wall-clock interval and monotonic duration are measured, but this
  candidate cannot bound clock error, so rows keep
  timeBasis "unknown" / uncertaintyMs null
"""

import argparse
import contextlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

MANIFEST_VERSION = 1
MAX_POSTS = 100
MAX_WINDOW = timedelta(hours=72)
OUTPUT_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB
NAV_TIMEOUT = 60_000
DETAIL_WAIT_MS = 2500

SOURCE = "taku3jp/x-collect-fixed"
METHOD = "x-collect-fixed-browser"
PARSER_VERSION = "x-collect-fixed-browser-1"

# Public read responses captured while browsing the fixed URL.
GRAPHQL_OPS = ("TweetDetail", "TweetResultByRestId")
BLOCKED_STATUSES = {401: "http_401", 403: "http_403", 429: "rate_limited"}

ALLOWED_HOSTS = {"x.com", "twitter.com", "www.x.com", "www.twitter.com"}
STATUS_PATH_RE = re.compile(r"^/([A-Za-z0-9_]{1,15})/status/(\d{1,19})/?$")
PANEL_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
MANIFEST_KEYS = frozenset(
    {"version", "panelId", "startAt", "expiresAt", "posts"})

_MAX_STATUS_ID = 2 ** 63 - 1

_CHRONY_REFERENCE = "https://chrony-project.org/doc/4.8/chronyc.html"
_NUM_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")
_MISSING = object()


class ManifestError(Exception):
    """Strict manifest validation failure. `code` is a safe fixed string."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class ObservationError(Exception):
    """Raw response data could not be read strictly. `code` is fixed."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class OutputError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- manifest

def _parse_utc(value, field):
    if not isinstance(value, str):
        raise ManifestError(f"{field}_not_string")
    if value.endswith("Z"):
        iso = value[:-1] + "+00:00"
    elif value.endswith("+00:00"):
        iso = value
    else:
        raise ManifestError(f"{field}_not_utc")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        raise ManifestError(f"{field}_invalid")
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise ManifestError(f"{field}_not_utc")
    return dt.astimezone(timezone.utc)


def _canonical_post(url):
    """Validate a public canonical status URL.

    Returns {"postId", "owner", "url"} with url normalized to
    https://x.com/<owner>/status/<id>. Raises ManifestError otherwise.
    """
    if not isinstance(url, str):
        raise ManifestError("post_not_string")
    try:
        p = urllib.parse.urlsplit(url)
        port = p.port  # may raise ValueError on garbage
    except ValueError:
        raise ManifestError("url_invalid")
    if p.scheme != "https":
        raise ManifestError("url_scheme")
    if p.username is not None or p.password is not None:
        raise ManifestError("url_credentials")
    host = (p.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ManifestError("url_host")
    if port not in (None, 443):
        raise ManifestError("url_port")
    if p.query or p.fragment:
        raise ManifestError("url_not_canonical")
    m = STATUS_PATH_RE.match(p.path)
    if not m:
        raise ManifestError("url_path")
    owner, pid = m.group(1), m.group(2)
    if not (1 <= int(pid) <= _MAX_STATUS_ID):
        raise ManifestError("post_id")
    return {"postId": pid, "owner": owner,
            "url": f"https://x.com/{owner}/status/{pid}"}


def load_manifest(text):
    """Parse and strictly validate a version-1 manifest.

    Shape: {"version":1, "panelId":<slug>, "startAt":<utc>,
            "expiresAt":<utc>, "posts":[<canonical url>, ...]}
    Unknown keys (including private-looking ones), windows >72h,
    non-canonical/credential/arbitrary-host URLs, duplicate or
    conflicting post IDs and >100 posts are all rejected.
    """
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        raise ManifestError("manifest_not_json")
    if not isinstance(obj, dict):
        raise ManifestError("manifest_not_object")
    if set(obj) - MANIFEST_KEYS:
        raise ManifestError("manifest_unknown_key")
    if MANIFEST_KEYS - set(obj):
        raise ManifestError("manifest_missing_key")
    if isinstance(obj["version"], bool) \
            or obj["version"] != MANIFEST_VERSION:
        raise ManifestError("manifest_version")
    panel = obj["panelId"]
    if not isinstance(panel, str) or not PANEL_ID_RE.match(panel):
        raise ManifestError("panel_id")
    start = _parse_utc(obj["startAt"], "startAt")
    end = _parse_utc(obj["expiresAt"], "expiresAt")
    if end <= start:
        raise ManifestError("window_order")
    if end - start > MAX_WINDOW:
        raise ManifestError("window_too_long")
    posts_raw = obj["posts"]
    if not isinstance(posts_raw, list):
        raise ManifestError("posts_not_array")
    if len(posts_raw) > MAX_POSTS:
        raise ManifestError("posts_too_many")
    posts = []
    seen = {}
    for u in posts_raw:
        post = _canonical_post(u)
        if post["postId"] in seen:
            if seen[post["postId"]] == post["url"]:
                raise ManifestError("post_duplicate")
            raise ManifestError("post_conflict")
        seen[post["postId"]] = post["url"]
        posts.append(post)
    return {"panelId": panel, "startAt": start, "expiresAt": end,
            "posts": posts}


def check_window(manifest, now):
    """Reject runs outside the declared UTC window."""
    if now < manifest["startAt"]:
        return "before_start"
    if now > manifest["expiresAt"]:
        return "expired"
    return None


# ------------------------------------------------------- response reading

def _iter_tweet_results(obj, out):
    """Same traversal as collector.iter_tweet_results: collect tweet
    result dicts (legacy.full_text + rest_id, retweet wrappers excluded)."""
    if isinstance(obj, dict):
        legacy = obj.get("legacy")
        if isinstance(legacy, dict) and legacy.get("full_text") \
                and obj.get("rest_id"):
            if "retweeted_status_result" not in legacy:
                out.append(obj)
        for v in obj.values():
            _iter_tweet_results(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _iter_tweet_results(v, out)


def select_tweet_result(bodies, post_id):
    """First tweet result whose rest_id is exactly the requested ID.

    Replies and other conversation members are never substituted.
    The same ID in multiple responses is not double-counted: the first
    match wins.
    """
    for body in bodies:
        results = []
        _iter_tweet_results(body, results)
        for tr in results:
            if tr.get("rest_id") == post_id:
                return tr
    return None


def _dig(obj, *keys):
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return _MISSING
        cur = cur.get(k, _MISSING)
        if cur is _MISSING:
            return _MISSING
    return cur


def _strict_int(v):
    """missing -> (None, True); int/numeric-str -> (value, True); else
    (None, False). Unlike normalize_tweet, missing never becomes 0."""
    if v is _MISSING:
        return None, True
    if isinstance(v, bool):
        return None, False
    if isinstance(v, int):
        return (v if v >= 0 else None), v >= 0
    if isinstance(v, str) and re.fullmatch(r"\d+", v):
        return int(v), True
    return None, False


def extract_observation(tr, post):
    """Strictly read public fields from one tweet result.

    Only author stable ID, display handle, posted time and the six public
    counters are extracted. Text, media and everything else is dropped.
    """
    views = tr.get("views")
    if views is not None and not isinstance(views, dict):
        raise ObservationError("invalid_metrics")
    raw = {
        "impressions": _dig(tr, "views", "count"),
        "likes": _dig(tr, "legacy", "favorite_count"),
        "replies": _dig(tr, "legacy", "reply_count"),
        "reposts": _dig(tr, "legacy", "retweet_count"),
        "quotes": _dig(tr, "legacy", "quote_count"),
        "bookmarks": _dig(tr, "legacy", "bookmark_count"),
    }
    out = {}
    for name, v in raw.items():
        val, ok = _strict_int(v)
        if not ok:
            raise ObservationError("invalid_metrics")
        out[name] = val

    legacy = tr.get("legacy") or {}
    user = _dig(tr, "core", "user_results", "result")
    user = user if isinstance(user, dict) else {}
    ulegacy = user.get("legacy") if isinstance(user.get("legacy"), dict) \
        else {}
    ucore = user.get("core") if isinstance(user.get("core"), dict) else {}
    author_id = user.get("rest_id")
    out["authorId"] = author_id if isinstance(author_id, str) else None
    handle = ulegacy.get("screen_name") or ucore.get("screen_name")
    out["authorHandle"] = handle if isinstance(handle, str) \
        else post["owner"]
    posted_at = None
    created = legacy.get("created_at")
    if isinstance(created, str) and created:
        try:
            posted_at = _iso(datetime.strptime(
                created, "%a %b %d %H:%M:%S %z %Y"))
        except ValueError:
            posted_at = None
    out["postedAt"] = posted_at
    return out


# --------------------------------------------------------------- gating

def _gate_reason(page_url, captured, body_text=""):
    """Classify a blocked condition from observed signals only.

    Response URLs / bodies are never returned; only fixed reason codes.
    """
    for c in captured:
        reason = BLOCKED_STATUSES.get(c.get("status"))
        if reason:
            return reason
    url = page_url or ""
    if "/i/flow/login" in url:
        return "login_required"
    if "/i/flow" in url or "challenge" in url:
        return "challenge"
    text = (body_text or "").lower()
    if "age-restricted" in text or "年齢制限" in text:
        return "age_gate"
    return None


# -------------------------------------------------------------- observe

def _base_row(post, run_key):
    return {
        "postId": post["postId"],
        "resendKey": f"{run_key}:{post['postId']}",
        "url": post["url"],
        "authorId": None,
        "authorHandle": None,
        "postedAt": None,
        "capturedAt": None,
        "captureStartedAt": None,
        "captureFinishedAt": None,
        "monotonicElapsedMs": None,
        "status": "pending",
        "reason": None,
        "impressions": None,
        "likes": None,
        "replies": None,
        "reposts": None,
        "quotes": None,
        "bookmarks": None,
        # This candidate cannot bound wall-clock error; it never
        # self-promotes on GitHub run metadata or caller claims.
        "timeBasis": "unknown",
        "uncertaintyMs": None,
    }


def observe_posts(posts, fetch_one, run_key, now=_utcnow,
                  mono=time.monotonic, expires_at=None, out=None):
    """Run the fetch loop. `fetch_one(post)` returns a dict with keys:
    blocked (reason str|None), captured ([{status, body, json_ok}]),
    error (fixed code, optional). Exception text is never propagated.
    When `out` is given, rows are appended to it as they complete so a
    caller can keep partial rows even if the loop aborts.
    """
    rows = out if out is not None else []
    stop = None  # "blocked" or "error"
    for post in posts:
        if stop:
            row = _base_row(post, run_key)
            row["status"] = stop
            row["reason"] = "not_attempted"
            rows.append(row)
            continue
        if expires_at is not None and now() > expires_at:
            row = _base_row(post, run_key)
            row["status"] = "error"
            row["reason"] = "expired"
            rows.append(row)
            stop = "error"
            continue
        w0, m0 = now(), mono()
        try:
            outcome = fetch_one(post) or {}
        except Exception:
            outcome = {"error": "transport_error"}
        w1, m1 = now(), mono()
        if m1 < m0 or w1 < w0:
            row = _base_row(post, run_key)
            row["status"] = "error"
            row["reason"] = "clock_invalid"
            rows.append(row)
            stop = "error"
            continue

        row = _base_row(post, run_key)
        row["captureStartedAt"] = _iso(w0)
        row["captureFinishedAt"] = _iso(w1)
        row["capturedAt"] = _iso(w1)
        row["monotonicElapsedMs"] = int(round((m1 - m0) * 1000))

        blocked = outcome.get("blocked")
        if blocked:
            row["status"] = "blocked"
            row["reason"] = blocked
            rows.append(row)
            stop = "blocked"
            continue
        if outcome.get("error"):
            row["status"] = "error"
            row["reason"] = outcome["error"]
            rows.append(row)
            continue
        captured = outcome.get("captured") or []
        bodies = [c["body"] for c in captured if c.get("json_ok")]
        if captured and not bodies:
            row["status"], row["reason"] = "error", "corrupt_json"
        elif not bodies:
            row["status"], row["reason"] = "error", "no_response"
        else:
            tr = select_tweet_result(bodies, post["postId"])
            if tr is None:
                row["status"] = "unavailable"
                row["reason"] = "post_not_in_response"
            else:
                try:
                    obs = extract_observation(tr, post)
                except ObservationError as e:
                    row["status"], row["reason"] = "error", e.code
                else:
                    row.update(obs)
                    row["status"] = "ok"
        rows.append(row)
    return rows


# ---------------------------------------------------------- clock (chrony)

def _field_float(fields, name):
    raw = fields.get(name)
    if raw is None:
        return None
    m = _NUM_RE.match(raw)
    if not m:
        return None
    v = float(m.group(1))
    return v if math.isfinite(v) else None


def parse_chronyc_tracking(text):
    """Parse read-only `chronyc -n tracking` output.

    Returns a sanitized sample dict, or None if the source is missing,
    unsynchronized, in local mode, non-normal leap state, or has
    non-finite/missing fields. The Ref hostname/IP is never exported.
    Error estimate follows the chrony doc/FAQ:
    abs(System time) + Root dispersion + 0.5*abs(Root delay).
    """
    if not isinstance(text, str):
        return None
    fields = {}
    for line in text.splitlines():
        k, sep, v = line.partition(":")
        if sep:
            fields[k.strip()] = v.strip()
    if fields.get("Leap status") != "Normal":
        return None
    try:
        stratum = int(fields.get("Stratum", ""))
    except ValueError:
        return None
    if not 1 <= stratum <= 15:
        return None
    refid = fields.get("Reference ID", "")
    if (not refid or refid == "0.0.0.0" or refid.startswith("127.127.")
            or refid.upper().startswith("7F7F")
            or "LOCAL" in refid.upper()):
        return None
    system = _field_float(fields, "System time")
    disp = _field_float(fields, "Root dispersion")
    delay = _field_float(fields, "Root delay")
    skew = _field_float(fields, "Skew")
    if system is None or disp is None or delay is None or skew is None:
        return None
    ref_raw = fields.get("Ref time (UTC)")
    if not ref_raw:
        return None
    try:
        ref_iso = _iso(datetime.strptime(
            ref_raw, "%a %b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc))
    except ValueError:
        return None
    return {
        "systemTimeSeconds": system,
        "rootDispersionSeconds": disp,
        "rootDelaySeconds": delay,
        "skewPpm": skew,
        "stratum": stratum,
        "leapStatus": "Normal",
        "referenceExternal": True,
        "refTimeUtc": ref_iso,
        "estimatedErrorSeconds": abs(system) + disp + 0.5 * abs(delay),
    }


def _chronyc_tracking(which=shutil.which, runner=subprocess.run):
    """Read-only chronyc sample; None when unavailable. Never installs,
    starts services, or touches the clock."""
    exe = which("chronyc")
    if not exe:
        return None
    try:
        cp = runner([exe, "-n", "tracking"], capture_output=True,
                    text=True, timeout=5)
    except Exception:
        return None
    if getattr(cp, "returncode", 1) != 0:
        return None
    return parse_chronyc_tracking(getattr(cp, "stdout", ""))


def collect_clock_evidence(pre, post, monotonic_elapsed_s):
    """Combine pre/post samples into sanitized clockEvidence, or None.

    The error is rounded up to whole ms and a skew allowance over the
    measured monotonic run interval is added so a later FanScope parser
    can bound drift during capture."""
    if pre is None or post is None:
        return None
    if not math.isfinite(monotonic_elapsed_s) or monotonic_elapsed_s < 0:
        return None
    base_s = max(pre["estimatedErrorSeconds"],
                 post["estimatedErrorSeconds"])
    skew_ppm = max(abs(pre["skewPpm"]), abs(post["skewPpm"]))
    skew_s = skew_ppm * 1e-6 * monotonic_elapsed_s
    return {
        "source": "chronyc -n tracking",
        "reference": _CHRONY_REFERENCE,
        "estimateBasis": "abs(system_time)+root_dispersion"
                         "+0.5*abs(root_delay)+|skew_ppm|*elapsed",
        "preCapture": pre,
        "postCapture": post,
        "monotonicElapsedMs": int(round(monotonic_elapsed_s * 1000)),
        "estimatedErrorMs": math.ceil(base_s * 1000 + skew_s * 1000),
        "skewAllowanceMs": math.ceil(skew_s * 1000),
    }


# ------------------------------------------------------------ playwright

@contextlib.contextmanager
def _browser_session(state_path):
    """Open the existing browser stack with the existing login state.

    Playwright is imported lazily here so the module stays importable
    without it. The state file is passed to the browser as-is; it is
    never read, printed, or copied by this code.
    """
    from playwright.sync_api import sync_playwright

    launch_args = ["--no-sandbox"]
    headful = os.environ.get("HEADFUL") == "1"
    browser = None
    ctx = None
    with sync_playwright() as p:
        try:
            try:
                browser = p.chromium.launch(channel="chrome",
                                            headless=not headful,
                                            args=launch_args)
            except Exception:
                browser = p.chromium.launch(headless=not headful,
                                            args=launch_args)
            ctx = browser.new_context(
                storage_state=state_path,
                viewport={"width": 700, "height": 900},
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
            )
            yield ctx.new_page()
        finally:
            close_err = None
            if ctx is not None:
                try:
                    ctx.close()
                except Exception as e:
                    close_err = e
            if browser is not None:
                try:
                    browser.close()
                except Exception as e:
                    if close_err is None:
                        close_err = e
            if close_err is not None:
                raise close_err


def _fetch_post_page(page, post):
    """Open one fixed public URL and capture TweetDetail /
    TweetResultByRestId read responses. No scrolling, clicking, or any
    X interaction beyond viewing the page. The response listener is
    always removed. Response URLs and bodies never leave this function
    except as parsed tweet JSON."""
    captured = []

    def on_response(res):
        try:
            url = res.url
        except Exception:
            return
        if not any(op in url for op in GRAPHQL_OPS):
            return
        try:
            status = res.status
        except Exception:
            status = 0
        try:
            body = res.json()
            captured.append({"status": status, "body": body,
                             "json_ok": True})
        except Exception:
            captured.append({"status": status, "body": None,
                             "json_ok": False})

    page.on("response", on_response)
    nav_error = False
    nav_status = None
    try:
        nav_res = page.goto(post["url"], timeout=NAV_TIMEOUT,
                            wait_until="domcontentloaded")
        if nav_res is not None:
            try:
                nav_status = nav_res.status
            except Exception:
                nav_status = None
        page.wait_for_timeout(DETAIL_WAIT_MS)
    except Exception:
        nav_error = True
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    if nav_status in BLOCKED_STATUSES:
        captured.append({"status": nav_status, "body": None,
                         "json_ok": False})

    try:
        page_url = page.url or ""
    except Exception:
        page_url = ""
    body_text = ""
    try:
        body_text = (page.locator("body").inner_text(
            timeout=2000) or "")[:4000]
    except Exception:
        pass

    outcome = {"blocked": _gate_reason(page_url, captured, body_text),
               "captured": captured}
    if nav_error and not captured and not outcome["blocked"]:
        outcome["error"] = "navigation_error"
    return outcome


# ------------------------------------------------------------------- run

def run_reobservation(manifest, state_path, fetch_one=None, now=_utcnow,
                      mono=time.monotonic,
                      chrony_sampler=_chronyc_tracking,
                      session_factory=None):
    """Execute one observation run against a validated manifest.

    Returns the artifact envelope dict. When fetch_one is None the
    existing Playwright browser stack is used via session_factory
    (default _browser_session). Browser launch, login-state read and
    close failures are caught here and recorded as fixed
    "browser_error" rows for every post that has no completed row; the
    exception object is never stringified, so no secret material or
    local path reaches the envelope or the CLI. Rows already fetched
    are kept, so partial and blocked results are preserved.
    """
    run_id = os.environ.get("GITHUB_RUN_ID") or "local"
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT") or "1"
    run_key = f"{run_id}:{run_attempt}"

    started = now()
    m_start = mono()
    pre = chrony_sampler() if chrony_sampler else None

    rows = []
    browser_failed = False
    if fetch_one is not None:
        observe_posts(manifest["posts"], fetch_one, run_key,
                      now=now, mono=mono,
                      expires_at=manifest["expiresAt"], out=rows)
    else:
        if session_factory is None:
            session_factory = _browser_session
        try:
            with session_factory(state_path) as page:
                def fetch_one(p):
                    return _fetch_post_page(page, p)
                observe_posts(manifest["posts"], fetch_one, run_key,
                              now=now, mono=mono,
                              expires_at=manifest["expiresAt"],
                              out=rows)
        except Exception:
            browser_failed = True
        if browser_failed:
            done = {r["postId"] for r in rows}
            for post in manifest["posts"]:
                if post["postId"] not in done:
                    row = _base_row(post, run_key)
                    row["status"] = "error"
                    row["reason"] = "browser_error"
                    rows.append(row)

    finished = now()
    m_end = mono()
    post_sample = chrony_sampler() if chrony_sampler else None
    evidence = collect_clock_evidence(pre, post_sample, m_end - m_start)

    statuses = [r["status"] for r in rows]
    if finished < started or m_end < m_start:
        state = "error"
    elif all(s == "ok" for s in statuses) and not browser_failed:
        state = "ok"
    elif "blocked" in statuses:
        state = "blocked"
    elif any(s == "ok" for s in statuses):
        state = "partial"
    else:
        state = "error"

    return {
        "version": 1,
        "source": SOURCE,
        "method": METHOD,
        "parserVersion": PARSER_VERSION,
        "runId": run_id,
        "runAttempt": run_attempt,
        "panelId": manifest["panelId"],
        "startedAt": _iso(started),
        "finishedAt": _iso(finished),
        "state": state,
        "clockEvidence": evidence,
        "rows": rows,
    }


# ----------------------------------------------------------------- output

def write_output_atomic(path, envelope):
    """Write the artifact atomically with mode 0600, UTF-8, <=2MiB."""
    data = json.dumps(envelope, ensure_ascii=False, indent=2,
                      sort_keys=False).encode("utf-8")
    if len(data) > OUTPUT_MAX_BYTES:
        raise OutputError("output_too_large")
    rows = envelope.get("rows")
    if isinstance(rows, list) and len(rows) > MAX_POSTS:
        raise OutputError("rows_too_many")
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".obs-",
                               suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, str(path))
        os.chmod(path, 0o600)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# -------------------------------------------------------------------- cli

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="fixed_reobserve",
        description="Re-observe fixed public post URLs (read-only).")
    parser.add_argument("--manifest", required=True,
                        help="path to manifest JSON (version 1)")
    parser.add_argument("--output", required=True,
                        help="path to write the observation envelope")
    parser.add_argument("--state",
                        default=os.environ.get("X_STATE_PATH",
                                               "state.json"),
                        help="path to existing X login state.json")
    parser.add_argument("--read-approved", action="store_true",
                        help="explicit approval for public read access")
    args = parser.parse_args(argv)

    if not args.read_approved:
        print("refused: --read-approved is required", file=sys.stderr)
        return 2
    try:
        text = Path(args.manifest).read_text(encoding="utf-8")
    except OSError:
        print("manifest_invalid: unreadable", file=sys.stderr)
        return 2
    try:
        manifest = load_manifest(text)
    except ManifestError as e:
        print(f"manifest_invalid: {e.code}", file=sys.stderr)
        return 2
    gate = check_window(manifest, _utcnow())
    if gate:
        print(f"manifest_invalid: {gate}", file=sys.stderr)
        return 2

    envelope = run_reobservation(manifest, args.state)
    try:
        write_output_atomic(args.output, envelope)
    except OutputError as e:
        print(f"output_error: {e.code}", file=sys.stderr)
        return 2
    counts = {}
    for r in envelope["rows"]:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"state={envelope['state']} rows={len(envelope['rows'])} "
          f"{counts}")
    return 0 if envelope["state"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
