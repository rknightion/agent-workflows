# agent-workflows

A run is one Claude Code session, the root, that works a goal across many tasks: it does small
things itself, hands the rest to pinned subagent lanes in their own git worktrees, reads every diff,
lands each pull request on its own, and stops with a closeout. This repository is a Claude Code
plugin marketplace that packages what makes that work, plus the one-page protocol (`protocol/loop/run.md`)
the root follows.

The write-up is at https://rob-knight.com/loop/ and the rendered protocol is at
https://rob-knight.com/reference/.

## What the plugin contains

Everything lives in `plugins/agent-workflows/`.

- `skills/loop/`: the `loop` skill (`/agent-workflows:loop <what you want>`). It starts a run in the
  session you invoke it in: the root reads `protocol/loop/run.md` and its Claude appendix and begins.
- `agents/`: the six lane roles, each with its model and effort pinned. A brief gives the goal, the
  check that proves it, what is in and out of scope, and what to send back; the lane decides what the
  goal implies and returns plain text whose first line is `Status: complete`, `partial`, `blocked`
  or `failed`:

  | Agent | Model / effort | Use |
  |---|---|---|
  | `agent-workflows:worker` | Sonnet 5.5 / medium | the default for every task: proven red then green, committed locally |
  | `agent-workflows:composer` | Sonnet 5.5 / medium | putting several lane branches or small changes onto one branch |
  | `agent-workflows:senior` | Opus 5.5 / high | a shared interface, an unknown cause, a security or privacy call, a task where a wrong answer could pass |
  | `agent-workflows:senior-escalation` | Opus 5.5 / high | a task that has failed or been parked twice |
  | `agent-workflows:reviewer` | Opus 5.5 / high | read-only review, PASS or FAIL with findings |
  | `agent-workflows:scout` | Sonnet 5.5 / medium | read-only mapping, sweeps and log reduction |

- `hooks/hooks.json` and `scripts/`: the run gate and the lane record. A run keeps its files in
  `codex/run-<YYYYMMDD>-<n>/`; the root writes an `UNATTENDED` file there when you leave and removes it
  when you are back. While it is there and the run has no `CLOSEOUT.md`, `scripts/run_gate.py` refuses
  the root's question prompt (the root parks the question in `PARKED.md` and carries on) and sends
  the root back to work when it ends a turn, at most three times in a row, unless its reply ends with
  a `WAITING:` line. Both act only on the main thread of a session whose transcript names that run
  folder, never on a subagent, and both let the call or the stop through on any fault of their own.
  `scripts/lane_return.py` (SubagentStop, the six roles only) reads a lane's status line, branch and
  commit and appends them to a record; it never sends a lane back over the shape of its return.
- `scripts/tmux_title_state.sh`: hooks that put the session's state (working, waiting, done,
  failed) in a tmux pane option, `@cc_state`, for a tmux title to show. Outside tmux they do
  nothing.
- `bin/`: the programs the protocol tells the root and the lanes to run. While the plugin is enabled,
  Claude Code puts this directory on the Bash tool's PATH, so the skill, the root and the lanes run
  them by name.
  - `loop-lint`: the charter, queue, parked-work and `LOOP.md` linter.
  - `loop-land`: the landing tool. `ready` decides whether a pull request (or, in direct mode, a
    commit) may land now and prints the merge command for the root to run; `verify` records what
    the merge did to main and any deploy; `abandon` and `summary` close the record. It never merges
    or pushes itself, and it keeps its ledger in the run directory (`landings.jsonl`). Two states stop
    a run on purpose: see "When `loop-land` stops on purpose" below.
  - `run-check`: the run brief's checks. `land` says whether a pull request may land now and refuses
    guarded diffs until the root signs them off, `reap` removes the run's merged worktrees and `close`
    says whether the run can close. It never merges or pushes.
  - `loop-teardown`: removes the worktrees, caches and large logs a run left behind (`closeout` for
    one run, `reap` for every finished run of a repository), archiving anything not already on a
    remote first. It is a dry run unless given `--apply`.
  - `wait-until`: a bounded wait. It runs a test command until it exits 0 or a timeout passes, and
    exits 124 on the timeout, so a root or lane never waits without a deadline.
- `tools/dsreview/`: `dsreview`, a code reviewer for lanes. It sends a candidate change and its
  brief to DeepSeek Flash through Cloudflare AI Gateway, lets the model read the repository through
  read-only tools, and prints a PASS or FAIL verdict as JSON. On 59 replayed reviews it caught as
  many defects as Opus reviewer lanes (8 of 11 each) with no false blocks, at about $0.05 a review
  against about $1.09. It is optional and needs your own gateway: see `tools/dsreview/README.md`.
- `tools/tracker/`: `tracker`, a thin shim over `gh` for GitHub issues and labels: `queue` lists the
  open issues labelled `ready`, and `create`, `claim`, `status`, `handoff`, `tick` and `close` write
  them as your `gh` login. `close` posts an evidence comment, then closes the issue. The protocol's
  `just tracker <command>` is a recipe of your own that runs it: see
  `protocol/README.md` and `tools/tracker/README.md`.
- `protocol/`: the contract, the three harness files, the charter and lane templates, the planner,
  the review profiles and the board setup guide. See `protocol/README.md`.

## Requirements

- Claude Code with plugin support (tested with 2.1.303). The skill relies on Claude Code
  substituting `${CLAUDE_PLUGIN_ROOT}` in skill text, and the protocol relies on the plugin `bin/`
  directory being on the Bash tool's PATH.
- git, and Python 3.9 or later for the hooks and the programs in `bin/`. `shasum` (or another way to
  take a sha256) for the Standing file and lane patches.
- The GitHub CLI `gh`, signed in to the repository's host: `loop-land` reads required checks, pull
  requests and main's CI through it, the root merges with `gh pr merge`, and a GitHub tracker is read
  with it. `loop-lint loopmd --offline` and `loop-lint charter --offline` skip its online checks.
- A tracker with ready work, one of:
  - GitHub issues. `LOOP.md` either says `tracker: github` (then it also needs `landing: pr` and a
    `## Queue` section) or names no tracker in a repository without a `backlog/` directory. Issues
    can live in another repository with `tracker-repo: OWNER/REPO`.
  - A Backlog.md board: a `backlog/` directory and the Backlog.md CLI (`backlog`), which the root
    and `loop-lint queue` read tasks through.
- Recommended: `just`, because the protocol's gate and verification examples are `just` recipes
  (any gate command works).
- For the tracker tool, only if you use it: Python 3.12 or later and `gh` signed in.
- Optional: `dsreview` (in this plugin, Python 3.12 or later and a Cloudflare AI Gateway with a
  DeepSeek key), one command that can fill the lane template's local review step. With no local
  reviewer, the planner freezes `none` there and lanes return `"review":null`.

## Install

Inside Claude Code:

```text
/plugin marketplace add rknightion/agent-workflows
/plugin install agent-workflows@rob-knight
```

Or from a shell:

```sh
claude plugin marketplace add rknightion/agent-workflows
claude plugin install agent-workflows@rob-knight
```

Start a new session (or run `/reload-plugins`). `claude plugin details agent-workflows` should list
one skill, six agents and the hooks.

## Prepare a repository

1. Give the loop a tracker with some ready work: GitHub issues, or a Backlog.md board
   (`backlog init`, see `protocol/docs/backlog-board-setup.md`).
2. Ignore the run directory: loop files live in `codex/` and never get committed.

   ```sh
   mkdir -p codex && echo 'codex/' >> .gitignore
   ```

3. Write `LOOP.md` at the repository root: the facts every run and every lane needs. Keep it under
   6 KB and free of dates and loop numbers. A minimal one:

   ```markdown
   # Loop: my-repo
   tier: routine
   gate: just check
   ci-required: none
   grafana-stack: none

   ## Lane brief

   - Build and test with `just check`; it must pass before a candidate is handed back.
   - Never edit `migrations/` without a task that names it.
   ```

   `tier` is `routine` or `guarded` (guarded runs get a security review before anything lands),
   `gate` is the command that proves a change, `ci-required` names required CI checks or `none`,
   and `grafana-stack` names a telemetry stack or `none`. With no `tracker:` key, a repository with a
   `backlog/` directory uses its board and lands commits directly on main (`landing: pr` lands by
   pull request instead); one without uses GitHub issues and lands by pull request. To declare a GitHub
   tracker, add `tracker: github`, `landing: pr` and a `## Queue` section that names the command
   printing your ready issues in priority order and says that only the root writes issues:

   ```markdown
   # Loop: my-repo
   tier: routine
   gate: just check
   ci-required: none
   grafana-stack: none
   tracker: github
   landing: pr

   ## Queue

   Ready issues, in priority order: `just tracker queue` (a recipe of your own that runs
   `tools/tracker/tracker.py`, see `protocol/README.md`). Only the root writes issues.
   ```

   Inside Claude Code, check it with `loop-lint loopmd LOOP.md --offline`.

## Start a run

1. In the repository, in a session at Opus 5.5 and high effort, run `/agent-workflows:loop <what you
   want>`. The root reads the run brief, asks you everything it needs in one batch, writes the run
   files in `codex/run-<YYYYMMDD>-<n>/` and starts work.
2. When you leave, say so: the root writes `UNATTENDED` in the run folder, and from then on parks its
   questions instead of asking. Tell it when you are back.
3. The run stops when the goal is met or the queue is empty, with every worktree gone and a
   `CLOSEOUT.md` in the run folder.

### When `loop-land` stops on purpose

Two states stop a run and need the repository owner, never the loop root:

- A pull request merged while `ready` was waiting. `ready` stopped with exit 30 and took no slot, so
  no landing is recorded.
- `verify` returned `wrong-head` (exit 21): the pull request merged with a head other than the slot's.
  The landing slot stays held, so every later `ready` is `slot-busy`.

The owner first confirms on GitHub what merged (`gh pr view <n> --json headRefOid,mergeCommit`). If it
is the change the run meant to land, the owner runs

```sh
loop-land adopt --repo <path> --run-dir <dir> --pr <n> --merged-head <sha> --reason "<why>"
```

with `--issues 11,12` as well when no slot or earlier event for the pull request names them, and then
`loop-land verify --repo <path> --run-dir <dir> --pr <n>`. `adopt` writes one `merged` event, frees
the slot when it names this pull request, and never merges, pushes or writes to GitHub. It refuses
and writes nothing unless GitHub shows the pull request merged at exactly `--merged-head` with a
merge commit on main. Exit 40 means it is not adoptable and 41 means the ledger already records the
merge. If the merged change is not what the run meant to land, do not adopt: fix forward by hand and
say so in the closeout.

## What the hooks write outside the repository

- Under `~/.local/state/agent-loops/`: one line per lane return in `lane-returns/<session>.jsonl`
  (status, branch and commit), and the count of turn-end continuations in `run-stop/<session>.json`.
  Nothing in this repository reads the first; you can delete either at any time.

## Not included

- pi and Codex runtimes. The pi appendix and the Loop v4 harness files are in `protocol/loop/` for
  reference, but this plugin supplies only the Claude Code hooks and agents. Codex is not a run root.
- Tools from the author's own setup: syncing `codex/` between machines, the session catalogue that reports a run's usage, the remote gate
  runner, the watchdog and scheduled verification. The protocol mentions some of them; skip those
  steps.

## Development

`just check` runs the syntax checks and the tests in `tests/`, which drive the hooks over
Claude-shaped input and `loop-lint` against temporary repositories, run `wait-until` to a pass and to a
timeout, and drive `dsreview` against a local fake gateway. It also runs the `loop-land` tests, which
drive every command of the landing tool against temporary repositories with a stand-in `gh`, and the
tracker tests, which drive the tracker against a stand-in `gh` (they skip below Python 3.12).
`claude plugin validate .` checks the manifests.

## Where this comes from

This repository is build output. It is generated from private source repositories, which stay the
source of truth, so each export replaces the previous tree. Issues are welcome. Pull requests are
read, and an accepted change is re-applied upstream and arrives here in a later export rather than
being merged.

## Licence

Apache-2.0. See `LICENSE`.
