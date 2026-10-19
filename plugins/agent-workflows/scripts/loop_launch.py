"""Loop v3 launch parser (seam S1).

Pure, stdlib-only module shared verbatim by the Claude Stop hook (`fanout_session.py`) and the
watchdog. No imports from other project scripts, and no I/O beyond the explicit `read_file`
injection point and the filesystem existence checks it documents below. Its public surface is
`parse_launch` and `report_target`; do not import anything else from outside. The whole module is
copied byte-for-byte into another repo.

A message is a launch when its operator text either:

  (a) matches ``\\bYou are the root\\b`` and contains exactly one distinct closeout
      token (``codex/closeout-<name>-loop<N>.md``); or
  (b) consists only of a path to an existing regular file named ``launch-*.txt`` or
      ``launch-*.md`` (after trimming whitespace and surrounding backticks), whose contents
      satisfy (a).
"""

import hashlib
import os
import re
from pathlib import Path

MARKER_RE = re.compile(r"\bYou are the root\b")

# The closeout token name. The loop number is only ever the "-loop<N>.md" suffix.
NAME = r"closeout-[\w.-]*-loop\d+\.md"

# Closeout-token prefixes: "/abs/.../codex/", "./codex/" or bare "codex/".
_PREFIX = r"(?:/(?:[^\s`'\"()]*/)*codex/|\./codex/|codex/)"

# Left boundary: start of text, whitespace, or one of ` ' " (
# Right boundary: end of text, whitespace, or one of ` ' " ) , ; :, or a sentence-ending "."
# that is itself followed by whitespace or the end of text ("Write codex/closeout-x-loop5.md.").
# "codex/closeout-x-loop5.md.backup" still does not match: that "." is followed by "b".
_LEFT = r"(?:\A|(?<=[\s`'\"(]))"
_RIGHT = r"(?:\Z|(?=[\s`'\")\,;:])|(?=\.(?:\s|\Z)))"

_LOOP_SUFFIX_RE = re.compile(r"-loop(\d+)\.md$")

_BARE_LAUNCH_NAME_RE = re.compile(r"^launch-.*\.(?:txt|md)$")

# Optional "Time budget: <N>s|m|h" line in the recognised launch text (pasted or file-backed).
_BUDGET_RE = re.compile(r"^\s*Time budget:\s*(\d+)\s*([smh])\b", re.IGNORECASE | re.MULTILINE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}


_TOKEN_RE = re.compile(_LEFT + "(" + _PREFIX + NAME + ")" + _RIGHT)


def _find_closeout_tokens(text):
    return [m.group(1) for m in _TOKEN_RE.finditer(text)]


def _extract_loop(token):
    m = _LOOP_SUFFIX_RE.search(token)
    return int(m.group(1)) if m else None


def _budget_seconds(text):
    m = _BUDGET_RE.search(text)
    return int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()] if m else None


def _single_target(tokens, base):
    """Resolve every token against `base` and return {"report", "loop"} when they name exactly one
    distinct target, else None.

    A relative token that is a path suffix of an absolute token in the same text is that same
    target, whatever `base` is: the absolute spelling wins, even when the relative one would
    resolve elsewhere (a launch pasted from a subdirectory). Every other token is compared after
    resolution against `base`, so two spellings of one path are one target and a relative path
    that is not such a suffix is a second target.
    """
    absolutes = [os.path.normpath(token) for token in tokens if token.startswith("/")]
    targets = {}
    for token in tokens:
        if not token.startswith("/"):
            rel = token[2:] if token.startswith("./") else token
            if any(a.endswith("/" + rel) for a in absolutes):
                continue
        targets.setdefault(_resolve_token(token, base), token)
    if len(targets) != 1:
        return None
    (report, token), = targets.items()
    return {"report": report, "loop": _extract_loop(token)}


def _recognize(text, base):
    """Return {"report": str, "loop": int|None} if `text` matches form (a), else None."""
    if not MARKER_RE.search(text):
        return None
    return _single_target(_find_closeout_tokens(text), base)


def _codex_dir_parent(abs_path):
    """The parent of the nearest enclosing `codex/` directory, or None if there is none."""
    for ancestor in Path(abs_path).parents:
        if ancestor.name == "codex":
            return str(ancestor.parent)
    return None


def _resolve_token(token, base):
    """Resolve a closeout token to a path. `base` is None for a token that is already absolute.

    A relative token with no base (the pasted-launch cwd was unavailable) is left relative rather
    than failing recognition outright: a caller that scans a transcript incrementally only gets one
    chance to recognise a given launch line, so refusing to recognise it here would lose the launch
    permanently rather than merely defer its resolution to a later, cwd-aware check.
    """
    if token.startswith("/"):
        return os.path.normpath(token)
    rel = token[2:] if token.startswith("./") else token
    if not base:
        return rel
    return os.path.normpath(os.path.join(base, rel))


def _strip_wrapping(text):
    s = text.strip()
    if len(s) >= 2 and s[0] == "`" and s[-1] == "`":
        s = s[1:-1].strip()
    return s


def _bare_candidate_path(text):
    """Return the bare path text if `text` is only a path (no other content), else None."""
    s = _strip_wrapping(text)
    if not s or "\n" in s:
        return None
    return s


def _finalize(target, text, launch_ts, hash_bytes):
    return {
        # `report` holds the closeout path; callers read this key name.
        "report": target["report"],
        "loop": target["loop"],
        "launch_ts": launch_ts,
        "mode": "v3",
        "launch_sha256": hashlib.sha256(hash_bytes).hexdigest(),
        # Optional, not one of the frozen snapshot keys: the "Time budget" of the recognised text
        # (the launch file's contents for a bare-path launch), in seconds, or None.
        "budget": _budget_seconds(text),
    }


def report_target(text, cwd, mode):
    """Token grammar and resolution for a message that is not itself a launch (for example a "Do
    not pivot on receipt" replacement): the single distinct closeout target in `text`, as
    ``{report, loop}``, or None for zero or several targets. `mode` is the armed snapshot's mode;
    it is accepted for caller compatibility and ignored, since there is one token shape.
    """
    if not text:
        return None
    return _single_target(_find_closeout_tokens(text), cwd or None)


def parse_launch(text, cwd, launch_ts, activated_at, read_file=Path.read_text):
    """Recognise a loop launch in `text`.

    Returns ``{report, loop, launch_ts, mode, launch_sha256, budget}`` or None (`mode` is always
    ``"v3"``; `budget` is an optional, non-frozen key: seconds or None). `activated_at` is
    accepted for caller compatibility and ignored. `cwd` is the launch-time working directory used to
    resolve a relative closeout token pasted directly in `text`. `read_file`
    is called as ``read_file(Path(path))`` and defaults to `Path.read_text`, letting a caller stub
    file reads in tests without touching the real filesystem.
    """
    if not text:
        return None
    direct = _recognize(text, cwd or None)
    if direct is not None:
        return _finalize(direct, text, launch_ts, text.encode("utf-8"))

    candidate = _bare_candidate_path(text)
    if candidate is None:
        return None
    launch_path = candidate if os.path.isabs(candidate) else os.path.normpath(os.path.join(cwd or "", candidate))
    if not _BARE_LAUNCH_NAME_RE.match(os.path.basename(launch_path)):
        return None
    if not os.path.isfile(launch_path):
        return None
    try:
        content = read_file(Path(launch_path))
    except (OSError, ValueError):  # ValueError includes UnicodeDecodeError
        return None
    if not isinstance(content, str):
        return None

    # A relative closeout resolves against the parent of the codex/ directory holding the launch
    # file. Outside any codex/ directory there is no base, so the file must name an absolute
    # closeout path (a relative token survives only as a suffix of that absolute one).
    inner = _recognize(content, _codex_dir_parent(launch_path))
    if inner is None or not os.path.isabs(inner["report"]):
        return None
    return _finalize(inner, content, launch_ts, content.encode("utf-8"))
