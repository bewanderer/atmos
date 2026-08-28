-- Station status and quality flags.
-- Status is always derived, never set by hand.

begin;


-- Flag list. Whether a flag excludes from consensus is stored here, not in code.
create table quality_flags (
  code                    text primary key,
  description             text not null,
  excludes_from_consensus boolean not null default true
);

insert into quality_flags (code, description, excludes_from_consensus) values
  ('flatline',    'Identical value repeated beyond the parameter threshold', true),
  ('zero_run',    'Consecutive zeros beyond the parameter threshold', true),
  ('negative',    'Negative concentration, physically impossible', true),
  ('out_of_range','Outside plausible bounds for the parameter', true),
  ('spike',       'Step change beyond what the parameter can physically do', true),
  ('frozen_vs_neighbours', 'Flat while comparable nearby stations vary', true),
  -- These two describe how we got the timestamp. The value is fine, so they
  -- are recorded but exclude nothing.
  ('date_inferred', 'Timestamp taken from a sibling table, not the reading own row', false),
  ('dst_ambiguous', 'Falls in the repeated local hour at the autumn changeover', false);


-- Bounds per parameter. Stored, not coded, so a flag can be re-derived later.
create table quality_thresholds (
  parameter_id     smallint primary key references parameters(id),
  min_plausible    numeric,
  max_plausible    numeric,
  flatline_periods int not null default 12,
  zero_run_periods int not null default 12,
  max_step         numeric
);


-- What a station says it publishes.
-- Cannot come from observations: Vares shows PM10 and PM2.5 tables with every
-- cell empty. Without this we cannot tell a broken station from a missing one.
create table station_parameters (
  station_id    bigint   not null references stations(id),
  parameter_id  smallint not null references parameters(id),
  first_seen_at timestamptz not null default now(),
  last_seen_at  timestamptz,

  primary key (station_id, parameter_id)
);

comment on table station_parameters is
  'What the source page declares, not what it delivered. Set by connectors.';


-- Status per station and parameter, not per station. PM10 can be fine while
-- the SO2 analyser has been dead a year.
create table station_status (
  station_id          bigint   not null references stations(id),
  parameter_id        smallint not null references parameters(id),
  status              text     not null,
  last_observation_at timestamptz,
  observations_total  bigint   not null default 0,
  flagged_total       bigint   not null default 0,
  computed_at         timestamptz not null default now(),

  primary key (station_id, parameter_id),
  constraint station_status_valid check (status in
    ('active', 'delayed', 'stale', 'dormant', 'never_reported'))
);

create index station_status_status_idx on station_status (status);

comment on table station_status is 'Derived by refresh_station_status().';


-- Going quiet is worth recording. Coming back more so, since aggregators
-- just drop dead stations and never show it.
create table station_status_events (
  id                  bigserial primary key,
  station_id          bigint   not null references stations(id),
  parameter_id        smallint not null references parameters(id),
  from_status         text,
  to_status           text     not null,
  last_observation_at timestamptz,
  noted_at            timestamptz not null default now()
);

create index station_status_events_station_idx
  on station_status_events (station_id, noted_at desc);


-- Recompute status and log any change. Thresholds are arguments so sources
-- with different cadences can be judged on their own terms.
create function refresh_station_status(
  p_delayed_after interval default interval '6 hours',
  p_stale_after   interval default interval '3 days',
  p_dormant_after interval default interval '90 days'
) returns integer
language plpgsql
as $$
declare
  changed integer := 0;
begin
  with observed as (
    select o.station_id,
           o.parameter_id,
           max(o.phenomenon_start) as last_at,
           count(*)                as total,
           count(*) filter (where cardinality(o.quality_flags) > 0) as flagged
      from observations o
     where o.revision = 1
     group by o.station_id, o.parameter_id
  ),
  -- Driven by what stations declare, not what they delivered. Otherwise a
  -- station that never published anything gets no row at all.
  expected as (
    select sp.station_id, sp.parameter_id from station_parameters sp
    union
    select ob.station_id, ob.parameter_id from observed ob
  ),
  computed as (
    select e.station_id,
           e.parameter_id,
           coalesce(ob.last_at, null) as last_at,
           coalesce(ob.total, 0)      as total,
           coalesce(ob.flagged, 0)    as flagged,
           case
             when ob.last_at is null                        then 'never_reported'
             when now() - ob.last_at > p_dormant_after      then 'dormant'
             when now() - ob.last_at > p_stale_after        then 'stale'
             when now() - ob.last_at > p_delayed_after      then 'delayed'
             else 'active'
           end as status
      from expected e
      left join observed ob
        on ob.station_id = e.station_id and ob.parameter_id = e.parameter_id
  ),
  transitions as (
    insert into station_status_events
      (station_id, parameter_id, from_status, to_status, last_observation_at)
    select c.station_id, c.parameter_id, s.status, c.status, c.last_at
      from computed c
      left join station_status s
        on s.station_id = c.station_id and s.parameter_id = c.parameter_id
     where s.status is distinct from c.status
    returning 1
  )
  insert into station_status
    (station_id, parameter_id, status, last_observation_at,
     observations_total, flagged_total, computed_at)
  select c.station_id, c.parameter_id, c.status, c.last_at,
         c.total, c.flagged, now()
    from computed c
  on conflict (station_id, parameter_id) do update
    set status              = excluded.status,
        last_observation_at = excluded.last_observation_at,
        observations_total  = excluded.observations_total,
        flagged_total       = excluded.flagged_total,
        computed_at         = excluded.computed_at;

  get diagnostics changed = row_count;
  return changed;
end
$$;

alter function refresh_station_status owner to atmos_owner;

comment on function refresh_station_status is
  'Recomputes status and logs transitions.';


-- Input to consensus stats. Drops non-reporting stations and flagged rows.
-- Excluded from the maths, never from the record. The rows stay queryable, and
-- anything built on this must say what it left out.
create view consensus_eligible as
select o.*
  from observations o
  join station_status ss
    on ss.station_id = o.station_id
   and ss.parameter_id = o.parameter_id
 where o.revision = 1
   and ss.status in ('active', 'delayed')
   and not exists (
     select 1
       from unnest(o.quality_flags) f
       join quality_flags qf on qf.code = f
      where qf.excludes_from_consensus
   );

comment on view consensus_eligible is
  'Default consensus input. A filter, not a deletion.';


grant select on quality_flags, quality_thresholds, station_status,
                station_status_events, station_parameters,
                consensus_eligible to atmos_api;
grant select, insert, update on station_status, station_status_events,
                                station_parameters to atmos_ingest;
grant select on quality_flags, quality_thresholds, consensus_eligible to atmos_ingest;
grant execute on function refresh_station_status to atmos_ingest;

commit;
