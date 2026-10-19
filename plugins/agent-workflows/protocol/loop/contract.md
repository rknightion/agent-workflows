# Loop contract (v3)

A loop is one long-lived root session working a living queue of Backlog tasks through parallel
lanes. The root reads its charter, this contract, its `harness-<name>.md` and the repository's
`LOOP.md`. Lanes read only their brief and the run's `LANE.md`. Precedence: owner message in the run >
charter > `Standing:` file > `LOOP.md` > this contract.

## 1. Run identity and files

Every file of run N lives in the repository's machine-local `codex/` directory (`<date>` is the UTC
preparation date):

| File | Written by | Purpose |
|---|---|---|
| `codex/charter-<date>-loop<N>.md` | planner | aim, priorities, exclusions, authority, gate route, first wave; frozen |
| `codex/launch-<date>-loop<N>.txt` | planner | the launch message |
| `codex/loop<N>/LANE.md` | planner, root | rules every lane reads |
| `codex/loop<N>/QUEUE.md` | root only | ready order, holds, landing order |
| `codex/loop<N>/LANES.md` | root | one line per live lane: id, task, agent or run id, worktree |
| `codex/loop<N>/DECISIONS.md` | root | rulings, amendments, parks, tripwire calls; append-only (a new entry supersedes an old one by naming it) |
| `codex/loop<N>/QUESTIONS.md` | root | open owner questions with a recommendation each |
| `codex/loop<N>/BATCH<k>.md` | root | one landing batch: rows, order, rulings to apply |
| `codex/loop<N>/<LANE>/` | lane | `candidate.patch`, `return.md`, logs and evidence |
| `codex/closeout-<date>-loop<N>.md` | root | terminal closeout (section 12) |

The launch names the closeout path and says "You are the root". Hooks and continuation
recognise a run by that pair; keep both exact. Amend the frozen charter only
by a `DECISIONS.md` entry (or an owner-approved addendum file beside it).

## 2. The queue

- The backlog is the queue. Start from the charter's first wave, then keep `QUEUE.md` fed from the
  board in the charter's priority order. A scout may draft waves; only the root writes ready rows.
- `QUEUE.md` has three sections: `## Ready` (ordered rows, the task ID first in each), `## Hold`
  (task, reason) and `## Landing` (the next batches). Live lanes are in `LANES.md`, not here.
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
  (unknown cause, frozen seam or shared contract, data-model call, composing a batch); `scout` for
  read-only mapping, sweeps, wave drafts and log reduction. A discovery brief names `jg` (jevgrep)
  for behavioural questions where it is installed, and the language server for symbols.

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
  finding, or four hours have passed since its first dispatch (excluding time spent
  `PAUSED` or in a blocking owner question), stop repairing. Record in
  `DECISIONS.md` one of: a named new approach, a split, a `senior` escalation, or a park with
  options. In overnight mode a second trip parks the task until the owner is back.
- A task is Done only after it lands in a pushed batch whose composed gate was green (section 6).

## 5. Review

- Routine tier: the lane's gate plus CodeRabbit. A `reviewer` lane is **mandatory** when the task
  touches concurrency or locking; privacy, redaction or scrubbing; authorisation or tenant scope;
  user-visible copy against approved decks; when its only proof is a test; on its second repair
  round; above about 15 changed files; or when CodeRabbit did not run. Otherwise add one whenever
  you doubt a return.
- Guarded tier (section 8): a `reviewer` with the security brief before landing, then CodeRabbit.
- A change after a verdict invalidates it: review the delta only. Reviewers report; they never edit.
- CodeRabbit for code, once per final candidate: fix every `critical`/`major`, name the rest you
  leave. One delta review only after such a fix. Exit 0 is not clean; no `complete` line is a
  failure; `unreviewedFileCount > 0` is partial. Skip docs, comments, declarative config and
  dependency bumps, and say so.

## 6. Landing

- Land in batches. The root, or a `senior` compose lane working from `BATCH<k>.md`, applies accepted
  candidates to current main as one commit per task carrying a `Task: <ID>` trailer, regenerates
  generated files once, and gates that exact tree in CI mode on the charter's gate route. The root
  pushes it unchanged as soon as it is green, unless a section 10 hold applies.
- Where the charter forbids the board's IDs in a public repository, its trailer carries a neutral
  alias (`Task: loop<N>-<lane>`, never a board ID or private name). Before closeout, land a commit
  in the board's private repository that consumes the work (a lock bump, for example) with one
  `Task: <ID>` trailer per delivered task, and record the alias map in `DECISIONS.md`.
- A compose lane also reads every applied patch: assertions kept, no test weakened, scope respected.
- Heavy gates run only through the gate route the charter and `LOOP.md` name.
- Gate once per (tree, base). Classify every red against base: one that also fails on base is not
  the candidate's; record both lists. A new failure is red.
- Hosted CI does not block landing. Watch main's CI for each pushed SHA with a background watcher
  and fix forward at once. Revert only on a recorded decision that the breakage blocks all other
  landing, damages data or a live deploy, or has no plausible repair.
- Other sessions commit here too: merge before pushing and never revert foreign work. Commit with
  explicit pathspecs.

## 7. The owner

- **Owner present** (the default): keep working. Put a question in chat with your recommendation
  and append it to `QUESTIONS.md`; act on the answer when it comes. A blocking question tool is
  allowed only when nothing else is waiting on you: process every queued return and dispatch every
  ready task first.
- Small design, copy and seam calls the evidence decides are yours: rule, record them in
  `DECISIONS.md` marked for ratification, and keep going.
- **Overnight mode** starts only when the owner says "overnight mode" (optionally "until HH:MM",
  the owner's local time; record it in UTC in `DECISIONS.md`).
  Then ask nothing and never call a blocking question tool: hold questions in `QUESTIONS.md`, rule
  what you can, park what you cannot, and work the charter's `overnight:` mandate. It ends only at
  the stated time or on "I'm back", "I'm awake" or "overnight off". A
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
- You may record an amendment repairing a charter defect, settle a design fork the evidence clearly
  decides, implement a small prerequisite outside any live lane's scope, and amend a seam first
  frozen in this run that has no accepted consumer (then re-brief every lane coding against it).
- No root decision supplies owner consent, removes a fence or rotates a credential.
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
  The owner's own release-please releases are not outward-facing.
- Never print a secret value. Read a Kubernetes Secret only through a tool that never prints it.
- A targeted `tofu`/IaC apply only after you saved and read its plan.
- Before merging a release or pushing anything that triggers a deploy, check what the deploy will
  interrupt (a live owner operation, a running pull) and hold if the owner set a hold.
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
- After compaction or a provider error, recover: re-read `QUEUE.md`,
  `LANES.md`, `DECISIONS.md` and the harness's live listing, reconcile, continue. Never start a
  fresh root. Do the same reconcile whenever a blocking call or an owner answer returns (an owner
  control in the answer comes first): every return that queued meanwhile and every lane in
  `LANES.md` gets a next step before new work.
- Close out on `close out`, an emergency stop, or when no ready unexcluded work remains in the
  charter's scope and no lane or watcher is live:
  1. Drain: in-flight mutations finish; a hung mutation goes under Parked with its identity.
  2. Mark every landed task Done (`--check-ac` and `-s Done` in one call) and append one note line
     to each parked task with `--append-notes`.
  3. Stop every watcher and lane you started. Keep worktrees with unlanded work; remove clean landed
     ones.
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
