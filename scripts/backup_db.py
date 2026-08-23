"""Back up the aussie_health cache. Launched from the Startup folder at logon.

Why Python and not the .cmd it replaces: a Startup shortcut pointing at
cmd.exe was observed twice (04/08 and 05/08 logons) NOT to run, while its
sibling shortcut pointing straight at pythonw.exe started the poll loop both
times from the same folder. Cause never established -- Startup processing
leaves no log -- so this mirrors the mechanism that demonstrably works
instead of arguing with the one that does not. backup_db.cmd is kept for
manual use; it works fine when invoked directly.

Why not Task Scheduler at all: a task-launched process on this machine cannot
spawn a child process. Proved by one action where an inline `echo` ran and
pg_dump.exe, in the same cmd invocation, did not. See the machine-env memory.

No password lives here: pg_dump reads %APPDATA%\\postgresql\\pgpass.conf,
which is outside OneDrive.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

AH = r"C:\Users\Xi\AppData\Local\AussieHealth"
PG_DUMP = os.path.join(AH, "pgsql", "bin", "pg_dump.exe")
DEST = r"C:\Users\Xi\OneDrive\Backups\AussieHealth"
LOG = os.path.join(AH, "backup_db.log")
KEEP_DAYS = 14
# The cluster does not auto-start; a logon backup can easily beat it up.
STARTUP_GRACE_SECONDS = 120
CREATE_NO_WINDOW = 0x08000000


def log(msg: str) -> None:
    try:
        os.makedirs(AH, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now():%a %d/%m/%Y %H:%M:%S}] {msg}\n")
    except OSError:
        pass  # a logger must never be the reason a backup fails


def postgres_ready() -> bool:
    """Wait briefly for the cluster: at logon we may simply be early."""
    import socket

    deadline = time.time() + STARTUP_GRACE_SECONDS
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(2)
            try:
                s.connect(("127.0.0.1", 5432))
                return True
            except OSError:
                time.sleep(5)
    return False


def prune() -> None:
    """Best-effort retention. Losing an old dump beats skipping today's."""
    cutoff = datetime.now() - timedelta(days=KEEP_DAYS)
    removed = 0
    try:
        for name in os.listdir(DEST):
            if not (name.startswith("aussie_health-") and name.endswith(".dump")):
                continue
            path = os.path.join(DEST, name)
            if datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
                os.remove(path)
                removed += 1
    except OSError as exc:
        log(f"retention skipped: {exc}")
        return
    log(f"retention: removed {removed} dump(s) older than {KEEP_DAYS} days")


def main() -> int:
    log("--- backup start (python launcher) ---")

    if not os.path.exists(PG_DUMP):
        log(f"FATAL: pg_dump missing at {PG_DUMP}")
        return 2
    if not postgres_ready():
        log(f"SKIPPED: postgres not accepting connections after "
            f"{STARTUP_GRACE_SECONDS}s -- nothing to back up")
        return 3

    os.makedirs(DEST, exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d-%H%M}"
    out = os.path.join(DEST, f"aussie_health-{stamp}.dump")

    result = subprocess.run(
        [PG_DUMP, "-h", "127.0.0.1", "-p", "5432", "-U", "rted",
         "-d", "aussie_health", "-Fc", "-f", out],
        creationflags=CREATE_NO_WINDOW,
        capture_output=True,
        text=True,
    )

    if result.returncode == 0 and os.path.exists(out):
        size_kb = os.path.getsize(out) / 1024
        log(f"pg_dump OK, wrote aussie_health-{stamp}.dump ({size_kb:.1f} KB)")
        prune()
        return 0

    err = " | ".join((result.stderr or "").split())[:300]
    log(f"pg_dump FAILED rc={result.returncode} {err}")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - a crash must leave a trace
        log(f"UNCAUGHT: {exc!r}")
        sys.exit(4)
