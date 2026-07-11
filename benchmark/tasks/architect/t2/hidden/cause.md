# cause.md — architect t2 (answer key, hidden)

## FILE
`depot/tariff.py`, line 92 (inside `compute_total`'s per-day loop).

Flawed line (rc2):
```python
if current_date.weekday() > 5:
```

Reference (correct) line:
```python
if current_date.weekday() >= 5:
```

## MECHANISM
Python's `date.weekday()` numbers days Monday=0 through Sunday=6, so
Saturday is `5` and Sunday is `6` — this exact fact is spelled out in the
comment immediately above the condition ("Saturday=5, Sunday=6 in Python's
date.weekday() numbering."), which the planted edit leaves untouched even
though it now contradicts the code beneath it. The reference condition
`weekday() >= 5` is inclusive of `5`, so it matches both Saturday (`5`) and
Sunday (`6`). The planted edit tightens the comparison to strict `> 5`,
which excludes `5` and matches only `6` — i.e. only Sunday. Every Saturday
in a `day_plan` therefore skips the `coef = coef * COEF_WEEKEND_EXTRA`
multiplication on line 93 and is priced with the bare day-type coefficient
(`COEF_FIELD_DAY` or `COEF_OFFICE_DAY`) alone, as if it were an ordinary
weekday.

## CAUSAL CHAIN
1. `compute_total()` iterates `day_plan` day by day (line 88-94),
   computing `current_date = start_date + timedelta(days=offset)` for each
   entry and deriving a per-day coefficient.
2. For each day, line 92 decides whether that day is a weekend day by
   testing `current_date.weekday() > 5`. For any date whose `weekday()`
   equals `5` (a Saturday), this test evaluates to `False`.
3. Because the test is `False` for Saturdays, line 93
   (`coef = coef * COEF_WEEKEND_EXTRA`) is skipped for every Saturday in
   the plan; only the base day-type coefficient (`COEF_FIELD_DAY` = 1.00 or
   `COEF_OFFICE_DAY` = 0.60) is used for that day's `base_rate * coef` term
   added to `subtotal` on line 94.
4. Sundays (`weekday() == 6`) still satisfy `> 5`, so they continue to
   receive the weekend multiplier correctly — the bug is asymmetric and
   affects only Saturdays, which makes it easy to miss in a spot check
   that only tests a single Sunday.
5. The unrounded `subtotal` accumulated this way is quantized once at the
   end of the function (line 102) and returned as the booking's total —
   the missing multiplier is baked into the total with no further
   opportunity to correct it.
6. For any booking whose `day_plan` spans one or more Saturdays, the
   returned total is short by exactly
   `base_rate * day_type_coef * (COEF_WEEKEND_EXTRA - 1)` per affected
   Saturday, relative to the documented rule ("weekend days additionally
   get multiplied by COEF_WEEKEND_EXTRA") stated in the module docstring
   two lines above. This is exactly what `repro.py` observes: for a
   3-day, all-field-day booking starting Saturday 2026-01-03 on a
   THEODOLITE (`RATE_THEODOLITE = 1500.00`), the actual total is
   `4800.00` against a hand-expected `5100.00` — a shortfall of `300.00`,
   which is precisely `1500.00 * 1.00 * (1.20 - 1)` for the one
   mispriced Saturday.
