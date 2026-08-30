-- Atmos initial schema.
-- Observations are append only. That property is enforced by grants at the bottom
-- of this file, not by application code.

begin;

create extension if not exists postgis;


-- Roles. The owner runs migrations. The app never connects as owner.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'atmos_owner') then
    create role atmos_owner nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'atmos_ingest') then
    create role atmos_ingest login;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'atmos_api') then
    create role atmos_api login;
  end if;
end
$$;


-- Where data comes from. One row per distinct publisher.
create table sources (
  id                bigserial primary key,
  slug              text        not null unique,
  name              text        not null,
  operator          text,
  tier              text        not null,
  base_url          text,
  license           text,
  license_url       text,
  terms_url         text,
  terms_snapshot_at timestamptz,
  attribution       text        not null,
  is_primary        boolean     not null,
  is_active         boolean     not null default true,
  notes             text,
  created_at        timestamptz not null default now(),

  constraint sources_tier_valid check (tier in (
    'reference', 'independent_reference', 'low_cost',
    'satellite', 'modelled', 'aggregator'
  ))
);

comment on column sources.is_primary is
  'False for republishers. Aggregators are never a source of record.';


-- Controlled vocabulary. No free text parameter names anywhere in the system.
create table parameters (
  id             smallserial primary key,
  code           text not null unique,
  canonical_unit text not null,
  description    text
);

insert into parameters (code, canonical_unit, description) values
  ('pm10',  'ug/m3', 'Particulate matter under 10 micrometres'),
  ('pm25',  'ug/m3', 'Particulate matter under 2.5 micrometres'),
  ('so2',   'ug/m3', 'Sulphur dioxide'),
  ('no2',   'ug/m3', 'Nitrogen dioxide'),
  ('no',    'ug/m3', 'Nitrogen monoxide'),
  ('nox',   'ug/m3', 'Nitrogen oxides'),
  ('o3',    'ug/m3', 'Ozone'),
  ('co',    'mg/m3', 'Carbon monoxide'),
  ('h2s',   'ug/m3', 'Hydrogen sulphide'),
  ('c6h6',  'ug/m3', 'Benzene'),
  ('temp',  'degC',  'Air temperature'),
  ('rh',    'pct',   'Relative humidity'),
  ('press', 'hPa',   'Air pressure'),
  ('wspd',  'm/s',   'Wind speed'),
  ('wdir',  'deg',   'Wind direction');


-- Measurement locations, as published by each source.
-- The same physical site appearing under two sources stays as two rows.
-- Linking them is the job of station_pairs, not of this table.
create table stations (
  id                bigserial   primary key,
  source_id         bigint      not null references sources(id),
  source_station_id text        not null,
  name              text        not null,
  geom              geography(point, 4326),
  elevation_m       numeric,
  station_type      text        not null default 'unknown',
  area_type         text        not null default 'unknown',
  is_indoor         boolean     not null default false,
  is_mobile         boolean     not null default false,
  first_seen_at     timestamptz not null default now(),
  last_seen_at      timestamptz,

  unique (source_id, source_station_id),
  constraint stations_type_valid check (station_type in
    ('background', 'traffic', 'industrial', 'unknown')),
  constraint stations_area_valid check (area_type in
    ('urban', 'suburban', 'rural', 'unknown'))
);

create index stations_geom_idx on stations using gist (geom);
create index stations_source_idx on stations (source_id);


-- Instrument and method, where the operator discloses it.
-- Frequently absent for BiH sources. The absence is itself worth recording.
create table instruments (
  id           bigserial primary key,
  station_id   bigint    not null references stations(id),
  parameter_id smallint  not null references parameters(id),
  method       text,
  model        text,
  manufacturer text,
  disclosed    boolean   not null default false,
  valid_from   timestamptz,
  valid_to     timestamptz
);


-- Every request we ever make. This is the root of the provenance chain.
create table fetches (
  id             bigserial   primary key,
  source_id      bigint      not null references sources(id),
  url            text        not null,
  requested_at   timestamptz not null,
  http_status    int,
  content_sha256 bytea,
  content_bytes  int,
  storage_key    text,
  archive_mode   text        not null,
  external_ref   text,
  ok             boolean     not null,
  error          text,
  duration_ms    int,

  constraint fetches_archive_mode_valid check (archive_mode in ('bytes', 'reference')),
  -- Archived bytes must be addressable, or the provenance chain is broken.
  constraint fetches_bytes_have_key check (
    archive_mode <> 'bytes' or not ok or storage_key is not null
  )
);

create index fetches_source_time_idx on fetches (source_id, requested_at desc);
create index fetches_failed_idx on fetches (source_id, requested_at desc) where not ok;

comment on column fetches.archive_mode is
  'bytes when the source keeps no durable archive of its own, which covers every BiH '
  'source. reference when the source maintains a permanent citable archive, such as '
  'Copernicus, in which case external_ref identifies the granule.';


-- The record itself.
--
-- A logical reading is identified by (station, parameter, phenomenon window).
-- Several rows may share that identity, separated by revision number.
-- Revision 1 is canonical and is what we serve by default.
--
-- There is deliberately no self referencing foreign key. The revision chain is
-- fully determined by the logical key plus the revision number.
create table observations (
  id                bigserial   not null,
  station_id        bigint      not null references stations(id),
  parameter_id      smallint    not null references parameters(id),
  phenomenon_start  timestamptz not null,
  phenomenon_end    timestamptz not null,

  value             numeric,
  unit              text        not null,
  raw_value         text,
  raw_unit          text,

  revision          int         not null default 1,
  revision_kind     text,
  previous_value    numeric,

  confirmations     int         not null default 1,
  first_seen_at     timestamptz not null,
  last_confirmed_at timestamptz not null,

  fetch_id          bigint      not null,
  parser_version    text        not null,
  is_backfill       boolean     not null default false,
  quality_flags     text[]      not null default '{}',

  primary key (id, phenomenon_start),

  constraint obs_window_ordered check (phenomenon_end > phenomenon_start),
  constraint obs_revision_positive check (revision >= 1),
  constraint obs_kind_valid check (
    revision_kind is null or revision_kind in
      ('value_change', 'withdrawal', 'reinstatement')
  ),
  -- Revision 1 is an original observation, never a change to something else.
  constraint obs_first_revision_unmarked check (
    (revision = 1) = (revision_kind is null)
  ),
  -- A null value is only meaningful as a withdrawal.
  constraint obs_value_present_unless_withdrawn check (
    value is not null or revision_kind = 'withdrawal'
  )
) partition by range (phenomenon_start);

-- One row per revision of a logical reading.
create unique index observations_logical_key_idx on observations
  (station_id, parameter_id, phenomenon_start, phenomenon_end, revision);

-- Canonical reads. Most queries hit this.
create index observations_canonical_idx on observations
  (station_id, parameter_id, phenomenon_start) where revision = 1;

-- Everything that was ever altered upstream.
create index observations_revised_idx on observations
  (station_id, phenomenon_start) where revision > 1;

create index observations_fetch_idx on observations (fetch_id);

comment on table observations is
  'Append only. Update and delete are revoked from every application role. '
  'Only confirm_observation() may touch the confirmation counters.';


-- Monthly partitions. A maintenance job creates these ahead of time.
-- Sensor.Community reaches 2015, so early months exist from the start.
--
-- Monthly rather than yearly because Sensor.Community reports every 2.5 minutes:
-- a year holds roughly 10 million rows, a month roughly 830 thousand. Partition
-- size is invisible to queries, which span as many months as they like.
do $$
declare
  y int;
  m int;
  name text;
  lo timestamptz;
begin
  for y in 2015..2027 loop
    for m in 1..12 loop
      name := format('observations_%s_%s', y, lpad(m::text, 2, '0'));
      lo := make_timestamptz(y, m, 1, 0, 0, 0, 'UTC');
      execute format(
        'create table %I partition of observations for values from (%L) to (%L)',
        name, lo, lo + interval '1 month'
      );
      -- Ownership does not cascade from the parent, so set it per partition.
      -- The maintenance job that adds future months must do the same.
      execute format('alter table %I owner to atmos_owner', name);
    end loop;
  end loop;
end
$$;

-- Anything outside the known range lands here rather than failing an insert.
-- A row appearing in this table means the partition job fell behind.
create table observations_overflow partition of observations default;
alter table observations_overflow owner to atmos_owner;


-- Operational tables. These are mutable, unlike observations.
create table collector_runs (
  id            bigserial   primary key,
  source_id     bigint      not null references sources(id),
  started_at    timestamptz not null,
  finished_at   timestamptz,
  targets_total int,
  targets_ok    int,
  observations_inserted int,
  revisions_inserted    int,
  ok            boolean,
  error         text
);

create index collector_runs_source_idx on collector_runs (source_id, started_at desc);

-- A parse failure is recoverable, since the bytes are already archived.
-- This table is the work queue for fixing parsers.
create table parse_failures (
  id             bigserial   primary key,
  fetch_id       bigint      not null references fetches(id),
  parser_version text        not null,
  error          text        not null,
  occurred_at    timestamptz not null default now(),
  resolved_at    timestamptz
);

create index parse_failures_open_idx on parse_failures (occurred_at desc)
  where resolved_at is null;


-- Append only enforcement.
--
-- The owner keeps full rights so migrations work. Application roles get the
-- narrowest grant that lets them do their job, and neither can alter or remove
-- an observation. An attacker holding either credential cannot change the record.
alter table observations owner to atmos_owner;

revoke all on observations from public;
grant insert, select on observations to atmos_ingest;
grant select on observations to atmos_api;

grant select, insert on sources, stations, parameters, instruments, fetches to atmos_ingest;
grant insert, update on collector_runs, parse_failures to atmos_ingest;
grant select on sources, stations, parameters, instruments, fetches to atmos_api;

grant usage, select on all sequences in schema public to atmos_ingest;


-- The single permitted mutation. Runs as owner so it can update, but it can
-- only ever touch the two counter columns.
create function confirm_observation(
  p_station_id   bigint,
  p_parameter_id smallint,
  p_start        timestamptz,
  p_end          timestamptz,
  p_revision     int,
  p_seen_at      timestamptz
) returns void
language sql
security definer
set search_path = public
as $$
  update observations
     set confirmations     = confirmations + 1,
         last_confirmed_at = greatest(last_confirmed_at, p_seen_at)
   where station_id       = p_station_id
     and parameter_id     = p_parameter_id
     and phenomenon_start = p_start
     and phenomenon_end   = p_end
     and revision         = p_revision;
$$;

alter function confirm_observation owner to atmos_owner;
revoke all on function confirm_observation from public;
grant execute on function confirm_observation to atmos_ingest;

comment on function confirm_observation is
  'The only sanctioned write to an existing observation row. Bumps the '
  'confirmation counter when a source republishes a value we already hold '
  'unchanged. Cannot alter a measured value.';

commit;
