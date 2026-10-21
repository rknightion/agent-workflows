# Harness: Claude Code (v3)

Read at launch with the contract and the charter.

## Launch

The launch file's first line is the command; the message follows it:

    cmd: claude --model claude-opus-5-5 --effort medium
    You are the root. Read <plugin root>/protocol/loop/contract.md, <plugin root>/protocol/loop/harness-claude.md
    and <abs charter path> in full. Write <abs closeout path> last, then reconcile and reply (contract section 12).
    Standing: <abs path> sha256=<hex>
    <each charter authority: or live: line touching customer data or a live system, verbatim>

- The root runs Opus 5.5 at medium unless the charter's `root:` says otherwise.
- The `agent-workflows` Stop hook arms on "You are the root" plus exactly one
  `codex/closeout-<date>-loop<N>.md` path, or a bare path to a launch file holding them. It lets the
  turn end only when that closeout counts (contract section 12, step 4) or the last line is a current
  `PAUSED:` or `WAITING: ... until <UTC>` line; otherwise it re-prompts up to three times in a chain.
- Nothing wakes an owner-only wait. Before ending that turn, start one background command that
  sleeps until its `WAITING` deadline, so the session wakes then (contract section 12).

## Permissions and owner questions

- A lane cannot clear an auto-mode permission block the root could, so SSH, deploys, tenant or
  cloud mutations, secret-store writes and destructive git stay with the root. Running the
  charter's gate route (a `just` gate recipe, even one that reaches a build host) is lane work, not
  SSH.
- The auto-mode classifier weighs only owner messages, never the charter file. The launch message
  therefore repeats each charter `authority:` and `live:` line that touches customer data or a live
  system, verbatim, after `Standing:`. Paste the message text, not a bare launch-file path, when it
  carries such lines.
- `AskUserQuestion` blocks the session: call it only as contract section 7 allows, never in
  overnight mode, and reconcile (section 12) as soon as it returns.

## Routes

| Role | `subagent_type` | Model / effort |
|---|---|---|
| implementation (default) | `agent-workflows:worker` | Sonnet 5.5 / high |
| judgement, unknown cause, seams, compose and board batches | `agent-workflows:senior` | Opus 5.5 / medium |
| escalation (tripwire or owner-named) | `agent-workflows:senior-escalation` | Opus 5.5 / xhigh |
| review and security review | `agent-workflows:reviewer` | Opus 5.5 / high |
| read-only mapping, sweeps, wave drafts, log reduction | `agent-workflows:scout` | Sonnet 5.5 / medium |

- Pass no `model` to these agents: it overrides the pin. Give each dispatch a `description` of
  `<lane> <task>`.
- Every role keeps a one-hour prompt cache, so a lane can wait on a gate slot without rewriting
  its context.
- If an `agent-workflows:*` type is missing, the plugin is not installed: stop and say so in chat.

## Dispatch and repairs

- Dispatch unnamed, in the background. Never `subagent_type: "fork"` for a lane.
- The `agent-workflows` PreToolUse hook denies an armed root's `Agent` call unless the prompt's first
  non-empty line is the brief header (contract section 3). Fix the header and dispatch again.
- At most 30 running subagents (`CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`); the 31st fails with
  `Concurrent subagent limit reached`. Wait for a return instead of retrying. Keep about 7 free when
  lanes may delegate; a brief says whether a lane may.
- A lane's completion arrives as a task notification. Repair rounds and answers go to the same lane
  with `SendMessage` to its agent id, carrying the new evidence.
- `TaskStop` a lane once its task lands or parks, so it stops counting against the cap.
- Make lane worktrees yourself (`git worktree add`); `isolation: "worktree"` branches from the
  default branch, not your base. Keep your own shell at the repository checkout: your working
  directory persists between Bash calls and every lane you dispatch inherits it. Never `cd` into a
  worktree; use `git -C <worktree>` and absolute paths, or a subshell `(cd <worktree> && ...)`. If
  `pwd` is not the checkout, `cd` back before the next dispatch. Lanes never call `EnterWorktree`.
- A lane's hand-back is one-shot. After a resume, its next return may arrive as an agent message
  instead of a task notification: take the `lane-return` block in it as the return. If a resumed
  lane goes idle with neither, read its `return.md`.

## Waits

- Your own waits (main CI, a release, a gate you run) are one `run_in_background` Bash command
  whose `until` loop exits on every terminal state; its completion wakes you. Streamed events use
  `Monitor`, re-armed when it expires.
- `LOOP.md`'s optional `ci-status` command replaces `gh` reads: substitute the full SHA for `{sha}`,
  sleep 60 s between reads, stop on `success` or `failure`, bound the loop by `ci-max-minutes`
  (default 30).
- A composed gate's legs (contract section 6): after the `git status --porcelain` check, start
  each leg as its own `run_in_background` Bash command against the compose worktree by absolute
  path, one log per leg. If the classifier refuses the fan-out, record the denial, run the legs
  one after another and say so in the return or `DECISIONS.md`.
- Bash calls time out at 10 minutes: run anything longer in the background. Never a foreground
  `sleep` over 60 seconds.

## Turn endings

```text
A message with no tool call ends your turn, and the loop stops there until the operator returns.
Do not end a turn with a summary that announces the next step without taking it, an offer to carry
on, a list of decisions none of which blocks the remaining work, or a decision that this is a good
place to stop. Put status notes in the same message as your next tool call. End a turn only when the
closeout is written, when paused, or when a running background command, monitor or lane will wake
this session; when waiting, make the last line `WAITING: <what> until <YYYY-MM-DDTHH:MM[:SS]Z>`;
when paused, `PAUSED: <reason>`. Never TaskStop a lane mid-mutation.
```
