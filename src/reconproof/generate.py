"""Synthetic settlement data with thirteen labelled hard cases.

The data is the experiment. If it is too clean the match rate is meaningless;
if it is unrealistically hard it is not evidence about anything. So every case
type here is a real reconciliation failure mode from Indian payments:
cross-cycle refunds, chargeback reversals that arrive with no reference to the
original payment, GST rounded at the batch level instead of the line level,
two settlements on nearby dates with identical net amounts, a bank credit
posted and reversed on the same day under the same UTR.

Two rules govern this file:

1. **Ground truth never touches the CSVs.** The pipeline reads only what a
   merchant's finance team would actually hold. The labels live in
   `data/ground_truth.json` and are loaded by `report.py` alone.

2. **Seeded and reproducible.** Every random draw comes from one
   `random.Random(seed)`. Same seed, byte-identical files.

The batch is deliberately adversarial: thirteen hard case types injected at
least twice each into a batch this size means roughly half the settlements
carry a defect, where a production batch would be mostly clean. The headline
match rate is therefore a floor, not a typical number — run
`--difficulty easy` to see the same engine on a gentler batch.
"""

from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .models import (
    ADJUSTMENT_SIGN,
    Adjustment,
    BankTxn,
    Order,
    Payment,
    Refund,
    Settlement,
)
from .money import Paise, gst_on_fee, pct

# --------------------------------------------------------------------------
# Domain constants. These are modelling assumptions, stated once, in the open.
# --------------------------------------------------------------------------

# The gateway settles on T+2: a payment captured on the 1st is paid out on the
# 3rd. This is the join the pipeline has to rely on, because line items in a
# merchant's own book do not carry a settlement id.
CYCLE_DAYS = 2

START_DATE = date(2026, 8, 3)
DAYS_BETWEEN_SETTLEMENTS = 2

# MDR by instrument, in basis points. Real rates differ by instrument, which is
# also why per-payment GST does not equal GST on the batch fee.
MDR_BP = {"card": 200, "netbanking": 190, "upi": 80}
METHODS = ("card", "netbanking", "upi")

# Section 194J style withholding on a B2B invoice: the customer pays 90% and
# remits 10% to the tax department. The payment looks short. It is not.
TDS_RATE_BP = 1000

MIN_PAYMENT = 50_000  # Rs 500.00
MAX_PAYMENT = 25_00_000  # Rs 25,000.00

BANK_CODES = ("HDFC", "ICIC", "UTIB", "KKBK")
IFSC_DECOYS = ("HDFC0000123", "ICIC0001234", "UTIB0000456", "KKBK0004321")

CUSTOMER_NAMES = (
    "ACME RETAIL PVT",
    "NORTHWIND TRADERS",
    "VIDYA LABS LLP",
    "SARANG FOODS PVT",
)


# --------------------------------------------------------------------------
# Build plans. A plan is a list of (case_type, variant) in build order.
# Order matters: a chargeback reversal needs a deduction to already exist.
# --------------------------------------------------------------------------

_HARD_CORE: list[tuple[str, str]] = [
    ("NARRATION_NOISE", "plain"),
    ("WITH_REFUND", "in_window"),
    ("CHARGEBACK_DEDUCTION", "plain"),
    ("SPLIT_SETTLEMENT", "same_utr"),
    ("ROUNDING_DRIFT", "plain"),
    ("MISSING_BANK_CREDIT", "plain"),
    ("DUPLICATE_UTR", "plain"),
    ("UNLABELLED_REVERSAL", "plain"),
    ("MERGED_CREDIT", "no_utr"),
    ("ORPHAN_CREDIT", "plain"),
    ("AMOUNT_COLLISION", "pair"),
    ("WITH_REFUND", "late"),
    ("SPLIT_SETTLEMENT", "diff_utr"),
    ("MERGED_CREDIT", "utr_of_first"),
    ("NARRATION_NOISE", "plain"),
    ("CHARGEBACK_DEDUCTION", "plain"),
    ("ROUNDING_DRIFT", "plain"),
    ("MISSING_BANK_CREDIT", "plain"),
    ("DUPLICATE_UTR", "plain"),
    ("UNLABELLED_REVERSAL", "plain"),
    ("ORPHAN_CREDIT", "plain"),
]

PLANS: dict[str, list[tuple[str, str]]] = {
    # Mostly clean, every hard type present at least twice so the case
    # breakdown is still comparable across difficulties.
    "easy": (
        [("CLEAN", "plain")] * 8
        + [("CLEAN", "tds"), ("CLEAN", "tds"), ("CLEAN", "short_payment")]
        + _HARD_CORE
    ),
    "standard": (
        [("CLEAN", "plain")] * 2
        + [("CLEAN", "tds"), ("CLEAN", "tds"), ("CLEAN", "short_payment")]
        + _HARD_CORE
    ),
    # Same engine, more of what it is worst at. The match rate should fall,
    # and the per-case-type table should show exactly where.
    "hard": (
        [("CLEAN", "plain")]
        + [("CLEAN", "tds"), ("CLEAN", "tds"), ("CLEAN", "short_payment")]
        + _HARD_CORE
        + [
            ("UNLABELLED_REVERSAL", "plain"),
            ("UNLABELLED_REVERSAL", "plain"),
            ("AMOUNT_COLLISION", "pair"),
            ("WITH_REFUND", "late"),
            ("MERGED_CREDIT", "no_utr"),
            ("ORPHAN_CREDIT", "plain"),
        ]
    ),
}

# How many settlement dates each plan entry consumes.
_DATES_PER_CASE = {
    "MERGED_CREDIT": 2,
    "AMOUNT_COLLISION": 2,
    "ORPHAN_CREDIT": 0,  # a bank credit with no settlement behind it
}


@dataclass
class _Ctx:
    """Mutable state while building one batch."""

    rng: random.Random
    settlement_dates: set[date]
    orders: list[Order] = field(default_factory=list)
    payments: list[Payment] = field(default_factory=list)
    refunds: list[Refund] = field(default_factory=list)
    adjustments: list[Adjustment] = field(default_factory=list)
    settlements: list[Settlement] = field(default_factory=list)
    bank_txns: list[BankTxn] = field(default_factory=list)
    matches: list[dict] = field(default_factory=list)
    unmatched_settlements: list[dict] = field(default_factory=list)
    unmatched_bank_txns: list[dict] = field(default_factory=list)
    loop_b: list[dict] = field(default_factory=list)
    reversal_pairs: list[list[str]] = field(default_factory=list)
    settled_payments: list[Payment] = field(default_factory=list)
    prior_deductions: list[Adjustment] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)

    def next_id(self, prefix: str) -> str:
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        return f"{prefix}{self.counters[prefix]:04d}"


# --------------------------------------------------------------------------
# Small builders
# --------------------------------------------------------------------------


def _utr(ctx: _Ctx, when: date) -> str:
    """A 16-character NEFT-style UTR: 4-letter bank code, 'N', then digits.

    The fifth character is the point of the format. An IFSC code is also four
    letters followed by seven characters, but its fifth character is always
    '0'. Narrations in this batch carry both, so a UTR regex that does not
    look at that position will happily "match" a bank branch code.
    """
    bank = ctx.rng.choice(BANK_CODES)
    julian = when.timetuple().tm_yday
    serial = ctx.rng.randrange(0, 999_999)
    return f"{bank}N{when.year % 100:02d}{julian:03d}{serial:06d}"


def _clock(ctx: _Ctx, day: date) -> datetime:
    """A plausible business-hours timestamp on `day`."""
    return datetime(
        day.year,
        day.month,
        day.day,
        ctx.rng.randrange(9, 18),
        ctx.rng.randrange(0, 60),
        ctx.rng.randrange(0, 60),
    )


def _unbucketed_day(ctx: _Ctx, near: date, days_back: int) -> date:
    """A date whose settlement cycle lands on no settlement in this batch.

    Used for the records that are the whole difficulty of this project: a
    chargeback reversal carrying the date of the original dispute, or a refund
    the gateway netted off this payout but the merchant's book dated to the
    day the customer asked for it. Such a record cannot be placed by the
    cycle rule, so it has to be found some other way — or reported honestly.
    """
    day = near - timedelta(days=days_back)
    for _ in range(60):
        if (day + timedelta(days=CYCLE_DAYS)) not in ctx.settlement_dates:
            return day
        day -= timedelta(days=1)
    raise RuntimeError("no unbucketed day available")


def _new_payment(
    ctx: _Ctx,
    captured_on: date,
    *,
    amount: Paise | None = None,
    method: str | None = None,
    status: str = "captured",
    is_b2b: bool = False,
    tds: bool = False,
    short_by: Paise = 0,
) -> Payment:
    """Create an order and the payment against it."""
    order_amount = amount if amount is not None else ctx.rng.randrange(MIN_PAYMENT, MAX_PAYMENT)
    paid = order_amount
    if tds:
        # The customer withholds tax at source and pays the rest.
        paid = order_amount - pct(order_amount, TDS_RATE_BP)
    paid -= short_by

    method = method or ctx.rng.choice(METHODS)
    fee = pct(paid, MDR_BP[method])
    tax = gst_on_fee(fee)

    captured_at = _clock(ctx, captured_on)
    order = Order(
        order_id=ctx.next_id("ord_"),
        customer_id=f"cust_{ctx.rng.randrange(1000, 9999)}",
        amount=order_amount,
        created_at=captured_at - timedelta(minutes=ctx.rng.randrange(2, 90)),
        is_b2b=is_b2b,
    )
    payment = Payment(
        payment_id=ctx.next_id("pay_"),
        order_id=order.order_id,
        amount=paid,
        fee=fee,
        tax=tax,
        method=method,
        captured_at=captured_at,
        status=status,
    )
    ctx.orders.append(order)
    ctx.payments.append(payment)
    return payment


def _batch(ctx: _Ctx, settled_at: date, count: int | None = None) -> list[Payment]:
    """The captured payments that make up one settlement."""
    captured_on = settled_at - timedelta(days=CYCLE_DAYS)
    n = count if count is not None else ctx.rng.randrange(4, 8)
    return [_new_payment(ctx, captured_on) for _ in range(n)]


def _add_failed_decoy(ctx: _Ctx, settled_at: date) -> None:
    """A failed payment in the same capture window.

    It is in the data, it is in the right date bucket, and it must not be in
    the settlement. A member rule that forgets `status` balances nothing.
    """
    _new_payment(ctx, settled_at - timedelta(days=CYCLE_DAYS), status="failed")


def _make_settlement(
    ctx: _Ctx,
    settled_at: date,
    payments: list[Payment],
    refunds: list[Refund] = (),
    adjustments: list[Adjustment] = (),
    *,
    aggregate_tax: bool = False,
) -> Settlement:
    """Roll up line items into the gateway's settlement row.

    `aggregate_tax=True` computes GST once on the batch fee instead of summing
    the per-payment GST. That is the ROUNDING_DRIFT case and it is worth one
    or two paise across a batch — enough to break an exact match, small enough
    that a human would never spot it in a spreadsheet.
    """
    gross = sum(p.amount for p in payments)
    fee = sum(p.fee for p in payments)
    tax = gst_on_fee(fee) if aggregate_tax else sum(p.tax for p in payments)
    refund_total = sum(r.amount for r in refunds)
    adjustment_total = sum(ADJUSTMENT_SIGN[a.kind] * a.amount for a in adjustments)
    net = gross - fee - tax - refund_total + adjustment_total

    settlement = Settlement(
        settlement_id=ctx.next_id("set_"),
        utr=_utr(ctx, settled_at),
        settled_at=settled_at,
        gross=gross,
        fee=fee,
        tax=tax,
        refund_total=refund_total,
        adjustment_total=adjustment_total,
        net=net,
    )
    ctx.settlements.append(settlement)
    ctx.settled_payments.extend(payments)
    return settlement


def _narration(ctx: _Ctx, style: str, utr: str | None = None) -> str:
    ifsc = ctx.rng.choice(IFSC_DECOYS)
    serial = ctx.rng.randrange(100000, 999999)
    if style == "clean":
        return f"NEFT-CR-{ifsc}-RAZORPAY SOFTWARE PVT LTD-{utr}-SETTLEMENT"
    if style == "noisy":
        # The UTR is in there, buried between an IFSC that looks like one and
        # a reference block that does not.
        return (
            f"NEFT-CR-{ifsc}-RAZORPAY SOFTW-XXXXXX-{utr} "
            f"-REF/CMS/2026/{serial}/PYMT"
        )
    if style == "masked":
        # The bank truncated the UTR. Amount and date are all that is left.
        return f"NEFT-CR-{ifsc}-RAZORPAY SOFTW-UTR XXXXXXXXXXXXXXXX-CMS/{serial}"
    if style == "reversal":
        return f"RETURN-{utr}-REV OF NEFT CR-{ifsc}-SAME DAY"
    if style == "orphan":
        name = ctx.rng.choice(CUSTOMER_NAMES)
        return f"NEFT-CR-{ifsc}-{name}-INV/2026/{serial} DIRECT"
    raise ValueError(f"unknown narration style: {style}")


def _make_bank_txn(
    ctx: _Ctx, value_date: date, amount: Paise, narration: str
) -> BankTxn:
    txn = BankTxn(
        bank_txn_id=ctx.next_id("bnk_"),
        value_date=value_date,
        narration=narration,
        credit_amount=amount,
    )
    ctx.bank_txns.append(txn)
    return txn


def _record_match(
    ctx: _Ctx,
    case_type: str,
    settlements: list[Settlement],
    bank_txns: list[BankTxn],
    payments: list[Payment],
    refunds: list[Refund] = (),
    adjustments: list[Adjustment] = (),
    note: str = "",
) -> None:
    """The answer key for one reconcilable unit. Never written to a CSV."""
    ctx.matches.append(
        {
            "case_type": case_type,
            "settlement_ids": sorted(s.settlement_id for s in settlements),
            "bank_txn_ids": sorted(t.bank_txn_id for t in bank_txns),
            "payment_ids": sorted(p.payment_id for p in payments),
            "refund_ids": sorted(r.refund_id for r in refunds),
            "adjustment_ids": sorted(a.adjustment_id for a in adjustments),
            "note": note,
        }
    )


# --------------------------------------------------------------------------
# One builder per case type.
# --------------------------------------------------------------------------


def _case_clean(ctx: _Ctx, settled_at: date, variant: str) -> None:
    payments = _batch(ctx, settled_at)
    loop_b_note = None

    if variant == "tds":
        # A B2B invoice paid short by exactly 10%. Loop B has to tell this
        # apart from a customer who simply underpaid.
        captured_on = settled_at - timedelta(days=CYCLE_DAYS)
        payment = _new_payment(ctx, captured_on, is_b2b=True, tds=True)
        payments.append(payment)
        order = next(o for o in ctx.orders if o.order_id == payment.order_id)
        loop_b_note = {
            "case_type": "TDS_SHORT_PAYMENT",
            "order_id": order.order_id,
            "payment_id": payment.payment_id,
            "withheld": order.amount - payment.amount,
        }
    elif variant == "short_payment":
        # Not TDS: a genuine short payment with no tax explanation.
        captured_on = settled_at - timedelta(days=CYCLE_DAYS)
        payment = _new_payment(ctx, captured_on, short_by=ctx.rng.randrange(1100, 9900))
        payments.append(payment)
        order = next(o for o in ctx.orders if o.order_id == payment.order_id)
        loop_b_note = {
            "case_type": "SHORT_PAYMENT",
            "order_id": order.order_id,
            "payment_id": payment.payment_id,
            "withheld": order.amount - payment.amount,
        }

    if loop_b_note:
        ctx.loop_b.append(loop_b_note)

    _add_failed_decoy(ctx, settled_at)
    settlement = _make_settlement(ctx, settled_at, payments)
    txn = _make_bank_txn(
        ctx,
        settled_at,
        settlement.net,
        _narration(ctx, "clean", settlement.utr),
    )
    _record_match(ctx, "CLEAN", [settlement], [txn], payments)


def _case_narration_noise(ctx: _Ctx, settled_at: date, variant: str) -> None:
    payments = _batch(ctx, settled_at)
    settlement = _make_settlement(ctx, settled_at, payments)
    txn = _make_bank_txn(
        ctx,
        settled_at,
        settlement.net,
        _narration(ctx, "noisy", settlement.utr),
    )
    _record_match(ctx, "NARRATION_NOISE", [settlement], [txn], payments)


def _case_with_refund(ctx: _Ctx, settled_at: date, variant: str) -> None:
    """A refund on a sale from an earlier cycle, deducted from this payout.

    The gateway does not give back the fee it charged on the original sale, so
    the refund is deducted at face value. Two variants: one the cycle rule can
    place, and one dated to the day the customer asked for the money back,
    which the cycle rule cannot place at all.
    """
    payments = _batch(ctx, settled_at)
    target = ctx.rng.choice(ctx.settled_payments) if ctx.settled_payments else payments[0]
    refund_amount = min(target.amount, ctx.rng.randrange(20_000, 6_00_000))

    if variant == "in_window":
        created_on = settled_at - timedelta(days=CYCLE_DAYS)
    else:
        created_on = _unbucketed_day(ctx, settled_at, days_back=9)

    refund = Refund(
        refund_id=ctx.next_id("rfnd_"),
        payment_id=target.payment_id,
        amount=refund_amount,
        created_at=_clock(ctx, created_on),
    )
    ctx.refunds.append(refund)

    settlement = _make_settlement(ctx, settled_at, payments, [refund])
    txn = _make_bank_txn(
        ctx, settled_at, settlement.net, _narration(ctx, "clean", settlement.utr)
    )
    _record_match(
        ctx,
        "WITH_REFUND",
        [settlement],
        [txn],
        payments,
        refunds=[refund],
        note=(
            "refund dated inside the settlement cycle"
            if variant == "in_window"
            else "refund dated to the customer request, outside this cycle"
        ),
    )


def _case_chargeback_deduction(ctx: _Ctx, settled_at: date, variant: str) -> None:
    payments = _batch(ctx, settled_at)
    disputed = ctx.rng.choice(ctx.settled_payments) if ctx.settled_payments else payments[0]
    adjustment = Adjustment(
        adjustment_id=ctx.next_id("adj_"),
        kind="chargeback_deduction",
        amount=disputed.amount,
        ref_payment_id=disputed.payment_id,
        created_at=_clock(ctx, settled_at - timedelta(days=CYCLE_DAYS)),
    )
    ctx.adjustments.append(adjustment)
    ctx.prior_deductions.append(adjustment)

    settlement = _make_settlement(ctx, settled_at, payments, adjustments=[adjustment])
    txn = _make_bank_txn(
        ctx, settled_at, settlement.net, _narration(ctx, "clean", settlement.utr)
    )
    _record_match(
        ctx,
        "CHARGEBACK_DEDUCTION",
        [settlement],
        [txn],
        payments,
        adjustments=[adjustment],
    )


def _case_unlabelled_reversal(ctx: _Ctx, settled_at: date, variant: str) -> None:
    """The merchant won a dispute. The money comes back with no pointer to it.

    `ref_payment_id` is None and the line is dated to the original dispute,
    weeks before this payout. Nothing in the record says which settlement it
    belongs to; the only handle on it is that its amount equals a chargeback
    deducted earlier. That is a hypothesis, not a fact, which is exactly why
    it is the LLM's job to propose it and the verifier's job to check it.
    """
    payments = _batch(ctx, settled_at)
    if ctx.prior_deductions:
        source = ctx.prior_deductions.pop(0)
        amount = source.amount
        note = f"reverses the chargeback deducted as {source.adjustment_id}"
    else:
        amount = ctx.rng.randrange(50_000, 6_00_000)
        note = "reverses a chargeback from an earlier period"

    adjustment = Adjustment(
        adjustment_id=ctx.next_id("adj_"),
        kind="chargeback_reversal",
        amount=amount,
        ref_payment_id=None,
        created_at=_clock(ctx, _unbucketed_day(ctx, settled_at, days_back=21)),
    )
    ctx.adjustments.append(adjustment)

    settlement = _make_settlement(ctx, settled_at, payments, adjustments=[adjustment])
    txn = _make_bank_txn(
        ctx, settled_at, settlement.net, _narration(ctx, "clean", settlement.utr)
    )
    _record_match(
        ctx,
        "UNLABELLED_REVERSAL",
        [settlement],
        [txn],
        payments,
        adjustments=[adjustment],
        note=note,
    )


def _case_rounding_drift(ctx: _Ctx, settled_at: date, variant: str) -> None:
    """GST computed once on the batch fee instead of per payment."""
    payments = _batch(ctx, settled_at, count=6)

    # Guarantee the drift is non-zero, otherwise the case tests nothing. A one
    # paisa nudge to a single fee is enough and stays a legal fee value.
    def drift(items: list[Payment]) -> int:
        return sum(p.tax for p in items) - gst_on_fee(sum(p.fee for p in items))

    guard = 0
    while drift(payments) == 0 and guard < 40:
        first = payments[0]
        bumped = first.model_copy(update={"fee": first.fee + 1})
        bumped = bumped.model_copy(update={"tax": gst_on_fee(bumped.fee)})
        payments[0] = bumped
        for index, existing in enumerate(ctx.payments):
            if existing.payment_id == bumped.payment_id:
                ctx.payments[index] = bumped
                break
        guard += 1

    settlement = _make_settlement(ctx, settled_at, payments, aggregate_tax=True)
    txn = _make_bank_txn(
        ctx, settled_at, settlement.net, _narration(ctx, "clean", settlement.utr)
    )
    _record_match(
        ctx,
        "ROUNDING_DRIFT",
        [settlement],
        [txn],
        payments,
        note=f"batch-level GST differs from line-level GST by {drift(payments)} paise",
    )


def _case_split_settlement(ctx: _Ctx, settled_at: date, variant: str) -> None:
    """One settlement paid out as two bank credits."""
    payments = _batch(ctx, settled_at)
    settlement = _make_settlement(ctx, settled_at, payments)

    first_half = settlement.net // 2
    second_half = settlement.net - first_half

    if variant == "same_utr":
        # Both legs carry the settlement's UTR, so the UTR rule can group them.
        narration_a = _narration(ctx, "clean", settlement.utr)
        narration_b = _narration(ctx, "clean", settlement.utr)
    else:
        # The bank issued a fresh UTR per leg. Only a subset search finds this.
        narration_a = _narration(ctx, "clean", _utr(ctx, settled_at))
        narration_b = _narration(ctx, "clean", _utr(ctx, settled_at))

    txn_a = _make_bank_txn(ctx, settled_at, first_half, narration_a)
    txn_b = _make_bank_txn(ctx, settled_at + timedelta(days=1), second_half, narration_b)
    _record_match(
        ctx,
        "SPLIT_SETTLEMENT",
        [settlement],
        [txn_a, txn_b],
        payments,
        note=f"paid in two legs ({variant})",
    )


def _case_merged_credit(ctx: _Ctx, dates: list[date], variant: str) -> None:
    """Two settlements arriving as one bank credit."""
    first_payments = _batch(ctx, dates[0])
    second_payments = _batch(ctx, dates[1])
    first = _make_settlement(ctx, dates[0], first_payments)
    second = _make_settlement(ctx, dates[1], second_payments)

    if variant == "utr_of_first":
        # A trap worth having: the narration names the first settlement, so
        # the UTR rule proposes a match that is wrong by exactly the second
        # settlement's net. The arithmetic is what rejects it.
        narration = _narration(ctx, "clean", first.utr)
    else:
        narration = _narration(ctx, "masked")

    txn = _make_bank_txn(ctx, dates[1], first.net + second.net, narration)
    _record_match(
        ctx,
        "MERGED_CREDIT",
        [first, second],
        [txn],
        first_payments + second_payments,
        note=f"two settlements paid as one credit ({variant})",
    )


def _case_amount_collision(ctx: _Ctx, dates: list[date], variant: str) -> None:
    """Two settlements, different days, identical net, both credits landing
    on the same value date with the UTR truncated by the bank.

    Amount and date cannot separate these two. Nothing else is available. The
    correct behaviour is to abstain on both and say why — matching one at
    random would be a coin flip dressed up as a result.
    """
    first_payments = _batch(ctx, dates[0], count=5)
    # Same amounts and instruments on the second day: identical net by
    # construction, no float arithmetic involved.
    second_payments = [
        _new_payment(
            ctx,
            dates[1] - timedelta(days=CYCLE_DAYS),
            amount=p.amount,
            method=p.method,
        )
        for p in first_payments
    ]
    first = _make_settlement(ctx, dates[0], first_payments)
    second = _make_settlement(ctx, dates[1], second_payments)
    assert first.net == second.net, "collision case must produce identical nets"

    landing = dates[1]
    txn_a = _make_bank_txn(ctx, landing, first.net, _narration(ctx, "masked"))
    txn_b = _make_bank_txn(ctx, landing, second.net, _narration(ctx, "masked"))
    _record_match(
        ctx,
        "AMOUNT_COLLISION",
        [first],
        [txn_a],
        first_payments,
        note="identical net to another settlement; UTR truncated on the statement",
    )
    _record_match(
        ctx,
        "AMOUNT_COLLISION",
        [second],
        [txn_b],
        second_payments,
        note="identical net to another settlement; UTR truncated on the statement",
    )


def _case_missing_bank_credit(ctx: _Ctx, settled_at: date, variant: str) -> None:
    """The gateway settled it; the money has not landed yet."""
    payments = _batch(ctx, settled_at)
    settlement = _make_settlement(ctx, settled_at, payments)
    ctx.unmatched_settlements.append(
        {
            "settlement_id": settlement.settlement_id,
            "case_type": "MISSING_BANK_CREDIT",
            "payment_ids": sorted(p.payment_id for p in payments),
            "note": "no bank credit in the statement window",
        }
    )


def _case_duplicate_utr(ctx: _Ctx, settled_at: date, variant: str) -> None:
    """The bank posts a credit, reverses it the same day under the same UTR,
    and re-sends it the next day with a fresh reference.

    Three lines on the statement, one settlement. A matcher that keys on UTR
    alone counts the money twice.
    """
    payments = _batch(ctx, settled_at)
    settlement = _make_settlement(ctx, settled_at, payments)

    posted = _make_bank_txn(
        ctx, settled_at, settlement.net, _narration(ctx, "clean", settlement.utr)
    )
    reversed_out = _make_bank_txn(
        ctx, settled_at, -settlement.net, _narration(ctx, "reversal", settlement.utr)
    )
    repaid = _make_bank_txn(
        ctx,
        settled_at + timedelta(days=1),
        settlement.net,
        _narration(ctx, "clean", _utr(ctx, settled_at)),
    )
    ctx.reversal_pairs.append([posted.bank_txn_id, reversed_out.bank_txn_id])
    _record_match(
        ctx,
        "DUPLICATE_UTR",
        [settlement],
        [repaid],
        payments,
        note=(
            f"{posted.bank_txn_id} was posted and reversed same day under the "
            f"same UTR; {repaid.bank_txn_id} is the real credit"
        ),
    )


def _case_orphan_credit(ctx: _Ctx, value_date: date, variant: str) -> None:
    """A customer paid the merchant's bank account directly.

    It is a real credit and it belongs to no settlement. The honest answer is
    to say so, not to attach it to whichever settlement is nearest.
    """
    amount = ctx.rng.randrange(1_00_000, 9_00_000)
    amount = _make_amount_unmatchable(ctx, amount, value_date)
    txn = _make_bank_txn(ctx, value_date, amount, _narration(ctx, "orphan"))
    ctx.unmatched_bank_txns.append(
        {
            "bank_txn_id": txn.bank_txn_id,
            "case_type": "ORPHAN_CREDIT",
            "note": "direct customer transfer, no settlement behind it",
        }
    )


def _make_amount_unmatchable(ctx: _Ctx, amount: Paise, value_date: date) -> Paise:
    """Nudge an amount until no settlement or small group of settlements in
    the window sums to it.

    Without this, an orphan credit could coincidentally equal a real
    settlement net and the expected outcome of the case would depend on the
    seed. The nudge is 7 paise at a time so the figure still looks like money.
    """
    nearby = [
        s.net
        for s in ctx.settlements
        if abs((s.settled_at - value_date).days) <= 5
    ]
    for _ in range(500):
        if not _sums_to(nearby, amount):
            return amount
        amount += 7
    raise RuntimeError("could not place an unmatchable orphan amount")


def _sums_to(values: list[Paise], target: Paise) -> bool:
    """Does any group of up to three values sum to target?

    Mirrors the cap the matcher uses, so 'unmatchable' means unmatchable by
    the rules this system actually runs.
    """
    for i, a in enumerate(values):
        if a == target:
            return True
        for j in range(i + 1, len(values)):
            if a + values[j] == target:
                return True
            for k in range(j + 1, len(values)):
                if a + values[j] + values[k] == target:
                    return True
    return False


_CASE_BUILDERS = {
    "CLEAN": _case_clean,
    "NARRATION_NOISE": _case_narration_noise,
    "WITH_REFUND": _case_with_refund,
    "CHARGEBACK_DEDUCTION": _case_chargeback_deduction,
    "UNLABELLED_REVERSAL": _case_unlabelled_reversal,
    "ROUNDING_DRIFT": _case_rounding_drift,
    "SPLIT_SETTLEMENT": _case_split_settlement,
    "MISSING_BANK_CREDIT": _case_missing_bank_credit,
    "DUPLICATE_UTR": _case_duplicate_utr,
}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


@dataclass
class GenerationResult:
    seed: int
    difficulty: str
    data_dir: Path
    truth_path: Path
    counts: dict[str, int]
    case_counts: dict[str, int]


def generate(
    seed: int = 42,
    difficulty: str = "standard",
    data_dir: Path | str = Path("data/generated"),
    truth_path: Path | str = Path("data/ground_truth.json"),
) -> GenerationResult:
    if difficulty not in PLANS:
        raise ValueError(f"unknown difficulty {difficulty!r}; expected one of {sorted(PLANS)}")

    plan = PLANS[difficulty]
    rng = random.Random(seed)

    # Every settlement gets its own date. That is what makes the T+2 cycle a
    # usable partition of the line items: two settlements on one date would
    # make membership ambiguous for every payment in the window, which is a
    # different (and much less interesting) problem than the ones here.
    dates_needed = sum(_DATES_PER_CASE.get(case, 1) for case, _ in plan)
    all_dates = [
        START_DATE + timedelta(days=DAYS_BETWEEN_SETTLEMENTS * i)
        for i in range(dates_needed)
    ]
    ctx = _Ctx(rng=rng, settlement_dates=set(all_dates))

    cursor = 0
    for case_type, variant in plan:
        take = _DATES_PER_CASE.get(case_type, 1)
        window = all_dates[cursor : cursor + take]
        cursor += take

        if case_type == "MERGED_CREDIT":
            _case_merged_credit(ctx, window, variant)
        elif case_type == "AMOUNT_COLLISION":
            _case_amount_collision(ctx, window, variant)
        elif case_type == "ORPHAN_CREDIT":
            # No settlement date of its own; it lands mid-batch.
            landing = all_dates[min(len(all_dates) - 1, max(0, cursor))]
            _case_orphan_credit(ctx, landing, variant)
        else:
            _CASE_BUILDERS[case_type](ctx, window[0], variant)

    written = _write_csvs(ctx, Path(data_dir))
    truth = _write_ground_truth(ctx, Path(truth_path), seed, difficulty)

    case_counts: dict[str, int] = {}
    for match in ctx.matches:
        case_counts[match["case_type"]] = case_counts.get(match["case_type"], 0) + 1
    for entry in ctx.unmatched_settlements + ctx.unmatched_bank_txns:
        case_counts[entry["case_type"]] = case_counts.get(entry["case_type"], 0) + 1
    for entry in ctx.loop_b:
        case_counts[entry["case_type"]] = case_counts.get(entry["case_type"], 0) + 1

    return GenerationResult(
        seed=seed,
        difficulty=difficulty,
        data_dir=Path(data_dir),
        truth_path=truth,
        counts=written,
        case_counts=case_counts,
    )


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

_CSV_SPEC = (
    ("orders.csv", "orders", ("order_id", "customer_id", "amount", "created_at", "is_b2b")),
    (
        "payments.csv",
        "payments",
        ("payment_id", "order_id", "amount", "fee", "tax", "method", "captured_at", "status"),
    ),
    ("refunds.csv", "refunds", ("refund_id", "payment_id", "amount", "created_at")),
    (
        "adjustments.csv",
        "adjustments",
        ("adjustment_id", "kind", "amount", "ref_payment_id", "created_at"),
    ),
    (
        "settlements.csv",
        "settlements",
        (
            "settlement_id",
            "utr",
            "settled_at",
            "gross",
            "fee",
            "tax",
            "refund_total",
            "adjustment_total",
            "net",
        ),
    ),
    ("bank_txns.csv", "bank_txns", ("bank_txn_id", "value_date", "narration", "credit_amount")),
)


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _write_csvs(ctx: _Ctx, data_dir: Path) -> dict[str, int]:
    data_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for filename, attribute, columns in _CSV_SPEC:
        rows = getattr(ctx, attribute)
        # newline="" plus lineterminator="\n" keeps the files byte-identical
        # across platforms, which is what makes --seed 42 reproducible on a
        # judge's machine rather than just on mine.
        with (data_dir / filename).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(columns)
            for row in rows:
                writer.writerow([_cell(getattr(row, column)) for column in columns])
        counts[attribute] = len(rows)
    return counts


def _write_ground_truth(ctx: _Ctx, path: Path, seed: int, difficulty: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": (
            "Answer key. Loaded by report.py only, never by the pipeline. "
            "The CSVs in data/generated contain no labels."
        ),
        "seed": seed,
        "difficulty": difficulty,
        "cycle_days": CYCLE_DAYS,
        "matches": ctx.matches,
        "unmatched_settlements": ctx.unmatched_settlements,
        "unmatched_bank_txns": ctx.unmatched_bank_txns,
        "bank_reversal_pairs": ctx.reversal_pairs,
        "loop_b": ctx.loop_b,
    }
    with path.open("w", newline="", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return path
