---
aliases: [Runbook, Local Stack, Deployment]
tags: [storage, deployment, docker, etl]
created: 2026-06-12
status: active
up: "[[System_Overview]]"
---

# Local Deployment (cache + ETL runbook)

How to stand up the local cache for [[System_Overview]] and run the first pipeline leg. Schema contract: [[Postgres_Cache_Schema]], mirrored in `db/init/01_schema.sql` (keep the two in sync). Pipeline context: [[NSW_Health_JSON_Engine]].

## Prerequisites

- Docker Desktop (Windows: WSL2 backend) with Compose v2
- Python 3.11+

## Stand up and load (four commands)

```
cp .env.example .env        # then set POSTGRES_PASSWORD to anything you like
docker compose up -d
pip install -r src/requirements.txt
python src/ingest_ed_waits.py
```

**The `.env` step is not optional.** Since 12/08/2026 `docker-compose.yml` takes `POSTGRES_PASSWORD` from the environment with a `${VAR:?message}` guard, so `docker compose up -d` fails with an instruction if it is unset — deliberately, rather than starting on a password that used to be committed. `.env` is gitignored; `.env.example` is not.

First boot of an empty volume auto-runs everything in `db/init/`. The ingest fetches the live Data.NSW facilities JSON (266 records), UPSERTs into `facilities`, and prints counts plus sample rows from the database.

## Useful variants

```
python src/ingest_ed_waits.py --dry-run                # transform + print, no DB needed
python src/ingest_ed_waits.py --from-file fixtures/ckan_datastore_search_sample.json --dry-run
python src/ingest_ed_waits.py --waits                  # live wait counts, whole state in one call
python src/ingest_ed_waits.py --waits --from-file fixtures/wait_payload_20260719T094206Z.json --dry-run
python src/ingest_ed_waits.py --wait-endpoint "<url>"  # capture a raw wait payload to fixtures/
python src/ingest_mbs_xml.py --dry-run                 # MBS mock parse, no DB needed
python src/ingest_mbs_xml.py --xml-path "MBS-XML-20260301-version 2.XML" --source-release 2026-03-01
docker compose down -v && docker compose up -d         # nuke and re-initialise the schema
```

Connection default is `postgresql://rted@127.0.0.1:5432/aussie_health` — **no password in the DSN**; override the whole thing with `DATABASE_URL`. libpq resolves the password from `%APPDATA%\postgresql\pgpass.conf` (outside OneDrive, not in git), which is the same mechanism `backup_db.cmd` already relies on. Verified 12/08/2026: psycopg2 connects on that DSN with `PGPASSWORD` unset. If you get an authentication failure, the pgpass entry is missing or its host/port/db/user line does not match — not a code problem. The container port is bound to loopback only, consistent with the privacy posture in [[System_Overview]].

## No-Docker stopgap (in use since 20/07/2026)

Docker Desktop is not yet installed on this machine, so the cache currently runs on **portable PostgreSQL 16.13** (official EDB binaries zip, no installer, no Windows service) at `%LOCALAPPDATA%\AussieHealth\` — same credentials, port, loopback-only binding and schema as the compose stack, so every command above works unchanged.

```powershell
$ah = "$env:LOCALAPPDATA\AussieHealth"
& "$ah\pgsql\bin\pg_ctl.exe" -D "$ah\pgdata" -l "$ah\pg.log" -o "-p 5432 -h 127.0.0.1" start
& "$ah\pgsql\bin\pg_ctl.exe" -D "$ah\pgdata" stop
```

The server does **not** auto-start on boot; start it before running ingests. Migrating to Docker later: stop this server, `docker compose up -d`, re-run the three ingest legs (the cache holds public data only and is rebuildable at any time), then delete `%LOCALAPPDATA%\AussieHealth\`.

## Recurring poll (since 20/07/2026)

A logon-started loop, **`scripts/poll_loop.py`**, polls every 15 minutes (the policy interval in [[NSW_Health_JSON_Engine]]). Each cycle runs `scripts/poll_waits.py` as a fresh subprocess: start the portable Postgres if it is down → one `GetHospitalDetails` call (whole state) → one Outages-list check → ~59 rows appended to `ed_wait_snapshots`. A Windows named mutex enforces a single instance, so a second launch exits without double-polling. Runs only while Jared is logged on. ~~Gaps in the series just mean the machine was off.~~ **Corrected 31/07/2026** — gaps are mostly the machine *asleep*, not off, and the loop cannot poll through them: see [[#Modern Standby breaks the loop's cadence]]. The UA carries a real contact (jaredlilliss@gmail.com) per the conduct policy.

Auto-start: a shortcut in the Startup folder (`shell:startup` → `AussieHealthWaitsPoll.lnk`) launches the loop via `pythonw.exe` (no console) at each logon.

```powershell
# start it now without logging out:
Start-Process "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe" `
  -ArgumentList '"…\Aussie_Health_Docs_v2\scripts\poll_loop.py"'
Get-Content "$env:LOCALAPPDATA\AussieHealth\poll_loop.log" -Tail 5   # cycle history
# stop it: end the pythonw.exe running poll_loop.py (Task Manager), or remove the .lnk to disable auto-start
```

### Why a loop and not Task Scheduler

The first attempt registered a scheduled task `AussieHealthWaitsPoll` (`pythonw.exe scripts/poll_waits.py`, every 15 min). It is registered but **left disabled**, because it could not be verified from the non-interactive setup session: a "run only when logged on" (Interactive-logon) task needs an interactive desktop to launch its action, and every trigger there reported a run yet executed nothing (no log, no DB rows, no heartbeat). Switching it to a headless S4U logon type needs elevation, which was not available. ~~On Jared's real interactive desktop the task would fire normally~~ — **disproven 31/07/2026**: tested on the real interactive desktop, the task reported `LastTaskResult 0x1` and executed nothing (no heartbeat, no log line), *even with the action replaced by `pythonw.exe` directly*. Deleting and re-registering it with `Register-ScheduledTask` produced a task that runs (`0x0`) — so the original registration was simply broken, not blocked by the logon type. ~~The remaining blocker is the interpreter path, not the task~~ — **also disproven** (03/08, see the per-machine Python section below; and conclusively 04/08, next).

#### Root cause, found 04/08/2026: Task Scheduler here cannot spawn a child process

**Stop trying launcher variants. Task Scheduler is unusable for automation on this machine — not for Python, not for anything.**

Isolated with a single task action running one `cmd.exe` command that did two things in sequence: an inline `echo` to a log, then `pg_dump.exe`. The **echo ran** — a cmd builtin, internal to the process. **`pg_dump` never did** — a child process. Same run, same shell, same token. Anything requiring `CreateProcess` dies before its first statement while Windows reports a healthy launch: the operational log shows event 129 *with a real PID*, then a fast exit.

That one fact collapses seven separate mysteries into one bug:

| # | Action tried | Result |
|---|---|---|
| 1 | `pythonw.exe`, spaced `C:\Program Files\Python313\` path | no heartbeat |
| 2 | same via 8.3 short path `C:\PROGRA~1\PYTHON~1\pythonw.exe` | no heartbeat |
| 3 | `run_poll.cmd` as the `Execute` | a `.cmd` is not a PE binary |
| 4 | `cmd.exe /c call "run_poll.cmd"` | no heartbeat |
| 5 | one `cmd.exe` action chaining `echo … & pythonw.exe poll_waits.py` | echo ran, Python did not |
| 6 | `powershell.exe -File run_poll.ps1` | rc `0xFFFD0000`, first line never executed |
| 7 | `powershell.exe -Command`, writing only a marker file | rc `0x1`, marker never created |

Both PowerShell command lines were then run **verbatim from an interactive shell and both succeeded** (exit 0, heartbeat +1), so binary, arguments, quoting and window style are all fine. The difference is purely the Task Scheduler execution context.

**Ruled out by direct check the same night — do not re-test:** Software Restriction Policy (`Safer\codeidentifiers` present but empty — no `DefaultLevel`, no rules), AppLocker (cmdlets absent, EXE-and-DLL log empty), ASR rules (none configured), Controlled Folder Access (off), Defender and Application logs (silent at the failure timestamps). Smart App Control **is** enforced (`VerifiedAndReputablePolicyState = 1`) and looked like the answer, but CodeIntegrity/Operational logs no block (3077) events for `powershell.exe` or `python.exe` — only Chrome DLL-load audit noise. Whatever sits beneath the child-spawn wall is still unexplained; [[VPS_Migration]] makes it moot.

**Consequence: use the Startup folder.** That context spawns children fine, which is why the poll loop works and why `AussieHealthDbBackup.lnk` was put there too. `AussieHealthWaitsPoll` stays registered and **disabled** — leave it that way. Enabling it does not double-poll; it simply does nothing. The `Enable-ScheduledTask` snippet that used to sit here has been removed rather than left to mislead.

Two diagnostic traps if anyone revisits this:

- **`LastTaskResult` is a lying oracle here.** Both `0` and `1` appeared with nothing executed. The only trustworthy signal is the heartbeat line count in `poll_heartbeat.txt` — `poll_waits.py` writes it on line ~38, before anything that can fail, so its absence proves the interpreter never started.
- **`New-ScheduledTaskSettingsSet` defaults `DisallowStartIfOnBatteries` and `StopIfGoingOnBatteries` to `$true`.** On a laptop usually on battery the task then queues (event 325) and "launches" (110) but **never emits 129**, and no process is created. That is a *different* signature, easily misread as the same bug. **Rule: no 129 = a condition blocked it (power/idle/network); 129 followed by a fast exit = the child-spawn wall.**

The cycle-spacing guard in `poll_waits.py` (stamp file `poll_last_run.txt`, 10-minute policy floor, since 22/07/2026) still protects against an accidental double-runner — the loop plus a manual run, say — degrading it to skipped cycles rather than double-polling.

### Persistence caveat (found 21/07/2026)

A loop instance launched from an automation tool session does **not** survive past that session — it is reaped when the launching session ends. The Startup shortcut itself is unaffected (it's a standard Windows mechanism, independent of any tool session) and will start the loop on Jared's next real logon or reboot — but until that happens, polling is only live if something in an active, held-open session is running it. Don't assume "the loop is running" carries forward between separate work sessions; check `Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'"` for a `poll_loop` command line, or look for a recent line in `poll_loop.log`, before relying on fresh snapshot data.

### Cold-start hang (found and fixed 22/07/2026)

The 21/07 note originally read "it ran one clean cycle, then was reaped" — the autopsy on 22/07 corrected this. Every **logon-cold** cycle (Postgres down) wedged forever in `poll_waits.py` at the `pg_ctl start` call: `subprocess.run(..., capture_output=True)` starts the long-lived `postgres.exe`, which **inherits the pipe write-handles**, so the pipes never reach EOF and `run()` blocks even after `pg_ctl` exits. Evidence: both cold starts (21/07 12:57, 22/07 19:39) logged `postgres not running; starting it` and then nothing; the only loop cycle that ever committed (20/07 19:48) found Postgres already up and skipped the branch — which is also why setup-day testing passed. The 21/07 13:02 commit had no heartbeat line: it was the manual retention-verification run, not the loop.

Fix: that one `subprocess.run` now sends output to the log file instead of pipes (a file handle needs no EOF wait). Verified 22/07/2026 by stopping Postgres and relaunching the loop: cold start to `cycle exit=0` in 5 s, 59 fresh rows committed. **Consequence: the Startup shortcut was never actually viable before this fix** (logon implies a cold DB); it is now.

### Hardening (22/07/2026, from the max code review)

The poll stack picked up several guards; behaviour is unchanged on the happy path:

- **Cycle spacing:** `poll_waits.py` skips a cycle if another launcher ran one under 10 minutes ago (`poll_last_run.txt`), so loop + re-enabled task + manual runs cannot exceed the conduct-policy cadence.
- **Cycle timeout:** the loop kills a cycle wedged past 14 minutes and keeps going (previously a single hang stopped polling forever while holding the mutex).
- **Failure backoff:** after 5 consecutive failed cycles the loop probes hourly instead of every 15 min — the loop-level stand-in for the poller's circuit breaker, which a fresh-subprocess-per-cycle design can never trip. **The backoff is skipped when the fault is ours.** Before backing off, the loop resolves the feed's hostname (read from `DEFAULT_WAITS_URL`, never duplicated): if the name will not resolve, this machine is offline, we never reached NSW Health, and waiting an hour protects nobody — so the normal 15-minute cadence is held and the first cycle after reconnect runs on time. Only a fault we can *reach* (connection refused, timeout after connect, HTTP errors) counts as "the upstream is unwell" and triggers the hourly probe. Measured cost of the old behaviour on 04/08/2026: five cycles failed with `getaddrinfo failed` while the laptop had no DNS, the loop dropped to hourly, and roughly ten 15-minute slots (~590 rows) were lost from a series that cannot be backfilled. Covered by `tests/test_poll_loop.py`.
- **Mutex acquisition** now uses `use_last_error` and handles a NULL handle, closing both silent-double-poll and silent-no-poll windows.
- **`pg_ctl start` output** goes to a dedicated `pg_ctl_start.log` (not `poll_waits.log`): postgres holds the inherited handle for its lifetime, which would have blocked the 5 MB rotation. Same no-pipes mechanism as the 22/07 cold-start fix; re-tested cold 24/07/2026 (see ledger) — `poll_waits.log` carried no `pg_ctl` output, confirming the split.
- **Outage leg:** OData `datetime''` filter literal, host aligned with the vault (no `www`), LHD label normalization + zero-match drift alarm, unreadable LHD fields skipped loudly instead of guessed statewide, fail-fast retries; each committed cycle now writes a `sync_runs` row recording whether the outage check ran.
- **Replay honesty:** `--waits --from-file` without `--dry-run` now refuses to commit (old counts would be stamped `captured_at=now()`); `--allow-stale-commit` overrides deliberately.

### Modern Standby breaks the loop's cadence

**Found 31/07/2026.** The loop runs, reports `cycle exit=0`, and still loses most of the day. Measured coverage over 24 h was **12–13%**, with blackouts of 6.6 h and 11.9 h. Observed poll gaps (expected 15 min):

```
07-30 21:27   —          07-31 03:08   169 min
07-31 00:19   172 min    07-31 05:03   114 min
07-31 08:45   222 min    07-31 15:24   399 min
```

Cause: `powercfg /a` reports this machine supports only **Standby (S0 Low Power Idle)** — Modern Standby — with S1/S2 unavailable. In S0 the OS keeps running but the Desktop Activity Moderator suspends ordinary user-session processes. `poll_loop.py` is exactly that: a `pythonw.exe` in Jared's logon session. When the lid closes it is frozen, `time.sleep()` stops advancing, and it resumes only when the machine does. The tell is a pair of cycles ~11 s apart on wake, because `max(0, INTERVAL_SECONDS - elapsed)` evaluates to 0 after a long suspension.

**A logon-session loop is therefore the wrong mechanism on this hardware** — no amount of hardening inside `poll_loop.py` can fix it.

> **Correction 05/08/2026 — the recommendation that stood here is dead.** This line used to continue "Task Scheduler is the right one (it is DAM-aware and can run during standby)". Task Scheduler on this machine **cannot launch anything**: a task-launched process cannot spawn a child process. Proved by a single task action in which an inline `cmd` `echo` ran and `pg_dump.exe`, in the same invocation, did not; `python.exe`, `pythonw.exe`, `powershell.exe` (`-File` and `-Command`), a `.cmd` file and `pg_dump.exe` all get a PID and die before their first statement. Seven forms tested across 03–04/08. So the diagnosis above is right and its proposed remedy is unavailable — **there is no correct mechanism on this laptop**, which is the strongest argument yet for the always-on host in [[VPS_Migration]].

**A second failure mode, measured 04–05/08/2026: the loop can stay frozen while the machine is AWAKE.** The loop logged `cycle exit=0` at 14:10:39 and wrote nothing for 13 hours, while its process stayed alive and the machine stayed up — Kernel-Power 107 reports 777.9 minutes awake, and the S0 transitions (**ids 506/507**, the Modern Standby equivalent; classic 42/107 alone are not reliable here) show an exit at 14:21:10 and no re-entry until 03:14:41. Cost: 13.1 h blackout, ~52 slots, coverage down to 21%. Note this is *not* the suspend-resume pattern described above, and the arithmetic rules out simple timer bias: a 900 s timer armed at 14:10 and paused for the ~10.5 min S0 excursion owes at most ~900 s of awake time and must fire by ~14:36. It did not fire for 13 hours — off by a factor of ~50. The best-fitting explanation is that the DAM froze the process on S0 entry and it was **never resumed**, but that was not proven; do not record it as fact.

Two mitigations shipped, neither a cure:

- **`poll_loop.py` waits in 30 s slices** (`wait_for_next_cycle`) instead of one `time.sleep(900)`, recomputing the deadline from the wall clock each slice. A mis-armed or over-long timer now costs one slice instead of the whole interval, and the first poll after a wake is immediate rather than serving out a stale timer. This is a damage bound, not a fix: a process frozen mid-slice is still frozen.
- **`scripts/poll_supervisor.py`** — a second logon-started `pythonw` process that restarts the loop when the heartbeat goes stale. Staleness is measured in **awake time** via `QueryUnbiasedInterruptTime` (verified present on this machine), so a nine-hour overnight standby accrues almost no awake time and never triggers a restart, while 13 awake hours without a poll triggers one at once. It kills before relaunching, because the loop holds a named mutex for its lifetime and a second instance would otherwise just exit. Restarts are rate-limited to 30 min, and the supervisor never touches the feed itself. **Its ceiling: it is also an ordinary user-session process, so the DAM can freeze it too — it helps only when it is thawed and the loop is not, which is exactly the 04/08 signature.**

Mitigation applied 31/07/2026: wake timers were **disabled on battery** (`SUB_SLEEP RTCWAKE` DC index `0x0`; AC was already `0x1`) and DC sleep begins after 5 min versus 60 min on AC. DC wake timers are now enabled:

```powershell
$g = ((powercfg /getactivescheme) -split '\s+')[3]
powercfg /setdcvalueindex $g SUB_SLEEP RTCWAKE 1   # 0=Disable 1=Enable 2=Important only
powercfg /setactive $g
```

This only *nudges* the system out of deep idle on schedule so the loop gets a chance to run; it is not a fix. `powercfg /waketimers` needs elevation, so an armed timer has **not** been confirmed — the honest test is a few hours unplugged followed by a coverage check.

### Per-machine Python install — TRIED 03/08/2026, PREMISE DISPROVEN

> **Do not repeat this.** The runbook that stood here proposed moving the
> interpreter out of `%LOCALAPPDATA%` on the theory that an execution policy
> blocked that path. It was executed on 03/08/2026 and **the theory was
> wrong**. The steps are removed rather than left to mislead; what follows is
> what actually happened.

**What was done.** Python **3.13.14** installed per-machine via winget
(`--scope machine`, hash verified from python.org) to `C:\Program Files\Python313`,
with `requests==2.34.2` and `psycopg2-binary==2.9.12` on native cp313 wheels.
A different minor version deliberately: a machine-scope install of the *same*
3.12.10 failed `1603` because the bundle mixes per-user and per-machine
components when that version already exists per-user, and 3.12 had to survive
as the rollback. The full suite (96 tests) passes under 3.13 as well as 3.12.

**What was disproven.** With both interpreters installed, a scheduled task
running `poll_waits.py` fails **identically** whether it points at
`C:\Program Files\Python313` or `%LOCALAPPDATA%\...\Python312`. Interpreter
location is irrelevant, so no AppData policy is involved. Three further
theories also died under test: that `pythonw.exe` specifically was the blocker
(`python.exe` fails the same way), that Task Scheduler could not launch Python
at all (it can — a task wrote its own `sys.executable` to a file), and that
Claude's MSIX container was redirecting the reads (both paths are byte-identical).

**The one hard fact.** After enabling the Task Scheduler operational log —
`wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true`, which had
been **disabled all along**, which is why every earlier "no events found" was
meaningless — a failing run reports:

```
id=129  launched "C:\Program Files\Python313\python.exe" with process ID 25152
id=201  completed with return code 2147942401      # 0x80070001
```

Task Scheduler launches Python and gets a PID; Python then exits 1 before
reaching the heartbeat write on line 38 of `poll_waits.py`. With no console
attached the traceback goes nowhere.

**Still unexplained.** Identical task configurations returned `0x80070001` on
one run and `0x0` on the next minutes later, and a task-run process reported
the heartbeat file as 952 bytes while the real file was 2716. Those two
observations are not reconciled. Do not build on this section without
reproducing them first.

**Conclusion: wrong host, not wrong config.** This is a personal laptop on
Modern Standby that sleeps most of the day; measured coverage sits at 20–28%
regardless of launcher. Chasing a scheduler quirk on a machine that is shut
half the time has a low ceiling. The pipeline is portable — Python, Postgres,
two dependencies, an offline suite — so a small always-on host (VPS or Pi,
cron) is the real fix and makes this entire section moot. The executable
checklist for that move is [[VPS_Migration]].

Current interpreter: **3.12.10, 64-bit**. Packages to replicate: `requests==2.34.2`, `psycopg2-binary==2.9.12` (deps resolve automatically: certifi, charset-normalizer, idna, urllib3).

1. **Install (elevated).** python.org `python-3.12.10-amd64.exe`, tick *Install for all users*, location `C:\Python312` (no spaces). Leave *Add to PATH* unticked — every reference below is absolute, and this avoids a PATH fight while the per-user copy still exists.
2. **Packages (elevated).**
   ```powershell
   C:\Python312\python.exe -m pip install "requests==2.34.2" "psycopg2-binary==2.9.12"
   C:\Python312\python.exe -c "import requests,psycopg2;print('ok')"
   ```
3. **Prove the block is gone — before re-pointing anything.**
   ```powershell
   schtasks /Create /TN AH_PathTest /TR "C:\Python312\pythonw.exe \"<repo>\scripts\poll_waits.py\"" /SC ONCE /ST 23:59 /F
   schtasks /Run /TN AH_PathTest
   ```
   Expect `Last Result 0` **and** a fresh line in `poll_heartbeat.txt`. If it is still non-zero the policy blocks more than `AppData` and this approach is dead — stop here. Clean up with `schtasks /Delete /TN AH_PathTest /F`.
4. **Re-point all six references** (missing one leaves a half-migrated stack):

   | Location | What |
   |---|---|
   | `~/.claude/settings.json` ×2 | bash-log + pre-compact hooks |
   | `cc/.claude/settings.local.json` | SessionStart Postgres/coverage hook |
   | `%LOCALAPPDATA%\AussieHealth\run_poll.cmd` | task launcher |
   | Startup `AussieHealthWaitsPoll.lnk` | loop target |
   | this file | the `Start-Process` snippet above |

5. **Cut over.** Point the task action at `C:\Python312\pythonw.exe scripts/poll_waits.py`, stop the loop, rename the Startup shortcut to `.lnk.disabled` (reversible).
6. **Verify.** Force a run, confirm heartbeat + a committed cycle, then watch 24 h coverage climb off 12%.

Keep the per-user Python installed until step 6 passes — it is the rollback. Note this fixes *launching* only; whether a Limited/Interactive task reliably runs during S0 standby is still unproven, and the fallback (`RunLevel Highest`, or a SYSTEM-account task) also needs elevation.

## Status ledger

| Leg | Where it has run | Date |
|---|---|---|
| Transform + dry-run against real saved payload | Claude sandbox; Jared's machine | 12/06/2026; 19/07/2026 |
| Facilities transform against **live** Data.NSW fetch (266 rows) | Jared's machine (dry-run) | 19/07/2026 |
| MBS parser: mock parse + 150k-record streaming test | Claude sandbox; mock re-run on Jared's machine | 12/06/2026; 19/07/2026 |
| Wait-count route captured + `parse_wait_payload` implemented | Jared's machine; live fetch parses 59 hospitals | 19/07/2026 |
| Schema init, DB UPSERT, row printout (portable PG stopgap, see above; compose boot itself still pending Docker) | Jared's machine: 266 facilities live-loaded | 20/07/2026 |
| MBS full-release load into Postgres | Jared's machine: July 2026 release (`MBS-XML-20260701.XML`), 6,045 items, 84 derived-fee NULLs | 20/07/2026 |
| `ed_wait_snapshots` live writer | Jared's machine: 59 statewide snapshots committed via `--waits` | 20/07/2026 |
| Cold-start hang in `poll_waits.py` found + fixed; loop verified cold-to-commit in 5 s (59 rows) | Jared's machine | 22/07/2026 |
| Post-hardening cold-start re-test: stopped PG, one cycle cold-started it in 6.3 s, committed 59 rows; `pg_ctl` output isolated to `pg_ctl_start.log`; `sync_runs` row written; live Outages query accepted + parsed (0 active, so no LHD-match proof yet) | Jared's machine | 24/07/2026 |

| PR #4 (hardening + docs) merged to `main`; loop restarted on fixed code; one cycle cold-to-commit in 5.5 s, 59 rows | Jared's machine | 31/07/2026 |
| Modern Standby (S0) identified as the cause of 12–13% daily coverage; DC wake timers enabled as mitigation (armed timer unconfirmed — needs elevation) | Jared's machine | 31/07/2026 |
| Scheduled task `AussieHealthWaitsPoll` re-registered and verified running (`0x0`), disproving the "broken because Interactive-logon" theory | Jared's machine | 31/07/2026 |
| Per-machine Python migration **prepared, not executed** (needs elevation) | — | 31/07/2026 |
| Migration **executed and premise disproven**: Python 3.13.14 installed per-machine (96 tests pass on it), but a scheduled task fails identically from `C:\Program Files` and `%LOCALAPPDATA%` — no AppData policy involved | Jared's machine | 03/08/2026 |
| Task Scheduler operational log **enabled** (was disabled, invalidating every earlier "no events found"); a failing run shows Python launched with a PID then exiting `0x80070001` | Jared's machine | 03/08/2026 |
| Conclusion recorded: laptop on Modern Standby is the wrong host at 20–28% coverage; next step is a small always-on host, not further scheduler debugging | — | 03/08/2026 |
| **Coverage lost to our own backoff.** Local DNS failure produced `cycle exit=1` at 07:26:51, 07:41:53, 07:56:53, 08:11:55, 08:26:56 → `5 consecutive failed cycles; next attempt in 3600s`; the 09:26:53 hourly probe also failed → `6 consecutive`. Six attempts where ~12 were due, then nothing until a manual restart at 10:25:37 committed 59 rows at 10:25:48. The fault was local, so the backoff protected nobody. Fixed since — see PR #15 (`0fa43f0`) | Jared's machine | 04/08/2026 |
| Supervisor deployed: `Startup\AussieHealthPollSupervisor.lnk` present and `poll_supervisor.py` running (pid 35640, started 15:49). **Deployed is not the same as working** — see the 06/08 row below | Jared's machine | 05/08/2026 |
| **842-minute blackout with the supervisor alive throughout.** Last cycle 05/08 22:31:46; next line in `poll_loop.log` is a manual `loop start` at 06/08 12:33:51 — a 14 h 2 min hole, measured from the log, not inferred. The supervisor (pid 35640, up since 05/08 15:49) issued no restart in that window. This is the ceiling PR #19 documented for itself — the DAM can freeze the supervisor as readily as the loop — observed in the wild rather than predicted. **A watchdog inside the same user session cannot close this gap** | Jared's machine | 06/08/2026 |
| **Cluster found dead; series rescued.** Postgres down with a stale `postmaster.pid` (claimed pid 4196; no such process — `pg.log` ended mid-checkpoint 02:05:30 with no shutdown record). Pid file cleared, cluster restarted via the documented `pg_ctl` command, `pg_isready` reported accepting connections. Restore-verified dump taken: `aussie_health-20260807-1322.dump`, 412,014 B — `pg_restore -l` lists 47 TOC entries and `pg_restore -f -` recovers 21,246 data lines. The newest prior dump was 05/08, so this closed a real exposure window | Jared's machine (performed by the strategy lane; dump and cluster state independently verified here) | 07/08/2026 |
| **Launch-context experiment armed — awaits a real logon.** A second Startup shortcut, `AussieHealthPollLoopViaCmd.lnk`, interposes cmd: `cmd.exe /c start "" /b "<3.12 pythonw>" "<repo>\scripts\poll_loop.py"`. It sits alongside `AussieHealthWaitsPoll.lnk` deliberately — the named mutex makes the loser exit *and log doing so*, and neither can double-poll. Pre-flight verified the same day: invoked against the live mutex it logged `another loop instance already running; exiting` at 13:36:04, proving the command line executes Python, so silence at a cold logon is diagnostic rather than ambiguous. Reading it: a line at logon time means the wall is launch-context and the same trick should retro-unblock Task Scheduler; continued silence means the cause is deeper and no launcher fix will help. Remove by deleting that one `.lnk` | Jared's machine | 07/08/2026 |
| **Experiment ANSWERED — negative, and the diagnosis changed.** Reboot 15:33:17; both shortcuts fired; captured 16:31 with both processes alive and deliberately unkilled. The cmd-interposed loop (pid 4008, parent already exited — the `cmd /c start` signature) froze **identically to a direct launch**, so interposing cmd does not rescue Python and the launch-context theory is dead. `poll_supervisor.py` (pid 14168) was launched by `explorer.exe` straight from the Startup folder and froze the same way. **`py-spy dump` then overturned the standing framing:** the loop was at `wait_for_next_cycle` (line 130, the `sleep` call) via `main` (line 213, the wait at the *end* of the while body) — so it had started, passed the mutex guard, run a full cycle and reached the between-cycle wait. It was **not** dying before its first statement. It is stuck **inside `time.sleep()`** — proven by an unchanged stack and CPU identical to the microsecond (0.140625) across a 45 s window against a 30 s slice, with no heartbeat growth. That is the exact case `wait_for_next_cycle`'s docstring says it cannot defend against. **Still unexplained:** that first cycle's `loop start` / `cycle exit` lines and its heartbeat never appeared, though a fresh interactive `python` appends to the same resolved path first try — the `except OSError: pass` in `log()` is what hides it, so making log failures visible is worth shipping regardless of root cause | Jared's machine | 07/08/2026 |

Nothing in this vault claims a run that did not happen; update the ledger when the local legs go green.

Row counts and PIDs above are stamped at their moment and are **stale on sight** — the ED series grows ~240 rows/day and a loop PID rarely survives a day. Run `count(*)` (never `n_live_tup`) and re-enumerate processes rather than trusting any figure written here.
