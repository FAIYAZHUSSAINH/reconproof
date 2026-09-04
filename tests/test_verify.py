"""Tests for the verifier.

These are the pitch. Everything else in the repo is a claim about correctness;
this file is the evidence for it. Each test attacks the verifier from a
different direction: corrupt the source data, forge the proof, list a member
that does not belong, spend the same payment twice.
"""

from __future__ import annotations

import pytest

from reconproof.candidates import generate_candidates
from reconproof.money import gst_on_fee
from reconproof.proof import build_proof, build_proof_from_members
from reconproof.verify import enforce_member_exclusivity, verify


def passing_proof(store):
    """The first proof in the batch that balances. A real one, not a fixture."""
    for candidate in generate_candidates(store).candidates:
        proof = build_proof(store, candidate)
        checked = verify(proof, store)
        if checked.verdict == "PASS" and proof.members["payment_ids"]:
            return proof
    raise AssertionError("no passing proof in the batch - the fixture is broken")


class TestHappyPath:
    def test_a_correct_proof_passes(self, store):
        proof = passing_proof(store)
        checked = verify(proof, store)
        assert checked.verdict == "PASS"
        assert checked.residual == 0
        assert checked.verdict_reason is None

    def test_the_identity_is_actually_computed(self, store):
        # Not just "it said PASS": the terms have to sum to the credit.
        checked = verify(passing_proof(store), store)
        assert sum(term.signed for term in checked.terms) == checked.computed_net
        assert checked.computed_net == checked.observed_credit

    def test_verify_does_not_mutate_its_input(self, store):
        proof = passing_proof(store)
        before = proof.model_dump()
        verify(proof, store)
        assert proof.model_dump() == before


class TestTamperedSourceData:
    """The demo, as a test."""

    def test_changing_a_payment_amount_flips_the_verdict(self, store):
        proof = passing_proof(store)
        assert verify(proof, store).verdict == "PASS"

        victim_id = proof.members["payment_ids"][0]
        victim = store.payment(victim_id)
        store.replace(victim.model_copy(update={"amount": victim.amount + 100}))

        checked = verify(proof, store)
        assert checked.verdict == "FAIL"
        # The reason names the term that broke, not just "mismatch".
        assert checked.verdict_reason == "TERM_MISMATCH:Gross payments"

    def test_changing_a_fee_names_the_fee_term(self, store):
        proof = passing_proof(store)
        victim = store.payment(proof.members["payment_ids"][0])
        store.replace(victim.model_copy(update={"fee": victim.fee + 7}))
        assert verify(proof, store).verdict_reason == "TERM_MISMATCH:Gateway fee"

    def test_changing_the_bank_credit_is_caught_as_a_residual(self, store):
        proof = passing_proof(store)
        txn_id = proof.members["bank_txn_ids"][0]
        txn = store.bank_txn(txn_id)
        store.replace(txn.model_copy(update={"credit_amount": txn.credit_amount - 5_000}))

        checked = verify(proof, store)
        assert checked.verdict == "FAIL"
        assert checked.residual == 5_000

    def test_the_verifier_uses_the_record_not_the_cached_term(self, store):
        """Rule 3, directly.

        The proof's term still says the old figure. If the verifier trusted
        that cached amount it would balance and pass. It must not.
        """
        proof = passing_proof(store)
        victim = store.payment(proof.members["payment_ids"][0])
        store.replace(victim.model_copy(update={"amount": victim.amount + 25_000}))

        checked = verify(proof, store)
        gross_claimed = next(t for t in proof.terms if t.label == "Gross payments")
        gross_recomputed = next(t for t in checked.terms if t.label == "Gross payments")
        assert gross_recomputed.amount == gross_claimed.amount + 25_000
        assert checked.verdict == "FAIL"


class TestForgedProofs:
    def test_a_hand_forged_computed_net_is_rejected(self, store):
        proof = passing_proof(store)
        forged = proof.model_copy(deep=True)
        forged.computed_net = forged.observed_credit + 100_000
        checked = verify(forged, store)
        assert checked.verdict == "FAIL"
        assert checked.verdict_reason == "FORGED_COMPUTED_NET"

    def test_a_forged_observed_credit_is_rejected(self, store):
        proof = passing_proof(store)
        forged = proof.model_copy(deep=True)
        forged.observed_credit = forged.observed_credit + 1
        assert verify(forged, store).verdict_reason == "FORGED_OBSERVED_CREDIT"

    def test_a_forged_term_amount_is_rejected(self, store):
        proof = passing_proof(store)
        forged = proof.model_copy(deep=True)
        forged.terms[0].amount += 500
        assert verify(forged, store).verdict_reason.startswith("TERM_MISMATCH")

    def test_a_forged_verdict_does_not_survive(self, store):
        """A proof that arrives already stamped PASS gets re-judged anyway."""
        proof = passing_proof(store)
        lying = proof.model_copy(deep=True)
        lying.verdict = "PASS"
        lying.members["payment_ids"] = lying.members["payment_ids"][:-1]
        assert verify(lying, store).verdict == "FAIL"


class TestMembership:
    def test_a_member_that_does_not_belong_fails(self, store):
        proof = passing_proof(store)
        outsider = next(
            payment_id
            for payment_id in sorted(store.payments)
            if payment_id not in proof.members["payment_ids"]
            and store.payment(payment_id).status == "captured"
        )
        members = {key: list(values) for key, values in proof.members.items()}
        members["payment_ids"].append(outsider)

        rebuilt = build_proof_from_members(store, "R1_UTR_EXACT", members)
        checked = verify(rebuilt, store)
        assert checked.verdict == "FAIL"
        assert checked.residual != 0

    def test_a_missing_record_id_fails(self, store):
        proof = passing_proof(store)
        broken = proof.model_copy(deep=True)
        broken.members["payment_ids"] = broken.members["payment_ids"] + ["pay_9999"]
        assert verify(broken, store).verdict_reason == "MISSING_RECORD:pay_9999"

    def test_a_duplicated_member_fails(self, store):
        proof = passing_proof(store)
        broken = proof.model_copy(deep=True)
        duplicate = broken.members["payment_ids"][0]
        broken.members["payment_ids"] = broken.members["payment_ids"] + [duplicate]
        assert verify(broken, store).verdict_reason == f"DUPLICATE_MEMBER:{duplicate}"

    def test_a_failed_payment_cannot_be_a_member(self, store):
        """Money that never moved cannot settle - even if it balances."""
        proof = passing_proof(store)
        failed = next(
            p.payment_id for p in store.payments.values() if p.status == "failed"
        )
        broken = proof.model_copy(deep=True)
        broken.members["payment_ids"] = broken.members["payment_ids"] + [failed]
        assert verify(broken, store).verdict_reason == f"INELIGIBLE_MEMBER:{failed}"

    def test_a_proof_with_no_bank_credit_fails(self, store):
        proof = passing_proof(store)
        broken = proof.model_copy(deep=True)
        broken.members["bank_txn_ids"] = []
        assert verify(broken, store).verdict_reason == "NO_BANK_CREDIT"


class TestExclusivity:
    def test_the_same_payment_cannot_settle_twice(self, store):
        """The classic reconciliation bug, caught globally after verification."""
        proof = passing_proof(store)
        first = verify(proof, store)
        first.confidence = 0.98

        # A second proof over the same records. Individually it verifies fine.
        twin = first.model_copy(deep=True)
        twin.proof_id = first.proof_id + "_twin"
        twin.confidence = 0.85

        resolved = enforce_member_exclusivity([first, twin])
        verdicts = {p.proof_id: (p.verdict, p.verdict_reason) for p in resolved}
        assert verdicts[first.proof_id][0] == "PASS"
        assert verdicts[twin.proof_id][0] == "FAIL"
        assert verdicts[twin.proof_id][1].startswith("MEMBER_CONFLICT")

    def test_the_stronger_proof_survives(self, store):
        proof = verify(passing_proof(store), store)
        weak = proof.model_copy(deep=True)
        weak.proof_id = proof.proof_id + "_weak"
        weak.confidence = 0.40
        strong = proof.model_copy(deep=True)
        strong.confidence = 0.98

        resolved = {p.proof_id: p for p in enforce_member_exclusivity([weak, strong])}
        assert resolved[strong.proof_id].verdict == "PASS"
        assert resolved[weak.proof_id].verdict == "FAIL"

    def test_failing_proofs_do_not_reserve_records(self, store):
        proof = verify(passing_proof(store), store)
        failed = proof.model_copy(deep=True)
        failed.proof_id = proof.proof_id + "_failed"
        failed.verdict = "FAIL"
        failed.confidence = 0.99

        resolved = {p.proof_id: p for p in enforce_member_exclusivity([failed, proof])}
        assert resolved[proof.proof_id].verdict == "PASS"


class TestResidualClassification:
    def test_a_paisa_of_drift_is_named_as_drift(self, store):
        proof = passing_proof(store)
        txn = store.bank_txn(proof.members["bank_txn_ids"][0])
        store.replace(txn.model_copy(update={"credit_amount": txn.credit_amount - 2}))
        checked = verify(proof.model_copy(deep=True), store)
        assert checked.verdict_reason == "ROUNDING_DRIFT"

    def test_tolerance_is_opt_in(self, store):
        proof = passing_proof(store)
        txn = store.bank_txn(proof.members["bank_txn_ids"][0])
        store.replace(txn.model_copy(update={"credit_amount": txn.credit_amount - 2}))

        assert verify(proof, store).verdict == "FAIL"
        relaxed = verify(proof, store, tolerance_paise=5)
        assert relaxed.verdict == "PASS"
        assert relaxed.verdict_reason == "within_tolerance"

    def test_a_missing_refund_is_named_with_the_refund_id(self, store):
        """Drop a refund from a proof that contains one and see it identified."""
        candidates = generate_candidates(store).candidates
        for candidate in candidates:
            proof = build_proof(store, candidate)
            if not proof.members["refund_ids"] or verify(proof, store).verdict != "PASS":
                continue
            refund_id = proof.members["refund_ids"][0]
            members = {key: list(values) for key, values in proof.members.items()}
            members["refund_ids"] = []
            stripped = build_proof_from_members(store, candidate.rule, members)

            checked = verify(stripped, store)
            assert checked.verdict == "FAIL"
            assert checked.verdict_reason == "SUSPECT_UNAPPLIED_REFUND"
            suspects = [
                entry["value"]
                for entry in checked.evidence
                if entry.get("check") == "residual_suspects"
            ]
            assert refund_id in suspects[0]
            return
        pytest.skip("no passing proof with a refund in this batch")

    def test_an_unexplained_residual_says_so(self, store):
        proof = passing_proof(store)
        txn = store.bank_txn(proof.members["bank_txn_ids"][0])
        # An odd amount that matches no refund, adjustment or withholding.
        store.replace(txn.model_copy(update={"credit_amount": txn.credit_amount - 33_333}))
        assert verify(proof, store).verdict_reason == "UNEXPLAINED_RESIDUAL"


class TestArithmeticItself:
    def test_gst_is_recomputed_from_the_stored_fee(self, store):
        """The GST term must be the gateway's tax figure, and it must be
        consistent with our own rounding rule on a clean payment."""
        proof = passing_proof(store)
        for payment_id in proof.members["payment_ids"]:
            payment = store.payment(payment_id)
            assert payment.tax == gst_on_fee(payment.fee)
