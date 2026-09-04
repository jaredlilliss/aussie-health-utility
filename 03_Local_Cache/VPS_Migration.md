---
aliases: [VPS Migration, Always-on Host]
tags: [deployment, etl, storage]
created: 2026-08-03
status: active
up: "[[Local_Deployment]]"
---

# VPS migration (always-on poller host)

Why this exists: the laptop is a personal machine on Modern Standby and measures **15–28% daily coverage** no matter which launcher runs the poller (evidence and post-mortem in [[Local_Deployment]]). The pipeline itself is portable — Python, Postgres, the three pins in `src/requirements.txt`, and a **140-test** offline suite — so the fix is a small always-on host, not more scheduler work. This is the checklist for that move, written so it can be executed in one sitting.

**One thing does not travel with the repo:** `MBS-XML-20260701.XML` (8.3 MB) is the input for the MBS leg and is **not in git** — `fixtures/` carries only the 3 KB `mbs_release_sample.xml`. It lives solely at `%LOCALAPPDATA%\AussieHealth\` on the laptop and is in no backup Jared holds. It is re-downloadable from MBS Online, so it is not *lost*, but after cutover the MBS leg has neither its dependency nor its input unless it is copied across or re-fetched. Plan for it rather than discovering it.

## Decision to make FIRST: the existing snapshot series

The [[Local_Deployment]] line "the cache is rebuildable at any time" is true for `facilities` (re-fetchable from Data.NSW) and the MBS load (re-parseable from the XML) — but **not for `ed_wait_snapshots`**. The RTED API serves *current* state only; the rows collected since 20/07 are the only copy of that history, and every hour before cutover adds permanent holes only to the laptop's copy. **Count as of 07/08/2026: 14,750 rows** across 59 facilities, growing roughly 240/day while the laptop is awake. Any figure written here is stale within hours — **run `select count(*) from ed_wait_snapshots` rather than trusting this line**, and never `pg_stat_user_tables.n_live_tup`, which is an estimate and reported one table here as 0 when it held 6,045 rows. (Previous values recorded in this doc: ~5,900, then 7,434 on 04/08 — both already wrong when read.) **It is no longer the *only* copy:** restore-verified dumps now live in the laptop's backup folder (see [[Local_Deployment]]), so the migration starts from a backup rather than a single live cluster.

- **Migrate the series (recommended):** one `pg_dump` on the laptop, one restore on the VPS. Ten minutes, keeps the history contiguous.
- **Start fresh:** VPS begins empty; laptop copy becomes a dead archive. Acceptable only if the 20/07–cutover history is genuinely disposable.

Either way, cut over sooner rather than later — delay is measured in unrecoverable gaps.

## Provider

Any $5-ish/month Linux VPS with a Sydney region works; an AU IP is also the polite look for a `*.nsw.gov.au` poller. Candidates: **BinaryLane** (Australian, ~AU$4), **Vultr Sydney**, **DigitalOcean Sydney**. Hetzner is cheapest but nearest region is Singapore. Spec floor is tiny: 1 vCPU, 1 GB RAM, 25 GB disk (the whole dataset is megabytes). Ubuntu 24.04 LTS assumed below.

## Build checklist

Run steps 1–4 as root. **Everything from step 5 onward runs as `poller`** (`sudo -u poller -i`) — if the clone lands in `/root`, the cron lines in steps 8 and 11 point at paths that do not exist.

1. **Provision** — smallest instance, Sydney region, SSH key auth only (no password login).
2. **Create the `poller` user and its directories.** Nothing later creates these, and the omission fails *silently*: the nightly backup cron dies with "could not open output file" every night and nobody notices for weeks.
   ```bash
   adduser --disabled-password --gecos "" poller
   install -d -o poller -g poller /home/poller/backups
   ```
3. **Harden (10 min, once):**
   ```bash
   apt update && apt -y upgrade
   apt -y install ufw unattended-upgrades
   ufw allow OpenSSH && ufw enable
   dpkg-reconfigure -plow unattended-upgrades
   ```
4. **Install runtime:**
   ```bash
   apt -y install python3 python3-pip python3-venv postgresql git
   ```
   Postgres runs as a real service here — the whole "start it if it's down" dance from the laptop is simply not needed.
5. **Database role and database** — new strong password. **Do NOT reuse the laptop's local-dev password.** It was committed to this repo in five places until 12/08/2026 and, although removed from HEAD, remains in git history on `origin` permanently. (The repo is *private*, but that is not the reason to rotate — a private repo does not make a published credential safe, and "it's private so the credential is fine" is exactly the wrong reading.) Postgres binds loopback-only by default; leave it that way.
   ```bash
   sudo -u postgres psql -c "create user rted password '<new-strong-password>';"
   sudo -u postgres psql -c "create database aussie_health owner rted;"
   ```
   **The schema load is deliberately not here** — `db/init/01_schema.sql` does not exist on this box until step 7 clones it. Load it as the **same role that polls**, or every insert later dies with `permission denied for table`.
6. **Repo access — use a deploy key, not a PAT.** The repo is **private**, so the anonymous `git clone` this checklist used to open with simply prompts and fails on a fresh box. The reflex fix is to paste a personal access token, which puts `repo` + `workflow` + `gist` scope over the *entire account* onto a $5 machine. A deploy key is scoped to this one repo, revocable from a web page, and needs no custody plan:
   ```bash
   sudo -u poller ssh-keygen -t ed25519 -N "" -f /home/poller/.ssh/id_ed25519
   cat /home/poller/.ssh/id_ed25519.pub    # paste into repo Settings -> Deploy keys
   ```
   Leave **"Allow write access" unchecked** — the poller only ever reads. Clone over SSH (`git@github.com:...`), not HTTPS.
7. **Deploy code, then load the schema** (in this order — the schema file arrives with the repo):
   ```bash
   git clone git@github.com:jaredlilliss/Aussie_Health_Docs_v2.git
   cd Aussie_Health_Docs_v2
   python3 -m venv .venv && .venv/bin/pip install -r src/requirements.txt
   .venv/bin/python -m unittest discover -s tests -t .
   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 --single-transaction -f db/init/01_schema.sql
   ```
   **Expect `Ran 147 tests ... OK (skipped=28)` — i.e. 119 actually run.** 28 skips on Linux is *correct, not broken*: `tests/test_poll_loop.py` and `tests/test_poll_supervisor.py` gate seven classes behind `sys.platform == "win32"` because those modules are Windows-only (`ctypes.wintypes`, `kernel32`). A green run with zero skips on Linux would mean the gating broke. (147 as of 07/08/2026 — the suite grows; treat the number as indicative and the *skip* count as the thing to sanity-check.)

   Install from `src/requirements.txt`, not a hand-copied pin list: it also carries **`defusedxml>=0.7`**, a hard dependency since PR #14 — `src/ingest_mbs_xml.py` does `from defusedxml.ElementTree import iterparse` with no fallback, so the MBS leg fails at import without it.

   The suite is **`unittest`, not pytest** — pytest is not a dependency, and `python -m pytest` returns "No module named pytest", which reads like a broken suite.

   `-v ON_ERROR_STOP=1 --single-transaction` on the schema load is not optional: by default `psql -f` **continues past errors and still exits 0**, leaving a half-built schema that reads as success.
8. **Migrate the series** (if chosen above). Include `mbs_items` — it carries the 6,045 parsed rows across in the same operation and removes the 8.3 MB MBS XML from the cutover path entirely.
   ```bash
   # laptop
   pg_dump -h 127.0.0.1 -U rted -d aussie_health --data-only \
     -t facilities -t ed_wait_snapshots -t sync_runs -t mbs_items -f dump.sql
   # VPS, after copying dump.sql across
   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 --single-transaction -f dump.sql
   ```
   Plain SQL here, restored with `psql`. **Note the format changes in step 11** — the nightly backup is `-Fc` (custom), which restores with `pg_restore`, not `psql`. Two different dumps, two different restore commands; do not mix them.
9. **Cron, not a loop.** `scripts/poll_loop.py` and `scripts/poll_waits.py` are Windows launcher shims (`LOCALAPPDATA`, `pg_ctl`, `CREATE_NO_WINDOW`) — they retire; cron replaces both. Crontab for the `poller` user:
   ```cron
   */15 * * * * cd /home/poller/Aussie_Health_Docs_v2 && flock -n /tmp/ah-poll.lock .venv/bin/python src/ingest_ed_waits.py --waits >> /home/poller/poll.log 2>&1
   ```
   `flock -n` gives the single-instance guarantee the Windows mutex used to; cron itself is the cadence, satisfying the 15-min policy floor in [[NSW_Health_JSON_Engine]]. Put `DATABASE_URL` in the crontab itself (`poller`-owned, mode 0600) — **not `/etc/environment`, which is world-readable 0644**, so every account on the box could read the password. Never commit it either way.
10. **Verify** — after two cycles: `select id, ok, detail from sync_runs order by id desc limit 4;` should show fresh `status=ok` rows ~15 min apart. `where not ok` is the health query, exactly as on the laptop.
11. **Backups — do not skip this step.** Without it the migration is a *downgrade*: today the laptop's series has restore-verified dumps in its backup folder, and a VPS with no backup would make a $5 instance the sole copy of the one table that cannot be rebuilt. Nightly dump plus an off-box copy:
    ```cron
    17 3 * * * pg_dump "$DATABASE_URL" -Fc -f /home/poller/backups/aussie_health-$(date +\%Y\%m\%d).dump && find /home/poller/backups -name '*.dump' -mtime +14 -delete
    ```
    **A dump that has never been restored is not a backup** — verify once, the way the laptop's were:
    ```bash
    sudo -u postgres psql -c "create database scratch owner rted;"
    pg_restore -d "postgresql://rted:<pw>@127.0.0.1/scratch" --no-owner --no-privileges \
      /home/poller/backups/aussie_health-YYYYMMDD.dump
    # compare, then drop
    psql "postgresql://rted:<pw>@127.0.0.1/scratch" -c "select count(*) from ed_wait_snapshots;"
    sudo -u postgres psql -c "drop database scratch;"
    ```
    `pg_restore`, because this dump is `-Fc` — `psql -f` will not read it. Off-box copy matters as much as the dump: a provider-side snapshot, or `rsync`/`rclone` to somewhere that is not this VPS. Losing the instance must not lose the history.
12. **Log rotation** — one file in `/etc/logrotate.d/` for `poll.log` (weekly, keep 4).

    **`sync_runs` does not prune itself.** The claim that it did was wrong: `PRUNE_SNAPSHOTS_SQL` in `src/ed_waits/db.py` deletes from `ed_wait_snapshots` **only**, and there is no `DELETE` against `sync_runs` anywhere in `src/`. It grows ~96 rows/day, ~35k/year — harmless at this scale and safe to leave, but it is an unbounded table, not a managed one. If it ever matters: `delete from sync_runs where started_at < now() - interval '90 days';` on the same nightly cron.
13. **MBS leg — write down its home now.** It has no cron entry and no procedure, and `src/ingest_mbs_xml.py` expects an XML path that will not exist on the VPS. It is a *quarterly manual* job (MBS publishes around March, July and November), so it does not block cutover — but decide and record it here rather than rediscovering it in October: fetch the release from MBS Online onto the VPS, then `.venv/bin/python src/ingest_mbs_xml.py --xml-path <file> --source-release YYYY-MM-DD`. Migrating `mbs_items` in step 8 means the table is populated meanwhile.
14. **SSH key custody.** Step 1 mandates key-only auth, which is right — but an always-on host reachable by exactly one key on exactly one laptop has simply *relocated* the single point of failure. Record where the private key lives, and keep a second copy somewhere the laptop dying does not take with it (password manager, or a printed/offline copy). Also add a second authorised key, or the provider's console-recovery path, before the laptop is retired from the picture. Note this is the *login* key — the step 6 deploy key is separate, repo-scoped, and revocable without touching host access.

## Monitoring

`sync_runs` is the source of truth (`ok=false` rows name their cause since PR #10). Cheapest real alerting: a free Healthchecks.io check pinged by a `&& curl -fsS <url>` appended to the cron line — silence for >30 min pages Jared's phone. The laptop's SessionStart hook can later point its coverage report at the VPS instead of the local heartbeat file; out of scope here.

## What retires on the laptop

Once the VPS shows a day of `status=ok` rows. **There are four Startup shortcuts, not one** — enumerated from `shell:startup` on 07/08/2026. Missing any of them leaves the laptop doing something unwanted:

| Shortcut | Why it must go |
|---|---|
| `AussieHealthWaitsPoll.lnk` | the loop itself |
| `AussieHealthPollSupervisor.lnk` | **restarts the loop whenever the heartbeat goes stale** — leave it and the laptop resurrects its own poller against `nsw.gov.au` indefinitely, duplicating a feed the VPS is already polling |
| `AussieHealthPollLoopViaCmd.lnk` | a launcher experiment, still armed |
| `AussieHealthDbBackup.lnk` | keeps dumping the *frozen* laptop database into the laptop's backup folder with fresh timestamps — dumps whose filenames lie about how current their contents are, which is worse than no dumps at all |

**Retire the supervisor first**, or it will restart the loop you just stopped.

Also: the `AussieHealthWaitsPoll` scheduled task (currently a cmd.exe wake-nudge), `run_poll.cmd`, the running loop and supervisor processes, and the DC wake-timer tweak (cosmetic to leave, tidy to revert). Local Postgres can stay for dev — it just stops being load-bearing. The conduct-policy UA with the real contact address carries over unchanged.

## Rollback

The laptop stack is left intact until the VPS proves out, so rollback is: relaunch the Startup shortcut, done. After retirement, rollback is re-running [[Local_Deployment]]'s existing steps — nothing is deleted, only stopped.

## Cost & ledger

~AU$4–8/month. Update the [[Local_Deployment]] status ledger when executed; nothing in this vault claims a run that did not happen.
