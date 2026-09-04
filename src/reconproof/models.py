"""Records, the Proof object, and the record store.

Two things live here and nothing else: the shape of the raw data, and the shape
of a claim about that data. Keeping them in one module with no logic is what
lets `verify.py` import this file without importing any of the matcher.

Every money field is `StrictInt`. Pydantic's strict mode refuses a float or a
numeric string outright, so a `4143.82` in a CSV column fails at the boundary
with a readable error instead of becoming a silent rounding error six stages
later.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .money import Paise

# Money in a pydantic model. StrictInt is the float guard at the schema layer;
# money.assert_paise is the same guard at the function layer. Both, on purpose.
Money = Annotated[StrictInt, Field(description="integer paise")]


# --------------------------------------------------------------------------
# Vocabulary shared by the generator, the verifier and the report.
# --------------------------------------------------------------------------

# The 13 labelled case types the generator injects. Ground truth tags every
# settlement, orphan credit and short payment with one of these, and the
# scorecard breaks the match rate down by them.
CASE_TYPES: tuple[str, ...] = (
    "CLEAN",
    "WITH_REFUND",
    "CHARGEBACK_DEDUCTION",
    "UNLABELLED_REVERSAL",
    "SPLIT_SETTLEMENT",
    "MERGED_CREDIT",
    "ROUNDING_DRIFT",
    "AMOUNT_COLLISION",
    "NARRATION_NOISE",
    "MISSING_BANK_CREDIT",
    "ORPHAN_CREDIT",
    "TDS_SHORT_PAYMENT",
    "DUPLICATE_UTR",
)

AdjustmentKind = Literal[
    "chargeback_deduction", "chargeback_reversal", "commission_correction"
]

# The sign each adjustment kind carries in the ledger identity. A deduction
# takes money away from the merchant; a reversal and a commission correction
# give it back. This map is the single place that decision is made, and both
# the proof builder and the verifier read it — a sign convention is a fact
# about the domain, not an implementation the verifier should re-derive.
ADJUSTMENT_SIGN: dict[str, int] = {
    "chargeback_deduction": -1,
    "chargeback_reversal": +1,
    "commission_correction": +1,
}

# Every exception this system can emit. A bare "could not match" is banned;
# each of these names a specific hypothesis a human can go and check.
REASON_CODES: tuple[str, ...] = (
    "SUSPECT_UNAPPLIED_REFUND",
    "SUSPECT_UNLABELLED_REVERSAL",
    "SUSPECT_TDS",
    "ROUNDING_DRIFT",
    "UNEXPLAINED_RESIDUAL",
    "AMBIGUOUS_MATCH",
    "LOW_CONFIDENCE",
    "ORPHAN_CREDIT",
    "MISSING_BANK_CREDIT",
    "MEMBER_CONFLICT",
    "NO_CANDIDATE",
    "SHORT_PAYMENT",
    "OVERPAYMENT",
    # The verifier disagreed with the proof itself rather than with the money.
    # Kept as their own codes because "could not match" would be a lie about
    # what happened: nothing here is a reconciliation difference.
    "SOURCE_RECORD_CHANGED",
    "INVALID_PROOF",
)


# --------------------------------------------------------------------------
# Raw records. These mirror what a merchant actually holds: their own order and
# payment book, the gateway's settlement report, and a bank statement.
# --------------------------------------------------------------------------


class Order(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str
    customer_id: str
    amount: Money
    created_at: datetime
    is_b2b: bool


class Payment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payment_id: str
    order_id: str
    amount: Money
    fee: Money  # gateway MDR on this payment
    tax: Money  # GST on that fee, not on the sale
    method: Literal["card", "upi", "netbanking"]
    captured_at: datetime
    status: Literal["captured", "failed"]


class Refund(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refund_id: str
    payment_id: str
    amount: Money
    created_at: datetime


class Adjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adjustment_id: str
    kind: AdjustmentKind
    amount: Money  # always positive; `kind` carries the sign
    # Deliberately optional. A chargeback the merchant wins comes back weeks
    # later as a generic adjustment line with no pointer to the payment it
    # relates to. That single missing field is the hardest case in the batch.
    ref_payment_id: str | None
    created_at: datetime


class Settlement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settlement_id: str
    utr: str
    settled_at: date
    gross: Money
    fee: Money
    tax: Money
    refund_total: Money
    # Signed: reversals and corrections positive, chargeback deductions
    # negative, so net = gross - fee - tax - refund_total + adjustment_total.
    adjustment_total: Money
    net: Money


class BankTxn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bank_txn_id: str
    value_date: date
    narration: str
    # Negative on a same-day reversal of a credit the bank posted in error.
    credit_amount: Money


# --------------------------------------------------------------------------
# The claim.
# --------------------------------------------------------------------------


class ProofTerm(BaseModel):
    """One line of the accounting derivation."""

    model_config = ConfigDict(extra="forbid")

    label: str
    sign: Literal[1, -1]
    amount: Money
    source_ids: list[str]

    @property
    def signed(self) -> Paise:
        return self.sign * self.amount


class Proof(BaseModel):
    """A machine-checkable claim that one bank credit equals one settlement.

    Written to `report.json` in full. Someone reading that file with no access
    to this code should be able to follow the arithmetic and check it by hand.
    """

    model_config = ConfigDict(extra="forbid")

    proof_id: str
    bank_txn_id: str  # anchor credit; the full list is members["bank_txn_ids"]
    settlement_id: str | None  # anchor settlement; full list in members
    rule: str  # which rule proposed this, e.g. R1_UTR_EXACT
    terms: list[ProofTerm]
    computed_net: Money
    observed_credit: Money
    residual: Money  # computed_net - observed_credit
    members: dict[str, list[str]]
    evidence: list[dict] = Field(default_factory=list)
    confidence: float = 0.0
    verdict: Literal["PASS", "FAIL", "UNVERIFIED"] = "UNVERIFIED"
    verdict_reason: str | None = None

    def member_ids(self, key: str) -> list[str]:
        return list(self.members.get(key, []))

    @property
    def all_member_ids(self) -> list[str]:
        out: list[str] = []
        for key in sorted(self.members):
            out.extend(self.members[key])
        return out


class ExceptionRecord(BaseModel):
    """Something the system refused to match, and what to do about it."""

    model_config = ConfigDict(extra="forbid")

    exception_id: str
    reason_code: str
    subject_kind: Literal["bank_txn", "settlement", "order"]
    subject_id: str
    residual: Money
    at_stake: Money  # rupee value a human is being asked to look at
    records_involved: list[str]
    what_to_check: str
    confidence: float | None = None
    proof_id: str | None = None
    case_type: str | None = None  # ground truth label, filled in by report.py


# --------------------------------------------------------------------------
# The record store.
# --------------------------------------------------------------------------


class MissingRecord(KeyError):
    """A proof referenced an ID that does not exist in the source data."""


# ID prefixes, so any string in a proof can be routed to its table.
ID_PREFIXES = {
    "ord_": "orders",
    "pay_": "payments",
    "rfnd_": "refunds",
    "adj_": "adjustments",
    "set_": "settlements",
    "bnk_": "bank_txns",
}


@dataclass
class RecordStore:
    """The raw records, indexed by ID.

    This is what the verifier is handed. It holds source data only — no
    proofs, no candidates, no scores — so there is nothing in it that a
    matcher could have quietly pre-computed.
    """

    orders: dict[str, Order] = dc_field(default_factory=dict)
    payments: dict[str, Payment] = dc_field(default_factory=dict)
    refunds: dict[str, Refund] = dc_field(default_factory=dict)
    adjustments: dict[str, Adjustment] = dc_field(default_factory=dict)
    settlements: dict[str, Settlement] = dc_field(default_factory=dict)
    bank_txns: dict[str, BankTxn] = dc_field(default_factory=dict)

    # -- typed lookups. Each raises rather than returning None, because a
    # -- missing member ID is a failed proof, not a zero.

    def payment(self, record_id: str) -> Payment:
        try:
            return self.payments[record_id]
        except KeyError:
            raise MissingRecord(record_id) from None

    def refund(self, record_id: str) -> Refund:
        try:
            return self.refunds[record_id]
        except KeyError:
            raise MissingRecord(record_id) from None

    def adjustment(self, record_id: str) -> Adjustment:
        try:
            return self.adjustments[record_id]
        except KeyError:
            raise MissingRecord(record_id) from None

    def settlement(self, record_id: str) -> Settlement:
        try:
            return self.settlements[record_id]
        except KeyError:
            raise MissingRecord(record_id) from None

    def bank_txn(self, record_id: str) -> BankTxn:
        try:
            return self.bank_txns[record_id]
        except KeyError:
            raise MissingRecord(record_id) from None

    def order(self, record_id: str) -> Order:
        try:
            return self.orders[record_id]
        except KeyError:
            raise MissingRecord(record_id) from None

    def table_for(self, record_id: str) -> dict:
        for prefix, table in ID_PREFIXES.items():
            if record_id.startswith(prefix):
                return getattr(self, table)
        raise MissingRecord(f"unrecognised id prefix: {record_id}")

    def any_record(self, record_id: str) -> BaseModel:
        """Fetch a record of any kind by ID. Used by the /api/record endpoint."""
        table = self.table_for(record_id)
        try:
            return table[record_id]
        except KeyError:
            raise MissingRecord(record_id) from None

    def counts(self) -> dict[str, int]:
        return {
            "orders": len(self.orders),
            "payments": len(self.payments),
            "refunds": len(self.refunds),
            "adjustments": len(self.adjustments),
            "settlements": len(self.settlements),
            "bank_txns": len(self.bank_txns),
        }

    @property
    def total_records(self) -> int:
        return sum(self.counts().values())

    def replace(self, record: BaseModel) -> None:
        """Swap one record for a modified copy. Used only by --tamper.

        Dispatch is on the record's type, not on which id attribute it happens
        to have: a Payment carries both `payment_id` and `order_id`, and
        picking the wrong one would file it in the wrong table.
        """
        table_and_key = {
            Order: (self.orders, "order_id"),
            Payment: (self.payments, "payment_id"),
            Refund: (self.refunds, "refund_id"),
            Adjustment: (self.adjustments, "adjustment_id"),
            Settlement: (self.settlements, "settlement_id"),
            BankTxn: (self.bank_txns, "bank_txn_id"),
        }.get(type(record))
        if table_and_key is None:
            raise MissingRecord(f"not a source record: {type(record).__name__}")
        table, key = table_and_key
        table[getattr(record, key)] = record
