"""adapters.py — the transport axis: WHICH vendor CLI, and how to build its
argv. Nothing in this module ever runs a process (design-runlane.md §8: "два
независимых архитектора-лейна... сошлись" — transport and substrate are
physically separate modules on purpose, so a future edit to one cannot reach
into the other by accident).

`LaneAdapter.build_invocation` returns a self-contained `Invocation` — argv,
env, stdin policy, cwd, the artifact-channel prompt addendum, and the
witness log path, if any. `substrate.py` executes that recipe without
importing this module's concrete classes or knowing the CLI's name.

Slice 3 (ADR-0005 §"План миграции срезами") ships exactly one working
adapter, `AgyAdapter` — it has the strongest model witness (a pinned,
per-invocation log file) and covers the two busiest lanes (`gemini-flash`,
`agy-opus`). `CodexAdapter`/`GrokAdapter`/`KimiAdapter`/`ClaudePrintAdapter`
are declared stubs for slices 4-5: the ABC is closed now so future slices
only fill bodies in, but their build_invocation etc. raise NotImplementedError
until then — never a silent no-op.
"""
from __future__ import annotations

import dataclasses
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path

from .envelope import LaneError

# --- capabilities (ADR-0005 поправка B, "capabilities — ГИБРИД") -----------


@dataclass(frozen=True)
class Capabilities:
    supports_effort: str      # 'flag' | 'config-key' | 'model-suffix' | 'no'
    supports_schema: str      # 'strict' | 'prompt' | 'no'
    has_own_sandbox: str      # 'strong' | 'weak' | 'none'
    artifact_channel: str     # 'output-flag' | 'agent-writes-file' | 'stdout-capture'
    model_verification: str   # 'log' | 'stream' | 'pin-validated' | 'none'


_CAPABILITIES_FIELDS = {f.name for f in dataclasses.fields(Capabilities)}


# --- the shared data contract between the two axes --------------------------


@dataclass
class InvocationRequest:
    """What `__main__` has already resolved (CLI args + lane defaults) before
    handing control to an adapter — `effort`/`model` are ALREADY the
    resolved values (ADR-0005 поправка B: `--effort`/`--model` default to
    the lane's, but win when given)."""

    prompt_file: Path
    workdir: Path
    out: Path
    role: str | None = None
    schema: Path | None = None
    timeout: int | None = None
    resume: str | None = None
    effort: str | None = None
    model: str | None = None


@dataclass
class Invocation:
    """A fully self-contained recipe for running one lane invocation. Never
    executed by the adapter that built it — `substrate.py` is the only
    consumer of `argv`/`env`/`stdin_policy`/`cwd`."""

    argv: list
    env: dict
    stdin_policy: str          # 'devnull' | 'pipe'
    cwd: str | None
    prompt_addendum: str | None
    log_file: str | None
    prompt_length: int | None = None


@dataclass
class ModelObservation:
    """The result of sweeping a lane's witness (log/stream/pin) for the model
    that actually ran. `error` is set (never silently swallowed, ADR-0005
    §5) whenever the witness is missing, unreadable, or internally
    inconsistent — e.g. the agy flag-order trap (design-runlane.md §4)."""

    observed: str | None
    verification: str
    evidence: str | None
    error: str | None = None


class LaneAdapter(ABC):
    transport: str
    CAPS: Capabilities

    @abstractmethod
    def build_invocation(self, lane, req: InvocationRequest) -> Invocation:
        ...

    @abstractmethod
    def parse_model_witness(self, res, inv: Invocation) -> ModelObservation:
        ...

    @abstractmethod
    def parse_usage(self, res):
        ...

    @abstractmethod
    def parse_session_id(self, res):
        ...

    @abstractmethod
    def parse_printed_text(self, res) -> tuple:
        ...

    def effective_capabilities(self, lane) -> Capabilities:
        """F-Caps hybrid (ADR-0005 поправка B): CAPS is the per-transport
        constant; a lane's `capabilities:` mapping overrides individual
        fields ONLY when it actually differs from its CLI's default (e.g. a
        future `composer-build`-style lane on `grok-cli` that does not hold
        `supports_effort`, while `grok-build` on the same transport does).
        An unknown override key is a config error, not a silently-ignored
        typo.

        A stub adapter (CAPS is None — codex/grok/kimi/claude-print in slice
        3, adapters.py `_not_implemented` family) raises NotImplementedError
        here, SYMMETRICALLY with its own build_invocation: `run()` already
        wraps that in a `LaneError("config", ...)` (__main__.py), and this
        call happens BEFORE build_invocation in the pipeline (§2 layer 3) —
        without this guard `dataclasses.replace(None, **override)` throws an
        unclassified TypeError straight past that catch, on a real,
        reachable lane (codex-critic/codex-code/grok-build/kimi-k3), and
        run-lane prints nothing at all instead of its one JSON envelope."""
        if self.CAPS is None:
            raise NotImplementedError(
                f"{type(self).__name__} — {getattr(self, '_slice_note', 'not implemented')} "
                f"(adapter stub, not implemented in slice 3; ADR-0005 "
                f"§\"План миграции срезами\")")

        override = getattr(lane, "capabilities_override", None) or {}
        unknown = set(override) - _CAPABILITIES_FIELDS
        if unknown:
            raise LaneError(
                "config",
                f"lane '{getattr(lane, 'name', '?')}'.capabilities has "
                f"unknown key(s) {sorted(unknown)} — must be a subset of "
                f"{sorted(_CAPABILITIES_FIELDS)}")

        # Every Capabilities field is a closed string vocabulary ('flag',
        # 'no', 'strict', ...) — a non-string override value is never
        # legitimate. The one way this actually happens in practice: YAML
        # 1.1 parses an UNQUOTED `no`/`yes`/`on`/`off` as a bool, so
        # `capabilities: {supports_schema: no}` silently becomes
        # `{"supports_schema": False}`. `False == "no"` is False, so every
        # downstream `caps.supports_schema == "no"` check in __main__ would
        # then silently fail closed-open — exactly the "possibility
        # requested, quietly granted anyway" failure ADR-0005 exists to
        # kill. Refuse loudly instead of merging a non-string value in.
        bad_values = {k: v for k, v in override.items() if not isinstance(v, str)}
        if bad_values:
            detail = ", ".join(
                f"{k}={v!r} ({type(v).__name__})" for k, v in sorted(bad_values.items()))
            raise LaneError(
                "config",
                f"lane '{getattr(lane, 'name', '?')}'.capabilities has "
                f"non-string value(s): {detail} — a Capabilities field is "
                f"always a string; this is almost always an UNQUOTED YAML "
                f"no/yes/on/off parsed as a bool (quote it, e.g. "
                f"supports_schema: \"no\")")

        return replace(self.CAPS, **override)


# --- agy (slice 3) -----------------------------------------------------------

# Witness lines, verbatim from live `~/.gemini/antigravity-cli/log/*.log`
# captures (config.yaml:779-786; design-runlane.md §4).
_PROPAGATING_RE = re.compile(
    r'Propagating selected model override to backend:\s*label="(?P<label>[^"]*)"')
_PRINT_MODE_RE = re.compile(
    r'Print mode: starting \(promptLength=(?P<length>\d+),\s*model="(?P<model>[^"]*)"\)')

# A stable, regex-friendly marker for the "write your result to a file"
# instruction (agy's artifact_channel is agent-writes-file — it has no
# --output-file, config.yaml F5). Kept on its own line with a fixed prefix
# instead of embedded in prose so both the model and this package's own
# tests can locate the path unambiguously.
_ARTIFACT_ADDENDUM_TEMPLATE = (
    "\n\n---\n"
    "RUN-LANE-ARTIFACT-PATH: {out}\n"
    "ОБЯЗАТЕЛЬНО запиши ВЕСЬ результат этой задачи целиком в файл, указанный "
    "выше в RUN-LANE-ARTIFACT-PATH (перезапиши его, если он уже существует). "
    "Печать в ответ — это отчёт о работе, не сама работа: она усекается и не "
    "оценивается; артефакт снимается только с этого файла (config.yaml v25)."
)

# printed_text is a report-about-the-work field, not the work (config.yaml
# v25) — capped so a runaway stdout cannot balloon the envelope; genuinely
# truncated is flagged, never silently clipped.
_MAX_PRINTED_CHARS = 8000

# The flag-order trap (design-runlane.md §4): `agy -p --model X "prompt"`
# eats "--model" as the prompt (7 chars) instead of the real one. Compare the
# log's reported promptLength against what we actually submitted; a report
# far short of the real length means the wrong text reached the backend,
# regardless of the exact number the trap happens to produce.
_PROMPT_LENGTH_TRAP_MIN_RATIO = 0.5
_PROMPT_LENGTH_TRAP_FLOOR = 30


class AgyAdapter(LaneAdapter):
    """`agy-print` transport — `gemini-flash`/`agy-opus` (ADR-0005 slice 3).

    Form verified live (config.yaml:726-818): every flag BEFORE `-p`, the
    prompt text as the last argv element, stdin closed, `--add-dir <workdir>`
    mandatory (the CLI does not inherit cwd — v24 incident), and a
    per-invocation `--log-file` (not the shared `~/.gemini/.../cli.log`
    glob) so a parallel fan-out cannot race two invocations' witnesses.
    """

    transport = "agy-print"
    CAPS = Capabilities(
        supports_effort="flag",
        supports_schema="prompt",
        has_own_sandbox="weak",
        artifact_channel="agent-writes-file",
        model_verification="log",
    )

    def build_invocation(self, lane, req: InvocationRequest) -> Invocation:
        try:
            prompt_text = Path(req.prompt_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise LaneError("config", f"cannot read --prompt-file: {exc}") from exc

        workdir = str(Path(req.workdir))
        addendum = _ARTIFACT_ADDENDUM_TEMPLATE.format(out=req.out)
        full_prompt = f"{prompt_text}{addendum}"

        # Pinned per-invocation witness log, scoped by the artifact's own
        # name so a parallel fan-out of several agy lanes in the same
        # workdir cannot collide on one file (design-runlane.md §4/§11).
        log_file = Path(req.workdir) / f".run-lane-agy-{Path(req.out).name}.log"

        argv = ["agy", "--add-dir", workdir, "--log-file", str(log_file)]
        if req.timeout:
            # agy's own flag takes a Go duration string; --timeout is
            # contracted in whole seconds (ADR-0005).
            argv += ["--print-timeout", f"{req.timeout}s"]
        if req.model:
            argv += ["--model", req.model]
        if req.effort:
            argv += ["--effort", req.effort]
        # `-p` is not a boolean flag — it consumes the NEXT token as the
        # prompt (config.yaml trap (1)). Every option above it; the prompt
        # is the very last argv element.
        argv += ["-p", full_prompt]

        return Invocation(
            argv=argv,
            env={},
            stdin_policy="devnull",
            cwd=workdir,
            prompt_addendum=addendum,
            log_file=str(log_file),
            prompt_length=len(full_prompt),
        )

    def parse_model_witness(self, res, inv: Invocation) -> ModelObservation:
        log_path = Path(inv.log_file) if inv.log_file else None
        if log_path is None or not log_path.is_file():
            return ModelObservation(
                None, "unavailable", None,
                error=f"agy log file not found: {inv.log_file}")
        text = log_path.read_text(encoding="utf-8", errors="replace")

        match = _PROPAGATING_RE.search(text)
        if not match:
            return ModelObservation(
                None, "unavailable", None,
                error="agy log carries no 'Propagating selected model "
                      "override to backend' line — cannot verify the model "
                      "that actually ran")
        observed = match.group("label")
        evidence = match.group(0)

        secondary = _PRINT_MODE_RE.search(text)
        if secondary and inv.prompt_length:
            reported_length = int(secondary.group("length"))
            floor = max(_PROMPT_LENGTH_TRAP_FLOOR,
                        int(inv.prompt_length * _PROMPT_LENGTH_TRAP_MIN_RATIO))
            if reported_length < floor:
                return ModelObservation(
                    observed, "log", evidence,
                    error=(
                        f"promptLength trap: agy log reports "
                        f"promptLength={reported_length}, but the submitted "
                        f"prompt was {inv.prompt_length} chars — flag order "
                        f"likely ate the prompt (config.yaml agy trap (1))"))

        return ModelObservation(observed, "log", evidence, error=None)

    def parse_usage(self, res):
        # agy exposes no documented usage witness (design-runlane.md §4
        # table) — honestly `None`, never fabricated.
        return None

    def parse_session_id(self, res):
        # agy's --help carries no --resume/session-id surface (Приложение A)
        return None

    def parse_printed_text(self, res) -> tuple:
        text = res.stdout or ""
        if len(text) > _MAX_PRINTED_CHARS:
            return text[:_MAX_PRINTED_CHARS], True
        return text, False


# --- future-slice adapters: declared, not implemented ------------------------


def _not_implemented(self, *_args, **_kwargs):
    raise NotImplementedError(
        f"{type(self).__name__} — {self._slice_note} (adapter stub, not "
        f"implemented in slice 3; ADR-0005 §\"План миграции срезами\")")


class CodexAdapter(LaneAdapter):
    """Слайс 4 (ADR-0005): codex-cli. НЕ РЕАЛИЗОВАН — вне границ этого среза."""

    transport = "codex-cli"
    CAPS = None
    _slice_note = "срез 4 (strict-схема/usage/sessionId/--resume)"
    build_invocation = _not_implemented
    parse_model_witness = _not_implemented
    parse_usage = _not_implemented
    parse_session_id = _not_implemented
    parse_printed_text = _not_implemented


class GrokAdapter(LaneAdapter):
    """Слайс 5 (ADR-0005): grok-cli. НЕ РЕАЛИЗОВАН — вне границ этого среза."""

    transport = "grok-cli"
    CAPS = None
    _slice_note = "срез 5 (grok_hardening + fail-closed gate)"
    build_invocation = _not_implemented
    parse_model_witness = _not_implemented
    parse_usage = _not_implemented
    parse_session_id = _not_implemented
    parse_printed_text = _not_implemented


class KimiAdapter(LaneAdapter):
    """Слайс 5 (ADR-0005): kimi-cli-headless. НЕ РЕАЛИЗОВАН — вне границ этого среза."""

    transport = "kimi-cli-headless"
    CAPS = None
    _slice_note = "срез 5 (kimi_hardening + fail-closed gate)"
    build_invocation = _not_implemented
    parse_model_witness = _not_implemented
    parse_usage = _not_implemented
    parse_session_id = _not_implemented
    parse_printed_text = _not_implemented


class ClaudePrintAdapter(LaneAdapter):
    """Слайс 4 (ADR-0005): claude-print, запасной не-Claude-хост транспорт.
    НЕ РЕАЛИЗОВАН — вне границ этого среза."""

    transport = "claude-print"
    CAPS = None
    _slice_note = "срез 4 (запасной транспорт для не-Claude хоста)"
    build_invocation = _not_implemented
    parse_model_witness = _not_implemented
    parse_usage = _not_implemented
    parse_session_id = _not_implemented
    parse_printed_text = _not_implemented


ADAPTERS = {
    "agy-print": AgyAdapter,
    "codex-cli": CodexAdapter,
    "grok-cli": GrokAdapter,
    "kimi-cli-headless": KimiAdapter,
    "claude-print": ClaudePrintAdapter,
}


def get_adapter(transport: str) -> LaneAdapter:
    cls = ADAPTERS.get(transport)
    if cls is None:
        raise LaneError("config", f"unknown transport '{transport}' — no adapter registered")
    return cls()
