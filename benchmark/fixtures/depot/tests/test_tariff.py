"""Tests for depot.tariff: day-type coefficients, weekend multiplier,
long-booking cap and rounding.
"""

from datetime import date
from decimal import Decimal

import pytest

from depot.errors import DepotError
from depot.models import DayType, InstrumentClass
from depot.tariff import (
    CAP_TOTAL_LEVEL,
    COEF_FIELD_DAY,
    COEF_OFFICE_DAY,
    COEF_WEEKEND_EXTRA,
    RATE_LEVEL,
    compute_total,
)

MONDAY = date(2026, 7, 13)
SATURDAY = date(2026, 7, 11)


def test_field_day_weekday_cost():
    """A single field day on a weekday costs exactly base_rate * COEF_FIELD_DAY."""
    total = compute_total(InstrumentClass.LEVEL, MONDAY, (DayType.FIELD,))
    expected = (RATE_LEVEL * COEF_FIELD_DAY).quantize(Decimal("0.01"))
    assert total == expected


def test_office_day_cheaper_than_field():
    """An office day on the same weekday is billed at the lower office coefficient."""
    field_total = compute_total(InstrumentClass.LEVEL, MONDAY, (DayType.FIELD,))
    office_total = compute_total(InstrumentClass.LEVEL, MONDAY, (DayType.OFFICE,))
    assert office_total < field_total
    assert office_total == (RATE_LEVEL * COEF_OFFICE_DAY).quantize(Decimal("0.01"))


def test_weekend_multiplier_applied():
    """A field day falling on a weekend gets the extra weekend multiplier."""
    total = compute_total(InstrumentClass.LEVEL, SATURDAY, (DayType.FIELD,))
    expected = (RATE_LEVEL * COEF_FIELD_DAY * COEF_WEEKEND_EXTRA).quantize(Decimal("0.01"))
    assert total == expected


def test_long_booking_cap_applied():
    """A booking longer than the long-booking threshold is clamped to the
    per-instrument cap instead of growing linearly.
    """
    long_plan = tuple(DayType.FIELD for _ in range(11))
    total = compute_total(InstrumentClass.LEVEL, MONDAY, long_plan)
    assert total == CAP_TOTAL_LEVEL


def test_empty_day_plan_raises():
    """A booking with no days at all is rejected."""
    with pytest.raises(DepotError) as exc_info:
        compute_total(InstrumentClass.LEVEL, MONDAY, ())
    assert exc_info.value.code == "DEP-005"
