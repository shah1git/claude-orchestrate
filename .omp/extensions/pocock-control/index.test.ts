import { describe, expect, test } from "bun:test";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import pocockControl, { isDispatchPlaceholder, pinRuntimeForSession, uiEvidenceBinding } from "./index";

const context = "Pocock sealed dispatch";
const task = "Pocock sealed dispatch placeholder";

describe("isDispatchPlaceholder", () => {
	test("accepts the raw skill payload", () => {
		expect(isDispatchPlaceholder({ context, tasks: [{ task }] })).toBe(true);
	});

	test("accepts OMP's normalized default agent", () => {
		expect(isDispatchPlaceholder({ context, tasks: [{ task, agent: "task" }] })).toBe(true);
	});

	test("rejects any caller-selected agent or extra dispatch field", () => {
		expect(isDispatchPlaceholder({ context, tasks: [{ task, agent: "scout" }] })).toBe(false);
		expect(isDispatchPlaceholder({ context, tasks: [{ task, agent: "task", effort: "high" }] })).toBe(false);
	});
});

describe("runtime pin lifecycle", () => {
	test("a new OMP session can pin updated runtime bytes without weakening the original session", () => {
		const firstSession = `runtime-pin-first-${Date.now()}`;
		const secondSession = `runtime-pin-second-${Date.now()}`;
		const original = { path: "/opt/pocock/omp_runtime.py", sha256: "a".repeat(64) };
		const updated = { path: original.path, sha256: "b".repeat(64) };

		pinRuntimeForSession(firstSession, original);
		pinRuntimeForSession(firstSession, original);
		expect(() => pinRuntimeForSession(firstSession, updated)).toThrow(/same OMP session pinned/);

		pinRuntimeForSession(secondSession, updated);
		expect(() => pinRuntimeForSession(firstSession, updated)).toThrow(/same OMP session pinned/);
	});
});

test("a runtime-mismatched mirror can be replaced by a new run", async () => {
	const manifestFingerprint = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
	const harness = adapterHarness((command, request) => {
		if (command === "status") {
			return coreResponse({
				card: {
					...card("old-run", 4, "ready"),
					manifestFingerprint,
					nextActions: [],
					blockedReason: "effective Pocock runtime differs",
					runtimeMismatch: { expected: "old", observed: "new" },
				},
			});
		}
		if (command === "metadata") return coreResponse({ omp: { slots: { scout: { alias: "@pocock-scout" } } } });
		if (command === "start") return coreCard({ ...card("new-run", 0, "frontier_admission"), manifestFingerprint });
		throw new Error(`Unexpected core command ${command}: ${JSON.stringify(request)}`);
	});

	const status = await harness.status("status-mismatched", { runId: "old-run" }, undefined, undefined, harness.context);
	expect(status.isError).not.toBe(true);
	const entered = await harness.enter(
		"enter-after-mismatch",
		{ entry: "frontier", objective: "Restart from durable provenance" },
		undefined,
		undefined,
		harness.context,
	);

	expect(entered.isError).not.toBe(true);
	const start = harness.requests.findLast(request => request.command === "start");
	expect(start?.request).toMatchObject({ entry: "frontier" });
	expect(start?.request).not.toHaveProperty("lead");
});


test("status without a mirrored run hydrates the core-owned active run", async () => {
	const manifestFingerprint = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
	const harness = adapterHarness((command, request) => {
		if (command === "status") {
			expect(request).toEqual({ manifestFingerprint });
			return coreCard({ ...card("durable-run", 7, "ready"), manifestFingerprint });
		}
		if (command === "metadata") return coreResponse({ omp: { slots: { scout: { alias: "@pocock-scout" } } } });
		throw new Error(`Unexpected core command ${command}: ${JSON.stringify(request)}`);
	});

	const observed = await harness.status("discover-active", {}, undefined, undefined, harness.context);

	expect(observed.isError).not.toBe(true);
	expect(harness.requests.map(request => request.command)).toEqual(["metadata", "status"]);
});


test("status without a mirrored run reports an empty workspace", async () => {
	const harness = adapterHarness((command, request) => {
		if (command === "metadata") return coreResponse({ omp: { slots: { scout: { alias: "@pocock-scout" } } } });
		if (command === "status") {
			expect(request).toEqual({
				manifestFingerprint: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
			});
			return coreResponse({ active: false });
		}
		throw new Error(`Unexpected core command ${command}: ${JSON.stringify(request)}`);
	});

	const observed = await harness.status("discover-empty", {}, undefined, undefined, harness.context);

	expect(observed.isError).not.toBe(true);
	expect(harness.requests.map(request => request.command)).toEqual(["metadata", "status"]);
});

type CoreRequest = {
	command: string;
	request: Record<string, unknown>;
	signal: AbortSignal | undefined;
};

type ToolExecute = (
	toolCallId: string,
	params: Record<string, unknown>,
	signal: AbortSignal | undefined,
	onUpdate: unknown,
	context: unknown,
) => Promise<{
	content?: Array<{ type: "text"; text: string }>;
	details: Record<string, unknown>;
	isError?: boolean;
}>;

type RegisteredHook = (event: Record<string, unknown>, context: unknown) => Promise<unknown> | unknown;
type CoreResponder = (command: string, request: Record<string, unknown>) => Record<string, unknown> | Error;

function isJsonRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

const card = (runId: string, revision: number, phase: string) => ({
	runId,
	revision,
	stateHash: `${runId}-${revision}-hash`,
	phase,
	manifestFingerprint: "manifest",
});

function coreResponse(response: Record<string, unknown>): Record<string, unknown> {
	return { protocolVersion: 1, ...response };
}

function coreCard(value: Record<string, unknown>): Record<string, unknown> {
	return coreResponse({ card: value });
}

let harnessSession = 0;

function adapterHarness(respond?: CoreResponder) {
	const sessionId = `session-${++harnessSession}`;
	const tools = new Map<string, { execute: ToolExecute }>();
	const hooks = new Map<string, RegisteredHook>();
	const requests: CoreRequest[] = [];
	const entries: Array<{ customType: string; data: unknown }> = [];
	const widgets: Array<{ name: string; content: unknown; options: unknown }> = [];
	const messages: unknown[][] = [];
	let appendError: Error | undefined;
	// The adapter builds schemas from shared field definitions; OMP's injected
	// omptype-backed zod shim has no `ZodObject.extend`, so the mock mirrors only
	// the chainable methods the adapter actually calls.
	const schema = {
		min: () => schema,
		int: () => schema,
		nonnegative: () => schema,
		optional: () => schema,
	};
	const z = {
		string: () => schema,
		number: () => schema,
		enum: () => schema,
		unknown: () => schema,
		array: () => schema,
		object: () => schema,
	};
	const context = {
		cwd: process.cwd(),
		hasUI: true,
		ui: {
			setWidget: (name: string, content: unknown, options: unknown) => {
				widgets.push({ name, content, options });
			},
		},
		sessionManager: {
			getSessionId: () => sessionId,
			getBranch: () => [],
		},
		models: {
			resolve: () => ({ provider: "openai", id: "gpt-5" }),
		},
	};
	const pi = {
		zod: { z },
		on: (name: string, hook: RegisteredHook) => {
			hooks.set(name, hook);
		},
		registerTool: (tool: { name: string; execute: ToolExecute }) => {
			tools.set(tool.name, tool);
		},
		appendEntry: (customType: string, data: unknown) => {
			if (appendError) throw appendError;
			entries.push({ customType, data });
		},
		logger: { warn: () => {} },
		sendMessage: (...args: unknown[]) => {
			messages.push(args);
		},
		exec: async (_program: string, args: string[], options?: { signal?: AbortSignal }) => {
			const requestPath = args[args.indexOf("--request-file") + 1];
			if (!requestPath) throw new Error("Pocock core request path is missing");
			const parsed: unknown = JSON.parse(await Bun.file(requestPath).text());
			if (!isJsonRecord(parsed)) throw new Error("Pocock core request must be an object");
			const request = parsed;
			const command = args[1];
			if (!command) throw new Error("Pocock core command is missing");
			requests.push({ command, request, signal: options?.signal });
			const response = respond?.(command, request) ?? (
				command === "metadata"
					? coreResponse({ omp: { slots: { scout: { alias: "@pocock-scout" } } } })
					: command === "start"
						? coreCard(card(`${request.entry}-run`, 0, "producer_dispatch_pending"))
						: coreCard(card(String(request.runId), Number(request.revision) + 1, "completed"))
			);
			if (response instanceof Error) throw response;
			return { code: 0, killed: false, stdout: JSON.stringify(response), stderr: "" };
		},
	};

	// The adapter only reaches these explicit OMP methods in this request-seam test.
	const adapterPi = pi as unknown as ExtensionAPI;
	pocockControl(adapterPi);
	return {
		requests,
		entries,
		widgets,
		messages,
		context,
		failNextAppend: (error: Error) => {
			appendError = error;
		},
		enter: tools.get("pocock_enter")!.execute,
		prepare: tools.get("pocock_prepare")!.execute,
		transition: tools.get("pocock_transition")!.execute,
		status: tools.get("pocock_status")!.execute,
		sessionStart: hooks.get("session_start")!,
		toolCall: hooks.get("tool_call")!,
		toolResult: hooks.get("tool_result")!,
		sessionStop: hooks.get("session_stop")!,
	};
}

describe("Pocock core protocol", () => {
	test("rejects a core response without protocolVersion", async () => {
		const harness = adapterHarness(command => {
			if (command === "metadata") return coreResponse({ omp: { slots: { scout: { alias: "@pocock-scout" } } } });
			if (command === "start") return { card: card("missing-version", 0, "ready") };
			throw new Error(`Unexpected core command ${command}`);
		});

		const result = await harness.enter(
			"missing-protocol-version",
			{ entry: "full", objective: "Validate response protocol" },
			undefined,
			undefined,
			harness.context,
		);

		expect(result).toMatchObject({
			isError: true,
			details: { error: "Pocock core start protocol mismatch: expected v1" },
		});
	});

	test("rejects a state card returned at the response root", async () => {
		const harness = adapterHarness(command => {
			if (command === "metadata") return coreResponse({ omp: { slots: { scout: { alias: "@pocock-scout" } } } });
			if (command === "start") return coreResponse(card("root-card", 0, "ready"));
			throw new Error(`Unexpected core command ${command}`);
		});

		const result = await harness.enter(
			"root-card",
			{ entry: "full", objective: "Validate response envelope" },
			undefined,
			undefined,
			harness.context,
		);

		expect(result).toMatchObject({
			isError: true,
			details: { error: "Pocock core start did not return a state card" },
		});
	});

	test("always passes a signal to core execution", async () => {
		const harness = adapterHarness();

		const result = await harness.enter(
			"core-timeout-signal",
			{ entry: "full", objective: "Observe core execution signal" },
			undefined,
			undefined,
			harness.context,
		);

		expect(result.isError).not.toBe(true);
		expect(harness.requests).toHaveLength(2);
		for (const request of harness.requests) expect(request.signal).toBeInstanceOf(AbortSignal);
	});
});

describe("Sweep adapter requests", () => {
	const harness = adapterHarness();

	test("forwards full and frontier caller tickets to prepare core requests", async () => {
		const tickets = [{ id: "T1", title: "Implement", dependsOn: [] }];
		for (const entry of ["full", "frontier"] as const) {
			const runId = `${entry}-run`;
			await harness.enter(`enter-${entry}`, { entry, objective: "Ship the change" }, undefined, undefined, harness.context);
			await harness.prepare(
				`prepare-${entry}`,
				{ runId, revision: 0, stateHash: `${runId}-0-hash`, tickets },
				undefined,
				undefined,
				harness.context,
			);
		}

		expect(harness.requests.filter(request => request.command === "prepare").map(request => request.request)).toEqual([
			{ runId: "full-run", revision: 0, stateHash: "full-run-0-hash", tickets },
			{ runId: "frontier-run", revision: 0, stateHash: "frontier-run-0-hash", tickets },
		]);
	});

	test("forwards Sweep entry and omits tickets for the sealed-ledger prepare request", async () => {
		await harness.enter("enter-sweep", { entry: "sweep", objective: "Reconcile accepted tickets" }, undefined, undefined, harness.context);
		await harness.prepare(
			"prepare-sweep",
			{ runId: "sweep-run", revision: 0, stateHash: "sweep-run-0-hash" },
			undefined,
			undefined,
			harness.context,
		);

		expect(harness.requests.findLast(request => request.command === "start")?.request).toMatchObject({
			entry: "sweep",
			objective: "Reconcile accepted tickets",
		});
		expect(harness.requests.findLast(request => request.command === "prepare")?.request).toEqual({
			runId: "sweep-run",
			revision: 0,
			stateHash: "sweep-run-0-hash",
		});
	});
});

describe("Pocock live dispatch observability", () => {
	test("projects opaque participation actors only into the dispatch widget", async () => {
		const actor = {
			dispatchName: "P/L • opaque-name",
			attemptId: "attempt-opaque",
			ticketId: "T-42",
			role: "reviewer",
			lens: "security",
			attemptOrdinal: 0,
			slotRole: "@advisor",
			declaredModel: "openai/gpt-5",
			observedModel: "anthropic/claude-4",
			modelWitness: "witness-42",
			status: "prepared",
			tokens: null,
		};
		const sealedTaskInput = {
			context: "sealed task transport",
			tasks: [{ task: "Inspect the change", agent: "task" }],
		};
		const projectedCard = {
			...card("projection-run", 1, "producer_dispatch_pending"),
			dispatch: { actors: [actor] },
		};
		let manifestFingerprint = "";
		const harness = adapterHarness((command, request) => {
			if (command === "metadata") return coreResponse({ omp: { slots: { scout: { alias: "@pocock-scout" } } } });
			if (command === "start") {
				manifestFingerprint = String(request.manifestFingerprint);
				return coreCard({ ...card("projection-run", 0, "producer_dispatch_pending"), manifestFingerprint });
			}
			if (command === "transition") return coreCard({ ...projectedCard, manifestFingerprint });
			if (command === "seal-task") {
				return coreResponse({
					card: { ...projectedCard, manifestFingerprint },
					dispatchId: "dispatch-opaque",
					attemptIds: [actor.attemptId],
					taskInput: sealedTaskInput,
				});
			}
			throw new Error(`Unexpected core command ${command} for ${String(request.runId)}`);
		});

		await harness.enter("enter-projection", { entry: "full", objective: "Project actor" }, undefined, undefined, harness.context);
		await harness.transition(
			"transition-projection",
			{ runId: "projection-run", revision: 0, stateHash: "projection-run-0-hash", action: "project" },
			undefined,
			undefined,
			harness.context,
		);
		const taskCall = await harness.toolCall(
			{
				toolName: "task",
				toolCallId: "dispatch-opaque-call",
				input: { context, tasks: [{ task }] },
			},
			harness.context,
		);

		const widget = harness.widgets.findLast(value => value.name === "pocock-dispatch");
		expect(widget?.options).toEqual({ placement: "aboveEditor" });
		const display = (widget?.content as string[]).join("\n");
		for (const expected of [
			actor.dispatchName,
			actor.ticketId,
			actor.slotRole,
			actor.declaredModel,
			actor.observedModel,
			actor.modelWitness,
			actor.status,
			"RUNTIME/PENDING, not SETTLED",
			"n/a",
		]) expect(display).toContain(expected);
		expect(taskCall).toEqual({ input: sealedTaskInput });
		for (const projected of [
			actor.dispatchName,
			actor.ticketId,
			actor.slotRole,
			actor.declaredModel,
			actor.observedModel,
			actor.modelWitness,
			actor.status,
		]) expect(JSON.stringify(taskCall)).not.toContain(projected);
		expect(harness.requests.findLast(value => value.command === "seal-task")?.request).toEqual({
			runId: "projection-run",
			revision: 1,
			stateHash: "projection-run-1-hash",
			kind: "producer",
		});
		expect(harness.messages).toEqual([]);
	});

	test("groups identical dispatch witnesses so wave width does not inflate the widget", async () => {
		const producer = {
			role: "builder",
			lens: null,
			attemptOrdinal: 1,
			slotRole: "@pocock-builder",
			declaredModel: "openai-codex/gpt-5.6-terra",
			observedModel: null,
			modelWitness: "DECLARED_ONLY",
			status: "running",
			tokens: null,
		};
		const actors: Record<string, unknown>[] = [1, 2, 3, 4, 5].map(index => ({
			...producer,
			dispatchName: `P1T${index}A1`,
			attemptId: `attempt-${index}`,
			ticketId: `C-${index}`,
		}));
		actors.push({
			...producer,
			dispatchName: "P1L1",
			attemptId: "attempt-critic",
			ticketId: "C-1",
			role: "reviewer",
			lens: "Critic",
			slotRole: "@pocock-lens-critic",
			status: "completed",
		});
		const harness = adapterHarness(command => {
			if (command === "metadata") return coreResponse({ omp: { slots: { scout: { alias: "@pocock-scout" } } } });
			if (command === "start") return coreCard(card("wide-run", 0, "producer_dispatch_pending"));
			if (command === "transition") {
				return coreCard({ ...card("wide-run", 1, "producer_dispatch_pending"), dispatch: { actors } });
			}
			throw new Error(`Unexpected core command ${command}`);
		});

		await harness.enter("enter-wide", { entry: "full", objective: "Dispatch a wide wave" }, undefined, undefined, harness.context);
		await harness.transition(
			"transition-wide",
			{ runId: "wide-run", revision: 0, stateHash: "wide-run-0-hash", action: "project" },
			undefined,
			undefined,
			harness.context,
		);

		const lines = harness.widgets.findLast(value => value.name === "pocock-dispatch")?.content as string[];
		// header + two witness groups of three lines + footer: height follows
		// distinct witnesses, not the six dispatched actors.
		expect(lines).toHaveLength(8);
		const display = lines.join("\n");
		for (const index of [1, 2, 3, 4, 5]) expect(display).toContain(`P1T${index}A1→C-${index}`);
		expect(display).toContain("P1L1→C-1");
		expect(display).toContain("RUNTIME/PENDING, not SETTLED");
		expect(display).toContain("SETTLED, not ACCEPTED");
		expect(display).toContain("tokens n/a");
	});

	test("clears the dispatch widget for terminal, no-mirror, and fail-closed states", async () => {
		const actorCard = {
			...card("clear-run", 1, "producer_dispatch_pending"),
			dispatch: {
				actors: [{
					dispatchName: "P/L clear",
					ticketId: "T-clear",
					slotRole: "@pocock-scout",
					declaredModel: "openai/gpt-5",
					observedModel: "openai/gpt-5",
					modelWitness: "clear-witness",
					status: "prepared",
				}],
			},
		};
		let transitions = 0;
		const harness = adapterHarness(command => {
			if (command === "metadata") return coreResponse({ omp: { slots: { scout: { alias: "@pocock-scout" } } } });
			if (command === "start") return coreCard(card("clear-run", 0, "producer_dispatch_pending"));
			if (command === "transition") {
				transitions += 1;
				return coreCard(transitions === 1 ? actorCard : card("clear-run", 2, "completed"));
			}
			throw new Error(`Unexpected core command ${command}`);
		});

		await harness.enter("enter-clear", { entry: "full", objective: "Clear widget" }, undefined, undefined, harness.context);
		await harness.transition(
			"transition-project",
			{ runId: "clear-run", revision: 0, stateHash: "clear-run-0-hash", action: "project" },
			undefined,
			undefined,
			harness.context,
		);
		expect(harness.widgets.findLast(value => value.name === "pocock-dispatch")?.content).toEqual(expect.any(Array));
		await harness.transition(
			"transition-terminal",
			{ runId: "clear-run", revision: 1, stateHash: "clear-run-1-hash", action: "complete" },
			undefined,
			undefined,
			harness.context,
		);
		expect(harness.widgets.findLast(value => value.name === "pocock-dispatch")?.content).toBeUndefined();
		await harness.sessionStart({}, harness.context);
		expect(harness.widgets.findLast(value => value.name === "pocock-dispatch")?.content).toBeUndefined();

		await harness.enter("enter-fail-closed", { entry: "full", objective: "Fail closed" }, undefined, undefined, harness.context);
		harness.failNextAppend(new Error("mirror write failed"));
		const failure = await harness.transition(
			"transition-fail-closed",
			{ runId: "clear-run", revision: 0, stateHash: "clear-run-0-hash", action: "project" },
			undefined,
			undefined,
			harness.context,
		);
		expect(failure.isError).toBe(true);
		expect(harness.widgets.findLast(value => value.name === "pocock-dispatch")?.content).toBeUndefined();
	});

	test("preserves task agent and model witnesses", async () => {
		const sealedTaskInput = {
			context: "sealed result transport",
			tasks: [
				{ task: "First", agent: "task" },
				{ task: "Second", agent: "task" },
			],
		};
		let manifestFingerprint = "";
		const harness = adapterHarness((command, request) => {
			if (command === "metadata") return coreResponse({ omp: { slots: { scout: { alias: "@pocock-scout" } } } });
			if (command === "start") {
				manifestFingerprint = String(request.manifestFingerprint);
				return coreCard({ ...card("result-run", 0, "producer_dispatch_pending"), manifestFingerprint });
			}
			if (command === "seal-task") {
				return coreResponse({
					card: { ...card("result-run", 1, "producer_dispatch_pending"), manifestFingerprint },
					dispatchId: "dispatch-results",
					attemptIds: ["attempt-1", "attempt-2"],
					taskInput: sealedTaskInput,
				});
			}
			if (command === "record-task-result") return coreCard({ ...card("result-run", 2, "completed"), manifestFingerprint });
			throw new Error(`Unexpected core command ${command}`);
		});

		await harness.enter("enter-results", { entry: "full", objective: "Normalize results" }, undefined, undefined, harness.context);
		await harness.toolCall(
			{
				toolName: "task",
				toolCallId: "dispatch-results-call",
				input: { context, tasks: [{ task }] },
			},
			harness.context,
		);
		await harness.toolResult(
			{
				toolName: "task",
				toolCallId: "dispatch-results-call",
				input: sealedTaskInput,
				details: {
					results: [
						{
							output: "first",
							agent: "producer-1",
							agentSource: "omp",
							resolvedModel: "anthropic/claude-4",
							resolvedModelIsFallback: true,
							tokens: 0,
						},
						{ output: "second" },
					],
				},
				isError: false,
			},
			harness.context,
		);

		const record = harness.requests.findLast(value => value.command === "record-task-result")?.request;
		const results = ((record?.details as Record<string, unknown>).results as Array<Record<string, unknown>>);
		expect(results[0]).toMatchObject({
			attemptId: "attempt-1",
			declaredAgent: "task",
			declaredModel: null,
			observedAgent: "producer-1",
			observedAgentSource: "omp",
			observedResolvedModel: "anthropic/claude-4",
			resolvedModelIsFallback: true,
			tokens: 0,
		});
	});

	test("gates nonterminal sessions but allows terminal sessions to stop", async () => {
		const runId = "stop-run";
		const harness = adapterHarness(command => {
			if (command === "metadata") return coreResponse({ omp: { slots: { scout: { alias: "@pocock-scout" } } } });
			if (command === "start") return coreCard(card(runId, 0, "ready"));
			if (command === "transition") return coreCard(card(runId, 1, "completed"));
			throw new Error(`Unexpected core command ${command}`);
		});

		await harness.enter("enter-stop", { entry: "full", objective: "Finish cleanly" }, undefined, undefined, harness.context);
		const pending = await harness.sessionStop(
			{ session_id: "stop-session", turn_id: "nonterminal", stop_hook_active: false },
			harness.context,
		);
		expect(pending).toEqual({
			continue: true,
			additionalContext: expect.stringContaining("still nonterminal"),
		});

		await harness.transition(
			"complete-stop",
			{ runId, revision: 0, stateHash: `${runId}-0-hash`, action: "complete" },
			undefined,
			undefined,
			harness.context,
		);
		const requestCount = harness.requests.length;
		const terminal = await harness.sessionStop(
			{ session_id: "stop-session", turn_id: "terminal", stop_hook_active: false },
			harness.context,
		);
		expect(terminal).toBeUndefined();
		expect(harness.requests).toHaveLength(requestCount);
	});

});

describe("Hub lifecycle guard", () => {
	test("blocks every Hub operation only for this session's nonterminal Pocock run", async () => {
		const harness = adapterHarness(command => {
			if (command === "metadata") return coreResponse({ omp: { slots: { scout: { alias: "@pocock-scout" } } } });
			if (command === "start") return coreCard(card("hub-guard-run", 0, "producer_dispatch_pending"));
			if (command === "transition") return coreCard(card("hub-guard-run", 1, "completed"));
			throw new Error(`Unexpected core command ${command}`);
		});
		const hubCall = (op: string) => harness.toolCall(
			{ toolName: "hub", toolCallId: `hub-${op}`, input: { op } },
			harness.context,
		);

		expect(await hubCall("send")).toBeUndefined();

		await harness.enter("enter-hub-guard", { entry: "full", objective: "Guard Hub lifecycle" }, undefined, undefined, harness.context);
		for (const op of ["send", "wait", "list"]) {
			expect(await hubCall(op)).toEqual({
				block: true,
				reason: expect.stringContaining("sealed blocking task is one-shot"),
			});
		}
		expect(await hubCall("send")).toEqual({
			block: true,
			reason: expect.stringContaining("must not be revived or waited through Hub"),
		});

		await harness.transition(
			"complete-hub-guard",
			{ runId: "hub-guard-run", revision: 0, stateHash: "hub-guard-run-0-hash", action: "complete" },
			undefined,
			undefined,
			harness.context,
		);
		expect(await hubCall("wait")).toBeUndefined();
	});

	test("keeps Hub blocked after malformed task settlement clears the session mirror", async () => {
		const sealedTaskInput = {
			context: "sealed result transport",
			tasks: [{ task: "First", agent: "task" }],
		};
		let manifestFingerprint = "";
		const harness = adapterHarness((command, request) => {
			if (command === "metadata") return coreResponse({ omp: { slots: { scout: { alias: "@pocock-scout" } } } });
			if (command === "start") {
				manifestFingerprint = String(request.manifestFingerprint);
				return coreCard({ ...card("hub-fail-closed", 0, "producer_dispatch_pending"), manifestFingerprint });
			}
			if (command === "seal-task") {
				return coreResponse({
					card: { ...card("hub-fail-closed", 1, "producer_running"), manifestFingerprint },
					dispatchId: "dispatch-fail-closed",
					attemptIds: ["attempt-1"],
					taskInput: sealedTaskInput,
				});
			}
			throw new Error(`Unexpected core command ${command}`);
		});
		await harness.enter(
			"enter-hub-fail-closed",
			{ entry: "full", objective: "Guard malformed settlement" },
			undefined,
			undefined,
			harness.context,
		);
		await harness.toolCall(
			{ toolName: "task", toolCallId: "dispatch-fail-closed-call", input: { context, tasks: [{ task }] } },
			harness.context,
		);
		const malformed = await harness.toolResult(
			{
				toolName: "task",
				toolCallId: "dispatch-fail-closed-call",
				input: sealedTaskInput,
				isError: false,
			},
			harness.context,
		);
		const guarded = await harness.toolCall(
			{ toolName: "hub", toolCallId: "hub-after-fail-closed", input: { op: "wait" } },
			harness.context,
		);

		expect(malformed?.isError).toBe(true);
		expect(guarded).toEqual({
			block: true,
			reason: expect.stringContaining("fail-closed after an unsettled sealed task"),
		});
	});

	test("status without runId replaces a terminal mirror with the core-owned active run", async () => {
		const harness = adapterHarness((command, request) => {
			if (command === "metadata") return coreResponse({ omp: { slots: { scout: { alias: "@pocock-scout" } } } });
			if (command === "start") return coreCard(card("finished-run", 0, "ready"));
			if (command === "transition") return coreCard(card("finished-run", 1, "completed"));
			if (command === "status") {
				expect(request).toEqual({
					manifestFingerprint: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
				});
				return coreCard({
					...card("resumed-run", 4, "ready"),
					manifestFingerprint: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
				});
			}
			throw new Error(`Unexpected core command ${command}`);
		});
		await harness.enter("enter-finished", { entry: "full", objective: "Finish old run" }, undefined, undefined, harness.context);
		await harness.transition(
			"complete-finished",
			{ runId: "finished-run", revision: 0, stateHash: "finished-run-0-hash", action: "complete" },
			undefined,
			undefined,
			harness.context,
		);
		const observed = await harness.status("discover-resumed", {}, undefined, undefined, harness.context);
		const guarded = await harness.toolCall(
			{ toolName: "hub", toolCallId: "hub-after-resume", input: { op: "wait" } },
			harness.context,
		);

		expect(observed.isError).not.toBe(true);
		expect(guarded).toEqual({
			block: true,
			reason: expect.stringContaining("sealed blocking task is one-shot"),
		});
	});
});

describe("uiEvidenceBinding", () => {
	const challenge = {
		attemptId: "run.w1.T1.a1",
		token: "pocock-ui-token",
		target: "http://fixture.test",
		criterion: "fixture renders",
		requiredStages: ["open", "witness"],
		completedStages: ["open"],
	};
	const stateCard = {
		runId: "run-1",
		revision: 3,
		stateHash: "hash",
		manifestFingerprint: "manifest",
		phase: "pregate_pending",
		evidenceRequests: [challenge],
	};

	test("binds a closed DOM or URL probe and generates adapter-owned code", () => {
		const expected = `text "quoted" \\\\ ${"unicode ✓"}`;
		const invocation = {
			action: "run",
			name: challenge.token,
			witness: { version: 1, probe: { kind: "dom", selector: `#target'); throw new Error('injected`, expected } },
		};
		const binding = uiEvidenceBinding(stateCard, { toolName: "browser", input: invocation });
		expect(binding).toMatchObject({
			challenge,
			stage: "witness",
			tool: "browser",
			invocation,
			witness: {
				version: 1,
				attemptId: challenge.attemptId,
				challengeToken: challenge.token,
				criterion: challenge.criterion,
				probe: invocation.witness.probe,
			},
		});
		const generated = binding?.generatedInput as { code: string };
		const values = JSON.stringify({ expected, selector: invocation.witness.probe.selector });
		expect(generated).toEqual({
			action: "run",
			name: challenge.token,
			code: `const observed = await tab.evaluate(({ selector, expected }) => document.querySelector(selector)?.textContent === expected, ${values});\nassert(observed, ${JSON.stringify(challenge.criterion)});`,
		});
		expect(uiEvidenceBinding(stateCard, {
			toolName: "write",
			input: {
				path: "xd://browser",
				content: JSON.stringify({
					action: "run",
					name: challenge.token,
					witness: { version: 1, probe: { kind: "url", expected: " http://fixture.test " } },
				}),
			},
		})?.witness?.probe).toEqual({ kind: "url", expected: " http://fixture.test " });
	});

	test("host assertion alone decides successful and false observations", async () => {
		const invocation = {
			action: "run",
			name: challenge.token,
			witness: { version: 1, probe: { kind: "url", expected: "http://fixture.test/" } },
		};
		const binding = uiEvidenceBinding(stateCard, { toolName: "browser", input: invocation });
		const generated = binding?.generatedInput as { code: string };
		const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor as new (
			...args: string[]
		) => (...values: unknown[]) => Promise<unknown>;
		const execute = new AsyncFunction("tab", "assert", generated.code);
		const hostAssert = (observed: unknown, message: string) => {
			if (!observed) throw new Error(message);
		};
		await expect(execute({ evaluate: async () => false }, hostAssert)).rejects.toThrow(challenge.criterion);
		await expect(execute({ evaluate: async () => true }, hostAssert)).resolves.toBeUndefined();
	});

	test("rejects closed-schema violations, lexical assertions, and completed witnesses", () => {
		const invocation = {
			action: "run",
			name: challenge.token,
			witness: { version: 1, probe: { kind: "url", expected: "http://fixture.test" } },
		};
		for (const malformed of [
			{ ...invocation, code: "assert(document.title)" },
			{ action: "run", name: challenge.token, code: "console.assert(document.title)" },
			{ action: "run", name: challenge.token, code: "try { assert(false) } catch {}" },
			{ action: "run", name: challenge.token, code: "Promise.reject().catch(() => assert(false))" },
			{ action: "run", name: challenge.token, code: "return; assert(false)" },
			{ action: "run", name: challenge.token, code: "if (false) assert(false)" },
			{ ...invocation, witness: { version: 1, probe: { kind: "url", expected: "", extra: true } } },
			{ ...invocation, witness: { version: 1, probe: { kind: "dom", selector: "#x", expected: "ok", extra: true } } },
			{ ...invocation, witness: { version: 2, probe: invocation.witness.probe } },
			{ ...invocation, witness: { version: 1, probe: { kind: "unknown", expected: "ok" } } },
			{ ...invocation, witness: { version: 1, probe: { kind: "url", expected: "\ud800" } } },
		]) {
			expect(uiEvidenceBinding(stateCard, { toolName: "browser", input: malformed })).toBeUndefined();
		}
		expect(uiEvidenceBinding({
			...stateCard,
			evidenceRequests: [{ ...challenge, completedStages: ["open", "witness"] }],
		}, { toolName: "browser", input: invocation })).toBeUndefined();
	});

	test("records only a successful one-shot pending declarative witness", async () => {
		const active = {
			...card("witness-run", 0, "pregate_pending"),
			evidenceRequests: [challenge],
		};
		const harness = adapterHarness((command, request) => {
			if (command === "metadata") return coreResponse({ omp: { slots: { scout: { alias: "@pocock-scout" } } } });
			if (command === "start") return coreCard(active);
			if (command === "record-evidence") {
				return coreCard({
					...active,
					revision: 1,
					stateHash: "witness-run-1-hash",
					evidenceRequests: [{ ...challenge, completedStages: ["open", "witness"] }],
				});
			}
			throw new Error(`Unexpected core command ${command}`);
		});
		await harness.enter("enter-witness", { entry: "full", objective: "Record witness" }, undefined, undefined, harness.context);
		const invocation = {
			action: "run",
			name: challenge.token,
			witness: { version: 1, probe: { kind: "url", expected: "http://fixture.test/" } },
		};
		const replacement = await harness.toolCall({
			toolName: "browser",
			toolCallId: "witness-call",
			input: invocation,
		}, harness.context);
		expect(replacement).toEqual({
			input: {
				action: "run",
				name: challenge.token,
				code: `const observed = await tab.evaluate((expected) => location.href === expected, ${JSON.stringify(invocation.witness.probe.expected)});\nassert(observed, ${JSON.stringify(challenge.criterion)});`,
			},
		});
		if (!isJsonRecord(replacement) || !isJsonRecord(replacement.input)) throw new Error("Witness replacement is malformed");
		await harness.toolResult({
			toolName: "browser",
			toolCallId: "witness-call",
			input: replacement.input,
			details: { returned: true },
			content: [{ type: "text", text: "true" }],
			isError: false,
		}, harness.context);
		const records = harness.requests.filter(entry => entry.command === "record-evidence");
		expect(records).toHaveLength(1);
		expect(records[0]!.request).toMatchObject({
			stage: "witness",
			attemptIds: [challenge.attemptId],
			challengeToken: challenge.token,
			invocation,
			witness: {
				version: 1,
				attemptId: challenge.attemptId,
				challengeToken: challenge.token,
				criterion: challenge.criterion,
				probe: invocation.witness.probe,
			},
		});
		await harness.toolResult({
			toolName: "browser",
			toolCallId: "witness-call",
			input: invocation,
			details: { returned: true },
			isError: false,
		}, harness.context);
		expect(harness.requests.filter(entry => entry.command === "record-evidence")).toHaveLength(1);
	});

	test("drops failed, forged, and legacy witness attempts before recording evidence", async () => {
		const active = {
			...card("rejected-witness-run", 0, "pregate_pending"),
			evidenceRequests: [challenge],
		};
		const harness = adapterHarness((command, request) => {
			if (command === "metadata") return coreResponse({ omp: { slots: { scout: { alias: "@pocock-scout" } } } });
			if (command === "start") return coreCard(active);
			if (command === "record-evidence") return coreCard(active);
			throw new Error(`Unexpected core command ${command}`);
		});
		await harness.enter("enter-rejected-witness", { entry: "full", objective: "Reject witness" }, undefined, undefined, harness.context);
		const valid = {
			action: "run",
			name: challenge.token,
			witness: { version: 1, probe: { kind: "url", expected: "http://fixture.test/" } },
		};
		await harness.toolCall({ toolName: "browser", toolCallId: "failed-witness", input: valid }, harness.context);
		const failure = await harness.toolResult({
			toolName: "browser",
			toolCallId: "failed-witness",
			input: valid,
			details: { returned: false },
			isError: true,
		}, harness.context);
		expect(failure).toMatchObject({ isError: true });
		expect(await harness.toolCall({
			toolName: "browser",
			toolCallId: "forged-witness",
			input: { ...valid, witness: { version: 1, probe: { kind: "url", expected: "http://fixture.test/", extra: true } } },
		}, harness.context)).toBeUndefined();
		await harness.toolResult({
			toolName: "browser",
			toolCallId: "legacy-witness",
			input: { action: "run", name: challenge.token, code: "assert(document.title, 'fixture renders')" },
			details: {},
			isError: false,
		}, harness.context);
		expect(harness.requests.filter(entry => entry.command === "record-evidence")).toHaveLength(0);
	});
});
