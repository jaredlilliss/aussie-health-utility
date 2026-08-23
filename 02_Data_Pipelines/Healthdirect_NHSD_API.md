---
aliases: [NHSD Pipeline, Pharmacy Hours Pipeline, Healthdirect API]
tags: [pipeline, nhsd, healthdirect, pharmacy]
created: 2026-06-11
status: draft
up: "[[System_Overview]]"
---

# Healthdirect NHSD API

Pipeline 2 of [[System_Overview]]. Verified pharmacy details, opening hours and operational status from the **National Health Services Directory**, landing in `pharmacies`, `pharmacy_hours` and `pharmacy_status_snapshots` in [[Postgres_Cache_Schema]].

## Why NHSD over Google Places

- **System of record.** NHSD is run by Healthdirect Australia (government owned) and spans 400,000+ service and practitioner records; pharmacies and EDs are among its most-queried categories. It is the directory the Australian health system itself uses.
- **We're allowed to keep the data.** Google Places' terms restrict caching/storing most returned content in your own database, which is precisely what a cache architecture does. Places also bills per request at scale. NHSD offers APIs, widgets and **data extracts**, i.e. distribution is the point.
- **Bonus:** NHSD also carries practitioner records, which keeps a door open for [[Medical_Costs_Finder]]-adjacent features without touching Ahpra's restricted PIE service.

## Access

- Register via the developer portal: `developers.nhsd.healthdirect.org.au`. Auth is an `x-api-key` header.
- API family: **v5 `healthcareServices`**. Public docs show:
    - search endpoints by coverage area, proximity, suburb, etc.
    - `GET /v5/healthcareServices/{id}/operationalStatus` (single service open/closed status)
    - `GET /v5/healthcareServices/_operationalStatus/{id},{id}` (batch status)
- ⚠ The host visible in public docs is `api.int.nhsd.healthdirect.org.au`, which looks like an **integration environment**. Production base URL, rate limits, quotas and licence/attribution terms: **confirm at registration** and record them here. Do not guess them into code.

### Registration walkthrough (scouted 22/07/2026, no signup performed)

The onboarding is heavier than a portal signup — it is a service-desk-mediated process with a legal agreement and a conformance gate. Healthdirect's own wording: "The entire process typically takes around three months, depending on availability of development resources and the number of integrators being onboarded simultaneously." **Plan Pipeline 2 around a ~3-month lead time, not a key-in-the-inbox.**

The 8 steps (from the Integration Hub getting-started page, `about.healthdirect.gov.au/what-we-do/portfolio/nhsd/integration-hub/getting-started`):

1. Submit a **test-environment connection request**: `healthdirect-serviceline.atlassian.net/servicedesk/customer/portal/3/group/12/create/44` (asks org name + business/technical contacts; identity verification)
2. Complete the **Production Environment Registration** form: same portal, `.../create/42`
3. **Execute the applicable NHSD Agreement** (a signed agreement, not a click-through)
4. **Data harmonisation** — map our fields to the NHSD model
5. **Build against the test API**
6. **Testing and NHSD review**
7. Notice of Connection + production access
8. Connect to production and **demonstrate conformance**

Steps 4–6 were scouted as a single group ("data harmonisation, build, testing, NHSD review"); the exact boundaries between them are not captured here. Re-read the getting-started page when you reach them rather than trusting this split.

Onboarding pack (FHIR-oriented): `media.healthdirect.org.au/publications/NHSD_Onboarding-pack-FHIR-integrator.zip`. A `NHSD_Developer-Guide_v1.0.pdf` also exists on their media host.

**Open question for the test-env request:** current public docs still document **v5 `healthcareServices` with `x-api-key`** (`developers.nhsd.healthdirect.org.au/docs/consumer-api/index.html`, host shown is still the `api.int.` integration environment), but the onboarding pack is branded *FHIR integrator* — ask explicitly whether new integrators get the v5 REST surface or FHIR-only (`build.fhir.nhsd.healthdirect.org.au`). If FHIR-only, the data-mapping table below needs a FHIR `HealthcareService` remap before build.

Also noted: NHSD offers **embeddable widgets** as an alternative integration; incompatible with our cache-first/on-device-filtering architecture, so not pursued — recorded only as a possible interim display stopgap.

Licence, attribution, quotas and the production base URL remain **disclosed at registration** (nothing public found; the API guide states none). Record them here the day they arrive, per the open item below.

## Data mapping (concept level — applies to both surfaces)

Which NHSD concept lands in which table. This is surface-independent and deliberately coarse. **If we get the FHIR surface, the field-level map in *FHIR → cache field map* below governs** — this table on its own is not enough to build an ingest from.

| NHSD concept | Cache table ([[Postgres_Cache_Schema]]) | Notes |
|---|---|---|
| Service record (pharmacy) | `pharmacies` | keyed by NHSD service ID |
| Standard opening hours | `pharmacy_hours` | per weekday; supports split hours |
| Operational status | `pharmacy_status_snapshots` | append-only, timestamped |

## FHIR readiness — the v5-vs-FHIR fork (researched 22/07/2026)

The registration open question (which surface do new integrators get?) can only be settled by NHSD at test-env access, but the **public FHIR IG** — `build.fhir.nhsd.healthdirect.org.au` (titled *NHSD Implementation Guide — Outbound v4.0.1*) — lets us pre-map the FHIR surface now. **Bottom line: [[Postgres_Cache_Schema]] holds its shape under either surface — no new tables, no changed columns — but it needs two small additions (a `source` discriminator on `pharmacy_hours_overrides`, a `check (closes > opens)` on `pharmacy_hours`), and the ingest fork is wider than auth alone.** It forks on auth, on a `Location` indirection, and on the hours mapping, which is the part that looks trivial and is not. Still not a blocker; still a build-time branch. *(Revised 29/07/2026 — the first pass claimed "survives unchanged" and "only auth and Location", which was wrong on both counts.)*

### The two surfaces side by side

| | v5 REST (older docs) | FHIR v4 Outbound (this IG) |
|---|---|---|
| Base | `api.int.nhsd.healthdirect.org.au` (integration host shown; prod undisclosed) | `https://api.fhir.nhsd.healthdirect.org.au/v4` — **as printed in the public IG, not confirmed by NHSD.** Same rule as the v5 host above: confirm at registration, do not hardcode |
| Auth | `x-api-key` header only | **OAuth2 client-credentials → Bearer token, *plus* api-key** |
| Service resource | `healthcareServices` JSON | `HealthcareService` (profile `nhsd-healthcareservice`) |
| Status | `/operationalStatus` (single + batch) | `operationalStatus` extension on the resource |

The auth difference is the real work: our `src/poller.py` takes arbitrary headers, so `x-api-key` is a one-liner, but OAuth2 needs a **token-fetch + refresh step** (client_id/secret → token endpoint → Bearer) — a small new auth hook on the poller, not a schema concern. Design the poller's `headers` to be lazily supplied by a callable so a Bearer can refresh without reconstructing the poller.

**Pagination is the second poller gap, and it is easier to miss than auth.** `JsonPoller` fetches exactly one URL with a fixed `params` dict per cycle and has no page-walking loop. The IG paginates with an explicit `page=000` parameter and **no `next` link in the bundle**, so there is nothing for a generic "follow the next link" helper to follow — the ingest has to count pages itself and stop on a short or empty bundle. Get this wrong and the first sync captures page 000 only: no error, no retry, just a directory that looks like NHSD has thin coverage of your suburb. Whatever drains the pages belongs in the NHSD ingest module, not in `JsonPoller` — the poller's contract is one endpoint, one payload, and widening it to understand bundles would push FHIR knowledge into a generic component the other two pipelines share.

### FHIR → cache field map (HealthcareService profile + Location)

| FHIR element | Cache column ([[Postgres_Cache_Schema]]) | Note |
|---|---|---|
| `identifier:rid` (NHSD Resource Identifier, 0..1) | `pharmacies.nhsd_id` | the stable service key |
| `HealthcareService.name` | `pharmacies.name` | direct |
| `telecom[].value` where `system='phone'` | `pharmacies.phone` | business phone — public directory, not personal (schema note holds) |
| **referenced `Location`** → `address`, `position.latitude/longitude` | `pharmacies.address/suburb/state/postcode/lat/lng` | **indirection:** address + geo are NOT inline on the service; resolve via `_include=HealthcareService:location` in the search bundle rather than an N+1 fetch |
| `availableTime[]`: `daysOfWeek`, `availableStartTime`, `availableEndTime`, `allDay` | `pharmacy_hours` | **not a row-for-row copy.** One entry can yield seven rows, or two rows on adjacent weekdays. See *Hours mapping rules* below — do not implement from this cell alone |
| `notAvailable[]`: `description`, `during` (Period) | `pharmacy_hours_overrides` | **declared** closures and holiday exceptions. Expand `during` to one row per date (table is unique on `(pharmacy_id, on_date)`), `closed_all_day = true`, `description` → `note`. See the declared-vs-observed caveat under *The override table stays* |
| `operationalStatus` extension (canonical `http://fhir.nhsd.com.au/StructureDefinition/operationalStatus`), coded by ValueSet `au-currentOperationalStatus` | `pharmacy_status_snapshots.status` | append-only, unchanged. **Canonical unverified** — that domain is `nhsd.com.au`, whereas every other NHSD host in this doc is under `nhsd.healthdirect.org.au`. Canonicals are matched as exact strings, so if it was mis-transcribed the extension lookup returns nothing and every pharmacy ingests with an empty status — no error, just a blank column. Re-read it off the IG before coding. Exact code set also not yet captured — pull the ValueSet's codes when mapping |

### Hours mapping rules (read before writing the ingest)

`availableTime[]` → `pharmacy_hours` looks like a straight copy and is not. Four rules, each a silent-wrong-data bug if missed:

1. **`daysOfWeek` is `0..*`, not a scalar.** A single entry commonly carries `["mon","tue","wed","thu","fri"]`. Fan out to **one row per day in the array** — the PK is `(pharmacy_id, weekday, opens)`, i.e. per weekday. Writing one row per *entry* leaves four weekdays looking closed, and nothing errors.
2. **`daysOfWeek` absent means every day**, not no days. Expand a missing array to all seven weekdays before applying rule 1.
3. **Never write `24:00`.** Postgres accepts `time '24:00:00'` so the INSERT succeeds, but psycopg maps `time` → Python `datetime.time`, whose hour must be `0..23`, and raises `ValueError` on read-back. The failure surfaces on some later `SELECT`, far from the row that caused it. Map `allDay = true` to a single row `00:00:00`–`23:59:59`.
4. **Overnight hours cross the weekday boundary.** A pharmacy open 21:00–02:00 arrives as `availableStartTime 21:00` / `availableEndTime 02:00`, i.e. `closes < opens`. Do not store it that way — any `now BETWEEN opens AND closes` test is false for the entire opening window, so the app reports every late-night pharmacy as closed. **Split into two rows:** `(weekday=N, 21:00:00, 23:59:59)` and `(weekday=(N+1) mod 7, 00:00:00, 02:00:00)`. This is the query the product exists to answer; it earns a fixture and a test of its own.

Rules 3 and 4 together mean `pharmacy_hours` should never hold a row with `closes <= opens`. Add `check (closes > opens)` to the DDL so a bad ingest fails loudly at write time rather than at some later read — open item below.

### Caveats before trusting this

- The IG's `HealthcareService-example0` is a **hospital A&E (Downunder Hospital), not a pharmacy, and populates neither `availableTime` nor `operationalStatus`** — so the *populated* shapes above are read from the profile definitions, not seen live. The open item "save a real fixture" therefore stands: capture a real **pharmacy** record at test-env access and build the pydantic drift-model against that, per the [[NSW_Health_JSON_Engine]] methodology. Do not fabricate a fixture from the doc example.
- FHIR search quirks noted in the IG to design around: `HealthcareService.specialty` search is **not supported**; pagination is explicit `page=000` with **no prev/next links** in bundles.
- Licensing on the IG: FHIR artifacts are **CC0**; the IG document is © Healthdirect. This is the *spec's* licence, not the *data's* — the data licence and attribution are still disclosed at registration (open item stands).

## Sync cadence

- **Directory data** (names, addresses, standard hours): weekly full refresh is plenty; it moves slowly.
- **Operational status:** refresh on a short interval (e.g. 15 min) for pharmacies actually being displayed, served from cache per the rule in [[System_Overview]]. Batch endpoint exists, use it.

## The override table stays

NHSD gives the *declared* hours. Reality diverges on public holidays and one-off closures. `pharmacy_hours_overrides` ([[Postgres_Cache_Schema]]) records **observed** open/close on specific dates with a note and timestamp, and the app's display logic is: override for today's date wins, otherwise standard hours, with status snapshot as a live tie-breaker. This was the good idea in the original blueprint; it survives the source swap intact.

**Caveat added 29/07/2026 — declared exceptions land here too.** FHIR `notAvailable[]` carries NHSD's *declared* closures, and the only sensible home for them is this same table. That breaks the original observed-only premise: the table would then hold two kinds of row with different trust levels, and it has no column to tell them apart (`pharmacy_hours` has `source`; `pharmacy_hours_overrides` does not). Add a `source` discriminator (`'nhsd_declared'` vs `'observed'`) before ingesting `notAvailable[]`, and keep the display precedence explicit: **observed beats declared beats standard hours.** Without it, a stale NHSD-declared closure silently outranks something seen with our own eyes.

## Open items

- [x] Registration path scouted 22/07/2026: 8-step service-desk onboarding, NHSD Agreement + conformance gate, ~3-month typical lead time — walkthrough above
- [x] FHIR surface pre-mapped 22/07/2026 from the public Outbound v4.0.1 IG, **corrected 29/07/2026**: the schema holds its shape but needs two additions (`check (closes > opens)` on `pharmacy_hours`, a `source` discriminator on `pharmacy_hours_overrides`); the ingest forks on **three** things — OAuth2 auth, the `Location` indirection, and the hours mapping — see FHIR readiness above
- [ ] Submit test-environment connection request (needs Jared: org identity + contacts; ask the v5-vs-FHIR question in the request)
- [ ] Execute NHSD Agreement when offered; record production base URL, quotas, licence and attribution requirements here the day they arrive
- [ ] Capture a real **pharmacy** record at test-env access → save a fixture and build the pydantic drift-model against it (do not use the IG's hospital example); confirm hours + operationalStatus populated shapes
- [ ] If FHIR: add an OAuth2 token-fetch/refresh hook to `src/poller.py` (design `headers` as a callable so a Bearer can refresh in place)
- [x] Hours mapping corrected 29/07/2026: `daysOfWeek` fan-out, absent-means-all-days, no `24:00`, overnight split across weekdays — see *Hours mapping rules*
- [ ] Schema additions before the FHIR ingest lands: `check (closes > opens)` on `pharmacy_hours`, and a `source` discriminator (`'nhsd_declared'` / `'observed'`) on `pharmacy_hours_overrides` — update [[Postgres_Cache_Schema]] and `db/init/01_schema.sql` together, they are kept in sync
- [ ] Fixture + test for the overnight case (open 21:00–02:00) and the all-day case; these are the two the mapping gets wrong by default
- [ ] If FHIR: write the bundle page-walker in the NHSD ingest module (explicit `page=000`, no `next` link, stop on short/empty bundle) — `JsonPoller` deliberately stays single-endpoint
- [ ] Re-read the `operationalStatus` extension canonical off the IG — the `nhsd.com.au` domain recorded here does not match any other NHSD host and an exact-string mismatch fails silently
- [ ] Pin down the display precedence in code: observed override > NHSD-declared closure > standard hours, with status snapshot as live tie-breaker
- [ ] Decide initial geography (start: Liverpool / south-west Sydney) before syncing the whole country
