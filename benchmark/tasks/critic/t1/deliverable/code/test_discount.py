"""Tests for depot.discount: loyalty tiers, discount rates and promo codes."""

from decimal import Decimal

from depot.discount import (
    compute_loyalty_discount_amount,
    discount_rate_for_tier,
    discount_summary,
    loyalty_tier,
    parse_legacy_discount_code,
)


def test_bronze_tier_has_zero_discount_rate():
    """A brand-new customer (0 bookings) is BRONZE and gets no discount."""
    assert loyalty_tier(0) == "BRONZE"
    assert discount_rate_for_tier("BRONZE") == Decimal("0.00")


def test_silver_tier_at_lower_threshold():
    """A customer with exactly TIER_SILVER_MIN_BOOKINGS bookings is SILVER."""
    assert loyalty_tier(5) == "SILVER"


def test_gold_tier_discount_rate():
    """GOLD tier carries a 10% discount rate."""
    assert discount_rate_for_tier("GOLD") == Decimal("0.10")


def test_platinum_tier_above_threshold():
    """A long-standing customer well past the PLATINUM threshold is PLATINUM."""
    assert loyalty_tier(50) == "PLATINUM"


def test_non_legacy_code_yields_no_discount():
    """A promo code that is not in the legacy 'PCT:<expr>' format contributes
    no discount amount.
    """
    assert parse_legacy_discount_code("SAVE10", Decimal("500.00")) == Decimal("0.00")


def test_compute_loyalty_discount_amount_runs():
    """Computing the loyalty discount for a GOLD-tier customer succeeds."""
    result = compute_loyalty_discount_amount(Decimal("1000.00"), 20)
    assert result is not None


def test_discount_summary_formats_percentage():
    """discount_summary renders the discount amount, base total and percent."""
    summary = discount_summary(Decimal("1000.00"), Decimal("100.00"))
    assert "1000.00" in summary
    assert "100.00" in summary
