import assert from "node:assert/strict";
import { createServer } from "node:http";
import { spawnSync } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import {
  buildSessionContext, createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager,
} from "@earendil-works/pi-coding-agent";
import { InMemoryCredentialStore } from "@earendil-works/pi-ai";
import { CHECKPOINT_TYPE, FORCE_THRESHOLD, MODE_TYPE, SUMMARY } from "./core.ts";

const extension = process.env.PI_NATIVE_EXTENSION_UNDER_TEST ?? fileURLToPath(new URL("./index.ts", import.meta.url));
const longPrompt = "Synthetic history. No project data is present. ".repeat(300);

function sendResponse(response, number, definition = {}) {
  const id = `resp_synthetic_${number}`;
  const text = definition.text ?? `SYNTHETIC_REPLY_${number}`;
  const message = { type: "message", id: `msg_${number}`, role: "assistant", status: "completed", phase: "final_answer",
    content: [{ type: "output_text", text, annotations: [] }] };
  const opaque = { type: "compaction", id: `cmp_${number}`, encrypted_content: `opaque-synthetic-${number}` };
  const result = definition.tool ? { type: "function_call", id: `fc_${number}`, call_id: `call_${number}`,
    name: "synthetic_probe", arguments: JSON.stringify({ value: 731946 }), status: "completed" } : message;
  const output = definition.compact === false ? [result] : [opaque, result];
  const events = [{ type: "response.created", response: { id, status: "in_progress", output: [] } }];
  for (const [index, item] of output.entries()) {
    events.push({ type: "response.output_item.added", output_index: index,
      item: item.type === "message" ? { ...item, content: [] } : item.type === "function_call" ? { ...item, arguments: "" } : item });
    if (item.type === "message") {
      events.push({ type: "response.content_part.added", output_index: index, item_id: item.id, content_index: 0, part: { type: "output_text", text: "", annotations: [] } });
      events.push({ type: "response.output_text.delta", output_index: index, item_id: item.id, content_index: 0, delta: text });
      events.push({ type: "response.output_text.done", output_index: index, item_id: item.id, content_index: 0, text });
      events.push({ type: "response.content_part.done", output_index: index, item_id: item.id, content_index: 0, part: item.content[0] });
    } else if (item.type === "function_call") {
      events.push({ type: "response.function_call_arguments.delta", output_index: index, item_id: item.id, delta: item.arguments });
      events.push({ type: "response.function_call_arguments.done", output_index: index, item_id: item.id, arguments: item.arguments });
    }
    events.push({ type: "response.output_item.done", output_index: index, item });
  }
  events.push({ type: "response.completed", response: { id, status: "completed", output,
    usage: { input_tokens: 3000, output_tokens: 10, total_tokens: 3010, input_tokens_details: { cached_tokens: 0 }, output_tokens_details: { reasoning_tokens: 0 } } } });
  response.writeHead(200, { "content-type": "text/event-stream", "x-synthetic-provider": "preserved" });
  response.end(events.map((event) => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join(""));
}

async function fixture(t, definitions = []) {
  const directory = await mkdtemp(join(tmpdir(), "pi-native-compact-test-"));
  const requests = [];
  const server = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const payload = JSON.parse(Buffer.concat(chunks).toString());
    requests.push(payload);
    const definition = typeof definitions === "function" ? definitions(payload, requests.length) : definitions[requests.length - 1];
    if (definition?.error) {
      response.writeHead(definition.status ?? 400, { "content-type": "application/json" });
      response.end(JSON.stringify({ error: { message: definition.error, code: "invalid_request_body", type: "invalid_request_error" } }));
    } else sendResponse(response, requests.length, definition);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${server.address().port}/v1`;
  const shim = join(directory, "extension.ts");
  await writeFile(shim, `import { installNativeCompaction } from ${JSON.stringify(extension)};\nexport default function(pi) { installNativeCompaction(pi, { thresholdTokens: 2048 }); }\n`);
  const sessions = [];
  async function open(sessionFile, identity = "gpt-native-synthetic", customTools = [], extensionPath = shim) {
    const runtime = await ModelRuntime.create({ credentials: new InMemoryCredentialStore(), modelsPath: null,
      modelsStorePath: join(directory, "unused-model-store.json"), allowModelNetwork: false });
    runtime.registerProvider("openai", {
      baseUrl: url, apiKey: "synthetic-test-key", api: "openai-responses",
      models: ["gpt-native-synthetic", "other-synthetic"].map((id) => ({ id, name: id, api: "openai-responses", reasoning: false,
        input: ["text"], contextWindow: 1_050_000, maxTokens: 4096, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } })),
    });
    const settings = SettingsManager.inMemory({ compaction: { enabled: true, keepRecentTokens: 1024 }, retry: { enabled: false } });
    const loader = new DefaultResourceLoader({ cwd: directory, agentDir: directory, settingsManager: settings,
      additionalExtensionPaths: [extensionPath], systemPromptOverride: () => "You are a synthetic test agent. Use no external services.",
      skillsOverride: () => ({ skills: [], diagnostics: [] }), promptsOverride: () => ({ prompts: [], diagnostics: [] }),
      agentsFilesOverride: () => ({ agentsFiles: [] }),
    });
    await loader.reload();
    assert.deepEqual(loader.getExtensions().errors, []);
    const sm = sessionFile ? SessionManager.open(sessionFile) : SessionManager.create(directory, join(directory, "sessions"));
    const { session } = await createAgentSession({ cwd: directory, agentDir: directory, modelRuntime: runtime,
      model: runtime.getModel("openai", identity), thinkingLevel: "off", sessionManager: sm,
      resourceLoader: loader, settingsManager: settings, tools: customTools.map((tool) => tool.name), customTools,
    });
    await session.bindExtensions({ mode: "json" });
    sessions.push(session);
    return { session, sm, runtime };
  }
  t.after(async () => {
    for (const session of sessions) session.dispose();
    await new Promise((resolve) => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  });
  return { open, requests, directory };
}

function texts(payload) {
  return payload.input.flatMap((item) => typeof item.content === "string" ? [item.content] :
    Array.isArray(item.content) ? item.content.filter((part) => typeof part.text === "string").map((part) => part.text) : []);
}

test("A fresh home installation loads compaction outside the repository without importing credentials", async (t) => {
  const f = await fixture(t);
  const home = join(f.directory, "fresh home");
  const root = fileURLToPath(new URL("../../", import.meta.url));
  const result = spawnSync(process.env.PYTHON ?? "python3", [join(root, "install.py"), "--home", home,
    "--agents", "pi", "claude", "codex"], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  const entry = join(home, ".pi/agent/extensions/openai-native-compaction.ts");
  const { session, sm } = await f.open(undefined, "gpt-native-synthetic", [], entry);
  await session.prompt(longPrompt);
  assert.ok(sm.getEntries().some((item) => item.type === "custom" && item.customType === CHECKPOINT_TYPE));
  assert.equal(f.requests[0].context_management[0].compact_threshold, 200_000);
});

test("SDK saves native compaction and replays its exact window without the old transcript", async (t) => {
  const f = await fixture(t, [{}, { compact: false }]);
  const { session, sm } = await f.open();
  await session.prompt(longPrompt);
  const checkpoint = sm.getBranch().find((entry) => entry.type === "custom" && entry.customType === CHECKPOINT_TYPE);
  assert.ok(checkpoint, "The checkpoint must be durable before the next request");
  assert.equal(checkpoint.data.window[0].encrypted_content, "opaque-synthetic-1");
  assert.ok(f.requests[0].context_management.some((entry) => entry.type === "compaction" && entry.compact_threshold === 2048));
  assert.equal(JSON.stringify(f.requests[0]).includes("[pi-openai-native-compaction:"), false);
  await session.prompt("Next synthetic question");
  assert.deepEqual(f.requests[1].input.filter((item) => item.type === "compaction"), [checkpoint.data.window[0]]);
  assert.equal(texts(f.requests[1]).includes(longPrompt), false);
  assert.ok(texts(f.requests[1]).includes("Next synthetic question"));
  assert.ok(f.requests[1].input.some((item) => item.type === "message" && item.id === "msg_1" && item.phase === "final_answer"));
  assert.equal(sm.getEntries().some((entry) => entry.type === "compaction"), false, "Automatic compaction must not call Pi's text summarizer");
});

test("SDK restores native state after a new process-style runtime opens the session", async (t) => {
  const f = await fixture(t, [{}, { compact: false }]);
  const first = await f.open();
  await first.session.prompt(longPrompt);
  const path = first.sm.getSessionFile();
  first.session.dispose();
  const resumed = await f.open(path);
  await resumed.session.prompt("Synthetic request after resume");
  assert.equal(f.requests[1].input.some((item) => item.type === "compaction" && item.id === "cmp_1"), true);
  assert.equal(texts(f.requests[1]).includes(longPrompt), false);
});

test("SDK manual compact uses native compaction and preserves readable history when disabled", async (t) => {
  const f = await fixture(t, [{}, {}, {}, { compact: false }]);
  const { session, sm } = await f.open();
  await session.prompt(longPrompt);
  await session.prompt(`Second synthetic turn. ${longPrompt}`);
  const result = await session.compact();
  assert.equal(result.summary, SUMMARY);
  assert.ok(result.details.openaiNative.window.some((item) => item.type === "compaction"));
  assert.equal(f.requests[2].context_management.find((item) => item.type === "compaction").compact_threshold, FORCE_THRESHOLD);
  assert.equal("tools" in f.requests[2], false, "A forced compaction cannot execute task tools");
  assert.ok(texts(f.requests[2]).some((text) => text.includes("compaction-only request")));
  sm.appendCustomEntry(MODE_TYPE, { enabled: false });
  await session.prompt("Synthetic request after native mode is off");
  assert.equal(f.requests[3].context_management, undefined);
  assert.equal(f.requests[3].input.some((item) => item.type === "compaction"), false);
  assert.ok(texts(f.requests[3]).includes(longPrompt), "Disabling native mode must restore the original readable history");
});

test("SDK model changes leave non-GPT requests and their context unchanged", async (t) => {
  const f = await fixture(t, [{}, {}, {}, { compact: false }]);
  const { session, runtime } = await f.open();
  await session.prompt(longPrompt);
  await session.prompt(`Second synthetic turn. ${longPrompt}`);
  await session.compact();
  await session.setModel(runtime.getModel("openai", "other-synthetic"));
  await session.prompt("A synthetic non-GPT turn");
  assert.equal(f.requests[3].model, "other-synthetic");
  assert.equal(f.requests[3].context_management, undefined);
  assert.equal(f.requests[3].input.some((item) => item.type === "compaction"), false);
  assert.ok(texts(f.requests[3]).includes(longPrompt));
});

test("SDK does not reuse a checkpoint from a branch that the user left", async (t) => {
  const f = await fixture(t, [{}, { compact: false }]);
  const first = await f.open();
  await first.session.prompt(longPrompt);
  const user = first.sm.getEntries().find((entry) => entry.type === "message" && entry.message.role === "user");
  first.sm.branch(user.id);
  assert.equal(buildSessionContext(first.sm.getEntries(), first.sm.getLeafId()).messages.length, 1);
  first.session.dispose();
  const resumed = await f.open(first.sm.getSessionFile());
  // Reapply the explicit branch because reopening a raw JSONL selects its last entry.
  resumed.sm.branch(user.id);
  resumed.session.agent.state.messages = buildSessionContext(resumed.sm.getEntries(), user.id).messages;
  await resumed.session.prompt("Synthetic alternate branch");
  assert.equal(f.requests[1].input.some((item) => item.type === "compaction"), false);
  assert.ok(texts(f.requests[1]).includes(longPrompt));
});

test("SDK preserves tool calls and their results across native compaction", async (t) => {
  const f = await fixture(t, [{ tool: true }, { compact: false }]);
  let calls = 0;
  const tool = {
    name: "synthetic_probe", label: "Synthetic probe", description: "Return a synthetic value without external actions.",
    parameters: { type: "object", properties: { value: { type: "integer" } }, required: ["value"], additionalProperties: false },
    execute: async (_id, args) => { calls++; return { content: [{ type: "text", text: `SYNTHETIC_VALUE_${args.value}` }], details: {} }; },
  };
  const { session } = await f.open(undefined, "gpt-native-synthetic", [tool]);
  await session.prompt(longPrompt);
  assert.equal(calls, 1);
  assert.equal(f.requests.length, 2);
  assert.ok(f.requests[1].input.some((item) => item.type === "function_call" && item.call_id === "call_1"));
  assert.ok(f.requests[1].input.some((item) => item.type === "function_call_output" && item.call_id === "call_1" && JSON.stringify(item).includes("SYNTHETIC_VALUE_731946")));
  assert.ok(f.requests[1].tools.some((item) => item.name === "synthetic_probe"));
});

test("SDK rejects an encrypted checkpoint from another endpoint without sending it", async (t) => {
  const f = await fixture(t, [{}, { compact: false }]);
  const { session, sm } = await f.open();
  await session.prompt(longPrompt);
  const original = sm.getBranch().find((entry) => entry.type === "custom" && entry.customType === CHECKPOINT_TYPE);
  sm.appendCustomEntry(CHECKPOINT_TYPE, { ...original.data, origin: "https://different.example.test" });
  await session.prompt("Synthetic endpoint mismatch");
  const last = session.messages.filter((message) => message.role === "assistant").at(-1);
  assert.equal(last.stopReason, "error");
  assert.match(last.errorMessage, /provider endpoint changed/);
  assert.equal(f.requests.length, 1);
  sm.appendCustomEntry(MODE_TYPE, { enabled: false });
  await session.prompt("Retry with native mode off");
  assert.equal(f.requests.length, 2);
  assert.equal(f.requests[1].input.some((item) => item.type === "compaction"), false);
  assert.ok(texts(f.requests[1]).includes(longPrompt));
});

test("SDK does not replace history when a forced native request returns no compaction item", async (t) => {
  const f = await fixture(t, [{ compact: false }]);
  const { session, sm } = await f.open();
  seedHistory(session, sm);
  await assert.rejects(session.compact(), /Compaction cancelled/);
  assert.equal(f.requests.length, 1, "Failure must not call Pi's text summarizer");
  assert.ok(sm.getEntries().some((entry) => entry.type === "custom" && entry.customType === "openai-native-compaction-failure-v1" && /no completed native compaction item/.test(entry.data.error)));
  assert.equal(sm.getEntries().some((entry) => entry.type === "compaction"), false);
  assert.ok(sm.getEntries().some((entry) => entry.type === "message" && entry.message.role === "user" && JSON.stringify(entry.message.content).includes(longPrompt)));
});

test("SDK keeps Pi text compaction for non-GPT models", async (t) => {
  const f = await fixture(t, [{ compact: false }, { compact: false }, { compact: false }]);
  const { session } = await f.open(undefined, "other-synthetic");
  await session.prompt(longPrompt);
  await session.prompt(`Second synthetic turn. ${longPrompt}`);
  const result = await session.compact();
  assert.notEqual(result.summary, SUMMARY);
  assert.equal(result.details.openaiNative, undefined);
  assert.equal(f.requests[2].context_management, undefined);
  assert.ok(texts(f.requests[2]).some((text) => text.includes("structured context checkpoint summary")));
});

test("SDK nested model calls do not inherit a session checkpoint or native mode", async (t) => {
  const f = await fixture(t, [{}, { compact: false }]);
  const { session, runtime } = await f.open();
  await session.prompt(longPrompt);
  await runtime.complete(runtime.getModel("openai", "gpt-native-synthetic"), {
    messages: [{ role: "user", timestamp: Date.now(), content: "Independent synthetic request" }],
  }, { maxTokens: 32 });
  assert.equal(f.requests[1].context_management, undefined);
  assert.equal(f.requests[1].input.some((item) => item.type === "compaction"), false);
  assert.ok(texts(f.requests[1]).includes("Independent synthetic request"));
});

test("SDK native compaction commands preserve the session-specific mode", async (t) => {
  const f = await fixture(t);
  const { session, sm } = await f.open();
  await session.prompt(longPrompt);
  await session.prompt(`Second synthetic turn. ${longPrompt}`);
  await session.prompt("/native-compact now");
  assert.equal(sm.getEntries().filter((entry) => entry.type === "compaction").length, 1);
  await session.prompt("/native-compact off");
  assert.equal(sm.getEntries().filter((entry) => entry.type === "custom" && entry.customType === MODE_TYPE).at(-1).data.enabled, false);
  await session.prompt("/native-compact on");
  assert.equal(sm.getEntries().filter((entry) => entry.type === "custom" && entry.customType === MODE_TYPE).at(-1).data.enabled, true);
  assert.equal(f.requests.length, 3);
});

test("SDK reload retains native checkpoints and does not retain stale extension bindings", async (t) => {
  const f = await fixture(t, [{}, { compact: false }]);
  const { session } = await f.open();
  await session.prompt(longPrompt);
  await session.reload();
  await session.prompt("Synthetic request after extension reload");
  assert.equal(f.requests[1].input.some((item) => item.type === "compaction" && item.id === "cmp_1"), true);
  assert.equal(texts(f.requests[1]).includes(longPrompt), false);
});

function seedHistory(session, sm, count = 20) {
  const timestamp = Date.now();
  for (let index = 0; index < count; index++) {
    sm.appendMessage({ role: "user", timestamp: timestamp + index * 2,
      content: [{ type: "text", text: `SYNTHETIC_OLD_MESSAGE_${index}. ${longPrompt}` }] });
    sm.appendMessage({ role: "assistant", timestamp: timestamp + index * 2 + 1,
      provider: "openai", model: "gpt-native-synthetic", api: "openai-responses", stopReason: "stop",
      content: [{ type: "text", text: `SYNTHETIC_OLD_REPLY_${index}` }],
      usage: { input: 0, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 10, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } });
  }
  session.agent.state.messages = buildSessionContext(sm.getEntries()).messages;
}

test("SDK recovers a large pre-extension history through bounded native requests", async (t) => {
  const f = await fixture(t);
  const { session, sm } = await f.open();
  seedHistory(session, sm);
  const result = await session.compact();
  assert.ok(result.details.recovery.requests > 1);
  assert.ok(sm.getEntries().filter((entry) => entry.type === "custom" && entry.customType === CHECKPOINT_TYPE).length > 1);
  for (let index = 0; index < 20; index++) {
    assert.equal(f.requests.filter((payload) => texts(payload).some((text) => text.includes(`SYNTHETIC_OLD_MESSAGE_${index}.`))).length, 1);
  }
  assert.ok(f.requests.every((payload) => !payload.tools));
  assert.equal(sm.getEntries().filter((entry) => entry.type === "message" && entry.message.role === "user").length, 20);
});

test("SDK overflow recovery splits rejected requests and retries the original user request", async (t) => {
  const f = await fixture(t, (payload) => JSON.stringify(payload.input).length > 120000
    ? { error: "Your input exceeds the context window of this model. Please adjust your input and try again." } : {});
  const { session, sm } = await f.open();
  seedHistory(session, sm);
  const query = "SYNTHETIC_PENDING_REQUEST_MUST_REMAIN_LAST";
  await session.prompt(query);
  assert.equal(session.messages.filter((message) => message.role === "assistant").at(-1).stopReason, "stop", JSON.stringify({
    requests: f.requests.length,
    lastError: session.messages.filter((message) => message.role === "assistant").at(-1).errorMessage,
    failures: sm.getEntries().filter((entry) => entry.type === "custom" && entry.customType === "openai-native-compaction-failure-v1").map((entry) => entry.data),
  }));
  assert.ok(f.requests.length > 3);
  assert.equal(texts(f.requests.at(-1)).at(-1), query);
  assert.ok(sm.getEntries().some((entry) => entry.type === "compaction" && entry.details.openaiNative));
  assert.equal(sm.getEntries().some((entry) => entry.type === "custom" && entry.customType === "openai-native-compaction-failure-v1"), false);
});

test("SDK native recovery retries a transient provider 503 within a bounded request", async (t) => {
  const f = await fixture(t, (_payload, number) => number === 1 ? { status: 503, error: "Synthetic provider overload" } : {});
  const { session, sm } = await f.open();
  seedHistory(session, sm);
  const result = await session.compact();
  assert.ok(result.details.recovery.requests > 1);
  assert.equal(f.requests.length, result.details.recovery.requests + 1);
  assert.deepEqual(f.requests[0], f.requests[1]);
});

test("SDK readable recovery preserves saved reasoning while excluding opaque replay blocks", async (t) => {
  const f = await fixture(t);
  const { session, sm } = await f.open();
  seedHistory(session, sm, 2);
  const signature = JSON.stringify({ type: "reasoning", id: "rs_old", encrypted_content: "opaque-synthetic-old" });
  sm.appendMessage({ role: "assistant", timestamp: Date.now() + 10, provider: "openai", model: "gpt-native-synthetic", api: "openai-responses",
    stopReason: "stop", content: [{ type: "thinking", thinking: "", thinkingSignature: signature }, { type: "text", text: "Saved synthetic reply" }],
    usage: { input: 0, output: 100, reasoning: 90, cacheRead: 0, cacheWrite: 0, totalTokens: 100, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } });
  session.agent.state.messages = buildSessionContext(sm.getEntries()).messages;
  await session.prompt("/native-compact recover");
  assert.equal(JSON.stringify(f.requests[0]).includes("opaque-synthetic-old"), false);
  assert.ok(sm.getEntries().some((entry) => entry.type === "message" && JSON.stringify(entry.message).includes("opaque-synthetic-old")));
  assert.equal(sm.getEntries().filter((entry) => entry.type === "compaction").at(-1).details.replay, "readable");
});

test("SDK can refresh a previously completed native window without requiring another compaction item", async (t) => {
  const f = await fixture(t, [{}, {}, { compact: false }]);
  const { session } = await f.open();
  await session.prompt(longPrompt);
  await session.prompt(`Second synthetic turn. ${longPrompt}`);
  const result = await session.compact();
  assert.ok(result.details.openaiNative.window.some((item) => item.type === "compaction" && item.id === "cmp_2"));
  assert.ok(result.details.openaiNative.window.some((item) => item.type === "message" && item.id === "msg_3"));
});
