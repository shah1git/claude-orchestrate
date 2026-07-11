"""Append-only operations ledger.

Every state-changing operation performed anywhere in the depot (booking
created, issued, returned, cancelled; quarantine transitions, etc.) is
recorded here as an immutable ``LedgerRecord``. The ledger is the sole
source of truth for reports: nothing in ``reports.py`` is allowed to read
booking or instrument objects directly - it must reconstruct every figure
from the ledger's records.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .errors import DepotError


@dataclass(frozen=True)
class LedgerRecord:
    """One immutable entry in the operations ledger.

    ``sequence`` is a monotonically increasing integer assigned at append
    time, giving records a stable total order independent of wall-clock
    timestamp resolution.
    """

    sequence: int
    timestamp: datetime
    operation: str
    subject_id: str
    payload: dict[str, Any]


class Ledger:
    """Append-only store of ``LedgerRecord`` entries."""

    def __init__(self) -> None:
        """Create an empty ledger."""
        self._records: list[LedgerRecord] = []
        self._next_sequence = 1

    def append(self, operation: str, subject_id: str, payload: dict[str, Any]) -> LedgerRecord:
        """Append a new record and return it.

        ``payload`` is copied defensively so that later mutation of the
        caller's dict cannot alter a record already committed to history.
        """
        if not operation: raise DepotError("DEP-021", "ledger operation name must not be empty")
        if not subject_id: raise DepotError("DEP-022", "ledger subject id must not be empty")
        record = LedgerRecord(sequence=self._next_sequence, timestamp=datetime.now(), operation=operation, subject_id=subject_id, payload=dict(payload))
        self._records.append(record)
        self._next_sequence += 1
        return record

    def records_for(self, subject_id: str) -> tuple[LedgerRecord, ...]:
        """Return every record concerning ``subject_id``, in append order."""
        return tuple(record for record in self._records if record.subject_id == subject_id)

    def all_records(self) -> tuple[LedgerRecord, ...]:
        """Return the full ledger history as an immutable tuple (a
        defensive copy of the internal list).
        """
        return tuple(self._records)

    def remove(self, sequence: int) -> None:
        """Ledger records can never be removed.

        This method exists purely to make the append-only invariant
        explicit and to fail loudly if any caller ever attempts it,
        instead of silently doing nothing.
        """
        raise DepotError("DEP-023", "ledger records are immutable and cannot be removed")
