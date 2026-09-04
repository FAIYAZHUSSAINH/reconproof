"""The LLM layer, tested without an API key.

The point of these tests is not that the model is good. It is that the model's
answer changes nothing on its own: a correct hypothesis becomes a match only
because the verifier re-derived it, and a wrong one is thrown away no matter
how confidently it was phrased.

A stub stands in for Groq so this runs offline, in CI, and on a judge's
machine with no key.
"""

from __future__ import annotations

from reconproof.adjudicate import Hypothesis, parse_hypothesis
from reconproof.pipeline import run_pipeline
from reconproof.report import build_scorecard, is_correct


class PerfectStub:
    """A model that always names the record that actually balances."""

    def __init__(self):
        self.calls = 0

    def propose(self, context):
        self.calls += 1
        target = abs(context["residual_paise"])
        for entry in context["unassigned_candidates"]:
            if entry["amount_paise"] == target:
                return Hypothesis(
                    add_members=[entry["id"]],
                    reason="amount equals the residual exactly",
                    model="stub-perfect",
                )
        return None


class ConfidentlyWrongStub:
    """A model that always answers, and is always wrong.

    This is the important one. It is the failure mode the whole architecture
    exists to survive.
    """

    def __init__(self):
        self.calls = 0

    def propose(self, context):
        self.calls += 1
        candidates = context["unassigned_candidates"]
        target = abs(context["residual_paise"])
        for entry in candidates:
            if entry["amount_paise"] != target:
                return Hypothesis(
                    add_members=[entry["id"]],
                    reason="I am certain this is the missing line",
                    model="stub-wrong",
                )
        return None


class TestAGoodHypothesisIsStillChecked:
    def test_a_correct_hypothesis_raises_the_match_rate(self, store, truth, batch_dir):
        from reconproof.ingest import load

        baseline = run_pipeline(load(batch_dir / "generated"))
        stub = PerfectStub()
        boosted = run_pipeline(store, adjudicator=stub)

        assert stub.calls > 0
        assert len(boosted.accepted) > len(baseline.accepted)

    def test_hypotheses_that_are_accepted_are_actually_correct(self, store, truth):
        result = run_pipeline(store, adjudicator=PerfectStub())
        card = build_scorecard(result, truth, seed=42, difficulty="standard")
        assert card.metrics["false_matches"] == 0
        for proof in result.accepted:
            assert is_correct(proof, truth), proof.proof_id

    def test_the_log_records_what_survived(self, store):
        result = run_pipeline(store, adjudicator=PerfectStub())
        outcomes = [entry["outcome"] for entry in result.llm_hypotheses]
        assert "accepted_by_verifier" in outcomes
        for entry in result.llm_hypotheses:
            assert entry["bank_txn_id"]
            assert "residual_paise" in entry

    def test_an_llm_match_carries_its_reasoning_as_evidence(self, store):
        result = run_pipeline(store, adjudicator=PerfectStub())
        llm_proofs = [p for p in result.accepted if p.rule == "R5_LLM_HYPOTHESIS"]
        assert llm_proofs
        for proof in llm_proofs:
            checks = {entry.get("check") for entry in proof.evidence}
            assert "llm_reason" in checks
            assert "llm_added" in checks
            assert proof.residual == 0


class TestAWrongHypothesisChangesNothing:
    def test_a_confident_wrong_answer_is_rejected(self, store, truth):
        stub = ConfidentlyWrongStub()
        result = run_pipeline(store, adjudicator=stub)
        card = build_scorecard(result, truth, seed=42, difficulty="standard")

        assert stub.calls > 0
        assert card.metrics["false_matches"] == 0
        assert all(
            entry["outcome"] != "accepted_by_verifier" for entry in result.llm_hypotheses
        )

    def test_a_wrong_answer_does_not_change_the_match_rate(self, store, truth, batch_dir):
        from reconproof.ingest import load

        baseline = run_pipeline(load(batch_dir / "generated"))
        result = run_pipeline(store, adjudicator=ConfidentlyWrongStub())
        assert len(result.accepted) == len(baseline.accepted)


class TestTheModelCannotReachPastTheVerifier:
    def test_only_offered_ids_are_accepted(self):
        context = {"unassigned_candidates": [{"id": "rfnd_0044", "amount_paise": 100}]}
        invented = parse_hypothesis(
            '{"add_members": ["rfnd_9999"], "reason": "trust me"}', context, "stub"
        )
        assert invented is None

    def test_malformed_json_is_dropped(self):
        context = {"unassigned_candidates": [{"id": "rfnd_0044", "amount_paise": 100}]}
        assert parse_hypothesis("not json at all", context, "stub") is None
        assert parse_hypothesis('["a", "list"]', context, "stub") is None
        assert parse_hypothesis('{"add_members": "rfnd_0044"}', context, "stub") is None

    def test_an_empty_answer_is_allowed(self):
        context = {"unassigned_candidates": [{"id": "rfnd_0044", "amount_paise": 100}]}
        assert parse_hypothesis('{"add_members": [], "reason": "nothing fits"}', context, "stub") is None

    def test_a_valid_answer_parses(self):
        context = {"unassigned_candidates": [{"id": "rfnd_0044", "amount_paise": 100}]}
        parsed = parse_hypothesis(
            '{"add_members": ["rfnd_0044"], "reason": "matches the residual"}',
            context,
            "stub",
        )
        assert parsed is not None
        assert parsed.add_members == ["rfnd_0044"]

    def test_the_model_cannot_add_payments(self, store):
        """Payment membership is a matter of record, not of inference."""
        from reconproof.pipeline import _apply_hypothesis

        proof = run_pipeline(store).accepted[0]
        assert _apply_hypothesis(proof, ["pay_0001"]) is None
        assert _apply_hypothesis(proof, ["set_0001"]) is None
