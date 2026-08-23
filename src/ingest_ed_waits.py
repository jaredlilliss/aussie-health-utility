"""ingest_ed_waits.py: ETL for the NSW ED pipeline (facilities leg + snapshot writer).

Loads the confirmed, auth-free facilities feed (Data.NSW CKAN mirror of the
RTED hospital directory) into the local Postgres cache, and provides the
append-only writer for ed_wait_snapshots, ready for the live wait-count
route once it is captured (see 02_Data_Pipelines/NSW_Health_JSON_Engine.md,
"Remaining (one browser step)").

Run sheet (on a machine with Docker + network):

    docker compose up -d                      # first boot auto-runs db/init/
    pip install -r src/requirements.txt
    python src/ingest_ed_waits.py             # live fetch -> upsert -> print rows

Useful flags:

    --from-file fixtures/x.json   ingest a saved payload instead of fetching
    --dry-run                     transform + print, no database needed
    --database-url ...            default: env DATABASE_URL or local compose DSN
    --wait-endpoint URL           capture a raw wait-count payload to fixtures/
                                  and exit
    --waits [URL]                 ingest the live wait counts: one
                                  GetHospitalDetails call carries every
                                  reporting hospital statewide (see vault:
                                  NSW_Health_JSON_Engine.md, spike 19/07/2026)

Design notes:
  * psycopg2 over asyncpg, deliberately: this is a small, sequential batch
    job on a 15-minute cadence. Async buys nothing here and costs debugging
    clarity. Revisit only if ingestion ever becomes concurrent.
  * The fetch leg reuses JsonPoller (honest UA, retries, backoff,
    drift-alarm hook) so conduct policy lives in exactly one place.
  * UPSERT is ON CONFLICT (source, source_id) DO UPDATE for directory data;
    snapshots are ON CONFLICT DO NOTHING because the series is append-only.
  * Implementation lives in the ed_waits package (common, facilities, waits,
    db, report); this file is the CLI entry point and orchestration only.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from poller import JsonPoller, PollerConfig  # noqa: E402
from ed_waits.common import (  # noqa: E402
    DEFAULT_DSN, DEFAULT_FACILITIES_URL, DEFAULT_WAITS_URL, RTED_SOURCE,
    USER_AGENT,
)
from ed_waits.db import (  # noqa: E402
    failed_sync_detail, insert_snapshot, prune_old_snapshots,
    record_sync_run, sync_outcome, upsert_facilities,
)
from ed_waits.facilities import load_payload, transform, validate_payload  # noqa: E402
from ed_waits.report import (  # noqa: E402
    print_dry_run, print_rows, print_waits_dry_run,
)
from ed_waits.outages import apply_outages, fetch_active_outages  # noqa: E402
from ed_waits.waits import parse_wait_payload, validate_wait_payload  # noqa: E402

log = logging.getLogger("ingest_ed_waits")


def capture_wait_sample(url: str) -> int:
    """Fetch the live wait-count endpoint once and save the raw payload."""
    cfg = PollerConfig(url=url, user_agent=USER_AGENT)
    payload = JsonPoller(cfg, on_payload=lambda _: None).fetch_once()
    os.makedirs("fixtures", exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join("fixtures", f"wait_payload_{stamp}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"sample wait payload saved to {path}\n"
          "Next: map its fields in the vault note, then implement "
          "parse_wait_payload().")
    return 0


def _record_failed_run(database_url: str, started_at: datetime,
                       exc: BaseException) -> None:
    """Write the ok=False row for a cycle that raised, on its OWN connection.

    The cycle's transaction is rolled back when it raises, so a row written
    inside it would vanish with everything else -- which is precisely how a
    failing pipeline used to leave no trace at all. Best-effort: if the
    database is the thing that is down, there is nowhere to record that, and
    the original exception matters more than this bookkeeping.
    """
    try:
        import psycopg2
        conn = psycopg2.connect(database_url)
        try:
            record_sync_run(conn, "nsw_ed_waits", started_at, ok=False,
                            detail=failed_sync_detail(exc))
            conn.commit()
        finally:
            conn.close()
    except Exception as rec_exc:  # noqa: BLE001
        log.error("could not record the failed cycle in sync_runs: %s",
                  rec_exc)


def ingest_waits(args: argparse.Namespace) -> int:
    """Run one cycle, guaranteeing a sync_runs row either way."""
    started_at = datetime.now(timezone.utc)
    try:
        return _ingest_waits(args, started_at)
    except Exception as exc:
        # A dry run never touches the database, so it has nothing to record.
        if not args.dry_run:
            _record_failed_run(args.database_url, started_at, exc)
        raise


def _ingest_waits(args: argparse.Namespace, started_at: datetime) -> int:
    if args.from_file:
        log.info("loading wait payload from file %s", args.from_file)
        with open(args.from_file, encoding="utf-8") as fh:
            payload = json.load(fh)
    else:
        cfg = PollerConfig(url=args.waits, user_agent=USER_AGENT)
        log.info("fetching %s", args.waits)
        payload = JsonPoller(cfg, on_payload=lambda _: None).fetch_once()
        if payload is None:  # 304: cannot happen on a fresh poller, defensive
            raise RuntimeError("no wait payload received (unexpected 304)")

    validate_wait_payload(payload)
    facilities, snapshots = parse_wait_payload(payload)
    log.info("parsed %d facilities, %d snapshots", len(facilities),
             len(snapshots))

    # Outage flags come from the site's SharePoint list, not the RTED API.
    # Live mode only: a fixture replay stays deterministic. Fetch failure
    # degrades to no-outage-info rather than blocking the wait counts; the
    # sync_runs row below records which of the two happened.
    outage_checked = False
    if not args.from_file:
        try:
            apply_outages(facilities, snapshots, fetch_active_outages())
            outage_checked = True
        except Exception as exc:  # noqa: BLE001
            log.error("outage check failed; snapshots carry no outage "
                      "info this cycle: %s", exc)

    if args.dry_run:
        print_waits_dry_run(snapshots)
        return 0

    if args.from_file and not args.allow_stale_commit:
        # A replayed fixture would be committed with captured_at=now(),
        # entering old counts into the append-only series as live data.
        log.error("refusing to commit a fixture replay as live data; use "
                  "--dry-run, or --allow-stale-commit to override")
        return 2

    import psycopg2
    log.info("connecting to %s", args.database_url.split("@")[-1])
    with psycopg2.connect(args.database_url) as conn:
        upsert_facilities(conn, facilities, source=RTED_SOURCE)
        for s in snapshots:
            insert_snapshot(conn, s["source_id"], s["captured_at"],
                            s["patients_waiting"], s["treatment_spaces"],
                            s["outage"], s["outage_text"],
                            source=RTED_SOURCE)
        pruned = prune_old_snapshots(conn)
        ok, detail = sync_outcome(len(snapshots), pruned, outage_checked,
                                  bool(args.from_file))
        record_sync_run(conn, "nsw_ed_waits", started_at, ok=ok,
                        detail=detail)
        conn.commit()
        log.info("waits committed (%d snapshot(s) pruned past retention)",
                 pruned)
        print_rows(conn)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-url", default=DEFAULT_FACILITIES_URL)
    parser.add_argument("--from-file", metavar="PATH")
    parser.add_argument("--database-url", default=DEFAULT_DSN)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--wait-endpoint", metavar="URL",
                        help="capture a live wait-count sample and exit")
    parser.add_argument("--waits", metavar="URL", nargs="?",
                        const=DEFAULT_WAITS_URL,
                        help="ingest live wait counts (whole state per call); "
                             "combine with --from-file to replay a fixture")
    parser.add_argument("--allow-stale-commit", action="store_true",
                        help="permit committing a --from-file replay "
                             "(captured_at will be now, not capture time)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.wait_endpoint:
        return capture_wait_sample(args.wait_endpoint)
    if args.waits:
        return ingest_waits(args)

    payload = load_payload(args)
    validate_payload(payload)
    rows = transform(payload)
    log.info("transformed %d facility rows", len(rows))

    if args.dry_run:
        print_dry_run(rows)
        return 0

    import psycopg2  # lazy: --dry-run must work without the driver
    log.info("connecting to %s", args.database_url.split("@")[-1])
    with psycopg2.connect(args.database_url) as conn:
        upsert_facilities(conn, rows)
        conn.commit()
        log.info("upsert committed")
        print_rows(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
