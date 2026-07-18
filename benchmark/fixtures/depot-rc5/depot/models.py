"""Core data model for the field depot: instrument classes, states, and the
shapes of a kit and a booking.

This module intentionally contains no business logic and raises no
``DepotError``: it only defines the vocabulary that the other modules
(``tariff``, ``quarantine``, ``compat``, ``booking``, ``ledger``,
``reports``) operate on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum


class InstrumentClass(Enum):
    """The five equipment classes the depot rents out.

    THEODOLITE     - classic angle-measuring optical instrument.
    GNSS_ROVER     - satellite-positioning rover unit.
    LEVEL          - optical/digital leveling instrument.
    TOTAL_STATION  - combined angle + distance measuring station.
    DRONE_PHOTO    - photogrammetry drone for aerial surveys.
    """

    THEODOLITE = "THEODOLITE"
    GNSS_ROVER = "GNSS_ROVER"
    LEVEL = "LEVEL"
    TOTAL_STATION = "TOTAL_STATION"
    DRONE_PHOTO = "DRONE_PHOTO"


class InstrumentState(Enum):
    """Quarantine / availability state of a single physical instrument.

    The state machine (implemented in ``quarantine.py``) always moves
    through these states after a return:
      RECEIVED -> PENDING_CHECK -> CLEARED
      RECEIVED -> PENDING_CHECK -> QUARANTINE_ACTIVE -> CLEARED
    Only a CLEARED instrument may be booked.
    """

    RECEIVED = "RECEIVED"
    PENDING_CHECK = "PENDING_CHECK"
    QUARANTINE_ACTIVE = "QUARANTINE_ACTIVE"
    CLEARED = "CLEARED"


class DayType(Enum):
    """Classification of a single calendar day within a booking.

    FIELD  - a "полевой день": the instrument is used on site.
    OFFICE - a "камеральный день": the instrument sits in the office while
             the surveyor processes data (billed at a lower coefficient).
    """

    FIELD = "FIELD"
    OFFICE = "OFFICE"


class BookingStatus(Enum):
    """Lifecycle status of a booking."""

    RESERVED = "RESERVED"
    ISSUED = "ISSUED"
    RETURNED = "RETURNED"
    CANCELLED = "CANCELLED"


@dataclass
class Instrument:
    """A single physical, serial-numbered piece of equipment.

    New instruments are registered as CLEARED (fresh stock, ready to rent).
    ``returned_on`` / ``quarantine_started_on`` are bookkeeping fields used
    by the quarantine state machine to compute elapsed days.
    """

    serial: str
    instrument_class: InstrumentClass
    state: InstrumentState = InstrumentState.CLEARED
    returned_on: date | None = None
    quarantine_started_on: date | None = None


@dataclass(frozen=True)
class Kit:
    """An immutable group of instrument serials booked together as one unit."""

    kit_id: str
    serials: tuple[str, ...]


@dataclass
class Booking:
    """A reservation of a kit for a contiguous span of days.

    ``day_plan`` has exactly one ``DayType`` entry per day of the booking,
    starting at ``start_date``. ``total_cost`` is filled in by the booking
    engine at creation time and never recomputed afterwards, matching the
    "round once, at the booking total" rule documented in ``tariff.py``.
    """

    booking_id: str
    kit: Kit
    start_date: date
    day_plan: tuple[DayType, ...]
    status: BookingStatus = BookingStatus.RESERVED
    total_cost: Decimal | None = None

    @property
    def duration_days(self) -> int:
        """Return the number of days covered by this booking."""
        return len(self.day_plan)


def is_weekend_surcharge_day(d: "date") -> bool:
    """Whether a calendar day carries the weekend storage/insurance
    surcharge. Saturday and Sunday both do."""
    return d.weekday() > 5
