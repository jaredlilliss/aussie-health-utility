"""Facilities leg: Data.NSW CKAN directory feed -> FacilityRow list."""

from __future__ import annotations

import argparse
import json
import logging
from typing import Any

from poller import JsonPoller, PollerConfig

from ed_waits.common import ED_FLAG_MAP, USER_AGENT, FacilityRow, _clean

log = logging.getLogger("ingest_ed_waits")


# --------------------------------------------------------------------- #
# Extract
# --------------------------------------------------------------------- #

def load_payload(args: argparse.Namespace) -> Any:
    if args.from_file:
        log.info("loading payload from file %s", args.from_file)
        with open(args.from_file, encoding="utf-8") as fh:
            return json.load(fh)

    cfg = PollerConfig(url=args.source_url, user_agent=USER_AGENT)
    poller = JsonPoller(cfg, on_payload=lambda _: None,
                        validate=validate_payload)
    log.info("fetching %s", args.source_url)
    payload = poller.fetch_once()
    if payload is None:  # 304: cannot happen on first fetch, defensive only
        raise RuntimeError("no payload received (304 on first fetch?)")
    return payload


def validate_payload(payload: Any) -> None:
    """Drift alarm: raise if the CKAN envelope is not what we mapped."""
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError("CKAN envelope: success != true")
    records = payload.get("result", {}).get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("CKAN envelope: result.records missing or empty")
    first = records[0]
    for field in ("Name", "ED"):
        if field not in first:
            raise ValueError(f"CKAN record shape changed: missing {field!r}")


# --------------------------------------------------------------------- #
# Transform
# --------------------------------------------------------------------- #

def transform(payload: Any) -> list[FacilityRow]:
    result = payload["result"]
    records = result["records"]
    total = result.get("total")
    if isinstance(total, int) and total > len(records):
        log.warning(
            "payload holds %d of %d records; raise limit or follow "
            "result._links.next to page", len(records), total,
        )

    rows: list[FacilityRow] = []
    for rec in records:
        name = _clean(rec.get("Name"))
        if not name:
            log.warning("skipping record with no Name: %r", rec)
            continue
        ed_flag = _clean(rec.get("ED")) or ""
        facility_type = ED_FLAG_MAP.get(ed_flag)
        if facility_type is None:
            log.warning("unknown ED flag %r for %s; storing raw value "
                        "(schema drift?)", ed_flag, name)
            facility_type = f"unmapped:{ed_flag}"
        rows.append(FacilityRow(
            source_id=name,            # directory has no numeric ID; names
            name=name,                 # are unique in this feed
            facility_type=facility_type,
            address=_clean(rec.get("Address")),
            suburb=_clean(rec.get("Suburb")),
            postcode=_clean(rec.get("Postcode")),
            lhd=_clean(rec.get("LHD")),
            phone=_clean(rec.get("Phone")),
        ))
    return rows
