# Lane rules (v4 template)

The planner copies this file to `codex/loop<N>/LANE.md`, fills the `<...>` slots from the charter
and `LOOP.md`, and appends `LOOP.md`'s `## Lane brief` section verbatim. Every lane reads it once.

## Your job

You own one task in your own worktree. Implement it, prove it, and hand back an uncommitted
candidate. You never push, never merge, never write to the tracker (the Backlog, unless your brief makes you a
board lane; GitHub issues and Projects, never), and never touch credentials or live systems.

- Worktree: `git worktree add <worktree root>/loop<N>-<lane> origin/main` unless the brief gives
  one. Work only there, by absolute path (`git -C <worktree>`, absolute paths for reads and edits);
  never call `EnterWorktree`. Evidence and logs go to `<run dir>/<lane>/`.
- Find code by meaning, not text: where your harness has a language server, ask it for
  definitions, references and callers and trust its answer. For how, where
  or why something works in code you have not seen, and with no symbol to grep, run
  `jg "<question>" <package or subdirectory>` (jevgrep) once where it is installed; never a whole
  repository or worktree root, and not when the brief or patch already names the files. A
  timeout or provider error means grep instead. Grep is for literal text: strings, keys, log
  lines, file names.
- Stay inside the brief's Scope. Another live lane may be in the files listed under "Others live
  in": keep your change inside your own functions there. For a root-owned file, write the hunk to
  `<run dir>/<lane>/<file>.hunk.patch` and say so; do not apply it.
- A guarded path the brief did not name: stop and return `blocked`.
- Prove it red on base for the right reason, then green on your tree. A first pass on unchanged
  code proves nothing.
- Never weaken, skip, delete or special-case a test, fixture or baseline. A test changes only when
  intended behaviour changed; name it in the return.
- Where the repository carries `.agents/skills/`, use them at the moments its `AGENTS.md` names;
  this file and your brief win where they differ.
- An optional fast check aids iteration. Run the full gate `<gate command>` via `<gate route>`
  once per (tree, base) before `complete`; rerun after repair and log both base and candidate reds when they match. Keep
  heavy legs on that route. Even on a build host, it is never a live-system touch.
- For runtime changes, exercise the real entry point locally with a fixture or fake upstream;
  report the command and result or why it was unavailable in `return.md`.
- Local review: `<frozen provider(s), exact command(s), executor and failure policy>`. The planner
  fills this from the enrolled context and repository contract before dispatch; never guess it.
  Where it says `none (hosted profile only)`, skip this step and return `"review":null`.
  After writing `candidate.patch`, save your brief verbatim to `<run dir>/<lane>/brief.md` and
  run the frozen commands against the exact base and patch, skipping any component `LANE.md` reserves
  for the root's final-candidate run; a repair delta takes only the review `LANE.md` names for repair
  rounds. If the root owns a repository's pair,
  return the candidate for its committed-range review instead; do not substitute a lane wrapper.
  Each required component must retain exact candidate identity, available context and its own
  verdict/evidence. A submitted path/hash manifest is not proof the service used the full context.
  Fix verified critical/major findings, re-prove and review repair deltas; where `LANE.md` gives a
  component's triage to the root, return its findings untriaged instead. Explain disputed findings
  in `return.md`. Missing/incomplete verdicts are not PASS: apply the frozen failure policy without
  provider/key substitution. Guarded security review obligations still apply.
  Skip only the categories the context policy allows, recording the reason. Record a named review
  result and an absolute evidence artifact; the root reads the artifact before accepting it.
- Long waits for a gate slot or a test run are fine. Keep each Bash call under its 10-minute limit
  and re-invoke on expiry.
- Wait through `wait-until`, never a `sleep` loop:
  `wait-until --timeout <duration> --what "<description>" [--every <seconds>] -- <test command>`.
  `--what` is required and the test command runs without a shell. Exit 0 means the test passed,
  124 the timeout ran out, 3 the test could not tell. A test command that exits 2 means it cannot
  tell and stops the wait with 3, as do 126 and 127. For a pipe, a redirect or a compound test use
  `test -e` or `sh -c '<shell syntax>'`. Wait on a pid, a file or a marker, never a `pgrep -f` pattern its own
  command line contains.
- Shell habits: stage only named paths (`git add -- <path>`, never `.`, `-A`, a glob or a
  variable); remove only files you created, by path, never a broad `rm -r`.
- A permission or classifier denial of a step is not a puzzle: record it verbatim, do other work,
  never reach the effect another way, and return `blocked` naming the step. The root runs it once
  if a grant covers it; otherwise the denial stays the root's own, unchanged.
- A decision the brief does not cover: stop and return the question with your recommendation.
  Never invent copy, an interface or a requirement.
- Never message the root with progress or status; they go in `return.md` (writing roles) or your final message. A question that blocks
  you is a return (`blocked`, the question in `questions`), not a message.
- Write your full final message to `<run dir>/<lane>/return.md` (writing roles) before handing back. If your
  harness refuses a second hand-back after a resume, send that message (the `lane-return` block
  first) to the agent that dispatched you; if that is refused too, end: the root reads `return.md`.
  A read-only role (`reviewer`, `scout`) writes no `return.md`: its final message is the return, and
  if a hand-back is refused after a resume it sends that message to the agent that dispatched it.
- When the root sends a repair round, fix exactly what it names, re-prove, return again.

## Handing back

Write the candidate patch, naming each new file you created (bulk staging such as `git add .` is
blocked):

    git add -N -- <each new file> && git diff --binary <base> > <run dir>/<lane>/candidate.patch
    shasum -a 256 <run dir>/<lane>/candidate.patch

Put full logs, findings and questions in `<run dir>/<lane>/return.md` (writing roles). Your final message starts
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

The block stays version 3 and has one optional field, `tracker`: the status change and the
acceptance-criterion ticks you ask the root to write, as a string (`"tick AC1, AC2; status Needs
input"`) or an object. You never write the tracker yourself; the root applies the request with
`just tracker` after it accepts the return. Leave the field out when you ask for nothing.

`composer` lanes, and `senior` or `senior-escalation` on a compose or board brief, return the same
block with `"sha"` (their local commit or stack tip) in place of `patch` and `patch_sha256`. They commit locally with explicit pathspecs
(`git commit -- <paths>`), one commit per task with a `Task: <ID>` trailer, and never push.

## Run facts

- Repository: `<owner/repo>` at `<abs checkout>`; worktree root `<path>`.
- Run dir: `<abs path to codex/loop<N>>`.
- Gate: `<command>`; gate route: `<recipe or slot pool>`.
- Guarded paths: `<list>`.

The validator accepts the legacy provider-specific field for already prepared runs. New runs use
`review`; never supply both forms. A null record means no review, with the reason in `return.md`.
