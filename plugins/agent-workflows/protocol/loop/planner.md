# Loop preparation (v3)

Read by the `loop` skill only. Preparing a run should take minutes, not an hour.

1. **Reconcile.** If you keep `codex/` on more than one machine, sync it so it
   matches everywhere. Read the last closeout's `## Parked` and `## For ratification`; apply the owner's
   answers as charter decisions.
2. **Pick the number.** N is one more than the highest `loop<N>` in this repository's `codex/`.
3. **Read the board.** `backlog task list --plain` and the owner's stated aim. Rank ready work by the
   owner's priorities. Exclude tasks that need an owner choice nobody has made, and say which.
4. **First wave.** Up to about 20 rows: task, scope (files), route (`worker`, or `senior` with the
   reason), and any pre-assigned migration number or dependency. Prefer many small file-disjoint
   tasks. Do not hold back work because two tasks share a file; note the overlap instead.
5. **Front-load decisions.** Batch every owner choice the first wave needs into one prompt now and
   write the answers under `## Decisions already made`. Run the gate and the board's
   `definition_of_done` commands on base through the gate route (never a task's own `Done when:`
   check, which is red at base by design), or reuse the last closeout's result for the same base
   SHA. Record each one already red there, with its failing tests and the reason, under
   `## Decisions already made`. For a cross-repo run, name the target's gate and its mutex.
   **Gate route.** Read it from the repository's `AGENTS.md` now, never from the last charter. Where
   `AGENTS.md` names a remote gate runner (a committed runner contract), the route is that runner and
   its slot pacer, and a workstation run under a mutex is only the fallback for the runner failures
   `AGENTS.md` lists; write both into `gate-route`. Re-route an older `LOOP.md` gate row to match.
6. **Write the files** (contract section 1): the charter from
   `<plugin root>/protocol/loop/charter.md`, `codex/loop<N>/LANE.md` from
   `<plugin root>/protocol/loop/lane.md` plus `LOOP.md`'s `## Lane brief`, and the launch file
   from the harness file's launch block. Copy the Standing file's current sha256 into both. On a
   `claude` or `claude-openai` harness, also copy into the launch every `authority:` or `live:` line that touches
   customer data or a live system, verbatim.
7. **Check.** `loop-lint charter <charter>` and `loop-lint queue <charter> <first-wave as QUEUE.md>`
   until clean.
8. **Deliver** the launch file path and a short summary. The owner launches it.

Routes and model choices live in each harness file. The root model defaults to the harness file's
launch command; change it only on the owner's instruction.
