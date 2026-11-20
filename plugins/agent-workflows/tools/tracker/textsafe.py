"""What the tracker may write to GitHub: escaping that stops notifications and cross-links, and the PII sweep."""
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
import re

# Only two regions are trusted as code: a closed fence, and a single-backtick span on one line. A span
# never crosses a line, a longer run or a backslash is not one, and an unclosed fence is not one, so
# anything the tool cannot be sure GitHub renders as code is escaped as if it were prose.
_SPAN = re.compile(r"(?<![`\\])(\\*)`([^`\n]+)`(?!`)")
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\n]*)$")
_LOGIN = r"@[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:/[A-Za-z0-9._-]+)?"
_NUMBER = r"(?:#|[Gg][Hh]-)[0-9]+\b"
# One run of tokens written back to back (`#1@octocat`, `@a@b`, `#1#2`) is wrapped as a single span: wrapping
# each alone would leave the next token just outside its neighbour's backticks, where it is live again.
_RUN = re.compile(
    # A qualified reference may only start where its owner name starts (not inside a run of name characters),
    # so a long run of dashes or dots is tried from its first character once, not from every position.
    r"(?<!\\)(\\*)((?:(?<!\w)" + _LOGIN + r"|(?<![\w&])" + _NUMBER + r"|(?<![A-Za-z0-9_.&-])[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+" + _NUMBER
    + r")(?:" + _LOGIN + "|" + _NUMBER + r")*)"
)

# The sweep: identifiers, then dotted quads less the ranges that can only be prose. The allowance is each
# range's own base address and the RFC 5737 documentation prefixes, never the whole range, so a real
# tailnet or private host stays reportable. The identifier patterns are not here: they live in one file,
# the shipped generic list beside this module unless TRACKER_IDENTIFIER_PATTERNS names another (configure).
PATTERN_FILE = Path(__file__).resolve().parent / "identifier-patterns.txt"
_override = None


def configure(path):
    """Use the pattern file at path (TRACKER_IDENTIFIER_PATTERNS) instead of the shipped one; None restores the shipped one."""
    global _override
    _override = Path(path) if path else None


def pattern_file():
    return _override or PATTERN_FILE
_ADDRESS = re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")
_ALLOWED_ADDRESS = re.compile(
    r"0\.0\.0\.0|255\.255\.255\.255|127\.0\.0\.1|169\.254\.0\.0|100\.64\.0\.0|10\.0\.0\.0|172\.16\.0\.0|192\.168\.0\.0"
    r"|192\.0\.2\.[0-9]{1,3}|198\.51\.100\.[0-9]{1,3}|203\.0\.113\.[0-9]{1,3}"
)


# The subset of regular-expression syntax a pattern line may use. The tracker reads the file with
# Python re and a repository's own pre-commit sweep may read it with rg (Rust regex); these constructs mean the same in
# both: literal characters, an escaped dot, a bracket set of plain characters and ranges, and one
# quantifier per atom. Anything else (a wildcard dot, alternation, anchors, groups, lookaround,
# backreferences, shorthand or POSIX classes, negation, possessive quantifiers) is read differently or
# rejected by one engine, so the file refuses it rather than let the two sweeps drift apart.
_PLAIN = r"[A-Za-z0-9@_:=%~!&,/-]"
_SET_CHAR = r"[A-Za-z0-9@_.]"
_PORTABLE_PATTERN = re.compile(
    r"(?:(?:" + _PLAIN + r"|\\\.|\[(?:" + _SET_CHAR + "-" + _SET_CHAR + "|" + _SET_CHAR + r")+-?\])"
    r"(?:[+*?]|\{[0-9]+(?:,[0-9]*)?\})?)+"
)


class SweepUnavailable(Exception):
    """The identifier patterns cannot be used, so no text can be called clean; callers refuse the write."""


def _identifier_pattern():
    """Compile the team's identifier patterns, failing closed: any problem raises SweepUnavailable.

    A line is a case-insensitive regular expression in the subset both engines read alike (see
    _PORTABLE_PATTERN); an empty line or one starting with # is a comment.
    The message names the file and the line number, never a pattern.
    """
    path = pattern_file()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as unreadable:
        raise SweepUnavailable(f"the identifier pattern file {path.name} cannot be read ({type(unreadable).__name__})") from None
    parts = []
    for number, line in enumerate(lines, 1):
        if not line or line.startswith("#"):
            continue
        try:
            if not _PORTABLE_PATTERN.fullmatch(line):
                raise re.error("outside the portable subset")
            re.compile(line)
        except re.error:
            raise SweepUnavailable(f"line {number} of the identifier pattern file {path.name} is not a valid portable pattern") from None
        parts.append(f"(?:{line})")
    if not parts:
        raise SweepUnavailable(f"the identifier pattern file {path.name} holds no patterns")
    try:
        return re.compile("|".join(parts), re.I)
    except re.error:
        raise SweepUnavailable(f"the identifier patterns in {path.name} do not combine into one pattern") from None


@dataclass(frozen=True)
class Hit:
    """One sweep finding. The matched text stays out of the repr so a log or message never carries it."""

    kind: str
    start: int
    text: str = field(repr=False, compare=False)


def _wrap(backslashes, token):
    """Backtick-wrap a token; an odd run of backslashes before it would escape the opening backtick, so even it out."""
    return backslashes + ("\\" if len(backslashes) % 2 else "") + f"`{token}`"


# An entity spelling of @ or # (&#64;, &commat;, &#35;, &num;) renders as the live character, so its ampersand is
# escaped; &amp; and the &#96; this module writes itself are already inert.
_ENTITY = re.compile(r"&(?!amp;|#96;)(#[0-9]+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);")


def _escape_plain(text):
    text = _ENTITY.sub(r"&amp;\1;", text)
    # A backtick left outside a trusted span is unpaired or ambiguous; as an entity it cannot open a span
    # that swallows or hides the wrapping.
    text = text.replace("`", "&#96;")
    return _RUN.sub(lambda m: _wrap(m.group(1), m.group(2)), text)


def _escape_line(line):
    out, position = [], 0
    for match in _SPAN.finditer(line):
        if len(match.group(1)) % 2:
            continue  # an escaped opening backtick is not a span
        out.append(_escape_plain(line[position:match.start()]))
        out.append(match.group(0))
        position = match.end()
    out.append(_escape_plain(line[position:]))
    return "".join(out)


def _fence_end(lines, start, unclosed):
    """Return the index of the line that closes the fence opened at start, or None when it never closes.

    unclosed maps a fence character to the shortest opener length already known to have no closer, so
    a text of many unclosed openers is scanned once per length rather than once per opener.
    """
    opened = _FENCE_OPEN.match(lines[start].rstrip("\r"))
    if not opened or (opened.group(1)[0] == "`" and "`" in opened.group(2)):
        return None
    char, size = opened.group(1)[0], len(opened.group(1))
    if size >= unclosed.get(char, size + 1):
        return None
    closer = re.compile(r"^ {0,3}" + re.escape(char) + "{" + str(size) + r",}[ \t]*$")
    for index in range(start + 1, len(lines)):
        if closer.match(lines[index].rstrip("\r")):
            return index
    unclosed[char] = size
    return None


def escape(text):
    """Wrap every @mention and issue reference outside trusted code in backticks, so it notifies and links nothing; stray backticks become entities."""
    lines, out, index, unclosed = text.split("\n"), [], 0, {}
    while index < len(lines):
        end = _fence_end(lines, index, unclosed)
        if end is None:
            out.append(_escape_line(lines[index]))
            index += 1
        else:
            out.extend(lines[index:end + 1])
            index = end + 1
    return "\n".join(out)


def sweep(text):
    """Return the PII findings in text, in order of position; empty means clean. Raises SweepUnavailable when the identifier patterns cannot be loaded."""
    hits = [Hit("identifier", m.start(), m.group(0)) for m in _identifier_pattern().finditer(text)]
    hits += [Hit("address", m.start(), m.group(0)) for m in _ADDRESS.finditer(text) if not _ALLOWED_ADDRESS.fullmatch(m.group(0))]
    return sorted(hits, key=lambda hit: hit.start)


def new_hits(text, baseline):
    """Return the findings in text beyond those the baseline already holds, matched as a multiset."""
    known = Counter((hit.kind, hit.text.lower()) for hit in sweep(baseline))
    fresh = []
    for hit in sweep(text):
        key = (hit.kind, hit.text.lower())
        if known[key] > 0:
            known[key] -= 1
        else:
            fresh.append(hit)
    return fresh
