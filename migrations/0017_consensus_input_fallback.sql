-- One vote per instrument, without throwing the vote away.
--
-- The previous definition kept a reading only where the station was its own
-- canonical station. That counts an instrument once, which is right, but it
-- also drops the reading entirely on any hour the canonical publisher happens
-- to be silent, even though another publisher carried the same instrument's
-- value. Forty five Lukavac PM10 readings were being lost that way: Tuzla
-- Canton published them, FHMZ did not, so consensus saw nothing at all.
--
-- The instrument should vote once. It should not lose its vote because the
-- publisher we prefer was quiet.
--
-- So: one row per instrument per reading, preferring the canonical publisher
-- and falling back to whoever else carried it. The old comment already claimed
-- this behaviour, which is how the gap stayed invisible.

begin;

create or replace view consensus_eligible as
select distinct on (
         canonical_station(o.station_id, o.phenomenon_start),
         o.parameter_id, o.phenomenon_start, o.phenomenon_end)
       o.*, canonical_station(o.station_id, o.phenomenon_start) as canonical_station_id
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
   )
 order by canonical_station(o.station_id, o.phenomenon_start),
          o.parameter_id, o.phenomenon_start, o.phenomenon_end,
          -- The canonical publisher first, anyone else only if it is absent.
          (o.station_id = canonical_station(o.station_id, o.phenomenon_start)) desc,
          o.station_id;

comment on view consensus_eligible is
  'Default consensus input. A filter, not a deletion. One row per instrument '
  'per reading, preferring the canonical publisher and falling back to another '
  'that carried it rather than losing the reading.';

commit;
