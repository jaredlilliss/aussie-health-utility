"""Postgres writers, exercised against a fake cursor -- no database needed."""

from __future__ import annotations

import datetime as dt
import unittest

from tests import fixture  # noqa: F401  (bootstraps sys.path for src/)

from ed_waits.common import SNAPSHOT_RETENTION_DAYS
from ed_waits.db import (
    failed_sync_detail, insert_snapshot, prune_old_snapshots,
    record_sync_run, sync_outcome,
)

LOGGER = "ingest_ed_waits"
WHEN = dt.datetime(2026, 7, 19, 9, 42, tzinfo=dt.timezone.utc)


class _Cursor:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount
        self.executed: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))


class _Conn:
    def __init__(self, rowcount=1):
        self._cursor = _Cursor(rowcount)

    def cursor(self):
        return self._cursor


class InsertSnapshotTests(unittest.TestCase):

    def test_a_stored_row_is_silent(self):
        conn = _Conn(rowcount=1)
        with self.assertNoLogs(LOGGER, level="WARNING"):
            insert_snapshot(conn, "209", WHEN, 4, 29, False, None,
                            source="nsw_rted")

    def test_a_dropped_row_warns(self):
        """Regression: INSERT ... SELECT writes nothing on a lookup miss.

        The facility subselect returning no rows meant the observation was
        discarded with no error at all -- a source/source_id mismatch would
        silently stop recording wait counts.
        """
        conn = _Conn(rowcount=0)
        with self.assertLogs(LOGGER, level="WARNING") as caught:
            insert_snapshot(conn, "209", WHEN, 4, source="wrong_source")
        joined = " ".join(caught.output)
        self.assertIn("209", joined)
        self.assertIn("wrong_source", joined)

    def test_parameters_are_passed_through(self):
        conn = _Conn()
        insert_snapshot(conn, "209", WHEN, 4, 29, True, "outage text",
                        source="nsw_rted")
        _sql, params = conn._cursor.executed[0]
        self.assertEqual(params["source"], "nsw_rted")
        self.assertEqual(params["source_id"], "209")
        self.assertEqual(params["captured_at"], WHEN)
        self.assertEqual(params["patients_waiting"], 4)
        self.assertEqual(params["treatment_spaces"], 29)
        self.assertTrue(params["outage"])
        self.assertEqual(params["outage_text"], "outage text")

    def test_na_count_is_stored_as_null_not_zero(self):
        conn = _Conn()
        insert_snapshot(conn, "209", WHEN, None)
        _sql, params = conn._cursor.executed[0]
        self.assertIsNone(params["patients_waiting"])


class PruneTests(unittest.TestCase):

    def test_returns_rows_removed(self):
        conn = _Conn(rowcount=17)
        self.assertEqual(prune_old_snapshots(conn), 17)

    def test_retention_days_are_parameterised(self):
        conn = _Conn()
        prune_old_snapshots(conn, days=30)
        _sql, params = conn._cursor.executed[0]
        self.assertEqual(params["days"], 30)

    def test_default_comes_from_the_configured_constant(self):
        """Called with no days, the prune must use SNAPSHOT_RETENTION_DAYS.

        Deliberately asserts the wiring, not the value. An assertion like
        `== 365` would have to be edited every time the policy changes, which
        is how a test stops being a check and becomes a chore. This one fails
        only if someone hardcodes a literal back into the default.
        """
        conn = _Conn()
        prune_old_snapshots(conn)
        _sql, params = conn._cursor.executed[0]
        self.assertEqual(params["days"], SNAPSHOT_RETENTION_DAYS)


class SyncRunTests(unittest.TestCase):

    def test_records_the_operational_row(self):
        conn = _Conn()
        record_sync_run(conn, "nsw_ed_waits", WHEN, ok=False,
                        detail="outage_checked=False")
        _sql, params = conn._cursor.executed[0]
        self.assertEqual(params["pipeline"], "nsw_ed_waits")
        self.assertEqual(params["started_at"], WHEN)
        self.assertFalse(params["ok"])
        self.assertIn("outage_checked=False", params["detail"])


class SyncOutcomeTests(unittest.TestCase):
    """Regression: ok was hardcoded True and only written on success.

    A five-day outage (25-30/07/2026) was therefore visible only as a gap
    between consecutive ids, with every surrounding row claiming ok=true.
    `where not ok` must find every cycle that did not fully do its job.
    """

    def test_a_verified_live_cycle_is_ok(self):
        ok, detail = sync_outcome(59, 0, outage_checked=True, replay=False)
        self.assertTrue(ok)
        self.assertIn("status=ok", detail)
        self.assertIn("snapshots=59", detail)

    def test_a_failed_outage_check_is_not_ok(self):
        ok, detail = sync_outcome(59, 0, outage_checked=False, replay=False)
        self.assertFalse(ok)
        self.assertIn("status=degraded", detail)
        self.assertIn("outage_checked=False", detail)

    def test_a_replay_is_not_ok(self):
        """A fixture replay is not a live observation, however clean."""
        ok, detail = sync_outcome(59, 0, outage_checked=True, replay=True)
        self.assertFalse(ok)
        self.assertIn("status=replay", detail)

    def test_the_three_false_cases_stay_separable(self):
        statuses = {
            sync_outcome(1, 0, outage_checked=False, replay=False)[1].split()[0],
            sync_outcome(1, 0, outage_checked=True, replay=True)[1].split()[0],
            failed_sync_detail(RuntimeError("boom")).split()[0],
        }
        self.assertEqual(
            statuses,
            {"status=degraded", "status=replay", "status=failed"},
        )

    def test_a_failure_names_the_cause(self):
        detail = failed_sync_detail(ConnectionError("host unreachable"))
        self.assertIn("status=failed", detail)
        self.assertIn("ConnectionError", detail)
        self.assertIn("host unreachable", detail)

    def test_a_huge_exception_message_is_truncated(self):
        """detail is operational metadata, not a log sink."""
        detail = failed_sync_detail(RuntimeError("x" * 5000))
        self.assertLessEqual(len(detail), 500)
        self.assertTrue(detail.startswith("status=failed"))


if __name__ == "__main__":
    unittest.main()
