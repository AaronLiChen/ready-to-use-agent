import { randomUUID } from "node:crypto";
import {
	buildSessionContext, convertToLlm, sessionEntryToContextMessages,
	type ExtensionAPI, type ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import type { Context, Model, Provider, StreamOptions, Usage } from "@earendil-works/pi-ai";
import {
	CHECKPOINT_TYPE, DEFAULT_THRESHOLD, FORCE_THRESHOLD, MARKER_PREFIX, MODE_TYPE, SUMMARY,
	captureCompletedCompactionItem, captureCompletedResponse, latestCheckpoint, latestCompactionIsNative, makeCheckpoint,
	mergeUnpersistedTail, messageKey, observingFetch, readableBranch, rewritePayload, supportsNativeCompaction,
	tailAfterCheckpoint, thresholdFor,
	type Capture, type Checkpoint, type Item,
} from "./core.ts";

import { NativeInputOverflow, READABLE_RECOVERY_TOKENS, readableRecoveryMeasure, readableRecoveryMessages, recoverNativeHistory, recoveryBatches, recoveryGroups, splitPendingTurn } from "./recovery.ts";

type AgentMessage = Parameters<typeof convertToLlm>[0][number];

type RequestState = {
	marker: string;
	threshold: number;
	checkpoint?: Checkpoint;
	force: boolean;
	capture?: Capture;
	payload?: Item;
	allowContinuation?: boolean;
	responseId?: string;
	compactionAbort?: AbortController;
	completedItemReason?: object;
};

type Options = { thresholdTokens?: number; recoveryTokens?: number; recoveryBytes?: number };

export function installNativeCompaction(pi: ExtensionAPI, options: Options = {}): void {
	const requestedThreshold = options.thresholdTokens ?? DEFAULT_THRESHOLD;
	thresholdFor({ provider: "openai", id: "gpt-test", api: "openai-responses" }, requestedThreshold);
	const requests = new Map<string, RequestState>();
	const completed = new Map<string, Capture>();
	const wrappedProviders = new Set<string>();
	let readableRecovery = false;
	let lastNativeStats: Item | undefined;

	const enabled = (ctx: ExtensionContext): boolean => {
		const branch = ctx.sessionManager.getBranch();
		for (let index = branch.length - 1; index >= 0; index--) {
			const entry = branch[index];
			if (entry.type === "custom" && entry.customType === MODE_TYPE && typeof (entry.data as Item)?.enabled === "boolean") {
				return (entry.data as Item).enabled;
			}
		}
		return true;
	};

	const active = (ctx: ExtensionContext): boolean => enabled(ctx) && supportsNativeCompaction(ctx.model);

	const fullMessages = (messages: AgentMessage[], ctx: ExtensionContext): AgentMessage[] => {
		const branch = ctx.sessionManager.getBranch();
		if (!latestCompactionIsNative(branch)) return messages;
		// Other models cannot use an encrypted OpenAI checkpoint. Restore their readable history.
		const restored = buildSessionContext(readableBranch(branch) as typeof branch).messages;
		return mergeUnpersistedTail(restored, messages) as AgentMessage[];
	};

	const prepare = (messages: AgentMessage[], ctx: ExtensionContext, force = false): { messages: AgentMessage[]; state: RequestState } => {
		const full = fullMessages(messages, ctx);
		let checkpoint = latestCheckpoint(ctx.sessionManager.getBranch(), ctx.model!);
		const tail = tailAfterCheckpoint(full, checkpoint);
		if (!tail) checkpoint = undefined;
		const marker = `${MARKER_PREFIX}${randomUUID()}]`;
		const state: RequestState = {
			marker, checkpoint, force,
			threshold: force ? FORCE_THRESHOLD : thresholdFor(ctx.model!, requestedThreshold),
		};
		requests.set(marker, state);
		return {
			messages: [{ role: "user", content: marker, timestamp: Date.now() }, ...(tail ?? full)] as AgentMessage[],
			state,
		};
	};

	const requestState = (context: Context): RequestState | undefined => {
		for (const message of context.messages) {
			if (message.role !== "user") continue;
			const text = typeof message.content === "string" ? message.content :
				message.content.length === 1 && message.content[0].type === "text" ? message.content[0].text : undefined;
			if (text && requests.has(text)) {
				const state = requests.get(text)!;
				requests.delete(text);
				return state;
			}
		}
		return undefined;
	};

	const nativeOptions = (model: Model<any>, state: RequestState, original: StreamOptions = {}): StreamOptions => {
		if (state.checkpoint && new URL(model.baseUrl).origin !== state.checkpoint.origin) {
			throw new Error("The provider endpoint changed. Use /native-compact off, then retry with the full history.");
		}
		return {
			...original,
			transport: "sse",
			fetch: observingFetch(original.fetch ?? globalThis.fetch, (capture) => {
				if (!state.capture?.completedItemOnly) state.capture = capture;
				if (!state.force) completed.set(capture.responseId, capture);
			}, (event, origin) => {
				if (state.allowContinuation && !state.capture && state.payload) {
					state.capture = captureCompletedResponse({ ...event, response: { ...event.response,
						output: [...state.payload.input, ...event.response.output],
					} }, origin);
				}
			}, (event, origin) => {
				if (!state.force) return;
				if (event.type === "response.created") state.responseId = event.response?.id;
				const item = captureCompletedCompactionItem(event, state.responseId, origin);
				if (item && !state.capture?.completedItemOnly) {
					state.capture = item;
					state.compactionAbort?.abort(state.completedItemReason);
				}
				if (["response.created", "response.output_item.added", "response.output_item.done", "response.completed", "response.incomplete", "response.failed"].includes(event.type)) {
					lastNativeStats = { ...lastNativeStats, lastEvent: event.type,
						itemType: event.item?.type, incompleteReason: event.response?.incomplete_details?.reason,
						usage: event.response?.usage };
					pi.appendEntry("openai-native-compaction-event-v1", { ...lastNativeStats });
				}
			}),
			onResponse: async (response, requestModel) => {
				await original.onResponse?.(response, requestModel);
				if (state.force) {
					lastNativeStats = { ...lastNativeStats, status: response.status };
					pi.appendEntry("openai-native-compaction-response-v1", { ...lastNativeStats });
				}
			},
			onPayload: async (payload, requestModel) => {
				const replacement = await original.onPayload?.(payload, requestModel);
				state.payload = rewritePayload((replacement === undefined ? payload : replacement) as Item,
					state.marker, state.threshold, state.checkpoint);
				if (state.force) {
					if (Array.isArray(state.payload.tools) && state.payload.tools.length) throw new Error("Native compaction-only requests cannot expose task tools.");
					lastNativeStats = { model: state.payload.model, items: state.payload.input.length,
						wireBytes: Buffer.byteLength(JSON.stringify(state.payload)), maxOutputTokens: state.payload.max_output_tokens,
						reasoning: state.payload.reasoning, contextManagement: state.payload.context_management,
						encryptedReasoningItems: state.payload.input.filter((item: Item) => item.type === "reasoning" && item.encrypted_content).length,
						replay: readableRecovery ? "readable" : "full" };
					pi.appendEntry("openai-native-compaction-request-v1", { ...lastNativeStats });
				}
				return state.payload;
			},
		};
	};

	const ensureProvider = (ctx: ExtensionContext): void => {
		if (!active(ctx) || wrappedProviders.has(ctx.model!.provider)) return;
		const original = ctx.modelRegistry.getProvider(ctx.model!.provider);
		if (!original) throw new Error("The current GPT provider is unavailable.");
		const wrapped: Provider = {
			...original,
			stream(model, context, streamOptions) {
				const state = requestState(context);
				return original.stream(model, context, (state ? nativeOptions(model, state, streamOptions) : streamOptions) as typeof streamOptions);
			},
			streamSimple(model, context, streamOptions) {
				const state = requestState(context);
				return original.streamSimple(model, context, state ? nativeOptions(model, state, streamOptions) : streamOptions);
			},
		};
		pi.registerProvider(wrapped);
		wrappedProviders.add(original.id);
	};

	const showStatus = (ctx: ExtensionContext): void => {
		if (!ctx.hasUI) return;
		ctx.ui.setStatus("openai-native-compaction", active(ctx) ? `compact: OpenAI (${thresholdFor(ctx.model!, requestedThreshold).toLocaleString()})` : undefined);
	};

	pi.on("session_start", (_event, ctx) => { ensureProvider(ctx); showStatus(ctx); });
	pi.on("model_select", (_event, ctx) => { ensureProvider(ctx); showStatus(ctx); });

	pi.on("context", (event, ctx) => {
		requests.clear();
		if (!active(ctx)) {
			const restored = fullMessages(event.messages, ctx);
			return restored === event.messages ? undefined : { messages: restored };
		}
		ensureProvider(ctx);
		return { messages: prepare(event.messages, ctx).messages };
	});

	pi.on("turn_end", (event, ctx) => {
		const message = event.message;
		if (message.role !== "assistant") return;
		const responseId = (message as Item).responseId;
		const capture = completed.get(responseId);
		completed.delete(responseId);
		if (!capture || !active(ctx) || !["stop", "toolUse"].includes(message.stopReason)) return;
		if (message.provider !== ctx.model!.provider || message.model !== ctx.model!.id) return;
		const anchor = [...ctx.sessionManager.getBranch()].reverse().find((entry) => entry.type === "message" &&
			entry.message.role === "assistant" && (entry.message as Item).responseId === responseId);
		if (!anchor) throw new Error("The native compaction response has no durable assistant entry.");
		pi.appendEntry(CHECKPOINT_TYPE, makeCheckpoint(capture, ctx.model!, anchor.id, message, thresholdFor(ctx.model!, requestedThreshold)));
		if (ctx.hasUI) ctx.ui.notify("OpenAI native compaction completed. The encrypted checkpoint is saved in this session.", "info");
	});

	pi.on("session_before_compact", async (event, ctx) => {
		if (!active(ctx)) return;
		try {
			ensureProvider(ctx);
			const branch = ctx.sessionManager.getBranch();
			const messages = fullMessages(buildSessionContext(branch).messages, ctx);
			const split = splitPendingTurn(messages, event.reason !== "manual");
			const anchors = new Map(branch.flatMap((entry) => sessionEntryToContextMessages(entry).map((message) => [messageKey(message), entry] as const)));
			const last = split.history.at(-1);
			const anchor = last && anchors.get(messageKey(last));
			if (!anchor || !last) throw new Error("This session has no older context to compact safely.");
			let initial = latestCheckpoint(branch, ctx.model!);
			const tail = tailAfterCheckpoint(split.history, initial);
			if (!tail) initial = undefined;
			const pending = (tail ?? split.history) as AgentMessage[];
			const forceThreshold = Math.max(FORCE_THRESHOLD, Math.ceil(ctx.getSystemPrompt().length / 3) + 4_096);
			if (event.customInstructions && ctx.hasUI) {
				ctx.ui.notify("Native compaction does not use Pi summary instructions.", "warning");
			}
			const measure = readableRecovery ? readableRecoveryMeasure : undefined;
			const tokenBudget = options.recoveryTokens ?? (readableRecovery
				? Math.min(READABLE_RECOVERY_TOKENS, ctx.model!.contextWindow > 0 ? Math.floor(ctx.model!.contextWindow / 3) : READABLE_RECOVERY_TOKENS) : undefined);
			const batches = recoveryBatches(recoveryGroups(pending, measure), tokenBudget, options.recoveryBytes).length;
			if (batches > 1 && ctx.hasUI) ctx.ui.notify(`Native recovery will process ${pending.length} messages in smaller requests.`, "info");
			const signal = event.signal;
			let checkpoint: Checkpoint | undefined;
			const recovered = await recoverNativeHistory({
				messages: pending, initial, emptyAnchor: last, signal, tokens: tokenBudget, bytes: options.recoveryBytes, measure,
				request: async (batch, previous, allowContinuation) => {
					const marker = `${MARKER_PREFIX}${randomUUID()}]`;
					const compactionAbort = new AbortController();
					const completedItemReason = { nativeCompactionItemCompleted: true };
					const state: RequestState = { marker, force: true, threshold: forceThreshold, allowContinuation, compactionAbort, completedItemReason,
						checkpoint: previous ? makeCheckpoint(previous, ctx.model!, anchor.id, last, forceThreshold) : undefined };
					requests.set(marker, state);
					const requestSignal = AbortSignal.any([signal, AbortSignal.timeout(180_000), compactionAbort.signal]);
					const response = await ctx.modelRegistry.complete(ctx.model!, {
						systemPrompt: ctx.getSystemPrompt(),
						messages: convertToLlm([
							{ role: "user", content: marker, timestamp: Date.now() },
							...(readableRecovery ? readableRecoveryMessages(batch) : batch) as AgentMessage[],
							{ role: "user", timestamp: Date.now(),
								content: "This is a compaction-only request, not a task instruction. Return only COMPACTION_COMPLETE for this request." },
						]),
					}, { maxTokens: Math.min(ctx.model!.maxTokens, 32_768), reasoningEffort: "low", signal: requestSignal,
						maxRetries: 2, maxRetryDelayMs: 30_000,
						cacheRetention: "none", sessionId: ctx.sessionManager.getSessionId() });
					if (state.capture?.completedItemOnly && requestSignal.reason === completedItemReason) {
						signal.throwIfAborted();
						return { capture: state.capture };
					}
					requestSignal.throwIfAborted();
					if (response.stopReason !== "stop" || !state.capture) {
						const error = response.errorMessage || (response.stopReason !== "stop"
							? `Native compaction ended with ${response.stopReason} after ${response.usage.output} output tokens. The original history remains unchanged.`
							: "The endpoint returned no completed native compaction item. The original history remains unchanged.");
						if (response.stopReason === "error" && /exceeds the context window|context[_ ]length[_ ]exceeded|prompt is too long|maximum context length/i.test(error)) {
							throw new NativeInputOverflow(error);
						}
						throw new Error(error);
					}
					return { capture: state.capture, usage: response.usage };
				},
				save: (capture, message, progress) => {
					const entry = anchors.get(messageKey(message));
					if (!entry) throw new Error("Native recovery cannot identify its history boundary.");
					checkpoint = makeCheckpoint(capture, ctx.model!, entry.id, message, forceThreshold);
					pi.appendEntry(CHECKPOINT_TYPE, { ...checkpoint, recovery: progress, replay: readableRecovery ? "readable" : "full" });
					if (ctx.hasUI) ctx.ui.setStatus("openai-native-compaction", `compact: OpenAI recovery ${progress.messagesDone}/${progress.messagesTotal}`);
				},
			});
			if (!checkpoint) throw new Error("Native recovery did not save a checkpoint.");
			const retainedEntry = split.retained[0] && anchors.get(messageKey(split.retained[0]));
			const fileOps = event.preparation.fileOps;
			return { compaction: {
				summary: SUMMARY,
				firstKeptEntryId: retainedEntry?.id ?? anchor.id,
				tokensBefore: event.preparation.tokensBefore,
				usage: recovered.usage as Usage | undefined,
				details: {
					openaiNative: checkpoint,
					recovery: recovered.progress,
					replay: readableRecovery ? "readable" : "full",
					readFiles: [...fileOps.read],
					modifiedFiles: [...new Set([...fileOps.written, ...fileOps.edited])],
				},
			} };
		} catch (error) {
			if (!event.signal.aborted) {
				const message = error instanceof Error ? error.message : String(error);
				pi.appendEntry("openai-native-compaction-failure-v1", { reason: event.reason, error: message.slice(0, 1000), request: lastNativeStats });
				if (ctx.hasUI) ctx.ui.notify(`Native compaction failed. Pi text compaction was skipped. ${message}`, "error");
			}
			// An exception would let Pi use its text summarizer. Cancel instead.
			return { cancel: true };
		} finally {
			showStatus(ctx);
		}
	});

	pi.registerCommand("native-compact", {
		description: "Show native compaction status, compact now, or set this session on/off",
		handler: async (args, ctx) => {
			const action = args.trim() || "status";
			if (action === "on" || action === "off") {
				await ctx.waitForIdle();
				pi.appendEntry(MODE_TYPE, { enabled: action === "on" });
				ensureProvider(ctx);
				showStatus(ctx);
				ctx.ui.notify(`OpenAI native compaction is ${action} for this session.`, "info");
				return;
			}
			if (action === "now" || action === "recover") {
				await ctx.waitForIdle();
				if (!active(ctx)) throw new Error("Native compaction is not active for this model and session.");
				readableRecovery = action === "recover";
				try {
					await new Promise<void>((resolve, reject) => ctx.compact({
						onComplete: () => { ctx.ui.notify("OpenAI native compaction completed.", "info"); resolve(); },
						onError: reject,
					}));
				} finally { readableRecovery = false; }
				return;
			}
			if (action !== "status") throw new Error("Use /native-compact status, now, recover, on, or off.");
			const checkpoint = ctx.model && latestCheckpoint(ctx.sessionManager.getBranch(), ctx.model);
			ctx.ui.notify(JSON.stringify({
				implementation: "bounded-recovery-v2",
				active: active(ctx), model: ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : null,
				thresholdTokens: ctx.model && supportsNativeCompaction(ctx.model) ? thresholdFor(ctx.model, requestedThreshold) : null,
				checkpoint: checkpoint ? { responseId: checkpoint.responseId, anchorEntryId: checkpoint.anchorEntryId } : null,
				transport: active(ctx) ? "sse" : "unchanged",
			}), "info");
		},
	});

	pi.on("session_shutdown", () => { requests.clear(); completed.clear(); });
}

export default function openaiNativeCompaction(pi: ExtensionAPI): void {
	installNativeCompaction(pi);
}
