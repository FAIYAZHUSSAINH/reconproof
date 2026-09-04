"""THE VERIFIER.

This file decides what is true. Everything else in the system only proposes.

It imports `models` and `money` and nothing else from this project - no
candidate generator, no proof builder, no LLM client. That restriction is not
tidiness, it is the entire architecture: a verifier that shares code with the
thing it checks is checking that the code agrees with itself.
`tests/test_trust_boundary.py` parses the imports of this file and fails the
build if that ever stops being true.

What it does, in order:

1. Fetches every member ID from the raw records. A member it cannot find, a
   duplicate, or a payment that never succeeded is a failed proof.
2. Rebuilds the ledger identity from those records - its own arithmetic, from
   its own reading of the source amounts. It never uses a figure cached inside
   the proof.
3. Compares its result with what the proof claimed, term by term. A
   disagreement means the proof was built from different data than the data
   on disk right now - either the matcher is buggy or a source record changed
   underneath it - and the reason names the term that broke.
4. Checks the proof against itself. Terms, computed net and residual have to
   agree with one another; nothing in this repo emits a proof where they do
   not, so a contradiction means the file was edited by hand.
5. Compares computed net with the money that actually landed in the bank, and
   classifies whatever is left over.

A residual of zero paise is the only thing that makes a match. Not a high
score, not a confident model, not a small difference. Zero.
"""

from __future__ import annotations

from .models import (
    ADJUSTMENT_SIGN,
    MissingRecord,
    Proof,
    ProofTerm,
    RecordStore,
)
from .money import Paise, pct

# The ledger identity, declared here independently of the proof builder.
# If these two lists ever disagree, every proof fails loudly with
# TERM_MISMATCH rather than quietly balancing on the wrong terms.
GROSS = "Gross payments"
FEE = "Gateway fee"
GST = "GST on fee"
REFUNDS = "Refunds"
CHARGEBACKS = "Chargeback deductions"
REVERSALS = "Chargeback reversals"
CORRECTIONS = "Commission corrections"

# Withholding rate used only to recognise a TDS-shaped residual, in basis
# points. Recognising is not accepting: it names a hypothesis for a human.
TDS_RATE_BP = 1000

# A residual this small or smaller is a rounding artefact, not a missing
# record. Five paise across a batch of a hundred-odd payments is what
# line-level versus batch-level GST rounding produces.
ROUNDING_PAISE = 5

MEMBER_KEYS = (
    "payment_ids",
    "refund_ids",
    "adjustment_ids",
    "bank_txn_ids",
    "settlement_ids",
)


def _rebuild_terms(proof: Proof, records: RecordStore) -> list[ProofTerm]:
    """Recompute every term of the identity from the raw records.

    Each amount is read from the record the ID points at, right now. If the
    proof says pay_0031 contributed Rs 4,120.00 and pay_0031 says otherwise,
    the record wins.
    """
    payment_ids = sorted(proof.members.get("payment_ids", []))
    refund_ids = sorted(proof.members.get("refund_ids", []))
    adjustment_ids = sorted(proof.members.get("adjustment_ids", []))

    payments = [records.payment(pid) for pid in payment_ids]
    refunds = [records.refund(rid) for rid in refund_ids]
    adjustments = [records.adjustment(aid) for aid in adjustment_ids]

    kinds: dict[str, list] = {kind: [] for kind in ADJUSTMENT_SIGN}
    for adjustment in adjustments:
        kinds[adjustment.kind].append(adjustment)

    lines: list[tuple[str, int, Paise, list[str]]] = [
        (GROSS, 1, sum(p.amount for p in payments), payment_ids),
        (FEE, -1, sum(p.fee for p in payments), payment_ids),
        (GST, -1, sum(p.tax for p in payments), payment_ids),
        (REFUNDS, -1, sum(r.amount for r in refunds), refund_ids),
        (
            CHARGEBACKS,
            -1,
            sum(a.amount for a in kinds["chargeback_deduction"]),
            sorted(a.adjustment_id for a in kinds["chargeback_deduction"]),
        ),
        (
            REVERSALS,
            1,
            sum(a.amount for a in kinds["chargeback_reversal"]),
            sorted(a.adjustment_id for a in kinds["chargeback_reversal"]),
        ),
        (
            CORRECTIONS,
            1,
            sum(a.amount for a in kinds["commission_correction"]),
            sorted(a.adjustment_id for a in kinds["commission_correction"]),
        ),
    ]
    return [
        ProofTerm(label=label, sign=sign, amount=amount, source_ids=ids)
        for label, sign, amount, ids in lines
        if ids
    ]


def _structural_failure(proof: Proof, records: RecordStore) -> str | None:
    """Checks that do not need arithmetic. Returns a reason code or None."""
    for key in MEMBER_KEYS:
        ids = proof.members.get(key, [])
        if len(set(ids)) != len(ids):
            duplicate = next(i for i in ids if ids.count(i) > 1)
            return f"DUPLICATE_MEMBER:{duplicate}"

    if not proof.members.get("bank_txn_ids"):
        return "NO_BANK_CREDIT"

    for key, fetch in (
        ("payment_ids", records.payment),
        ("refund_ids", records.refund),
        ("adjustment_ids", records.adjustment),
        ("bank_txn_ids", records.bank_txn),
        ("settlement_ids", records.settlement),
    ):
        for record_id in proof.members.get(key, []):
            try:
                fetch(record_id)
            except MissingRecord:
                return f"MISSING_RECORD:{record_id}"

    # Money that never moved cannot settle. A failed payment sitting in the
    # right date window is the obvious way to make a stubborn residual
    # disappear, so the verifier refuses it outright - including when the
    # suggestion came from the model.
    for payment_id in proof.members.get("payment_ids", []):
        payment = records.payment(payment_id)
        if payment.status != "captured":
            return f"INELIGIBLE_MEMBER:{payment_id}"

    return None


def _claim_failure(proof: Proof, terms: list[ProofTerm], computed_net: Paise) -> str | None:
    """Does the proof's own arithmetic survive being redone?

    Two different questions live here, and keeping them apart is what took
    the longest to get right (see WHAT_BROKE.md, 4 Sep):

    1. *Is the proof self-consistent?* A proof states its terms, its computed
       net and its residual. Those three have to agree with each other. If
       they do not, the file was edited by hand after it was built - there is
       no code path in this repo that produces an internally contradictory
       proof. That is forgery, and it fails outright.
    2. *Does the proof still agree with the data?* Recompute every term from
       the records and compare. A disagreement here means the proof was built
       from different data than the data on disk right now, and the reason
       names the term that moved.

    What is deliberately *not* here is the observed bank credit. That figure
    is a straight read of one field on one record, so there is no arithmetic
    to forge; if it has changed underneath the proof, the honest output is a
    residual with a diagnosis, not an accusation. Rule 3 says the record
    wins, and a residual is the thing a finance team can act on.
    """
    claimed = {term.label: term for term in proof.terms}
    recomputed = {term.label: term for term in terms}

    for label in recomputed:
        if label not in claimed:
            return f"TERM_MISSING:{label}"
    for label in claimed:
        if label not in recomputed:
            return f"TERM_NOT_SUPPORTED:{label}"
    for label, term in recomputed.items():
        other = claimed[label]
        if (
            other.amount != term.amount
            or other.sign != term.sign
            or sorted(other.source_ids) != sorted(term.source_ids)
        ):
            return f"TERM_MISMATCH:{label}"

    # Self-consistency. The terms above are now known to match the records,
    # so a stated net that does not equal their sum was written, not computed.
    if proof.computed_net != computed_net:
        return "FORGED_COMPUTED_NET"
    if proof.residual != proof.computed_net - proof.observed_credit:
        # The proof's three closing figures contradict one another. Whichever
        # was edited, the credit line is the one that no longer follows.
        return "FORGED_OBSERVED_CREDIT"
    return None


def classify_residual(
    residual: Paise, proof: Proof, records: RecordStore
) -> tuple[str, list[str]]:
    """Name the most likely explanation for what is left over.

    The residual is the diagnosis. Its size and sign say what is missing, and
    a reason code with the records to check is worth more to a finance team
    than a match would have been.

    An exact amount match against a record that exists is stronger evidence
    than an arithmetic coincidence, so the adjustment check runs before the
    TDS check - the reverse of the order these were first written in.
    """
    magnitude = abs(residual)
    member_refunds = set(proof.members.get("refund_ids", []))
    member_adjustments = set(proof.members.get("adjustment_ids", []))

    # A refund the gateway netted off this payout that the cycle rule did not
    # place. The commonest real cause of a positive residual.
    refund_hits = sorted(
        refund.refund_id
        for refund in records.refunds.values()
        if refund.amount == magnitude and refund.refund_id not in member_refunds
    )
    if refund_hits and magnitude >= 100:
        return "SUSPECT_UNAPPLIED_REFUND", refund_hits[:3]

    if magnitude <= ROUNDING_PAISE:
        return "ROUNDING_DRIFT", []

    # A chargeback that came back. The reversal carries no reference to the
    # payment, but its amount still equals the deduction it reverses.
    adjustment_hits = sorted(
        adjustment.adjustment_id
        for adjustment in records.adjustments.values()
        if adjustment.amount == magnitude
        and adjustment.adjustment_id not in member_adjustments
    )
    if adjustment_hits:
        return "SUSPECT_UNLABELLED_REVERSAL", adjustment_hits[:3]

    # Tax withheld at source: the residual is exactly a tenth of an order.
    tds_hits = sorted(
        order.order_id
        for order in records.orders.values()
        if pct(order.amount, TDS_RATE_BP) == magnitude
    )
    if tds_hits:
        return "SUSPECT_TDS", tds_hits[:3]

    return "UNEXPLAINED_RESIDUAL", []


def verify(proof: Proof, records: RecordStore, tolerance_paise: int = 0) -> Proof:
    """Independently recompute the proof from the RAW records.

    Trusts nothing inside `proof` except the member IDs. Returns a new Proof
    carrying the verifier's own figures and its verdict; the input is never
    mutated.
    """
    verified = proof.model_copy(deep=True)

    structural = _structural_failure(proof, records)
    if structural is not None:
        verified.verdict = "FAIL"
        verified.verdict_reason = structural
        verified.evidence = list(verified.evidence) + [
            {"check": "verifier", "value": "rejected before arithmetic", "note": structural}
        ]
        return verified

    terms = _rebuild_terms(proof, records)
    computed_net = sum(term.signed for term in terms)
    observed_credit = sum(
        records.bank_txn(txn_id).credit_amount
        for txn_id in proof.members.get("bank_txn_ids", [])
    )
    residual = computed_net - observed_credit

    # From here on the verifier reports its own numbers, not the claim's.
    verified.terms = terms
    verified.computed_net = computed_net
    verified.observed_credit = observed_credit
    verified.residual = residual

    evidence = list(verified.evidence)
    evidence.append(
        {
            "check": "verifier",
            "value": (
                f"recomputed from {len(proof.members.get('payment_ids', []))} payments, "
                f"{len(proof.members.get('refund_ids', []))} refunds, "
                f"{len(proof.members.get('adjustment_ids', []))} adjustments"
            ),
        }
    )

    if observed_credit != proof.observed_credit:
        # Not a verdict on its own: the record is the authority, so the
        # verifier simply uses it and lets the residual speak. Recorded
        # because "the credit moved after the proof was built" is exactly
        # what --tamper does to a bank transaction, and the derivation
        # should say so out loud.
        evidence.append(
            {
                "check": "observed_credit",
                "value": f"proof claimed {proof.observed_credit}, record says {observed_credit}",
                "note": "source record changed after the proof was built",
            }
        )

    claim = _claim_failure(proof, terms, computed_net)
    if claim is not None:
        verified.verdict = "FAIL"
        verified.verdict_reason = claim
        evidence.append(
            {
                "check": "claim_audit",
                "value": claim,
                "note": "the proof's own figures do not survive recomputation",
            }
        )
        verified.evidence = evidence
        return verified

    if residual == 0:
        verified.verdict = "PASS"
        verified.verdict_reason = None
    elif tolerance_paise > 0 and abs(residual) <= tolerance_paise:
        # Only reachable when a tolerance was asked for explicitly. The
        # default is zero, because a tolerance is a policy decision about how
        # much unexplained money is acceptable, and that is not the
        # verifier's call to make quietly.
        verified.verdict = "PASS"
        verified.verdict_reason = "within_tolerance"
    else:
        reason, suspects = classify_residual(residual, proof, records)
        verified.verdict = "FAIL"
        verified.verdict_reason = reason
        if suspects:
            evidence.append(
                {"check": "residual_suspects", "value": ", ".join(suspects)}
            )

    verified.evidence = evidence
    return verified


def enforce_member_exclusivity(proofs: list[Proof]) -> list[Proof]:
    """No source record may be claimed by two passing proofs.

    The classic reconciliation bug is double counting: two settlements both
    "balance" because the same payment was spent twice. Each proof can be
    individually correct and the batch still wrong, so this check is global
    and runs after verification.

    Proofs are considered strongest first (confidence, then zero residual,
    then ID for a stable tie-break). A later proof that reuses any already
    claimed record is demoted to FAIL with MEMBER_CONFLICT, naming the proof
    that holds the record.
    """
    ordered = sorted(
        proofs,
        key=lambda p: (-p.confidence, abs(p.residual), p.proof_id),
    )
    claimed: dict[str, str] = {}
    resolved: list[Proof] = []

    for proof in ordered:
        if proof.verdict != "PASS":
            resolved.append(proof)
            continue

        conflicts = [
            (record_id, claimed[record_id])
            for record_id in proof.all_member_ids
            if record_id in claimed
        ]
        if conflicts:
            record_id, holder = conflicts[0]
            demoted = proof.model_copy(deep=True)
            demoted.verdict = "FAIL"
            demoted.verdict_reason = f"MEMBER_CONFLICT:{record_id}"
            demoted.evidence = list(demoted.evidence) + [
                {
                    "check": "exclusivity",
                    "value": f"{record_id} already claimed by {holder}",
                    "note": "a record cannot settle twice",
                }
            ]
            resolved.append(demoted)
            continue

        for record_id in proof.all_member_ids:
            claimed[record_id] = proof.proof_id
        resolved.append(proof)

    return sorted(resolved, key=lambda p: p.proof_id)
