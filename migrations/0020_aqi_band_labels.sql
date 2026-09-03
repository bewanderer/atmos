-- Names of the index categories, kept with the scale that defines them.
--
-- A different scale has different categories: the US EPA AQI runs from Good to
-- Hazardous over six bands that do not line up with these. So the names belong
-- to the scale rather than to the application, and a caller can render a band
-- without knowing which scale produced it.
--
-- The English names are the EEA's own category names, from the same source as
-- the bands. Translation is the interface's job; this is the stable key.

begin;

create table aqi_band_labels (
  scale_id smallint  not null references aqi_scales(id),
  band     smallint  not null check (band between 1 and 6),
  code     text      not null,
  name     text      not null,
  primary key (scale_id, band)
);

comment on table aqi_band_labels is
  'Category names per scale. code is the stable key for interfaces, name is '
  'the wording the publishing body uses.';

insert into aqi_band_labels (scale_id, band, code, name)
select s.id, v.band, v.code, v.name
  from aqi_scales s
  cross join (values
    (1, 'good',           'Good'),
    (2, 'fair',           'Fair'),
    (3, 'moderate',       'Moderate'),
    (4, 'poor',           'Poor'),
    (5, 'very_poor',      'Very poor'),
    (6, 'extremely_poor', 'Extremely poor')
  ) as v(band, code, name)
 where s.code = 'eaqi';

grant select on aqi_band_labels to atmos_api, atmos_ingest;

commit;
