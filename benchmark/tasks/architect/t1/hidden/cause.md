# cause.md — architect t1 (answer key, hidden)

## FILE
`depot/booking.py`, line 81 (inside `BookingEngine.create_booking`, in the
per-instrument validation loop).

Flawed line (rc1):
```python
if instrument.state is InstrumentState.QUARANTINE_ACTIVE: raise DepotError("DEP-015", f"instrument '{instrument.serial}' is not cleared for booking (state={instrument.state.value})")
```

Reference (correct) line:
```python
if instrument.state is not InstrumentState.CLEARED: raise DepotError("DEP-015", f"instrument '{instrument.serial}' is not cleared for booking (state={instrument.state.value})")
```

## MECHANISM
The bookability gate is supposed to implement an allow-list policy — "only
`CLEARED` passes" — but the planted edit rewrites it as a deny-list that
blocks exactly one specific state, `QUARANTINE_ACTIVE`. Every other
non-`CLEARED` state (`RECEIVED`, `PENDING_CHECK`) is therefore never
matched by the condition and silently falls through to the rest of
`create_booking`, which has no other cleared-state check anywhere else in
the call chain (`registry.get`, `validate_kit`, and the
`self._active_by_serial` ownership check that follows on the next line
do not inspect `InstrumentState` at all). The `DEP-015` code and message
text are unchanged, so the error text itself gives no hint that the
condition's polarity/target changed — only a state that is genuinely
`QUARANTINE_ACTIVE` still produces `DEP-015`.

## CAUSAL CHAIN
1. `quarantine.receive_return()` (`depot/quarantine.py:40`) unconditionally
   sets a returned instrument's state to `InstrumentState.RECEIVED`,
   regardless of its state before the return — this is by design, so that
   every return restarts the inspection cycle from scratch.
2. `BookingEngine.return_booking()` (`depot/booking.py:107-117`) calls
   `quarantine.receive_return()` for every serial in the returned kit and
   never calls `begin_inspection` / `resolve_inspection` itself — those are
   separate, explicit operations performed later by inspection staff.
3. If nobody has yet called `begin_inspection` / `resolve_inspection` on
   that instrument, it remains in `InstrumentState.RECEIVED` indefinitely.
4. A second call to `create_booking()` with that same serial reaches the
   per-instrument validation loop at line 80-82. The flawed guard at
   line 81 evaluates `instrument.state is InstrumentState.QUARANTINE_ACTIVE`,
   which is `False` for a `RECEIVED` instrument, so no `DepotError` is
   raised and the loop proceeds to the next check (active-booking
   ownership), which also passes because the first booking's
   `return_booking()` already popped the serial from `_active_by_serial`.
5. `create_booking()` therefore completes normally: it computes a tariff
   total, allocates a new `booking_id`, marks the serial active again, and
   returns a `RESERVED` `Booking` — i.e. a second, live booking against an
   instrument that was never cleared, in direct violation of the policy
   documented in `InstrumentState`'s docstring ("Only a CLEARED instrument
   may be booked").
6. This is exactly what `repro.py` observes: after
   `return_booking()` the instrument is `RECEIVED`, and the subsequent
   `create_booking()` call succeeds instead of raising `DEP-015` — the
   `SYMPTOM:` line reports the new booking's id and the instrument's
   (still-`RECEIVED`) state.
