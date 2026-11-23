# Runs on Claude Code

The harness appendix to `run.md`; read both.

- **Root.** Opus 5.5 at high. The plugin refuses `AskUserQuestion` in the session while a file `UNATTENDED` exists in
  the run folder; to remove it for the whole session, launch with
  `claude --model claude-opus-5-5 --effort high --disallowedTools AskUserQuestion`.
- **Hand over** at about 250k tokens of context: bring `RESUME.md` up to date, then a fresh session started as above
  reads it and carries on.
- **Lanes** are the pinned role agents: `agent-workflows:worker`, `agent-workflows:senior`,
  `agent-workflows:reviewer` and `agent-workflows:scout`. The pin fixes model and effort: pass no `model`. Dispatch
  unnamed, in the background, with a `description` of `<lane> <task>`. A return arrives as a task notification;
  send a repair to the same agent with `SendMessage`. `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` caps every running
  subagent, read-only ones included.
- **Worktrees.** Make each lane's worktree yourself with `git worktree add` (`isolation: "worktree"` branches from the
  default branch, not your base). Keep your shell at the repository checkout; never `cd` into a worktree, use
  `git -C` and absolute paths.
- **Waits.** One `run_in_background` Bash command, `wait-until --timeout <duration> --what "<what>" -- <test>`; its
  exit wakes you. Exit 0 is met, 124 timed out, 3 cannot tell. The test runs without a shell, so shell syntax needs
  `sh -c '...'`, and a probe that itself exits 2 on "not yet" stops the wait with 3. Bash calls time out at 10
  minutes: run anything longer in the background.
- **Landing.** `<plugin root>/bin/run-drive` prints one JSON object a call; exit 0 means the pass ran: act
  only on its `needs_you`. Open each lane once, then sweep each time you wake:

      run-drive open --run <run folder> --lane <id> --worktree <lane worktree> --body <worktree>/PR.md --review "Review: codex" --reviewer codex [--reviewer bugbot] [--base main] --json
      run-drive sweep --run <run folder> [--signed-off <lane>="<who and why>"]... --json
      run-drive answer --run <run folder> --lane <id> --match '<regex>' --reply '<text>' --json
      run-drive repair-pushed --run <run folder> --lane <id> --json

  After a repair: push, `answer`, `repair-pushed`, `sweep`. Omit `--reviewer` for "Review: none".
- **Code mode stays on.** A script may do the waiting and polling (checks, review verdicts, merges) so the model is
  called only when something needs judgement.
