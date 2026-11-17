import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readdirSync } from "node:fs";
import { tmpdir, homedir } from "node:os";
import { join } from "node:path";
import { createRequire } from "node:module";
import { test, after } from "node:test";

const root = mkdtempSync(join(tmpdir(), "autoworkflow-extension-test-"));
process.env.AUTOWORKFLOW_ROOT = root;
mkdirSync(join(root, "jobs"));
after(() => rmSync(root, { recursive: true, force: true }));
const require = createRequire(import.meta.url);
const { createJiti } = require("jiti");
const jiti = createJiti(import.meta.url);
const factory = await jiti.import(new URL("./index.ts", import.meta.url).pathname, { default: true });

function setup(overrides = {}, job = { id: "owned-pr", status: "active", session: "session", owner: { pid: process.pid } }) {
  for (const name of readdirSync(join(root, "jobs"))) rmSync(join(root, "jobs", name));
  if (job) writeFileSync(join(root, "jobs", "task.json"), JSON.stringify(job));
  const handlers = {}, messages = [], commands = [], notifications = [], registeredCommands = [];
  const ctx = { mode: "tui", isIdle: () => true, hasPendingMessages: () => false,
    sessionManager: { getSessionId: () => "session" }, ui: { notify: (...args) => notifications.push(args) }, ...overrides };
  const pi = { on: (name, fn) => { handlers[name] = fn; },
    exec: async (command, args) => { commands.push([command, ...args]); return { code: 0, stdout: "[]" }; },
    sendMessage: (...args) => messages.push(args), registerCommand: (name) => registeredCommands.push(name) };
  factory(pi);
  return { ctx, handlers, messages, commands, notifications, registeredCommands };
}
const finish = (s, stopReason = "stop") => s.handlers.agent_end({ messages: [{ role: "assistant", stopReason }] }, s.ctx);


test("PR workflow status and resumed Teams inbox commands are available", () => {
  const s = setup({}, null);
  assert.deepEqual(s.registeredCommands, ["review-inbox", "workflow-status"]);
});
test("no enrolled job => no keep-going message or subprocess", async () => {
  const s = setup({}, null);
  await finish(s); await s.handlers.agent_settled({}, s.ctx);
  assert.equal(s.messages.length, 0); assert.equal(s.commands.length, 0);
});
test("foreground task gets a bounded continuation and explicit handoff reminder", async () => {
  const s = setup();
  assert.match(s.handlers.before_agent_start({}, s.ctx).message.content, /owned-pr/);
  await finish(s);
  await s.handlers.agent_settled({}, s.ctx); await s.handlers.agent_settled({}, s.ctx);
  assert.equal(s.messages.length, 2);
  assert.match(s.messages[0][0].content, /not a new user request or extra authority/);
  assert.equal(s.messages[0][1].deliverAs, "followUp");
  await s.handlers.agent_settled({}, s.ctx);
  assert.equal(s.messages.length, 2);
  assert.ok(s.commands.some(c => c.includes("blocked")));
});
test("a scheduled worker is not mistaken for its original foreground session", async () => {
  const s = setup({}, { id: "owned-pr", status: "active", session: "session", owner: null, worker_pid: 12345 });
  await finish(s); await s.handlers.agent_settled({}, s.ctx);
  assert.equal(s.messages.length, 0); assert.equal(s.commands.length, 0);
});
test("a different session in the same process does not inherit ownership", async () => {
  const s = setup({ sessionManager: { getSessionId: () => "other-session" } });
  await finish(s); await s.handlers.agent_settled({}, s.ctx);
  assert.equal(s.messages.length, 0);
});
test("Escape pauses instead of triggering another turn", async () => {
  const s = setup(); await finish(s, "aborted"); await s.handlers.agent_settled({}, s.ctx);
  assert.equal(s.messages.length, 0); assert.ok(s.commands.some(c => c.includes("pause")));
});
test("an exhausted provider error becomes a blocker, not blind retry", async () => {
  const s = setup(); await finish(s, "error"); await s.handlers.agent_settled({}, s.ctx);
  assert.equal(s.messages.length, 0); assert.ok(s.commands.some(c => c.includes("blocked")));
});
test("pending input wins over the ownership nudge", async () => {
  const s = setup({ hasPendingMessages: () => true });
  await finish(s); await s.handlers.agent_settled({}, s.ctx);
  assert.equal(s.messages.length, 0); assert.equal(s.commands.length, 0);
});
test("print/json workers are not recursively continued", async () => {
  for (const mode of ["print", "json"]) {
    const s = setup({ mode }); await finish(s); await s.handlers.agent_settled({}, s.ctx);
    assert.equal(s.messages.length, 0);
  }
});
test("RPC frontend retains the same ownership guard", async () => {
  const s = setup({ mode: "rpc" }); await finish(s); await s.handlers.agent_settled({}, s.ctx);
  assert.equal(s.messages.length, 1);
});
test("normal session close hands off; reload does not release a live owner", async () => {
  const s = setup(); await s.handlers.session_shutdown({ reason: "reload" }, s.ctx);
  assert.equal(s.commands.length, 0);
  await s.handlers.session_shutdown({ reason: "quit" }, s.ctx);
  assert.ok(s.commands.some(c => c.includes("checkpoint") && c.includes("ready")));
});
test("quit immediately after abort cannot reactivate the aborted job", async () => {
  const s = setup(); await finish(s, "aborted");
  await s.handlers.session_shutdown({ reason: "quit" }, s.ctx);
  assert.ok(s.commands.some(c => c.includes("pause")));
  assert.ok(!s.commands.some(c => c.includes("checkpoint")));
});
test("explicit stop pauses owned waiting jobs too", async () => {
  const s = setup({}, { id: "owned-pr", status: "waiting", session: "session", owner: null });
  await s.handlers.input({ source: "interactive", text: "stop" }, s.ctx);
  assert.ok(s.commands.some(c => c.includes("pause")));
});

test("native Pi RPC loads ownership commands without a model or provider call", async () => {
  const home = mkdtempSync(join(tmpdir(), "portable-workflow-rpc-"));
  const child = spawn(process.execPath, [new URL("../../node_modules/.bin/pi", import.meta.url).pathname,
    "--offline", "--mode", "rpc", "--no-session", "--no-context-files", "--no-extensions", "--no-skills",
    "--no-prompt-templates", "--no-themes", "--extension", new URL("./index.ts", import.meta.url).pathname], {
    cwd: home, env: { HOME: home, PATH: process.env.PATH, PI_CODING_AGENT_DIR: join(home, ".pi/agent"),
      AUTOWORKFLOW_ROOT: join(home, "state"), PI_OFFLINE: "1" }, stdio: ["pipe", "pipe", "pipe"],
  });
  let buffer = "";
  try {
    const response = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error("Native RPC command discovery timed out")), 15000);
      child.once("error", (error) => { clearTimeout(timeout); reject(error); });
      child.once("exit", (code) => { clearTimeout(timeout); reject(new Error(`Native RPC exited before discovery: ${code}`)); });
      child.stdout.on("data", (chunk) => {
        buffer += chunk.toString();
        let index;
        while ((index = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, index); buffer = buffer.slice(index + 1);
          if (!line.trim()) continue;
          const value = JSON.parse(line);
          if (value.type === "extension_error") { clearTimeout(timeout); reject(new Error("Native extension load failed")); }
          if (value.id === "commands") { clearTimeout(timeout); resolve(value); }
        }
      });
      child.stdin.write(JSON.stringify({ id: "commands", type: "get_commands" }) + "\n");
    });
    assert.equal(response.success, true);
    const commands = response.data.commands.map((command) => command.name);
    assert.ok(commands.includes("workflow-status"));
    assert.ok(commands.includes("review-inbox"));
  } finally {
    child.kill("SIGTERM");
    await new Promise((resolve) => child.once("close", resolve));
    rmSync(home, { recursive: true, force: true });
  }
});
