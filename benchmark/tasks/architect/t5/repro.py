"""Repro for architect/t5.

The documented pricing rules (see depot/tariff.py module docstring and
depot/models.py) say this exact 7-day theodolite booking should cost
9180.00 RUB. The engine returns a different, higher figure. Run from the
fixture root:

    python3 repro.py
"""
from datetime import date
from decimal import Decimal

from depot import tariff
from depot.models import DayType, InstrumentClass

# Wed 2026-03-04 .. Tue 2026-03-10. Offsets 3 and 4 fall on Sat and Sun.
start = date(2026, 3, 4)
day_plan = (
    DayType.FIELD,    # Wed
    DayType.OFFICE,   # Thu
    DayType.FIELD,    # Fri
    DayType.FIELD,    # Sat
    DayType.OFFICE,   # Sun
    DayType.OFFICE,   # Mon
    DayType.FIELD,    # Tue
)

EXPECTED = Decimal("9180.00")  # per the documented pricing rules
actual = tariff.compute_total(InstrumentClass.THEODOLITE, start, day_plan)

print(f"expected (documented rules): {EXPECTED}")
print(f"actual   (engine)          : {actual}")
if actual != EXPECTED:
    print(
        f"SYMPTOM: booking total is off by {actual - EXPECTED} "
        f"(actual {actual} vs documented {EXPECTED})."
    )
else:
    print("no symptom")
