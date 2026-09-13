"""Monetary amounts.

One rule, enforced by the type: money is an integer count of minor units
plus a currency. There is no constructor that accepts a float, no operator
that returns a float, and no arithmetic between different currencies.

`Decimal` appears only at the edges - parsing a human figure and rendering
one - and never inside a calculation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Self

from ledgerloop.core.enums import Currency

__all__ = ["Money"]


@dataclass(frozen=True, slots=True)
class Money:
    """An exact monetary amount in a single currency.

    Amounts may be negative: a refund leg, a reversal, or a reconciliation
    difference is legitimately negative, and forbidding it would push callers
    into carrying signs alongside the value.

    Example:
        >>> Money.from_major("1250.50", Currency.INR)
        Money(minor_units=125050, currency=INR)
        >>> Money(10_000, Currency.INR) + Money(500, Currency.INR)
        Money(minor_units=10500, currency=INR)
    """

    minor_units: int
    currency: Currency

    def __post_init__(self) -> None:
        if not isinstance(self.minor_units, int) or isinstance(self.minor_units, bool):
            raise TypeError(
                f"Money.minor_units must be an int, got {type(self.minor_units).__name__}. "
                "Use Money.from_major() to build an amount from a decimal figure."
            )
        if not isinstance(self.currency, Currency):
            raise TypeError(
                f"Money.currency must be a Currency, got {type(self.currency).__name__}"
            )

    # ---- construction -----------------------------------------------------

    @classmethod
    def zero(cls, currency: Currency) -> Self:
        """The zero amount in `currency`."""
        return cls(0, currency)

    @classmethod
    def from_major(cls, amount: str | Decimal | int, currency: Currency) -> Self:
        """Build an amount from a major-unit figure, e.g. "1250.50".

        Prefer passing a `str`. A `float` is rejected outright - by the time
        a decimal figure has been through binary floating point, the value
        the caller meant is already gone.

        Args:
            amount: The major-unit figure. Strings are parsed exactly.
            currency: The currency, which fixes the number of decimal places.

        Returns:
            The amount, rounded half-to-even to the currency's exponent.

        Raises:
            TypeError: `amount` was a float.
            ValueError: `amount` could not be parsed, or carried more
                precision than the currency permits.
        """
        if isinstance(amount, float):
            raise TypeError(
                "Money.from_major does not accept float - pass a str or Decimal. "
                "Floats cannot represent decimal amounts exactly."
            )
        try:
            value = Decimal(amount)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"Could not parse {amount!r} as a decimal amount") from exc

        if not value.is_finite():
            raise ValueError(f"Money amount must be finite, got {amount!r}")

        scaled = value.scaleb(currency.exponent)
        rounded = scaled.quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
        if rounded != scaled:
            raise ValueError(
                f"{amount} carries more precision than {currency.value} supports "
                f"({currency.exponent} decimal places). Round it explicitly first."
            )
        return cls(int(rounded), currency)

    # ---- arithmetic -------------------------------------------------------

    def __add__(self, other: Money) -> Money:
        self._assert_same_currency(other)
        return Money(self.minor_units + other.minor_units, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._assert_same_currency(other)
        return Money(self.minor_units - other.minor_units, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.minor_units, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.minor_units), self.currency)

    def times(self, factor: int) -> Money:
        """Multiply by a whole number - e.g. a quantity or an instalment count.

        There is deliberately no division and no fractional multiplication:
        splitting an amount without losing or inventing minor units is
        `allocate`'s job.
        """
        if not isinstance(factor, int) or isinstance(factor, bool):
            raise TypeError(f"Money.times requires an int factor, got {type(factor).__name__}")
        return Money(self.minor_units * factor, self.currency)

    def allocate(self, weights: tuple[int, ...]) -> tuple[Money, ...]:
        """Split this amount across `weights`, conserving every minor unit.

        Remainder minor units are distributed one each, largest weight first,
        so the parts always sum back to the original exactly. This is the
        only correct way to split money.

        Args:
            weights: Positive relative weights, one per resulting part.

        Returns:
            One `Money` per weight, summing to this amount.

        Raises:
            ValueError: `weights` was empty, contained a negative, or summed
                to zero.
        """
        if not weights:
            raise ValueError("allocate requires at least one weight")
        if any(w < 0 for w in weights):
            raise ValueError("allocate weights must be non-negative")
        total_weight = sum(weights)
        if total_weight == 0:
            raise ValueError("allocate weights must not sum to zero")

        if self.minor_units < 0:
            # Split the magnitude and put the sign back. Floor division rounds
            # a negative share away from zero, so a reversal split the direct
            # way does not come out as the negation of the charge it reverses,
            # and each party is left a unit up or down after a full refund.
            return tuple(-part for part in (-self).allocate(weights))

        parts = [self.minor_units * w // total_weight for w in weights]
        remainder = self.minor_units - sum(parts)

        # Hand out the leftover units to the heaviest weights first.
        order = sorted(range(len(weights)), key=lambda i: weights[i], reverse=True)
        for i in range(remainder):
            parts[order[i % len(order)]] += 1

        return tuple(Money(p, self.currency) for p in parts)

    # ---- comparison -------------------------------------------------------

    def __lt__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.minor_units < other.minor_units

    def __le__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.minor_units <= other.minor_units

    def __gt__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.minor_units > other.minor_units

    def __ge__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.minor_units >= other.minor_units

    # ---- predicates -------------------------------------------------------

    @property
    def is_zero(self) -> bool:
        return self.minor_units == 0

    @property
    def is_positive(self) -> bool:
        return self.minor_units > 0

    @property
    def is_negative(self) -> bool:
        return self.minor_units < 0

    # ---- rendering --------------------------------------------------------

    def to_major(self) -> Decimal:
        """Exact major-unit value, for display and for external APIs."""
        return Decimal(self.minor_units).scaleb(-self.currency.exponent)

    def format(self) -> str:
        """Render as `"1250.50 INR"`, with the currency's exact precision."""
        return f"{self.to_major():.{self.currency.exponent}f} {self.currency.value}"

    def __str__(self) -> str:
        return self.format()

    # ---- internals --------------------------------------------------------

    def _assert_same_currency(self, other: Money) -> None:
        if not isinstance(other, Money):
            raise TypeError(f"Expected Money, got {type(other).__name__}")
        if self.currency is not other.currency:
            raise ValueError(
                f"Cannot combine {self.currency.value} and {other.currency.value}. "
                "Convert explicitly at a recorded rate first."
            )
