# cause.md — architect t3 (answer key, hidden)

## FILE
`depot/booking.py`, inside `BookingEngine.create_booking`, the per-instrument
validation loop and its immediate surroundings (roughly lines 80-92 in the
rc3 copy — the loop that used to be validation-only, plus the removal of
the original end-of-method reservation loop that used to sit right before
`return booking`).

Flawed region (rc3):
```python
        booking_id = f"BK-{self._next_booking_seq:05d}"
        for instrument in instruments:
            if instrument.state is not InstrumentState.CLEARED: raise DepotError("DEP-015", f"instrument '{instrument.serial}' is not cleared for booking (state={instrument.state.value})")
            if instrument.serial in self._active_by_serial: raise DepotError("DEP-020", f"instrument '{instrument.serial}' already has an active booking")
            self._active_by_serial[instrument.serial] = booking_id
```
(and, later in the method, the original `for serial in serials:
self._active_by_serial[serial] = booking_id` block right before
`self._ledger.append("booking_created", ...)` / `return booking` has been
deleted — reservation is not performed there any more.)

Reference (correct) region:
```python
        for instrument in instruments:
            if instrument.state is not InstrumentState.CLEARED: raise DepotError("DEP-015", f"instrument '{instrument.serial}' is not cleared for booking (state={instrument.state.value})")
            if instrument.serial in self._active_by_serial: raise DepotError("DEP-020", f"instrument '{instrument.serial}' already has an active booking")

        # ... tariff computation ...

        booking_id = f"BK-{self._next_booking_seq:05d}"
        self._next_booking_seq += 1
        kit = Kit(kit_id=kit_id, serials=tuple(serials))
        booking = Booking(...)

        for serial in serials:
            self._active_by_serial[serial] = booking_id
        self._ledger.append("booking_created", booking_id, {...})
        return booking
```

## MECHANISM
The reference implementation is atomic with respect to `_active_by_serial`:
it runs the *entire* per-instrument validation loop to completion (every
instrument's CLEARED-state check and every instrument's active-booking
check) before it ever writes to `_active_by_serial`, in one final loop that
only executes once validation for the whole kit has already succeeded — a
failed validation therefore reserves nothing, for any instrument, on any
kit. The planted edit collapses this two-phase "validate-all, then
reserve-all" structure into a single pass: it moves the `booking_id`
computation ahead of the loop and inserts
`self._active_by_serial[instrument.serial] = booking_id` *inside* the loop
body, executed immediately after each individual instrument clears its own
two checks. Because Python's `raise` inside a `for` loop propagates
immediately and unwinds the stack with no `try`/`except`/`finally` around
the loop to undo prior iterations, any instrument that was processed
*before* the one that fails has already had its serial written into
`self._active_by_serial` under `booking_id` — a booking id that is never
actually attached to a `Booking` object, never appended to the ledger, and
never returned to the caller, because the `DepotError` raised by a later
instrument in the same loop propagates straight out of `create_booking`
before any of that downstream construction runs. The dictionary entry is
never rolled back on the exceptional path, so it becomes a permanent,
orphaned reservation: `_active_by_serial[serial]` now points at a
`booking_id` string with no corresponding booking anywhere, and the only
two operations that ever remove an entry from `_active_by_serial` —
`return_booking` and `cancel_booking` — both require an actual `Booking`
object that was never created, so nothing can ever clear it.

## CAUSAL CHAIN
1. A kit booking is attempted with an ordered list of instruments where an
   earlier instrument (A) is genuinely `CLEARED` and a later instrument (B)
   is not (e.g. `RECEIVED`, because it was returned via `return_booking` /
   `quarantine.receive_return` but never sent through
   `begin_inspection` / `resolve_inspection`).
2. `create_booking` computes `booking_id` up front, then enters the
   per-instrument loop. For A (processed first), the `CLEARED` check
   passes, the `_active_by_serial` ownership check passes (A is free), and
   the flawed line immediately executes
   `self._active_by_serial["A-serial"] = booking_id` — A is now marked
   "reserved" under `booking_id`, even though the kit as a whole has not
   been validated yet.
3. The loop proceeds to B. B's `CLEARED` check fails
   (`instrument.state is not InstrumentState.CLEARED`), so
   `DepotError("DEP-015", ...)` is raised right there, inside the loop.
4. This exception propagates immediately out of `create_booking`. Nothing
   after the loop ever runs: `total` is never computed, `self._next_booking_seq`
   is never incremented, no `Kit` or `Booking` object is ever constructed,
   and `self._ledger.append("booking_created", ...)` is never called. From
   the caller's point of view, and from the ledger's point of view, this
   booking attempt never happened — except for the entry the loop already
   wrote into `_active_by_serial` for A in step 2, which has no `try`/
   `except` around it to undo on the way out.
5. `_active_by_serial["A-serial"]` now permanently holds `booking_id`
   (e.g. `"BK-00002"`), a string that is not the id of any `Booking` object
   that exists anywhere in the system — `booking_id` was consumed by the
   failed attempt but the sequence counter (`self._next_booking_seq`) was
   never advanced past it either, since that increment also lives after
   the loop.
6. A's registry state is unaffected — it is still `InstrumentState.CLEARED`
   — because nothing in this loop touches `Instrument.state`; only
   `_active_by_serial` was mutated.
7. A subsequent, independent `create_booking` call for A alone reaches the
   same per-instrument loop. A's `CLEARED` check passes as before, but the
   very next line, `if instrument.serial in self._active_by_serial: raise
   DepotError("DEP-020", ...)`, now finds A's serial already present in the
   dictionary (from step 2) and raises `DEP-020` — "already has an active
   booking" — even though `get_booking`-style state (a real `Booking`
   object, a ledger entry, an issued/returned lifecycle) for A does not
   exist anywhere in the system.
8. This is exactly what `repro.py` observes: after the `[A, B]` kit booking
   is correctly rejected with `DEP-015` (because of B), A is still
   `CLEARED` in the registry, yet the immediately following, independent
   attempt to book A alone fails with `DEP-020` instead of succeeding — a
   phantom reservation leaked by the non-atomic, unrolled-back write to
   `_active_by_serial` inside the validation loop, with no route left to
   ever free it (`return_booking`/`cancel_booking` both require a `Booking`
   object that was never created for this `booking_id`).
