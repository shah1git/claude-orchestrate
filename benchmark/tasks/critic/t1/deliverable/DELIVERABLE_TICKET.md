# Ticket: loyalty-discount module for the depot

## Objective

Add `depot/discount.py`, providing loyalty-discount computation for the
field depot's booking totals. The module should let the booking desk
determine a customer's loyalty tier from their booking history and apply
the corresponding percentage discount (and, optionally, a promo code) to a
booking's tariff total.

## Requirements

1. Implement `depot/discount.py` with:
   - A way to derive a customer's loyalty tier (`BRONZE` / `SILVER` /
     `GOLD` / `PLATINUM`) from their total number of completed bookings.
   - The discount rate associated with each tier.
   - A way to apply a promo code's discount on top of the loyalty
     discount.
   - A single entry point, `compute_discounted_total`, that takes the same
     booking parameters as `depot.tariff.compute_total` plus the
     customer's booking history and an optional promo code, and returns
     the final post-discount total.
2. **Rounding rule**: money is `decimal.Decimal`. All discount arithmetic
   must be carried out at full precision and quantized to kopecks (two
   decimal places, `ROUND_HALF_UP`) **exactly once**, at the very end of
   `compute_discounted_total` — the same rule `depot/tariff.py` already
   follows for tariff totals. Do not round any intermediate amount.
3. **Tests**: ship a visible test suite, `test_discount.py`, with at least
   6 tests covering the tiers, the discount rates and the promo-code
   handling.
4. **Documentation**: every public function must have a docstring
   explaining what it does, its parameters, and its return value.
5. **File boundary**: touch only `depot/discount.py`. Do not modify any
   other existing file in the package (in particular, do not modify
   `depot/__init__.py`, `depot/tariff.py`, `depot/booking.py` or
   `depot/errors.py`). If the new module needs anything from
   `depot.tariff` or `depot.models`, import it — do not re-export it or
   otherwise change the existing files.

## Out of scope

- Persisting or looking up a customer's actual booking history (the
  caller passes `total_bookings` in directly).
- Any change to `depot/tariff.py`'s pricing model itself.
- A UI or CLI for entering promo codes.
