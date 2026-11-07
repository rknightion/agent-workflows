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
combined gate and its activation proof is independent.

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
Native Kodus CLI exit 0 alone proves neither completion nor a passing review.

`SKIPPED` (with `skip_reason: all-changed-paths-ignored`, exit 0) means every changed path matched
the bound ignores, so nothing was sent and no token was read. It is not a Kodus PASS: record the
skip, its reason and the ignores identity, and let the independent verdict decide the combined
gate alone. Any other skip, or a skip whose `submitted_paths` is not empty, is no verdict.

Kodus must return an authenticated completed review. Retain the job identity, raw result, findings
and failure evidence. Authentication errors, quota exhaustion, trial fallback, skipped/partial jobs,
malformed results or unknown result semantics are no verdict. Never silently substitute a route.
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

Require a separate local artifact with:

- The adapter's complete candidate identity and independently checked content hashes.
- Reviewer identity, actual inspected scope, intent considered and context limitations.
- Findings with severity, concrete evidence and a disposition for every finding.
- A `PASS`, `FAIL` or `NO_VERDICT` verdict, without unresolved critical/major findings on PASS.

Have the reviewer form its own assessment before reconciling Kodus findings. Preserve both original
results and an explicit disposition for every finding; disputes require code evidence. Fix valid
findings only within the authorized remediation scope. Changed candidates or repair deltas require
new evidence from both components; keep prior evidence and exact identity links.

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
equal to the submitted paths (the `createdAt` against `captured_at` check binds only unedited
evidence, since the capture writes `captured_at`; it is no freshness guarantee); a network or authentication failure
is no verdict. Exit 0 with `"verdict": "VERIFIED"` is required; any mismatch or error is no verdict.
Take `base`, `head` and `tree` from verify's output, and require `ignores_commit` to equal the
brief's pinned rules commit. Kodus does not echo the request it reviewed, so verify cannot prove the
job reviewed this exact submission; that residual is why the independent review stays mandatory.

The root reads both artifacts and verifies matching repository, base/head, tree, patch, brief,
ignores, scope and candidate and base manifest hashes, artifact integrity, and current candidate
identity. Bind the Kodus verdict to what Kodus received: `submitted.patch` must hash to
`submitted_patch_sha256`, `files.json` (sent diffs plus one content-free notice per withheld path,
`notified_paths`) to `files_sha256`, and the result's `filesAnalyzed` must
equal the length of `submitted_paths` plus `notified_paths`; every changed path must appear once in `dispositions`. Both components must PASS, except that a Kodus `SKIPPED` for all-changed-paths-ignored
leaves the independent PASS as the deciding verdict; FAIL, NO_VERDICT, missing evidence or an
identity mismatch blocks the combined gate. Neither review
substitutes for the other. Preserve Kodus context limitations even when the combined gate passes.

Write a fresh combined evidence artifact containing both artifact paths and hashes, candidate
identity, all finding dispositions and the combined verdict. Point the lane-return `review.evidence`
to its absolute path and name both providers in `review.provider`. An adapter result alone cannot
serve as combined PASS evidence. Never imply local review grants a required human GitHub approval
or permits bypassing required checks. Guarded work completes both reviews before landing.
