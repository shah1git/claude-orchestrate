"""Tests for depot.ledger and depot.reports: append-only history and the
summaries built strictly from it.
"""

from datetime import date

import pytest

from depot.booking import BookingEngine
from depot.errors import DepotError
from depot.ledger import Ledger
from depot.models import DayType, InstrumentClass
from depot.registry import InstrumentRegistry
from depot.reports import generate_report, operation_counts, subject_history, total_revenue

MONDAY = date(2026, 7, 13)


def test_ledger_append_only_and_ordered():
    """Records are returned in append order with a monotonic sequence number."""
    ledger = Ledger()
    first = ledger.append("booking_created", "BK-00001", {"total_cost": "100.00"})
    second = ledger.append("booking_issued", "BK-00001", {})
    assert first.sequence == 1
    assert second.sequence == 2
    assert len(ledger.all_records()) == 2
    assert [r.operation for r in ledger.records_for("BK-00001")] == ["booking_created", "booking_issued"]


def test_ledger_remove_raises():
    """The ledger is append-only: remove() always fails loudly."""
    ledger = Ledger()
    ledger.append("booking_created", "BK-00001", {"total_cost": "100.00"})
    with pytest.raises(DepotError) as exc_info:
        ledger.remove(1)
    assert exc_info.value.code == "DEP-023"


def test_ledger_empty_operation_raises():
    """An empty operation name is rejected."""
    ledger = Ledger()
    with pytest.raises(DepotError) as exc_info:
        ledger.append("", "BK-00001", {})
    assert exc_info.value.code == "DEP-021"


def test_ledger_empty_subject_raises():
    """An empty subject id is rejected."""
    ledger = Ledger()
    with pytest.raises(DepotError) as exc_info:
        ledger.append("booking_created", "", {})
    assert exc_info.value.code == "DEP-022"


def test_reports_revenue_and_counts():
    """Revenue and operation counts are derived purely from ledger records."""
    registry = InstrumentRegistry()
    ledger = Ledger()
    engine = BookingEngine(registry, ledger)
    registry.register("LV-0001", InstrumentClass.LEVEL)
    registry.register("LV-0002", InstrumentClass.LEVEL)

    booking_a = engine.create_booking("KIT-1", ["LV-0001"], MONDAY, (DayType.FIELD,))
    booking_b = engine.create_booking("KIT-2", ["LV-0002"], MONDAY, (DayType.FIELD,))
    engine.issue_booking(booking_a)

    assert total_revenue(ledger) == booking_a.total_cost + booking_b.total_cost
    counts = operation_counts(ledger)
    assert counts["booking_created"] == 2
    assert counts["booking_issued"] == 1
    assert generate_report(ledger, "revenue") == total_revenue(ledger)


def test_reports_subject_history():
    """subject_history returns the ordered records for one booking."""
    registry = InstrumentRegistry()
    ledger = Ledger()
    engine = BookingEngine(registry, ledger)
    registry.register("LV-0001", InstrumentClass.LEVEL)
    booking = engine.create_booking("KIT-1", ["LV-0001"], MONDAY, (DayType.FIELD,))
    engine.issue_booking(booking)

    history = subject_history(ledger, booking.booking_id)
    assert [r.operation for r in history] == ["booking_created", "booking_issued"]


def test_reports_unknown_type_raises():
    """generate_report rejects report types it doesn't know about."""
    ledger = Ledger()
    with pytest.raises(DepotError) as exc_info:
        generate_report(ledger, "bogus")
    assert exc_info.value.code == "DEP-025"


def test_reports_subject_history_unknown_raises():
    """subject_history rejects a subject id with no records at all."""
    ledger = Ledger()
    with pytest.raises(DepotError) as exc_info:
        subject_history(ledger, "nope")
    assert exc_info.value.code == "DEP-024"
