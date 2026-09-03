"""Money for ReconProof.

Every amount in this project is an integer number of paise. There is no float
anywhere in a money path, and that is a correctness requirement, not a style
preference: 0.1 + 0.2 != 0.3 in binary floating point, and a reconciliation
engine that claims to balance "to the paisa" cannot be built on a type that
cannot represent a paisa exactly.

This module owns the money type, the only rounding decision in the system, and
the display format. Nothing else is allowed to round money.
"""

from __future__ import annotations

# A Paise is a plain int. The alias exists so the intent is visible in every
# signature; it carries no runtime behaviour of its own.
Paise = int

# GST on the gateway fee, in basis points. 1800 bp = 18%.
GST_RATE_BP = 1800

# The rupee sign. Source files are UTF-8; stdout on Windows is not always, so
# run.py reconfigures the console before anything is printed (see WHAT_BROKE.md).
RUPEE = "₹"


class MoneyTypeError(TypeError):
    """Raised when a non-integer sneaks into a money path."""


def assert_paise(value: object, field: str = "amount") -> Paise:
    """Return `value` as paise, or raise if it is not a plain int.

    `bool` is a subclass of `int` in Python, so it is rejected explicitly -
    otherwise `True` would silently become 1 paisa. Call this at every ingest
    boundary: CSV parsing, LLM output parsing, HTTP request bodies.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise MoneyTypeError(
            f"{field} must be integer paise, got {type(value).__name__}: {value!r}"
        )
    return value


def parse_paise(value: object, field: str = "amount") -> Paise:
    """Parse a value that arrived as text (CSV, JSON, form field) into paise.

    Accepts an int, or a string of digits with an optional sign. A string with
    a decimal point is rejected on purpose: if a caller has a rupee figure it
    must convert deliberately, because that conversion is where precision is
    lost.
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise MoneyTypeError(f"{field} is empty")
        if "." in text or "e" in text.lower():
            raise MoneyTypeError(
                f"{field} looks like a rupee/float figure ({text!r}); "
                "amounts must be integer paise"
            )
        try:
            return int(text)
        except ValueError as exc:
            raise MoneyTypeError(f"{field} is not an integer: {text!r}") from exc
    return assert_paise(value, field)


def gst_on_fee(fee_paise: Paise, rate_bp: int = GST_RATE_BP) -> Paise:
    """GST charged on the gateway fee, in paise.

    Rounding mode is half-up, made explicit by the +5000 before the floor
    division: (fee * 1800 + 5000) // 10000. This is a *decision*, not a
    default - a gateway that rounds half-even or truncates will produce a
    figure 1-2 paise away from this one on a batch of a hundred payments, and
    that difference is one of the exception classes this system detects
    (ROUNDING_DRIFT). Making the choice visible here is what lets us classify
    the residual instead of shrugging at it.

    Negative fees (a fee correction) round half-up in magnitude, so the
    identity gst_on_fee(-x) == -gst_on_fee(x) holds.
    """
    assert_paise(fee_paise, "fee_paise")
    assert_paise(rate_bp, "rate_bp")
    if fee_paise < 0:
        return -((-fee_paise * rate_bp + 5000) // 10000)
    return (fee_paise * rate_bp + 5000) // 10000


def pct(amount: Paise, rate_bp: int) -> Paise:
    """A percentage of an amount, in basis points, rounded half-up.

    Used for gateway fees (MDR) and for TDS. Same rounding rule as GST.
    """
    assert_paise(amount, "amount")
    assert_paise(rate_bp, "rate_bp")
    if amount < 0:
        return -((-amount * rate_bp + 5000) // 10000)
    return (amount * rate_bp + 5000) // 10000


def group_indian(digits: str) -> str:
    """Group a digit string the Indian way: last three, then pairs.

    414382 -> 4,14,382 (not 414,382). This is an India-first product; a
    Western grouping in a settlement report is the kind of detail a finance
    team notices immediately.
    """
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups + [tail])


def rupees(value: Paise) -> str:
    """Format paise for display: 41438200 -> Rs 4,14,382.00.

    Negatives use accounting parentheses - (Rs 9,000.00) - because that is how
    a ledger shows a deduction, and this system's whole surface is a ledger.
    """
    assert_paise(value, "value")
    negative = value < 0
    magnitude = -value if negative else value
    whole, fraction = divmod(magnitude, 100)
    text = f"{RUPEE}{group_indian(str(whole))}.{fraction:02d}"
    return f"({text})" if negative else text


def rupees_plain(value: Paise) -> str:
    """Same as `rupees` but with a minus sign instead of parentheses.

    For log lines and JSON-adjacent text where parentheses would be confusing.
    """
    assert_paise(value, "value")
    if value < 0:
        return "-" + rupees(-value)
    return rupees(value)
