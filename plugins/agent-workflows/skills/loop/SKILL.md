---
name: loop
description: Prepare a Loop v3 run: charter, lane rules and launch file, linted, for the owner to launch.
---

# loop: prepare a v3 run

Invoke `/agent-workflows:loop [last-closeout-path]`. Prepare the files and hand over the launch
path. Launch only if the owner asks; never spawn a root.

## Sources

Read these each time and never restate them here:

- `${CLAUDE_PLUGIN_ROOT}/protocol/loop/planner.md`: the preparation steps. Follow it.
- `${CLAUDE_PLUGIN_ROOT}/protocol/loop/charter.md`, `lane.md`, `contract.md` (section 1 names the files)
  and the chosen `harness-<name>.md` (launch block, routes): `harness-pi.md`, `harness-claude.md`
  or `harness-codex.md`.
- The repository's `LOOP.md` (tier, gate, guards, traps) and its `## Lane brief`.
- Tools: `loop-lint`, on the Bash tool's PATH while this plugin is enabled
  (`${CLAUDE_PLUGIN_ROOT}/bin/loop-lint`). `loop-lint queue` reads the board through `backlog`.
- Claude Code writes this plugin's absolute install path into every path above when it loads
  this skill. Write those absolute paths into the run files exactly as they appear here. If a
  protocol path above does not start with `/`, the path was not substituted: stop and say so.
- A repository with no gitignored `codex/` has no loop campaign: stop and say so.

## Flow

1. **Last run.** If you keep `codex/` on more than one machine, sync it first.
   Read the last closeout's `## Parked` and
   `## For ratification`; the owner's answers become charter decisions. N is one more than the
   highest `loop<N>` in `codex/`.
2. **Read the board.** `backlog task list --plain` and the owner's stated aim. Rank ready work by the
   owner's priorities; exclude and name any task that needs an owner choice nobody has made.
3. **First wave.** Up to about 20 rows of task, scope and route. `worker` is the default; `senior`
   only with its reason (unknown cause, frozen seam, data-model call, composing a batch); `scout` for
   read-only mapping and sweeps. Many small file-disjoint tasks; note an overlap, do not hold work
   back for it. Pre-assign migration numbers and registry ids.
4. **Decisions.** Put every owner choice the first wave needs in ONE batched `AskUserQuestion`, each
   with your recommendation and alternatives. Freeze the answers under `## Decisions already made`.
   Engineering detail inside approved scope is not an owner question.
5. **Harness.** The owner names it; otherwise `claude`. This plugin supplies the Claude Code
   hooks and lane agents; the `codex` and `pi` harnesses need their own
   launchers and roles. Copy the root model and launch command from the harness file; change
   them only on the owner's instruction.
6. **Standing.** Standing grants are optional and none by default. With none, write
   `codex/standing.md` holding the one line `No standing grants.`; if the owner keeps a Standing
   file of per-repository grants, use its absolute path, and take a new or changed entry to the
   owner first. Hash the file with `shasum -a 256` and put the path and hash in the charter and
   the launch file.
   **Landing mode.** Apply the planner's landing checks before writing files: `LOOP.md` selects
   `reviewer: coderabbit` (default) or `reviewer: copilot`. Direct local-pair instead declares
   `review: local-pair` and `landing-branch: <branch>`, with no `reviewer:`; follow the canonical
   planner's exact-repo authority, branch-rule and local-review proof requirements.
   Run `loop-lint loopmd <abs LOOP.md>`;
   for PR landings, carry the selected profile's required authority and checks into the charter and,
   where required, the launch. Copilot activation needs observed sandbox lifecycle evidence;
   settings alone do not satisfy the planner's check.
7. **Write the files**, `<date>` being the UTC preparation date:
   - `codex/charter-<date>-loop<N>.md` from `charter.md`, under 8 KB;
   - `codex/loop<N>/LANE.md` from `lane.md` with its `<...>` slots filled, then `LOOP.md`'s
     `## Lane brief` appended verbatim;
   - `codex/launch-<date>-loop<N>.txt` from the harness file's launch block.
8. **Lint.** `loop-lint charter <charter>`, then `loop-lint queue <charter> <first wave as QUEUE.md>`,
   until both are clean. Never edit the linter.
9. **Deliver** the absolute launch path and a short summary: first-wave count, exclusions, decisions
   frozen, anything parked. Write every task as `ID (plain title)`.

## Launch file

For Codex, tell the owner to run the command after `cmd:` in a shell to start a fresh session and
paste only the following lines into it. The `cmd:` line is shell launch metadata, not prompt text:
pasting it into an already-running session changes neither its model nor its reasoning effort.
Include this instruction when handing over the launch path.

```text
cmd: <launcher command from the harness file>
You are the root. Read ${CLAUDE_PLUGIN_ROOT}/protocol/loop/contract.md, ${CLAUDE_PLUGIN_ROOT}/protocol/loop/harness-<name>.md
and <abs repo>/codex/charter-<date>-loop<N>.md in full. Write <abs repo>/codex/closeout-<date>-loop<N>.md as the terminal action.
Standing: <abs standing path> sha256=<hex>
```

Name the closeout path once, absolute, with no trailing period. "You are the root" plus that one path
is how every hook recognises the run.
