-- Quality flags as their own append only table.
--
-- Observations cannot be updated, so a flag worked out later cannot be written
-- onto the row. Flags therefore live here, keyed by the reading they describe,
-- and carry the ruleset version that produced them so they can be re-derived
-- or argued with.

begin;

create table observation_flags (
  station_id       bigint      not null references stations(id),
  parameter_id     smallint    not null references parameters(id),
  phenomenon_start timestamptz not null,
  phenomenon_end   timestamptz not null,
  revision         int         not null,
  flag             text        not null references quality_flags(code),
  ruleset_version  text        not null,
  detail           text,
  flagged_at       timestamptz not null default now(),

  primary key (station_id, parameter_id, phenomenon_start, phenomenon_end,
               revision, flag, ruleset_version)
);

create index observation_flags_lookup_idx
  on observation_flags (station_id, parameter_id, phenomenon_start);

comment on table observation_flags is
  'Flags about a reading. Separate from observations because observations are '
  'append only and a flag is usually worked out after the fact.';


-- Temperature can legitimately go below zero. A concentration cannot.
alter table quality_thresholds add column allows_negative boolean not null default false;

-- Plausible ranges. Deliberately wide: the job is catching broken instruments,
-- not second guessing unusual weather.
insert into quality_thresholds
  (parameter_id, min_plausible, max_plausible, flatline_periods, zero_run_periods,
   allows_negative)
select id, mn, mx, 12, 12, neg from parameters p
join (values
  ('pm1',   0,   2000, false),
  ('pm10',  0,   5000, false),
  ('pm25',  0,   3000, false),
  ('so2',   0,  10000, false),
  ('no',    0,  10000, false),
  ('no2',   0,  10000, false),
  ('nox',   0,  20000, false),
  ('o3',    0,   2000, false),
  ('co',    0,    100, false),
  ('h2s',   0,   5000, false),
  ('c6h6',  0,    500, false),
  ('temp', -60,    60, true),
  ('rh',     0,   100, false),
  ('press', 800,  1100, false),
  ('wspd',   0,   120, false),
  ('wdir',   0,   360, false)
) as t(code, mn, mx, neg) on t.code = p.code
on conflict (parameter_id) do update
  set min_plausible = excluded.min_plausible,
      max_plausible = excluded.max_plausible,
      allows_negative = excluded.allows_negative;


-- Range checks. Cheap, need no neighbouring readings, and catch the obvious
-- broken instruments: a community sensor reporting -144 C, or a negative
-- concentration.
create function apply_range_flags(p_ruleset text default 'range-1')
returns integer
language plpgsql
as $$
declare
  written integer;
begin
  with found as (
    select o.station_id, o.parameter_id, o.phenomenon_start, o.phenomenon_end,
           o.revision,
           -- negative only where a negative value is physically impossible.
           -- Temperature below zero is weather, not a fault.
           case when o.value < 0 and not t.allows_negative
                then 'negative' else 'out_of_range' end as flag,
           'value ' || o.value || ' outside ' || t.min_plausible
             || '..' || t.max_plausible as detail
      from observations o
      join quality_thresholds t on t.parameter_id = o.parameter_id
     where o.value is not null
       and (o.value < t.min_plausible or o.value > t.max_plausible)
  ),
  ins as (
    insert into observation_flags
      (station_id, parameter_id, phenomenon_start, phenomenon_end, revision,
       flag, ruleset_version, detail)
    select station_id, parameter_id, phenomenon_start, phenomenon_end, revision,
           flag, p_ruleset, detail
      from found
    on conflict do nothing
    returning 1
  )
  select count(*) into written from ins;
  return written;
end
$$;

alter function apply_range_flags owner to atmos_owner;

comment on function apply_range_flags is
  'Flags readings outside the plausible range for their parameter. Safe to re-run.';


-- Rebuild the consensus input so it reads flags from the new table rather than
-- from the array on the observation.
drop view consensus_eligible;

create view consensus_eligible as
select o.*
  from observations o
  join station_status ss
    on ss.station_id = o.station_id
   and ss.parameter_id = o.parameter_id
 where o.revision = 1
   and o.value is not null
   and ss.status in ('active', 'delayed')
   and not exists (
     select 1
       from observation_flags f
       join quality_flags qf on qf.code = f.flag
      where f.station_id = o.station_id
        and f.parameter_id = o.parameter_id
        and f.phenomenon_start = o.phenomenon_start
        and f.phenomenon_end = o.phenomenon_end
        and f.revision = o.revision
        and qf.excludes_from_consensus
   );

comment on view consensus_eligible is
  'Default consensus input. A filter, not a deletion.';

grant select on observation_flags, consensus_eligible to atmos_api;
grant select, insert on observation_flags to atmos_ingest;
grant select on consensus_eligible to atmos_ingest;
grant execute on function apply_range_flags to atmos_ingest;

commit;
