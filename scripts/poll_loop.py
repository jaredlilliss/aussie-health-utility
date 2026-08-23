"""Durable 15-minute wait-count poll loop (logon-started).

Why a loop instead of Task Scheduler: a "run only when logged on" scheduled
task needs an interactive desktop session to launch its action, and the setup
environment had no way to register a headless (S4U) task without elevation.
This loop runs in the ordinary user session, started from the Startup folder
(see 03_Local_Cache/Local_Deployment.md), and simply invokes one poll_waits.py
cycle every INTERVAL_SECONDS.

A Windows named mutex guarantees a single loop instance (poll_waits.py adds
its own cycle-spacing guard, so even a second launcher mechanism cannot
double-poll). Each cycle is a fresh subprocess with a hard timeout: a crash
or hang in one ingest never kills or wedges the loop. After several
consecutive failed cycles the loop backs off to an hourly probe -- the
fresh-subprocess design means the shared poller's circuit breaker can never
accumulate state across cycles, so the "stop hammering a failing pipeline"
duty lives here.

That backoff is deliberately NOT applied when the failures are our own fault.
Measured 04/08/2026: five cycles failed with
`getaddrinfo failed` -- this laptop had no DNS -- the loop backed off to an
hourly probe, and roughly ten 15-minute slots (~590 rows) were lost from a
series that cannot be backfilled, because the RTED API serves current state
only. Backing off exists to spare a struggling upstream; a name that will not
resolve means we never reached the upstream at all, so waiting an hour
protects nobody and only costs data. The loop therefore resolves the feed's
hostname before backing off and keeps the normal cadence when the fault is
local: retrying every 15 minutes while offline is free, and the moment
connectivity returns the next cycle succeeds instead of waiting out the hour.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime
from typing import Callable, Optional

INTERVAL_SECONDS = 900          # 15 min; matches the polling policy
SLEEP_SLICE_SECONDS = 30        # never hold one long timer; see wait_for_next_cycle
CYCLE_TIMEOUT_SECONDS = 840     # kill a wedged cycle before the next is due
FAILURE_BACKOFF_THRESHOLD = 5   # consecutive failed cycles -> back off
FAILURE_BACKOFF_SECONDS = 3600  # probe hourly while the pipeline is failing
CREATE_NO_WINDOW = 0x08000000
ERROR_ALREADY_EXISTS = 183
ERROR_ACCESS_DENIED = 5

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
POLL_ONCE = os.path.join(HERE, "poll_waits.py")
AH = os.path.join(os.environ["LOCALAPPDATA"], "AussieHealth")
LOOP_LOG = os.path.join(AH, "poll_loop.log")


def upstream_host() -> Optional[str]:
    """Hostname of the wait-counts feed, read from the pipeline's own constant.

    Imported rather than duplicated so the loop cannot drift from the URL the
    ingest actually calls. Returns None if it cannot be determined, which the
    caller treats as "cannot tell" and falls back to backing off.
    """
    try:
        src = os.path.join(REPO, "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        from ed_waits.common import DEFAULT_WAITS_URL

        return urllib.parse.urlsplit(DEFAULT_WAITS_URL).hostname
    except Exception:  # noqa: BLE001 - never let a diagnostic kill the loop
        return None


def local_network_down(resolve: Optional[Callable[[str], object]] = None) -> bool:
    """True when the feed's hostname will not resolve: our fault, not theirs.

    Only a name-resolution failure counts. A refused or timed-out connection
    means we reached the network and the upstream is genuinely unwell, which
    is exactly the case the backoff is for.
    """
    host = upstream_host()
    if not host:
        return False  # cannot tell -> treat as an upstream problem
    resolve = resolve or (lambda h: socket.getaddrinfo(h, 443))
    try:
        resolve(host)
        return False
    except socket.gaierror:
        return True
    except Exception:  # noqa: BLE001 - an odd resolver error is not proof
        return False


def wait_for_next_cycle(
    started: float,
    interval: int,
    sleep: Optional[Callable[[float], None]] = None,
    now: Optional[Callable[[], float]] = None,
) -> None:
    """Wait until `interval` of WALL-CLOCK time has passed since `started`.

    Deliberately not one long `time.sleep(interval)`. Measured 04-05/08/2026:
    the loop completed a cycle at 14:10:39 and then wrote nothing for 13 hours
    while its process stayed alive and the machine stayed in a working power
    state -- roughly 52 unrecoverable 15-minute slots. The suspected mechanism
    is that a Windows wait is charged in *unbiased* interrupt time, which does
    not advance across an S0 low-power transition, so a timer armed before one
    can outlast its wall-clock deadline by hours. That was never proven, so
    this fix deliberately does not depend on it being the cause.

    Slicing helps whatever the cause: the deadline is recomputed from the wall
    clock on every iteration, so a slice that returns late (or a machine that
    was suspended) is noticed immediately and the loop polls at once instead of
    serving out a stale timer. Exposure to any single mis-armed timer drops
    from the whole interval to one slice. It cannot save us from a `sleep` that
    never returns at all -- that is what the external watchdog is for.
    """
    sleep = sleep or time.sleep
    now = now or time.time
    deadline = started + interval
    while True:
        remaining = deadline - now()
        if remaining <= 0:
            return
        sleep(min(SLEEP_SLICE_SECONDS, remaining))


def next_interval(
    consecutive_failures: int,
    resolve: Optional[Callable[[str], object]] = None,
) -> tuple[int, Optional[str]]:
    """Seconds to wait before the next cycle, plus a line to log if notable.

    Split out from the loop body so the backoff decision is testable offline.
    """
    if consecutive_failures < FAILURE_BACKOFF_THRESHOLD:
        return INTERVAL_SECONDS, None
    if local_network_down(resolve):
        return INTERVAL_SECONDS, (
            f"{consecutive_failures} consecutive failed cycles, but the feed "
            f"hostname does not resolve -- this machine is offline, not the "
            f"upstream. Holding the normal {INTERVAL_SECONDS}s cadence so the "
            f"first cycle after reconnect is not delayed by the backoff."
        )
    return FAILURE_BACKOFF_SECONDS, (
        f"{consecutive_failures} consecutive failed cycles and the feed "
        f"hostname resolves, so the upstream looks unwell; "
        f"next attempt in {FAILURE_BACKOFF_SECONDS}s"
    )


_lost_log_writes = 0

# Where a failed log write leaves its trace. A module-level constant rather
# than an inline path so the test suite can redirect it: writing to the real
# sentinel from tests would make every subsequent session report failures that
# never happened, and a diagnostic nobody trusts is worse than none. Read by
# the SessionStart hook, which is what makes the evidence reach a human without
# needing the (possibly frozen) poller to still be alive.
LOG_FAILURE_SENTINEL = os.path.join(tempfile.gettempdir(),
                                    "aussie_health_log_failures.txt")


def _record_lost_write(exc: BaseException) -> None:
    """Leave a trace somewhere else when the primary log cannot be written.

    Deliberately a different directory: if `%LOCALAPPDATA%\\AussieHealth` is
    what is unwritable, a fallback inside it would fail the same way. Itself
    best-effort -- if even this fails there is nothing further to try, and
    raising here would kill the loop over a diagnostic.
    """
    try:
        with open(LOG_FAILURE_SENTINEL, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat()} pid={os.getpid()} "
                     f"could not write {LOOP_LOG}: {exc!r}\n")
    except OSError:
        pass


def log(msg: str) -> None:
    """Append one line to the loop log. Never raises.

    Swallowing the error is still right -- a diagnostic must not kill the
    pipeline -- but swallowing it *silently* cost two multi-hour
    investigations on 06-07/08/2026. A running loop whose writes failed was
    indistinguishable from a process that died before its first statement,
    and the wrong one of those was assumed for days. So a failure now leaves
    evidence in two places: a sentinel file outside this directory, and a
    count that is reported the moment logging recovers.
    """
    global _lost_log_writes
    try:
        with open(LOOP_LOG, "a", encoding="utf-8") as fh:
            if _lost_log_writes:
                fh.write(f"{datetime.now().isoformat()} WARNING "
                         f"{_lost_log_writes} earlier log write(s) failed "
                         f"silently; see {LOG_FAILURE_SENTINEL}\n")
                _lost_log_writes = 0
            fh.write(f"{datetime.now().isoformat()} {msg}\n")
    except OSError as exc:
        _lost_log_writes += 1
        _record_lost_write(exc)


def main() -> int:
    os.makedirs(AH, exist_ok=True)  # fresh machine: log dir must exist

    # Single-instance guard held for the loop's lifetime. use_last_error
    # snapshots the status reliably; plain windll can have the thread's
    # last-error clobbered by ctypes' own intervening Win32 calls (e.g.
    # the GetProcAddress behind first attribute access). A NULL handle is
    # also handled: ERROR_ACCESS_DENIED means the mutex already exists
    # under another account, which still means "someone else is polling".
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = (ctypes.wintypes.LPVOID,
                                      ctypes.wintypes.BOOL,
                                      ctypes.wintypes.LPCWSTR)
    handle = kernel32.CreateMutexW(None, False,
                                   "Global\\AussieHealthWaitsPollLoop")
    err = ctypes.get_last_error()
    if handle and err == ERROR_ALREADY_EXISTS:
        log("another loop instance already running; exiting")
        return 0
    if not handle:
        if err == ERROR_ACCESS_DENIED:
            log("loop mutex held by another session; exiting")
            return 0
        log(f"CreateMutexW failed (winerror {err}); continuing WITHOUT "
            "the single-instance guard")

    log(f"loop start pid={os.getpid()} interval={INTERVAL_SECONDS}s")
    consecutive_failures = 0
    while True:
        started = time.time()
        try:
            result = subprocess.run([sys.executable, POLL_ONCE],
                                    creationflags=CREATE_NO_WINDOW,
                                    timeout=CYCLE_TIMEOUT_SECONDS)
            log(f"cycle exit={result.returncode}")
            failed = result.returncode != 0
        except subprocess.TimeoutExpired:
            log(f"cycle killed after {CYCLE_TIMEOUT_SECONDS}s timeout")
            failed = True
        except Exception as exc:  # noqa: BLE001 - log and keep looping
            log(f"cycle error: {exc}")
            failed = True

        consecutive_failures = 0 if not failed else consecutive_failures + 1
        interval, note = next_interval(consecutive_failures)
        if note:
            log(note)
        wait_for_next_cycle(started, interval)


if __name__ == "__main__":
    sys.exit(main())
