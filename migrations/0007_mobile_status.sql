-- A mobile station showing old data is not broken.
--
-- Tuzla Canton runs one mobile unit around the region. Each location keeps its
-- last campaign, so Celic still shows 2018. Marking those dormant would report
-- eleven failures that are not failures.

begin;

alter table station_status drop constraint station_status_valid;
alter table station_status add constraint station_status_valid
  check (status in ('active', 'delayed', 'stale', 'dormant',
                    'never_reported', 'campaign_ended'));

create or replace function refresh_station_status(
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
    select o.station_id, o.parameter_id,
           max(o.phenomenon_start) as last_at,
           count(*) as total
      from observations o
     where o.revision = 1
     group by o.station_id, o.parameter_id
  ),
  flagged as (
    select station_id, parameter_id, count(*) as flagged
      from observation_flags group by station_id, parameter_id
  ),
  expected as (
    select sp.station_id, sp.parameter_id from station_parameters sp
    union
    select ob.station_id, ob.parameter_id from observed ob
  ),
  computed as (
    select e.station_id, e.parameter_id, ob.last_at,
           coalesce(ob.total, 0) as total,
           coalesce(fl.flagged, 0) as flagged,
           case
             when ob.last_at is null then 'never_reported'
             -- A mobile unit that has moved on is finished here, not faulty.
             when s.is_mobile and now() - ob.last_at > p_stale_after
               then 'campaign_ended'
             when now() - ob.last_at > p_dormant_after then 'dormant'
             when now() - ob.last_at > p_stale_after   then 'stale'
             when now() - ob.last_at > p_delayed_after then 'delayed'
             else 'active'
           end as status
      from expected e
      join stations s on s.id = e.station_id
      left join observed ob
        on ob.station_id = e.station_id and ob.parameter_id = e.parameter_id
      left join flagged fl
        on fl.station_id = e.station_id and fl.parameter_id = e.parameter_id
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

commit;
