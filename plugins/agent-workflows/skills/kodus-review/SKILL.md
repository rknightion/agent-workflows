---
name: kodus-review
description: Run the context-selected combined Kodus and independent agent review of an exact local candidate, preserve both verdicts, and handle findings within the requested review or remediation scope.
---

# Combined Kodus review

Use only when the enrolled context and repository contract select this workflow. Preserve another
context's existing review route. A review request is read-only; changing code requires remediation
authority. This skill does not install integrations, activate repository rules, enable Trace or
change branch protections.

## Freeze and submit the candidate

Verify the repository against the approved inventory, including fork/archive classification, before
sending source. Never review credential-bearing backups, runtime homes or chat archives. Prepare a
sanitized intent brief: relevant acceptance criteria and constraints, without unrelated private
board material or credentials. Public findings must not disclose private intent or linked context.

## Bind centrally managed rules locally

Before submission, resolve the approved central rule source at a full immutable Git commit. Select
applicable organization, repository and business-logic rules for this exact repository; retain their
IDs and scope. Render their complete applicable text into a clearly delimited rules block in the
sanitized intent brief. Record source identity, full source commit, selected rule IDs and SHA-256 of
the exact rendered rules bytes immediately above that block. Preserve the task intent separately
within the same brief. Missing source, ambiguous selection or unverifiable pin blocks the combined
gate; do not fetch mutable default-branch rules during review or invent an empty rule set.

The adapter copies and hashes the whole brief; the independent local reviewer verifies the pin,
selection and rules-block digest, then evaluates both the rules and intent against the snapshots.
Its evidence records the source commit, rule IDs, rules digest and candidate brief hash. Do not copy
private rules or linked intent into a public repository, public finding or public artifact. Keep
private central material in private local evidence. This local rules path applies even when Kodus
has no native repository integration: a shared team key does not prove server-side repository rule
application, hosted parity or a separate workspace. Hosted review is supplementary to this local
combined gate and its activation proof is independent; the one exception is the tooling tier's
advisory run under "Review order".

## Render the repository scope

At the same pinned central commit as the rules block, render the repository's scope document next
to the brief: `just ignores OWNER/REPO FULL_SHA > /absolute/ignores.json` in the central policy
checkout. It carries the ignore patterns for generated, recorded and bulk files that Kodus must not
receive, plus the repository's snapshot context rules, content and budget limits and any secret
patterns it adds. Never write, trim or widen it by hand, and never raise a limit instead; a scope
change belongs in the central source. A missing or invalid document, or one naming another
repository, refuses the run.

Each changed path gets one disposition: `secret-pattern`, `secret-copy`, `ignored`, `binary` or
`sent`, in that precedence. Only `sent` paths are sent to Kodus, as a diff plus full content within
the content budget. Secret, ignored and binary paths are never sent to Kodus; it gets only a
content-free notice per withheld path (name and class; size for non-secrets) so it knows
the file is part of the change. A withheld path with `notified: false` gives `notified_reason`:
`skipped` (no request) or `no-slot` (sent paths filled the request limit). The built-in secret
list can only be extended, never removed. Secrets are never copied into a snapshot; the full local
`candidate.patch` is their only local carrier.

Use the installed, validated adapter interface:

```sh
kodus-review --repo /absolute/repository --base FULL_BASE_SHA --brief /absolute/brief-file --ignores /absolute/ignores.json --out /absolute/new-evidence.json
```

Add `--head FULL_HEAD_SHA` for a committed range. Without `--head`, the candidate is the final
worktree, including untracked files. Use a fresh output path; preserve prior evidence.

The adapter creates an adjacent `.artifacts/` directory containing `snapshot/`, `base-snapshot/`,
`manifest.json`, `base-manifest.json`, `candidate.patch`, `submitted.patch`, `files.json`,
`scope.json`, `ignores.json` and `brief.txt`. Kodus receives exactly `submitted.patch` and
`files.json`. `candidate.patch` covers every changed path, including secrets, and is never sent to
Kodus. Both snapshots are scoped: changed paths except secrets, then context the scope selects
within its budget; `scope.json` lists what was materialised, omitted and withheld, with reasons.
The JSON candidate identity contains `repository`, `base`, `head` (or null), `tree`, `base_tree`,
`patch_sha256`, `brief_sha256`, `snapshot_manifest_sha256`, `base_manifest_sha256`, `files_sha256`,
`scope_sha256` and the absolute artifact paths, plus `changed_paths`, `dispositions`,
`submitted_paths`, `submitted_patch_sha256`, `submitted_patch_path`, `ignored_paths`,
`withheld_paths`, `ignores_sha256`, `ignores_commit`, `ignores_source`, `ignores_path` and
`ignores_renderer_verified`. `kodus.request_sha256` hashes the exact request body.
Check both tree manifests and all artifact hashes; preserve both snapshots
unchanged. If the candidate changes during review, freeze and review the new candidate.

Read the adapter result, not just its process exit. `kodus.verdict` is `PASS`, `FAIL`, `NO_VERDICT`
or `SKIPPED`;
`kodus.context` is `partial-or-unknown`. The adapter's `overall_verdict` remains `PENDING` or `FAIL`,
and `local_review_required` is true. Adapter exit 0 means only that its Kodus component passed.
Native Kodus CLI exit 0 alone proves neither completion nor a passing review. A Kodus `FAIL` is a
set of findings to triage (below), not a failed gate, and never a reason to run Kodus again.

`SKIPPED` (with `skip_reason: all-changed-paths-ignored`, exit 0) means every changed path matched
the bound ignores, so nothing was sent and no token was read. It is not a Kodus PASS: record the
skip, its reason and the ignores identity, and let the independent verdict decide the combined
gate alone. Any other skip, or a skip whose `submitted_paths` is not empty, is no verdict.

Kodus must return an authenticated completed review. Retain the job identity, raw result, findings
and failure evidence. Authentication errors, quota exhaustion, trial fallback, skipped/partial jobs,
malformed results or unknown result semantics are no verdict. Never silently substitute a route.
Retry a product-tier run that gives no verdict once, on a fresh output path; the retry does not
count toward the two runs. If the retry gives none either, that run is unavailable: the combined
evidence records both attempts and their errors. After an unavailable first run, the final run is
due only if the candidate changes. When the run on the head to land is unavailable, the independent
PASS alone decides the gate, and its verdict binds the head by Git identity as in the tooling tier.
Never substitute another provider or key.
The service does not attest unchanged-file/sandbox availability or model routing; record those
limitations without inventing proof. The brief is retained locally, not transmitted to Kodus.

## Obtain the independent verdict

Dispatch an authorized read-only reviewer subagent with both immutable snapshots, base/range,
the full `candidate.patch` (never `submitted.patch` alone), `scope.json`, the ignores document and
sanitized brief. Ignored, binary and secret changed paths are in Kodus's blind spot, so the
independent reviewer must cover them; secrets appear only in `candidate.patch`. Do not start another top-level session. The implementation author may
not supply this verdict. If reviewer dispatch is unavailable, retain the Kodus result and block
the combined gate; do not approve it yourself.

The scoped snapshots, the full candidate patch and required base/intent context must be available
to this agent; unchanged files outside the snapshots are readable from the repository object
database at the recorded `tree` and `base_tree` if it needs them. It traces
relevant callers, contracts and tests, and records what it actually inspected; availability does
not claim that every file was read. Missing required context means `NO_VERDICT` for that component.

For the first and final candidates, require a separate local artifact with:

- The adapter's complete candidate identity and independently checked content hashes (the Git
  identity instead where the review order below says there is no adapter run to bind).
- Reviewer identity, actual inspected scope, intent considered and context limitations.
- Findings with severity, concrete evidence and a disposition for every finding.
- A `PASS`, `FAIL` or `NO_VERDICT` verdict, without unresolved critical/major findings on PASS.

Have the reviewer form its own assessment before reconciling Kodus findings. Preserve both original
results and an explicit disposition for every finding; disputes require code evidence. Fix valid
findings only within the authorized remediation scope. A changed candidate or repair delta needs a
new independent verdict; Kodus runs follow the review order below. Keep prior evidence and exact
identity links.

## Review order

Kodus is a source of findings, not a pass/fail gate. It reports something new on almost every run,
so a candidate is never repaired and re-run until Kodus passes. The independent agent review covers
the first candidate and each repair delta, and its PASS on the head to land decides the gate.

The context's review policy assigns each repository a tier; read it there, never guess it.

- **Product tier.** Kodus reviews the first candidate and, when the candidate has changed since,
  the final candidate. Two runs at most; one when the first candidate lands unchanged.
- **Tooling tier.** One advisory Kodus run on the first candidate, with no final run and no
  retry. It never blocks landing: triage what has arrived by the independent PASS on the head to
  land; triage what arrives later fix-forward or to the follow-up task. Where the repository lands
  by pull request and the hosted Kodus review runs on that pull request, the hosted review is the
  advisory run: make no local Kodus run, do not wait for it before merging, and triage its
  comments when they arrive. Otherwise the one local run passes `--fast`. A product-tier run never
  passes `--fast`.

Start each Kodus run in the background beside the independent review; never wait on it idle. Each
repair delta gets one independent review and no Kodus run of its own; that review binds the
candidate by base, head or worktree tree and full-patch hash taken from Git, and needs no adapter
artifacts.

The final candidate is the exact tree the root intends to land, after an independent PASS with no
unresolved critical/major. In a composed batch it is the task's own commit range on the composed
tree. In a loop the root runs the product tier's final Kodus run and its independent review; lanes
run the first-candidate review and the repair-delta reviews. The final independent verdict must
carry the final adapter's complete candidate identity (including repository, base, head, tree,
`candidate.patch`, brief, ignores, scope and both manifest hashes); an earlier verdict that does
not match all of it is rerun on the final artifacts. In the tooling tier, which has no final
adapter run, it binds the landing head by repository, base, head or tree and full-patch hash from
Git. The first candidate lands unchanged when the landed commit sits on the recorded base with the
same `tree` and `candidate.patch` hash; its reviews are then the final ones, despite the worktree
candidate having no `head`.

## Triage Kodus findings

The root (outside a loop, the main thread) gives every Kodus finding exactly one disposition with
code evidence. In a loop a lane returns Kodus findings untriaged; the root triages them and names
any fix in the repair round.

- Kodus `critical`, `high`, `major` and `error` findings are fix candidates. **Fix** one only when
  the code shows a real defect: reachable with an input or environment the brief's intent
  supports, and causing wrong behaviour, data loss, a security exposure or a missed acceptance
  criterion. For a security exposure, input from any party the change exposes it to always counts
  as supported, whatever the brief says.
- **Follow-up or decline** everything else: every other severity, and hardening against malformed
  local state, unsupported platforms, odd encodings, concurrent manual changes or same-second
  timing that the brief does not name. Record them on one follow-up task, or decline each with the
  evidence that it is unreachable or wrong.
- This bar applies to Kodus findings only. The independent reviewer's findings keep their own
  severities and are never narrowed by it.
- A fix from the first run goes into the first repair round, with the independent review's
  findings. A fix from the final run gets one independent delta review and then lands.
- Nothing earns a third counted Kodus run (a no-verdict retry is not counted), and nothing after
  the final run earns another: not a fix, a rebase, a renumber or a compose. Changed content after
  the final run gets an independent delta review of that change instead.
- Independent-review minors: fix the first candidate's in the same round as its majors; send
  repair-round minors to the follow-up task.

A repair round that fixes a confirmed Kodus finding counts like any other failed round for the
convergence tripwire. State the inputs, environments and platforms the change must support and its
non-goals in the sanitized intent brief, so the triage has a written definition of reachable.

## Combine evidence before landing

A lane can run the adapter file directly with its own renderer, so a lane's evidence proves
nothing by itself. Before accepting any adapter result, the root runs, through the trusted
launcher itself:

```sh
kodus-review verify --evidence /absolute/evidence.json --repo /absolute/repository
```

It re-renders the ignores document with the launcher-pinned renderer at `ignores_commit`, rehashes
every artifact and snapshot file, recomputes the candidate from Git (the recorded base and head;
for a worktree candidate the current working tree, which must equal the recorded tree, so run
verify before the worktree moves on) and requires the same per-path `dispositions`, `submitted_paths`, `submitted.patch`,
`files.json`, manifests and scope record, SKIPPED eligibility and request hash. For a PASS or FAIL
it re-fetches the Kodus job and requires the stored result to be identical and `filesAnalyzed`
equal to the submitted paths plus the notified paths (the `createdAt` against `captured_at` check binds only unedited
evidence, since the capture writes `captured_at`; it is no freshness guarantee); a network or authentication failure
is no verdict. Exit 0 with `"verdict": "VERIFIED"` is required; any mismatch or error is no verdict.
Take `base`, `head`, `tree` and `fast` from verify's output, and require `ignores_commit` to equal
the brief's pinned rules commit. A product-tier PASS or FAIL whose verified `fast` is true does not
count as that tier's run: it is no verdict for the gate, and the run made again without `--fast` is
that run's one retry. A `SKIPPED` run sent nothing, so its mode does not matter. Kodus does not echo the request it reviewed, so verify cannot prove the
job reviewed this exact submission; that residual is why the independent review stays mandatory.

The root reads both artifacts and verifies matching repository, base/head, tree, patch, brief,
ignores, scope and candidate and base manifest hashes, artifact integrity, and current candidate
identity. Bind the Kodus verdict to what Kodus received: `submitted.patch` must hash to
`submitted_patch_sha256`, `files.json` (sent diffs plus one content-free notice per withheld path,
`notified_paths`) to `files_sha256`, and the result's `filesAnalyzed` must
equal the length of `submitted_paths` plus `notified_paths`; every changed path must appear once in `dispositions`. The combined gate passes when the independent
verdict on the head to land is PASS and the Kodus component is settled. In the product tier each
due run either completed with every finding dispositioned (a fixed finding names its independent
delta review; a follow-up names its task; a declined one carries its evidence), or was `SKIPPED`
for all-changed-paths-ignored, or is recorded unavailable after one retry. In the tooling tier the
advisory run never blocks: record its state and the dispositions made so far; for a hosted advisory
run record the pull request and head instead of an adapter artifact, and `kodus-review verify` does
not apply. An independent FAIL
or NO_VERDICT, missing evidence, an identity mismatch, or a product-tier Kodus fix candidate with
no disposition blocks the combined gate. A Kodus `FAIL` verdict alone does not. The independent
review is never waived. Preserve Kodus context limitations even when the combined gate passes.

Write a fresh combined evidence artifact containing both artifact paths and hashes, candidate
identity, the tier, the Kodus run count, all finding dispositions and the combined verdict. Point the lane-return `review.evidence`
to its absolute path and name both providers in `review.provider`. An adapter result alone cannot
serve as combined PASS evidence. Never imply local review grants a required human GitHub approval
or permits bypassing required checks. Guarded work completes its reviews and triage before landing.
