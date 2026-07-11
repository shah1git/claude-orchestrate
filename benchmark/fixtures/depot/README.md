# depot

`depot` is a small, fully synthetic Python package modelling the rental
desk of a **field depot of surveying equipment**
("полевой склад геодезического оборудования"). It is a benchmark fixture:
the domain is invented so that no tutorial or public library resembles it,
and later tasks run mechanical inventories, feature additions, and
refactorings against this codebase.

## Domain

The depot rents out five classes of surveying instruments, each tracked as
an individually serial-numbered `Instrument`:

- `THEODOLITE` - optical angle-measuring instrument.
- `GNSS_ROVER` - satellite-positioning rover unit.
- `LEVEL` - optical/digital leveling instrument.
- `TOTAL_STATION` - combined angle + distance measuring station.
- `DRONE_PHOTO` - photogrammetry drone.

### Tariffs

A booking is a contiguous span of calendar days. Each day is classified as
a **field day** ("полевой день" - the instrument is out on site) or an
**office day** ("камеральный день" - the instrument is idle while data is
processed indoors); office days are billed at a lower coefficient. Weekend
days carry an extra multiplier on top of the day-type coefficient. Long
bookings (beyond a configurable day threshold) are capped at a flat
per-instrument ceiling instead of growing linearly forever. All money is
`decimal.Decimal`, rounded to kopecks exactly once, at the level of a
single instrument's booking total - see the rounding-rule comment at the
top of `depot/tariff.py` for the reasoning.

### Quarantine

Every returned instrument must pass through an inspection cycle before it
can be booked again:

```
RECEIVED -> PENDING_CHECK -> CLEARED                        (inspection passed)
RECEIVED -> PENDING_CHECK -> QUARANTINE_ACTIVE -> CLEARED    (inspection failed)
```

The number of days an instrument must spend in `QUARANTINE_ACTIVE` before
release is configurable per instrument class. An instrument that is not
`CLEARED` cannot be booked.

### Kit incompatibility

Some instrument classes must never be booked together in one kit - for
example a `DRONE_PHOTO` and a `GNSS_ROVER` interfere over radio in the
field, and the depot only issues one primary angle instrument
(`TOTAL_STATION` or `THEODOLITE`) per crew. `depot/compat.py` holds this
incompatibility matrix and validates every kit against it.

### Ledger and reports

Every state-changing operation (booking created / issued / returned /
cancelled, quarantine transitions) appends an immutable record to an
append-only `Ledger`. Reports (`depot/reports.py`) are computed strictly
from ledger records, never from live objects, so a report always reflects
exactly what happened, in order.

## Module layout

```
depot/
  __init__.py    - package-level re-exports
  errors.py      - the single DepotError exception (code + message)
  models.py      - dataclasses/enums: Instrument, Kit, Booking, states
  registry.py    - instrument registry, serial-number validation
  tariff.py      - tariff computation
  quarantine.py  - quarantine state machine
  compat.py      - kit incompatibility matrix + validation
  booking.py     - booking engine tying tariff+quarantine+compat together
  ledger.py      - append-only operations ledger
  reports.py     - summaries computed from the ledger
tests/           - pytest suite covering the happy paths of every module
```

## Errors

Every business-rule violation raises the single `depot.DepotError`
exception, carrying a stable, literal error code of the form `"DEP-###"`
(e.g. `DepotError("DEP-015", "instrument not cleared")`). Codes are unique
per rule and are the intended way to match on a specific failure; the
message text may be reworded freely.

## Running the tests

Stdlib only; tests use `pytest` (already installed in this environment).
From this directory:

```
python3 -m pytest
```
