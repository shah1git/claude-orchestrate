"""run_lane — the thin executor package behind tools/run-lane (ADR-0005,
поправка B; design-runlane.md).

Two axes are kept physically apart, in their own modules, on purpose (the
ticket this package answers exists BECAUSE letting them blur once already
produced silent, plausible-looking failures — ADR-0005 «Контекст»):

  - transport (WHICH vendor CLI)  -> adapters.py  (`LaneAdapter.build_invocation`
    returns a self-contained `Invocation`; it never runs anything)
  - substrate (HOW that argv runs) -> substrate.py (`Substrate.run` executes an
    `Invocation`; it never knows which CLI it just ran)

`config_resolve.py` turns `--lane` + `availability.aliases` into a
`LaneRecord`; `artifact.py` snapshots the declared `--out` file strictly from
disk (never from a model's own account of it); `envelope.py` assembles the
ADR-0005 JSON envelope and owns the `ok` formula and the `error.class`
taxonomy. `__main__.py` is the only module that wires all of the above
together in order.

Slices 3–6 (ADR-0005 §"План миграции срезами") are landed: all five adapters
(`AgyAdapter`, `CodexAdapter`, `GrokAdapter`, `KimiAdapter`,
`ClaudePrintAdapter`) and `SubprocessSubstrate` are concrete, plus the
telemetry-from-envelope path. `OrcaTerminalSubstrate` is now fully implemented (2026-07-22): it runs the
Invocation inside an Orca terminal and returns the same RunResult.

Live snapshots taken 2026-07-22 closed the witness/wiring tail deterministically:
grok model witness promoted to `'stream'` (from the `modelUsage` key), kimi
confirmed `'none'` (no model in stream-json), codex `'pin-validated'` (no model
in `--json`); `hardening.gate` wired with grok's evidence path
(`~/.grok/sandbox-events.jsonl`); claude-print materialize hook landed. What
remains is **live end-to-end only** (not deterministic): one real lane run
through run-lane against a live model per adapter, and `OrcaTerminalSubstrate`
against a live Orca runtime — the parsing/argv/strip logic is unit-tested against
fixtures, but the real CLI/Orca round-trip is an integration check.
"""
