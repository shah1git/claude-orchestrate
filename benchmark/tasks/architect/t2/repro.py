"""Repro for architect/t2.

Demonstrates that ``tariff.compute_total`` undercharges a booking that
spans a Saturday: the module docstring's documented rule is "weekend days
[...] additionally get multiplied by COEF_WEEKEND_EXTRA", and Saturday is
unambiguously a weekend day. The script computes the actual total returned
by the fixture for a booking that includes exactly one Saturday, and
separately hand-computes the total that the documented rule implies (with
the weekend multiplier applied to *both* Saturday and Sunday), then
compares the two.

Run from the fixture root:

    python3 repro.py
"""

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from depot import DayType, InstrumentClass
from depot.tariff import COEF_FIELD_DAY, COEF_WEEKEND_EXTRA, RATE_THEODOLITE
from depot.tariff import compute_total

# 2026-01-03 is a Saturday (verified: date(2026, 1, 3).weekday() == 5).
# A 3-day, all-field-day booking starting there covers Sat, Sun, Mon - i.e.
# exactly one Saturday and one Sunday, so the two weekend days are easy to
# reason about by hand.
start_date = date(2026, 1, 3)
day_plan = (DayType.FIELD, DayType.FIELD, DayType.FIELD)

assert start_date.weekday() == 5, "fixture assumption broken: 2026-01-03 must be a Saturday"

actual_total = compute_total(InstrumentClass.THEODOLITE, start_date, day_plan)

# Hand-expected total per the documented rule: every day with
# weekday() in {5 (Sat), 6 (Sun)} gets COEF_WEEKEND_EXTRA on top of the
# day-type coefficient. This mirrors compute_total's own arithmetic, just
# with the weekend test written out explicitly and correctly (>= 5, not
# > 5), so it does not depend on the code under test for the weekend
# classification itself.
subtotal = Decimal("0")
for offset in range(len(day_plan)):
    current_date = start_date + timedelta(days=offset)
    coef = COEF_FIELD_DAY
    is_weekend = current_date.weekday() >= 5  # Sat=5, Sun=6
    if is_weekend:
        coef = coef * COEF_WEEKEND_EXTRA
    subtotal += RATE_THEODOLITE * coef
    print(f"  day {offset}: {current_date.isoformat()} ({current_date.strftime('%A')}), weekend={is_weekend}, coef={coef}")

expected_total = subtotal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

print(f"\nactual total (compute_total):      {actual_total}")
print(f"hand-expected total (documented rule): {expected_total}")

if actual_total < expected_total:
    print(
        f"SYMPTOM: actual total {actual_total} is LOWER than the hand-expected total "
        f"{expected_total} for a booking spanning a Saturday (shortfall = {expected_total - actual_total})"
    )
else:
    print("no discrepancy observed")
