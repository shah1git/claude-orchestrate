"""Instrument registry: keeps track of every physical instrument known to
the depot and validates serial numbers before an instrument is admitted.

Serial numbers follow the fixed format ``XX-9999`` where ``XX`` is a
two-letter prefix tied to the instrument class (see
``SERIAL_PREFIX_BY_CLASS``) and ``9999`` is a four-digit sequence number.
"""

from __future__ import annotations

import re

from .errors import DepotError
from .models import Instrument, InstrumentClass

# Two-letter serial prefix required for each instrument class. Enforcing
# this at registration time lets anyone reading a serial number ("GR-0007")
# immediately know what kind of equipment it refers to, without a lookup.
SERIAL_PREFIX_BY_CLASS: dict[InstrumentClass, str] = {
    InstrumentClass.THEODOLITE: "TH",
    InstrumentClass.GNSS_ROVER: "GR",
    InstrumentClass.LEVEL: "LV",
    InstrumentClass.TOTAL_STATION: "TS",
    InstrumentClass.DRONE_PHOTO: "DR",
}

_SERIAL_PATTERN = re.compile(r"^[A-Z]{2}-\d{4}$")


def validate_serial(serial: str, instrument_class: InstrumentClass) -> None:
    """Validate that ``serial`` is well-formed and matches its class prefix.

    Raises DepotError("DEP-001", ...) if the serial does not match the
    required ``XX-9999`` shape, and DepotError("DEP-002", ...) if it is
    well-formed but carries the wrong class prefix.
    """
    if not _SERIAL_PATTERN.fullmatch(serial): raise DepotError("DEP-001", f"serial '{serial}' does not match required format XX-9999")
    expected_prefix = SERIAL_PREFIX_BY_CLASS[instrument_class]
    if not serial.startswith(expected_prefix): raise DepotError("DEP-002", f"serial '{serial}' does not carry the '{expected_prefix}' prefix required for {instrument_class.value}")


class InstrumentRegistry:
    """In-memory registry of every instrument the depot owns."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._instruments: dict[str, Instrument] = {}

    def register(self, serial: str, instrument_class: InstrumentClass) -> Instrument:
        """Validate and admit a new instrument, returning it.

        Newly registered instruments start CLEARED (fresh stock is assumed
        ready to rent; it has not been through a quarantine cycle because
        it has never been returned).
        """
        validate_serial(serial, instrument_class)
        if serial in self._instruments: raise DepotError("DEP-003", f"serial '{serial}' is already registered")
        instrument = Instrument(serial=serial, instrument_class=instrument_class)
        self._instruments[serial] = instrument
        return instrument

    def get(self, serial: str) -> Instrument:
        """Look up a registered instrument by serial number."""
        if serial not in self._instruments: raise DepotError("DEP-004", f"instrument '{serial}' not found in registry")
        return self._instruments[serial]

    def all(self) -> tuple[Instrument, ...]:
        """Return every registered instrument, in registration order."""
        return tuple(self._instruments.values())

    def by_class(self, instrument_class: InstrumentClass) -> tuple[Instrument, ...]:
        """Return all registered instruments of a given class."""
        return tuple(i for i in self._instruments.values() if i.instrument_class is instrument_class)
