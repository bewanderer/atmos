-- Index for the query the whole comparison rests on.
--
-- Consensus asks "every station reporting this parameter in this window". The
-- canonical index leads with station_id, so it cannot answer that: the planner
-- fell back to the primary key and filtered on parameter afterwards. Fine at a
-- few thousand rows, wrong at ten million a year.

begin;

create index observations_consensus_idx on observations
  (parameter_id, phenomenon_start, station_id)
  where revision = 1 and value is not null;

comment on index observations_consensus_idx is
  'Serves consensus: all stations reporting one parameter over a window. '
  'Partial, because consensus only ever reads canonical rows that hold a value.';

commit;
