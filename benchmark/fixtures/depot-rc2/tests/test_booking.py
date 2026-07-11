"""Tests for depot.booking: the engine tying tariff, quarantine and compat
together into create/issue/return/cancel operations.
"""

from datetime import date

import pytest

from depot.booking import BookingEngine
from depot.errors import DepotError
from depot.ledger import Ledger
from depot.models import BookingStatus, DayType, InstrumentClass, InstrumentState
from depot.registry import InstrumentRegistry

MONDAY = date(2026, 7, 13)


def _engine() -> tuple[BookingEngine, InstrumentRegistry, Ledger]:
    registry = InstrumentRegistry()
    ledger = Ledger()
    return BookingEngine(registry, ledger), registry, ledger


def test_create_issue_return_cycle():
    """A compatible, cleared kit can be created, issued and returned, and
    the returned instruments land back in RECEIVED (start of quarantine).
    """
    engine, registry, ledger = _engine()
    registry.register("TH-0001", InstrumentClass.THEODOLITE)
    registry.register("GR-0001", InstrumentClass.GNSS_ROVER)

    booking = engine.create_booking("KIT-1", ["TH-0001", "GR-0001"], MONDAY, (DayType.FIELD, DayType.FIELD))
    assert booking.status is BookingStatus.RESERVED
    assert booking.total_cost > 0

    engine.issue_booking(booking)
    assert booking.status is BookingStatus.ISSUED

    engine.return_booking(booking, date(2026, 7, 15))
    assert booking.status is BookingStatus.RETURNED
    assert registry.get("TH-0001").state is InstrumentState.RECEIVED
    assert registry.get("GR-0001").state is InstrumentState.RECEIVED


def test_booking_uncleared_instrument_raises():
    """An instrument that is not CLEARED cannot be booked."""
    engine, registry, ledger = _engine()
    instrument = registry.register("LV-0001", InstrumentClass.LEVEL)
    instrument.state = InstrumentState.PENDING_CHECK

    with pytest.raises(DepotError) as exc_info:
        engine.create_booking("KIT-1", ["LV-0001"], MONDAY, (DayType.FIELD,))
    assert exc_info.value.code == "DEP-015"


def test_double_booking_raises():
    """An instrument already tied to an active booking cannot be booked again."""
    engine, registry, ledger = _engine()
    registry.register("LV-0001", InstrumentClass.LEVEL)
    engine.create_booking("KIT-1", ["LV-0001"], MONDAY, (DayType.FIELD,))

    with pytest.raises(DepotError) as exc_info:
        engine.create_booking("KIT-2", ["LV-0001"], MONDAY, (DayType.FIELD,))
    assert exc_info.value.code == "DEP-020"


def test_incompatible_kit_raises_through_engine():
    """The booking engine enforces the kit incompatibility matrix too."""
    engine, registry, ledger = _engine()
    registry.register("DR-0001", InstrumentClass.DRONE_PHOTO)
    registry.register("GR-0001", InstrumentClass.GNSS_ROVER)

    with pytest.raises(DepotError) as exc_info:
        engine.create_booking("KIT-1", ["DR-0001", "GR-0001"], MONDAY, (DayType.FIELD,))
    assert exc_info.value.code == "DEP-013"


def test_kit_size_cap_raises():
    """A kit larger than CAP_MAX_KIT_SIZE is rejected."""
    engine, registry, ledger = _engine()
    serials = []
    for i in range(7):
        serial = f"LV-{i:04d}"
        registry.register(serial, InstrumentClass.LEVEL)
        serials.append(serial)

    with pytest.raises(DepotError) as exc_info:
        engine.create_booking("KIT-1", serials, MONDAY, (DayType.FIELD,))
    assert exc_info.value.code == "DEP-016"


def test_cancel_booking():
    """A RESERVED booking can be cancelled, freeing up its instruments."""
    engine, registry, ledger = _engine()
    registry.register("LV-0001", InstrumentClass.LEVEL)
    booking = engine.create_booking("KIT-1", ["LV-0001"], MONDAY, (DayType.FIELD,))

    engine.cancel_booking(booking)
    assert booking.status is BookingStatus.CANCELLED

    # The instrument is free again: a new booking against it succeeds.
    engine.create_booking("KIT-2", ["LV-0001"], MONDAY, (DayType.FIELD,))
