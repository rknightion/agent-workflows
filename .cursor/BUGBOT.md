# Review guide

This repository is a Claude Code plugin marketplace with one plugin, `plugins/agent-workflows`. It
packages a run protocol (the root session works a goal across many tasks and hands pieces to pinned
subagent lanes), the hooks and programs that enforce it, and two standalone tools. The code is Python
3.15 using only the standard library, plus one POSIX shell launcher and one shell hook script. There
are no third-party dependencies and no lockfiles.

## How it is checked

- `just check` is the gate and the only CI job (`.github/workflows/ci.yml`, Python 3.15). It runs
  `just fmt-check`, `just lint` (byte-compiles the scripts and tools, parses the `bin/` programs,
  `git diff --check`) and `just test` (`python -m unittest discover -s tests`).
- Tests are `unittest` files in `tests/`. They build temporary repositories and use a stand-in `gh`
  (`plugins/agent-workflows/tools/tracker/fake_gh.py`), so nothing touches a real home or the network.
- `leak-patterns.public.txt` and `leak-scan.repo` drive the privacy scan of the published tree.

## Where a bug costs most

- `plugins/agent-workflows/scripts/run_gate.py` and `lane_return.py` are Claude Code hooks. A hook that
  cannot start must exit 1 (non-blocking). Exit 2 blocks the session, so flag any path that can reach
  it by accident. Both must act only on the main thread of a session whose transcript names a run
  folder, never on a subagent, and must let the call or the stop through on any fault of their own.
- `plugins/agent-workflows/bin/run-check` and `run-drive` decide whether a pull request may land and
  enable auto-merge on an exact head commit. They must never merge directly and never wait without a
  bound. Flag a check that can pass on a stale head, a guarded diff that is not refused, or a missing
  timeout on a subprocess or `gh` call. `wait-until` exits 124 on timeout and must keep doing so.
- `plugins/agent-workflows/scripts/py3` starts programs on Python 3.15. It walks `PATH` by hand so a
  relative or empty element never runs a file from the caller's directory, and it never lets `uv` run
  inside the caller's repository. Flag anything that weakens either.
- `plugins/agent-workflows/tools/tracker/` is a shim over `gh` for issues and labels. Writing verbs
  must read the issue first and refuse a pull request number before any write. Output is one JSON
  object on stdout.
- `plugins/agent-workflows/tools/dsreview/` calls a model API: flag a secret in an argument list, a log
  or a test fixture.

## Consistency to check

- The six agent files in `plugins/agent-workflows/agents/` pin a model and an effort each. The hook
  matcher in `hooks/hooks.json` and the README table must name the same six roles.
- A version bump touches `plugins/agent-workflows/.claude-plugin/plugin.json` and the marketplace entry in
  `.claude-plugin/marketplace.json` together.
- A `${CLAUDE_PLUGIN_ROOT}/...` path must name a file that exists in the plugin.

## What not to flag

Formatting, naming taste and long prose lines. A new behaviour without a test that fails first is worth
a comment; a typo in a comment is not.
