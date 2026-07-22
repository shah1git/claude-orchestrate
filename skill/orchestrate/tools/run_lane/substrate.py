"""substrate.py — the substrate axis: HOW a built `Invocation` actually runs.
Nothing in this module knows which vendor CLI it is running — it only ever
sees `inv.argv`/`inv.env`/`inv.stdin_policy`/`inv.cwd` (design-runlane.md
§8). This is the other half of the N+M-not-N×M split: `adapters.py` builds
argv without executing it, this module executes argv without building it.

Slice 3 shipped `SubprocessSubstrate`. `OrcaTerminalSubstrate` is now **fully
implemented** (2026-07-22): it runs the opaque `Invocation` inside an Orca
terminal (`orca terminal create` … `close`), captures stdout/stderr via on-disk
redirect files (terminal scrollback is unreliable for a fast exit), waits for
completion by polling an on-disk exit-code sentinel the shell command writes
last (`orca terminal wait --for exit` proved unreliable live — see run()),
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
import select
import shlex
import subprocess
import tempfile
import threading
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
    # Which limit fired, when timed_out is True (ADR-0007): 'idle' — the process
    # went silent past the liveness threshold (dead/hung transport); 'envelope' —
    # it kept producing output but exceeded the lane's recorded latency ceiling
    # (streaming-but-looping). None when the run did not time out. `__main__`
    # maps these onto the midflight death-evidence dictionary.
    timeout_kind: str | None = None


@dataclass(frozen=True)
class RunLimits:
    """The two orthogonal bounds on how long the executor waits for a lane
    (ADR-0007, doctrine §2.9). They separate two things a single wall-clock
    timeout conflated:

    - `idle_s`  — the liveness bound: the maximum SILENCE tolerated (no output
      byte / no output-file growth). A process still producing output is alive
      and is never killed for taking long, no matter the total duration. This is
      an infrastructure default — a detector of a dead/hung transport.
    - `max_s`   — the lane's latency envelope: an absolute ceiling on total
      duration, recorded per lane FROM THE MODEL'S REAL LATENCY as a deliberate
      requirement (not an arbitrary default). `None` means no ceiling — idle
      alone governs. It catches a transport that streams forever without ever
      going idle.

    Immutable value-object so the pair travels as one domain quantity and can
    grow (grace period, per-stream thresholds) without a new run() signature."""

    idle_s: float
    max_s: float | None = None


def classify_wait(now: float, last_output_at: float, started_at: float,
                  limits: RunLimits) -> str:
    """Pure liveness policy (ADR-0007): decide whether a still-running process
    should keep waiting or be killed. Returns 'continue', 'idle', or 'envelope'.

    Premise this closes (доктрина §2.10): a wall-clock timeout killed a lane for
    being slow, conflating "hung" with "thinking". Here a process whose output is
    still advancing (`last_output_at` recent) is alive and is NEVER killed on
    total duration alone — only the envelope ceiling can stop a live stream, and
    only silence past `idle_s` marks a genuine hang. `now`/`last_output_at`/
    `started_at` are injected (monotonic seconds) so the decision is deterministic
    and testable without a real process."""
    if limits.max_s is not None and (now - started_at) >= limits.max_s:
        return "envelope"
    if (now - last_output_at) >= limits.idle_s:
        return "idle"
    return "continue"


class Substrate(ABC):
    @abstractmethod
    def run(self, inv: Invocation, limits: RunLimits) -> RunResult:
        ...


class SubprocessSubstrate(Substrate):
    """Runs `inv.argv` via `Popen`, waiting on LIVENESS rather than wall-clock
    (ADR-0007): a process still producing output is never killed for taking
    long; only silence past `limits.idle_s`, or total duration past
    `limits.max_s`, ends it. The default substrate for
    orchestrate/orchestrate-frontier (ADR-0006 поправка A)."""

    _POLL_INTERVAL_S = 0.25
    _KILL_GRACE_S = 5.0

    def __init__(self, clock=None):
        # Injectable monotonic clock for the liveness bookkeeping. `select()`'s
        # own timeout still uses real time, so tests exercise real (short)
        # durations rather than a simulated wall clock.
        self._clock = clock or time.monotonic

    def run(self, inv: Invocation, limits: RunLimits) -> RunResult:
        env = {**os.environ, **inv.env} if inv.env else dict(os.environ)
        for key in getattr(inv, "env_unset", ()) or ():
            env.pop(key, None)

        stdin_data = getattr(inv, "stdin_data", None)
        stdin_arg = subprocess.DEVNULL if inv.stdin_policy == "devnull" else subprocess.PIPE

        start = self._clock()
        try:
            # bufsize=0: raw unbuffered pipes so os.read on the fd sees every
            # byte (no BufferedReader layer holding some back from select).
            proc = subprocess.Popen(
                inv.argv, cwd=inv.cwd, env=env, bufsize=0,
                stdin=stdin_arg, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            # The binary/cwd could not be started (missing CLI, bad workdir) —
            # a transport-death signal, not a Python crash. __main__ classifies
            # a -1 exit the same way regardless.
            duration_ms = int((self._clock() - start) * 1000)
            return RunResult(-1, "", str(exc), duration_ms, False)

        # Feed piped stdin from a WRITER THREAD so the read loop never blocks on
        # a large prompt exceeding the pipe buffer — the classic single-threaded
        # write-all-then-read deadlock (a transport whose prompt rides stdin).
        writer = None
        if stdin_arg is subprocess.PIPE:
            payload = (stdin_data or "").encode("utf-8")

            def _feed():
                try:
                    proc.stdin.write(payload)
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass

            writer = threading.Thread(target=_feed, daemon=True)
            writer.start()

        out_fd, err_fd = proc.stdout.fileno(), proc.stderr.fileno()
        buffers = {out_fd: b"", err_fd: b""}
        streams = [proc.stdout, proc.stderr]
        last_output = start
        timed_out = False
        timeout_kind = None

        while streams:
            readable, _, _ = select.select(streams, [], [], self._POLL_INTERVAL_S)
            now = self._clock()
            for stream in readable:
                chunk = os.read(stream.fileno(), 65536)
                if chunk:
                    buffers[stream.fileno()] += chunk
                    last_output = now
                else:
                    streams.remove(stream)   # EOF on this pipe
            if streams:
                # Liveness check every tick — even when data arrived — so a
                # transport that streams forever still hits the envelope ceiling.
                verdict = classify_wait(now, last_output, start, limits)
                if verdict != "continue":
                    timed_out = True
                    timeout_kind = verdict
                    self._terminate(proc)
                    break

        if timed_out:
            exit_code = -1
        else:
            # Both pipes are at EOF. A well-behaved process has exited or is about
            # to; a pathological one (closed its fds but still running) must STILL
            # honour the envelope — a bare `proc.wait()` here would block forever
            # past both idle_s and max_s, defeating the ceiling ADR-0007 promises.
            # Bound the final wait by the remaining envelope (or an idle-length
            # grace when there is no ceiling); overrun is a real timeout.
            now = self._clock()
            grace = (max(0.0, limits.max_s - (now - start))
                     if limits.max_s is not None else limits.idle_s)
            try:
                proc.wait(timeout=grace)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                self._terminate(proc)
                timed_out = True
                timeout_kind = "envelope" if limits.max_s is not None else "idle"
                exit_code = -1

        if writer is not None:
            writer.join(timeout=0.1)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass

        duration_ms = int((self._clock() - start) * 1000)
        stdout = buffers[out_fd].decode("utf-8", errors="replace")
        stderr = buffers[err_fd].decode("utf-8", errors="replace")
        return RunResult(exit_code, stdout, stderr, duration_ms, timed_out, timeout_kind)

    def _terminate(self, proc):
        """SIGTERM → grace → SIGKILL → reap: a killed lane leaves no zombie, and
        whatever it already flushed stays in the captured buffers."""
        try:
            proc.terminate()
            proc.wait(timeout=self._KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        except OSError:
            pass


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
        rc_path: str | Path, stdin_path: str | Path | None = None) -> str:
    """Build the shell command run inside an Orca terminal.

    This deliberately knows only the Invocation recipe.  ``env`` expresses
    additions/overrides and removals before the opaque argv; redirections keep
    output in ordinary files rather than relying on terminal scrollback.

    LIVE FIX (2026-07-22, e2e): the Orca terminal runs an *interactive* shell
    that does NOT reliably transition to an "exited" state that
    `terminal wait --for exit` detects — verified live, it times out even for a
    fast `sleep 4; exit 0`. So the command's exit code is written to an on-disk
    **sentinel file** (`rc_path`) which the substrate polls, exactly the
    artifact-from-disk discipline applied to the exit code. `wait` is no longer
    trusted for the code. Deterministic fakes could not surface this — only a
    live Orca round-trip did.
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
    # Write the exit code to the sentinel LAST — its presence is the "command
    # finished" signal the substrate polls for. Space around `;` keeps it a
    # separate shell token (valid either way; also keeps shlex-based tests clean).
    shell_command += f' ; echo "$?" > {shlex.quote(str(rc_path))}'
    return shell_command


def build_orca_create_command(worktree_selector: str, title: str,
                              shell_command: str) -> list[str]:
    """Build the control-plane request to create a terminal."""
    return [
        "orca", "terminal", "create", "--worktree", worktree_selector,
        "--title", title, "--command", shell_command, "--json",
    ]


def build_orca_close_command(handle: str) -> list[str]:
    """Build the best-effort terminal cleanup request."""
    return ["orca", "terminal", "close", "--terminal", handle, "--json"]


class OrcaTerminalSubstrate(Substrate):
    """Run an opaque Invocation command inside an Orca worktree terminal."""

    def __init__(self, runner=None, clock=None, sleep=None):
        # Kept injectable so deterministic tests exercise the control-plane
        # protocol without creating a live Orca terminal. `sleep` paces the
        # exit-code sentinel poll (see run()).
        self._runner = runner or subprocess.run
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep

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

    def run(self, inv: Invocation, limits: RunLimits) -> RunResult:
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
        rc_file = None
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

            rc_file = tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", delete=False, dir=temp_dir,
                prefix=".run-lane-orca-rc-")
            rc_file.close()

            shell_command = build_orca_shell_command(
                inv, stdout_file.name, stderr_file.name, rc_file.name,
                stdin_file.name if stdin_file is not None else None)
            created = self._run_orca(build_orca_create_command(
                worktree_selector, "run-lane", shell_command))
            handle = ((created.get("terminal") or {}).get("handle"))
            if not handle:
                raise OSError("orca terminal create response has no terminal handle")

            # Wait on LIVENESS, not a wall clock (ADR-0007). Two disk signals:
            #   * the exit-code sentinel (rc file) — written LAST by the shell
            #     command, so a complete (newline-terminated) rc means "finished";
            #     the trailing `echo "$?"` newline is the completion marker, so a
            #     read racing the write can never parse a torn partial integer.
            #   * growth of the stdout/stderr files — the "still working" signal
            #     while no rc is present. Silence (no growth) past `idle_s` = a
            #     hung terminal; total duration past `max_s` = the lane's envelope
            #     exceeded. `orca terminal wait` is NOT used (verified live: it
            #     times out even for a command that has already exited).
            # For a burst-only transport (one that writes the output file only on
            # final flush) the file does not grow mid-work, so liveness there is
            # governed by the lane's recorded `idle_s` envelope — the same
            # deliberate requirement the subprocess pipe needs, not a defect.
            wait_started = self._clock()
            last_growth = wait_started
            last_size = 0
            exit_code = -1
            timed_out = False
            timeout_kind = None
            while True:
                now = self._clock()
                try:
                    rc_text = Path(rc_file.name).read_text()
                except OSError:
                    rc_text = ""
                if rc_text.endswith("\n") and rc_text.strip():
                    try:
                        exit_code = int(rc_text.strip().splitlines()[-1].strip())
                    except ValueError:
                        # rc is written only by `echo "$?"` → always digits; a
                        # complete-but-non-numeric line is anomalous. Keep waiting
                        # (degrades to a clean idle/envelope timeout) rather than
                        # crash or invent an exit code.
                        pass
                    else:
                        break
                try:
                    size = (Path(stdout_file.name).stat().st_size
                            + Path(stderr_file.name).stat().st_size)
                except OSError:
                    size = last_size
                if size > last_size:
                    last_size = size
                    last_growth = now
                verdict = classify_wait(now, last_growth, wait_started, limits)
                if verdict != "continue":
                    timed_out = True
                    timeout_kind = verdict
                    break
                self._sleep(0.5)
            duration_ms = int((self._clock() - wait_started) * 1000)
            stdout = strip_terminal_control(Path(stdout_file.name).read_text())
            stderr = strip_terminal_control(Path(stderr_file.name).read_text())
            return RunResult(exit_code, stdout, stderr, duration_ms, timed_out, timeout_kind)
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
            for file_obj in (stdout_file, stderr_file, stdin_file, rc_file):
                if file_obj is not None:
                    try:
                        os.unlink(file_obj.name)
                    except FileNotFoundError:
                        pass
