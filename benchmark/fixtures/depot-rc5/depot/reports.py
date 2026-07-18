"""Reporting: every figure here is derived strictly from ``Ledger``
records, never from live ``Booking``/``Instrument`` objects, so that a
report always reflects exactly what was recorded, in order.
"""

from __future__ import annotations

from decimal import Decimal

from .errors import DepotError
from .ledger import Ledger, LedgerRecord

# The only report types ``generate_report`` currently knows how to build.
KNOWN_REPORT_TYPES = frozenset({"revenue", "operation_counts"})


def total_revenue(ledger: Ledger) -> Decimal:
    """Sum the ``total_cost`` of every ``booking_created`` record.

    Cancelled bookings are intentionally still counted here: this report
    answers "how much has been booked", not "how much was collected".
    """
    total = Decimal("0.00")
    for record in ledger.all_records():
        if record.operation == "booking_created":
            total += Decimal(record.payload["total_cost"])
    return total


def operation_counts(ledger: Ledger) -> dict[str, int]:
    """Return a count of ledger records grouped by operation name."""
    counts: dict[str, int] = {}
    for record in ledger.all_records():
        counts[record.operation] = counts.get(record.operation, 0) + 1
    return counts


def subject_history(ledger: Ledger, subject_id: str) -> tuple[LedgerRecord, ...]:
    """Return the ordered ledger history of a single booking or instrument.

    Raises if no records exist for ``subject_id`` at all, since that
    normally indicates a typo'd or unknown identifier rather than a
    subject with a legitimately empty history.
    """
    records = ledger.records_for(subject_id)
    if not records: raise DepotError("DEP-024", f"no ledger records found for subject '{subject_id}'")
    return records


def generate_report(ledger: Ledger, report_type: str) -> Decimal | dict[str, int]:
    """Dispatch to the requested report by name.

    Supported values of ``report_type`` are listed in
    ``KNOWN_REPORT_TYPES``; anything else is a caller error.
    """
    if report_type == "revenue":
        return total_revenue(ledger)
    if report_type == "operation_counts":
        return operation_counts(ledger)
    raise DepotError("DEP-025", f"unknown report type '{report_type}'")
