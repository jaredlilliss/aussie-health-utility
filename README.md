> **Public mirror.** This is a curated snapshot of a private working repository,
> published for portfolio review. The working repo's history, correspondence
> records, and operational runbooks stay private; everything here — schema,
> pipeline code, tests, and the engineering log — is the real thing.
> Source-available for reading; all rights reserved.

# Aussie Health Utility

**Live: [the Queue Board](https://jaredlilliss.github.io/queue-board/)** — 59 NSW
emergency departments, current counts, five weeks of collection, and how much of
each hospital's swing is shared with the rest of the state. Refreshed every three
hours from the collector described below.

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

## What it found

Three things, from 77,000 readings. They are on the site above; the reasoning is
here because the method is the part worth checking.

**Monday is the busiest day of the week** — 6.06 people waiting on average
against Sunday's 3.99, a 52% swing. This one survives the collection gaps
described below, because it pools raw readings rather than comparing whole days
to each other: a day with six readings simply contributes six.

**Emergency department load is mostly local.** When one department is unusually
busy, the rest of the state largely is not. The most system-coupled hospital
shares about a sixth of its variation with everywhere else; the quieter ones are
statistically indistinguishable from no relationship at all.

The naive version of that second finding would have been wrong in a way that
looked right. Correlating raw counts between any two hospitals produces a strong
number — but only because both are quiet at dawn and busy in the evening, so the
measurement rediscovers the daily cycle and presents it as a result. So each
reading has **that hospital's own average for that hour of day** subtracted
first, leaving only what is unusual for it at that time. The comparison series
then **excludes the hospital being measured**, because otherwise a large hospital
partly correlates with itself and scores too high.

The noise floor is stated rather than hidden: at 1,313 shared observations the
standard error of a correlation is about 0.028, so anything inside ±0.05 cannot
be distinguished from zero and is labelled as noise instead of being reported as
a negative relationship.

**One caveat is load-bearing and cost the tidier headline.** Coupling tracks
hospital size — the busier half averages 0.22 against 0.14 for the quieter half.
The tempting reading is that metropolitan hospitals share demand. That is not
claimed, because a large part of the effect is arithmetic rather than medicine: a
small department's count is dominated by whether one or two people happened to
arrive, and that randomness dilutes any shared signal. A low score at a small
hospital is partly a measurement artefact.

---

## The collection is not clean, and the interface says so

Of the days collected, **only four captured 90 or more of the 96 possible
readings**. For its first month the collector ran on the Windows laptop described
above, so most days are partial — some hold a single reading — and five days
captured nothing at all. Since the move to an always-on host on 23 August,
nothing is missing.

That is why the interface leads with **readings captured per day** rather than a
daily average line. Charting a mean computed from one reading beside a mean
computed from ninety-six would produce a clean, plausible, meaningless chart. The
per-hospital table shows observed-day coverage for the same reason: a hospital
seen on fewer days has a less reliable average, and hiding that would make the
table look more certain than the data supports.

**A detail that falls out of the two collectors overlapping.** Cron fires on
exact quarter hours, so readings from the always-on host land at `:00`, `:15`,
`:30` and `:45` — 24 of each, 96 a day, never drifting. The laptop ran a sleeping
loop, so its readings landed wherever it happened to wake: `:02`, `:11`, `:26`,
`:39`. The cutover day holds 115 readings rather than 96 — the 96 clean ones plus
19 drifting ones from the laptop still running alongside. Both were live for a
few hours, and **no reading was double-counted: across all 77,000 rows there is
not one duplicate of the same hospital at the same instant.**

---

## Status

Emergency department collection works end to end, and **now has a public
interface** — see the link at the top. Pharmacy data is blocked on directory
onboarding, not on code. Typical-cost data is blocked on a written permission
request to the responsible department, sent 10 August 2026 and not yet answered;
until it is, that table stays empty by permission boundary rather than by
omission. The command-line report generator and dry-run printer remain.
