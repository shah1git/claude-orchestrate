"""substrate.py — the substrate axis: HOW a built `Invocation` actually runs.
Nothing in this module knows which vendor CLI it is running — it only ever
sees `inv.argv`/`inv.env`/`inv.stdin_policy`/`inv.cwd` (design-runlane.md
§8). This is the other half of the N+M-not-N×M split: `adapters.py` builds
argv without executing it, this module executes argv without building it.

`SubprocessSubstrate` is the sole substrate. An Orca-terminal substrate once
lived here alongside it, transporting an `Invocation` through an Orca
worktree terminal instead of a local `Popen`; it shipped and was later
withdrawn outright — the owner's decision to drop Orca from the project
(ADR-0008) — rather than kept dormant. Nothing Orca-shaped remains to opt
into; the substrate axis has one member.

Slice 4-5 (GAPS, flagged loudly per the ticket's own boundary clause — an
adapter-driven necessity, not a scope creep): two fields on `Invocation`
this module has to honour that its first adapter never needed.
  - `inv.stdin_data` — a transport whose CLI reads its prompt over stdin
    rather than an argv element needs the actual bytes handed to
    `subprocess.run(..., input=...)`; `stdin=PIPE` with no `input` merely
    closes stdin immediately (an empty prompt), which the first adapter
    never exercised (it puts the prompt in argv).
  - `inv.env_unset` — `{**os.environ, **inv.env}` can only ADD/OVERRIDE
    environment keys, never DELETE one; a transport that must ERASE an
    inherited nested-spawn signal (not merely override it with some other
    value) needs an explicit pop after the merge.
"""
from __future__ import annotations

import os
import select
import signal
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .adapters import Invocation


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
            # start_new_session=True (E2): the child becomes the leader of its
            # own session/process group, so `_terminate` can kill the whole tree
            # a vendor CLI may itself have forked — not just this one process.
            proc = subprocess.Popen(
                inv.argv, cwd=inv.cwd, env=env, bufsize=0, start_new_session=True,
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
            # honour liveness — a bare `proc.wait()` here would block forever past
            # both idle_s and max_s, defeating the ceiling ADR-0007 promises. E1
            # fix: bound the wait by the SHORTER of idle_s and whatever envelope
            # remains, not by the full remaining envelope — a post-EOF hang is pure
            # silence (the work is over, only a zombie is left), so it is idle_s
            # that must govern except when the envelope itself is the tighter
            # bound (a lane already near its max_s when EOF hit). Letting a silent
            # post-EOF process ride out the whole remaining envelope (the old
            # behaviour) tolerated tens of minutes of nothing on a lane whose
            # idle_s is far smaller than its max_s, and, if it then exited 0,
            # recorded it a SUCCESS.
            now = self._clock()
            remaining = (max(0.0, limits.max_s - (now - start))
                         if limits.max_s is not None else float("inf"))
            grace = min(limits.idle_s, remaining)
            try:
                proc.wait(timeout=grace)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                self._terminate(proc)
                timed_out = True
                envelope_bound = limits.max_s is not None and remaining <= limits.idle_s
                timeout_kind = "envelope" if envelope_bound else "idle"
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
        """SIGTERM → grace → SIGKILL → reap, on the WHOLE PROCESS GROUP (E2), not
        just the direct child: `start_new_session=True` above makes this child
        the leader of its own session/process group, so any subprocess the
        vendor CLI itself forked (a common pattern) shares that group. Killing
        only the leader let those grandchildren survive — reparented to init,
        still working (and, for a real vendor CLI, still calling out to its
        API) — long after run-lane had already reported the lane dead. A killed
        lane still leaves no zombie of its own, and whatever it already flushed
        stays in the captured buffers. `os.getpgid` resolves the leader's pid to
        its group; a lookup failure (`OSError`, which covers
        `ProcessLookupError`) means the process — and so its group — is
        already gone, and there is nothing left to reap."""
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
            proc.wait(timeout=self._KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
        except OSError:
            pass
