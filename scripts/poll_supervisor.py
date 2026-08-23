"""Restart the wait-count loop when it stops polling while the machine is awake.

Why this exists
---------------
Measured 04-05/08/2026: poll_loop.py logged `cycle exit=0` at 14:10:39 and then
wrote nothing for 13 hours. Its process was still alive the next morning, and
the machine was NOT asleep for that window -- Kernel-Power 107 reports 777.9
minutes awake, and the S0 transitions (ids 506/507) show an exit at 14:21:10
with no re-entry until 03:14:41. Roughly 52 fifteen-minute slots were lost, and
the RTED API serves current state only, so they are gone for good.

`03_Local_Cache/Local_Deployment.md` names the mechanism: in S0 the Desktop
Activity Moderator suspends ordinary user-session processes, and poll_loop.py is
exactly that. What the 04/08 numbers add is that the process was apparently
never resumed -- the machine ran for 13 awake hours with the loop still frozen.
A frozen process cannot unfreeze itself, which is the whole argument for an
observer outside it.

Honest about the ceiling
------------------------
This supervisor is also an ordinary user-session process, so the DAM can freeze
it too. It helps only when it is thawed and the loop is not -- which is exactly
the observed failure. It cannot help if both stay frozen, and it cannot poll
anything itself. The documented conclusion in Local_Deployment.md stands: a
logon-session loop is the wrong mechanism on this hardware, and the real fix is
the always-on host in VPS_Migration.md. This narrows the window; it does not
close it.

Why staleness is measured in AWAKE time
---------------------------------------
Wall-clock staleness cannot tell a wedge from a normal overnight standby, and an
alarm that fires every night is an alarm nobody reads. `QueryUnbiasedInterruptTime`
excludes time spent in low-power states, so a nine-hour standby accrues almost no
awake time and never triggers a restart, while 13 awake hours without a poll
triggers one immediately. Verified present and sane on this machine 05/08/2026.

Safety
------
The supervisor NEVER polls the NSW feed. Only poll_loop.py -> poll_waits.py may
do that. Restarts are rate-limited well above the 10-minute conduct floor, and
poll_waits.py keeps its own 600s cycle-spacing guard regardless. Relaunching is
inherently safe: poll_loop.py takes a named mutex at startup, so a second
instance exits immediately if the first is genuinely alive.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Callable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
LOOP_SCRIPT = os.path.join(HERE, "poll_loop.py")
AH = os.path.join(os.environ["LOCALAPPDATA"], "AussieHealth")
HEARTBEAT = os.path.join(AH, "poll_heartbeat.txt")
LOG = os.path.join(AH, "poll_supervisor.log")

CHECK_INTERVAL_SECONDS = 120
# Two missed polls of awake time. Generous on purpose: a restart costs a cycle,
# a false alarm costs trust, and the loop's own interval is 900s.
STALE_AFTER_AWAKE_SECONDS = 1_800
# Never restart more often than this. Independent of poll_waits.py's own guard,
# so the feed stays protected even if that guard is ever changed.
MIN_RESTART_SPACING_SECONDS = 1_800
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


def log(msg: str) -> None:
    try:
        os.makedirs(AH, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat()} {msg}\n")
    except OSError:
        pass  # a supervisor that dies of its own logging is worse than useless


def awake_seconds() -> float:
    """Seconds since boot EXCLUDING low-power time (QueryUnbiasedInterruptTime)."""
    value = ctypes.c_ulonglong(0)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.QueryUnbiasedInterruptTime(ctypes.byref(value)):
        raise OSError("QueryUnbiasedInterruptTime failed")
    return value.value / 1e7  # 100-ns units


def heartbeat_mtime() -> Optional[float]:
    try:
        return os.path.getmtime(HEARTBEAT)
    except OSError:
        return None


def should_restart(
    awake_since_heartbeat: Optional[float],
    awake_since_last_restart: Optional[float],
) -> tuple[bool, str]:
    """The whole decision, as a pure function so it is testable offline.

    `awake_since_heartbeat` is None when no heartbeat file exists yet.
    `awake_since_last_restart` is None when we have not restarted this run.
    """
    if awake_since_heartbeat is None:
        return False, "no heartbeat file yet; nothing to judge"
    if awake_since_heartbeat < STALE_AFTER_AWAKE_SECONDS:
        return False, ""
    if (awake_since_last_restart is not None
            and awake_since_last_restart < MIN_RESTART_SPACING_SECONDS):
        return False, (
            f"heartbeat stale {awake_since_heartbeat:.0f}s awake, but a restart "
            f"was attempted {awake_since_last_restart:.0f}s ago; holding off"
        )
    return True, (
        f"heartbeat stale {awake_since_heartbeat:.0f}s of AWAKE time "
        f"(threshold {STALE_AFTER_AWAKE_SECONDS}s) -- the loop is not polling "
        f"while the machine is up, which is the 04/08 wedge signature"
    )


def find_loop_pids() -> list[int]:
    """PIDs of running poll_loop.py processes, via WMI through wmic-free CIM."""
    pids: list[int] = []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or "
             "Name='python.exe'\" | Where-Object { $_.CommandLine -match "
             "'poll_loop' } | ForEach-Object { $_.ProcessId }"],
            capture_output=True, text=True, timeout=60,
            creationflags=CREATE_NO_WINDOW,
        )
        for line in (out.stdout or "").split():
            if line.strip().isdigit():
                pids.append(int(line.strip()))
    except Exception as exc:  # noqa: BLE001
        log(f"could not enumerate loop processes: {exc!r}")
    return pids


def restart_loop() -> None:
    """Kill any wedged loop, then start a fresh one.

    Killing first is required, not optional: poll_loop.py holds a named mutex
    for its lifetime, so a new instance launched alongside a live-but-wedged one
    would see the mutex, log "another loop instance already running" and exit.
    Terminating the old process closes its handles and frees the mutex.
    """
    for pid in find_loop_pids():
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=30,
                           creationflags=CREATE_NO_WINDOW)
            log(f"terminated wedged loop pid={pid}")
        except Exception as exc:  # noqa: BLE001
            log(f"failed to terminate pid={pid}: {exc!r}")

    try:
        subprocess.Popen(
            [sys.executable, LOOP_SCRIPT],
            cwd=os.path.dirname(HERE),
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
            close_fds=True,
        )
        log(f"relaunched {LOOP_SCRIPT}")
    except Exception as exc:  # noqa: BLE001
        log(f"FAILED to relaunch the loop: {exc!r}")


def main(iterations: Optional[int] = None,
         sleep: Optional[Callable[[float], None]] = None) -> int:
    sleep = sleep or time.sleep
    log(f"supervisor start pid={os.getpid()} "
        f"check={CHECK_INTERVAL_SECONDS}s stale_after={STALE_AFTER_AWAKE_SECONDS}s awake")

    last_restart_awake: Optional[float] = None
    seen_mtime: Optional[float] = None
    seen_at_awake: Optional[float] = None
    count = 0

    while iterations is None or count < iterations:
        count += 1
        try:
            now_awake = awake_seconds()
            mtime = heartbeat_mtime()

            # Track when the heartbeat last CHANGED, in awake time. Comparing
            # the file's wall-clock mtime against an awake clock would be
            # mixing units; this records the awake instant we first saw it.
            if mtime is not None and mtime != seen_mtime:
                seen_mtime = mtime
                seen_at_awake = now_awake

            since = None if seen_at_awake is None else now_awake - seen_at_awake
            since_restart = (None if last_restart_awake is None
                             else now_awake - last_restart_awake)

            restart, reason = should_restart(since, since_restart)
            if reason:
                log(reason)
            if restart:
                restart_loop()
                last_restart_awake = now_awake
                seen_at_awake = now_awake  # give the new loop room to prove itself
        except Exception as exc:  # noqa: BLE001 - never let a check kill the watch
            log(f"check error: {exc!r}")

        sleep(CHECK_INTERVAL_SECONDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
