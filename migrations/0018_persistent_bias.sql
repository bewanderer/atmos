-- Persistent bias, which is the only thing a divergence claim can rest on.
--
-- A single divergent hour is noise. Instrument faults, calibration events,
-- local transients and genuine short pollution episodes all produce isolated
-- outliers, so one hour never supports a statement about a source.
--
-- What survives scrutiny is a pattern: how far a station sits from the
-- consensus of its own neighbourhood, over how many comparisons, over what
-- window, and how consistently on the same side.
--
-- Only comparisons where consensus means something are counted. At n = 2 a
-- difference is measurable but attribution is not, so those are excluded rather
-- than quietly folded in.

begin;

create or replace function persistent_bias(
  p_parameter text,
  p_from      timestamptz,
  p_to        timestamptz,
  p_radius_m  numeric default 5000,
  p_sources   text[] default null,
  p_bucket    interval default '1 hour'
)
returns table (
  station_id            bigint,
  comparisons           integer,
  first_comparison      timestamptz,
  last_comparison       timestamptz,
  median_deviation      numeric,
  median_relative       numeric,
  direction_consistency numeric,
  flag_rate             numeric
)
language sql
stable
as $$
  with d as (
    select *
      from divergence(p_parameter, p_from, p_to, p_radius_m, p_sources, p_bucket)
     where n >= 3
  )
  select d.station_id,
         count(*)::int,
         min(d.bucket_start),
         max(d.bucket_start),
         median_numeric(array_agg(d.deviation)),
         -- A derived ratio, not a measurement, so it is rounded like the
         -- other derived columns rather than carrying division scale.
         trim_scale(round(median_numeric(array_agg(
           case when d.median <> 0 then d.deviation / d.median end)), 4)),
         -- How one sided it is. 0.5 is a coin toss, 1.0 is always the same way.
         trim_scale(round(
           greatest(
             count(*) filter (where d.deviation > 0),
             count(*) filter (where d.deviation < 0)
           )::numeric / nullif(count(*) filter (where d.deviation <> 0), 0),
           4)),
         trim_scale(round(
           count(*) filter (where abs(d.modified_z) > 3.5)::numeric
             / nullif(count(*) filter (where d.modified_z is not null), 0),
           4))
    from d
   group by d.station_id;
$$;

comment on function persistent_bias is
  'Signed deviation from neighbourhood consensus over a window. Counts only '
  'comparisons with three or more instruments, since two can show a difference '
  'but cannot attribute it. Report the comparison count and window with any '
  'figure taken from this.';

grant execute on function persistent_bias to atmos_api, atmos_ingest;

commit;
