-- Same instrument, more than one publisher.
--
-- FHMZ republishes the Tuzla Canton network verbatim: 45 of 45 readings
-- identical across every shared parameter. Counting those as two independent
-- sources agreeing would be the worst error this project can make, because it
-- manufactures confidence out of a single instrument.
--
-- Identity is time bounded, and that is not a detail. Tuzla publishes its
-- mobile unit both as `mobilna`, which follows the unit wherever it goes, and
-- under the town it is parked in. Those two agree only while the unit is in
-- that town. A permanent mapping would be right today and silently wrong the
-- moment it moves, which is worse than having none at all.

begin;

create table station_identity (
  id                   bigserial   primary key,
  station_id           bigint      not null references stations(id),
  canonical_station_id bigint      not null references stations(id),
  -- The window in which the two genuinely carry the same instrument. A moving
  -- station ends its window when it moves, so an old alias cannot leak into
  -- later data.
  valid_from           timestamptz not null,
  valid_to             timestamptz,
  evidence             text        not null,
  readings_compared    integer     not null,
  readings_identical   integer     not null,
  detected_at          timestamptz not null default now(),

  constraint identity_distinct check (station_id <> canonical_station_id),
  constraint identity_window_ordered check (valid_to is null or valid_to > valid_from)
);

create index station_identity_canonical_idx on station_identity (canonical_station_id);
create index station_identity_window_idx
  on station_identity (station_id, valid_from, valid_to);

comment on table station_identity is
  'Windows in which two station rows carry one physical instrument. Consensus '
  'counts the instrument once. No row covering a reading means the station '
  'stands alone for that reading.';


-- Which station stands for a reading, at the time of that reading. Time bounded
-- on purpose: a mobile unit duplicates one town this month and another the next.
create function canonical_station(p_station_id bigint, p_at timestamptz)
returns bigint
language sql
stable
as $$
  select coalesce(
    (select i.canonical_station_id
       from station_identity i
      where i.station_id = p_station_id
        and p_at >= i.valid_from
        and (i.valid_to is null or p_at < i.valid_to)
      order by i.valid_from desc
      limit 1),
    p_station_id
  );
$$;

comment on function canonical_station is
  'The station that represents a reading at its own timestamp, following any '
  'identity window in force then.';


-- Detection proposes, it does not conclude.
--
-- Matching values alone are NOT sufficient and this is the important part.
-- Two stations reading identically could be one instrument published twice, or
-- two instruments that genuinely agree. Only the name the source gives each
-- page separates those. Tuzla labels both `mobilna` and `mobilna-kalesija`
-- "Mobilna: Kalesija" while the unit is there, and labels `mobilna-banovici`
-- "Mobilna: Banovici", which is a different campaign that must never be merged.
--
-- So a pair must agree on BOTH the label and the readings, over a window that
-- is recorded with the claim.
create function detect_duplicate_stations(
  p_min_readings integer default 20,
  p_min_ratio    numeric default 0.99
) returns integer
language plpgsql
as $$
declare
  found integer;
begin
  with pairs as (
    select a.station_id as left_id,
           b.station_id as right_id,
           count(*) as compared,
           count(*) filter (where a.value = b.value and a.unit = b.unit) as identical,
           min(a.phenomenon_start) as seen_from,
           max(a.phenomenon_start) as seen_to
      from observations a
      join stations sa on sa.id = a.station_id
      join observations b
        on b.parameter_id = a.parameter_id
       and b.phenomenon_start = a.phenomenon_start
       and b.phenomenon_end = a.phenomenon_end
       and b.revision = a.revision
       and b.station_id > a.station_id
      join stations sb on sb.id = b.station_id
     where a.revision = 1
       and a.value is not null
       and b.value is not null
       -- The corroboration. Without it, two genuinely agreeing instruments
       -- would be silently merged into one.
       and sa.name = sb.name
     group by a.station_id, b.station_id
  ),
  duplicates as (
    select least(left_id, right_id) as canonical_id,
           greatest(left_id, right_id) as alias_id,
           compared, identical, seen_from, seen_to
      from pairs
     where compared >= p_min_readings
       and identical::numeric / compared >= p_min_ratio
  ),
  ins as (
    insert into station_identity
      (station_id, canonical_station_id, valid_from, valid_to, evidence,
       readings_compared, readings_identical)
    select d.alias_id,
           d.canonical_id,
           d.seen_from,
           -- Bounded by what was observed rather than left open. An open window
           -- would claim the pair stays identical after a mobile unit moves.
           d.seen_to + interval '1 hour',
           'same station label, and identical readings across ' || d.compared
             || ' comparable hours, ' || d.seen_from::date
             || ' to ' || d.seen_to::date,
           d.compared,
           d.identical
      from duplicates d
     where not exists (
       select 1 from station_identity e
        where e.station_id = d.alias_id
          and e.canonical_station_id = d.canonical_id
          and e.valid_from = d.seen_from
     )
    returning 1
  )
  select count(*) into found from ins;
  return found;
end
$$;

comment on function detect_duplicate_stations is
  'Proposes identity where readings match too exactly to be separate '
  'instruments, bounded by the window observed. Evidence based, so it can be '
  're-run and argued with.';


-- Consensus input, now counting each instrument once.
drop view consensus_eligible;

create view consensus_eligible as
select o.*, canonical_station(o.station_id, o.phenomenon_start) as canonical_station_id
  from observations o
  join station_status ss
    on ss.station_id = o.station_id
   and ss.parameter_id = o.parameter_id
 where o.revision = 1
   and o.value is not null
   and ss.status in ('active', 'delayed')
   -- One vote per instrument. Where an instrument is published twice, the
   -- duplicate does not get a second one.
   and o.station_id = canonical_station(o.station_id, o.phenomenon_start)
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
  'Default consensus input. A filter, not a deletion. Counts each physical '
  'instrument once, however many publishers carry it.';

grant select on station_identity, consensus_eligible to atmos_api;
grant select on consensus_eligible to atmos_ingest;
grant select, insert, update on station_identity to atmos_ingest;
grant usage, select on sequence station_identity_id_seq to atmos_ingest;
grant execute on function canonical_station to atmos_api, atmos_ingest;
grant execute on function detect_duplicate_stations to atmos_ingest;

commit;
