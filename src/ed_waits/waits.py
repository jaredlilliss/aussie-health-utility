"""Waits leg: RTED GetHospitalDetails payload -> facility + snapshot rows."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from ed_waits.common import STALE_AFTER_MINUTES, FacilityRow, _clean

log = logging.getLogger("ingest_ed_waits")

# The feed counts one more reporting hospital than it delivers: The New
# Maitland Hospital is in totalHospitalsCount but has no record anywhere in
# the payload (chased 31/07/2026, vault: NSW_Health_JSON_Engine.md). It has
# been absent since the 19/07 capture, so warning about it every cycle is
# ~96 identical WARNINGs a day that bury the case we actually care about --
# the gap *changing*. Alarm on the delta, not on the known constant. Set
# back to 0 the day NSW Health fills the hole.
KNOWN_MISSING_HOSPITALS = 1


def validate_wait_payload(payload: Any) -> None:
    """Drift alarm for the GetHospitalDetails shape (captured 19/07/2026)."""
    if not isinstance(payload, dict):
        raise ValueError("wait payload: not a JSON object")
    hospitals = payload.get("hospitalDetails")
    if not isinstance(hospitals, list) or not hospitals:
        raise ValueError("wait payload: hospitalDetails missing or empty")
    reporting = payload.get("reportingHospitalDetails", [])
    if reporting:
        first = reporting[0]
        for field in ("hospitalID", "hospitalName", "waitCount",
                      "waitCountThreshold", "totalDurationSinceLastUpdate"):
            if field not in first:
                raise ValueError(
                    f"wait payload shape changed: missing {field!r}")


def _clean_postcode(value: Any) -> Optional[str]:
    """RTED postcodes arrive as 'NSW 2145'; the directory feed uses '2145'."""
    text = _clean(value)
    if text and text.upper().startswith("NSW "):
        text = text[4:].strip()
    return text


def _as_int(value: Any) -> Optional[int]:
    """JSON number -> int; bools and non-integral floats are not counts.

    bool is a subclass of int, so a bare isinstance(int) check would let
    JSON true/false through to an integer column; and a serializer that
    starts emitting 4.0 instead of 4 must not silently NA the whole state.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _apply_na_rule(wait_count: Any, threshold: Any,
                   minutes_since_update: Any) -> Optional[int]:
    """Site display rule: negative, over-threshold or stale counts are NA."""
    count = _as_int(wait_count)
    if count is None:
        return None
    if count < 0:
        return None
    limit = _as_int(threshold)
    if limit is not None and count > limit:
        return None
    if (isinstance(minutes_since_update, (int, float))
            and not isinstance(minutes_since_update, bool)
            and minutes_since_update > STALE_AFTER_MINUTES):
        return None
    return count


def _rted_facility(rec: dict, facility_type: str = "ed_reporting") -> FacilityRow:
    return FacilityRow(
        source_id=str(rec["hospitalID"]),
        name=_clean(rec.get("hospitalName")) or str(rec["hospitalID"]),
        facility_type=facility_type,
        address=_clean(rec.get("address")),
        suburb=_clean(rec.get("location")),
        postcode=_clean_postcode(rec.get("postCode")),
        lhd=_clean(rec.get("districtName")),
        phone=_clean(rec.get("phone")),
        lat=rec.get("latitude"),
        lng=rec.get("longitude"),
    )


def parse_wait_payload(
    payload: Any, captured_at: Optional[datetime] = None,
) -> tuple[list[FacilityRow], list[dict]]:
    """Map one GetHospitalDetails payload to facility + snapshot rows.

    One call carries the whole state: the queried hospital (hospitalDetails +
    waitingDetails + bedDetails, joined on facilityIdentifier) plus every
    other reporting hospital in reportingHospitalDetails. LHD-outage NA
    handling is not applied here: outages live in the site's SharePoint
    Outages list, not this API (vault: NSW_Health_JSON_Engine.md).
    """
    captured_at = captured_at or datetime.now(timezone.utc)
    facilities: list[FacilityRow] = []
    snapshots: list[dict] = []

    queried = payload["hospitalDetails"][0]
    waiting = payload.get("waitingDetails") or []
    beds = payload.get("bedDetails") or []
    seen_ids: set[str] = set()
    queried_id = queried.get("hospitalID")
    if waiting and queried_id is None:
        log.warning("queried hospital record has no hospitalID (drift?); "
                    "skipping its snapshot: %r", queried)
    elif waiting:
        fid = queried.get("facilityIdentifier")
        # No blind waiting[0] fallback: attributing another facility's
        # queue to the queried hospital is worse than recording nothing.
        wait = next((w for w in waiting
                     if w.get("facilityIdentifier") == fid), None)
        if wait is None:
            log.warning("no waitingDetails entry matches queried facility "
                        "%r (drift?); skipping its snapshot", fid)
        else:
            bed = next((b for b in beds
                        if b.get("facilityIdentifier") == fid), None)
            facilities.append(_rted_facility(queried))
            seen_ids.add(str(queried_id))
            snapshots.append({
                "source_id": str(queried_id),
                "name": queried.get("hospitalName"),
                "captured_at": captured_at,
                "patients_waiting": _apply_na_rule(
                    wait.get("waitCount"), wait.get("waitCountThreshold"),
                    wait.get("totalDurationSinceLastUpdate")),
                "treatment_spaces": bed.get("bedCapacity") if bed else None,
                "outage": False,
                "outage_text": None,
            })
    else:
        log.info("queried hospital %s (%s) is not reporting; no snapshot",
                 queried.get("hospitalID"), queried.get("hospitalName"))

    for rec in payload.get("reportingHospitalDetails") or []:
        rec_id = rec.get("hospitalID")
        if rec_id is None:
            # Validation only shapes-checks the first entry; guard each.
            log.warning("reporting entry with no hospitalID (drift?); "
                        "skipped: %r", rec)
            continue
        if str(rec_id) in seen_ids:
            # A repeat would also break the single-statement facilities
            # upsert ("ON CONFLICT DO UPDATE cannot affect row a second
            # time"), taking the whole cycle down with it.
            log.warning("duplicate hospitalID %s in payload (drift?); "
                        "keeping first occurrence", rec_id)
            continue
        seen_ids.add(str(rec_id))
        facilities.append(_rted_facility(rec))
        snapshots.append({
            "source_id": str(rec_id),
            "name": rec.get("hospitalName"),
            "captured_at": captured_at,
            "patients_waiting": _apply_na_rule(
                rec.get("waitCount"), rec.get("waitCountThreshold"),
                rec.get("totalDurationSinceLastUpdate")),
            "treatment_spaces": None,
            "outage": False,
            "outage_text": None,
        })

    # _as_int, not isinstance(int): a JSON true would otherwise read as 1
    # and fake a 58-hospital shortfall every cycle.
    expected = _as_int((payload.get("reportingHospitals") or [{}])[0].get(
        "totalHospitalsCount"))
    if expected is not None:
        shortfall = expected - len(snapshots)
        if shortfall == KNOWN_MISSING_HOSPITALS:
            log.info("payload advertises %d reporting hospitals, delivered "
                     "%d; matches the known upstream gap (Maitland)",
                     expected, len(snapshots))
        elif shortfall:
            log.warning("payload advertises %d reporting hospitals but %d "
                        "snapshot rows parsed: shortfall %d, expected the "
                        "known %d. The upstream gap CHANGED -- a hospital "
                        "appeared or dropped out; investigate",
                        expected, len(snapshots), shortfall,
                        KNOWN_MISSING_HOSPITALS)
    return facilities, snapshots
