-- Aussie Health Utility: cache schema.
-- Source of truth: 03_Local_Cache/Postgres_Cache_Schema.md (keep the two in sync).
-- Public data only. No tables or columns for users, accounts, sessions,
-- devices, or any person-identifier. See the forbidden-tables rule in the doc.

-- ED facilities (sources: Data.NSW CKAN mirror / RTED GetHospitalsReport)
create table facilities (
  id               bigint generated always as identity primary key,
  source           text not null,                 -- e.g. 'data_nsw_ckan'
  source_id        text not null,                 -- stable ID from the feed
  name             text not null,
  facility_type    text not null default 'ed',
  address          text,
  suburb           text,
  postcode         text,
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
-- The retention delete filters on captured_at alone, which the index above
-- cannot serve (its leading column is facility_id). Without this the prune
-- sequentially scans the whole table on every 15-minute commit. Harmless at
-- today's 16k rows; at full VPS coverage the table reaches ~2M rows a year.
create index on ed_wait_snapshots (captured_at);

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
  note           text,
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
  source_release date not null
);

-- Typical costs layer (source: Medical Costs Finder)
create table mcf_typical_costs (
  id            bigint generated always as identity primary key,
  item_num      integer references mbs_items(item_num),
  service_label text not null,
  region        text not null,
  setting       text,
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
  pipeline    text not null,
  started_at  timestamptz not null default now(),
  finished_at timestamptz,
  ok          boolean,
  detail      text
);
