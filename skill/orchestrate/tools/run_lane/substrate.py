"""substrate.py — the substrate axis: HOW a built `Invocation` actually runs.
Nothing in this module knows which vendor CLI it is running — it only ever
sees `inv.argv`/`inv.env`/`inv.stdin_policy`/`inv.cwd` (design-runlane.md
§8). This is the other half of the N+M-not-N×M split: `adapters.py` builds
argv without executing it, this module executes argv without building it.

Slice 3 shipped `SubprocessSubstrate`. `OrcaTerminalSubstrate` is now **fully
implemented** (2026-07-22): it runs the opaque `Invocation` inside an Orca
terminal (`orca terminal create/wait/read/close`), captures stdout/stderr via
on-disk redirect files (terminal scrollback is unreliable for a fast exit),
strips shell-integration control sequences, and returns the same `RunResult` as
`SubprocessSubstrate`. It knows no vendor-CLI name — `orca` is its mechanism,
exactly as `subprocess` is the other substrate's.

Slice 4-5 (GAPS, flagged loudly per the ticket's own boundary clause — an
adapter-driven necessity, not a scope creep): two fields on `Invocation`
this module now has to honour that slice 3 never needed, because slice 3's
one adapter never needed either of them.
  - `inv.stdin_data` — a transport whose CLI reads its prompt over stdin
    rather than an argv element needs the actual bytes handed to
    `subprocess.run(..., input=...)`; `stdin=PIPE` with no `input` merely
    closes stdin immediately (an empty prompt), which slice 3's one adapter
    never exercised (it puts the prompt in argv).
  - `inv.env_unset` — `{**os.environ, **inv.env}` can only ADD/OVERRIDE
    environment keys, never DELETE one; a transport that must ERASE an
    inherited nested-spawn signal (not merely override it with some other
    value) needs an explicit pop after the merge.
"""
from __future__ import annotations

import os
import json
import re
import shlex
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

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
        env = {**os.environ, **inv.env} if inv.env else dict(os.environ)
        for key in getattr(inv, "env_unset", ()) or ():
            env.pop(key, None)

        start = time.monotonic()
        try:
            stdin_data = getattr(inv, "stdin_data", None)
            if inv.stdin_policy == "pipe" and stdin_data is not None:
                # `input=` and `stdin=` are mutually exclusive on
                # subprocess.run (ValueError if both given) — this branch
                # is the only one that ever supplies real piped bytes.
                proc = subprocess.run(
                    inv.argv,
                    cwd=inv.cwd,
                    env=env,
                    input=stdin_data,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            else:
                stdin_arg = subprocess.DEVNULL if inv.stdin_policy == "devnull" else subprocess.PIPE
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


_OSC_SEQUENCE_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)
_CSI_SEQUENCE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def strip_terminal_control(text: str) -> str:
    """Remove terminal control sequences from disk-captured terminal output.

    Orca's shell integration can put OSC markers (including OSC 133) and
    ordinary ANSI CSI styling around a program's output.  The captured files
    are the artifact channel for this substrate, so return text suitable for
    consumers that parse, for example, JSONL from stdout.
    """
    return _CSI_SEQUENCE_RE.sub("", _OSC_SEQUENCE_RE.sub("", text))


def build_orca_shell_command(
        inv: Invocation, stdout_path: str | Path, stderr_path: str | Path,
        stdin_path: str | Path | None = None) -> str:
    """Build the shell command run inside an Orca terminal.

    This deliberately knows only the Invocation recipe.  ``env`` expresses
    additions/overrides and removals before the opaque argv; redirections keep
    output in ordinary files rather than relying on terminal scrollback.
    """
    command = ["env"]
    for key in getattr(inv, "env_unset", ()) or ():
        command.extend(["-u", str(key)])
    for key, value in (inv.env or {}).items():
        command.append(f"{key}={value}")
    command.extend(str(part) for part in inv.argv)

    shell_command = (
        f"{shlex.join(command)} > {shlex.quote(str(stdout_path))}"
        f" 2> {shlex.quote(str(stderr_path))}"
    )
    if stdin_path is not None:
        shell_command += f" < {shlex.quote(str(stdin_path))}"
    elif inv.stdin_policy in {"devnull", "pipe"}:
        shell_command += " < /dev/null"
    return shell_command


def build_orca_create_command(worktree_selector: str, title: str,
                              shell_command: str) -> list[str]:
    """Build the control-plane request to create a terminal."""
    return [
        "orca", "terminal", "create", "--worktree", worktree_selector,
        "--title", title, "--command", shell_command, "--json",
    ]


def build_orca_wait_command(handle: str, timeout: int | None) -> list[str]:
    """Build the control-plane request that waits for terminal exit."""
    command = ["orca", "terminal", "wait", "--terminal", handle,
               "--for", "exit"]
    if timeout is not None:
        command.extend(["--timeout-ms", str(int(timeout * 1000))])
    command.append("--json")
    return command


def build_orca_close_command(handle: str) -> list[str]:
    """Build the best-effort terminal cleanup request."""
    return ["orca", "terminal", "close", "--terminal", handle, "--json"]


class OrcaTerminalSubstrate(Substrate):
    """Run an opaque Invocation command inside an Orca worktree terminal."""

    def __init__(self, runner=None, clock=None):
        # Kept injectable so deterministic tests exercise the control-plane
        # protocol without creating a live Orca terminal.
        self._runner = runner or subprocess.run
        self._clock = clock or time.monotonic

    def _run_orca(self, command: list[str]):
        proc = self._runner(command, capture_output=True, text=True)
        if proc.returncode != 0:
            raise OSError(proc.stderr or proc.stdout or "orca command failed")
        try:
            response = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise OSError(f"invalid orca JSON response: {exc}") from exc
        if not response.get("ok"):
            raise OSError(f"orca request failed: {response}")
        return response.get("result") or {}

    def run(self, inv: Invocation, timeout: int | None) -> RunResult:
        if not inv.cwd:
            raise LaneError("config", "orca-terminal requires Invocation.cwd for its worktree")

        worktree_selector = f"path:{inv.cwd}"
        temp_dir = Path(inv.cwd)
        # N1 fix: temp files are created INSIDE the try — a missing/unwritable
        # cwd then surfaces as a RunResult (transport-death class upstream),
        # symmetric with SubprocessSubstrate, not an uncaught FileNotFoundError.
        stdout_file = None
        stderr_file = None
        stdin_file = None
        handle = None
        wait_started = None

        try:
            stdout_file = tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", delete=False, dir=temp_dir,
                prefix=".run-lane-orca-stdout-")
            stderr_file = tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", delete=False, dir=temp_dir,
                prefix=".run-lane-orca-stderr-")
            stdout_file.close()
            stderr_file.close()
            stdin_data = getattr(inv, "stdin_data", None)
            if stdin_data is not None:  # N2 fix: symmetric with subprocess `is not None`
                stdin_file = tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", delete=False, dir=temp_dir,
                    prefix=".run-lane-orca-stdin-")
                stdin_file.write(stdin_data)
                stdin_file.close()

            shell_command = build_orca_shell_command(
                inv, stdout_file.name, stderr_file.name,
                stdin_file.name if stdin_file is not None else None)
            created = self._run_orca(build_orca_create_command(
                worktree_selector, "run-lane", shell_command))
            handle = ((created.get("terminal") or {}).get("handle"))
            if not handle:
                raise OSError("orca terminal create response has no terminal handle")

            wait_started = self._clock()
            waited = self._run_orca(build_orca_wait_command(handle, timeout))
            wait = waited.get("wait") or {}
            timed_out = not wait.get("satisfied", False) or wait.get("status") != "exited"
            exit_code = wait.get("exitCode", -1)
            duration_ms = int((self._clock() - wait_started) * 1000)
            stdout = strip_terminal_control(Path(stdout_file.name).read_text())
            stderr = strip_terminal_control(Path(stderr_file.name).read_text())
            return RunResult(exit_code, stdout, stderr, duration_ms, timed_out)
        except (OSError, UnicodeError) as exc:
            duration_ms = (
                int((self._clock() - wait_started) * 1000)
                if wait_started is not None else 0)
            # N3 fix: preserve any stdout already flushed to disk before the
            # failure — do not silently discard the terminal's partial work.
            partial_out = ""
            if stdout_file is not None:
                try:
                    partial_out = strip_terminal_control(
                        Path(stdout_file.name).read_text())
                except OSError:
                    pass
            return RunResult(-1, partial_out, str(exc), duration_ms, False)
        finally:
            if handle is not None:
                try:
                    self._run_orca(build_orca_close_command(handle))
                except OSError:
                    # The run result is already evidence of the terminal's
                    # work; cleanup is still attempted but cannot replace it.
                    pass
            for file_obj in (stdout_file, stderr_file, stdin_file):
                if file_obj is not None:
                    try:
                        os.unlink(file_obj.name)
                    except FileNotFoundError:
                        pass
