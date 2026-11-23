# Runs

A run is one root working a goal across many tasks and landing them by pull request, attended or not. This page is the
protocol; `run-claude.md` or `run-pi.md` beside it holds what differs by harness. Whatever must hold every time is
enforced by a tool and listed under Gates; the rest is judgement.

Roots are Claude Code and pi; Codex is not a run root.

## Start

Start in the session the operator is in: `/loop <what he wants>` or his plain words. There is no separate preparation session,
charter or launch file.

While he is present:
1. Read the repository's `AGENTS.md` and `LOOP.md` (gate, landing, guarded paths, traps) and look at the work: what
   he named, or the open issues labelled `ready`.
2. Ask everything you need in one batch with the question prompt, recommendation first.
3. Write the run files in `codex/run-<YYYYMMDD>-<n>/` (n counts from 1 each day) and say you have:
   - `AUTHORITY.md`: first line `# Authority: <repo> run-<YYYYMMDD>-<n>`, then `protocol: v5`, then his answers word
     for word, the rule for deciding what they do not cover, and what is off limits.
   - `PLAN.md`: the lanes; any interface two lanes share, frozen before either starts; reserved migration numbers; the
     check that proves each lane; and a mark on any lane where a wrong answer could still pass its check.
   - `RESUME.md`: where state lives and what this run has learned, so a fresh context can carry on.
   - `PARKED.md` and `FOLLOWUPS.md`.

## Working

- Do small things yourself. Use lanes for the rest: `worker` by default; `senior` for a shared interface, an unknown
  cause, a security or privacy call, or a lane marked wrong-answer-can-pass; `reviewer` (fresh context) for the diffs
  under Gates; `scout` for read-only mapping.
- A brief gives the goal, the check that proves it, what is in and out of scope, and what to send back. A return gives
  the outcome, the evidence (commands and results), blockers and anything left out, and decisions made.
- Each lane works in its own worktree outside the repository checkout, on a branch `run-<YYYYMMDD>-<n>/<lane>`, on
  files no other live lane owns.
- Run 6 to 8 building lanes at once; pull requests waiting on CI or review do not count. Start one only while load is
  under twice the CPU count and 4 GB of memory is free.
- Builds, lints and tests beyond one package go through the repository's remote gate (`LOOP.md` names it), never the
  workstation.
- A task parked twice does not go back as the same lane: give it one senior attempt with a different approach, or put
  it to the operator as a one-paragraph decision.
- Read every diff before it lands.

## Land

- Each pull request lands on its own through `run-check land` when required checks are green on the exact commit and
  review is settled; `run-drive` (appendix) does the mechanics. The review table is `reference/review.md` in the agent
  home. Ask for review when the pull request opens. Batch small related issues into one pull request. A lane may
  build on an unmerged branch and rebase when that lands.
- Two repair rounds for any task. After the second, what is left and not blocking goes on a follow-up issue and the
  change lands. A finding marked blocker, security, privacy or data loss holds the merge. Lint, format and docstring
  failures are fixed in place.
- Main red: fixing it is the first task and always in scope. Keep merging only if the failing job is a listed flake with
  an open fix, or the pull request is the fix. If a merge turns a green job red, stop landing until it is fixed or
  reverted. Do not dispatch more than about twice what you can land.

## Questions

Never block the run on a question. Write it in `PARKED.md` with your recommendation and carry on with everything it
does not block. These wait for the operator whatever the recommendation: accepting a security or privacy exposure, destroying
data, a production deploy or release he has not named, spending money, anything sent outside his repositories. For
everything else apply your recommendation and record it. Put parked questions to him in one batch when he is back.

## Stop

Stop when the goal is met or the queue is empty, or after three hours with nothing merged (say why). Then:
- `run-check reap --run <run folder>` is a dry run; with `--apply` it removes the run's merged worktrees: none may be
  left. Work that did not land is a pushed branch with a draft pull request labelled `unlanded` whose body is the lane's return, or, where a push is not possible, a patch at
  `<run folder>/patches/<repository folder name>/<branch with % and / percent-encoded>.patch`.
- Write `CLOSEOUT.md` in the run folder, about 300 words: first line `# Closeout: <repo> run-<YYYYMMDD>-<n>`, then
  `protocol: v5`, then `## Landed` (`- #<n> <title>` per pull request), `## Parked`, `## Decisions needed` and
  `## Spend by role`.
- Close each issue you verified, with the evidence in a comment.
- `run-check close --run <run folder>` passes (`--repo <checkout>` per other repository).

## Gates

Enforced by tools; do not work around one, fix what it reports. `run-check` is `<plugin root>/bin/run-check`.

- Required checks and the repository ruleset; merges carry `--match-head-commit`. No admin bypass, no force push.
- Only the root pushes, merges, deploys, uses ssh or writes issues. Lanes hold no credentials.
- `run-check land` refuses, until the root signs it off after a fresh-context review, a diff that widens an allowlist,
  changes a baseline or snapshot, deletes or skips a test, or touches a path in `guarded-paths:`.
- The staging guard (explicit paths only) and the bounded-wait check (no sleep loops).
- `run-check close`: no run worktree left, every landing either verified on main or listed as not.
- Unattended (a file `UNATTENDED` in the run folder, written when the operator leaves and removed when he is back), the root's
  question prompt is refused, so a question cannot stall the run.
- Secrets never appear in a command line, log, issue or pull request.
