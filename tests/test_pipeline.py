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


class TestAmbiguityIsNotResolvedByLuck:
    """The AMOUNT_COLLISION pair, and why the competition cap is load-bearing.

    Two settlements on the same date with identical nets produce four
    candidate pairings, and *all four* balance to exactly zero paise. The
    arithmetic cannot separate them, so neither can this system. Without the
    0.50 cap the batch still reports zero false matches on this seed - but
    only because the exclusivity tie-break happens to sort the correct
    permutation first. Being right by ID order is not evidence, and this test
    exists so that nobody later reads that zero as a result.
    """

    COLLIDING = {"bnk_0018", "bnk_0019"}

    def _colliding_proofs(self, store):
        from reconproof.proof import build_proof
        from reconproof.verify import verify

        return [
            verify(build_proof(store, candidate), store)
            for candidate in generate_candidates(store).candidates
            if set(candidate.bank_txn_ids) & self.COLLIDING
        ]

    def test_every_permutation_balances(self, store):
        proofs = self._colliding_proofs(store)
        assert len(proofs) == 4
        assert all(p.verdict == "PASS" and p.residual == 0 for p in proofs)

    def test_half_of_the_permutations_are_wrong(self, store, truth):
        """Proof that the choice is a coin flip, not a deduction."""
        proofs = self._colliding_proofs(store)
        assert sum(is_correct(p, truth) for p in proofs) == 2

    def test_the_system_abstains_on_both(self, store):
        result = run_pipeline(store)
        matched = {
            txn_id for proof in result.accepted for txn_id in proof.members["bank_txn_ids"]
        }
        assert not (self.COLLIDING & matched)

        filed = {
            exception.subject_id: exception.reason_code
            for exception in result.exceptions
        }
        for txn_id in self.COLLIDING:
            assert filed[txn_id] == "AMBIGUOUS_MATCH"

    def test_the_cap_is_what_does_it(self, store):
        """Drop the threshold and the system starts guessing.

        It guesses correctly on this seed. That is the point: the cap is not
        buying a lower false-match rate on this batch, it is buying the
        system's refusal to answer a question the data cannot answer.
        """
        reckless = run_pipeline(store, abstain_below=0.0)
        matched = {
            txn_id for proof in reckless.accepted for txn_id in proof.members["bank_txn_ids"]
        }
        assert self.COLLIDING <= matched
        guessed = [p for p in reckless.accepted if set(p.members["bank_txn_ids"]) & self.COLLIDING]
        assert all(p.confidence == 0.50 for p in guessed)


class TestIntegrityFailuresAreTypedHonestly:
    """A rejected *proof* is not a reconciliation difference.

    Before this, tampering with a payment filed UNEXPLAINED_RESIDUAL against
    the credit and told a human to go and read the gateway's settlement
    report. The verifier knew perfectly well that the gross payments term no
    longer matched the records; the exception threw that away and filed the
    wrong diagnosis in a queue somebody has to work.
    """

    def test_a_tampered_payment_is_filed_as_a_changed_record(self, store):
        result = run_pipeline(store)
        target = next(p for p in result.accepted if p.members["payment_ids"])
        apply_tamper(store, target.members["payment_ids"][0], 5_000)
        after = reverify(result, store)

        filed = next(
            e for e in after.exceptions if e.subject_id == target.bank_txn_id
        )
        assert filed.reason_code == "SOURCE_RECORD_CHANGED"
        # The verifier's own words survive into the human-facing advice.
        assert "TERM_MISMATCH:Gross payments" in filed.what_to_check

    def test_the_code_is_a_declared_one(self, store):
        result = run_pipeline(store)
        target = next(p for p in result.accepted if p.members["payment_ids"])
        apply_tamper(store, target.members["payment_ids"][0], 5_000)
        after = reverify(result, store)
        for exception in after.exceptions:
            assert exception.reason_code in REASON_CODES

    def test_a_forged_proof_is_not_called_a_residual(self, store):
        from reconproof.pipeline import integrity_code

        assert integrity_code("FORGED_COMPUTED_NET") == "INVALID_PROOF"
        assert integrity_code("MISSING_RECORD:pay_9999") == "INVALID_PROOF"
        assert integrity_code("INELIGIBLE_MEMBER:pay_0002") == "INVALID_PROOF"
        # An ordinary residual is not an integrity failure and must fall
        # through to the money-shaped taxonomy.
        assert integrity_code("SUSPECT_UNAPPLIED_REFUND") is None
        assert integrity_code(None) is None


class TestTheMatchRateFallsWhereItShould:
    """The bar says one cherry-picked match proves nothing.

    The seed is the wrong axis to prove that on: it varies amounts, dates and
    narrations but not the *shape* of the batch, so ten seeds give ten
    identical match rates. `--difficulty` is the axis that changes how many
    hard cases there are, and it is the one worth reporting - the rate has to
    fall as the data gets harder, and the false-match rate has to stay at zero
    while it does.
    """

    def _score(self, tmp_path, difficulty):
        from reconproof.generate import generate
        from reconproof.ingest import load
        from reconproof.report import load_ground_truth

        root = tmp_path / difficulty
        generate(
            seed=42,
            difficulty=difficulty,
            data_dir=root / "generated",
            truth_path=root / "ground_truth.json",
        )
        result = run_pipeline(load(root / "generated"))
        return build_scorecard(
            result,
            load_ground_truth(root / "ground_truth.json"),
            seed=42,
            difficulty=difficulty,
        ).metrics

    def test_harder_data_matches_less(self, tmp_path):
        rates = {
            level: self._score(tmp_path, level)["auto_match_rate_records"]
            for level in ("easy", "standard", "hard")
        }
        assert rates["easy"] > rates["standard"] > rates["hard"], rates

    def test_no_difficulty_produces_a_false_match(self, tmp_path):
        """The one number that is not allowed to move."""
        for level in ("easy", "standard", "hard"):
            assert self._score(tmp_path, level)["false_matches"] == 0, level


class TestTheLedgerAgreesWithTheExceptionList:
    """Two screens, one number.

    A credit with no candidate has no proof, so the ledger row used to carry
    `residual: None` - which the dashboard renders as the em dash it reserves
    for a residual of exactly zero. The exceptions screen, reading the
    exception record instead, showed the same credit thousands of rupees out.
    Whichever screen a reviewer opened first decided what they believed, and
    one of the two was wrong.
    """

    def _ledger(self, store, truth):
        from reconproof.report import build_ledger

        result = run_pipeline(store)
        return result, {row["bank_txn_id"]: row for row in build_ledger(result, truth)}

    def test_every_exception_row_carries_its_residual(self, store, truth):
        result, ledger = self._ledger(store, truth)
        for exception in result.exceptions:
            row = ledger.get(exception.subject_id)
            if row is None:  # a settlement or order, not a bank credit
                continue
            assert row["residual"] == exception.residual, (
                f"{exception.subject_id}: ledger says {row['residual']}, "
                f"exceptions say {exception.residual}"
            )

    def test_a_proofless_credit_is_not_shown_as_balanced(self, store, truth):
        """`None` reads as zero on screen, so it must not stand in for unknown."""
        _, ledger = self._ledger(store, truth)
        orphans = [
            row
            for row in ledger.values()
            if row["reason_code"] == "ORPHAN_CREDIT"
        ]
        assert orphans, "the standard batch is supposed to contain orphan credits"
        for row in orphans:
            assert row["proof_id"] is None
            assert row["residual"] not in (None, 0), row["bank_txn_id"]

    def test_matched_rows_still_report_zero(self, store, truth):
        _, ledger = self._ledger(store, truth)
        matched = [row for row in ledger.values() if row["status"] == "matched"]
        assert matched
        for row in matched:
            assert row["residual"] == 0, row["bank_txn_id"]
