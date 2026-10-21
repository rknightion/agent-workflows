# agent-workflows

A loop is one long Claude Code session, the root, that works a Backlog.md board in waves: it hands
each task to a pinned subagent lane in its own git worktree, reviews and lands what comes back, and
stops only when it has written a closeout. This repository is a Claude Code plugin marketplace that
packages what makes that work, plus the protocol (Loop v3) the root and its lanes follow.

The write-up is at https://rob-knight.com/loop/ and the rendered protocol is at
https://rob-knight.com/reference/.

## What the plugin contains

Everything lives in `plugins/agent-workflows/`.

- `skills/loop/`: the `loop` skill (`/agent-workflows:loop`). It prepares a run: the charter, the
  lane rules and the launch file, linted, for you to launch.
- `agents/`: the five lane roles, each with its model and effort pinned:

  | Agent | Model / effort | Use |
  |---|---|---|
  | `agent-workflows:worker` | Sonnet 5.5 / high | one task, proven red then green, handed back as a patch |
  | `agent-workflows:senior` | Opus 5.5 / medium | judgement calls, unknown causes, composing a landing batch |
  | `agent-workflows:senior-escalation` | Opus 5.5 / xhigh | a task that has already failed review rounds |
  | `agent-workflows:reviewer` | Opus 5.5 / high | read-only review, PASS or FAIL with findings |
  | `agent-workflows:scout` | Sonnet 5.5 / medium | read-only mapping, sweeps and log reduction |

- `hooks/hooks.json` and `scripts/`: two hooks. The Stop hook re-prompts a root that ends its turn
  before its closeout exists, up to three times in a row, unless its last line is a `PAUSED:` line
  or a `WAITING: ... until <UTC time>` line. The PreToolUse hook denies a root's lane dispatch
  whose brief does not open with the contract's header line. Both arm only in a session whose
  operator message is a loop launch ("You are the root" plus one closeout path), never in
  ordinary sessions, and both let the call through on any fault of their own.
- `bin/loop-lint`: the charter, queue and `LOOP.md` linter. While the plugin is enabled it is on
  the Bash tool's PATH, so the skill, the root and the lanes run it as `loop-lint`.
- `tools/dsreview/`: `dsreview`, a code reviewer for lanes. It sends a candidate change and its
  brief to DeepSeek Flash through Cloudflare AI Gateway, lets the model read the repository through
  read-only tools, and prints a PASS or FAIL verdict as JSON. On 59 replayed reviews it caught as
  many defects as Opus reviewer lanes (8 of 11 each) with no false blocks, at about $0.05 a review
  against about $1.09. It is optional and needs your own gateway: see `tools/dsreview/README.md`.
- `protocol/`: the contract, the three harness files, the charter and lane templates, the planner
  and the board setup guide. See `protocol/README.md`.

## Requirements

- Claude Code with plugin support (tested with 2.1.303). The skill relies on Claude Code
  substituting `${CLAUDE_PLUGIN_ROOT}` in skill text, and `loop-lint` relies on the plugin `bin/`
  directory being on the Bash tool's PATH.
- git, and Python 3.9 or later for the hooks and `loop-lint`.
- The Backlog.md CLI (`backlog`) and a board in the repository you run loops on. The root reads
  and updates tasks through it, and `loop-lint queue` checks every wave against it.
- Recommended: `just`, because the protocol's gate and verification examples are `just` recipes
  (any gate command works), and the GitHub CLI `gh` for the online `LOOP.md` checks
  (`loop-lint loopmd --offline` skips them).
- Optional: `dsreview` (in this plugin, Python 3.12 or later and a Cloudflare AI Gateway with a
  DeepSeek key). The lane template asks lanes to run it on each candidate; set it up as its README
  says or delete that rule from your `LANE.md`.
- Optional: the CodeRabbit CLI. The lane template asks lanes to run it on each candidate; delete
  that rule from your `LANE.md` if you do not use it.

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
one skill, five agents and the hooks.

## Prepare a repository

1. Set up a Backlog.md board (`backlog init`) and give it some tasks.
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
   and `grafana-stack` names a telemetry stack or `none`. Inside Claude Code, check it with
   `loop-lint loopmd LOOP.md --offline`.

## Run a first loop

1. In the repository, run `/agent-workflows:loop`. The skill reads the planner and the board, asks
   you every decision the first wave needs in one round, then writes
   `codex/charter-<date>-loop1.md`, `codex/loop1/LANE.md` and `codex/launch-<date>-loop1.txt`,
   lints them and gives you the launch file's path.
2. Start a fresh session with the command on the launch file's `cmd:` line (for example
   `claude --model claude-opus-5-5 --effort medium`) and paste the rest of the file as the first
   message. That message arms the hooks.
3. The root works the queue until it writes `codex/closeout-<date>-loop1.md` and replies with its
   path. For the next run, pass that closeout to the skill: `/agent-workflows:loop <closeout path>`.

The launch file names the contract and the harness file by absolute path inside the installed
plugin. Claude Code keeps a previous plugin version on disk for 14 days after an update, so prepare
and launch a run in the same sitting.

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
- Tools from the author's own setup: syncing `codex/` between machines, the session catalogue that
  reports a run's usage, the watchdog and scheduled verification. The protocol mentions some of
  them; skip those steps.

## Development

`just check` runs the syntax checks and the tests in `tests/`, which drive the hooks, the launch
template and `loop-lint` against temporary repositories, and `dsreview` against a local fake
gateway. `claude plugin validate .` checks the
manifests.

## Where this comes from

This repository is build output. It is generated from private source repositories, which stay the
source of truth, so each export replaces the previous tree. Issues are welcome. Pull requests are
read, and an accepted change is re-applied upstream and arrives here in a later export rather than
being merged.

## Licence

Apache-2.0. See `LICENSE`.
