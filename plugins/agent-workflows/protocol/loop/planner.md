# Loop preparation (v3)

Read by the `loop` skill only. Preparing a run should take minutes, not an hour.

1. **Last run.** Read the last closeout's `## Parked` and `## For ratification`; apply the owner's
   answers as charter decisions. Run `<plugin root>/bin/loop-teardown reap --repo <abs repo>`
   (a dry run) and put anything it would remove in the owner prompt.
2. **Pick the number.** N is one more than the highest `loop<N>` in this repository's `codex/`.
3. **Read the tracker.** `LOOP.md`'s `tracker:` (absent means `backlog`) picks it: `backlog task list
   --plain`, or for `tracker: github` the queue command in `LOOP.md`'s `## Queue`; then the owner's
   stated aim. Rank ready work by the
   owner's priorities. Exclude tasks that need an owner choice nobody has made, and say which.
4. **First wave.** Up to about 20 rows: task, scope (files), route (`worker`, `composer` for a compose or board
   batch, or `senior` for one of the four contract section 3 starts, named), and any pre-assigned migration number or dependency. Prefer many small file-disjoint
   tasks. Do not hold back work because two tasks share a file; note the overlap instead. Landing is
   continuous (contract section 6); name tasks that must land together under
   `## Decisions already made`.
5. **Front-load decisions.** Batch every owner choice the first wave needs into one prompt now and
   write the answers under `## Decisions already made`. Run the gate and the tracker's
   definition-of-done commands (Backlog `definition_of_done`, or the issue template's) on base through the gate route (never a task's own `Done when:`
   check, which is red at base by design), or reuse the last closeout's result for the same base
   SHA. Record each one already red there, with its failing tests and the reason, under
   `## Decisions already made`. For a cross-repo run, name the target's gate and its mutex.
   **Gate route.** Read it from the repository's `AGENTS.md` now, never from the last charter. Where
   `AGENTS.md` names a remote gate runner (a committed runner contract), the route is that runner and
   its slot pacer, and a workstation run under a mutex is only the fallback for the runner failures
   `AGENTS.md` lists; write both into `gate-route`. Re-route an older `LOOP.md` gate row to match.
   **Review and landing.** Read `landing:` from `LOOP.md` (absent means `direct`). Resolve
   the local review command(s) from the enrolled context and repository contract, including exact
   repository scope, provider/key route, executor, candidate/base binding and failure policy. Freeze
   them in `LANE.md`. Where central rules apply, freeze their source commit, selected rule IDs and
   rendered-content digest in the sanitized intent brief; the local reviewer verifies that context.
   Never infer hosted rule application from CLI access. Never select a tool from PATH or infer a shared reviewer default. Preserve a
   repository's declared local pair. Missing or conflicting selection blocks activation.
   Resolve `reviewer:` through the same context/contract, then read only the selected
   `<plugin root>/protocol/loop/review-profiles/` file named for that profile. A legacy context may explicitly
   retain its existing default; a new context does not inherit it. `review: local-pair` instead
   selects that profile with no hosted `reviewer:`; direct landing also requires `landing-branch:`.
   Record exact repository, branch, live rules and command/key availability. Never waive required
   hosted approval or infer main/deploy authority. Run `loop-lint loopmd <abs LOOP.md>`; lint
   acceptance validates syntax, not proof or authorization to activate a hosted reviewer.
   For `pr`, grant pushing landing branches, opening PRs and `gh pr merge --squash --match-head-commit`
   in the charter; name the profile, merge gate and required checks (including `ai-review` where
   required). On `claude`, copy those grants into the launch. Record the selected profile's live
   activation proof and grant only its evidenced request/thread actions. Missing proof parks
   activation; never invent check/status names or relax approval. Keep section 5's local review.
6. **Write the files** (contract section 1): the charter from
   `<plugin root>/protocol/loop/charter.md`, `codex/loop<N>/LANE.md` from
   `<plugin root>/protocol/loop/lane.md` plus `LOOP.md`'s `## Lane brief`, and the launch file
   from the harness file's launch block. Copy the Standing file's current sha256 into both. On a
   `claude` harness, also copy into the launch every `authority:` or `live:` line that touches
   customer data or a live system, verbatim.
7. **Check.** `loop-lint charter <charter>` and `loop-lint queue <charter> <first-wave as QUEUE.md>`
   until clean.
8. **Deliver** the launch file path and a short summary. The owner launches it.

Routes and model choices live in each harness file. The root model defaults to the harness file's
launch command; change it only on the owner's instruction.
