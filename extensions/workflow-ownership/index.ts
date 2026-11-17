/** Protect enrolled ownership. Do not start timers, models, or provider operations. */
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const HOME = join(homedir(), ".agents/ready-to-use-agent");
const ROOT = process.env.AUTOWORKFLOW_ROOT ?? join(HOME, "state/workflow");
const CLI = process.env.AUTOWORKFLOW_CLI ?? join(HOME, "workflow/workflow.py");
const POLICY = process.env.AUTOWORKFLOW_POLICY ?? join(HOME, "workflow/SKILL.md");
const PYTHON = process.env.AUTOWORKFLOW_PYTHON ?? "python3";
type Job = { id: string; status: string; session?: string; owner?: { pid: number } | null };

function ownedJobs(ctx: ExtensionContext, activeOnly = true): Job[] {
	const directory = join(ROOT, "jobs");
	if (!existsSync(directory)) return [];
	const session = ctx.sessionManager.getSessionId();
	try {
		return readdirSync(directory).filter((name) => name.endsWith(".json")).flatMap((name) => {
			try {
				const job = JSON.parse(readFileSync(join(directory, name), "utf8")) as Job;
				if (!/^[a-z0-9][a-z0-9-]{0,79}$/.test(job.id)) return [];
				const matches = job.session ? job.session === session : job.owner?.pid === process.pid;
				const owned = activeOnly ? matches && job.owner?.pid === process.pid : matches;
				return owned && (activeOnly ? job.status === "active" : !["done", "paused"].includes(job.status)) ? [job] : [];
			} catch {
				return [];
			}
		});
	} catch {
		return [];
	}
}

export default function ownership(pi: ExtensionAPI): void {
	let nudges = 0;
	let lastStop: string | undefined;
	const interactive = (ctx: ExtensionContext) => ctx.mode === "tui" || ctx.mode === "rpc";
	const invoke = async (args: string[]) => {
		const result = await pi.exec(PYTHON, ["-I", CLI, ...args], { timeout: 10000 });
		if (result.code !== 0) throw new Error("Workflow handoff failed. Inspect agent-workflow list.");
	};

	pi.on("input", async (event, ctx) => {
		if (event.source === "extension") return;
		nudges = 0;
		lastStop = undefined;
		if (!interactive(ctx) || !/^(?:stop|pause|cancel)[.!\s]*$/i.test(event.text)) return;
		for (const job of ownedJobs(ctx, false)) {
			try { await invoke(["pause", job.id]); }
			catch { ctx.ui.notify(`Workflow ${job.id} is busy. Coordinate its live worker before pause.`, "warning"); }
		}
	});

	pi.on("before_agent_start", (_event, ctx) => {
		if (!interactive(ctx)) return;
		const jobs = ownedJobs(ctx);
		if (!jobs.length) return;
		return { message: {
			customType: "portable-workflow-ownership",
			content: `You own enrolled tasks ${jobs.map((job) => job.id).join(", ")}. Read ${POLICY}. Do only authorized work. Before you stop, checkpoint evidence and transfer ownership. Enrollment does not broaden permissions.`,
			display: false,
		} };
	});

	pi.on("agent_end", (event) => {
		const last = [...event.messages].reverse().find((message) => message.role === "assistant");
		lastStop = last?.role === "assistant" ? last.stopReason : undefined;
	});

	pi.on("agent_settled", async (_event, ctx) => {
		if (!interactive(ctx) || !ctx.isIdle() || ctx.hasPendingMessages()) return;
		const jobs = ownedJobs(ctx);
		if (!jobs.length) { nudges = 0; return; }
		if (lastStop === "aborted") {
			for (const job of jobs) await invoke(["pause", job.id]);
			return;
		}
		if (lastStop === "error" || nudges >= 2) {
			for (const job of jobs) await invoke(["checkpoint", job.id, "--status", "blocked",
				"--next", "Inspect the provider error or missing handoff", "--evidence", "The ownership guard could not establish a safe handoff",
				"--decision", "Inspect this task before resumption. Preserve native permissions, scope, and interrupted changes."]);
			ctx.ui.notify("Workflow blocked. Inspect the provider error or missing durable handoff.", "warning");
			return;
		}
		nudges += 1;
		pi.sendMessage({
			customType: "portable-workflow-continue",
			content: `Tasks ${jobs.map((job) => job.id).join(", ")} remain foreground-owned. This is not a new user request or extra authority. Do the next authorized step or checkpoint evidence. Read ${POLICY}. Do not start another watcher or broaden scope.`,
			display: true,
		}, { triggerTurn: true, deliverAs: "followUp" });
	});

	pi.on("session_shutdown", async (event, ctx) => {
		if (!interactive(ctx) || event.reason === "reload") return;
		for (const job of ownedJobs(ctx)) {
			if (lastStop === "aborted") { await invoke(["pause", job.id]); continue; }
			await invoke(["checkpoint", job.id, "--status", "ready", "--next", "Restore unfinished authorized work after session close",
				"--evidence", `Session shutdown: ${event.reason}. Verify fresh worktree, source, identity, threads, and checks before action.`]);
		}
	});

	for (const [name, command, description] of [
		["review-inbox", "inbox", "Show Teams review intake and source coverage without a model call"],
		["workflow-status", "list", "Show durable PR ownership without a model call"],
	] as const) {
		pi.registerCommand(name, {
			description,
			handler: async (_args, ctx) => {
				const result = await pi.exec(PYTHON, ["-I", CLI, command], { timeout: 10000 });
				ctx.ui.notify(result.code === 0 ? result.stdout : "Workflow state is unavailable", result.code === 0 ? "info" : "error");
			},
		});
	}
}
