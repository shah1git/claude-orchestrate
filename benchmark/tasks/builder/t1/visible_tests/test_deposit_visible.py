"""Visible smoke tests for depot.deposit (builder t1).

These are copied into the candidate's workspace and may be run at any
time during implementation. They do not cover every rule in the spec -
see ticket.md for the full behaviour required.
"""

from decimal import Decimal

from depot.models import InstrumentClass
from depot.deposit import compute_deposit, deposit_refund


def test_compute_deposit_happy_path_base_floor():
    """THEODOLITE, total=1000.00: rate*total=300.00 is well below the
    class's base deposit, so the flat base deposit applies.
    """
    deposit = compute_deposit(InstrumentClass.THEODOLITE, Decimal("1000.00"))
    assert deposit == Decimal("3000.00")


def test_compute_deposit_clamped_to_ceiling():
    """GNSS_ROVER, total=200000.00: rate*total is far above the class
    ceiling, so the deposit is clamped down to the ceiling value.
    """
    deposit = compute_deposit(InstrumentClass.GNSS_ROVER, Decimal("200000.00"))
    assert deposit == Decimal("35000.00")


def test_deposit_refund_basic():
    """A damage surcharge smaller than the deposit is subtracted from it."""
    refund = deposit_refund(Decimal("2000.00"), Decimal("500.00"))
    assert refund == Decimal("1500.00")
