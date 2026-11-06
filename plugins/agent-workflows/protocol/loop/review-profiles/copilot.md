# Review profile: copilot

Read only when selected by the frozen run contract. Step numbers refer to contract section 6.

Preparation requires observed sandbox evidence: approval counts under actual branch rules without
bypass, a new push dismisses it and triggers review, and exact start/completion API reads and bot
login. Grant only the evidenced request/thread-resolution actions. Settings alone prove none of
these; missing evidence parks activation. Never infer identity or import another profile's rules.

**copilot.** Without planner sandbox proof, park `dependency`.
  `gh pr view <n> --json headRefOid,reviewDecision`: first match `headRefOid` to pushed SHA.
  Merge also requires `reviewDecision: APPROVED`.
  Watch `gh api --paginate repos/<owner>/<repo>/commits/<sha>/check-runs`: name
  `copilot-pull-request-reviewer`, `head_sha` matching head. `started_at` marks start;
  `status: completed` plus `conclusion: success` marks completion. Read
  `gh api --paginate repos/<owner>/<repo>/pulls/<n>/reviews`: latest submitted REST review with
  `user.login: copilot-pull-request-reviewer[bot]` and `commit_id` matching head. COMMENTED ends
  a round; only APPROVED permits merge.
  If no head check exists after about 3 minutes, run `gh pr edit <n> --add-reviewer @copilot` once
  per head. Resolve only Copilot threads verified fixed; log proof in `DECISIONS.md`. Unresolved
  findings block merge; disputes go to the owner. Re-read all thread pages before merge. Without
  head APPROVED, wait within step 5's caps; never bypass or self- or bot-approve.
