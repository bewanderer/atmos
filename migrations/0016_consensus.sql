-- Comparable sets and robust consensus statistics.
--
-- Nothing here stores a computed value as if it were a measurement. These are
-- functions, evaluated on request against whichever sources the reader has in
-- scope, so any figure we publish can be reproduced or deliberately changed by
-- excluding a source.
--
-- Everything stays numeric. percentile_cont would have been shorter but it
-- casts to double precision, and this project does not convert measurements to
-- float anywhere.

begin;

-- Exact median. percentile_disc would return a stored value rather than the
-- midpoint of two, and percentile_cont goes through float.
create or replace function median_numeric(p_vals numeric[])
returns numeric
language sql
immutable
as $$
  with n as (select count(*)::int as c from unnest(p_vals) v where v is not null)
  select case
    when (select c from n) = 0 then null
    when (select c from n) % 2 = 1 then (
      select v from unnest(p_vals) v where v is not null
       order by v offset ((select c from n) - 1) / 2 limit 1
    )
    else (
      select trim_scale(avg(v)) from (
        select v from unnest(p_vals) v where v is not null
         order by v offset (select c from n) / 2 - 1 limit 2
      ) m
    )
  end;
$$;

comment on function median_numeric is
  'Median without leaving numeric. Averages the two middle values on even n.';


-- Quartiles as medians of the halves, the Tukey definition. Split excludes the
-- middle value on odd n.
create or replace function quartile_numeric(p_vals numeric[], p_upper boolean)
returns numeric
language sql
immutable
as $$
  with s as (
    select v, row_number() over (order by v) as rn, count(*) over () as c
      from unnest(p_vals) v where v is not null
  )
  select median_numeric(array_agg(v))
    from s
   where case when p_upper then rn > (c + 1) / 2 else rn <= c / 2 end;
$$;

comment on function quartile_numeric is
  'Lower or upper quartile, as the median of that half.';


-- Which stations may be compared with which.
--
-- A set is anchored on one station and holds the stations near it, so every
-- station is scored against its own neighbourhood. Clustering into groups was
-- the alternative and it is arbitrary: the same station lands in a different
-- group depending on where you start.
--
-- Every station anchors a set containing itself, including stations we have no
-- position for. Those come out as n = 1, which reads as unconfirmed rather than
-- as the station silently disappearing from the comparison.
--
-- Rounded positions get slack. Sensor.Community rounds for privacy, so a sensor
-- reported 1.2 km away could be at 300 m or at 2 km, and treating that distance
-- as surveyed would be false precision.
create or replace function comparable_stations(
  p_radius_m numeric default 5000,
  p_slack_m  numeric default 1000
)
returns table (
  anchor_station_id bigint,
  member_station_id bigint,
  distance_m        numeric,
  slack_m           numeric
)
language sql
stable
as $$
  select a.id, a.id, 0::numeric, 0::numeric
    from stations a
  union all
  select a.id, b.id,
         round(st_distance(a.geom, b.geom)::numeric, 1),
         (case when a.location_precise then 0 else p_slack_m end)
       + (case when b.location_precise then 0 else p_slack_m end)
    from stations a
    join stations b on b.id <> a.id
   where a.geom is not null
     and b.geom is not null
     and st_dwithin(
           a.geom, b.geom,
           p_radius_m
             + (case when a.location_precise then 0 else p_slack_m end)
             + (case when b.location_precise then 0 else p_slack_m end)
         );
$$;

comment on function comparable_stations is
  'Station pairs close enough to compare, anchored per station. Slack widens '
  'the radius where a position is rounded rather than surveyed.';


-- Consensus for one parameter over a window.
--
-- p_sources is a list of source slugs, or null for all of them. Source
-- selection is the reader lever: every statistic recomputes against it.
--
-- Readings are binned to a shared bucket, one hour by default, rather than
-- matched on identical timestamps. Sources do not agree on when to measure:
-- Sensor.Community reports every two and a half minutes at arbitrary seconds
-- and FHMZ reports on the hour, so exact matching would never once put a
-- low-cost sensor in the same set as the reference station beside it. Over six
-- days of PM10 that is 29,571 distinct timestamps against 120 distinct hours.
--
-- Each instrument contributes one value per bucket, reduced by median, so a
-- sensor reporting every two minutes does not outvote an hourly station, and
-- one spike inside the hour does not carry the hour.
--
-- A reading longer than the bucket is left out rather than folded in. A 24 hour
-- mean is not an hour, and averaging it into one is a methodological error that
-- nothing downstream could detect.
--
-- Known limit: buckets align to the epoch, so hours line up but a one day
-- bucket lands on UTC midnight rather than Sarajevo midnight. Daily aggregates
-- need an origin argument before they can be trusted.
create or replace function consensus(
  p_parameter text,
  p_from      timestamptz,
  p_to        timestamptz,
  p_radius_m  numeric default 5000,
  p_sources   text[] default null,
  p_bucket    interval default '1 hour'
)
returns table (
  anchor_station_id bigint,
  bucket_start      timestamptz,
  bucket_end        timestamptz,
  n                 integer,
  median            numeric,
  mad               numeric,
  mean              numeric,
  stddev            numeric,
  min_value         numeric,
  max_value         numeric,
  q1                numeric,
  q3                numeric,
  basis             text,
  aggregate         text
)
language sql
stable
as $$
  with raw as (
    -- consensus_eligible already returns one row per instrument per reading.
    -- There is a test on that invariant.
    select o.canonical_station_id as cs,
           date_bin(p_bucket, o.phenomenon_start, timestamptz 'epoch') as b,
           o.value
      from consensus_eligible o
      join parameters p on p.id = o.parameter_id
      join stations st on st.id = o.station_id
      join sources s   on s.id = st.source_id
     where p.code = p_parameter
       and o.phenomenon_start >= p_from
       and o.phenomenon_start <  p_to
       and o.phenomenon_end - o.phenomenon_start <= p_bucket
       and (p_sources is null or s.slug = any(p_sources))
  ),
  voted as (
    select cs, b, median_numeric(array_agg(value)) as value
      from raw group by cs, b
  ),
  members as (
    select nb.anchor_station_id as anchor, v.b, v.value
      from voted v
      join comparable_stations(p_radius_m) nb on nb.member_station_id = v.cs
  ),
  base as (
    select anchor, b, array_agg(value) as vals,
           count(*)::int as n,
           median_numeric(array_agg(value)) as med
      from members group by anchor, b
  )
  select b.anchor, b.b, b.b + p_bucket,
         b.n,
         b.med,
         -- Null at n = 1: a value's deviation from itself is zero, and printing
         -- that as spread reads like sources agreeing when there is only one.
         case when b.n > 1
              then median_numeric(array(select abs(v - b.med) from unnest(b.vals) v))
         end,
         trim_scale((select avg(v) from unnest(b.vals) v)),
         trim_scale((select stddev_samp(v) from unnest(b.vals) v)),
         (select min(v) from unnest(b.vals) v),
         (select max(v) from unnest(b.vals) v),
         quartile_numeric(b.vals, false),
         quartile_numeric(b.vals, true),
         case
           when b.n = 1 then 'unconfirmed'
           when b.n = 2 then 'two_sources'
           when median_numeric(array(select abs(v - b.med) from unnest(b.vals) v)) = 0
             then 'exact_agreement'
           else 'robust'
         end,
         'median'
    from base b;
$$;

comment on function consensus is
  'Median, MAD and spread per comparable set, over aligned time buckets. Each '
  'instrument contributes one value per bucket, reduced by median. basis says '
  'what the numbers can carry: n=1 is unconfirmed, n=2 measures a difference '
  'but cannot attribute it, exact_agreement means MAD is zero.';


-- How far each station sits from the consensus of its own neighbourhood.
--
-- This is the input to persistent bias. One divergent hour is noise, so nothing
-- here is a finding on its own.
--
-- The modified z-score is only reported where it means something. At n = 2 a
-- difference is measurable but attribution is not, so it stays null rather than
-- being computed and quietly misread as blame. Where every source agrees
-- exactly, MAD is zero and the z-score is undefined, so a proportional
-- deviation is reported instead and basis says so.
create or replace function divergence(
  p_parameter text,
  p_from      timestamptz,
  p_to        timestamptz,
  p_radius_m  numeric default 5000,
  p_sources   text[] default null,
  p_bucket    interval default '1 hour'
)
returns table (
  station_id   bigint,
  bucket_start timestamptz,
  bucket_end   timestamptz,
  value        numeric,
  readings     integer,
  n            integer,
  median       numeric,
  mad          numeric,
  deviation    numeric,
  modified_z   numeric,
  proportional numeric,
  basis        text
)
language sql
stable
as $$
  with raw as (
    select o.canonical_station_id as cs,
           date_bin(p_bucket, o.phenomenon_start, timestamptz 'epoch') as b,
           o.value
      from consensus_eligible o
      join parameters p on p.id = o.parameter_id
      join stations st on st.id = o.station_id
      join sources s   on s.id = st.source_id
     where p.code = p_parameter
       and o.phenomenon_start >= p_from
       and o.phenomenon_start <  p_to
       and o.phenomenon_end - o.phenomenon_start <= p_bucket
       and (p_sources is null or s.slug = any(p_sources))
  ),
  voted as (
    select cs, b,
           median_numeric(array_agg(value)) as value,
           count(*)::int as readings
      from raw group by cs, b
  ),
  c as (
    select * from consensus(p_parameter, p_from, p_to, p_radius_m,
                            p_sources, p_bucket)
  )
  select v.cs, v.b, v.b + p_bucket, v.value, v.readings,
         c.n, c.median, c.mad,
         v.value - c.median,
         case
           when c.n >= 3 and c.mad > 0
             then round(0.6745 * (v.value - c.median) / c.mad, 4)
         end,
         case
           when c.n >= 3 and c.mad = 0 and c.median <> 0
             then round((v.value - c.median) / c.median, 4)
         end,
         c.basis
    from voted v
    join c on c.anchor_station_id = v.cs and c.bucket_start = v.b;
$$;

comment on function divergence is
  'Each station against the consensus of its own neighbourhood, per bucket. '
  'readings is how many raw values formed that station bucket, since one '
  'reading in an hour is not as well observed as twenty four.';


grant execute on function median_numeric, quartile_numeric,
                          comparable_stations, consensus, divergence
  to atmos_api, atmos_ingest;

commit;
