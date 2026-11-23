#!/usr/bin/env python3.15
"""Run gate hooks for an unattended run root: `run_gate.py ask` (PreToolUse on AskUserQuestion)
and `run_gate.py stop` (Stop).

A run keeps its files in `codex/run-<YYYYMMDD>-<n>/` in the repository checkout. The root writes an
`UNATTENDED` file there when the owner leaves and removes it when they are back. A session is that
run's root while it is unattended when all of these hold:

  - the payload is the main thread's (no `agent_id`): subagents are never affected;
  - the checkout holding the session's cwd, or `$CLAUDE_PROJECT_DIR`, has a run folder with an
    `UNATTENDED` file and no `CLOSEOUT.md`;
  - the session's transcript names that run folder (the root wrote or read its run files), so another
    session in the same checkout is left alone.

`ask` refuses the question prompt for that root and tells it to park the question in PARKED.md.
`stop` sends that root back to work when it ends a turn, at most three times in a row, unless its
reply ends with a `WAITING:` line. The count lives in `<state>/run-stop/<session>.json` (state is
`$AGENT_LOOPS_STATE_DIR`, else `~/.local/state/agent-loops`) and restarts with every turn the hook did not
cause. Anything unexpected, a fault or a count that cannot be kept, lets the call or the stop through.
"""

import json
import os
import re
import sys

RUN_DIR = re.compile(r"^run-\d{8}-\d+$")
STATE_DIR_ENV = "AGENT_LOOPS_STATE_DIR"
MAX_CONTINUATIONS = 3
MAX_LEVELS = 64
CHUNK = 8 << 20
WAITING = re.compile(r"^[\s>*_`]*WAITING[*_`]*:")


def checkouts(payload):
    """Directories that may hold the run folders: each start's ancestors up to its checkout top."""
    seen = []
    for start in (payload.get("cwd"), os.environ.get("CLAUDE_PROJECT_DIR")):
        if not isinstance(start, str) or not os.path.isabs(start):
            continue
        path = os.path.realpath(start)
        for _ in range(MAX_LEVELS):
            if path not in seen:
                seen.append(path)
            if os.path.exists(os.path.join(path, ".git")):
                break
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    return seen


def unattended_runs(payload):
    runs = []
    for top in checkouts(payload):
        codex = os.path.join(top, "codex")
        try:
            names = sorted(os.listdir(codex))
        except OSError:
            continue
        for name in names:
            run = os.path.join(codex, name)
            if (RUN_DIR.match(name) and os.path.isfile(os.path.join(run, "UNATTENDED"))
                    and not os.path.exists(os.path.join(run, "CLOSEOUT.md"))):
                runs.append(run)
    return runs


def transcript_names(transcript, names):
    """The first of `names` the transcript mentions as a whole run name, or None."""
    patterns = {name: re.compile(re.escape(name.encode()) + rb"(?![0-9])") for name in names}
    tail = b""
    with open(transcript, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                return None
            window = tail + chunk
            for name, pattern in patterns.items():
                if pattern.search(window):
                    return name
            tail = window[-64:]


def unattended_root(payload):
    """The run folder this main-thread session is the unattended root of, or None."""
    if not isinstance(payload, dict) or payload.get("agent_id"):
        return None
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return None
    runs = unattended_runs(payload)
    if not runs:
        return None
    by_name = {}
    for run in runs:
        by_name.setdefault(os.path.basename(run), run)
    name = transcript_names(transcript, list(by_name))
    return by_name.get(name) if name else None


def ask(payload):
    if payload.get("tool_name") != "AskUserQuestion":
        return None
    run = unattended_root(payload)
    if not run:
        return None
    reason = (
        f"Run {os.path.basename(run)} is unattended ({os.path.join(run, 'UNATTENDED')} exists), so the question "
        f"prompt is off for its root. Write the question in {os.path.join(run, 'PARKED.md')} with your "
        "recommendation, apply the recommendation unless it is one of the things that wait for the owner, and "
        "carry on with everything it does not block. Remove UNATTENDED only when the owner says they are back, "
        "then put the parked questions to them in one batch."
    )
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def waiting(message):
    lines = [line for line in (message or "").splitlines() if line.strip()] if isinstance(message, str) else []
    return bool(lines) and WAITING.match(lines[-1]) is not None


def stop(payload):
    run = unattended_root(payload)
    if not run or waiting(payload.get("last_assistant_message")):
        return None
    session = re.sub(r"[^\w.-]", "_", str(payload.get("session_id") or "")).lstrip(".")
    if not session:
        return None
    base = os.environ.get(STATE_DIR_ENV) or os.path.expanduser("~/.local/state/agent-loops")
    directory = os.path.join(base, "run-stop")
    path = os.path.join(directory, session + ".json")
    count = 0
    if payload.get("stop_hook_active"):
        try:
            with open(path, encoding="utf-8") as fh:
                count = int(json.load(fh).get("count", 0))
        except (OSError, ValueError, AttributeError, TypeError):
            count = 0
    if count >= MAX_CONTINUATIONS:
        return None
    # Count before blocking: a count that cannot be written raises, and the stop goes through.
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"count": count + 1, "run": run}, fh)
    reason = (
        f"Run {os.path.basename(run)} is unattended and has no {os.path.join(run, 'CLOSEOUT.md')} yet, so it is "
        "not finished. Carry on with the next piece of work in PLAN.md and RESUME.md. If you are waiting on "
        "running lanes, checks or a script, end your reply with one line starting WAITING: that names what you "
        "wait for. When the goal is met, the queue is empty or nothing has merged for three hours, stop the run "
        "as run.md says and write CLOSEOUT.md. If the owner is back, remove "
        f"{os.path.join(run, 'UNATTENDED')} instead. "
        f"(continuation {count + 1} of {MAX_CONTINUATIONS})"
    )
    return {"decision": "block", "reason": reason}


def main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        out = ask(payload) if mode == "ask" else stop(payload) if mode == "stop" else None
    except Exception as exc:  # a gate fault must never trap a session or block a call
        print(f"run gate: {exc}", file=sys.stderr)
        return 0
    if out:
        json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
