"""Booking engine: ties tariff computation, quarantine gating and kit
compatibility together into the operations a front desk clerk performs:
create a booking, issue it, return it, or cancel it.

Every state-changing call also appends a record to the ledger, so the
ledger ends up as the single, append-only history of everything that
happened to a booking.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from . import quarantine, tariff
from .compat import validate_kit
from .errors import DepotError
from .ledger import Ledger
from .models import Booking, BookingStatus, DayType, InstrumentState, Kit
from .registry import InstrumentRegistry

# --- Kit-size limit ---------------------------------------------------------
# A crew realistically carries at most six instruments on one job; beyond
# that, the booking should be split into multiple kits.
CAP_MAX_KIT_SIZE = 6

# --- Late-return penalty and damage surcharge ------------------------------
# Flat penalty billed per calendar day an instrument is returned late.
RATE_LATE_RETURN_PENALTY_PER_DAY = Decimal("500.00")
# Multiplier applied to a booking's total when equipment comes back damaged.
COEF_DAMAGE_SURCHARGE = Decimal("1.50")


def compute_late_return_penalty(days_late: int) -> Decimal:
    """Compute the flat penalty for returning a booking after its expected
    end date. Returns Decimal("0.00") if the return was not late.
    """
    if days_late <= 0:
        return Decimal("0.00")
    return (RATE_LATE_RETURN_PENALTY_PER_DAY * days_late).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_damage_surcharge(base_total: Decimal) -> Decimal:
    """Apply the damage surcharge coefficient to a booking total.

    Called by front-desk staff when equipment is returned damaged, on top
    of (not instead of) the ordinary booking total.
    """
    return (base_total * COEF_DAMAGE_SURCHARGE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class BookingEngine:
    """Coordinates registry, tariff, quarantine, compat and ledger to
    implement the depot's booking workflow.
    """

    def __init__(self, registry: InstrumentRegistry, ledger: Ledger) -> None:
        """Create a booking engine bound to a registry and a ledger."""
        self._registry = registry
        self._ledger = ledger
        # Tracks which booking currently "owns" each serial, so a second
        # booking cannot be created against an instrument that is already
        # reserved or issued elsewhere.
        self._active_by_serial: dict[str, str] = {}
        self._next_booking_seq = 1

    def create_booking(self, kit_id: str, serials: list[str], start_date: date, day_plan: tuple[DayType, ...]) -> Booking:
        """Validate and create a new RESERVED booking for a kit of instruments.

        Validates, in order: every serial exists in the registry, the kit's
        instrument classes are mutually compatible, the kit is not larger
        than CAP_MAX_KIT_SIZE, every instrument is CLEARED, and no
        instrument already has an active booking.
        """
        instruments = [self._registry.get(serial) for serial in serials]
        classes = [instrument.instrument_class for instrument in instruments]
        validate_kit(classes)
        if len(serials) > CAP_MAX_KIT_SIZE: raise DepotError("DEP-016", f"kit of {len(serials)} instruments exceeds the maximum kit size of {CAP_MAX_KIT_SIZE}")

        for instrument in instruments:
            if instrument.state is not InstrumentState.CLEARED: raise DepotError("DEP-015", f"instrument '{instrument.serial}' is not cleared for booking (state={instrument.state.value})")
            if instrument.serial in self._active_by_serial: raise DepotError("DEP-020", f"instrument '{instrument.serial}' already has an active booking")

        # Kit total is the sum of already-rounded per-instrument totals
        # (see tariff.compute_total's rounding rule); summing rounded
        # Decimals introduces no further rounding error.
        total = Decimal("0.00")
        for instrument in instruments:
            total += tariff.compute_total(instrument.instrument_class, start_date, day_plan)

        booking_id = f"BK-{self._next_booking_seq:05d}"
        self._next_booking_seq += 1
        kit = Kit(kit_id=kit_id, serials=tuple(serials))
        booking = Booking(booking_id=booking_id, kit=kit, start_date=start_date, day_plan=tuple(day_plan), status=BookingStatus.RESERVED, total_cost=total)

        for serial in serials:
            self._active_by_serial[serial] = booking_id
        self._ledger.append("booking_created", booking_id, {"serials": list(serials), "total_cost": str(total)})
        return booking

    def issue_booking(self, booking: Booking) -> None:
        """Transition a RESERVED booking to ISSUED (equipment handed over)."""
        if booking.status is not BookingStatus.RESERVED: raise DepotError("DEP-017", f"booking '{booking.booking_id}' must be RESERVED to be issued, was {booking.status.value}")
        booking.status = BookingStatus.ISSUED
        self._ledger.append("booking_issued", booking.booking_id, {})

    def return_booking(self, booking: Booking, returned_on: date) -> None:
        """Transition an ISSUED booking to RETURNED and start the
        quarantine cycle for every instrument in the kit.
        """
        if booking.status is not BookingStatus.ISSUED: raise DepotError("DEP-018", f"booking '{booking.booking_id}' must be ISSUED to be returned, was {booking.status.value}")
        booking.status = BookingStatus.RETURNED
        for serial in booking.kit.serials:
            instrument = self._registry.get(serial)
            quarantine.receive_return(instrument, returned_on)
            self._active_by_serial.pop(serial, None)
        self._ledger.append("booking_returned", booking.booking_id, {"returned_on": returned_on.isoformat()})

    def cancel_booking(self, booking: Booking) -> None:
        """Cancel a RESERVED booking before it is ever issued."""
        if booking.status is not BookingStatus.RESERVED: raise DepotError("DEP-019", f"booking '{booking.booking_id}' must be RESERVED to be cancelled, was {booking.status.value}")
        booking.status = BookingStatus.CANCELLED
        for serial in booking.kit.serials:
            self._active_by_serial.pop(serial, None)
        self._ledger.append("booking_cancelled", booking.booking_id, {})
