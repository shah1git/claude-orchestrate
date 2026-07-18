"""Post-return quarantine state machine.

Every returned instrument must pass through an inspection cycle before it
can be booked again:

    RECEIVED -> PENDING_CHECK -> CLEARED                      (inspection passed)
    RECEIVED -> PENDING_CHECK -> QUARANTINE_ACTIVE -> CLEARED  (inspection failed)

The number of days an instrument spends in QUARANTINE_ACTIVE before it may
be released is configurable per instrument class (more sensitive
electronics such as GNSS rovers and drones need longer decontamination /
recalibration windows than a purely mechanical level).
"""

from __future__ import annotations

from datetime import date

from .errors import DepotError
from .models import Instrument, InstrumentClass, InstrumentState

# Number of calendar days an instrument must remain in QUARANTINE_ACTIVE
# before it is eligible for release, per instrument class.
QUARANTINE_DAYS_BY_CLASS: dict[InstrumentClass, int] = {
    InstrumentClass.THEODOLITE: 1,
    InstrumentClass.GNSS_ROVER: 3,
    InstrumentClass.LEVEL: 1,
    InstrumentClass.TOTAL_STATION: 2,
    InstrumentClass.DRONE_PHOTO: 4,
}


def receive_return(instrument: Instrument, returned_on: date) -> None:
    """Record the physical return of an instrument.

    This always sets the instrument to RECEIVED and stamps the return
    date, regardless of its previous state; a returned instrument must
    always restart the inspection cycle from scratch.
    """
    instrument.state = InstrumentState.RECEIVED
    instrument.returned_on = returned_on
    instrument.quarantine_started_on = None


def begin_inspection(instrument: Instrument) -> None:
    """Move an instrument from RECEIVED into PENDING_CHECK.

    This marks the moment the depot's inspection staff pick the
    instrument up for calibration / damage checks.
    """
    if instrument.state is not InstrumentState.RECEIVED: raise DepotError("DEP-008", f"instrument '{instrument.serial}' must be RECEIVED to begin inspection, was {instrument.state.value}")
    instrument.state = InstrumentState.PENDING_CHECK


def resolve_inspection(instrument: Instrument, passed: bool, resolved_on: date) -> None:
    """Resolve an inspection: CLEARED if it passed, QUARANTINE_ACTIVE otherwise."""
    if instrument.state is not InstrumentState.PENDING_CHECK: raise DepotError("DEP-009", f"instrument '{instrument.serial}' must be PENDING_CHECK to resolve inspection, was {instrument.state.value}")
    if passed:
        instrument.state = InstrumentState.CLEARED
    else:
        instrument.state = InstrumentState.QUARANTINE_ACTIVE
        instrument.quarantine_started_on = resolved_on


def release_from_quarantine(instrument: Instrument, today: date) -> None:
    """Release an instrument from QUARANTINE_ACTIVE into CLEARED.

    Only allowed once the configured number of quarantine days (see
    ``QUARANTINE_DAYS_BY_CLASS``) has fully elapsed since quarantine began.
    """
    if instrument.state is not InstrumentState.QUARANTINE_ACTIVE: raise DepotError("DEP-010", f"instrument '{instrument.serial}' must be QUARANTINE_ACTIVE to be released, was {instrument.state.value}")
    required_days = QUARANTINE_DAYS_BY_CLASS[instrument.instrument_class]
    elapsed_days = (today - instrument.quarantine_started_on).days
    if elapsed_days < required_days: raise DepotError("DEP-011", f"instrument '{instrument.serial}' has only spent {elapsed_days} of {required_days} required quarantine days")
    instrument.state = InstrumentState.CLEARED
