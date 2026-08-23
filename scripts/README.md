# scripts/

Host-side launchers and operational tooling. **None of this is the pipeline** —
the pipeline is `src/`. Everything here exists to make `src/` run unattended on
one specific Windows laptop, and all of it retires at the VPS cutover described
in `03_Local_Cache/VPS_Migration.md`.

| File | What it does | After cutover |
| --- | --- | --- |
| `poll_loop.py` | 15-minute loop, single-instance via a named mutex, cold-starts Postgres | Retires — cron + `flock` replace both mechanisms |
| `poll_supervisor.py` | Restarts the loop when the heartbeat goes stale | Retires — **and must be stopped FIRST**, or it restarts the loop you just stopped |
| `poll_waits.py` | One-shot poll wrapper with a spacing guard | Retires — cron is the cadence |
| `backup_db.py` | Nightly `pg_dump` to OneDrive, 14-day retention | Retires — the VPS runs `pg_dump` + `rclone` from cron |
| `run_poll.cmd` / `run_poll.ps1` | Manual invocation for a human at a terminal | Retires |

## Why these were untracked until now

They lived only at `%LOCALAPPDATA%\AussieHealth\`, outside OneDrive and outside
every backup. **The script that makes the backups was itself unbacked** — a
disk failure would have taken the backup mechanism along with the thing it
protects. There is nothing sensitive in them: `backup_db.py` reads
`%APPDATA%\postgresql\pgpass.conf` rather than embedding a credential.

## Known defects, recorded rather than silently carried

**`backup_db.py:42` swallows `OSError` in `log()`** — the identical bug PR #24
fixed in `poll_loop.py`. Its comment ("a logger must never be the reason a
backup fails") is correct in intent and wrong in effect: a failed log write
makes a *running* backup indistinguishable from one that never started. Worth
porting #24's fix — a sentinel written to a different directory, plus a count
reported on recovery.

**These scripts assume absolute paths under `C:\Users\Xi\`.** Deliberate — they
are machine-local by definition — but it means they are documentation on any
other machine, not runnable code.

**`backup_db.py` has not run automatically since 05/08/2026.** Its Startup
shortcut launches `pythonw.exe`, which is the exact non-interactive launch
pattern that freezes on this machine (see `machine-env` memory and
`03_Local_Cache/Local_Deployment.md`). Every dump newer than that date was
taken by hand. **Until cutover, backups are a manual step**, and the check is
that `backup_db.log` grew — not that the command appeared to succeed.

## Not committed

`probe.py` — a 176-byte diagnostic one-liner with a hardcoded output path,
written during the process-freeze investigation to test whether a launched
process could write a file at all. It answered its question and has no ongoing
use. Its method is recorded in the freeze findings; the file is not worth
carrying.
