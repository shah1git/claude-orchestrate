"""Visible smoke tests for the depot.tariff refactor (builder t2).

These are copied into the candidate's workspace and may be run at any
time during implementation. They only check that the new helper exists
with the right signature and a couple of known values, plus a smoke test
of compute_total - they do not pin the full behaviour of compute_total.
See ticket.md for the full requirement (compute_total's observable
behaviour must be unchanged).
"""

from datetime import date
from decimal import Decimal

from depot.models import DayType, InstrumentClass
from depot.tariff import COEF_FIELD_DAY, COEF_OFFICE_DAY, COEF_WEEKEND_EXTRA, compute_total, day_coefficient

MONDAY = date(2026, 7, 13)


def test_day_coefficient_known_values():
    """day_coefficient(day_type, is_weekend) exists and returns the
    documented product of the existing coefficient constants for a field
    weekday and an office weekend.
    """
    assert day_coefficient(DayType.FIELD, False) == COEF_FIELD_DAY
    assert day_coefficient(DayType.OFFICE, True) == COEF_OFFICE_DAY * COEF_WEEKEND_EXTRA


def test_compute_total_smoke():
    """compute_total still runs and returns a positive Decimal total for
    an ordinary short booking.
    """
    total = compute_total(InstrumentClass.LEVEL, MONDAY, (DayType.FIELD, DayType.OFFICE))
    assert isinstance(total, Decimal)
    assert total > 0
