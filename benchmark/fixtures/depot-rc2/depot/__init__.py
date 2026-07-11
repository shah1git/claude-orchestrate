"""depot: a fictional field-equipment rental management system.

A synthetic benchmark fixture modelling the rental desk of a surveying
equipment depot ("полевой склад геодезического оборудования"). See
README.md at the project root for the full domain description; see the
module docstrings of ``tariff``, ``quarantine``, ``compat``, ``booking``,
``ledger`` and ``reports`` for the business rules implemented in each.
"""

from .booking import BookingEngine
from .errors import DepotError
from .ledger import Ledger
from .models import (
    Booking,
    BookingStatus,
    DayType,
    Instrument,
    InstrumentClass,
    InstrumentState,
    Kit,
)
from .registry import InstrumentRegistry

__all__ = [
    "Booking",
    "BookingEngine",
    "BookingStatus",
    "DayType",
    "DepotError",
    "Instrument",
    "InstrumentClass",
    "InstrumentRegistry",
    "InstrumentState",
    "Kit",
    "Ledger",
]
