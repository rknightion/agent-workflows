# agent-workflows

A loop is one long Claude Code session, the root, that works a tracker queue (GitHub issues or a
Backlog.md board) in waves: it hands each task to a pinned subagent lane in its own git worktree,
reviews and lands what comes back, and stops only when it has written a closeout. This repository is
a Claude Code plugin marketplace that packages what makes that work, plus the protocol (Loop v4) the
root and its lanes follow.

The write-up is at https://rob-knight.com/loop/ and the rendered protocol is at
https://rob-knight.com/reference/.

## What the plugin contains

Everything lives in `plugins/agent-workflows/`.

- `skills/loop/`: the `loop` skill (`/agent-workflows:loop`). It prepares a run: the charter, the
  lane rules and the launch file, linted, for you to launch.
- `agents/`: the six lane roles, each with its model and effort pinned:

  | Agent | Model / effort | Use |
  |---|---|---|
  | `agent-workflows:worker` | Sonnet 5.5 / high | the default for every task: proven red then green, handed back as a patch |
  | `agent-workflows:composer` | Sonnet 5.5 / high | composing a landing batch or a board batch, committed locally |
  | `agent-workflows:senior` | Opus 5.5 / medium | a shared-contract freeze, an unknown cause, a security or privacy call, a design return, an escalated failure |
  | `agent-workflows:senior-escalation` | Opus 5.5 / xhigh | a task that has already failed review rounds |
  | `agent-workflows:reviewer` | Opus 5.5 / high | read-only review, PASS or FAIL with findings |
  | `agent-workflows:scout` | Sonnet 5.5 / medium | read-only mapping, sweeps and log reduction |

- `hooks/hooks.json` and `scripts/`: two root hooks. The Stop hook re-prompts a root that ends its turn
  before its closeout counts, up to three times in a row, unless its last line is a `PAUSED:` line
  or a `WAITING: ... until <UTC time>` line. Under a `protocol: v4` charter a closeout counts only
  when the run's `loop-land` ledger is settled: every merged landing verified with main and any
  deploy read, no unmerged landing left at `ready` (verify it or abandon it), and a
  `loop-land summary --check` newer than the last merge. A charter with `loop-land: off` is judged
  on the closeout file alone. The PreToolUse hook denies a root's lane dispatch whose brief does not
  open with the contract's header line. Both arm only in a session whose operator message is a loop
  launch ("You are the root" plus one closeout path), never in ordinary sessions, and both let the
  call through on any fault of their own.
- The plugin also registers a global SubagentStop lane hook, `scripts/lane_return.py`. It
  requires a nonempty `agent_id` and an exact `agent-workflows:worker`, `agent-workflows:composer`, `agent-workflows:senior`,
  `agent-workflows:senior-escalation`, `agent-workflows:reviewer` or `agent-workflows:scout` type.
  Root sessions (including `--agent`), unrelated agents and empty-type forks are no-ops; the
  registration matcher alone is not the authority. Plugin-agent frontmatter hooks are not used.
  It sends a lane back to repair its return when the lane-return block is missing, malformed or
  duplicated, lacks or mistypes a field, names a patch whose sha256 does not match (the reason
  names both hashes, or the unreadable path), names a review record without an absolute evidence
  path, or reports `complete` with a nonzero check exit.
  Honest partial/blocked returns, read-only null patches and composer, senior and senior-escalation commit SHAs
  remain valid; a reviewer or scout returns `"review":null`, because its verdict or answer is its
  message. Return fences are limited to 1,800 UTF-16 units and JSON nesting to 32; decoder
  failures block rather than fail open. The hook writes no state and repeated stops still
  validate, but Claude's native consecutive-stop-block cap (default 8) can end a lane despite
  later block requests. No hook inspects lane commands: the lanes' no-push rule lives in the agent
  definitions.
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
- `tools/tracker/`: `tracker`, the one way the loop root writes GitHub issues and a GitHub Project:
  `queue` lists the ready issues, and `create`, `claim`, `status`, `handoff`, `tick` and `close` write
  them. Lanes never run it. `close --pr` closes an issue only on a merged pull request's passing
  acceptance evidence. It runs as your `gh` login or, optionally, as a GitHub App through
  `with-token`. The protocol's `just tracker <command>` is a recipe of your own that runs it: see
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
- For the tracker tool, only if you use it: Python 3.12 or later, `gh` signed in with the `project`
  scope, and a GitHub Project with the fields `protocol/README.md` lists.
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

## Run a first loop

1. In the repository, run `/agent-workflows:loop`. The skill reads the planner and the tracker,
   asks you every decision the first wave needs in one round, then writes
   `codex/charter-<date>-loop1.md`, `codex/loop1/LANE.md` and `codex/launch-<date>-loop1.txt`,
   lints them and gives you the launch file's path.
2. Start a fresh session with the command on the launch file's `cmd:` line (for example
   `claude --model claude-opus-5-5 --effort medium --disallowedTools AskUserQuestion`) and paste the
   rest of the file as the first message. That message arms the hooks.
3. The root works the queue, landing each batch with `loop-land`, until it writes
   `codex/closeout-<date>-loop1.md` and replies with its path. For the next run, pass that closeout
   to the skill: `/agent-workflows:loop <closeout path>`.

The launch file names the contract and the harness file by absolute path inside the installed
plugin. Claude Code keeps a previous plugin version on disk for 14 days after an update, so prepare
and launch a run in the same sitting.

### Landing without `loop-land`

`loop-land: off` in the charter turns the landing tool off for that run. The root then merges once
every gate leg and required check on the head is green, waits on main's CI for the merge commit with
`wait-until`, and keeps no ledger, and the Stop hook judges the closeout on the file alone. The skill
writes the line only when you ask for it.

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

### Standing authority

A charter can point at a Standing file: per-repository grants (live reads, deploy targets) that
carry from run to run. It is optional and none by default: the skill writes `codex/standing.md`
holding `No standing grants.` and pins its sha256 in the charter and the launch file. Per-run
grants go in the charter's `authority:` lines.

## What the hooks write outside the repository

- A scan cache per session in the system temp directory, under `agent-workflows-fanout/`.
- One line per Stop decision, and an incident file when the hook gives up or faults, under
  `~/.local/state/agent-loops/`. An external watchdog in the author's setup reads them; nothing in
  this repository does, and you can delete them at any time.

## Not included

- Codex, pi and Claude-on-GPT runtimes. Their harness files are in `protocol/loop/` for
  reference, but this plugin supplies only the Claude Code hooks and agents.
- Tools from the author's own setup: syncing `codex/` between machines, the session catalogue that reports a run's usage, the remote gate
  runner, the watchdog and scheduled verification. The protocol mentions some of them; skip those
  steps.

## Development

`just check` runs the syntax checks and the tests in `tests/`, which drive the hooks, the launch
template and `loop-lint` against temporary repositories, run `wait-until` to a pass and to a
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
