"""adapters.py — the transport axis: WHICH vendor CLI, and how to build its
argv. Nothing in this module ever runs a process (design-runlane.md §8: "два
независимых архитектора-лейна... сошлись" — transport and substrate are
physically separate modules on purpose, so a future edit to one cannot reach
into the other by accident).

`LaneAdapter.build_invocation` returns a self-contained `Invocation` — argv,
env, stdin policy, cwd, the artifact-channel prompt addendum, and the
witness log path, if any. `substrate.py` executes that recipe without
importing this module's concrete classes or knowing the CLI's name.

Slice 3 (ADR-0005 §"План миграции срезами") shipped `AgyAdapter` first — it
has the strongest model witness (a pinned, per-invocation log file) and
covers the two busiest lanes (`gemini-flash`, `agy-opus`). Slices 4-5 (this
module's current state) fill in the four remaining adapters —
`CodexAdapter`, `ClaudePrintAdapter`, `GrokAdapter`, `KimiAdapter` — against
the SAME `LaneAdapter` ABC and the same rule: never a silent no-op, never a
guessed witness (design-runlane.md §9's open questions on grok/kimi model
verification and kimi's effort surface are closed here with the honest
default the design names — `model_verification: none` / `supports_effort:
no` — not an invented 'stream'/'flag').
"""
from __future__ import annotations

import dataclasses
import json
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
    # Slice 4 (CodexAdapter): the CLI's prompt travels over stdin, not as an
    # argv element — `substrate.py` needs the actual bytes to hand its
    # process-runner's own "feed this to stdin" parameter. `None` (every
    # slice-3 adapter, and every agent-writes-file/output-flag adapter
    # here) preserves the original devnull/pipe-with-no-data behaviour
    # exactly — `adapters.py` never runs a process itself either way.
    stdin_data: str | None = None
    # Slice 4 (ClaudePrintAdapter): env keys the substrate must ERASE from
    # the inherited `os.environ`, not merely override — `inv.env` can only
    # ever ADD/OVERRIDE keys (`{**os.environ, **inv.env}`), which cannot
    # express "delete" for a nested-spawn signal like CLAUDECODE that a
    # non-empty override value would not honestly clear. Empty for every
    # other adapter.
    env_unset: tuple = ()


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

    def materialize_artifact(self, res, out_path) -> None:
        """Give stdout-capture transports a chance to create their artifact.

        Native-output and agent-writes-file transports keep the default
        no-op, which is important: this hook must never overwrite a file
        already written by the CLI or the agent.
        """
        return None

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


def model_witness_matches(transport: str, declared, observed) -> bool:
    """Compare a declared model to a witnessed model without weakening the
    normal exact-match rule.

    Grok's confirmed JSON format reports build-qualified keys such as
    ``grok-4.5-build`` while its invocation pin is ``grok-4.5``.  Only that
    documented ``-build`` qualification (optionally followed by another
    dash-separated build detail) is accepted; a merely similar model name
    is still a mismatch.
    """
    if declared == observed:
        return True
    if not isinstance(declared, str) or not isinstance(observed, str):
        return False
    return (
        transport == "grok-cli"
        and observed.startswith(f"{declared}-build")
        and (len(observed) == len(declared) + len("-build")
             or observed[len(declared) + len("-build")] == "-")
    )


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


# --- shared parsing helpers (codex/grok/kimi/claude-print, slices 4-5) ------


def _extract_single_json(text: str):
    """Best-effort parse of a CLI's single-shot JSON envelope (grok/claude
    `--output-format json`): try the whole text first, then the substring
    between the first `{` and the last `}` — a straight port of the
    bridge's `extractJson` (`run-external-agent.mjs:125-131`), which exists
    because a CLI's stdout occasionally carries a stray banner/warning line
    around the actual JSON object. Returns `None`, never raises, on
    anything that still doesn't parse — a malformed envelope is a missing
    witness, not a crash."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _iter_json_events(text: str):
    """Parse a JSONL/stream-json body (codex `--json`, kimi `--output-format
    stream-json`) — one JSON object per non-blank line, in order. A
    malformed line is skipped, never fatal (mirrors the bridge's
    `parseCodexEvents`/`parseCodexFinalAgentMessage`,
    `run-external-agent.mjs:141-173`, which tolerate the same)."""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def strictify_for_codex(node):
    """Port of the bridge's `strictifyForCodex`
    (`run-external-agent.mjs:178-190`, itself a direct requirement of
    `codex exec --output-schema`'s strict-mode contract, ADR-0005
    `0005-...:104`): every object-node gets `additionalProperties: false`
    and `required` set to ALL of its own `properties` keys, recursively.
    Optionality in strict mode is expressed by the schema author via a type
    union with `"null"`, never by omission from `required` — this function
    does not invent that union, it only enforces the two structural
    invariants strict mode demands."""
    if isinstance(node, list):
        return [strictify_for_codex(item) for item in node]
    if isinstance(node, dict):
        out = {key: strictify_for_codex(value) for key, value in node.items()}
        if out.get("type") == "object" and isinstance(out.get("properties"), dict):
            out["additionalProperties"] = False
            out["required"] = list(out["properties"].keys())
        return out
    return node


# --- codex (slice 4) ----------------------------------------------------------


class CodexAdapter(LaneAdapter):
    """`codex-cli` transport (ADR-0005 slice 4). Form verified live via
    `codex exec --help`: `-m/--output-schema/--json/-o/-s/-C/--add-dir` all
    present, `--effort` absent (design-runlane.md §12 "Notable" — the
    proza/cross-provider.md forms citing `codex exec --effort` do not match
    the installed CLI; reasoning effort is `-c model_reasoning_effort=`, a
    TOML-typed config-key override, not a flag). The prompt travels over
    stdin (never as an argv element — `--json`'s JSONL stream is the
    ENTIRE witness surface: session id from `thread.started`, usage from
    `turn.completed`, the final response text from the `agent_message`
    `item.completed` event — a direct port of the bridge's own parser,
    `run-external-agent.mjs:141-173`)."""

    transport = "codex-cli"
    CAPS = Capabilities(
        supports_effort="config-key",
        supports_schema="strict",
        has_own_sandbox="strong",
        artifact_channel="output-flag",
        model_verification="pin-validated",
    )

    def build_invocation(self, lane, req: InvocationRequest) -> Invocation:
        try:
            prompt_text = Path(req.prompt_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise LaneError("config", f"cannot read --prompt-file: {exc}") from exc

        workdir = str(Path(req.workdir))
        argv = ["codex", "exec"]
        if req.model:
            argv += ["-m", req.model]
        if req.effort:
            # NOT --effort — live `codex exec --help` carries no such flag;
            # reasoning effort is a dotted config-key override (see class
            # docstring).
            argv += ["-c", f"model_reasoning_effort={req.effort}"]
        sandbox = getattr(lane, "sandbox", None) if lane is not None else None
        if sandbox:
            argv += ["-s", sandbox]
        argv += ["-C", workdir]
        argv += ["--json"]
        argv += ["-o", str(req.out)]

        if req.schema:
            try:
                schema_text = Path(req.schema).read_text(encoding="utf-8")
            except OSError as exc:
                raise LaneError("config", f"cannot read --schema: {exc}") from exc
            try:
                schema_data = json.loads(schema_text)
            except json.JSONDecodeError as exc:
                raise LaneError("config", f"--schema is not valid JSON: {exc}") from exc
            strict_schema = strictify_for_codex(schema_data)
            schema_out = Path(req.workdir) / f".run-lane-codex-schema-{Path(req.out).name}.json"
            schema_out.write_text(json.dumps(strict_schema), encoding="utf-8")
            argv += ["--output-schema", str(schema_out)]

        return Invocation(
            argv=argv,
            env={},
            stdin_policy="pipe",
            cwd=workdir,
            prompt_addendum=None,
            log_file=None,
            prompt_length=len(prompt_text),
            stdin_data=prompt_text,
        )

    def parse_model_witness(self, res, inv: Invocation) -> ModelObservation:
        for event in _iter_json_events(res.stdout if res else ""):
            model = event.get("model")
            if isinstance(model, str) and model:
                return ModelObservation(model, "stream", json.dumps(event), error=None)
        # No `--json` event has carried a bare "model" field in any capture
        # taken so far (design-runlane.md §9 open question 3 — the bridge
        # never read one either, run-external-agent.mjs:141-158). codex's
        # `-m` is still a strong pin: an unknown/stale slug is a loud CLI
        # failure (nonzero exit), never a silent substitution — report the
        # pinned slug itself as the observed model rather than `None`.
        pinned = None
        if "-m" in inv.argv:
            idx = inv.argv.index("-m")
            if idx + 1 < len(inv.argv):
                pinned = inv.argv[idx + 1]
        return ModelObservation(pinned, "pin-validated", None, error=None)

    def parse_usage(self, res):
        usage = None
        for event in _iter_json_events(res.stdout if res else ""):
            if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                usage = event["usage"]
        return usage

    def parse_session_id(self, res):
        session_id = None
        for event in _iter_json_events(res.stdout if res else ""):
            if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                session_id = event["thread_id"]
        return session_id

    def parse_printed_text(self, res) -> tuple:
        text = ""
        for event in _iter_json_events(res.stdout if res else ""):
            item = event.get("item")
            if (event.get("type") == "item.completed" and isinstance(item, dict)
                    and item.get("type") == "agent_message" and isinstance(item.get("text"), str)):
                text = item["text"].strip()
        if len(text) > _MAX_PRINTED_CHARS:
            return text[:_MAX_PRINTED_CHARS], True
        return text, False


# --- grok (slice 5) ------------------------------------------------------------


class GrokAdapter(LaneAdapter):
    """`grok-cli` transport (ADR-0005 slice 5). Form verified live via
    `grok --help`: `--prompt-file/-m/--reasoning-effort/--sandbox
    (env GROK_SANDBOX)/--cwd/--output-format/-r/--json-schema` all present.
    Artifact channel is `agent-writes-file` (same prompt-instruction trick
    as agy/kimi — grok has no native `--out`-equivalent flag either);
    `--output-format json` reports the executed model as the key of its
    `modelUsage` mapping, alongside usage and sessionId."""

    transport = "grok-cli"
    CAPS = Capabilities(
        supports_effort="flag",
        supports_schema="strict",
        has_own_sandbox="strong",
        artifact_channel="agent-writes-file",
        model_verification="stream",
    )

    def build_invocation(self, lane, req: InvocationRequest) -> Invocation:
        try:
            prompt_text = Path(req.prompt_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise LaneError("config", f"cannot read --prompt-file: {exc}") from exc

        workdir = str(Path(req.workdir))
        addendum = _ARTIFACT_ADDENDUM_TEMPLATE.format(out=req.out)
        full_prompt = f"{prompt_text}{addendum}"
        # `--prompt-file`, not inline `-p`/`--single`, sidesteps both
        # OS argv-length limits and any shell-adjacent escaping concern for
        # an arbitrarily large addendum-carrying prompt (grok's `--help`
        # offers both; agy's own inline form is the one CLI here that has
        # no file-based alternative at all).
        prompt_file = Path(req.workdir) / f".run-lane-grok-prompt-{Path(req.out).name}.md"
        prompt_file.write_text(full_prompt, encoding="utf-8")

        env = {}
        argv = ["grok", "--prompt-file", str(prompt_file), "--output-format", "json",
                "--cwd", workdir]
        if req.model:
            argv += ["-m", req.model]
        if req.effort:
            argv += ["--reasoning-effort", req.effort]
        sandbox = getattr(lane, "sandbox", None) if lane is not None else None
        if sandbox:
            argv += ["--sandbox", sandbox]
            # config.yaml grok_hardening.env sets the same key — a
            # deliberate duplicate that "survives a swapped config file"
            # (config.yaml:907-908); setting it here too means a lane's own
            # declared sandbox profile is honoured even before hardening.py
            # is wired into the pipeline (see this package's GAPS report).
            env["GROK_SANDBOX"] = sandbox
        if req.resume:
            argv += ["--resume", req.resume]
        if req.schema:
            try:
                schema_text = Path(req.schema).read_text(encoding="utf-8")
            except OSError as exc:
                raise LaneError("config", f"cannot read --schema: {exc}") from exc
            # `--json-schema` takes the schema INLINE (live `grok --help`'s
            # own example: `--json-schema '{"type":"object",...}'`), not a
            # file path — unlike codex's `--output-schema <FILE>`.
            argv += ["--json-schema", schema_text]

        return Invocation(
            argv=argv, env=env, stdin_policy="devnull", cwd=workdir,
            prompt_addendum=addendum, log_file=None, prompt_length=len(full_prompt),
        )

    def parse_model_witness(self, res, inv: Invocation) -> ModelObservation:
        envelope = _extract_single_json(res.stdout if res else "")
        model_usage = envelope.get("modelUsage") if isinstance(envelope, dict) else None
        if isinstance(model_usage, dict):
            observed = next((key for key in model_usage if isinstance(key, str) and key), None)
            if observed is not None:
                return ModelObservation(observed, "stream", json.dumps(envelope), error=None)
        # An empty/missing modelUsage is an honest absence of evidence, not a
        # parser failure: Grok can still return a usable response.
        return ModelObservation(None, "none", None, error=None)

    def parse_usage(self, res):
        envelope = _extract_single_json(res.stdout if res else "")
        usage = envelope.get("usage") if isinstance(envelope, dict) else None
        return usage if isinstance(usage, dict) else None

    def parse_session_id(self, res):
        envelope = _extract_single_json(res.stdout if res else "")
        if isinstance(envelope, dict):
            for key in ("session_id", "sessionId"):
                if isinstance(envelope.get(key), str):
                    return envelope[key]
        return None

    def parse_printed_text(self, res) -> tuple:
        envelope = _extract_single_json(res.stdout if res else "")
        text = None
        if isinstance(envelope, dict):
            for key in ("result", "response", "text", "output"):
                value = envelope.get(key)
                if isinstance(value, str):
                    text = value
                    break
        if text is None:
            text = (res.stdout or "") if res else ""
        if len(text) > _MAX_PRINTED_CHARS:
            return text[:_MAX_PRINTED_CHARS], True
        return text, False


# --- kimi (slice 5) -------------------------------------------------------------


class KimiAdapter(LaneAdapter):
    """`kimi-cli-headless` transport (ADR-0005 slice 5). Form verified live
    via `kimi --help`: `-m/-p/--output-format text|stream-json/--add-dir`
    present, no effort flag anywhere on the surface (design-runlane.md §9
    open question 2 — `supports_effort: no`, not a guess at where a
    `low|high|max` effort config.yaml documents might otherwise live).
    Kimi has no `--prompt-file`, only inline `-p <prompt>`, so the
    artifact-instruction addendum travels as the `-p` value itself, same as
    agy's inline form. No filesystem sandbox of its own — isolation is
    worktree + OS/container (config.yaml kimi_hardening.isolation)."""

    transport = "kimi-cli-headless"
    CAPS = Capabilities(
        supports_effort="no",
        supports_schema="prompt",
        has_own_sandbox="none",
        artifact_channel="agent-writes-file",
        model_verification="none",
    )

    def build_invocation(self, lane, req: InvocationRequest) -> Invocation:
        try:
            prompt_text = Path(req.prompt_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise LaneError("config", f"cannot read --prompt-file: {exc}") from exc

        workdir = str(Path(req.workdir))
        addendum = _ARTIFACT_ADDENDUM_TEMPLATE.format(out=req.out)
        full_prompt = f"{prompt_text}{addendum}"

        argv = ["kimi", "--add-dir", workdir, "--output-format", "stream-json"]
        if req.model:
            argv += ["-m", req.model]
        argv += ["-p", full_prompt]

        return Invocation(
            argv=argv, env={}, stdin_policy="devnull", cwd=workdir,
            prompt_addendum=addendum, log_file=None, prompt_length=len(full_prompt),
        )

    def parse_model_witness(self, res, inv: Invocation) -> ModelObservation:
        observed, evidence = None, None
        for event in _iter_json_events(res.stdout if res else ""):
            model = event.get("model")
            if isinstance(model, str) and model:
                observed, evidence = model, json.dumps(event)
        if observed is not None:
            return ModelObservation(observed, "stream", evidence, error=None)
        # Honest 'none' — CAPS.model_verification declares no witness is
        # expected (design-runlane.md §9 open question 2's sibling: the
        # stream-json schema itself is unverified live).
        return ModelObservation(None, "none", None, error=None)

    def parse_usage(self, res):
        usage = None
        for event in _iter_json_events(res.stdout if res else ""):
            candidate = event.get("usage")
            if isinstance(candidate, dict):
                usage = candidate
        return usage

    def parse_session_id(self, res):
        session_id = None
        for event in _iter_json_events(res.stdout if res else ""):
            for key in ("session_id", "sessionId"):
                value = event.get(key)
                if isinstance(value, str):
                    session_id = value
        return session_id

    def parse_printed_text(self, res) -> tuple:
        parts = []
        for event in _iter_json_events(res.stdout if res else ""):
            for key in ("text", "delta", "content"):
                value = event.get(key)
                if isinstance(value, str):
                    parts.append(value)
        text = "".join(parts) if parts else ((res.stdout or "") if res else "")
        if len(text) > _MAX_PRINTED_CHARS:
            return text[:_MAX_PRINTED_CHARS], True
        return text, False


# --- claude-print (slice 4, запас / not-Claude-host fallback) ------------------

# ADR-0005 §"Claude-работники": nested-spawn signal that must be ERASED, not
# merely overridden, so a spawned `claude -p` never believes it is running
# inside an existing Claude Code session (`0005-...:184`).
_CLAUDE_NESTED_SPAWN_ENV_KEYS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


class ClaudePrintAdapter(LaneAdapter):
    """`claude-print` transport (ADR-0005 slice 4) — the not-Claude-host
    fallback. Form verified live via `claude --help`:
    `-p/--model/--agent/--effort/--add-dir/--output-format` all present;
    `-p` is a boolean print-mode flag here (unlike agy's consuming `-p`),
    the prompt is a plain positional argument. A declared DEGRADATION, not
    parity (`0005-...:149-157`): Agent tool remains the default for Claude
    workers, worktree/telemetry/context inheritance are all lost on this
    path. Nested-spawn env keys are erased via `Invocation.env_unset`
    (`0005-...:184`) — `substrate.py`'s the only place that can actually
    pop a key out of the inherited environment."""

    transport = "claude-print"
    CAPS = Capabilities(
        supports_effort="flag",
        supports_schema="prompt",
        has_own_sandbox="none",
        artifact_channel="stdout-capture",
        model_verification="stream",
    )

    def build_invocation(self, lane, req: InvocationRequest) -> Invocation:
        try:
            prompt_text = Path(req.prompt_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise LaneError("config", f"cannot read --prompt-file: {exc}") from exc

        workdir = str(Path(req.workdir))
        argv = ["claude", "-p"]
        if req.model:
            argv += ["--model", req.model]
        if req.role:
            argv += ["--agent", req.role]
        if req.effort:
            argv += ["--effort", req.effort]
        argv += ["--add-dir", workdir]
        argv += ["--output-format", "json"]
        argv += [prompt_text]

        return Invocation(
            argv=argv,
            env={},
            stdin_policy="devnull",
            cwd=workdir,
            prompt_addendum=None,
            log_file=None,
            prompt_length=len(prompt_text),
            env_unset=_CLAUDE_NESTED_SPAWN_ENV_KEYS,
        )

    def parse_model_witness(self, res, inv: Invocation) -> ModelObservation:
        envelope = _extract_single_json(res.stdout if res else "")
        model_usage = envelope.get("modelUsage") if isinstance(envelope, dict) else None
        if isinstance(model_usage, dict):
            observed = next((key for key in model_usage if isinstance(key, str) and key), None)
            if observed is not None:
                return ModelObservation(observed, "stream", json.dumps(envelope), error=None)
        return ModelObservation(
            None, "stream", None,
            error="claude --output-format json carried no modelUsage key — "
                  "cannot verify the model that actually ran")

    def materialize_artifact(self, res, out_path) -> None:
        envelope = _extract_single_json(res.stdout if res else "")
        result = envelope.get("result") if isinstance(envelope, dict) else None
        if isinstance(result, str):
            Path(out_path).write_text(result, encoding="utf-8")

    def parse_usage(self, res):
        envelope = _extract_single_json(res.stdout if res else "")
        usage = envelope.get("usage") if isinstance(envelope, dict) else None
        return usage if isinstance(usage, dict) else None

    def parse_session_id(self, res):
        envelope = _extract_single_json(res.stdout if res else "")
        if isinstance(envelope, dict) and isinstance(envelope.get("session_id"), str):
            return envelope["session_id"]
        return None

    def parse_printed_text(self, res) -> tuple:
        envelope = _extract_single_json(res.stdout if res else "")
        text = envelope.get("result") if isinstance(envelope, dict) else None
        if not isinstance(text, str):
            text = (res.stdout or "") if res else ""
        if len(text) > _MAX_PRINTED_CHARS:
            return text[:_MAX_PRINTED_CHARS], True
        return text, False


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
