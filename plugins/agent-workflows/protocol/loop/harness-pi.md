# Harness: pi (v3)

Read at launch with the contract and the charter.

## Runtime and launch

- Models: codex-lb `gpt-6.1-sol`, `gpt-6-astra` only; the guard blocks model, thinking and
  run-deadline overrides.
- Launch with the context's loop-pi launcher. The message names the contract, this file, the
  charter, the closeout path and the `Standing:` file with its sha256:

      You are the root. Read <plugin root>/protocol/loop/contract.md, <plugin root>/protocol/loop/harness-pi.md
      and <abs charter path> in full. Write <abs closeout path> last, then reconcile and reply (contract section 12).
      Standing: <abs path> sha256=<hex>

- Launch runs the preflight and snapshots the remote for the audit. A cross-repo charter
  needs `loop-pi-preflight <repo>` and `loop-pi-audit add <repo>` first.
- Continuation arms on "You are the root" plus one closeout path. A wrong host or repository, a
  changed charter, or a `Standing:` line that differs from the charter's `standing:` key refuses the
  launch. A Standing file that is missing or fails its sha256 still arms, with no grants and a
  `loop-standing-rejected` incident.
- **Recovery is automatic.** After a provider error, a stalled request or a request-ceiling abort,
  continuation resumes the root. A quota error gets two retries (owner input resets), then
  stops; authentication, policy and context-length errors stop at once. Never relaunch yourself.
- The loop-state extension records dispatch, return, watch and heartbeat events in
  `codex/state-<date>-loop<N>.jsonl` automatically. You write none of them; `loop-state digest
  <log> --json` gives live lanes. The state log and lane-patch capture parse the brief's first
  non-empty line, `Lane: <id> · Task: <ID> (<title>) · Tier: routine|guarded` (no `·` inside the
  title); a header that does not parse drops that lane from both.

## Root load

- `LANES.md` stays the contract's one line per live lane: add it with one `printf '%s\n' ... >>` at
  dispatch and delete it at land or park, in a `bash` call you already make (never one running
  `loop-gate-lock`). Never rewrite it or add status prose; `DECISIONS.md` is append-only. In
  `QUEUE.md` move rows between sections in place.
- Combine the routine mandatory section 5 reviews since the last one into one `reviewer` lane
  (`Task: none (land review)`) after landing; guarded reviews stay before landing, with the
  security brief. Edit the run `LANE.md` CodeRabbit line so task lanes skip it; the root, or the
  compose lane its brief names, runs it once on each landing's composed tree.

## Routes

| Role | Agent | Model / thinking |
|---|---|---|
| implementation (default) | `worker` | sol / medium |
| judgement, compose, board batches | `senior` | sol / high |
| escalation (tripwire or owner-named) | `senior-escalation` | astra / high |
| review and security review | `reviewer` | sol / high |
| read-only mapping, sweeps, wave drafts | `scout` | sol / low |

A missing agent is a harness fault: stop and say so.

## Dispatch and repairs

- Every `worker`, `senior` and `senior-escalation` lane: async
  `subagent {agent, task, worktree: true}`, the complete brief as `task`, with
  `Landing: returns candidate` on its own line (the tree is kept only then). loop-pi keeps the tree
  and runs the lane in it, so a bare `loop-gate-lock <name>` gates it (`cd … &&` is refused).
  Scout, reviewer: `subagent {agent, task}`. No `workflow`, no model or thinking override.
- 19 native async slots shared by parents and children; keep about 7 free. Only `senior` may spawn
  children (`scout`, `worker`, `reviewer`), and only when the brief grants a count. Depth 2 at most.
- Repair rounds and answers resume the same lane's run with new evidence (`subagent` `resume`, which
  keeps its tree). Never poll a lane's status between wakes.
- **Lanes never push** (the guard refuses it); only the root's `bash` runs `git push origin main`.
- A new `worktree: true` lane needs a clean main checkout (ignored files are fine). `/loop-closeout`
  removes only verified clean landed trees; lane patches go to `$LOOP_PI_RUN_DIR/lane-patches/`.

## Waits

- Wakes: a lane return, a `watch_start` exit or a `wake_at` timer. Process every return, then end
  with `WAITING: <what> until <deadline>`; continuation arms a timer for it.
- Root CI and release waits: a bounded, quiet `watch_start` that exits on every terminal state.
  Lanes use `watch_process` with `deadline_s` up to 3600.
- `LOOP.md`'s optional `ci-status` replaces `gh` reads.
- Heavy gates run only through `loop-gate-lock <name>` rows under `LOOP.md` `## Mutexes`. One name
  runs one leg at a time, so a composed gate's parallel legs (contract section 6) need a row each,
  each started under its own watcher. If one is refused, run the legs in turn and say so.
- Give any `bash` call that can run past 15 minutes a `timeout`, or run it under `watch_start`.

## Owner questions

While the owner is present, ask in a message that also makes a tool call and append the question to
`QUESTIONS.md`. An idle-lane wake notice (`Inspect subagent updates above.`) is not an owner control.

## Closeout sweep

1. Stop async `subagent` runs; list any that will not stop.
2. Push landed work with the root's `bash` (`git push origin main`).
3. Stop your watchers (`watch_stop`) and timers (`wake_cancel`), then run
   `loop-pi-audit closeout --run-dir "$LOOP_PI_RUN_DIR" --push-log "$LOOP_PI_RUN_DIR/push-log.jsonl"`
   and report a nonzero result under `## Notes`, naming each flagged move the charter authorised.
   Remove no lane tree yourself: list them under `## Notes` and ask the owner to run
   `/loop-closeout`, which you cannot call.
4. Write the closeout file, then run the contract's reconcile and reply (section 12, steps 5 and 6).

## Turn endings

```text
A message with no tool call ends your turn. Never end a turn with a summary that announces the next
step, an offer to carry on, a list of decisions none of which blocks the remaining work, or a
decision that this is a good place to stop. End a turn only when the closeout is written, when
paused, or when a lane, a watch_start watcher or a wake_at timer will wake this session; when
waiting, arm wake_at and make the last line `WAITING: <what> until <YYYY-MM-DDTHH:MM[:SS]Z>`; when
paused, `PAUSED: <reason>`. Launch each lane as its own subagent call.
```
