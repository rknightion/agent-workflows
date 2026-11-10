# kodus-review

An authenticated Kodus review component plus a retained exact Git candidate for a separate local
agent review. Python 3.11+ and Git 2.40 or later are required; no third-party Python packages are
used.

```sh
kodus-review --repo /absolute/repo --base HEAD --brief /absolute/intent.txt --ignores /absolute/ignores.json --out /absolute/new-review.json
kodus-review --repo /absolute/repo --base BASE_SHA --head HEAD_SHA --brief /absolute/intent.txt --ignores /absolute/ignores.json --out /absolute/new-review.json
kodus-review verify --evidence /absolute/new-review.json --repo /absolute/repo
```

## The ignores document

`--ignores` is required. It names the repository's scope document rendered from the central policy
source at a full commit, next to the brief render step:

```sh
just ignores OWNER/REPO FULL_SHA > /absolute/ignores.json   # in the central policy checkout
```

The document is exactly these keys (`kodus-review-ignores/2`):

| Key | Meaning |
|---|---|
| `schema`, `source`, `commit`, `repository` | `kodus-review-ignores/2`, the policy repository, its full lowercase commit, the reviewed `owner/repo` |
| `patterns` | ignore patterns: changed paths they match are not sent to Kodus |
| `context_include` | patterns for unchanged files copied into the local snapshots as context |
| `same_directory` | also copy unchanged siblings of changed files as context |
| `generated_markers` | treat files the committed tree's `.gitattributes` marks (`linguist-generated`, `linguist-vendored`, `-diff`) and `Code generated ... DO NOT EDIT` files as generated: never context, no Kodus content |
| `send_changed_content` | send each sent changed file's full text as Kodus `content` |
| `secret_add` | patterns **added** to the built-in secret-file list |
| `max_context_file_bytes`, `snapshot_budget_bytes`, `kodus_content_bytes` | per context file, both snapshots together, Kodus content; ceilings 2 MiB, 48 MiB, 4 MiB |

The adapter refuses a missing or relative path, any other key set (so `secret_exclude` and
`inherit_default` are refused), schema or commit form, duplicate keys, a `repository` that is not
the reviewed origin (compared case-insensitively), a non-boolean flag, a limit outside `1..ceiling`,
more than 500 patterns in a list, a repeated pattern, and any pattern that is empty, over 200
characters, has leading or trailing whitespace, starts with `/`, `!` or `./`, contains a backslash,
a control character, `{`, `}`, `[` or `]`, an extended glob (`!(`, `@(`, `+(`, `?(`, `*(`), or a
`..`, `.` or empty path segment. A pattern whose every segment is only `*` and `?` would match every
path and is refused. These are exactly the central validator's rules. Parse errors, including
deeply nested JSON, are no verdict (exit 2). The bytes are read once, hashed and copied into the
artifacts as `ignores.json`.

The launcher must set `KODUS_REVIEW_IGNORES_RENDERER` to the absolute path of an executable regular
file that renders the central document. After validating `--ignores`, the adapter runs
`<renderer> --repo <document repository> --commit <document commit>` (argument list, no shell,
`GIT_*` removed from the environment, 30-second timeout, stdout capped at 1 MiB) and requires its
stdout to equal the `--ignores` bytes exactly. A nonzero exit, timeout, oversized output or any
difference is no verdict, so no hand-written document can narrow what Kodus sees. Unset, the run is
refused, except at the synthetic localhost fixture (plain HTTP at 127.0.0.1 with a `kodus_test_`
key), where evidence records `ignores_renderer_verified: false`; a bound run records `true`.

## One matcher

Every path decision is a Git glob pathspec rooted at the top (`:(top,glob)<pattern>`): the Kodus
selection, snapshot context, secret matching and the submitted aggregate patch. `*` and `?` do not
cross `/`, a leading `**/` matches any leading directories including none, and a trailing `/**`
matches everything below. A path is ignored when a wildcard pattern (one with `*` or `?`) matches
it as a Git glob pathspec, or when it equals a wildcard-free pattern exactly. A wildcard-free
pattern never covers paths below it, even when it names a directory: `docs/metrics.md` does not
ignore `docs/metrics.md/evil.go`, and `vendor` does not ignore `vendor/a.js` (while `vendor/lib.js`
listed as well still ignores that file). To ignore a directory, write `dir/**`. `**/dist` alone does not hide `x/dist/y.py`; `**/dist/**` does. Secret patterns add the
`icase` magic, so `.ENV`, `ID_RSA` and `*.PEM` match.

## What happens to each changed path

Each changed path gets exactly one disposition, in this precedence:

1. `secret-pattern`: matches the built-in secret list or `secret_add` (case-insensitive).
2. `secret-copy`: either side's blob equals a non-empty secret-pattern blob from the full base or
   candidate tree, or Git copy detection (`diff-tree --find-copies-harder`, 50% similarity, Git's
   default) derives it from one. This catches `cp .env config/app.txt` of an unchanged secret, an
   edited rename (`.env` to `app.conf` plus a line) and an edited former copy. Git-ignored files
   on disk that match the secret patterns (an uncommitted `.env`) seed the same set.
   Detection pairs only blobs within a 2x size window of a secret blob (Git cannot score others
   at 50%; the window is a bisection over sorted secret sizes) and is bounded explicitly, checked
   on the unfiltered counts before any scan: over 2,000,000 pairs or 30,000 blobs, a Git warning that
   detection was skipped, or more than 300 seconds is no verdict, never an unchecked pass.
3. `ignored`: matches `patterns`.
4. `binary`: the blob on either side has a NUL byte in its first 8000 bytes (Git's rule, and the
   only one; at most 8000 bytes are read at any size). Content alone decides: Git attributes and config never change a disposition, so a
   committed `.gitattributes` `binary` on a text file still sends it.
5. `sent`: everything else.

The built-in secret list is secret FILE shapes only: `.env`, `.env.*`, `*.env`, keys and stores
(`*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.ppk`, `*.jks`, `*.keystore`, `*.asc`, `id_rsa*`,
`id_ed25519*`, `id_ecdsa*`, `id_dsa*`), `credentials` and `secrets` files and directories,
`credentials.json`, `service-account*.json`, `.netrc`, `.git-credentials`, `.npmrc`, `.pypirc`,
`.pgpass`, `.htpasswd`, `kubeconfig`, `.kube/config`, `.docker/config.json`, `*.tfvars`,
`*.tfstate`, `*.tfstate.backup`, `*.kdbx`, `secring.gpg`, `*.p8`, `.envrc`, `.terraformrc`,
`terraform.rc`, `*.age`, and `*secret*.EXT` for config and data extensions. Source named
after secrets (`internal/auth/secret_reveal.go`) is reviewed. No document key, ignore pattern or
`drop_shared` entry can un-withhold a secret.

Only `sent` paths go to Kodus as diffs. Secret, ignored and binary paths are never sent to Kodus: no
diff and no content. Kodus is still told that each of them is part of the change, so it does not
report a missing file: when a request is made, `config.files` gets one notice per withheld path,
after the sent entries, with the real status, empty `content`, and a one-line hunk such as

    @@ -0,0 +1 @@
    +[kodus-review: this changed file is part of the change but withheld from this review (ignored by the repository review scope); its content is not shown; 311843 bytes]

(`@@ -1 +0,0 @@` with a `-` line for a deletion). The class is `ignored by the repository review
scope`, `binary`, `secret file` or `copy of a secret file`; secret paths carry only their name and
class, other paths also their size (candidate side, base side for a deletion), never a blob id,
which for a small file could be brute-forced back to its content. A withheld path over the API's
500-character limit is never listed individually; it is counted in the grouped line. Notices
are not in the raw `diff` field, which the service `git apply`s in its sandbox. Sent paths always
get their real entries (over 500 of them is no verdict). Notices fill the rest of the 500-file
request limit in order: secret, then binary, then ignored, each by path. If not all fit, the last
notice also carries `+[kodus-review: N further changed paths are withheld from this review and not
listed individually: ignored a, binary b, secret c]` (on the last notice with a new side; only if
every notice is a deletion does it go on the last one, as a second `-` line under `@@ -1,2 +0,0 @@`). Each withheld disposition records `notified`
as `individual`, `grouped` or false, and a false one carries `notified_reason`: `skipped` (SKIPPED,
no request is made) or `no-slot` (no request entry remained to carry a notice, for example when
exactly 500 sent paths fill the limit; sent paths keep priority);
`notified_paths` lists the individual notices. The full `candidate.patch`, including secret diffs, is written locally for the
independent reviewer and is the only local carrier of secret content; secrets are never copied into
either snapshot.

If every changed path matches `patterns`, there is no request and no token read: `kodus.verdict` is
`SKIPPED` with `kodus.skip_reason: all-changed-paths-ignored`, exit 0. Otherwise, if no path is
`sent` (everything outside the ignores is binary or secret), the result is no verdict
(`No reviewable text changes`).

Kodus transport limits apply to sent paths only: the submitted patch is at most 1.5 MB, UTF-8 and
free of binary markers; at most 500 sent files (checked on every changed path outside the ignores
and secret rules, binaries included, before any per-file probe), 500 characters per path and 500,000 bytes per file
patch; sent names stay inside the raw-diff grammar (ASCII letters, digits, `_./@+ -`, no edge spaces
in a component, no ` b/` or `diff --git`). Unsent paths outside that grammar are recorded in
`unsent_paths_outside_grammar`, never refused. Every changed path must still be representable
locally (UTF-8, no backslash or control character) and no changed path may be a symlink or gitlink:
those refuse, because the mandatory local reviewer must see every change. A changed path, or an
on-disk secret path considered for copy detection, whose name contains `*`, `?`, `[` or `]` is
also no verdict naming it: Git matches glob pathspecs literally too, so `docs/*/y.go` would be
ignored by `docs/*`.

A sent path is never silently withheld. If one cannot be submitted whole, the run is no verdict
and the reason names it: a side that is not valid UTF-8 (a Latin-1 comment in source is still
source), a per-file diff over 500,000 bytes, a name outside the raw-diff grammar or over 500
characters; an aggregate over 1.5 MB names its largest diff. A large text file with no NUL in its
head is sent as a diff, its content withheld as over budget, provided its diff fits.

Each sent changed file carries its full text as `content` when `send_changed_content` is true,
in path order within `kodus_content_bytes`. Content is withheld, with the reason recorded and the
diff still sent, when the file is generated, over 2,000,000 characters, past the budget (decided
from the recorded size before any read), or content is disabled.

## Caller-controlled Git state

After the working-tree snapshot, every read (diffs, trees, blobs, attributes, copy detection) runs
against a throwaway bare Git directory that borrows the repository's objects through
`objects/info/alternates`, with system and global config off and replace objects and grafts off.
`.git/config`, `.git/info/attributes`, `core.attributesFile`, `core.bigFileThreshold`,
`core.ignorecase`, textconv and external diff drivers and replace refs therefore cannot change a
diff, a disposition or a content decision. Sent diffs use `--text`. Only the committed tree's
`.gitattributes` (read with `check-attr --source=<tree>`) feeds `generated_markers`. The
working-tree snapshot itself runs with `core.ignorecase=false`; clean filters still apply to it as
they would to a commit.

## Scoped snapshots

`snapshot/` and `base-snapshot/` hold the scoped paths only, read-only. In order: every changed path
except secrets (sent, ignored and binary alike, wherever each side exists), then unchanged
`context_include` matches, then unchanged same-directory siblings, each group sorted by path.
Context excludes ignored, secret-pattern, exact secret-blob, generated, symlink and gitlink
entries. After the ordered-prefix budget selection, Git copy detection (50%) runs on the admitted
context only; a near-copy of a secret is dropped and listed as omitted `secret-copy`, and the
freed budget is not refilled. A directory with tens of thousands of siblings therefore still gets
a verdict, with the overflow listed as `budget`; the copy-detection bounds still fail closed on the
admitted set.
A context file over `max_context_file_bytes` is omitted as `file-size`; the rest is an ordered
prefix under `snapshot_budget_bytes` (both snapshots together). Once one file would exceed it, it
and every later file are omitted as `budget` and no further blob is read. Changed files are never
truncated: if they alone exceed the budget the result is no verdict. Scope comes from the central
rules, never a raised limit. The full trees stay readable from the object database at the recorded
`tree` and `base_tree`.

## Capture

Without `--head`, a separate temporary Git index captures final tracked, staged, unstaged and
nonignored untracked working bytes. Explicitly staged ignored files remain included; ignored
untracked files remain excluded. A tracked file replaced by a directory is refused before staging,
preventing recursive force-add of ignored children. The caller's index is preserved. With `--head`,
the selected committed tree is reviewed regardless of live working changes. Git objects for captured
trees may be added to the repository object database; no commit, ref, branch or working file is
changed. The working candidate is rechecked before submission and after the response.

Patches disable Git color and relative paths, use full object names (`--full-index`; Git's
abbreviation length varies with the object count), explicit `a/` and `b/` prefixes, and disable
rename detection: a rename is a deletion plus an addition. `candidate.patch` covers every changed
path; Git writes it straight to disk, it is hashed from disk and a full patch over 64 MiB is
refused. `submitted.patch` is the exact `diff` sent to Kodus. The request uses `config.files` with
path, status, a per-file diff and `content`; per-file patches must concatenate to the submitted
patch byte for byte, the submitted path list must equal the sent paths, and with nothing withheld
the submitted patch must equal `candidate.patch`. Each per-file extraction excludes descendants of
that path, preserving file/directory replacements. The aggregate is an inclusion list of exactly
the sent paths, so a withheld file replaced by a directory never drops its sent children. This grammar and structured converter were
exercised against the upstream CLI converter at
`kodustech/kodus-ai@2ee5d3257d900944b792a00cf63e60c2ae8fa9fa`; it does not prove the hosted service
runs that exact revision.

## Launcher environment

The trusted launcher must establish `KODUS_REVIEW_CONTEXT=personal`, `KODUS_REVIEW_TOKEN_FILE`, an
exact GitHub repository allowlist (`KODUS_REVIEW_ALLOWED_REPOS` or `KODUS_REVIEW_ALLOWED_REPOS_FILE`),
denied backup/runtime roots (`KODUS_REVIEW_DENIED_ROOTS` or `KODUS_REVIEW_DENIED_ROOTS_FILE`), the
renderer, and `KODUS_REVIEW_GIT`, the absolute path to the executable Git it pins. There is no
PATH fallback: unset, relative or non-executable is no verdict (and a `verify` mismatch), except
unset inside the synthetic localhost fixture. Git below 2.40 is refused before anything else. Missing configuration fails closed; private identities belong in that deployment
configuration, never in this public package. Backup/runtime paths, bare repositories and origins
outside GitHub are refused even when a caller supplies an allowlist.

The API origin defaults to `https://api.kodus.io`. `KODUS_REVIEW_API_URL` permits an explicit HTTPS
origin; redirects and ambient HTTP proxies are disabled. Plain HTTP is supported only at 127.0.0.1
with a `kodus_test_` synthetic key. `KODUS_REVIEW_TIMEOUT` defaults to 900 seconds, maximum 3600 (hosted jobs have taken 35 minutes);
`KODUS_REVIEW_POLL_INTERVAL` defaults to 1 second, maximum 5. There is one enqueue request,
authenticated status polling and no anonymous/trial fallback or automatic paid POST retry. Only
idempotent GETs (job polls and verify's job re-fetch) are retried, on HTTP 502, 503, 504, 524 and
transport failures with backoff of 2, 4, 8, 16 then 30 seconds, and on 429 after its `Retry-After`,
always inside the overall deadline; every retry is recorded under `kodus.retries`. Any other 4xx
is terminal, and a failed enqueue POST is never retried. All
refusals happen before the token is read or the network is used.

## Evidence

The exclusive output path has an adjacent `.artifacts` directory containing `snapshot/`,
`base-snapshot/`, `manifest.json`, `base-manifest.json`, `candidate.patch`, `submitted.patch`,
`files.json`, `scope.json`, `ignores.json` and `brief.txt`, all read-only. An existing output is
never replaced. Evidence inside the source repository must be ignored by Git, and so must everything under its
`.artifacts` directory: the evidence file and `candidate.patch` (secret diffs included) are checked
before writing, and once every artifact exists Git must report nothing addable under the directory
(`ls-files --others --exclude-standard`), else the run is no verdict naming the first addable path.
verify repeats that check, so a file added there later is caught. Ignore the whole directory
(`*.artifacts/` or the folder holding the evidence) rather than individual files. A same-user process can
still modify the artifacts, so consumers recompute hashes before trusting them.

Schema version 2 records `candidate` with repository, base, head, `tree`, `base_tree`,
`changed_paths`, `dispositions` (every changed path with its status and exactly one disposition),
`submitted_paths`, `ignored_paths`, `withheld_paths`, `all_changed_paths_ignored`,
`unsent_paths_outside_grammar`, `patch_sha256`/`patch_path` (the full candidate patch),
`candidate_patch_bytes`, `candidate_patch_utf8`, `candidate_patch_has_binary`,
`submitted_patch_sha256`/`submitted_patch_path`, `files_sha256`/`files_path` (hashed before
sending), manifest hashes and paths, `scope_sha256`/`scope_path`, brief hash and path,
`ignores_sha256`, `ignores_commit`, `ignores_source`, `ignores_path` and
`ignores_renderer_verified`. `scope.json` records the effective scope and secret list, tree sizes,
every materialised path with its reason and blobs, every omitted context path with its reason, the
excluded-context count, budget use, and under `kodus` the diff paths, every withheld file with its
reason and blobs, every content path with its hash and size, and every withheld content path.
`kodus` holds `request_sha256` and `request_bytes` (recorded before the POST), the component
verdict, job identity, raw result and `context: partial-or-unknown`. Returned `filesAnalyzed` must
equal the submitted-path count plus the notices (the service counts `config.files`); that is a
sanity check, not semantic coverage proof. The brief is
retained for the independent local agent and is **not sent to Kodus**.

Exits: 0 means the **Kodus component** reported no blocking findings or was `SKIPPED`; 1 means
critical/high/major/error findings; 2 means no usable verdict, including every refusal and any
unexpected adapter failure. No exit certifies an overall PASS. `local_review_required` is always
true; `overall_verdict` is PENDING or FAIL. A separate independent agent reviews the full
`candidate.patch` and both snapshots with the sanitized intent and writes its own candidate-bound
artifact; the root checks both before declaring the combined gate passed.

`--fast` (review command only, not `verify`) adds `"fast": true` to the request's `config`, next to
`config.files`, and records `kodus.fast: true` in the evidence before submission; without it the
request bytes are unchanged and the field is absent. Everything else sent is identical. `verify`
takes the mode from the evidence (`kodus.fast` must be a boolean when present) and recomputes the
request hash under that mode, so evidence whose recorded mode does not reproduce `request_sha256`
fails. Its report carries `fast` (true or false) so the root can refuse a fast run where the review
policy requires a normal one.

## verify

`kodus-review verify --evidence ABS --repo ABS` is for the root, run through the trusted launcher
(it refuses without `KODUS_REVIEW_IGNORES_RENDERER`). Anyone can run the adapter file directly with
a renderer of their choosing, so evidence alone is never trusted, and verify trusts no path or size
from it: every artifact is taken from the evidence file's own `.artifacts` directory at its fixed
name (evidence naming any other path is a mismatch), each file is opened without following a link,
must be a regular file and is refused above a fixed bound (the evidence itself 64 MiB, the full
patch 64 MiB, the submitted patch 1.5 MB, `files.json` 64 MiB, `scope.json` 32 MiB, manifests
16 MiB, `ignores.json` 1 MB, brief 100 KB), and each snapshot walk refuses links and stops at
200,000 entries or 48 MiB. Capture enforces each of these same constants where it writes the
artifact, before any request, so nothing it produces can fail verify on size: an artifact over its
bound is no verdict naming it; the evidence must leave 40 MiB for the Kodus responses and is
reduced to a no-verdict record if they overflow it; context beyond the snapshot entry bound is
omitted as `budget` like the byte budget, and changed files alone over it are no verdict.
verify requires
`ignores_renderer_verified`, rehashes every artifact, re-binds `ignores.json` to the launcher's
renderer, recomputes the candidate from Git at the recorded base and head (for a worktree candidate
it recomputes the working-tree tree exactly as capture does and requires it to equal the recorded
`tree`, which the evidence author could otherwise choose; if the worktree has moved on, re-run the
review), and requires identical tree hashes, patch hashes,
`dispositions`, `submitted_paths`, `ignored_paths`, `withheld_paths`, `files.json`, manifests and
`scope.json`, identical snapshot files, SKIPPED eligibility, the recorded request hash, a result
whose `filesAnalyzed` matches and a verdict consistent with its findings. Copy detection is
seeded from the trees plus the on-disk secret hashes recorded in `scope.json`
(`ondisk_secret_blobs`, hashes only): verify re-reads and re-hashes the gitignored secret files on
disk, and a missing or changed file is a mismatch. They are hashed into the throwaway view only,
never into the candidate repository. For a PASS or FAIL it fetches `GET /cli/review/jobs/<job_id>` with the same
authenticated client (launcher token file and endpoint; nothing recorded in evidence chooses
them, and evidence captured against a different origin, or against the localhost test fixture
when verify is not itself in it, is refused; no redirects, no proxies) and requires status
`COMPLETED`, a result identical to the stored one, `filesAnalyzed` equal to the submitted-path
plus notice
count, and the recorded `createdAt`, no earlier than the recorded capture time minus 120 seconds
of clock skew. `captured_at` is written by whoever ran the capture and is not recomputed, so this
check binds only unedited evidence; it is not a freshness guarantee and sits inside the residual
below. Any network or authentication failure is `MISMATCH`. SKIPPED has no job and
needs no fetch. It prints one JSON line naming `base`, `head`, `tree`, `base_tree` and `job_id`, so
the root binds those from the verified output; exit 0 means `VERIFIED`, exit 2 `MISMATCH`.

Residual: Kodus does not echo the request it reviewed. verify proves the job exists with this result,
not when the capture happened or that the job reviewed this submission: a lane that submits a
different request under its own job can still produce a matching record. The independent local
review remains the second, required component.

The wire contract is pinned to Kodus CLI 0.4.21: authenticated `POST /cli/review` with `X-Team-Key`
and `X-Kodus-Async: 1`, then authenticated `/cli/review/jobs/{jobId}`. Unexpected legacy synchronous
responses fail closed. No sessions, traces, credentials or local snapshots are uploaded.

For centrally managed policies, the caller renders applicable rules from an approved immutable
central Git commit into the brief, with source identity, rule IDs and the SHA-256 of the rules
block. The brief hash binds rules and intent. Private rules remain in private evidence, never in
public source or published findings.
