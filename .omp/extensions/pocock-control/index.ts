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
const TERMINAL_PHASES: Readonly<Record<string, true>> = {
	completed: true,
	complete: true,
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
		family(model: unknown): string;
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
	vendor: string;
	family: string;
	resolvedModelIsFallback: false;
}

interface LaneModel extends Omit<ModelWitness, "id"> {
	role: string;
}

interface DeclaredAgent {
	agent: string;
	lane: string | null;
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
	laneModels: Record<string, LaneModel>;
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

export function pinRuntimeForSession(sessionId: string, observed: RuntimePin): void {
	const pinned = pinnedRuntimes.get(sessionId);
	if (!pinned) {
		pinnedRuntimes.set(sessionId, observed);
		return;
	}
	if (pinned.path === observed.path && pinned.sha256 === observed.sha256) return;
	throw new PocockError(
		`Pocock runtime changed after the same OMP session pinned it: ${pinned.path}; ` +
			`expected sha256=${pinned.sha256}, observed sha256=${observed.sha256}. ` +
			"Open a new OMP session to adopt the updated runtime; start a new Pocock run if the core reports runtime_changed.",
	);
}

function discoverRuntime(sessionId: string): string {
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
	pinRuntimeForSession(sessionId, { path: selected, sha256 });
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
	try {
		const parsed: unknown = JSON.parse(stdout);
		if (!isRecord(parsed)) throw new Error("stdout was not a JSON object");
		return parsed;
	} catch (error) {
		throw new PocockError(`Pocock core ${command} returned invalid JSON: ${errorMessage(error)}`);
	}
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
	return readCard(response) ?? readCard(response.state) ?? readCard(response.stateCard) ?? readCard(response.card);
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

function classifyVendor(provider: string, id: string): string | undefined {
	// Model identity takes precedence: a Claude model served through a Google
	// account remains Anthropic for independence checks, and likewise for GPT.
	const model = id.toLowerCase();
	const transport = provider.toLowerCase();
	if (model.includes("claude")) return "Anthropic";
	if (model.includes("gpt") || /^o[1-9]([-.]|$)/.test(model) || model.includes("codex")) return "OpenAI";
	if (model.includes("gemini")) return "Google";
	if (model.includes("grok")) return "xAI";
	if (model.includes("kimi")) return "Moonshot";
	if (transport.includes("anthropic") || transport.includes("claude")) return "Anthropic";
	if (transport.includes("openai") || transport.includes("codex")) return "OpenAI";
	if (transport.includes("google") || transport.includes("gemini")) return "Google";
	if (transport.includes("xai") || transport.includes("grok")) return "xAI";
	if (transport.includes("moonshot") || transport.includes("kimi")) return "Moonshot";
	return undefined;
}

function observeModel(model: unknown, models: RuntimeContext["models"]): ModelWitness {
	const record = isRecord(model) ? model : undefined;
	const provider = record && nonEmptyString(record.provider);
	const id = record && nonEmptyString(record.id);
	if (!provider || !id) throw new PocockError("OMP did not expose a provider/id for a required model role");
	const vendor = classifyVendor(provider, id);
	if (!vendor) throw new PocockError(`OMP model ${provider}/${id} has no deterministic Pocock vendor classification`);
	const family = nonEmptyString(models.family(model));
	if (!family) throw new PocockError(`OMP model ${provider}/${id} has no family witness`);
	return {
		provider,
		id,
		resolvedModel: `${provider}/${id}`,
		vendor,
		family,
		resolvedModelIsFallback: false,
	};
}

function metadataLanes(metadata: JsonRecord): JsonRecord {
	const omp = isRecord(metadata.omp) ? metadata.omp : undefined;
	const lanes = (omp && isRecord(omp.lanes) ? omp.lanes : undefined) ?? (isRecord(metadata.lanes) ? metadata.lanes : undefined);
	if (!lanes || Object.keys(lanes).length === 0) throw new PocockError("Pocock metadata contains no OMP lane aliases");
	return lanes;
}

function resolveLaneModels(metadata: JsonRecord, context: RuntimeContext): Record<string, LaneModel> {
	const laneModels: Record<string, LaneModel> = {};
	for (const [lane, definition] of Object.entries(metadataLanes(metadata))) {
		const alias = isRecord(definition) ? nonEmptyString(definition.alias) : undefined;
		if (!alias) throw new PocockError(`Pocock metadata lane ${lane} has no role alias`);
		const resolved = context.models.resolve(alias);
		if (!resolved) throw new PocockError(`OMP cannot resolve required Pocock role ${alias}`);
		const observed = observeModel(resolved, context.models);
		laneModels[lane] = {
			role: alias,
			provider: observed.provider,
			resolvedModel: observed.resolvedModel,
			vendor: observed.vendor,
			family: observed.family,
			resolvedModelIsFallback: false,
		};
	}
	return laneModels;
}

function addDeclaredAgents(
	target: Map<string, DeclaredAgent>,
	capability: string,
	mapping: unknown,
	laneModels: Record<string, LaneModel>,
): void {
	if (!isRecord(mapping)) return;
	for (const [lane, agent] of Object.entries(mapping)) {
		if (typeof agent !== "string" || agent.length === 0) continue;
		const model = laneModels[lane];
		target.set(agent, {
			agent,
			lane: model ? lane : null,
			role: model?.role ?? null,
			resolvedModel: model?.resolvedModel ?? null,
		});
	}
}

/** Accept the two metadata layouts used by the runtime while retaining no policy here. */
function declaredAgents(metadata: JsonRecord, laneModels: Record<string, LaneModel>): Map<string, DeclaredAgent> {
	const declared = new Map<string, DeclaredAgent>();
	const omp = isRecord(metadata.omp) ? metadata.omp : undefined;
	const roles = (omp && isRecord(omp.roles) ? omp.roles : undefined) ?? (isRecord(metadata.roles) ? metadata.roles : undefined);
	if (!roles) return declared;

	for (const [capability, value] of Object.entries(roles)) {
		if (capability === "agents") continue;
		if (!isRecord(value)) continue;
		addDeclaredAgents(declared, capability, value.agents ?? value, laneModels);
	}
	const nested = isRecord(roles.agents) ? roles.agents : undefined;
	if (nested) {
		for (const [capability, mapping] of Object.entries(nested)) {
			addDeclaredAgents(declared, capability, mapping, laneModels);
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

function hasCriterionBoundAssertion(code: string, criterion: string): boolean {
	return (
		code.includes(criterion)
		&& /\bassert\s*\(/.test(code)
		&& !/\bassert\s*\(\s*true\s*(?:,|\))/i.test(code)
	);
}

export function uiEvidenceBinding(
	card: StateCard,
	event: { toolName: string; input: JsonRecord },
): {
	challenge: EvidenceRequest;
	stage: "open" | "exercise";
	tool: "browser" | "xdev";
	invocation: JsonRecord;
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
		);
	});
	if (!challenge) return undefined;

	let stage: "open" | "exercise";
	if (invocation.action === "open") {
		const app = isRecord(invocation.app) ? invocation.app : undefined;
		const observedTarget = nonEmptyString(invocation.url) ?? nonEmptyString(app?.target);
		if (observedTarget !== challenge.target || challenge.completedStages.includes("open")) return undefined;
		stage = "open";
	} else if (invocation.action === "run") {
		const code = nonEmptyString(invocation.code);
		if (
			!challenge.completedStages.includes("open")
			|| challenge.completedStages.includes("exercise")
			|| !code
			|| !hasCriterionBoundAssertion(code, challenge.criterion)
		) return undefined;
		stage = "exercise";
	} else {
		return undefined;
	}
	return { challenge, stage, tool: event.toolName === "browser" ? "browser" : "xdev", invocation };
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
			laneAlias: value.laneAlias,
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


function renderDispatchActor(actor: JsonRecord): string[] {
	const dispatchName = displayValue(actor.dispatchName);
	const ticketId = displayValue(actor.ticketId);
	const role = displayValue(actor.role);
	const lens = displayValue(actor.lens);
	const attemptOrdinal = displayValue(actor.attemptOrdinal);
	const laneAlias = displayValue(actor.laneAlias);
	const declaredModel = displayValue(actor.declaredModel);
	const observedModel = displayValue(actor.observedModel);
	const modelWitness = displayValue(actor.modelWitness);
	const status = displayValue(actor.status);
	const tokens = displayValue(actor.tokens);
	return [
		`${dispatchName} → ticket ${ticketId} · role ${role} · lens ${lens} · attempt ${attemptOrdinal} · lane ${laneAlias}`,
		`  declared ${declaredModel} · observed ${observedModel} · witness ${modelWitness}`
			+ ` · status ${renderDispatchStatus(status)} · tokens ${tokens}`,
	];
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
		for (const actor of actors) lines.push(...renderDispatchActor(actor));
		lines.push("RUNTIME/PENDING is not a settled witness · SETTLED is not ACCEPTED");
		if (actors.some(actor => actor.laneAlias === "@advisor")) {
			lines.push("lane @advisor is a worker lane, not the Watchdog Advisor role");
		}
		context.ui.setWidget(DISPATCH_WIDGET, lines, { placement: "aboveEditor" });
	} catch (error) {
		pi.logger.warn(`[pocock-control] Cannot update the dispatch widget: ${errorMessage(error)}`);
	}
}
let activeRun: ActiveRun | undefined;
let vetoReason: string | undefined;
let sealingToolCallId: string | undefined;
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

function failClosed(pi: ExtensionAPI, reason: string, context?: RuntimeContext): void {
	activeRun = undefined;
	sealingToolCallId = undefined;
	vetoReason = reason;
	updateDispatchWidget(pi, context);
	pi.logger.warn(`[pocock-control] ${reason}`);
}

function commitCard(
	pi: ExtensionAPI,
	context: RuntimeContext,
	card: StateCard,
	options: {
		expectedRunId?: string;
		laneModels?: Record<string, LaneModel>;
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
		laneModels: options.laneModels ?? previous?.laneModels ?? {},
		agents: options.agents ?? previous?.agents ?? new Map(),
		manifestFingerprint: options.manifestFingerprint ?? previous?.manifestFingerprint ?? card.manifestFingerprint,
		dispatch: reset ? undefined : previous?.dispatch,
	};
	updateDispatchWidget(pi, context, card);
	vetoReason = undefined;
	return activeRun;
}

async function invokeCore(
	pi: ExtensionAPI,
	context: RuntimeContext,
	command: string,
	request: JsonRecord,
	signal?: AbortSignal,
): Promise<JsonRecord> {
	const runtime = discoverRuntime(context.sessionManager.getSessionId());
	const requestDirectory = mkdtempSync(join(tmpdir(), "pocock-core-"));
	const requestPath = join(requestDirectory, "request.json");
	try {
		writeFileSync(requestPath, jsonText(request), { encoding: "utf8", flag: "wx", mode: 0o600 });
		const result = await pi.exec("python3", [runtime, command, "--request-file", requestPath], {
			cwd: context.cwd,
			signal,
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
	laneModels: Record<string, LaneModel>;
	agents: Map<string, DeclaredAgent>;
	manifestFingerprint: string;
}> {
	const metadata = await invokeCore(pi, context, "metadata", {}, signal);
	const laneModels = resolveLaneModels(metadata, context);
	const agents = declaredAgents(metadata, laneModels);
	return { laneModels, agents, manifestFingerprint: manifestWitness(agents, context.cwd).fingerprint };
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

	updateDispatchWidget(pi, context);
	if (!mirror.found) {
		// A child without a state card is deliberately inert: it must not inherit
		// the parent's sealed dispatch or make the adapter manufacture one.
		vetoReason = undefined;
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
				throw new PocockError("Installed Pocock agent manifests differ from the run snapshot");
			}
			commitCard(pi, context, card, {
				expectedRunId: mirror.card!.runId,
				laneModels: manifest.laneModels,
				agents: manifest.agents,
				manifestFingerprint: manifest.manifestFingerprint,
				resetDispatch: true,
			});
		} catch (error) {
			if (epoch === lifecycleEpoch && context.sessionManager.getSessionId() === sessionId) {
				failClosed(pi, `Pocock session hydration failed: ${errorMessage(error)}`, context);
			}
		}
	});
}

export default function pocockControl(pi: ExtensionAPI): void {
	const { z } = pi.zod;
	const cardReference = z.object({
		runId: z.string().min(1),
		revision: z.number().int().nonnegative(),
		stateHash: z.string().min(1),
	});

	pi.on("session_start", async (_event, context) => hydrateSession(pi, asRuntimeContext(context)));
	pi.on("session_switch", async (_event, context) => hydrateSession(pi, asRuntimeContext(context)));
	pi.on("session_branch", async (_event, context) => hydrateSession(pi, asRuntimeContext(context)));
	pi.on("session_tree", async (_event, context) => hydrateSession(pi, asRuntimeContext(context)));

	pi.on("session_stop", (event, context) => {
		const runtime = asRuntimeContext(context);
		const run = activeFor(runtime);
		if (!run || vetoReason || event.stop_hook_active || isTerminal(run.card)) return;
		const nudgeKey = `${event.session_id}:${event.turn_id}`;
		if (lastStopNudge === nudgeKey) return;
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
					if (current && !isTerminal(current.card) && !isRecord(current.card.runtimeMismatch)) {
						throw new PocockError(
							`Pocock run ${current.card.runId} is still active in phase ${current.card.phase}; resume or cancel it before entering another run`,
						);
					}
					const manifest = await observeManifest(pi, runtime, signal);
					const laneModels = manifest.laneModels;
					const start = await invokeCore(
						pi,
						runtime,
						"start",
						{
							cwd: runtime.cwd,
							entry: params.entry,
							objective: params.objective,
							sessionId: runtime.sessionManager.getSessionId(),
							models: laneModels,
							manifestFingerprint: manifest.manifestFingerprint,
						},
						signal,
					);
					const card = requireCard(start, "start");
					commitCard(pi, runtime, card, {
						laneModels,
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
		parameters: cardReference.extend({
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
		parameters: cardReference.extend({ tickets: z.array(z.unknown()).optional() }),
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
					const runId = params.runId ?? (current && !isTerminal(current.card) ? current.card.runId : undefined);
					if (current && !isTerminal(current.card) && runId && current.card.runId !== runId) {
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
						laneModels: manifest.laneModels,
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
				if (!run || isTerminal(run.card)) return;
				return {
					block: true,
					reason:
						"The sealed blocking task is one-shot; completed or settled workers must not be revived or waited through Hub. " +
						"Continue only through the next authorized Pocock runtime command.",
				};
			});
		}
		if (event.toolName !== "task") return;
		return serialize(async () => {
			const runtime = asRuntimeContext(context);
			if (vetoReason) return { block: true, reason: vetoReason };
			const run = activeFor(runtime);
			if (!run || isTerminal(run.card)) return;

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
				if (!run || isTerminal(run.card)) return;
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

		if (event.isError) return;
		return serialize(async () => {
			const run = activeFor(runtime);
			if (!run || isTerminal(run.card) || vetoReason) return;
			const binding = uiEvidenceBinding(run.card, event);
			if (!binding) return;

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
				stage: binding.stage,
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
