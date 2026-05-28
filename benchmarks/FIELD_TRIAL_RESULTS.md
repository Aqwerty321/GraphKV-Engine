# OpenCode Binary Star Field Trial Results

Run date: 2026-05-28

Command:

```sh
python3 benchmarks/opencode_repo_trials.py \
  /home/Aaditya/CRE-Knowledge-Engine \
  /home/Aaditya/agent-browser \
  --timeout-seconds 420 \
  --output benchmarks/results/field-trials/latest.json
```

Bounded source tool rerun:

```sh
python3 benchmarks/opencode_repo_trials.py \
  /home/Aaditya/CRE-Knowledge-Engine \
  /home/Aaditya/agent-browser \
  --timeout-seconds 420 \
  --output benchmarks/results/field-trials/latest_bounded_source.json
```

The run used the isolated Binary Star OpenCode profile with `OPENCODE_DISABLE_PROJECT_CONFIG=1`, so repo-local OpenCode configs could not add extra MCP servers. Both repositories reported exactly one visible MCP server, `graphkv-binary-star`, and native debug probes for `read`, `bash`, `grep`, `glob`, `webfetch`, `websearch`, `codesearch`, and `apply_patch` were disabled.

## Strict MCP baseline

| Repository | Raw text tokens | MCP context tokens | Reduction | Savings | Survey time | Work time | Work result |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| CRE-Knowledge-Engine | 411,337 | 6,940 | 59.27x | 98.31% | 20.074s | 67.123s | edited and executed probe |
| agent-browser | 878,136 | 6,068 | 144.72x | 99.31% | 14.725s | 23.932s | edited and executed probe |

## Bounded Source Tool Rerun

The rerun added MCP-native `search_source` and `read_source_slice` so the cloud model could find exact edit coordinates without using sandbox one-liners as a line-number oracle.

| Repository | Raw text tokens | MCP context tokens | Reduction | Savings | Survey time | Work time | Work tools |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| CRE-Knowledge-Engine | 411,366 | 6,469 | 63.59x | 98.43% | 21.998s | 31.482s | `query_context`, `search_source`, `read_source_slice`, `edit_source_code`, `execute_sandbox` |
| agent-browser | 878,162 | 6,024 | 145.78x | 99.31% | 12.940s | 23.390s | `query_context`, `search_source`, `read_source_slice`, `edit_source_code`, `execute_sandbox` |

Measured deltas versus the strict MCP baseline:

| Repository | Work time delta | Work LLM token delta | Work context delta | Reduction delta |
| --- | ---: | ---: | ---: | ---: |
| CRE-Knowledge-Engine | 67.123s -> 31.482s, 53.10% faster | 111,632 -> 114,068, 2.18% higher | 3,522 -> 3,356 | 59.27x -> 63.59x |
| agent-browser | 23.932s -> 23.390s, 2.26% faster | 54,501 -> 44,454, 18.43% lower | 3,038 -> 2,765 | 144.72x -> 145.78x |

Document ingestion was fast relative to agent runtime: CRE-Knowledge-Engine built a 305-source bundle in 0.206s and ingested it in 0.158s; agent-browser built a 268-source bundle in 0.162s and ingested it in 0.232s.

Observed bottlenecks and fixes:

- Repo-local OpenCode config could add extra MCP servers. Fixed in the runner by setting `OPENCODE_DISABLE_PROJECT_CONFIG=1` for `opencode mcp list`, native debug probes, and `opencode run`.
- Directory lens output still included common cache/generated directories. Fixed in the MCP server by blocking cache, venv, frontend build, coverage, and Python bytecode directories.
- The field-trial runner streamed full OpenCode logs while also capturing them, which produced very large terminal logs. Fixed by making streaming opt-in with `--stream-logs`.
- The model used sandbox commands to discover line numbers before edits. Fixed by adding bounded `search_source` and `read_source_slice` MCP tools, then updating the profile and harness to require them before `edit_source_code`.

Remaining bottlenecks:

- The CRE work turn became much faster after bounded source tools, but logged LLM tokens were still slightly higher than baseline because the model still made an extra `read_directory_lens` call and accumulated a large cache-read total.
- Context payloads are compact, but still not strongly ranked by symbol graph for arbitrary repos unless the repo has fresh SCIP/doc evidence ingested. The current results sell token savings and successful work execution; the next lift is better target-node ranking to reduce exploratory tool calls.
