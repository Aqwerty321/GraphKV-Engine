#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$script_dir"

exec /home/Aaditya/.local/bin/uv run \
  --with-requirements "$script_dir/search_requirements.txt" \
  python "$script_dir/search_server.py" \
  "$@"