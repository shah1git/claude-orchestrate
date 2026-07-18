"""Kit incompatibility matrix.

Some instrument classes must never be booked together in the same kit.
The two invented rules modelled here:

* DRONE_PHOTO + GNSS_ROVER - the drone's photogrammetry radio link and the
  rover's RTK correction stream both use adjacent frequency bands and
  interfere with each other in the field.
* TOTAL_STATION + THEODOLITE - the depot only stocks one "primary angle
  instrument" per crew; issuing both together is treated as a booking
  mistake rather than a legitimate combined kit.
"""

from __future__ import annotations

from .errors import DepotError
from .models import InstrumentClass

# Each entry is an unordered pair (frozenset of exactly two classes) that
# must never appear together in a single kit.
INCOMPATIBLE_PAIRS: frozenset[frozenset[InstrumentClass]] = frozenset(
    {
        frozenset({InstrumentClass.DRONE_PHOTO, InstrumentClass.GNSS_ROVER}),
        frozenset({InstrumentClass.TOTAL_STATION, InstrumentClass.THEODOLITE}),
    }
)


def validate_kit(classes: list[InstrumentClass]) -> None:
    """Validate that a proposed kit's instrument classes contain no
    incompatible pair.

    ``classes`` is the list of instrument classes for every serial in the
    kit (duplicates allowed, e.g. two theodolites is fine on its own).
    """
    if not classes: raise DepotError("DEP-012", "kit must contain at least one instrument")
    for instrument_class in classes:
        if instrument_class not in InstrumentClass: raise DepotError("DEP-014", f"unknown instrument class '{instrument_class}' encountered while checking kit compatibility")
    distinct_classes = set(classes)
    for pair in INCOMPATIBLE_PAIRS:
        if pair.issubset(distinct_classes): raise DepotError("DEP-013", f"kit contains incompatible instrument classes: {sorted(c.value for c in pair)}")
