# One command, Windows first.
#
#   .\run.ps1                       deterministic run, writes report.json + RESULTS.md
#   .\run.ps1 --tamper pay_0031     corrupt a record and watch a proof flip to FAIL
#   .\run.ps1 --difficulty hard     more hard cases, and a lower match rate
#
# Sets PYTHONPATH rather than requiring an editable install, so a clone runs
# without pip touching the environment beyond requirements.txt. Prefers the
# repo's own .venv if there is one, because forgetting to activate it is the
# most likely way for this to fail on someone else's machine.

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$env:PYTHONPATH = Join-Path $root "src"

$venv = Join-Path $root ".venv\Scripts\python.exe"
$python = if (Test-Path $venv) { $venv } else { "python" }

& $python -m reconproof.run @args
exit $LASTEXITCODE
