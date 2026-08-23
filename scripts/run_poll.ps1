# Launcher for the AussieHealthWaitsPoll scheduled task, via powershell.exe.
#
# Why powershell.exe: Task Scheduler here launches cmd.exe fine but silently
# no-op'd five other forms (see the machine-env memory). powershell.exe is a
# real PE binary on a space-free System32 path -- the same properties cmd.exe
# has -- so CreateProcess should accept it identically.
#
# Why every path below is hardcoded: %LOCALAPPDATA% may be absent from the
# task's environment block. poll_waits.py does os.environ["LOCALAPPDATA"] at
# import time, so an unset variable is a KeyError and exit 1 *before* the
# heartbeat write -- which is precisely the reported symptom. run_poll.cmd
# redirects into "%LOCALAPPDATA%\..." too, so the same gap would kill it
# silently. This script must not be able to fail the same way.

$base   = 'C:\Users\Xi\AppData\Local\AussieHealth'
$log    = Join-Path $base 'run_poll_ps.log'
$py     = 'C:\Program Files\Python313\python.exe'
$script = 'C:\Users\Xi\OneDrive\Desktop\cc\project\Aussie_Health_Docs_v2\scripts\poll_waits.py'

function Note($m) {
    Add-Content -Path $log -Value ("[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $m) -Encoding utf8
}

function Or-Unset($v) { if ($v) { $v } else { '<UNSET>' } }

Note "--- action start (pid $PID, user $env:USERNAME) ---"
Note ("LOCALAPPDATA={0}" -f (Or-Unset $env:LOCALAPPDATA))
Note ("USERPROFILE={0}"  -f (Or-Unset $env:USERPROFILE))
Note ("cwd={0}"          -f (Get-Location).Path)

# Preserve the wake-nudge signal the previous action provided, so anything
# watching wake.log keeps working while this form is on trial.
Add-Content -Path (Join-Path $base 'wake.log') `
    -Value ("[{0:ddd dd/MM/yyyy HH:mm:ss.ff}] wake (ps)" -f (Get-Date)) -Encoding utf8

if (-not $env:LOCALAPPDATA) {
    $env:LOCALAPPDATA = 'C:\Users\Xi\AppData\Local'
    Note 'LOCALAPPDATA was UNSET -- injected for the child process'
}

if (-not (Test-Path $py))     { Note "FATAL: interpreter missing at $py"; exit 2 }
if (-not (Test-Path $script)) { Note "FATAL: script missing at $script";  exit 3 }

$out = Join-Path $base 'run_poll_ps.out'
$err = Join-Path $base 'run_poll_ps.err'

try {
    $p = Start-Process -FilePath $py -ArgumentList "`"$script`"" `
        -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $out -RedirectStandardError $err
    Note ("python exit={0}" -f $p.ExitCode)

    # The traceback this investigation has never been able to see.
    $stderr = Get-Content $err -Raw -ErrorAction SilentlyContinue
    if ($stderr -and $stderr.Trim()) {
        Note ("stderr: {0}" -f (($stderr.Trim() -replace "`r?`n", ' | ')))
    }
    exit $p.ExitCode
}
catch {
    Note ("LAUNCH FAILED: {0}" -f $_.Exception.Message)
    exit 4
}
