# Harness: Claude Code (v4)

Read at launch with the contract and the charter.

## Launch

The launch file's first line is the command; the message follows it:

    cmd: claude --model claude-opus-5-5 --effort medium --disallowedTools AskUserQuestion
    You are the root. Read <plugin root>/protocol/loop/contract.md, <plugin root>/protocol/loop/harness-claude.md
    and <abs charter path> in full. Write <abs closeout path> last, then reply (contract section 12).
    Standing: <abs path> sha256=<hex>
    <each charter authority: or live: line touching customer data or a live system, verbatim>

- The root runs Opus 5.5 at medium unless the charter's `root:` says otherwise.
- The `agent-workflows` Stop hook arms on "You are the root" plus exactly one
  `codex/closeout-<date>-loop<N>.md` path, or a bare path to a launch file holding them. It lets the
  turn end only when that closeout counts or the last line is a current `PAUSED:` or
  `WAITING: ... until <UTC>` line; otherwise it re-prompts up to three times in a chain.
- A closeout counts when written (contract section 12, step 4) and, under `protocol: v4` unless the
  charter says `loop-land: off`, only when every landing's last ledger event is `verified` (main and
  any deploy read) or `abandoned`, and the last `closeout-check` is `ok` and newer than the last
  `merged` event.
- Nothing wakes an owner-only wait. Before ending that turn, start one `run_in_background`
  `wait-until --timeout <time to the WAITING deadline> --what "<owner decision>" --every 300 -- false`;
  its exit at the deadline wakes the session.

## Permissions and owner questions

- A lane cannot clear an auto-mode permission block the root could, so SSH, deploys, tenant or
  cloud mutations, secret-store writes and destructive git stay with the root. A `just` gate recipe,
  even one that reaches a build host, is lane work, not SSH.
- The auto-mode classifier weighs only owner messages, never the charter file. The launch message
  therefore repeats each charter `authority:` and `live:` line that touches customer data or a live
  system, verbatim, after `Standing:`. Paste the message text, not a bare launch-file path, when it
  carries such lines.
- `AskUserQuestion` is disallowed at launch because it blocks the session (contract section 7).
  While the owner is present, ask in a message that also makes a tool call and append the question
  to `QUESTIONS.md`.

## Routes

| Role | `subagent_type` | Model / effort |
|---|---|---|
| implementation (default), guarded paths included | `agent-workflows:worker` | Sonnet 5.5 / high |
| compose, landing and board batches | `agent-workflows:composer` | Sonnet 5.5 / high |
| senior starts (contract section 3), escalated worker or composer failures | `agent-workflows:senior` | Opus 5.5 / medium |
| escalation (tripwire or owner-named) | `agent-workflows:senior-escalation` | Opus 5.5 / xhigh; Personal: Fable 5.2 / medium |
| review and security review | `agent-workflows:reviewer` | Opus 5.5 / high |
| read-only mapping, sweeps, wave drafts, log reduction | `agent-workflows:scout` | Sonnet 5.5 / medium |

- Pass no `model` to these agents: it overrides the pin. Give each dispatch a `description` of
  `<lane> <task>`.
- The one exception: in a Personal run (`AGENT_CONTEXT=personal`), dispatch
  `senior-escalation` with `model: "fable"` and `effort: "medium"`.
  Any other context has no Fable: pass neither there.
- If an `agent-workflows:*` type is missing, the plugin is not installed: stop and say so in chat.

## Dispatch and repairs

- Dispatch unnamed, in the background. Never `subagent_type: "fork"` for a lane.
- The `agent-workflows` PreToolUse hook denies an armed root's `Agent` call unless the prompt's first
  non-empty line is the brief header (contract section 3).
- At most 30 running subagents (`CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`); the 31st fails with
  `Concurrent subagent limit reached`. Wait for a return instead of retrying. Keep about 7 free when
  lanes may delegate.
- A lane's completion arrives as a task notification. Repairs and answers go by `SendMessage`
  to the lane's agent id, with the new evidence.
- `TaskStop` a lane once its task lands or parks.
- Make lane worktrees yourself (`git worktree add`); `isolation: "worktree"` branches from the
  default branch, not your base. Keep your own shell at the repository checkout: its working
  directory persists between Bash calls and every lane inherits it. Never `cd` into a
  worktree; use `git -C <worktree>` and absolute paths, or a subshell `(cd <worktree> && ...)`. If
  `pwd` is not the checkout, `cd` back before the next dispatch.
- A lane's hand-back is one-shot. After a resume, its next return may arrive as an agent message
  instead of a task notification: take the `lane-return` block in it as the return. If a resumed
  writing role goes idle with neither, read its `return.md`; a read-only role writes none, so
  `SendMessage` it for its final message.

## Waits

- Your own waits (main CI, a release, a gate you run) are one `run_in_background` Bash command,
  `wait-until --timeout <duration> --what "<what>" -- <test>`; its completion wakes you. Exit 0 is
  met, 124 timed out, 3 cannot tell. The test runs without a shell, so shell
  syntax needs `sh -c '...'`, and a probe that itself exits 2 on "not yet" stops the wait with 3.
  Streamed events use `Monitor`, re-armed when it expires.
- `loop-land ready` and `loop-land verify` (contract section 6) are long waits: start each as its
  own `run_in_background` command and keep dispatching; its exit wakes you, and its JSON line and
  exit code are the outcome.
- In `pr` mode, the selected profile's wait (contract section 6) is a background `wait-until`: 3m
  on its review-started read, then 17m on its done read (20m a head in all), both on the exact head.
- `LOOP.md`'s optional `ci-status` command replaces `gh` reads: substitute the full SHA for `{sha}`
  and wrap it in `sh -c` so the test exits 0 on `success` or `failure`; `--every 60`, and
  `--timeout` from `ci-max-minutes` (default 30).
- A composed gate's legs (contract section 6): start each as its own `run_in_background` Bash
  command against the compose worktree by absolute path, one log per leg. If the classifier refuses
  the fan-out, record the denial, run the legs one after another and say so in the return or
  `DECISIONS.md`.
- Bash calls time out at 10 minutes: run anything longer in the background.

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
