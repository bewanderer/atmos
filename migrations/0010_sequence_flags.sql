-- Two fixes the audit turned up.
--
-- 1. low_cost_calibrated is described as a tier everywhere but the constraint
--    rejected it, so the calibrated series we plan could never be stored.
-- 2. flatline and zero_run were in the flag vocabulary, counted against
--    consensus, and produced by nothing at all.

begin;

alter table sources drop constraint sources_tier_valid;
alter table sources add constraint sources_tier_valid check (tier in (
  'reference', 'independent_reference', 'low_cost', 'low_cost_calibrated',
  'satellite', 'modelled', 'aggregator', 'derived'
));


-- A stuck instrument repeats itself. Detected by grouping runs of an identical
-- value at one station and parameter, then flagging runs past the threshold.
--
-- Bounded by p_since because this reads a window of history, and at ten million
-- rows a year an unbounded sweep is not something to run casually.
create function apply_sequence_flags(
  p_since   timestamptz default now() - interval '7 days',
  p_ruleset text default 'sequence-1'
) returns integer
language plpgsql
-- Invoker rights, matching apply_range_flags. The ingest role already holds
-- everything needed: read observations and thresholds, insert flags. Definer
-- rights would need the owner granted reads it has no other reason to have.
as $$
declare
  written integer;
begin
  with ordered as (
    select o.station_id, o.parameter_id, o.phenomenon_start, o.phenomenon_end,
           o.revision, o.value,
           t.flatline_periods, t.zero_run_periods,
           -- Islands: the difference between the two row numbers stays constant
           -- for as long as the value does not change.
           row_number() over w_all - row_number() over w_val as run_id
      from observations o
      join quality_thresholds t on t.parameter_id = o.parameter_id
     where o.revision = 1
       and o.value is not null
       and o.phenomenon_start >= p_since
    window
      w_all as (partition by o.station_id, o.parameter_id order by o.phenomenon_start),
      w_val as (partition by o.station_id, o.parameter_id, o.value
                order by o.phenomenon_start)
  ),
  runs as (
    select station_id, parameter_id, run_id, value,
           min(flatline_periods) as flatline_periods,
           min(zero_run_periods) as zero_run_periods,
           count(*) as run_length
      from ordered
     group by station_id, parameter_id, run_id, value
  ),
  offending as (
    select o.station_id, o.parameter_id, o.phenomenon_start, o.phenomenon_end,
           o.revision,
           case when o.value = 0 then 'zero_run' else 'flatline' end as flag,
           'value ' || o.value || ' repeated ' || r.run_length
             || ' times in a row' as detail
      from ordered o
      join runs r
        on r.station_id = o.station_id and r.parameter_id = o.parameter_id
       and r.run_id = o.run_id
     where (o.value = 0 and r.run_length >= r.zero_run_periods)
        or (o.value <> 0 and r.run_length >= r.flatline_periods)
  ),
  ins as (
    insert into observation_flags
      (station_id, parameter_id, phenomenon_start, phenomenon_end, revision,
       flag, ruleset_version, detail)
    select station_id, parameter_id, phenomenon_start, phenomenon_end, revision,
           flag, p_ruleset, detail
      from offending
    on conflict do nothing
    returning 1
  )
  select count(*) into written from ins;
  return written;
end
$$;

alter function apply_sequence_flags owner to atmos_owner;
grant execute on function apply_sequence_flags to atmos_ingest;

comment on function apply_sequence_flags is
  'Flags stuck instruments: a value repeated past its threshold. Zeros are '
  'flagged separately because a single zero is often a real below-limit '
  'reading, while a long run of them is a dead sensor. Safe to re-run.';

commit;
