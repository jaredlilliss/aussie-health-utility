> **Public mirror.** This is a curated snapshot of a private working repository,
> published for portfolio review. The working repo's history, correspondence
> records, and operational runbooks stay private; everything here — schema,
> pipeline code, tests, and the engineering log — is the real thing.
> Source-available for reading; all rights reserved.

# Aussie Health Utility

A data pipeline for Australian public health information, and the documentation
that goes with it. Three sources feed one local Postgres cache: **NSW emergency
department queues** (live), **pharmacy hours and open/closed status** (pending
directory access), and **Medicare Benefits Schedule fees plus typical specialist
costs** (MBS loaded, costs pending permission).

Privacy-by-design, structurally rather than aspirationally: there are no tables
or columns for users, accounts, sessions, devices or any person identifier, and
`03_Local_Cache/Postgres_Cache_Schema.md` maintains that as a permanent
forbidden list rather than a convention.

---

## Run the tests

```bash
python -m unittest discover -s tests -t .
```

That is the whole setup. **148 tests, offline, no database, no fixtures beyond
the ones committed, in about 0.03 seconds.** No virtualenv needed to run them
and no service to stand up first — the Postgres writers are exercised against a
fake cursor. Verified on Python 3.12 and 3.13, Windows and Ubuntu.

Seven test classes are guarded by `skipUnless(WINDOWS)` and skip on Linux. A
Linux run reporting *zero* skips means that gating has broken, not that
everything passed — CI asserts this explicitly rather than checking a count,
because counts expire.

---

## Is it actually running?

**Yes — on an always-on Melbourne host since 23 August 2026, at 96 of a possible
96 cycles a day.**

Since 20 July 2026 the collector has captured **77,054 snapshots** across **1,306
cycles** and exactly 59 reporting hospitals, no ragged cycles, no cleaning step.
Of **1,275 logged runs, 1,257 succeeded**. Most failures were upstream connection
errors to the NSW Health host; one was a deliberate failure injection against a
dead local port, to prove the backoff logic fires.

**Lifetime coverage is about 35%, and the shape of that number is the interesting
part — nearly every missing cycle is from the first month.** A spot check on 27
August returned **96 cycles in the preceding 24 hours, zero failed runs, and zero
outages across 5,664 snapshots**, with the newest reading three minutes old. Cron
with `flock -n` supplies the single-instance guarantee, which is why the cadence
is exactly 96 rather than approximately 96.

The gap was never a code failure, and the diagnosis is worth keeping. For its
first month the collector ran on a Windows laptop, where Modern Standby suspends
desktop processes once the machine idles. The process did not crash — it started,
completed a cycle, entered its wait, and was frozen mid-`sleep()` by the operating
system. It resumed looking healthy, which is why it took several days and four
wrong theories to find.

Each of those theories is recorded in the repository rather than quietly
dropped, two of them in pull requests that exist solely to retract earlier
claims:

- **Task Scheduler as the launcher** — five configurations tested. This machine
  runs `cmd.exe` from the scheduler happily and has never once launched Python
  from it.
- **A per-interpreter problem** — both installed Python versions run the full
  cycle identically. Retracted in PR #11.
- **File permissions, antivirus interference, or path redirection** — ruled out
  by inspecting loaded modules and by a fresh process writing the same path.
- **Interposing `cmd.exe` at logon** — armed as an experiment, ran on a real
  reboot, failed. The negative result is what closed the question.

The diagnosis came from a live stack dump (`py-spy dump`) of the frozen process,
which placed it inside its between-cycle wait rather than dead at startup, and
from correlating Kernel-Power 506/507 events against the freeze window. A
supporting finding: the wake log that appeared to prove the machine was awake
actually recorded *wakes* — its entries land in the same second as
standby-exit events, so a machine surfacing for one second every fifteen minutes
looks identical to one that never slept.

The remedy was a $5/month always-on host, specified in
`03_Local_Cache/VPS_Migration.md`. Not another launcher.

**Why gaps are permanent.** The upstream API serves current state only; there is
no historical endpoint. A missed reading cannot be backfilled, ever. That
constraint is why the collector was built before any user interface — the
interface can be written at any time, the data cannot.

---

## Attribution and licensing

**NSW emergency department data is © State of New South Wales (NSW Ministry of
Health), licensed CC BY 4.0.** The NSW Health copyright notice licenses all its
material under CC BY 4.0 and explicitly encourages reuse of publicly funded
information. The required attribution form, recorded 20 July 2026 and reproduced
here verbatim, must appear wherever this data is displayed:

> © State of New South Wales NSW Ministry of Health. For current information go
> to www.health.nsw.gov.au.

Medicare Benefits Schedule item data is Commonwealth material. A written request
to display typical-cost data with attribution was sent to the Department of
Health on 10 August 2026 and has not been answered; **until it is, typical-cost
data is not displayed.** That is why the MBS table holds item reference data and
the typical-cost table is empty — a permission boundary, not an unfinished
feature.

Pharmacy directory data is not collected at all pending directory onboarding.
Those tables exist and are empty by design.

The pipeline code itself is source-available for reading; all rights reserved.

---

## Two constraints on describing this data

**It is not a waiting *time*.** The feed publishes a count of people waiting for
treatment. There is no minutes field anywhere in it.

**It is not "statewide".** This covers the 59 emergency departments that publish
live counts. NSW has 176.

---

## Layout

| Path | What it is |
| --- | --- |
| `src/` | The pipeline. `ed_waits/` is the NSW ED package; `ingest_*.py` are the entry points. |
| `scripts/` | Host-side launchers, Windows-specific, all retiring at the VPS cutover. See `scripts/README.md`. |
| `tests/` | The suite. Offline by design. |
| `db/init/` | Schema DDL. Kept in step with the vault doc; the two move together in the same PR. |
| `01_Architecture/` | System overview, privacy posture, non-goals. |
| `02_Data_Pipelines/` | One document per source, including what was tried and disproven. |
| `03_Local_Cache/` | Schema rationale, deployment status ledger, VPS migration plan. |

---

## Conventions

Work ships as **one pull request per finding, squash-merged, with the evidence
in the commit body — including what was disproven.** A retracted theory left
standing costs the next reader a day, which is why PRs #11 and #18 exist.

The vault documents under `0*_` are the source of truth for behaviour. Code and
documentation move together in the same change; the status ledger in
`Local_Deployment.md` never claims a run that did not happen.

---

## Status

Emergency department collection works end to end. Pharmacy data is blocked on
directory onboarding, not on code. Typical-cost data is blocked on a written
permission request to the responsible department. There is no user-facing
application yet — the current surface is a report generator and a dry-run
printer, and that is a sequencing decision rather than an omission.
