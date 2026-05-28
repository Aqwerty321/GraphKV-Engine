#!/usr/bin/env bash

set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "${script_dir}/.." && pwd)
output_dir="${repo_root}/benchmarks/data"
scip_python_pkg='@sourcegraph/scip-python@0.6.6'

mkdir -p "${output_dir}"

common_args=(
  npx
  -y
  "${scip_python_pkg}"
  index
  --cwd
  "${repo_root}"
  --project-name
  graphkv-bench
  --project-version
  0.0.0
  --project-namespace
  local
  --quiet
)

printf 'Generating real SCIP corpus: %s\n' 'benchmarks/data/python_send_scip_ws.scip'
"${common_args[@]}" \
  --target-only tools/send_scip_ws.py \
  --output benchmarks/data/python_send_scip_ws.scip

printf 'Generating real SCIP corpus: %s\n' 'benchmarks/data/python_repo.scip'
"${common_args[@]}" \
  --output benchmarks/data/python_repo.scip

printf 'Finished generating real SCIP corpora in %s\n' "${output_dir}"