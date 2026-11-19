# Harness: Codex (v4)

Read at launch with the contract and the charter.

## Launch

Run the command after `cmd:` in a shell to start a fresh session; paste only the following lines
into that session. Do not paste the `cmd:` line: it is not an instruction to an already-running
Codex session and does not change that session's model or reasoning effort.

    cmd: codex -m gpt-6.2-sol -c model_reasoning_effort=high
    You are the root. Read <plugin root>/protocol/loop/contract.md, <plugin root>/protocol/loop/harness-codex.md
    and <abs charter path> in full. Write <abs closeout path> last, then reply (contract section 12).
    Standing: <abs path> sha256=<hex>

- The `agent-workflows` Stop hook arms on "You are the root" plus one closeout path and releases a
  stop only for a counting closeout or a current `PAUSED:` line. **Nothing wakes a Codex root that
  ends its turn**, so `WAITING:` releases nothing: hold the turn with `wait_agent` until the
  closeout is written.
- A closeout counts when written (contract section 12, step 4) and, under `protocol: v4` unless the
  charter says `loop-land: off`, only when every landing's last ledger event is `verified` (main and
  any deploy read) or `abandoned`, and the last `closeout-check` is `ok` and newer than the last
  `merged` event.
- Start at 6 to 8 live lanes and raise the count only while your own model time stays under about
  half of wall time.

## Routes

| Role | `agent_type` | Model / effort |
|---|---|---|
| implementation (default), guarded paths included | `worker` | gpt-6.1-terra / high |
| compose, landing and board batches | `composer` | gpt-6.1-terra / high |
| senior starts (contract section 3), escalated worker or composer failures | `senior` | gpt-6.2-sol / high |
| escalation (tripwire or owner-named) | `senior-escalation` | gpt-6.2-astra / high |
| review and security review (read-only sandbox) | `reviewer` | gpt-6.2-sol / high |
| read-only mapping, sweeps, single waits | `scout` | gpt-6.1-terra / medium |

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
- Your default leans to asking and parking; correct for it. Before any owner question or `owner`
  park, apply cite or decide (contract section 9): name the clause that reserves the call, or rule
  it for ratification and continue. Never ask permission for a call section 9 lists as yours:
  another repair round, an exact retry, a small harness or tooling fix.
- Never conclude dispatch is unavailable without a recorded `spawn_agent` error. A thread-pool
  limit means wait for a return, then retry.

## Waits

- With lanes in flight and no local work, make one `wait_agent` call (up to 19 minutes).
  **Returns arrive in context, not in the wait**: process every return already in context before
  each wait. After compaction, reconcile `list_agents` against `LANES.md` before the first wait.
- A process backgrounded with `&`, `nohup` or `disown` dies with its exec call. Start every watcher
  and every `loop-land` run as a foreground `exec_command` with `yield_time_ms` 1000 and record the
  `session_id`.
- `loop-land ready` and `loop-land verify` (contract section 6) are long waits and nothing wakes a
  Codex root, so run each as its own `exec_command` session and keep dispatching. Read the session
  with a bounded `write_stdin` poll each time a lane returns or a wait ends, and before each
  `wait_agent`, and with no lane live hold the turn on it; its exit code and JSON line are the
  outcome. Start no second `ready` or `verify` while one session is still running.
- For an owner-only wait with no live lanes or ready work, `wait_agent` has no child to wait on:
  do not spawn one. Record `WAITING: <owner decision> until <UTC deadline>`, start an
  `exec_command` session running
  `wait-until --timeout <time to the deadline> --what "<owner decision>" --every 300 -- false`, and
  hold the turn with bounded `write_stdin` calls on it until it exits; never end the turn expecting
  it to wake a stopped root. On an earlier owner reply, stop it and reconcile. At expiry reconcile
  answers, ready work and the live listing: if none arrived, park the waiting items as `owner` and
  close out (contract section 12).
- A root with lanes in flight cannot also wait in an exec cell. Hand each process wait (main CI, a
  release) to a `scout` with the exact `wait-until` command and what each exit means; its return
  quotes the raw readback. Exit 124 means "not observed" and 3 "could not tell".
- Lanes wait on their own processes with a self-polling exec cell (`yield_time_ms` up to 1140000)
  running one quiet `wait-until` that exits on the terminal state. The test runs without a shell, so
  shell syntax needs `sh -c '...'`, and a probe that itself exits 2 on "not yet" stops the wait
  with 3.
- `LOOP.md`'s optional `ci-status` replaces `gh` reads: substitute the full SHA for `{sha}` and
  wrap it in `sh -c` so the test exits 0 on `success` or `failure`; `--every 60`, and `--timeout`
  from `ci-max-minutes` (default 30).
- A composed gate's legs (contract section 6) start the same way, one `exec_command` per leg. If
  one is refused, run the legs in turn and say so.

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
