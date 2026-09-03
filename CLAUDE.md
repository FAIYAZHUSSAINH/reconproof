# CLAUDE.md — ReconProof

> Read automatically at the start of every Claude Code session.
> Single source of truth for what this project is and what the rules are.
> If a task conflicts with this file, this file wins — ask before deviating.

---

## What we are building

**ReconProof** — a settlement-reconciliation agent that **proves** every match it makes,
**quantifies** how sure it is, and **abstains** when it cannot prove the answer.

Built for the **Razorpay AI Buildathon, Track 04 — AI Finance Controller.**

The track brief, verbatim:

> Build an agent that closes one finance-ops loop across a 50+ record batch of synthetic
> data, reporting its match rate and the exceptions it could not resolve.

The track bar, verbatim:

> Throughput plus measured accuracy plus an honest exception list.
> One cherry-picked match proves nothing.

The track's "why now", verbatim:

> The 2026 builder consensus: verification capacity, not generation speed, is the
> bottleneck. Reconciliation, settlement and forecasting are still done by hand.

## The one idea this project exists to demonstrate

**The LLM proposes. The verifier disposes.**

An LLM is never allowed to declare a match. It may only *hypothesise* — "I think this
₹412 residual is a delayed refund on payment pay_0031." That hypothesis is converted
into a **proof object**, and a **deterministic, LLM-free Python verifier** independently
re-reads the raw source records and recomputes the arithmetic to the paisa. If it does
not balance, the match is rejected and becomes a typed exception.

This makes the system *structurally incapable of faking a match*. That is the entire
pitch. Every design decision must protect this property.

If you are ever about to write code where an LLM's output is trusted without a
deterministic recheck, **stop and flag it.**

## Non-negotiable engineering rules

1. **No floats. Ever.** All money is `int` paise. A float in a money path is a bug,
   not a style preference. `money.py` defines the type and the only allowed operations.
2. **The verifier imports nothing from the matcher.** `verify.py` must not import
   `candidates.py`, `proof.py`, `adjudicate.py`, or any LLM code. It takes a proof plus
   the raw records and recomputes from scratch. It trusts no field inside the proof.
   `tests/test_trust_boundary.py` enforces this by parsing the import graph.
3. **The verifier re-reads source records by ID.** If a proof claims `pay_0031` is a
   member, the verifier fetches `pay_0031` from the raw data and uses *that* amount —
   never the amount cached in the proof.
4. **`--no-llm` must always work.** A judge cloning this repo with no API key must get
   a complete run with a full scorecard. LLM adjudication is an *enhancement layer*
   that raises the match rate; it is never load-bearing.
5. **Deterministic and seeded.** `--seed 42` must reproduce byte-identical data and an
   identical scorecard. A judge running the repo must see the numbers in `RESULTS.md`.
6. **Abstention is a success, not a failure.** An honest exception beats a wrong match.
   The false-match rate must be **0**. If a change raises match rate but produces one
   false match, revert it.
7. **Every exception is typed.** Never emit a bare "could not match". Emit a reason
   code, the residual in paise, the records involved, and what a human should check.

## Repo layout

```
reconproof/
├── CLAUDE.md              → this file
├── README.md              → judge-facing. Run instructions in the first 20 lines.
├── RESULTS.md             → the committed scorecard from a seeded run
├── ARCHITECTURE.md        → the pipeline, the trust boundary, design decisions
├── WHAT_BROKE.md          → running engineering log
├── docs/                  → the build specs this repo was executed against
├── data/generated/        → committed, seeded, so results are reproducible
├── src/reconproof/
│   ├── money.py           → Paise type. No floats.
│   ├── models.py          → pydantic records + the Proof schema + RecordStore
│   ├── generate.py        → synthetic batch with 13 labelled hard cases
│   ├── ingest.py          → CSV → records, integer-checked at the boundary
│   ├── candidates.py      → deterministic candidate generation (R1–R4)
│   ├── proof.py           → proof construction
│   ├── verify.py          → THE VERIFIER. Pure. No LLM. No matcher imports.
│   ├── confidence.py      → scoring + abstention thresholds
│   ├── adjudicate.py      → LLM hypothesis proposer (optional layer)
│   ├── pipeline.py        → orchestration shared by the CLI and the server
│   ├── report.py          → scorecard, RESULTS.md, report.json
│   ├── run.py             → CLI entry point
│   └── serve.py           → Phase 2 FastAPI
├── tests/
└── web/                   → Phase 2 dashboard (no build step, no Node)
```

## The two phases

- **Phase 1 (`docs/PHASE_1_BUILD.md`)** — a working, measured, tested command-line
  system. Ends when `python -m reconproof.run --no-llm --seed 42` prints a real
  scorecard and `pytest` is green.
- **Phase 2 (`docs/PHASE_2_POLISH.md`)** — the dashboard, the tamper demo, the docs,
  the video. Phase 2 makes the work *legible*; it adds no reconciliation features.

**Scope discipline:** if a Phase 2 idea requires changing the reconciliation core, it is
out of scope. Write it in `IDEAS.md` and move on.

## WHAT_BROKE.md — keep it from the first commit

The application form asks *"What broke, and how you got out"* and states that this is
the field they read first. Every time something genuinely breaks — a wrong assumption,
a silent bug, a bad abstraction — append a dated entry with the symptom, the diagnosis,
and the fix. Write it while it is happening, not from memory on the last day.

## Working style

- Build in the order given in `docs/PHASE_1_BUILD.md`. Do not skip ahead.
- After each step, run the tests and show the output before continuing.
- Short explanatory comments on the non-obvious lines — this code gets defended live to
  an engineering panel, so every branch has to be explainable.
- Prefer boring stdlib over a dependency. Every dependency has to be justified out loud.
- Commit in small, well-named increments. The commit history is part of the submission.
