# Same three things as run.ps1, for a POSIX shell.
export PYTHONPATH := src

run:
	python -m reconproof.run --no-llm --seed 42

test:
	python -m pytest -q

serve:
	python -m reconproof.serve

.PHONY: run test serve
