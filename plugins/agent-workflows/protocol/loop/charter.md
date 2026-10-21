# Charter (v3 template)

The loop skill writes `codex/charter-<date>-loop<N>.md` in this shape. Keep it under 8 KB: it
states intent and authority; the board holds the tasks. `loop-lint charter` checks it.

```markdown
# Charter: <repo> loop<N>

harness:    claude | codex | pi
host:       <optional machine name; pi refuses the launch on any other host>
root:       <model> / <effort>
aim:        <one or two sentences: what this run is for>
priorities: <ordered board selectors, e.g. label:priority-high > milestone:M7 > status:"To Do">
exclude:    <task IDs (title) and labels never to admit, e.g. ABC-0123 (example task); label:needs-owner>
lanes:      <cap on live implementation lanes>
gate:       <exact gate command>
gate-route: <remote runner + slot pacer from AGENTS.md, else the recipe or slot pool that runs heavy legs off the workstation; then the fallback>
guarded-paths: <globs beyond the contract's built-in guarded set, or none>
standing:   <abs path to the Standing file> sha256=<hex>
authority:
  - <each extra grant, one per line: live reads, tenants, infra, deploy targets>
live:       <what the root may read live and with which tool, never a credential value>
overnight:  <the mandate when the owner says "overnight mode", e.g. keep building waves from the board in priority order>
budget:     <optional wall-time end, ISO UTC, or none>

## First wave

| Task | Scope | Route | Note |
|---|---|---|---|
| <ID> (<title>) | <files> | worker or senior (why) | <migration number, dependency, ruling> |

## Decisions already made

- <owner rulings the root must apply, one per line, with the task they settle>
```

Rules:

- `exclude:` and `priorities:` are what `loop-lint queue` checks every wave against.
- The first wave holds at most about 20 rows. The root grows the queue from the board after that.
- Put repository facts in `LOOP.md`, never in the charter.
- No protocol text: the root already reads the contract and its harness file.
