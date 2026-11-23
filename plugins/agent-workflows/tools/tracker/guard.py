#!/usr/bin/env python3.15
"""Refuse GitHub Project writes in a shell command: issues and labels are the tracker now, and nothing writes a Project.

Issue writes (create, label, comment, close) are made with plain gh and are allowed; so is everything on a pull request.

Defence in depth for a PreToolUse hook, not a security boundary: it reads the command the way a shell
splits it and cannot see a write built at run time. Exit 0 allows, exit 2 refuses with the reason on
stderr (the status a Claude Code hook treats as a block).

    guard.py --command 'gh project item-add 1 --url u'
    guard.py < hook-payload.json        # a PreToolUse payload, or the bare command text
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import sys

PROJECT_READS = {"list", "view", "item-list", "field-list"}
GUARDED_PATH = re.compile(r"(?:^|/)(?:projectsV2|projects)(?:/|\?|$)")
PROJECT_MUTATION = re.compile(r"project(?:v2|card|column)|(?:create|update|delete|close|copy|link|unlink)project", re.I)
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)
SKIP_WORDS = {"{", "}", "!", "then", "do", "else", "elif", "if", "while", "until", "time", "builtin", "exec", "command", "nohup", "setsid", "caffeinate"}
# Wrapper commands whose own options (and the value of those listed) come before the real command.
WRAPPERS = {"env": {"-u"}, "xargs": {"-n", "-I", "-P", "-L", "-d", "-E", "-s", "-a"}, "sudo": {"-u", "-g", "-h", "-p", "-C", "-U"},
            "nice": {"-n"}, "timeout": {"-k", "-s"}, "stdbuf": set()}
API_VALUE_FLAGS = {"-X", "--method", "-H", "--header", "-f", "--raw-field", "-F", "--field", "--input", "-q", "--jq", "-t", "--template",
                   "--cache", "--hostname", "-p", "--preview"}
REPO_FLAGS = {"-R", "--repo"}
SEPARATORS = re.compile(r"[;&|()<>\n]+")
HELP = "the Project is closed and nothing writes it; track work with issues and labels (agent-workflows tools/tracker/README.md)"


def _line_runs_a_shell(line):
    """True when any command on the line is a shell, so a heredoc body it reads is executed rather than data."""
    for part in re.split(r"[;&|(]", line):
        try:
            program, _ = _program(_tokens(part))
            runs = program in SHELLS or program == "eval"
        except ValueError:
            # A quote the shell would read differently (a comment after the marker, say): look for a shell word instead.
            runs = bool(re.search(r"(?:^|\s)(?:bash|sh|zsh|dash|ksh|eval)(?:\s|$)", part))
        if runs:
            return True
    return False


_HEREDOC = re.compile(r"<<-?[ \t]*(?:'([^']+)'|\"([^\"]+)\"|(\\?)([A-Za-z_][A-Za-z0-9_]*))")


def _heredocs(line, mode):
    """Return the unquoted heredoc operators on a line as (delimiter, quoted) and the quote mode at its end.

    mode is None, "'", '"' or "$'" (bash ANSI-C quoting, where a backslash escapes the next character).
    """
    found, index = [], 0
    while index < len(line):
        char = line[index]
        if mode == "'":
            mode = None if char == "'" else mode
        elif mode == "$'":
            if char == "\\":
                index += 1
            elif char == "'":
                mode = None
        elif char == "\\":
            index += 1
        elif mode == '"':
            mode = None if char == '"' else mode
        elif char == "#" and (index == 0 or line[index - 1] in " \t;&|("):
            break  # an unquoted comment: nothing after it is a heredoc operator
        elif line.startswith("$'", index):
            mode = "$'"
            index += 1
        elif char == "'":
            mode = "'"
        elif char == '"':
            mode = '"'
        elif line.startswith("<<<", index):
            index += 2
        elif line.startswith("<<", index):
            match = _HEREDOC.match(line, index)
            if match:
                found.append((match.group(1) or match.group(2) or match.group(4), bool(match.group(1) or match.group(2) or match.group(3))))
                index = match.end() - 1
        index += 1
    return found, mode


def _heredoc_substitutions(body):
    """Return the inside of each $(...) and backtick expansion in an unquoted heredoc body (a backslash escapes one)."""
    found, index = [], 0
    while index < len(body):
        char = body[index]
        if char == "\\":
            index += 2
        elif body.startswith("$(", index):
            depth, start = 1, index + 2
            end = start
            while end < len(body) and depth:
                depth += {"(": 1, ")": -1}.get(body[end], 0)
                end += 1
            found.append(body[start:end - 1])
            index = start
        elif char == "`":
            end = index + 1
            while end < len(body) and body[end] != "`":
                end += 2 if body[end] == "\\" else 1
            if end >= len(body):
                break
            found.append(body[index + 1:end])
            index = end + 1
        else:
            index += 1
    return found


def _strip_heredocs(text):
    """Drop heredoc bodies that are data (a commit message); keep one a shell will run, or one never closed.

    Only an operator outside quotes opens a heredoc: `echo '<<EOF'` does not, and the lines after it stay commands.
    """
    out, lines, index, state = [], text.split("\n"), 0, None
    while index < len(lines):
        line = lines[index]
        out.append(line)
        index += 1
        markers, state = _heredocs(line, state)
        for delimiter, quoted in markers:
            end = index
            while end < len(lines) and lines[end].strip() != delimiter:
                end += 1
            if end >= len(lines):
                continue
            if _line_runs_a_shell(line):
                out.extend(lines[index:end])
            elif not quoted:
                # An unquoted delimiter still lets the shell expand $(...) and backticks in the body; quotes there
                # do not suppress them, only a backslash does.
                out.extend(f"$({inner})" for inner in _heredoc_substitutions("\n".join(lines[index:end])))
            index = end + 1
    return "\n".join(out)


def _substitutions(text):
    """Return the inside of every $(...) and backtick substitution the shell would run (none inside single quotes)."""
    found, index, mode = [], 0, None
    while index < len(text):
        char = text[index]
        if mode == "'":
            mode = None if char == "'" else mode
            index += 1
        elif mode == "$'":
            mode = None if char == "'" else mode
            index += 2 if char == "\\" else 1
        elif char == "\\":
            index += 2
        elif char == "'" and mode is None:
            mode = "'"
            index += 1
        elif text.startswith("$'", index) and mode is None:
            mode = "$'"
            index += 2
        elif char == '"':
            mode = None if mode == '"' else '"'
            index += 1
        elif text.startswith("$(", index) or (mode is None and text.startswith("<(", index)):
            depth, start = 1, index + 2
            end = start
            while end < len(text) and depth:
                depth += {"(": 1, ")": -1}.get(text[end], 0)
                end += 1
            found.append(text[start:end - 1])
            index = start
        elif char == "`":
            end = index + 1
            while end < len(text) and text[end] != "`":
                end += 2 if text[end] == "\\" else 1
            if end >= len(text):
                break
            found.append(text[index + 1:end])
            index = end + 1
        else:
            index += 1
    return found


def _tokens(text):
    lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>\n")
    lex.whitespace, lex.whitespace_split, lex.commenters = " \t\r", True, ""
    return list(lex)


REDIRECT = re.compile(r"[<>&]*[<>][<>&]*")


def _segments(tokens):
    """Split tokens into simple commands, dropping each redirection operator, its operand and a file-descriptor digit."""
    segment, skip = [], False
    for position, token in enumerate(tokens):
        if skip:
            skip = False
        elif REDIRECT.fullmatch(token):
            skip = True
        elif token.isdigit() and position + 1 < len(tokens) and REDIRECT.fullmatch(tokens[position + 1]):
            continue
        elif SEPARATORS.fullmatch(token):
            if segment:
                yield segment
            segment = []
        else:
            segment.append(token)
    if segment:
        yield segment


def _program(tokens):
    """Skip assignments and wrapper commands; return the real program word and its arguments."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        name = os.path.basename(token)
        if ASSIGNMENT.fullmatch(token) or token in SKIP_WORDS or token == "--":
            # A bare `--` ends a wrapper's options (`command -- gh ...`); the program is the word after it.
            index += 1
        elif name in WRAPPERS:
            index += 1
            takes = WRAPPERS[name]
            if name == "timeout":
                while index < len(tokens) and tokens[index] != "--" and tokens[index].startswith("-"):
                    index += 2 if tokens[index] in takes else 1
                if index < len(tokens) and tokens[index] == "--":
                    index += 1
                index += 1
                continue
            while index < len(tokens) and tokens[index] != "--" and (tokens[index].startswith("-") or ASSIGNMENT.fullmatch(tokens[index])):
                index += 2 if tokens[index] in takes else 1
        else:
            return name, tokens[index + 1:]
    return None, []


def _graphql_mutates(fields, has_input, inputs):
    for key, value in fields:
        if key == "query":
            if value.startswith("@"):
                try:
                    value = Path(value[1:]).read_text(encoding="utf-8")
                except OSError:
                    return True
            if re.search(r"\bmutation\b", value, re.I) and PROJECT_MUTATION.search(value):
                return True
    for path in inputs:
        if path == "-":
            return True  # a query on stdin cannot be read here
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            return True
        if re.search(r"\bmutation\b", text, re.I) and PROJECT_MUTATION.search(text):
            return True
    return False


def _gh_api(args):
    method, fields, inputs, path, has_fields = None, [], [], None, False
    index = 0
    while index < len(args):
        arg = args[index]
        flag, equals, inline = arg.partition("=") if arg.startswith("--") else (arg, "", "")
        short = arg[:2] if arg.startswith("-") and not arg.startswith("--") else None
        if flag in API_VALUE_FLAGS or (short in API_VALUE_FLAGS and len(arg) > 2):
            value = inline if equals else (arg[2:] if short in API_VALUE_FLAGS and len(arg) > 2 else (args[index + 1] if index + 1 < len(args) else ""))
            name = flag if flag in API_VALUE_FLAGS else short
            if name in ("-X", "--method"):
                method = value.upper()
            elif name in ("-f", "--raw-field", "-F", "--field"):
                has_fields = True
                key, _, content = value.partition("=")
                fields.append((key, content))
            elif name == "--input":
                has_fields = True
                inputs.append(value)
            index += 1 if (equals or (short and len(arg) > 2)) else 2
        elif arg.startswith("-"):
            index += 1
        else:
            path = path or arg
            index += 1
    if path and re.search(r"(?:^|/)graphql/?$", path):
        return _graphql_mutates(fields, bool(inputs), inputs)
    effective = method or ("POST" if has_fields else "GET")
    return effective != "GET" and bool(path and GUARDED_PATH.search(path))


def _gh(args):
    words, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg in REPO_FLAGS:
            skip = True
        elif not arg.startswith("-"):
            words.append(arg)
    if not words:
        return None
    group, sub = words[0], words[1] if len(words) > 1 else ""
    if group == "project" and sub and sub not in PROJECT_READS:
        return f"gh project {sub}".strip()
    if group == "api" and _gh_api(args[args.index("api") + 1:] if "api" in args else []):
        return "gh api (a write to a Project path, or a GraphQL mutation of a Project)"
    return None


def _scan(text, found):
    for inner in _substitutions(text):
        _scan(inner, found)
    for segment in _segments(_tokens(text)):
        program, args = _program(segment)
        if program == "gh":
            hit = _gh(args)
            if hit:
                found.append(hit)
        elif program in SHELLS or program == "eval":
            if program == "eval":
                _scan(" ".join(args), found)
                continue
            for position, arg in enumerate(args):
                attached = re.fullmatch(r"-[a-zA-Z]*?c(.*\s.*)", arg, re.S)
                if attached:
                    _scan(attached.group(1), found)
                    break
                if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", arg) and position + 1 < len(args):
                    _scan(args[position + 1], found)
                    break


_FALLBACK = re.compile(
    r"\bgh\s+(?:project\s+(?!list\b|view\b|item-list\b|field-list\b)\S+"
    r"|api\b.*(?:-X|--method)[ =]?(?:POST|PATCH|PUT|DELETE)\b.*projects"
    r"|api\b.*\bmutation\b.*projectv2)", re.I)


def check(command):
    """Return one message per Project write in the command; empty means it may run."""
    # The shell deletes a backslash-newline outright (it does not become a space), so `g\<NL>h` is `gh`.
    text = _strip_heredocs(re.sub(r"\\\r?\n", "", command or ""))
    found = []
    try:
        _scan(text, found)
    except ValueError:
        # Most often a quote inside a # comment, which the shell ignores: retry without comments.
        found.clear()
        try:
            _scan(re.sub(r"(?m)(^|\s)#[^\n]*", r"\1", text), found)
        except ValueError:
            # The shell would not parse it either; scan the raw text rather than wave it through.
            found += [m.group(0).split("\n")[0][:40] for m in _FALLBACK.finditer(text)]
    return [f"GitHub Project write refused ({hit}): {HELP}" for hit in dict.fromkeys(found)]


def _command_from(stdin_text):
    try:
        payload = json.loads(stdin_text)
    except ValueError:
        return stdin_text
    if isinstance(payload, dict) and "tool_input" in payload:
        if payload.get("tool_name", "Bash") != "Bash":
            return ""
        return str((payload.get("tool_input") or {}).get("command", ""))
    return stdin_text


def main(argv=None, stdin=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--command", help="the shell command to check; default reads stdin")
    args = parser.parse_args(argv)
    command = args.command if args.command is not None else _command_from((stdin or sys.stdin).read())
    problems = check(command)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 2 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
