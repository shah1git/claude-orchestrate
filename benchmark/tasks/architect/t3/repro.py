"""Repro for architect/t3.

Demonstrates that a CLEARED instrument which was merely *part of* a kit
booking that later failed validation (because a different instrument in the
same kit was not CLEARED) becomes permanently unbookable on its own: a
subsequent, independent attempt to book that instrument alone is rejected
with DEP-020 ("already has an active booking"), even though no booking for
it was ever actually created.

Run from the fixture root:

    python3 repro.py
"""

from datetime import date

from depot import BookingEngine, DayType, InstrumentClass, InstrumentRegistry, Ledger
from depot.errors import DepotError

registry = InstrumentRegistry()
ledger = Ledger()
engine = BookingEngine(registry, ledger)

# Two fresh theodolites. New instruments register CLEARED.
registry.register("TH-0001", InstrumentClass.THEODOLITE)
registry.register("TH-0002", InstrumentClass.THEODOLITE)

instrument_a = registry.get("TH-0001")
instrument_b = registry.get("TH-0002")

# Drive B through a first, independent booking lifecycle and return it
# WITHOUT ever inspecting it (no begin_inspection / resolve_inspection),
# so it lands back in RECEIVED, not CLEARED, per the quarantine state
# machine documented in depot/quarantine.py.
plan = (DayType.FIELD,)
b_only_booking = engine.create_booking("KIT-B-SOLO", ["TH-0002"], date(2026, 2, 2), plan)
engine.issue_booking(b_only_booking)
engine.return_booking(b_only_booking, date(2026, 2, 3))

print(f"instrument A ({instrument_a.serial}) state = {instrument_a.state.value}")
print(f"instrument B ({instrument_b.serial}) state = {instrument_b.state.value} (returned, never inspected)")

# Attempt a kit booking of [A, B]. A is CLEARED and should validate fine;
# B is RECEIVED (not CLEARED) and must be rejected with DEP-015. Per depot
# policy this validation failure should mean NEITHER instrument ends up
# reserved -- a rejected booking should reserve nothing.
try:
    engine.create_booking("KIT-AB", ["TH-0001", "TH-0002"], date(2026, 3, 2), plan)
except DepotError as exc:
    print(f"kit [A, B] booking rejected as expected: [{exc.code}] {exc.message}")
else:
    raise AssertionError("expected kit [A, B] booking to raise DEP-015 (B is not CLEARED)")

# A was never part of any successful booking: the only booking it has ever
# been mentioned in (KIT-AB) failed validation and was never created. A is
# still CLEARED and has no real, live booking anywhere.
print(f"instrument A ({instrument_a.serial}) state = {instrument_a.state.value} (unchanged, still CLEARED)")

# So booking A alone, on its own, should succeed cleanly.
try:
    solo_booking = engine.create_booking("KIT-A-SOLO", ["TH-0001"], date(2026, 4, 1), plan)
except DepotError as exc:
    print(
        f"SYMPTOM: booking A alone FAILED with [{exc.code}] {exc.message} -- "
        f"but A is CLEARED and was never part of any booking that actually got created "
        f"(the only booking mentioning A, KIT-AB, raised DEP-015 and should have reserved nothing)"
    )
else:
    print(
        f"booking A alone SUCCEEDED -- booking_id={solo_booking.booking_id}, "
        f"status={solo_booking.status.value} (this is the expected, correct behaviour)"
    )
