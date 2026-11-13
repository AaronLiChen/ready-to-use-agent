import type { Capture, Item } from "./core.ts";

export const RECOVERY_TOKENS = 60_000;
export const RECOVERY_BYTES = 4 * 1024 * 1024;
export const READABLE_RECOVERY_TOKENS = 150_000;
export type RecoveryMeasure = (message: Item) => { tokens: number; bytes: number };

export class NativeInputOverflow extends Error {}

export type Group = { messages: Item[]; tokens: number; bytes: number };
export type RecoveryProgress = { messagesDone: number; messagesTotal: number; requests: number };
export type RecoveryReply = { capture: Capture; usage?: Item };

export function recoveryMessageTokens(message: Item): number {
	let chars = 0;
	if (typeof message.content === "string") chars += message.content.length;
	else if (Array.isArray(message.content)) {
		for (const part of message.content) {
			if (part.type === "text") chars += (part.text ?? "").length;
			if (part.type === "thinking") chars += (part.thinking ?? "").length;
			if (part.type === "toolCall") chars += JSON.stringify(part.arguments ?? {}).length;
			if (part.type === "image") chars += 16_000;
		}
	}
	chars += (message.summary ?? message.output ?? "").length;
	const visible = Math.ceil(chars / 4) + 32;
	const output = message.role === "assistant" ? Number(message.usage?.output) || 0 : 0;
	return Math.max(visible, output);
}

export function readableRecoveryMessages(messages: Item[]): Item[] {
	return messages.map((message) => message.role !== "assistant" || !Array.isArray(message.content) ? message : {
		...message,
		content: message.content.flatMap((part: Item) => part.type !== "thinking" ? [part]
			: typeof part.thinking === "string" && part.thinking.trim() ? [{ type: "text", text: part.thinking }] : []),
	});
}

export function readableRecoveryMeasure(message: Item): { tokens: number; bytes: number } {
	const copy = readableRecoveryMessages([message])[0];
	const measured = copy.role === "assistant" && copy.usage ? {
		...copy, usage: { ...copy.usage, output: Math.max(0, (copy.usage.output ?? 0) - (copy.usage.reasoning ?? 0)) },
	} : copy;
	return { tokens: recoveryMessageTokens(measured), bytes: Buffer.byteLength(JSON.stringify(copy)) };
}

export function recoveryGroups(messages: Item[], measure: RecoveryMeasure = (message) => ({
	tokens: recoveryMessageTokens(message), bytes: Buffer.byteLength(JSON.stringify(message)),
})): Group[] {
	const groups: Group[] = [];
	const resultIds = new Set(messages.filter((message) => message.role === "toolResult").map((message) => message.toolCallId));
	const pending = new Set<string>();
	for (const message of messages) {
		const size = measure(message);
		if (groups.length && (pending.size || message.role === "toolResult")) {
			const previous = groups[groups.length - 1];
			previous.messages.push(message);
			previous.tokens += size.tokens;
			previous.bytes += size.bytes;
		} else {
			groups.push({ messages: [message], ...size });
		}
		if (message.role === "assistant" && Array.isArray(message.content)) {
			// Pi's provider serializer repairs interrupted calls that have no saved result.
			for (const part of message.content) if (part.type === "toolCall" && resultIds.has(part.id)) pending.add(part.id);
		}
		if (message.role === "toolResult") pending.delete(message.toolCallId);
	}
	return groups;
}

export function recoveryBatches(groups: Group[], tokens = RECOVERY_TOKENS, bytes = RECOVERY_BYTES): Group[][] {
	if (!(tokens > 0) || !(bytes > 0)) throw new Error("Native recovery requires positive request budgets.");
	const batches: Group[][] = [];
	let batch: Group[] = [];
	let usedTokens = 0;
	let usedBytes = 0;
	for (const group of groups) {
		if (batch.length && (usedTokens + group.tokens > tokens || usedBytes + group.bytes > bytes)) {
			batches.push(batch); batch = []; usedTokens = 0; usedBytes = 0;
		}
		batch.push(group); usedTokens += group.tokens; usedBytes += group.bytes;
	}
	if (batch.length) batches.push(batch);
	return batches;
}

export function splitPendingTurn(messages: Item[], preservePending: boolean): { history: Item[]; retained: Item[] } {
	if (!preservePending) return { history: messages, retained: [] };
	let last = messages.length - 1;
	while (last >= 0 && messages[last].role === "assistant" && ["error", "aborted"].includes(messages[last].stopReason)) last--;
	if (last < 0) return { history: messages, retained: [] };
	let boundary = last;
	if (messages[last].role === "toolResult") {
		while (boundary >= 0 && messages[boundary].role === "toolResult") boundary--;
		if (boundary < 0 || messages[boundary].role !== "assistant") return { history: messages, retained: [] };
	} else if (!["user", "custom", "bashExecution"].includes(messages[last].role)) {
		return { history: messages, retained: [] };
	}
	return { history: messages.slice(0, boundary), retained: messages.slice(boundary) };
}

function sumUsage(total: Item | undefined, usage: Item): Item {
	const keys = ["input", "output", "cacheRead", "cacheWrite", "cacheWrite1h", "reasoning", "totalTokens"];
	const result: Item = {};
	for (const key of keys) {
		if (usage[key] !== undefined || total?.[key] !== undefined) result[key] = (total?.[key] ?? 0) + (usage[key] ?? 0);
	}
	result.cost = {};
	for (const key of ["input", "output", "cacheRead", "cacheWrite", "total"]) {
		result.cost[key] = (total?.cost?.[key] ?? 0) + (usage.cost?.[key] ?? 0);
	}
	return result;
}

export async function recoverNativeHistory(options: {
	messages: Item[];
	initial?: Capture;
	emptyAnchor?: Item;
	tokens?: number;
	bytes?: number;
	measure?: RecoveryMeasure;
	signal: AbortSignal;
	request: (messages: Item[], previous: Capture | undefined, allowContinuation: boolean) => Promise<RecoveryReply>;
	save: (capture: Capture, anchor: Item, progress: RecoveryProgress) => void | Promise<void>;
}): Promise<RecoveryReply & { progress: RecoveryProgress }> {
	const groups = recoveryGroups(options.messages, options.measure);
	const queue = recoveryBatches(groups, options.tokens, options.bytes);
	if (!queue.length && options.initial && options.emptyAnchor) queue.push([]);
	let capture = options.initial;
	let usage: Item | undefined;
	let messagesDone = 0;
	let requests = 0;
	const messagesTotal = options.messages.length;
	let allowContinuation = queue.length > 1 || !!options.initial;
	while (queue.length) {
		options.signal.throwIfAborted();
		const batch = queue.shift()!;
		const messages = batch.flatMap((group) => group.messages);
		let result: RecoveryReply;
		try {
			requests++;
			result = await options.request(messages, capture, allowContinuation);
		} catch (error) {
			options.signal.throwIfAborted();
			if (!(error instanceof NativeInputOverflow) || batch.length < 2) throw error;
			// Split only complete message groups. Never cut a tool call from its results.
			const middle = Math.floor(batch.length / 2);
			allowContinuation = true;
			queue.unshift(batch.slice(0, middle), batch.slice(middle));
			continue;
		}
		options.signal.throwIfAborted();
		capture = result.capture;
		if (result.usage) usage = sumUsage(usage, result.usage);
		messagesDone += messages.length;
		await options.save(capture, messages[messages.length - 1] ?? options.emptyAnchor!, { messagesDone, messagesTotal, requests });
	}
	if (!capture) throw new Error("This session has no older context available for native recovery.");
	return { capture, usage, progress: { messagesDone, messagesTotal, requests } };
}
