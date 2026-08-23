---
aliases: [ED Wait Times Pipeline, NSW ED Feed]
tags: [pipeline, nsw-health, ed-wait-times]
created: 2026-06-11
status: draft
up: "[[System_Overview]]"
---

# NSW Health JSON Engine

Pipeline 1 of [[System_Overview]]. Live ED wait counts for major NSW hospitals, landing in `facilities` and `ed_wait_snapshots` in [[Postgres_Cache_Schema]].

## Why the JSON endpoint, not the HTML

The page source at `emergencywait.health.nsw.gov.au` is a **client-rendered template**. The raw HTML contains literal placeholders (`{patientWaitCount}`, `{hospitalAddress}`, `{outageText}`) that get hydrated in the browser. Scraping the markup therefore means:

- a headless browser dependency (Playwright) for no good reason,
- fragility to every frontend redesign,
- and parsing data that arrived as clean JSON one network hop earlier.

Backend payloads change far less often than frontends. Poll the payload.

## Endpoint discovery procedure (the Phase 2 spike, ~30 min)

1. Open the site in Chrome. DevTools → **Network** tab → filter **Fetch/XHR**.
2. Hard reload. Identify the request whose response contains hospital names and wait counts (JSON).
3. Record: full URL, query params, request headers actually required, response schema, and any cache headers (`ETag`, `Last-Modified`, `Cache-Control`).
4. Reproduce outside the browser with `curl` using our own honest User-Agent. If it answers, we have our feed.
5. Save a raw sample payload to `fixtures/` and write a pydantic model against it. That model is our **drift alarm**: validation failure = schema changed = alert, don't silently ingest garbage.
6. Only if no direct endpoint is reachable does Playwright re-enter the picture, as a fallback, not the design.

Feasibility check: commercial aggregators already republish this NSW Health data at roughly 15-minute refresh, so the feed is demonstrably extractable.

## Polling policy

- **Interval:** 15 min default, never below 10. Matches the data's real cadence; faster adds load, not information.
- **Conditional requests:** send `If-None-Match` / `If-Modified-Since` when the endpoint supports them; a `304` costs everyone almost nothing.
- **Backoff:** exponential with jitter on any error, capped at 5 min. Honour `Retry-After` on 429/503.
- **Circuit breaker:** after 5 consecutive failures, stop polling, raise an alert, mark the pipeline degraded in `sync_runs` ([[Postgres_Cache_Schema]]).
- **Concurrency:** exactly one in-flight request to this host, ever.
- **Outage states:** the template has an `{outageText}` slot, so the feed has an outage concept. Model it (`outage`, `outage_text` columns) and show "data unavailable" in the app rather than a stale number presented as live.

## User-Agent and conduct policy

- UA string: `AussieHealthUtility/0.1 (+contact email)`. Identifiable, contactable, nothing pretending to be a browser.
- Check and obey `robots.txt` before the first poll.
- Users are served **only from cache** (see serving rule in [[System_Overview]]); our traffic to NSW Health is one request per interval regardless of user count.
- Check the site's copyright/terms notice and attribute NSW Health as the source in-app. Much Australian government content is published under permissive licences, but verify what this site states; record the answer here when known.

## Implementation

Generic scaffold: `src/poller.py` (config-driven, conditional GET, backoff, validate hook). The NSW-specific subclass only supplies the endpoint, the pydantic model, and an upsert into [[Postgres_Cache_Schema]].

## Spike findings (11/06/2026)

Run remotely (web fetch + open-data catalogue). One route still needs the two-minute browser step below.

**Confirmed:**

- The public site is only a SharePoint shell. The data layer is a separate ASP.NET service, **`Rted.External.Api`**, at `https://rted-web-external.citc.health.nsw.gov.au/` (RTED = Real Time Emergency Department, matching the site's `NSW-Health-RTED-Branding` assets).
- **`GET https://rted-web-external.citc.health.nsw.gov.au/api/GetHospitalsReport`** is live (returns the hospital directory as a CSV file download). It is officially catalogued on Data.NSW as the "NSW Hospitals" dataset (publisher: NSW Ministry of Health, contact: digital@health.nsw.gov.au), so reuse of this route is sanctioned, not just tolerated.
- The same directory is mirrored as **auth-free JSON** in the CKAN DataStore:
  `https://data.nsw.gov.au/data/api/action/datastore_search?resource_id=e17840df-ecfc-4e38-b51b-9f49af5dc21a&limit=300`
  266 records. Fields: Name, Address, Suburb, Postcode, Phone, Email Address, Fax, LHD, Hospital Website, **ED**. The `ED` field takes the values `Reporting wait times`, `Not reporting wait times`, `No emergency department`: filter on it to load `facilities` ([[Postgres_Cache_Schema]]) with exactly the reporting hospitals.
- Site URL pattern `?hid=<id>` selects a hospital (e.g. `hid=298` = Walcha). Address/phone render server-side; the wait count hydrates client-side, so the live number comes from a fetch back to the RTED API.
- Real sample payload saved at `fixtures/ckan_datastore_search_sample.json`. `src/poller.py --once` tested against it via a localhost replay: fetch, JSON decode and payload handoff all pass.

## Spike findings (19/07/2026): wait-count route captured

Browser leg done (site loaded, its own JS inspected — no probing). The routes, all on `https://rted-web-external.citc.health.nsw.gov.au/`, no auth:

- **`GET api/GetHospitalDetails/{hospitalID}`** — the wait-count feed. **One call returns the entire state**: the queried hospital (details + wait + bed capacity) plus `reportingHospitalDetails[]` with `waitCount` for every other reporting hospital. The poller needs exactly one request per interval, anchored on any reporting hospital (we use 209 = Westmead).
- `GET api/GetHospitalsPostcode?searchTerm=...` — search/autocomplete; maps names to `hospitalID` and flags `isReportingOnline`.
- `GET api/GetNearestHospitals/{hospitalID}` — same reporting list, not needed.

Verified outside the browser with the honest UA. Sample payloads in `fixtures/`: `wait_payload_20260719T094206Z.json` (Westmead, id 209 — reporting, full shape) and `wait_payload_20260719T094109Z.json` (Walcha RAC, id 298 — non-reporting: no `waitingDetails`, so `hid=298` on the site is Walcha, confirming the URL pattern).

### Field map (implemented in `src/ed_waits/waits.py`, `parse_wait_payload`; CLI entry point stays `src/ingest_ed_waits.py`)

| Payload | Column / use |
|---|---|
| `hospitalDetails[0]`: hospitalID, hospitalName, address, location, postCode (`"NSW 2145"` — strip prefix), districtName, phone, latitude, longitude, facilityIdentifier | `facilities` upsert, `source='nsw_rted'`, `source_id=hospitalID` |
| `waitingDetails[0].waitCount` (join `facilityIdentifier`) | `patients_waiting` |
| `waitingDetails[0].lastUpdatedDate` | UTC (site renders +10h as AEST) |
| `waitingDetails[0].totalDurationSinceLastUpdate` | minutes since update |
| `bedDetails[0].bedCapacity` | `treatment_spaces` (queried hospital only) |
| `historicalDetails[0]` (`statisticIdentifier='usual_patients'`) | usual-arrivals stat; not stored in v1 |
| `reportingHospitalDetails[]` | one facility + snapshot row each (no bed/lastUpdated fields) |
| `reportingHospitals[0].totalHospitalsCount` | drift check: payload says 60, delivers 59. **Persistent, not a capture-day fluke** — cause identified 31/07/2026, see *The missing 60th hospital* below |

**NA rule** (from the site's `getNSWHealthInformation.js`, applied in `_apply_na_rule`): count is NA when `waitCount < 0`, `waitCount > waitCountThreshold`, **or last update > 120 min ago**. Fourth condition — an active LHD outage — comes from the site's SharePoint `Outages` list (`_api/web/lists/getbytitle('Outages')`), not this API; polled once per live cycle by `src/ed_waits/outages.py`, which marks affected snapshots `outage=true` and forces the count to NA (wired 20/07/2026, details in open items below).

**Facilities keying:** RTED rows land under `source='nsw_rted'` keyed by numeric `hospitalID` (with lat/lng, which CKAN lacks). CKAN directory rows keep `source='data_nsw_ckan'` keyed by name. Names differ between feeds ("Auburn ... Service" vs "... Services"; CKAN "The Tweed Hospital" vs RTED "Tweed Valley Hospital"), so never join on name — the RTED source is the one snapshots reference.

### The missing 60th hospital (chased 31/07/2026)

The `totalHospitalsCount` drift alarm has fired every cycle since the pipeline started. It is **correct**, it is **persistent**, and it is **not a parser bug** — do not "fix" the parser.

**The missing hospital is The New Maitland Hospital (Metford, Hunter New England).** It is counted in `totalHospitalsCount` but has no record anywhere in the payload.

How the arithmetic works out:

| | |
|---|---|
| `reportingHospitals[0].totalHospitalsCount` | 60 |
| `reportingHospitalDetails[]` entries | 58 |
| queried anchor — Westmead Hospital, id 209, absent from that array (note: *The Children's Hospital at Westmead*, id 181, is a different facility and **is** in the array — a substring search for `Westmead` will hit it and look like this line is wrong) | +1 |
| **parseable total** | **59** |

Method, so it can be re-run: `reportingHospitals` carries only a count and no list, so the payload cannot identify its own omission. The `facilities` table can, because it holds both sources — `data_nsw_ckan` has 60 rows typed `ed_reporting`, `nsw_rted` has 59. Two CKAN names had no RTED match (**The New Maitland Hospital**, **The Tweed Hospital**), but only one hospital is genuinely absent, so one had to be a rename. The reverse query settled it: exactly one RTED name has no CKAN match, **Tweed Valley Hospital**. That confirms the Tweed rename already recorded under *Facilities keying* above, and leaves Maitland as the real gap.

Verified against `fixtures/wait_payload_20260719T094206Z.json` (no name matching `Maitland`; 13 Hunter New England hospitals present, none of them Maitland) and against the live `facilities` table, which every cycle rebuilds. Same shortfall on 19/07 and still firing now. **Use that fixture specifically** — the other 19/07 capture, `wait_payload_20260719T094109Z.json`, carries an empty `reportingHospitalDetails[]` and cannot reproduce any of this.

**User impact, and it is real.** Someone near Metford sees no Maitland ED in the app, silently. This is exactly the failure the drift-alarm design exists to catch, and it has been catching it into a log nobody reads.

**Alarm fatigue was the second problem — fixed 04/08/2026.** A permanent upstream gap emitting a WARNING every 15 minutes trains us to ignore the line, which is what happened for eleven days. `src/ed_waits/waits.py` now carries `KNOWN_MISSING_HOSPITALS = 1`: the known shortfall logs INFO, and only a **change** in it warns. So if Maitland reappears, or a second hospital drops out, that warns; Maitland ninety-six times a day does not. **Set the constant back to 0 the day NSW Health fills the hole** — otherwise a filled gap plus a fresh omission would cancel out and stay silent.

**The number is 60 — settled 04/08/2026.** A 29/07 search had described the public site as covering "58", which would have made three sources disagree. Checked the site directly: it states **no count at all**, and its HTML is the client-rendered template described at the top of this note (`{hospitalName1}`, `{waitCount1}` placeholders, list hydrated at runtime). The "58" came from third-party telehealth pages republishing the feed, not from NSW Health. **60 stands**, corroborated independently by the CKAN dataset and the RTED payload's own count.

**robots.txt:** 404 on both `emergencywait.health.nsw.gov.au` and `rted-web-external.citc.health.nsw.gov.au` (checked 19/07/2026) — no crawl restrictions declared.

**Site terms (recorded 20/07/2026):** the site's footer Copyright link resolves to the NSW Health copyright notice, which licenses **all NSW Health material CC BY 4.0** (AusGOAL-endorsed, "supports and encourages the reuse of its publicly funded information"). Required attribution form, to appear in-app wherever ED data shows: *"© State of New South Wales NSW Ministry of Health. For current information go to www.health.nsw.gov.au."* This is the clean legal green light for this pipeline — a sharp contrast with the Commonwealth MCF position, see [[Medical_Costs_Finder]].

## The local series is sampled, not continuous (recorded 04/08/2026)

**The `ed_wait_snapshots` table on this machine is not a continuous 15-minute series and cannot be made into one here.** Treat it as a development sample. Measured 24-hour coverage over 30/07–04/08 ran between **8% and 30%**, with single blackouts up to **22 hours**.

The cause is not a bug in the pipeline. The poller runs from the Startup folder and polls correctly whenever the machine is awake and logged in. It cannot poll while the machine is suspended, and **Task Scheduler — the normal way to wake a machine on a schedule — cannot launch anything on this box** (a Task Scheduler process here cannot spawn a child process at all; runbook detail in [[Local_Deployment]]). Polling *at* wake would recover at most one interval; the multi-hour holes are unrecoverable, because a live wait count has no backfill.

Consequences worth holding onto:

- **Do not compute rates, averages or trends from this table** without accounting for the gaps. Rows are dense while the laptop is open and absent otherwise, so any time-weighted statistic is biased toward waking hours.
- **This is a hosting problem, not a code problem.** A continuous series needs an always-on host. That is the fix; nothing in `poll_loop.py` substitutes for it.
- **It bears on the product claim.** [[System_Overview]] sells live ED wait times as pipeline 1, and the competitive read there concluded the differentiator is the combination plus the privacy posture rather than the data itself. A series collected 8–30% of the time is fine for building against and cannot be what ships.

## Open items

- [x] Discovery spike (remote leg) run 11/06/2026; findings above
- [x] Browser leg: wait-count route captured 19/07/2026; payloads in `fixtures/`, findings above
- [x] robots.txt checked 19/07/2026 (none on either host); site terms recorded 20/07/2026 (CC BY 4.0 + attribution form, above)
- [x] Outages wired 20/07/2026: `src/ed_waits/outages.py` GETs the SharePoint list anonymously (`_api/web/lists/getbytitle('Outages')/items` filtered to `nswStartTime < now < nswEndTime`, `Accept: application/json;odata=verbose`). Items carry `nswOutageText` + `nswLocalHealthDistrict` taxonomy labels (multi-LHD; empty = treated as statewide). Affected snapshots get `outage=true`, the text, and `patients_waiting=NULL`, mirroring the site's NA rule. Live path fetches it each cycle (fixture replays skip it); a fetch failure logs and degrades to no-outage-info. Matching verified against a synthetic WSLHD + statewide outage; no real outage was active on capture day.
- [x] `totalHospitalsCount` drift chased 31/07/2026: **The New Maitland Hospital** is advertised but absent from the payload; parser is correct, gap is upstream — see *The missing 60th hospital* above
- [x] Known-gap allowance added 04/08/2026: `KNOWN_MISSING_HOSPITALS = 1` in `src/ed_waits/waits.py`. The known shortfall now logs INFO; only a **change** in it warns. Five checks cover known-gap/widened/closed/negative/bool-count. Set the constant back to 0 the day NSW Health fills the hole
- [x] Reporting-ED count settled 04/08/2026: **60**. The public site states no count and is a client-rendered template; the "58" came from third-party republishers, not NSW Health
- [ ] Report the Maitland omission to NSW Health (`digital@health.nsw.gov.au` — the Data.NSW contact for the NSW Hospitals dataset). It is their data gap, not ours, and it is one hospital's ED invisible to every consumer of this feed — **drafted 05/08/2026, see an unsent outreach draft (private, kept in the working repo); awaiting Jared's go-ahead to send**
- [ ] Decide snapshot retention (raw vs hourly rollups), see [[Postgres_Cache_Schema]]
- [~] Outages query live-verified 24/07/2026: the corrected request (host without `www`, OData `datetime''` filter literal, verbose `Accept`) was accepted by SharePoint and parsed to a well-formed empty result during a cold-start cycle (`sync_runs.detail` recorded `outage_checked=True`). Still **unproven against a real outage**: with 0 active outages, LHD label normalization/matching (`_norm_lhd`, the zero-match drift alarm) has only unit-test coverage — confirm when an actual outage is live
