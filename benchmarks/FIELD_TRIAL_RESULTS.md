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

The run used the isolated Binary Star OpenCode profile with `OPENCODE_DISABLE_PROJECT_CONFIG=1`, so repo-local OpenCode configs could not add extra MCP servers. Both repositories reported exactly one visible MCP server, `graphkv-binary-star`, and native debug probes for `read`, `bash`, `grep`, `glob`, `webfetch`, `websearch`, `codesearch`, and `apply_patch` were disabled.

| Repository | Raw text tokens | MCP context tokens | Reduction | Savings | Survey time | Work time | Work result |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| CRE-Knowledge-Engine | 411,337 | 6,940 | 59.27x | 98.31% | 20.074s | 67.123s | edited and executed probe |
| agent-browser | 878,136 | 6,068 | 144.72x | 99.31% | 14.725s | 23.932s | edited and executed probe |

Document ingestion was fast relative to agent runtime: CRE-Knowledge-Engine built a 305-source bundle in 0.206s and ingested it in 0.158s; agent-browser built a 268-source bundle in 0.162s and ingested it in 0.232s.

Observed bottlenecks and fixes:

- Repo-local OpenCode config could add extra MCP servers. Fixed in the runner by setting `OPENCODE_DISABLE_PROJECT_CONFIG=1` for `opencode mcp list`, native debug probes, and `opencode run`.
- Directory lens output still included common cache/generated directories. Fixed in the MCP server by blocking cache, venv, frontend build, coverage, and Python bytecode directories.
- The field-trial runner streamed full OpenCode logs while also capturing them, which produced very large terminal logs. Fixed by making streaming opt-in with `--stream-logs`.

Remaining bottlenecks:

- The CRE work turn was slower and heavier than agent-browser: 67.123s and 111,632 logged LLM tokens versus 23.932s and 54,501. The model used extra directory lens and sandbox inspection calls before editing.
- Context payloads are compact, but still not strongly ranked by symbol graph for arbitrary repos unless the repo has fresh SCIP/doc evidence ingested. The current results sell token savings and successful work execution; the next lift is better target-node ranking to reduce exploratory tool calls.
