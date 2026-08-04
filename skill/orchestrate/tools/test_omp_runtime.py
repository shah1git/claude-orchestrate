"""Behavioral tests for the OMP-native Pocock control plane.

These tests call the runtime at its public command seam.  OMP itself remains the
worker transport; fixtures provide only host-observed task result envelopes and
disk artifacts, never a fake routing decision.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

import omp_runtime as runtime


PROVENANCE = {
    "durable": True,
    "tracker": "fixture-tracker",
    "spec": "spec-1",
    "approval": "owner-approved",
    "tickets": ["T1"],
    "dependencies": {"T1": []},
}


def model_manifest(*, fallback: bool = False) -> dict[str, dict[str, object]]:
    """One witness per declared slot, exactly as the adapter reports them.

    The fixture derives its keys from `omp.slots` so a new slot cannot be added
    to the config without the tests seeing it.
    """
    slots = runtime.load_config()["omp"]["slots"]
    return {
        slot: {
            "role": definition["alias"],
            "provider": "fixture",
            "resolvedModel": f"fixture/{slot}-model",
            "resolvedModelIsFallback": fallback,
        }
        for slot, definition in slots.items()
    }


@pytest.fixture
def config(tmp_path: Path) -> dict:
    value = copy.deepcopy(runtime.load_config())
    value["telemetry"]["log"] = str(tmp_path / "routing-log.jsonl")
    return value


@pytest.fixture
def cwd() -> Path:
    return Path(__file__).resolve().parents[3]


def start(tmp_path: Path, cwd: Path, config: dict, entry: str, *, models: dict | None = None) -> tuple[str, dict]:
    state_dir = str(tmp_path / "state")
    response = runtime.command_start(
        cwd,
        state_dir,
        {
            "entry": entry,
            "objective": f"{entry} fixture objective",
            "sessionId": "fixture-session",
            "manifestFingerprint": runtime.trusted_manifest_fingerprint(cwd, config),
            "models": models or model_manifest(),
        },
        config,
    )
    return state_dir, response

def reference(card: dict) -> dict:
    return {"runId": card["runId"], "revision": card["revision"], "stateHash": card["stateHash"]}


def transition(cwd: Path, state_dir: str, response: dict, action: str, payload: dict | None = None) -> dict:
    card = response["card"]
    request = {**reference(card), "action": action}
    if payload is not None:
        request["payload"] = payload
    return runtime.command_transition(cwd, state_dir, request)


def advance_full(cwd: Path, state_dir: str, response: dict) -> dict:
    steps = (
        ("record_triage", {"shape": "сборка"}),
        ("record_clarification", {"decisions": ["fixture decision"]}),
        ("record_plan", {"tickets": ["T1"]}),
        ("approve_plan", {"approved": True, "owner": "fixture"}),
        ("publish_tickets", PROVENANCE),
    )
    for action, payload in steps:
        response = transition(cwd, state_dir, response, action, payload)
    return response


def admit_frontier(cwd: Path, state_dir: str, response: dict, provenance: dict | None = None) -> dict:
    return transition(cwd, state_dir, response, "admit_frontier", provenance or PROVENANCE)


def mechanical_ticket(*, ui_live: bool = False) -> dict:
    ticket = {
        "ticketId": "T1",
        "OBJECTIVE": "Inspect one known file",
        "CONTEXT": "Deterministic fixture",
        "INPUTS": "README.md",
        "OUTPUT": "Strict structured inspection report",
        "TOOLS": "read",
        "BOUNDARIES": "Do not modify files",
        "ACCEPTANCE": "Return observed evidence",
        "signals": [
            "every step enumerable in advance",
            "zero decisions remain",
            "exact output format given",
            "a wrong result is detectable on sight",
        ],
        "write": False,
        "writablePaths": [],
        "verification": [{"argv": ["python3", "-c", "print(1)"], "cwd": ".", "timeoutSeconds": 10}],
        "ui_live": ui_live,
    }
    if ui_live:
        ticket["ui_evidence"] = {"target": "http://fixture.test", "criterion": "fixture renders"}
    return ticket

def ticket_named(ticket_id: str) -> dict:
    ticket = mechanical_ticket()
    ticket["ticketId"] = ticket_id
    return ticket

def writer_ticket(ticket_id: str, path: str, *, verification: list[dict] | None = None) -> dict:
    ticket = mechanical_ticket()
    ticket.update({
        "ticketId": ticket_id,
        "OBJECTIVE": f"Update {path}",
        "INPUTS": path,
        "OUTPUT": f"Patched {path}",
        "TOOLS": "read, edit",
        "BOUNDARIES": f"Modify only {path}",
        "ACCEPTANCE": f"{path} contains the requested line",
        "signals": ["complete spec exists", "success is objectively checkable (tests, criteria)"],
        "write": True,
        "writablePaths": [path],
        "verification": verification or [],
    })
    return ticket

@pytest.fixture
def canonical_sweep_admission() -> dict:
    tickets = []
    for ticket_id_value, depends_on in (("T1", []), ("T2", []), ("T3", ["T1", "T2"])):
        ticket = ticket_named(ticket_id_value)
        ticket["dependsOn"] = depends_on
        tickets.append(ticket)
    return {
        "witness": {
            "closed": True,
            "acceptancePredecided": True,
            "integration": "aggregate",
            "evidence": "The owner pre-decided acceptance and aggregate integration.",
        },
        "tickets": tickets,
    }


def init_git_repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=path, check=True)
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=path, check=True)
    return path


def add_line_patch(path: str, original: str, added: list[str]) -> bytes:
    additions = "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1 +1,{1 + len(added)} @@\n"
        f" {original}\n"
        f"{additions}"
    ).encode()


def prepare(cwd: Path, state_dir: str, response: dict, config: dict, ticket: dict | None = None) -> dict:
    card = response["card"]
    return runtime.command_prepare(
        cwd,
        state_dir,
        {**reference(card), "tickets": [ticket or mechanical_ticket()]},
        config,
    )

def prepare_tickets(cwd: Path, state_dir: str, response: dict, config: dict, tickets: list[dict]) -> dict:
    card = response["card"]
    return runtime.command_prepare(cwd, state_dir, {**reference(card), "tickets": tickets}, config)

def prepare_sweep(cwd: Path, state_dir: str, response: dict, config: dict) -> dict:
    return runtime.command_prepare(cwd, state_dir, reference(response["card"]), config)


def seal(cwd: Path, state_dir: str, response: dict, kind: str) -> dict:
    card = response["card"]
    return runtime.command_seal(
        cwd,
        state_dir,
        {**reference(card), "kind": kind},
    )


def authoritative(cwd: Path, state_dir: str, run_id: str) -> dict:
    path, _ = runtime.state_paths(cwd, state_dir, run_id)
    return runtime.read_state(path)


def producer_result() -> dict:
    return {
        "status": "COMPLETED",
        "summary": "Known input inspected",
        "evidence": ["README.md was read"],
        "changedFiles": [],
        "verification": ["inspection complete"],
    }


def normalized_result(
    tmp_path: Path,
    assignment: dict,
    *,
    fallback: bool = False,
    suffix: str = ":medium",
    include_patch: bool = True,
) -> dict:
    artifact = tmp_path / f"{assignment['attemptId'].replace('/', '-')}.json"
    artifact.write_text(json.dumps({"attempt": assignment["attemptId"]}), encoding="utf-8")
    patch = tmp_path / f"{assignment['attemptId'].replace('/', '-')}.patch"
    patch.write_bytes(b"")
    return {
        "attemptId": assignment["attemptId"],
        "declaredAgent": assignment["agent"],
        "declaredModel": assignment["declaredModel"],
        "observedAgent": assignment["agent"],
        "observedAgentSource": "user",
        "observedResolvedModel": assignment["declaredModel"] + suffix,
        "resolvedModelIsFallback": fallback,
        "exitCode": 0,
        "aborted": False,
        "tokens": 7,
        "usage": None,
        "outputPath": str(artifact),
        "patchPath": str(patch) if include_patch else None,
        "branchName": None,
    }


def settle_producer(
    tmp_path: Path,
    cwd: Path,
    state_dir: str,
    sealed: dict,
    config: dict,
    *,
    include_patch: bool = True,
) -> dict:
    state = authoritative(cwd, state_dir, sealed["card"]["runId"])
    results = []
    content = {}
    for attempt_id in sealed["attemptIds"]:
        assignment = state["attempts"][attempt_id]
        results.append(normalized_result(tmp_path, assignment, include_patch=include_patch))
        content[attempt_id] = producer_result()
    card = sealed["card"]
    return runtime.command_record_result(
        cwd,
        state_dir,
        {
            **reference(card),
            "dispatchId": sealed["dispatchId"],
            "toolCallId": "producer-tool",
            "input": sealed["taskInput"],
            "details": {"results": results},
            "content": content,
            "isError": False,
        },
        config,
    )

def settle_producer_patches(
    tmp_path: Path,
    cwd: Path,
    state_dir: str,
    sealed: dict,
    config: dict,
    patches: dict[str, tuple[bytes, list[str]]],
) -> dict:
    state = authoritative(cwd, state_dir, sealed["card"]["runId"])
    results = []
    content = {}
    for attempt_id in sealed["attemptIds"]:
        assignment = state["attempts"][attempt_id]
        patch_bytes, changed_files = patches[assignment["ticketId"]]
        result = normalized_result(tmp_path, assignment)
        Path(result["patchPath"]).write_bytes(patch_bytes)
        results.append(result)
        content[attempt_id] = {**producer_result(), "changedFiles": changed_files}
    return runtime.command_record_result(
        cwd,
        state_dir,
        {
            **reference(sealed["card"]),
            "dispatchId": sealed["dispatchId"],
            "toolCallId": "producer-patch-tool",
            "input": sealed["taskInput"],
            "details": {"results": results},
            "content": content,
            "isError": False,
        },
        config,
    )


def pregate(cwd: Path, state_dir: str, response: dict, config: dict) -> dict:
    card = response["card"]
    return runtime.command_pregate(cwd, state_dir, reference(card), config)


def prepare_lenses(cwd: Path, state_dir: str, response: dict, config: dict) -> dict:
    card = response["card"]
    return runtime.command_prepare_lenses(cwd, state_dir, reference(card), config)


def settle_lenses(
    tmp_path: Path,
    cwd: Path,
    state_dir: str,
    sealed: dict,
    config: dict,
    *,
    critic_verdict: str = "PASS",
    standards_blocking: bool = False,
    critic_fail_ticket: str | None = None,
    availability_error: str | None = None,
    observed_models: dict[str, str] | None = None,
) -> dict:
    state = authoritative(cwd, state_dir, sealed["card"]["runId"])
    results = []
    content = {}
    for attempt_id in sealed["attemptIds"]:
        assignment = state["lensAttempts"][attempt_id]
        result = normalized_result(tmp_path, assignment)
        if observed_models and assignment["lens"] in observed_models:
            result["observedResolvedModel"] = observed_models[assignment["lens"]]
            result["resolvedModelIsFallback"] = True
        if availability_error is not None:
            result["error"] = availability_error
        results.append(result)
        reports = []
        for producer_attempt_id in assignment["producerAttemptIds"]:
            producer = state["attempts"][producer_attempt_id]
            findings = []
            if standards_blocking and assignment["lens"] == "Standards":
                findings.append({
                    "scope": "introduced",
                    "severity": "high",
                    "blocking": True,
                    "evidence": "fixture blocking standard violation",
                })
            reports.append({
                "attemptId": producer_attempt_id,
                "summary": "Fixture review complete",
                "findings": findings,
                "verdict": (
                    "FAIL"
                    if assignment["lens"] == "Critic" and producer["ticketId"] == critic_fail_ticket
                    else critic_verdict
                )
                if assignment["lens"] == "Critic"
                else "NO_VERDICT",
            })
        content[attempt_id] = {
            "lens": assignment["lens"],
            "summary": "Fixture review complete",
            "reports": reports,
        }
    card = sealed["card"]
    return runtime.command_record_result(
        cwd,
        state_dir,
        {
            **reference(card),
            "dispatchId": sealed["dispatchId"],
            "toolCallId": "lens-tool",
            "input": sealed["taskInput"],
            "details": {"results": results},
            "content": content,
            "isError": False,
        },
        config,
    )


def reach_adjudication(tmp_path: Path, cwd: Path, state_dir: str, response: dict, config: dict) -> tuple[dict, dict]:
    response = prepare(cwd, state_dir, response, config)
    response = seal(cwd, state_dir, response, "producer")
    response = settle_producer(tmp_path, cwd, state_dir, response, config)
    response = pregate(cwd, state_dir, response, config)
    response = prepare_lenses(cwd, state_dir, response, config)
    sealed_lenses = seal(cwd, state_dir, response, "lenses")
    return sealed_lenses, authoritative(cwd, state_dir, sealed_lenses["card"]["runId"])

def accept_prepared_sweep_wave(
    tmp_path: Path,
    cwd: Path,
    state_dir: str,
    response: dict,
    config: dict,
) -> dict:
    response = seal(cwd, state_dir, response, "producer")
    response = settle_producer(tmp_path, cwd, state_dir, response, config)
    response = pregate(cwd, state_dir, response, config)
    response = prepare_lenses(cwd, state_dir, response, config)
    response = seal(cwd, state_dir, response, "lenses")
    response = settle_lenses(tmp_path, cwd, state_dir, response, config)
    response = runtime.command_adjudicate(cwd, state_dir, reference(response["card"]))
    return runtime.command_accept(cwd, state_dir, reference(response["card"]), config)


def test_load_request_accepts_file_transport_and_rejects_dual_sources(tmp_path):
    request_path = tmp_path / "request.json"
    request_path.write_text('{"runId":"run-1"}', encoding="utf-8")

    assert runtime.load_request(None, str(request_path)) == {"runId": "run-1"}
    with pytest.raises(runtime.RuntimeFailure) as error:
        runtime.load_request("{}", str(request_path))
    assert error.value.code == "invalid_request"


def test_start_rejects_legacy_lead_witness(tmp_path, cwd, config):
    with pytest.raises(runtime.RuntimeFailure) as error:
        runtime.command_start(
            cwd,
            str(tmp_path / "state"),
            {
                "entry": "frontier",
                "objective": "fixture objective",
                "sessionId": "fixture-session",
                "manifestFingerprint": runtime.trusted_manifest_fingerprint(cwd, config),
                "models": model_manifest(),
                "lead": {},
            },
            config,
        )
    assert error.value.code == "invalid_request"


def test_start_rejects_a_second_nonterminal_run_for_the_same_repository(tmp_path, cwd, config):
    state_dir, first = start(tmp_path, cwd, config, "frontier")

    with pytest.raises(runtime.RuntimeFailure) as error:
        runtime.command_start(
            cwd,
            state_dir,
            {
                "entry": "frontier",
                "objective": "second fixture objective",
                "sessionId": "fixture-session-2",
                "manifestFingerprint": runtime.trusted_manifest_fingerprint(cwd, config),
                "models": model_manifest(),
            },
            config,
        )

    assert error.value.code == "active_run_exists"
    assert error.value.details == {
        "runId": first["card"]["runId"],
        "phase": "frontier_admission",
    }


def test_status_without_run_id_finds_the_single_active_run(tmp_path, cwd, config):
    state_dir, started = start(tmp_path, cwd, config, "frontier")

    observed = runtime.command_status(cwd, state_dir, {})

    assert observed["card"] == started["card"]


def test_status_without_run_id_reports_when_no_active_run(tmp_path, cwd):
    observed = runtime.command_status(cwd, str(tmp_path / "empty-state"), {})

    assert observed == {"active": False}


def test_start_supersedes_only_a_runtime_mismatched_active_run(tmp_path, cwd, config, monkeypatch):
    state_dir, first = start(tmp_path, cwd, config, "frontier")
    monkeypatch.setattr(runtime, "runtime_fingerprint", lambda: "replacement-runtime")

    _, replacement = start(tmp_path, cwd, config, "frontier")

    retired = authoritative(cwd, state_dir, first["card"]["runId"])
    assert retired["phase"] == "cancelled"
    assert retired["cancelReason"] == "superseded after runtime mismatch"
    assert retired["supersededBy"] == replacement["card"]["runId"]
    retired_card = runtime.command_status(cwd, state_dir, {"runId": first["card"]["runId"]})["card"]
    assert retired_card["cancelReason"] == "superseded after runtime mismatch"
    assert retired_card["supersededBy"] == replacement["card"]["runId"]
    assert runtime.command_status(cwd, state_dir, {})["card"] == replacement["card"]


def test_start_supersedes_a_manifest_only_mismatched_active_run(tmp_path, cwd, config, monkeypatch):
    observed_manifest = {"value": "fixture-agent-manifests"}
    monkeypatch.setattr(
        runtime,
        "trusted_manifest_fingerprint",
        lambda _cwd, _config: observed_manifest["value"],
    )
    state_dir, first = start(tmp_path, cwd, config, "frontier")
    observed_manifest["value"] = "replacement-agent-manifests"
    observed = runtime.command_status(
        cwd,
        state_dir,
        {"manifestFingerprint": "replacement-agent-manifests"},
        config,
    )
    assert observed["card"]["runtimeMismatch"] == {
        "expectedManifestFingerprint": "fixture-agent-manifests",
        "observedManifestFingerprint": "replacement-agent-manifests",
    }

    replacement = runtime.command_start(
        cwd,
        state_dir,
        {
            "entry": "frontier",
            "objective": "replacement fixture objective",
            "sessionId": "replacement-session",
            "manifestFingerprint": "replacement-agent-manifests",
            "models": model_manifest(),
        },
        config,
    )

    retired = authoritative(cwd, state_dir, first["card"]["runId"])
    assert retired["phase"] == "cancelled"
    assert retired["supersededBy"] == replacement["card"]["runId"]


def test_start_rejects_a_forged_manifest_witness(tmp_path, cwd, config):
    with pytest.raises(runtime.RuntimeFailure) as error:
        runtime.command_start(
            cwd,
            str(tmp_path / "state"),
            {
                "entry": "frontier",
                "objective": "forged manifest fixture",
                "sessionId": "fixture-session",
                "manifestFingerprint": "attacker-selected-manifest",
                "models": model_manifest(),
            },
            config,
        )

    assert error.value.code == "manifest_witness_mismatch"
    assert error.value.details["observed"] == "attacker-selected-manifest"



def test_runtime_replacement_recovers_after_activation_write_failure(tmp_path, cwd, config, monkeypatch):
    state_dir, first = start(tmp_path, cwd, config, "frontier")
    monkeypatch.setattr(runtime, "runtime_fingerprint", lambda: "replacement-runtime")
    old_state_path, _old_lock_path = runtime.state_paths(cwd, state_dir, first["card"]["runId"])
    real_write_state = runtime.write_state

    def fail_replacement_activation(path, state):
        if (
            path != old_state_path
            and state.get("phase") == "frontier_admission"
            and state.get("revision") == 1
        ):
            raise OSError("simulated replacement activation failure")
        real_write_state(path, state)

    monkeypatch.setattr(runtime, "write_state", fail_replacement_activation)
    with pytest.raises(OSError, match="simulated replacement activation failure"):
        runtime.command_start(
            cwd,
            state_dir,
            {
                "entry": "frontier",
                "objective": "recoverable replacement objective",
                "sessionId": "replacement-session",
                "manifestFingerprint": runtime.trusted_manifest_fingerprint(cwd, config),
                "models": model_manifest(),
            },
            config,
        )

    retired = authoritative(cwd, state_dir, first["card"]["runId"])
    replacement_run_id = retired["supersededBy"]
    staged = authoritative(cwd, state_dir, replacement_run_id)
    assert retired["phase"] == "cancelled"
    assert staged["phase"] == runtime.REPLACEMENT_STAGING_PHASE

    monkeypatch.setattr(runtime, "write_state", real_write_state)
    observed = runtime.command_status(cwd, state_dir, {})
    assert observed["card"]["runId"] == replacement_run_id
    assert observed["card"]["phase"] == "frontier_admission"
    assert observed["card"]["revision"] == 1
    transaction = runtime.replacement_transaction_path(cwd, state_dir, replacement_run_id)
    assert json.loads(transaction.read_text(encoding="utf-8"))["status"] == "committed"

def test_prepare_rejects_incomplete_tracker_inputs_but_allows_resolved_references(tmp_path, cwd, config):
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    shorthand = mechanical_ticket()
    shorthand["INPUTS"] = "Approved tracker reference #123"
    with pytest.raises(runtime.RuntimeFailure) as error:
        prepare(cwd, state_dir, response, config, shorthand)
    assert error.value.code == "incomplete_tracker_reference"

    for index, reference_value in enumerate((
        "https://github.com/owner/repo/issues/123",
        "issue://owner/repo/123",
        "skill/orchestrate/tools/omp_runtime.py",
    )):
        allowed_dir = tmp_path / f"allowed-{index}"
        allowed_state_dir, allowed_response = start(allowed_dir, cwd, config, "frontier")
        allowed_response = admit_frontier(cwd, allowed_state_dir, allowed_response)
        ticket = mechanical_ticket()
        ticket["INPUTS"] = reference_value
        prepared = prepare(cwd, allowed_state_dir, allowed_response, config, ticket)
        assert prepared["card"]["phase"] == "producer_dispatch_pending"


def test_copy_reinstall_preserves_live_telemetry(tmp_path):
    repo_root = Path(__file__).resolve().parents[3]
    home = tmp_path / "home"
    env = {
        **os.environ,
        "HOME": str(home),
        "PI_CODING_AGENT_DIR": str(home / ".omp" / "agent"),
    }
    legacy_run = home / ".agents" / "skills" / "pocock-run"
    legacy_telemetry = legacy_run / "telemetry"
    legacy_telemetry.mkdir(parents=True)
    (legacy_run / "SKILL.md").write_text("name: pocock-run\n", encoding="utf-8")
    (legacy_telemetry / "routing-log.jsonl").write_text('{"renamed":true}\n', encoding="utf-8")
    legacy_frontier = home / ".agents" / "skills" / "pocock-frontier"
    legacy_frontier.mkdir()
    (legacy_frontier / "SKILL.md").write_text("name: pocock-frontier\n", encoding="utf-8")
    omp_config = home / ".omp" / "agent" / "config.yml"
    omp_config.parent.mkdir(parents=True)
    omp_config.write_text(
        "ownerSetting:\n"
        "  preserved: true\n"
        "modelRoles:\n"
        "  default: owner/model\n"
        "  pocock-retired-backup: retired/model\n"
        "retry:\n"
        "  fallbackChains:\n"
        "    default: [owner/fallback]\n"
        "    pocock-retired-backup: [retired/fallback]\n",
        encoding="utf-8",
    )
    command = ["bash", str(repo_root / "install.sh")]
    first = subprocess.run(command, cwd=repo_root, env=env, text=True, capture_output=True, check=False)
    assert first.returncode == 0, first.stderr
    assert not legacy_run.exists()
    assert not legacy_frontier.exists()
    assert not (repo_root / ".omp" / "config.yml").exists()
    installed_config = yaml.safe_load(omp_config.read_text(encoding="utf-8"))
    expected_roles = {
        manifest.stem
        for manifest in (repo_root / ".omp" / "agents").glob("pocock-*.md")
    }
    assert installed_config["ownerSetting"] == {"preserved": True}
    assert installed_config["modelRoles"]["default"] == "owner/model"
    assert installed_config["retry"]["fallbackChains"]["default"] == ["owner/fallback"]
    assert {
        role
        for role in installed_config["modelRoles"]
        if role.startswith("pocock-")
    } == expected_roles
    assert {
        role
        for role in installed_config["retry"]["fallbackChains"]
        if role.startswith("pocock-")
    } == expected_roles
    assert "pocock-retired-backup" not in installed_config["modelRoles"]
    assert "pocock-retired-backup" not in installed_config["retry"]["fallbackChains"]

    installed = home / ".agents" / "skills" / "orchestrate"
    telemetry = installed / "telemetry"
    assert (telemetry / "routing-log.jsonl").read_text(encoding="utf-8") == '{"renamed":true}\n'
    if telemetry.is_symlink():
        telemetry.unlink()
    telemetry.mkdir(exist_ok=True)
    (telemetry / "routing-log.jsonl").write_text('{"live":true}\n', encoding="utf-8")
    with (installed / "SKILL.md").open("a", encoding="utf-8") as handle:
        handle.write("\nforced reinstall\n")

    second = subprocess.run(command, cwd=repo_root, env=env, text=True, capture_output=True, check=False)

    assert second.returncode == 0, second.stderr
    assert (telemetry / "routing-log.jsonl").read_text(encoding="utf-8") == '{"live":true}\n'
    assert not list(installed.parent.glob("orchestrate.bak*"))
    assert not list(installed.parent.glob("pocock-run.bak*"))
    assert not list(installed.parent.glob("pocock-frontier.bak*"))
    backup_root = home / ".local" / "state" / "claude-orchestrate" / "install-backups"
    assert list(backup_root.rglob("orchestrate.bak*"))
    assert list(backup_root.rglob("pocock-run.bak*"))
    assert list(backup_root.rglob("pocock-frontier.bak*"))


def test_full_run_reaches_durable_completion(tmp_path, cwd, config, monkeypatch):
    state_dir, response = start(tmp_path, cwd, config, "full")
    response = advance_full(cwd, state_dir, response)
    sealed_lenses, _ = reach_adjudication(tmp_path, cwd, state_dir, response, config)
    response = settle_lenses(tmp_path, cwd, state_dir, sealed_lenses, config)
    card = response["card"]
    response = runtime.command_adjudicate(cwd, state_dir, reference(card))
    assert response["card"]["phase"] == "accepted"

    monkeypatch.setattr(runtime, "telemetry_exists", lambda *_args: False)
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "appended\n", ""),
    )
    card = response["card"]
    response = runtime.command_accept(cwd, state_dir, reference(card), config)
    assert response["card"]["nextActions"] == ["continue_wave", "cancel"]
    with pytest.raises(runtime.RuntimeFailure) as error:
        transition(cwd, state_dir, response, "begin_synthesis")
    assert error.value.code == "frontier_not_exhausted"
    response = transition(
        cwd,
        state_dir,
        response,
        "continue_wave",
        {
            "remainingTicketIds": [],
            "nextTicketIds": [],
            "blockedTicketIds": [],
            "evidence": "durable tracker reports no remaining tickets",
        },
    )
    assert response["card"]["nextActions"] == ["begin_synthesis", "cancel"]
    response = transition(cwd, state_dir, response, "begin_synthesis")
    response = transition(cwd, state_dir, response, "complete")
    assert response["card"]["phase"] == "completed"
    assert runtime.command_status(cwd, state_dir, {"runId": response["card"]["runId"]})["card"] == response["card"]
    assert authoritative(cwd, state_dir, response["card"]["runId"])["tokensSpent"] == 28


def test_read_only_producer_accepts_host_result_without_patch_artifact(tmp_path, cwd, config):
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    sealed = seal(cwd, state_dir, prepare(cwd, state_dir, response, config), "producer")

    response = settle_producer(tmp_path, cwd, state_dir, sealed, config, include_patch=False)

    assert response["card"]["phase"] == "pregate_pending"
    state = authoritative(cwd, state_dir, response["card"]["runId"])
    attempt = state["attempts"][sealed["attemptIds"][0]]
    assert attempt["patchArtifact"] is None
    assert attempt["patchFiles"] == []

def test_lost_settlement_can_be_abandoned_fail_closed_and_retry_is_bounded(tmp_path, cwd, config):
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    sealed = seal(cwd, state_dir, prepare(cwd, state_dir, response, config), "producer")
    assert sealed["card"]["nextActions"] == ["abandon_dispatch"]

    with pytest.raises(runtime.RuntimeFailure) as error:
        transition(cwd, state_dir, sealed, "abandon_dispatch", {"reason": "session process disappeared"})
    assert error.value.code == "dispatch_still_ambiguous"

    response = transition(
        cwd,
        state_dir,
        sealed,
        "abandon_dispatch",
        {"confirmedLostSettlement": True, "reason": "session process disappeared and no result exists"},
    )
    state = authoritative(cwd, state_dir, response["card"]["runId"])
    assert response["card"]["phase"] == "repair_pending"
    assert state["pendingDispatch"]["status"] == "abandoned"
    assert state["projectedTokens"] == 0
    assert {attempt["status"] for attempt in state["attempts"].values()} == {"availability_failed"}

    response = transition(cwd, state_dir, response, "retry", {"diagnosis": "availability"})
    second = prepare(cwd, state_dir, response, config)
    second_state = authoritative(cwd, state_dir, second["card"]["runId"])
    second_attempt = second_state["attempts"][second_state["pendingDispatch"]["attemptIds"][0]]
    assert second_attempt["qualityAttempt"] == 2
    assert second_attempt["slot"] == "scout"
    assert second_attempt["agent"] == "pocock-scout"

    sealed_again = seal(cwd, state_dir, second, "producer")
    response = transition(
        cwd,
        state_dir,
        sealed_again,
        "abandon_dispatch",
        {"confirmedLostSettlement": True, "reason": "second session process also disappeared"},
    )
    response = transition(cwd, state_dir, response, "retry", {"diagnosis": "availability"})
    assert response["card"]["phase"] == "blocked"
    assert "retry limit reached for: T1" == response["card"]["blockedReason"]


def test_accepted_wave_can_authorize_one_exact_remaining_frontier(tmp_path, cwd, config, monkeypatch):
    provenance = {**PROVENANCE, "tickets": ["T1", "T2"], "dependencies": {"T1": [], "T2": ["T1"]}}
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response, provenance)
    sealed_lenses, _ = reach_adjudication(tmp_path, cwd, state_dir, response, config)
    response = settle_lenses(tmp_path, cwd, state_dir, sealed_lenses, config)
    card = response["card"]
    response = runtime.command_adjudicate(cwd, state_dir, reference(card))

    monkeypatch.setattr(runtime, "telemetry_exists", lambda *_args: False)
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "appended\n", ""),
    )
    card = response["card"]
    response = runtime.command_accept(cwd, state_dir, reference(card), config)
    response = transition(
        cwd,
        state_dir,
        response,
        "continue_wave",
        {
            "remainingTicketIds": ["T2"],
            "nextTicketIds": ["T2"],
            "blockedTicketIds": [],
            "evidence": "durable tracker reports T2 unblocked by accepted T1",
        },
    )
    assert response["card"]["phase"] == "ready"

    with pytest.raises(runtime.RuntimeFailure) as error:
        prepare(cwd, state_dir, response, config, ticket_named("T1"))
    assert error.value.code == "ticket_already_accepted"

    response = prepare(cwd, state_dir, response, config, ticket_named("T2"))
    state = authoritative(cwd, state_dir, response["card"]["runId"])
    assignment = state["attempts"][state["pendingDispatch"]["attemptIds"][0]]
    assert assignment["ticketId"] == "T2"
    assert assignment["qualityAttempt"] == 1
    assert state["currentWave"] == "wave-2"


def test_frontier_requires_provenance_but_accepts_explicit_unavailable_attestation(tmp_path, cwd, config):
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    with pytest.raises(runtime.RuntimeFailure, match="payload.tracker"):
        admit_frontier(cwd, state_dir, response, {"durable": True})
    assert runtime.command_status(cwd, state_dir, {"runId": response["card"]["runId"]})["card"]["revision"] == 0

    response = admit_frontier(
        cwd,
        state_dir,
        response,
        {
            "trackerUnavailable": True,
            "ownerAttestation": "The approved Pocock spine produced this frontier",
            "unavailableReason": "local tracker exposes no durable dependency API",
        },
    )
    assert response["card"]["phase"] == "ready"


def test_stale_revision_and_corrupt_state_fail_closed(tmp_path, cwd, config):
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    stale = response["card"]["revision"] - 1
    with pytest.raises(runtime.RuntimeFailure) as error:
        runtime.command_transition(cwd, state_dir, {**reference(response["card"]), "revision": stale, "action": "cancel"})
    assert error.value.code == "stale_revision"

    state_path, _ = runtime.state_paths(cwd, state_dir, response["card"]["runId"])
    value = json.loads(state_path.read_text(encoding="utf-8"))
    value["objective"] = "tampered"
    state_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(runtime.RuntimeFailure) as error:
        runtime.command_status(cwd, state_dir, {"runId": response["card"]["runId"]})
    assert error.value.code == "state_corrupt"

    with pytest.raises(runtime.RuntimeFailure) as start_error:
        start(tmp_path, cwd, config, "frontier")
    assert start_error.value.code == "state_corrupt"

    value["stateHash"] = runtime.digest(runtime.public_snapshot(value))
    state_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(runtime.RuntimeFailure) as authentication_error:
        start(tmp_path, cwd, config, "frontier")
    assert authentication_error.value.code == "state_auth_failed"


def test_runtime_drift_blocks_hydration_and_mutation_but_not_status(tmp_path, cwd, config, monkeypatch):
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    card = response["card"]
    pinned = authoritative(cwd, state_dir, card["runId"])["runtimeFingerprint"]
    changed = "f" * 64 if pinned != "f" * 64 else "e" * 64
    monkeypatch.setattr(runtime, "runtime_fingerprint", lambda: changed)

    with pytest.raises(runtime.RuntimeFailure) as hydration_error:
        runtime.command_hydrate(cwd, state_dir, reference(card))
    assert hydration_error.value.code == "runtime_changed"
    assert hydration_error.value.details == {"expected": pinned, "observed": changed}

    with pytest.raises(runtime.RuntimeFailure) as mutation_error:
        runtime.command_transition(cwd, state_dir, {**reference(card), "action": "cancel"})
    assert mutation_error.value.code == "runtime_changed"
    status = runtime.command_status(cwd, state_dir, {"runId": card["runId"]})
    assert status["card"]["phase"] == card["phase"]
    assert status["card"]["nextActions"] == []
    assert status["card"]["blockedReason"] == (
        "effective Pocock runtime differs from the runtime that created this run; "
        "inspect it with status and start a new run"
    )
    assert status["card"]["runtimeMismatch"] == {"expected": pinned, "observed": changed}


def test_run_starts_on_a_model_the_contour_has_never_heard_of(tmp_path, cwd, config):
    """The whole point of the slot model: a new provider needs no code change.

    Before slots, an unrecognised model was refused outright by a hand-written
    vendor table. Now the contour only ever sees a slot name and an opaque
    resolved-model string, so an unknown provider dispatches like any other.
    """
    exotic = model_manifest()
    for slot, witness in exotic.items():
        witness["provider"] = "brand-new-cloud"
        witness["resolvedModel"] = f"brand-new-cloud/unheard-of-{slot}"
    state_dir, response = start(tmp_path, cwd, config, "frontier", models=exotic)
    response = prepare(cwd, state_dir, admit_frontier(cwd, state_dir, response), config)
    state = authoritative(cwd, state_dir, response["card"]["runId"])
    assignment = state["attempts"][state["pendingDispatch"]["attemptIds"][0]]
    assert assignment["agent"] == "pocock-scout"
    assert assignment["declaredModel"].startswith("brand-new-cloud/")
    assert seal(cwd, state_dir, response, "producer")["card"]["phase"] == "producer_running"


def test_executed_input_is_rejected_and_fallback_result_preserves_live_witness(tmp_path, cwd, config):
    state_dir, response = start(tmp_path, cwd, config, "frontier", models=model_manifest(fallback=True))
    sealed = seal(cwd, state_dir, prepare(cwd, state_dir, admit_frontier(cwd, state_dir, response), config), "producer")
    state = authoritative(cwd, state_dir, sealed["card"]["runId"])
    assignment = state["attempts"][sealed["attemptIds"][0]]
    result = normalized_result(tmp_path, assignment, fallback=True, suffix="-fallback")
    request = {
        **reference(sealed["card"]),
        "dispatchId": sealed["dispatchId"],
        "toolCallId": "producer-tool",
        "input": {"context": "tampered", "tasks": []},
        "details": {"results": [result]},
        "content": {assignment["attemptId"]: producer_result()},
        "isError": False,
    }
    with pytest.raises(runtime.RuntimeFailure) as error:
        runtime.command_record_result(cwd, state_dir, request, config)
    assert error.value.code == "seal_mismatch"

    request["input"] = sealed["taskInput"]
    settled = runtime.command_record_result(cwd, state_dir, request, config)
    assert settled["card"]["phase"] == "pregate_pending"
    persisted = authoritative(cwd, state_dir, settled["card"]["runId"])
    attempt = persisted["attempts"][assignment["attemptId"]]
    assert persisted["models"][assignment["slot"]]["resolvedModelIsFallback"] is True
    assert "lead" not in persisted
    assert persisted["tokensSpent"] == 7
    assert (
        attempt["observedAgent"],
        attempt["observedAgentSource"],
        attempt["observedModel"],
        attempt["modelFallback"],
    ) == (assignment["agent"], "user", result["observedResolvedModel"], True)
    assert "durationMs" not in attempt
    assert "requests" not in attempt
    actor = settled["card"]["dispatch"]["actors"][0]
    assert actor["observedModel"] == result["observedResolvedModel"]
    assert actor["modelWitness"] == "OBSERVED_FALLBACK"



def test_failed_task_preserves_transport_error_in_attempt(tmp_path, cwd, config):
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    sealed = seal(cwd, state_dir, prepare(cwd, state_dir, admit_frontier(cwd, state_dir, response), config), "producer")
    state = authoritative(cwd, state_dir, sealed["card"]["runId"])
    assignment = state["attempts"][sealed["attemptIds"][0]]
    result = normalized_result(tmp_path, assignment)
    transport_error = "EBUSY: resource busy or locked, rmdir '/tmp/omp-overlay'"
    result.update(
        {
            "observedResolvedModel": None,
            "exitCode": None,
            "aborted": False,
            "error": transport_error,
        }
    )

    settled = runtime.command_record_result(
        cwd,
        state_dir,
        {
            **reference(sealed["card"]),
            "dispatchId": sealed["dispatchId"],
            "toolCallId": "producer-transport-error-tool",
            "input": sealed["taskInput"],
            "details": {"results": [result]},
            "content": {},
            "isError": True,
        },
        config,
    )

    persisted = authoritative(cwd, state_dir, settled["card"]["runId"])
    attempt = persisted["attempts"][assignment["attemptId"]]
    assert attempt["status"] == "availability_failed"
    assert attempt["failureReason"] == transport_error
    assert attempt["availabilityEvidence"] == {
        "declaredModel": assignment["declaredModel"],
        "observedModel": None,
        "fallback": False,
        "error": transport_error,
        "exitCode": None,
        "aborted": False,
        "isError": True,
        "reason": transport_error,
    }



def test_success_exit_with_patch_capture_error_preserves_exact_failure(tmp_path, cwd, config):
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    sealed = seal(cwd, state_dir, prepare(cwd, state_dir, admit_frontier(cwd, state_dir, response), config), "producer")
    state = authoritative(cwd, state_dir, sealed["card"]["runId"])
    assignment = state["attempts"][sealed["attemptIds"][0]]
    result = normalized_result(tmp_path, assignment)
    capture_error = "Patch capture failed: fatal: Not a valid object name"
    result.update({"exitCode": 0, "error": capture_error})

    settled = runtime.command_record_result(
        cwd,
        state_dir,
        {
            **reference(sealed["card"]),
            "dispatchId": sealed["dispatchId"],
            "toolCallId": "producer-patch-capture-error-tool",
            "input": sealed["taskInput"],
            "details": {"results": [result]},
            "content": {},
            "isError": False,
        },
        config,
    )

    persisted = authoritative(cwd, state_dir, settled["card"]["runId"])
    attempt = persisted["attempts"][assignment["attemptId"]]
    assert attempt["status"] == "availability_failed"
    assert attempt["failureReason"] == capture_error
    assert attempt["availabilityEvidence"]["reason"] == capture_error


def test_ui_evidence_failure_spends_two_quality_attempts_then_blocks(tmp_path, cwd, config):
    ticket = mechanical_ticket(ui_live=True)
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    for expected_failure in (1, 2):
        sealed = seal(cwd, state_dir, prepare(cwd, state_dir, response, config, ticket), "producer")
        response = settle_producer(tmp_path, cwd, state_dir, sealed, config)
        response = pregate(cwd, state_dir, response, config)
        assert response["card"]["phase"] == "repair_pending"
        state = authoritative(cwd, state_dir, response["card"]["runId"])
        assert state["qualityFailures"]["T1"] == expected_failure
        response = transition(cwd, state_dir, response, "retry", {"diagnosis": "effort"})
    assert response["card"]["phase"] == "blocked"


@pytest.mark.parametrize(
    ("critic_verdict", "standards_blocking", "needle"),
    (("FAIL", False, "Critic FAIL"), ("PASS", True, "blocking Standards")),
)
def test_three_lens_gate_refuses_critic_or_standards_failure(
    tmp_path,
    cwd,
    config,
    critic_verdict,
    standards_blocking,
    needle,
):
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    sealed_lenses, _ = reach_adjudication(tmp_path, cwd, state_dir, admit_frontier(cwd, state_dir, response), config)
    response = settle_lenses(
        tmp_path,
        cwd,
        state_dir,
        sealed_lenses,
        config,
        critic_verdict=critic_verdict,
        standards_blocking=standards_blocking,
    )
    card = response["card"]
    response = runtime.command_adjudicate(cwd, state_dir, reference(card))
    assert response["card"]["phase"] == "repair_pending"
    assert needle in response["card"]["blockedReason"]


def test_router_uses_alias_roles_and_rejects_under_routing(tmp_path, cwd, config):
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    skilled = mechanical_ticket()
    skilled["signals"] = ["complete spec exists", "success is objectively checkable (tests, criteria)"]
    response = prepare(cwd, state_dir, response, config, skilled)
    state = authoritative(cwd, state_dir, response["card"]["runId"])
    attempt = state["attempts"][state["pendingDispatch"]["attemptIds"][0]]
    assert (attempt["class"], attempt["agent"], attempt["slot"]) == (
        "skilled",
        "pocock-builder",
        "builder",
    )
    assert attempt["declaredModel"] == state["models"]["builder"]["resolvedModel"]
    assert state["pendingDispatch"]["taskInput"]["tasks"][0]["isolated"] is True

    other_state_dir, other = start(tmp_path / "other", cwd, config, "frontier")
    other = admit_frontier(cwd, other_state_dir, other)
    skilled["class"] = "mechanical"
    with pytest.raises(runtime.RuntimeFailure) as error:
        prepare(cwd, other_state_dir, other, config, skilled)
    assert error.value.code == "under_routing"


def test_budget_projection_refuses_dispatch_before_task(tmp_path, cwd, config):
    config["session_budget"]["tokens_max"] = config["omp"]["budget_projection"]["producer_per_ticket"] - 1
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    with pytest.raises(runtime.RuntimeFailure) as error:
        prepare(cwd, state_dir, response, config)
    assert error.value.code == "budget_exceeded"
    assert runtime.command_status(cwd, state_dir, {"runId": response["card"]["runId"]})["card"]["phase"] == "ready"


def test_effective_omp_settings_are_fail_closed(monkeypatch, tmp_path):
    required = {
        "async.enabled": False,
        "task.batch": True,
        "task.enableEffort": True,
        "task.isolation.mode": "auto",
        "task.isolation.apply": False,
        "task.isolation.merge": "patch",
        "task.maxRecursionDepth": 1,
        "task.maxConcurrency": 6,
        "retry.modelFallback": True,
    }

    def result(values):
        payload = {key: {"value": value} for key, value in values.items()}
        return subprocess.CompletedProcess(["omp"], 0, json.dumps(payload), "")

    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: result(required))
    runtime.validate_effective_omp_settings(tmp_path)

    for mode in ("rcopy", "overlayfs"):
        isolated = {**required, "task.isolation.mode": mode}
        monkeypatch.setattr(runtime.subprocess, "run", lambda *args, values=isolated, **kwargs: result(values))
        runtime.validate_effective_omp_settings(tmp_path)

    for mode in ("none", "unknown-backend"):
        unisolated = {**required, "task.isolation.mode": mode}
        monkeypatch.setattr(runtime.subprocess, "run", lambda *args, values=unisolated, **kwargs: result(values))
        with pytest.raises(runtime.RuntimeFailure) as isolation_error:
            runtime.validate_effective_omp_settings(tmp_path)
        assert isolation_error.value.code == "omp_config_incompatible"
        assert isolation_error.value.details["mismatches"][0]["key"] == "task.isolation.mode"

    for incompatible, mismatch_key in (
        ({**required, "task.isolation.apply": True}, "task.isolation.apply"),
        ({**required, "task.isolation.merge": "apply"}, "task.isolation.merge"),
        ({**required, "retry.modelFallback": False}, "retry.modelFallback"),
    ):
        monkeypatch.setattr(runtime.subprocess, "run", lambda *args, values=incompatible, **kwargs: result(values))
        with pytest.raises(runtime.RuntimeFailure) as error:
            runtime.validate_effective_omp_settings(tmp_path)
        assert error.value.code == "omp_config_incompatible"
        assert error.value.details["mismatches"][0]["key"] == mismatch_key


def test_seal_rejects_only_nested_repository_without_resolvable_head(tmp_path, config):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "root"],
        cwd=repo,
        check=True,
    )
    nested = repo / "scratch" / "unborn"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=nested, check=True)

    state_dir, response = start(tmp_path / "run", repo, config, "frontier")
    response = prepare(repo, state_dir, admit_frontier(repo, state_dir, response), config)
    before = response["card"]
    with pytest.raises(runtime.RuntimeFailure) as error:
        runtime.command_seal(repo, state_dir, {**reference(before), "kind": "producer"})
    assert error.value.code == "omp_isolation_baseline_invalid"
    assert error.value.details == {
        "repositories": [{"path": "scratch/unborn", "reason": "HEAD does not resolve to a commit"}]
    }
    unchanged = runtime.command_status(repo, state_dir, {"runId": before["runId"]})["card"]
    assert unchanged["revision"] == before["revision"]
    assert unchanged["phase"] == "producer_dispatch_pending"

    (nested / "fixture.txt").write_text("healthy\n", encoding="utf-8")
    subprocess.run(["git", "add", "fixture.txt"], cwd=nested, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "nested"],
        cwd=nested,
        check=True,
    )
    sealed = runtime.command_seal(repo, state_dir, {**reference(before), "kind": "producer"})
    assert sealed["card"]["phase"] == "producer_running"


def test_same_revision_state_rewrite_is_rejected_by_authenticated_witness(tmp_path, cwd, config):
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    state_path, _ = runtime.state_paths(cwd, state_dir, response["card"]["runId"])
    assert runtime.state_key_path(state_path).stat().st_mode & 0o777 == 0o600
    value = json.loads(state_path.read_text(encoding="utf-8"))
    value["objective"] = "tampered but internally rehashed"
    value["stateHash"] = runtime.digest(runtime.public_snapshot(value))
    state_path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(runtime.RuntimeFailure) as error:
        runtime.command_transition(cwd, state_dir, {**reference(response["card"]), "action": "cancel"})
    assert error.value.code == "state_auth_failed"


def test_ui_evidence_is_attempt_bound_and_ui_tickets_cannot_share_a_wave(tmp_path, cwd, config):
    ui_ticket = mechanical_ticket(ui_live=True)
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    with pytest.raises(runtime.RuntimeFailure) as error:
        prepare_tickets(cwd, state_dir, response, config, [ui_ticket, ticket_named("T2")])
    assert error.value.code == "ticket_invalid"

    sealed = seal(cwd, state_dir, prepare(cwd, state_dir, response, config, ui_ticket), "producer")
    response = settle_producer(tmp_path, cwd, state_dir, sealed, config)
    card = response["card"]
    challenge = card["evidenceRequests"][0]
    response = runtime.command_record_evidence(
        cwd,
        state_dir,
        {
            **reference(card),
            "toolCallId": "browser-open",
            "tool": "browser",
            "success": True,
            "invocation": {
                "action": "open",
                "name": challenge["token"],
                "url": challenge["target"],
            },
            "details": {"url": "http://fixture.test"},
            "content": "opened",
            "attemptIds": [challenge["attemptId"]],
            "challengeToken": challenge["token"],
            "stage": "open",
        },
    )
    card = response["card"]
    exercise_request = {
        **reference(card),
        "toolCallId": "browser-exercise",
        "tool": "browser",
        "success": True,
        "invocation": {
            "action": "run",
            "name": challenge["token"],
            "code": "const rendered = document.body.textContent?.includes('fixture'); assert(rendered, 'fixture renders')",
        },
        "details": {"url": "http://fixture.test"},
        "content": "rendered",
        "attemptIds": [challenge["attemptId"]],
        "challengeToken": challenge["token"],
        "stage": "exercise",
    }
    invalid_exercise = copy.deepcopy(exercise_request)
    invalid_exercise["invocation"]["code"] = "return document.body.textContent"
    with pytest.raises(runtime.RuntimeFailure) as error:
        runtime.command_record_evidence(cwd, state_dir, invalid_exercise)
    assert error.value.code == "evidence_invalid"

    constant_exercise = copy.deepcopy(exercise_request)
    constant_exercise["invocation"]["code"] = "assert(true, 'fixture renders')"
    with pytest.raises(runtime.RuntimeFailure) as error:
        runtime.command_record_evidence(cwd, state_dir, constant_exercise)
    assert error.value.code == "evidence_invalid"

    response = runtime.command_record_evidence(cwd, state_dir, exercise_request)
    response = pregate(cwd, state_dir, response, config)
    assert response["card"]["phase"] == "lens_prepare_pending"
    state = authoritative(cwd, state_dir, response["card"]["runId"])
    ui_check = next(check for check in state["pregate"]["checks"] if check.get("kind") == "ui-evidence")
    assert ui_check["criterion"] == "fixture renders"
    assert any("assert(" in record for record in ui_check["records"])


def test_pregate_keeps_passed_candidates_and_retries_only_failed_tickets(tmp_path, cwd, config):
    first = ticket_named("T1")
    first["verification"] = [{"argv": ["python3", "-c", "raise SystemExit(1)"], "cwd": ".", "timeoutSeconds": 10}]
    second = ticket_named("T2")
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    response = prepare_tickets(cwd, state_dir, response, config, [first, second])
    sealed = seal(cwd, state_dir, response, "producer")
    response = pregate(cwd, state_dir, settle_producer(tmp_path, cwd, state_dir, sealed, config), config)
    state = authoritative(cwd, state_dir, response["card"]["runId"])

    assert response["card"]["phase"] == "lens_prepare_pending"
    assert state["qualityFailures"] == {"T1": 1}
    assert state["pregate"]["status"] == "PARTIAL"
    assert state["retryTicketIds"] == ["T1"]
    statuses = {attempt["ticketId"]: attempt["status"] for attempt in state["attempts"].values()}
    assert statuses == {"T1": "pregate_failed", "T2": "completed"}
    assert state["pregate"]["passedAttemptIds"] == [next(
        attempt_id
        for attempt_id, attempt in state["attempts"].items()
        if attempt["ticketId"] == "T2"
    )]
    prepared_lenses = prepare_lenses(cwd, state_dir, response, config)
    assert len(prepared_lenses["card"]["dispatch"]["actors"]) == 3


def test_lenses_occupy_three_slots_a_producer_can_never_hold(tmp_path, cwd, config):
    """Independence, restated in slot terms and checked as such."""
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    response = prepare_tickets(cwd, state_dir, response, config, [ticket_named("T1"), ticket_named("T2")])
    response = seal(cwd, state_dir, response, "producer")
    response = pregate(cwd, state_dir, settle_producer(tmp_path, cwd, state_dir, response, config), config)
    prepared = prepare_lenses(cwd, state_dir, response, config)
    state = authoritative(cwd, state_dir, prepared["card"]["runId"])
    assignments = [state["lensAttempts"][attempt_id] for attempt_id in state["pendingDispatch"]["attemptIds"]]
    producer_ids = state["pregate"]["passedAttemptIds"]
    producer_slots = {state["attempts"][attempt_id]["slot"] for attempt_id in producer_ids}
    lens_slots = {assignment["slot"] for assignment in assignments}

    assert len(assignments) == 3
    assert {assignment["lens"] for assignment in assignments} == {"Standards", "Spec", "Critic"}
    assert len(lens_slots) == 3
    assert not (lens_slots & producer_slots)
    assert all(assignment["producerAttemptIds"] == producer_ids for assignment in assignments)
    assert prepared["card"]["dispatch"]["kind"] == "lenses"
    assert len(prepared["card"]["dispatch"]["actors"]) == 3


def test_one_wave_may_mix_ticket_classes_and_each_lands_on_its_own_slot(tmp_path, cwd, config):
    """A wave is not required to be slot-homogeneous.

    Independence no longer depends on the wave having a single producer
    identity, so a mechanical and a judgment ticket may share one wave.
    """
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    architect_ticket = ticket_named("T2")
    architect_ticket["class"] = "judgment"
    response = prepare_tickets(cwd, state_dir, response, config, [ticket_named("T1"), architect_ticket])
    state = authoritative(cwd, state_dir, response["card"]["runId"])
    by_ticket = {
        attempt["ticketId"]: attempt
        for attempt in state["attempts"].values()
    }
    assert by_ticket["T2"]["class"] == "judgment"
    assert by_ticket["T2"]["slot"] == "architect"
    assert by_ticket["T2"]["agent"] == "pocock-architect"
    assert by_ticket["T1"]["slot"] != by_ticket["T2"]["slot"]


def test_config_refuses_a_lens_slot_a_producer_could_also_hold(config):
    """The single invariant that replaced the vendor/family apparatus."""
    omp = copy.deepcopy(config["omp"])
    omp["lenses"]["slots"]["Spec"]["slot"] = omp["producers"]["skilled"]["slot"]
    with pytest.raises(runtime.RuntimeFailure) as error:
        runtime.validate_slot_disjointness(omp)
    assert error.value.code == "config_invalid"
    assert "disjoint" in error.value.message


def test_config_refuses_a_slot_that_names_no_omp_role(config):
    omp = copy.deepcopy(config["omp"])
    omp["producers"]["skilled"]["slot"] = "no-such-slot"
    with pytest.raises(runtime.RuntimeFailure) as error:
        runtime.validate_slot_disjointness(omp)
    assert error.value.code == "config_invalid"
    assert "omp.slots" in error.value.message


def test_config_refuses_an_unbound_slot(config):
    omp = copy.deepcopy(config["omp"])
    omp["slots"]["retired"] = {"alias": "@pocock-retired"}
    with pytest.raises(runtime.RuntimeFailure) as error:
        runtime.validate_slot_disjointness(omp)
    assert error.value.code == "config_invalid"
    assert "unbound slots: retired" in error.value.message


def test_config_refuses_two_lenses_on_one_slot(config):
    """Three lenses on two slots would be a two-lens gate wearing three names."""
    omp = copy.deepcopy(config["omp"])
    omp["lenses"]["slots"]["Spec"]["slot"] = omp["lenses"]["slots"]["Standards"]["slot"]
    with pytest.raises(runtime.RuntimeFailure) as error:
        runtime.validate_slot_disjointness(omp)
    assert error.value.code == "config_invalid"
    assert "shares a slot" in error.value.message


def test_config_refuses_a_lens_without_a_slot(config):
    omp = copy.deepcopy(config["omp"])
    del omp["lenses"]["slots"]["Critic"]["slot"]
    with pytest.raises(runtime.RuntimeFailure) as error:
        runtime.validate_slot_disjointness(omp)
    assert error.value.code == "config_invalid"
    assert "declares no slot" in error.value.message


def test_capability_retry_deepens_the_class(tmp_path, cwd, config):
    """A model that was not deep enough is answered with a deeper class."""
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    response = prepare(cwd, state_dir, response, config)
    state = authoritative(cwd, state_dir, response["card"]["runId"])
    first = state["attempts"][state["pendingDispatch"]["attemptIds"][0]]
    assert (first["class"], first["slot"]) == ("mechanical", "scout")

    runtime.mutate(
        cwd,
        state_dir,
        reference(response["card"]),
        lambda current: current["attempts"][first["attemptId"]].update({"status": "pregate_failed"})
        or current.update({"phase": "repair_pending", "retryTicketIds": ["T1"], "pendingDispatch": None}),
    )
    response = runtime.command_status(cwd, state_dir, {"runId": response["card"]["runId"]})
    response = transition(cwd, state_dir, response, "retry", {"diagnosis": "capability"})
    second = prepare(cwd, state_dir, response, config)
    second_state = authoritative(cwd, state_dir, second["card"]["runId"])
    retried = second_state["attempts"][second_state["pendingDispatch"]["attemptIds"][0]]
    assert second_state["classFloor"]["T1"] == "skilled"
    assert (retried["class"], retried["slot"]) == ("skilled", "builder")


def test_capability_retry_of_a_writing_ticket_blocks_at_its_depth_ceiling(tmp_path, cwd, config):
    """A writing ticket cannot silently switch models after exhausting `skilled`."""
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    ticket = writer_ticket("T1", "fixture.txt")
    response = prepare(cwd, state_dir, response, config, ticket)
    state = authoritative(cwd, state_dir, response["card"]["runId"])
    first = state["attempts"][state["pendingDispatch"]["attemptIds"][0]]
    assert (first["class"], first["slot"]) == ("skilled", "builder")

    runtime.mutate(
        cwd,
        state_dir,
        reference(response["card"]),
        lambda current: current["attempts"][first["attemptId"]].update({"status": "pregate_failed"})
        or current.update({"phase": "repair_pending", "retryTicketIds": ["T1"], "pendingDispatch": None}),
    )
    response = runtime.command_status(cwd, state_dir, {"runId": response["card"]["runId"]})
    response = transition(cwd, state_dir, response, "retry", {"diagnosis": "capability"})
    assert response["card"]["phase"] == "blocked"
    assert "escalation is exhausted" in response["card"]["blockedReason"]


def test_capability_retry_blocks_a_judgment_ticket_instead_of_sidestepping(tmp_path, cwd, config):
    """`judgment` is the deepest class, so the run blocks with a diagnosis."""
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    ticket = ticket_named("T1")
    ticket["class"] = "judgment"
    response = prepare(cwd, state_dir, response, config, ticket)
    state = authoritative(cwd, state_dir, response["card"]["runId"])
    first = state["attempts"][state["pendingDispatch"]["attemptIds"][0]]
    assert (first["class"], first["slot"]) == ("judgment", "architect")

    runtime.mutate(
        cwd,
        state_dir,
        reference(response["card"]),
        lambda current: current["attempts"][first["attemptId"]].update({"status": "pregate_failed"})
        or current.update({"phase": "repair_pending", "retryTicketIds": ["T1"], "pendingDispatch": None}),
    )
    response = runtime.command_status(cwd, state_dir, {"runId": response["card"]["runId"]})
    response = transition(cwd, state_dir, response, "retry", {"diagnosis": "capability"})
    assert response["card"]["phase"] == "blocked"
    assert "escalation is exhausted" in response["card"]["blockedReason"]


def test_bare_retry_uses_the_recorded_failure_kind(tmp_path, cwd, config):
    """A retry without a payload must not silently pick the inert diagnosis.

    The core already recorded why the wave failed; that recorded kind is the
    default, while model replacement remains inside the unchanged OMP role.
    """
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    sealed = seal(cwd, state_dir, prepare(cwd, state_dir, response, config), "producer")
    response = transition(
        cwd,
        state_dir,
        sealed,
        "abandon_dispatch",
        {"confirmedLostSettlement": True, "reason": "session process disappeared and no result exists"},
    )
    assert authoritative(cwd, state_dir, response["card"]["runId"])["lastFailureKind"] == "availability"
    response = transition(cwd, state_dir, response, "retry")
    second = prepare(cwd, state_dir, response, config)
    retried = authoritative(cwd, state_dir, second["card"]["runId"])
    attempt = retried["attempts"][retried["pendingDispatch"]["attemptIds"][0]]
    assert attempt["slot"] == "scout"


def test_partial_pregate_routes_the_failed_ticket_without_an_explicit_retry(tmp_path, cwd, config):
    """A passing sibling carries the wave onward, so the failure never sees `retry`.

    Its escalation therefore has to happen where the failure is recorded, or the
    next attempt repeats the identical dispatch.
    """
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    response = prepare_tickets(cwd, state_dir, response, config, [ticket_named("T1"), ticket_named("T2")])
    response = seal(cwd, state_dir, response, "producer")
    response = settle_producer(tmp_path, cwd, state_dir, response, config)
    state = authoritative(cwd, state_dir, response["card"]["runId"])
    doomed = next(
        attempt for attempt in state["attempts"].values() if attempt["ticketId"] == "T2"
    )
    runtime.mutate(
        cwd,
        state_dir,
        reference(response["card"]),
        lambda current: current["attempts"][doomed["attemptId"]]["ticket"]["verification"].__setitem__(
            0, {"argv": ["python3", "-c", "raise SystemExit(1)"], "cwd": ".", "timeoutSeconds": 10}
        ),
    )
    response = runtime.command_status(cwd, state_dir, {"runId": response["card"]["runId"]})
    response = pregate(cwd, state_dir, response, config)
    after = authoritative(cwd, state_dir, response["card"]["runId"])
    assert after["waves"][-1]["status"] == "pregate_partial"
    assert after["classFloor"]["T2"] == "skilled"
    assert "T1" not in after.get("classFloor", {})


def test_lens_resolving_to_a_producer_model_fails_the_wave_closed(tmp_path, cwd, config):
    """Disjoint slots guarantee different roles, not different models.

    The owner may point two roles at one model, so the last word is a string
    comparison of the witnesses OMP reported — no model knowledge involved.
    """
    collided = model_manifest()
    collided["lens-critic"]["resolvedModel"] = collided["scout"]["resolvedModel"]
    state_dir, response = start(tmp_path, cwd, config, "frontier", models=collided)
    response = admit_frontier(cwd, state_dir, response)
    response = prepare(cwd, state_dir, response, config)
    response = seal(cwd, state_dir, response, "producer")
    response = pregate(cwd, state_dir, settle_producer(tmp_path, cwd, state_dir, response, config), config)
    with pytest.raises(runtime.RuntimeFailure) as error:
        prepare_lenses(cwd, state_dir, response, config)
    assert error.value.code == "independent_reviewer_unavailable"
    assert "Critic" in error.value.message


def test_producer_fallback_to_a_lens_primary_model_fails_before_lens_dispatch(tmp_path, cwd, config):
    models = model_manifest()
    state_dir, response = start(tmp_path, cwd, config, "frontier", models=models)
    response = admit_frontier(cwd, state_dir, response)
    sealed = seal(cwd, state_dir, prepare(cwd, state_dir, response, config), "producer")
    state = authoritative(cwd, state_dir, sealed["card"]["runId"])
    assignment = state["attempts"][sealed["attemptIds"][0]]
    result = normalized_result(tmp_path, assignment, fallback=True)
    result["observedResolvedModel"] = models["lens-critic"]["resolvedModel"]
    settled = runtime.command_record_result(
        cwd,
        state_dir,
        {
            **reference(sealed["card"]),
            "dispatchId": sealed["dispatchId"],
            "toolCallId": "producer-fallback-tool",
            "input": sealed["taskInput"],
            "details": {"results": [result]},
            "content": {assignment["attemptId"]: producer_result()},
            "isError": False,
        },
        config,
    )
    settled = pregate(cwd, state_dir, settled, config)

    with pytest.raises(runtime.RuntimeFailure) as error:
        prepare_lenses(cwd, state_dir, settled, config)

    assert error.value.code == "independent_reviewer_unavailable"
    assert "Critic" in error.value.message


def test_lens_fallback_collision_retries_only_the_colliding_lenses(tmp_path, cwd, config):
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    sealed_lenses, _state = reach_adjudication(tmp_path, cwd, state_dir, response, config)

    collided_model = "fallback/shared-review-model"
    settled = settle_lenses(
        tmp_path,
        cwd,
        state_dir,
        sealed_lenses,
        config,
        observed_models={"Standards": collided_model, "Spec": collided_model},
    )

    assert settled["card"]["phase"] == "lens_prepare_pending"
    state = authoritative(cwd, state_dir, settled["card"]["runId"])
    assert state["retryLensNames"] == ["Standards", "Spec"]
    assert {
        attempt["lens"]: attempt["status"]
        for attempt in state["lensAttempts"].values()
    } == {
        "Standards": "result_invalid",
        "Spec": "result_invalid",
        "Critic": "completed",
    }


def test_lens_fallback_to_the_observed_producer_model_retries_that_lens(tmp_path, cwd, config):
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    sealed_lenses, state = reach_adjudication(tmp_path, cwd, state_dir, response, config)
    producer_model = next(iter(state["attempts"].values()))["observedModel"]

    settled = settle_lenses(
        tmp_path,
        cwd,
        state_dir,
        sealed_lenses,
        config,
        observed_models={"Critic": producer_model},
    )

    assert settled["card"]["phase"] == "lens_prepare_pending"
    persisted = authoritative(cwd, state_dir, settled["card"]["runId"])
    assert persisted["retryLensNames"] == ["Critic"]
    critic = next(
        attempt
        for attempt in persisted["lensAttempts"].values()
        if attempt["lens"] == "Critic"
    )
    assert critic["status"] == "result_invalid"
    assert "producer model" in critic["failureReason"]


def test_lens_settlement_retries_only_the_failed_wave_lens(tmp_path, cwd, config):
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    sealed_lenses, state = reach_adjudication(tmp_path, cwd, state_dir, response, config)
    failed_lens = state["lensAttempts"][sealed_lenses["attemptIds"][0]]["lens"]
    results = []
    content = {}
    for attempt_id in sealed_lenses["attemptIds"]:
        assignment = state["lensAttempts"][attempt_id]
        result = normalized_result(tmp_path, assignment)
        if assignment["lens"] == failed_lens:
            result["error"] = "EBUSY: fixture lens transport unavailable"
        results.append(result)
        content[attempt_id] = {
            "lens": assignment["lens"],
            "summary": "Fixture review complete",
            "reports": [
                {
                    "attemptId": producer_attempt_id,
                    "summary": "Fixture review complete",
                    "findings": [],
                    "verdict": "PASS" if assignment["lens"] == "Critic" else "NO_VERDICT",
                }
                for producer_attempt_id in assignment["producerAttemptIds"]
            ],
        }
    response = runtime.command_record_result(
        cwd,
        state_dir,
        {
            **reference(sealed_lenses["card"]),
            "dispatchId": sealed_lenses["dispatchId"],
            "toolCallId": "lens-tool",
            "input": sealed_lenses["taskInput"],
            "details": {"results": results},
            "content": content,
            "isError": False,
        },
        config,
    )
    assert response["card"]["phase"] == "lens_prepare_pending"
    state = authoritative(cwd, state_dir, response["card"]["runId"])
    assert state["retryLensNames"] == [failed_lens]
    assert {attempt["status"] for attempt in state["lensAttempts"].values()} == {
        "availability_failed",
        "completed",
    }

    prepared_retry = prepare_lenses(cwd, state_dir, response, config)
    assert prepared_retry["card"]["dispatch"]["kind"] == "lenses"
    assert len(prepared_retry["card"]["dispatch"]["actors"]) == 1
    retry_state = authoritative(cwd, state_dir, prepared_retry["card"]["runId"])
    assert retry_state["pendingDispatch"]["lensNames"] == [failed_lens]
    retried = retry_state["lensAttempts"][retry_state["pendingDispatch"]["attemptIds"][0]]
    expected_slot = config["omp"]["lenses"]["slots"][failed_lens]["slot"]
    assert retried["slot"] == expected_slot
    assert len(set(retry_state["waves"][-1]["reviewerSlots"].values())) == 3
    retry_sealed = seal(cwd, state_dir, prepared_retry, "lenses")
    response = settle_lenses(tmp_path, cwd, state_dir, retry_sealed, config)
    assert response["card"]["phase"] == "adjudication_pending"
    response = runtime.command_adjudicate(cwd, state_dir, reference(response["card"]))
    assert response["card"]["phase"] == "accepted"


def test_partial_adjudication_accepts_passing_subset_and_preserves_failed_retry(tmp_path, cwd, config, monkeypatch):
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    response = prepare_tickets(cwd, state_dir, response, config, [ticket_named("T1"), ticket_named("T2")])
    response = seal(cwd, state_dir, response, "producer")
    response = settle_producer(tmp_path, cwd, state_dir, response, config)
    response = prepare_lenses(cwd, state_dir, pregate(cwd, state_dir, response, config), config)
    sealed_lenses = seal(cwd, state_dir, response, "lenses")
    response = settle_lenses(tmp_path, cwd, state_dir, sealed_lenses, config, critic_fail_ticket="T1")
    response = runtime.command_adjudicate(cwd, state_dir, reference(response["card"]))
    state = authoritative(cwd, state_dir, response["card"]["runId"])

    assert response["card"]["phase"] == "accepted"
    assert state["adjudication"]["status"] == "PARTIAL"
    assert state["qualityFailures"] == {"T1": 1}
    producer_statuses = {attempt["ticketId"]: attempt["status"] for attempt in state["attempts"].values()}
    assert producer_statuses == {"T1": "review_failed", "T2": "accepted"}
    assert state["retryTicketIds"] == ["T1"]

    monkeypatch.setattr(runtime, "telemetry_exists", lambda *_args: False)
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "appended\n", ""),
    )
    response = runtime.command_accept(cwd, state_dir, reference(response["card"]), config)
    persisted = authoritative(cwd, state_dir, response["card"]["runId"])
    assert persisted["attempts"][state["adjudication"]["acceptedAttemptIds"][0]]["status"] == "recorded"
    assert persisted["retryTicketIds"] == ["T1"]

    response = transition(
        cwd,
        state_dir,
        response,
        "continue_wave",
        {
            "remainingTicketIds": ["T1"],
            "nextTicketIds": ["T1"],
            "blockedTicketIds": [],
            "evidence": "tracker retains only the failed ticket",
        },
    )
    response = prepare(cwd, state_dir, response, config, ticket_named("T1"))
    assert response["card"]["phase"] == "producer_dispatch_pending"


def test_ticket_contract_rejects_legacy_verification_and_overlapping_writers(tmp_path, cwd, config):
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)

    legacy = mechanical_ticket()
    legacy["verification"] = ["python3 -c 'print(1)'"]
    with pytest.raises(runtime.RuntimeFailure) as error:
        prepare(cwd, state_dir, response, config, legacy)
    assert error.value.code == "ticket_invalid"

    invalid_override = mechanical_ticket()
    invalid_override["max_diff_lines_override"] = config["gates"]["pre_gate"]["max_diff_lines"] - 1
    with pytest.raises(runtime.RuntimeFailure) as error:
        prepare(cwd, state_dir, response, config, invalid_override)
    assert error.value.code == "ticket_invalid"


    missing_scope = writer_ticket("T1", "a.txt")
    del missing_scope["writablePaths"]
    with pytest.raises(runtime.RuntimeFailure) as error:
        prepare(cwd, state_dir, response, config, missing_scope)
    assert error.value.code == "ticket_invalid"

    first = writer_ticket("T1", "src/")
    second = writer_ticket("T2", "src/module.py")
    with pytest.raises(runtime.RuntimeFailure) as error:
        prepare_tickets(cwd, state_dir, response, config, [first, second])
    assert error.value.code == "ticket_overlap"


def test_writer_scope_rejects_a_symbolic_link_parent(tmp_path, config):
    repo = init_git_repo(tmp_path / "repo", {"tracked.txt": "old\n"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target.txt").write_text("old\n", encoding="utf-8")
    (repo / "linked").symlink_to(outside, target_is_directory=True)
    state_dir, response = start(tmp_path, repo, config, "frontier")
    response = admit_frontier(repo, state_dir, response)

    with pytest.raises(runtime.RuntimeFailure) as error:
        prepare(repo, state_dir, response, config, writer_ticket("T1", "linked/target.txt"))
    assert error.value.code == "ticket_invalid"


def test_writer_patch_is_validated_applied_and_rolled_back_after_review_failure(tmp_path, config):
    repo = init_git_repo(tmp_path / "repo", {"target.txt": "old\n"})
    state_dir, response = start(tmp_path, repo, config, "frontier")
    response = admit_frontier(repo, state_dir, response)
    ticket = writer_ticket("T1", "target.txt")
    sealed = seal(repo, state_dir, prepare(repo, state_dir, response, config, ticket), "producer")
    assert all(item["isolated"] is True and "apply" not in item for item in sealed["taskInput"]["tasks"])
    response = settle_producer_patches(
        tmp_path,
        repo,
        state_dir,
        sealed,
        config,
        {"T1": (add_line_patch("target.txt", "old", ["new"]), ["target.txt"])},
    )
    assert (repo / "target.txt").read_text(encoding="utf-8") == "old\nnew\n"

    response = pregate(repo, state_dir, response, config)
    sealed_lenses = seal(repo, state_dir, prepare_lenses(repo, state_dir, response, config), "lenses")
    response = settle_lenses(tmp_path, repo, state_dir, sealed_lenses, config, critic_verdict="FAIL")
    response = runtime.command_adjudicate(repo, state_dir, reference(response["card"]))
    assert response["card"]["phase"] == "repair_pending"
    assert (repo / "target.txt").read_text(encoding="utf-8") == "old\n"


def test_partial_review_rolls_back_only_failed_patch_and_keeps_accepted_patch(tmp_path, config, monkeypatch):
    repo = init_git_repo(tmp_path / "repo", {"accepted.txt": "old\n", "rejected.txt": "old\n"})
    state_dir, response = start(tmp_path, repo, config, "frontier")
    response = admit_frontier(repo, state_dir, response)
    tickets = [
        writer_ticket("T1", "accepted.txt"),
        writer_ticket("T2", "rejected.txt"),
    ]
    sealed = seal(repo, state_dir, prepare_tickets(repo, state_dir, response, config, tickets), "producer")
    response = settle_producer_patches(
        tmp_path,
        repo,
        state_dir,
        sealed,
        config,
        {
            "T1": (add_line_patch("accepted.txt", "old", ["accepted"]), ["accepted.txt"]),
            "T2": (add_line_patch("rejected.txt", "old", ["rejected"]), ["rejected.txt"]),
        },
    )
    response = pregate(repo, state_dir, response, config)
    sealed_lenses = seal(repo, state_dir, prepare_lenses(repo, state_dir, response, config), "lenses")
    response = settle_lenses(tmp_path, repo, state_dir, sealed_lenses, config, critic_fail_ticket="T2")
    response = runtime.command_adjudicate(repo, state_dir, reference(response["card"]))

    assert response["card"]["phase"] == "accepted"
    state = authoritative(repo, state_dir, response["card"]["runId"])
    assert state["adjudication"]["status"] == "PARTIAL"
    assert {attempt["ticketId"]: attempt["status"] for attempt in state["attempts"].values()} == {
        "T1": "accepted",
        "T2": "review_failed",
    }
    assert state["retryTicketIds"] == ["T2"]
    assert (repo / "accepted.txt").read_text(encoding="utf-8") == "old\naccepted\n"
    assert (repo / "rejected.txt").read_text(encoding="utf-8") == "old\n"

    monkeypatch.setattr(runtime, "telemetry_exists", lambda *_args: False)
    original_run = runtime.subprocess.run

    def run_with_telemetry_stub(argv, *args, **kwargs):
        if len(argv) > 1 and argv[1] == str(runtime.TELEMETRY_TOOL):
            return subprocess.CompletedProcess(argv, 0, "appended\n", "")
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(runtime.subprocess, "run", run_with_telemetry_stub)
    response = runtime.command_accept(repo, state_dir, reference(response["card"]), config)
    assert response["card"]["phase"] == "accepted"
    assert (repo / "accepted.txt").read_text(encoding="utf-8") == "old\naccepted\n"
    assert (repo / "rejected.txt").read_text(encoding="utf-8") == "old\n"


def test_cancel_preserves_recorded_patches_from_prior_waves(tmp_path, config, monkeypatch):
    repo = init_git_repo(tmp_path / "repo", {"target.txt": "old\n"})
    state_dir, response = start(tmp_path, repo, config, "frontier")
    response = admit_frontier(repo, state_dir, response)
    sealed = seal(repo, state_dir, prepare(repo, state_dir, response, config, writer_ticket("T1", "target.txt")), "producer")
    response = settle_producer_patches(
        tmp_path,
        repo,
        state_dir,
        sealed,
        config,
        {"T1": (add_line_patch("target.txt", "old", ["new"]), ["target.txt"])},
    )
    response = pregate(repo, state_dir, response, config)
    sealed_lenses = seal(repo, state_dir, prepare_lenses(repo, state_dir, response, config), "lenses")
    response = settle_lenses(tmp_path, repo, state_dir, sealed_lenses, config)
    response = runtime.command_adjudicate(repo, state_dir, reference(response["card"]))

    original_run = runtime.subprocess.run

    def run_with_telemetry_stub(argv, *args, **kwargs):
        if len(argv) > 1 and argv[1] == str(runtime.TELEMETRY_TOOL):
            return subprocess.CompletedProcess(argv, 0, "appended\n", "")
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(runtime, "telemetry_exists", lambda *_args: False)
    monkeypatch.setattr(runtime.subprocess, "run", run_with_telemetry_stub)
    response = runtime.command_accept(repo, state_dir, reference(response["card"]), config)
    response = transition(
        repo,
        state_dir,
        response,
        "continue_wave",
        {
            "remainingTicketIds": ["T2"],
            "nextTicketIds": ["T2"],
            "blockedTicketIds": [],
            "evidence": "fixture frontier has one more ticket",
        },
    )
    assert authoritative(repo, state_dir, response["card"]["runId"])["attempts"][sealed["attemptIds"][0]]["status"] == "recorded"
    assert (repo / "target.txt").read_text(encoding="utf-8") == "old\nnew\n"

    response = transition(repo, state_dir, response, "cancel")

    assert response["card"]["phase"] == "cancelled"
    assert (repo / "target.txt").read_text(encoding="utf-8") == "old\nnew\n"



def test_lens_availability_exhaustion_rolls_back_only_current_wave(tmp_path, config, monkeypatch):
    repo = init_git_repo(tmp_path / "repo", {"first.txt": "old\n", "second.txt": "old\n"})
    state_dir, response = start(tmp_path, repo, config, "frontier")
    response = admit_frontier(repo, state_dir, response)

    first_dispatch = seal(
        repo,
        state_dir,
        prepare(repo, state_dir, response, config, writer_ticket("T1", "first.txt")),
        "producer",
    )
    response = settle_producer_patches(
        tmp_path,
        repo,
        state_dir,
        first_dispatch,
        config,
        {"T1": (add_line_patch("first.txt", "old", ["accepted"]), ["first.txt"])},
    )
    response = pregate(repo, state_dir, response, config)
    first_lenses = seal(repo, state_dir, prepare_lenses(repo, state_dir, response, config), "lenses")
    response = settle_lenses(tmp_path, repo, state_dir, first_lenses, config)
    response = runtime.command_adjudicate(repo, state_dir, reference(response["card"]))

    original_run = runtime.subprocess.run

    def run_with_telemetry_stub(argv, *args, **kwargs):
        if len(argv) > 1 and argv[1] == str(runtime.TELEMETRY_TOOL):
            return subprocess.CompletedProcess(argv, 0, "appended\n", "")
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(runtime, "telemetry_exists", lambda *_args: False)
    monkeypatch.setattr(runtime.subprocess, "run", run_with_telemetry_stub)
    response = runtime.command_accept(repo, state_dir, reference(response["card"]), config)
    response = transition(
        repo,
        state_dir,
        response,
        "continue_wave",
        {
            "remainingTicketIds": ["T2"],
            "nextTicketIds": ["T2"],
            "blockedTicketIds": [],
            "evidence": "fixture frontier advances to T2",
        },
    )

    second_dispatch = seal(
        repo,
        state_dir,
        prepare(repo, state_dir, response, config, writer_ticket("T2", "second.txt")),
        "producer",
    )
    response = settle_producer_patches(
        tmp_path,
        repo,
        state_dir,
        second_dispatch,
        config,
        {"T2": (add_line_patch("second.txt", "old", ["rejected"]), ["second.txt"])},
    )
    response = pregate(repo, state_dir, response, config)
    second_lenses = seal(repo, state_dir, prepare_lenses(repo, state_dir, response, config), "lenses")
    response = settle_lenses(
        tmp_path,
        repo,
        state_dir,
        second_lenses,
        config,
        availability_error="EBUSY: fixture lens transport unavailable",
    )
    assert response["card"]["phase"] == "lens_prepare_pending"

    second_lenses = seal(repo, state_dir, prepare_lenses(repo, state_dir, response, config), "lenses")
    response = settle_lenses(
        tmp_path,
        repo,
        state_dir,
        second_lenses,
        config,
        availability_error="EBUSY: fixture lens transport unavailable",
    )

    assert response["card"]["phase"] == "blocked"
    assert (repo / "first.txt").read_text(encoding="utf-8") == "old\naccepted\n"
    assert (repo / "second.txt").read_text(encoding="utf-8") == "old\n"

@pytest.mark.parametrize(
    ("patch", "changed_files", "code"),
    (
        (add_line_patch("outside.txt", "old", ["new"]), ["outside.txt"], "patch_scope_violation"),
        (add_line_patch("target.txt", "old", ["new"]), ["other.txt"], "changed_files_mismatch"),
        (
            b"diff --git a/target.txt b/target.txt\n"
            b"deleted file mode 100644\n"
            b"--- a/target.txt\n"
            b"+++ /dev/null\n"
            b"@@ -1 +0,0 @@\n"
            b"-old\n",
            ["target.txt"],
            "patch_operation_forbidden",
        ),
        (
            b"diff --git a/nested b/nested\n"
            b"new file mode 160000\n"
            b"index 0000000..0123456\n"
            b"--- /dev/null\n"
            b"+++ b/nested\n"
            b"@@ -0,0 +1 @@\n"
            b"+Subproject commit 0123456789012345678901234567890123456789\n",
            ["nested"],
            "patch_operation_forbidden",
        ),
    ),
)
def test_invalid_or_unauthorized_patch_is_recorded_without_reaching_working_tree(tmp_path, config, patch, changed_files, code):
    repo = init_git_repo(tmp_path / "repo", {"target.txt": "old\n", "outside.txt": "old\n"})
    state_dir, response = start(tmp_path, repo, config, "frontier")
    response = admit_frontier(repo, state_dir, response)
    sealed = seal(repo, state_dir, prepare(repo, state_dir, response, config, writer_ticket("T1", "target.txt")), "producer")
    response = settle_producer_patches(tmp_path, repo, state_dir, sealed, config, {"T1": (patch, changed_files)})
    state = authoritative(repo, state_dir, response["card"]["runId"])

    attempt = state["attempts"][sealed["attemptIds"][0]]
    assert response["card"]["phase"] == "repair_pending"
    assert attempt["status"] == "result_invalid"
    assert attempt["failureCode"] == code
    assert state["retryTicketIds"] == ["T1"]
    assert (repo / "target.txt").read_text(encoding="utf-8") == "old\n"
    assert (repo / "outside.txt").read_text(encoding="utf-8") == "old\n"


def test_partial_producer_settlement_applies_only_the_valid_sibling_patch(tmp_path, config):
    repo = init_git_repo(tmp_path / "repo", {"a.txt": "old-a\n", "b.txt": "old-b\n"})
    state_dir, response = start(tmp_path, repo, config, "frontier")
    response = admit_frontier(repo, state_dir, response)
    tickets = [writer_ticket("T1", "a.txt"), writer_ticket("T2", "b.txt")]
    sealed = seal(repo, state_dir, prepare_tickets(repo, state_dir, response, config, tickets), "producer")
    response = settle_producer_patches(
        tmp_path,
        repo,
        state_dir,
        sealed,
        config,
        {
            "T1": (add_line_patch("a.txt", "old-a", ["new-a"]), ["a.txt"]),
            "T2": (add_line_patch("b.txt", "wrong-context", ["new-b"]), ["b.txt"]),
        },
    )
    state = authoritative(repo, state_dir, response["card"]["runId"])
    attempts = {attempt["ticketId"]: attempt for attempt in state["attempts"].values()}

    assert response["card"]["phase"] == "pregate_pending"
    assert attempts["T1"]["status"] == "completed"
    assert attempts["T2"]["status"] == "result_invalid"
    assert attempts["T2"]["failureCode"] == "patch_conflict"
    assert attempts["T2"]["artifact"]["bytes"] > 0
    assert attempts["T2"]["patchArtifact"]["bytes"] > 0
    assert attempts["T2"]["reportedChangedFiles"] == ["b.txt"]
    assert state["retryTicketIds"] == ["T2"]
    assert state["waves"][-1]["candidateAttemptIds"] == [attempts["T1"]["attemptId"]]
    assert (repo / "a.txt").read_text(encoding="utf-8") == "old-a\nnew-a\n"
    assert (repo / "b.txt").read_text(encoding="utf-8") == "old-b\n"


def test_pregate_selectively_rolls_back_only_failed_candidate_patch(tmp_path, config):
    repo = init_git_repo(tmp_path / "repo", {"a.txt": "old-a\n", "b.txt": "old-b\n"})
    config["gates"]["pre_gate"]["max_diff_lines"] = 1
    state_dir, response = start(tmp_path, repo, config, "frontier")
    response = admit_frontier(repo, state_dir, response)
    tickets = [writer_ticket("T1", "a.txt"), writer_ticket("T2", "b.txt")]
    sealed = seal(repo, state_dir, prepare_tickets(repo, state_dir, response, config, tickets), "producer")
    response = settle_producer_patches(
        tmp_path,
        repo,
        state_dir,
        sealed,
        config,
        {
            "T1": (add_line_patch("a.txt", "old-a", ["new-a"]), ["a.txt"]),
            "T2": (add_line_patch("b.txt", "old-b", ["new-b-1", "new-b-2"]), ["b.txt"]),
        },
    )
    response = pregate(repo, state_dir, response, config)
    state = authoritative(repo, state_dir, response["card"]["runId"])

    assert response["card"]["phase"] == "lens_prepare_pending"
    assert state["pregate"]["status"] == "PARTIAL"
    assert state["qualityFailures"] == {"T2": 1}
    assert state["retryTicketIds"] == ["T2"]
    assert (repo / "a.txt").read_text(encoding="utf-8") == "old-a\nnew-a\n"
    assert (repo / "b.txt").read_text(encoding="utf-8") == "old-b\n"


def test_verification_argv_is_executed_without_a_shell(tmp_path, cwd, config):
    marker = tmp_path / "shell-injection"
    ticket = mechanical_ticket()
    ticket["verification"] = [{
        "argv": ["python3", "-c", "import sys; assert sys.argv[1].startswith(';')", f";touch {marker}"],
        "cwd": ".",
        "timeoutSeconds": 10,
    }]
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    sealed = seal(cwd, state_dir, prepare(cwd, state_dir, response, config, ticket), "producer")
    response = pregate(cwd, state_dir, settle_producer(tmp_path, cwd, state_dir, sealed, config), config)
    assert response["card"]["phase"] == "lens_prepare_pending"
    assert not marker.exists()


def test_ui_evidence_rejects_unissued_challenge_token(tmp_path, cwd, config):
    ticket = mechanical_ticket(ui_live=True)
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = admit_frontier(cwd, state_dir, response)
    sealed = seal(cwd, state_dir, prepare(cwd, state_dir, response, config, ticket), "producer")
    response = settle_producer(tmp_path, cwd, state_dir, sealed, config)
    with pytest.raises(runtime.RuntimeFailure) as error:
        runtime.command_record_evidence(
            cwd,
            state_dir,
            {
                **reference(response["card"]),
                "toolCallId": "browser-call",
                "tool": "browser",
                "success": True,
                "invocation": {
                    "action": "open",
                    "name": "not-issued",
                    "url": "http://fixture.test",
                },
                "details": {},
                "content": "rendered",
                "attemptIds": sealed["attemptIds"],
                "challengeToken": "not-issued",
                "stage": "open",
            },
        )
    assert error.value.code == "evidence_invalid"


def test_patch_application_journal_recovers_after_state_write_failure(tmp_path, config, monkeypatch):
    repo = init_git_repo(tmp_path / "repo", {"target.txt": "old\n"})
    state_dir, response = start(tmp_path, repo, config, "frontier")
    response = admit_frontier(repo, state_dir, response)
    sealed = seal(repo, state_dir, prepare(repo, state_dir, response, config, writer_ticket("T1", "target.txt")), "producer")
    patches = {"T1": (add_line_patch("target.txt", "old", ["new"]), ["target.txt"])}

    real_write_state = runtime.write_state
    monkeypatch.setattr(runtime, "write_state", lambda *_args: (_ for _ in ()).throw(OSError("simulated state write failure")))
    with pytest.raises(OSError, match="simulated state write failure"):
        settle_producer_patches(tmp_path, repo, state_dir, sealed, config, patches)
    assert (repo / "target.txt").read_text(encoding="utf-8") == "old\nnew\n"

    monkeypatch.setattr(runtime, "write_state", real_write_state)
    response = settle_producer_patches(tmp_path, repo, state_dir, sealed, config, patches)
    assert response["card"]["phase"] == "pregate_pending"
    assert (repo / "target.txt").read_text(encoding="utf-8") == "old\nnew\n"


def test_abandon_dispatch_rolls_back_patch_orphaned_by_state_write_failure(tmp_path, config, monkeypatch):
    repo = init_git_repo(tmp_path / "repo", {"target.txt": "old\n"})
    state_dir, response = start(tmp_path, repo, config, "frontier")
    response = admit_frontier(repo, state_dir, response)
    sealed = seal(
        repo,
        state_dir,
        prepare(repo, state_dir, response, config, writer_ticket("T1", "target.txt")),
        "producer",
    )
    patches = {"T1": (add_line_patch("target.txt", "old", ["new"]), ["target.txt"])}

    real_write_state = runtime.write_state
    monkeypatch.setattr(
        runtime,
        "write_state",
        lambda *_args: (_ for _ in ()).throw(OSError("simulated state write failure")),
    )
    with pytest.raises(OSError, match="simulated state write failure"):
        settle_producer_patches(tmp_path, repo, state_dir, sealed, config, patches)
    assert (repo / "target.txt").read_text(encoding="utf-8") == "old\nnew\n"

    monkeypatch.setattr(runtime, "write_state", real_write_state)
    observed = runtime.command_status(repo, state_dir, {"runId": sealed["card"]["runId"]})
    abandoned = runtime.command_transition(
        repo,
        state_dir,
        {
            **reference(observed["card"]),
            "action": "abandon_dispatch",
            "payload": {
                "confirmedLostSettlement": True,
                "reason": "adapter lost the failed settlement",
            },
        },
    )

    assert abandoned["card"]["phase"] == "repair_pending"
    assert (repo / "target.txt").read_text(encoding="utf-8") == "old\n"
    journals = list(
        (runtime.state_paths(repo, state_dir, sealed["card"]["runId"])[0].with_suffix(".artifacts")).glob(
            "wave-*.apply.json"
        )
    )
    assert len(journals) == 1
    assert json.loads(journals[0].read_text(encoding="utf-8"))["status"] == "rolled_back"


def test_patch_rollback_journal_recovers_after_state_write_failure(tmp_path, config, monkeypatch):
    repo = init_git_repo(tmp_path / "repo", {"target.txt": "old\n"})
    config["gates"]["pre_gate"]["max_diff_lines"] = 0
    state_dir, response = start(tmp_path, repo, config, "frontier")
    response = admit_frontier(repo, state_dir, response)
    sealed = seal(repo, state_dir, prepare(repo, state_dir, response, config, writer_ticket("T1", "target.txt")), "producer")
    response = settle_producer_patches(
        tmp_path,
        repo,
        state_dir,
        sealed,
        config,
        {"T1": (add_line_patch("target.txt", "old", ["new"]), ["target.txt"])},
    )

    real_write_state = runtime.write_state
    monkeypatch.setattr(runtime, "write_state", lambda *_args: (_ for _ in ()).throw(OSError("simulated state write failure")))
    with pytest.raises(OSError, match="simulated state write failure"):
        pregate(repo, state_dir, response, config)
    assert (repo / "target.txt").read_text(encoding="utf-8") == "old\n"

    monkeypatch.setattr(runtime, "write_state", real_write_state)
    response = pregate(repo, state_dir, response, config)
    assert response["card"]["phase"] == "repair_pending"
    assert (repo / "target.txt").read_text(encoding="utf-8") == "old\n"


def test_replacement_recovers_mixed_patch_state_after_partial_pregate_write_failure(
    tmp_path,
    config,
    monkeypatch,
):
    repo = init_git_repo(tmp_path / "repo", {"failed.txt": "old\n", "passed.txt": "old\n"})
    failed = writer_ticket(
        "T1",
        "failed.txt",
        verification=[{"argv": ["python3", "-c", "raise SystemExit(1)"], "cwd": ".", "timeoutSeconds": 10}],
    )
    passed = writer_ticket(
        "T2",
        "passed.txt",
        verification=[{"argv": ["python3", "-c", "raise SystemExit(0)"], "cwd": ".", "timeoutSeconds": 10}],
    )
    state_dir, response = start(tmp_path, repo, config, "frontier")
    response = admit_frontier(repo, state_dir, response)
    sealed = seal(repo, state_dir, prepare_tickets(repo, state_dir, response, config, [failed, passed]), "producer")
    settled = settle_producer_patches(
        tmp_path,
        repo,
        state_dir,
        sealed,
        config,
        {
            "T1": (add_line_patch("failed.txt", "old", ["failed-new"]), ["failed.txt"]),
            "T2": (add_line_patch("passed.txt", "old", ["passed-new"]), ["passed.txt"]),
        },
    )

    real_write_state = runtime.write_state
    monkeypatch.setattr(runtime, "write_state", lambda *_args: (_ for _ in ()).throw(OSError("simulated state write failure")))
    with pytest.raises(OSError, match="simulated state write failure"):
        pregate(repo, state_dir, settled, config)
    assert (repo / "failed.txt").read_text(encoding="utf-8") == "old\n"
    assert (repo / "passed.txt").read_text(encoding="utf-8") == "old\npassed-new\n"

    monkeypatch.setattr(runtime, "write_state", real_write_state)
    monkeypatch.setattr(runtime, "runtime_fingerprint", lambda: "replacement-runtime")
    replacement = runtime.command_start(
        repo,
        state_dir,
        {
            "entry": "frontier",
            "objective": "replace after mixed rollback state",
            "sessionId": "replacement-session",
            "manifestFingerprint": runtime.trusted_manifest_fingerprint(repo, config),
            "models": model_manifest(),
        },
        config,
    )

    assert replacement["card"]["phase"] == "frontier_admission"
    assert (repo / "failed.txt").read_text(encoding="utf-8") == "old\n"
    assert (repo / "passed.txt").read_text(encoding="utf-8") == "old\n"


def test_sweep_runs_sealed_two_wave_dag_to_terminal_completion(
    tmp_path,
    cwd,
    config,
    canonical_sweep_admission,
    monkeypatch,
):
    state_dir, response = start(tmp_path, cwd, config, "sweep")
    response = transition(cwd, state_dir, response, "admit_sweep", canonical_sweep_admission)
    admitted = response["card"]
    hashes = {field: admitted[field] for field in ("ledgerHash", "dagHash")}

    assert admitted["phase"] == "ready"
    assert admitted["acceptedTicketIds"] == []
    assert admitted["remainingTicketIds"] == ["T1", "T2", "T3"]
    assert admitted["readyTicketIds"] == ["T1", "T2"]
    assert admitted["blockedTicketIds"] == ["T3"]
    assert all(len(value) == 64 and set(value) <= set("0123456789abcdef") for value in hashes.values())

    response = prepare_sweep(cwd, state_dir, response, config)
    first_wave = authoritative(cwd, state_dir, response["card"]["runId"])["waves"][-1]
    assert first_wave["ticketIds"] == ["T1", "T2"]
    assert {field: first_wave[field] for field in hashes} == hashes

    monkeypatch.setattr(runtime, "telemetry_exists", lambda *_args: False)
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "appended\n", ""),
    )
    response = accept_prepared_sweep_wave(tmp_path, cwd, state_dir, response, config)
    first_accepted = response["card"]
    assert first_accepted["phase"] == "accepted"
    assert first_accepted["nextActions"] == ["continue_wave", "cancel"]
    assert first_accepted["acceptedTicketIds"] == ["T1", "T2"]
    assert first_accepted["remainingTicketIds"] == ["T3"]
    assert first_accepted["readyTicketIds"] == ["T3"]
    assert {field: first_accepted[field] for field in hashes} == hashes

    with pytest.raises(runtime.RuntimeFailure) as synthesis_error:
        transition(cwd, state_dir, response, "begin_synthesis")
    assert synthesis_error.value.code == "sweep_not_exhausted"
    with pytest.raises(runtime.RuntimeFailure) as scheduler_error:
        transition(cwd, state_dir, response, "continue_wave", {"nextTicketIds": ["T3"]})
    assert scheduler_error.value.code == "sweep_payload_forbidden"

    response = transition(cwd, state_dir, response, "continue_wave")
    assert response["card"]["phase"] == "ready"
    assert response["card"]["readyTicketIds"] == ["T3"]
    assert {field: response["card"][field] for field in hashes} == hashes

    response = prepare_sweep(cwd, state_dir, response, config)
    second_wave = authoritative(cwd, state_dir, response["card"]["runId"])["waves"][-1]
    assert second_wave["ticketIds"] == ["T3"]
    assert {field: second_wave[field] for field in hashes} == hashes
    response = accept_prepared_sweep_wave(tmp_path, cwd, state_dir, response, config)
    assert response["card"]["nextActions"] == ["begin_synthesis", "cancel"]
    assert response["card"]["acceptedTicketIds"] == ["T1", "T2", "T3"]
    assert response["card"]["remainingTicketIds"] == []
    assert response["card"]["readyTicketIds"] == []
    assert response["card"]["blockedTicketIds"] == []

    response = transition(cwd, state_dir, response, "begin_synthesis")
    response = transition(cwd, state_dir, response, "complete")
    terminal = response["card"]
    assert terminal["phase"] == "completed"
    assert {field: terminal[field] for field in hashes} == hashes
    assert runtime.command_status(cwd, state_dir, {"runId": terminal["runId"]})["card"] == terminal


def test_partial_sweep_acceptance_records_only_passing_subset_and_keeps_dag_retry(tmp_path, cwd, config, canonical_sweep_admission, monkeypatch):
    state_dir, response = start(tmp_path, cwd, config, "sweep")
    response = transition(cwd, state_dir, response, "admit_sweep", canonical_sweep_admission)
    response = prepare_sweep(cwd, state_dir, response, config)
    response = seal(cwd, state_dir, response, "producer")
    response = settle_producer(tmp_path, cwd, state_dir, response, config)
    response = pregate(cwd, state_dir, response, config)
    response = prepare_lenses(cwd, state_dir, response, config)
    sealed_lenses = seal(cwd, state_dir, response, "lenses")
    response = settle_lenses(tmp_path, cwd, state_dir, sealed_lenses, config, critic_fail_ticket="T1")
    response = runtime.command_adjudicate(cwd, state_dir, reference(response["card"]))
    assert response["card"]["phase"] == "accepted"
    assert response["card"]["retryTicketIds"] == ["T1"]

    monkeypatch.setattr(runtime, "telemetry_exists", lambda *_args: False)
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "appended\n", ""),
    )
    response = runtime.command_accept(cwd, state_dir, reference(response["card"]), config)
    assert response["card"]["acceptedTicketIds"] == ["T2"]
    assert response["card"]["remainingTicketIds"] == ["T1", "T3"]
    assert response["card"]["readyTicketIds"] == ["T1"]
    assert response["card"]["blockedTicketIds"] == ["T3"]

    response = transition(cwd, state_dir, response, "continue_wave")
    assert response["card"]["phase"] == "ready"
    response = prepare_sweep(cwd, state_dir, response, config)
    next_wave = authoritative(cwd, state_dir, response["card"]["runId"])["waves"][-1]
    assert next_wave["ticketIds"] == ["T1"]


def test_sweep_continue_blocks_when_a_ready_retry_is_exhausted(
    tmp_path,
    cwd,
    config,
    canonical_sweep_admission,
    monkeypatch,
):
    admission = copy.deepcopy(canonical_sweep_admission)
    next(ticket for ticket in admission["tickets"] if ticket["ticketId"] == "T3")["dependsOn"] = ["T2"]
    state_dir, response = start(tmp_path, cwd, config, "sweep")
    response = transition(cwd, state_dir, response, "admit_sweep", admission)

    def review_wave_with_t1_failure(current):
        current = prepare_sweep(cwd, state_dir, current, config)
        current = seal(cwd, state_dir, current, "producer")
        current = settle_producer(tmp_path, cwd, state_dir, current, config)
        current = pregate(cwd, state_dir, current, config)
        current = prepare_lenses(cwd, state_dir, current, config)
        sealed_lenses = seal(cwd, state_dir, current, "lenses")
        current = settle_lenses(
            tmp_path,
            cwd,
            state_dir,
            sealed_lenses,
            config,
            critic_fail_ticket="T1",
        )
        return runtime.command_adjudicate(cwd, state_dir, reference(current["card"]))

    monkeypatch.setattr(runtime, "telemetry_exists", lambda *_args: False)
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "appended\n", ""),
    )
    response = review_wave_with_t1_failure(response)
    response = runtime.command_accept(cwd, state_dir, reference(response["card"]), config)
    assert response["card"]["readyTicketIds"] == ["T1", "T3"]

    response = transition(cwd, state_dir, response, "continue_wave")
    response = review_wave_with_t1_failure(response)
    response = runtime.command_accept(cwd, state_dir, reference(response["card"]), config)
    assert response["card"]["acceptedTicketIds"] == ["T2", "T3"]
    assert response["card"]["remainingTicketIds"] == ["T1"]

    response = transition(cwd, state_dir, response, "continue_wave")
    assert response["card"]["phase"] == "blocked"
    assert response["card"]["blockedReason"] == "retry limit reached for: T1"


def test_sweep_admission_rejects_extra_authority(tmp_path, cwd, config, canonical_sweep_admission):
    state_dir, response = start(tmp_path, cwd, config, "sweep")
    admission = copy.deepcopy(canonical_sweep_admission)
    admission["dependencies"] = {"T1": [], "T2": [], "T3": ["T1", "T2"]}

    with pytest.raises(runtime.RuntimeFailure) as error:
        transition(cwd, state_dir, response, "admit_sweep", admission)

    assert error.value.code == "sweep_admission_invalid"
    card = runtime.command_status(cwd, state_dir, {"runId": response["card"]["runId"]})["card"]
    assert card["phase"] == "sweep_admission"
    assert "ledgerHash" not in card


@pytest.mark.parametrize("field", ["closed", "acceptancePredecided"])
def test_sweep_admission_requires_true_witness_fields(
    tmp_path,
    cwd,
    config,
    canonical_sweep_admission,
    field,
):
    state_dir, response = start(tmp_path, cwd, config, "sweep")
    admission = copy.deepcopy(canonical_sweep_admission)
    admission["witness"][field] = False

    with pytest.raises(runtime.RuntimeFailure) as error:
        transition(cwd, state_dir, response, "admit_sweep", admission)

    assert error.value.code == "sweep_witness_invalid"
    assert runtime.command_status(cwd, state_dir, {"runId": response["card"]["runId"]})["card"]["nextActions"] == [
        "admit_sweep",
        "cancel",
    ]


@pytest.mark.parametrize("fault", ["cycle", "missing_ticket", "missing_depends_on"])
def test_sweep_admission_rejects_noncanonical_dependency_graph(
    tmp_path,
    cwd,
    config,
    canonical_sweep_admission,
    fault,
):
    state_dir, response = start(tmp_path, cwd, config, "sweep")
    admission = copy.deepcopy(canonical_sweep_admission)
    if fault == "cycle":
        admission["tickets"][0]["dependsOn"] = ["T3"]
    elif fault == "missing_ticket":
        admission["tickets"][2]["dependsOn"] = ["T1", "MISSING"]
    else:
        del admission["tickets"][2]["dependsOn"]

    with pytest.raises(runtime.RuntimeFailure) as error:
        transition(cwd, state_dir, response, "admit_sweep", admission)

    assert error.value.code == "dag_invalid"
    assert runtime.command_status(cwd, state_dir, {"runId": response["card"]["runId"]})["card"]["phase"] == "sweep_admission"


@pytest.mark.parametrize(
    ("integration", "writer_ticket_required"),
    [("aggregate", True), ("disjoint_patches", False)],
)
def test_sweep_admission_enforces_integration_writer_contract(
    tmp_path,
    config,
    canonical_sweep_admission,
    integration,
    writer_ticket_required,
):
    state_dir, response = start(tmp_path, tmp_path, config, "sweep")
    admission = copy.deepcopy(canonical_sweep_admission)
    admission["witness"]["integration"] = integration
    if writer_ticket_required:
        admission["tickets"][0] = {
            **writer_ticket("T1", "changed.txt", verification=mechanical_ticket()["verification"]),
            "dependsOn": [],
        }

    with pytest.raises(runtime.RuntimeFailure) as error:
        transition(tmp_path, state_dir, response, "admit_sweep", admission)

    assert error.value.code == "sweep_witness_invalid"
    assert runtime.command_status(tmp_path, state_dir, {"runId": response["card"]["runId"]})["card"]["phase"] == "sweep_admission"


def test_sweep_admits_disjoint_writer_paths(tmp_path, config, canonical_sweep_admission):
    state_dir, response = start(tmp_path, tmp_path, config, "sweep")
    admission = copy.deepcopy(canonical_sweep_admission)
    admission["witness"]["integration"] = "disjoint_patches"
    verification = mechanical_ticket()["verification"]
    admission["tickets"][0] = {
        **writer_ticket("T1", "src/one.py", verification=verification),
        "dependsOn": [],
    }
    admission["tickets"][1] = {
        **writer_ticket("T2", "src/two.py", verification=verification),
        "dependsOn": [],
    }

    response = transition(tmp_path, state_dir, response, "admit_sweep", admission)

    card = response["card"]
    assert card["phase"] == "ready"
    assert card["readyTicketIds"] == ["T1", "T2"]
    assert card["blockedTicketIds"] == ["T3"]
    assert card["ledgerHash"] != card["dagHash"]


def test_sweep_rejects_incomparable_writer_path_conflict(tmp_path, config, canonical_sweep_admission):
    state_dir, response = start(tmp_path, tmp_path, config, "sweep")
    admission = copy.deepcopy(canonical_sweep_admission)
    admission["witness"]["integration"] = "disjoint_patches"
    verification = mechanical_ticket()["verification"]
    admission["tickets"][0] = {
        **writer_ticket("T1", "src/", verification=verification),
        "dependsOn": [],
    }
    admission["tickets"][1] = {
        **writer_ticket("T2", "src/worker.py", verification=verification),
        "dependsOn": [],
    }

    with pytest.raises(runtime.RuntimeFailure) as error:
        transition(tmp_path, state_dir, response, "admit_sweep", admission)

    assert error.value.code == "ticket_overlap"
    assert runtime.command_status(tmp_path, state_dir, {"runId": response["card"]["runId"]})["card"]["phase"] == "sweep_admission"


def test_sweep_prepare_rejects_caller_owned_tickets(tmp_path, cwd, config, canonical_sweep_admission):
    state_dir, response = start(tmp_path, cwd, config, "sweep")
    response = transition(cwd, state_dir, response, "admit_sweep", canonical_sweep_admission)
    admitted = response["card"]

    with pytest.raises(runtime.RuntimeFailure) as error:
        runtime.command_prepare(
            cwd,
            state_dir,
            {**reference(admitted), "tickets": [ticket_named("T1")]},
            config,
        )

    assert error.value.code == "sweep_payload_forbidden"
    assert runtime.command_status(cwd, state_dir, {"runId": admitted["runId"]})["card"] == admitted



def test_usage_tokens_returns_zero_for_complete_canonical_zero_usage():
    assert runtime.usage_tokens(
        {"usage": {"input": 0, "output": 0, "cacheWrite": 0, "cacheRead": 0}}
    ) == 0


def test_usage_tokens_excludes_cache_reads_from_canonical_total():
    assert runtime.usage_tokens(
        {"usage": {"input": 2, "output": 3, "cacheWrite": 5, "cacheRead": 89}}
    ) == 10


def test_usage_tokens_accepts_equal_canonical_aliases():
    assert runtime.usage_tokens(
        {
            "usage": {
                "input": 2,
                "inputTokens": 2,
                "output": 3,
                "output_tokens": 3,
                "cacheWrite": 5,
                "cache_write_input_tokens": 5,
                "cacheRead": 89,
                "cachedInputTokens": 89,
            }
        }
    ) == 10


def test_usage_tokens_rejects_conflicting_canonical_aliases():
    assert runtime.usage_tokens(
        {
            "usage": {
                "input": 2,
                "output": 3,
                "outputTokens": 4,
                "cacheWrite": 5,
                "cacheRead": 89,
            }
        }
    ) is None


@pytest.mark.parametrize(
    "usage",
    [
        {"input": -1, "output": 3, "cacheWrite": 5, "cacheRead": 89},
        {"input": 2, "output": "3", "cacheWrite": 5, "cacheRead": 89},
    ],
    ids=["negative", "invalid"],
)
def test_usage_tokens_rejects_invalid_or_negative_canonical_values(usage):
    assert runtime.usage_tokens({"usage": usage}) is None


def test_usage_tokens_uses_positive_top_level_fallback():
    assert runtime.usage_tokens({"tokens": 11}) == 11


@pytest.mark.parametrize("result", [{"tokens": 0}, {}], ids=["zero", "absent"])
def test_usage_tokens_returns_none_for_zero_or_absent_top_level_fallback(result):
    assert runtime.usage_tokens(result) is None


def test_dispatch_card_exposes_one_opaque_pending_actor_without_renaming_sealed_input(tmp_path, cwd, config):
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    response = prepare(cwd, state_dir, admit_frontier(cwd, state_dir, response), config)
    state = authoritative(cwd, state_dir, response["card"]["runId"])
    assignment = state["attempts"][state["pendingDispatch"]["attemptIds"][0]]
    input_name = state["pendingDispatch"]["taskInput"]["tasks"][0]["name"]

    status = runtime.command_status(cwd, state_dir, {"runId": response["card"]["runId"]})
    dispatch = status["card"]["dispatch"]
    assert dispatch["status"] == "prepared"
    assert len(dispatch["actors"]) == 1
    actor = dispatch["actors"][0]
    assert set(actor) == {
        "dispatchName",
        "attemptId",
        "ticketId",
        "role",
        "lens",
        "attemptOrdinal",
        "slotRole",
        "declaredModel",
        "observedModel",
        "modelWitness",
        "status",
        "tokens",
    }
    assert actor["dispatchName"] == input_name
    assert actor["dispatchName"] != assignment["ticket"]["OBJECTIVE"]
    assert actor["slotRole"] == config["omp"]["slots"][assignment["slot"]]["alias"]
    assert actor["declaredModel"] == assignment["declaredModel"]
    assert actor["modelWitness"] == "DECLARED_ONLY"
    assert actor["status"] == "prepared"
    assert actor["tokens"] == "n/a"

    sealed = seal(cwd, state_dir, status, "producer")
    assert sealed["taskInput"]["tasks"][0]["name"] == input_name


def test_successful_lenses_become_accepted_only_at_adjudication(tmp_path, cwd, config):
    state_dir, response = start(tmp_path, cwd, config, "frontier")
    sealed_lenses, _ = reach_adjudication(
        tmp_path,
        cwd,
        state_dir,
        admit_frontier(cwd, state_dir, response),
        config,
    )
    completed_lenses = settle_lenses(tmp_path, cwd, state_dir, sealed_lenses, config)
    before_adjudication = authoritative(cwd, state_dir, completed_lenses["card"]["runId"])
    assert {attempt["status"] for attempt in before_adjudication["attempts"].values()} == {"completed"}
    assert {attempt["status"] for attempt in before_adjudication["lensAttempts"].values()} == {"completed"}

    adjudicated = runtime.command_adjudicate(cwd, state_dir, reference(completed_lenses["card"]))
    accepted = authoritative(cwd, state_dir, adjudicated["card"]["runId"])
    assert {attempt["status"] for attempt in accepted["attempts"].values()} == {"accepted"}
    assert {attempt["status"] for attempt in accepted["lensAttempts"].values()} == {"accepted"}
