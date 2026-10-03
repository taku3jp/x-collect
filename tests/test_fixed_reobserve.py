#!/usr/bin/env python3
"""Pure-python unittest for fixed_reobserve.

Must be importable and runnable without playwright or network access.
"""

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fixed_reobserve as fr  # noqa: E402

UTC = timezone.utc


def make_tr(rest_id, screen="alice", user_rest="42",
            created="Wed Oct 01 12:00:00 +0000 2025", views="12345",
            favorite=7, retweet=2, reply=1, quote=3, bookmark=4,
            drop=()):
    legacy = {
        "full_text": "fixture body text that must never leak",
        "created_at": created,
        "favorite_count": favorite,
        "retweet_count": retweet,
        "reply_count": reply,
        "quote_count": quote,
        "bookmark_count": bookmark,
        "lang": "ja",
    }
    for k in drop:
        legacy.pop(k, None)
    tr = {
        "rest_id": rest_id,
        "legacy": legacy,
        "core": {"user_results": {"result": {
            "rest_id": user_rest,
            "legacy": {"screen_name": screen},
        }}},
    }
    if views is not None:
        tr["views"] = {"count": views}
    return tr


def wrap_body(*trs):
    return {"data": {"threaded_conversation_with_injections_v2": {
        "instructions": [{"entries": list(trs)}]}}}


def manifest_dict(**over):
    d = {
        "version": 1,
        "panelId": "panel-2026-10",
        "startAt": "2026-10-01T00:00:00Z",
        "expiresAt": "2026-10-02T00:00:00Z",
        "posts": ["https://x.com/alice/status/1234567890123456789"],
    }
    d.update(over)
    return d


def fetch_ok(post, bodies=None, tr=None):
    tr = tr if tr is not None else make_tr(post["postId"])
    bodies = bodies if bodies is not None else [wrap_body(tr)]
    return {"blocked": None,
            "captured": [{"status": 200, "body": b, "json_ok": True}
                         for b in bodies]}


class FakeClock:
    """Deterministic wall/monotonic clock pair."""
    def __init__(self, start=1_700_000_000.0):
        self.wall = start
        self.mono = 1000.0
        self.wall_step = 1.0
        self.mono_step = 1.0

    def now(self):
        v = self.wall
        self.wall += self.wall_step
        return datetime.fromtimestamp(v, tz=UTC)

    def monotonic(self):
        v = self.mono
        self.mono += self.mono_step
        return v


def run_rows(posts, fetch_one, clock=None, **kw):
    clock = clock or FakeClock()
    manifest = {
        "panelId": "panel-2026-10",
        "startAt": datetime(2026, 10, 1, tzinfo=UTC),
        "expiresAt": datetime(2026, 10, 3, tzinfo=UTC),
        "posts": posts,
    }
    return fr.run_reobservation(
        manifest, state_path="unused",
        fetch_one=fetch_one, now=clock.now, mono=clock.monotonic,
        chrony_sampler=None, **kw)


def post(url):
    return fr._canonical_post(url)


class FakeNavResponse:
    """Minimal stand-in for the page.goto() return value."""
    def __init__(self, status, url="https://x.com/a/status/1"):
        self.status = status
        self.url = url


class FakeGraphQLResponse:
    """Stand-in for a captured response event (url/status/json)."""
    def __init__(self, status=200, body=None, json_raises=False,
                 url="https://x.com/i/api/graphql/abc/TweetDetail"):
        self.status = status
        self.url = url
        self._body = body
        self._raises = json_raises

    def json(self):
        if self._raises:
            raise ValueError("invalid json")
        return self._body


class FakeLocator:
    def __init__(self, text):
        self._text = text

    def inner_text(self, timeout=None):
        return self._text


class FakePage:
    """Pure stand-in for the Playwright page surface used by
    _fetch_post_page: on/remove_listener/goto/wait_for_timeout/url/
    locator."""
    def __init__(self, nav_status=200, emit=(), url="",
                 body_text="", goto_exc=None):
        self.nav_status = nav_status
        self.emit = list(emit)
        self._url = url
        self._body_text = body_text
        self.goto_exc = goto_exc
        self.listeners = []
        self.gotos = []

    def on(self, event, cb):
        self.listeners.append((event, cb))

    def remove_listener(self, event, cb):
        self.listeners.remove((event, cb))

    def goto(self, url, timeout=None, wait_until=None):
        self.gotos.append(url)
        if self.goto_exc is not None:
            raise self.goto_exc
        for res in self.emit:
            for event, cb in list(self.listeners):
                if event == "response":
                    cb(res)
        return FakeNavResponse(self.nav_status, url=url)

    def wait_for_timeout(self, ms):
        pass

    @property
    def url(self):
        return self._url

    def locator(self, selector):
        return FakeLocator(self._body_text)


class ManifestTests(unittest.TestCase):
    def ok(self, d):
        return fr.load_manifest(json.dumps(d))

    def bad(self, d, code):
        with self.assertRaises(fr.ManifestError) as cm:
            self.ok(d)
        self.assertEqual(cm.exception.code, code)

    def test_valid(self):
        m = self.ok(manifest_dict())
        self.assertEqual(m["panelId"], "panel-2026-10")
        self.assertEqual(m["posts"][0]["postId"], "1234567890123456789")
        self.assertEqual(m["posts"][0]["url"],
                         "https://x.com/alice/status/1234567890123456789")

    def test_twitter_host_normalized(self):
        m = self.ok(manifest_dict(
            posts=["https://twitter.com/bob/status/777"]))
        self.assertEqual(m["posts"][0]["url"],
                         "https://x.com/bob/status/777")

    def test_large_id(self):
        pid = "9223372036854775807"  # int64 max, 19 digits
        m = self.ok(manifest_dict(posts=[f"https://x.com/a/status/{pid}"]))
        self.assertEqual(m["posts"][0]["postId"], pid)

    def test_version(self):
        self.bad(manifest_dict(version=2), "manifest_version")
        self.bad(manifest_dict(version="1"), "manifest_version")

    def test_version_bool_rejected(self):
        # JSON `true` parses to bool; True == 1 must not slip through
        self.bad(manifest_dict(version=True), "manifest_version")
        self.bad(manifest_dict(version=False), "manifest_version")

    def test_missing_key(self):
        d = manifest_dict()
        del d["panelId"]
        self.bad(d, "manifest_missing_key")

    def test_unknown_or_private_key(self):
        self.bad(manifest_dict(ownerPrivate="x"), "manifest_unknown_key")
        self.bad(manifest_dict(_secret="x"), "manifest_unknown_key")
        self.bad(manifest_dict(precision="acquired"), "manifest_unknown_key")

    def test_panel_slug(self):
        self.bad(manifest_dict(panelId="Bad Slug"), "panel_id")
        self.bad(manifest_dict(panelId="-x"), "panel_id")
        self.bad(manifest_dict(panelId=123), "panel_id")
        self.assertEqual(
            self.ok(manifest_dict(panelId="a1"))["panelId"], "a1")

    def test_window(self):
        self.bad(manifest_dict(
            startAt="2026-10-02T00:00:00Z",
            expiresAt="2026-10-01T00:00:00Z"), "window_order")
        self.bad(manifest_dict(
            expiresAt="2026-10-04T00:00:01Z"), "window_too_long")
        self.ok(manifest_dict(
            expiresAt="2026-10-04T00:00:00Z"))  # exactly 72h ok

    def test_time_must_be_utc(self):
        self.bad(manifest_dict(startAt="2026-10-01T00:00:00+09:00"),
                 "startAt_not_utc")
        self.bad(manifest_dict(startAt="2026-10-01 00:00:00"),
                 "startAt_not_utc")
        self.bad(manifest_dict(startAt="not-a-date"), "startAt_not_utc")
        self.bad(manifest_dict(startAt="2026-13-99T99:99:99Z"),
                 "startAt_invalid")

    def test_posts_over_100_not_truncated(self):
        urls = [f"https://x.com/u/status/{1000 + i}" for i in range(101)]
        self.bad(manifest_dict(posts=urls), "posts_too_many")

    def test_posts_type(self):
        self.bad(manifest_dict(posts="https://x.com/a/status/1"),
                 "posts_not_array")

    def test_url_validation(self):
        cases = [
            ("http://x.com/a/status/1", "url_scheme"),
            ("https://evil.com/a/status/1", "url_host"),
            ("https://x.com.evil.com/a/status/1", "url_host"),
            ("https://user:pass@x.com/a/status/1", "url_credentials"),
            ("https://user@x.com/a/status/1", "url_credentials"),
            ("https://x.com/a/status/1?s=20", "url_not_canonical"),
            ("https://x.com/a/status/1#frag", "url_not_canonical"),
            ("https://x.com/i/web/status/1", "url_path"),
            ("https://x.com/a/status/abc", "url_path"),
            ("https://x.com/a/status/", "url_path"),
            ("https://x.com/a/", "url_path"),
            ("https://x.com/a/status/1:443/x", "url_path"),
            ("https://x.com:444/a/status/1", "url_port"),
            ("https://x.com/a/status/12345678901234567890", "url_path"),
            ("https://x.com/a/status/0", "post_id"),
        ]
        for url, code in cases:
            with self.subTest(url=url):
                self.bad(manifest_dict(posts=[url]), code)

    def test_duplicate_same_value(self):
        u = "https://x.com/a/status/1"
        self.bad(manifest_dict(posts=[u, u]), "post_duplicate")

    def test_duplicate_conflicting_values(self):
        # same postId under a different owner slug: ambiguous -> reject
        self.bad(manifest_dict(
            posts=["https://x.com/a/status/1",
                   "https://x.com/b/status/1"]), "post_conflict")
        # twitter.com canonicalizes to the same x.com URL -> duplicate
        self.bad(manifest_dict(
            posts=["https://x.com/a/status/1",
                   "https://twitter.com/a/status/1"]), "post_duplicate")

    def test_window_gate(self):
        m = self.ok(manifest_dict())
        self.assertEqual(
            fr.check_window(m, datetime(2026, 9, 30, tzinfo=UTC)),
            "before_start")
        self.assertEqual(
            fr.check_window(m, datetime(2026, 10, 3, tzinfo=UTC)),
            "expired")
        self.assertIsNone(
            fr.check_window(m, datetime(2026, 10, 1, 12, tzinfo=UTC)))


class SelectAndMetricTests(unittest.TestCase):
    P = post("https://x.com/alice/status/111")

    def test_select_exact_id_not_reply(self):
        reply = make_tr("112", screen="mallory")
        focal = make_tr("111")
        body = wrap_body(reply, focal)
        tr = fr.select_tweet_result([body], "111")
        self.assertIs(tr, focal)

    def test_select_none_when_absent(self):
        body = wrap_body(make_tr("112"), make_tr("113"))
        self.assertIsNone(fr.select_tweet_result([body], "111"))

    def test_select_first_wins_across_bodies(self):
        a, b = make_tr("111", user_rest="42"), make_tr("111", user_rest="99")
        tr = fr.select_tweet_result([wrap_body(a), wrap_body(b)], "111")
        self.assertIs(tr, a)

    def test_metrics_missing_is_null_zero_is_zero(self):
        tr = make_tr("111", views=None, drop=("bookmark_count",),
                     favorite=0, retweet=0)
        obs = fr.extract_observation(tr, self.P)
        self.assertIsNone(obs["impressions"])
        self.assertIsNone(obs["bookmarks"])
        self.assertEqual(obs["likes"], 0)
        self.assertEqual(obs["reposts"], 0)

    def test_metrics_read(self):
        tr = make_tr("111", views="9001")
        obs = fr.extract_observation(tr, self.P)
        self.assertEqual(obs["impressions"], 9001)
        self.assertEqual(obs["likes"], 7)
        self.assertEqual(obs["replies"], 1)
        self.assertEqual(obs["reposts"], 2)
        self.assertEqual(obs["quotes"], 3)
        self.assertEqual(obs["bookmarks"], 4)
        self.assertEqual(obs["authorId"], "42")
        self.assertEqual(obs["authorHandle"], "alice")
        self.assertEqual(obs["postedAt"], "2025-10-01T12:00:00Z")

    def test_metrics_invalid(self):
        tr = make_tr("111")
        tr["legacy"]["favorite_count"] = "abc"
        with self.assertRaises(fr.ObservationError) as cm:
            fr.extract_observation(tr, self.P)
        self.assertEqual(cm.exception.code, "invalid_metrics")

    def test_views_malformed(self):
        tr = make_tr("111")
        tr["views"] = "nope"
        with self.assertRaises(fr.ObservationError):
            fr.extract_observation(tr, self.P)
        tr = make_tr("111")
        tr["views"] = {"count": {"x": 1}}
        with self.assertRaises(fr.ObservationError):
            fr.extract_observation(tr, self.P)

    def test_no_forbidden_fields(self):
        tr = make_tr("111")
        obs = fr.extract_observation(tr, self.P)
        for k in ("text", "full_text", "media", "extended_entities",
                  "body", "session", "entities"):
            self.assertNotIn(k, obs)


class ObserveTests(unittest.TestCase):
    def test_ok_row_shape(self):
        posts = [post("https://x.com/alice/status/111")]
        env = run_rows(posts, lambda p: fetch_ok(p))
        self.assertEqual(env["state"], "ok")
        row = env["rows"][0]
        self.assertEqual(row["status"], "ok")
        self.assertIsNone(row["reason"])
        self.assertEqual(row["impressions"], 12345)
        self.assertEqual(row["timeBasis"], "unknown")
        self.assertIsNone(row["uncertaintyMs"])
        self.assertEqual(row["resendKey"],
                         f"{env['runId']}:{env['runAttempt']}:111")
        self.assertIsNotNone(row["capturedAt"])
        self.assertIsNotNone(row["captureStartedAt"])
        self.assertIsNotNone(row["captureFinishedAt"])
        self.assertIsInstance(row["monotonicElapsedMs"], int)

    def test_unavailable_not_deleted(self):
        posts = [post("https://x.com/alice/status/111")]
        env = run_rows(posts, lambda p: fetch_ok(p, tr=make_tr("999")))
        self.assertEqual(env["rows"][0]["status"], "unavailable")
        self.assertEqual(env["rows"][0]["reason"], "post_not_in_response")
        self.assertEqual(env["state"], "error")  # no ok rows, not blocked

    def test_corrupt_json_is_error(self):
        posts = [post("https://x.com/alice/status/111")]
        outcome = {"blocked": None,
                   "captured": [{"status": 200, "body": None,
                                 "json_ok": False}]}
        env = run_rows(posts, lambda p: outcome)
        self.assertEqual(env["rows"][0]["status"], "error")
        self.assertEqual(env["rows"][0]["reason"], "corrupt_json")

    def test_no_response_is_error(self):
        posts = [post("https://x.com/alice/status/111")]
        env = run_rows(posts, lambda p: {"blocked": None, "captured": []})
        self.assertEqual(env["rows"][0]["status"], "error")
        self.assertEqual(env["rows"][0]["reason"], "no_response")

    def test_blocked_stops_and_keeps_rest(self):
        posts = [post("https://x.com/a/status/1"),
                 post("https://x.com/b/status/2"),
                 post("https://x.com/c/status/3")]
        calls = []

        def fetch(p):
            calls.append(p["postId"])
            if len(calls) == 2:
                return {"blocked": "rate_limited", "captured": []}
            return fetch_ok(p, tr=make_tr(p["postId"]))

        env = run_rows(posts, fetch)
        self.assertEqual(calls, ["1", "2"])  # stopped after block
        self.assertEqual(env["rows"][1]["status"], "blocked")
        self.assertEqual(env["rows"][1]["reason"], "rate_limited")
        self.assertEqual(env["rows"][2]["status"], "blocked")
        self.assertEqual(env["rows"][2]["reason"], "not_attempted")
        self.assertEqual(env["state"], "blocked")

    def test_partial_not_success(self):
        posts = [post("https://x.com/a/status/1"),
                 post("https://x.com/b/status/2")]

        def fetch(p):
            if p["postId"] == "1":
                return fetch_ok(p)
            return {"blocked": None, "captured": []}

        env = run_rows(posts, fetch)
        self.assertEqual(env["state"], "partial")
        self.assertNotEqual(env["state"], "ok")

    def test_exception_does_not_leak(self):
        posts = [post("https://x.com/a/status/1")]

        def fetch(p):
            raise RuntimeError("secret-token ?query=secret&auth=xyz")

        env = run_rows(posts, fetch)
        row = env["rows"][0]
        self.assertEqual(row["status"], "error")
        blob = json.dumps(env, ensure_ascii=False)
        self.assertNotIn("secret-token", blob)
        self.assertNotIn("query=secret", blob)

    def test_clock_reversal_is_invalid(self):
        clock = FakeClock()
        clock.wall_step = -5.0  # wall goes backward after first read
        posts = [post("https://x.com/a/status/1"),
                 post("https://x.com/b/status/2")]
        env = run_rows(posts, lambda p: fetch_ok(p), clock=clock)
        self.assertEqual(env["rows"][0]["status"], "error")
        self.assertEqual(env["rows"][0]["reason"], "clock_invalid")
        self.assertEqual(env["rows"][1]["reason"], "not_attempted")
        self.assertEqual(env["state"], "error")

    def test_expiry_mid_run(self):
        clock = FakeClock(start=datetime(2026, 10, 2, 23, 59, 59,
                                         tzinfo=UTC).timestamp())
        clock.wall_step = 10.0
        posts = [post("https://x.com/a/status/1"),
                 post("https://x.com/b/status/2")]
        env = run_rows(posts, lambda p: fetch_ok(p), clock=clock)
        self.assertEqual(env["rows"][1]["status"], "error")
        self.assertEqual(env["rows"][1]["reason"], "not_attempted")

    def test_same_id_single_observation(self):
        # same id appearing in two bodies is still one row
        posts = [post("https://x.com/a/status/1")]
        tr1, tr2 = make_tr("1", views="5"), make_tr("1", views="9")
        env = run_rows(posts, lambda p: fetch_ok(
            p, bodies=[wrap_body(tr1), wrap_body(tr2)]))
        self.assertEqual(len(env["rows"]), 1)
        self.assertEqual(env["rows"][0]["impressions"], 5)

    def test_no_media_text_session_in_envelope(self):
        posts = [post("https://x.com/a/status/1")]
        tr = make_tr("1")
        tr["legacy"]["extended_entities"] = {
            "media": [{"media_url_https": "https://pbs.example/x.jpg"}]}
        env = run_rows(posts, lambda p: fetch_ok(p, tr=tr))
        blob = json.dumps(env, ensure_ascii=False)
        for needle in ("fixture body text", "media_url_https",
                       "pbs.example", "full_text", "extended_entities",
                       "storage_state"):
            self.assertNotIn(needle, blob)


class FetchPostPageTests(unittest.TestCase):
    """_fetch_post_page must gate on the navigation document response
    itself, not only on captured GraphQL responses. A fake page keeps
    this pure (no playwright, no network)."""

    P = post("https://x.com/alice/status/111")

    def test_doc_403_blocks(self):
        page = FakePage(nav_status=403,
                        url="https://x.com/alice/status/111")
        outcome = fr._fetch_post_page(page, self.P)
        self.assertEqual(outcome["blocked"], "http_403")
        self.assertIn({"status": 403, "body": None, "json_ok": False},
                      outcome["captured"])

    def test_doc_429_blocks(self):
        page = FakePage(nav_status=429)
        outcome = fr._fetch_post_page(page, self.P)
        self.assertEqual(outcome["blocked"], "rate_limited")
        entry = [c for c in outcome["captured"] if c["status"] == 429]
        self.assertEqual(entry, [{"status": 429, "body": None,
                                 "json_ok": False}])

    def test_doc_gate_not_overridden_by_graphql_ok(self):
        good = FakeGraphQLResponse(
            status=200, body=wrap_body(make_tr("111")))
        page = FakePage(nav_status=403, emit=[good])
        outcome = fr._fetch_post_page(page, self.P)
        self.assertEqual(outcome["blocked"], "http_403")

    def test_graphql_gate_still_blocks(self):
        res = FakeGraphQLResponse(status=401, body={"errors": []})
        page = FakePage(nav_status=200, emit=[res])
        outcome = fr._fetch_post_page(page, self.P)
        self.assertEqual(outcome["blocked"], "http_401")

    def test_normal_200(self):
        res = FakeGraphQLResponse(
            status=200, body=wrap_body(make_tr("111")))
        page = FakePage(nav_status=200, emit=[res])
        outcome = fr._fetch_post_page(page, self.P)
        self.assertIsNone(outcome["blocked"])
        self.assertIsNone(outcome.get("error"))
        self.assertEqual(len(outcome["captured"]), 1)
        self.assertTrue(outcome["captured"][0]["json_ok"])

    def test_listener_removed_on_success_and_error(self):
        page = FakePage(nav_status=200)
        fr._fetch_post_page(page, self.P)
        self.assertEqual(page.listeners, [])
        bad = FakePage(goto_exc=RuntimeError("boom"))
        outcome = fr._fetch_post_page(bad, self.P)
        self.assertEqual(bad.listeners, [])
        self.assertEqual(outcome["error"], "navigation_error")

    def test_doc_blocked_stops_run_keeps_not_attempted(self):
        posts = [post("https://x.com/a/status/1"),
                 post("https://x.com/b/status/2")]
        calls = []

        def fetch(p):
            calls.append(p["postId"])
            return fr._fetch_post_page(FakePage(nav_status=403), p)

        env = run_rows(posts, fetch)
        self.assertEqual(calls, ["1"])
        self.assertEqual(env["rows"][0]["status"], "blocked")
        self.assertEqual(env["rows"][0]["reason"], "http_403")
        self.assertEqual(env["rows"][1]["status"], "blocked")
        self.assertEqual(env["rows"][1]["reason"], "not_attempted")
        self.assertEqual(env["state"], "blocked")

    def test_no_success_is_error(self):
        posts = [post("https://x.com/a/status/1")]
        env = run_rows(posts, lambda p: fr._fetch_post_page(
            FakePage(nav_status=200), p))
        self.assertEqual(env["rows"][0]["status"], "error")
        self.assertEqual(env["rows"][0]["reason"], "no_response")
        self.assertEqual(env["state"], "error")

    def test_doc_response_url_and_body_never_leak(self):
        page = FakePage(nav_status=403,
                        url="https://x.com/alice/status/111?k=SECRET9",
                        body_text="internal error detail SECRET9")
        outcome = fr._fetch_post_page(page, self.P)
        blob = json.dumps(outcome, ensure_ascii=False)
        self.assertNotIn("SECRET9", blob)
        self.assertNotIn("k=", blob)
        for c in outcome["captured"]:
            self.assertEqual(set(c), {"status", "body", "json_ok"})


class BrowserFailureTests(unittest.TestCase):
    """Browser startup / state-read / close failures must be captured
    into the metadata envelope as fixed browser_error rows. Exception
    text, secrets and local paths must never leak; exit is non-zero;
    rows already fetched are kept (partial / blocked preserved)."""

    def _run(self, posts, session_factory):
        clock = FakeClock()
        manifest = {
            "panelId": "panel-2026-10",
            "startAt": datetime(2026, 10, 1, tzinfo=UTC),
            "expiresAt": datetime(2026, 10, 3, tzinfo=UTC),
            "posts": posts,
        }
        return fr.run_reobservation(
            manifest, state_path="unused",
            session_factory=session_factory,
            now=clock.now, mono=clock.monotonic,
            chrony_sampler=None)

    def test_startup_failure_records_browser_error(self):
        posts = [post("https://x.com/a/status/1"),
                 post("https://x.com/b/status/2")]

        @contextlib.contextmanager
        def bad_session(path):
            raise RuntimeError(
                "cannot launch /private/dir/state.json token=SECRET")
            yield

        env = self._run(posts, bad_session)
        self.assertEqual(env["state"], "error")
        self.assertEqual(len(env["rows"]), 2)
        for row in env["rows"]:
            self.assertEqual(row["status"], "error")
            self.assertEqual(row["reason"], "browser_error")
        blob = json.dumps(env, ensure_ascii=False)
        for needle in ("SECRET", "/private/dir", "state.json",
                       "token="):
            self.assertNotIn(needle, blob)

    def test_state_read_failure_records_browser_error(self):
        # new_context(storage_state=...) failing is the same path
        posts = [post("https://x.com/a/status/1")]

        @contextlib.contextmanager
        def bad_state(path):
            raise RuntimeError("state file secret contents {}")
            yield

        env = self._run(posts, bad_state)
        self.assertEqual(env["state"], "error")
        self.assertEqual(env["rows"][0]["status"], "error")
        self.assertEqual(env["rows"][0]["reason"], "browser_error")
        self.assertNotIn("secret contents", json.dumps(env))

    def test_close_failure_keeps_completed_rows(self):
        posts = [post("https://x.com/a/status/1")]

        @contextlib.contextmanager
        def close_fail(path):
            yield object()
            raise RuntimeError("close failed /private/state.json")

        orig = fr._fetch_post_page
        fr._fetch_post_page = lambda page, p: fetch_ok(p)
        try:
            env = self._run(posts, close_fail)
        finally:
            fr._fetch_post_page = orig
        self.assertEqual(env["rows"][0]["status"], "ok")
        self.assertEqual(env["rows"][0]["impressions"], 12345)
        self.assertNotEqual(env["state"], "ok")
        self.assertNotIn("/private/state.json", json.dumps(env))

    def test_mid_run_failure_keeps_partial_rows(self):
        posts = [post("https://x.com/a/status/1"),
                 post("https://x.com/b/status/2")]

        @contextlib.contextmanager
        def session(path):
            yield object()

        orig = fr.observe_posts

        def fake_observe(posts_, fetch_one_, run_key, now, mono,
                         expires_at=None, out=None):
            rows = out if out is not None else []
            row = fr._base_row(posts_[0], run_key)
            row["status"] = "ok"
            row["impressions"] = 5
            rows.append(row)
            raise RuntimeError("browser crashed token=XYZ")

        fr.observe_posts = fake_observe
        try:
            env = self._run(posts, session)
        finally:
            fr.observe_posts = orig
        self.assertEqual(env["state"], "partial")
        self.assertEqual(env["rows"][0]["status"], "ok")
        self.assertEqual(env["rows"][0]["impressions"], 5)
        self.assertEqual(env["rows"][1]["status"], "error")
        self.assertEqual(env["rows"][1]["reason"], "browser_error")
        self.assertNotIn("token=XYZ", json.dumps(env))

    def test_blocked_state_survives_browser_failure(self):
        posts = [post("https://x.com/a/status/1"),
                 post("https://x.com/b/status/2")]

        @contextlib.contextmanager
        def session(path):
            yield object()

        orig = fr.observe_posts

        def fake_observe(posts_, fetch_one_, run_key, now, mono,
                         expires_at=None, out=None):
            rows = out if out is not None else []
            row = fr._base_row(posts_[0], run_key)
            row["status"] = "blocked"
            row["reason"] = "rate_limited"
            rows.append(row)
            raise RuntimeError("crash")

        fr.observe_posts = fake_observe
        try:
            env = self._run(posts, session)
        finally:
            fr.observe_posts = orig
        self.assertEqual(env["state"], "blocked")
        self.assertEqual(env["rows"][0]["status"], "blocked")
        self.assertEqual(env["rows"][0]["reason"], "rate_limited")
        self.assertEqual(env["rows"][1]["status"], "error")
        self.assertEqual(env["rows"][1]["reason"], "browser_error")

    def test_cli_browser_failure_exit_nonzero_no_leak(self):
        with tempfile.TemporaryDirectory() as d:
            mp = Path(d) / "m.json"
            op = Path(d) / "o.json"
            now = datetime.now(UTC)
            mp.write_text(json.dumps(manifest_dict(
                startAt=(now - timedelta(hours=1)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"),
                expiresAt=(now + timedelta(hours=1)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"))), "utf-8")

            @contextlib.contextmanager
            def bad(path):
                raise RuntimeError("secret-xyz /private/state.json")
                yield

            orig = fr._browser_session
            fr._browser_session = bad
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    rc = fr.main(["--manifest", str(mp),
                                  "--output", str(op),
                                  "--read-approved"])
            finally:
                fr._browser_session = orig
            self.assertNotEqual(rc, 0)
            self.assertTrue(op.exists())
            env = json.loads(op.read_text("utf-8"))
            self.assertEqual(env["state"], "error")
            self.assertEqual(env["rows"][0]["reason"], "browser_error")
            self.assertNotIn("secret-xyz", err.getvalue())
            self.assertNotIn("/private/state.json", err.getvalue())


class LaunchArgsTests(unittest.TestCase):
    def test_no_stealth_flags(self):
        src = (Path(__file__).resolve().parent.parent /
               "fixed_reobserve.py").read_text("utf-8")
        self.assertNotIn("AutomationControlled", src)
        self.assertIn('"--no-sandbox"', src)


class GateTests(unittest.TestCase):
    def test_status_codes(self):
        self.assertEqual(fr._gate_reason(
            "https://x.com/a/status/1",
            [{"status": 401, "body": None, "json_ok": True}], ""), "http_401")
        self.assertEqual(fr._gate_reason(
            "https://x.com/a/status/1",
            [{"status": 403, "body": None, "json_ok": True}], ""), "http_403")
        self.assertEqual(fr._gate_reason(
            "https://x.com/a/status/1",
            [{"status": 429, "body": None, "json_ok": True}],
            ""), "rate_limited")

    def test_login_redirect(self):
        self.assertEqual(fr._gate_reason(
            "https://x.com/i/flow/login?redirect_after_login=x", [], ""),
            "login_required")

    def test_challenge(self):
        self.assertEqual(fr._gate_reason(
            "https://x.com/i/flow/checkpoint", [], ""), "challenge")

    def test_age_gate_text(self):
        self.assertEqual(fr._gate_reason(
            "https://x.com/a/status/1", [],
            "Age-restricted adult content"), "age_gate")
        self.assertEqual(fr._gate_reason(
            "https://x.com/a/status/1", [],
            "このメディアは年齢制限があります"), "age_gate")

    def test_clean(self):
        self.assertIsNone(fr._gate_reason(
            "https://x.com/a/status/1",
            [{"status": 200, "body": {}, "json_ok": True}], "normal text"))


class ChronyTests(unittest.TestCase):
    GOOD = """Reference ID    : A29FC87B (foo.example.internal)
Stratum         : 3
Ref time (UTC)  : Thu Oct 02 12:34:56 2025
System time     : 0.000492 seconds slow of NTP time
Last offset     : +0.000021 seconds
RMS offset      : 0.000330 seconds
Frequency       : 4.521 ppm slow
Residual freq   : +0.001 ppm
Skew            : 0.123 ppm
Root delay      : 0.025600 seconds
Root dispersion : 0.001234 seconds
Update interval : 517.3 seconds
Leap status     : Normal
"""

    def test_parse_good(self):
        ev = fr.parse_chronyc_tracking(self.GOOD)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["stratum"], 3)
        self.assertEqual(ev["leapStatus"], "Normal")
        self.assertTrue(ev["referenceExternal"])
        self.assertAlmostEqual(
            ev["estimatedErrorSeconds"],
            0.000492 + 0.001234 + 0.5 * 0.025600)
        self.assertEqual(ev["refTimeUtc"], "2025-10-02T12:34:56Z")
        # ref hostname / ip must not be exported
        self.assertNotIn("foo.example", json.dumps(ev))
        self.assertNotIn("A29FC87B", json.dumps(ev))

    def test_leap_not_normal(self):
        self.assertIsNone(fr.parse_chronyc_tracking(
            self.GOOD.replace("Leap status     : Normal",
                              "Leap status     : Insert second")))

    def test_stratum_unsync(self):
        self.assertIsNone(fr.parse_chronyc_tracking(
            self.GOOD.replace("Stratum         : 3",
                              "Stratum         : 16")))

    def test_local_mode(self):
        self.assertIsNone(fr.parse_chronyc_tracking(
            self.GOOD.replace("A29FC87B (foo.example.internal)",
                              "7F7F7F01 ()")))

    def test_missing_fields(self):
        self.assertIsNone(fr.parse_chronyc_tracking(
            self.GOOD.replace("Skew            : 0.123 ppm\n", "")))
        self.assertIsNone(fr.parse_chronyc_tracking("garbage"))
        self.assertIsNone(fr.parse_chronyc_tracking(""))

    def test_combine_evidence(self):
        pre = fr.parse_chronyc_tracking(self.GOOD)
        post_ = fr.parse_chronyc_tracking(self.GOOD)
        ev = fr.collect_clock_evidence(pre, post_, 42.0)
        base_ms = (0.000492 + 0.001234 + 0.5 * 0.025600) * 1000
        skew_ms = 0.123e-6 * 42.0 * 1000
        import math
        self.assertEqual(ev["estimatedErrorMs"],
                         math.ceil(base_ms + skew_ms))
        self.assertEqual(ev["monotonicElapsedMs"], 42000)
        self.assertIn("chrony-project.org", ev["reference"])

    def test_combine_none(self):
        self.assertIsNone(fr.collect_clock_evidence(None, {}, 1.0))
        self.assertIsNone(fr.collect_clock_evidence({}, None, 1.0))
        self.assertIsNone(fr.collect_clock_evidence({}, {}, -1.0))

    def test_chronyc_missing_binary(self):
        self.assertIsNone(fr._chronyc_tracking(which=lambda x: None))

    def test_chronyc_error(self):
        class CP:
            returncode = 1
            stdout = ""
        self.assertIsNone(fr._chronyc_tracking(
            which=lambda x: "/usr/bin/chronyc",
            runner=lambda *a, **k: CP()))

        def boom(*a, **k):
            raise OSError("nope")
        self.assertIsNone(fr._chronyc_tracking(
            which=lambda x: "/usr/bin/chronyc", runner=boom))


class OutputTests(unittest.TestCase):
    def test_atomic_write_mode(self):
        env = {"version": 1, "rows": []}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.json"
            fr.write_output_atomic(path, env)
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual(json.loads(path.read_text("utf-8")), env)

    def test_size_cap(self):
        big = {"rows": [{"postId": "1", "pad": "x" * (2 * 1024 * 1024)}]}
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(fr.OutputError):
                fr.write_output_atomic(Path(d) / "o.json", big)

    def test_envelope_fields(self):
        posts = [post("https://x.com/a/status/1")]
        env = run_rows(posts, lambda p: fetch_ok(p))
        self.assertEqual(env["version"], 1)
        self.assertEqual(env["source"], "taku3jp/x-collect-fixed")
        self.assertEqual(env["method"], "x-collect-fixed-browser")
        self.assertEqual(env["parserVersion"],
                         "x-collect-fixed-browser-1")
        for k in ("runId", "runAttempt", "panelId", "startedAt",
                  "finishedAt", "state", "rows"):
            self.assertIn(k, env)


class CliTests(unittest.TestCase):
    def test_requires_read_approved(self):
        with tempfile.TemporaryDirectory() as d:
            mp = Path(d) / "m.json"
            op = Path(d) / "o.json"
            mp.write_text(json.dumps(manifest_dict()), "utf-8")
            rc = fr.main(["--manifest", str(mp), "--output", str(op)])
            self.assertEqual(rc, 2)
            self.assertFalse(op.exists())

    def test_manifest_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            mp = Path(d) / "m.json"
            op = Path(d) / "o.json"
            mp.write_text("{}", "utf-8")
            rc = fr.main(["--manifest", str(mp), "--output", str(op),
                          "--read-approved"])
            self.assertEqual(rc, 2)

    def test_cli_refuses_expired(self):
        with tempfile.TemporaryDirectory() as d:
            mp = Path(d) / "m.json"
            op = Path(d) / "o.json"
            mp.write_text(json.dumps(manifest_dict(
                startAt="2020-01-01T00:00:00Z",
                expiresAt="2020-01-02T00:00:00Z")), "utf-8")
            rc = fr.main(["--manifest", str(mp), "--output", str(op),
                          "--read-approved"])
            self.assertEqual(rc, 2)


class WorkflowYamlTests(unittest.TestCase):
    PATH = Path(__file__).resolve().parent.parent / \
        ".github" / "workflows" / "reobserve.yml"

    def setUp(self):
        self.text = self.PATH.read_text("utf-8")

    def test_dispatch_only(self):
        self.assertIn("workflow_dispatch:", self.text)
        self.assertNotIn("schedule:", self.text)
        self.assertNotIn("cron:", self.text)
        self.assertNotIn("push:", self.text)

    def test_permissions_and_limits(self):
        self.assertIn("contents: read", self.text)
        self.assertIn("reobserve", self.text)
        self.assertIn("cancel-in-progress: false", self.text)
        self.assertIn("timeout-minutes: 10", self.text)
        self.assertIn("retention-days: 7", self.text)
        self.assertIn("x-fixed-observations", self.text)

    def test_no_shell_interpolated_inputs(self):
        # manifest input may only appear as an env mapping; never expanded
        # inside run script text
        for line in self.text.splitlines():
            if "inputs.manifest_json" in line:
                self.assertIn("MANIFEST_JSON:", line)
            if "inputs.read_approved" in line:
                self.assertIn("READ_APPROVED:", line)
        self.assertIn("MANIFEST_JSON:", self.text)

    def test_state_secret_not_echoed(self):
        self.assertNotIn('echo "$X_STATE_JSON"', self.text)
        self.assertNotIn("cat state.json", self.text)

    def test_no_debug_or_session_upload(self):
        self.assertNotIn("debug_", self.text)
        # artifact must be metadata only
        self.assertNotIn("path: state.json", self.text)

    def test_early_gate_before_deps_and_session(self):
        # read approval + manifest validation must run after
        # checkout/setup but before dependency install and before the
        # session secret is restored
        gate = self.text.index("read approval + manifest validation")
        self.assertLess(gate, self.text.index("Install dependencies"))
        self.assertLess(gate, self.text.index("Restore X session"))
        self.assertLess(gate, self.text.index("playwright install"))

    def test_gate_uses_pure_loader_and_window_check(self):
        self.assertIn("load_manifest", self.text)
        self.assertIn("check_window", self.text)
        self.assertLess(self.text.index("load_manifest"),
                        self.text.index("X_STATE_JSON"))
        self.assertLess(self.text.index("READ_APPROVED"),
                        self.text.index("X_STATE_JSON"))


if __name__ == "__main__":
    unittest.main()
