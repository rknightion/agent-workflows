# Runs

A run is one root (Claude Code or pi, never Codex) working a goal across many tasks and landing them by pull request,
attended or not. This page is the protocol; `run-claude.md` or `run-pi.md` holds what differs by harness. What must
hold every time is enforced by a tool and listed under Gates; the rest is judgement.

## Start

Start in the operator's session, on his plain words (Claude Code has `/loop <what he wants>`; pi has no slash command,
see `run-pi.md`).
While he is present:
1. Read the repository's `AGENTS.md` and `LOOP.md` (gate, landing, guarded paths, traps) and look at the work: what
   he named, or the open issues labelled `ready`.
2. Ask everything you need in one batch, recommendation first, including the approvals the goal implies: publishing
   dashboards or alerts, enabling collectors, deploys and production rollouts. Record each answer in `AUTHORITY.md`.
3. Write the run files in `codex/run-<YYYYMMDD>-<n>/` (n counts from 1 daily):
   - `AUTHORITY.md`: first line `# Authority: <repo> run-<YYYYMMDD>-<n>`, then `protocol: v5`, then his answers word
     for word, the rule for deciding what they do not cover, and what is off limits.
   - `PLAN.md`: the lanes; any interface two lanes share, frozen before either starts; reserved migration numbers; the
     check that proves each lane; and a mark on any lane where a wrong answer could still pass its check.
   - `RESUME.md`: where state lives and what this run has learned, for a fresh context; under 12,000 bytes,
     history in `LOG.md`.
   - `PARKED.md` and `FOLLOWUPS.md`.

## Working

- Do small things yourself; lanes for the rest: `worker` by default; `senior` for a shared interface, an unknown
  cause, a security or privacy call, or a lane marked wrong-answer-can-pass; `reviewer` (fresh context) for the diffs
  under Gates; `scout` for read-only mapping.
- A brief gives the goal, check, scope and what to send back. A return gives the outcome, evidence (commands and
  results), blockers, omissions and decisions.
- Each lane works in its own worktree outside the checkout, on a branch `run-<YYYYMMDD>-<n>/<lane>`, on files no
  other live lane owns.
- Run 6 to 8 building lanes at once (pull requests awaiting CI or review do not count), starting one only while load
  is under twice the CPU count and 4 GB is free.
- Reap as work lands: after each landing run `run-check reap --run <run folder> --apply`. A lane that will not land
  leaves a draft pull request or patch (see Stop). Keep the run folder small: one `sweep.json`
  (`run-drive` writes it), CI logs by run id.
- Builds, lints and tests beyond one package use the remote gate (`LOOP.md`), never the workstation.
- A task parked twice does not go back as the same lane: give it one senior attempt with a different approach, or put
  it to the operator in a paragraph.
- Read every diff before it lands.

## Land

- Each pull request lands on its own through `run-check land` when required checks are green on the exact commit and
  review is settled; `run-drive` (appendix) does the mechanics. The review table is `reference/review.md`. Ask for review as the pull request opens. Batch small related issues. A lane may build on an unmerged branch.
- Two repair rounds per task; then what is left and not blocking goes on a follow-up issue and the change lands.
  A finding marked blocker, security, privacy or data loss holds the merge. Fix lint and format failures in place.
- Main red: fixing it is the first task and always in scope. Keep merging only if the failing job is a listed flake with
  an open fix, or the pull request is the fix. If a merge turns a green job red, stop landing until it is fixed or
  reverted. Dispatch at most twice what you can land.

## Questions

Never block the run on a question. For a decision with a reversible recommended answer: proceed on it and record it in
`RESUME.md` or `LOG.md` and in the closeout. Park only a destructive or irreversible step, spending money, or a
credential, or an approval `AUTHORITY.md` lacks (an unnamed deploy or release, a security or privacy exposure,
anything sent outside his repositories): write it in `PARKED.md` with your recommendation and carry on. Put parked questions to him in one batch on his return.

## Stop

Stop when the goal is met, the queue is empty, or three hours pass with nothing merged (say why):
- `run-check reap --run <run folder>` is a dry run; with `--apply` it removes the run's merged worktrees: none may be
  left. Work that did not land is a pushed branch with a draft pull request labelled `unlanded` (body: the lane's return)
  or, where a push is not possible, a patch at
  `<run folder>/patches/<repository folder name>/<branch with % and / percent-encoded>.patch`.
- Write `CLOSEOUT.md` in the run folder, about 300 words: first line `# Closeout: <repo> run-<YYYYMMDD>-<n>`, then
  `protocol: v5`, then `## Landed` (`- #<n> <title>` per pull request), `## Parked`, `## Decisions needed`, `## Spend by role`.
- Close each verified issue with the evidence in a comment.
- `run-check close --run <run folder>` passes (`--repo <checkout>` per other repository).

## Gates

Tools enforce these; fix what one reports. `run-check` is `<plugin root>/bin/run-check`.

- Required checks and the repository ruleset; merges carry `--match-head-commit`; no admin bypass or force push.
- Only the root pushes, merges, deploys, uses ssh or writes issues; lanes hold no credentials.
- `run-check land` refuses, until the root signs it off after a fresh-context review, a diff that widens an allowlist,
  changes a baseline or snapshot, deletes or skips a test, or touches a path in `guarded-paths:`.
- The staging guard (explicit paths only) and the bounded-wait check (no sleep loops).
- `run-check close`: no run worktree left, every landing verified on main or listed as not, a small run folder.
- Unattended (a file `UNATTENDED` in the run folder), the root's question prompt is refused.
- No secret in a command line, log, issue or pull request.
