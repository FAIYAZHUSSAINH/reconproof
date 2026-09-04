# One command, Windows first.
#
#   .\run.ps1                       deterministic run, writes report.json + RESULTS.md
#   .\run.ps1 --tamper pay_0031     corrupt a record and watch a proof flip to FAIL
#   .\run.ps1 --difficulty hard     more hard cases, and a lower match rate
#
# Sets PYTHONPATH instead of requiring an editable install, so a judge can
# clone and run without pip touching the environment beyond requirements.txt.
$env:PYTHONPATH = "src"
python -m reconproof.run @args
