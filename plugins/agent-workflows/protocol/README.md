# Loop protocol (v3)

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
- `docs/backlog-board-setup.md`: how the author sets up and migrates Backlog.md boards.

## Placeholders

- `<plugin root>` is the absolute path of this plugin's installed version, the directory that
  holds `protocol/`. The `loop` skill writes the real path into each launch file; never type the
  placeholder into a run file.
- Paths that start with `$` (`$PROTOCOL_SOURCE`, `$PROJECTS_DIR`, `$CREDENTIALS_DIR`, `$HOME`) are
  locations you supply for your own setup.
- Organisations, private repositories, hosts and credential locations from the author's setup are
  replaced with neutral examples. The documents still describe some of the author's own fleet
  procedures (a remote gate runner, a second machine, a session catalogue); adapt or skip them.

The same documents are rendered at https://rob-knight.com/reference/.
