set shell := ["bash", "-euo", "pipefail", "-c"]

# List supported tasks.
default:
    @just --list

# Check the tools the plugin and its tests need.
setup:
    @command -v python3 git >/dev/null

# Format the task interface.
[group('check')]
fmt:
    just --fmt

# Check task interface formatting.
[group('check')]
fmt-check:
    just --fmt --check

# Compile the hook scripts, the tools and the bin/ programs, and check whitespace.
[group('check')]
lint:
    python3 -m py_compile plugins/agent-workflows/scripts/*.py plugins/agent-workflows/tools/*/*.py plugins/agent-workflows/bin/*.py
    python3 -c 'import ast, sys; [ast.parse(open(p).read(), p) for p in sys.argv[1:]]' plugins/agent-workflows/bin/loop-lint plugins/agent-workflows/bin/loop-land plugins/agent-workflows/bin/loop-teardown plugins/agent-workflows/bin/run-check plugins/agent-workflows/bin/wait-until
    git diff --check

# Drive the hooks, the loop skill, the bin/ programs (loop-land included, with a stand-in gh), dsreview and
# the tracker (with a stand-in gh) against temporary repositories.
[group('check')]
test:
    python3 -m unittest discover -s tests

# The pre-commit gate.
[group('check')]
check: fmt-check lint test
