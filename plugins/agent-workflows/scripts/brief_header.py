#!/usr/bin/env python3
"""PreToolUse hook: an armed loop root's lane brief must open with the contract's header.

Loop v3 contract section 3 fixes a brief's first line as
``Lane: <id> · Task: <ID> (<title>) · Tier: routine|guarded``; a lane with no single task writes
``Task: none (<purpose>)``. Harnesses parse that line to know which task each live lane holds.

On an `Agent` (or legacy `Task`) call in a session that `fanout_session.scan` recognises as an armed
loop root, the call is denied unless the prompt's first non-empty line parses. `HEADER_RE` is the
same expression loop-pi's loop-state uses, so both harnesses accept and refuse the same lines,
except that this hook also refuses a tier copied from the template as `routine|guarded`.

Everything else passes untouched: a session that is not an armed root, a call made inside a
subagent (`agent_id` set), any other tool, and an input with no string prompt. The scan keeps its
own cache key, so it never moves the Stop hook's continuation state. A hook fault allows the call.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fanout_session  # noqa: E402

DISPATCH_TOOLS = ("Agent", "Task")
HEADER_FORM = "Lane: <id> · Task: <ID> (<title>) · Tier: routine|guarded"
# loop-pi extensions/loop-state/core.ts HEADER_RE, verbatim.
HEADER_RE = re.compile(r"^Lane:\s*(\S+?)\s*·\s*Task:\s*(\S+?)(?:\s*\([^·]*\))?\s*·\s*Tier:\s*(routine|guarded)\b")
CACHE_SUFFIX = "-brief-header"
SHOWN = 100


def first_line(text):
    """The first non-empty line of `text`, stripped, or ""."""
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def header_ok(prompt):
    """True when the first non-empty line parses. The one place this is stricter than loop-pi: a tier
    copied from the template as `routine|guarded` names no tier, so it is refused."""
    line = first_line(prompt)
    match = HEADER_RE.match(line)
    return match is not None and not line[match.end():].lstrip().startswith("|")


def decide(payload):
    """Return the denial reason, or None to let the call through."""
    if not isinstance(payload, dict) or payload.get("agent_id"):
        return None
    if payload.get("tool_name") not in DISPATCH_TOOLS:
        return None
    tool_input = payload.get("tool_input")
    prompt = tool_input.get("prompt") if isinstance(tool_input, dict) else None
    if not isinstance(prompt, str) or header_ok(prompt):
        return None
    session_id = payload.get("session_id")
    transcript = payload.get("transcript_path")
    if not session_id or not transcript:
        return None
    state = fanout_session.scan(session_id + CACHE_SUFFIX, transcript, payload.get("cwd"))
    if not state.get("armed") or not state.get("launch"):
        return None
    seen = first_line(prompt)
    if len(seen) > SHOWN:
        seen = seen[:SHOWN] + "..."
    return (
        "loop brief header: this session is a loop root, so the first non-empty line of every lane "
        f"brief must be exactly `{HEADER_FORM}` (contract section 3), with a colon after Lane, Task "
        "and Tier, the middle dot `·` as separator and none inside the title. Reviewer, scout, "
        "compose and board lanes carry it too: a lane with no single task writes "
        "`Task: none (<purpose>)`. "
        f"Got: `{seen}`. Dispatch again with the header as the prompt's first line."
    )


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    try:
        reason = decide(payload)
    except Exception as exc:  # a hook fault must never block a dispatch
        print(f"brief-header: {exc}", file=sys.stderr)
        return 0
    if reason:
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            sys.stdout,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
