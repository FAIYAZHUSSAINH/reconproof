"""Tests for the money type.

These are short because the module is short, but they are load-bearing: a
rounding bug here shows up as an unexplained 1-2 paise residual on every
settlement in the batch, and it looks like a matching bug for hours before you
find it.
"""

import pytest

from reconproof.money import (
    MoneyTypeError,
    assert_paise,
    group_indian,
    gst_on_fee,
    parse_paise,
    pct,
    rupees,
    rupees_plain,
)


class TestIndianFormatting:
    def test_lakh_grouping(self):
        # 4,14,382.00 - not 414,382.00. This is the whole point.
        assert rupees(41_438_200) == "₹4,14,382.00"

    def test_crore_grouping(self):
        assert rupees(1_23_45_678_00) == "₹1,23,45,678.00"

    def test_small_amounts_have_no_separator(self):
        assert rupees(0) == "₹0.00"
        assert rupees(1) == "₹0.01"
        assert rupees(99_900) == "₹999.00"

    def test_thousand_boundary(self):
        assert rupees(1_000_00) == "₹1,000.00"
        assert rupees(10_000_00) == "₹10,000.00"
        assert rupees(1_00_000_00) == "₹1,00,000.00"

    def test_paise_are_never_dropped(self):
        assert rupees(41_438_212) == "₹4,14,382.12"
        assert rupees(41_438_201) == "₹4,14,382.01"

    def test_negatives_use_accounting_parentheses(self):
        assert rupees(-9_000_00) == "(₹9,000.00)"
        assert rupees_plain(-9_000_00) == "-₹9,000.00"

    def test_group_indian_directly(self):
        assert group_indian("1") == "1"
        assert group_indian("123") == "123"
        assert group_indian("1234") == "1,234"
        assert group_indian("414382") == "4,14,382"
        assert group_indian("12345678") == "1,23,45,678"


class TestGstRounding:
    def test_documented_rate(self):
        # 18% of Rs 100.00 fee = Rs 18.00
        assert gst_on_fee(100_00) == 18_00

    def test_half_rounds_up_not_down(self):
        # 18% of 25 paise = 4.5 paise. Half-up gives 5, truncation gives 4.
        # That single paisa, times 140 payments, is a residual you will chase.
        assert gst_on_fee(25) == 5

    def test_below_half_rounds_down(self):
        # 18% of 24 paise = 4.32 paise -> 4
        assert gst_on_fee(24) == 4

    def test_just_above_half_rounds_up(self):
        # 18% of 26 paise = 4.68 -> 5
        assert gst_on_fee(26) == 5

    def test_tiny_fee(self):
        assert gst_on_fee(0) == 0
        assert gst_on_fee(1) == 0  # 0.18 paise
        assert gst_on_fee(3) == 1  # 0.54 paise

    def test_negative_fee_is_symmetric(self):
        # A fee correction must not drift when it is reversed.
        assert gst_on_fee(-25) == -5
        assert gst_on_fee(-100_00) == -18_00

    def test_alternate_rate(self):
        assert gst_on_fee(100_00, rate_bp=500) == 5_00

    def test_line_level_and_aggregate_rounding_can_disagree(self):
        # This is not a bug, it is ROUNDING_DRIFT: the same fees taxed
        # per line and taxed in aggregate differ by a paisa. The generator
        # relies on it and the verifier classifies it.
        fees = [25, 25, 25, 25]
        per_line = sum(gst_on_fee(f) for f in fees)  # 4 x 5 = 20
        aggregate = gst_on_fee(sum(fees))  # 18% of 100 = 18
        assert per_line - aggregate == 2


class TestPct:
    def test_mdr(self):
        # 2% MDR on Rs 1,000.00
        assert pct(1_000_00, 200) == 20_00

    def test_half_up(self):
        assert pct(25, 1800) == 5

    def test_tds_ten_percent(self):
        assert pct(50_000_00, 1000) == 5_000_00


class TestFloatGuard:
    @pytest.mark.parametrize(
        "bad", [1.0, 0.1, 4143.82, float("nan"), float("inf")]
    )
    def test_floats_are_rejected(self, bad):
        with pytest.raises(MoneyTypeError):
            assert_paise(bad, "amount")

    def test_bools_are_rejected(self):
        # bool is a subclass of int; True would silently mean 1 paisa.
        with pytest.raises(MoneyTypeError):
            assert_paise(True, "amount")

    def test_strings_are_rejected_by_the_strict_guard(self):
        with pytest.raises(MoneyTypeError):
            assert_paise("100", "amount")

    def test_none_is_rejected(self):
        with pytest.raises(MoneyTypeError):
            assert_paise(None, "amount")

    def test_ints_pass_through(self):
        assert assert_paise(-42) == -42
        assert assert_paise(0) == 0

    def test_money_functions_refuse_floats(self):
        with pytest.raises(MoneyTypeError):
            gst_on_fee(100.0)
        with pytest.raises(MoneyTypeError):
            rupees(1.5)


class TestParsePaise:
    def test_parses_csv_integers(self):
        assert parse_paise("41438200") == 41_438_200
        assert parse_paise("-500") == -500
        assert parse_paise(" 250 ") == 250

    def test_rejects_a_rupee_figure(self):
        # "4143.82" is the classic CSV mistake: rupees where paise are expected.
        with pytest.raises(MoneyTypeError):
            parse_paise("4143.82")

    def test_rejects_scientific_notation(self):
        with pytest.raises(MoneyTypeError):
            parse_paise("1e5")

    def test_rejects_empty(self):
        with pytest.raises(MoneyTypeError):
            parse_paise("")
