-- Identity is proposed automatically and confirmed by a person.
--
-- Detection can be trusted within one source, where the publisher names the
-- same station the same way: Tuzla labels both `mobilna` and `mobilna-kalesija`
-- "Mobilna: Kalesija", so identical readings plus an identical label settle it.
--
-- Across sources it cannot. FHMZ and Tuzla publish five of the same stations
-- with 100 percent identical readings under different names:
--
--     Tuzla Bukinje   / Bukinje       270 of 270 identical
--     Lukavac Centar  / Lukavac       225 of 225
--     Tuzla BKC       / BKC           225 of 225
--     Tuzla Skver     / Skver         225 of 225
--     Zivinice Centar / Zivinice      129 of 129
--
-- Any name rule loose enough to catch those is loose enough to merge two
-- genuinely different stations, which would delete a real disagreement. So
-- cross source pairs are proposed with their evidence and wait for review.
-- Only confirmed identities collapse in consensus.

begin;

alter table station_identity
  add column status text not null default 'proposed',
  add column confirmed_by text,
  add column confirmed_at timestamptz,
  add column note text;

alter table station_identity add constraint identity_status_valid
  check (status in ('proposed', 'confirmed', 'rejected'));

comment on column station_identity.status is
  'proposed: detected, not yet reviewed, and NOT applied to consensus. '
  'confirmed: reviewed and applied. rejected: reviewed and found to be two '
  'genuinely different stations, kept so it is not proposed again.';


-- Only a confirmed identity collapses a reading. A proposal changes nothing
-- until someone has looked at it.
create or replace function canonical_station(p_station_id bigint, p_at timestamptz)
returns bigint
language sql
stable
as $$
  select coalesce(
    (select i.canonical_station_id
       from station_identity i
      where i.station_id = p_station_id
        and i.status = 'confirmed'
        and p_at >= i.valid_from
        and (i.valid_to is null or p_at < i.valid_to)
      order by i.valid_from desc
      limit 1),
    p_station_id
  );
$$;


-- Propose pairs whose readings match too exactly to be independent, regardless
-- of naming. Nothing is applied; this fills the review queue.
create function propose_duplicate_stations(
  p_min_readings integer default 20,
  p_min_ratio    numeric default 0.99
) returns integer
language plpgsql
as $$
declare
  found integer;
begin
  with pairs as (
    select a.station_id as left_id, b.station_id as right_id,
           sa.name as left_name, sb.name as right_name,
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
     where a.revision = 1 and a.value is not null and b.value is not null
     group by a.station_id, b.station_id, sa.name, sb.name
  ),
  candidates as (
    select least(left_id, right_id) as canonical_id,
           greatest(left_id, right_id) as alias_id,
           left_name, right_name, compared, identical, seen_from, seen_to,
           (left_name = right_name) as names_agree
      from pairs
     where compared >= p_min_readings
       and identical::numeric / compared >= p_min_ratio
  ),
  ins as (
    insert into station_identity
      (station_id, canonical_station_id, valid_from, valid_to, evidence,
       readings_compared, readings_identical, status, confirmed_by, confirmed_at)
    select c.alias_id, c.canonical_id, c.seen_from, c.seen_to + interval '1 hour',
           case when c.names_agree
                then 'same station label and ' || c.identical || ' of '
                     || c.compared || ' readings identical, '
                     || c.seen_from::date || ' to ' || c.seen_to::date
                else 'readings identical (' || c.identical || ' of ' || c.compared
                     || ') but the sources name it differently: '
                     || c.left_name || ' / ' || c.right_name
                end,
           c.compared, c.identical,
           -- A matching label within one publisher is enough. A different label
           -- means a person decides, because a wrong merge deletes a real
           -- disagreement and nothing would announce it.
           case when c.names_agree then 'confirmed' else 'proposed' end,
           case when c.names_agree then 'detector' end,
           case when c.names_agree then now() end
      from candidates c
     where not exists (
       select 1 from station_identity e
        where e.station_id = c.alias_id
          and e.canonical_station_id = c.canonical_id
          and e.valid_from = c.seen_from
     )
    returning 1
  )
  select count(*) into found from ins;
  return found;
end
$$;

comment on function propose_duplicate_stations is
  'Fills the identity review queue. Auto-confirms only where one publisher '
  'gives both pages the same label; anything else waits for a person.';

drop function detect_duplicate_stations(integer, numeric);

grant execute on function propose_duplicate_stations to atmos_ingest;

commit;
