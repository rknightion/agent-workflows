# Loop preparation (v4)

Read by the `loop` skill only. Preparing a run should take minutes, not an hour.

1. **Last run.** Read the last closeout's `## Parked` and `## For ratification`; apply the owner's
   answers as charter decisions. Run `<plugin root>/bin/loop-teardown reap --repo <abs repo>`
   (a dry run) and put anything it would remove in the owner prompt.
   **Parked work.** Run `$PROTOCOL_SOURCE/bin/loop-lint parked --repo <abs repo>`: it lists each
   task the last three closeouts parked, with the reason and how many closeouts in a row parked it.
   Write every listed task into the charter's `exclude:`, unless its park named a blocking issue
   that is now closed. Print the list for the owner; a task the owner re-admits stays out of
   `exclude:` and goes under `## Decisions already made`.
2. **Pick the number.** N is one more than the highest `loop<N>` in this repository's `codex/`.
3. **Read the tracker.** `LOOP.md`'s `tracker:` picks it; an absent key means Backlog only when the
   repository has a `backlog/` directory, else GitHub. Backlog: `backlog task list --plain`. GitHub:
   the queue command in `LOOP.md`'s `## Queue`, normally `just tracker queue`, never a raw
   `gh issue list`. A repository whose issues live in another repository (`tracker-repo:` in
   `LOOP.md`, or in the charter for a boardless repository with no `tracker:` key) is
   `tracker: github`: read that repository's ready queue with its label,
   `just tracker queue --label <repo> --repo OWNER/REPO`, and put the same `tracker-repo:` in the
   charter. Then the owner's stated aim. Rank ready work by the
   owner's priorities. Exclude tasks that need an owner choice nobody has made, and say which.
   Report ready depth against the `lanes:` cap: how many ready, unexcluded tasks there are for that
   many lanes, and say so when the depth is below the cap. Never invent work to fill it.
4. **First wave.** Up to about 20 rows: task, scope (files), route (`worker`, `composer` for a compose or board
   batch, or `senior` for one of the four contract section 3 starts, named), and any pre-assigned migration number or dependency. Prefer many small file-disjoint
   tasks: split one whose candidate patch would pass about 30 KB into slices that each land alone, because
   review findings and repair rounds grow with patch size. In each row's note, state the inputs and
   environments the change must support and its non-goals; the root copies them into the brief's `Why:`
   and tests review findings against them. Do not hold back work because two tasks share a file; note the overlap instead. Landing is
   continuous (contract section 6); name tasks that must land together under
   `## Decisions already made`.
5. **Front-load decisions.** Batch every owner choice the first wave needs into one prompt now and
   write the answers under `## Decisions already made`. Ask in it which categories of work are not
   worth tonight's review cost (for example dependency dashboards, cosmetic docs or protocol
   churn); record the answer there, and put a category that is a label in `exclude:`. List every
   privileged command the run is known to need (a deploy, an SSH step, a secret write, a merge) in
   the charter's `preflight:`, each in the exact form it will run, with what it proves and which
   rows need it, so the root runs them while the owner is present (contract section 7). Run the
   gate and the tracker's definition-of-done commands (Backlog `definition_of_done`, or the issue template's) on base through the gate route (never a task's own `Done when:`
   check, which is red at base by design), or reuse the last closeout's result for the same base
   SHA. Record each one already red there, with its failing tests and the reason, under
   `## Decisions already made`. For a cross-repo run, name the target's gate and its mutex.
   **Gate route.** Read it from the repository's `AGENTS.md` now, never from the last charter. Where
   `AGENTS.md` names a remote gate runner (a committed runner contract), the route is that runner and
   its slot pacer, and a workstation run under a mutex is only the fallback for the runner failures
   `AGENTS.md` lists; write both into `gate-route`. Re-route an older `LOOP.md` gate row to match.
   Pick an optional fast check and a local runtime fixture where available. Name them in `LANE.md`
   for the affected work.
   **Review and landing.** Landing mode follows contract section 6: a GitHub tracker (`tracker: github`, a
   `tracker-repo:`, or neither `tracker:` nor `backlog/`) lands by pull request; only a Backlog
   repository with no `landing:` key lands `direct`. Read `landing:` from `LOOP.md` for the rest. Resolve
   the local review command(s) from the enrolled context and repository contract (`none` where its
   policy names only a hosted profile), including exact
   repository scope, provider/key route, executor, candidate/base binding and failure policy. Freeze
   them in `LANE.md`, with the policy's review order, tier rule (the tier can depend on the change, not only the repository), run cap, triage owner and no-verdict rule: which components
   run on a lane's first candidate, which on repair deltas, and which the root runs on the final candidate. Where central rules apply, freeze their source commit, selected rule IDs and
   rendered-content digest in the sanitized intent brief; the local reviewer verifies that context.
   Never infer hosted rule application from CLI access. Never select a tool from PATH or infer a shared reviewer default. Preserve a
   repository's declared local pair. Missing or conflicting selection blocks activation.
   Resolve `reviewer:` through the same context/contract, then read only the selected
   `<plugin root>/protocol/loop/review-profiles/` file named for that profile. `review: local-pair` instead
   selects that profile with no hosted `reviewer:`; a direct landing also requires `landing-branch:`.
   Record exact repository, branch, live rules and command/key availability. Never waive required
   hosted approval or infer main/deploy authority. Run `loop-lint loopmd <abs LOOP.md>`; lint
   acceptance validates syntax, not proof or authorization to activate a hosted reviewer.
   For `pr`, grant pushing landing branches, opening PRs and `gh pr merge --squash --match-head-commit`
   in the charter; name the profile, merge gate and required checks (including `ai-review` where
   required). On `claude`, copy those grants into the launch. Record the selected profile's live
   activation proof and grant only its evidenced request/thread actions. Missing proof parks
   activation; never invent check/status names or relax approval. Keep section 5's local review
   where the policy has one.
6. **Write the files** (contract section 1): the charter, with `protocol: v4` (and `loop-land: off` only on
   the owner's say-so, never unasked), from
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
