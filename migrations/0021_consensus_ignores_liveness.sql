-- A reading does not stop counting because its station went quiet later.
--
-- consensus_eligible filtered historical observations by the station's CURRENT
-- status, and status is computed from now() minus the last observation. Three
-- days of silence makes a station stale, and every reading it ever produced then
-- vanished from consensus.
--
-- Found when consensus over 30 August returned nothing on 1 September: 2,053,248
-- observations, effectively the whole archive, had become invisible without a
-- single row changing. In a year, every statistic about today would be empty.
--
-- Worse than the emptiness is what it does to reproducibility. A figure we
-- publish is supposed to be recomputable by anyone, from the same data, forever.
-- One that silently depends on which stations happen to be reporting on the day
-- you ask is not reproducible at all.
--
-- Liveness and eligibility are different questions. Whether a station is
-- reporting now belongs on a front page. Whether a past reading may enter a
-- statistic is answered by its quality flags and its revision, both of which
-- are properties of the reading itself and do not change with the calendar.

begin;

create or replace view consensus_eligible as
select distinct on (
         canonical_station(o.station_id, o.phenomenon_start),
         o.parameter_id, o.phenomenon_start, o.phenomenon_end)
       o.*, canonical_station(o.station_id, o.phenomenon_start) as canonical_station_id
  from observations o
 where o.revision = 1
   and o.value is not null
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
  'that carried it. Deliberately says nothing about whether the station is '
  'still reporting: that changes with the calendar, and a published figure has '
  'to stay reproducible.';

commit;
