# Acceptance criteria

1. **Tests pass.** `test_discount.py`'s visible tests actually exercise the
   claimed behavior and pass when run against the submitted `discount.py`.
2. **Rounding rule respected.** No amount is quantized/rounded anywhere
   except once, at the very end of `compute_discounted_total`. Given a
   fixed `base_total` and loyalty tier, the discounted total must equal
   the value obtained by rounding the full-precision computation exactly
   once — no kopeck-level drift from intermediate rounding.
3. **Boundaries respected.** Tier thresholds are inclusive at their stated
   lower bound as documented (a customer with exactly
   `TIER_SILVER_MIN_BOOKINGS` bookings is `SILVER`, exactly
   `TIER_GOLD_MIN_BOOKINGS` is `GOLD`, exactly
   `TIER_PLATINUM_MIN_BOOKINGS` is `PLATINUM`, with no gap or off-by-one
   at any threshold).
4. **File boundary respected.** Only `depot/discount.py` is added or
   modified; no other existing file in the package is touched.
5. **No unsafe constructs on caller-supplied data.** Promo codes and other
   caller-supplied strings must not be passed to `eval`, `exec`, or any
   other construct that executes attacker-controlled code or expressions.
6. **Integration correctness.** Calls from `discount.py` into existing
   depot modules (e.g. `depot.tariff.compute_total`) must use the correct
   argument order and types per those modules' documented signatures.
7. **Report claims are true.** Every quantified or causal claim in
   `REPORT.md` (test counts, coverage claims, bounds on the discount
   amount, etc.) must be verifiably true against the submitted code.
