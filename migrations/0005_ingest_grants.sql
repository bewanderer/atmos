-- Metadata tables are mutable, observations are not.
-- Ingest needs to update a station name or a source attribution when it changes.

begin;

grant update on sources, stations to atmos_ingest;
grant usage, select on all sequences in schema public to atmos_ingest;

commit;
