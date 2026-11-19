# Runs the whole rdp-reader suite and reports wall time (budget: 10 minutes).
param([string]$Python = 'C:\Users\Studio\AppData\Local\Programs\Python\Python312\python.exe')
# unittest reports on stderr; keep PowerShell 5.1 from turning that into a terminating error.
$ErrorActionPreference = 'Continue'
Push-Location $PSScriptRoot
try {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    cmd.exe /d /c "`"$Python`" -m unittest discover -s tests -v 2>&1"
    $code = $LASTEXITCODE
    $sw.Stop()
    "rdp-reader tests: exit=$code, wall time {0:N1} s (budget 600 s)" -f $sw.Elapsed.TotalSeconds
    exit $code
}
finally { Pop-Location }
