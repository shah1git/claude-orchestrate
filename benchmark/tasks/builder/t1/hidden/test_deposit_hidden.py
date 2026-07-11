"""Hidden acceptance tests for depot.deposit (builder t1).

NOT visible to candidates during implementation. Dropped in against the
candidate's diff, applied to a clean fixture copy, at grading time.
"""

from decimal import Decimal

import pytest

from depot.errors import DepotError
from depot.models import InstrumentClass
from depot.deposit import compute_deposit, deposit_refund


# --- compute_deposit: max(base, rate * total) choice -----------------------

def test_base_wins_when_total_is_small():
    """LEVEL, total=2000.00: rate*total=600.00 is below the base deposit
    of 1500.00, so the flat base deposit applies.
    """
    deposit = compute_deposit(InstrumentClass.LEVEL, Decimal("2000.00"))
    assert deposit == Decimal("1500.00")


def test_rate_wins_when_total_is_large():
    """LEVEL, total=10000.00: rate*total=3000.00 exceeds the 1500.00 base
    deposit, so the percentage-of-total floor applies instead.
    """
    deposit = compute_deposit(InstrumentClass.LEVEL, Decimal("10000.00"))
    assert deposit == Decimal("3000.00")


# --- compute_deposit: ceiling clamp -----------------------------------------

def test_deposit_exactly_at_ceiling_is_not_reduced_further():
    """THEODOLITE, total=50000.00: rate*total=15000.00 lands exactly on
    the class ceiling. The clamp must not push it below the ceiling value.
    """
    deposit = compute_deposit(InstrumentClass.THEODOLITE, Decimal("50000.00"))
    assert deposit == Decimal("15000.00")


def test_deposit_above_ceiling_is_clamped():
    """THEODOLITE, total=100000.00: rate*total=30000.00 is well above the
    15000.00 ceiling and must be clamped down to it.
    """
    deposit = compute_deposit(InstrumentClass.THEODOLITE, Decimal("100000.00"))
    assert deposit == Decimal("15000.00")


# --- compute_deposit: rounding ----------------------------------------------

def test_deposit_rounds_half_up_at_exact_half_cent():
    """LEVEL, total=5000.15: rate*total=1500.045 is exactly half a
    hundredth-of-a-kopeck short of 1500.05 - i.e. a genuine ROUND_HALF_UP
    tie (1500.04 vs 1500.05, with the discarded digit exactly 5). Banker's
    rounding (ROUND_HALF_EVEN, Python's decimal default) would keep the
    even hundredths digit and land on 1500.04; the depot's rounding rule
    is ROUND_HALF_UP, which rounds away from zero to 1500.05.
    """
    deposit = compute_deposit(InstrumentClass.LEVEL, Decimal("5000.15"))
    assert deposit == Decimal("1500.05")


# --- compute_deposit: errors ------------------------------------------------

def test_compute_deposit_unknown_class_raises():
    """A value that carries no deposit schedule is rejected with DEP-101."""
    with pytest.raises(DepotError) as exc_info:
        compute_deposit("BALLISTA", Decimal("1000.00"))
    assert exc_info.value.code == "DEP-101"


def test_compute_deposit_negative_total_raises():
    """A negative booking total is rejected with DEP-102."""
    with pytest.raises(DepotError) as exc_info:
        compute_deposit(InstrumentClass.LEVEL, Decimal("-1.00"))
    assert exc_info.value.code == "DEP-102"


# --- deposit_refund ----------------------------------------------------------

def test_refund_subtracts_surcharge():
    """A surcharge smaller than the deposit is simply subtracted from it."""
    refund = deposit_refund(Decimal("1000.00"), Decimal("250.00"))
    assert refund == Decimal("750.00")


def test_refund_floors_at_zero_when_surcharge_exceeds_deposit():
    """A surcharge larger than the deposit does not drive the refund
    negative - it floors at Decimal("0.00").
    """
    refund = deposit_refund(Decimal("500.00"), Decimal("800.00"))
    assert refund == Decimal("0.00")


def test_refund_negative_deposit_raises():
    """A negative deposit argument is rejected with DEP-103."""
    with pytest.raises(DepotError) as exc_info:
        deposit_refund(Decimal("-100.00"), Decimal("0.00"))
    assert exc_info.value.code == "DEP-103"


def test_refund_negative_surcharge_raises():
    """A negative damage surcharge argument is rejected with DEP-104."""
    with pytest.raises(DepotError) as exc_info:
        deposit_refund(Decimal("500.00"), Decimal("-1.00"))
    assert exc_info.value.code == "DEP-104"
