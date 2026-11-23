# Loop protocol

These are the documents a run follows. `loop/run.md` is the whole protocol on one page, with one
appendix per harness: `loop/run-claude.md` (Claude Code, the harness this plugin supplies) and
`loop/run-pi.md`. The `loop` skill points the root at them by absolute path. The Loop v4 documents
below are kept for reference while they are retired.

- `loop/contract.md`: what every root obeys at run time: run files, queue, dispatch, review,
  landing, authority and close-out.
- `loop/harness-claude.md`: Claude Code, the harness this plugin supplies (hooks and lane agents).
- `loop/harness-codex.md` and `loop/harness-pi.md`: the same
  protocol on Codex and on pi. They are here for reference;
  this plugin does not supply their launchers, agents or hooks.
- `loop/charter.md` and `loop/lane.md`: the templates for a run's charter and its lane rules.
- `loop/planner.md`: how a run is prepared. Only the `loop` skill reads it.
- `loop/review-profiles/`: the hosted pull request reviewers a charter can select (`reviewer:`).
- `docs/backlog-board-setup.md`: how the author sets up and migrates Backlog.md boards.

## Programs

The programs the documents tell the root and the lanes to run ship in this plugin's `bin/`, which
Claude Code puts on the Bash tool's PATH while the plugin is enabled: `loop-lint`, `loop-land`,
`loop-teardown`, `run-check` and `wait-until`. By absolute path they are in `<plugin root>/bin/`.

Where the documents say `just tracker <command>` (`queue`, `close`, `handoff` and the rest), they mean a
`tracker` recipe in your repository's own `justfile` that runs the shipped tool,
`<plugin root>/tools/tracker/tracker.py`. It is a thin shim over `gh` and runs as your ambient `gh` login:

```just
export TRACKER_REPO := "OWNER/REPO"

# Absolute path of this plugin's installed version, the directory that holds `protocol/`.
plugin_root := "/absolute/path/to/agent-workflows"

# The arguments reach the tool as given: free text is neither split on spaces nor read by the shell.
# The loop's tracker command: queue, view, create, claim, status, handoff, tick, close.
[positional-arguments]
tracker *args:
    @python3 {{plugin_root}}/tools/tracker/tracker.py "$@"
```

The tool needs Python 3.12 or later: name that interpreter in the recipe if `python3` is older.

The queue is the repository's open issues labelled `ready`; `blocked`, `needs-owner` and `p1` to `p3` are the
other working labels, and the tool creates a missing one before it adds it. `close` posts an evidence comment
(`--pr`, `--why` or `--evidence-file`; at least one is required) and closes the issue. `<plugin root>/tools/tracker/README.md` lists every
command and the optional guard that refuses raw `gh` writes to a GitHub Project.

A Backlog.md board does not use the tracker tool. There the `backlog` CLI lists, closes and hands
over tasks, so use it wherever the documents name `just tracker`.

## Placeholders

- `<plugin root>` is the absolute path of this plugin's installed version, the directory that
  holds `protocol/`. Claude Code writes the real path into the `loop` skill when it loads it; never
  type the placeholder into a run file.
- Paths that start with `$` (`$PROTOCOL_SOURCE`, `$PROJECTS_DIR`, `$CREDENTIALS_DIR`, `$HOME`) are
  locations in the author's setup that you supply for your own. `$PROTOCOL_SOURCE` is the author's
  checkout of these documents; the files the board guide names in it (`registry.tsv`, `bin/sync`,
  `bin/codex-reconcile`, `bin/conformance`, `sources/`) are not shipped.
- Organisations, private repositories, hosts and credential locations from the author's setup are
  replaced with neutral examples. The documents still describe some of the author's own fleet
  procedures (a remote gate runner, a second machine, a session catalogue, publishing the loop
  pointer, the code search tool `jg`); adapt or skip them.

The same documents are rendered at https://rob-knight.com/reference/.
