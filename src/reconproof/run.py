"""Command-line entry point.

    python -m reconproof.run --no-llm --seed 42

Generates the batch, reconciles it, writes `report.json` and `RESULTS.md`, and
prints the scorecard. No API key required, and none used unless `--llm` is
passed.

Exit code 1 means a false match was found. That is deliberate: on this
project, a wrong match is a build failure, not a warning.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .adjudicate import AdjudicatorUnavailable, build_adjudicator
from .confidence import DEFAULT_ABSTAIN_BELOW
from .generate import generate
from .ingest import load
from .money import rupees
from .pipeline import apply_tamper, reverify, run_pipeline
from .report import (
    build_scorecard,
    load_ground_truth,
    print_derivation,
    print_scorecard,
    sweep_difficulties,
    write_report_json,
    write_results_md,
)


def _configure_stdout() -> None:
    """Make the console safe for the rupee sign.

    On Windows a redirected stdout defaults to the ANSI code page, and the
    first thing this program prints is a rupee amount. Reconfiguring here is
    cheaper than discovering it inside a pipe on the day of the demo.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - unusual consoles
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m reconproof.run",
        description=(
            "Reconcile a synthetic settlement batch, prove every match, and "
            "file a typed exception for everything else."
        ),
    )
    parser.add_argument("--seed", type=int, default=42, help="data generator seed (default: 42)")
    parser.add_argument(
        "--difficulty",
        choices=("easy", "standard", "hard"),
        default="standard",
        help="how many hard cases the batch contains (default: standard)",
    )
    llm_group = parser.add_mutually_exclusive_group()
    llm_group.add_argument(
        "--llm",
        dest="llm",
        action="store_true",
        help="enable the LLM adjudication layer (needs GROQ_API_KEY)",
    )
    llm_group.add_argument(
        "--no-llm",
        dest="llm",
        action="store_false",
        help="deterministic only. The default, and always sufficient.",
    )
    parser.set_defaults(llm=False)
    parser.add_argument(
        "--abstain-below",
        type=float,
        default=DEFAULT_ABSTAIN_BELOW,
        help=f"confidence below which the system files an exception (default: {DEFAULT_ABSTAIN_BELOW})",
    )
    parser.add_argument(
        "--tolerance-paise",
        type=int,
        default=0,
        help=(
            "accept a residual up to this size as a match. Default 0: how much "
            "unexplained money is acceptable is a policy decision, so it has to "
            "be asked for."
        ),
    )
    parser.add_argument(
        "--tamper",
        metavar="RECORD_ID",
        help="corrupt one source amount after the proofs are built, then re-verify",
    )
    parser.add_argument(
        "--tamper-delta",
        type=int,
        default=100,
        help="how many paise to shift the tampered record by (default: 100)",
    )
    parser.add_argument("--show", metavar="PROOF_OR_TXN_ID", help="print one proof's derivation")
    parser.add_argument("--out", default="report.json", help="machine-readable report path")
    parser.add_argument("--results", default="RESULTS.md", help="human-readable scorecard path")
    parser.add_argument("--data-dir", default="data/generated", help="generated CSV directory")
    parser.add_argument("--truth", default="data/ground_truth.json", help="ground truth path")
    parser.add_argument(
        "--no-regenerate",
        action="store_true",
        help="reconcile the committed CSVs instead of regenerating them",
    )
    parser.add_argument(
        "--no-sweep",
        action="store_true",
        help="skip the easy/standard/hard comparison table in RESULTS.md",
    )
    parser.add_argument("--quiet", action="store_true", help="write files, print nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    args = build_parser().parse_args(argv)

    if not args.no_regenerate:
        generate(
            seed=args.seed,
            difficulty=args.difficulty,
            data_dir=args.data_dir,
            truth_path=args.truth,
        )

    store = load(args.data_dir)

    adjudicator = None
    if args.llm:
        try:
            adjudicator = build_adjudicator(True)
        except AdjudicatorUnavailable as exc:
            # Never fatal. The deterministic run is the product; the model is
            # an enhancement, and a missing key must not cost a judge a run.
            print(f"[llm] disabled: {exc}", file=sys.stderr)

    result = run_pipeline(
        store,
        abstain_below=args.abstain_below,
        tolerance_paise=args.tolerance_paise,
        adjudicator=adjudicator,
    )

    if args.tamper:
        result = _run_tamper(result, store, args)

    truth = load_ground_truth(args.truth)
    scorecard = build_scorecard(
        result,
        truth,
        seed=args.seed,
        difficulty=args.difficulty,
        tampered=args.tamper,
    )

    out_path, results_path = args.out, args.results
    if args.tamper and not _paths_given(argv):
        # A tampered run is a run over data I deliberately corrupted, so its
        # numbers are not this project's numbers. Writing them to the
        # committed scorecard is how RESULTS.md once came to quote a one-rupee
        # residual that does not exist in the seed-42 data at all
        # (WHAT_BROKE.md, 4 Sep). The demo now writes beside the real
        # artefacts, never over them, unless a path was asked for explicitly.
        out_path, results_path = "report.tampered.json", "RESULTS.tampered.md"

    # The difficulty sweep is measured, not typed in, so the table in
    # RESULTS.md cannot go stale behind a change to the generator. Skipped on
    # a tampered run, where the numbers would be meaningless anyway.
    sweep = None
    if not args.tamper and not args.no_sweep:
        sweep = sweep_difficulties(args.seed)

    write_report_json(result, scorecard, out_path)
    write_results_md(result, scorecard, results_path, sweep=sweep)

    if not args.quiet:
        print_scorecard(result, scorecard)
        if args.show:
            _show(result, args.show)
        print(f"\nWrote {out_path} and {results_path}")
        if args.tamper and results_path != args.results:
            print(
                "  (tampered run: the committed RESULTS.md was left "
                "untouched. Pass --results to override.)"
            )

    if scorecard.metrics["false_matches"]:
        print(
            f"FALSE MATCHES: {scorecard.metrics['false_match_ids']}",
            file=sys.stderr,
        )
        return 1
    return 0


def _paths_given(argv: list[str] | None) -> bool:
    """Did the caller name an output path themselves?"""
    return any(
        arg.startswith(("--out", "--results"))
        for arg in (sys.argv[1:] if argv is None else argv)
    )


def _run_tamper(result, store, args):
    """Corrupt a record, re-verify, and show what flipped."""
    affected_before = [
        proof
        for proof in result.accepted
        if args.tamper in proof.all_member_ids
    ]
    change = apply_tamper(store, args.tamper, args.tamper_delta)

    tampered_result = reverify(
        result,
        store,
        abstain_below=args.abstain_below,
        tolerance_paise=args.tolerance_paise,
    )
    after_by_id = {proof.proof_id: proof for proof in tampered_result.proofs}

    if not args.quiet:
        print(
            f"\n--tamper: {change['record_id']}.{change['field']} "
            f"{rupees(change['before'])} -> {rupees(change['after'])}"
        )
        if not affected_before:
            print("  no accepted proof contained that record; nothing to flip")
        for proof in affected_before:
            after = after_by_id.get(proof.proof_id)
            if after is None:
                continue
            print(
                f"  {proof.proof_id}  {proof.bank_txn_id}  "
                f"{proof.verdict} -> {after.verdict}"
                f"   reason: {after.verdict_reason}"
                f"   residual: {rupees(after.residual)}"
            )
        print(
            "  the verifier re-read the source records; nothing about the "
            "proof itself changed\n"
        )
    return tampered_result


def _show(result, wanted: str) -> None:
    for proof in result.proofs:
        if wanted in (proof.proof_id, proof.bank_txn_id) or wanted in proof.members.get(
            "bank_txn_ids", []
        ):
            print_derivation(proof)
            return
    print(f"no proof found for {wanted!r}")


if __name__ == "__main__":
    raise SystemExit(main())
