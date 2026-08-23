"""poller.py: generic, polite JSON endpoint poller.

Scaffold for the sync pipelines described in the Aussie_Health_Docs_v2 vault
(see 02_Data_Pipelines/). One instance polls one JSON endpoint. Design rules:

  * Honest, contactable User-Agent. We never pretend to be a browser.
  * Conditional GETs (ETag / Last-Modified) so unchanged data costs ~nothing.
  * Exponential backoff with jitter; Retry-After is honoured on 429/503.
  * A circuit breaker stops a misbehaving loop instead of hammering a host.
  * A validate() hook turns silent schema drift into a loud failure.
  * Exactly one in-flight request per poller. No concurrency, ever.

Usage (single fetch):

    python poller.py https://example.com/api/feed.json --once

Usage (as a library):

    cfg = PollerConfig(url="https://example.com/api/feed.json",
                       headers={"x-api-key": "..."},
                       interval_seconds=900)
    JsonPoller(cfg, on_payload=store_in_postgres,
               validate=MyPydanticModel.model_validate).run_forever()
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import requests

log = logging.getLogger("poller")

# Politeness floor: never poll a public endpoint faster than this.
MIN_INTERVAL_SECONDS = 60


@dataclass
class PollerConfig:
    """Everything a poller needs to know. No secrets hardcoded; pass them in."""

    url: str
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    interval_seconds: int = 900          # 15 min default; see pipeline docs
    timeout_seconds: float = 30.0
    max_retries: int = 5                 # per fetch attempt
    backoff_base_seconds: float = 2.0
    backoff_cap_seconds: float = 300.0
    breaker_threshold: int = 5           # consecutive failed fetches -> stop
    user_agent: str = "AussieHealthUtility/0.1 (+set-a-contact-email)"


class CircuitOpen(RuntimeError):
    """Raised when consecutive failures exceed the breaker threshold."""


class JsonPoller:
    """Polls one JSON endpoint and hands each fresh payload to a callback.

    on_payload : called with the decoded JSON whenever new data arrives.
    validate   : optional; called with the decoded JSON before on_payload.
                 Raise (e.g. pydantic ValidationError) to reject a payload.
                 A rejected payload counts as a failure: drift gets noticed.
    """

    def __init__(
        self,
        config: PollerConfig,
        on_payload: Callable[[Any], None],
        validate: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self.cfg = config
        self.on_payload = on_payload
        self.validate = validate
        self._etag: Optional[str] = None
        self._last_modified: Optional[str] = None
        self._consecutive_failures = 0

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.cfg.user_agent,
                                     "Accept": "application/json"})
        self.session.headers.update(self.cfg.headers)

    # ------------------------------------------------------------------ #
    # Fetching
    # ------------------------------------------------------------------ #

    def _conditional_headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        if self._etag:
            h["If-None-Match"] = self._etag
        if self._last_modified:
            h["If-Modified-Since"] = self._last_modified
        return h

    def _sleep_backoff(self, attempt: int, retry_after: Optional[str]) -> None:
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = self.cfg.backoff_base_seconds ** attempt
        else:
            delay = self.cfg.backoff_base_seconds ** attempt
        delay = min(delay, self.cfg.backoff_cap_seconds)
        delay += random.uniform(0, delay * 0.25)  # jitter
        log.warning("backing off %.1fs (attempt %d)", delay, attempt)
        time.sleep(delay)

    def fetch_once(self) -> Optional[Any]:
        """One fetch with retries. Returns decoded JSON, or None on 304.

        Raises CircuitOpen if the breaker trips. Other exhausted failures
        raise the last underlying exception.
        """
        last_exc: Optional[Exception] = None

        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                resp = self.session.get(
                    self.cfg.url,
                    params=self.cfg.params,
                    headers=self._conditional_headers(),
                    timeout=self.cfg.timeout_seconds,
                )

                if resp.status_code == 304:
                    log.info("304 Not Modified; nothing to do")
                    self._consecutive_failures = 0
                    return None

                if resp.status_code in (429, 503):
                    # Record it. This branch `continue`s without raising, so
                    # if EVERY attempt is throttled we fall out of the loop
                    # with last_exc still None and nothing to re-raise --
                    # exactly the case this whole politeness layer exists for.
                    last_exc = requests.HTTPError(
                        f"{resp.status_code} after {attempt} attempt(s) "
                        f"for {self.cfg.url}", response=resp)
                    self._sleep_backoff(attempt, resp.headers.get("Retry-After"))
                    continue

                resp.raise_for_status()
                payload = resp.json()

                if self.validate is not None:
                    self.validate(payload)  # raises on drift

                self._etag = resp.headers.get("ETag", self._etag)
                self._last_modified = resp.headers.get("Last-Modified",
                                                       self._last_modified)
                self._consecutive_failures = 0
                return payload

            except (requests.RequestException, json.JSONDecodeError,
                    ValueError) as exc:
                last_exc = exc
                log.error("fetch failed: %s", exc)
                self._sleep_backoff(attempt, None)

        self._consecutive_failures += 1
        if self._consecutive_failures >= self.cfg.breaker_threshold:
            raise CircuitOpen(
                f"{self._consecutive_failures} consecutive failed fetches "
                f"for {self.cfg.url}; stopping. Investigate before resuming."
            )
        if last_exc is None:
            # Belt and braces. An assert here would be stripped under -O and
            # degrade into `raise None` -> TypeError, hiding the real cause.
            last_exc = RuntimeError(
                f"all {self.cfg.max_retries} attempts failed for "
                f"{self.cfg.url} without a recorded cause")
        raise last_exc

    # ------------------------------------------------------------------ #
    # Loop
    # ------------------------------------------------------------------ #

    def run_forever(self) -> None:
        interval = max(self.cfg.interval_seconds, MIN_INTERVAL_SECONDS)
        if interval != self.cfg.interval_seconds:
            log.warning("interval raised to politeness floor of %ss", interval)

        log.info("polling %s every %ss", self.cfg.url, interval)
        try:
            while True:
                try:
                    payload = self.fetch_once()
                    if payload is not None:
                        self.on_payload(payload)
                except CircuitOpen:
                    raise
                except Exception as exc:  # noqa: BLE001 - log and keep looping
                    log.error("cycle failed, will retry next interval: %s", exc)
                time.sleep(interval + random.uniform(0, 5))
        except KeyboardInterrupt:
            log.info("stopped by user")


# ---------------------------------------------------------------------- #
# CLI for quick spikes (e.g. testing a discovered endpoint)
# ---------------------------------------------------------------------- #

def _print_summary(payload: Any) -> None:
    text = json.dumps(payload, indent=2)
    print(text[:2000] + ("\n... [truncated]" if len(text) > 2000 else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="Politely poll a JSON endpoint.")
    parser.add_argument("url", help="Target JSON endpoint")
    parser.add_argument("--interval", type=int, default=900,
                        help="Seconds between polls (floor: 60)")
    parser.add_argument("--header", action="append", default=[],
                        metavar="KEY:VALUE",
                        help="Extra request header, repeatable")
    parser.add_argument("--once", action="store_true",
                        help="Fetch once, print a summary, exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    headers = {}
    for raw in args.header:
        key, _, value = raw.partition(":")
        if not value:
            parser.error(f"--header expects KEY:VALUE, got {raw!r}")
        headers[key.strip()] = value.strip()

    cfg = PollerConfig(url=args.url, headers=headers,
                       interval_seconds=args.interval)
    poller = JsonPoller(cfg, on_payload=_print_summary)

    if args.once:
        payload = poller.fetch_once()
        if payload is not None:
            _print_summary(payload)
    else:
        poller.run_forever()


if __name__ == "__main__":
    main()
