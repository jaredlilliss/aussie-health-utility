"""Report: row-count summaries and dry-run tables printed to stdout."""

from __future__ import annotations

from ed_waits.common import FacilityRow


def print_rows(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("select facility_type, count(*) from facilities "
                    "group by 1 order by 1;")
        print("\nfacilities by type:")
        for ftype, count in cur.fetchall():
            print(f"  {ftype:<22} {count}")

        cur.execute("select id, name, suburb, lhd, phone from facilities "
                    "order by name limit 10;")
        print("\nfirst 10 facilities:")
        for row in cur.fetchall():
            print("  " + " | ".join(str(c) if c is not None else "" for c in row))

        cur.execute("select count(*) from ed_wait_snapshots;")
        print(f"\ned_wait_snapshots rows: {cur.fetchone()[0]}")


def print_waits_dry_run(snapshots: list[dict]) -> None:
    if not snapshots:
        print("dry run: no snapshots parsed")
        return
    print(f"dry run: {len(snapshots)} wait-count snapshots "
          f"(captured_at {snapshots[0]['captured_at']:%Y-%m-%d %H:%M:%SZ}):")
    header = f"  {'id':>4}  {'hospital':<50} {'waiting':>7}  spaces"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for s in snapshots:
        waiting = "NA" if s["patients_waiting"] is None else s["patients_waiting"]
        spaces = s["treatment_spaces"] if s["treatment_spaces"] is not None else ""
        flag = f"  OUTAGE: {(s['outage_text'] or '')[:60]}" if s["outage"] else ""
        print(f"  {s['source_id']:>4}  {(s['name'] or '')[:50]:<50} "
              f"{waiting:>7}  {spaces}{flag}")


def print_dry_run(rows: list[FacilityRow]) -> None:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.facility_type] = counts.get(r.facility_type, 0) + 1
    print("dry run: rows that would be upserted into facilities "
          f"({len(rows)} total):")
    for ftype in sorted(counts):
        print(f"  {ftype:<22} {counts[ftype]}")
    print()
    header = f"  {'name':<42} {'type':<18} {'suburb':<14} phone"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in rows:
        print(f"  {r.name[:42]:<42} {r.facility_type:<18} "
              f"{(r.suburb or ''):<14} {r.phone or ''}")
