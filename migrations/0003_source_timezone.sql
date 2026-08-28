-- Timezone handling.
--
-- Observations are stored as timestamptz, which Postgres keeps in UTC. Display
-- is always Europe/Sarajevo, whatever the source used. This column records what
-- the source published in, so the interface can show the original alongside.

begin;

alter table sources
  add column timezone text not null default 'Europe/Sarajevo';

comment on column sources.timezone is
  'IANA name of the timezone the source publishes in. Used to show the original '
  'time next to the local one, and to catch a source changing convention.';

-- Sources that publish in local time. Others get set when their connector lands.
update sources set timezone = 'Europe/Sarajevo' where slug in ('fhmz', 'tuzla');

commit;
