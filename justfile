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

# Compile the hook scripts and loop-lint, and check whitespace.
[group('check')]
lint:
    python3 -m py_compile plugins/agent-workflows/scripts/*.py
    python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' plugins/agent-workflows/bin/loop-lint
    git diff --check

# Drive the hooks, the launch template and loop-lint against temporary repositories.
[group('check')]
test:
    python3 -m unittest discover -s tests

# The pre-commit gate.
[group('check')]
check: fmt-check lint test
