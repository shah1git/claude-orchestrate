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
telemetry-from-envelope path. `OrcaTerminalSubstrate` remains a declared stub
until the `orca_orchestrate` head lands (ADR-0006, вынос-по-сигналу).

Live-integration tail (design §6/§9 — one live snapshot per adapter, not
deterministic): grok/kimi model witness (currently honest `'none'`), the
`hardening.gate` sandbox-evidence path for grok/kimi, and claude-print's
artifact materialize hook still need a real CLI run to promote/wire — see the
adapters' docstrings and the run-lane design doc.
"""
