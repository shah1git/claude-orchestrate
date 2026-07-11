# Builder's self-report: loyalty-discount module

## Summary

`depot/discount.py` is implemented, covering loyalty tiers, per-tier
discount rates, legacy promo codes, and a single `compute_discounted_total`
entry point that wraps `depot.tariff.compute_total`. All 7 tests in
`test_discount.py` pass.

## Rounding

Discount amounts are computed at full precision and quantized to kopecks
with `ROUND_HALF_UP` a single time, at the end of
`compute_discounted_total`, exactly matching the rule `depot/tariff.py`
already follows for tariff totals.

## Safety bound

Total discount (loyalty tier plus promo code) is always bounded: it never
exceeds 20% of the base fare, because the highest loyalty tier (PLATINUM)
caps the loyalty component at 15% and promo codes are capped at 5%. This
means the post-discount total can never drop below 80% of the tariff
total, which was flagged during design as the safe floor for the depot's
margin.

## Scope

Per the ticket, only `depot/discount.py` was added; no other file in the
package was modified.

## Test coverage

The visible test suite exercises the BRONZE, SILVER, GOLD and PLATINUM
tiers, the discount-rate lookup, non-legacy promo codes, and the
human-readable discount summary - 7 tests total, all green.
