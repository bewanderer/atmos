-- Support for instantaneous readings and PM1.
--
-- Reference stations publish hourly averages, so start and end differ. Low cost
-- sensors report a value at a moment. Giving those a made up window would imply
-- an averaging that did not happen, so an equal start and end now means an
-- instant.

begin;

alter table observations drop constraint obs_window_ordered;
alter table observations add constraint obs_window_ordered
  check (phenomenon_end >= phenomenon_start);

comment on column observations.phenomenon_end is
  'Equal to phenomenon_start for an instantaneous reading. Later for an average '
  'over a period, such as an hourly mean.';

insert into parameters (code, canonical_unit, description) values
  ('pm1', 'ug/m3', 'Particulate matter under 1 micrometre')
on conflict (code) do nothing;

commit;
