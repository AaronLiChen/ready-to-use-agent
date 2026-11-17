import { createHash } from "node:crypto";

export const CHECKPOINT_TYPE = "openai-native-compaction-v1";
export const MODE_TYPE = "openai-native-compaction-mode-v1";
export const DEFAULT_THRESHOLD = 200_000;
export const FORCE_THRESHOLD = 16_384;
const MIN_THRESHOLD = 1_024;
export const MARKER_PREFIX = "[pi-openai-native-compaction:";
export const SUMMARY = "OpenAI native compaction checkpoint. The extension restores the encrypted context for this model.";

export type Item = Record<string, any>;
export type ModelIdentity = { provider: string; id: string; api: string; contextWindow?: number; baseUrl?: string };
export type Capture = { responseId: string; window: Item[]; origin: string; usage?: Item; completedItemOnly?: boolean };
export type Checkpoint = Capture & {
	version: 1;
	provider: string;
	model: string;
	api: string;
	anchorEntryId: string;
	anchorKey: string;
	threshold: number;
};

const PROVIDERS = new Set(["github-copilot", "openai", "openai-codex", "azure-openai-responses"]);
const APIS = new Set(["openai-responses", "openai-codex-responses", "azure-openai-responses"]);

export function supportsNativeCompaction(model: ModelIdentity | undefined): boolean {
	return !!model && /^gpt-/i.test(model.id) && PROVIDERS.has(model.provider) && APIS.has(model.api);
}

export function thresholdFor(model: ModelIdentity, requested = DEFAULT_THRESHOLD): number {
	if (!Number.isSafeInteger(requested) || requested < MIN_THRESHOLD) {
		throw new Error("The native compaction threshold must be an integer of at least 1024 tokens.");
	}
	const limit = model.contextWindow && model.contextWindow > 0 ? Math.floor(model.contextWindow * 0.7) : requested;
	return Math.max(MIN_THRESHOLD, Math.min(requested, limit));
}

export function messageKey(message: Item): string {
	const identity = message.responseId || message.toolCallId;
	return createHash("sha256").update(JSON.stringify([
		message.role, message.timestamp, identity ?? null,
		identity ? null : message.content ?? message.summary ?? message.output ?? null,
	])).digest("hex");
}

export function checkpointFromEntry(entry: Item): Checkpoint | undefined {
	const value = entry.type === "custom" && entry.customType === CHECKPOINT_TYPE
		? entry.data
		: entry.type === "compaction" ? entry.details?.openaiNative : undefined;
	if (value?.version !== 1 || typeof value.anchorEntryId !== "string" || typeof value.anchorKey !== "string" ||
		typeof value.provider !== "string" || typeof value.model !== "string" || typeof value.api !== "string" ||
		typeof value.origin !== "string" || typeof value.responseId !== "string" || !Array.isArray(value.window) ||
		!value.window.some((item: Item) => item?.type === "compaction" && typeof item.encrypted_content === "string" && item.encrypted_content.length > 0)) {
		return undefined;
	}
	return value;
}

export function latestCheckpoint(branch: Item[], model: ModelIdentity): Checkpoint | undefined {
	const ids = new Set(branch.map((entry) => entry.id));
	for (let index = branch.length - 1; index >= 0; index--) {
		const entry = branch[index];
		const checkpoint = checkpointFromEntry(entry);
		if (checkpoint && checkpoint.provider === model.provider && checkpoint.model === model.id &&
			checkpoint.api === model.api && ids.has(checkpoint.anchorEntryId)) return checkpoint;
		if (entry.type === "compaction" && !checkpoint) return undefined;
	}
	return undefined;
}

export function latestCompactionIsNative(branch: Item[]): boolean {
	for (let index = branch.length - 1; index >= 0; index--) {
		if (branch[index].type === "compaction") return branch[index].summary === SUMMARY || branch[index].details?.openaiNative !== undefined;
	}
	return false;
}

export function readableBranch(branch: Item[]): Item[] {
	return branch.map((entry) => entry.type === "compaction" && (entry.summary === SUMMARY || entry.details?.openaiNative !== undefined)
		? { type: "custom", id: entry.id, parentId: entry.parentId, timestamp: entry.timestamp, customType: `${CHECKPOINT_TYPE}-boundary` }
		: entry);
}

export function tailAfterCheckpoint(messages: Item[], checkpoint?: Checkpoint): Item[] | undefined {
	if (!checkpoint) return undefined;
	for (let index = messages.length - 1; index >= 0; index--) {
		if (messageKey(messages[index]) === checkpoint.anchorKey) return messages.slice(index + 1);
	}
	return undefined;
}

export function mergeUnpersistedTail(restored: Item[], current: Item[]): Item[] {
	const keys = new Set(restored.map(messageKey));
	for (let index = current.length - 1; index >= 0; index--) {
		if (keys.has(messageKey(current[index]))) return [...restored, ...current.slice(index + 1)];
	}
	return [...restored, ...current.filter((message) => message.role !== "compactionSummary")];
}

export function makeCheckpoint(capture: Capture, model: ModelIdentity, anchorEntryId: string, anchor: Item, threshold: number): Checkpoint {
	return {
		...capture, version: 1, provider: model.provider, model: model.id, api: model.api,
		anchorEntryId, anchorKey: messageKey(anchor), threshold,
	};
}

function itemText(item: Item): string | undefined {
	if (typeof item.content === "string") return item.content;
	if (Array.isArray(item.content) && item.content.length === 1 && typeof item.content[0]?.text === "string") {
		return item.content[0].text;
	}
	return undefined;
}

export function rewritePayload(payload: Item, marker: string, threshold: number, checkpoint?: Checkpoint): Item {
	if (!Array.isArray(payload?.input)) throw new Error("Native compaction requires a Responses input array.");
	let matches = 0;
	const input = payload.input.flatMap((item: Item) => {
		if (item.role !== "user" || itemText(item) !== marker) return [item];
		matches++;
		return checkpoint ? structuredClone(checkpoint.window) : [];
	});
	if (matches !== 1) throw new Error("The native compaction request marker is missing or duplicated.");
	const contextManagement = Array.isArray(payload.context_management)
		? payload.context_management.filter((entry: Item) => entry?.type !== "compaction") : [];
	const result: Item = {
		...payload, input, store: false,
		context_management: [...contextManagement, { type: "compaction", compact_threshold: threshold }],
	};
	delete result.previous_response_id;
	return result;
}

export function captureCompletedResponse(event: Item, origin: string): Capture | undefined {
	const response = event?.response;
	if (event?.type !== "response.completed" || response?.status !== "completed" ||
		typeof response.id !== "string" || !Array.isArray(response.output)) return undefined;
	let index = -1;
	for (let position = 0; position < response.output.length; position++) {
		const item = response.output[position];
		if (item?.type === "compaction" && typeof item.encrypted_content === "string" && item.encrypted_content.length > 0) index = position;
	}
	if (index < 0) return undefined;
	// Inline compaction permits removal of items before the latest compaction item.
	return { responseId: response.id, window: structuredClone(response.output.slice(index)), origin, usage: response.usage };
}

export function captureCompletedCompactionItem(event: Item, responseId: string | undefined, origin: string): Capture | undefined {
	const item = event?.item;
	if (event?.type !== "response.output_item.done" || !responseId || item?.type !== "compaction" ||
		typeof item.encrypted_content !== "string" || !item.encrypted_content) return undefined;
	return { responseId, origin, window: [structuredClone(item)], completedItemOnly: true };
}

export class SseObserver {
	private buffer = "";
	private data: string[] = [];
	private readonly onEvent: (event: Item) => void;
	constructor(onEvent: (event: Item) => void) { this.onEvent = onEvent; }
	feed(text: string): void {
		this.buffer += text;
		let newline: number;
		while ((newline = this.buffer.indexOf("\n")) >= 0) {
			const line = this.buffer.slice(0, newline).replace(/\r$/, "");
			this.buffer = this.buffer.slice(newline + 1);
			this.line(line);
		}
	}
	finish(): void {
		if (this.buffer) this.line(this.buffer.replace(/\r$/, ""));
		this.buffer = "";
		this.line("");
	}
	private line(line: string): void {
		if (line === "") {
			const data = this.data.join("\n");
			this.data = [];
			if (!data || data === "[DONE]") return;
			let event: Item;
			try { event = JSON.parse(data); } catch { return; }
			this.onEvent(event);
		} else if (line.startsWith("data:")) {
			this.data.push(line.slice(5).replace(/^ /, ""));
		}
	}
}

export function observingFetch(fetchImpl: typeof fetch, onCapture: (capture: Capture) => void,
	onCompleted?: (event: Item, origin: string) => void,
	onEvent?: (event: Item, origin: string) => void): typeof fetch {
	return async (input, init) => {
		const response = await fetchImpl(input, init);
		if (!response.ok || !response.body || !response.headers.get("content-type")?.includes("text/event-stream")) return response;
		const address = input instanceof Request ? input.url : String(input);
		const origin = new URL(address).origin;
		const observer = new SseObserver((event) => {
			onEvent?.(event, origin);
			const capture = captureCompletedResponse(event, origin);
			if (capture) onCapture(capture);
			if (event?.type === "response.completed" && event.response?.status === "completed") onCompleted?.(event, origin);
		});
		const decoder = new TextDecoder();
		const body = response.body.pipeThrough(new TransformStream<Uint8Array, Uint8Array>({
			transform(chunk, controller) {
				observer.feed(decoder.decode(chunk, { stream: true }));
				controller.enqueue(chunk);
			},
			flush() { observer.feed(decoder.decode()); observer.finish(); },
		}));
		return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
	};
}
