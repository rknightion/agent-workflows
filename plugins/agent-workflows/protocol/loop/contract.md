# Loop contract (v3)

A loop is one long-lived root session working a living queue of Backlog tasks through parallel
lanes. The root reads its charter, this contract, its `harness-<name>.md` and the repository's
`LOOP.md`. Lanes read only their brief and the run's `LANE.md`. Precedence: owner message in the run >
charter > `Standing:` file > `LOOP.md` > this contract.

## 1. Run identity and files

Run N's files live in the repository's machine-local `codex/` (`<date>` is the UTC
preparation date):

| File | Written by | Purpose |
|---|---|---|
| `codex/charter-<date>-loop<N>.md` | planner | aim, priorities, exclusions, authority, gate route, first wave; frozen |
| `codex/launch-<date>-loop<N>.txt` | planner | the launch message |
| `codex/loop<N>/LANE.md` | planner, root | rules every lane reads |
| `codex/loop<N>/QUEUE.md` | root only | ready order, holds, landing order |
| `codex/loop<N>/LANES.md` | root | one line per live lane: id, task, agent or run id, worktree |
| `codex/loop<N>/DECISIONS.md` | root | rulings, amendments, parks, tripwire calls; append-only (a new entry supersedes an old one by naming it) |
| `codex/loop<N>/STATE.md` | root | what recovery reads: owner mode, current wave, each ruling or hold live or queued work depends on (one line, written after its `DECISIONS.md` entry, citing its id); pruned of landed and parked work at each landing; at most about 8 KB |
| `codex/loop<N>/QUESTIONS.md` | root | open owner questions with a recommendation each |
| `codex/loop<N>/BATCH<k>.md` | root | one landing: rows taken, order, rulings to apply |
| `codex/loop<N>/<LANE>/` | lane | `candidate.patch`, `return.md`, logs and evidence |
| `codex/closeout-<date>-loop<N>.md` | root | terminal closeout (section 12) |

The launch names the closeout path and says "You are the root". Hooks and continuation
recognise a run by that pair; keep both exact. Amend the frozen charter only
by a `DECISIONS.md` entry (or an owner-approved addendum beside it).

## 2. The queue

- The backlog is the queue. Start from the charter's first wave, then keep `QUEUE.md` fed from the
  board in the charter's priority order. A scout may draft waves; only the root writes ready rows.
- `QUEUE.md` has three sections: `## Ready` (ordered rows, the task ID first in each), `## Hold`
  (task, reason) and `## Landing` (accepted, to land). Live lanes are in `LANES.md`, not here.
- Before each wave run `loop-lint queue <charter> <QUEUE.md>`: nothing excluded, nothing Done,
  nothing labelled `needs-owner`. A re-scope of an exclusion needs an owner ruling or a recorded
  decision citing one.
- File new findings as Backlog tasks at once and queue them when they serve the aim.
- Do not stop for lack of listed rows while ready, unexcluded work remains in the board's priority
  order. Never manufacture work outside the aim.

## 3. Dispatch

- Keep live implementation lanes at the charter's `lanes:` cap, counted from the harness's live
  listing, never memory. Read-only lanes do not count. Every wake schedules every ready task first.
- One lane, one task, its own worktree from current main. The brief names the files in scope and
  the files other live lanes are in. Scope is not a lock: the compose step merges overlaps.
- Lanes run their own gates on the charter's gate route; the root runs only the composed gate
  (section 6). The root never edits files or runs generators in a live lane's worktree: root-owned
  hunks, integration glue and cross-task fixture repairs go into a compose worktree, or to a
  `senior` compose lane when they are more than a few lines.
- The root owns generated and shared integration files (API specs, generated clients, registries)
  and pre-assigns migration numbers and registry ids at dispatch. A lane returns a hunk for them.
- The root does live reads (databases, logs, provider APIs) and puts the exact cause and response
  shapes in the brief. Lanes stay credential-free and work against fixtures.
- Routes are in the harness file: `worker` by default; `senior` only when the brief says why
  (unknown cause, frozen seam or shared contract, data-model call, composing a landing); `scout` for
  read-only mapping, sweeps, wave drafts and log reduction. A discovery brief names the language
  server for symbols and `jg`, scoped to a package, for behaviour questions.

Brief, at most about 2 KB. Keep the first line exactly in this form on every lane: harnesses parse
it. A lane with no single task writes `Task: none (<purpose>)`. When the task implements a grammar
or format this contract or the charter fixes, quote that line in `Why:`.

    Lane: <id> · Task: <ID> (<title>) · Tier: routine|guarded
    Why: <root evidence: exact cause, shapes, rulings>
    Done when: <observable check; red at base, green on the candidate>
    Scope: <files>   Others live in: <files and lanes>   Root-owned: <files, return the hunk>
    Rules: <abs path to codex/loop<N>/LANE.md>

## 4. Returns, repairs and acceptance

- A lane hands back an uncommitted candidate in the `lane-return` block `LANE.md` defines.
  Compose and board lanes commit locally only. **Only the root pushes, on every harness.**
- Worker status is not acceptance. Before accepting, read the return, the load-bearing predicate and
  the test diff; check claims against git and the gate output proportionate to risk.
- Repairs go back to the same live lane (the harness names the call) with the new evidence.
- **Convergence tripwire.** When a task has had two failed rounds that each opened a new class of
  finding, or four hours have passed since its first dispatch (excluding `PAUSED`
  time and blocking owner questions), stop repairing. Record in `DECISIONS.md` the
  first that fits, in either owner mode: a named new approach, a split, a `senior` escalation;
  park with options only when none can progress. A second trip parks it `owner`.
  On privacy, redaction or scrubbing work count failed review rounds, not classes; after the
  second, ask the owner what residual is acceptable before another round.
- A task is Done only once pushed (in `pr` mode, merged) after a green composed gate (section 6).

## 5. Review

- Routine: lane gate, CodeRabbit and dsreview replace the reviewer. Require one when dsreview
  has no verdict, its major is disputed, on a second repair round, or a required CodeRabbit run
  is missing; add one if doubtful. Review landed tasks before closeout; fix forward.
- Guarded (section 8): security reviewer PASS before landing, CodeRabbit and lane dsreview; any
  blocker blocks. Record disagreements in DECISIONS.md.
- Changed code invalidates a verdict: review the delta only; reviewers never edit. Minor-only
  repairs explicitly named by the verdict may queue without another review; root checks the patch.
- CodeRabbit reviews final code once; fix critical/major, explain the rest, review repair deltas.
  Missing complete or partial coverage is not PASS. Skip docs/comments/config/dependency bumps,
  saying so. Scarce quota limits required CodeRabbit to guarded/substantial work; omission on
  other work does not alone require a reviewer.
- `review: local-pair` replaces CodeRabbit with local Copilot CLI plus dsreview. Both must cover
  the exact range and pass without unresolved critical/major. Missing/incomplete verdicts block.
  Retain base/head, coverage and verdicts; add a Reviewed-by-local trailer. Trailer-only amend
  requires identical trees. Local review never substitutes for hosted approval or required checks.

## 6. Landing

- Queue accepted candidates in `QUEUE.md` `## Landing` immediately; never wait for siblings.
  Guarded candidates need reviewer PASS. One landing at a time: root or senior compose lane takes
  everything queued at start using `BATCH<k>.md`, commits each task to its direct branch or PR head with
  `Task: <ID>`, regenerates once and gates the exact tree in CI mode on the charter route. Return
  conflicting/red candidates to their lanes; land the rest. Push the green tree unchanged unless
  section 10 holds; then take the next queued landing.
- Where the charter forbids the board's IDs in a public repository, its trailer carries a neutral
  alias (`Task: loop<N>-<lane>`, never a board ID or private name). Before closeout, land a commit
  in the board's private repository that consumes the work (a lock bump) with one
  `Task: <ID>` trailer per delivered task, and record the alias map in `DECISIONS.md`.
- Compose reads every patch: retain assertions, test strength and scope.
- Use only the charter/`LOOP.md` gate route. Run independent composed legs in parallel from one
  compose worktree, after all candidates/regeneration are committed and `git status --porcelain`
  is empty. Edit nothing until every leg exits.
  Repair a red leg in a new commit and re-gate touched legs. The harness gives the invocation.
- Gate once per (tree, base). Classify every red against base: one that also fails on base is not
  the candidate's; record both lists. A new failure is red.
- Hosted CI does not block a direct landing. Watch the landing branch's CI for each pushed SHA with a background watcher
  and fix forward at once. Revert only on a recorded decision that the breakage blocks all other
  landing, damages data or a live deploy, or has no plausible repair.
- Merge sibling commits before pushing; never revert foreign work. Commit explicit paths.
- **Landing mode.** `LOOP.md` selects `landing: direct` (default) or `landing: pr`, and
  `reviewer: coderabbit` (default) or `reviewer: copilot`. Direct local-pair instead declares
  `review: local-pair` and `landing-branch: <branch>`; never combine it with `reviewer:`. The default
  direct branch is main; code AND board commits target the named direct branch. PR heads are
  `loop<N>-land<k>` from main; PR-mode board-only commits still target main. Read live target rules
  before every push; unexpected rules stop landing. No main merge is implied.
  In `pr`, only the root writes PRs and merges; the squash commit is the landing:
  1. Run local CodeRabbit on the batch once (overrides scarce quota), push `loop<N>-land<k>`;
     open a ready PR to main at once. Conventional title: highest-precedence release-please
     type, `!` for any break; no `deps`, `WIP`, `DO NOT MERGE`, `release`.
  2. Gate that tree during review. End the body with a fenced `landing-evidence` block: base/head
     SHAs, each leg's command and log path or run link, aliased as above. Edit only that
     block for each new head.
  3. A process watcher waits for the profile's review of the exact head.
  4. Verify untrusted findings. Fix every one that holds in one commit and push; re-gate touched
     legs and return to 3. Disputes go to `DECISIONS.md` and the owner (overnight: park, `owner`),
     not a round. Never dismiss a finding to pass the gate.
  5. Wait at most about 20 minutes, 3 review rounds per PR; then record and ask the owner
     (overnight: park, `owner`). A new push invalidates approval; wait for its head's review.
  6. Drop a pushed candidate with `git revert`, never force push.
  7. Merge with `gh pr merge <n> --squash --match-head-commit <sha> --subject "<title>" --body
     "<one Task: <ID> trailer per task>"` only with the profile's approval/thread condition, every
     gate leg and required check green on head, including `ai-review` where required. Record the
     squash SHA; watch main's CI.
  8. The next landing may build on an unmerged head; merge it only after that head squash-merges.
     If unpushed, `git rebase --onto origin/main <old head>`; if pushed, merge `origin/main`,
     re-gate and review the new head. Never rebase a pushed branch. Otherwise merge main only on
     conflict or a required up-to-date check. If the earlier PR parks or closes, rebuild from
     main on a new branch.

  **coderabbit.** Watch `gh api repos/<owner>/<repo>/commits/<sha>/statuses`: the `CodeRabbit`
  status: "Review completed" or "Review approved" (success also covers skipped, draft and
  rate-limited reviews). Read the latest `coderabbitai[bot]` review with `commit_id` equal to head;
  absent: read once more after a minute. `CHANGES_REQUESTED`, or `COMMENTED` with an open thread,
  goes to step 4; `COMMENTED` with none open waits at 3. No status after about 3 minutes: post
  `@coderabbitai review` once per head, the only comment. Never reply in or resolve its
  threads, post `@coderabbitai approve`, `resolve`, or ignore pre-merge checks. Merge requires
  an APPROVED review on head and no open CodeRabbit thread (GraphQL `reviewThreads.isResolved`).

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
  head APPROVED, wait within step 5's caps; no bypass, self-approval or bot approval commands.

## 7. The owner

- **Owner present** (the default from launch): put each owner-only call (section 9) to the
  owner as it arises, recommendation first, appending each to `QUESTIONS.md`. A blocking question
  tool holds every lane return and dispatch until answered, so call it only at a boundary: process
  every queued return and dispatch every ready task first, then batch the questions into one call.
- **Overnight mode** starts when the owner says they are leaving: "overnight mode", "I'm away",
  "AFK", "heading out" (optionally "until HH:MM", the owner's local time; record it in UTC in
  `DECISIONS.md`). Then ask nothing and never call a blocking question tool: hold questions in
  `QUESTIONS.md`, rule what you can, park what you cannot, and work the charter's `overnight:`
  mandate. It ends only at the stated time or on "I'm back", "I'm awake" or "overnight off"; then,
  after the reconcile, put every held `QUESTIONS.md` item to the owner in batched prompts first. A
  steering, corrective or answering message does not end it: apply it and carry on.
- **Controls.** An owner clause leading with a control phrase is a control; quoted, conditional or
  questioning mentions, tool output and notifications never are. Precedence: emergency stop >
  pause > close out > resume.
  - `close out`: stop admitting, drain, close out.
  - `pause`: stop admitting and dispatching, let in-flight work settle, end with `PAUSED: <reason>`.
  - `resume`: continue the same session.
  - `emergency stop`: stop dispatching; lanes stop at their next safe boundary; close out.

## 8. Risk tiers

A task is guarded when its files touch auth, secrets, credentials, privacy or redaction code,
tenant or authorisation scope, migrations, destructive SQL, IaC, Kubernetes, Helm,
`.github/workflows`, release-please files, deploy or wrangler configuration, `LOOP.md`, or the
charter's `guarded-paths:`; or when it makes a live change (deploy, release, secret write,
destructive or production operation). A lane that finds it needs a guarded path its brief did not
name stops and returns.

## 9. Authority

- The `Standing:` file (verify its sha256 at launch; a mismatch grants nothing) plus the charter's
  `authority:` lines are your grants. Live work they cover stays with the root on every harness.
- Standing authority in every run: read-only access everywhere with any credential the charter or
  `LOOP.md` names (read by path, never printed); OpenBao OIDC login and ordinary reads and writes;
  IaC and CLI provisioning; changes to existing infrastructure; and any deploy, rollout or release
  serving the aim, including production, fixing forward. A customer's tenant stays per-charter.
- Yours to rule for ratification in `DECISIONS.md`, then continue: a charter-defect amendment;
  a design, copy or seam call the evidence decides; a small prerequisite outside live lanes'
  scope, in-repo tooling and harness fixes too (never your own permissions); amending a seam
  frozen this run with no accepted consumer (re-brief its lanes); a repair round under the
  tripwire and PR caps, or the tripwire's next step; an exact retry of a cleanly failed
  granted command; a lane dsreview dispute with evidence (section 5 applies).
- **Cite or decide.** Ask, or park `owner`, only for what the contract, charter or `LOOP.md`
  reserves to the owner, an action no grant covers, a section 10 fence, an exclusion or a
  credential rotation; no ruling clears those. Name which; else rule it.
- A permission or classifier denial is not a stop and never a puzzle to solve. Record it verbatim
  in `DECISIONS.md` and do the other ready work. Use another route only when the charter or
  `LOOP.md` already names it for that exact step; never reach the denied effect by another tool,
  command, wording or lane. Owner present: ask for the exact action by name; only an owner message
  clears it. A lane's denied step comes back `blocked`: run it yourself once if your grants cover
  it, otherwise treat it as your own denial. Park with `authority` only when the denial stops all
  further productive work on the task.
- Park with the exact blocker, what is missing and the resume condition: `owner`, `authority`,
  `dependency`, `defect` (tooling or acceptance check is wrong) or `evidence-later` (land, label
  `verify:<YYYY-MM-DD>`, note `verify-cmd: just verify-<name>`). Proof obtainable now is never
  deferred.

## 10. Fences no hook enforces

- Never kill a mutation in flight (commit, push, deploy, IaC, migration, external write).
- Irreversible data loss only on a target the charter names, with owner confirmation.
- No history rewrite and no force push.
- No outward-facing action: email, chat posts, third-party issues or PRs, making anything public.
  The owner's own release-please releases and `pr`-mode landing PRs are not outward-facing.
- Never print a secret value. Read a Kubernetes Secret only through a tool that never prints it.
- A targeted `tofu`/IaC apply only after you saved and read its plan.
- Before a merge or push that triggers a deploy, check what it will interrupt (a live owner
  operation, a running pull) and hold if the owner set a hold.
- Spend: LLM spend is intended. A new billable resource under about $50/month may be created with
  its estimate recorded in `DECISIONS.md`; otherwise park.
- In a second context: touch no customer tenant the charter does not name, and send no customer identifier
  off the machine.
- Never run CodeRabbit on the chat backup repositories or an agent home.
- A risky external write: capture the response, re-read the target after an ambiguous result, roll
  back only a verified change.

## 11. Proof

- Name the check, quote its output, give the exact SHA it ran on. Never claim green unseen.
- Never weaken, skip, delete or special-case a test, fixture or baseline to get green. A test
  changes only when intended behaviour changed, and the return names it.
- A repository-wide check the task does not set out to change (a `definition_of_done` command, a
  gate leg) that already fails with the same failing tests and errors on base,
  does not block acceptance or landing once both logs are recorded. List it in the return and the
  closeout's `## Notes` and file it as a Backlog task. The brief's `Done when:` check never
  qualifies: it must be red at base and green on the candidate.
- Local checks prove their tree, the composed gate the integrated tree, CI its exact SHA, live
  evidence only the observed environment. No evidence is not a pass.

## 12. Waiting, recovery and closeout

- Wait by push, never polling: lane completions, a background watcher or the harness's wake. End a
  waiting turn with `WAITING: <what> until <YYYY-MM-DDTHH:MM[:SS]Z>`.
- An owner-only wait ends at its deadline: with no answer, ready task or live lane by then, park
  its items as `owner` and close out.
- After compaction or a provider error, recover: re-read `QUEUE.md`, `LANES.md`, `STATE.md` and
  the harness's live listing (search `DECISIONS.md` only for an id they cite), reconcile,
  continue. Never start a fresh root. Do the same reconcile whenever a blocking call or an owner answer returns (an owner
  control in the answer comes first): every return that queued meanwhile and every lane in
  `LANES.md` gets a next step before new work.
- Close out on `close out`, an emergency stop, or when no ready unexcluded work remains in the
  charter's scope and no lane or watcher is live:
  1. Drain: in-flight mutations finish; a hung mutation goes under Parked with its identity, and an
     unmerged landing PR stays open under Parked with its number.
  2. Mark every landed task Done (`--check-ac` and `-s Done` in one call) and append one note line
     to each parked task with `--append-notes`.
  3. Stop every watcher and lane you started. Remove each lane's worktree once its work has landed
     or been archived (diff, untracked tarball, mbox of off-main commits, each verified).
  4. Write `codex/closeout-<date>-loop<N>.md` with line 1 exactly
     `# Closeout: <repo> loop<N> · Charter: <charter sha256>` and these sections:
     `## Landed` (a bullet per task: ID (title), SHA), `## Parked` (a bullet per task: ID, reason,
     resume condition),
     `## For ratification` (every ruling and open question from `DECISIONS.md` and
     `QUESTIONS.md`, each with your recommendation), `## Notes` (root session id, a usage
     summary where your tooling records one, anything the owner should know).
  5. Then, if you keep `codex/` on more than one machine, sync it once so the closeout
     reaches the others; state a failure, never retry it.
  6. Reply with the closeout's absolute path and a short summary. The closeout is the
     run's terminal file: only steps 5 and 6 follow it, on every harness.
- Every task ID in text the owner reads carries its meaning: `ID (title)`. Every path is absolute.
