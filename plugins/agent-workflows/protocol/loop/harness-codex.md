# Harness: Codex (v3)

Read at launch with the contract and the charter.

## Launch

    cmd: codex -m gpt-6.1-sol -c model_reasoning_effort=high
    You are the root. Read <plugin root>/protocol/loop/contract.md, <plugin root>/protocol/loop/harness-codex.md
    and <abs charter path> in full. Write <abs closeout path> last, then reconcile and reply (contract section 12).
    Standing: <abs path> sha256=<hex>

- The `agent-workflows` Stop hook arms on "You are the root" plus one closeout path and releases a
  stop only for a counting closeout or a current `PAUSED:` line. **Nothing wakes a Codex root that
  ends its turn**, so `WAITING:` releases nothing: hold the turn with `wait_agent` until the
  closeout is written.
- Start at 6 to 8 live lanes and raise the count only while your own model time stays under about
  half of wall time.

## Routes

| Role | `agent_type` | Model / effort |
|---|---|---|
| implementation (default) | `worker` | gpt-6.1-sol / medium |
| judgement, compose, board batches | `senior` | gpt-6.1-sol / high |
| escalation (tripwire or owner-named) | `senior-escalation` | gpt-6-astra / high |
| review and security review (read-only sandbox) | `reviewer` | gpt-6.1-sol / high |
| read-only mapping, sweeps, single waits | `scout` | gpt-6.1-sol / low |

- Spawn these by `agent_type` and pass no model or effort. A spawn without a matching installed
  agent is a harness fault: stop and say so.
- `fork_turns: "none"` with the complete brief for every lane. 19 child threads plus the root; keep
  about 7 free when lanes may delegate, and say in each brief whether it may.

## Dispatch, repairs and questions

- `spawn_agent`, `wait_agent`, `list_agents`, `followup_task`, `send_message` and `interrupt_agent`
  are direct calls, never inside an exec cell.
- Prepare a lane's worktree in one exec cell; the next call is the `spawn_agent`.
- Repair rounds and answers go to the same lane with `followup_task`, carrying the new evidence.
- Owner questions while the owner is present: `request_user_input_async` for a real decision only,
  plus a `QUESTIONS.md` entry. Never prompt for something that needs no decision.
- Never conclude dispatch is unavailable without a recorded `spawn_agent` error. A thread-pool
  limit means wait for a return, then retry.

## Waits

- With lanes in flight and no local work, make one `wait_agent` call (up to 19 minutes).
  **Returns arrive in context, not in the wait**: process every return already in context before
  each wait. After compaction, reconcile `list_agents` against `LANES.md` before the first wait.
- A root with lanes in flight cannot also wait in an exec cell. Hand each process wait (main CI, a
  release) to a `scout` with the exact identity, terminal states and a deadline; its return quotes
  the raw readback. A deadline exit means "not observed".
- Lanes wait on their own processes with a self-polling exec cell (`yield_time_ms` up to 1140000)
  running one quiet command that exits on the terminal state.
- `LOOP.md`'s optional `ci-status` replaces `gh` reads exactly as the contract's lanes use it.
- A process backgrounded with `&`, `nohup` or `disown` dies with its exec call: start watchers as a
  foreground `exec_command` with `yield_time_ms` 1000 and record the `session_id`.
- A composed gate's legs (contract section 6) start the same way, one `exec_command` per leg after
  the `git status --porcelain` check. If one is refused, run the legs in turn and say so.

## Closeout sweep

Send `write_stdin` `"\u0003"` to every watcher session still running and `interrupt_agent` every
live lane; list both under `## Notes`.

## Turn endings

```text
A message with no tool call ends your turn and the loop stops until the operator returns. Never end
a turn with a summary that announces the next step, an offer to carry on, a list of decisions none
of which blocks the remaining work, or a decision that this is a good place to stop. Hold the turn
with wait_agent while lanes run. End a turn only when the closeout is written or when paused
(`PAUSED: <reason>` as the last line).
```
