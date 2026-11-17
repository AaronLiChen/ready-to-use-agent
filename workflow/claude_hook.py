#!/usr/bin/env python3
"""Claude Stop/SessionEnd adapter. No transcript parsing, tool permission changes,
PR actions, subprocess workers, or unconditional keep-going loop.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workflow as w


def handle(data):
    event = data.get("hook_event_name")
    if event not in ("Stop", "SessionEnd"):
        return {}
    if not (w.ROOT / "jobs").exists():
        return {}
    # Claude's Bash does not always export its session ID. Match its actual live
    # harness ancestor as fallback; never choose a job from cwd or modification time.
    try:
        pid = w.owner()["pid"]
    except RuntimeError:
        return {}
    jobs = w.guard(data.get("session_id"), pid)
    if not jobs:
        return {}
    if event == "Stop" and not data.get("stop_hook_active"):
        ids = ", ".join(j["id"] for j in jobs)
        return {"decision": "block", "reason":
                f"Enrolled PR task(s) {ids} remain foreground-owned. This is not new authority. "
                f"Read {w.POLICY}. Continue the next authorized implementation/review/E2E step without "
                "asking for routine consent, or atomically hand off with agent-workflow checkpoint "
                "--status waiting/blocked --next ... --evidence ... . Do not sleep through CI, "
                "rubber-stamp approval, change scope, or start another monitor."}
    for j in jobs:
        if event == "SessionEnd" and data.get("reason") == "logout":
            w.control(j["id"], "pause")
            continue
        failed_handoff = event == "Stop"  # Already continued once: never loop forever.
        w.checkpoint(argparse.Namespace(
            id=j["id"], status="blocked" if failed_handoff else "ready",
            next="Inspect missing safe handoff" if failed_handoff else "Rehydrate unfinished authorized work after Claude session closed",
            evidence=["Claude Stop hook continued once without checkpoint" if failed_handoff else
                      "Claude session ended; verify fresh worktree, head, threads, and checks"],
            decision="Agent did not establish a safe durable handoff; inspect this task before resuming" if failed_handoff else None,
            wake_after=None, build_id=[]))
    return {}


def main():
    try:
        data = json.load(sys.stdin)
        result = handle(data)
        if result:
            print(json.dumps(result))
    except (ValueError, RuntimeError, OSError):
        # Fail visible but never force an unrelated conversation to continue or
        # write guessed state if this adapter cannot establish ownership.
        print("Workflow hook could not establish a safe handoff; inspect agent-workflow list", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
