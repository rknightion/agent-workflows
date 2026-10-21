# dsreview

`dsreview` is a code reviewer for loop lanes. It sends one candidate change and the brief it was
written against to DeepSeek Flash through Cloudflare AI Gateway, lets the model read the repository
at the candidate state through three read-only tools (`read_file`, `grep`, `list_files`), and prints
a JSON verdict: PASS or FAIL with findings. The lane template in `protocol/loop/lane.md` tells a
lane to run it on its candidate before handing back.

On a replay of 59 reviews that Opus reviewer lanes had already done, tool-using DeepSeek Flash
matched them: each caught 8 of the 11 known defects, neither blocked a sound change, and a review
cost about $0.05 against about $1.09 for Opus.

It is one Python file with no dependencies beyond the standard library, plus its system prompt
(`system.md`).

## What leaves your machine

The diff, the brief and whatever files the model asks to read are sent to DeepSeek through your
gateway. The tools see a scratch copy of tracked and non-ignored files only: never `.git`, never
ignored files such as `.env`, and never anything outside the repository. Because the code leaves
your machine, `dsreview` refuses every repository until you name the owners it may review with
`DSREVIEW_ALLOWED_OWNERS`, and it checks the repository's `origin` against that list before it
sends anything.

## Requirements

- Python 3.12 or later, and git.
- `rg` (ripgrep) is used for the `grep` tool when it is installed; otherwise `grep -rE`.
- A Cloudflare AI Gateway with a DeepSeek provider key stored in it (see below).

## Install

The plugin ships the tool at `<plugin root>/tools/dsreview/`. Put a small launcher on your PATH
that sets your configuration and runs it, for example `~/.local/bin/dsreview`:

```sh
#!/bin/sh
export DSREVIEW_CF_ACCOUNT_ID=your-account-id
export DSREVIEW_CF_GATEWAY=reviews
export DSREVIEW_TOKEN_FILE="$HOME/.config/dsreview/token"
export DSREVIEW_ALLOWED_OWNERS=your-github-user,your-org
exec python3 /path/to/plugin/tools/dsreview/dsreview.py "$@"
```

Copy `dsreview.py` and `system.md` somewhere stable instead if you prefer: the script reads
`system.md` from its own directory.

## Configuration

| Variable | Meaning |
|---|---|
| `DSREVIEW_CF_ACCOUNT_ID` | Your Cloudflare account id. |
| `DSREVIEW_CF_GATEWAY` | The AI Gateway name. With the account id this gives `https://gateway.ai.cloudflare.com/v1/<account>/<gateway>/deepseek/chat/completions`. |
| `DSREVIEW_URL` | A full chat completions URL. Overrides the two above, for a custom domain or another proxy. |
| `DSREVIEW_TOKEN` | The AI Gateway token, sent as `cf-aig-authorization: Bearer <token>`. |
| `DSREVIEW_TOKEN_FILE` | A file holding that token, used when `DSREVIEW_TOKEN` is unset. |
| `DSREVIEW_ALLOWED_OWNERS` | Comma-separated owners whose repositories may be reviewed. A bare `owner` matches that owner on any host; `host/owner` (for example `github.com/your-org`) matches only that host. Unset refuses everything. |
| `DSREVIEW_MODEL` | The model name. Default `deepseek-flash`. |

A missing gateway, token or owner list is an error with exit status 2, and nothing is sent.

## Usage

```sh
# The uncommitted worktree in the current directory against HEAD
dsreview --brief brief.md

# A committed range, brief on stdin
dsreview --repo /path/to/repo --base main --head HEAD --brief - < brief.md

# A lane's patch applied to its base in a scratch tree (the form the lane template uses)
dsreview --repo /path/to/worktree --base <base sha> --patch candidate.patch --brief brief.md --out review.json
```

| Option | Meaning |
|---|---|
| `--repo` | The repository. Default: the current directory. |
| `--base` | The revision the change applies to. Default `HEAD`. |
| `--head` | Review the committed range `base..head`. Needs `--base`. |
| `--patch` | Review a patch file applied to `--base` in a scratch tree. Needs `--base`. |
| `--brief` | A file holding the lane brief, or `-` for stdin. |
| `--effort` | `low`, `high` (default) or `max`. |
| `--max-turns` | Model turns before the final answer is forced. Default 50. |
| `--timeout` | Seconds per model call. Default 900. |
| `--out` | Also write the JSON to this file. |
| `--verbose` | Report each tool turn on stderr. |

The repository itself is never modified: worktree mode copies files into a scratch directory, and
range and patch modes extract `git archive` output there.

## Exit status and output

| Exit | Meaning |
|---|---|
| 0 | PASS |
| 1 | FAIL: the model returned FAIL. Its instructions say to fail on any critical or major finding with high or medium confidence; the exit status follows the model's verdict as given. |
| 2 | No verdict: a configuration error, a refused origin, a gateway failure, or a reply that never parsed |

Treat 2 as "not reviewed", never as a pass. The JSON on stdout carries `verdict`, `summary`,
`findings` (each with `severity`, `confidence`, `file`, `line`, `quote`, `title`, `scenario` and
`quote_ok`, which is false when the quoted line appears in neither the diff nor the cited file),
the model and its fingerprint, turns, tool calls, elapsed seconds, token usage, an estimated cost at
peak rates, and `error`.

## Cloudflare AI Gateway setup

1. In the Cloudflare dashboard, go to AI > AI Gateway and create a gateway. Its name is
   `DSREVIEW_CF_GATEWAY`.
2. Turn on authentication for the gateway and create a token for it. That token is
   `DSREVIEW_TOKEN`. `dsreview` sends it as `cf-aig-authorization` and sends no provider key of
   its own.
3. Store your DeepSeek API key in the gateway's provider keys (BYOK), so the gateway adds it to each
   request.
4. If you use the gateway's DLP (data loss prevention) checks, apply them to the request only.
   `dsreview` streams every call, and a DLP check on the response makes the gateway buffer the
   whole reply before it returns anything. Behind a proxied custom domain that buffering meets
   Cloudflare's 100 second proxy timeout, and long reviews are cut off and come back as exit 2.
5. Caching is skipped per request (`cf-aig-skip-cache: true`), and each request carries
   `cf-aig-metadata: {"app": "dsreview"}` so you can filter the gateway logs.

See Cloudflare's DeepSeek provider page for the endpoint form:
https://developers.cloudflare.com/ai-gateway/usage/providers/deepseek/
