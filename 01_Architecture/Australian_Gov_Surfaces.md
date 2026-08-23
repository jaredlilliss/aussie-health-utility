---
aliases: [Gov Surfaces, Which Gov Site Is Which, Domain Map]
tags: [architecture, governance, licensing, reference]
created: 2026-07-29
status: active
up: "[[System_Overview]]"
---

# Australian government surfaces — which is which

Reference note, not a pipeline. Every source this project touches is a government or government-adjacent surface, and the `.gov.au` badge tells you far less than it looks like it does. This note records the authorities worth knowing, the one rule that follows from them, and the evidence behind it.

## The rule

**A `.gov.au` domain is not proof of a Commonwealth entity, and the entity determines the licence.** Before assuming a source is Commonwealth-owned — and therefore covered by the Commonwealth copyright notice, or by a `data.gov.au` open licence — look the body up in the Australian Government Organisations Register at `directory.gov.au`. If it is not listed as a Commonwealth entity or company, its data licence is its own and has to be asked for separately.

The counter-example is already in this vault: see the Healthdirect finding below.

## The authorities

| Surface | What it is authoritative for | Use it for |
|---|---|---|
| `directory.gov.au` | The **Australian Government Organisations Register** (AGOR) — what a body legally *is*, which portfolio it sits in, and whether it is a Commonwealth entity, a Commonwealth company, or merely a board | Settling "who actually owns this data" before assuming a licence |
| `data.gov.au` | The open-data catalogue. Also publishes AGOR itself as a downloadable dataset | The only catalogue that matters for ingestion. Rung 2 of the [[Medical_Costs_Finder]] strategy ladder watches it |
| `legislation.gov.au` | Commonwealth instruments as made | The statutory basis behind MBS items, if a question ever turns on it |
| `australia.gov.au` | Whole-of-government front door for citizens | Signposting only. Not a data source — do not build against it |
| `digital.gov.au` | The Digital Transformation Agency: standards, policy, Digital ID | Context for gov service design. Not a data source for us |
| `cyber.gov.au` | The Australian Signals Directorate's ACSC: Essential Eight, the ISM, **Secure by Design / Secure by Default** | Standards, not data. See below |

**Portfolio note:** the department behind MBS and Medical Costs Finder is currently the **Department of Health, Disability and Ageing**. `copyright@health.gov.au` in the permission-letter record (private correspondence, kept in the working repo) is still the right contact; the departmental name is recorded here so the correspondence log stays legible after the next machinery-of-government rename.

## The Healthdirect finding (checked 29/07/2026)

**Healthdirect Australia does not appear in `directory.gov.au` as a Commonwealth entity or company.**

Checked directly against the Health, Disability and Ageing portfolio page. Its *Commonwealth entities and companies* list is complete — nineteen bodies, Aged Care Quality and Safety Commission through Professional Services Review, and it does include peers like the Australian Digital Health Agency. Healthdirect Australia is not among them. It appears in the Directory only as a board entry, *Healthdirect Australia Board of Directors*, filed under boards and structures. (Portfolio page last updated 16 May 2025.)

That explains the domain sprawl in [[Healthdirect_NHSD_API]], which is otherwise baffling. **Eight hosts for one "organisation":**

| Host | Role |
|---|---|
| `about.healthdirect.gov.au` | corporate / about |
| `media.healthdirect.org.au` | publications host (onboarding pack, developer guide) |
| `developers.nhsd.healthdirect.org.au` | developer portal |
| `api.int.nhsd.healthdirect.org.au` | v5 integration API |
| `build.fhir.nhsd.healthdirect.org.au` | FHIR Implementation Guide |
| `api.fhir.nhsd.healthdirect.org.au` | FHIR API |
| `healthdirect-serviceline.atlassian.net` | the actual onboarding service desk |
| `fhir.nhsd.com.au` | the `operationalStatus` extension canonical |

Read structurally rather than as mess: `.gov.au` fronts the government-funded *service*, `.org.au` carries the *operator's* actual infrastructure, the service desk is outsourced SaaS, and `.com.au` is the outlier — which is independent corroboration for the open item in [[Healthdirect_NHSD_API]] that says re-read that canonical off the IG before coding.

### Why it matters to us

The Commonwealth copyright notice at `health.gov.au` — the blocker on the [[Medical_Costs_Finder]] pipeline — **does not automatically govern NHSD data.** Different owner, different licence. [[Healthdirect_NHSD_API]] already says the NHSD licence is disclosed at registration, which is right; this note records *why* that is a genuinely separate question rather than the same notice a second time.

## cyber.gov.au — standards, not data

The ACSC publishes no health data. It is worth knowing for one reason: **Secure by Design / Secure by Default** is the Australian-authored framing that matches what [[System_Overview]] already argues about privacy posture. That doc makes the case that the Privacy Act's health-information obligations never *arise* here because no personal information is ever collected — data minimisation taken to its endpoint. ACSC's language ("consider cyberthreats from the outset… protect consumer privacy and data through designing, developing and delivering products with fewer vulnerabilities") is the same argument from the security side, and it is a domestic source rather than a borrowed overseas one.

Use it if the posture ever needs defending to a third party — an app store reviewer, NHSD's conformance gate, or the Department. Do not use it as a data source; there isn't one.

## Prior art — is this already published? (checked 29/07/2026)

Before building three ingestion pipelines it is worth knowing whether the data is already sitting on `data.gov.au`. Searched all three domains. **Answer: no, and the negative results are load-bearing.** Re-run these before any major build decision; the searches are recorded so the check is repeatable rather than a one-off impression.

| Searched | Hits | What was actually there | Consequence |
|---|---|---|---|
| `emergency department waiting times` | 649 | **Historical aggregates only.** SA Health median waiting times by financial year (XLS/CSV, updated 26/06/2025); Queensland Health ED performance data pre-October 2020; assorted annual reports. Nothing live, nothing per-facility current | **The live NSW feed is the differentiator.** [[NSW_Health_JSON_Engine]] is not duplicating an open dataset — no open dataset does this |
| `pharmacy location opening hours` | 241 | **Nothing relevant.** Every hit was an annual report or a government gazette | **NHSD is the only route.** The ~3-month onboarding in [[Healthdirect_NHSD_API]] cannot be side-stepped by downloading a directory, because no such directory is published |
| `medical costs finder out of pocket specialist fees` | 3 | **Nothing relevant.** Three unrelated annual reports | **Rung 2 of the [[Medical_Costs_Finder]] ladder checked: still nothing.** The permission route remains the only live option |

Two things worth carrying forward:

- **SA Health and Queensland Health both publish ED data.** Aggregate, not live, so useless for the app's core promise — but it means those jurisdictions have a data-publishing habit and an obvious contact surface if the app ever expands past NSW. Queensland also has a distinct *Rural Emergency Departments* dataset.
- **`data.gov.au` has a "Suggest a new dataset" function.** That turns rung 2 from passive watching into something you can actually push on: request the MCF extract formally, in parallel with the permission email. Two independent routes to the same unblock, and the request costs nothing.

**Not checked:** whether a competing consumer app already does this. That is a different question from "is the data published" and has not been investigated.

## The name-collision trap

`digital.gov.au` is the Digital Transformation Agency. `digital.org.au` is the **Australian Digital Alliance**, a copyright-reform advocacy body — not government at all. Near-identical names, opposite sides of the fence.

Worth knowing for [[Medical_Costs_Finder]]: the ADA works on copyright in the public interest, which is adjacent to our permission problem. They are an advocacy group, not a licensing authority, so **they cannot grant us anything** — do not mistake a sympathetic position for permission.

The general pattern, if you want a non-health example: `myetoll.transport.nsw.gov.au` is a government-branded front, while the entity actually holding the account is Linkt. The `.gov.au` badge marks *who fronts the service*, not *who owns the data or signs the agreement*.

## What is verified and what is not

Recorded honestly, because a note about checking authorities is worthless if it smuggles in unchecked claims.

**Verified 29/07/2026 by reading `directory.gov.au` directly:**

- Healthdirect Australia is absent from the Health, Disability and Ageing *Commonwealth entities and companies* list (full list read, not truncated).
- It is present in the Directory as *Healthdirect Australia Board of Directors*.
- AGOR is downloadable as a dataset from `data.gov.au`.
- The portfolio is currently named Health, Disability and Ageing.

**Inferred, not verified — treat as a lead:**

- Healthdirect Australia's precise ownership and constitution. The absence above is consistent with a company owned jointly by the Commonwealth and the states/territories, which would place it outside a Commonwealth-only register, but no primary source was read for this.
- That the `health.gov.au` copyright notice therefore does not reach NHSD data. This follows from the entity separation but is a legal conclusion drawn from a directory listing, not from NHSD's own terms. **The NHSD licence arrives at registration — that document settles it, nothing here does.**

## Open items

- [ ] Confirm Healthdirect Australia's constitution and ownership from a primary source (annual report or constitution), and record it here
- [ ] When the NHSD Agreement and licence arrive, record the actual licensor and terms in [[Healthdirect_NHSD_API]] and strike the inference above
- [ ] Check whether AGOR on `data.gov.au` is worth ingesting as a small reference table, or whether an occasional manual lookup is enough — likely the latter, it is governance context and not user-facing
