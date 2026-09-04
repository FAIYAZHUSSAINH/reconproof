"""Turning a candidate into a claim.

A Proof is written to read like a page from a ledger, because that is what it
is: the terms are in accounting order, each carries the IDs it was computed
from, and the last three lines are computed net, observed credit, and the
residual between them.

Nothing here decides anything. Everything this module writes into a Proof is a
claim that `verify.py` recomputes from the raw records and is free to
contradict.
"""

from __future__ import annotations

import hashlib

from .candidates import CYCLE_DAYS, Candidate, settlement_members
from .models import ADJUSTMENT_SIGN, Proof, ProofTerm, RecordStore

# The ledger identity, in the order a finance team would write it down:
#
#   gross payments - fee - GST on fee - refunds - chargebacks + reversals = net
#
# `verify.py` builds this same list independently. The duplication is
# deliberate: if the verifier imported the builder, it would only be checking
# that the code agrees with itself.
TERM_SPECS = (
    ("Gross payments", 1, "payment_ids"),
    ("Gateway fee", -1, "payment_ids"),
    ("GST on fee", -1, "payment_ids"),
    ("Refunds", -1, "refund_ids"),
    ("Chargeback deductions", -1, "adjustment_ids"),
    ("Chargeback reversals", 1, "adjustment_ids"),
    ("Commission corrections", 1, "adjustment_ids"),
)


def build_members(
    store: RecordStore, candidate: Candidate, cycle_days: int = CYCLE_DAYS
) -> dict[str, list[str]]:
    """Collect the line items behind every settlement in the candidate."""
    payments: list[str] = []
    refunds: list[str] = []
    adjustments: list[str] = []
    for settlement_id in candidate.settlement_ids:
        members = settlement_members(store, store.settlement(settlement_id), cycle_days)
        payments.extend(members["payment_ids"])
        refunds.extend(members["refund_ids"])
        adjustments.extend(members["adjustment_ids"])
    return {
        "payment_ids": sorted(set(payments)),
        "refund_ids": sorted(set(refunds)),
        "adjustment_ids": sorted(set(adjustments)),
        "bank_txn_ids": sorted(candidate.bank_txn_ids),
        "settlement_ids": sorted(candidate.settlement_ids),
    }


def build_terms(store: RecordStore, members: dict[str, list[str]]) -> list[ProofTerm]:
    """One line per non-empty term, in accounting order."""
    payments = [store.payment(pid) for pid in members.get("payment_ids", [])]
    refunds = [store.refund(rid) for rid in members.get("refund_ids", [])]
    adjustments = [store.adjustment(aid) for aid in members.get("adjustment_ids", [])]

    by_kind: dict[str, list] = {kind: [] for kind in ADJUSTMENT_SIGN}
    for adjustment in adjustments:
        by_kind[adjustment.kind].append(adjustment)

    amounts = {
        "Gross payments": (
            sum(p.amount for p in payments),
            [p.payment_id for p in payments],
        ),
        "Gateway fee": (sum(p.fee for p in payments), [p.payment_id for p in payments]),
        "GST on fee": (sum(p.tax for p in payments), [p.payment_id for p in payments]),
        "Refunds": (sum(r.amount for r in refunds), [r.refund_id for r in refunds]),
        "Chargeback deductions": (
            sum(a.amount for a in by_kind["chargeback_deduction"]),
            [a.adjustment_id for a in by_kind["chargeback_deduction"]],
        ),
        "Chargeback reversals": (
            sum(a.amount for a in by_kind["chargeback_reversal"]),
            [a.adjustment_id for a in by_kind["chargeback_reversal"]],
        ),
        "Commission corrections": (
            sum(a.amount for a in by_kind["commission_correction"]),
            [a.adjustment_id for a in by_kind["commission_correction"]],
        ),
    }

    terms: list[ProofTerm] = []
    for label, sign, _ in TERM_SPECS:
        amount, source_ids = amounts[label]
        if not source_ids:
            continue
        terms.append(
            ProofTerm(label=label, sign=sign, amount=amount, source_ids=sorted(source_ids))
        )
    return terms


def proof_id_for(rule: str, members: dict[str, list[str]]) -> str:
    """A stable ID derived from the claim itself.

    Content-addressed rather than a counter, so the same run on the same seed
    produces the same proof IDs and a diff of two report.json files is
    readable.
    """
    digest = hashlib.sha1()
    digest.update(rule.encode())
    for key in sorted(members):
        digest.update(key.encode())
        for value in members[key]:
            digest.update(value.encode())
    return f"prf_{digest.hexdigest()[:10]}"


def build_proof(
    store: RecordStore, candidate: Candidate, cycle_days: int = CYCLE_DAYS
) -> Proof:
    """Assemble the claim. Verdict stays UNVERIFIED until the verifier rules."""
    members = build_members(store, candidate, cycle_days)
    terms = build_terms(store, members)

    computed_net = sum(term.signed for term in terms)
    observed_credit = sum(
        store.bank_txn(txn_id).credit_amount for txn_id in members["bank_txn_ids"]
    )

    evidence: list[dict] = [{"check": key, "value": value} for key, value in candidate.evidence]
    declared_net = sum(
        store.settlement(sid).net for sid in members["settlement_ids"]
    )
    if declared_net != computed_net:
        # Not a failure by itself - ROUNDING_DRIFT lives here - but a fact
        # worth carrying: the gateway's own net and the sum of its line items
        # disagree.
        evidence.append(
            {
                "check": "settlement_declared_net",
                "value": str(declared_net),
                "note": "gateway's declared net differs from the sum of its line items",
            }
        )
    evidence.append(
        {
            "check": "member_counts",
            "value": ", ".join(
                f"{key.replace('_ids', '')}={len(members[key])}" for key in sorted(members)
            ),
        }
    )

    return Proof(
        proof_id=proof_id_for(candidate.rule, members),
        bank_txn_id=members["bank_txn_ids"][0],
        settlement_id=members["settlement_ids"][0] if members["settlement_ids"] else None,
        rule=candidate.rule,
        terms=terms,
        computed_net=computed_net,
        observed_credit=observed_credit,
        residual=computed_net - observed_credit,
        members=members,
        evidence=evidence,
    )


def build_proof_from_members(
    store: RecordStore,
    rule: str,
    members: dict[str, list[str]],
    evidence: list[dict] | None = None,
) -> Proof:
    """Build a proof from an explicit member set.

    Used by the LLM layer: a hypothesis is nothing but a member set, and it
    goes through the same construction and the same verifier as everything
    else. There is no path into a match that skips this.
    """
    normalised = {key: sorted(set(values)) for key, values in members.items()}
    terms = build_terms(store, normalised)
    computed_net = sum(term.signed for term in terms)
    observed_credit = sum(
        store.bank_txn(txn_id).credit_amount for txn_id in normalised["bank_txn_ids"]
    )
    return Proof(
        proof_id=proof_id_for(rule, normalised),
        bank_txn_id=normalised["bank_txn_ids"][0],
        settlement_id=(
            normalised["settlement_ids"][0] if normalised.get("settlement_ids") else None
        ),
        rule=rule,
        terms=terms,
        computed_net=computed_net,
        observed_credit=observed_credit,
        residual=computed_net - observed_credit,
        members=normalised,
        evidence=list(evidence or []),
    )
