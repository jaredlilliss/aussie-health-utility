"""ingest_mbs_xml.py: streaming ETL for the MBS schedule XML into mbs_items.

Parses the official MBS XML release (root <MBS_XML>, flat <Data> records;
structure verified against MBS-XML-20260301-version 2 on 12/06/2026) and
batch-UPSERTs into the mbs_items table defined in db/init/01_schema.sql
(contract: 03_Local_Cache/Postgres_Cache_Schema.md).

Production files are large and arrive as one unbroken line, so parsing is
streaming-only: defusedxml.ElementTree.iterparse, one <Data> record in memory
at a time, root cleared as we go. Never load the whole tree.

Run sheet (machine with Docker + the compose stack from Local_Deployment.md):

    python src/ingest_mbs_xml.py                          # mock data, dry default DB
    python src/ingest_mbs_xml.py --xml-path MBS-XML-20260301-version 2.XML \
        --source-release 2026-03-01                       # real release

Flags:
    --xml-path PATH         default src/data/mock_mbs.xml
    --source-release DATE   ISO date of the release; if omitted, derived from
                            a YYYYMMDD in the filename, else today (warned)
    --database-url ...      default env DATABASE_URL or local compose DSN
    --dry-run               parse + print, no database needed

Field mapping (verified-real tag -> column):
    ItemNum         -> item_num        (int, required)
    Description     -> descriptor      (required; record skipped + warned if absent)
    Category, Group -> category        (stored as 'Category/Group', e.g. '1/A1')
    ScheduleFee     -> schedule_fee    (NULL for derived-fee items: FeeType D
                                        records carry DerivedFee text and no
                                        ScheduleFee element at all)
    Benefit75/85/100 -> benefit_75/85/100 (read independently; GP items carry
                                        Benefit100 only, so absences are normal)
    --source-release -> source_release

Dates in the feed are DD.MM.YYYY; none are stored in v1, noted for future use.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator, Optional

# defusedxml, not xml.etree: the stdlib parser expands entities, so a release
# file carrying a billion-laughs / quadratic-blowup payload would exhaust
# memory before a single record is yielded. Imported directly rather than
# behind a try/except fallback on purpose -- silently degrading to the unsafe
# parser is exactly the "quietly ingest garbage" failure this codebase's
# drift alarms exist to prevent. It is a hard dependency (src/requirements.txt).
# forbid_dtd is left at its default (False): the real MBS releases carry no
# DTD, but entity and external-reference expansion are what actually bite,
# and defusedxml forbids both by default.
from defusedxml.ElementTree import iterparse  # noqa: E402

log = logging.getLogger("ingest_mbs_xml")

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_XML = os.path.join(HERE, "data", "mock_mbs.xml")
# Passwordless by design; libpq reads pgpass.conf. See the note in
# src/ed_waits/common.py -- a literal here is a committed credential.
DEFAULT_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://rted@127.0.0.1:5432/aussie_health",
)
RECORD_TAG = "Data"
BATCH_SIZE = 1000


@dataclass
class MbsRow:
    item_num: int
    descriptor: str
    category: Optional[str]
    schedule_fee: Optional[Decimal]
    benefit_75: Optional[Decimal]
    benefit_85: Optional[Decimal]
    benefit_100: Optional[Decimal]
    is_derived: bool


# --------------------------------------------------------------------- #
# Extract: streaming
# --------------------------------------------------------------------- #

def stream_records(xml_path: str) -> Iterator[dict[str, str]]:
    """Yield one <Data> record at a time as {tag: text}; constant memory.

    Drift alarm: raises if the root element is not <MBS_XML> or if the file
    yields zero <Data> records, so a silently restructured release fails
    loudly instead of ingesting nothing.
    """
    context = iterparse(xml_path, events=("start", "end"))
    event, root = next(context)  # first event is the root's start
    if root.tag != "MBS_XML":
        raise ValueError(
            f"unexpected root element <{root.tag}>; expected <MBS_XML>. "
            "Release structure may have changed: diff against the real file."
        )
    count = 0
    for event, elem in context:
        if event == "end" and elem.tag == RECORD_TAG:
            yield {child.tag: (child.text or "").strip() for child in elem}
            count += 1
            root.clear()  # drop processed records: keeps memory flat
    if count == 0:
        raise ValueError(
            "no <Data> records found; release structure may have changed."
        )


# --------------------------------------------------------------------- #
# Transform
# --------------------------------------------------------------------- #

def _dec(raw: Optional[str], item: str, field: str) -> Optional[Decimal]:
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        log.warning("item %s: unparseable %s value %r; storing NULL",
                    item, field, raw)
        return None


def to_row(raw: dict[str, str]) -> Optional[MbsRow]:
    item_raw = raw.get("ItemNum", "")
    if not item_raw.isdigit():
        log.warning("skipping record with missing/non-numeric ItemNum: %r",
                    item_raw)
        return None
    descriptor = raw.get("Description", "")
    if not descriptor:
        log.warning("skipping item %s: empty Description", item_raw)
        return None

    category = "/".join(p for p in (raw.get("Category", ""),
                                    raw.get("Group", "")) if p) or None
    is_derived = raw.get("FeeType", "") == "D" or bool(raw.get("DerivedFee"))
    if is_derived and raw.get("ScheduleFee"):
        log.warning("item %s: both DerivedFee and ScheduleFee present; "
                    "keeping ScheduleFee", item_raw)

    return MbsRow(
        item_num=int(item_raw),
        descriptor=descriptor,
        category=category,
        schedule_fee=_dec(raw.get("ScheduleFee"), item_raw, "ScheduleFee"),
        benefit_75=_dec(raw.get("Benefit75"), item_raw, "Benefit75"),
        benefit_85=_dec(raw.get("Benefit85"), item_raw, "Benefit85"),
        benefit_100=_dec(raw.get("Benefit100"), item_raw, "Benefit100"),
        is_derived=is_derived,
    )


def derive_source_release(args: argparse.Namespace) -> date:
    if args.source_release:
        return date.fromisoformat(args.source_release)
    match = re.search(r"(\d{8})", os.path.basename(args.xml_path))
    if match:
        derived = datetime.strptime(match.group(1), "%Y%m%d").date()
        log.info("source_release derived from filename: %s", derived)
        return derived
    today = date.today()
    log.warning("no --source-release and none derivable from filename; "
                "defaulting to today (%s). Pass the official release date "
                "for real ingests.", today)
    return today


# --------------------------------------------------------------------- #
# Load (psycopg2 imported lazily so --dry-run needs no driver)
# --------------------------------------------------------------------- #

UPSERT_MBS_SQL = """
insert into mbs_items
  (item_num, descriptor, category, schedule_fee,
   benefit_75, benefit_85, benefit_100, source_release)
values %s
on conflict (item_num) do update set
  descriptor     = excluded.descriptor,
  category       = excluded.category,
  schedule_fee   = excluded.schedule_fee,
  benefit_75     = excluded.benefit_75,
  benefit_85     = excluded.benefit_85,
  benefit_100    = excluded.benefit_100,
  source_release = excluded.source_release;
"""


def upsert_batches(conn, rows: Iterator[Optional[MbsRow]],
                   source_release: date) -> tuple[int, int]:
    """Stream rows into batched UPSERTs inside one transaction."""
    from psycopg2.extras import execute_values

    loaded = skipped = 0
    batch: list[tuple] = []
    with conn.cursor() as cur:
        for row in rows:
            if row is None:
                skipped += 1
                continue
            batch.append((row.item_num, row.descriptor, row.category,
                          row.schedule_fee, row.benefit_75, row.benefit_85,
                          row.benefit_100, source_release))
            if len(batch) >= BATCH_SIZE:
                execute_values(cur, UPSERT_MBS_SQL, batch)
                loaded += len(batch)
                batch.clear()
        if batch:
            execute_values(cur, UPSERT_MBS_SQL, batch)
            loaded += len(batch)
    return loaded, skipped


# --------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------- #

def print_rows(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("select count(*), count(schedule_fee) from mbs_items;")
        total, with_fee = cur.fetchone()
        print(f"\nmbs_items rows: {total} ({total - with_fee} derived-fee "
              "items with NULL schedule_fee)")
        cur.execute(
            "select item_num, category, schedule_fee, benefit_85, "
            "benefit_100, left(descriptor, 48) from mbs_items "
            "order by item_num limit 10;")
        print("\nfirst 10 items:")
        print(f"  {'item':>6} {'cat':<6} {'fee':>8} {'b85':>8} {'b100':>8}  descriptor")
        for item, cat, fee, b85, b100, desc in cur.fetchall():
            print(f"  {item:>6} {cat or '':<6} {fee or '':>8} {b85 or '':>8} "
                  f"{b100 or '':>8}  {desc}")


def print_dry_run(rows: list[MbsRow], skipped: int,
                  source_release: date) -> None:
    print(f"dry run: {len(rows)} rows would be upserted into mbs_items "
          f"(source_release={source_release}, skipped={skipped}):\n")
    print(f"  {'item':>6} {'cat':<6} {'fee':>8} {'b75':>8} {'b85':>8} "
          f"{'b100':>8}  descriptor")
    print("  " + "-" * 92)
    for r in rows:
        fee = "derived" if r.is_derived and r.schedule_fee is None else (r.schedule_fee or "")
        print(f"  {r.item_num:>6} {r.category or '':<6} {fee!s:>8} "
              f"{r.benefit_75 or '':>8} {r.benefit_85 or '':>8} "
              f"{r.benefit_100 or '':>8}  {r.descriptor[:46]}")


# --------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--xml-path", default=DEFAULT_XML)
    parser.add_argument("--source-release", metavar="YYYY-MM-DD")
    parser.add_argument("--database-url", default=DEFAULT_DSN)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    source_release = derive_source_release(args)
    log.info("parsing %s", args.xml_path)

    if args.dry_run:
        rows, skipped = [], 0
        for raw in stream_records(args.xml_path):
            row = to_row(raw)
            if row is None:
                skipped += 1
            else:
                rows.append(row)
        print_dry_run(rows, skipped, source_release)
        return 0

    import psycopg2  # lazy: --dry-run must work without the driver
    log.info("connecting to %s", args.database_url.split("@")[-1])
    with psycopg2.connect(args.database_url) as conn:
        loaded, skipped = upsert_batches(
            conn, (to_row(raw) for raw in stream_records(args.xml_path)),
            source_release,
        )
        conn.commit()
        log.info("upsert committed: %d loaded, %d skipped", loaded, skipped)
        print_rows(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
