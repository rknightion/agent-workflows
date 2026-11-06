# Lane rules (v3 template)

The planner copies this file to `codex/loop<N>/LANE.md`, fills the `<...>` slots from the charter
and `LOOP.md`, and appends `LOOP.md`'s `## Lane brief` section verbatim. Every lane reads it once.

## Your job

You own one task in your own worktree. Implement it, prove it, and hand back an uncommitted
candidate. You never push, never merge, never write to the tracker (the Backlog, unless your brief makes you a
board lane; GitHub issues and Projects, never), and never touch credentials or live systems.

- Worktree: `git worktree add <worktree root>/loop<N>-<lane> origin/main` unless the brief gives
  one. Work only there, by absolute path (`git -C <worktree>`, absolute paths for reads and edits);
  never call `EnterWorktree`. Evidence and logs go to `<run dir>/<lane>/`.
- Find code by meaning, not text: ask the language server for definitions, references and
  callers (the LSP tool, or `gopls` MCP tools where present) and trust its answer. For how, where
  or why something works in code you have not seen, and with no symbol to grep, run
  `jg "<question>" <package or subdirectory>` (jevgrep) once where it is installed; never a whole
  repository or worktree root, and not when the brief or patch already names the files. A
  timeout or provider error means grep instead. Grep is for literal text: strings, keys, log
  lines, file names.
- Stay inside the brief's Scope. Another live lane may be in the files listed under "Others live
  in": keep your change inside your own functions there. For a root-owned file, write the hunk to
  `<run dir>/<lane>/<file>.hunk.patch` and say so; do not apply it.
- A guarded path the brief did not name: stop and return `blocked`.
- Prove it red first: a check that fails on the base for the right reason, then passes on your
  tree. A check that passes on its first run against unchanged code asserts nothing.
- Never weaken, skip, delete or special-case a test, fixture or baseline. A test changes only when
  intended behaviour changed; name it in the return.
- Gate: `<gate command>`, run through `<gate route>`; run heavy legs nowhere else. Gate once per
  (tree, base); a red that also fails on base is not yours: list both. A repaired tree is a new
  tree: rerun the gate before returning `complete`. Running the gate route is never a live-system
  touch, even when it reaches a build host.
- Local review: `<frozen provider(s), exact command(s), executor and failure policy>`. The planner
  fills this from the enrolled context and repository contract before dispatch; never guess it.
  After writing `candidate.patch`, save your brief verbatim to `<run dir>/<lane>/brief.md` and
  run the frozen commands against the exact base and patch. If the root owns a repository's pair,
  return the candidate for its committed-range review instead; do not substitute a lane wrapper.
  Each required component must retain exact candidate identity, available context and its own
  verdict/evidence. A submitted path/hash manifest is not proof the service used the full context.
  Fix verified critical/major findings, re-prove and review repair deltas. Explain disputed findings
  in `return.md`. Missing/incomplete verdicts are not PASS: apply the frozen failure policy without
  provider/key substitution. Guarded security review and local-pair obligations still apply.
  Skip only the categories the context policy allows, recording the reason. Record a named review
  result and an absolute evidence artifact; the root reads the artifact before accepting it.
- Long waits (a gate slot, a test run) are fine: your prompt cache lasts an hour. Keep each Bash
  call under its 10-minute limit and re-invoke on expiry.
- Shell habits the hooks deny, each costing a turn: stage only named paths (`git add -- <path>`,
  never `.`, `-A`, a glob or a variable); wait on the command itself or the harness's monitor,
  never with `sleep`; search with `rg`, not `grep`; remove only files you created, by path, never
  a broad `rm -r`.
- A decision the brief does not cover: stop and return the question with your recommendation.
  Never invent copy, an interface or a requirement.
- Never message the root with progress or status; they go in `return.md`. A question that blocks
  you is a return (`blocked`, the question in `questions`), not a message.
- Write your full final message to `<run dir>/<lane>/return.md` before handing back. If your
  harness refuses a second hand-back after a resume, send that message (the `lane-return` block
  first) to the agent that dispatched you; if that is refused too, end: the root reads `return.md`.
- When the root sends a repair round, fix exactly what it names, re-prove, return again.

## Handing back

Write the candidate patch, naming each new file you created (bulk staging such as `git add .` is
blocked):

    git add -N -- <each new file> && git diff --binary <base> > <run dir>/<lane>/candidate.patch
    shasum -a 256 <run dir>/<lane>/candidate.patch

Put full logs, findings and questions in `<run dir>/<lane>/return.md`. Your final message starts
with exactly this block (compact JSON, at most 1,800 characters), then at most a few prose lines:

````
```lane-return
{"v":3,"lane":"<id>","task":"<ID>","status":"complete|partial|blocked|failed",
 "base":"<full SHA>","worktree":"<abs path>","patch":"<abs path>","patch_sha256":"<hex>",
 "check":"<exact command>","exit":<int>|null,"tail":"<last lines, at most 40>",
 "review":{"provider":"<frozen route>","evidence":"<abs artifact>","verdict":"PASS|FAIL|none","major":<n>,"fixed":<n>,"disputed":<n>}|null,
 "questions":["<short; details in return.md>"]}
```
````

Compose and board lanes return the same block with `"sha"` (their local commit or stack tip) in
place of `patch` and `patch_sha256`. They commit locally with explicit pathspecs
(`git commit -- <paths>`), one commit per task with a `Task: <ID>` trailer, and never push.

## Run facts

- Repository: `<owner/repo>` at `<abs checkout>`; worktree root `<path>`.
- Run dir: `<abs path to codex/loop<N>>`.
- Gate: `<command>`; gate route: `<recipe or slot pool>`.
- Guarded paths: `<list>`.

The validator accepts the legacy provider-specific field for already prepared runs. New runs use
`review`; never supply both forms. A null record means no review, with the reason in `return.md`.
