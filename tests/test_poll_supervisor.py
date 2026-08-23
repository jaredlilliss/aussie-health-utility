"""The supervisor's restart decision.

The measured failure it exists for: on 04/08/2026 the loop stopped polling for
13 hours while the machine was awake and its process was still alive. The two
things that must both hold are in tension, so both are pinned here:

  * a genuine wedge (awake, not polling) triggers a restart promptly, and
  * a normal overnight standby NEVER does -- which is why staleness is measured
    in awake time. An alarm that fires every night is an alarm nobody reads.

Offline like the rest of the suite: the decision is a pure function, so nothing
here touches processes, the clock, or the network.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

from tests import SCRIPTS  # noqa: F401  (bootstraps sys.path for scripts/)

WINDOWS = sys.platform == "win32"
if WINDOWS:
    os.environ.setdefault("LOCALAPPDATA", tempfile.gettempdir())
    import poll_supervisor as sup


@unittest.skipUnless(WINDOWS, "poll_supervisor.py is Windows-only (ctypes/kernel32)")
class TestShouldRestart(unittest.TestCase):
    def test_fresh_heartbeat_is_left_alone(self):
        restart, _ = sup.should_restart(60, None)
        self.assertFalse(restart)

    def test_missing_heartbeat_file_is_not_a_wedge(self):
        """First run on a fresh machine must not thrash."""
        restart, reason = sup.should_restart(None, None)
        self.assertFalse(restart)
        self.assertIn("no heartbeat", reason)

    def test_just_under_threshold_holds(self):
        restart, _ = sup.should_restart(sup.STALE_AFTER_AWAKE_SECONDS - 1, None)
        self.assertFalse(restart)

    def test_the_measured_wedge_triggers_a_restart(self):
        """13 awake hours with no poll: the 04/08 signature."""
        restart, reason = sup.should_restart(13 * 3600, None)
        self.assertTrue(restart)
        self.assertIn("AWAKE", reason)

    def test_overnight_standby_never_triggers_a_restart(self):
        """9 hours asleep accrues almost no AWAKE time, so nothing fires.

        This is the false-alarm case that would make the supervisor useless.
        """
        awake_during_9h_standby = 20  # seconds; DRIPS time is excluded
        restart, _ = sup.should_restart(awake_during_9h_standby, None)
        self.assertFalse(restart)

    def test_restarts_are_rate_limited(self):
        """A wedge that persists must not become a restart storm."""
        restart, reason = sup.should_restart(13 * 3600, 60)
        self.assertFalse(restart)
        self.assertIn("holding off", reason)

    def test_rate_limit_expires(self):
        restart, _ = sup.should_restart(
            13 * 3600, sup.MIN_RESTART_SPACING_SECONDS + 1)
        self.assertTrue(restart)

    def test_restart_spacing_respects_the_conduct_floor(self):
        """Even back-to-back restarts cannot poll faster than policy allows."""
        self.assertGreaterEqual(sup.MIN_RESTART_SPACING_SECONDS, 600)

    def test_threshold_is_at_least_two_missed_polls(self):
        """Guards against someone tuning it down into false-alarm territory."""
        self.assertGreaterEqual(sup.STALE_AFTER_AWAKE_SECONDS, 2 * 900)


@unittest.skipUnless(WINDOWS, "poll_supervisor.py is Windows-only (ctypes/kernel32)")
class TestAwakeClock(unittest.TestCase):
    def test_awake_seconds_is_available_and_sane(self):
        """The whole design rests on this API existing on this machine."""
        value = sup.awake_seconds()
        self.assertGreater(value, 0)
        self.assertLess(value, 10 * 365 * 24 * 3600)

    def test_awake_clock_advances(self):
        first = sup.awake_seconds()
        second = sup.awake_seconds()
        self.assertGreaterEqual(second, first)


@unittest.skipUnless(WINDOWS, "poll_supervisor.py is Windows-only (ctypes/kernel32)")
class TestSupervisorNeverPolls(unittest.TestCase):
    def test_supervisor_does_not_reference_the_ingest(self):
        """Exactly one path to the NSW feed exists, and it is not this file."""
        with open(sup.__file__, encoding="utf-8") as fh:
            source = fh.read()
        for forbidden in ("poll_waits", "ingest_ed_waits", "requests"):
            self.assertNotIn(
                f"import {forbidden}", source,
                f"the supervisor must never reach the feed itself ({forbidden})",
            )


if __name__ == "__main__":
    unittest.main()
