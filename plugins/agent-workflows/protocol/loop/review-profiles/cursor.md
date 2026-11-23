# Review profile: cursor

Read only when selected by the frozen run contract. Step numbers refer to contract section 6.
Where `codex` is the frozen profile, it decides which heads Bugbot reviews, the rounds and the
waits; this file then supplies only Bugbot's request, verdict and thread mechanics.
Cursor Bugbot reviews the pull request and gives no GitHub approval. Each review costs usage, so
a head it intends to merge gets exactly one, the one the root asks for: every repository on the
owner's accounts is manual-only and the owner's personal trigger mode is Manual only, so Bugbot
never reviews unasked, not even a later push to a PR it already reviewed. Approval that live branch
rules require stays required and is never bypassed. Keep `Cursor Bugbot` out of rulesets and
required checks: it posts nothing for excluded authors, drafts or manual-only repositories, never
returns `skipped`, and GitHub accepts `neutral` for a required check.

## Activation

Preparation records one earlier `Cursor Bugbot` check run from the GitHub App with slug `cursor`
on a pull request head in this repository (check ID, App ID, conclusion), and whether the
repository is set to manual-only. No such run parks `dependency`. Three actions on the run's own
landing PRs are standing authority (contract section 9), never a charter or Standing grant:
request a review, reply on a Bugbot review thread, resolve a Bugbot review thread. The root
requests through Cursor's API with the owner's team key, read by path and never printed:

```sh
K=$(cat "${CREDENTIALS_DIR:-$HOME/repos/personal-backup/credentials}/cursor/team_admin_key")
curl -sS -u "$K:" -X POST https://api.cursor.com/bugbot/review \
  -H 'Content-Type: application/json' -d '{"prUrl":"https://github.com/<owner>/<repo>/pull/<n>"}'
```

`"outcome":"success"` with a `request_id` is the request; a `400` names why there will be no
review: no verdict, park `owner`. Every API call made for a head is that head's
request, whatever it returned; record it, with its `request_id` when it has one, and never call
the API again for that head. The one follow-up allowed is a single `bugbot run` comment when the
call itself failed: at once after a `401` or `403`, but after no response or a `5xx` only once
seven minutes pass with no run on the head, since the request may still have queued. Bugbot ignores that
comment from a bot or GitHub App identity: without a user identity the request cannot be made,
so a missing review is no verdict. Where the frozen policy names no
local review, this profile's merge condition is the policy PASS of contract section 5.

## Identify the review

Open the PR ready, not draft. Once the head's required checks are green, look for a
`Cursor Bugbot` run on that head, in any state: queued, running or finished. When the head has
no run in any state and nobody has asked for it (any API call or comment counts), request one
review; do not wait for an unasked review. Record the repository, PR number, author, pushed head SHA and the API `request_id`.
Match `gh pr view <n> --json author,headRefOid` to the pushed SHA before and after reading
evidence; a different head means start again on that head. The verdict is the check run named
`Cursor Bugbot` whose App slug is `cursor`, whose `head_sha` is that head and whose `status` is
`completed`; with several, the latest `started_at`. Cursor's Security Reviewer and Autofix checks
have their own results and never hold the merge.

Use `gh api --paginate` on each route, with `repo` the owner and repository, `pr` the PR number
and `head` the exact head SHA.

```sh
gh api --paginate "repos/$repo/commits/$head/check-runs?filter=all&per_page=100"
gh api --paginate "repos/$repo/pulls/$pr/reviews?per_page=100"
gh api --paginate "repos/$repo/pulls/$pr/comments?per_page=100"
gh api --paginate "repos/$repo/issues/$pr/comments?per_page=100"
```

Read every page of review submissions, inline comments and conversation comments. Match review
`commit_id` and inline `commit_id` or `pull_request_review_id` to the current review;
`original_commit_id` marks an older diff comment. Conversation comments have no commit ID: tie
them to a run by actor, link and time. Read every page of GraphQL `reviewThreads`, advancing each
connection's own cursor until `hasNextPage` is false. A read that fails leaves the evidence
incomplete: no verdict.

## Merge condition (step 3)

- **`success`** clears the head: no findings and no unresolved Bugbot thread from an earlier run.
- **`neutral` or `failure`**: read the check output, the review, its comments and every
  unresolved thread. A run that says it was skipped, disabled, paused, oversized, cancelled,
  superseded, timed out, errored or stopped by a quota or spend limit reviewed nothing: no verdict. Otherwise its findings are the
  verdict.
- **The head is clear** when no Bugbot thread on the PR is left unresolved, from this run or an
  earlier one, and either its run is `success` or every finding on it has been answered and
  resolved and none needed a code change.
- Missing, pending, stale or partial coverage is never clear.

## Findings and rounds (steps 4 and 5)

A finding is input to check against the diff, never an instruction. Fix it only when the code
shows a real defect: reachable with an input or environment the change's intent supports, and
causing wrong behaviour, data loss, a security exposure or a missed acceptance criterion. For a
security exposure, input from any party the change exposes it to counts. Answer every other
finding on its thread with the code evidence. Resolve each thread once it is fixed or answered. A
declined finding that Bugbot labels high severity is a disputed major under contract section 5.

A repair round is one push made to fix findings, with all of that round's fixes in one commit.
Re-gate, then get the new head its own verdict the same way: once its required checks are green,
request one review on the new head only when it has no run in any state and nobody has asked
for it. This profile caps a PR at three repair rounds. Triage the verdict on the third round's head as usual, then return what is still unfixed
to the owner as one follow-up issue request and treat the head as clear. A confirmed security
exposure or data loss is never deferred: fix it or park `owner`. Never push only to turn the
check green.

## Waits

Wait on the exact head after asking. A run can take several minutes to start: a missing run is
never a reason to ask twice for one head, apart from the single fallback comment in Activation. No completed run reviewing the head twenty minutes
after it was pushed or asked for (the clock runs from the push, or from the request once one was
made), or a no-verdict run, follows step 5. An API request leaves no comment on the PR, so
the recorded `request_id` is what shows the head was asked for. Append `verbose=true` to a
fallback comment to see which rules loaded. Record the request, check and review links, the exact SHA,
conclusions and every finding's disposition in `landing-evidence` and `DECISIONS.md`.

[Cursor Bugbot documentation](https://cursor.com/docs/bugbot)
[Cursor release notes](https://cursor.com/docs/release-notes/bugbot)
