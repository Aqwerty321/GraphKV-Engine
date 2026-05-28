#!/usr/bin/env bash

set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "${script_dir}/.." && pwd)
output_dir="${repo_root}/benchmarks/data/local_repos"
scip_python_pkg='@sourcegraph/scip-python@0.6.6'
node_options=${GRAPHKV_SCIP_NODE_OPTIONS:---max-old-space-size=8192}

mkdir -p "${output_dir}"
printf 'Using NODE_OPTIONS=%s\n' "${node_options}"

repos=("$@")
if [[ ${#repos[@]} -eq 0 ]]; then
  repos=(
    /home/Aaditya/vllm
    /home/Aaditya/browser-use
    /home/Aaditya/GLM-OCR
  )
fi

for repo in "${repos[@]}"; do
  if [[ ! -d "${repo}" ]]; then
    printf 'Skipping missing repo: %s\n' "${repo}" >&2
    continue
  fi
  name=$(basename "${repo}")
  output="${output_dir}/${name}.scip"
  temp_output="${output}.tmp"
  rm -f "${temp_output}"
  printf 'Generating local real SCIP corpus: %s -> %s\n' "${repo}" "${output}"
  NODE_OPTIONS="${node_options}" npx -y "${scip_python_pkg}" index \
    --cwd "${repo}" \
    --project-name "${name}" \
    --project-version 0.0.0 \
    --project-namespace local-home \
    --quiet \
    --output "${temp_output}"
  mv "${temp_output}" "${output}"
done

printf 'Finished generating local repo SCIP corpora in %s\n' "${output_dir}"