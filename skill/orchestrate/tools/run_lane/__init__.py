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
(`~/.grok/sandbox-events.jsonl`); claude-print materialize hook landed.

Live end-to-end has since been exercised (2026-07-22): the agy, codex, and grok
lanes each ran real builder tickets through run-lane, and `OrcaTerminalSubstrate`
completed a live Orca round-trip (gemini-flash through an Orca terminal, ok:true,
model witness matched). Still unverified live: the kimi lane (its `k3` model needs
a `[models."k3"]` entry in the machine's kimi config.toml) and claude-print. The
parsing/argv/strip logic stays unit-tested against fixtures; the live round-trips
are integration checks, now run at least once for those three adapters + Orca.
"""
