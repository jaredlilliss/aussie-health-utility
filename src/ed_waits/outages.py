"""Outages leg: the site's SharePoint Outages list -> LHD outage flags.

The RTED API has no outage concept; the public site keeps planned/unplanned
outage notices in a SharePoint list on emergencywait.health.nsw.gov.au and
shows NA for every hospital in an affected Local Health District. A plain
anonymous GET on the list REST endpoint works (probed 20/07/2026, a day
with zero active outages):

    GET /_api/web/lists/getbytitle('Outages')/items
        ?$filter=(nswStartTime lt datetime'<now>') and (nswEndTime gt datetime'<now>')
        Accept: application/json;odata=verbose

The DateTime literal uses the OData v3 datetime'' form matching the
verbose-OData contract; the 20/07 probe used a bare quoted string and,
returning an empty list, could not distinguish "filter works" from "filter
never matches" -- live re-verification is an open item in
02_Data_Pipelines/NSW_Health_JSON_Engine.md.

Item fields: nswOutageText, nswStartTime / nswEndTime (the outage window,
UTC), nswDisplayTime (when the banner starts showing), and
nswLocalHealthDistrict -- a taxonomy collection of LHD labels; an outage may
target several districts. The site's NA rule uses the start/end window and
the LHD match, so that is what apply_outages mirrors. An item with no LHD
labels is treated as statewide; an item whose LHD field cannot be read is
skipped with a drift alarm, never guessed statewide.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from ed_waits.common import USER_AGENT, FacilityRow, _clean
from poller import JsonPoller, PollerConfig

log = logging.getLogger("ingest_ed_waits")

# Host as probed and recorded in the vault (no www prefix; the code
# briefly disagreed with its own docs on this).
OUTAGES_URL = ("https://emergencywait.health.nsw.gov.au"
               "/_api/web/lists/getbytitle('Outages')/items")
_LHD_SUFFIX = " local health district"


@dataclass
class Outage:
    lhds: list[str]            # empty list = statewide
    text: Optional[str]
    starts_at: str             # raw feed timestamps, kept for logging
    ends_at: str


def _parse_lhd_labels(item: Any) -> Optional[list[str]]:
    """Labels from one list item; [] = statewide by design, None = drift.

    Verbose OData delivers the multi-value taxonomy field as
    {"results": [{"Label": ...}, ...]}. An explicitly readable but EMPTY
    results list is a genuine no-LHD item (statewide). Any other shape
    (deferred navigation property, renamed keys, absent field) is drift:
    guessing statewide from an unreadable field would NULL the whole
    state's counts over a one-district notice.
    """
    field = item.get("nswLocalHealthDistrict")
    if field is None:
        # Absent or null. This used to return [] -- statewide -- which is
        # the single most destructive reading available: it forces every
        # hospital in NSW to NA. SharePoint omits or defers unset
        # multi-value fields, so "absent" is at least as likely to mean
        # "the shape changed" as "deliberately statewide", and this branch
        # has never run against a real outage (every probe to date found
        # zero active outages). Statewide now requires an explicit,
        # readable, empty results list, handled below. Failing loudly here
        # loses outage marking on one item; failing the old way silently
        # blanks the entire state.
        return None
    if not isinstance(field, dict) or "results" not in field:
        return None
    results = field["results"]
    if not isinstance(results, list):
        return None
    labels: list[str] = []
    for entry in results:
        if not isinstance(entry, dict) or "Label" not in entry:
            return None
        label = _clean(entry.get("Label"))
        if label:
            labels.append(label)
    return labels


def _norm_lhd(value: Optional[str]) -> Optional[str]:
    """Normalize an LHD name for cross-feed comparison.

    SharePoint taxonomy labels and RTED districtName can spell the same
    district differently ("Western Sydney" vs "Western Sydney Local
    Health District"); casefold and drop the suffix so both forms meet.
    """
    if not value:
        return None
    text = " ".join(value.split()).casefold()
    if text.endswith(_LHD_SUFFIX):
        text = text[: -len(_LHD_SUFFIX)].strip()
    return text or None


def fetch_active_outages(now: Optional[datetime] = None) -> list[Outage]:
    """One GET for outages whose start/end window covers now."""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    cfg = PollerConfig(
        url=OUTAGES_URL,
        headers={"Accept": "application/json;odata=verbose"},
        params={"$filter": f"(nswStartTime lt datetime'{stamp}') "
                           f"and (nswEndTime gt datetime'{stamp}')"},
        user_agent=USER_AGENT,
        max_retries=2,   # best-effort enrichment: fail fast, caller degrades
    )
    payload = JsonPoller(cfg, on_payload=lambda _: None,
                         validate=validate_outages_payload).fetch_once()
    if payload is None:  # 304: cannot happen on a fresh poller, defensive
        raise RuntimeError("no outages payload received (unexpected 304)")
    outages = []
    for item in payload["d"]["results"]:
        lhds = _parse_lhd_labels(item)
        if lhds is None:
            log.warning("outage item LHD field shape unrecognized (drift?);"
                        " skipping item: %r",
                        item.get("nswLocalHealthDistrict"))
            continue
        outages.append(Outage(
            lhds=lhds,
            text=_clean(item.get("nswOutageText")),
            starts_at=item.get("nswStartTime", ""),
            ends_at=item.get("nswEndTime", ""),
        ))
    return outages


def validate_outages_payload(payload: Any) -> None:
    """Drift alarm for the SharePoint list envelope."""
    if not isinstance(payload, dict) or "d" not in payload:
        raise ValueError("outages payload: no OData 'd' envelope")
    if not isinstance(payload["d"].get("results"), list):
        raise ValueError("outages payload: d.results is not a list")


def apply_outages(facilities: list[FacilityRow], snapshots: list[dict],
                  outages: list[Outage]) -> None:
    """Mark snapshots in an outage-affected LHD; mirrors the site's NA rule.

    Mutates snapshots in place: outage=True, outage_text set, and
    patients_waiting forced to None so a stale count is never shown as
    live. LHD names are normalized on both sides before comparing, and a
    targeted outage that matches no facility raises a drift alarm instead
    of passing silently.
    """
    if not outages:
        return
    lhd_by_id = {f.source_id: _norm_lhd(f.lhd) for f in facilities}
    targets: list[set[str]] = []
    for outage in outages:
        names = {_norm_lhd(label) for label in outage.lhds}
        names.discard(None)
        targets.append(names)
    hits = [0] * len(outages)
    total = 0
    for snap in snapshots:
        lhd = lhd_by_id.get(snap["source_id"])
        # Count coverage for EVERY matching outage, but apply only the
        # first. Incrementing hits inside a loop that breaks made the
        # zero-match drift alarm below fire on any second outage whose
        # facilities were already claimed by an earlier one -- a false
        # alarm on a perfectly good payload.
        matched = [i for i in range(len(outages))
                   if not targets[i] or lhd in targets[i]]
        if not matched:
            continue
        for i in matched:
            hits[i] += 1
        snap["outage"] = True
        snap["outage_text"] = outages[matched[0]].text
        snap["patients_waiting"] = None
        total += 1
    for outage, hit in zip(outages, hits):
        if outage.lhds and hit == 0:
            log.warning("targeted outage matched NO facilities (LHD label "
                        "drift?): labels=%r vs districts=%r", outage.lhds,
                        sorted({f.lhd for f in facilities if f.lhd}))
    log.warning("active outage(s): %d of %d snapshots marked NA (%s)",
                total, len(snapshots),
                "; ".join((o.text or "no text")[:80] for o in outages))
