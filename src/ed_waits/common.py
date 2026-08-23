"""Shared constants and row types for the NSW ED pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

DEFAULT_FACILITIES_URL = (
    "https://data.nsw.gov.au/data/api/action/datastore_search"
    "?resource_id=e17840df-ecfc-4e38-b51b-9f49af5dc21a&limit=300"
)
# No password in the fallback DSN, deliberately. libpq resolves the password
# from %APPDATA%\postgresql\pgpass.conf (POSIX: ~/.pgpass), which lives
# outside the OneDrive-synced tree and is not in git. Verified 12/08/2026:
# psycopg2 connects on this exact DSN with PGPASSWORD unset.
# A literal here would be a committed credential, and git history keeps it
# forever -- so the only thing that ever revokes one is rotating the role's
# password, not editing this line.
DEFAULT_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://rted@127.0.0.1:5432/aussie_health",
)
# Conduct policy (vault: NSW_Health_JSON_Engine.md): identifiable and
# contactable, required before any recurring poll runs.
USER_AGENT = "AussieHealthUtility/0.1 (+contact: jaredlilliss@gmail.com)"
SOURCE = "data_nsw_ckan"
RTED_SOURCE = "nsw_rted"
# Any reporting hospital works as the anchor; the response carries the whole
# state. 209 = Westmead.
DEFAULT_WAITS_URL = (
    "https://rted-web-external.citc.health.nsw.gov.au/api/GetHospitalDetails/209"
)
# Site rule (getNSWHealthInformation.js): counts older than 120 min show NA.
STALE_AFTER_MINUTES = 120
# Retention policy (vault: Postgres_Cache_Schema.md, "Decide before launch,
# not after the disk fills."). Raw only for now; hourly rollup is explicitly
# deferred there until a trend chart actually needs it.
#
# Raised 30 -> 365 on 08/08/2026. The original 30 was chosen when the dumps
# were assumed reliable, which made the live table a cache and the dumps the
# archive. That assumption does not currently hold: backup_db.py has not run
# automatically since 05/08 and nobody noticed for two days, so a 30-day prune
# is presently a permanent-loss mechanism for a series the upstream API cannot
# reproduce. 365 makes the live table survive a backup outage measured in
# months rather than weeks.
SNAPSHOT_RETENTION_DAYS = 365

# Feed value -> facility_type. Unknown values are a drift alarm, not a guess.
ED_FLAG_MAP = {
    "Reporting wait times": "ed_reporting",
    "Not reporting wait times": "ed_not_reporting",
    "No emergency department": "no_ed",
}


@dataclass
class FacilityRow:
    source_id: str
    name: str
    facility_type: str
    address: Optional[str]
    suburb: Optional[str]
    postcode: Optional[str]
    lhd: Optional[str]
    phone: Optional[str]
    lat: Optional[float] = None
    lng: Optional[float] = None


def _clean(value: Any) -> Optional[str]:
    """Feed uses '' and the literal string 'NULL' for absent values."""
    if value is None:
        return None
    text = str(value).strip()
    return text if text and text.upper() != "NULL" else None
