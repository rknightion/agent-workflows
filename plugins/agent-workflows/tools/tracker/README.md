# Tracker

The loop root's one way to write GitHub issues and the Project. `tracker.py` is stdlib Python (3.12 or
newer) that shells out to `gh`; `with-token` runs it with a freshly minted GitHub App token. **Only the
loop root runs it.** A lane never writes to GitHub: it returns the `status` and `tick` requests it wants
in its lane return and the root applies them. The App token and the Project login are never in a lane's
environment.

`tracker --help` and `tracker <command> --help` hold the flags. Output is one JSON object on stdout.

## Identities

| Identity | Default for | Credential |
|---|---|---|
| `app` | every issue write: create, body edits, comments, sub-issue and blocked-by links, close | the GitHub App installation token in `TRACKER_APP_TOKEN` |
| `user` | every Project write, and all reads | the ambient `gh` login (classic token with the `project` scope) |

`TRACKER_ISSUE_IDENTITY` and `TRACKER_PROJECT_IDENTITY` (each `app` or `user`) move either kind of write
to the other identity. `TRACKER_PROJECT_IDENTITY=app` is refused (exit 2, before any write) when the
Project is owned by a user account: GitHub Apps and fine-grained tokens cannot write a user-owned Project.
Managed comments belong to the login issue writes are made as: `TRACKER_APP_LOGIN` (the App's bot login,
such as `my-tracker[bot]`; there is no default) for `app`, the ambient login (read from `GET /user`) for
`user`.

A write that needs the App with no `TRACKER_APP_TOKEN` stops before any request. Reads work without it.
The token reaches `gh` only through that one process's `GH_TOKEN`, never an argument, and never the audit
log or an error message.

### Running without a GitHub App

The App is optional. With `TRACKER_ISSUE_IDENTITY=user` (and `TRACKER_PROJECT_IDENTITY` left at its default
`user`) every write goes out as the ambient `gh` login, so no App, no `with-token`, no credentials directory
and no `TRACKER_APP_TOKEN` is needed:

```bash
TRACKER_ISSUE_IDENTITY=user tracker status 123 --file note.md
```

The tool asks for the App token only when a command writes as the `app` identity, so this setup never reaches
that check. Managed comments are then authored by, and found by, the ambient login (read from `GET /user`).

One limit follows from that: the tool cannot tell its own comments from yours. An ordinary comment by the
same login on an issue counts as a stray comment, and `status` and `close --reason` then refuse on that
issue (see `status` below). Keep your own remarks out of issues the tracker manages, or use an App.

### with-token

```bash
TRACKER_APP_CRED=my-tracker with-token status 123 --file note.md
```

`with-token <tracker command and flags>` reads the App's credentials where they lie in the credentials
tree, `${CREDENTIALS_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/tracker/credentials}/github/$TRACKER_APP_CRED/`: `app.env`
(`GITHUB_APP_ID`, and `GITHUB_APP_SLUG`) and `private-key.pem`. It signs a JWT with `openssl` reading
the key file in place (the key is never copied), finds the App's installation on the tracker's
repository (`GET /repos/<owner>/<name>/installation`), mints an installation token scoped to that one
repository (`POST /app/installations/<id>/access_tokens` with `{"repositories": ["<name>"]}`; the
permissions are the installation's own and are not narrowed) and runs the tracker in the same process
with the token in the tracker's own settings only: it is never exported, printed, logged or passed as an
argument. There is no mode that prints a token, since a printed token can land in an agent transcript. When `TRACKER_APP_LOGIN` is unset it becomes `<GITHUB_APP_SLUG>[bot]`
(from `app.env`, else `GET /app`). A token lasts about an hour; `with-token` mints one per command.
Its exit status is the tracker's; a minting failure exits 2 (a missing or bad setting or credential) or
4 (GitHub refused or did not answer). `my-tracker` above is an example credential directory name, not a
default. Set `CREDENTIALS_DIR` to keep the credentials tree somewhere else.

Never paste the key or a token into an issue, a commit or a log.

### appmint

The minting lives in `appmint.py` beside the tracker, stdlib only and independent of `tracker.py`, so
other tools import it rather than copy it. Its API is fixed:

- `MintError(code, message)`: `.code` is 2 (a setting or credential) or 4 (GitHub); the message never
  carries the JWT, the token or the key.
- `mint(env, repo, now=None) -> (token, login)`: the installation token scoped to `repo` alone, and
  `<slug>[bot]` or `None` for the login. The slug is always resolved (`GITHUB_APP_SLUG` in `app.env`, else
  `GET /app`), whatever `TRACKER_APP_LOGIN` says, so a caller comparing authors gets the App's real login;
  `with-token` still lets an explicit `TRACKER_APP_LOGIN` win for the tracker it runs.
- `make_jwt(app_id, key_path, now=None)`, `read_env_file(path)` and `credential_dir(env)`, the pieces
  `mint` is built from.

## Settings

All settings are environment variables; nothing defaults to a particular repository or account, except the
evidence workflow named below, which is the author's public reusable workflow and which a reader overrides with
`TRACKER_EVIDENCE_WORKFLOW`.

| Setting | Meaning | Default |
|---|---|---|
| `TRACKER_REPO` | `owner/name` the issues live in (`--repo` overrides it) | the `github.com` `origin` remote of the current checkout; refused when neither gives one |
| `TRACKER_PROJECT` | the Project number (`--project` overrides it); needed by every command that touches the Project | none |
| `TRACKER_PROJECT_OWNER` | the login that owns the Project, an organisation or a user | the repository owner |
| `TRACKER_APP_LOGIN` | the App's bot login, the author a managed comment must have | none (`with-token` fills it) |
| `TRACKER_ISSUE_IDENTITY` | `app` or `user` | `app` |
| `TRACKER_PROJECT_IDENTITY` | `app` or `user` | `user` |
| `TRACKER_EVIDENCE_CHECK` | the check run that proves a pull request's acceptance evidence | `Acceptance evidence` |
| `TRACKER_EVIDENCE_WORKFLOW` | the reusable workflow that check must come from, as the start of a referenced workflow path: `owner/repo/<path to the file>@`, ending at the `@` | `rknightion/.github/.github/workflows/ac-evidence.yml@` (a default; set this to your own reusable workflow) |
| `TRACKER_IDENTIFIER_PATTERNS` | path of the identifier pattern file (replaces the shipped one) | `identifier-patterns.txt` beside the tool |
| `TRACKER_STATE_DIR` | rate-limit state, locks and the default audit log | `$XDG_STATE_HOME/tracker`, else `~/.local/state/tracker` |
| `TRACKER_AUDIT_LOG` | the audit log | `audit.jsonl` in the state directory |
| `TRACKER_MAX_WAIT` | the longest wait in seconds the tool will sleep through | 600 |
| `TRACKER_APP_TOKEN` | the installation token (`with-token` sets it) | none |
| `TRACKER_APP_CRED` | `with-token` only: the credentials directory under `github/` | none |

Whether an account is an organisation or a user is read once per run (`GET /users/<login>`) when a
command needs it. A Project is then reached under `/orgs/<login>/projectsV2/<n>` or the same path beneath `/users/<login>`.

## What each command guarantees

- **view** reads one issue and returns selected fields. It never fetches comments unless
  `--comments N`, which returns the last N human-authored ones (bot comments and the tool's own
  comments are skipped). `--edges` adds blockers and sub-issues.
- **queue** lists the open issues of one repository at a Project status (default Ready): one field lookup, then
  the items request with a `repo:<owner>/<name>` term (the Project item filter's repository qualifier) for the
  repository the tool resolved (`--repo`, `TRACKER_REPO`, the origin remote). `--all-repos` drops that term for the
  Project-wide listing, which on a Project shared by several repositories is every one of them. It follows the
  items listing's `Link` header cursor (Project items page by cursor, `after`, not by page number) until the
  items run out, so by default it returns every row. `--limit N` (any N of 1 or more, past 100 too) stops after N
  rows; the result is `{"items": [...], "count": <rows returned>, "truncated": <bool>}`, and `truncated` is true only
  when the limit stopped the listing with items still to come (a limit that exactly fits what exists is false: the
  tool asks for one row over the limit to tell the two apart). A page that is not a list, a repeated cursor or a
  listing past 500 pages is an error, never an end. An organisation Project's item list can lag a write by a few
  seconds, so an issue created or moved a moment ago may not be listed yet: read again before concluding the queue
  is empty.
- **create** posts the issue with its type, labels and milestone in one call, then links a parent and
  blockers, adds it to the Project and sets fields. If a later step fails the result still names the
  new issue and the step that failed, so a retry does not duplicate it. A create that gets no answer at
  all (a timeout) may still have created the issue: look for it before retrying.
  `--field NAME=VALUE` sets a **Project** field (the Project's own fields, written through the Project item)
  and cannot reach an organisation's issue fields. `--priority VALUE` sets the organisation-level
  **Priority** issue field instead: the tool reads `/orgs/<owner>/issue-fields` first, matches the value
  against that field's options without regard to case, and sends `issue_field_values` (the field's id and the
  option as spelled) in the issue creation itself. It needs no Project. A value the field does not offer, or
  an organisation with no single-select Priority field, is refused (exit 2) before any write; the message
  lists the allowed values. **Organisation-only features** (`--type`, which sets an issue type, and
  `--priority`) on a repository owned by a user account are refused (exit 2) before any write or render:
  GitHub accepts a type on a user repository's issue and silently drops it. A Project field named Priority,
  if the Project has one, is set with `--field` and is what `queue` reports as `priority`.
  **On a user-owned repository priority is a label and a Project field kept equal.** There is no organisation
  Priority issue field there, so `create` keeps the `priority:<low|medium|high>` label and the Project's single-select
  `Priority` field in step: `--label priority:high` also sets the field to the option of that name (matched without
  case; with no Project configured only the label goes out), and `--field Priority=High` also adds the label
  `priority:high`. The same value given both ways is fine; two different values or two priority labels is a usage
  error (exit 2) before any write. **Urgent is field-only**: `--field Priority=Urgent` sets the field and adds no
  label, while `--label priority:urgent` (there is no such label) or Urgent beside a priority label is a usage error.
  Any other option the Project's Priority field carries passes through `--field` untouched and is ignored by the
  sync. An organisation-owned repository is unchanged: a `priority:` label is only a label.
- **priority `<number> <low|medium|high|urgent>`** changes an existing issue's priority on a user-owned repository,
  writing both halves: the issue identity adds the wanted `priority:*` label, then removes every other one (so the
  issue is never without a label), then the Project identity sets the item's Priority field (adding the issue to the
  Project first if it is not there, as `claim` does). `urgent` sets the field and removes every priority label. A
  half already right is not rewritten, and a command that has nothing to change says `"changed": false`. It reads
  the Project and the option before any write. The result's `label` and `field` say what was actually written
  (`unchanged`, `written`, `partly written`, `not written`) and `changed` is true once any write went out; a failure
  after the first write still prints that result naming the step (`label` or `project`) and exits 4. On an
  **organisation-owned** repository it is refused (exit 2) before any write: the organisation Priority issue field is
  set at creation with `create --priority`, and the tool has no command that sets it on an existing issue.
- **priority-check** is read-only. On a user-owned repository it lists the open issues of this repository **that are
  on the Project** (every page, as `queue` reads them; an issue never added to the Project is not seen) whose
  priority label and Project Priority field disagree (`mismatch`, which includes two priority labels, and Urgent
  beside any priority label) or of which only one is set (`field missing`, `label missing`). An issue with neither is
  not reported, an Urgent field with no label is in sync, and any other field option with no label is ignored. The
  result is `{"items": [...], "count": N, "checked": <issues read>}`, printed either way, and the exit status is 5
  when `count` is not 0 (not 1, which a crashed Python exits with). It is refused (exit 2) on an organisation-owned
  repository.
- **claim** sets Project Status to In progress and a `Lane` text field in one write. It refuses an
  issue another lane already holds unless `--force`, and does nothing when the same lane repeats it;
  the read and the write sit inside the per-issue lock, and a Project field value in a shape the tool
  does not know stops the claim rather than reading as empty. Field and option ids are resolved by name once
  per run.
- Adding an issue to the Project (create, claim, handoff, close) keeps an existing item: GitHub answers a
  second add with 422 "Content already exists in this project" (top-level or as an `UNPROCESSABLE` entry
  in `errors`), and a parent's sub-issues join the Project with it, so the tool then reads the issue's
  Project items and uses this Project's one. An issue GitHub says is there but lists no item for fails.
- **status** keeps exactly one managed comment per issue, found by a hidden marker **and** the issue
  identity's login as its author, so a pasted marker from anyone else is ignored. It updates in place and
  writes nothing when the text is unchanged. It refuses when two managed comments exist, or when that login
  already has some other non-handoff comment there. Comments carrying the overflow marker are exempt.
- **handoff** sets a Project status (default Needs input) and posts the one handoff comment (scope,
  verified, blockers, next). A second handoff is refused until a human has replied.
- **tick** flips one `- [ ] ACn` box, found only in the issue's "Acceptance criteria" section (to the next
  heading of the same or a higher level; the whole body when it has no such heading), as the evidence checker
  reads it. It takes a per-issue lock, reads the body inside it, edits one line, writes, reads again and
  fails unless the body reads back ticked. A body that already carries a sweep hit can still be ticked: only
  hits the edit adds count.
- **close** needs a PR of this repository, merged into its default branch, that closes the issue (any closing
  keyword for it, in the grammar of the acceptance evidence checker `ac-evidence-check.mjs`, whose answers the
  tests hold) and whose evidence check runs concluded success, read through REST. A PR that changed any file
  under `.github/workflows/` or `.github/actions/` (by its name or, for a rename, its previous name) is refused
  (exit 3) before any write: a PR that changes CI cannot judge itself, so hand the issue to the owner with
  `handoff`. Every page of `GET /repos/<repo>/pulls/<n>/files` is read, and a list that cannot be read whole
  refuses too: an error, a page that is not a list of named files, a count that disagrees with the PR's
  `changed_files`, or a list that reaches GitHub's cap of 3000 files. A run is a candidate when
  its name is `TRACKER_EVIDENCE_CHECK` or ends with ` / ` and that name (a job called through a reusable
  workflow is named `<caller job> / <called job>`) and it was made by GitHub Actions (`app.slug` is
  `github-actions`); runs from any other app are ignored. A pull request can add its own workflow or a stub
  job with that name, so a candidate also has to be **bound** to the real evidence workflow: its workflow run
  (`GET /repos/<repo>/actions/runs?check_suite_id=<its check suite>`, exactly one) must list a
  `referenced_workflows` entry whose `path` starts with `TRACKER_EVIDENCE_WORKFLOW` and whose `sha` is a full
  40 character commit on the default branch of that workflow's repository (`GET
  /repos/<workflow repo>/compare/<sha>...<default branch>` answers `ahead` or `identical`; a commit the
  repository does not know counts as off it). Check runs belong to a commit, not a pull request, so a second
  PR at the same head would otherwise lend this one its success. The run must therefore also be bound to
  **this PR**: it must be one of its workflow run's jobs (a job's `check_run_url` in every page of
  `GET /repos/<repo>/actions/runs/<run>/jobs?filter=all`; a check run made through the API has no job), and
  it must carry exactly one annotation titled `Acceptance evidence binding` (every page of
  `GET /repos/<repo>/check-runs/<id>/annotations`), a `notice` whose message is exactly
  `pr=<the --pr number> head=<the head the PR merged at>`. The evidence job writes that annotation from
  `github.event.pull_request.number` and `.head.sha`. A missing, second, or differing annotation, or a jobs
  or annotations answer that is an error or not a list, refuses. Workflow bindings are read once per check
  suite and commit. Candidates are grouped by name, and the newest run of **every** name must have
  concluded success and be bound in all these ways; any that is not refuses the close (exit 3) and says
  why, and so does having no candidate at all. The check runs of the head are read up to 10 pages of 100; a
  listing that fills them may be cut short and refuses. Each of those runs must be for the head the PR merged at and must
  have started after the PR description was last edited (`lastEditedAt`, read through GraphQL): the job reads
  the description live but an edit does not re-run it, so an old success is not proof for a closing keyword
  added later. The issue's own criteria are not bound, since ticks edit them after the check. The alternative
  is `--reason not-planned|duplicate --why <text>`; the reason is written into the status comment before the
  issue closes, so a failed close leaves the reason recorded and a retry updates it in place. The text the status
  comment held before is kept beneath the reason, under `Status before closing:`, so a close never discards
  acceptance evidence; a retry replaces its own line and does not stack.
  `--reason completed --why <text>` is for an imported issue whose code landed before the cutover: the body
  must start with the import tool's `Legacy ID: <ID>` line, and `.github/tracker/id-map.json` as committed at the
  checkout's HEAD (`git show HEAD:.github/tracker/id-map.json`, checked as `legacy` checks it; an uncommitted
  or edited working copy does not count, and a map absent at HEAD refuses) must map that id to this issue
  number of this repository, so a `Legacy ID` line typed into an issue made with `create` is not enough. Anything else is refused (exit 3);
  a native issue closes through a merged PR. It is never used with `--pr`, and `--why` must carry an `https://github.com/<repo>/` link to an issue comment, pull
  request, commit or run as the evidence, recorded in the status comment. With `--pr` the proof is checked
  every time, an already closed issue included, and a closing keyword inside an HTML comment does not count.
  After the issue closes it also sets Project Status Done (skipped, and said so, when no Project is
  configured); the write is idempotent, so a retry heals a failed one, and an already closed issue only gets
  that step.

  Threat model: the binding keeps a careless lane, a mistaken root and an outside contributor from closing
  an issue on proof that is not this pull request's own passing evidence. It does not hold against anyone
  who can push to the repository with workflow-file write: a `pull_request` run takes workflow files from
  the merge of the PR head into its base, so a pushed branch used as the base of a second pull request at
  the same head can run a stub job that forges both the check name and the binding annotation, and a
  local commit can forge the id map that `close --reason completed` reads. Those actors (the owner and a
  loop root holding the App token) could close the issue through the API directly, so the tool does not
  try to stop them; the protection against them is review and the rule that only the root or the
  interactive main thread writes. The binding also rests on the evidence workflow being correct at the
  pinned commit and writing the annotation as specified. Annotations are only added, never replaced, so a
  forged second binding annotation makes two and refuses.

- **legacy `<ID>`** prints the issue a legacy Backlog id (such as `ABC-0560` or `ABC-0560.01`, matched
  without case) became, from the checkout's working-tree `.github/tracker/id-map.json` (unlike
  `close --reason completed`, which reads it at HEAD) as a separate import tool (not part of this distribution)
  writes it: `{"schema": 1, "repo": "<owner>/<name>", "issues": {"<ID>": {"number":
  N, ...}}, ...}`. It makes no GitHub call. A map that is missing, unreadable, of another schema or for
  another repository, an id it does not list, or an entry with no issue number exits 3, so a lookup never
  quietly answers "no issue".

## Text safety

Everything the tool writes that carries lane text goes through four steps (status, handoff, a close reason,
an issue body and title, free-text Project fields).

1. Raw text is swept: identifiers, then dotted-quad addresses outside the documentation and base addresses.
   A hit refuses that write and names only the kind of finding, never the matched text. No usage message
   repeats what the caller typed (a bad command line gets one fixed message), and the refusal reaches the
   audit log. The identifier patterns are the lines of `identifier-patterns.txt` beside the tool, a generic
   list of secret and token shapes and personal-data shapes (consumer mailboxes, tailnet names) suited to
   public issues, or of the file `TRACKER_IDENTIFIER_PATTERNS` names instead (a replacement, so copy the
   shipped lines into it). A missing, unreadable or pattern-free file, or a line outside the syntax Python
   `re` and `rg` read alike (the file's header lists it), refuses every write (exit 3), so a broken list can
   never read as clean. Text carrying a tracker marker, or over 200,000 characters (checked before any
   escaping), is refused.
2. It is escaped as Markdown source: an `@handle`, a `#N`, a repository-qualified `owner/repo#N` or a `GH-N`
   outside trusted code is wrapped in backticks, tokens written back to back share one span, every
   HTML character entity has its ampersand escaped (`&#64;` cannot render as a mention; `&lt;` and `&copy;`
   show as typed text), and a backtick outside a closed fence or a one-line single-backtick span becomes
   `&#96;`. A full issue URL is left as written.
3. **GitHub's own renderer has the last word.** Escaping works on source; GitHub notifies and links on the
   rendered result, which can differ (a fence inside a raw HTML block is not code). The final text is
   POSTed to `/markdown` (gfm, this repository as context) as the issue identity, paced by the limiter but
   neither audited nor counted as a write. If the HTML holds a `user-mention`, `team-mention` or
   `issue-link`, the lane text is sent inside a fence the renderer must then confirm inert; a title or field
   value, which cannot be fenced, is refused, and so is text that stays live even fenced. A render that
   fails, or whose answer is not `text/html` (or is empty for non-empty input), stops the write and is never
   read as inert.
4. The final request payload is swept again, in the one place every write passes.

## API version

Requests send `X-GitHub-Api-Version: 2026-03-10`.

## Budget and audit

Each identity has its own bucket, persisted under a lock file so every process shares it: a 1.2 second
minimum gap, 80 writes a minute, 450 an hour. Every write through an identity counts (a read does not),
Project writes included. A `retry-after` or `x-ratelimit-reset` answer is honoured and recorded, so the next
process waits too. A 422 spam answer on a sub-issue add backs off and retries. A wait longer than
`TRACKER_MAX_WAIT` exits 75 instead of sleeping. That includes a Project step after a create or close: the
partial result is still printed, and the exit is 75. Waiting for another tracker process's lock (the budget
lock or a per-issue lock) is bounded the same way and exits 75. A damaged state file is never replaced by a
fresh bucket (its cooldowns would be lost): the tool exits 2 until it is fixed or removed. A success status
with a failed `gh` exit is an error, not an answer. A comments page or check-run page that is not a list is
an error, never an empty page. Each wait for the budget is one deadline across its sleeps and lock waits.

Every write, and every refused write, appends one line to the audit log: time, identity, method,
endpoint, issue, bytes, status and duration. Never a body or a token. The log is the metrics source for
writes per hour and rate-limit hits.

## Exit codes

0 done, 2 usage or missing setting (including an organisation-only feature on a user-owned repository and
the App identity on a user-owned Project), 3 refused (sweep, policy, missing proof, or a legacy id the map
cannot answer), 4 GitHub error or a failed verification, 5 `priority-check` found items to look at (its result is still printed; not 1, which a crashed Python exits with), 75 rate limited.

## Guard

`guard.py` refuses a shell command that makes one of these raw GitHub writes outside this tool: `gh issue`
comment, create, edit, close, reopen, develop, delete, lock, unlock, transfer, pin and unpin; `gh pr
comment`; any `gh project` subcommand except the reads (list, view, item-list, field-list); `gh api` with
a write method or fields on an issue, comment or Project path (a pull request's comments included); and
`gh api graphql` mutations. Pull requests carry code, not tracking, so `gh pr create`, `edit`, `ready`,
`close`, `reopen`, `merge` and `review`, and REST writes to a pull request itself, are allowed. It exits
2 with the reason on stderr and 0 otherwise. It reads shell structure (wrappers, `bash -c`, `$(...)`,
heredocs fed to a shell), not run-time values; text piped or here-stringed into a shell is not
analysed. It is defence in depth, not a boundary. Wire it as a Claude Code `PreToolUse` hook on `Bash`,
naming wherever the guard is installed:

```json
{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 /path/to/tracker/guard.py"}]}]}}
```

The hook reads the payload from stdin.

## Tests

`python3 -m unittest discover -s plugins/agent-workflows/tests -p 'test_tracker*.py'` runs the tracker's
tests against `fake_gh.py`, a stand-in `gh` that serves recorded routes and logs every call (a route may instead
serve a list of Project items page by page, with `per_page`, the `repo:`, `status:` and `label:` filter terms and a
`Link` cursor, as GitHub does); `with-token`'s
tests fake the minting calls (`appmint.urllib.request.urlopen`) and check that the token request names
the one repository. No test reaches GitHub. The REST shapes of Project items and fields follow
GitHub's documentation and are fixtures here.
