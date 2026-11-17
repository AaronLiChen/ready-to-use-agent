import assert from "node:assert/strict";
import { test } from "node:test";
import {
  CHECKPOINT_TYPE, DEFAULT_THRESHOLD, SUMMARY, SseObserver,
  captureCompletedCompactionItem, captureCompletedResponse, checkpointFromEntry, latestCheckpoint, latestCompactionIsNative,
  makeCheckpoint, mergeUnpersistedTail, messageKey, observingFetch, readableBranch,
  rewritePayload, supportsNativeCompaction, tailAfterCheckpoint, thresholdFor,
} from "./core.ts";

const model = { provider: "github-copilot", id: "gpt-6.1-sol", api: "openai-responses", contextWindow: 1_050_000 };
const anchor = { role: "assistant", timestamp: 100, responseId: "resp_1", content: [{ type: "text", text: "Previous reply" }] };
const opaque = { type: "compaction", id: "cmp_1", encrypted_content: "opaque-synthetic-state" };
const capture = { responseId: "resp_1", origin: "https://api.example.test", window: [opaque] };
const checkpoint = makeCheckpoint(capture, model, "anchor", anchor, DEFAULT_THRESHOLD);
const anchorEntry = { type: "message", id: "anchor", parentId: null, message: anchor };
const checkpointEntry = { type: "custom", customType: CHECKPOINT_TYPE, id: "checkpoint", parentId: "anchor", data: checkpoint };
const event = (output = [opaque]) => ({ type: "response.completed", response: { id: "resp_1", status: "completed", output, usage: { input_tokens: 123 } } });

for (const provider of ["github-copilot", "openai", "openai-codex", "azure-openai-responses"]) {
  test(`GPT Responses models qualify on ${provider}`, () => {
    assert.equal(supportsNativeCompaction({ ...model, provider }), true);
  });
}
for (const candidate of [undefined, { ...model, id: "claude-opus-4-7" }, { ...model, id: "gemini-pro" },
  { ...model, api: "openai-completions" }, { ...model, provider: "unverified-proxy" }]) {
  test(`Unrelated model remains unchanged: ${candidate?.id ?? "none"}/${candidate?.provider ?? "none"}/${candidate?.api ?? "none"}`, () => {
    assert.equal(supportsNativeCompaction(candidate), false);
  });
}

test("Threshold leaves room on smaller models", () => {
  assert.equal(thresholdFor(model), 200_000);
  assert.equal(thresholdFor({ ...model, contextWindow: 128_000 }), 89_600);
  assert.equal(thresholdFor(model, 2048), 2048);
  for (const invalid of [0, -1, 1000, 2048.5, NaN, Infinity]) assert.throws(() => thresholdFor(model, invalid));
});

test("Payload replacement preserves system instructions, schemas, retained items, and later input", () => {
  const marker = "synthetic-request-marker";
  const system = { role: "developer", content: "Keep the existing policy." };
  const latest = { role: "user", content: [{ type: "input_text", text: "Next question" }] };
  const tools = [{ type: "function", name: "synthetic_tool", parameters: { type: "object" } }];
  const payload = { model: model.id, input: [system, { role: "user", content: marker }, latest], tools,
    previous_response_id: "old_id", context_management: [{ type: "other_setting" }, { type: "compaction", compact_threshold: 9999 }] };
  const original = structuredClone(payload);
  const result = rewritePayload(payload, marker, 200_000, checkpoint);
  assert.deepEqual(result.input, [system, opaque, latest]);
  assert.deepEqual(result.tools, tools);
  assert.equal(result.store, false);
  assert.equal("previous_response_id" in result, false);
  assert.deepEqual(result.context_management, [{ type: "other_setting" }, { type: "compaction", compact_threshold: 200_000 }]);
  assert.deepEqual(payload, original);
  result.input[1].encrypted_content = "changed-copy";
  assert.equal(checkpoint.window[0].encrypted_content, "opaque-synthetic-state");
});

test("Initial request removes the marker without removing actual messages", () => {
  const message = { role: "user", content: "real synthetic test message" };
  assert.deepEqual(rewritePayload({ input: [{ role: "user", content: [{ type: "input_text", text: "marker" }] }, message] }, "marker", 2048).input, [message]);
});

test("Missing and duplicate markers fail without sending a damaged context", () => {
  assert.throws(() => rewritePayload({ input: [] }, "marker", 2048));
  assert.throws(() => rewritePayload({ input: [{ role: "user", content: "marker" }, { role: "user", content: "marker" }] }, "marker", 2048));
  assert.throws(() => rewritePayload({ input: "not-an-array" }, "marker", 2048));
});

test("Inline capture keeps the latest compaction item and all later output unchanged", () => {
  const suffix = { type: "function_call", id: "fc_1", call_id: "call_1", name: "synthetic", arguments: "{}" };
  const latest = { ...opaque, id: "cmp_latest", encrypted_content: "latest-synthetic-state" };
  const output = [{ type: "message", content: [] }, opaque, { type: "message", content: [] }, latest, suffix];
  const result = captureCompletedResponse(event(output), capture.origin);
  assert.deepEqual(result.window, [latest, suffix]);
  assert.deepEqual(result.usage, { input_tokens: 123 });
  result.window[0].id = "copy-only";
  assert.equal(latest.id, "cmp_latest");
});

for (const invalid of [
  { type: "response.failed", response: event().response },
  { type: "response.incomplete", response: event().response },
  { type: "response.completed", response: { ...event().response, status: "incomplete" } },
  event([]), event([{ type: "compaction", encrypted_content: "" }]), event([{ type: "message" }]),
]) {
  test(`Only a completed native checkpoint qualifies: ${JSON.stringify(invalid)}`, () => {
    assert.equal(captureCompletedResponse(invalid, capture.origin), undefined);
  });
}

test("A forced compaction checkpoint requires a completed compaction item, not a partial item", () => {
  const completed = captureCompletedCompactionItem({ type: "response.output_item.done", item: opaque }, "resp_1", capture.origin);
  assert.deepEqual(completed.window, [opaque]);
  assert.equal(completed.completedItemOnly, true);
  assert.equal(captureCompletedCompactionItem({ type: "response.output_item.added", item: opaque }, "resp_1", capture.origin), undefined);
  assert.equal(captureCompletedCompactionItem({ type: "response.output_item.done", item: opaque }, undefined, capture.origin), undefined);
  assert.equal(captureCompletedCompactionItem({ type: "response.output_item.done", item: { type: "message" } }, "resp_1", capture.origin), undefined);
});

test("SSE observer handles Unicode, CRLF, comments, multiple lines, and chunk boundaries", () => {
  const events = [];
  const parser = new SseObserver((item) => events.push(item));
  const text = ': comment\r\nevent: synthetic\r\ndata: {"type":\r\ndata: "synthetic", "text":"你好"}\r\n\r\ndata: malformed\n\ndata: [DONE]\n\ndata: {"type":"last"}';
  for (let index = 0; index < text.length; index += 3) parser.feed(text.slice(index, index + 3));
  parser.finish();
  assert.deepEqual(events, [{ type: "synthetic", text: "你好" }, { type: "last" }]);
});

test("Fetch observation preserves every response byte, status, and header", async () => {
  const frames = `event: response.completed\r\ndata: ${JSON.stringify(event([{ ...opaque, encrypted_content: "你好-synthetic" }]))}\r\n\r\n`;
  const bytes = new TextEncoder().encode(frames);
  const seen = [];
  const response = new Response(new ReadableStream({ start(controller) {
    for (let index = 0; index < bytes.length; index += 7) controller.enqueue(bytes.slice(index, index + 7));
    controller.close();
  } }), { status: 200, headers: { "content-type": "text/event-stream", "x-synthetic": "present" } });
  const request = new Request("https://api.example.test/responses", { method: "POST" });
  const wrapped = await observingFetch(async (input) => { assert.equal(input, request); return response; }, (value) => seen.push(value))(request);
  assert.deepEqual(new Uint8Array(await wrapped.arrayBuffer()), bytes);
  assert.equal(wrapped.status, 200);
  assert.equal(wrapped.headers.get("x-synthetic"), "present");
  assert.equal(seen.length, 1);
  assert.equal(seen[0].origin, "https://api.example.test");
});

test("HTTP errors and non-stream responses pass through without interpretation", async () => {
  for (const response of [new Response("denied", { status: 401 }), new Response("{}", { headers: { "content-type": "application/json" } })]) {
    assert.equal(await observingFetch(async () => response, () => assert.fail("No capture expected"))("https://api.example.test/responses"), response);
    assert.equal(response.bodyUsed, false);
  }
});

test("Stream cancellation reaches the original response and does not create a checkpoint", async () => {
  let cancelled = false;
  const response = new Response(new ReadableStream({
    pull(controller) { controller.enqueue(new TextEncoder().encode('data: {"type":"response.created"}\n\n')); },
    cancel() { cancelled = true; },
  }), { headers: { "content-type": "text/event-stream" } });
  const wrapped = await observingFetch(async () => response, () => assert.fail("No terminal response exists"))("https://api.example.test/responses");
  const reader = wrapped.body.getReader();
  await reader.read();
  await reader.cancel();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(cancelled, true);
});

test("Checkpoint selection follows only the active branch and exact provider/model/API", () => {
  const branch = [anchorEntry, checkpointEntry];
  assert.deepEqual(latestCheckpoint(branch, model), checkpoint);
  assert.equal(latestCheckpoint(branch, { ...model, id: "gpt-other" }), undefined);
  assert.equal(latestCheckpoint(branch, { ...model, provider: "openai" }), undefined);
  assert.equal(latestCheckpoint(branch, { ...model, api: "openai-codex-responses" }), undefined);
  assert.equal(latestCheckpoint([checkpointEntry], model), undefined);
  assert.equal(latestCheckpoint([anchorEntry], model), undefined);
});

test("An ordinary Pi compaction invalidates an older native checkpoint", () => {
  assert.equal(latestCheckpoint([anchorEntry, checkpointEntry, { type: "compaction", id: "pi", summary: "A regular Pi summary" }], model), undefined);
});

test("Malformed encrypted checkpoint data never replaces readable history", () => {
  assert.equal(checkpointFromEntry({ ...checkpointEntry, data: { ...checkpoint, window: [] } }), undefined);
  assert.equal(checkpointFromEntry({ ...checkpointEntry, data: { ...checkpoint, anchorKey: null } }), undefined);
});

test("Tail selection preserves tool results and new messages after the assistant boundary", () => {
  const tool = { role: "toolResult", timestamp: 101, toolCallId: "call_1", content: [] };
  const user = { role: "user", timestamp: 102, content: "Next request" };
  assert.deepEqual(tailAfterCheckpoint([{ role: "user", timestamp: 90, content: "Old" }, anchor, tool, user], checkpoint), [tool, user]);
  assert.equal(tailAfterCheckpoint([user], checkpoint), undefined);
  assert.notEqual(messageKey(tool), messageKey({ ...tool, toolCallId: "call_2" }));
});

test("Readable restoration retains parent links and removes only native compaction behavior", () => {
  const native = { type: "compaction", id: "native", parentId: "anchor", timestamp: "2026-11-13T00:00:00Z", summary: SUMMARY, details: { openaiNative: checkpoint } };
  const latest = { type: "message", id: "latest", parentId: "native", message: { role: "user", timestamp: 102, content: "Next" } };
  const branch = [anchorEntry, native, latest];
  assert.equal(latestCompactionIsNative(branch), true);
  const restored = readableBranch(branch);
  assert.equal(restored[1].type, "custom");
  assert.equal(restored[1].id, "native");
  assert.equal(restored[1].parentId, "anchor");
  assert.equal(restored[2].parentId, "native");
  assert.equal(branch[1].type, "compaction");
  assert.equal(latestCompactionIsNative([...branch, { type: "compaction", summary: "Regular" }]), false);
});

test("Readable restoration also handles damaged native checkpoint metadata", () => {
  const native = { type: "compaction", summary: SUMMARY, details: { openaiNative: { broken: true } } };
  assert.equal(latestCompactionIsNative([native]), true);
  assert.equal(readableBranch([native])[0].type, "custom");
});

test("Restoration retains queued messages that do not yet exist in the session file", () => {
  const queued = { role: "user", timestamp: 102, content: "Queued request" };
  assert.deepEqual(mergeUnpersistedTail([anchor], [anchor, queued]), [anchor, queued]);
  assert.deepEqual(mergeUnpersistedTail([anchor], [{ role: "compactionSummary", summary: SUMMARY }, anchor]), [anchor]);
});
