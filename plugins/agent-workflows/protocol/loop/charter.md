# Charter (v4 template)

The loop skill writes `codex/charter-<date>-loop<N>.md` in this shape. Keep it under 8 KB: it
states intent and authority; the tracker holds the work. `loop-lint charter` checks it.

```markdown
# Charter: <repo> loop<N>

protocol:   v4
harness:    claude | codex | pi
tracker-repo: <optional OWNER/REPO, where the issues live when not in this repository>
host:       <optional machine name; pi refuses the launch on any other host>
root:       <model> / <effort>
aim:        <one or two sentences: what this run is for>
priorities: <ordered tracker selectors, e.g. label:priority-high > milestone:M7 > status:"To Do">
exclude:    <task IDs or #issue numbers (title) and labels never to admit, e.g. ABC-0123 (example task); label:needs-owner>
lanes:      <cap on live implementation lanes>
gate:       <exact gate command>
gate-route: <remote runner + slot pacer from AGENTS.md, else the recipe or slot pool that runs heavy legs off the workstation; then the fallback>
guarded-paths: <globs beyond the contract's built-in guarded set, or none>
standing:   <abs path to the Standing file> sha256=<hex>
authority:
  - <each extra grant, one per line: live reads, tenants, infra, deploy targets>
live:       <what the root may read live and with which tool, never a credential value>
preflight:
  - <exact privileged command, as it will run> | proves: <what a pass shows> | rows: <task IDs that need it, or all>
loop-land:  <omit; write off only to land as v3 did: no ledger, no ledger check by the hook>
overnight:  <the mandate when the owner says "overnight mode", e.g. keep building waves from the board in priority order>
budget:     <optional wall-time end, ISO UTC, or none>

## First wave

| Task | Scope | Route | Note |
|---|---|---|---|
| <ID> (<title>) | <files> | worker, composer or senior (why) | <migration number, dependency, ruling> |

## Decisions already made

- <owner rulings the root must apply, one per line, with the task they settle>
```

Rules:

- Drop the optional `tracker-repo:` line when unused.
- `exclude:` and `priorities:` are what `loop-lint queue` checks every wave against.
- `preflight:` lists the privileged commands the run is known to need (a deploy, an SSH step, a
  secret write, a merge), each in the exact form it will run, with what it proves and which rows
  need it. The root runs each while the owner is present (contract section 7); write `none` when
  the run needs no privileged command.
- `loop-land: off` turns the landing tool off for the run; leave the line out otherwise.
- The first wave holds at most about 20 rows. The root grows the queue from the tracker after that.
- Put repository facts in `LOOP.md`, never in the charter.
- No protocol text: the root already reads the contract and its harness file.
