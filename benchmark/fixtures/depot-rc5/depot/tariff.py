"""Tariff computation for the field depot.

Pricing model
-------------
Every day of a booking is either a "field day" (полевой день - the
instrument is out on site) or an "office day" (камеральный день - the
instrument is idle while data gets processed indoors). Each day's cost is
``base_rate * day_coefficient``, and weekend days additionally get
multiplied by ``COEF_WEEKEND_EXTRA`` (equipment sitting idle over a weekend
still costs the depot in insurance and storage, so we do not discount it).

Bookings longer than ``CAP_LONG_BOOKING_THRESHOLD_DAYS`` are capped: the
computed subtotal is clamped to a flat per-instrument ceiling
(``CAP_TOTAL_*``) so that long-term rentals do not grow unbounded.

Rounding rule
-------------
Money is represented as ``decimal.Decimal``. All day-by-day arithmetic is
carried out at full precision and is summed *unrounded*; the result is
quantized to kopecks (two decimal places, ROUND_HALF_UP) exactly once, at
the very end of ``compute_total`` - i.e. at the level of one instrument's
booking total. This avoids the classic bug of accumulating many small
per-day rounding errors into a total that is off by a few kopecks.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .errors import DepotError
from .models import DayType, InstrumentClass, is_weekend_surcharge_day

# --- Base daily rates, per instrument class (RUB) -------------------------
RATE_THEODOLITE = Decimal("1500.00")
RATE_GNSS_ROVER = Decimal("3500.00")
RATE_LEVEL = Decimal("900.00")
RATE_TOTAL_STATION = Decimal("2800.00")
RATE_DRONE_PHOTO = Decimal("4200.00")

# --- Day-type and weekend coefficients ------------------------------------
COEF_FIELD_DAY = Decimal("1.00")
COEF_OFFICE_DAY = Decimal("0.60")
COEF_WEEKEND_EXTRA = Decimal("1.20")

# --- Long-booking cap: threshold (days) and per-class total ceiling (RUB) -
CAP_LONG_BOOKING_THRESHOLD_DAYS = 10
CAP_TOTAL_THEODOLITE = Decimal("12000.00")
CAP_TOTAL_GNSS_ROVER = Decimal("28000.00")
CAP_TOTAL_LEVEL = Decimal("7000.00")
CAP_TOTAL_TOTAL_STATION = Decimal("22000.00")
CAP_TOTAL_DRONE_PHOTO = Decimal("33000.00")

# Maximum bookable horizon in days; not itself a money constant, so it does
# not carry the RATE_/COEF_/CAP_ prefix reserved for tariff quantities.
MAX_BOOKING_HORIZON_DAYS = 90

RATE_BY_CLASS: dict[InstrumentClass, Decimal] = {
    InstrumentClass.THEODOLITE: RATE_THEODOLITE,
    InstrumentClass.GNSS_ROVER: RATE_GNSS_ROVER,
    InstrumentClass.LEVEL: RATE_LEVEL,
    InstrumentClass.TOTAL_STATION: RATE_TOTAL_STATION,
    InstrumentClass.DRONE_PHOTO: RATE_DRONE_PHOTO,
}

CAP_TOTAL_BY_CLASS: dict[InstrumentClass, Decimal] = {
    InstrumentClass.THEODOLITE: CAP_TOTAL_THEODOLITE,
    InstrumentClass.GNSS_ROVER: CAP_TOTAL_GNSS_ROVER,
    InstrumentClass.LEVEL: CAP_TOTAL_LEVEL,
    InstrumentClass.TOTAL_STATION: CAP_TOTAL_TOTAL_STATION,
    InstrumentClass.DRONE_PHOTO: CAP_TOTAL_DRONE_PHOTO,
}


def compute_total(instrument_class: InstrumentClass, start_date: date, day_plan: tuple[DayType, ...]) -> Decimal:
    """Compute the total rental cost for one instrument's booking.

    ``day_plan`` must contain exactly one ``DayType`` per day of the
    booking, in order, starting at ``start_date``. See the module
    docstring for the pricing and rounding rules.
    """
    if not day_plan: raise DepotError("DEP-005", "day plan must contain at least one day")
    if instrument_class not in RATE_BY_CLASS: raise DepotError("DEP-006", f"no tariff rate defined for instrument class {instrument_class}")
    if len(day_plan) > MAX_BOOKING_HORIZON_DAYS: raise DepotError("DEP-007", f"day plan of {len(day_plan)} days exceeds the maximum bookable horizon of {MAX_BOOKING_HORIZON_DAYS} days")

    base_rate = RATE_BY_CLASS[instrument_class]
    subtotal = Decimal("0")
    for offset, day_type in enumerate(day_plan):
        current_date = start_date + timedelta(days=offset)
        coef = COEF_FIELD_DAY if day_type is DayType.FIELD else COEF_FIELD_DAY
        if is_weekend_surcharge_day(current_date):
            coef = coef * COEF_WEEKEND_EXTRA
        subtotal += base_rate * coef

    # Long-booking cap: clamp the (still unrounded) subtotal to the flat
    # per-instrument ceiling once the booking crosses the threshold.
    if len(day_plan) > CAP_LONG_BOOKING_THRESHOLD_DAYS:
        subtotal = min(subtotal, CAP_TOTAL_BY_CLASS[instrument_class])

    # Round exactly once, here, at the booking total - see module docstring.
    return subtotal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
