"""The dashboard server.

    python -m reconproof.serve

One command, one process, no build step: FastAPI serves the JSON and the
static files in `web/` straight from disk. There is no bundler and no Node,
because a judge should not need a toolchain to see the demo and a committed
`dist/` nobody can rebuild is worse than no `dist/` at all.

This module adds no reconciliation logic. Every endpoint calls the same
`run_pipeline` / `apply_tamper` / `reverify` that the CLI calls, so what the
browser shows is what `python -m reconproof.run` prints. In particular
`/api/tamper` really corrupts a source record and really re-runs `verify()` —
the stamp that flips in the browser flips because the arithmetic stopped
holding, not because a button said so.
"""

from __future__ import annotations

import argparse
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .confidence import DEFAULT_ABSTAIN_BELOW
from .generate import generate
from .ingest import load
from .models import MissingRecord
from .pipeline import apply_tamper, reverify, run_pipeline
from .report import build_ledger, build_scorecard, load_ground_truth

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "web"
DATA_DIR = REPO_ROOT / "data" / "generated"
TRUTH_PATH = REPO_ROOT / "data" / "ground_truth.json"


class TamperRequest(BaseModel):
    record_id: str
    delta_paise: int = Field(default=100, description="how far to shift the amount")


class RerunRequest(BaseModel):
    abstain_below: float = DEFAULT_ABSTAIN_BELOW
    tolerance_paise: int = 0


class Session:
    """One batch, its pipeline result, and whatever has been tampered with.

    Held in module state because this is a single-user demo server. A lock
    guards it so that a double-click on the tamper button cannot interleave a
    mutation with a re-verification.
    """

    def __init__(self, seed: int = 42, difficulty: str = "standard") -> None:
        self.seed = seed
        self.difficulty = difficulty
        self.abstain_below = DEFAULT_ABSTAIN_BELOW
        self.tolerance_paise = 0
        self.tampered: list[dict] = []
        self._lock = threading.Lock()
        self.reload(regenerate=not DATA_DIR.exists())

    # -- lifecycle --------------------------------------------------------

    def reload(self, *, regenerate: bool = False) -> None:
        """Read the batch from disk and reconcile it from scratch.

        Also the restore path: the tampered amounts only ever lived in
        memory, so re-reading the CSVs is a genuine restore rather than an
        inverse edit that could drift.
        """
        if regenerate:
            generate(
                seed=self.seed,
                difficulty=self.difficulty,
                data_dir=DATA_DIR,
                truth_path=TRUTH_PATH,
            )
        self.store = load(DATA_DIR)
        self.truth = load_ground_truth(TRUTH_PATH)
        self.tampered = []
        self.result = run_pipeline(
            self.store,
            abstain_below=self.abstain_below,
            tolerance_paise=self.tolerance_paise,
        )

    def rerun(self, abstain_below: float, tolerance_paise: int) -> None:
        self.abstain_below = abstain_below
        self.tolerance_paise = tolerance_paise
        self.result = run_pipeline(
            self.store,
            abstain_below=abstain_below,
            tolerance_paise=tolerance_paise,
        )

    def tamper(self, record_id: str, delta_paise: int) -> dict:
        """Corrupt one source amount, then re-verify the existing proofs.

        `reverify` deliberately does not rebuild the proofs. They stay exactly
        as they were built, and only the verifier's reading of the source
        data changes - which is the whole claim being demonstrated.
        """
        change = apply_tamper(self.store, record_id, delta_paise)
        self.tampered.append(change)
        self.result = reverify(
            self.result,
            self.store,
            abstain_below=self.abstain_below,
            tolerance_paise=self.tolerance_paise,
        )
        return change

    # -- serialisation ----------------------------------------------------

    def payload(self) -> dict:
        scorecard = build_scorecard(
            self.result,
            self.truth,
            seed=self.seed,
            difficulty=self.difficulty,
            tampered=self.tampered[-1]["record_id"] if self.tampered else None,
        )
        return {
            "run": scorecard.run,
            "totals": scorecard.totals,
            "metrics": scorecard.metrics,
            "by_case_type": scorecard.by_case,
            "calibration": scorecard.calibration,
            "exception_taxonomy": scorecard.exception_taxonomy,
            "llm": scorecard.llm,
            "loop_b": scorecard.loop_b,
            "subset_cap_hits": self.result.candidate_set.cap_hits,
            "ledger": build_ledger(self.result, self.truth),
            "proofs": [proof.model_dump(mode="json") for proof in self.result.proofs],
            "accepted_proof_ids": sorted(p.proof_id for p in self.result.accepted),
            "exceptions": [e.model_dump(mode="json") for e in self.result.exceptions],
            "tampered": self.tampered,
            "params": {
                "abstain_below": self.abstain_below,
                "tolerance_paise": self.tolerance_paise,
            },
        }

    @property
    def lock(self) -> threading.Lock:
        return self._lock


session = Session()
app = FastAPI(title="ReconProof", docs_url="/api/docs", redoc_url=None)


@app.get("/api/report")
def get_report() -> JSONResponse:
    with session.lock:
        return JSONResponse(session.payload())


@app.post("/api/rerun")
def post_rerun(request: RerunRequest) -> JSONResponse:
    with session.lock:
        session.rerun(request.abstain_below, request.tolerance_paise)
        return JSONResponse(session.payload())


@app.post("/api/tamper")
def post_tamper(request: TamperRequest) -> JSONResponse:
    with session.lock:
        try:
            change = session.tamper(request.record_id, request.delta_paise)
        except MissingRecord:
            raise HTTPException(404, f"no such record: {request.record_id}") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        payload = session.payload()
        payload["last_change"] = change
        return JSONResponse(payload)


@app.post("/api/restore")
def post_restore() -> JSONResponse:
    """Re-read the committed CSVs. Undoes every tamper in one step."""
    with session.lock:
        session.reload()
        return JSONResponse(session.payload())


@app.get("/api/record/{record_id}")
def get_record(record_id: str) -> JSONResponse:
    """The raw source record behind an ID in a derivation.

    Every ID in the proof panel links here. Traceability is the product: a
    derivation you cannot drill into is just a nicer-looking assertion.
    """
    with session.lock:
        try:
            record = session.store.any_record(record_id)
        except MissingRecord:
            raise HTTPException(404, f"no such record: {record_id}") from None
        tampered = [t for t in session.tampered if t["record_id"] == record_id]
        return JSONResponse(
            {
                "record_id": record_id,
                "kind": type(record).__name__,
                "fields": record.model_dump(mode="json"),
                "tampered": tampered[-1] if tampered else None,
            }
        )


# Mounted last so that nothing here can shadow an /api route.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m reconproof.serve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    import uvicorn

    print(f"ReconProof dashboard on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
