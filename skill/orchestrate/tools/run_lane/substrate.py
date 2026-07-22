"""substrate.py — the substrate axis: HOW a built `Invocation` actually runs.
Nothing in this module knows which vendor CLI it is running — it only ever
sees `inv.argv`/`inv.env`/`inv.stdin_policy`/`inv.cwd` (design-runlane.md
§8). This is the other half of the N+M-not-N×M split: `adapters.py` builds
argv without executing it, this module executes argv without building it.

Slice 3 ships `SubprocessSubstrate`. `OrcaTerminalSubstrate` is a declared
interface + stub raising `error.class: config` — its body lands with the
`orca_orchestrate` head (ADR-0006 поправка A), not before (ADR-0006 §1.7:
architecture changes on a signal of wear, not preemptively).
"""
from __future__ import annotations

import os
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .adapters import Invocation
from .envelope import LaneError


@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool


class Substrate(ABC):
    @abstractmethod
    def run(self, inv: Invocation, timeout: int | None) -> RunResult:
        ...


class SubprocessSubstrate(Substrate):
    """Runs `inv.argv` directly via `subprocess.run` — the default substrate
    for `orchestrate`/`orchestrate-frontier` (ADR-0006 поправка A)."""

    def run(self, inv: Invocation, timeout: int | None) -> RunResult:
        stdin_arg = subprocess.DEVNULL if inv.stdin_policy == "devnull" else subprocess.PIPE
        env = {**os.environ, **inv.env} if inv.env else None

        start = time.monotonic()
        try:
            proc = subprocess.run(
                inv.argv,
                cwd=inv.cwd,
                env=env,
                stdin=stdin_arg,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            return RunResult(proc.returncode, proc.stdout, proc.stderr, duration_ms, False)
        except subprocess.TimeoutExpired as exc:
            # text=True above means subprocess already decoded these for us.
            duration_ms = int((time.monotonic() - start) * 1000)
            return RunResult(-1, exc.stdout or "", exc.stderr or "", duration_ms, True)
        except OSError as exc:
            # The binary/cwd itself could not be started (missing CLI, bad
            # workdir, …) — this IS a transport-death signal, not a Python
            # crash; __main__ classifies a nonzero/-1 exit the same way
            # regardless of which of the two branches produced it.
            duration_ms = int((time.monotonic() - start) * 1000)
            return RunResult(-1, "", str(exc), duration_ms, False)


class OrcaTerminalSubstrate(Substrate):
    """Interface declared now; body lands with the `orca_orchestrate` head
    (ADR-0006 поправка A). Until then, running through it is a loud
    `error.class: config`, never a silent no-op or accidental fallback to
    subprocess."""

    def run(self, inv: Invocation, timeout: int | None) -> RunResult:
        raise LaneError(
            "config",
            "substrate orca-terminal ещё не реализован — интерфейс "
            "объявлен, тело появится с головой orca_orchestrate (ADR-0006 "
            "поправка A, design-runlane.md §7)")
