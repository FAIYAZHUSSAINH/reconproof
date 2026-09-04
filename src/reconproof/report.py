"""Scoring and the scorecard.

This is the only module that opens `ground_truth.json`. Nothing upstream of it
can see a label, which is what makes the numbers below evidence rather than
decoration.

The metric that matters is not the match rate. It is the false-match rate,
and it has to be zero: a match is only counted correct when the accepted proof
names exactly the same credits, settlements and line items as the answer key.
A proof that balances against the wrong member set is a false match even
though its arithmetic came out at zero.
"""

from __future__ import annotations

import json
import platform
import tempfile
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .models import CASE_TYPES, ExceptionRecord, Proof
from .money import rupees
from .pipeline import PipelineResult

CONFIDENCE_BUCKETS = (
    (0.00, 0.50),
    (0.50, 0.70),
    (0.70, 0.85),
    (0.85, 0.95),
    (0.95, 1.01),
)


@dataclass
class GroundTruth:
    seed: int
    difficulty: str
    matches: list[dict]
    unmatched_settlements: list[dict]
    unmatched_bank_txns: list[dict]
    bank_reversal_pairs: list[list[str]]
    loop_b: list[dict]

    @property
    def units(self) -> list[dict]:
        """Every labelled thing the system is judged on."""
        return self.matches

    def unit_key(self, entry: dict) -> tuple[frozenset[str], frozenset[str]]:
        return frozenset(entry["bank_txn_ids"]), frozenset(entry["settlement_ids"])

    def case_of_bank_txn(self, txn_id: str) -> str | None:
        for entry in self.matches:
            if txn_id in entry["bank_txn_ids"]:
                return entry["case_type"]
        for entry in self.unmatched_bank_txns:
            if entry["bank_txn_id"] == txn_id:
                return entry["case_type"]
        return None

    def case_of_settlement(self, settlement_id: str) -> str | None:
        for entry in self.matches:
            if settlement_id in entry["settlement_ids"]:
                return entry["case_type"]
        for entry in self.unmatched_settlements:
            if entry["settlement_id"] == settlement_id:
                return entry["case_type"]
        return None


def load_ground_truth(path: Path | str = Path("data/ground_truth.json")) -> GroundTruth:
    with Path(path).open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return GroundTruth(
        seed=payload["seed"],
        difficulty=payload["difficulty"],
        matches=payload["matches"],
        unmatched_settlements=payload["unmatched_settlements"],
        unmatched_bank_txns=payload["unmatched_bank_txns"],
        bank_reversal_pairs=payload["bank_reversal_pairs"],
        loop_b=payload["loop_b"],
    )


def _proof_key(proof: Proof) -> tuple[frozenset[str], frozenset[str]]:
    return (
        frozenset(proof.members.get("bank_txn_ids", [])),
        frozenset(proof.members.get("settlement_ids", [])),
    )


def _members_match(proof: Proof, entry: dict) -> bool:
    """Same credits, same settlements, same line items. No partial credit."""
    for key, truth_key in (
        ("payment_ids", "payment_ids"),
        ("refund_ids", "refund_ids"),
        ("adjustment_ids", "adjustment_ids"),
    ):
        if set(proof.members.get(key, [])) != set(entry[truth_key]):
            return False
    return True


def is_correct(proof: Proof, truth: GroundTruth) -> bool:
    key = _proof_key(proof)
    for entry in truth.matches:
        if truth.unit_key(entry) == key:
            return _members_match(proof, entry)
    return False


@dataclass
class Scorecard:
    run: dict = field(default_factory=dict)
    totals: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    by_case: dict = field(default_factory=dict)
    calibration: list[dict] = field(default_factory=list)
    exception_taxonomy: list[dict] = field(default_factory=list)
    llm: dict = field(default_factory=dict)
    loop_b: dict = field(default_factory=dict)


def build_scorecard(
    result: PipelineResult,
    truth: GroundTruth,
    *,
    seed: int,
    difficulty: str,
    tampered: str | None = None,
) -> Scorecard:
    store = result.store
    neutralised = result.candidate_set.neutralised_ids

    # A credit is "live" if it is real money that should reconcile: positive,
    # and not one half of a same-day post-and-reverse pair. The neutralised
    # pairs are reported separately rather than quietly dropped.
    live_credits = [
        txn
        for txn in store.bank_txns.values()
        if txn.credit_amount > 0 and txn.bank_txn_id not in neutralised
    ]
    live_ids = {txn.bank_txn_id for txn in live_credits}
    covered = result.accepted_txn_ids & live_ids

    accepted = result.accepted
    correct = [proof for proof in accepted if is_correct(proof, truth)]
    false_matches = [proof for proof in accepted if proof not in correct]

    total_value = sum(txn.credit_amount for txn in live_credits)
    covered_value = sum(store.bank_txn(txn_id).credit_amount for txn_id in covered)

    settlements_closed = result.accepted_settlement_ids
    by_case = _by_case(result, truth)
    labelled_units = sum(row["units"] for row in by_case.values())
    unmatched_units = sum(row["exceptions"] for row in by_case.values())
    correct_diagnoses = sum(row["correctly_diagnosed"] for row in by_case.values())
    handled = sum(row["handled_correctly"] for row in by_case.values())

    metrics = {
        "auto_match_rate_records": _rate(len(covered), len(live_ids)),
        "matched_credits": len(covered),
        "live_credits": len(live_ids),
        "settlement_closure_rate": _rate(len(settlements_closed), len(store.settlements)),
        "settlements_closed": len(settlements_closed),
        "settlements_total": len(store.settlements),
        "value_coverage": _rate(covered_value, total_value),
        "value_matched_paise": covered_value,
        "value_total_paise": total_value,
        "false_match_rate": _rate(len(false_matches), len(accepted)),
        "false_matches": len(false_matches),
        "false_match_ids": sorted(p.proof_id for p in false_matches),
        "accepted_matches": len(accepted),
        "exceptions": len(result.exceptions),
        "exception_value_at_stake_paise": sum(e.at_stake for e in result.exceptions),
        "exception_coverage": _exception_coverage(result, live_ids, covered),
        "bank_reversal_pairs": len(result.candidate_set.neutralised_pairs),
        "subset_cap_hits": len(result.candidate_set.cap_hits),
        "candidates_generated": len(result.candidate_set.candidates),
        "proofs_verified": len(result.proofs),
        "proofs_passed": sum(1 for p in result.proofs if p.verdict == "PASS"),
        "proofs_failed": sum(1 for p in result.proofs if p.verdict == "FAIL"),
        # Of the labelled units it could not match, how often did it name the
        # right reason? An exception queue is only useful if it is right.
        "labelled_units": labelled_units,
        "unmatched_units": unmatched_units,
        "correct_diagnoses": correct_diagnoses,
        "diagnosis_accuracy": _rate(correct_diagnoses, unmatched_units),
        "handled_correctly": handled,
        "handled_rate": _rate(handled, labelled_units),
    }

    throughput = {
        "records": store.total_records,
        "wall_clock_seconds": round(result.elapsed_seconds, 3),
        "records_per_second": (
            round(store.total_records / result.elapsed_seconds, 1)
            if result.elapsed_seconds > 0
            else None
        ),
    }

    scorecard = Scorecard(
        run={
            "seed": seed,
            "difficulty": difficulty,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "python": platform.python_version(),
            "platform": platform.system(),
            # Flags only. sys.argv[0] is an absolute path, and a home
            # directory has no business in a committed artefact.
            "flags": " ".join(sys.argv[1:]),
            "tampered_record": tampered,
            **result.params,
        },
        totals={**store.counts(), "records": store.total_records, **throughput},
        metrics=metrics,
        by_case=by_case,
        calibration=_calibration(result, truth),
        exception_taxonomy=_taxonomy(result.exceptions),
        llm=_llm_summary(result),
        loop_b=_loop_b_summary(result, truth),
    )
    return scorecard


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def _exception_coverage(result: PipelineResult, live_ids: set[str], covered: set[str]) -> float:
    """Did every unmatched credit get a typed exception?

    Anything less than 1.0 means the system dropped something on the floor,
    which is worse than a low match rate: it is a silent gap.
    """
    unmatched = live_ids - covered
    explained = {
        exception.subject_id
        for exception in result.exceptions
        if exception.subject_kind == "bank_txn"
    }
    return _rate(len(unmatched & explained), len(unmatched))


# When the system does not match a labelled unit, this is the exception it
# should have filed. Getting the *diagnosis* right on something you could not
# match is a separate skill from matching, and the track brief asks for it
# explicitly: "the exceptions it could not resolve".
EXPECTED_EXCEPTION_WHEN_UNMATCHED = {
    "MISSING_BANK_CREDIT": "MISSING_BANK_CREDIT",
    "ORPHAN_CREDIT": "ORPHAN_CREDIT",
    "SHORT_PAYMENT": "SHORT_PAYMENT",
    "AMOUNT_COLLISION": "AMBIGUOUS_MATCH",
    "ROUNDING_DRIFT": "ROUNDING_DRIFT",
    "UNLABELLED_REVERSAL": "SUSPECT_UNLABELLED_REVERSAL",
    "WITH_REFUND": "SUSPECT_UNAPPLIED_REFUND",
}

# Case types where an exception is the right answer, not a shortfall. A bank
# credit from a customer's own account has no settlement behind it; a
# settlement the bank has not paid yet has no credit. Reporting those is the
# correct outcome, so they are shown separately from a genuine miss.
EXCEPTION_IS_CORRECT = {"MISSING_BANK_CREDIT", "ORPHAN_CREDIT", "SHORT_PAYMENT"}


def _blank_row() -> dict:
    return {
        "units": 0,
        "matched": 0,
        "exceptions": 0,
        "correctly_diagnosed": 0,
        "expected_outcome": "match",
    }


def _by_case(result: PipelineResult, truth: GroundTruth) -> dict:
    """Per labelled case type: what was matched, and what was diagnosed.

    Two different questions in one table. "Matched" is how often the system
    proved the answer. "Correctly diagnosed" is how often, having refused to
    match, it still said the right thing about why - which is the number that
    tells a finance team whether the exception queue is worth reading.
    """
    accepted_keys = {_proof_key(proof) for proof in result.accepted}
    exception_reasons = {e.subject_id: e.reason_code for e in result.exceptions}
    loop_b_by_order = {finding.order_id: finding for finding in result.loop_b}

    table: dict[str, dict] = {}

    def row_for(case: str) -> dict:
        return table.setdefault(case, _blank_row())

    def diagnose(case: str, subject_ids: list[str], row: dict) -> None:
        expected = EXPECTED_EXCEPTION_WHEN_UNMATCHED.get(case)
        raised = next(
            (exception_reasons[s] for s in subject_ids if s in exception_reasons), None
        )
        if raised is not None and raised == expected:
            row["correctly_diagnosed"] += 1

    for entry in truth.matches:
        case = entry["case_type"]
        row = row_for(case)
        row["units"] += 1
        if truth.unit_key(entry) in accepted_keys:
            row["matched"] += 1
        else:
            row["exceptions"] += 1
            diagnose(case, entry["bank_txn_ids"], row)

    for entry in truth.unmatched_settlements:
        row = row_for(entry["case_type"])
        row["expected_outcome"] = "exception"
        row["units"] += 1
        row["exceptions"] += 1
        diagnose(entry["case_type"], [entry["settlement_id"]], row)

    for entry in truth.unmatched_bank_txns:
        row = row_for(entry["case_type"])
        row["expected_outcome"] = "exception"
        row["units"] += 1
        row["exceptions"] += 1
        diagnose(entry["case_type"], [entry["bank_txn_id"]], row)

    for entry in truth.loop_b:
        case = entry["case_type"]
        row = row_for(case)
        row["units"] += 1
        finding = loop_b_by_order.get(entry["order_id"])
        if case == "TDS_SHORT_PAYMENT":
            # Loop B "matches" a short payment by explaining it, not by
            # pairing it with anything.
            if finding is not None and finding.verdict == "TDS_WITHHELD":
                row["matched"] += 1
            else:
                row["exceptions"] += 1
        else:
            row["expected_outcome"] = "exception"
            row["exceptions"] += 1
            diagnose(case, [entry["order_id"]], row)

    for case in CASE_TYPES:
        if case in EXCEPTION_IS_CORRECT:
            row_for(case)["expected_outcome"] = "exception"

    for row in table.values():
        row["match_rate"] = _rate(row["matched"], row["units"])
        row["diagnosis_rate"] = _rate(row["correctly_diagnosed"], row["exceptions"])
        # What the row is worth overall: a match where a match was possible,
        # a correct exception where it was not.
        resolved = row["matched"] + row["correctly_diagnosed"]
        row["handled_correctly"] = resolved
        row["handled_rate"] = _rate(resolved, row["units"])
    return {case: row for case, row in sorted(table.items()) if row["units"]}


def _calibration(result: PipelineResult, truth: GroundTruth) -> list[dict]:
    """Confidence bucket against how often that confidence was right.

    Computed over every proof that *passed verification*, not just the ones
    that were accepted - including the ones the threshold rejected. That is
    what makes the table an argument for the threshold rather than a
    restatement of it: the bucket at the abstention cap is right about half
    the time, which is exactly what an ambiguous amount collision should look
    like.
    """
    passing = [proof for proof in result.proofs if proof.verdict == "PASS"]
    rows: list[dict] = []
    for low, high in CONFIDENCE_BUCKETS:
        bucket = [p for p in passing if low <= p.confidence < high]
        if not bucket:
            continue
        correct = sum(1 for proof in bucket if is_correct(proof, truth))
        rows.append(
            {
                "bucket": f"{low:.2f}-{min(high, 1.0):.2f}",
                "proofs": len(bucket),
                "correct": correct,
                "accuracy": _rate(correct, len(bucket)),
                "accepted": all(
                    p.confidence >= result.params.get("abstain_below", 0.75) for p in bucket
                ),
            }
        )
    return rows


def _taxonomy(exceptions: list[ExceptionRecord]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for exception in exceptions:
        row = grouped.setdefault(
            exception.reason_code, {"reason_code": exception.reason_code, "count": 0, "at_stake_paise": 0}
        )
        row["count"] += 1
        row["at_stake_paise"] += exception.at_stake
    return sorted(grouped.values(), key=lambda row: (-row["at_stake_paise"], row["reason_code"]))


def _llm_summary(result: PipelineResult) -> dict:
    log = result.llm_hypotheses
    accepted = sum(1 for entry in log if entry["outcome"] == "accepted_by_verifier")
    rejected = sum(1 for entry in log if entry["outcome"].startswith("rejected"))
    return {
        "enabled": bool(result.params.get("llm")),
        "proposed": len([entry for entry in log if entry["proposed"]]),
        "accepted_by_verifier": accepted,
        "rejected_by_verifier": rejected,
        "log": log,
    }


def _loop_b_summary(result: PipelineResult, truth: GroundTruth) -> dict:
    findings = result.loop_b
    return {
        "orders_checked": len(result.store.orders),
        "short_payments_found": len(findings),
        "explained_as_tds": sum(1 for f in findings if f.verdict == "TDS_WITHHELD"),
        "unexplained": sum(1 for f in findings if f.verdict == "SHORT_PAYMENT"),
        "overpayments": sum(1 for f in findings if f.verdict == "OVERPAYMENT"),
        "findings": [
            {
                "order_id": f.order_id,
                "ordered_paise": f.ordered,
                "paid_paise": f.paid,
                "shortfall_paise": f.shortfall,
                "verdict": f.verdict,
                "note": f.note,
            }
            for f in findings
        ],
    }


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def write_report_json(
    result: PipelineResult, scorecard: Scorecard, path: Path | str
) -> Path:
    """The full machine-readable record: every proof, every exception."""
    payload = {
        "run": scorecard.run,
        "totals": scorecard.totals,
        "metrics": scorecard.metrics,
        "by_case_type": scorecard.by_case,
        "calibration": scorecard.calibration,
        "exception_taxonomy": scorecard.exception_taxonomy,
        "llm": scorecard.llm,
        "loop_b": scorecard.loop_b,
        "bank_reversal_pairs": [list(pair) for pair in result.candidate_set.neutralised_pairs],
        "subset_cap_hits": result.candidate_set.cap_hits,
        "accepted_proof_ids": sorted(proof.proof_id for proof in result.accepted),
        "proofs": [proof.model_dump(mode="json") for proof in result.proofs],
        "exceptions": [exception.model_dump(mode="json") for exception in result.exceptions],
    }
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return destination


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def sweep_difficulties(seed: int, levels=("easy", "standard", "hard")) -> list[dict]:
    """Re-run the whole pipeline at each difficulty, into a temporary batch.

    Measured rather than asserted: a table of numbers typed into a document is
    a claim, and this one is cheap enough to just recompute. Nothing here
    touches `data/generated`, so the committed batch is never disturbed.
    """
    # Imported here, not at module scope: `report` is imported by `serve`, and
    # a scorecard writer should not drag the generator in with it.
    from .generate import generate
    from .ingest import load
    from .pipeline import run_pipeline

    rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="reconproof-sweep-") as tmp:
        for level in levels:
            root = Path(tmp) / level
            generate(
                seed=seed,
                difficulty=level,
                data_dir=root / "generated",
                truth_path=root / "ground_truth.json",
            )
            result = run_pipeline(load(root / "generated"))
            card = build_scorecard(
                result,
                load_ground_truth(root / "ground_truth.json"),
                seed=seed,
                difficulty=level,
            )
            rows.append(
                {
                    "difficulty": level,
                    "records": card.totals["records"],
                    **{
                        key: card.metrics[key]
                        for key in (
                            "auto_match_rate_records",
                            "matched_credits",
                            "live_credits",
                            "value_coverage",
                            "exceptions",
                            "false_matches",
                        )
                    },
                }
            )
    return rows


def write_results_md(
    result: PipelineResult,
    scorecard: Scorecard,
    path: Path | str,
    sweep: list[dict] | None = None,
) -> Path:
    metrics = scorecard.metrics
    run = scorecard.run
    lines: list[str] = []

    lines.append("# RESULTS")
    lines.append("")
    lines.append(
        f"Committed scorecard for `--seed {run['seed']} --difficulty {run['difficulty']}"
        f"{'' if run.get('llm') else ' --no-llm'}`. Reproduce it with that command; the "
        "generator is seeded, so these numbers are the numbers you will get - "
        "every line below except the throughput row, which is wall-clock "
        "and depends on the machine."
    )
    lines.append("")
    lines.append("## Headline")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(
        f"| Auto-match rate (bank credits) | **{_pct(metrics['auto_match_rate_records'])}** "
        f"({metrics['matched_credits']}/{metrics['live_credits']}) |"
    )
    lines.append(
        f"| Value coverage | **{_pct(metrics['value_coverage'])}** "
        f"({rupees(metrics['value_matched_paise'])} of {rupees(metrics['value_total_paise'])}) |"
    )
    lines.append(
        f"| **False-match rate** | **{_pct(metrics['false_match_rate'])}** "
        f"({metrics['false_matches']} of {metrics['accepted_matches']} accepted) |"
    )
    lines.append(
        f"| Settlements closed | {_pct(metrics['settlement_closure_rate'])} "
        f"({metrics['settlements_closed']}/{metrics['settlements_total']}) |"
    )
    lines.append(
        f"| Exceptions filed | {metrics['exceptions']} "
        f"({rupees(metrics['exception_value_at_stake_paise'])} at stake) |"
    )
    lines.append(
        f"| Exception coverage | {_pct(metrics['exception_coverage'])} "
        "of unmatched credits carry a typed reason |"
    )
    lines.append(
        f"| Diagnosis accuracy | {_pct(metrics['diagnosis_accuracy'])} "
        f"({metrics['correct_diagnoses']}/{metrics['unmatched_units']} unmatched units "
        "given the right reason code) |"
    )
    lines.append(
        f"| Handled correctly | {_pct(metrics['handled_rate'])} "
        f"({metrics['handled_correctly']}/{metrics['labelled_units']} labelled units "
        "either proved or correctly flagged) |"
    )
    lines.append(
        f"| Throughput | {scorecard.totals['records']} records in "
        f"{scorecard.totals['wall_clock_seconds']}s "
        f"({scorecard.totals['records_per_second']} records/sec) |"
    )
    llm = scorecard.llm
    if llm["enabled"]:
        lines.append(
            f"| LLM adjudication | {llm['proposed']} proposed, "
            f"{llm['accepted_by_verifier']} accepted by the verifier, "
            f"{llm['rejected_by_verifier']} rejected |"
        )
    else:
        lines.append("| LLM adjudication | disabled (`--no-llm`) |")
    lines.append("")

    if sweep:
        lines.append("## Across difficulty")
        lines.append("")
        lines.append(
            "One number on one batch proves nothing. These rows are the same "
            "pipeline re-run on `--difficulty easy|standard|hard`, measured "
            "during this run rather than typed in. The match rate falls as the "
            "hard cases multiply; the false-match rate does not move. (The "
            "seed varies amounts, dates and narrations but not the mix of case "
            "types, so varying it gives identical rates - which confirms "
            "determinism and says nothing about robustness.)"
        )
        lines.append("")
        lines.append(
            "| Difficulty | Records | Match rate | Value coverage | Exceptions | False matches |"
        )
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
        for row in sweep:
            marker = " *(this run)*" if row["difficulty"] == run["difficulty"] else ""
            lines.append(
                f"| {row['difficulty']}{marker} | {row['records']} | "
                f"{_pct(row['auto_match_rate_records'])} "
                f"({row['matched_credits']}/{row['live_credits']}) | "
                f"{_pct(row['value_coverage'])} | {row['exceptions']} | "
                f"**{row['false_matches']}** |"
            )
        lines.append("")

    lines.append("## Per case type")
    lines.append("")
    lines.append(
        "The point of this table is the rows at the bottom. Every case type is "
        "injected at least twice, so a low rate here is a statement about the "
        "system, not a sampling artefact."
    )
    lines.append("")
    lines.append(
        "| Case type | Expected | Units | Matched | Exceptions | Correctly diagnosed | Handled |"
    )
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for case, row in sorted(
        scorecard.by_case.items(), key=lambda kv: (-kv[1]["handled_rate"], kv[0])
    ):
        lines.append(
            f"| {case} | {row['expected_outcome']} | {row['units']} | {row['matched']} | "
            f"{row['exceptions']} | {row['correctly_diagnosed']} | "
            f"{_pct(row['handled_rate'])} |"
        )
    lines.append("")

    lines.append("## Exceptions by reason")
    lines.append("")
    lines.append("| Reason code | Count | At stake |")
    lines.append("| --- | ---: | ---: |")
    for row in scorecard.exception_taxonomy:
        lines.append(
            f"| {row['reason_code']} | {row['count']} | {rupees(row['at_stake_paise'])} |"
        )
    lines.append("")

    lines.append("## The exception list")
    lines.append("")
    lines.append("Sorted by rupees at stake. This is the queue a human works through.")
    lines.append("")
    lines.append("| Subject | Reason | Residual | At stake | What to check |")
    lines.append("| --- | --- | ---: | ---: | --- |")
    for exception in result.exceptions:
        lines.append(
            f"| `{exception.subject_id}` | {exception.reason_code} | "
            f"{rupees(exception.residual)} | {rupees(exception.at_stake)} | "
            f"{exception.what_to_check} |"
        )
    lines.append("")

    lines.append("## Calibration")
    lines.append("")
    lines.append(
        "Every proof that balanced, bucketed by confidence, against whether it "
        "was actually right. The bucket at the abstention cap is the argument "
        "for abstaining."
    )
    lines.append("")
    lines.append("| Confidence | Proofs | Correct | Accuracy | Accepted |")
    lines.append("| --- | ---: | ---: | ---: | --- |")
    for row in scorecard.calibration:
        lines.append(
            f"| {row['bucket']} | {row['proofs']} | {row['correct']} | "
            f"{_pct(row['accuracy'])} | {'yes' if row['accepted'] else 'no (abstained)'} |"
        )
    lines.append("")

    loop_b = scorecard.loop_b
    lines.append("## Loop B - order versus payment")
    lines.append("")
    lines.append(
        f"{loop_b['orders_checked']} orders checked, {loop_b['short_payments_found']} "
        f"paid short: {loop_b['explained_as_tds']} explained as tax withheld at source, "
        f"{loop_b['unexplained']} filed as exceptions."
    )
    lines.append("")

    lines.append("## Run detail")
    lines.append("")
    lines.append("```")
    lines.append(f"seed              {run['seed']}")
    lines.append(f"difficulty        {run['difficulty']}")
    lines.append(f"abstain below     {run.get('abstain_below')}")
    lines.append(f"tolerance (paise) {run.get('tolerance_paise')}")
    lines.append(f"records           {scorecard.totals['records']}")
    lines.append(f"candidates        {metrics['candidates_generated']}")
    lines.append(
        f"proofs            {metrics['proofs_verified']} "
        f"({metrics['proofs_passed']} passed, {metrics['proofs_failed']} failed)"
    )
    lines.append(f"reversal pairs    {metrics['bank_reversal_pairs']} neutralised")
    lines.append(f"subset cap hits   {metrics['subset_cap_hits']}")
    lines.append(f"python            {run['python']} on {run['platform']}")
    lines.append("```")
    lines.append("")

    destination = Path(path)
    with destination.open("w", newline="", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return destination


def print_scorecard(result: PipelineResult, scorecard: Scorecard) -> None:
    """The stdout view. Same numbers, fewer of them."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    metrics = scorecard.metrics

    headline = Table(
        title=(
            f"ReconProof - seed {scorecard.run['seed']} - "
            f"{scorecard.run['difficulty']} - "
            f"{scorecard.totals['records']} records in "
            f"{scorecard.totals['wall_clock_seconds']}s"
        ),
        show_header=True,
        header_style="bold",
    )
    headline.add_column("Metric")
    headline.add_column("Value", justify="right")
    headline.add_row(
        "Auto-match rate (credits)",
        f"{_pct(metrics['auto_match_rate_records'])}  "
        f"({metrics['matched_credits']}/{metrics['live_credits']})",
    )
    headline.add_row("Value coverage", _pct(metrics["value_coverage"]))
    headline.add_row(
        "False-match rate",
        f"[bold green]{_pct(metrics['false_match_rate'])}[/]"
        if metrics["false_matches"] == 0
        else f"[bold red]{_pct(metrics['false_match_rate'])}[/]",
    )
    headline.add_row(
        "Exceptions", f"{metrics['exceptions']}  ({rupees(metrics['exception_value_at_stake_paise'])} at stake)"
    )
    headline.add_row("Exception coverage", _pct(metrics["exception_coverage"]))
    headline.add_row(
        "Diagnosis accuracy",
        f"{_pct(metrics['diagnosis_accuracy'])}  "
        f"({metrics['correct_diagnoses']}/{metrics['unmatched_units']})",
    )
    headline.add_row(
        "Handled correctly",
        f"{_pct(metrics['handled_rate'])}  "
        f"({metrics['handled_correctly']}/{metrics['labelled_units']})",
    )
    if scorecard.llm["enabled"]:
        headline.add_row(
            "LLM hypotheses",
            f"{scorecard.llm['proposed']} proposed / "
            f"{scorecard.llm['accepted_by_verifier']} kept",
        )
    console.print(headline)

    cases = Table(title="Per case type", show_header=True, header_style="bold")
    cases.add_column("Case type")
    cases.add_column("Expected", justify="left")
    cases.add_column("Units", justify="right")
    cases.add_column("Matched", justify="right")
    cases.add_column("Diagnosed", justify="right")
    cases.add_column("Handled", justify="right")
    for case, row in sorted(
        scorecard.by_case.items(), key=lambda kv: (-kv[1]["handled_rate"], kv[0])
    ):
        cases.add_row(
            case,
            row["expected_outcome"],
            str(row["units"]),
            str(row["matched"]),
            str(row["correctly_diagnosed"]),
            _pct(row["handled_rate"]),
        )
    console.print(cases)

    exceptions = Table(
        title="Exceptions, by rupees at stake", show_header=True, header_style="bold"
    )
    exceptions.add_column("Subject")
    exceptions.add_column("Reason")
    exceptions.add_column("Residual", justify="right")
    exceptions.add_column("At stake", justify="right")
    for exception in result.exceptions[:12]:
        exceptions.add_row(
            exception.subject_id,
            exception.reason_code,
            rupees(exception.residual),
            rupees(exception.at_stake),
        )
    console.print(exceptions)
    if len(result.exceptions) > 12:
        console.print(f"  ... and {len(result.exceptions) - 12} more in RESULTS.md")


def format_derivation(proof: Proof) -> str:
    """One proof, written out the way a ledger writes it.

    Deductions in parentheses, a single rule above the computed net, a double
    rule above the residual. This is the same derivation the dashboard
    renders; having it on the terminal means the proof is legible even with
    the browser closed.
    """
    width = 52
    lines: list[str] = []
    header = (
        f"{proof.proof_id}  ·  {proof.bank_txn_id}  ·  {proof.rule}"
        f"  ·  {', '.join(proof.members.get('settlement_ids', [])) or 'no settlement'}"
    )
    lines.append(header)
    lines.append("-" * max(width, len(header)))

    for term in proof.terms:
        count = len(term.source_ids)
        label = f"{term.label} ({count})" if count > 1 else term.label
        lines.append(f"{label:<32}{rupees(term.signed):>20}")

    lines.append(" " * 32 + "-" * 20)
    lines.append(f"{'Computed net':<32}{rupees(proof.computed_net):>20}")
    lines.append(f"{'Observed credit':<32}{rupees(proof.observed_credit):>20}")
    lines.append(" " * 32 + "=" * 20)
    verdict = proof.verdict if proof.verdict != "PASS" else "PASS"
    lines.append(f"{'Residual':<32}{rupees(proof.residual):>20}   [{verdict}]")

    if proof.verdict_reason:
        lines.append(f"reason: {proof.verdict_reason}")
    for entry in proof.evidence:
        if entry.get("check") == "residual_suspects":
            lines.append(f"check:  {entry['value']}")
    lines.append(f"confidence: {proof.confidence}")
    return "\n".join(lines)


def print_derivation(proof: Proof) -> None:
    print()
    print(format_derivation(proof))


# --------------------------------------------------------------------------
# The ledger view model, for the dashboard.
# --------------------------------------------------------------------------


def build_ledger(result: PipelineResult, truth: GroundTruth) -> list[dict]:
    """One row per bank credit, in the order the statement shows them.

    Built here rather than in `serve.py` because it needs the ground-truth
    labels, and ground truth is loaded in exactly one module. It is not
    written into `report.json`: the dashboard is a view, and a view has no
    business inflating the machine-readable record.
    """
    best: dict[str, Proof] = {}
    for proof in result.proofs:
        for txn_id in proof.members.get("bank_txn_ids", []):
            current = best.get(txn_id)
            # Prefer an accepted proof, then a passing one, then whichever
            # got closest, so an unmatched credit still shows its nearest
            # attempt rather than an arbitrary one.
            rank = (
                proof.proof_id in {p.proof_id for p in result.accepted},
                proof.verdict == "PASS",
                -abs(proof.residual),
            )
            if current is None or rank > (
                current.proof_id in {p.proof_id for p in result.accepted},
                current.verdict == "PASS",
                -abs(current.residual),
            ):
                best[txn_id] = proof

    accepted_ids = {proof.proof_id for proof in result.accepted}
    exceptions_by_subject: dict[str, ExceptionRecord] = {
        exception.subject_id: exception for exception in result.exceptions
    }
    # A credit posted in error and pulled back the same day is neither a match
    # nor an exception - it is two lines that cancel. Showing it as "could not
    # match" would be a lie about the batch, so it gets its own status and is
    # excluded from the denominator everywhere else too.
    neutralised = {
        txn_id: other
        for pair in result.candidate_set.neutralised_pairs
        for txn_id, other in (pair, tuple(reversed(pair)))
    }

    rows: list[dict] = []
    for txn_id, txn in sorted(result.store.bank_txns.items()):
        proof = best.get(txn_id)
        exception = exceptions_by_subject.get(txn_id)
        matched = bool(proof and proof.proof_id in accepted_ids)
        if txn_id in neutralised:
            status = "neutralised"
        elif matched:
            status = "matched"
        else:
            status = "exception"
        rows.append(
            {
                "bank_txn_id": txn_id,
                "value_date": txn.value_date.isoformat(),
                "narration": txn.narration,
                "credit_amount": txn.credit_amount,
                "status": status,
                "reversed_by": neutralised.get(txn_id),
                "rule": proof.rule if proof else None,
                "proof_id": proof.proof_id if proof else None,
                "settlement_ids": proof.members.get("settlement_ids", []) if proof else [],
                "residual": proof.residual if proof else None,
                "confidence": proof.confidence if proof else None,
                "verdict": proof.verdict if proof else None,
                "verdict_reason": proof.verdict_reason if proof else None,
                "reason_code": exception.reason_code if exception else None,
                "what_to_check": exception.what_to_check if exception else None,
                # The generator's label. Shown so the weak rows are findable,
                # and never read by anything upstream of this function.
                "case_type": truth.case_of_bank_txn(txn_id),
            }
        )
    return rows
