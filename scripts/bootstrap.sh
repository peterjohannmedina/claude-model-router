#!/bin/sh
set -eu
routing_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
if [ -n "${CLAUDE_ROUTING_PYTHON:-}" ]; then routing_python=$CLAUDE_ROUTING_PYTHON
elif command -v python3 >/dev/null 2>&1; then routing_python=$(command -v python3)
elif command -v python >/dev/null 2>&1; then routing_python=$(command -v python)
else printf '%s\n' 'Python 3.11+ is required; no dependency was installed.' >&2; exit 2
fi
exec "$routing_python" -B "$routing_dir/routing_bootstrap.py" "$@"
