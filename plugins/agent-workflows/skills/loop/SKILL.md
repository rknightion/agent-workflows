---
name: loop
description: Start a run in this session. You become the root that works the owner's goal across many tasks and lands them by pull request, attended or not. Invoke as /loop <what he wants>.
---

# loop: run mode

`/loop <what the owner wants>` starts a run in this session, now, and you are its root. There is no separate
preparation session, charter, launch file or lint.

1. Read `${CLAUDE_PLUGIN_ROOT}/protocol/loop/run.md` and its Claude appendix
   `${CLAUDE_PLUGIN_ROOT}/protocol/loop/run-claude.md` in full. They are the whole protocol: never restate
   them here or in the run files. Claude Code writes this plugin's absolute install path into both paths
   when it loads this skill; if one does not start with `/`, the path was not substituted: stop and
   say so.
2. Do what they say, beginning with Start while the owner is present: read the repository, ask everything in
   one batch with the question prompt, write the run files, then work.

What this plugin's hooks expect:

- The run folder is `codex/run-<YYYYMMDD>-<n>/` in the repository checkout, `n` counting from 1 each
  day. `AUTHORITY.md` starts `# Authority: <repo> run-<YYYYMMDD>-<n>`, then `protocol: v5`. The closeout
  is `CLOSEOUT.md` there: `# Closeout: <repo> run-<YYYYMMDD>-<n>`, then `protocol: v5`, then
  `## Landed` (`- #<n> <title>` per pull request), `## Parked`, `## Decisions needed` and
  `## Spend by role`.
- When the owner says he is leaving, write an empty `UNATTENDED` file in the run folder; remove it when he
  says he is back. While it is there and `CLOSEOUT.md` is not, the question prompt is refused for you
  (park the question in `PARKED.md`), and ending your turn sends you back to work up to three times in
  a row. When you are waiting on lanes, checks or a script, end the reply with a line starting
  `WAITING:`. Lanes are never affected.
- Lanes are the pinned role agents `agent-workflows:worker`, `senior`, `senior-escalation`,
  `reviewer`, `scout` and `composer`. A return is plain text whose first line is its `Status:`.
  Read it and repair what you need; nothing rejects a return over its format.

Codex is not a run root: in Codex, say so and stop.
