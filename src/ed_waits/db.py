"""Load: Postgres writers (psycopg2 imported lazily so --dry-run needs no driver)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from ed_waits.common import SNAPSHOT_RETENTION_DAYS, SOURCE, FacilityRow

log = logging.getLogger("ingest_ed_waits")

PRUNE_SNAPSHOTS_SQL = """
delete from ed_wait_snapshots
where captured_at < now() - make_interval(days => %(days)s);
"""

UPSERT_FACILITIES_SQL = """
insert into facilities
  (source, source_id, name, facility_type, address, suburb, postcode,
   lhd, phone, lat, lng, refreshed_at)
values %s
on conflict (source, source_id) do update set
  name          = excluded.name,
  facility_type = excluded.facility_type,
  address       = excluded.address,
  suburb        = excluded.suburb,
  postcode      = excluded.postcode,
  lhd           = excluded.lhd,
  phone         = excluded.phone,
  lat           = excluded.lat,
  lng           = excluded.lng,
  refreshed_at  = now();
"""

INSERT_SNAPSHOT_SQL = """
insert into ed_wait_snapshots
  (facility_id, captured_at, patients_waiting, treatment_spaces,
   outage, outage_text)
select f.id, %(captured_at)s, %(patients_waiting)s, %(treatment_spaces)s,
       %(outage)s, %(outage_text)s
from facilities f
where f.source = %(source)s and f.source_id = %(source_id)s
on conflict (facility_id, captured_at) do nothing;
"""


def upsert_facilities(conn, rows: list[FacilityRow],
                      source: str = SOURCE) -> None:
    from psycopg2.extras import execute_values

    values = [(source, r.source_id, r.name, r.facility_type, r.address,
               r.suburb, r.postcode, r.lhd, r.phone, r.lat, r.lng)
              for r in rows]
    with conn.cursor() as cur:
        execute_values(
            cur, UPSERT_FACILITIES_SQL, values,
            template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())",
        )


def insert_snapshot(conn, source_id: str, captured_at: datetime,
                    patients_waiting: Optional[int],
                    treatment_spaces: Optional[int] = None,
                    outage: bool = False,
                    outage_text: Optional[str] = None,
                    source: str = SOURCE) -> None:
    """Append one wait-count observation. Duplicate (facility, time) is a no-op."""
    with conn.cursor() as cur:
        cur.execute(INSERT_SNAPSHOT_SQL, {
            "source": source, "source_id": source_id,
            "captured_at": captured_at,
            "patients_waiting": patients_waiting,
            "treatment_spaces": treatment_spaces,
            "outage": outage, "outage_text": outage_text,
        })
        if cur.rowcount == 0:
            # INSERT ... SELECT writes nothing when the facility lookup
            # misses, so a source/source_id mismatch drops observations
            # with no error at all. The only benign cause is the
            # ON CONFLICT DO NOTHING duplicate, which needs an identical
            # captured_at and so should not occur on a live cycle.
            log.warning("snapshot not stored for %s/%s at %s: no matching "
                        "facility row, or duplicate captured_at",
                        source, source_id, captured_at)


INSERT_SYNC_RUN_SQL = """
insert into sync_runs (pipeline, started_at, finished_at, ok, detail)
values (%(pipeline)s, %(started_at)s, now(), %(ok)s, %(detail)s);
"""


def sync_outcome(snapshots: int, pruned: int, outage_checked: bool,
                 replay: bool) -> tuple[bool, str]:
    """(ok, detail) for a cycle that committed.

    `ok` means a complete, trustworthy LIVE observation: it committed, the
    outage check ran, and it was not a fixture replay. Anything less is
    False so that `select * from sync_runs where not ok` finds every cycle
    that did not fully do its job -- which is the entire point of the table.
    `detail` leads with status= so the false cases stay separable.
    """
    ok = outage_checked and not replay
    status = "ok" if ok else ("replay" if replay else "degraded")
    return ok, (f"status={status} snapshots={snapshots} pruned={pruned} "
                f"outage_checked={outage_checked} replay={replay}")


def failed_sync_detail(exc: BaseException) -> str:
    """detail for a cycle that raised. Truncated: detail is not a log."""
    return f"status=failed {type(exc).__name__}: {exc}"[:500]


def record_sync_run(conn, pipeline: str, started_at: datetime,
                    ok: bool, detail: str) -> None:
    """One operational-metadata row per cycle (the sync_runs contract).

    Makes a degraded cycle (e.g. outage check failed) distinguishable in
    the database from a fully verified one; without this, outage=false
    rows read identically in both cases.

    A row is written whether the cycle succeeded or failed. That matters:
    when this only recorded successes with a hardcoded ok=True, a five-day
    outage (25-30/07/2026) showed up solely as a gap between consecutive
    ids, with every surrounding row claiming ok=true. An outage must be
    visible as a FAILURE, never as an absence.
    """
    with conn.cursor() as cur:
        cur.execute(INSERT_SYNC_RUN_SQL, {
            "pipeline": pipeline, "started_at": started_at,
            "ok": ok, "detail": detail,
        })


def prune_old_snapshots(conn, days: int = SNAPSHOT_RETENTION_DAYS) -> int:
    """Delete raw snapshots past the retention window; returns rows removed.

    Cheap enough to run every poll cycle at this table's scale (a few
    hundred thousand rows/month): no dedicated index on captured_at alone,
    add one if this table ever grows past the point a periodic scan is fine.
    """
    with conn.cursor() as cur:
        cur.execute(PRUNE_SNAPSHOTS_SQL, {"days": days})
        return cur.rowcount
