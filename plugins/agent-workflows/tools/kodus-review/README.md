# kodus-review

An authenticated Kodus review component plus a retained exact Git candidate for a separate local
agent review. Python 3.11+ and Git are required; no third-party Python packages are used.

```sh
kodus-review --repo /absolute/repo --base HEAD --brief /absolute/intent.txt --out /absolute/new-review.json
kodus-review --repo /absolute/repo --base BASE_SHA --head HEAD_SHA --brief /absolute/intent.txt --out /absolute/new-review.json
```

The trusted launcher must establish `KODUS_REVIEW_CONTEXT=personal`,
`KODUS_REVIEW_TOKEN_FILE`, and an exact GitHub repository allowlist through
`KODUS_REVIEW_ALLOWED_REPOS` (comma/whitespace separated) or `KODUS_REVIEW_ALLOWED_REPOS_FILE`.
The launcher must also supply `KODUS_REVIEW_DENIED_ROOTS` (absolute paths separated by the OS
path separator) or `KODUS_REVIEW_DENIED_ROOTS_FILE` (one absolute backup/runtime root per line).
Missing denied-root configuration fails closed; private identities belong in that deployment
configuration, never in this public package. Neither owner wildcards nor missing scope are accepted. The standalone program checks these inputs;
it cannot independently authenticate a caller-supplied context label. Backup/runtime paths, bare
repositories and origins outside GitHub are refused even when a caller supplies an allowlist.
A trusted installer must pin context, token path, scope and API origin rather than inherit overrides.

The API origin defaults to `https://api.kodus.io`. `KODUS_REVIEW_API_URL` permits an explicit HTTPS
origin; redirects and ambient HTTP proxies are disabled. Plain HTTP is supported only at 127.0.0.1
with a `kodus_test_` synthetic key. `KODUS_REVIEW_TIMEOUT` defaults to 900 seconds, maximum 1800;
`KODUS_REVIEW_POLL_INTERVAL` defaults to 1 second, maximum 5. HTTP operations have bounded socket
waits and response sizes; Git commands have 30-second timeouts. There is one enqueue request,
authenticated status polling and no anonymous/trial fallback or automatic paid POST retry.

Without `--head`, a separate temporary Git index captures final tracked, staged, unstaged and
nonignored untracked working bytes. Explicitly staged ignored files remain included; ignored
untracked files remain excluded. A tracked file replaced by a directory is refused before staging,
preventing recursive force-add of ignored children. The caller's index is preserved. With `--head`,
the selected committed tree is reviewed regardless of live working changes. Git objects for captured trees may
be added to the repository object database; no commit, ref, branch or working file is changed.
The working candidate is rechecked before submission and after the response; a later landing must
still compare its current candidate with the evidence. Existing Git clean filters affect Git's
candidate bytes as they do an ordinary commit.

The exclusive output path has an adjacent `.artifacts` directory containing `snapshot/`,
`base-snapshot/`, their manifests, `candidate.patch` and `brief.txt`. Both complete trees are retained
as read-only regular files. Their file hashes/modes/blob IDs and the patch/brief hashes bind the
reviewed identity. The directory is private; a same-user process can still modify filesystem
artifacts, so consumers must recompute hashes before trusting them. Evidence inside the source
repository must be ignored by Git. An existing output is never replaced by a new invocation.

Version 1 refuses all symlinks (including safe internal ones), gitlinks/submodules, binary or non-UTF8
changed files, unsupported changed-path quoting, trees over 64 MiB each, patches over
1.5 MB, and briefs over 100 KB. It never drops these paths to obtain a passing result. Unchanged binary assets are retained byte-for-byte in both local snapshots and manifests; they
are never silently excluded.

Schema version 1 records `candidate` with repository, base/head/tree, changed paths, snapshot/base
manifest hashes and paths, patch hash/path and brief hash/path. `kodus` contains its component
verdict, job identity, raw result and `context: partial-or-unknown`. Returned `filesAnalyzed` must
match submitted changed-path cardinality; that is only a sanity check, not semantic coverage proof.
The service may review without a sandbox or omit repository context; this component does not claim
otherwise. The service currently rejects `config.focus`, so the brief is retained for the independent
local agent and is **not sent to Kodus**. The remote-available base is sent as `mergeBaseSha`; a
local-only base may prevent the server sandbox from reproducing the tree, another reason the local
review is mandatory.

Exits: 0 means the **Kodus component** reported no blocking findings; 1 means critical/high/major/error
findings; 2 means no usable verdict, including empty diffs, unknown severity, authentication failure,
invalid result, count mismatch, timeout or changed candidate. Medium/warning/low/minor/info findings
remain in the result for disposition. No exit certifies an overall PASS.

`local_review_required` is always true; `overall_verdict` is PENDING or FAIL, never PASS. A separate
independent agent must inspect the exact candidate and base snapshots, patch and sanitized intent,
and write its own candidate-bound review artifact. The root checks both artifacts, all identities
and finding dispositions before declaring the combined gate passed. No self-review, missing local
artifact, stale snapshot or Kodus-only success satisfies that gate. Findings remain untrusted data.

The wire contract is pinned to installed Kodus CLI 0.4.21: authenticated `POST /cli/review` with
`X-Team-Key` and `X-Kodus-Async: 1`, followed by authenticated `/cli/review/jobs/{jobId}`. Unexpected
legacy synchronous responses fail closed. No sessions, traces, credentials or whole local snapshot
are uploaded; the external request contains the diff and repository/base metadata.

For centrally managed policies, the caller renders applicable rules from an approved immutable
central Git commit into the brief, together with source identity, rule IDs and the SHA-256 of the
rules block. The existing brief hash binds both rules and task intent. The independent local agent
verifies that provenance and scope; the adapter does not resolve policy or transmit the brief. This
supports CLI-only repositories without asserting that the hosted service applies their rules.
Private rules remain in private evidence, never in public source or published findings.
