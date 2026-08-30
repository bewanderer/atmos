-- Who runs the instrument, as distinct from who publishes it.
--
-- FHMZ publishes 33 stations and operates a minority of them. Its own 2024
-- annual report names the operator per station: the Metalurski institut in
-- Zenica, the Sarajevo public health institute, the municipalities of Vares and
-- Kakanj, and the Tuzla cantonal ministry.
--
-- Attribution has to name the body that took the measurement, not only the
-- website it appeared on. It also matters for independence: two stations run by
-- the same operator are less independent than two run by different ones, even
-- when published separately.

begin;

alter table stations add column operator text;

comment on column stations.operator is
  'The body running the instrument, where the source discloses it. Often not '
  'the publisher. Null when unknown, never guessed.';

commit;
