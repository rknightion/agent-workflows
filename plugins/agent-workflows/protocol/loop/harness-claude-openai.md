# Harness: claude-openai (v3)

Claude Code on GPT models through a LiteLLM gateway, in its own `~/.claude-openai` home. Same
plugin, Stop hook and roles as `harness-claude.md`; read that file's Permissions and owner
questions, Dispatch, Waits and Turn endings sections too. Only the launch and routes differ.

## Launch

    cmd: claude-openai --model lane-main
    You are the root. Read <plugin root>/protocol/loop/contract.md, <plugin root>/protocol/loop/harness-claude-openai.md,
    <plugin root>/protocol/loop/harness-claude.md and <abs charter path> in full. Write <abs closeout path> last, then reconcile and reply (contract section 12).
    Standing: <abs path> sha256=<hex>
    <each charter authority: or live: line touching customer data or a live system, verbatim>

- `lane-main` runs `gpt-6.1-sol` at high; the home's lane table sets both and no flag changes them.
- Start at 6 to 8 live lanes and raise the count only while your own model time stays under about
  half of wall time.

## Routes

Each pin is a gateway lane with a fixed model and effort (the gateway's lane table).

| Role | `subagent_type` | Model / effort |
|---|---|---|
| implementation (default) | `agent-workflows:worker` | gpt-6.1-sol / medium |
| judgement, compose, board batches | `agent-workflows:senior` | gpt-6.1-sol / high |
| escalation | `agent-workflows:senior-escalation` | gpt-6-astra / high |
| review and security review | `agent-workflows:reviewer` | gpt-6.1-sol / high |
| read-only mapping and sweeps | `agent-workflows:scout` | gpt-6.1-sol / low |

- Pass no `model` and no `effort`: the lane table decides both.
- `EnterWorktree` is denied in this home; lanes work in their worktree by absolute path.
- Auto mode runs Claude Code's own classifier.
