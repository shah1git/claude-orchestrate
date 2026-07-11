"""Repro for architect/t1.

Demonstrates that an instrument which was returned but never inspected
(still sitting in state RECEIVED, having never passed through
``begin_inspection`` / ``resolve_inspection``) can be booked a second time,
even though depot policy is that only a CLEARED instrument may be booked.

Run from the fixture root:

    python3 repro.py
"""

from datetime import date

from depot import BookingEngine, DayType, InstrumentClass, InstrumentRegistry, Ledger

registry = InstrumentRegistry()
ledger = Ledger()
engine = BookingEngine(registry, ledger)

# One fresh theodolite. New instruments register CLEARED.
registry.register("TH-0001", InstrumentClass.THEODOLITE)

# First booking: reserve -> issue -> return. A single field day is enough
# to exercise the full lifecycle once.
first_plan = (DayType.FIELD,)
first_booking = engine.create_booking("KIT-01", ["TH-0001"], date(2026, 2, 2), first_plan)
engine.issue_booking(first_booking)
engine.return_booking(first_booking, date(2026, 2, 3))

instrument = registry.get("TH-0001")
print(f"After return, instrument state = {instrument.state.value}")
print("(no begin_inspection / resolve_inspection was ever called on this instrument)")

# Per depot policy (see InstrumentState docstring in depot/models.py:
# "Only a CLEARED instrument may be booked."), the instrument above is
# RECEIVED, not CLEARED, and a second booking against it should be
# rejected. Attempt it and report what actually happens.
second_plan = (DayType.FIELD,)
try:
    second_booking = engine.create_booking("KIT-02", ["TH-0001"], date(2026, 2, 10), second_plan)
except Exception as exc:  # noqa: BLE001 - repro script, we want to see anything
    print(f"Second booking was rejected: {exc}")
else:
    print(
        f"SYMPTOM: second booking SUCCEEDED — booking_id={second_booking.booking_id}, "
        f"status={second_booking.status.value}, instrument_state={instrument.state.value} "
        f"(expected: rejected, instrument was not CLEARED)"
    )
