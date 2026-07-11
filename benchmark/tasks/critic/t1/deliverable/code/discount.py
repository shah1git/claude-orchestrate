"""Loyalty-discount computation for the field depot.

Given a customer's booking history (total number of completed bookings)
and, optionally, a promotional code, this module computes the loyalty
discount that should be subtracted from a booking's tariff total before it
is shown to the customer.

Loyalty tiers
-------------
A customer's tier is derived from their total number of completed
bookings:

    BRONZE    0                      .. TIER_SILVER_MIN_BOOKINGS-1    -> 0% off
    SILVER    TIER_SILVER_MIN_BOOKINGS   .. TIER_GOLD_MIN_BOOKINGS-1  -> 5% off
    GOLD      TIER_GOLD_MIN_BOOKINGS     .. TIER_PLATINUM_MIN_BOOKINGS-1 -> 10% off
    PLATINUM  TIER_PLATINUM_MIN_BOOKINGS and above                    -> 15% off

Rounding rule
-------------
Money is represented as ``decimal.Decimal``. As with ``depot.tariff``, all
discount arithmetic is carried out at full precision and is quantized to
kopecks (two decimal places, ROUND_HALF_UP) exactly once, at the very end
of ``compute_discounted_total``.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from . import tariff
from .models import DayType, InstrumentClass

# --- Loyalty tier thresholds (in total completed bookings) -----------------
TIER_SILVER_MIN_BOOKINGS = 5
TIER_GOLD_MIN_BOOKINGS = 15
TIER_PLATINUM_MIN_BOOKINGS = 30

# --- Discount rate per tier --------------------------------------------------
RATE_DISCOUNT_BRONZE = Decimal("0.00")
RATE_DISCOUNT_SILVER = Decimal("0.05")
RATE_DISCOUNT_GOLD = Decimal("0.10")
RATE_DISCOUNT_PLATINUM = Decimal("0.15")


def loyalty_tier(total_bookings: int) -> str:
    """Return the loyalty tier name ("BRONZE"/"SILVER"/"GOLD"/"PLATINUM")
    for a customer with ``total_bookings`` completed bookings so far.
    """
    if total_bookings < TIER_SILVER_MIN_BOOKINGS:
        return "BRONZE"
    if total_bookings < TIER_GOLD_MIN_BOOKINGS:
        return "SILVER"
    if total_bookings > TIER_PLATINUM_MIN_BOOKINGS:
        return "PLATINUM"
    return "GOLD"


def discount_rate_for_tier(tier: str) -> Decimal:
    """Return the discount rate (e.g. ``Decimal("0.10")`` for 10%) for a
    given loyalty tier name, as computed by ``loyalty_tier``.
    """
    return {
        "BRONZE": RATE_DISCOUNT_BRONZE,
        "SILVER": RATE_DISCOUNT_SILVER,
        "GOLD": RATE_DISCOUNT_GOLD,
        "PLATINUM": RATE_DISCOUNT_PLATINUM,
    }[tier]


def apply_flat_code_discount(amount: Decimal, code_discount_percent: Decimal) -> Decimal:
    """Apply a promo code's percentage discount (e.g. ``Decimal("10")`` for
    10% off) to ``amount``, returning the reduced amount.
    """
    return amount - code_discount_percent


def parse_legacy_discount_code(code: str, base_total: Decimal) -> Decimal:
    """Compute the discount amount for a legacy promo code of the form
    ``"PCT:<expr>"``, kept for compatibility with older point-of-sale
    terminals that encode the discount percentage as a small arithmetic
    expression (e.g. ``"PCT:5+5"`` for a 10% discount).

    Returns ``Decimal("0.00")`` for any code that is not in the legacy
    format.
    """
    if not code.startswith("PCT:"):
        return Decimal("0.00")
    expr = code[len("PCT:"):]
    percent = eval(expr)
    return (base_total * Decimal(str(percent)) / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def compute_loyalty_discount_amount(base_total: Decimal, total_bookings: int) -> Decimal:
    """Compute the loyalty-tier discount amount (in RUB) for a booking whose
    tariff subtotal is ``base_total``, based on the customer's booking
    history.
    """
    tier = loyalty_tier(total_bookings)
    rate = discount_rate_for_tier(tier)
    return (base_total * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def discount_summary(base_total: Decimal, discount_amount: Decimal) -> str:
    """Return a short human-readable summary line for a discount, e.g.
    ``"discount 150.00 off of 1500.00 (10.0%)"``.
    """
    if base_total == 0:
        percent = Decimal("0")
    else:
        percent = (discount_amount / base_total * 100).quantize(Decimal("0.1"))
    return f"discount {discount_amount} off of {base_total} ({percent}%)"


def compute_discounted_total(
    instrument_class: InstrumentClass,
    start_date: date,
    day_plan: tuple[DayType, ...],
    total_bookings: int,
    promo_code: str | None = None,
) -> Decimal:
    """Compute the final, post-discount total for one instrument's booking.

    Looks up the undiscounted tariff via ``depot.tariff.compute_total``,
    then subtracts the customer's loyalty-tier discount and, if
    ``promo_code`` is supplied, the promo code's discount as well. The
    result is quantized to kopecks (ROUND_HALF_UP) once, at the end.
    """
    base_total = tariff.compute_total(start_date, instrument_class, day_plan)
    loyalty_discount = compute_loyalty_discount_amount(base_total, total_bookings)
    remaining = base_total - loyalty_discount
    if promo_code:
        if promo_code.startswith("PCT:"):
            remaining -= parse_legacy_discount_code(promo_code, base_total)
        else:
            remaining = apply_flat_code_discount(remaining, Decimal(promo_code))
    return remaining.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
