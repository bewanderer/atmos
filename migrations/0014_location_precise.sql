-- Whether a station's coordinates are the real site or an approximation.
--
-- Sensor.Community rounds coordinates for privacy unless the owner opts out, so
-- a community sensor can sit up to about a kilometre from where it says it is.
-- Comparable sets match stations by distance, and matching on a rounded position
-- as though it were surveyed would quietly overstate how close two stations are.
--
-- Also the honest way to record a position we inferred rather than read, such as
-- a mobile unit known only by the municipality it was working in.

begin;

alter table stations
  add column location_precise boolean not null default true;

comment on column stations.location_precise is
  'False when the coordinates are rounded or inferred rather than the surveyed '
  'site. Distance matching has to account for it.';

commit;
