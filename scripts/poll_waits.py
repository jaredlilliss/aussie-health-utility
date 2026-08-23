"""One wait-count ingest cycle, normally run as a subprocess of poll_loop.py.

scripts/poll_loop.py (logon-started; see 03_Local_Cache/Local_Deployment.md)
invokes this every 15 minutes; a registered-but-disabled Task Scheduler task
(AussieHealthWaitsPoll) is the documented alternative launcher. Starts the
portable Postgres if it is not already up, then runs the --waits leg of
ingest_ed_waits. All output lands in
%LOCALAPPDATA%\\AussieHealth\\poll_waits.log (rotated once past 5 MB);
pythonw has no console, so stdout/stderr must be redirected before anything
prints. A spacing guard skips the cycle when another launcher ran one less
than MIN_CYCLE_SPACING_SECONDS ago, so overlapping mechanisms (loop +
re-enabled task + manual run) cannot poll the NSW feed faster than the
conduct-policy floor.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime

AH = os.path.join(os.environ["LOCALAPPDATA"], "AussieHealth")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(AH, "poll_waits.log")
STAMP = os.path.join(AH, "poll_last_run.txt")
CREATE_NO_WINDOW = 0x08000000
# Polling policy floor (vault: NSW_Health_JSON_Engine.md): never below 10 min.
MIN_CYCLE_SPACING_SECONDS = 600

# Fresh machine: under pythonw a missing dir would otherwise kill the cycle
# before the heartbeat, the log, or any other evidence exists.
os.makedirs(AH, exist_ok=True)

# Heartbeat before anything else that can fail: proves the interpreter
# reached the script even if the cycle itself dies without a log line.
with open(os.path.join(AH, "poll_heartbeat.txt"), "a", encoding="utf-8") as _hb:
    _hb.write(datetime.now().isoformat() + "\n")

if os.path.exists(LOG) and os.path.getsize(LOG) > 5 * 1024 * 1024:
    try:
        os.replace(LOG, LOG + ".old")
    except OSError:
        pass  # someone still holds the log open; rotate on a later cycle
sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8", buffering=1)

# Spacing guard: whatever combination of launchers exists, cycles must not
# run closer together than the policy floor.
try:
    with open(STAMP, encoding="utf-8") as fh:
        _last = float(fh.read().strip() or 0)
except (OSError, ValueError):
    _last = 0.0
_elapsed = time.time() - _last
if 0 <= _elapsed < MIN_CYCLE_SPACING_SECONDS:
    print(f"cycle skipped: previous run {_elapsed:.0f}s ago "
          f"(spacing floor {MIN_CYCLE_SPACING_SECONDS}s)")
    sys.exit(0)
with open(STAMP, "w", encoding="utf-8") as fh:
    fh.write(str(time.time()))

pg_ctl = os.path.join(AH, "pgsql", "bin", "pg_ctl.exe")
pgdata = os.path.join(AH, "pgdata")
status = subprocess.run([pg_ctl, "-D", pgdata, "status"],
                        capture_output=True, creationflags=CREATE_NO_WINDOW)
if status.returncode != 0:
    print("postgres not running; starting it")
    # No pipes on this call: pg_ctl spawns the long-lived postgres server,
    # which inherits the pipe write-handles, so with capture_output=True the
    # pipes never reach EOF and run() blocks forever once pg_ctl exits
    # (found 22/07/2026 -- every logon-cold cycle wedged here; warm cycles
    # skip this branch, which is why setup-day testing passed). Output goes
    # to a dedicated, never-rotated file: a file handle needs no EOF wait,
    # and postgres keeps the inherited handle for its lifetime, which would
    # break the 5 MB rotation above if it pointed at poll_waits.log.
    with open(os.path.join(AH, "pg_ctl_start.log"), "a",
              encoding="utf-8") as start_out:
        start = subprocess.run(
            [pg_ctl, "-D", pgdata, "-l", os.path.join(AH, "pg.log"),
             "-o", "-p 5432 -h 127.0.0.1", "-w", "start"],
            stdout=start_out, stderr=start_out,
            creationflags=CREATE_NO_WINDOW)
    if start.returncode != 0:
        print("pg_ctl start failed; see pg_ctl_start.log and pg.log")
        sys.exit(1)

os.chdir(REPO)
sys.path.insert(0, os.path.join(REPO, "src"))
sys.argv = ["ingest_ed_waits.py", "--waits"]
import ingest_ed_waits  # noqa: E402

sys.exit(ingest_ed_waits.main())
