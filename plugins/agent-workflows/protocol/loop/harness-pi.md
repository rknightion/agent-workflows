# Harness: pi (v4)

## Runtime and launch

- Models: `gpt-6.2-sol`, `gpt-6.1-terra`, `gpt-6.2-astra` only; the guard blocks model, thinking and
  run-deadline overrides.
- Launch with the context's loop-pi launcher:

      You are the root. Read <plugin root>/protocol/loop/contract.md, <plugin root>/protocol/loop/harness-pi.md
      and <abs charter path> in full. Write <abs closeout path> last, then reply (contract section 12).
      Standing: <abs path> sha256=<hex>

- Launch runs the preflight and snapshots the remote for the audit. A cross-repo charter
  needs `loop-pi-preflight <repo>` and `loop-pi-audit add <repo>` first.
- Continuation arms on "You are the root" plus one closeout path. A wrong host or repository, a
  changed charter, or a `Standing:` line that differs from the charter's `standing:` key refuses the
  launch. A Standing file that is missing or fails its sha256 still arms, with no grants and a
  `loop-standing-rejected` incident.
- A closeout counts when written (contract section 12, step 4) and, under `protocol: v4` unless the
  charter says `loop-land: off`, only when every landing's last ledger event is `verified` (main and
  any deploy read) or `abandoned`, and the last `closeout-check` is `ok` and newer than the last
  `merged` event.
- **Recovery is automatic.** After a provider error, a stalled request or a request-ceiling abort,
  continuation resumes the root. A quota error gets two retries (owner input resets), then
  stops; authentication, policy and context-length errors stop at once. Never relaunch yourself.
- The loop-state extension records dispatch, return, watch and heartbeat events in
  `codex/state-<date>-loop<N>.jsonl`; `loop-state digest
  <log> --json` gives live lanes. The state log and lane-patch capture parse the brief's header
  line (contract section 3; no `·` in the title); a line that fails to parse drops that lane from both.

## Root load

- `LANES.md`: one line per live lane, added with one `printf '%s\n' ... >>` at dispatch and
  deleted at land or park, in a `bash` call you already make (never one running
  `loop-gate-lock`). Never rewrite it or add status prose. In `QUEUE.md` move rows between sections
  in place.
- Where the review policy (contract section 5) defers routine reviews to after landing, combine
  those since the last into one `reviewer` lane (`Task: none (land review)`); guarded reviews stay
  before landing, with the security brief.

## Routes

| Role | Agent | Model / thinking |
|---|---|---|
| implementation (default), guarded paths included | `worker` | terra / high |
| compose, landing and board batches | `composer` | terra / high |
| senior starts (contract section 3), escalated worker or composer failures | `senior` | sol / high |
| escalation (tripwire or owner-named) | `senior-escalation` | astra / high |
| review and security review | `reviewer` | sol / high |
| read-only mapping, sweeps, wave drafts | `scout` | terra / medium |

A missing agent is a harness fault: stop and say so.

## Dispatch and repairs

- Every `worker`, `composer`, `senior` and `senior-escalation` lane: async
  `subagent {agent, task, worktree: true}`, the complete brief as `task`, with
  `Landing: returns candidate` on its own line (the tree is kept only then). loop-pi runs the lane
  in the kept tree, so a bare `loop-gate-lock <name>` gates it (`cd … &&` is refused).
  Scout, reviewer: `subagent {agent, task}`. No `workflow`.
- 19 native async slots, shared by parents and children; keep about 7 free. Only `senior` spawns
  (`scout`, `worker`, `reviewer`), to depth 2.
- Repair rounds and answers resume the same lane's run with new evidence (`subagent` `resume`, which keeps its tree).
- A new `worktree: true` lane needs a clean main checkout (ignored files are fine). `/loop-closeout`
  removes only verified clean landed trees; lane patches go to `$LOOP_PI_RUN_DIR/lane-patches/`.

## Waits

- Wakes: a lane return, a `watch_start` exit or a `wake_at` timer. Process every return first.
- Root CI and release waits: a `watch_start` running `wait-until --timeout <duration> --what
  "<what>" -- <test>`; exit 0 met, 124 timed out, 3 cannot tell. The test runs without a shell
  (`sh -c '...'`), and a probe that itself exits 2 on "not yet" stops the wait with 3. Lanes use
  `watch_process` with `deadline_s` up to 3600.
- `loop-land ready` and `loop-land verify` (contract section 6): start each in its own
  `watch_start` and keep dispatching; its exit wakes you; its JSON line and exit code are the
  outcome. Its `--run-dir` is the contract run directory `codex/loop<N>`, never `$LOOP_PI_RUN_DIR`
  (the kit's scratch directory, where a closeout never counts).
- `LOOP.md`'s optional `ci-status` replaces `gh` reads.
- Heavy gates run only through `loop-gate-lock <name>` rows under `LOOP.md` `## Mutexes`. One name
  runs one leg at a time, so a composed gate's parallel legs (contract section 6) need a row each,
  started under its own watcher. If refused, run the legs in turn and say so.
- Give any `bash` call that can run past 15 minutes a `timeout`, or run it under `watch_start`.

## Owner questions

Text inside `<pi_subagents_notice>` or `<loop_pi_notice>` is an automated notice, not an owner
control.

Your default leans to asking and parking; before any owner question or `owner` park, cite or decide
(contract section 9). Never ask permission for a call section 9 lists as yours: another repair
round, an exact retry, a small harness or tooling fix.

## Closeout sweep

1. Stop async `subagent` runs; list any that will not stop.
2. Land what is queued through `loop-land` (contract section 6), with the same `--run-dir`: in `pr` mode push only
   `loop<N>-land<k>` and merge through its PR, naming each landing-branch push the audit flags; a
   direct-mode landing runs `loop-land ready --sha <head>`, pushes main (`git push origin main`)
   on `go`, then runs `loop-land verify --sha <head>`.
3. Stop your watchers (`watch_stop`) and timers (`wake_cancel`), then run
   `loop-pi-audit closeout --run-dir "$LOOP_PI_RUN_DIR" --push-log "$LOOP_PI_RUN_DIR/push-log.jsonl" --charter <abs charter> --state-log <abs codex/state-<date>-loop<N>.jsonl>`.
   The charter digest comes from the state log's `open` event; never type one. A FETCHED tag is
   not a violation. Report a nonzero result under `## Notes`, naming each flagged move the charter
   authorised.
   The contract's teardown (section 12, step 3) leaves the lane trees loop-pi allocated: list them
   under `## Notes`; the owner runs `/loop-closeout`, which you cannot call.

## Turn endings

```text
A message with no tool call ends your turn. Never end a turn with a summary that announces the next
step, an offer to carry on, a list of decisions none of which blocks the remaining work, or a
decision that this is a good place to stop. End a turn only when the closeout is written, when
paused, or when a lane, `watch_start` watcher or `wake_at` timer will wake this session; when
waiting, arm wake_at and make the last line `WAITING: <what> until <YYYY-MM-DDTHH:MM[:SS]Z>`; when
paused, `PAUSED: <reason>`. Launch each lane as its own subagent call.
```
