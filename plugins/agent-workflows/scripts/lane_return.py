#!/usr/bin/env python3.15
"""SubagentStop hook: record a run lane's return; never block it.

A lane's return is plain text whose first line is `Status: complete|partial|blocked|failed`. This hook reads
that status line, a branch and a commit from the text and appends them, one JSON line per stop, to
`<state>/lane-returns/<session>.jsonl` (state is `$AGENT_LOOPS_STATE_DIR`, else `~/.local/state/agent-loops`).
The root reads the return itself and repairs what it needs; the hook never sends a lane back over the
shape of its return. A return with no status line is recorded with a null status. It acts only on
the six agent-workflows roles; any fault, malformed input included, lets the stop through.
"""

import datetime
import json
import os
import re
import sys

ROLES = {"agent-workflows:" + role for role in
         ("worker", "composer", "senior", "senior-escalation", "reviewer", "scout")}
STATE_DIR_ENV = "AGENT_LOOPS_STATE_DIR"
SCAN_CHARS = 65536
STATUS = re.compile(r"^[\s>*_`#-]*status[*_`]*\s*:[*_`\s]*(complete|partial|blocked|failed)\b", re.I | re.M)
FULL_SHA = re.compile(r"(?<![0-9a-fA-F])([0-9a-f]{40})(?![0-9a-fA-F])")
SHORT_SHA = re.compile(r"\bcommit(?:ted)?\b[\s:`*]*(?:as\s+|at\s+)?([0-9a-f]{7,40})\b", re.I)
SHORT_SHA_BEFORE = re.compile(r"\bcommit(?:ted)?\s+`?([0-9a-f]{7,40})\b", re.I)
BRANCH = re.compile(r"\bbranch\b[\s:`*]*(?:is\s+|named\s+)?`?([A-Za-z0-9][\w./-]*[\w-])", re.I)


def extract(message):
    text = message[:SCAN_CHARS] if isinstance(message, str) else ""
    status = STATUS.search(text)
    commit = FULL_SHA.search(text) or SHORT_SHA.search(text) or SHORT_SHA_BEFORE.search(text)
    branch = BRANCH.search(text)
    return {
        "status": status.group(1).lower() if status else None,
        "branch": branch.group(1) if branch else None,
        "commit": commit.group(1) if commit else None,
    }


def record(payload):
    agent_id, agent_type = payload.get("agent_id"), payload.get("agent_type")
    if not isinstance(agent_id, str) or not agent_id.strip() or agent_type not in ROLES:
        return
    if payload.get("hook_event_name") not in (None, "SubagentStop"):
        return
    entry = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds"),
             "agent_id": agent_id, "agent_type": agent_type, **extract(payload.get("last_assistant_message"))}
    session = re.sub(r"[^\w.-]", "_", str(payload.get("session_id") or "unknown")).lstrip(".") or "unknown"
    base = os.environ.get(STATE_DIR_ENV) or os.path.expanduser("~/.local/state/agent-loops")
    directory = os.path.join(base, "lane-returns")
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, session + ".jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def main():
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict):
            record(payload)
    except Exception as exc:  # a record is never worth a blocked lane
        print("lane return: " + str(exc), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
