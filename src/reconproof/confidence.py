"""How sure are we, and when do we refuse to answer.

Confidence here is not a probability from a model. It is a deterministic score
built from what the evidence actually was: which rule fired, whether the
arithmetic came out at zero, and - the part that matters - whether anything
else also fits.

The abstention threshold is the point of the whole file. Below it the system
files a typed exception instead of a match. On this data that costs several
percentage points of match rate and buys a false-match rate of zero, which is
the trade the track brief asks for.
"""

from __future__ import annotations

from .models import Proof

# What each rule is worth before any adjustment.
#
# R1 is a bank reference number printed on the statement; it is as close to a
# fact as this domain offers. R2 is an exact amount within a three-day window,
# which is strong but is the kind of coincidence that does happen. The subset
# rules are inference: several credits that happen to add up.
BASE_BY_RULE = {
    "R1_UTR_EXACT": 0.98,
    "R2_AMOUNT_DATE": 0.85,
    "R3_SPLIT": 0.70,
    "R4_MERGE": 0.70,
    # A hypothesis from the model. Deliberately the lowest score the system
    # will ever accept: 0.75 plus the zero-residual bonus lands on 0.77, one
    # notch above the default abstention threshold. Raise --abstain-below at
    # all and every model-proposed match disappears from the scorecard, which
    # is the knob a finance team should have. It is worth nothing whatever
    # unless the verifier has already balanced it to zero paise.
    "R5_LLM_HYPOTHESIS": 0.75,
}

ZERO_RESIDUAL_BONUS = 0.02

# A subset match with no rival grouping is materially stronger than one of
# several ways to hit the same total. Without this, an exactly balanced,
# unambiguous split payout would score 0.72 and abstain - which would be
# abstaining from a proof, not from a guess.
UNIQUE_GROUPING_BONUS = 0.06

# Sixty-plus line items in one proof usually means the member rule swept in a
# neighbouring cycle. It can still balance; it should still be looked at.
LARGE_MEMBER_COUNT = 60
LARGE_MEMBER_PENALTY = 0.03

# When two proofs both balance against the same credit, the arithmetic cannot
# separate them and neither can we. Capping below any usable threshold is how
# the system says "I do not know" instead of picking one.
COMPETITION_CAP = 0.50

# Nothing is certain. A ceiling below 1.0 keeps that visible in the output.
MAX_CONFIDENCE = 0.99

DEFAULT_ABSTAIN_BELOW = 0.75


def score(
    proof: Proof,
    *,
    has_rival: bool,
    unique_grouping: bool,
) -> float:
    """Score one verified proof."""
    value = BASE_BY_RULE.get(proof.rule, 0.60)

    if proof.verdict == "PASS" and proof.residual == 0:
        value += ZERO_RESIDUAL_BONUS
    if unique_grouping and proof.rule in {"R3_SPLIT", "R4_MERGE"}:
        value += UNIQUE_GROUPING_BONUS
    if len(proof.members.get("payment_ids", [])) > LARGE_MEMBER_COUNT:
        value -= LARGE_MEMBER_PENALTY

    value = min(value, MAX_CONFIDENCE)
    if has_rival:
        value = min(value, COMPETITION_CAP)
    return round(max(value, 0.0), 4)


def score_all(proofs: list[Proof]) -> list[Proof]:
    """Score a batch, taking rivalry into account.

    Rivalry is only counted between proofs that *both* passed verification. A
    candidate the verifier rejected is not a competing explanation, it is a
    disproved one - which is why the merged-credit case, where the UTR rule
    proposes a plausible but arithmetically wrong match, does not end up
    suppressing the correct answer.
    """
    passing = [p for p in proofs if p.verdict == "PASS"]

    holders: dict[str, list[Proof]] = {}
    for proof in passing:
        for record_id in proof.members.get("bank_txn_ids", []) + proof.members.get(
            "settlement_ids", []
        ):
            holders.setdefault(record_id, []).append(proof)

    scored: list[Proof] = []
    for proof in proofs:
        rivals: set[str] = set()
        if proof.verdict == "PASS":
            for record_id in proof.members.get("bank_txn_ids", []) + proof.members.get(
                "settlement_ids", []
            ):
                for other in holders.get(record_id, []):
                    if other.proof_id != proof.proof_id:
                        rivals.add(other.proof_id)

        updated = proof.model_copy(deep=True)
        updated.confidence = score(
            proof, has_rival=bool(rivals), unique_grouping=not rivals
        )
        if rivals:
            updated.evidence = list(updated.evidence) + [
                {
                    "check": "competing_proofs",
                    "value": ", ".join(sorted(rivals)),
                    "note": "another proof balances against the same records",
                }
            ]
        scored.append(updated)
    return scored


def abstains(proof: Proof, abstain_below: float = DEFAULT_ABSTAIN_BELOW) -> bool:
    """A proof is a match only if it passed AND cleared the threshold.

    A FAIL is never promoted, whatever its confidence. There is no branch in
    this system where a high score rescues a proof that did not balance.
    """
    return proof.verdict != "PASS" or proof.confidence < abstain_below
