# Running Atmos on a server

One small machine. Hetzner CX22 class, roughly 5 EUR a month, running Postgres,
the collector and the API. GitHub Actions keeps collecting independently, so the
archive still fills if this box is down.

Nothing here has been deployed yet. It is written so that deploying is a
followed procedure rather than a remembered one.

---

## What runs, and how often

| Unit | Schedule | Why |
|---|---|---|
| `atmos-sync-fast.timer` | every 20 minutes | RHMZ RS keeps one hour. A missed window is gone for everyone. |
| `atmos-sync.timer` | every 3 hours | FHMZ, Tuzla and Sensor.Community return days per fetch. |

Timers, not cron, for two reasons. `Persistent=true` runs a missed job after a
reboot instead of skipping it, and `RandomizedDelaySec` stops every deployment
of this project hitting the same source in the same second.

**Collection is the half that cannot be recovered.** Loading into Postgres can
be redone from the archive at any time, so a failed load is an inconvenience and
a failed fetch is a hole in the record. `atmos sync` reflects that: one source
failing never stops the others.

---

## First run

```bash
# 1. Postgres and the API
docker compose up -d

# 2. Schema. Migrations are ordered and each is applied once.
for f in migrations/0*.sql; do
  docker compose exec -T db psql -U postgres -d atmos -v ON_ERROR_STOP=1 -q < "$f"
done

# 3. Timers
sudo cp deploy/systemd/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now atmos-sync.timer atmos-sync-fast.timer

# 4. Check
systemctl list-timers 'atmos-*'
journalctl -u atmos-sync -n 50
```

## Secrets

`ATMOS_DATABASE_URL` and the Postgres password come from `/etc/atmos/atmos.env`,
readable only by root. It is not in the repository and never should be.

```
POSTGRES_PASSWORD=...
ATMOS_DATABASE_URL=postgresql://atmos_ingest:...@localhost:5432/atmos
ATMOS_API_DATABASE_URL=postgresql://atmos_api:...@localhost:5432/atmos
```

## Backups

The archive lives off this box already, in GitHub Release assets, and that is
the record. Postgres is rebuildable from it, which is why a lost database is a
delay rather than a loss.

Nightly dumps still go to object storage, because rebuilding two million rows
from the archive takes hours and restoring a dump takes minutes. **A restore
that has never been tested is not a backup**, so the restore is exercised on a
schedule rather than assumed.
