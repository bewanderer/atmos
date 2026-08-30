-- The source published the same reading twice, with two different values.
--
-- Sensor.Community's daily archive does this: one location, one parameter, one
-- timestamp, two rows, two values. It is not a revision, because nothing was
-- changed between publications. The payload disagrees with itself.
--
-- Left unhandled it manufactured revisions. Ingest kept whichever row landed
-- first, the next ingest compared the other one against it, recorded a
-- value_change that never happened, and the two took turns forever. Fabricating
-- evidence that a source altered its data is the worst failure this project has,
-- since detecting real alterations is the point of the ledger.
--
-- We keep the first value, which is the same rule the ledger uses everywhere,
-- and mark it so the disagreement stays visible instead of being quietly
-- resolved. It does not exclude the reading from consensus: the value is real,
-- and it is for the reader to decide what to do about the ambiguity.

begin;

insert into quality_flags (code, description, excludes_from_consensus)
values (
  'source_duplicate',
  'Source published more than one value for this reading in one payload',
  false
)
on conflict (code) do nothing;

commit;
