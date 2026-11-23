set shell := ["bash", "-euo", "pipefail", "-c"]

# Python 3.15, found the way the hooks find it.
py := "sh plugins/agent-workflows/scripts/py3"

# List supported tasks.
default:
    @just --list

# Check the tools the plugin and its tests need.
setup:
    @command -v git >/dev/null
    @{{ py }} -c ''

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
    {{ py }} -m py_compile plugins/agent-workflows/scripts/*.py plugins/agent-workflows/tools/*/*.py
    {{ py }} -c 'import ast, sys; [ast.parse(open(p).read(), p) for p in sys.argv[1:]]' plugins/agent-workflows/bin/run-check plugins/agent-workflows/bin/run-drive plugins/agent-workflows/bin/wait-until
    git diff --check

# Drive the hooks, the loop skill, the bin/ programs, dsreview and the tracker (with a stand-in gh)
# against temporary repositories.
[group('check')]
test:
    {{ py }} -m unittest discover -s tests

# The pre-commit gate.
[group('check')]
check: fmt-check lint test
