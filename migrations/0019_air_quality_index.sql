-- The European Air Quality Index, as published by the EEA.
--
-- Bands are data, not constants in code, and they are versioned. The EAQI was
-- revised in 2024 and will be revised again. An index we publish today has to
-- stay reproducible afterwards, so a revision adds a scale rather than editing
-- one.
--
-- Source of the figures below: ETC HE Report 2024/17, "EEA's revision of the
-- European air quality index bands", Table 5.2, "Final agreed updated EEA
-- European air quality index". Confirmed against the live index at
-- airindex.eea.europa.eu on 2026-09-01.
--
-- Every pollutant uses the hourly concentration. The 2017 index used a 24 hour
-- running mean for PM2.5 and PM10; the 2024 revision moved PM onto hourly values
-- to improve sensitivity to temporal changes. Anything written against the older
-- definition is wrong, which is why the revision is named here.

begin;

create table aqi_scales (
  id            smallserial primary key,
  code          text not null unique,
  name          text not null,
  revision      text not null,
  citation      text not null,
  effective_from date not null,
  verified_on   date not null
);

comment on table aqi_scales is
  'Index scales we can compute. Versioned so a published figure stays '
  'reproducible after the scale is revised.';

insert into aqi_scales (code, name, revision, citation, effective_from, verified_on)
values (
  'eaqi',
  'European Air Quality Index',
  '2024 revision',
  'ETC HE Report 2024/17, EEA revision of the European air quality index bands, '
  'Table 5.2. Confirmed against airindex.eea.europa.eu.',
  date '2024-01-01',
  date '2026-09-01'
);


create table aqi_bands (
  scale_id     smallint not null references aqi_scales(id),
  parameter_id smallint not null references parameters(id),
  band         smallint not null check (band between 1 and 6),
  upper_bound  numeric,
  primary key (scale_id, parameter_id, band)
);

comment on table aqi_bands is
  'Upper bound of each band, in the parameter canonical unit. Null upper bound '
  'is the open top band.';

comment on column aqi_bands.upper_bound is
  'Inclusive. The published table prints integer ranges such as 0-5 then 6-15, '
  'which leaves 5.5 unstated. We read them as continuous: a value is in the '
  'band whose upper bound it first falls at or below.';


-- band 1 good, 2 fair, 3 moderate, 4 poor, 5 very poor, 6 extremely poor
insert into aqi_bands (scale_id, parameter_id, band, upper_bound)
select s.id, p.id, v.band, v.upper_bound
  from aqi_scales s
  cross join (values
    ('pm25', 1, 5),    ('pm25', 2, 15),  ('pm25', 3, 50),
    ('pm25', 4, 90),   ('pm25', 5, 140), ('pm25', 6, null),
    ('pm10', 1, 15),   ('pm10', 2, 45),  ('pm10', 3, 120),
    ('pm10', 4, 195),  ('pm10', 5, 270), ('pm10', 6, null),
    ('o3',   1, 60),   ('o3',   2, 100), ('o3',   3, 120),
    ('o3',   4, 160),  ('o3',   5, 180), ('o3',   6, null),
    ('no2',  1, 10),   ('no2',  2, 25),  ('no2',  3, 60),
    ('no2',  4, 100),  ('no2',  5, 150), ('no2',  6, null),
    ('so2',  1, 20),   ('so2',  2, 40),  ('so2',  3, 125),
    ('so2',  4, 190),  ('so2',  5, 275), ('so2',  6, null)
  ) as v(code, band, upper_bound)
  join parameters p on p.code = v.code
 where s.code = 'eaqi';


-- Which band one concentration falls in. No opinion about anything else.
create or replace function aqi_band(
  p_parameter text,
  p_value     numeric,
  p_scale     text default 'eaqi'
)
returns smallint
language sql
stable
as $$
  select min(b.band)
    from aqi_bands b
    join aqi_scales s on s.id = b.scale_id
    join parameters p on p.id = b.parameter_id
   where s.code = p_scale
     and p.code = p_parameter
     and p_value is not null
     and (b.upper_bound is null or p_value <= b.upper_bound);
$$;

comment on function aqi_band is
  'Band 1 good to 6 extremely poor for one concentration, or null when the '
  'parameter is not part of the scale. CO and H2S are not EAQI pollutants.';


-- The index for a station at one hour.
--
-- The worst band of any pollutant present, never an average. A pollutant we do
-- not have can only push the true value up, so a partial index is a floor rather
-- than an estimate, and it says how many pollutants it rests on.
--
-- Readings flagged as questionable are left out. A stuck analyser reading zero
-- would otherwise quietly improve a station's index, which is the wrong
-- direction to be wrong in.
--
-- Unlike the EEA we do not fill gaps with modelled forecasts. Their index
-- complements missing values from CAMS; ours is measurements only, so our figure
-- and theirs can differ for the same station and hour. That is deliberate and
-- disclosed.
create or replace function station_aqi(
  p_station_id bigint,
  p_at         timestamptz,
  p_scale      text default 'eaqi'
)
returns table (
  band            smallint,
  driver          text,
  driver_value    numeric,
  observed_at     timestamptz,
  pollutants_used smallint,
  missing         text[],
  complete        boolean
)
language sql
stable
as $$
  with scale_pollutants as (
    select distinct p.code
      from aqi_bands b
      join aqi_scales s on s.id = b.scale_id
      join parameters p on p.id = b.parameter_id
     where s.code = p_scale
  ),
  readings as (
    select p.code, o.value, o.phenomenon_start,
           aqi_band(p.code, o.value, p_scale) as band
      from observations o
      join parameters p on p.id = o.parameter_id
     where o.station_id = p_station_id
       and o.revision = 1
       and o.value is not null
       and o.phenomenon_start = date_trunc('hour', p_at)
       and p.code in (select code from scale_pollutants)
       and not exists (
         select 1 from observation_flags f
         join quality_flags qf on qf.code = f.flag
          where f.station_id = o.station_id
            and f.parameter_id = o.parameter_id
            and f.phenomenon_start = o.phenomenon_start
            and f.phenomenon_end = o.phenomenon_end
            and f.revision = o.revision
            and qf.excludes_from_consensus)
  ),
  worst as (
    select * from readings order by band desc, value desc limit 1
  )
  select w.band, w.code, w.value, w.phenomenon_start,
         (select count(*) from readings)::smallint,
         array(select code from scale_pollutants
                except select code from readings order by 1),
         (select count(*) from readings) = (select count(*) from scale_pollutants)
    from worst w
   -- Particulates drive the index nearly everywhere here. Without them there is
   -- no figure worth showing, so we show none rather than a weak one.
   where exists (select 1 from readings where code in ('pm25','pm10'));
$$;

comment on function station_aqi is
  'European Air Quality Index for one station and hour, computed from that '
  'station measurements only. Returns no row when neither PM2.5 nor PM10 is '
  'available. A partial index is a floor: the missing pollutants can only make '
  'the true value worse.';

grant select on aqi_scales, aqi_bands to atmos_api, atmos_ingest;
grant execute on function aqi_band, station_aqi to atmos_api, atmos_ingest;

commit;
