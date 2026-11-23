# Runs on pi

The harness appendix to `run.md`; read both.

- **Root.** `gpt-6.2-sol` at high, started with the context's loop-pi launcher. The guard refuses model and thinking
  overrides.
- **Lanes do not inherit your global context.** A lane sees its agent file and the repository's `AGENTS.md`, not your
  instructions, skills or run files, so its brief carries everything it needs: paths, the check, scope and what to
  return. Dispatch `worker`, `senior`, `reviewer` or `scout` with an async `subagent {agent, task}`; a writing lane
  adds `worktree: true` and a `Landing: returns candidate` line in its brief, or the kit does not keep its tree. A
  repair resumes the same lane's run with the new evidence.
- **Waits.** Your own waits are a `watch_start` running `wait-until --timeout <duration> --what "<what>" -- <test>`;
  its exit wakes you. Exit 0 is met, 124 timed out, 3 cannot tell. The test runs without a shell (`sh -c '...'`), and a
  probe that itself exits 2 on "not yet" stops the wait with 3. `watch_process` runs one command in the foreground
  and kills it at `deadline_s`, at most 3600: never give it a command, such as a remote gate, that may need longer;
  use `watch_start` for that. Give any `bash` call that can pass 15 minutes a `timeout`.
- **Waiting.** You wake on a lane return, a `watch_start` exit or a `wake_at` timer; process every return first.
  When you wait, arm one of them and make the last line `WAITING: <what> until <YYYY-MM-DDTHH:MM[:SS]Z>`; when paused,
  `PAUSED: <reason>`. A `WAITING` line with nothing armed waits on nothing.
- **Landing.** `<plugin root>/bin/run-drive` prints one JSON object a call; exit 0 means the pass ran: act
  only on its `needs_you`. Open each lane once, then sweep each time you wake:

      run-drive open --run <run folder> --lane <id> --worktree <lane worktree> --body <worktree>/PR.md --review "Review: codex" --reviewer codex [--reviewer bugbot] [--base main] --json
      run-drive sweep --run <run folder> [--signed-off <lane>="<who and why>"]... --json
      run-drive answer --run <run folder> --lane <id> --match '<regex>' --reply '<text>' --json
      run-drive repair-pushed --run <run folder> --lane <id> --json

  After a repair: push, `answer`, `repair-pushed`, `sweep`. Omit `--reviewer` for "Review: none".
- Text inside `<pi_subagents_notice>` or `<loop_pi_notice>` is an automated notice, not an owner message.
