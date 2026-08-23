---
aliases: [Costs Pipeline, Gap Fees Pipeline, MCF]
tags: [pipeline, mbs, medical-costs-finder, out-of-pocket]
created: 2026-06-11
status: draft
up: "[[System_Overview]]"
---

# Medical Costs Finder (+ MBS) ingestion

Pipeline 3 of [[System_Overview]]. Typical specialist out-of-pocket costs by MBS item and region, landing in `mbs_items` and `mcf_typical_costs` in [[Postgres_Cache_Schema]].

## The premise, corrected

A gap fee is **(what the provider charges) minus (Medicare benefit, plus insurer where applicable)**, and providers set their own fees. No register publishes per-doctor charges comprehensively, so this app never promises a per-doctor gap. It shows **typical** costs, which is what the federal Medical Costs Finder exists to publish. Two data layers:

### Layer 1: MBS schedule (official, downloadable, confirmed ✅)

- MBS Online (`mbsonline.gov.au`, Downloads section) publishes the full schedule as **XML files**, with full releases plus change files (the format practice-management software uses to update itself). An XML field-descriptions document is published alongside.
- The MBS Book is published in full around **March, July and November**; XML updates accompany each change cycle, so an ingest run is scheduled, not polled.
- Gives us per item: descriptor, category, schedule fee, benefit amounts → `mbs_items`.
- These files are explicitly flagged as informational, not legal documents; fine for a transparency utility.

**Structure verified live 12/06/2026** against `MBS-XML-20260301-version 2.XML`, fetched via the March 2026 downloads page (https://www.mbsonline.gov.au/internet/mbsonline/publishing.nsf/Content/Downloads-20260301):

- Root `<MBS_XML>`, flat repeated `<Data>` records, no attributes, whole file on one unbroken line
- Dates are `DD.MM.YYYY`
- GP attendance items carry `Benefit100` only (no Benefit75/85 elements at all); specialist items carry the fuller benefit set
- Derived-fee items (`FeeType` = `D`) have **no `ScheduleFee` element whatsoever**: free-text `DerivedFee` instead. They land in `mbs_items` with NULL fees by design and are excluded from the cross-check rule below.
- Parser: `src/ingest_mbs_xml.py`, streaming `iterparse` with drift alarms on root/record shape. Scale-tested: 150k records in a 67 MB single-line file parsed in ~2 s at 15 MB peak RSS.
- **Parser hardened 02/08/2026:** `iterparse` now comes from `defusedxml.ElementTree`, not the stdlib. The stdlib parser expands entities, so a release file carrying a billion-laughs / quadratic-blowup payload would exhaust memory before yielding a record — the one code path in this repo that consumes a *downloaded file* rather than a JSON API. Imported directly (no try/except fallback): degrading silently to the unsafe parser would defeat the point. Verified both ways — the real July 2026 release still parses to 6,045 items with 0 skipped, and a nine-level entity-expansion file is now rejected with `EntitiesForbidden` instead of expanding.

### Layer 2: Medical Costs Finder (typical fees, ingestion method TBC ⚠)

What MCF publishes: typical fees and fee ranges for common GP/specialist services, the Medicare contribution, in-hospital vs out-of-hospital settings, searchable by procedure or **MBS item number**, plus indicative fees of *participating* specialists by postcode. Coverage is partial by design ("data where available"); some insurer gap data exists but is thin.

**Verified 11/06/2026: no public API or bulk dataset for MCF was found.** Ingestion strategy ladder, in order:

1. **Network-tab inspection** of `medicalcostsfinder.health.gov.au` for the JSON endpoints behind the search UI, using the exact method documented in [[NSW_Health_JSON_Engine]]. If a clean endpoint exists, this becomes a scheduled (not frequent) pull.
2. **Catalogue watch:** periodically check data.gov.au and health.gov.au for a published MCF extract; the government has stated ambitions of quarterly-ish data refreshes, so an official dataset may appear.
3. **Fallback:** small, targeted snapshot ingestion of the service/region combinations the app actually displays, each row stamped with `source_date`, refreshed on a slow cycle (quarterly), with attribution and a check of the site's terms of use beforehand.

This is reference data, not live data: slow cadence, append-with-source-date, never present as current-day pricing.

## Display rules (product-level, non-negotiable)

- Always label costs **"typical"**, show ranges where MCF provides them, and surface the "data where available" caveat in the UI.
- Never present a number as a quote for a named doctor. The only per-specialist figures that may ever appear are ones a specialist has voluntarily published into MCF, labelled as such.
- Cross-check: `typical_oop` from MCF should roughly equal `typical_fee` minus the relevant `mbs_items` benefit. If it doesn't, flag the row, don't ship it.

## Spike findings (20/07/2026): clean JSON endpoints exist

Network spike run per the [[NSW_Health_JSON_Engine]] method (browser pane + the site's own JS). Rung 1 of the strategy ladder is the answer: **same-origin, auth-free JSON API** on `medicalcostsfinder.health.gov.au`:

- `GET /api/typical-searches/` — curated service list. IDs encode the domain: `Q104` = MBS item 104 out-of-hospital service, `H9` = in-hospital procedure ("Colonoscopy"); each carries a `procedureId` GUID and `hasInHospitalAggregateFees`.
- `GET /api/search-service/?id=Q104` — service detail: description, flags (`hasOutHospitalAggregateFees` etc.), specialty list with `dms` codes (e.g. `021802` cardiothoracic, `TOT` = all specialties). GraphQL-shaped (`__typename`), so the backend is a Dataverse/GraphQL export.
- `GET /api/search-servicejourney/?id=` and `GET /api/validate-postcode/?postcode=` — journey content and postcode check.
- Service pages: `/service?id=Q104&mode=OH` (OH/IH = out-/in-hospital). Q104 renders 2023-24 aggregates: typical fee $240, Medicare paid $81, patient OOP $159, "83% had an out-of-pocket cost" — the `mcf_typical_costs` columns map cleanly.
- Search autocomplete is **Azure Cognitive Search** (`ause-mcf-prod-azsearch.search.windows.net`, index `azsearchblob-index`) queried straight from the browser with a query api-key embedded in the page source. Treat as an implementation detail that can rotate any day, not a data contract; the `/api/` routes are the stable surface.
- Not yet mapped: the exact transport for the aggregate dollar figures (tail of the `search-service` payload vs. client bundle); capture properly when building the ingest.

**Licensing blocker, resolve before shipping this layer.** MCF's own terms page is guide-only consumer wording, but its footer adopts the Department of Health **copyright notice at health.gov.au: personal/internal use only, no commercial use, and no reproduction of site content on another website without express written permission** (contact `copyright@health.gov.au`). That is not CC-licensed like the NSW feed. Ingesting for internal evaluation is fine; **displaying MCF-derived numbers in the app needs written permission or an official dataset release first** (strategy rungs 2/3 remain the fallback). Checked 20/07/2026.

## Open items

- [x] MCF network spike run 20/07/2026: clean endpoints found, above
- [x] MCF terms checked 20/07/2026: restrictive Commonwealth copyright — written permission needed before in-app display, see blocker above
- [x] MBS XML parser built 12/06/2026: `src/ingest_mbs_xml.py`, streaming, structure-verified against the live March 2026 file, mock + 150k-record scale tested
- [x] Full release loaded 20/07/2026: `MBS-XML-20260701.XML` → 6,045 items into local `mbs_items` (see [[Local_Deployment]] ledger)
- [x] Rung 2 catalogue check run 29/07/2026: searched `data.gov.au` for `medical costs finder out of pocket specialist fees` — 3 hits, all unrelated annual reports. **No MCF extract published.** Method and the other two pipelines' checks: [[Australian_Gov_Surfaces]]
- [x] health.gov.au copyright notice re-verified 29/07/2026: unchanged, page dated **1 February 2024**, `copyright@health.gov.au` confirmed live. Use that date as the anchor for future re-checks
- [x] **Emailed copyright@health.gov.au on 10/08/2026**, afternoon, from jaredlilliss@gmail.com in the browser — permission to display MCF typical-cost data with attribution. Full send record, including the phone number added to the signature and the follow-up window: the permission-letter record (private correspondence, kept in the working repo). **This is now a waiting item, not a doing item:** no acknowledgement expected, follow up in early September if nothing arrives. Any reply is an escalation for the strategy lane, not a code change
- [ ] **Blocker remains until a reply arrives.** Displaying MCF-derived numbers still needs written permission — asking is not being granted. The costs surface ships MBS-only until then; strategy rungs 2/3 (catalogue watch, data.gov.au dataset request) remain live in parallel
- [x] Third-party copyright asked in the request, 07/08/2026. The letter now frames category 3 (specialist-published indicative fees) as a question — whether the Department can grant it, or whether the practitioners hold copyright directly — quoting the notice's own "contact them directly" wording back. See the permission-letter record (private correspondence, kept in the working repo). Claims-derived aggregates are covered by the same paragraph if the Department chooses to raise them
- [ ] Lodge a **"Suggest a new dataset"** request on `data.gov.au` for an MCF extract, in parallel with the email. Two independent routes to the same unblock; costs nothing
- [ ] Pick the launch set of services/regions (start with common specialist consults, Sydney south-west)
