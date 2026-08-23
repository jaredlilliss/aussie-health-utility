@echo off
REM Launcher for the AussieHealthWaitsPoll scheduled task.
REM Runs ONE ingest cycle; the task trigger provides the 15-min cadence.
REM
REM Why a .cmd wrapper instead of pointing the task straight at python:
REM Task Scheduler would not launch an Execute path containing a space
REM ("C:\Program Files\Python313\pythonw.exe") -- the action silently did
REM nothing while the task still reported success. cmd.exe lives at a
REM space-free path, launches fine, and quotes the interpreter itself.
REM Uses the ALL-USERS 3.13 interpreter (outside %LOCALAPPDATA%), which is
REM the one Task Scheduler is willing to run.
set "LOG=%LOCALAPPDATA%\AussieHealth\run_poll.log"
echo [%DATE% %TIME%] launcher start >> "%LOG%"
"C:\Program Files\Python313\pythonw.exe" "C:\Users\Xi\OneDrive\Desktop\cc\project\Aussie_Health_Docs_v2\scripts\poll_waits.py" >> "%LOG%" 2>&1
echo [%DATE% %TIME%] exit=%ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
