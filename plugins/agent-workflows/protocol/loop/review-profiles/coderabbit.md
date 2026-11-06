# Review profile: coderabbit

Read only when selected by the frozen run contract. Step numbers refer to contract section 6.

Preparation: grant one `@coderabbitai review` per stalled head. Check `.coderabbit.yaml`
sets `reviews.request_changes_workflow: true`; otherwise make that the first task.

**coderabbit.** Watch `gh api repos/<owner>/<repo>/commits/<sha>/statuses`: the `CodeRabbit`
  status: "Review completed" or "Review approved" (success also covers skipped, draft and
  rate-limited reviews). Read the latest `coderabbitai[bot]` review with `commit_id` equal to head;
  absent: read once more after a minute. `CHANGES_REQUESTED`, or `COMMENTED` with an open thread,
  goes to step 4; `COMMENTED` with none open waits at 3. No status after about 3 minutes: post
  `@coderabbitai review` once per head, the only comment. Never reply in or resolve its
  threads, post `@coderabbitai approve`, `resolve`, or ignore pre-merge checks. Merge requires
  an APPROVED review on head and no open CodeRabbit thread (GraphQL `reviewThreads.isResolved`).
