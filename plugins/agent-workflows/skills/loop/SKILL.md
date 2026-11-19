---
name: loop
description: Prepare a Loop v4 run: charter, lane rules and launch file, linted, for the owner to launch.
---

# loop: prepare a v4 run

Invoke `/agent-workflows:loop [last-closeout-path]`. Prepare the files and hand over the launch
path. Launch only if the owner asks; never spawn a root.

## Sources

Read these each time and never restate them here:

- `${CLAUDE_PLUGIN_ROOT}/protocol/loop/planner.md`: the v4 preparation steps. Follow it.
- `${CLAUDE_PLUGIN_ROOT}/protocol/loop/charter.md`, `lane.md`, `contract.md`
  and the chosen `harness-<name>.md` (launch block, routes): `harness-pi.md`, `harness-claude.md`
  or `harness-codex.md`.
- The repository's `LOOP.md` (tier, gate, `## Queue`) and its `## Lane brief`.
- Tools: `loop-lint`, on the Bash tool's PATH while this plugin is enabled
  (`${CLAUDE_PLUGIN_ROOT}/bin/loop-lint`). `loop-lint queue` reads the tracker the repository's `LOOP.md` names.
- Claude Code writes this plugin's absolute install path into every path above when it loads
  this skill. Write those absolute paths into the run files exactly as they appear here. If a
  protocol path above does not start with `/`, the path was not substituted: stop and say so.
- A repository with no gitignored `codex/` has no loop campaign: stop and say so.

## Flow

1. **Last run.** If you keep `codex/` on more than one machine, sync it first.
   Read the last closeout's `## Parked` and
   `## For ratification`; the owner's answers become charter decisions. Then planner step 1: the
   `loop-teardown reap` dry run and `loop-lint parked --repo <abs repo>`. Every parked task goes
   into the charter's `exclude:` unless its park named a blocking issue that is now closed; print the
   list for the owner, who alone re-admits one (under `## Decisions already made`). N is one more
   than the highest `loop<N>` in `codex/`.
2. **Read the tracker.** Through the repository's queue command, as planner step 3 says: `LOOP.md`'s
   `tracker:` picks it, and an absent key means Backlog only when the repository has a `backlog/`
   directory, else GitHub. Backlog: `backlog task list --plain`; GitHub: the command in its
   `## Queue`, never a raw `gh issue list`. Issues that live in another repository (`tracker-repo:`)
   make it GitHub: read that repository's queue with its label and `--repo`, and put the same
   `tracker-repo:` in the charter. Then the owner's aim. Rank ready work by the owner's
   priorities; exclude and name any task needing an owner choice nobody has made. Report ready depth
   against the `lanes:` cap; never invent work to fill it.
3. **First wave.** Up to about 20 rows of task, scope and route. `worker` is the default, guarded paths
   included; `senior` only for a shared-contract freeze, an unknown cause, a security or privacy call
   or a design return, with the `Why:` naming which; `composer` for compose, landing and board lanes;
   `scout` for read-only mapping and sweeps. Many small file-disjoint tasks; note an overlap, never hold work back
   for it. Pre-assign migration numbers and registry ids.
4. **Decisions.** Put every owner choice the first wave needs in ONE batched `AskUserQuestion`, each
   with your recommendation and alternatives. Ask in it which categories of work are not worth
   tonight's review cost, and list the privileged commands the run needs for the charter's
   `preflight:`. Freeze the answers under `## Decisions already made`. Engineering detail inside
   approved scope is not an owner question.
5. **Harness.** The owner names it; otherwise `claude`. This plugin supplies the Claude Code
   hooks and lane agents; the `codex` and `pi` harnesses need their own
   launchers and roles. Copy the root model and launch command from the harness file; change
   them only on the owner's instruction.
6. **Standing.** Standing grants are optional and none by default. With none, write
   `codex/standing.md` holding the one line `No standing grants.`; if the owner keeps a Standing
   file of per-repository grants, use its absolute path, and take a new or changed entry to the
   owner first. Hash the file with `shasum -a 256` and put the path and hash in the charter and
   the launch file.
   **Review and landing.** Apply planner step 5 before writing files. Landing mode follows contract
   section 6: a GitHub tracker lands by pull request, and only a Backlog repository with no `landing:`
   key lands `direct`. Freeze the exact local review command(s) in the lane rules (never from PATH), keep a declared local pair, run `loop-lint loopmd <abs LOOP.md>` and carry the
   required authority and live proof into the charter and launch. Missing proof parks activation.
7. **Write the files**, `<date>` being the UTC preparation date:
   - `codex/charter-<date>-loop<N>.md` from `charter.md`, with `protocol: v4`, `preflight:` and the
     parked `exclude:`, under 8 KB; `loop-land: off` only on the owner's say-so, never unasked;
   - `codex/loop<N>/LANE.md` from `lane.md` with its `<...>` slots filled, then `LOOP.md`'s
     `## Lane brief` appended verbatim;
   - `codex/launch-<date>-loop<N>.txt` from the harness file's launch block.
8. **Lint.** `loop-lint charter <charter>`, then `loop-lint queue <charter> <first wave as QUEUE.md>`,
   until both are clean. Never edit the linter.
9. **Deliver** the absolute launch path and a short summary: first-wave count, exclusions, decisions
   frozen, anything parked. Write every task as `ID (plain title)`.

## Launch file

For every harness, tell the owner to run the command after `cmd:` in a shell to start a fresh
session and paste only the following lines into it. The `cmd:` line is shell launch metadata, not
prompt text: pasting it into an already-running session changes neither its model nor its reasoning
effort, and on claude `--disallowedTools AskUserQuestion` takes effect only in a session started
with it. Hand this over with the launch path.

```text
cmd: <launcher command from the harness file; on claude it keeps --disallowedTools AskUserQuestion>
You are the root. Read ${CLAUDE_PLUGIN_ROOT}/protocol/loop/contract.md, ${CLAUDE_PLUGIN_ROOT}/protocol/loop/harness-<name>.md
and <abs repo>/codex/charter-<date>-loop<N>.md in full. Write <abs repo>/codex/closeout-<date>-loop<N>.md last, then reply (contract section 12).
Standing: <abs standing path> sha256=<hex>
<claude only: each charter `authority:` or `live:` line that touches customer data or a live system, verbatim>
```

Name the closeout path once, absolute, with no trailing period. "You are the root" plus that one path
is how every hook recognises the run. The Claude auto-mode classifier weighs only the owner's
messages, never the charter, so those lines go in the launch message itself; when it carries any,
tell the owner to paste the message text, not the launch-file path.
