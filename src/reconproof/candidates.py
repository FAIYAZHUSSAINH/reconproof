"""Candidate generation: every plausible match, and no opinion about which one wins.

This module is deliberately generous. It proposes; it does not decide. A
greedy matcher that stops at the first plausible hit is how reconciliation
tools end up with a high match rate and quiet errors underneath it - the first
version of this file did exactly that (see WHAT_BROKE.md). So every rule that
fires produces a candidate, and the verifier throws out whatever does not
balance.

Four deterministic rules, each tagged so the proof records what proposed it:

    R1_UTR_EXACT    the bank narration carries the settlement's UTR
    R2_AMOUNT_DATE  net equals the credit, within a 3-day value-date window
    R3_SPLIT        a group of credits sums to one settlement's net
    R4_MERGE        a group of settlements sums to one credit

R5_RESIDUAL_HYPOTHESIS is not here. That slot belongs to the LLM layer in
`adjudicate.py`, and it is kept empty on purpose: the point of the experiment
is to measure what the model's proposals are worth, which you cannot do if a
hand-written search has already picked up the same cases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import combinations

from .models import RecordStore, Settlement

# A NEFT UTR: four-letter bank code, then 'N', then eleven characters.
# The 'N' is doing real work. An IFSC code (HDFC0000123) is also four letters
# followed by seven, and narrations in this batch carry both - but an IFSC
# always has '0' in the fifth position. Anchoring on 'N' is what stops the
# matcher from confidently reconciling against a branch code.
UTR_PATTERN = re.compile(r"\b[A-Z]{4}N[0-9A-Z]{11}\b")

# Caps. A 150-record batch with an uncapped subset search does not finish.
MAX_SUBSET_SIZE = 3
SUBSET_WINDOW_DAYS = 5
AMOUNT_WINDOW_DAYS = 3
CYCLE_DAYS = 2

RULE_PRIORITY = {
    "R1_UTR_EXACT": 0,
    "R2_AMOUNT_DATE": 1,
    "R3_SPLIT": 2,
    "R4_MERGE": 3,
    "R5_LLM_HYPOTHESIS": 4,
}


@dataclass(frozen=True)
class Candidate:
    """A proposal: these bank credits correspond to these settlements."""

    rule: str
    bank_txn_ids: tuple[str, ...]
    settlement_ids: tuple[str, ...]
    evidence: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> tuple[frozenset[str], frozenset[str]]:
        return frozenset(self.bank_txn_ids), frozenset(self.settlement_ids)


@dataclass
class CandidateSet:
    candidates: list[Candidate] = field(default_factory=list)
    # Credits the bank posted and pulled back the same day under the same UTR.
    # They net to zero and are excluded from matching, but they are reported,
    # not hidden: unexplained statement lines are how errors survive an audit.
    neutralised_pairs: list[tuple[str, str]] = field(default_factory=list)
    cap_hits: list[dict] = field(default_factory=list)

    @property
    def neutralised_ids(self) -> set[str]:
        return {txn for pair in self.neutralised_pairs for txn in pair}


def extract_utr(narration: str) -> str | None:
    """Pull a UTR out of a bank narration, or return None."""
    match = UTR_PATTERN.search(narration.upper())
    return match.group(0) if match else None


def find_reversal_pairs(store: RecordStore) -> list[tuple[str, str]]:
    """Same UTR, same value date, equal and opposite amounts.

    A credit posted in error and pulled back. Matching either leg to a
    settlement would count the money twice, and the second leg would look like
    an unexplained debit forever.
    """
    pairs: list[tuple[str, str]] = []
    used: set[str] = set()
    by_utr: dict[str, list[str]] = {}
    for txn_id in sorted(store.bank_txns):
        utr = extract_utr(store.bank_txns[txn_id].narration)
        if utr:
            by_utr.setdefault(utr, []).append(txn_id)

    for utr in sorted(by_utr):
        group = by_utr[utr]
        for credit_id, debit_id in combinations(group, 2):
            if credit_id in used or debit_id in used:
                continue
            credit = store.bank_txns[credit_id]
            debit = store.bank_txns[debit_id]
            if (
                credit.credit_amount == -debit.credit_amount
                and credit.credit_amount != 0
                and credit.value_date == debit.value_date
            ):
                first, second = sorted([credit_id, debit_id])
                pairs.append((first, second))
                used.update({credit_id, debit_id})
    return pairs


def settlement_members(
    store: RecordStore, settlement: Settlement, cycle_days: int = CYCLE_DAYS
) -> dict[str, list[str]]:
    """The line items a settlement should contain, by the T+2 cycle rule.

    This is the only join available. A merchant's own payment and refund book
    does not carry the gateway's settlement id, so membership has to be
    inferred from when the money moved: a payment captured on the 1st is paid
    out on the 3rd. `status` matters - a failed payment sitting in the same
    window is not settled money.

    Where this rule is wrong is where the interesting cases live. A refund
    dated to the day the customer asked for it, or a chargeback reversal
    carrying the date of the original dispute, will not be found here, and the
    residual is what tells you so.
    """
    target = settlement.settled_at
    payments = sorted(
        p.payment_id
        for p in store.payments.values()
        if p.status == "captured"
        and p.captured_at.date() + timedelta(days=cycle_days) == target
    )
    refunds = sorted(
        r.refund_id
        for r in store.refunds.values()
        if r.created_at.date() + timedelta(days=cycle_days) == target
    )
    adjustments = sorted(
        a.adjustment_id
        for a in store.adjustments.values()
        if a.created_at.date() + timedelta(days=cycle_days) == target
    )
    return {
        "payment_ids": payments,
        "refund_ids": refunds,
        "adjustment_ids": adjustments,
    }


def _within(left: date, right: date, days: int) -> bool:
    return abs((left - right).days) <= days


def generate_candidates(
    store: RecordStore,
    *,
    max_subset_size: int = MAX_SUBSET_SIZE,
    subset_window_days: int = SUBSET_WINDOW_DAYS,
    amount_window_days: int = AMOUNT_WINDOW_DAYS,
) -> CandidateSet:
    """Every candidate the four deterministic rules can see."""
    result = CandidateSet(neutralised_pairs=find_reversal_pairs(store))
    skip = result.neutralised_ids

    # Only genuine credits are matchable. A debit line is either half of a
    # reversal pair (already handled) or something a human needs to look at.
    live_txns = [
        store.bank_txns[txn_id]
        for txn_id in sorted(store.bank_txns)
        if txn_id not in skip and store.bank_txns[txn_id].credit_amount > 0
    ]
    settlements = [store.settlements[sid] for sid in sorted(store.settlements)]

    seen: dict[tuple[frozenset[str], frozenset[str]], Candidate] = {}

    def offer(candidate: Candidate) -> None:
        existing = seen.get(candidate.key)
        if existing is None or RULE_PRIORITY[candidate.rule] < RULE_PRIORITY[existing.rule]:
            seen[candidate.key] = candidate

    utr_by_txn = {txn.bank_txn_id: extract_utr(txn.narration) for txn in live_txns}

    # R1 - the UTR in the narration names the settlement outright. If a
    # settlement's UTR appears on several credits, they are proposed together:
    # that is what a split payout looks like on a statement.
    for settlement in settlements:
        hits = [txn for txn in live_txns if utr_by_txn[txn.bank_txn_id] == settlement.utr]
        if hits:
            offer(
                Candidate(
                    rule="R1_UTR_EXACT",
                    bank_txn_ids=tuple(sorted(t.bank_txn_id for t in hits)),
                    settlement_ids=(settlement.settlement_id,),
                    evidence=(
                        ("utr", settlement.utr),
                        ("narration", hits[0].narration),
                    ),
                )
            )

    # R2 - the amount is exactly right and the date is close enough.
    for txn in live_txns:
        for settlement in settlements:
            if settlement.net == txn.credit_amount and _within(
                settlement.settled_at, txn.value_date, amount_window_days
            ):
                offer(
                    Candidate(
                        rule="R2_AMOUNT_DATE",
                        bank_txn_ids=(txn.bank_txn_id,),
                        settlement_ids=(settlement.settlement_id,),
                        evidence=(
                            ("net", str(settlement.net)),
                            ("value_date_gap_days", str((txn.value_date - settlement.settled_at).days)),
                        ),
                    )
                )

    # R3 - one settlement paid out in several legs.
    for settlement in settlements:
        window = [
            txn
            for txn in live_txns
            if _within(txn.value_date, settlement.settled_at, subset_window_days)
        ]
        for size in range(2, max_subset_size + 1):
            if _too_many(len(window), size):
                result.cap_hits.append(
                    {
                        "rule": "R3_SPLIT",
                        "subject": settlement.settlement_id,
                        "window_size": len(window),
                        "subset_size": size,
                    }
                )
                continue
            for group in combinations(window, size):
                if sum(t.credit_amount for t in group) == settlement.net:
                    offer(
                        Candidate(
                            rule="R3_SPLIT",
                            bank_txn_ids=tuple(sorted(t.bank_txn_id for t in group)),
                            settlement_ids=(settlement.settlement_id,),
                            evidence=(("legs", str(size)),),
                        )
                    )

    # R4 - several settlements paid as one credit.
    for txn in live_txns:
        window = [
            settlement
            for settlement in settlements
            if _within(settlement.settled_at, txn.value_date, subset_window_days)
        ]
        for size in range(2, max_subset_size + 1):
            if _too_many(len(window), size):
                result.cap_hits.append(
                    {
                        "rule": "R4_MERGE",
                        "subject": txn.bank_txn_id,
                        "window_size": len(window),
                        "subset_size": size,
                    }
                )
                continue
            for group in combinations(window, size):
                if sum(s.net for s in group) == txn.credit_amount:
                    offer(
                        Candidate(
                            rule="R4_MERGE",
                            bank_txn_ids=(txn.bank_txn_id,),
                            settlement_ids=tuple(sorted(s.settlement_id for s in group)),
                            evidence=(("settlements_merged", str(size)),),
                        )
                    )

    result.candidates = sorted(
        seen.values(),
        key=lambda c: (RULE_PRIORITY[c.rule], c.bank_txn_ids, c.settlement_ids),
    )
    return result


# Above this many combinations we stop and log it rather than grinding. The
# limit is stated in the README as a real limitation: a batch dense enough to
# hit it would need a smarter search than combinations().
MAX_COMBINATIONS = 20_000


def _too_many(window_size: int, subset_size: int) -> bool:
    if window_size < subset_size:
        return False
    count = 1
    for i in range(subset_size):
        count = count * (window_size - i) // (i + 1)
        if count > MAX_COMBINATIONS:
            return True
    return False
