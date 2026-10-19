#!/usr/bin/env python3
"""Stop hook: keep a loop root working until its closeout exists.

Loop v3 run identity (seams S1 and S2). Once a session is armed by a launch
(fanout_session.scan), nothing but a new launch disarms it. On a Stop event while armed:

  - allow if the snapshotted closeout currently counts (fanout_session.report_status), or the last
    assistant message's last non-empty line carries a current PAUSED:/WAITING: marker;
  - otherwise block, up to 3 times in a row within one hook-driven chain (identified by
    stop_hook_active). A 4th Stop in the same chain allows the stop and writes one hook-exhausted
    incident file for that chain, unless the closeout already existed before the launch (a launch
    quoted in a session that is not that run's root).

After every Stop on an armed session, a result line is appended to
~/.local/state/agent-loops/stop-results/<session>.jsonl for the watchdog's Claude reducer. A hook
exception always allows the stop and writes a hook-exception incident file.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fanout_session  # noqa: E402

MAX_CONTINUATIONS = 3


def _home_for_incident():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def decide(payload):
    """Return the block reason, or None to let the turn end.

    Appends a stop-results line for every Stop on a session this hook recognises as armed.
    """
    if payload.get("agent_id"):
        return None
    session_id = payload.get("session_id")
    transcript = payload.get("transcript_path")
    if not session_id or not transcript:
        return None
    state = fanout_session.scan(session_id, transcript, payload.get("cwd"))
    launch = state.get("launch")
    if not state.get("armed") or not launch:
        return None

    cwd = payload.get("cwd")
    counted, why_not = fanout_session.report_status(launch, cwd)
    marker = None if counted else fanout_session.final_marker(payload.get("last_assistant_message"))

    active = bool(payload.get("stop_hook_active"))
    count = state.get("continuations", 0) if active else 0
    if not active:
        state["chain_incident_written"] = False

    if counted or marker is not None:
        reason = "report-counted" if counted else f"{marker}-marker"
        fanout_session.save_state(session_id, state)
        fanout_session.append_stop_result(session_id, "allow", reason, count)
        return None

    if count >= MAX_CONTINUATIONS:
        if not state.get("chain_incident_written") and not fanout_session.report_predates_launch(launch, cwd):
            fanout_session.write_incident(session_id, _home_for_incident(), "hook-exhausted")
            state["chain_incident_written"] = True
        fanout_session.save_state(session_id, state)
        fanout_session.append_stop_result(session_id, "allow", "chain-exhausted", count)
        return None

    state["continuations"] = count + 1
    fanout_session.save_state(session_id, state)
    reason = (
        f"{fanout_session.HOOK_MARKER} The closeout {launch['report']} {why_not}, so this "
        "loop run is not finished. Continue with the next open item from the charter and "
        "the run files, and put any status note in the same message as your next tool call. "
        "If you are waiting on a running subagent, background command or monitor, end your reply "
        "with one line of exactly this form, with a UTC deadline: "
        "WAITING: <what> until <YYYY-MM-DDTHH:MMZ>\n"
        "If the run cannot continue without the operator, end your reply with one line: "
        "PAUSED: <reason>\n"
        "A bare WAITING: line with no deadline does not end the turn. When no ready work remains "
        "and no lane or watcher is live, write the closeout under the loop contract: line 1 exactly "
        "`# Closeout: <repo> loop<N> · Charter: <charter sha256>`, then the sections "
        "`## Landed`, `## Parked`, `## For ratification` and `## Notes`. "
        f"(continuation {count + 1} of {MAX_CONTINUATIONS})"
    )
    fanout_session.append_stop_result(session_id, "block", "continuation-block", count + 1)
    return reason


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    try:
        reason = decide(payload)
    except Exception as exc:  # a hook fault must never trap the session
        print(f"fanout-continue: {exc}", file=sys.stderr)
        try:
            session_id = payload.get("session_id") if isinstance(payload, dict) else None
        except Exception:
            session_id = None
        try:
            fanout_session.write_incident(session_id, _home_for_incident(), "hook-exception")
            fanout_session.append_stop_result(session_id, "allow", "hook-exception", None)
        except Exception as inner:  # incident bookkeeping must never trap the session either
            print(f"fanout-continue: incident write failed: {inner}", file=sys.stderr)
        return 0
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
