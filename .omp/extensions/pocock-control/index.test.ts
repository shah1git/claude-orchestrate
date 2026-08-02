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
			return {
				card: {
					...card("old-run", 4, "ready"),
					manifestFingerprint,
					nextActions: [],
					blockedReason: "effective Pocock runtime differs",
					runtimeMismatch: { expected: "old", observed: "new" },
				},
			};
		}
		if (command === "metadata") return { omp: { lanes: { producer: { alias: "producer" } } } };
		if (command === "start") return { card: { ...card("new-run", 0, "frontier_admission"), manifestFingerprint } };
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

type CoreRequest = {
	command: string;
	request: Record<string, unknown>;
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
	const schema = {
		min: () => schema,
		int: () => schema,
		nonnegative: () => schema,
		optional: () => schema,
		extend: () => schema,
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
			family: () => "gpt-5",
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
		exec: async (_program: string, args: string[]) => {
			const requestPath = args[args.indexOf("--request-file") + 1];
			if (!requestPath) throw new Error("Pocock core request path is missing");
			const parsed: unknown = JSON.parse(await Bun.file(requestPath).text());
			if (!isJsonRecord(parsed)) throw new Error("Pocock core request must be an object");
			const request = parsed;
			const command = args[1];
			if (!command) throw new Error("Pocock core command is missing");
			requests.push({ command, request });
			const response = respond?.(command, request) ?? (
				command === "metadata"
					? { omp: { lanes: { producer: { alias: "producer" } } } }
					: command === "start"
						? card(`${request.entry}-run`, 0, "producer_dispatch_pending")
						: card(String(request.runId), Number(request.revision) + 1, "completed")
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
			laneAlias: "@advisor",
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
			if (command === "metadata") return { omp: { lanes: { producer: { alias: "producer" } } } };
			if (command === "start") {
				manifestFingerprint = String(request.manifestFingerprint);
				return { ...card("projection-run", 0, "producer_dispatch_pending"), manifestFingerprint };
			}
			if (command === "transition") return { ...projectedCard, manifestFingerprint };
			if (command === "seal-task") {
				return {
					...projectedCard,
					manifestFingerprint,
					dispatchId: "dispatch-opaque",
					attemptIds: [actor.attemptId],
					taskInput: sealedTaskInput,
				};
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
			actor.laneAlias,
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
			actor.laneAlias,
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

	test("clears the dispatch widget for terminal, no-mirror, and fail-closed states", async () => {
		const actorCard = {
			...card("clear-run", 1, "producer_dispatch_pending"),
			dispatch: {
				actors: [{
					dispatchName: "P/L clear",
					ticketId: "T-clear",
					laneAlias: "producer",
					declaredModel: "openai/gpt-5",
					observedModel: "openai/gpt-5",
					modelWitness: "clear-witness",
					status: "prepared",
				}],
			},
		};
		let transitions = 0;
		const harness = adapterHarness(command => {
			if (command === "metadata") return { omp: { lanes: { producer: { alias: "producer" } } } };
			if (command === "start") return card("clear-run", 0, "producer_dispatch_pending");
			if (command === "transition") {
				transitions += 1;
				return transitions === 1 ? actorCard : card("clear-run", 2, "completed");
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
			if (command === "metadata") return { omp: { lanes: { producer: { alias: "producer" } } } };
			if (command === "start") {
				manifestFingerprint = String(request.manifestFingerprint);
				return { ...card("result-run", 0, "producer_dispatch_pending"), manifestFingerprint };
			}
			if (command === "seal-task") {
				return {
					...card("result-run", 1, "producer_dispatch_pending"),
					manifestFingerprint,
					dispatchId: "dispatch-results",
					attemptIds: ["attempt-1", "attempt-2"],
					taskInput: sealedTaskInput,
				};
			}
			if (command === "record-task-result") return { ...card("result-run", 2, "completed"), manifestFingerprint };
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
			if (command === "metadata") return { omp: { lanes: { producer: { alias: "producer" } } } };
			if (command === "start") return card(runId, 0, "ready");
			if (command === "transition") return card(runId, 1, "completed");
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

describe("uiEvidenceBinding", () => {
	const challenge = {
		attemptId: "run.w1.T1.a1",
		token: "pocock-ui-token",
		target: "http://fixture.test",
		criterion: "fixture renders",
		requiredStages: ["open", "exercise"],
		completedStages: [],
	};
	const card = {
		runId: "run-1",
		revision: 3,
		stateHash: "hash",
		manifestFingerprint: "manifest",
		phase: "pregate_pending",
		evidenceRequests: [challenge],
	};

	test("binds only the issued target and token to the open challenge", () => {
		const invocation = { action: "open", name: challenge.token, url: challenge.target };
		expect(uiEvidenceBinding(card, {
			toolName: "write",
			input: { path: "xd://browser", content: JSON.stringify(invocation) },
		})).toEqual({ challenge, stage: "open", tool: "xdev", invocation });
		expect(uiEvidenceBinding(card, {
			toolName: "write",
			input: {
				path: "xd://browser",
				content: JSON.stringify({ action: "open", name: challenge.token, url: "http://other.test" }),
			},
		})).toBeUndefined();
		expect(uiEvidenceBinding(card, {
			toolName: "browser",
			input: { action: "open", url: challenge.target },
		})).toBeUndefined();
	});

	test("requires recorded open evidence and a host assertion before exercise", () => {
		const opened = { ...card, evidenceRequests: [{ ...challenge, completedStages: ["open"] }] };
		expect(uiEvidenceBinding(card, {
			toolName: "browser",
			input: { action: "run", name: challenge.token, code: "assert(true)" },
		})).toBeUndefined();
		expect(uiEvidenceBinding(opened, {
			toolName: "browser",
			input: { action: "run", name: challenge.token, code: "return document.title" },
		})).toBeUndefined();
		expect(uiEvidenceBinding(opened, {
			toolName: "browser",
			input: { action: "run", name: challenge.token, code: "assert(true, 'fixture renders')" },
		})).toBeUndefined();
		expect(uiEvidenceBinding(opened, {
			toolName: "browser",
			input: {
				action: "run",
				name: challenge.token,
				code: "const rendered = document.title.length > 0; assert(rendered, 'fixture renders')",
			},
		})?.stage).toBe("exercise");
	});
});
