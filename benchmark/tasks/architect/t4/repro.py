"""Repro for architect/t4.

A single booking is created and returned; its own ``total_cost`` is correct.
Yet the revenue report — the only booking in the system — reports a *higher*
figure than that booking's total. Run from the fixture root:

    python3 repro.py
"""
from datetime import date

from depot import BookingEngine, InstrumentClass, InstrumentRegistry, Ledger, reports
from depot.models import DayType

registry = InstrumentRegistry()
ledger = Ledger()
engine = BookingEngine(registry, ledger)

registry.register("GR-0001", InstrumentClass.GNSS_ROVER)

# 12-day booking: a mix of field and office days, crossing a weekend, and
# longer than the long-booking cap threshold. Every one of those factors
# should pull the real total *below* a naive "field rate x days" list price.
day_plan = tuple(
    DayType.OFFICE if i % 3 == 2 else DayType.FIELD for i in range(12)
)
booking = engine.create_booking("KIT-1", ["GR-0001"], date(2026, 3, 2), day_plan)

revenue = reports.generate_report(ledger, "revenue")

print(f"returned booking.total_cost : {booking.total_cost}")
print(f"revenue report (1 booking)  : {revenue}")
if revenue != booking.total_cost:
    print(
        f"SYMPTOM: the revenue report ({revenue}) does not equal the only "
        f"booking's own total ({booking.total_cost}); it overstates by "
        f"{revenue - booking.total_cost}."
    )
else:
    print("no symptom: revenue matches the booking total")
