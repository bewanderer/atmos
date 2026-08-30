-- Confirm many readings in one call.
--
-- Re-observing is the common case, not the exception: FHMZ reprints a six day
-- window every three hours, so most of what arrives is already held. Doing that
-- one row at a time cost 5.75 ms each, almost all of it round trip and function
-- call rather than real work, which made re-ingest seven times slower than a
-- fresh load. Measured at 150 rows per second against 1,121.
--
-- The single row confirm_observation() stays for callers that genuinely have one.

begin;

create function confirm_observations(
  p_station   bigint[],
  p_parameter smallint[],
  p_start     timestamptz[],
  p_end       timestamptz[],
  p_revision  int[],
  p_seen_at   timestamptz
) returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  touched integer;
begin
  with wanted as (
    select unnest(p_station)   as station_id,
           unnest(p_parameter) as parameter_id,
           unnest(p_start)     as phenomenon_start,
           unnest(p_end)       as phenomenon_end,
           unnest(p_revision)  as revision
  )
  update observations o
     set confirmations     = o.confirmations + 1,
         last_confirmed_at = greatest(o.last_confirmed_at, p_seen_at)
    from wanted w
   where o.station_id       = w.station_id
     and o.parameter_id     = w.parameter_id
     and o.phenomenon_start = w.phenomenon_start
     and o.phenomenon_end   = w.phenomenon_end
     and o.revision         = w.revision;

  get diagnostics touched = row_count;
  return touched;
end
$$;

alter function confirm_observations owner to atmos_owner;
revoke all on function confirm_observations from public;
grant execute on function confirm_observations to atmos_ingest;

comment on function confirm_observations is
  'Batched confirm. Same guarantee as the single row version: touches only the '
  'two counter columns and cannot alter a measured value.';

commit;
