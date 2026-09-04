"""CSV in, typed records out.

This is the boundary where text becomes money, so it is the boundary where the
integer rule is enforced. `parse_paise` rejects anything with a decimal point:
a column of rupee figures where paise are expected is a data problem that has
to fail loudly here, not become a 100x error in a settlement three stages
later.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

from .models import (
    Adjustment,
    BankTxn,
    Order,
    Payment,
    RecordStore,
    Refund,
    Settlement,
)
from .money import parse_paise


class IngestError(ValueError):
    """A source file is missing, malformed, or has the wrong columns."""


def _bool(value: str, field: str) -> bool:
    text = value.strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no", ""}:
        return False
    raise IngestError(f"{field}: expected a boolean, got {value!r}")


def _dt(value: str, field: str) -> datetime:
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise IngestError(f"{field}: bad timestamp {value!r}") from exc


def _date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise IngestError(f"{field}: bad date {value!r}") from exc


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise IngestError(
            f"{path} not found. Run `python -m reconproof.run --seed 42` to "
            "generate the batch first."
        )
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load(data_dir: Path | str = Path("data/generated")) -> RecordStore:
    """Read the six source files into a RecordStore indexed by ID."""
    directory = Path(data_dir)
    store = RecordStore()

    for row in _rows(directory / "orders.csv"):
        order = Order(
            order_id=row["order_id"],
            customer_id=row["customer_id"],
            amount=parse_paise(row["amount"], "order.amount"),
            created_at=_dt(row["created_at"], "order.created_at"),
            is_b2b=_bool(row["is_b2b"], "order.is_b2b"),
        )
        store.orders[order.order_id] = order

    for row in _rows(directory / "payments.csv"):
        payment = Payment(
            payment_id=row["payment_id"],
            order_id=row["order_id"],
            amount=parse_paise(row["amount"], "payment.amount"),
            fee=parse_paise(row["fee"], "payment.fee"),
            tax=parse_paise(row["tax"], "payment.tax"),
            method=row["method"].strip(),
            captured_at=_dt(row["captured_at"], "payment.captured_at"),
            status=row["status"].strip(),
        )
        store.payments[payment.payment_id] = payment

    for row in _rows(directory / "refunds.csv"):
        refund = Refund(
            refund_id=row["refund_id"],
            payment_id=row["payment_id"],
            amount=parse_paise(row["amount"], "refund.amount"),
            created_at=_dt(row["created_at"], "refund.created_at"),
        )
        store.refunds[refund.refund_id] = refund

    for row in _rows(directory / "adjustments.csv"):
        reference = row["ref_payment_id"].strip()
        adjustment = Adjustment(
            adjustment_id=row["adjustment_id"],
            kind=row["kind"].strip(),
            amount=parse_paise(row["amount"], "adjustment.amount"),
            # An empty cell is a real value here, not a missing one: it means
            # the gateway sent money with no reference to what it was for.
            ref_payment_id=reference or None,
            created_at=_dt(row["created_at"], "adjustment.created_at"),
        )
        store.adjustments[adjustment.adjustment_id] = adjustment

    for row in _rows(directory / "settlements.csv"):
        settlement = Settlement(
            settlement_id=row["settlement_id"],
            utr=row["utr"].strip(),
            settled_at=_date(row["settled_at"], "settlement.settled_at"),
            gross=parse_paise(row["gross"], "settlement.gross"),
            fee=parse_paise(row["fee"], "settlement.fee"),
            tax=parse_paise(row["tax"], "settlement.tax"),
            refund_total=parse_paise(row["refund_total"], "settlement.refund_total"),
            adjustment_total=parse_paise(
                row["adjustment_total"], "settlement.adjustment_total"
            ),
            net=parse_paise(row["net"], "settlement.net"),
        )
        store.settlements[settlement.settlement_id] = settlement

    for row in _rows(directory / "bank_txns.csv"):
        txn = BankTxn(
            bank_txn_id=row["bank_txn_id"],
            value_date=_date(row["value_date"], "bank_txn.value_date"),
            narration=row["narration"],
            credit_amount=parse_paise(row["credit_amount"], "bank_txn.credit_amount"),
        )
        store.bank_txns[txn.bank_txn_id] = txn

    if not store.settlements or not store.bank_txns:
        raise IngestError(f"no settlements or bank transactions found in {directory}")
    return store
