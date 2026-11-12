#!/usr/bin/env bash
set -euo pipefail
base="$(cd -- "$(dirname -- "$0")" && pwd)"
exec "$base/.venv/bin/python" "$base/manage.py" serve "$@"
