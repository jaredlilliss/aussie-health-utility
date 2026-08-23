"""The loop's backoff policy: back off for them, not for us.

Regression cover for a measured data loss. On 04/08/2026 five cycles failed
with `getaddrinfo failed` -- the laptop had no DNS -- the loop dropped to an
hourly probe, and roughly ten 15-minute slots went uncollected in a series
that cannot be backfilled. The backoff is meant to spare a struggling
upstream; when the name will not even resolve we never reached the upstream,
so the wait buys nothing and costs rows.

Offline like the rest of the suite: the resolver is injected, so no test here
touches DNS or the network.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest

from tests import SCRIPTS  # noqa: F401  (bootstraps sys.path for scripts/)

# poll_loop.py is Windows-only at import time -- ctypes.wintypes does not exist
# elsewhere, and it reads %LOCALAPPDATA% at module scope. Guard the import so
# the suite still runs clean on the Linux host in VPS_Migration.md, where the
# loop is replaced by cron and this module is not used at all.
WINDOWS = sys.platform == "win32"
if WINDOWS:
    os.environ.setdefault("LOCALAPPDATA", tempfile.gettempdir())
    import poll_loop


def resolves(_host):
    """Stand-in for a successful getaddrinfo."""
    return [("ok",)]


def does_not_resolve(host):
    raise socket.gaierror(11001, "getaddrinfo failed")


def refused(_host):
    """Reached the network, but the far end said no."""
    raise ConnectionRefusedError(61, "connection refused")


@unittest.skipUnless(WINDOWS, "poll_loop.py is Windows-only (ctypes.wintypes)")
class TestBackoffPolicy(unittest.TestCase):
    def test_healthy_cycles_use_the_normal_cadence(self):
        interval, note = poll_loop.next_interval(0, resolve=resolves)
        self.assertEqual(interval, poll_loop.INTERVAL_SECONDS)
        self.assertIsNone(note)

    def test_below_threshold_does_not_back_off(self):
        below = poll_loop.FAILURE_BACKOFF_THRESHOLD - 1
        interval, note = poll_loop.next_interval(below, resolve=resolves)
        self.assertEqual(interval, poll_loop.INTERVAL_SECONDS)
        self.assertIsNone(note)

    def test_upstream_unwell_still_backs_off(self):
        """The original purpose must survive: they resolve, so spare them."""
        interval, note = poll_loop.next_interval(
            poll_loop.FAILURE_BACKOFF_THRESHOLD, resolve=resolves
        )
        self.assertEqual(interval, poll_loop.FAILURE_BACKOFF_SECONDS)
        self.assertIn("upstream", note)

    def test_local_dns_failure_holds_normal_cadence(self):
        """The regression: offline must NOT cost an hour of slots."""
        interval, note = poll_loop.next_interval(
            poll_loop.FAILURE_BACKOFF_THRESHOLD, resolve=does_not_resolve
        )
        self.assertEqual(interval, poll_loop.INTERVAL_SECONDS)
        self.assertIn("offline", note)

    def test_many_failures_while_offline_never_back_off(self):
        """Being offline for hours must not accumulate into a backoff."""
        interval, _ = poll_loop.next_interval(50, resolve=does_not_resolve)
        self.assertEqual(interval, poll_loop.INTERVAL_SECONDS)

    def test_connection_refused_is_treated_as_upstream(self):
        """Only name resolution proves the fault is local."""
        interval, note = poll_loop.next_interval(
            poll_loop.FAILURE_BACKOFF_THRESHOLD, resolve=refused
        )
        self.assertEqual(interval, poll_loop.FAILURE_BACKOFF_SECONDS)
        self.assertIn("upstream", note)


@unittest.skipUnless(WINDOWS, "poll_loop.py is Windows-only (ctypes.wintypes)")
class TestNetworkClassification(unittest.TestCase):
    def test_gaierror_means_local_failure(self):
        self.assertTrue(poll_loop.local_network_down(resolve=does_not_resolve))

    def test_resolution_success_means_not_local(self):
        self.assertFalse(poll_loop.local_network_down(resolve=resolves))

    def test_unexpected_resolver_error_is_not_proof(self):
        def odd(_host):
            raise RuntimeError("resolver exploded")

        self.assertFalse(poll_loop.local_network_down(resolve=odd))


class FakeClock:
    """A clock we control, so no test ever really sleeps."""

    def __init__(self, start=1_000.0, drift=0.0):
        self.t = start
        self.drift = drift  # extra wall-clock seconds per sleep: a suspend
        self.slices = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slices.append(seconds)
        self.t += seconds + self.drift


@unittest.skipUnless(WINDOWS, "poll_loop.py is Windows-only (ctypes.wintypes)")
class TestWaitForNextCycle(unittest.TestCase):
    def test_returns_immediately_when_deadline_already_passed(self):
        """A cycle that overran its own interval must not wait again."""
        clock = FakeClock()
        poll_loop.wait_for_next_cycle(
            clock.now() - 5_000, poll_loop.INTERVAL_SECONDS,
            sleep=clock.sleep, now=clock.now,
        )
        self.assertEqual(clock.slices, [])

    def test_never_sleeps_longer_than_one_slice(self):
        """The whole point: no single long timer to be mis-armed."""
        clock = FakeClock()
        poll_loop.wait_for_next_cycle(
            clock.now(), poll_loop.INTERVAL_SECONDS,
            sleep=clock.sleep, now=clock.now,
        )
        self.assertTrue(clock.slices)
        self.assertLessEqual(max(clock.slices), poll_loop.SLEEP_SLICE_SECONDS)

    def test_waits_the_full_interval_in_normal_conditions(self):
        clock = FakeClock()
        started = clock.now()
        poll_loop.wait_for_next_cycle(
            started, poll_loop.INTERVAL_SECONDS,
            sleep=clock.sleep, now=clock.now,
        )
        self.assertAlmostEqual(clock.now() - started,
                               poll_loop.INTERVAL_SECONDS, places=6)

    def test_a_suspend_mid_wait_does_not_extend_the_wait(self):
        """The regression: 13 wall-clock hours passed inside one wait.

        The clock jumps far past the deadline during a slice. The next check
        must notice and return at once rather than serving out the old timer.
        """
        clock = FakeClock(drift=13 * 3600)  # each slice "loses" 13 hours
        started = clock.now()
        poll_loop.wait_for_next_cycle(
            started, poll_loop.INTERVAL_SECONDS,
            sleep=clock.sleep, now=clock.now,
        )
        self.assertEqual(len(clock.slices), 1,
                         "should notice the jump after the first slice")

    def test_never_requests_a_negative_or_zero_sleep(self):
        clock = FakeClock()
        poll_loop.wait_for_next_cycle(
            clock.now(), 95, sleep=clock.sleep, now=clock.now,
        )
        for s in clock.slices:
            self.assertGreater(s, 0)

    def test_short_interval_is_not_overshot(self):
        """A remainder smaller than a slice must not sleep a whole slice."""
        clock = FakeClock()
        started = clock.now()
        poll_loop.wait_for_next_cycle(
            started, 5, sleep=clock.sleep, now=clock.now,
        )
        self.assertEqual(clock.now() - started, 5)


@unittest.skipUnless(WINDOWS, "poll_loop.py is Windows-only (ctypes.wintypes)")
class TestUpstreamHost(unittest.TestCase):
    def test_host_comes_from_the_pipeline_constant(self):
        """Guards against the loop drifting from the URL the ingest calls."""
        from ed_waits.common import DEFAULT_WAITS_URL

        host = poll_loop.upstream_host()
        self.assertIsNotNone(host)
        self.assertIn(host, DEFAULT_WAITS_URL)


@unittest.skipUnless(WINDOWS, "poll_loop.py is Windows-only (ctypes.wintypes)")
class TestLogFailuresAreVisible(unittest.TestCase):
    """A failed log write must leave evidence somewhere.

    Regression cover for the diagnostic hole behind two multi-hour
    investigations on 06-07/08/2026. `log()` swallowed OSError silently, so a
    loop that was running but could not write was indistinguishable from one
    that died before its first statement -- and the wrong one of those was
    assumed for days. Swallowing is still correct; swallowing without a trace
    is not.
    """

    def setUp(self):
        self._real_log = poll_loop.LOOP_LOG
        self._real_sentinel = poll_loop.LOG_FAILURE_SENTINEL
        poll_loop._lost_log_writes = 0
        self._dir = tempfile.mkdtemp()
        # Redirect the sentinel. Writing to the real one would leave entries
        # the SessionStart hook then reports as genuine failures in every
        # later session -- a self-inflicted false alarm, and the exact
        # alarm-fatigue failure this whole change exists to avoid.
        self._sentinel = os.path.join(self._dir, "sentinel.txt")
        poll_loop.LOG_FAILURE_SENTINEL = self._sentinel

    def tearDown(self):
        poll_loop.LOOP_LOG = self._real_log
        poll_loop.LOG_FAILURE_SENTINEL = self._real_sentinel
        poll_loop._lost_log_writes = 0

    def _break_logging(self):
        """Point the log at a path inside a directory that does not exist."""
        poll_loop.LOOP_LOG = os.path.join(self._dir, "no_such_dir", "loop.log")

    def _working_log(self):
        poll_loop.LOOP_LOG = os.path.join(self._dir, "loop.log")
        return poll_loop.LOOP_LOG

    def test_normal_write_lands(self):
        path = self._working_log()
        poll_loop.log("hello")
        with open(path, encoding="utf-8") as fh:
            self.assertIn("hello", fh.read())

    def test_failure_does_not_raise(self):
        self._break_logging()
        poll_loop.log("this cannot be written")  # must not raise

    def test_failure_is_counted(self):
        self._break_logging()
        poll_loop.log("one")
        poll_loop.log("two")
        self.assertEqual(poll_loop._lost_log_writes, 2)

    def test_failure_leaves_a_sentinel_outside_the_broken_directory(self):
        self._break_logging()
        poll_loop.log("vanished")
        self.assertTrue(os.path.exists(self._sentinel))
        with open(self._sentinel, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("could not write", text)
        self.assertIn(str(os.getpid()), text)

    def test_tests_do_not_write_to_the_real_sentinel(self):
        """The suite must not manufacture the alarm the hook reports."""
        default = os.path.join(tempfile.gettempdir(),
                               "aussie_health_log_failures.txt")
        before = (os.path.getsize(default) if os.path.exists(default) else 0)
        self._break_logging()
        poll_loop.log("must not touch the real sentinel")
        after = (os.path.getsize(default) if os.path.exists(default) else 0)
        self.assertEqual(before, after)

    def test_recovery_reports_what_was_lost(self):
        """The count must surface in the log itself, not only in the sentinel."""
        self._break_logging()
        poll_loop.log("lost one")
        poll_loop.log("lost two")
        path = self._working_log()
        poll_loop.log("back")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("2 earlier log write(s) failed silently", text)
        self.assertIn("back", text)
        self.assertEqual(poll_loop._lost_log_writes, 0)

    def test_counter_does_not_re_report_after_recovery(self):
        self._break_logging()
        poll_loop.log("lost")
        path = self._working_log()
        poll_loop.log("first")
        poll_loop.log("second")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text.count("failed silently"), 1)


if __name__ == "__main__":
    unittest.main()
