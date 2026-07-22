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

Slice 3 (ADR-0005 §"План миграции срезами") ships exactly one concrete
adapter, `AgyAdapter`, and one concrete substrate, `SubprocessSubstrate`.
`OrcaTerminalSubstrate` and the codex/grok/kimi/claude-print adapters are
declared interfaces/stubs for later slices — see their docstrings.
"""
