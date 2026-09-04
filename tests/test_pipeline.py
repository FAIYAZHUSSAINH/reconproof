"""End-to-end behaviour: the numbers the README quotes, as assertions.

The false-match test is the one that matters. If it ever fails, the project's
central claim is false and no other number in the repo means anything.
"""

from __future__ import annotations

from reconproof.candidates import extract_utr, find_reversal_pairs, generate_candidates
from reconproof.models import REASON_CODES
from reconproof.pipeline import apply_tamper, reverify, run_pipeline
from reconproof.report import build_scorecard, is_correct


def scorecard_for(store, truth, **kwargs):
    result = run_pipeline(store, **kwargs)
    return result, build_scorecard(result, truth, seed=42, difficulty="standard")


class TestTheCentralClaim:
    def test_there_are_no_false_matches(self, store, truth):
        result, card = scorecard_for(store, truth)
        assert card.metrics["false_matches"] == 0, card.metrics["false_match_ids"]
        assert card.metrics["false_match_rate"] == 0.0

    def test_every_accepted_match_is_exactly_right(self, store, truth):
        """Not "close enough": the same credits, settlements and line items."""
        result = run_pipeline(store)
        assert result.accepted
        for proof in result.accepted:
            assert is_correct(proof, truth), proof.proof_id

    def test_a_failing_proof_is_never_accepted(self, store):
        result = run_pipeline(store)
        assert all(proof.verdict == "PASS" for proof in result.accepted)
        assert all(proof.residual == 0 for proof in result.accepted)

    def test_high_confidence_does_not_rescue_a_failure(self, store):
        result = run_pipeline(store)
        failures = [p for p in result.proofs if p.verdict == "FAIL"]
        assert failures
        accepted_ids = {p.proof_id for p in result.accepted}
        assert not any(p.proof_id in accepted_ids for p in failures)


class TestExceptions:
    def test_every_unmatched_credit_has_a_typed_exception(self, store, truth):
        result, card = scorecard_for(store, truth)
        assert card.metrics["exception_coverage"] == 1.0

    def test_every_exception_is_typed_and_actionable(self, store):
        result = run_pipeline(store)
        assert result.exceptions
        for exception in result.exceptions:
            assert exception.reason_code in REASON_CODES, exception.reason_code
            assert exception.what_to_check
            assert exception.records_involved
            assert "could not match" not in exception.what_to_check.lower()

    def test_exceptions_are_ordered_by_money_at_stake(self, store):
        result = run_pipeline(store)
        stakes = [exception.at_stake for exception in result.exceptions]
        assert stakes == sorted(stakes, reverse=True)

    def test_the_ambiguous_pair_is_abstained_on_not_guessed(self, store):
        """Two settlements with identical nets and no readable UTR.

        Both proofs balance. Picking one would be a coin flip, so the system
        files both as ambiguous instead.
        """
        result = run_pipeline(store)
        ambiguous = [e for e in result.exceptions if e.reason_code == "AMBIGUOUS_MATCH"]
        assert len(ambiguous) >= 2
        for exception in ambiguous:
            assert exception.residual == 0  # the arithmetic was fine
            assert exception.confidence is not None and exception.confidence <= 0.5


class TestDeterminism:
    def test_two_runs_give_identical_metrics(self, store, truth, batch_dir):
        from reconproof.ingest import load

        _, first = scorecard_for(store, truth)
        _, second = scorecard_for(load(batch_dir / "generated"), truth)
        assert first.metrics == second.metrics
        assert first.by_case == second.by_case

    def test_proof_ids_are_stable(self, store, batch_dir):
        from reconproof.ingest import load

        first = {p.proof_id for p in run_pipeline(store).proofs}
        second = {p.proof_id for p in run_pipeline(load(batch_dir / "generated")).proofs}
        assert first == second


class TestTamperFlow:
    def test_tampering_flips_a_proof_and_lowers_the_match_rate(self, store, truth):
        before, before_card = scorecard_for(store, truth)
        victim = before.accepted[0]
        payment_id = victim.members["payment_ids"][0]

        apply_tamper(store, payment_id, 100)
        after = reverify(before, store)
        after_card = build_scorecard(after, truth, seed=42, difficulty="standard")

        flipped = next(p for p in after.proofs if p.proof_id == victim.proof_id)
        assert flipped.verdict == "FAIL"
        assert flipped.verdict_reason.startswith("TERM_MISMATCH")
        assert (
            after_card.metrics["auto_match_rate_records"]
            < before_card.metrics["auto_match_rate_records"]
        )
        # And still no false matches: a corrupted batch loses matches, it does
        # not gain wrong ones.
        assert after_card.metrics["false_matches"] == 0


class TestCandidateRules:
    def test_the_utr_pattern_ignores_an_ifsc_code(self):
        # An IFSC is four letters then a zero. A UTR is four letters then N.
        assert extract_utr("NEFT-CR-HDFC0000123-ACME-PVT") is None
        assert extract_utr("NEFT-CR-HDFC0000123-X-HDFCN26215000137-CMS") == "HDFCN26215000137"

    def test_the_utr_is_found_inside_a_noisy_narration(self, store):
        noisy = [
            txn
            for txn in store.bank_txns.values()
            if "REF/CMS" in txn.narration
        ]
        assert noisy
        for txn in noisy:
            assert extract_utr(txn.narration) is not None

    def test_same_day_post_and_reversal_are_neutralised(self, store):
        pairs = find_reversal_pairs(store)
        assert len(pairs) >= 2
        for credit_id, debit_id in pairs:
            amounts = {
                store.bank_txn(credit_id).credit_amount,
                store.bank_txn(debit_id).credit_amount,
            }
            assert sum(amounts) == 0

    def test_the_subset_search_stays_inside_its_cap(self, store):
        candidates = generate_candidates(store)
        for candidate in candidates.candidates:
            assert len(candidate.bank_txn_ids) <= 3
            assert len(candidate.settlement_ids) <= 3

    def test_candidate_generation_proposes_more_than_it_accepts(self, store):
        """Generosity is the design: propose everything, let the verifier cut."""
        result = run_pipeline(store)
        assert len(result.proofs) > len(result.accepted)


class TestLoopB:
    def test_tds_is_told_apart_from_a_short_payment(self, store):
        result = run_pipeline(store)
        verdicts = {finding.order_id: finding.verdict for finding in result.loop_b}
        assert "TDS_WITHHELD" in verdicts.values()
        assert "SHORT_PAYMENT" in verdicts.values()

    def test_tds_orders_are_not_filed_as_exceptions(self, store):
        result = run_pipeline(store)
        tds_orders = {
            f.order_id for f in result.loop_b if f.verdict == "TDS_WITHHELD"
        }
        filed = {e.subject_id for e in result.exceptions}
        assert not (tds_orders & filed)
