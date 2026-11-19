"""Shared state for the loop root Stop hooks (Loop v3).

A session is a loop root when its operator text is recognised as a launch by
`loop_launch.parse_launch` (it says "You are the root" and names a
single `codex/closeout-*-loop<N>.md` path, pasted or bare-launch-file-backed). A mid-run
replacement that starts "Do not pivot on receipt" may move the closeout path within the same loop
number. The transcript is scanned incrementally and the result cached per session, so a
multi-hour transcript is read once rather than on every hook call.

Once armed, nothing but a new launch changes the armed state: an ordinary operator message, a
steering message, a compaction summary or a tool result never disarms it. A Stop event is allowed
only when a live per-call check finds the snapshotted closeout currently counts (seam S2) or the
last assistant message's last non-empty line carries a current `PAUSED:`/`WAITING:` marker, after
stripping markdown decoration from that line.

A closeout counts when it is a regular file written after the launch, line 1 matches `HEADER_RE`
with the launch's loop number, and it holds each of `REQUIRED_SECTIONS` on a line of its own.

Loop v4 adds a landing-ledger rule on top of that. The hook knows only the closeout's path; by the
contract's file naming it finds the run directory (`codex/loop<N>/`, holding `landings.jsonl`) and
the charter (`codex/charter-*-loop<N>.md`). When the charter says `protocol: v4` and not
`loop-land: off`, a closeout that passes the v3 rule still does not count while the ledger cannot
be parsed, a landing's last event is `ready` or `merged`, a `verified` event has main or the deploy
`unread`, or the last `closeout-check` event is missing, older than the last `merged` event, or not
`ok`. A charter without `protocol:`, or with `loop-land: off`, is judged by the v3 rule alone.
"""

import datetime
import json
import os
import re
import tempfile
import uuid

import loop_launch

HOOK_MARKER = "[fanout-continue]"
REPLACEMENT_RE = re.compile(r"^\s*Do not pivot on receipt")

# Overridable for tests only: never write to the real ~/.local/state from a test.
STATE_DIR_ENV = "AGENT_LOOPS_STATE_DIR"

HEADER_RE = re.compile(r"^# Closeout: \S+ loop(\d+) · Charter: [0-9a-f]{64}$")
REQUIRED_SECTIONS = (
    "## Landed",
    "## Parked",
    "## For ratification",
    "## Notes",
)

CLOSEOUT_NAME_RE = re.compile(r"^closeout-.*-loop(\d+)\.md$")
CHARTER_KEY_RE = re.compile(r"^([A-Za-z][\w-]*):[ \t]*(.*?)\s*$")
LEDGER_NAME = "landings.jsonl"
# Ledger events that carry a landing's state (a landing is keyed by its pull request, or by its
# head in direct mode). `closeout-check` is run-wide; `error` means the tool could not tell, so it
# changes no landing's state.
LANDING_EVENTS = ("ready", "refused", "merged", "verified", "abandoned")
LEDGER_EVENTS = LANDING_EVENTS + ("closeout-check", "error")
MAIN_STATES = ("green", "red", "unread")
DEPLOY_STATES = ("synced", "failed", "unread", "n/a")

PAUSED_RE = re.compile(r"^PAUSED: .+$")
WAITING_RE = re.compile(r"^WAITING: .+ until (\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?Z)$")
# Markdown decoration a model wraps a marker line in: bullets, quotes, emphasis, a closing period.
MARKER_LEAD_RE = re.compile(r"^[\s*_`>-]+")
MARKER_TRAIL_RE = re.compile(r"[\s.!*_`)]+$")

# A bare slash-command echo ("/compact", "/model opus") and an interrupt notice are not operator
# text: neither starts a turn nor can be a launch. The watchdog applies the same exclusion.
COMMAND_ECHO_RE = re.compile(r"^/[\w:-]+(\s.*)?$")
INTERRUPT_PREFIX = "[Request interrupted by user"

_EXCLUDED_TAGS = (
    "<local-command-stdout",
    "<local-command-caveat",
    "<command-name",
    "<task-notification",
    "<system-reminder",
)


def state_dir():
    """Base dir for loop runtime state, overridable via AGENT_LOOPS_STATE_DIR for tests."""
    override = os.environ.get(STATE_DIR_ENV)
    return override if override else os.path.expanduser("~/.local/state/agent-loops")


def _atomic_write(path, write_fn):
    """Write via a unique temp file in the same directory, then os.replace."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w") as f:
            write_fn(f)
        os.replace(tmp, path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _atomic_write_json(path, payload):
    _atomic_write(path, lambda f: json.dump(payload, f))


def cache_dir():
    path = os.path.join(tempfile.gettempdir(), "agent-workflows-fanout")
    os.makedirs(path, exist_ok=True)
    return path


def _safe_session(session_id):
    """A session id made safe to use as a file name (no separators, no traversal)."""
    return re.sub(r"[^\w-]", "_", session_id or "unknown")


def _cache_path(session_id):
    return os.path.join(cache_dir(), _safe_session(session_id) + ".json")


# Cache format version. A cache written by an earlier hook (an older version or unversioned) holds a
# launch snapshot in a retired shape and a cursor past the launch, so it is discarded and the
# transcript rescanned.
STATE_VERSION = 3


def _fresh_state(transcript_path):
    return {
        "v": STATE_VERSION,
        "transcript": transcript_path,
        "offset": 0,
        "armed": False,
        "launch": None,
        "continuations": 0,
        "chain_incident_written": False,
    }


def _checked(state, transcript_path):
    """The cached state if it is a current-version object, else a fresh state."""
    if not isinstance(state, dict) or state.get("v") != STATE_VERSION:
        return _fresh_state(transcript_path)
    return state


def load_state(session_id, transcript_path=None):
    try:
        with open(_cache_path(session_id)) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return _fresh_state(transcript_path)
    return _checked(raw, transcript_path)


def save_state(session_id, state):
    state["v"] = STATE_VERSION
    _atomic_write_json(_cache_path(session_id), state)


def _prompt_text(entry):
    """Return the text of an operator prompt, or None for anything else."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return None
    if entry.get("isCompactSummary") or entry.get("type") == "summary":
        return None
    origin = entry.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(p, dict) and p.get("type") == "tool_result" for p in content):
            return None
        text = "\n".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        )
    else:
        return None
    stripped = text.lstrip()
    if not stripped:
        return None
    if any(tag in text for tag in _EXCLUDED_TAGS):
        return None
    if HOOK_MARKER in text:
        return None
    if COMMAND_ECHO_RE.match(text.strip()) or stripped.startswith(INTERRUPT_PREFIX):
        return None
    return text


def codex_prompt_text(entry):
    """Return the text of a Codex operator prompt, or None for anything else.

    Codex records operator text as a `response_item` user message whose
    `content_item_kinds` entries are `user.text`; AGENTS.md, environment context and hook prompts
    share the role but carry other kinds. The loop watchdog uses the same kind rule; this also
    drops hook prompts and slash-command echoes.
    """
    if entry.get("type") != "response_item":
        return None
    payload = entry.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message" or payload.get("role") != "user":
        return None
    content = payload.get("content")
    kinds = (payload.get("internal_chat_message_metadata_passthrough") or {}).get("content_item_kinds")
    if not isinstance(content, list) or not isinstance(kinds, list) or len(kinds) != len(content):
        return None
    text = "\n".join(
        item["text"]
        for item, kind in zip(content, kinds)
        if kind == "user.text" and isinstance(item, dict) and isinstance(item.get("text"), str)
    )
    stripped = text.strip()
    if not stripped or HOOK_MARKER in text or stripped.startswith("<hook_prompt"):
        return None
    if COMMAND_ECHO_RE.match(stripped):
        return None
    return text


def _epoch(timestamp):
    if not timestamp:
        return None
    try:
        return datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def scan(session_id, transcript_path, cwd=None, extract=None, first_only=False):
    """Advance the cached scan of transcript_path and return the session state.

    A relative closeout token resolves at recognition against the launch entry's own `cwd` (Claude
    entries carry one); `cwd` here, the hook payload's cwd, is only the fallback for an entry that
    has none. A launch recognised with neither keeps a relative closeout path, resolved at Stop
    time by report_status() below. A malformed entry is skipped, never fatal. `extract` returns an
    entry's operator text or None; it defaults to the Claude transcript rule. With `first_only`, an
    unarmed session arms only when its first operator message is a launch, as the watchdog decides.
    """
    state = load_state(session_id, transcript_path)
    if state.get("transcript") != transcript_path:
        state = _fresh_state(transcript_path)
    try:
        with open(transcript_path, "rb") as f:
            f.seek(state["offset"])
            data = f.read()
    except OSError:
        return state
    end = data.rfind(b"\n")
    if end < 0:
        return state
    for raw in data[: end + 1].splitlines():
        try:
            _apply_entry(state, json.loads(raw), cwd, extract or _prompt_text, first_only)
        except Exception:  # one bad entry must not stop the scan or trap the session
            continue
    state["offset"] += end + 1
    save_state(session_id, state)
    return state


def _apply_entry(state, entry, cwd, extract=_prompt_text, first_only=False):
    if not isinstance(entry, dict):
        return
    text = extract(entry)
    if text is None:
        return
    seen = state.get("operator_seen")
    state["operator_seen"] = True
    if first_only and seen and not state.get("armed"):
        return
    entry_cwd = entry.get("cwd")
    base = entry_cwd if isinstance(entry_cwd, str) and entry_cwd else cwd
    ts = entry.get("timestamp")
    parsed = loop_launch.parse_launch(text, base, ts, None)
    if parsed is not None:
        state["launch"] = dict(parsed, start=_epoch(ts))
        state["armed"] = True
        state["continuations"] = 0
        state["chain_incident_written"] = False
    elif REPLACEMENT_RE.search(text) and state.get("launch"):
        launch = state["launch"]
        target = loop_launch.report_target(text, base, launch.get("mode"))
        # A replacement may move the closeout path, never the loop: a different loop number is a
        # new run and needs a new launch.
        if target is not None and (launch.get("loop") is None or target["loop"] == launch["loop"]):
            launch["report"] = target["report"]
            launch["loop"] = target["loop"]
        state["continuations"] = 0
        state["chain_incident_written"] = False
    # Nothing else disarms: an ordinary operator message, a steering message, a compaction summary
    # and a tool result all leave `armed`/`launch` untouched.


def resolve_report_path(report, cwd):
    """An absolute snapshot is used as is. A still-relative one (a launch recognised with no cwd at
    all) is resolved at Stop time: expand `~`, then walk up from cwd to the first directory where
    the closeout exists or that holds `.git`."""
    path = os.path.expanduser(report)
    if os.path.isabs(path):
        return os.path.normpath(path)
    base = cwd or os.getcwd()
    probe = base
    while True:
        candidate = os.path.join(probe, path)
        if os.path.exists(candidate) or os.path.isdir(os.path.join(probe, ".git")):
            return os.path.normpath(candidate)
        parent = os.path.dirname(probe)
        if parent == probe:
            return os.path.normpath(os.path.join(base, path))
        probe = parent


def report_status(launch, cwd):
    """Seam S2: ``(counts, why_not)`` for the snapshotted closeout right now. `why_not` is a short
    clause naming the first failed rule (None when the closeout counts)."""
    if not launch or not launch.get("report"):
        return False, "is not known"
    path = resolve_report_path(launch["report"], cwd)
    if os.path.islink(path):
        return False, "is a symlink, not a regular file"
    if not os.path.exists(path):
        return False, "does not exist yet"
    if not os.path.isfile(path):
        return False, "is not a regular file"
    try:
        st = os.stat(path)
    except OSError:
        return False, "cannot be read"
    launch_epoch = _epoch(launch.get("launch_ts"))
    if launch_epoch is not None and st.st_mtime <= launch_epoch:
        return False, "predates the launch (it was last written before this run started)"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return False, "cannot be read"
    lines = content.splitlines()
    header = HEADER_RE.match(lines[0]) if lines else None
    if not header:
        return False, "has no valid header on line 1 (`# Closeout: <repo> loop<N> · Charter: <64-hex sha256>`)"
    if launch.get("loop") is not None and int(header.group(1)) != launch["loop"]:
        return False, f"has a header that names loop {int(header.group(1))}, not loop {launch['loop']}"
    missing = [section for section in REQUIRED_SECTIONS if section not in lines]
    if missing:
        return False, "is missing sections: " + ", ".join(missing)
    ledger_why = _ledger_rule(path)
    if ledger_why:
        return False, ledger_why
    return True, None


def _charter_for(codex_dir, loop):
    """The path of the run's charter (`charter-*-loop<N>.md` beside the closeout), or None. When
    several carry the loop number the last by name (the latest date) is the run's."""
    name_re = re.compile(r"^charter-.*-loop" + str(loop) + r"\.md$")
    try:
        names = sorted(n for n in os.listdir(codex_dir) if name_re.match(n))
    except OSError:
        return None
    for name in reversed(names):
        path = os.path.join(codex_dir, name)
        if os.path.isfile(path):
            return path
    return None


FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _charter_requires_ledger(path):
    """True when the charter says `protocol: v4` and does not say `loop-land: off`. Only a `key:
    value` line in the charter's header counts (before the first `## ` heading, outside code
    fences, which close only on a fence of the same character at least as long); the first of each
    key wins, and its first token is compared, quotes stripped."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return False
    keys = {}
    fence = None
    for line in lines:
        marker = FENCE_RE.match(line)
        if fence:
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence) \
                    and not line.strip().strip(fence[0]):
                fence = None
            continue
        if marker:
            fence = marker.group(1)
            continue
        if line.startswith("## "):
            break
        match = CHARTER_KEY_RE.match(line)
        if match and match.group(1).lower() not in keys:
            value = match.group(2).split()
            keys[match.group(1).lower()] = value[0].strip("\"'`").lower() if value else ""
    return keys.get("protocol") == "v4" and keys.get("loop-land") != "off"


def _ledger_rule(closeout_path):
    """The v4 landing-ledger rule for a closeout that passed the v3 rule: None when it holds or
    does not apply, else a short clause naming the first failed rule."""
    named = CLOSEOUT_NAME_RE.match(os.path.basename(closeout_path))
    if not named:
        return None
    codex_dir = os.path.dirname(closeout_path)
    loop = int(named.group(1))
    charter = _charter_for(codex_dir, loop)
    if charter is None or not _charter_requires_ledger(charter):
        return None
    ledger = os.path.join(codex_dir, f"loop{loop}", LEDGER_NAME)
    why = _ledger_why_not(ledger)
    if why:
        return f"does not count under protocol v4: {why}"
    return None


def _ledger_event(line):
    """One parsed ledger line as ``(event, epoch)``, or a string naming what is wrong with it."""
    try:
        event = json.loads(line)
    except (ValueError, RecursionError):
        return "is not valid JSON"
    if not isinstance(event, dict):
        return "is not a JSON object"
    if event.get("event") not in LEDGER_EVENTS:
        return f"has an unknown event {event.get('event')!r}"
    epoch = _epoch(event.get("ts")) if isinstance(event.get("ts"), str) else None
    if epoch is None:
        return "has no readable `ts`"
    if event["event"] in LANDING_EVENTS and _landing_key(event) is None:
        return "names neither a pull request nor a head"
    if event["event"] == "verified" and (
        event.get("main") not in MAIN_STATES or event.get("deploy") not in DEPLOY_STATES
    ):
        return "is a `verified` event without a valid `main` and `deploy`"
    return event, epoch


def _landing_key(event):
    pr, head = event.get("pr"), event.get("head")
    if type(pr) is int:
        return f"pull request #{pr}"
    if isinstance(head, str) and head.strip():
        return f"head {head[:12]}"
    return None


def _unsettled(history):
    """``(what, hint)`` when a landing's events leave it unsettled, else None. A landing that was
    merged needs a `verified` event after its merge with main and the deploy read, whatever event
    follows (`abandoned` and `refused` do not undo a merge); one never merged is open only at
    `ready`."""
    last_merged = max((i for i, e in enumerate(history) if e["event"] == "merged"), default=None)
    if last_merged is not None:
        verified = [e for e in history[last_merged:] if e["event"] == "verified"]
        if not verified:
            return "was merged and not verified", "run `loop-land verify`"
        latest = verified[-1]
        if "unread" in (latest["main"], latest["deploy"]):
            what = "main" if latest["main"] == "unread" else "the deploy"
            return f"was verified with {what} unread", "run `loop-land verify` again"
        return None
    if history[-1]["event"] == "ready":
        return "is at `ready`", "run `loop-land verify` if it merged, else `loop-land abandon`"
    return None


def _ledger_why_not(path):
    """None when the run's landing ledger lets the closeout count, else a clause saying why not."""
    if not os.path.exists(path):
        return (f"the landing ledger {path} does not exist, so there is no `closeout-check` "
                "event (run `loop-land summary --check`)")
    if not os.path.isfile(path):
        return f"the landing ledger {path} is not a regular file"
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return f"the landing ledger {path} cannot be read or parsed"
    events = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        parsed = _ledger_event(line)
        if isinstance(parsed, str):
            return f"the landing ledger {path} cannot be parsed: line {number} {parsed}"
        events.append(parsed)
    states = {}
    for event, _ in events:
        if event["event"] in LANDING_EVENTS:
            states.setdefault(_landing_key(event), []).append(event)
    open_landings, hints = [], set()
    for key, history in states.items():
        problem = _unsettled(history)
        if problem:
            open_landings.append(f"{key} {problem[0]}")
            hints.add(problem[1])
    if open_landings:
        return ("the landing ledger is not settled: " + "; ".join(open_landings[:5])
                + " (" + " or ".join(sorted(hints)) + ")")
    checks = [i for i, (e, _) in enumerate(events) if e["event"] == "closeout-check"]
    if not checks:
        return "the landing ledger has no `closeout-check` event (run `loop-land summary --check`)"
    merges = [(i, t) for i, (e, t) in enumerate(events) if e["event"] == "merged"]
    last = checks[-1]
    check, check_epoch = events[last]
    if merges and (last < merges[-1][0] or check_epoch < max(t for _, t in merges)):
        return ("the last `closeout-check` event is older than the last `merged` event "
                "(run `loop-land summary --check` again)")
    if check.get("outcome") != "ok":
        missing = check.get("missing")
        return ("the last `closeout-check` event is not `ok`"
                + (f" (the ledger lacks: {missing})" if missing else ""))
    return None


def report_counts(launch, cwd):
    """Seam S2: does the snapshotted closeout count right now?"""
    return report_status(launch, cwd)[0]


def report_predates_launch(launch, cwd):
    """True when the snapshotted closeout is a regular file last written before the launch: the
    signature of a launch quoted in a session that is not that run's root."""
    if not launch or not launch.get("report"):
        return False
    launch_epoch = _epoch(launch.get("launch_ts"))
    if launch_epoch is None:
        return False
    path = resolve_report_path(launch["report"], cwd)
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return False
        return os.stat(path).st_mtime <= launch_epoch
    except OSError:
        return False


def final_marker(last_message):
    """The marker on the last non-empty line of `last_message`, or None."""
    if not last_message:
        return None
    lines = [line.strip() for line in last_message.splitlines() if line.strip()]
    if not lines:
        return None
    last_line = MARKER_TRAIL_RE.sub("", MARKER_LEAD_RE.sub("", lines[-1]))
    if PAUSED_RE.match(last_line):
        return "paused"
    if WAITING_RE.match(last_line):
        return "waiting"
    return None


def write_incident(session_id, home, klass):
    """An incident file for the watchdog. klass is hook-exhausted|hook-exception."""
    incident_id = uuid.uuid4().hex
    payload = {
        "v": 1,
        "id": incident_id,
        "session": session_id or "unknown",
        "home": home,
        "class": klass,
        "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = os.path.join(state_dir(), "incidents", f"{incident_id}.json")
    _atomic_write_json(path, payload)
    return path


def append_stop_result(session_id, decision, reason, chain):
    """One JSONL line per Stop event, for the watchdog's Claude reducer."""
    path = os.path.join(state_dir(), "stop-results", f"{_safe_session(session_id)}.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    record = {
        "at": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z",
        "decision": decision,
        "reason": reason,
        "chain": chain,
    }
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")
