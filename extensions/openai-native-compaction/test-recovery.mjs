import assert from "node:assert/strict";
import { test } from "node:test";
import {
  NativeInputOverflow, readableRecoveryMeasure, readableRecoveryMessages, recoverNativeHistory, recoveryBatches, recoveryGroups, recoveryMessageTokens, splitPendingTurn,
} from "./recovery.ts";

const message = (index, extra = {}) => ({ role: "user", timestamp: index, content: `Synthetic message ${index}.`, ...extra });
const capture = (index) => ({ responseId: `resp_${index}`, origin: "https://synthetic.example.test", window: [{ type: "compaction", encrypted_content: `opaque_${index}` }] });
const usage = { input: 10, output: 5, cacheRead: 2, cacheWrite: 3, totalTokens: 20, cost: { input: 1, output: 2, cacheRead: 3, cacheWrite: 4, total: 10 } };
const signal = () => new AbortController().signal;

test("Recovery estimation includes hidden reasoning from the original output usage", () => {
  assert.equal(recoveryMessageTokens(message(1, { role: "assistant", usage: { output: 9000 }, content: [{ type: "thinking", thinking: "", thinkingSignature: "opaque" }] })), 9000);
  assert.ok(recoveryMessageTokens(message(1, { content: "x".repeat(8000) })) >= 2000);
});

test("Recovery keeps multiple tool calls, intervening metadata, and results in one group", () => {
  const assistant = message(2, { role: "assistant", content: [{ type: "toolCall", id: "call_1", arguments: {} }, { type: "toolCall", id: "call_2", arguments: {} }] });
  const custom = message(3, { role: "custom" });
  const first = message(4, { role: "toolResult", toolCallId: "call_2" });
  const second = message(5, { role: "toolResult", toolCallId: "call_1" });
  const all = [message(1), assistant, custom, first, second, message(6)];
  const groups = recoveryGroups(all);
  assert.equal(groups.length, 3);
  assert.deepEqual(groups[1].messages, [assistant, custom, first, second]);
  assert.deepEqual(recoveryBatches(groups, 1, 1).flat(2).flatMap((group) => group.messages), all);
});

test("Recovery request budgets never truncate a large indivisible message", () => {
  const all = [message(1), message(2, { content: "x".repeat(10000) }), message(3)];
  const batches = recoveryBatches(recoveryGroups(all), 100, 1000);
  assert.equal(batches.length, 3);
  assert.deepEqual(batches.flat().flatMap((group) => group.messages), all);
  assert.throws(() => recoveryBatches([], 0, 1000));
});

test("Automatic recovery keeps the pending user request outside the compacted history", () => {
  const all = [message(1), message(2, { role: "assistant", stopReason: "stop" }), message(3),
    message(4, { role: "assistant", stopReason: "error" })];
  assert.deepEqual(splitPendingTurn(all, true), { history: all.slice(0, 2), retained: all.slice(2) });
  assert.deepEqual(splitPendingTurn(all, false), { history: all, retained: [] });
});

test("Automatic recovery retains the current tool batch for model continuation", () => {
  const all = [message(1), message(2, { role: "assistant", content: [{ type: "toolCall", id: "call_1", arguments: {} }] }),
    message(3, { role: "toolResult", toolCallId: "call_1" }), message(4, { role: "assistant", stopReason: "error" })];
  assert.deepEqual(splitPendingTurn(all, true), { history: all.slice(0, 1), retained: all.slice(1) });
});

test("Recovery chains native checkpoints in order and accounts for every request", async () => {
  const all = Array.from({ length: 10 }, (_, index) => message(index));
  const seen = [];
  const saves = [];
  let requests = 0;
  const result = await recoverNativeHistory({ messages: all, signal: signal(), tokens: 100,
    request: async (batch, previous, allowContinuation) => {
      assert.equal(previous?.responseId, requests ? `resp_${requests}` : undefined);
      assert.equal(allowContinuation, true);
      seen.push(...batch); requests++;
      return { capture: capture(requests), usage };
    }, save: (state, anchor, progress) => saves.push({ state, anchor, progress }),
  });
  assert.deepEqual(seen, all);
  assert.equal(result.progress.messagesDone, all.length);
  assert.equal(result.usage.totalTokens, requests * 20);
  assert.equal(result.usage.cost.total, requests * 10);
  assert.equal(saves.at(-1).anchor, all.at(-1));
});

test("Recovery splits an oversized request without splitting message groups", async () => {
  const all = Array.from({ length: 8 }, (_, index) => message(index));
  const successful = [];
  let requests = 0;
  const result = await recoverNativeHistory({ messages: all, signal: signal(), tokens: 10000,
    request: async (batch) => {
      requests++;
      if (batch.length > 2) throw new NativeInputOverflow("Synthetic context overflow");
      successful.push(...batch);
      return { capture: capture(requests), usage };
    }, save: () => {},
  });
  assert.deepEqual(successful, all);
  assert.equal(result.progress.requests, 7);
});

test("Recovery stops for an indivisible oversized tool batch instead of deleting content", async () => {
  let calls = 0;
  const all = [message(1, { role: "assistant", content: [{ type: "toolCall", id: "call_1", arguments: {} }] }),
    message(2, { role: "toolResult", toolCallId: "call_1" })];
  await assert.rejects(recoverNativeHistory({ messages: all, signal: signal(),
    request: async () => { calls++; throw new NativeInputOverflow("Still too large"); }, save: () => assert.fail("No completed checkpoint exists"),
  }), NativeInputOverflow);
  assert.equal(calls, 1);
});

test("Recovery does not retry authentication or rate-limit failures as context overflows", async () => {
  let calls = 0;
  await assert.rejects(recoverNativeHistory({ messages: [message(1), message(2)], signal: signal(),
    request: async () => { calls++; throw new Error("Synthetic authentication failure"); }, save: () => {},
  }), /authentication failure/);
  assert.equal(calls, 1);
});

test("Cancellation retains only completed checkpoint progress", async () => {
  const controller = new AbortController();
  let requests = 0;
  let saves = 0;
  await assert.rejects(recoverNativeHistory({ messages: Array.from({ length: 5 }, (_, index) => message(index)), signal: controller.signal, tokens: 50,
    request: async () => { requests++; return { capture: capture(requests), usage }; },
    save: () => { saves++; controller.abort(new Error("Synthetic user cancellation")); },
  }), /Synthetic user cancellation/);
  assert.equal(requests, 1);
  assert.equal(saves, 1);
});

test("An already compacted window can refresh without duplicating its last assistant message", async () => {
  let requests = 0;
  const anchor = message(1, { role: "assistant" });
  const result = await recoverNativeHistory({ messages: [], initial: capture(0), emptyAnchor: anchor, signal: signal(),
    request: async (batch, previous) => { assert.deepEqual(batch, []); assert.equal(previous.responseId, "resp_0"); requests++; return { capture: capture(1), usage }; },
    save: (_capture, last) => assert.equal(last, anchor),
  });
  assert.equal(requests, 1);
  assert.equal(result.progress.messagesTotal, 0);
});

test("Interrupted historical calls do not join all later turns into one oversized batch", () => {
  const interrupted = message(1, { role: "assistant", content: [{ type: "toolCall", id: "missing_result", arguments: {} }] });
  const later = message(2, { role: "assistant", content: [{ type: "toolCall", id: "completed_call", arguments: {} }] });
  const result = message(3, { role: "toolResult", toolCallId: "completed_call" });
  const all = [interrupted, later, result, message(4)];
  const groups = recoveryGroups(all);
  assert.equal(groups.length, 3);
  assert.deepEqual(groups[0].messages, [interrupted]);
  assert.deepEqual(groups[1].messages, [later, result]);
  assert.deepEqual(groups.flatMap((group) => group.messages), all);
});

test("One-time readable recovery removes opaque reasoning only from the request copy", () => {
  const user = message(1);
  const tool = message(3, { role: "toolResult", toolCallId: "call_1" });
  const original = message(2, { role: "assistant", content: [
    { type: "thinking", thinking: "", thinkingSignature: "opaque-reasoning" },
    { type: "text", text: "Saved reply", textSignature: "saved-text-signature" },
    { type: "toolCall", id: "call_1", arguments: { value: 1 } },
  ] });
  const recovered = readableRecoveryMessages([user, original, tool]);
  assert.equal(recovered[0], user);
  assert.equal(recovered[2], tool);
  assert.deepEqual(recovered[1].content, original.content.slice(1));
  assert.equal(original.content[0].thinkingSignature, "opaque-reasoning");
});

test("Readable request planning does not budget for opaque reasoning that it does not transmit", () => {
  const original = message(1, { role: "assistant", usage: { output: 10000, reasoning: 9000 }, content: [
    { type: "thinking", thinking: "", thinkingSignature: "opaque".repeat(10000) },
    { type: "text", text: "Recorded reply" },
  ] });
  const measure = readableRecoveryMeasure(original);
  assert.equal(measure.tokens, 1000);
  assert.ok(measure.bytes < 1000);
  assert.equal(original.usage.output, 10000);
  const groups = recoveryGroups([original], readableRecoveryMeasure);
  assert.equal(groups[0].messages[0], original);
});
