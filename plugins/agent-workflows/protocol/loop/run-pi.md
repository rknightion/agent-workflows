# Runs on pi

The harness appendix to `run.md`.

- **Root.** `gpt-6.2-sol` at high, started with the context's loop-pi launcher; the guard refuses model and thinking overrides.
- **Launch.** pi has no `/loop` or run command: a launch prompt is plain text with no leading `/`, opening
  `Run protocol v5: read <plugin root>/protocol/loop/run.md and run-pi.md, then <goal>.` loop-pi's continuation
  arms only on v3 launch text, so nothing arms it for v5: wait as below.
- **Lanes do not inherit your context.** A lane sees its agent file and the repository's `AGENTS.md`, not your
  instructions, skills or run files, so its brief carries paths, the check, scope, what to return. Dispatch `worker`, `senior`, `reviewer`, `scout`
  with an async `subagent {agent, task}`; a writing lane adds `worktree: true` and a `Landing: returns candidate` line, or the kit drops its tree. A repair resumes that lane's run.
- **Waits.** Your waits are a `watch_start` running `wait-until --timeout <duration> --what "<what>" -- <test>`;
  its exit wakes you: 0 met, 124 timed out, 3 cannot tell. The test runs without a shell (`sh -c '...'`); a probe that exits 2 on "not yet" ends it with 3. `watch_process` runs one foreground command and kills
  it at `deadline_s` (at most 3600): anything longer, such as a remote gate, goes to `watch_start`. Give a `bash` call that can pass 15 minutes a `timeout`.
- **Waiting.** You wake on a lane return, a `watch_start` exit or a `wake_at` timer; process returns first.
  To wait, arm one; last line `WAITING: <what> until <YYYY-MM-DDTHH:MM[:SS]Z>`; when paused,
  `PAUSED: <reason>`. `WAITING` with nothing armed waits on nothing.
- **Landing.** `<plugin root>/bin/run-drive` prints one JSON object a call; exit 0 means the pass ran: act
  only on its `needs_you`. Open each lane once, then sweep on each wake:

      run-drive open --run <run folder> --lane <id> --worktree <lane worktree> --body <worktree>/PR.md --review "Review: codex" --reviewer codex [--reviewer bugbot] [--base main] --json
      run-drive sweep --run <run folder> [--signed-off <lane>="<who and why>"]... --json
      run-drive answer --run <run folder> --lane <id> --match '<regex>' --reply '<text>' --json
      run-drive repair-pushed --run <run folder> --lane <id> --json

  After a repair: push, `answer`, `repair-pushed`, `sweep`. Omit `--reviewer` for "Review: none".
- Text in `<pi_subagents_notice>` or `<loop_pi_notice>` is an automated notice, not an owner message.
