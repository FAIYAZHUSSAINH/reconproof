"""Tests for the data generator.

The generator is the experiment, so its properties are the experiment's
validity: every case type present, the ledger identity exactly true for every
labelled answer, no label anywhere in the CSVs, and byte-identical output for
a given seed.
"""

from __future__ import annotations

import csv
import json

from reconproof.generate import CYCLE_DAYS, generate
from reconproof.ingest import load
from reconproof.models import ADJUSTMENT_SIGN, CASE_TYPES


def read_truth(root):
    with (root / "ground_truth.json").open(encoding="utf-8") as handle:
        return json.load(handle)


class TestCoverage:
    def test_every_case_type_appears_at_least_twice(self, batch_dir):
        truth = read_truth(batch_dir)
        counts: dict[str, int] = {}
        for group in ("matches", "unmatched_settlements", "unmatched_bank_txns", "loop_b"):
            for entry in truth[group]:
                counts[entry["case_type"]] = counts.get(entry["case_type"], 0) + 1
        missing = [case for case in CASE_TYPES if counts.get(case, 0) < 2]
        assert not missing, f"case types under-represented: {missing}"

    def test_the_batch_is_bigger_than_the_track_minimum(self, store):
        assert len(store.payments) >= 50
        assert store.total_records >= 300

    def test_there_is_an_adjustment_with_no_payment_reference(self, store):
        """The hardest case has to actually be in the data."""
        unlabelled = [
            a
            for a in store.adjustments.values()
            if a.kind == "chargeback_reversal" and a.ref_payment_id is None
        ]
        assert len(unlabelled) >= 2

    def test_there_are_failed_payments_as_decoys(self, store):
        assert any(p.status == "failed" for p in store.payments.values())

    def test_there_is_a_negative_bank_line(self, store):
        assert any(t.credit_amount < 0 for t in store.bank_txns.values())


class TestLedgerIdentity:
    def test_every_labelled_match_balances(self, store, batch_dir):
        """The answer key has to be arithmetically true.

        If this fails, every match rate in the project is measuring the
        generator's bugs rather than the matcher's behaviour.
        """
        truth = read_truth(batch_dir)
        for entry in truth["matches"]:
            payments = [store.payment(pid) for pid in entry["payment_ids"]]
            refunds = [store.refund(rid) for rid in entry["refund_ids"]]
            adjustments = [store.adjustment(aid) for aid in entry["adjustment_ids"]]
            computed = (
                sum(p.amount for p in payments)
                - sum(p.fee for p in payments)
                - sum(p.tax for p in payments)
                - sum(r.amount for r in refunds)
                + sum(ADJUSTMENT_SIGN[a.kind] * a.amount for a in adjustments)
            )
            observed = sum(store.bank_txn(t).credit_amount for t in entry["bank_txn_ids"])
            residual = computed - observed
            if entry["case_type"] == "ROUNDING_DRIFT":
                # This one is meant to be off, by a paisa or two, and no more.
                assert 0 < abs(residual) <= 5
            else:
                assert residual == 0, f"{entry['case_type']} {entry['settlement_ids']}"

    def test_settlement_totals_agree_with_their_line_items(self, store, batch_dir):
        truth = read_truth(batch_dir)
        for entry in truth["matches"]:
            if entry["case_type"] in {"ROUNDING_DRIFT", "MERGED_CREDIT"}:
                continue
            payments = [store.payment(pid) for pid in entry["payment_ids"]]
            settlement = store.settlement(entry["settlement_ids"][0])
            assert settlement.gross == sum(p.amount for p in payments)
            assert settlement.fee == sum(p.fee for p in payments)

    def test_the_cycle_rule_holds_for_clean_settlements(self, store, batch_dir):
        from datetime import timedelta

        truth = read_truth(batch_dir)
        clean = [entry for entry in truth["matches"] if entry["case_type"] == "CLEAN"]
        assert clean
        for entry in clean:
            settlement = store.settlement(entry["settlement_ids"][0])
            for payment_id in entry["payment_ids"]:
                payment = store.payment(payment_id)
                assert (
                    payment.captured_at.date() + timedelta(days=CYCLE_DAYS)
                    == settlement.settled_at
                )


class TestNoLeakage:
    def test_no_csv_contains_a_case_label(self, batch_dir):
        """The pipeline must not be able to peek at the answer."""
        for path in sorted((batch_dir / "generated").glob("*.csv")):
            text = path.read_text(encoding="utf-8")
            for case in CASE_TYPES:
                assert case not in text, f"{path.name} leaks {case}"
            assert "case_type" not in text

    def test_no_csv_carries_a_settlement_id_on_a_line_item(self, batch_dir):
        """Membership has to be inferred, not read off a join key."""
        for name in ("payments.csv", "refunds.csv", "adjustments.csv"):
            with (batch_dir / "generated" / name).open(encoding="utf-8") as handle:
                columns = next(csv.reader(handle))
            assert "settlement_id" not in columns


class TestDeterminism:
    def test_the_same_seed_produces_identical_bytes(self, tmp_path):
        first = tmp_path / "a"
        second = tmp_path / "b"
        for root in (first, second):
            generate(seed=42, difficulty="standard", data_dir=root / "gen", truth_path=root / "gt.json")

        for name in sorted(p.name for p in (first / "gen").glob("*.csv")):
            assert (first / "gen" / name).read_bytes() == (second / "gen" / name).read_bytes()
        assert (first / "gt.json").read_bytes() == (second / "gt.json").read_bytes()

    def test_a_different_seed_produces_different_data(self, tmp_path):
        generate(seed=42, difficulty="standard", data_dir=tmp_path / "a", truth_path=tmp_path / "a.json")
        generate(seed=7, difficulty="standard", data_dir=tmp_path / "b", truth_path=tmp_path / "b.json")
        assert (tmp_path / "a" / "payments.csv").read_bytes() != (
            tmp_path / "b" / "payments.csv"
        ).read_bytes()

    def test_difficulty_changes_the_batch(self, tmp_path):
        sizes = {}
        for difficulty in ("easy", "standard", "hard"):
            result = generate(
                seed=42,
                difficulty=difficulty,
                data_dir=tmp_path / difficulty,
                truth_path=tmp_path / f"{difficulty}.json",
            )
            sizes[difficulty] = result.counts["settlements"]
        assert sizes["easy"] > sizes["standard"]
        assert sizes["hard"] > sizes["standard"]


class TestIngestGuards:
    def test_a_rupee_figure_in_a_paise_column_is_rejected(self, tmp_path, batch_dir):
        """The float guard, at the boundary where data actually arrives."""
        import shutil

        from reconproof.money import MoneyTypeError

        broken = tmp_path / "broken"
        shutil.copytree(batch_dir / "generated", broken)
        path = broken / "payments.csv"
        rows = path.read_text(encoding="utf-8").splitlines()
        columns = rows[1].split(",")
        columns[2] = "4143.82"  # amount, as rupees instead of paise
        rows[1] = ",".join(columns)
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")

        try:
            load(broken)
        except MoneyTypeError as exc:
            assert "integer paise" in str(exc)
        else:
            raise AssertionError("a rupee figure was accepted into a paise column")
