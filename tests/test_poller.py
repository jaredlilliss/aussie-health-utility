"""JsonPoller: retry, backoff, breaker and conditional-GET behaviour."""

from __future__ import annotations

import unittest
from unittest import mock

from tests import fixture  # noqa: F401  (bootstraps sys.path for src/)

import requests

from poller import CircuitOpen, JsonPoller, PollerConfig

URL = "https://example.test/feed.json"


class _Resp:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


class _Session:
    """Serves a scripted list of responses; repeats the last one forever."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.headers = {}
        self.calls = 0
        self.last_kwargs = None

    def get(self, *args, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        item = (self._responses.pop(0) if len(self._responses) > 1
                else self._responses[0])
        if isinstance(item, Exception):
            raise item
        return item


class PollerTests(unittest.TestCase):

    def setUp(self):
        # Never actually sleep; capture what the backoff would have waited.
        self.delays: list[float] = []
        patcher = mock.patch("poller.time.sleep", self.delays.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _poller(self, responses, **cfg_kwargs):
        cfg_kwargs.setdefault("max_retries", 3)
        cfg_kwargs.setdefault("breaker_threshold", 99)
        cfg_kwargs.setdefault("backoff_base_seconds", 1.0)
        poller = JsonPoller(PollerConfig(url=URL, **cfg_kwargs),
                            on_payload=lambda _: None)
        poller.session = _Session(responses)
        return poller

    # -- regression: sustained throttling ------------------------------- #

    def test_sustained_throttling_raises_a_real_exception(self):
        """Every attempt throttled must raise a meaningful error.

        Regression: the 429/503 branch `continue`s without recording an
        exception, so the retry loop used to fall through to
        `assert last_exc is not None` -- a bare AssertionError, and under
        -O (asserts stripped) `raise None` -> TypeError. Both hid the cause,
        and both fired exactly when a host was rate-limiting us.
        """
        poller = self._poller([_Resp(429)])
        with self.assertRaises(requests.HTTPError) as ctx:
            poller.fetch_once()
        self.assertIn("429", str(ctx.exception))
        self.assertEqual(poller.session.calls, 3)  # all retries consumed

    def test_sustained_503_also_raises(self):
        poller = self._poller([_Resp(503)])
        with self.assertRaises(requests.HTTPError) as ctx:
            poller.fetch_once()
        self.assertIn("503", str(ctx.exception))

    def test_throttling_then_success_recovers(self):
        poller = self._poller([_Resp(429), _Resp(200, payload={"ok": True})])
        self.assertEqual(poller.fetch_once(), {"ok": True})
        self.assertEqual(poller._consecutive_failures, 0)

    def test_retry_after_header_is_honoured(self):
        poller = self._poller([_Resp(429, headers={"Retry-After": "7"})])
        with self.assertRaises(requests.HTTPError):
            poller.fetch_once()
        # 7s plus up to 25% jitter, and never the 1.0s exponential default.
        self.assertTrue(all(7.0 <= d <= 8.75 for d in self.delays),
                        f"delays were {self.delays}")

    def test_unparseable_retry_after_falls_back_to_backoff(self):
        poller = self._poller(
            [_Resp(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})])
        with self.assertRaises(requests.HTTPError):
            poller.fetch_once()
        self.assertTrue(self.delays)  # did not crash on an HTTP-date

    # -- breaker -------------------------------------------------------- #

    def test_circuit_breaker_trips_at_threshold(self):
        poller = self._poller([_Resp(429)], breaker_threshold=1)
        with self.assertRaises(CircuitOpen):
            poller.fetch_once()

    def test_breaker_message_names_the_url(self):
        poller = self._poller([_Resp(429)], breaker_threshold=1)
        with self.assertRaises(CircuitOpen) as ctx:
            poller.fetch_once()
        self.assertIn(URL, str(ctx.exception))

    # -- normal paths --------------------------------------------------- #

    def test_304_returns_none_and_clears_failure_count(self):
        poller = self._poller([_Resp(304)])
        poller._consecutive_failures = 2
        self.assertIsNone(poller.fetch_once())
        self.assertEqual(poller._consecutive_failures, 0)

    def test_payload_returned_and_validators_cache_headers(self):
        poller = self._poller([_Resp(200, payload={"a": 1},
                                     headers={"ETag": "W/\"x\"",
                                              "Last-Modified": "Mon, 20 Jul 2026 00:00:00 GMT"})])
        self.assertEqual(poller.fetch_once(), {"a": 1})
        self.assertEqual(poller._etag, "W/\"x\"")
        self.assertEqual(poller._last_modified,
                         "Mon, 20 Jul 2026 00:00:00 GMT")

    def test_conditional_headers_sent_on_the_next_fetch(self):
        poller = self._poller([_Resp(200, payload={"a": 1},
                                     headers={"ETag": "abc"})])
        poller.fetch_once()
        poller.fetch_once()
        sent = poller.session.last_kwargs["headers"]
        self.assertEqual(sent.get("If-None-Match"), "abc")

    def test_validation_failure_is_treated_as_a_fetch_failure(self):
        """A rejected payload must not be handed on as if it were good."""
        def _drift(_payload):
            raise ValueError("shape changed")

        poller = JsonPoller(
            PollerConfig(url=URL, max_retries=2, breaker_threshold=99,
                         backoff_base_seconds=1.0),
            on_payload=lambda _: None, validate=_drift)
        poller.session = _Session([_Resp(200, payload={"a": 1})])
        with self.assertRaises(ValueError) as ctx:
            poller.fetch_once()
        self.assertIn("shape changed", str(ctx.exception))

    def test_network_error_surfaces_the_underlying_exception(self):
        poller = self._poller([requests.ConnectionError("no route to host")])
        with self.assertRaises(requests.ConnectionError):
            poller.fetch_once()

    def test_http_500_surfaces_as_http_error(self):
        poller = self._poller([_Resp(500)])
        with self.assertRaises(requests.HTTPError):
            poller.fetch_once()

    # -- conduct policy ------------------------------------------------- #

    def test_user_agent_and_accept_are_set_on_the_session(self):
        poller = JsonPoller(PollerConfig(url=URL, user_agent="Test/1.0 (+me)"),
                            on_payload=lambda _: None)
        self.assertEqual(poller.session.headers["User-Agent"], "Test/1.0 (+me)")
        self.assertEqual(poller.session.headers["Accept"], "application/json")

    def test_caller_headers_override_defaults(self):
        poller = JsonPoller(
            PollerConfig(url=URL, headers={"Accept": "application/json;odata=verbose"}),
            on_payload=lambda _: None)
        self.assertEqual(poller.session.headers["Accept"],
                         "application/json;odata=verbose")


if __name__ == "__main__":
    unittest.main()
