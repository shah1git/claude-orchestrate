"""Tests for depot.quarantine: the post-return inspection state machine."""

from datetime import date

import pytest

from depot.errors import DepotError
from depot.models import Instrument, InstrumentClass, InstrumentState
from depot.quarantine import begin_inspection, receive_return, release_from_quarantine, resolve_inspection


def _fresh_instrument() -> Instrument:
    return Instrument(serial="LV-0001", instrument_class=InstrumentClass.LEVEL)


def test_full_cycle_pass_inspection():
    """RECEIVED -> PENDING_CHECK -> CLEARED when the inspection passes."""
    instrument = _fresh_instrument()
    receive_return(instrument, date(2026, 7, 10))
    assert instrument.state is InstrumentState.RECEIVED
    begin_inspection(instrument)
    assert instrument.state is InstrumentState.PENDING_CHECK
    resolve_inspection(instrument, passed=True, resolved_on=date(2026, 7, 10))
    assert instrument.state is InstrumentState.CLEARED


def test_full_cycle_fail_then_quarantine_then_clear():
    """RECEIVED -> PENDING_CHECK -> QUARANTINE_ACTIVE -> CLEARED once the
    per-class quarantine duration has elapsed.
    """
    instrument = _fresh_instrument()
    receive_return(instrument, date(2026, 7, 10))
    begin_inspection(instrument)
    resolve_inspection(instrument, passed=False, resolved_on=date(2026, 7, 10))
    assert instrument.state is InstrumentState.QUARANTINE_ACTIVE

    # LEVEL requires 1 quarantine day; releasing on the same day is too early.
    with pytest.raises(DepotError) as exc_info:
        release_from_quarantine(instrument, date(2026, 7, 10))
    assert exc_info.value.code == "DEP-011"

    release_from_quarantine(instrument, date(2026, 7, 11))
    assert instrument.state is InstrumentState.CLEARED


def test_invalid_transition_raises():
    """Beginning an inspection on an instrument that was never returned is rejected."""
    instrument = _fresh_instrument()  # starts CLEARED, not RECEIVED
    with pytest.raises(DepotError) as exc_info:
        begin_inspection(instrument)
    assert exc_info.value.code == "DEP-008"
