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
  - `run-check`: the run brief's checks. `land` says whether a pull request may land now and refuses
    guarded diffs until the root signs them off, `reap` removes the run's merged worktrees and `close`
    says whether the run can close. It never merges or pushes.
  - `run-drive`: the mechanical part of landing, one pass per call. `open` pushes a lane's branch,
    opens its pull request and asks the named reviewers; `sweep` goes over every open pull request
    of the run and enables auto-merge on an exact head only when its reviews, threads, checks and
    `run-check land` are all clear; `answer` and `repair-pushed` handle review threads and repairs.
    It never merges and never waits.
  - `wait-until`: a bounded wait. It runs a test command until it exits 0 or a timeout passes, and
    exits 124 on the timeout, so a root or lane never waits without a deadline.
- `tools/dsreview/`: `dsreview`, a code reviewer for lanes. It sends a candidate change and its
  brief to DeepSeek Flash through Cloudflare AI Gateway, lets the model read the repository through
  read-only tools, and prints a PASS or FAIL verdict as JSON. On 59 replayed reviews it caught as
  many defects as Opus reviewer lanes (8 of 11 each) with no false blocks, at about $0.05 a review
  against about $1.09. It is optional and needs your own gateway: see `tools/dsreview/README.md`.
- `tools/tracker/`: `tracker`, a thin shim over `gh` for GitHub issues and labels: `queue` lists the
  open issues labelled `ready`, and `create`, `claim`, `status`, `handoff`, `tick` and `close` write
  them as your `gh` login. `close` posts an evidence comment, then closes the issue. A
  `just tracker <command>` recipe of your own can run it: see
  `protocol/README.md` and `tools/tracker/README.md`.
- `protocol/`: the run brief (`loop/run.md`) and its Claude Code and pi appendices. See
  `protocol/README.md`.

## Requirements

- Claude Code with plugin support (tested with 2.1.303). The skill relies on Claude Code
  substituting `${CLAUDE_PLUGIN_ROOT}` in skill text, and the protocol relies on the plugin `bin/`
  directory being on the Bash tool's PATH.
- git, and Python 3.9 or later for the hooks and the programs in `bin/`.
- The GitHub CLI `gh`, signed in to the repository's host: `run-check` and `run-drive` read pull
  requests, checks and reviews through it, and the root opens issues and pull requests with it.
- Something to do: what you name when you start a run, or open GitHub issues labelled `ready`.
- Recommended: `just`, because the protocol's gate examples are `just` recipes (any gate command
  works).
- For the tracker tool, only if you use it: Python 3.12 or later and `gh` signed in.
- Optional: `dsreview` (in this plugin, Python 3.12 or later and a Cloudflare AI Gateway with a
  DeepSeek key), a cheap reviewer a root can run on a lane's change.

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

1. Ignore the run directory: run files live in `codex/` and never get committed.

   ```sh
   mkdir -p codex && echo 'codex/' >> .gitignore
   ```

2. Write `LOOP.md` at the repository root: the facts every run and every lane needs, which the root
   reads before it starts. Keep it short and free of dates. A minimal one:

   ```markdown
   # Loop: my-repo
   gate: just check
   guarded-paths: migrations/**, .github/workflows/**

   ## Traps

   - Build and test with `just check`; it must pass before a lane hands its change back.
   ```

   `gate` is the command that proves a change (name a remote runner here if builds should not run on
   your workstation) and `guarded-paths` lists the paths `run-check land` refuses until the root signs
   the diff off after a fresh-context review.

## Start a run

1. In the repository, in a session at Opus 5.5 and high effort, run `/agent-workflows:loop <what you
   want>`. The root reads the run brief, asks you everything it needs in one batch, writes the run
   files in `codex/run-<YYYYMMDD>-<n>/` and starts work.
2. When you leave, say so: the root writes `UNATTENDED` in the run folder, and from then on parks its
   questions instead of asking. Tell it when you are back.
3. The run stops when the goal is met or the queue is empty, with every worktree gone and a
   `CLOSEOUT.md` in the run folder.

## What the hooks write outside the repository

- Under `~/.local/state/agent-loops/`: one line per lane return in `lane-returns/<session>.jsonl`
  (status, branch and commit), and the count of turn-end continuations in `run-stop/<session>.json`.
  Nothing in this repository reads the first; you can delete either at any time.

## Not included

- pi and Codex runtimes. The pi appendix is in `protocol/loop/` for reference, but this plugin
  supplies only the Claude Code hooks and agents. Codex is not a run root.
- Tools from the author's own setup: syncing `codex/` between machines, the session catalogue that reports a run's usage, the remote gate
  runner, the watchdog and scheduled verification. The protocol mentions some of them; skip those
  steps.

## Development

`just check` runs the syntax checks and the tests in `tests/`, which drive the hooks over
Claude-shaped input, check that `run-check` and `run-drive` start, run `wait-until` to a pass and to a
timeout, and drive `dsreview` against a local fake gateway. It also runs the tracker tests, which drive
the tracker against a stand-in `gh` (they skip below Python 3.12).
`claude plugin validate .` checks the manifests.

## Where this comes from

This repository is build output. It is generated from private source repositories, which stay the
source of truth, so each export replaces the previous tree. Issues are welcome. Pull requests are
read, and an accepted change is re-applied upstream and arrives here in a later export rather than
being merged.

## Licence

Apache-2.0. See `LICENSE`.
