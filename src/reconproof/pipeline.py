"""The pipeline: raw records in, accepted matches and typed exceptions out.

Six stages, in one place, so that the order they run in is readable:

    ingest -> candidates -> proofs -> verify -> score/abstain -> exceptions

The optional LLM layer slots in between verification and the final selection.
It can only add candidate member sets; everything it proposes is rebuilt into a
proof and pushed through the same `verify()` call as every deterministic rule.

This module exists so that `run.py` stays a command-line interface and
`serve.py` can re-run the same pipeline on a request without going through
argparse.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import timedelta

from .candidates import (
    CYCLE_DAYS,
    CandidateSet,
    extract_utr,
    generate_candidates,
    settlement_members,
)
from .confidence import (
    COMPETITION_CAP,
    DEFAULT_ABSTAIN_BELOW,
    abstains,
    score_all,
)
from .models import ExceptionRecord, Proof, RecordStore
from .money import pct, rupees
from .proof import build_proof, build_proof_from_members
from .verify import enforce_member_exclusivity, verify

TDS_RATE_BP = 1000

# How much unassigned context the model is allowed to see for one residual.
# Not the whole dataset: a window around the credit, capped, so the task is
# "is one of these the missing line" and not "search everything".
LLM_WINDOW_DAYS = 30
LLM_MAX_CONTEXT_RECORDS = 12
LLM_MAX_ADDITIONS = 3


@dataclass
class LoopBFinding:
    """Order versus payment. The second, smaller loop."""

    order_id: str
    payment_ids: list[str]
    ordered: int
    paid: int
    shortfall: int
    verdict: str  # SETTLED | TDS_WITHHELD | SHORT_PAYMENT
    note: str


@dataclass
class PipelineResult:
    store: RecordStore
    proofs: list[Proof]
    accepted: list[Proof]
    exceptions: list[ExceptionRecord]
    candidate_set: CandidateSet
    loop_b: list[LoopBFinding]
    llm_hypotheses: list[dict] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    params: dict = field(default_factory=dict)

    @property
    def accepted_txn_ids(self) -> set[str]:
        return {
            txn_id
            for proof in self.accepted
            for txn_id in proof.members.get("bank_txn_ids", [])
        }

    @property
    def accepted_settlement_ids(self) -> set[str]:
        return {
            settlement_id
            for proof in self.accepted
            for settlement_id in proof.members.get("settlement_ids", [])
        }


# --------------------------------------------------------------------------
# What a human should do about each exception. Every reason code has an
# entry: "could not match" on its own is not an output this system emits.
# --------------------------------------------------------------------------

GUIDANCE = {
    "SUSPECT_UNAPPLIED_REFUND": (
        "The residual equals refund {ids} exactly. Check whether the gateway "
        "netted that refund off this payout while your books date it to an "
        "earlier cycle."
    ),
    "SUSPECT_UNLABELLED_REVERSAL": (
        "The residual equals adjustment {ids}, which carries no payment "
        "reference. Check whether a won chargeback was credited back in this "
        "payout."
    ),
    "SUSPECT_TDS": (
        "The residual is exactly 10% of order {ids}. Check for tax withheld at "
        "source on a B2B invoice rather than a short payment."
    ),
    "ROUNDING_DRIFT": (
        "Residual of {residual}. GST appears to have been rounded on the batch "
        "fee instead of per payment. Re-run with --tolerance-paise 5 to accept "
        "differences this size."
    ),
    "UNEXPLAINED_RESIDUAL": (
        "Residual of {residual} with no matching refund, adjustment or "
        "withholding. Pull the gateway settlement report for this payout and "
        "compare its line items against the cycle."
    ),
    "AMBIGUOUS_MATCH": (
        "More than one settlement balances against this credit ({ids}) and the "
        "narration carries no readable UTR. Ask the bank for the full "
        "reference; amount and date cannot separate these."
    ),
    "LOW_CONFIDENCE": (
        "The best proof balances but scores below the abstention threshold. "
        "Confirm the settlement reference before posting."
    ),
    "MEMBER_CONFLICT": (
        "Records in this proof are already claimed by another passing proof "
        "({ids}). One of the two groupings is wrong; neither is posted."
    ),
    "ORPHAN_CREDIT": (
        "No settlement matches this credit and the narration carries no UTR. "
        "It looks like a direct transfer from a customer - check the "
        "receivables ledger, not the gateway."
    ),
    "NO_CANDIDATE": (
        "The narration names UTR {ids} but no settlement in the batch carries "
        "it. Check whether the settlement report for this payout is missing."
    ),
    "MISSING_BANK_CREDIT": (
        "The gateway settled {ids} but no credit has landed. Expected "
        "{residual} on or after the settlement date - chase the bank if it is "
        "more than one working day late."
    ),
    "SHORT_PAYMENT": (
        "Order {ids} was paid short by {residual} and is not a B2B invoice, so "
        "TDS does not explain it. Check for a partial payment or a failed "
        "retry."
    ),
    "OVERPAYMENT": (
        "Order {ids} was paid {residual} more than its value. Check for a "
        "duplicate capture before refunding anything."
    ),
}


def _guidance(reason: str, ids: list[str], residual: int) -> str:
    template = GUIDANCE.get(
        reason, "Unclassified exception. Inspect the records listed."
    )
    return template.format(ids=", ".join(ids) if ids else "-", residual=rupees(abs(residual)))


# --------------------------------------------------------------------------
# Loop B: order versus payment
# --------------------------------------------------------------------------


def reconcile_orders(store: RecordStore) -> list[LoopBFinding]:
    """Compare what was ordered with what was captured.

    A B2B customer who withholds 10% at source and a customer who simply
    underpays produce the same shape of record. The only thing separating them
    is whether the shortfall is exactly the withholding rate on the order, so
    that is the test - and when it does not hold, the finding says so rather
    than assuming tax.
    """
    by_order: dict[str, list] = {}
    for payment in store.payments.values():
        if payment.status == "captured":
            by_order.setdefault(payment.order_id, []).append(payment)

    findings: list[LoopBFinding] = []
    for order_id in sorted(by_order):
        payments = by_order[order_id]
        order = store.order(order_id)
        paid = sum(p.amount for p in payments)
        shortfall = order.amount - paid
        if shortfall == 0:
            continue
        if shortfall < 0:
            # More money arrived than was ordered. Usually a duplicate
            # capture. It is not a short payment and must not be filed as
            # one - the sign of the difference is the whole diagnosis.
            verdict = "OVERPAYMENT"
            note = "captured more than the order value"
        elif order.is_b2b and shortfall == pct(order.amount, TDS_RATE_BP):
            verdict = "TDS_WITHHELD"
            note = "shortfall is exactly 10% of the order on a B2B invoice"
        else:
            verdict = "SHORT_PAYMENT"
            note = "shortfall does not match any withholding rate"
        findings.append(
            LoopBFinding(
                order_id=order_id,
                payment_ids=sorted(p.payment_id for p in payments),
                ordered=order.amount,
                paid=paid,
                shortfall=shortfall,
                verdict=verdict,
                note=note,
            )
        )
    return findings


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def _select(proofs: list[Proof], abstain_below: float) -> tuple[list[Proof], list[Proof]]:
    """Score, apply the threshold, then enforce exclusivity on what is left.

    Order matters. Exclusivity runs last and only over proofs that are already
    accepted, so a proof nobody trusts cannot reserve records and knock out a
    proof that is trusted.
    """
    scored = score_all(proofs)
    eligible = [p for p in scored if not abstains(p, abstain_below)]
    resolved = enforce_member_exclusivity(eligible)

    resolved_by_id = {p.proof_id: p for p in resolved}
    final = [resolved_by_id.get(p.proof_id, p) for p in scored]
    accepted = [p for p in final if p.verdict == "PASS" and p.confidence >= abstain_below]
    return final, accepted


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------


def _exception_id(prefix: str, index: int) -> str:
    return f"exc_{prefix}_{index:03d}"


def build_exceptions(
    store: RecordStore,
    candidate_set: CandidateSet,
    proofs: list[Proof],
    accepted: list[Proof],
    loop_b: list[LoopBFinding],
) -> list[ExceptionRecord]:
    """Everything the system refused to match, with a reason and a next step."""
    exceptions: list[ExceptionRecord] = []
    covered_txns = {
        txn_id for proof in accepted for txn_id in proof.members.get("bank_txn_ids", [])
    }
    involved_settlements = {
        settlement_id
        for proof in proofs
        for settlement_id in proof.members.get("settlement_ids", [])
    }
    neutralised = candidate_set.neutralised_ids

    proofs_by_txn: dict[str, list[Proof]] = {}
    for proof in proofs:
        for txn_id in proof.members.get("bank_txn_ids", []):
            proofs_by_txn.setdefault(txn_id, []).append(proof)

    # --- credits that did not get matched
    index = 0
    for txn_id in sorted(store.bank_txns):
        txn = store.bank_txns[txn_id]
        if txn_id in covered_txns or txn_id in neutralised or txn.credit_amount <= 0:
            continue
        index += 1
        related = proofs_by_txn.get(txn_id, [])

        if not related:
            utr = extract_utr(txn.narration)
            reason = "NO_CANDIDATE" if utr else "ORPHAN_CREDIT"
            ids = [utr] if utr else []
            exceptions.append(
                ExceptionRecord(
                    exception_id=_exception_id("bnk", index),
                    reason_code=reason,
                    subject_kind="bank_txn",
                    subject_id=txn_id,
                    residual=-txn.credit_amount,
                    at_stake=txn.credit_amount,
                    records_involved=[txn_id],
                    what_to_check=_guidance(reason, ids, txn.credit_amount),
                )
            )
            continue

        conflicted = [p for p in related if (p.verdict_reason or "").startswith("MEMBER_CONFLICT")]
        balanced = [p for p in related if p.verdict == "PASS"]

        if conflicted:
            best = conflicted[0]
            reason = "MEMBER_CONFLICT"
            ids = [(best.verdict_reason or "").split(":", 1)[-1]]
        elif balanced:
            best = max(balanced, key=lambda p: p.confidence)
            rivals = sorted(
                {
                    settlement_id
                    for proof in balanced
                    for settlement_id in proof.members.get("settlement_ids", [])
                }
            )
            if best.confidence <= COMPETITION_CAP and len(rivals) > 1:
                reason = "AMBIGUOUS_MATCH"
                ids = rivals
            else:
                reason = "LOW_CONFIDENCE"
                ids = rivals
        else:
            # Nothing balanced. The closest attempt is the most informative
            # one: smallest unexplained amount, strongest rule to break ties.
            best = min(related, key=lambda p: (abs(p.residual), -p.confidence, p.proof_id))
            reason = (best.verdict_reason or "UNEXPLAINED_RESIDUAL").split(":")[0]
            ids = _suspect_ids(best)
            if reason not in GUIDANCE:
                # Verifier integrity failures (TERM_MISMATCH, FORGED_*) keep
                # their full reason string; they are not ordinary exceptions.
                ids = [best.verdict_reason or ""]
                reason = "UNEXPLAINED_RESIDUAL"

        records_involved = sorted(
            set(best.members.get("bank_txn_ids", []) + best.members.get("settlement_ids", []) + ids)
        )
        at_stake = abs(best.residual) if best.residual else txn.credit_amount
        exceptions.append(
            ExceptionRecord(
                exception_id=_exception_id("bnk", index),
                reason_code=reason,
                subject_kind="bank_txn",
                subject_id=txn_id,
                residual=best.residual,
                at_stake=at_stake,
                records_involved=records_involved,
                what_to_check=_guidance(reason, ids, best.residual),
                confidence=best.confidence,
                proof_id=best.proof_id,
            )
        )

    # --- settlements nothing ever pointed at: the money has not landed
    index = 0
    for settlement_id in sorted(store.settlements):
        if settlement_id in involved_settlements:
            continue
        index += 1
        settlement = store.settlements[settlement_id]
        members = settlement_members(store, settlement)
        exceptions.append(
            ExceptionRecord(
                exception_id=_exception_id("set", index),
                reason_code="MISSING_BANK_CREDIT",
                subject_kind="settlement",
                subject_id=settlement_id,
                residual=settlement.net,
                at_stake=settlement.net,
                records_involved=[settlement_id] + members["payment_ids"][:5],
                what_to_check=_guidance("MISSING_BANK_CREDIT", [settlement_id], settlement.net),
            )
        )

    # --- loop B
    index = 0
    for finding in loop_b:
        if finding.verdict not in {"SHORT_PAYMENT", "OVERPAYMENT"}:
            continue
        index += 1
        exceptions.append(
            ExceptionRecord(
                exception_id=_exception_id("ord", index),
                reason_code=finding.verdict,
                subject_kind="order",
                subject_id=finding.order_id,
                residual=finding.shortfall,
                at_stake=abs(finding.shortfall),
                records_involved=[finding.order_id] + finding.payment_ids,
                what_to_check=_guidance(
                    finding.verdict, [finding.order_id], finding.shortfall
                ),
            )
        )

    return sorted(exceptions, key=lambda e: (-e.at_stake, e.subject_id))


def _suspect_ids(proof: Proof) -> list[str]:
    for entry in proof.evidence:
        if entry.get("check") == "residual_suspects":
            return [value.strip() for value in str(entry.get("value", "")).split(",") if value.strip()]
    return []


# --------------------------------------------------------------------------
# The LLM layer (optional, never load-bearing)
# --------------------------------------------------------------------------


def _unassigned_records(store: RecordStore, cycle_days: int = CYCLE_DAYS) -> dict[str, list[str]]:
    """Refunds and adjustments the cycle rule could not place anywhere.

    These are exactly the records the deterministic side gave up on, and they
    are the only ones the model is shown.
    """
    placed_refunds: set[str] = set()
    placed_adjustments: set[str] = set()
    for settlement in store.settlements.values():
        members = settlement_members(store, settlement, cycle_days)
        placed_refunds.update(members["refund_ids"])
        placed_adjustments.update(members["adjustment_ids"])
    return {
        "refund_ids": sorted(set(store.refunds) - placed_refunds),
        "adjustment_ids": sorted(set(store.adjustments) - placed_adjustments),
    }


def _llm_context(store: RecordStore, proof: Proof, unassigned: dict[str, list[str]]) -> dict:
    """A compact window around one unexplained residual."""
    anchor = store.bank_txn(proof.members["bank_txn_ids"][0])
    horizon = timedelta(days=LLM_WINDOW_DAYS)

    nearby: list[dict] = []
    for refund_id in unassigned["refund_ids"]:
        refund = store.refund(refund_id)
        if abs(refund.created_at.date() - anchor.value_date) <= horizon:
            nearby.append(
                {
                    "id": refund_id,
                    "type": "refund",
                    "amount_paise": refund.amount,
                    "date": refund.created_at.date().isoformat(),
                    "on_payment": refund.payment_id,
                }
            )
    for adjustment_id in unassigned["adjustment_ids"]:
        adjustment = store.adjustment(adjustment_id)
        if abs(adjustment.created_at.date() - anchor.value_date) <= horizon:
            nearby.append(
                {
                    "id": adjustment_id,
                    "type": adjustment.kind,
                    "amount_paise": adjustment.amount,
                    "date": adjustment.created_at.date().isoformat(),
                    "on_payment": adjustment.ref_payment_id,
                }
            )

    nearby.sort(key=lambda entry: (abs(entry["amount_paise"] - abs(proof.residual)), entry["id"]))
    return {
        "bank_txn_id": anchor.bank_txn_id,
        "value_date": anchor.value_date.isoformat(),
        "observed_credit_paise": anchor.credit_amount,
        "computed_net_paise": proof.computed_net,
        "residual_paise": proof.residual,
        "settlement_ids": proof.members.get("settlement_ids", []),
        "current_member_counts": {
            key: len(values) for key, values in sorted(proof.members.items())
        },
        "unassigned_candidates": nearby[:LLM_MAX_CONTEXT_RECORDS],
    }


def _apply_hypothesis(proof: Proof, add_ids: list[str]) -> dict[str, list[str]] | None:
    """Route proposed IDs into the member set by prefix.

    Anything that is not a refund or an adjustment is dropped: the model does
    not get to add payments to a settlement, because a payment's membership is
    a matter of record, not of inference.
    """
    members = {key: list(values) for key, values in proof.members.items()}
    added = 0
    for record_id in add_ids:
        if record_id.startswith("rfnd_"):
            key = "refund_ids"
        elif record_id.startswith("adj_"):
            key = "adjustment_ids"
        else:
            continue
        if record_id in members.get(key, []):
            continue
        members.setdefault(key, []).append(record_id)
        added += 1
    return members if added else None


def run_llm_layer(
    store: RecordStore,
    proofs: list[Proof],
    accepted: list[Proof],
    adjudicator,
    tolerance_paise: int,
) -> tuple[list[Proof], list[dict]]:
    """Ask the model about residuals the deterministic rules could not explain.

    Every answer becomes a proof and goes through `verify()`. The log records
    what was proposed and what survived, which is the only honest way to
    report what the model was worth.
    """
    covered = {
        txn_id for proof in accepted for txn_id in proof.members.get("bank_txn_ids", [])
    }
    unassigned = _unassigned_records(store)
    hypotheses: list[dict] = []
    new_proofs: list[Proof] = []

    unresolved: dict[str, Proof] = {}
    for proof in proofs:
        anchor = proof.members.get("bank_txn_ids", [None])[0]
        if anchor in covered or proof.verdict != "FAIL" or proof.residual == 0:
            continue
        best = unresolved.get(anchor)
        if best is None or abs(proof.residual) < abs(best.residual):
            unresolved[anchor] = proof

    for anchor in sorted(unresolved):
        proof = unresolved[anchor]
        context = _llm_context(store, proof, unassigned)
        if not context["unassigned_candidates"]:
            continue

        proposal = adjudicator.propose(context)
        record = {
            "bank_txn_id": anchor,
            "residual_paise": proof.residual,
            "proposed": [],
            "reason": "",
            "outcome": "no_hypothesis",
        }
        if proposal is None:
            hypotheses.append(record)
            continue

        record["proposed"] = proposal.add_members[:LLM_MAX_ADDITIONS]
        record["reason"] = proposal.reason
        members = _apply_hypothesis(proof, record["proposed"])
        if members is None:
            record["outcome"] = "rejected_unusable"
            hypotheses.append(record)
            continue

        candidate_proof = build_proof_from_members(
            store,
            "R5_LLM_HYPOTHESIS",
            members,
            evidence=[
                {"check": "llm_reason", "value": proposal.reason},
                {"check": "llm_added", "value": ", ".join(record["proposed"])},
                {"check": "llm_model", "value": proposal.model},
            ],
        )
        checked = verify(candidate_proof, store, tolerance_paise)
        record["outcome"] = "accepted_by_verifier" if checked.verdict == "PASS" else "rejected_by_verifier"
        record["verdict_reason"] = checked.verdict_reason
        record["proof_id"] = checked.proof_id
        hypotheses.append(record)
        new_proofs.append(checked)

    return new_proofs, hypotheses


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


TAMPERABLE_FIELDS = {
    "pay_": "amount",
    "rfnd_": "amount",
    "adj_": "amount",
    "bnk_": "credit_amount",
    "set_": "net",
}


def apply_tamper(store: RecordStore, record_id: str, delta_paise: int = 100) -> dict:
    """Corrupt one source amount in place, after the proofs were built.

    This is the demo, and it is deliberately the real thing: nothing here
    touches a proof or a verdict. It changes one number in the source data and
    lets the verifier read it again. If the stamp flips, it flips because the
    arithmetic no longer holds.
    """
    prefix = record_id[: record_id.find("_") + 1]
    field_name = TAMPERABLE_FIELDS.get(prefix)
    if field_name is None:
        raise ValueError(
            f"cannot tamper with {record_id!r}: expected a payment, refund, "
            "adjustment, settlement or bank transaction id"
        )
    original = store.any_record(record_id)
    before = getattr(original, field_name)
    modified = original.model_copy(update={field_name: before + delta_paise})
    store.replace(modified)
    return {
        "record_id": record_id,
        "field": field_name,
        "before": before,
        "after": before + delta_paise,
        "delta": delta_paise,
    }


def reverify(
    previous: PipelineResult,
    store: RecordStore,
    *,
    abstain_below: float = DEFAULT_ABSTAIN_BELOW,
    tolerance_paise: int = 0,
) -> PipelineResult:
    """Re-run verification and selection over existing proofs.

    Used after `apply_tamper`: the proofs stay exactly as they were built, and
    only the verifier's reading of the source data changes.
    """
    verified = [verify(proof, store, tolerance_paise) for proof in previous.proofs]
    scored, accepted = _select(verified, abstain_below)
    loop_b = reconcile_orders(store)
    exceptions = build_exceptions(store, previous.candidate_set, scored, accepted, loop_b)
    return PipelineResult(
        store=store,
        proofs=sorted(scored, key=lambda p: p.proof_id),
        accepted=sorted(accepted, key=lambda p: p.bank_txn_id),
        exceptions=exceptions,
        candidate_set=previous.candidate_set,
        loop_b=loop_b,
        llm_hypotheses=previous.llm_hypotheses,
        elapsed_seconds=previous.elapsed_seconds,
        params=dict(previous.params),
    )


def run_pipeline(
    store: RecordStore,
    *,
    abstain_below: float = DEFAULT_ABSTAIN_BELOW,
    tolerance_paise: int = 0,
    adjudicator=None,
) -> PipelineResult:
    started = time.perf_counter()

    candidate_set = generate_candidates(store)
    proofs = [build_proof(store, candidate) for candidate in candidate_set.candidates]
    verified = [verify(proof, store, tolerance_paise) for proof in proofs]
    scored, accepted = _select(verified, abstain_below)

    hypotheses: list[dict] = []
    if adjudicator is not None:
        extra, hypotheses = run_llm_layer(
            store, scored, accepted, adjudicator, tolerance_paise
        )
        if extra:
            # Re-score everything together: a new passing proof changes what
            # counts as a rival, so the whole batch is re-selected rather than
            # patched.
            scored, accepted = _select(verified + extra, abstain_below)

    loop_b = reconcile_orders(store)
    exceptions = build_exceptions(store, candidate_set, scored, accepted, loop_b)
    elapsed = time.perf_counter() - started

    return PipelineResult(
        store=store,
        proofs=sorted(scored, key=lambda p: p.proof_id),
        accepted=sorted(accepted, key=lambda p: p.bank_txn_id),
        exceptions=exceptions,
        candidate_set=candidate_set,
        loop_b=loop_b,
        llm_hypotheses=hypotheses,
        elapsed_seconds=elapsed,
        params={
            "abstain_below": abstain_below,
            "tolerance_paise": tolerance_paise,
            "llm": adjudicator is not None,
        },
    )
