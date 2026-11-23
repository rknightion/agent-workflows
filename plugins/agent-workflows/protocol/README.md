# Loop protocol

These are the documents a run follows. `loop/run.md` is the whole protocol on one page, with one
appendix per harness: `loop/run-claude.md` (Claude Code, the harness this plugin supplies) and
`loop/run-pi.md` (pi, for reference; this plugin does not supply its launchers or agents). The `loop`
skill points the root at them by absolute path.

## Programs

The programs the documents tell the root and the lanes to run ship in this plugin's `bin/`, which
Claude Code puts on the Bash tool's PATH while the plugin is enabled: `run-check`, `run-drive` and
`wait-until`. By absolute path they are in `<plugin root>/bin/`.

## Tracker recipe

The run brief reads open GitHub issues labelled `ready`. To write them through the shipped tracker
tool instead of raw `gh`, add a `tracker` recipe to your repository's own `justfile` that runs
`<plugin root>/tools/tracker/tracker.py`. It is a thin shim over `gh` and runs as your ambient `gh` login:

```just
export TRACKER_REPO := "OWNER/REPO"

# Absolute path of this plugin's installed version, the directory that holds `protocol/`.
plugin_root := "/absolute/path/to/agent-workflows"

# The arguments reach the tool as given: free text is neither split on spaces nor read by the shell.
# The loop's tracker command: queue, view, create, claim, status, handoff, tick, close.
[positional-arguments]
tracker *args:
    @sh {{plugin_root}}/scripts/py3 {{plugin_root}}/tools/tracker/tracker.py "$@"
```

The tool needs Python 3.15 or later; `scripts/py3` finds it and says how to get it if it is missing.

The queue is the repository's open issues labelled `ready`; `blocked`, `needs-owner` and `p1` to `p3` are the
other working labels, and the tool creates a missing one before it adds it. `close` posts an evidence comment
(`--pr`, `--why` or `--evidence-file`; at least one is required) and closes the issue. `<plugin root>/tools/tracker/README.md` lists every
command and the optional guard that refuses raw `gh` writes to a GitHub Project.

## Placeholders

- `<plugin root>` is the absolute path of this plugin's installed version, the directory that
  holds `protocol/`. Claude Code writes the real path into the `loop` skill when it loads it; never
  type the placeholder into a run file.
- Names from the author's setup (an operator, a review table in the agent home, a remote gate) are
  kept generic. Adapt or skip those steps.

The same documents are rendered at https://rob-knight.com/reference/.
