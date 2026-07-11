"""Hidden characterization tests for depot.tariff (builder t2 — refactor).

NOT visible to candidates during implementation. Dropped in against the
candidate's diff, applied to a clean fixture copy, at grading time.

These tests pin the exact Decimal output of ``compute_total`` across a
spread of day plans, computed by running the *unmodified* reference
``tariff.py`` before any refactor. A behaviour-preserving refactor must
keep every one of these values bit-for-bit identical. They also pin the
four documented values of the new ``day_coefficient`` helper.
"""

from datetime import date
from decimal import Decimal

from depot.models import DayType, InstrumentClass
from depot.tariff import COEF_FIELD_DAY, COEF_OFFICE_DAY, COEF_WEEKEND_EXTRA, compute_total, day_coefficient

MONDAY = date(2026, 7, 13)
SATURDAY = date(2026, 7, 11)


# --- day_coefficient: the four documented field/office x weekday/weekend
# combinations, expressed both as literal pinned values and as the product
# of the existing named constants (so a candidate cannot satisfy this by
# hardcoding numbers unrelated to COEF_FIELD_DAY / COEF_OFFICE_DAY /
# COEF_WEEKEND_EXTRA).

def test_day_coefficient_field_weekday():
    assert day_coefficient(DayType.FIELD, False) == Decimal("1.00") == COEF_FIELD_DAY


def test_day_coefficient_field_weekend():
    assert day_coefficient(DayType.FIELD, True) == Decimal("1.20") == COEF_FIELD_DAY * COEF_WEEKEND_EXTRA


def test_day_coefficient_office_weekday():
    assert day_coefficient(DayType.OFFICE, False) == Decimal("0.60") == COEF_OFFICE_DAY


def test_day_coefficient_office_weekend():
    assert day_coefficient(DayType.OFFICE, True) == Decimal("0.72") == COEF_OFFICE_DAY * COEF_WEEKEND_EXTRA


# --- compute_total: characterization (golden values pinned against the
# pre-refactor reference implementation) --------------------------------

def test_characterization_theodolite_short_all_field_weekday():
    """3 weekday field days, no weekend, no cap."""
    total = compute_total(InstrumentClass.THEODOLITE, MONDAY, (DayType.FIELD,) * 3)
    assert total == Decimal("4500.00")


def test_characterization_gnss_rover_weekend_field():
    """2 field days that are both weekend (Sat+Sun)."""
    total = compute_total(InstrumentClass.GNSS_ROVER, SATURDAY, (DayType.FIELD, DayType.FIELD))
    assert total == Decimal("8400.00")


def test_characterization_level_all_office_weekday():
    """5 weekday office days."""
    total = compute_total(InstrumentClass.LEVEL, MONDAY, (DayType.OFFICE,) * 5)
    assert total == Decimal("2700.00")


def test_characterization_total_station_full_week_field():
    """7 field days starting Monday: 5 weekday + Sat + Sun."""
    total = compute_total(InstrumentClass.TOTAL_STATION, MONDAY, (DayType.FIELD,) * 7)
    assert total == Decimal("20720.00")


def test_characterization_drone_photo_long_booking_capped():
    """12 field days starting Monday (> 10-day threshold): the uncapped
    subtotal (52080.00) exceeds the DRONE_PHOTO ceiling, so the cap
    engages.
    """
    total = compute_total(InstrumentClass.DRONE_PHOTO, MONDAY, (DayType.FIELD,) * 12)
    assert total == Decimal("33000.00")


def test_characterization_theodolite_long_booking_under_cap_not_reduced():
    """11 office days starting Monday (> 10-day threshold), but the
    uncapped subtotal (10260.00) is already below the THEODOLITE ceiling
    (12000.00) - crossing the threshold must not spuriously shrink a total
    that was never near the cap.
    """
    total = compute_total(InstrumentClass.THEODOLITE, MONDAY, (DayType.OFFICE,) * 11)
    assert total == Decimal("10260.00")


def test_characterization_level_weekend_office():
    """2 office days that are both weekend (Sat+Sun)."""
    total = compute_total(InstrumentClass.LEVEL, SATURDAY, (DayType.OFFICE, DayType.OFFICE))
    assert total == Decimal("1296.00")


def test_characterization_gnss_rover_mixed_field_office_weekday():
    """6 weekday days alternating field/office."""
    plan = (DayType.FIELD, DayType.OFFICE, DayType.FIELD, DayType.OFFICE, DayType.FIELD, DayType.OFFICE)
    total = compute_total(InstrumentClass.GNSS_ROVER, MONDAY, plan)
    assert total == Decimal("17220.00")


def test_characterization_total_station_long_mixed_weekend_capped():
    """14 days starting Saturday, alternating field/office (> 10-day
    threshold): the uncapped subtotal (33152.00) exceeds the
    TOTAL_STATION ceiling, so the cap engages and lands exactly on it.
    """
    plan = (DayType.FIELD, DayType.OFFICE) * 7
    total = compute_total(InstrumentClass.TOTAL_STATION, SATURDAY, plan)
    assert total == Decimal("22000.00")


def test_characterization_drone_photo_single_office_day():
    """A 1-day booking, office day, no weekend, no cap threshold reached."""
    total = compute_total(InstrumentClass.DRONE_PHOTO, MONDAY, (DayType.OFFICE,))
    assert total == Decimal("2520.00")


def test_characterization_theodolite_exactly_at_threshold_not_capped():
    """10 field days starting Monday: exactly at CAP_LONG_BOOKING_THRESHOLD_DAYS
    (not beyond it - the cap condition is a strict '>'), so the cap does
    NOT engage even though the uncapped subtotal (15600.00) exceeds the
    THEODOLITE ceiling (12000.00). This pins the exact boundary of the
    threshold comparison.
    """
    total = compute_total(InstrumentClass.THEODOLITE, MONDAY, (DayType.FIELD,) * 10)
    assert total == Decimal("15600.00")
