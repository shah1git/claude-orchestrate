"""Security deposit (залог) computation for the field depot.

Deposit model
-------------
Every booking's equipment deposit is the larger of two floors: a flat
per-class base deposit (``DEP_BASE_*``) and a percentage of the booking's
total cost (``DEP_RATE_OF_TOTAL``). Whichever floor is higher applies -
this protects the depot on both small, cheap bookings (the base floor
covers the replacement cost of, say, a level even when its own rental
total is tiny) and large, expensive bookings (the percentage floor scales
with the value actually at risk). The result is then clamped to a flat
per-class ceiling (``DEP_CAP_*``) so that a single very long or very
expensive booking never demands an unreasonably large deposit.

Refund model
------------
On return, ``deposit_refund`` gives back the deposit minus any damage
surcharge billed against it, floored at zero - the depot never asks the
renter to pay *more* through the deposit refund call; any surcharge in
excess of the deposit is billed separately, elsewhere (see
``booking.compute_damage_surcharge``).

Rounding rule
-------------
As in ``tariff.py``: money is ``decimal.Decimal``, and quantization to
kopecks (two decimal places, ROUND_HALF_UP) happens exactly once, at the
very end of each public function in this module.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from .errors import DepotError
from .models import InstrumentClass

# --- Per-class flat base deposit (RUB) -------------------------------------
DEP_BASE_THEODOLITE = Decimal("3000.00")
DEP_BASE_GNSS_ROVER = Decimal("8000.00")
DEP_BASE_LEVEL = Decimal("1500.00")
DEP_BASE_TOTAL_STATION = Decimal("6000.00")
DEP_BASE_DRONE_PHOTO = Decimal("10000.00")

# --- Percentage-of-booking-total floor --------------------------------------
DEP_RATE_OF_TOTAL = Decimal("0.30")

# --- Per-class deposit ceiling (RUB) ----------------------------------------
DEP_CAP_THEODOLITE = Decimal("15000.00")
DEP_CAP_GNSS_ROVER = Decimal("35000.00")
DEP_CAP_LEVEL = Decimal("9000.00")
DEP_CAP_TOTAL_STATION = Decimal("28000.00")
DEP_CAP_DRONE_PHOTO = Decimal("42000.00")

DEP_BASE_BY_CLASS: dict[InstrumentClass, Decimal] = {
    InstrumentClass.THEODOLITE: DEP_BASE_THEODOLITE,
    InstrumentClass.GNSS_ROVER: DEP_BASE_GNSS_ROVER,
    InstrumentClass.LEVEL: DEP_BASE_LEVEL,
    InstrumentClass.TOTAL_STATION: DEP_BASE_TOTAL_STATION,
    InstrumentClass.DRONE_PHOTO: DEP_BASE_DRONE_PHOTO,
}

DEP_CAP_BY_CLASS: dict[InstrumentClass, Decimal] = {
    InstrumentClass.THEODOLITE: DEP_CAP_THEODOLITE,
    InstrumentClass.GNSS_ROVER: DEP_CAP_GNSS_ROVER,
    InstrumentClass.LEVEL: DEP_CAP_LEVEL,
    InstrumentClass.TOTAL_STATION: DEP_CAP_TOTAL_STATION,
    InstrumentClass.DRONE_PHOTO: DEP_CAP_DRONE_PHOTO,
}


def compute_deposit(instrument_class: InstrumentClass, booking_total: Decimal) -> Decimal:
    """Compute the security deposit owed for a booking.

    ``deposit`` is the larger of the class's flat base deposit and
    ``DEP_RATE_OF_TOTAL`` of ``booking_total``, then clamped to the
    class's ceiling. See the module docstring for the reasoning and the
    rounding rule.
    """
    if instrument_class not in DEP_BASE_BY_CLASS: raise DepotError("DEP-101", f"no deposit schedule defined for instrument class {instrument_class}")
    if booking_total < 0: raise DepotError("DEP-102", f"booking total must not be negative, got {booking_total}")

    base = DEP_BASE_BY_CLASS[instrument_class]
    cap = DEP_CAP_BY_CLASS[instrument_class]

    # Unrounded until the very end - see module docstring's rounding rule.
    deposit = max(base, DEP_RATE_OF_TOTAL * booking_total)
    deposit = min(deposit, cap)

    return deposit.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def deposit_refund(deposit: Decimal, damage_surcharge: Decimal) -> Decimal:
    """Compute the amount of a deposit to hand back on return.

    ``refund`` is ``deposit - damage_surcharge``, floored at
    ``Decimal("0.00")`` (the deposit itself is never negative; any
    surcharge in excess of the deposit is recovered elsewhere, not by
    this function going negative).
    """
    if deposit < 0: raise DepotError("DEP-103", f"deposit must not be negative, got {deposit}")
    if damage_surcharge < 0: raise DepotError("DEP-104", f"damage surcharge must not be negative, got {damage_surcharge}")

    refund = max(deposit - damage_surcharge, Decimal("0.00"))

    return refund.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
