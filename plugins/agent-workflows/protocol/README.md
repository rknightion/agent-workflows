# Loop protocol (v4)

These are the documents a loop runs on. The `loop` skill reads them while it prepares a run, and the
launch file it writes names the contract and one harness file by absolute path, so the root reads
them from this directory.

- `loop/contract.md`: what every root obeys at run time: run files, queue, dispatch, review,
  landing, authority and close-out.
- `loop/harness-claude.md`: Claude Code, the harness this plugin supplies (hooks and lane agents).
- `loop/harness-codex.md` and `loop/harness-pi.md`: the same
  protocol on Codex and on pi. They are here for reference;
  this plugin does not supply their launchers, agents or hooks.
- `loop/charter.md` and `loop/lane.md`: the templates for a run's charter and its lane rules.
- `loop/planner.md`: how a run is prepared. Only the `loop` skill reads it.
- `loop/review-profiles/`: the hosted pull request reviewers a charter can select (`reviewer:`), and
  `local-pair` for a run with no hosted reviewer.
- `docs/backlog-board-setup.md`: how the author sets up and migrates Backlog.md boards.

## Programs

The programs the documents tell the root and the lanes to run ship in this plugin's `bin/`, which
Claude Code puts on the Bash tool's PATH while the plugin is enabled: `loop-lint`, `loop-land`,
`loop-teardown` and `wait-until`. By absolute path they are in `<plugin root>/bin/`.

`just tracker` (with `queue`, `close` and `handoff`) is the author's own tracker command and is not
shipped. Where the documents name it, use your tracker's equivalent: the queue command your
`LOOP.md` names under `## Queue`, `gh issue close` (or a `Closes #N` line in the landing pull
request) to close an issue, and a label or comment of your choosing to hand an issue to the owner.
On a Backlog.md board, the `backlog` CLI does all three.

## Placeholders

- `<plugin root>` is the absolute path of this plugin's installed version, the directory that
  holds `protocol/`. The `loop` skill writes the real path into each launch file; never type the
  placeholder into a run file.
- Paths that start with `$` (`$PROTOCOL_SOURCE`, `$PROJECTS_DIR`, `$CREDENTIALS_DIR`, `$HOME`) are
  locations in the author's setup that you supply for your own. `$PROTOCOL_SOURCE` is the author's
  checkout of these documents; the files the board guide names in it (`registry.tsv`, `bin/sync`,
  `bin/codex-reconcile`, `bin/conformance`, `sources/`) are not shipped.
- Organisations, private repositories, hosts and credential locations from the author's setup are
  replaced with neutral examples. The documents still describe some of the author's own fleet
  procedures (a remote gate runner, a second machine, a session catalogue, publishing the loop
  pointer, the code search tool `jg`); adapt or skip them.

The same documents are rendered at https://rob-knight.com/reference/.
