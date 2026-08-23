---
aliases: [Cache Schema, Postgres Schema, DDL]
tags: [storage, postgres, schema, privacy-by-design]
created: 2026-06-11
status: draft
up: "[[System_Overview]]"
---

# Postgres Cache Schema

The single local store behind [[System_Overview]]. It is a **cache of public data**, nothing else. Fed by [[NSW_Health_JSON_Engine]], [[Healthdirect_NHSD_API]] and [[Medical_Costs_Finder]].

## Principles

1. **Public data only.** Every row is reconstructible from a public source. Losing the database loses nothing but time.
2. **No personal data, structurally.** See forbidden list below. This is what makes the privacy posture in [[System_Overview]] true rather than aspirational.
3. Every cached row carries provenance: `source` and/or `refreshed_at` / `source_date`.
4. **Snapshots are append-only** (wait counts, statuses); directory data is upserted.
5. Postgres gives us ACID for free; what we actually lean on is upserts, FK integrity, and one transaction per sync batch so a failed run never half-writes.

## Forbidden tables and columns (permanent)

No tables for: users, accounts, sessions, devices, favourites-tied-to-identity, push tokens, request logs containing IP + query.
No columns anywhere matching person-identifier patterns: `user_id`, `email`, `dob`, `date_of_birth`, `medicare_no`, `ihi`, `given_name`, `surname`, `device_id`, `ip_address`.

**Enforcement idea:** a 20-line schema lint in CI that greps `information_schema.columns` for the pattern list above and fails the build on a hit. Cheap, blunt, effective.

(`facilities.phone` and `pharmacies.phone` are business phone numbers from public directories, not personal data.)

## DDL

```sql
-- ED facilities (sources: Data.NSW CKAN mirror / RTED GetHospitalsReport)
create table facilities (
  id               bigint generated always as identity primary key,
  source           text not null,                 -- e.g. 'data_nsw_ckan'
  source_id        text not null,                 -- stable ID from the feed
  name             text not null,
  facility_type    text not null default 'ed',
  address          text,
  suburb           text,
  postcode         text,                          -- text, not numeric: leading zeros
  lhd              text,                          -- Local Health District
  phone            text,
  lat              double precision,
  lng              double precision,
  refreshed_at     timestamptz not null default now(),
  unique (source, source_id)
);

-- Live ED wait counts, append-only time series
create table ed_wait_snapshots (
  facility_id      bigint not null references facilities(id),
  captured_at      timestamptz not null,
  patients_waiting integer,
  treatment_spaces integer,
  outage           boolean not null default false,
  outage_text      text,
  primary key (facility_id, captured_at)
);
create index on ed_wait_snapshots (facility_id, captured_at desc);

-- Pharmacies (source: NHSD)
create table pharmacies (
  id           bigint generated always as identity primary key,
  nhsd_id      text not null unique,
  name         text not null,
  address      text,
  suburb       text,
  state        text,
  postcode     text,
  phone        text,
  lat          double precision,
  lng          double precision,
  refreshed_at timestamptz not null default now()
);

-- Declared standard hours (NHSD). PK includes opens to allow split hours.
create table pharmacy_hours (
  pharmacy_id  bigint not null references pharmacies(id),
  weekday      smallint not null check (weekday between 0 and 6), -- 0 = Monday
  opens        time not null,
  closes       time not null,
  source       text not null default 'nhsd',
  refreshed_at timestamptz not null default now(),
  primary key (pharmacy_id, weekday, opens)
);

-- Observed reality overrides (public holidays, one-off closures)
create table pharmacy_hours_overrides (
  id             bigint generated always as identity primary key,
  pharmacy_id    bigint not null references pharmacies(id),
  on_date        date not null,
  opens          time,
  closes         time,
  closed_all_day boolean not null default false,
  note           text,                            -- e.g. 'Good Friday 2026, observed'
  observed_at    timestamptz not null default now(),
  unique (pharmacy_id, on_date)
);

-- Live open/closed status (NHSD operationalStatus), append-only
create table pharmacy_status_snapshots (
  pharmacy_id  bigint not null references pharmacies(id),
  captured_at  timestamptz not null,
  status       text not null,
  primary key (pharmacy_id, captured_at)
);

-- MBS schedule (source: MBS Online XML releases)
create table mbs_items (
  item_num       integer primary key,
  descriptor     text not null,
  category       text,
  schedule_fee   numeric(10,2),
  benefit_75     numeric(10,2),
  benefit_85     numeric(10,2),
  benefit_100    numeric(10,2),
  source_release date not null                    -- XML release this row came from
);

-- Typical costs layer (source: Medical Costs Finder)
create table mcf_typical_costs (
  id            bigint generated always as identity primary key,
  item_num      integer references mbs_items(item_num),
  service_label text not null,
  region        text not null,                    -- as published: national/state/area
  setting       text,                             -- 'in_hospital' / 'out_of_hospital'
  typical_fee   numeric(10,2),
  typical_oop   numeric(10,2),
  medicare_paid numeric(10,2),
  insurer_paid  numeric(10,2),
  source_date   date not null,
  unique (item_num, region, setting, source_date)
);

-- Operational metadata only (no user data)
create table sync_runs (
  id          bigint generated always as identity primary key,
  pipeline    text not null,                      -- 'nsw_ed' / 'nhsd' / 'mbs' / 'mcf'
  started_at  timestamptz not null default now(),
  finished_at timestamptz,
  ok          boolean,
  detail      text
);
```

## Notes

- **Geo:** plain lat/lng + bounding-box filtering is enough at this scale; PostGIS only if "nearest open pharmacy" maths ever needs more. Distance ranking happens on device anyway (privacy rule in [[System_Overview]]).
- **Retention:** `ed_wait_snapshots` implemented 21/07/2026 — `ed_waits.db.prune_old_snapshots` deletes rows past `SNAPSHOT_RETENTION_DAYS` inside the same transaction as every `--waits` commit, so the 15-min loop self-enforces it; no cron/scheduler needed. **Raised 30 → 365 on 08/08/2026.** The 30-day figure was correct under an assumption that has since failed: that the live table is a cache and the dumps are the archive. `backup_db.py` has not run automatically since 05/08 (its Startup shortcut hits the machine's non-interactive freeze) and the gap went unnoticed for two days — so a 30-day prune was, in practice, a permanent-loss mechanism for a series the upstream API cannot reproduce. 365 makes the live table survive a backup outage measured in months. **This is reversible and arguably should be reversed once the VPS makes backups reliable** — at full coverage the table reaches ~2M rows/year (~270 MB), against ~16k rows and 2 MB today at 15% coverage. Add a dedicated index on `captured_at` at the same time (done 08/08): the delete filters on `captured_at` alone, which `(facility_id, captured_at desc)` cannot serve because its leading column is wrong, so the prune sequentially scanned the whole table on every commit. Free today, not free at 2M rows. **Existing deployments need the index created by hand** — `01_schema.sql` runs at init only, so the laptop cluster does not get it from this change; the VPS will. Verified live: inserted a synthetic 40-day-old row, one poll cycle pruned exactly it and left real data untouched. Hourly rollup stays deferred until a trend chart needs it (still no rollup table). Pharmacy `pharmacy_status_snapshots` retention (~7 days) is not yet implemented — no writer exists until the NHSD pipeline is built, so nothing to prune yet.
- **Display logic for hours:** `pharmacy_hours_overrides` for today's date wins → else `pharmacy_hours` → `pharmacy_status_snapshots` as live tie-breaker (documented in [[Healthdirect_NHSD_API]]).
