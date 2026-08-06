import { createHash } from "node:crypto";
import { mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { YAML } from "bun";
import type { ExtensionAPI, ExtensionUIContext } from "@oh-my-pi/pi-coding-agent";

/**
 * Pocock deliberately keeps its policy and durable state in omp_runtime.py.
 * This extension is only an OMP-native adapter: it mirrors state cards,
 * seals the one legal task transport call, and returns observed runtime facts.
 */

const STATE_ENTRY = "pocock-state";
const DISPATCH_WIDGET = "pocock-dispatch";
const DISPATCH_CONTEXT = "Pocock sealed dispatch";
const DISPATCH_PLACEHOLDER = "Pocock sealed dispatch placeholder";
const CORE_PROTOCOL_VERSION = 1;
const CORE_TIMEOUT_MS = 120000;
const PREGATE_CORE_TIMEOUT_MS = 3660000;
const TERMINAL_PHASES: Readonly<Record<string, true>> = {
	completed: true,
	cancelled: true,
};
const DISPATCH_PHASES: Readonly<Record<string, "producer" | "lenses">> = {
	producer_dispatch_pending: "producer",
	lens_dispatch_pending: "lenses",
};

type RuntimePin = { path: string; sha256: string };
const pinnedRuntimes = new Map<string, RuntimePin>();
type JsonRecord = Record<string, unknown>;

type RuntimeContext = {
	cwd: string;
	hasUI?: boolean;
	ui?: Pick<ExtensionUIContext, "setWidget">;
	sessionManager: {
		getSessionId(): string;
		getBranch(): unknown[];
	};
	models: {
		resolve(spec: string): unknown;
	};
};

interface StateCard extends JsonRecord {
	runId: string;
	revision: number;
	stateHash: string;
	phase: string;
	manifestFingerprint: string;
}

interface EvidenceRequest extends JsonRecord {
	attemptId: string;
	token: string;
	target: string;
	criterion: string;
	requiredStages: string[];
	completedStages: string[];
}

interface ModelWitness {
	provider: string;
	id: string;
	resolvedModel: string;
	resolvedModelIsFallback: false;
}

interface SlotModel extends Omit<ModelWitness, "id"> {
	role: string;
}

interface DeclaredAgent {
	agent: string;
	slot: string | null;
	role: string | null;
	resolvedModel: string | null;
}

interface SealedDispatch {
	dispatchId: string;
	attemptIds: string[];
	toolCallId: string;
	taskInput: JsonRecord;
	kind: "producer" | "lenses";
	settled: boolean;
}

interface ManifestWitness {
	fingerprint: string;
}


interface ActiveRun {
	sessionId: string;
	card: StateCard;
	slotModels: Record<string, SlotModel>;
	agents: Map<string, DeclaredAgent>;
	manifestFingerprint: string;
	dispatch?: SealedDispatch;
}

class PocockError extends Error {}

class CoreCliError extends PocockError {
	constructor(
		readonly command: string,
		message: string,
		readonly diagnostic?: JsonRecord,
	) {
		super(message);
		this.name = "CoreCliError";
	}
}

/**
 * A mirrored run the core proved incompatible with the installed contour.
 *
 * The core refuses to hydrate or mutate such a run: `require_run_runtime`
 * checks the runtime fingerprint before revision and stateHash, so even
 * `cancel` is unreachable. Its only recovery is `status`, which proves
 * `runtimeMismatch`, followed by `enter`, which transactionally supersedes it.
 */
class IncompatibleRunError extends PocockError {}

/** Core diagnostics that prove the mirrored run is stale, not the mirror untrustworthy. */
const INCOMPATIBLE_RUN_CODES: Record<string, true> = { runtime_changed: true, config_changed: true };

function isIncompatibleRun(error: unknown): boolean {
	if (error instanceof IncompatibleRunError) return true;
	if (!(error instanceof CoreCliError) || !isRecord(error.diagnostic)) return false;
	const code = nonEmptyString(error.diagnostic.code);
	return code !== undefined && INCOMPATIBLE_RUN_CODES[code] === true;
}

const extensionDirectory = dirname(fileURLToPath(import.meta.url));

function asRuntimeContext(context: unknown): RuntimeContext {
	return context as RuntimeContext;
}

function isRecord(value: unknown): value is JsonRecord {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nonEmptyString(value: unknown): string | undefined {
	return typeof value === "string" && value.trim().length > 0 ? value : undefined;
}

function stringOrNull(value: unknown): string | null {
	return typeof value === "string" ? value : null;
}

function errorMessage(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

function jsonText(value: unknown): string {
	try {
		return JSON.stringify(value);
	} catch {
		return JSON.stringify({ error: "Pocock adapter could not serialize the response" });
	}
}

function toolSuccess(value: JsonRecord) {
	return {
		content: [{ type: "text" as const, text: jsonText(value) }],
		details: value,
	};
}

function toolFailure(error: unknown) {
	const message = errorMessage(error);
	return {
		content: [{ type: "text" as const, text: message }],
		details: { error: message },
		isError: true,
	};
}

function isRegularFile(path: string): boolean {
	try {
		return statSync(path).isFile();
	} catch {
		return false;
	}
}

function isDirectory(path: string): boolean {
	try {
		return statSync(path).isDirectory();
	} catch {
		return false;
	}
}

function physicalExtensionDirectory(): string {
	try {
		return realpathSync(extensionDirectory);
	} catch {
		return extensionDirectory;
	}
}

function sha256(value: string): string {
	return createHash("sha256").update(value).digest("hex");
}

function agentNameFromManifest(content: string): string | undefined {
	const lines = content.split(/\r?\n/);
	if (lines[0] !== "---") return undefined;
	const end = lines.indexOf("---", 1);
	if (end < 2) return undefined;
	try {
		const frontmatter: unknown = YAML.parse(lines.slice(1, end).join("\n"));
		return isRecord(frontmatter) ? nonEmptyString(frontmatter.name) : undefined;
	} catch {
		return undefined;
	}
}

function agentDefinitions(directory: string): Map<string, { path: string; sha256: string }> {
	const definitions = new Map<string, { path: string; sha256: string }>();
	let names: string[];
	try {
		names = readdirSync(directory).filter(name => name.endsWith(".md")).sort();
	} catch {
		return definitions;
	}
	for (const fileName of names) {
		const path = resolve(directory, fileName);
		if (!isRegularFile(path)) continue;
		const content = readFileSync(path, "utf8");
		const name = agentNameFromManifest(content);
		if (name && !definitions.has(name)) definitions.set(name, { path, sha256: sha256(content) });
	}
	return definitions;
}

function userAgentDirectory(): string {
	const base = process.env.PI_CODING_AGENT_DIR?.trim() || resolve(homedir(), ".omp/agent");
	return resolve(base, "agents");
}

function nearestProjectAgentDirectory(cwd: string): string | undefined {
	let current = resolve(cwd);
	for (;;) {
		const candidate = resolve(current, ".omp/agents");
		if (isDirectory(candidate)) return candidate;
		const parent = dirname(current);
		if (parent === current) return undefined;
		current = parent;
	}
}

function manifestWitness(agents: Map<string, DeclaredAgent>, cwd: string): ManifestWitness {
	const installedDirectory = userAgentDirectory();
	const fallbackDirectory = resolve(physicalExtensionDirectory(), "../../agents");
	const trustedDefinitions = agentDefinitions(isDirectory(installedDirectory) ? installedDirectory : fallbackDirectory);
	const projectDirectory = nearestProjectAgentDirectory(cwd);
	const projectDefinitions = projectDirectory ? agentDefinitions(projectDirectory) : new Map();
	const witness = createHash("sha256");

	for (const name of [...agents.keys()].sort()) {
		const trusted = trustedDefinitions.get(name);
		if (!trusted) throw new PocockError(`Trusted Pocock agent manifest is missing for ${name}`);
		const effective = projectDefinitions.get(name) ?? trusted;
		if (effective.sha256 !== trusted.sha256) {
			throw new PocockError(`Project agent ${name} shadows the trusted Pocock manifest: ${effective.path}`);
		}
		witness.update(name);
		witness.update("\0");
		witness.update(trusted.sha256);
		witness.update("\0");
	}
	return { fingerprint: witness.digest("hex") };
}

/**
 * The installed extension may be a symlink into the repository or a copied
 * directory. Resolve only fixed locations; notably, do not infer the runtime
 * from the caller's cwd, which could point at any user project.
 */
function runtimeCandidates(): string[] {
	return [
		resolve(physicalExtensionDirectory(), "../../../skill/orchestrate/tools/omp_runtime.py"),
		resolve(homedir(), ".agents/skills/orchestrate/tools/omp_runtime.py"),
	];
}

/**
 * Commands that may observe a runtime replaced under a live OMP session.
 *
 * `status` mutates nothing and is the ONLY place the core proves
 * `runtimeMismatch`; `metadata` reads no run at all; `start` records the new
 * fingerprint into a new run and is the documented replacement path. Refusing
 * these three wedged the session: the core rejects every mutation of the
 * mismatched run — `mutate` checks the runtime before revision and stateHash,
 * so even `cancel` is unreachable — while the adapter refused the status call
 * that would authorize the replacement. Mutating commands keep the hard
 * refusal as defence in depth over the core's per-run fingerprint.
 */
const RUNTIME_ADOPTING_COMMANDS: Record<string, true> = { status: true, metadata: true, start: true };

export function pinRuntimeForSession(sessionId: string, observed: RuntimePin, adopt = false): void {
	const pinned = pinnedRuntimes.get(sessionId);
	if (!pinned) {
		pinnedRuntimes.set(sessionId, observed);
		return;
	}
	if (pinned.path === observed.path && pinned.sha256 === observed.sha256) return;
	if (adopt) {
		pinnedRuntimes.set(sessionId, observed);
		return;
	}
	throw new PocockError(
		`Pocock runtime changed after the same OMP session pinned it: ${pinned.path}; ` +
			`expected sha256=${pinned.sha256}, observed sha256=${observed.sha256}. ` +
			"Inspect the run with status; the core authorizes a replacement run that adopts the updated runtime. " +
			"If the contour was just updated, restart OMP so it loads the adapter that matches these runtime bytes.",
	);
}

function discoverRuntime(sessionId: string, adopt: boolean): string {
	const candidates = runtimeCandidates();
	const override = process.env.POCOCK_RUNTIME?.trim();
	let selected: string | undefined;
	if (override) {
		if (isAbsolute(override) && isRegularFile(override)) {
			selected = realpathSync(override);
		} else {
			throw new PocockError(
				`POCOCK_RUNTIME must name a regular absolute file. Candidates: ${[override, ...candidates].join(", ")}`,
			);
		}
	} else {
		const candidate = candidates.find(isRegularFile);
		if (candidate) selected = realpathSync(candidate);
	}
	if (!selected) throw new PocockError(`Pocock runtime was not found. Candidates: ${candidates.join(", ")}`);

	const sha256 = createHash("sha256").update(readFileSync(selected)).digest("hex");
	pinRuntimeForSession(sessionId, { path: selected, sha256 }, adopt);
	return selected;
}

function parseDiagnostic(stderr: string): JsonRecord | undefined {
	const trimmed = stderr.trim();
	if (!trimmed) return undefined;
	try {
		const parsed: unknown = JSON.parse(trimmed);
		return isRecord(parsed) ? parsed : undefined;
	} catch {
		return undefined;
	}
}

function responseMessage(command: string, result: { code: number; killed: boolean; stderr: string }): string {
	if (result.killed) return `Pocock core ${command} was cancelled`;
	const stderr = result.stderr.trim();
	return stderr ? `Pocock core ${command} failed: ${stderr}` : `Pocock core ${command} failed with exit code ${result.code}`;
}

function parseCoreResponse(command: string, stdout: string): JsonRecord {
	let parsed: unknown;
	try {
		parsed = JSON.parse(stdout);
	} catch (error) {
		throw new PocockError(`Pocock core ${command} returned invalid JSON: ${errorMessage(error)}`);
	}
	if (!isRecord(parsed) || parsed.protocolVersion !== CORE_PROTOCOL_VERSION) {
		throw new PocockError(`Pocock core ${command} protocol mismatch: expected v${CORE_PROTOCOL_VERSION}`);
	}
	return parsed;
}

function readCard(value: unknown): StateCard | undefined {
	if (!isRecord(value)) return undefined;
	const runId = nonEmptyString(value.runId);
	const stateHash = nonEmptyString(value.stateHash);
	const phase = nonEmptyString(value.phase);
	const manifestFingerprint = nonEmptyString(value.manifestFingerprint);
	if (
		!runId
		|| !stateHash
		|| !phase
		|| !manifestFingerprint
		|| typeof value.revision !== "number"
		|| !Number.isInteger(value.revision)
	) {
		return undefined;
	}
	return value as StateCard;
}

function responseCard(response: JsonRecord): StateCard | undefined {
	return readCard(response.card);
}

function requireCard(response: JsonRecord, command: string): StateCard {
	const card = responseCard(response);
	if (!card) throw new PocockError(`Pocock core ${command} did not return a state card`);
	return card;
}

function sameKeys(value: JsonRecord, expected: readonly string[]): boolean {
	const keys = Object.keys(value);
	return keys.length === expected.length && expected.every(key => Object.hasOwn(value, key));
}

/** Accept the inert skill payload before or after OMP applies its `agent=task` schema default. */
export function isDispatchPlaceholder(input: unknown): boolean {
	if (!isRecord(input) || !sameKeys(input, ["context", "tasks"]) || input.context !== DISPATCH_CONTEXT) return false;
	if (!Array.isArray(input.tasks) || input.tasks.length !== 1 || !isRecord(input.tasks[0])) return false;
	const item = input.tasks[0];
	const expectedTask = item.task === DISPATCH_PLACEHOLDER;
	const rawShape = sameKeys(item, ["task"]);
	const normalizedShape = sameKeys(item, ["task", "agent"]) && item.agent === "task";
	return expectedTask && (rawShape || normalizedShape);
}

function isTerminal(card: StateCard): boolean {
	return TERMINAL_PHASES[card.phase] === true;
}

/**
 * A card the adapter must not treat as a drivable run.
 *
 * A terminal run is finished. A card carrying core-proven `runtimeMismatch` is
 * incompatible with the installed contour and refuses every mutation — even
 * `cancel`, because the core checks the runtime fingerprint before revision and
 * stateHash. Neither can hold a sealed dispatch, so gating native delegation on
 * them protects nothing; gating on the second one is exactly what left a
 * session able to neither orchestrate nor delegate.
 */
function isInert(card: StateCard): boolean {
	return isTerminal(card) || isRecord(card.runtimeMismatch);
}

function observeModel(model: unknown): ModelWitness {
	const record = isRecord(model) ? model : undefined;
	const provider = record && nonEmptyString(record.provider);
	const id = record && nonEmptyString(record.id);
	if (!provider || !id) throw new PocockError("OMP did not expose a provider/id for a required model role");
	return {
		provider,
		id,
		resolvedModel: `${provider}/${id}`,
		resolvedModelIsFallback: false,
	};
}

function metadataSlots(metadata: JsonRecord): JsonRecord {
	const omp = isRecord(metadata.omp) ? metadata.omp : undefined;
	const slots = (omp && isRecord(omp.slots) ? omp.slots : undefined) ?? (isRecord(metadata.slots) ? metadata.slots : undefined);
	if (!slots || Object.keys(slots).length === 0) throw new PocockError("Pocock metadata contains no OMP slot aliases");
	return slots;
}

function resolveSlotModels(metadata: JsonRecord, context: RuntimeContext): Record<string, SlotModel> {
	const slotModels: Record<string, SlotModel> = {};
	for (const [slot, definition] of Object.entries(metadataSlots(metadata))) {
		const alias = isRecord(definition) ? nonEmptyString(definition.alias) : undefined;
		if (!alias) throw new PocockError(`Pocock metadata slot ${slot} has no role alias`);
		const resolved = context.models.resolve(alias);
		if (!resolved) throw new PocockError(`OMP cannot resolve required Pocock role ${alias}`);
		const observed = observeModel(resolved);
		slotModels[slot] = {
			role: alias,
			provider: observed.provider,
			resolvedModel: observed.resolvedModel,
			resolvedModelIsFallback: false,
		};
	}
	return slotModels;
}

function addDeclaredAgents(
	target: Map<string, DeclaredAgent>,
	capability: string,
	mapping: unknown,
	slotModels: Record<string, SlotModel>,
): void {
	if (!isRecord(mapping)) return;
	for (const [slot, agent] of Object.entries(mapping)) {
		if (typeof agent !== "string" || agent.length === 0) continue;
		const model = slotModels[slot];
		target.set(agent, {
			agent,
			slot: model ? slot : null,
			role: model?.role ?? null,
			resolvedModel: model?.resolvedModel ?? null,
		});
	}
}

/** Accept the two metadata layouts used by the runtime while retaining no policy here. */
function declaredAgents(metadata: JsonRecord, slotModels: Record<string, SlotModel>): Map<string, DeclaredAgent> {
	const declared = new Map<string, DeclaredAgent>();
	const omp = isRecord(metadata.omp) ? metadata.omp : undefined;
	const roles = (omp && isRecord(omp.roles) ? omp.roles : undefined) ?? (isRecord(metadata.roles) ? metadata.roles : undefined);
	if (!roles) return declared;

	for (const [capability, value] of Object.entries(roles)) {
		if (capability === "agents") continue;
		if (!isRecord(value)) continue;
		addDeclaredAgents(declared, capability, value.agents ?? value, slotModels);
	}
	const nested = isRecord(roles.agents) ? roles.agents : undefined;
	if (nested) {
		for (const [capability, mapping] of Object.entries(nested)) {
			addDeclaredAgents(declared, capability, mapping, slotModels);
		}
	}
	return declared;
}

function activeFor(context: RuntimeContext): ActiveRun | undefined {
	if (!activeRun) return undefined;
	if (activeRun.sessionId === context.sessionManager.getSessionId()) return activeRun;
	return undefined;
}

function requireActive(context: RuntimeContext): ActiveRun {
	const run = activeFor(context);
	if (!run) throw new PocockError("No hydrated Pocock run is active in this OMP session");
	if (isTerminal(run.card)) throw new PocockError(`Pocock run ${run.card.runId} is already terminal`);
	return run;
}

function validateReference(params: JsonRecord, run: ActiveRun): { runId: string; revision: number; stateHash: string } {
	if (params.runId !== run.card.runId) {
		throw new PocockError("The supplied Pocock runId does not match the current state card");
	}
	if (params.revision !== run.card.revision) {
		throw new PocockError("The supplied Pocock revision does not match the current state card");
	}
	if (params.stateHash !== run.card.stateHash) {
		throw new PocockError("The supplied Pocock stateHash does not match the current state card");
	}
	return { runId: run.card.runId, revision: run.card.revision, stateHash: run.card.stateHash };
}

function locateMirror(context: RuntimeContext): { found: boolean; card?: StateCard } {
	const branch = context.sessionManager.getBranch();
	for (let index = branch.length - 1; index >= 0; index -= 1) {
		const entry = branch[index];
		if (!isRecord(entry) || entry.type !== "custom" || entry.customType !== STATE_ENTRY) continue;
		return { found: true, card: readCard(entry.data) };
	}
	return { found: false };
}

function detailsForObservedTool(details: unknown): unknown {
	// This is deliberately the host-provided details object only. The model's
	// requested command, prose, and claimed evidence never become Pocock facts.
	return details ?? null;
}

function browserInvocation(event: { toolName: string; input: JsonRecord }): JsonRecord | undefined {
	if (event.toolName === "browser") return event.input;
	if (event.toolName !== "write" || event.input.path !== "xd://browser" || typeof event.input.content !== "string") {
		return undefined;
	}
	try {
		const parsed: unknown = JSON.parse(event.input.content);
		return isRecord(parsed) ? parsed : undefined;
	} catch {
		return undefined;
	}
}

type WitnessProbe =
	| { kind: "url"; expected: string }
	| { kind: "dom"; selector: string; expected: string };

interface StructuredWitness {
	version: 1;
	witnessId: string;
	attemptId: string;
	challengeToken: string;
	criterion: string;
	probe: WitnessProbe;
	probeHash: string;
}

interface PendingWitness {
	runId: string;
	tool: "browser" | "xdev";
	invocation: JsonRecord;
	witness: StructuredWitness;
	stage: "witness";
}

function hasLoneSurrogate(value: string): boolean {
	for (let index = 0; index < value.length; index += 1) {
		const codeUnit = value.charCodeAt(index);
		if (codeUnit >= 0xd800 && codeUnit <= 0xdbff) {
			if (index + 1 >= value.length) return true;
			const next = value.charCodeAt(index + 1);
			if (next < 0xdc00 || next > 0xdfff) return true;
			index += 1;
		} else if (codeUnit >= 0xdc00 && codeUnit <= 0xdfff) {
			return true;
		}
	}
	return false;
}

function nonEmptyWellFormedString(value: unknown): value is string {
	return typeof value === "string" && value.length > 0 && !hasLoneSurrogate(value);
}

/**
 * Serializes the closed witness values identically on every host. Probe
 * validation admits only strings and the integer protocol version, so this
 * deliberately small JSON writer has no lossy or implementation-defined cases.
 */
function canonicalJson(value: string | number | JsonRecord): string {
	if (typeof value === "string") return JSON.stringify(value);
	if (typeof value === "number") return JSON.stringify(value);
	const keys = Object.keys(value).sort();
	return `{${keys.map(key => `${JSON.stringify(key)}:${canonicalJson(value[key] as string | number | JsonRecord)}`).join(",")}}`;
}

function normalizedProbe(value: unknown): WitnessProbe | undefined {
	if (!isRecord(value) || !nonEmptyWellFormedString(value.kind)) return undefined;
	if (value.kind === "url") {
		if (!sameKeys(value, ["kind", "expected"]) || !nonEmptyWellFormedString(value.expected)) return undefined;
		return { expected: value.expected, kind: "url" };
	}
	if (value.kind === "dom") {
		if (
			!sameKeys(value, ["kind", "selector", "expected"])
			|| !nonEmptyWellFormedString(value.selector)
			|| !nonEmptyWellFormedString(value.expected)
		) return undefined;
		return { expected: value.expected, kind: "dom", selector: value.selector };
	}
	return undefined;
}

function witnessCode(probe: WitnessProbe, criterion: string): string {
	if (probe.kind === "url") {
		return `const observed = await tab.evaluate((expected) => location.href === expected, ${JSON.stringify(probe.expected)});\nassert(observed, ${JSON.stringify(criterion)});`;
	}
	const values = JSON.stringify({ expected: probe.expected, selector: probe.selector });
	return `const observed = await tab.evaluate(({ selector, expected }) => document.querySelector(selector)?.textContent === expected, ${values});\nassert(observed, ${JSON.stringify(criterion)});`;
}

function generatedBrowserInvocation(
	event: { toolName: string; input: JsonRecord },
	challengeToken: string,
	probe: WitnessProbe,
	criterion: string,
): JsonRecord {
	const execution = { action: "run", name: challengeToken, code: witnessCode(probe, criterion) };
	if (event.toolName === "browser") return execution;
	return { ...event.input, content: JSON.stringify(execution) };
}

export function uiEvidenceBinding(
	card: StateCard,
	event: { toolName: string; input: JsonRecord },
): {
	challenge: EvidenceRequest;
	stage: "open" | "witness";
	tool: "browser" | "xdev";
	invocation: JsonRecord;
	witness?: StructuredWitness;
	generatedInput?: JsonRecord;
} | undefined {
	const invocation = browserInvocation(event);
	if (!invocation || !Array.isArray(card.evidenceRequests)) return undefined;
	const challengeToken = nonEmptyString(invocation.name);
	if (!challengeToken) return undefined;
	const challenge = card.evidenceRequests.find((value): value is EvidenceRequest => {
		if (!isRecord(value)) return false;
		return (
			nonEmptyString(value.attemptId) !== undefined
			&& value.token === challengeToken
			&& nonEmptyString(value.target) !== undefined
			&& nonEmptyString(value.criterion) !== undefined
			&& Array.isArray(value.requiredStages)
			&& Array.isArray(value.completedStages)
		);
	});
	if (!challenge) return undefined;
	if (
		challenge.requiredStages.length !== 2
		|| challenge.requiredStages[0] !== "open"
		|| challenge.requiredStages[1] !== "witness"
		|| !challenge.completedStages.every(stage => stage === "open" || stage === "witness")
	) return undefined;
	const attemptId = nonEmptyString(challenge.attemptId);
	const criterion = nonEmptyString(challenge.criterion);
	const runId = nonEmptyString(card.runId);
	if (!attemptId || !criterion || !runId) return undefined;
	const tool = event.toolName === "browser" ? "browser" : "xdev";

	if (invocation.action === "open") {
		const app = isRecord(invocation.app) ? invocation.app : undefined;
		const observedTarget = nonEmptyString(invocation.url) ?? nonEmptyString(app?.target);
		if (observedTarget !== challenge.target || challenge.completedStages.includes("open")) return undefined;
		return { challenge, stage: "open", tool, invocation };
	}

	if (
		invocation.action !== "run"
		|| !sameKeys(invocation, ["action", "name", "witness"])
		|| !challenge.completedStages.includes("open")
		|| challenge.completedStages.includes("witness")
		|| !isRecord(invocation.witness)
		|| !sameKeys(invocation.witness, ["version", "probe"])
		|| invocation.witness.version !== 1
	) return undefined;
	const probe = normalizedProbe(invocation.witness.probe);
	if (!probe) return undefined;
	const probeHash = sha256(canonicalJson(probe));
	const witness: StructuredWitness = {
		version: 1,
		witnessId: sha256(canonicalJson({
			attemptId,
			challengeToken,
			criterion,
			probeHash,
			runId,
			version: 1,
		})),
		attemptId,
		challengeToken,
		criterion,
		probe,
		probeHash,
	};
	return {
		challenge,
		stage: "witness",
		tool,
		invocation,
		witness,
		generatedInput: generatedBrowserInvocation(event, challengeToken, probe, criterion),
	};
}


function taskItems(input: JsonRecord): JsonRecord[] {
	return Array.isArray(input.tasks) ? input.tasks.filter(isRecord) : [];
}

function workerOutput(result: JsonRecord): unknown {
	const structured = isRecord(result.structuredOutput) ? result.structuredOutput : undefined;
	if (structured && Object.hasOwn(structured, "data")) return structured.data;
	return typeof result.output === "string" ? result.output : null;
}

function normalizedTaskResult(
	result: JsonRecord,
	attemptId: string,
	declaredAgent: string | null,
	declaredModel: string | null,
): JsonRecord {
	const normalized: JsonRecord = {
		attemptId,
		declaredAgent,
		declaredModel,
		observedAgent: stringOrNull(result.agent),
		observedAgentSource: stringOrNull(result.agentSource),
		observedResolvedModel: stringOrNull(result.resolvedModel),
		resolvedModelIsFallback: result.resolvedModelIsFallback === true,
		exitCode: typeof result.exitCode === "number" ? result.exitCode : null,
		aborted: result.aborted === true,
		error: stringOrNull(result.error),
		tokens: typeof result.tokens === "number" ? result.tokens : null,
		usage: result.usage ?? null,
		outputPath: stringOrNull(result.outputPath),
		patchPath: stringOrNull(result.patchPath),
		branchName: stringOrNull(result.branchName),
	};
	return normalized;
}


const TUI_CONTROL_CHARACTERS = /[\u0000-\u001f\u007f-\u009f]/g;

function displayValue(value: unknown, fallback = "n/a"): string {
	if (typeof value === "number" && Number.isFinite(value)) return String(value);
	if (typeof value !== "string") return fallback;
	const flattened = value.replace(TUI_CONTROL_CHARACTERS, " ").trim();
	return flattened || fallback;
}

function projectedDispatchActors(card: StateCard): JsonRecord[] {
	const dispatch = isRecord(card.dispatch) ? card.dispatch : undefined;
	if (!dispatch || !Array.isArray(dispatch.actors)) return [];
	const actors: JsonRecord[] = [];
	for (const value of dispatch.actors) {
		if (!isRecord(value)) continue;
		actors.push({
			dispatchName: value.dispatchName,
			attemptId: value.attemptId,
			ticketId: value.ticketId,
			role: value.role,
			lens: value.lens,
			attemptOrdinal: value.attemptOrdinal,
			slotRole: value.slotRole,
			declaredModel: value.declaredModel,
			observedModel: value.observedModel,
			modelWitness: value.modelWitness,
			status: value.status,
			tokens: value.tokens,
		});
	}
	return actors;
}

type DispatchStatusKind = "accepted" | "failed" | "pending" | "settled" | "unknown";

function dispatchStatusKind(status: string): DispatchStatusKind {
	const normalized = status.toLowerCase();
	if (normalized === "accepted" || normalized === "recorded") return "accepted";
	if (normalized === "completed") return "settled";
	if (normalized === "failed" || normalized.endsWith("_failed")) return "failed";
	if (normalized === "prepared" || normalized === "running") return "pending";
	return "unknown";
}

function renderDispatchStatus(status: string): string {
	switch (dispatchStatusKind(status)) {
		case "accepted":
			return `${status} · ACCEPTED`;
		case "failed":
			return `${status} · FAILED, not ACCEPTED`;
		case "pending":
			return `${status} · RUNTIME/PENDING, not SETTLED`;
		case "settled":
			return `${status} · SETTLED, not ACCEPTED`;
		case "unknown":
			return `${status} · settlement unknown`;
	}
}


/** Render actors grouped by identical witness so wave width does not inflate the panel.
 *
 * A sealed wave normally dispatches N tickets onto one slot with one declared
 * model, one witness kind and one status. Two lines per actor repeated that
 * same witness N times, so the widget grew with the wave and pushed the editor
 * down. Grouping keeps every field the panel exists for — declared vs observed
 * model, witness kind, settlement state, tokens — but its height now tracks the
 * number of DISTINCT witnesses, not the number of tickets.
 */
function renderDispatchGroups(actors: JsonRecord[]): string[] {
	type Member = { label: string; tokens: string };
	type Group = { head: string; witness: string; members: Member[] };
	const groups = new Map<string, Group>();
	for (const actor of actors) {
		const head = `role ${displayValue(actor.role)} · lens ${displayValue(actor.lens)}`
			+ ` · attempt ${displayValue(actor.attemptOrdinal)} · slot ${displayValue(actor.slotRole)}`;
		const witness = `declared ${displayValue(actor.declaredModel)} · observed ${displayValue(actor.observedModel)}`
			+ ` · witness ${displayValue(actor.modelWitness)}`
			+ ` · status ${renderDispatchStatus(displayValue(actor.status))}`;
		const member: Member = {
			label: `${displayValue(actor.dispatchName)}→${displayValue(actor.ticketId)}`,
			tokens: displayValue(actor.tokens),
		};
		const key = `${head}\n${witness}`;
		const group = groups.get(key);
		if (group) group.members.push(member);
		else groups.set(key, { head, witness, members: [member] });
	}
	const lines: string[] = [];
	for (const group of groups.values()) {
		// Tokens are per attempt only after settlement; while a wave is pending
		// they are uniformly unknown, and repeating that per ticket is noise.
		const uniformTokens = group.members.every(member => member.tokens === group.members[0]!.tokens);
		lines.push(
			group.head,
			`  ${group.witness}${uniformTokens ? ` · tokens ${group.members[0]!.tokens}` : ""}`,
			`  ${group.members
				.map(member => (uniformTokens ? member.label : `${member.label} tokens ${member.tokens}`))
				.join(" · ")}`,
		);
	}
	return lines;
}

function updateDispatchWidget(pi: ExtensionAPI, context: RuntimeContext | undefined, card?: StateCard): void {
	if (!context?.ui || context.hasUI === false) return;
	try {
		const actors = card && !isTerminal(card) ? projectedDispatchActors(card) : [];
		if (actors.length === 0) {
			context.ui.setWidget(DISPATCH_WIDGET, undefined, { placement: "aboveEditor" });
			return;
		}
		const noun = actors.length === 1 ? "actor" : "actors";
		const lines = [`Pocock dispatch participation · ${actors.length} ${noun}`];
		lines.push(...renderDispatchGroups(actors));
		lines.push("RUNTIME/PENDING is not a settled witness · SETTLED is not ACCEPTED");
		if (actors.some(actor => actor.slotRole === "@advisor")) {
			lines.push("slot @advisor is a worker slot, not the Watchdog Advisor role");
		}
		context.ui.setWidget(DISPATCH_WIDGET, lines, { placement: "aboveEditor" });
	} catch (error) {
		pi.logger.warn(`[pocock-control] Cannot update the dispatch widget: ${errorMessage(error)}`);
	}
}
let activeRun: ActiveRun | undefined;
let vetoReason: string | undefined;
/**
 * A mirrored run released for supersession. It vetoes nothing — the core
 * refuses every mutation of it anyway — but it must be announced, because an
 * unannounced one reads as "orchestration silently captured delegation".
 */
let supersedableRun: { runId: string; phase: string; reason: string } | undefined;
let sealingToolCallId: string | undefined;
const pendingWitnesses = new Map<string, PendingWitness>();
let lifecycleEpoch = 0;
let lastStopNudge = "";
let mutationTail: Promise<void> = Promise.resolve();

/** Serialize all core mutations so revision witnesses cannot race in one OMP turn. */
function serialize<T>(operation: () => Promise<T>): Promise<T> {
	const next = mutationTail.then(operation, operation);
	mutationTail = next.then(
		() => undefined,
		() => undefined,
	);
	return next;
}

function resetRunState(): void {
	activeRun = undefined;
	sealingToolCallId = undefined;
	pendingWitnesses.clear();
}

function failClosed(pi: ExtensionAPI, reason: string, context?: RuntimeContext): void {
	resetRunState();
	supersedableRun = undefined;
	vetoReason = reason;
	updateDispatchWidget(pi, context);
	pi.logger.warn(`[pocock-control] ${reason}`);
}

/**
 * Release a mirrored run the core proved incompatible with the installed contour.
 *
 * Vetoing native `task` and `hub` here protects nothing: the core rejects every
 * mutation of such a run, and a session that has just started holds no sealed
 * dispatch that ordinary delegation could be confused with. The veto only cost
 * the owner the ability to work at all, so the run is released and announced.
 */
function releaseIncompatibleRun(pi: ExtensionAPI, context: RuntimeContext, card: StateCard, reason: string): void {
	resetRunState();
	vetoReason = undefined;
	supersedableRun = { runId: card.runId, phase: card.phase, reason };
	updateDispatchWidget(pi, context);
	pi.logger.warn(`[pocock-control] ${reason}`);
}

function commitCard(
	pi: ExtensionAPI,
	context: RuntimeContext,
	card: StateCard,
	options: {
		expectedRunId?: string;
		slotModels?: Record<string, SlotModel>;
		agents?: Map<string, DeclaredAgent>;
		resetDispatch?: boolean;
		manifestFingerprint?: string;
	} = {},
): ActiveRun {
	const sessionId = context.sessionManager.getSessionId();
	if (options.expectedRunId && card.runId !== options.expectedRunId) {
		throw new PocockError("Pocock core returned a card for a different run");
	}
	const previous = activeFor(context);
	try {
		pi.appendEntry(STATE_ENTRY, card);
	} catch (error) {
		failClosed(pi, `Cannot persist the Pocock state mirror: ${errorMessage(error)}`, context);
		throw new PocockError("Pocock state changed but its OMP mirror could not be persisted; task dispatch is locked");
	}

	const reset = options.resetDispatch === true || isTerminal(card);
	activeRun = {
		sessionId,
		card,
		slotModels: options.slotModels ?? previous?.slotModels ?? {},
		agents: options.agents ?? previous?.agents ?? new Map(),
		manifestFingerprint: options.manifestFingerprint ?? previous?.manifestFingerprint ?? card.manifestFingerprint,
		dispatch: reset ? undefined : previous?.dispatch,
	};
	updateDispatchWidget(pi, context, card);
	vetoReason = undefined;
	// A committed mismatch card keeps the run announceable: `status` proves the
	// incompatibility, and the announcement must survive until `enter` replaces it.
	supersedableRun = isRecord(card.runtimeMismatch)
		? {
			runId: card.runId,
			phase: card.phase,
			reason: nonEmptyString(card.blockedReason) ?? "the core proved this run incompatible with the installed contour",
		}
		: undefined;
	return activeRun;
}

async function invokeCore(
	pi: ExtensionAPI,
	context: RuntimeContext,
	command: string,
	request: JsonRecord,
	signal?: AbortSignal,
): Promise<JsonRecord> {
	const runtime = discoverRuntime(
		context.sessionManager.getSessionId(),
		RUNTIME_ADOPTING_COMMANDS[command] === true,
	);
	const requestDirectory = mkdtempSync(join(tmpdir(), "pocock-core-"));
	const requestPath = join(requestDirectory, "request.json");
	const timeoutMs = command === "pregate" ? PREGATE_CORE_TIMEOUT_MS : CORE_TIMEOUT_MS;
	const timeoutSignal = AbortSignal.timeout(timeoutMs);
	const coreSignal = signal ? AbortSignal.any([timeoutSignal, signal]) : timeoutSignal;
	try {
		writeFileSync(requestPath, jsonText(request), { encoding: "utf8", flag: "wx", mode: 0o600 });
		const result = await pi.exec("python3", [runtime, command, "--request-file", requestPath], {
			cwd: context.cwd,
			signal: coreSignal,
		});
		if (result.code !== 0 || result.killed) {
			throw new CoreCliError(command, responseMessage(command, result), parseDiagnostic(result.stderr));
		}
		return parseCoreResponse(command, result.stdout);
	} finally {
		rmSync(requestDirectory, { recursive: true, force: true });
	}
}

async function observeManifest(
	pi: ExtensionAPI,
	context: RuntimeContext,
	signal?: AbortSignal,
): Promise<{
	slotModels: Record<string, SlotModel>;
	agents: Map<string, DeclaredAgent>;
	manifestFingerprint: string;
}> {
	const metadata = await invokeCore(pi, context, "metadata", {}, signal);
	const slotModels = resolveSlotModels(metadata, context);
	const agents = declaredAgents(metadata, slotModels);
	return { slotModels, agents, manifestFingerprint: manifestWitness(agents, context.cwd).fingerprint };
}

function requireCurrentManifest(run: ActiveRun, context: RuntimeContext): void {
	const observed = manifestWitness(run.agents, context.cwd).fingerprint;
	if (observed !== run.manifestFingerprint || observed !== run.card.manifestFingerprint) {
		throw new PocockError("Pocock agent manifests changed after this run pinned them");
	}
}

async function hydrateSession(pi: ExtensionAPI, context: RuntimeContext): Promise<void> {
	const mirror = locateMirror(context);
	const sessionId = context.sessionManager.getSessionId();
	const epoch = ++lifecycleEpoch;
	activeRun = undefined;
	sealingToolCallId = undefined;
	pendingWitnesses.clear();

	updateDispatchWidget(pi, context);
	if (!mirror.found) {
		// A child without a state card is deliberately inert: it must not inherit
		// the parent's sealed dispatch or make the adapter manufacture one.
		vetoReason = undefined;
		supersedableRun = undefined;
		return;
	}
	if (!mirror.card) {
		failClosed(pi, "Pocock session mirror is malformed; native task dispatch is locked", context);
		return;
	}

	await serialize(async () => {
		if (epoch !== lifecycleEpoch || context.sessionManager.getSessionId() !== sessionId) return;
		try {
			const manifest = await observeManifest(pi, context);
			const response = await invokeCore(pi, context, "hydrate", {
				runId: mirror.card!.runId,
				stateHash: mirror.card!.stateHash,
				revision: mirror.card!.revision,
			});
			if (epoch !== lifecycleEpoch || context.sessionManager.getSessionId() !== sessionId) return;
			const card = requireCard(response, "hydrate");
			if (card.manifestFingerprint !== manifest.manifestFingerprint) {
				throw new IncompatibleRunError("Installed Pocock agent manifests differ from the run snapshot");
			}
			commitCard(pi, context, card, {
				expectedRunId: mirror.card!.runId,
				slotModels: manifest.slotModels,
				agents: manifest.agents,
				manifestFingerprint: manifest.manifestFingerprint,
				resetDispatch: true,
			});
		} catch (error) {
			if (epoch !== lifecycleEpoch || context.sessionManager.getSessionId() !== sessionId) return;
			if (isIncompatibleRun(error)) {
				releaseIncompatibleRun(pi, context, mirror.card!, errorMessage(error));
				return;
			}
			failClosed(pi, `Pocock session hydration failed: ${errorMessage(error)}`, context);
		}
	});
}

export default function pocockControl(pi: ExtensionAPI): void {
	const { z } = pi.zod;
	// OMP injects `pi.zod` as the omptype-backed zod shim, which does not
	// implement zod's `ZodObject.extend`. Compose schemas from shared field
	// definitions instead of calling `.extend()` on a built object schema.
	const cardReferenceFields = {
		runId: z.string().min(1),
		revision: z.number().int().nonnegative(),
		stateHash: z.string().min(1),
	};
	const cardReference = z.object(cardReferenceFields);

	pi.on("session_start", async (_event, context) => hydrateSession(pi, asRuntimeContext(context)));
	pi.on("session_switch", async (_event, context) => hydrateSession(pi, asRuntimeContext(context)));
	pi.on("session_branch", async (_event, context) => hydrateSession(pi, asRuntimeContext(context)));
	pi.on("session_tree", async (_event, context) => hydrateSession(pi, asRuntimeContext(context)));

	pi.on("session_stop", (event, context) => {
		const runtime = asRuntimeContext(context);
		const run = activeFor(runtime);
		const nudgeKey = `${event.session_id}:${event.turn_id}`;
		if (event.stop_hook_active || vetoReason || lastStopNudge === nudgeKey) return;
		if (supersedableRun && (!run || isInert(run.card))) {
			lastStopNudge = nudgeKey;
			return {
				continue: true,
				additionalContext:
					`Pocock run ${supersedableRun.runId} is mirrored in this session but incompatible with the installed `
					+ `contour in phase ${supersedableRun.phase}: ${supersedableRun.reason}. `
					+ "It cannot be hydrated, resumed, or cancelled; prove the mismatch with pocock_status and supersede "
					+ "it with pocock_enter. It blocks no native task delegation.",
			};
		}
		if (!run || isInert(run.card)) return;
		lastStopNudge = nudgeKey;
		return {
			continue: true,
			additionalContext:
				`Pocock run ${run.card.runId} is still nonterminal in phase ${run.card.phase}. ` +
				"It was not completed by session settlement; request pocock_status and follow the returned state card.",
		};
	});

	pi.registerTool({
		name: "pocock_enter",
		label: "Pocock enter",
		description: "Start a Pocock full, frontier, or sweep run with OMP-observed model witnesses.",
		parameters: z.object({
			entry: z.enum(["full", "frontier", "sweep"]),
			objective: z.string().min(1),
		}),
		async execute(_toolCallId, params, signal, _onUpdate, context) {
			try {
				const response = await serialize(async () => {
					const runtime = asRuntimeContext(context);
					const current = activeFor(runtime);
					if (current && !isInert(current.card)) {
						throw new PocockError(
							`Pocock run ${current.card.runId} is still active in phase ${current.card.phase}; resume or cancel it before entering another run`,
						);
					}
					const manifest = await observeManifest(pi, runtime, signal);
					const slotModels = manifest.slotModels;
					const start = await invokeCore(
						pi,
						runtime,
						"start",
						{
							cwd: runtime.cwd,
							entry: params.entry,
							objective: params.objective,
							sessionId: runtime.sessionManager.getSessionId(),
							models: slotModels,
							manifestFingerprint: manifest.manifestFingerprint,
						},
						signal,
					);
					const card = requireCard(start, "start");
					commitCard(pi, runtime, card, {
						slotModels,
						agents: manifest.agents,
						manifestFingerprint: manifest.manifestFingerprint,
						resetDispatch: true,
					});
					return start;
				});
				return toolSuccess(response);
			} catch (error) {
				return toolFailure(error);
			}
		},
	});

	pi.registerTool({
		name: "pocock_transition",
		label: "Pocock transition",
		description: "Request one core-authorized Pocock state transition.",
		parameters: z.object({
			...cardReferenceFields,
			action: z.string().min(1),
			payload: z.unknown().optional(),
		}),
		async execute(_toolCallId, params, signal, _onUpdate, context) {
			try {
				const response = await serialize(async () => {
					const runtime = asRuntimeContext(context);
					const run = requireActive(runtime);
					const request: JsonRecord = { ...validateReference(params as JsonRecord, run), action: params.action };
					if (Object.hasOwn(params, "payload")) request.payload = params.payload;
					const result = await invokeCore(pi, runtime, "transition", request, signal);
					const card = requireCard(result, "transition");
					commitCard(pi, runtime, card, { expectedRunId: run.card.runId, resetDispatch: true });
					return result;
				});
				return toolSuccess(response);
			} catch (error) {
				return toolFailure(error);
			}
		},
	});

	pi.registerTool({
		name: "pocock_prepare",
		label: "Pocock prepare",
		description: "Prepare deterministic producer dispatch from published tickets or the sealed local sweep ledger.",
		parameters: z.object({ ...cardReferenceFields, tickets: z.array(z.unknown()).optional() }),
		async execute(_toolCallId, params, signal, _onUpdate, context) {
			try {
				const response = await serialize(async () => {
					const runtime = asRuntimeContext(context);
					const run = requireActive(runtime);
					const request: JsonRecord = validateReference(params as JsonRecord, run);
					if (Object.hasOwn(params, "tickets")) request.tickets = params.tickets;
					const result = await invokeCore(
						pi,
						runtime,
						"prepare",
						request,
						signal,
					);
					const card = requireCard(result, "prepare");
					commitCard(pi, runtime, card, {
						expectedRunId: run.card.runId,
						resetDispatch: true,
					});
					return result;
				});
				return toolSuccess(response);
			} catch (error) {
				return toolFailure(error);
			}
		},
	});

	pi.registerTool({
		name: "pocock_pregate",
		label: "Pocock pregate",
		description: "Run core-owned deterministic pre-gates for the settled producer attempt.",
		parameters: cardReference,
		async execute(_toolCallId, params, signal, _onUpdate, context) {
			try {
				const response = await serialize(async () => {
					const runtime = asRuntimeContext(context);
					const run = requireActive(runtime);
					const request: JsonRecord = validateReference(params as JsonRecord, run);
					const result = await invokeCore(pi, runtime, "pregate", request, signal);
					const card = requireCard(result, "pregate");
					commitCard(pi, runtime, card, { expectedRunId: run.card.runId, resetDispatch: true });
					return result;
				});
				return toolSuccess(response);
			} catch (error) {
				return toolFailure(error);
			}
		},
	});

	pi.registerTool({
		name: "pocock_prepare_lenses",
		label: "Pocock prepare lenses",
		description: "Ask the core to select and seal independent review lenses.",
		parameters: cardReference,
		async execute(_toolCallId, params, signal, _onUpdate, context) {
			try {
				const response = await serialize(async () => {
					const runtime = asRuntimeContext(context);
					const run = requireActive(runtime);
					const result = await invokeCore(pi, runtime, "prepare-lenses", validateReference(params as JsonRecord, run), signal);
					const card = requireCard(result, "prepare-lenses");
					commitCard(pi, runtime, card, { expectedRunId: run.card.runId, resetDispatch: true });
					return result;
				});
				return toolSuccess(response);
			} catch (error) {
				return toolFailure(error);
			}
		},
	});

	pi.registerTool({
		name: "pocock_adjudicate",
		label: "Pocock adjudicate",
		description: "Ask the core to adjudicate current strict lens reports.",
		parameters: cardReference,
		async execute(_toolCallId, params, signal, _onUpdate, context) {
			try {
				const response = await serialize(async () => {
					const runtime = asRuntimeContext(context);
					const run = requireActive(runtime);
					const result = await invokeCore(pi, runtime, "adjudicate", validateReference(params as JsonRecord, run), signal);
					const card = requireCard(result, "adjudicate");
					commitCard(pi, runtime, card, { expectedRunId: run.card.runId, resetDispatch: true });
					return result;
				});
				return toolSuccess(response);
			} catch (error) {
				return toolFailure(error);
			}
		},
	});

	pi.registerTool({
		name: "pocock_accept",
		label: "Pocock accept",
		description: "Ask the core to accept an adjudicated Pocock run.",
		parameters: cardReference,
		async execute(_toolCallId, params, signal, _onUpdate, context) {
			try {
				const response = await serialize(async () => {
					const runtime = asRuntimeContext(context);
					const run = requireActive(runtime);
					const result = await invokeCore(pi, runtime, "accept", validateReference(params as JsonRecord, run), signal);
					const card = requireCard(result, "accept");
					commitCard(pi, runtime, card, { expectedRunId: run.card.runId, resetDispatch: true });
					return result;
				});
				return toolSuccess(response);
			} catch (error) {
				return toolFailure(error);
			}
		},
	});


	pi.registerTool({
		name: "pocock_status",
		label: "Pocock status",
		description: "Read and mirror the authoritative status of a Pocock run.",
		parameters: z.object({ runId: z.string().min(1).optional() }),
		async execute(_toolCallId, params, signal, _onUpdate, context) {
			try {
				const response = await serialize(async () => {
					const runtime = asRuntimeContext(context);
					const current = activeFor(runtime);
					const runId = params.runId ?? (current && !isInert(current.card) ? current.card.runId : undefined);
					if (current && !isInert(current.card) && runId && current.card.runId !== runId) {
						throw new PocockError(
							`Pocock run ${current.card.runId} is still active; pocock_status cannot replace its mirror with ${runId}`,
						);
					}
					const manifest = await observeManifest(pi, runtime, signal);
					const request = runId
						? { runId, manifestFingerprint: manifest.manifestFingerprint }
						: { manifestFingerprint: manifest.manifestFingerprint };
					const result = await invokeCore(pi, runtime, "status", request, signal);
					if (!runId && result.active === false) return result;
					const card = requireCard(result, "status");
					if (
						manifest.manifestFingerprint !== card.manifestFingerprint
						&& !isRecord(card.runtimeMismatch)
					) {
						throw new PocockError("Installed Pocock agent manifests differ from the run's pinned manifest");
					}
					commitCard(pi, runtime, card, {
						expectedRunId: runId ?? card.runId,
						slotModels: manifest.slotModels,
						agents: manifest.agents,
						manifestFingerprint: manifest.manifestFingerprint,
						resetDispatch: true,
					});
					return result;
				});
				return toolSuccess(response);
			} catch (error) {
				return toolFailure(error);
			}
		},
	});

	pi.on("tool_call", async (event, context) => {
		const runtime = asRuntimeContext(context);
		if (event.toolName === "hub") {
			return serialize(async () => {
				const run = activeFor(runtime);
				if (vetoReason) {
					return {
						block: true,
						reason: `Pocock is fail-closed after an unsettled sealed task: ${vetoReason}`,
					};
				}
				if (!run || isInert(run.card)) return;
				return {
					block: true,
					reason:
						"The sealed blocking task is one-shot; completed or settled workers must not be revived or waited through Hub. " +
						"Continue only through the next authorized Pocock runtime command.",
				};
			});
		}
		if (event.toolName === "browser" || event.toolName === "write") {
			return serialize(async () => {
				const run = activeFor(runtime);
				if (!run || isInert(run.card) || vetoReason) return;
				const binding = uiEvidenceBinding(run.card, event);
				if (!binding || binding.stage !== "witness" || !binding.witness || !binding.generatedInput) return;
				if (pendingWitnesses.has(event.toolCallId)) {
					return { block: true, reason: "A Pocock witness invocation is already sealed for this tool call" };
				}
				pendingWitnesses.set(event.toolCallId, {
					runId: run.card.runId,
					tool: binding.tool,
					invocation: binding.invocation,
					witness: binding.witness,
					stage: "witness",
				});
				return { input: binding.generatedInput };
			});
		}
		if (event.toolName !== "task") return;
		return serialize(async () => {
			const runtime = asRuntimeContext(context);
			if (vetoReason) return { block: true, reason: vetoReason };
			const run = activeFor(runtime);
			if (!run || isInert(run.card)) return;

			if (run.dispatch) {
				if (run.dispatch.toolCallId === event.toolCallId && isDispatchPlaceholder(event.input)) {
					return { input: run.dispatch.taskInput };
				}
				return { block: true, reason: "A Pocock task dispatch is already sealed; no second native task call is legal" };
			}
			if (sealingToolCallId && sealingToolCallId !== event.toolCallId) {
				return { block: true, reason: "A Pocock task seal is already in progress" };
			}
			if (!isDispatchPlaceholder(event.input)) {
				return { block: true, reason: "An active Pocock run accepts only the exact sealed-dispatch task placeholder" };
			}
			const kind = DISPATCH_PHASES[run.card.phase];
			if (!kind) {
				return { block: true, reason: `Pocock phase ${run.card.phase} does not authorize native task transport` };
			}

			sealingToolCallId = event.toolCallId;
			try {
				requireCurrentManifest(run, runtime);
				const response = await invokeCore(
					pi,
					runtime,
					"seal-task",
					{ runId: run.card.runId, revision: run.card.revision, stateHash: run.card.stateHash, kind },
				);
				const card = requireCard(response, "seal-task");
				const dispatchId = nonEmptyString(response.dispatchId);
				const attemptIds = Array.isArray(response.attemptIds) ? response.attemptIds.filter(nonEmptyString) : [];
				const taskInput = isRecord(response.taskInput) ? response.taskInput : undefined;
				if (!dispatchId || attemptIds.length === 0 || !taskInput) {
					throw new PocockError("Pocock core seal-task response is incomplete");
				}
				const committed = commitCard(pi, runtime, card, { expectedRunId: run.card.runId });
				committed.dispatch = {
					dispatchId,
					attemptIds,
					toolCallId: event.toolCallId,
					taskInput,
					kind,
					settled: false,
				};
				return { input: taskInput };
			} catch (error) {
				return { block: true, reason: errorMessage(error) };
			} finally {
				sealingToolCallId = undefined;
			}
		});
	});

	pi.on("tool_result", async (event, context) => {
		const runtime = asRuntimeContext(context);
		if (event.toolName === "task") {
			return serialize(async () => {
				const run = activeFor(runtime);
				if (!run || isInert(run.card)) return;
				if (vetoReason) return toolFailure(vetoReason);
				try {
					requireCurrentManifest(run, runtime);
				} catch (error) {
					const reason = `Pocock agent manifest witness failed at task settlement: ${errorMessage(error)}`;
					failClosed(pi, reason, runtime);
					return toolFailure(reason);
				}
				const dispatch = run.dispatch;
				if (!dispatch || dispatch.toolCallId !== event.toolCallId) {
					const reason = "Observed a native task result that is not the current Pocock sealed dispatch";
					failClosed(pi, reason, runtime);
					return toolFailure(reason);
				}
				const details = isRecord(event.details) ? event.details : undefined;
				if (!details || details.async !== undefined) {
					const reason = "Pocock requires settled blocking TaskToolDetails; async or missing task details are rejected";
					failClosed(pi, reason, runtime);
					return toolFailure(reason);
				}
				if (!Array.isArray(details.results) || details.results.length !== dispatch.attemptIds.length) {
					const reason = "Pocock task result count does not match the sealed attempt batch";
					failClosed(pi, reason, runtime);
					return toolFailure(reason);
				}
				const sourceResults = details.results;
				if (!sourceResults.every(isRecord)) {
					const reason = "Pocock received malformed settled TaskToolDetails.results";
					failClosed(pi, reason, runtime);
					return toolFailure(reason);
				}

				const tasks = taskItems(event.input);
				const normalizedResults: JsonRecord[] = [];
				const content: JsonRecord = {};
				for (let index = 0; index < sourceResults.length; index += 1) {
					const result = sourceResults[index]!;
					const task = tasks[index];
					const declaredAgent = task ? stringOrNull(task.agent) : null;
					const declaration = declaredAgent ? run.agents.get(declaredAgent) : undefined;
					const attemptId = dispatch.attemptIds[index]!;
					normalizedResults.push(normalizedTaskResult(result, attemptId, declaredAgent, declaration?.resolvedModel ?? null));
					content[attemptId] = workerOutput(result);
				}

				try {
					const response = await invokeCore(
						pi,
						runtime,
						"record-task-result",
						{
							runId: run.card.runId,
							revision: run.card.revision,
							stateHash: run.card.stateHash,
							dispatchId: dispatch.dispatchId,
							toolCallId: event.toolCallId,
							input: event.input,
							details: { results: normalizedResults },
							content,
							isError: event.isError,
						},
					);
					const card = requireCard(response, "record-task-result");
					const committed = commitCard(pi, runtime, card, { expectedRunId: run.card.runId });
					committed.dispatch = { ...dispatch, settled: true };
					return;
				} catch (error) {
					const reason = `Pocock could not bind the native task result: ${errorMessage(error)}`;
					failClosed(pi, reason, runtime);
					return toolFailure(reason);
				}
			});
		}

		const pending = pendingWitnesses.get(event.toolCallId);
		if (pending) {
			pendingWitnesses.delete(event.toolCallId);
			return serialize(async () => {
				if (
					event.isError
					|| (pending.tool === "browser" && event.toolName !== "browser")
					|| (pending.tool === "xdev" && event.toolName !== "write")
				) {
					return toolFailure("Pocock declarative witness execution did not succeed");
				}
				const run = activeFor(runtime);
				if (!run || isInert(run.card) || vetoReason || run.card.runId !== pending.runId) {
					return toolFailure("Pocock declarative witness result does not match the active run");
				}
				const request: JsonRecord = {
					runId: run.card.runId,
					revision: run.card.revision,
					stateHash: run.card.stateHash,
					toolCallId: event.toolCallId,
					tool: pending.tool,
					invocation: pending.invocation,
					details: detailsForObservedTool(event.details),
					content: event.content,
					success: true,
					attemptIds: [pending.witness.attemptId],
					challengeToken: pending.witness.challengeToken,
					stage: pending.stage,
					witness: pending.witness,
				};
				try {
					const response = await invokeCore(pi, runtime, "record-evidence", request);
					const card = requireCard(response, "record-evidence");
					commitCard(pi, runtime, card, { expectedRunId: run.card.runId });
					return;
				} catch (error) {
					return toolFailure(`Pocock could not record observed browser evidence: ${errorMessage(error)}`);
				}
			});
		}

		if (event.isError) return;
		return serialize(async () => {
			const run = activeFor(runtime);
			if (!run || isInert(run.card) || vetoReason) return;
			const binding = uiEvidenceBinding(run.card, event);
			if (!binding || binding.stage !== "open") return;

			const request: JsonRecord = {
				runId: run.card.runId,
				revision: run.card.revision,
				stateHash: run.card.stateHash,
				toolCallId: event.toolCallId,
				tool: binding.tool,
				invocation: binding.invocation,
				details: detailsForObservedTool(event.details),
				content: event.content,
				success: true,
				attemptIds: [binding.challenge.attemptId],
				challengeToken: binding.challenge.token,
				stage: "open",
			};

			try {
				const response = await invokeCore(pi, runtime, "record-evidence", request);
				const card = requireCard(response, "record-evidence");
				commitCard(pi, runtime, card, { expectedRunId: run.card.runId });
				return;
			} catch (error) {
				return toolFailure(`Pocock could not record observed browser evidence: ${errorMessage(error)}`);
			}
		});
	});
}
