#!/usr/bin/env python3
"""SubagentStop hook: send a lane back when its lane-return block does not check out.

Plugin-agent frontmatter hooks are ignored by Claude, so the manifest registers this script as a
global SubagentStop hook. It acts only on a nonempty agent_id with one of five exact qualified
agent-workflows types; roots, unrelated agents and empty-type forks are no-ops. Repeated stops
still validate, but Claude's native consecutive-stop-block cap (default 8) can end a lane despite
later block requests. No transcript scan, command execution or state write occurs. Lane commands
are never inspected here: lanes' no-push rule stays in the agent definitions.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


BLOCK = re.compile(r"^```lane-return[ \t]*\r?\n(.*?)\r?\n```[ \t]*\r?$", re.M | re.S)
SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
HASH = re.compile(r"[0-9a-fA-F]{64}\Z")
ROLES = {"agent-workflows:" + role: role for role in
         ("worker", "senior", "senior-escalation", "reviewer", "scout")}
MAX_RETURN_UNITS = 1800
MAX_MESSAGE_CHARS = 65536
MAX_JSON_DEPTH = 32
REQUIRED = {"v", "lane", "task", "status", "base", "worktree", "check", "exit", "tail",
            "coderabbit", "dsreview", "questions"}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def review_ok(value, kind):
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    if kind == "coderabbit":
        return (type(value.get("ran")) is bool
                and all(type(value.get(k)) is int and value[k] >= 0 for k in ("major", "unreviewed")))
    return (value.get("verdict") in ("PASS", "FAIL", "none")
            and all(type(value.get(k)) is int and value[k] >= 0 for k in ("major", "fixed", "disputed")))


def bounded_json(text, limit):
    """Bound size and nesting before invoking the recursive decoder; honour JSON string escapes."""
    if len(text) > limit:
        raise ValueError("JSON size limit exceeded")
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("JSON depth limit exceeded")
        elif char in "]}":
            depth -= 1
    return json.loads(text, object_pairs_hook=unique_object)


def return_error(payload, role):
    message = payload.get("last_assistant_message")
    if not isinstance(message, str):
        return "missing last_assistant_message with a lane-return block"
    if len(message) > MAX_MESSAGE_CHARS:
        return "last_assistant_message size limit exceeded"
    blocks = list(BLOCK.finditer(message))
    if len(blocks) != 1:
        return "expected exactly one fenced lane-return JSON block"
    try:
        if len(blocks[0].group().encode("utf-16-le")) // 2 > MAX_RETURN_UNITS:
            return "lane-return size limit exceeded (1800 UTF-16 units including fences)"
        value = bounded_json(blocks[0].group(1), MAX_RETURN_UNITS)
    except (ValueError, RecursionError, UnicodeError) as exc:
        return "invalid lane-return JSON: " + str(exc)
    if not isinstance(value, dict):
        return "lane-return JSON must be an object"
    missing = REQUIRED - value.keys()
    if missing:
        return "missing required fields: " + ", ".join(sorted(missing))
    if type(value["v"]) is not int or value["v"] != 3:
        return "v must be 3"
    for key in ("lane", "task", "base", "worktree"):
        if not isinstance(value[key], str) or not value[key].strip():
            return key + " must be a nonempty string"
    if not SHA.fullmatch(value["base"]):
        return "base must be a full commit SHA"
    if not Path(value["worktree"]).is_absolute():
        return "worktree must be absolute"
    if value["status"] not in ("complete", "partial", "blocked", "failed"):
        return "invalid status"
    for key in ("check", "tail"):
        if not isinstance(value[key], str):
            return key + " must be a string"
    if value["exit"] is not None and type(value["exit"]) is not int:
        return "exit must be an integer or null"
    if value["status"] == "complete" and value["exit"] not in (None, 0):
        return "status complete conflicts with nonzero check exit " + str(value["exit"])
    if not isinstance(value["questions"], list) or not all(isinstance(q, str) for q in value["questions"]):
        return "questions must be an array of strings"
    for key in ("coderabbit", "dsreview"):
        if not review_ok(value[key], key):
            return "invalid " + key + " review fields"
    if "sha" in value:
        if role not in ("senior", "senior-escalation"):
            return "sha handbacks are only for senior compose/board lanes"
        if "patch" in value or "patch_sha256" in value:
            return "sha replaces patch and patch_sha256; do not supply both forms"
        if not isinstance(value["sha"], str) or not SHA.fullmatch(value["sha"]):
            return "sha must be a full commit SHA"
        return None
    missing = {"patch", "patch_sha256"} - value.keys()
    if missing:
        return "missing required fields: " + ", ".join(sorted(missing))
    patch, expected = value["patch"], value["patch_sha256"]
    if patch is None and expected is None:
        if value["status"] == "complete" and role not in ("reviewer", "scout"):
            return "complete implementation needs a patch and patch_sha256 (or senior sha)"
        return None
    if not isinstance(patch, str) or not Path(patch).is_absolute():
        return "patch must be an absolute file path (or null with a null hash)"
    if not isinstance(expected, str) or not HASH.fullmatch(expected):
        return "patch_sha256 must be a 64-digit hex hash"
    try:
        # Nonblocking open + regular-file check prevents a named pipe/device from hanging the hook.
        fd = os.open(patch, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        return "cannot read patch " + patch + ": " + (exc.strerror or str(exc))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "patch must name a regular file: " + patch
        digest = hashlib.sha256()
        for chunk in iter(lambda: os.read(fd, 65536), b""):
            digest.update(chunk)
        actual = digest.hexdigest()
    except OSError as exc:
        return "cannot read patch " + patch + ": " + (exc.strerror or str(exc))
    finally:
        os.close(fd)
    if actual != expected.lower():
        return "patch_sha256 mismatch: expected " + expected + ", actual " + actual
    return None


def main():
    try:
        # A malformed/decode-failing envelope cannot establish identity: fail closed with exit 2,
        # never the nonblocking exit 1. Valid unrelated/root envelopes are no-ops before validation.
        payload = json.load(sys.stdin.buffer)
        if not isinstance(payload, dict):
            raise ValueError("hook input must be an object")
        agent_id, agent_type = payload.get("agent_id"), payload.get("agent_type")
        if (not isinstance(agent_id, str) or not agent_id.strip()
                or not isinstance(agent_type, str) or agent_type not in ROLES):
            return 0
        if payload.get("hook_event_name") != "SubagentStop":
            return 0
        reason = return_error(payload, ROLES[agent_type])
        if reason:
            json.dump({"decision": "block", "reason": "lane return: " + reason +
                       ". Repair the return and stop again; preserve an honest partial/blocked status."}, sys.stdout)
    except (ValueError, OSError, TypeError, RecursionError, UnicodeError) as exc:
        print("lane return: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
