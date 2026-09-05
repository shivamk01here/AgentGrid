"""Tests for monetary arithmetic.

These are the highest-stakes tests in the repo. Every bug here is a bug that
moves the wrong amount of real money.
"""

from decimal import Decimal

import pytest

from ledgerloop.core.enums import Currency
from ledgerloop.core.money import Money


class TestConstruction:
    def test_from_major_scales_by_the_currency_exponent(self):
        assert Money.from_major("1250.50", Currency.INR).minor_units == 125050

    def test_from_major_rejects_float(self):
        # 0.1 + 0.2 != 0.3 is exactly why.
        with pytest.raises(TypeError, match="does not accept float"):
            Money.from_major(1250.50, Currency.INR)

    def test_from_major_accepts_decimal_and_int(self):
        assert Money.from_major(Decimal("10.05"), Currency.USD).minor_units == 1005
        assert Money.from_major(7, Currency.USD).minor_units == 700

    def test_zero_decimal_currency_does_not_scale(self):
        assert Money.from_major("500", Currency.JPY).minor_units == 500

    def test_three_decimal_currency_scales_by_a_thousand(self):
        assert Money.from_major("1.234", Currency.KWD).minor_units == 1234

    def test_excess_precision_is_rejected_not_silently_rounded(self):
        with pytest.raises(ValueError, match="more precision"):
            Money.from_major("10.567", Currency.INR)

    def test_unparseable_amount_raises(self):
        with pytest.raises(ValueError, match="Could not parse"):
            Money.from_major("not-a-number", Currency.INR)

    def test_non_finite_amount_is_rejected(self):
        with pytest.raises(ValueError, match="finite"):
            Money.from_major(Decimal("Infinity"), Currency.INR)

    def test_minor_units_must_be_int(self):
        with pytest.raises(TypeError, match="must be an int"):
            Money(10.5, Currency.INR)

    def test_bool_is_not_an_acceptable_int(self):
        with pytest.raises(TypeError):
            Money(True, Currency.INR)

    def test_zero_helper(self):
        assert Money.zero(Currency.EUR) == Money(0, Currency.EUR)


class TestArithmetic:
    def test_addition_and_subtraction(self):
        a = Money(10_000, Currency.INR)
        b = Money(500, Currency.INR)
        assert (a + b).minor_units == 10_500
        assert (a - b).minor_units == 9_500

    def test_negative_amounts_are_legal(self):
        # A reversal leg is legitimately negative.
        assert (Money(100, Currency.INR) - Money(300, Currency.INR)).minor_units == -200

    def test_cross_currency_arithmetic_is_refused(self):
        with pytest.raises(ValueError, match="Cannot combine"):
            Money(100, Currency.INR) + Money(100, Currency.USD)

    def test_cross_currency_comparison_is_refused(self):
        with pytest.raises(ValueError, match="Cannot combine"):
            _ = Money(100, Currency.INR) < Money(100, Currency.USD)

    def test_negate_and_abs(self):
        assert (-Money(250, Currency.INR)).minor_units == -250
        assert abs(Money(-250, Currency.INR)).minor_units == 250

    def test_times_requires_an_int(self):
        assert Money(100, Currency.INR).times(3).minor_units == 300
        with pytest.raises(TypeError, match="int factor"):
            Money(100, Currency.INR).times(1.5)


class TestAllocate:
    def test_even_split_conserves_every_minor_unit(self):
        parts = Money(100, Currency.INR).allocate((1, 1, 1))
        assert [p.minor_units for p in parts] == [34, 33, 33]
        assert sum(p.minor_units for p in parts) == 100

    def test_weighted_split_conserves_the_total(self):
        parts = Money(1000, Currency.INR).allocate((3, 1))
        assert sum(p.minor_units for p in parts) == 1000

    def test_negative_total_still_conserves(self):
        parts = Money(-100, Currency.INR).allocate((1, 1, 1))
        assert sum(p.minor_units for p in parts) == -100

    def test_remainder_goes_to_the_heaviest_weight_first(self):
        parts = Money(10, Currency.INR).allocate((1, 8))
        assert sum(p.minor_units for p in parts) == 10
        assert parts[1].minor_units > parts[0].minor_units

    def test_empty_weights_rejected(self):
        with pytest.raises(ValueError, match="at least one weight"):
            Money(100, Currency.INR).allocate(())

    def test_negative_weight_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            Money(100, Currency.INR).allocate((1, -1))

    def test_zero_sum_weights_rejected(self):
        with pytest.raises(ValueError, match="sum to zero"):
            Money(100, Currency.INR).allocate((0, 0))


class TestPredicatesAndRendering:
    def test_predicates(self):
        assert Money.zero(Currency.INR).is_zero
        assert Money(1, Currency.INR).is_positive
        assert Money(-1, Currency.INR).is_negative

    def test_to_major_is_exact(self):
        assert Money(125050, Currency.INR).to_major() == Decimal("1250.50")

    def test_format_uses_the_currency_precision(self):
        assert Money(125050, Currency.INR).format() == "1250.50 INR"
        assert Money(500, Currency.JPY).format() == "500 JPY"
        assert Money(1234, Currency.KWD).format() == "1.234 KWD"

    def test_round_trip_through_major_preserves_value(self):
        for raw, currency in (
            ("1250.50", Currency.INR),
            ("500", Currency.JPY),
            ("1.234", Currency.KWD),
            ("0.01", Currency.USD),
        ):
            amount = Money.from_major(raw, currency)
            assert Money.from_major(amount.to_major(), currency) == amount


class TestCurrencyMetadata:
    def test_default_exponent_is_two(self):
        assert Currency.INR.exponent == 2
        assert Currency.INR.minor_units_per_major == 100

    def test_zero_decimal_currencies(self):
        assert Currency.JPY.exponent == 0
        assert Currency.JPY.minor_units_per_major == 1

    def test_three_decimal_currencies(self):
        assert Currency.KWD.exponent == 3
        assert Currency.KWD.minor_units_per_major == 1000
