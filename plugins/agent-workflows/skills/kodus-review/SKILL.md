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

Use the installed, validated adapter interface:

```sh
kodus-review --repo /absolute/repository --base FULL_BASE_SHA --brief /absolute/brief-file --out /absolute/new-evidence.json
```

Add `--head FULL_HEAD_SHA` for a committed range. Without `--head`, the candidate is the final
worktree, including untracked files. Use a fresh output path; preserve prior evidence.

The adapter creates an adjacent `.artifacts/` directory containing `snapshot/`, `manifest.json`,
`candidate.patch` and `brief.txt`. Its JSON candidate identity contains `repository`, `base`,
`head` (or null), `tree`, `patch_sha256`, `brief_sha256`, `snapshot_manifest_sha256` and absolute
`snapshot_path`, `manifest_path`, `patch_path`, `brief_path`. It also retains `base_snapshot_path`,
`base_manifest_path`, `base_manifest_sha256` and `changed_paths` for deleted-file and base context.
Check both tree manifests and all artifact hashes; preserve both snapshots
unchanged. If the candidate changes during review, freeze and review the new candidate.

Read the adapter result, not just its process exit. `kodus.verdict` is `PASS`, `FAIL` or `NO_VERDICT`;
`kodus.context` is `partial-or-unknown`. The adapter's `overall_verdict` remains `PENDING` or `FAIL`,
and `local_review_required` is true. Adapter exit 0 means only that its Kodus component passed.
Native Kodus CLI exit 0 alone proves neither completion nor a passing review.

Kodus must return an authenticated completed review. Retain the job identity, raw result, findings
and failure evidence. Authentication errors, quota exhaustion, trial fallback, skipped/partial jobs,
malformed results or unknown result semantics are no verdict. Never silently substitute a route.
The service does not attest unchanged-file/sandbox availability or model routing; record those
limitations without inventing proof. The brief is retained locally, not transmitted to Kodus.

## Obtain the independent verdict

Dispatch an authorized read-only reviewer subagent with both immutable snapshots, base/range,
patch and sanitized brief. Do not start another top-level session. The implementation author may
not supply this verdict. If reviewer dispatch is unavailable, retain the Kodus result and block
the combined gate; do not approve it yourself.

The full candidate tree and required base/intent context must be available to this agent. It traces
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

The root reads both artifacts and verifies matching repository, base/head, tree, patch, brief and
candidate and base manifest hashes, artifact integrity, and current candidate identity. Both components must PASS;
FAIL, NO_VERDICT, missing evidence or an identity mismatch blocks the combined gate. Neither review
substitutes for the other. Preserve Kodus context limitations even when the combined gate passes.

Write a fresh combined evidence artifact containing both artifact paths and hashes, candidate
identity, all finding dispositions and the combined verdict. Point the lane-return `review.evidence`
to its absolute path and name both providers in `review.provider`. An adapter result alone cannot
serve as combined PASS evidence. Never imply local review grants a required human GitHub approval
or permits bypassing required checks. Guarded work completes both reviews before landing.
