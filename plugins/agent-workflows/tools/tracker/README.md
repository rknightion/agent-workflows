# Tracker

A thin shim over `gh` for GitHub issues and labels. `tracker.py` is stdlib Python (3.12 or newer); every call it
makes is `gh api` as the ambient `gh` login. There is no GitHub App, no token minting and no Project: nothing here
reads or writes Project fields.

The model is labels on issues:

- A repository's queue is its open issues labelled `ready`.
- Other working labels: `blocked`, `needs-owner` (a decision only the owner can make), and one priority, `p1`, `p2`
  or `p3`. The tool creates a working label the repository does not have yet before it adds it.
- A close carries a comment with the evidence: the merged pull request or commit, and any acceptance criterion
  left unmet. A pull request body still says `Closes #N` (GitHub closes the issue on merge) or `Task: none, <reason>`;
  the acceptance-evidence check in CI is separate from this tool and unchanged.

Anyone may make the same writes with plain `gh issue` commands; the tool only keeps the shapes consistent. Every
writing verb reads the issue first and refuses (exit 3) a number that is a pull request, before any write.
`tracker --help` and `tracker <command> --help` hold the flags. Output is one JSON object on stdout.

## Commands

- **queue**: the open issues labelled `ready` (pull requests skipped), every page. `--label X` (repeatable) also
  requires that label; `--status NAME` lists another status label instead (`Blocked`, `Needs owner` or `Needs input`
  map to `blocked` and `needs-owner`; any other name is read as a label, lower-cased with spaces as hyphens);
  `--all-repos` searches every repository of the owner (`org:` or `user:` issue search); `--limit N` returns at most
  N rows and says `truncated` when more matched. Rows carry `repo`, `number`, `title`, `status`, `priority` (the
  `p` label) and `labels`; `lane` is always null.
- **view**: one issue, field-selected; `--comments N` adds the last N comments not written by a bot; `--edges` adds
  blockers and sub-issues.
- **create**: one issue with its title, body, labels, type and milestone in one call, then the `--parent` and
  `--blocked-by` links. `--status Ready` adds the `ready` label (likewise Blocked, Needs owner); `--priority`
  adds `p1`, `p2` or `p3` (urgent and high are p1, medium p2, low p3). `--field Status=...` and
  `--field Priority=...` mean the same; any other `--field` is ignored with a note. A link that fails after the
  issue exists still prints the new issue's number and the step that failed (exit 4).
- **priority `<n> <value>`**: sets the one priority label and removes the other two.
- **claim `<n> --lane L`**: takes the issue out of the queue by removing `ready`. The lane is not recorded and
  `--force` has nothing left to override.
- **status `<n> [ready|blocked|needs-owner] [--text T | --file F]`**: a state sets that label and removes the other
  two status labels; text (or `--file -` for stdin) is posted as a comment. Text that is only a state word is read
  as the state.
- **handoff `<n> --scope --verified --blockers --next [--status S]`**: sets the status label (default `needs-owner`)
  and posts one comment with the four parts. A status name with no label (In progress, say) only removes `ready`.
- **tick `<n> ACn`**: flips one `- [ ] ACn` box in the issue's "Acceptance criteria" section (to the next heading of
  the same or a higher level; the whole body when there is no such heading), reads the body again just before the
  write and refuses (exit 3) if it changed, writes the body and fails unless the answer reads back ticked. An already-ticked box writes nothing.
- **close `<n> [--pr P] [--why TEXT | --evidence-file F] [--reason completed|not-planned|duplicate]`**: posts the
  evidence comment (the reason, the pull request when given, the evidence text and any acceptance criterion left
  unticked) and then closes the issue with that state reason. `--pr P` alone is enough: the comment says the issue
  was closed by that pull request. With neither `--pr` nor evidence text it is refused (exit 2) before any call.
  It does not require or read a pull request. An issue already closed is left alone.
- **legacy `<ID>`**: the issue a legacy Backlog id became, from the committed `.github/tracker/id-map.json`.
- **priority-check**: retired. It prints a one-line note on stderr, `{"retired": "priority-check"}` on stdout, and
  exits 0.

`--project` and the old settings (`TRACKER_PROJECT`, `TRACKER_PROJECT_OWNER`, `TRACKER_ISSUE_IDENTITY`,
`TRACKER_PROJECT_IDENTITY`, `TRACKER_APP_*`) are accepted and ignored, so existing recipes keep working.

## Settings

| Setting | Meaning | Default |
|---|---|---|
| `TRACKER_REPO` | `owner/name` the issues live in (`--repo` overrides it) | the `github.com` `origin` remote of the current checkout |
| `TRACKER_IDENTIFIER_PATTERNS` | path of the identifier pattern file (replaces the shipped one) | `identifier-patterns.txt` beside the tool |

## Text sweep

Titles, bodies, comments and close evidence are swept before anything is sent: the lines of
`identifier-patterns.txt` (secret and token shapes, personal-data shapes) and dotted-quad addresses outside the
documentation ranges (`textsafe.py`). A hit refuses the write (exit 3) and names only the kind of finding. A missing
or unusable pattern file refuses every write. Text is otherwise sent as written: mentions and `#N` references stay
live, as they would with plain `gh`.

## with-token

`with-token` is a retired name kept so recipes that run `tracker-with-token` keep working. It reads no credentials
and mints nothing: it runs the tracker with the same arguments and passes its exit status through.

`appmint.py` (GitHub App token minting) stays in this directory only because the Backlog importer
(`tools/tracker-import`) imports it; the tracker does not.

## Guard

`guard.py` refuses a shell command that writes a GitHub Project: any `gh project` subcommand except the reads
(list, view, item-list, field-list), `gh api` with a write method or fields on a `projects` or `projectsV2` path,
and a `gh api graphql` mutation of a Project (or one whose query it cannot read). Issue and pull request writes
with plain `gh` are allowed. It reads shell structure (wrappers, `bash -c`, `$(...)`, heredocs fed to a shell),
not run-time values; it is defence in depth, not a boundary. Wire it as a Claude Code `PreToolUse` hook on `Bash`:

```json
{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 /path/to/tracker/guard.py"}]}]}}
```

## Exit codes

0 done (a retired verb included), 2 usage (including a close with neither --pr nor evidence), 3 refused (sweep hit, a pull request
where an issue was meant, a body edited during a tick, a criterion missing or repeated, a legacy id the map cannot answer), 4 `gh` failed or answered in a shape the tool does not know.

## Tests

`python3 -m unittest discover -s plugins/agent-workflows/tests -p 'test_tracker*.py'` runs the tracker's tests against
`fake_gh.py`, a stand-in `gh` on PATH that answers `gh api` from recorded routes and logs every call. No test
reaches GitHub.
