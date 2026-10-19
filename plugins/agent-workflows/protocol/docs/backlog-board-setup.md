# Backlog.md board setup and tracker migration

Read this before initialising a Backlog.md board on a repository, or before migrating a repository
off GitHub Issues or another tracker. It is the single canonical copy. `AGENTS.md` in this repo keeps
only the short procedure and points here.

Backlog.md is the default task tracker for GitHub repositories owned by `rknightion`, `m7kni` or
`example-client-org`. Establish the owner from the remote, not the checkout path. A qualifying
repository without `backlog/` is not permission to guess the immutable settings: use the question
round in section 7 when durable tracking is needed, and never fall back silently to GitHub Issues or
a loose task file.

**Owning the tracker does not imply owning the push.** `example-client-org` gets a board but is not on
the push-to-`main` list, so its setup lands as a branch and a pull request. Check the two lists
separately.

Verified against **Backlog.md 1.53.0**. Several behaviours below changed in patch releases; check
`backlog --version` and the relevant `--help` before trusting a trap.

## 1. Initialise

Three settings are **set once and cannot change**: the task prefix, the zero-padding width and the
backlog directory. Choose them before running anything.

```bash
backlog init "<ProjectName>" --defaults \
  --task-prefix <letters> --zero-padded-ids 4 \
  --backlog-dir backlog --config-location folder \
  --agent-instructions agents \
  --check-branches false --include-remote false \
  --install-claude-agent false
printf '@AGENTS.md\n' > CLAUDE.md
```

- **`--task-prefix`**: short, project-specific, **letters only** (a digit is rejected, so `f38` or
  `m7c` fail), and `draft`, `doc` and `decision` are reserved. IDs come out `ABC-0001`. Never keep
  the default `task`: IDs pasted between repos become ambiguous. State the letters-only rule in the
  question round, or it costs a second round.
- **`--zero-padded-ids 4`** is not retroactive. Turning it on later gives `ABC-9` and `ABC-0010`
  side by side forever.
- **`--backlog-dir backlog`** cannot vary: `init` refuses to move it, and `bin/doctor`,
  `bin/doctor-remote` and `bin/publish.py` here hardcode `backlog/docs`.
- **`--config-location folder`** is the default, not an unconditional one. Run the `find` in
  section 3 first and use `root` if it matches anything. This one is not immutable: `git mv` the file
  later and the CLI reads either location.
- **`--agent-instructions agents`** writes the upstream marker-delimited nudge block into
  `AGENTS.md` only. `CLAUDE.md` is then the one-line `@AGENTS.md` import, so Claude Code and Codex
  read one canonical file. On an existing repo without the block, `backlog agents
  --update-instructions` adds or refreshes it and preserves other content (interactive).
  **The block is tool-owned: nothing repo-specific goes inside its markers**, or the next refresh
  silently drops it. Put repo rules and `## Deeper references` outside.
- **`--check-branches false --include-remote false`**: the default is still `true`, and it is the
  most bug-prone surface in the product (section 3).
- **`--install-claude-agent false`**: the optional subagent overlaps the instruction block.

### Then edit the config file directly

List-valued keys cannot be set through `backlog config set`. Hand-editing `config.yml` is the one
sanctioned exception to the never-hand-edit rule, which covers task, draft, doc, decision and
milestone markdown.

```yaml
statuses: ["To Do", "In Progress", "Parked", "Done"]
definition_of_done: ["<real gate command>", "<second gate>"]
auto_commit: false
```

- **`Parked`** means attempted, blocked and left with a resume boundary. It is not `To Do`, and
  flattening it loses the most valuable thing a long run produces. `Done` must stay last: the final
  status is terminal.
- **`definition_of_done`** carries the repo's real gate commands, read from its CI, not guessed.
  Every new task inherits them as a checklist.
- **`auto_commit: false`**: the loop root owns every commit, and the auto-commit path has had two
  staging-scope bugs.
- **If the repo lints YAML, this file is in scope.** Where a line-length cap bites, write long entries
  as folded scalars (`- >-` with short continuation lines). A later `backlog config set` rewrites the
  file and re-flattens them, which fails the gate.

## 2. Non-negotiable rules

Put these in the repo's `AGENTS.md`, outside the tool-managed markers.

**`backlog/` is committed, so tasks, docs and decisions never contain real account identifiers or
personal data**: no email addresses, handles, usernames, account IDs, device names, addresses,
coordinates or data-archive values. Write the shape, not the instance. Aggregate counts, timings and
structural findings are fine. Sweep before committing:

```bash
grep -rniE "<your handles>|<your domains>|@gmail|<known account ids>" backlog/ && echo "PII FOUND"
```

**Check `gh repo view --json visibility` and widen the sweep on a public repo.** Installation, org,
repo, tenant and stack ids and internal hostnames are unremarkable in a private repo and must not
land on a public board. Put them in a private repo's commit message instead and refer to them by
shape. A public repo with its own scrub gate (for example a private denylist in pre-commit) holds
`backlog/` to it like any other tracked path.

**Never use the bare form of any field that has an `--append-*` variant.** The bare flag replaces the
whole section and destroys another session's writes. The rule is the whole family: `--notes`,
`--plan` and `--final-summary` each have an `--append-` form. Check `--help` for an append variant
before using any field-setting flag; a new one is covered on sight. The global `backlog-guard.py`
`PreToolUse` hook denies the bare forms in every repo on both harnesses. Install nothing per repo.

**A second `task edit` on one task REPLACES what the first set** for every set-semantics flag with no
append form: `--dep`, `--assignee`, `--label`, `--ref`, `--acceptance-criteria`, `--modified-file`.
Repeating a flag inside one call is additive (`--dep A --dep B` gives both); a second call discards
the first. Prefer the additive forms: `--add-label` / `--remove-label`, `--ac`, `--add-ref` /
`--remove-ref`. The guard hook cannot see across separate tool calls.

**Finalize in one call**, so an interrupt cannot leave finished work looking unfinished:

```bash
backlog task edit ABC-0007 --check-ac 1 --check-ac 2 -s Done
```

**Never hand-edit task markdown.** Section boundaries are HTML-comment markers. Break one and the
section is silently dropped, exit 0, with the data still in the file until the next write destroys
it. `backlog doctor` only fixes duplicate task IDs; there is no repair path.

**Never let two agents edit the same task.** The 1.50 concurrency fix covers the edit funnel only,
not reorder, draft saves, the TUI path, `doc update` or decision updates.

## 3. Upstream traps

- **`check_active_branches: true` breaks ordinary branch work.** It decides ambiguity by content
  hash, so a task with an uncommitted edit, or a divergent blob on any active local or remote ref,
  409s as an ambiguous duplicate while `doctor` reports the board clean. Saving a task in the web UI
  is enough to reproduce it.
- **Sequential IDs collide across clones and worktrees**, and upstream declined a collision-free
  mode. `backlog doctor` repairs task-ID duplicates only; duplicate doc and decision IDs resolve
  silently by title order, so retitling a doc can re-point its ID.
- **Archiving releases the ID for reuse**, so an archived-then-reused number in a commit message is
  ambiguous.
- **There is no `task delete`.** `archive` moves the file to `backlog/archive/tasks/`, so a
  throwaway task still leaves a file to remove before committing.
- **Decisions are half-built**: only `create` and `list`, empty sections only the web UI can fill,
  no supersede and no status validation. Put durable reference in docs; tasks are the unit.
- **Do not build on the MCP server.** Upstream keeps it as legacy and does not plan to evolve it,
  and it costs far more context than the CLI.
- **There is no in-tool hook system.** Enforcement lives in the agent harness.
- **Multi-repo boards are not supported.** The backlog lives inside the repo.
- **`backlog config get definition_of_done` answers "Unknown config key"** although the snake_case
  YAML is honoured; the CLI namespace is camelCase (`definitionOfDone`). The failing `get` looks like
  a silently ignored setting. Verify by creating a canary task and viewing its Definition of Done
  (then archive it and remove the archived file).
- **`doc create -t` accepts only `readme`, `guide`, `specification`, `other`.** The error arrives
  after the create attempt, so it reads like a failure of the whole command.
- **`init` appends its block to an existing `AGENTS.md`**; it does not overwrite. In a repo that
  already has both `CLAUDE.md` and `AGENTS.md`, diff them first: the drift decides which becomes
  canonical.
- **`backlog/config.yml` can enlist the tracker in the repo's own build.** Tooling that discovers
  components by a shallow config file matches it (a Home Assistant add-on repo built `backlog` as a
  fifth add-on and failed with five missing-field errors). Check before `init`, because it only fails
  in CI:

  ```bash
  find ./ -maxdepth 2 \( -name config.json -o -name config.yaml -o -name config.yml \) -not -path './.git/*'
  ```

  If anything discovers that way, use `--config-location root` and record the reason in `AGENTS.md`,
  because a stray `backlog.config.yml` at the root invites being moved back.

## 4. The document set

**The loop pointer is generated.** Repository loop facts do not get a board doc: they go in
`LOOP.md` (section 5), so nothing restates the loop contract and drifts.

### The loop pointer

Build output, rendered from `sources/loop-pointer.md` here under its own header, which marks it as a
pointer, not authoritative. It names the canonical loop files in `<plugin root>/protocol/loop/`;
the protocol itself is never copied into a consumer. Never hand-copy the pointer and never edit a
consumer's copy. Onboarding is the four coupled edits in `AGENTS.md` "Adding a consumer repository",
then `just publish loop-pointer` on a publisher host (`publisher-host`). `bin/sync` is retired and
exits 2.

- **Create the doc first**, with `backlog doc create "Loop protocol pointer" -t specification`, and
  before the repo's other docs so it usually lands at `doc-0001`. Nothing depends on the number:
  `registry.tsv` records what the CLI assigned, and one consumer keeps it at `doc-0005`. Older consumers
  keep their original doc title; publication sets the title to `Loop protocol pointer` (the
  publisher renames the file to match) and replaces the body.
- The rendered header names the source commit, not a date. Do not write a header.
- Changing the loop protocol: edit `sources/loop/`, commit and push. Consumers need no publication,
  because the pointer does not change; publish only when the pointer itself changes.
- A new consumer **owner** needs OpenBao work (permission set, mint step, `token_for`) or the daily
  drift run fails for every repo; a new repo under an existing owner still needs adding to that
  owner's permission-set repository list, or it reports `NO TOKEN`. List wired owners live:

  ```bash
  awk -F'\t' '/^[^#]/ && NF {split($2,a,"/"); print a[1]}' $PROTOCOL_SOURCE/registry.tsv | sort -u
  ```

- **Why a pointer, not a copy:** full copies went stale across the fleet while agents kept reading
  them. A root reads only its charter, the contract and its harness file, by path; a lane reads its brief and
  the run's `LANE.md`.

**Recorded exception: `rknightion/grafana-aio11y-demo` carries no loop pointer.** It is a public
customer-facing demo whose scrub gate rejected the rendered protocol, and the protocol is internal
guidance. Its `doc-0001` says so, and it has no `registry.tsv` row. A loop on that repo reads the
loop files from this checkout. Do not "fix" it by registering the repo.

### Optional docs and wiring

Add a domain contract, expensive reference findings or standing decisions when there is something to
put in them. On a migration, the closed-work index doc is the default (section 7), titled exactly
`Closed GitHub issues (pre-Backlog history index)` with `-t other`. It is local content and not in
`registry.tsv`.

`doc create` takes no content, so create then fill with `backlog doc update <id> --content "..."`
(real newlines; a literal `\n` is stored as text). Numbering follows creation order and cannot be set
or repaired, and one doc id maps to many titles across the fleet. **Refer to docs by title in prose,
and key tooling on `registry.tsv`.**

Point `AGENTS.md` at the pointer and `LOOP.md`, outside the markers:

```markdown
- Read the loop protocol pointer doc before designing a loop, and `LOOP.md` for this project's own
  loop facts. `backlog doc list --plain` shows the pointer.
```

## 5. Loop artefacts every consumer needs

- **A committed `LOOP.md`**, pointed to from `AGENTS.md` under `## Deeper references` (outside the
  markers), in the shape `sources/loop/planner.md` gives and `loop-lint loopmd` checks: at most 6 KB,
  key lines first, nothing dated. No em or en dashes in it; several consumers gate shipped text on
  that.
- **A gitignored `codex/` at the repo root** for run artefacts. The name is load-bearing:
  `bin/codex-reconcile` targets it exactly, and `bin/conformance` checks the ignore line.
- The loop contract is authoritative on everything else, including that the run-end closeout is a file in
  `codex/` whether or not the repo has a tracker.

## 6. Deliberately not done

- **No MCP server** and **no decisions workflow** (section 3).
- **No Claude Code subagent**: it overlaps the instruction block.
- **No repo-local guard hook.** `backlog-guard.py` is global, wired in every Claude and Codex home and
  parity-enforced by the config doctors. Per-repo copies drifted into four variants; those under
  `$PROJECTS_DIR/*/.claude/hooks/` are stale and are not a template. Gitignore `.claude/` whole.
- **No bulk import of history.** Only open work becomes tasks.
- **Synthetic shared-board example.** The fictional `example-org/widget-cli` and
  `example-org/widget-sdk` repositories could share a `wgt` board in a separate
  `example-org/widget-planning` checkout at `$PROJECTS_DIR/widget-planning`. Tasks
  can span both packages; each repository should point contributors to the shared
  board rather than create a duplicate queue. Choose the board's visibility and
  scrub private identifiers before publishing tasks or history. This example is
  not an inventory or an owner grant: decide board placement for your own projects
  during the section 7 question round.

## 7. Migrating off an existing tracker

**Default, without asking: import open work as tasks and closed work as ONE index doc.** Importing
closed items as `Done` tasks creates a second ID space over the same history (backlog IDs follow
creation order, so they can never match the `#NNN` already cited in commits and code) and buries the
board's real signal, what is left. The index doc (one row per closed item: id, title, closed date,
resulting SHA where derivable) keeps history readable from the checkout and keeps one ID space.

**Ask instead of defaulting when the shape differs**: closed items few enough to be tasks, no
surviving external home to cite back to (the index then needs full bodies), or unclear open work.

`tailscale2otel` (commits `4468bca` and `333b0cc`) is the reference migration; copy from it.

### Investigate before changing anything

Report all of this first:

- every open and closed issue with counts. **`gh issue list` defaults to 30 results**: always pass
  `--limit 1000` and state the count you got;
- other queues that are really open work (a gitignored roadmap, `todos.txt`, plans, `CONTINUE.md`),
  judged against the newest closed issue and whether the issues they name are closed;
- real `TODO`/`FIXME` residue in source;
- whether `CLAUDE.md` and `AGENTS.md` both exist and have drifted, and which is stale (Codex reads
  only `AGENTS.md`);
- ownership and visibility (`gh repo view --json owner,isFork,visibility`): push-to-`main` or PR,
  whether the owner is already wired for drift (section 4), and how wide the identifier sweep is;
- the real gate commands from CI, which become `definition_of_done`;
- the shallow `config.*` `find` from section 3.

**An empty board is a legitimate outcome.** Do not mine stale plan files to look productive.

### Batch every fork into one question round

Ask all at once, then stop: the **task prefix** (permanent, letters only, reserved words), which
open issues to **exclude**, the `CLAUDE.md`/`AGENTS.md` **direction** if they drifted, the
**`definition_of_done` scope** where the full CI gate would be noise on every task, and how to handle
**drift onboarding for an owner new to the fleet** (do the OpenBao work now, or track it as a task).
Do not ask about closed issues; the default above settles that.

### Archive, then delete: in that order, and only after the archive is pushed

The index doc is a pointer that only works while the issues exist. If they are to be deleted, the
archive becomes the record and must be captured, verified, committed and pushed first.

```bash
gh issue list --state all --limit 1000 \
  --json number,title,body,comments,labels,state,stateReason,author,createdAt,updatedAt,closedAt,url,milestone,assignees \
  > issues-dump.json
```

- **Verify comments are complete, not merely present.** `--json comments` paginates. Require an exact
  match between the dump and the REST per-issue counts:
  `gh api --paginate 'repos/OWNER/REPO/issues?state=all&per_page=100' --jq '.[]|select(.pull_request==null)|.comments'`
- **Redact before it enters git**, with stable placeholders (one real value, one token everywhere) and
  an `archive/README.md` carrying the mapping. Committing the raw dump moves identifiers from
  somewhere deletable into permanent git history at the moment you delete them.
- **Sweep the decoded fields, never the serialized JSON.** An escape such as `\n` leaves a literal `n`
  before the next word and breaks a `\b` boundary: a blob sweep found 51 occurrences of one host name
  where a per-field sweep found 57, a confident false pass.
- **Refuse to delete anything not in the archive.** Build the delete list from the live API, assert
  every number is archived and that `deleted + kept == live`, using a real set comparison: `comm`
  misreports on numerically sorted input.
- **Delete only your own and your bots' issues.** The tracker stays enabled for external contributors,
  and Renovate's dependency dashboard stays.
- Deletion is irreversible and one call per issue. Pace it against GitHub's secondary rate limit and
  retry on that error.
- **Then fix the index doc**, whose header promised `gh issue view <N>`: point it at the redacted
  archive with a `jq` one-liner.

### The rest

- Follow sections 1 to 5.
- Stage explicit pathspecs if the tree has changes that are not yours. Never `git add -A` or
  `git commit -a` in a shared checkout.
- Run the repo's own gate before pushing or opening the PR: `backlog/` is subject to whatever lints
  and contract tests the repo already has.

## 8. Caveats

Nobody has published a measurement of whether agents keep a tracker current, and the documented
failure is agents stopping partway through finalization. The upstream block ships a
`<CRITICAL_INSTRUCTION>` nudge on every request, which admits unprompted compliance is not the
default. Expect to keep harness-level enforcement and to tune it.
