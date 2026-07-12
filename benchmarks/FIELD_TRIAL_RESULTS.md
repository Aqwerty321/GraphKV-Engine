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

## Zero-Hesitation Enforcement Rerun

The zero-hesitation pass tightened Binary Star so source coordinates are treated as final edit evidence instead of as a prompt to re-read the world. `query_context` returns compact graph context plus an evidence contract, and source-coordinate tools keep snippets bounded so the model does not need raw file dumps.

Strict e2e command:

```sh
python3 tests/e2e_agent_interrogation.py \
  --allow-underfilled-context \
  --timeout-seconds 420
```

Result against the live isolated profile: `rc=0`, with `context_payload_tokens=2,561`. Parsed write pipeline:

```text
query_context -> search_source -> edit_source_code -> execute_sandbox
```

The e2e and field-trial harnesses now raise `HESITATION VIOLATION` if `read_directory_lens` appears after `search_source` or `read_source_slice`, or if any tool call intervenes between source-coordinate evidence and `edit_source_code`. The successful e2e write turn used no native tools, no `read_source_slice`, and no exploratory directory read after coordinates.

## Vague Query Router

The next failure mode is broad human intent such as "fix auth" or "why did the pipeline crash". Binary Star now has a pre-edit `rank_intent_targets` tool that collapses vague intent into ranked source coordinates before the zero-hesitation edit protocol begins.

Implemented hybrid signals:

- Lexical fingerprinting: local TF-IDF over paths, symbols, comments, literals, and source text.
- Execution-trace grafting: stack-trace-like `file:line` evidence becomes an immediate high-confidence target.
- Git heatmap: recently and frequently touched files receive a bounded score boost.
- Failure memory: repeated sandbox failure signatures can boost matching files in later runs.
- Semantic rebound: matching tests are favored as human-readable intent anchors.
- Entropy sieve: higher-complexity files receive a small bug-probability bias.
- Centrality proxy: symbols referenced across multiple files receive a bounded structural boost.

Operational contract: vague prompts may call `rank_intent_targets` once, then must translate the top target into `query_context`, `search_source`, and immediate `edit_source_code`. The router is an intent compiler and beam ranker before the zero-hesitation edit path, not a new browsing loop.

Follow-up hardening added three practical details:

- `search_source` and `rank_intent_targets` now return a compact `edit_range` for the nearest symbol block, so the model can patch the target without asking for a second slice.
- Stack-trace-like prompts such as `path/to/file.py:123` take an execution-trace fast path and skip repo-wide vague-intent scanning.
- The harness now also flags repeated `rank_intent_targets` calls or `rank_intent_targets -> read_directory_lens` as `HESITATION VIOLATION`.

Structural upgrade after the router pass:

- `rank_intent_targets` now emits `structured_task`, `edit_beam`, `causal_chain`, `adversarial_review`, `failure_memory_hits`, and an observability `ranking_trace_id`, turning vague intent into a replayable internal task instead of a flat search result.
- `query_context` now attaches a compact `local_evidence` packet when graph evidence is thin, so graph misses still produce bounded local candidates and checker findings.
- `search_source`, `read_source_slice`, and ranked targets now carry deterministic `evidence_id` and `range_sha256` values.
- `edit_source_code` accepts `expected_range_sha256` and `evidence_id`, rejects stale spans when the hash mismatches, and returns a proof record with before/after file and range hashes.
- Failed `execute_sandbox` validations are recorded in ignored local state under `.graphkv/failure_memory.jsonl` and are reused by future ranking.

Strict proof-carrying e2e after hash canonicalization passed with `rc=0`, `context_payload_tokens=524`, write tools `query_context -> search_source -> edit_source_code -> execute_sandbox`, and proof markers present for `evidence_id`, `range_sha256`, `before_range_sha256`, and `edit_proof_id`.

Final strict e2e after edit-range hardening passed with `rc=0`, no assertion/traceback/hesitation markers, and a corrected `query_context`-only `context_payload_tokens=425`. The write turn still stayed `query_context -> search_source -> edit_source_code -> execute_sandbox`.

Router smoke checks:

- Config/profile query ranked `benchmarks/opencode_repo_trials.py`, `tests/e2e_agent_interrogation.py`, `mcp-server/opencode/profiles/binary-star/AGENTS.md`, and `mcp-server/opencode/profiles/binary-star/opencode.json` in the top five.
- Stack-trace-like query `mcp-server/server.py:680` returned `mcp-server/server.py` line 680 as a `trace` confidence target.
- Field-trial artifact JSON under `benchmarks/results/field-trials` is pruned from router scans.
- Strict e2e after adding the router passed with `rc=0`, no native tools, no `HESITATION VIOLATION`, and the write turn stayed `query_context -> search_source -> edit_source_code -> execute_sandbox` with `context_payload_tokens=2,947`.

Document ingestion was fast relative to agent runtime: CRE-Knowledge-Engine built a 305-source bundle in 0.206s and ingested it in 0.158s; agent-browser built a 268-source bundle in 0.162s and ingested it in 0.232s.

Observed bottlenecks and fixes:

- Repo-local OpenCode config could add extra MCP servers. Fixed in the runner by setting `OPENCODE_DISABLE_PROJECT_CONFIG=1` for `opencode mcp list`, native debug probes, and `opencode run`.
- Directory lens output still included common cache/generated directories. Fixed in the MCP server by blocking cache, venv, frontend build, coverage, and Python bytecode directories.
- The field-trial runner streamed full OpenCode logs while also capturing them, which produced very large terminal logs. Fixed by making streaming opt-in with `--stream-logs`.
- The model used sandbox commands to discover line numbers before edits. Fixed by adding bounded `search_source` and `read_source_slice` MCP tools, then updating the profile and harness to require them before `edit_source_code`.

Remaining bottlenecks:

- The CRE work turn became much faster after bounded source tools, but logged LLM tokens were still slightly higher than baseline because the model still made an extra `read_directory_lens` call and accumulated a large cache-read total.
- Context payloads are compact, but still not strongly ranked by symbol graph for arbitrary repos unless the repo has fresh SCIP/doc evidence ingested. The current results sell token savings and successful work execution; the next lift is better target-node ranking to reduce exploratory tool calls.

## Complex Suite With Sandboxed Execution Evidence

Run date: 2026-05-29

Command:

```sh
python3 benchmarks/opencode_repo_trials.py \
  --suite complex \
  --scenarios survey,proof_edit,failure_recovery \
  --timeout-seconds 420 \
  --output benchmarks/results/field-trials/latest_complex_suite.json
```

The expanded runner now clones a built-in complex suite under `benchmarks/data/local_repos/complex`, seeds two disposable Python probes into each repo, restores those probes after each run, and separates behavioral success from protocol discipline. It runs three scenarios per repo:

- `survey`: GraphKV context plus one directory lens, no native OpenCode tools.
- `proof_edit`: direct proof-carrying edit using `query_context -> search_source -> edit_source_code -> execute_sandbox`.
- `failure_recovery`: run the sandbox first to produce a traceback and failure signature, then use that execution evidence to rank, edit, and revalidate.

Aggregate result from the generated JSON report:

- 5 repositories, 15 OpenCode turns.
- 5/5 repositories passed the full protocol gate.
- 5/5 repositories passed the behavioral probe contracts after validation.
- 0 native tool violations; `read`, `bash`, `grep`, `glob`, `webfetch`, `websearch`, `codesearch`, and `apply_patch` stayed blocked.
- 20 sandbox executions were requested by the agent.
- Raw tracked text estimate: 9,863,899 tokens.
- MCP context payload total: 17,678 tokens.
- Weighted raw-to-MCP reduction: 557.98x.
- Total measured OpenCode turn time: 508.934s.

| Repository | Result | Raw tokens | MCP context tokens | Reduction | Savings | Survey | Proof edit | Failure recovery | Sandbox calls | Notes |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| fastapi | pass | 3,750,144 | 4,036 | 929.17x | 99.89% | 15.925s | 25.315s | 45.405s | 4 | Clean proof edit and execution-first repair. |
| httpx | pass | 193,506 | 3,615 | 53.53x | 98.13% | 15.289s | 30.855s | 61.395s | 6 | Passed, but used several validation sandboxes during failure recovery. |
| pydantic | pass | 2,098,841 | 3,414 | 614.77x | 99.84% | 20.735s | 26.010s | 60.833s | 3 | Passed; the agent used direct proof coordinates from `query_context`. |
| ripgrep | pass | 525,352 | 3,118 | 168.49x | 99.41% | 14.229s | 41.003s | 57.999s | 4 | Passed after allowing proof-carrying `query_context` coordinates without mandatory `search_source`. |
| redis | pass | 3,296,056 | 3,495 | 943.08x | 99.89% | 27.709s | 22.815s | 43.417s | 3 | Passed; C repo still used portable Python probes for cross-repo comparability. |

An interim strict run failed the protocol gate even though all probes validated, because the agent often used proof-carrying coordinates returned by `query_context` directly instead of making a mandatory `search_source` call. That was a benchmark design problem more than an agent failure: if `query_context` returns exact `evidence_id` and `range_sha256`, the shorter `query_context -> edit_source_code -> execute_sandbox` path is acceptable. The runner now records this as a valid proof-coordinate path while still rejecting directory browsing or source slices before the first edit.

Observed strengths:

- The sandboxed bash tool is now visibly part of the agent workflow. Proof edits used it as validation; failure recovery used it as the first evidence source.
- Failed sandbox runs emitted execution-derived retrieval signals with failure signatures and failure memory records.
- OpenCode stayed isolated to `graphkv-binary-star` across all five repositories.
- The benchmark now reports `behavior_ok` separately from `protocol_ok`, which is more honest than a single green badge.

Observed weaknesses:

- OpenCode event logs sometimes omit later sandbox result objects even though the agent reports the evidence fields in its final message. The runner records this as incomplete `sandbox_usage.calls[*]` detail and backs it with local probe verification.
- `failure_recovery` still allows repeated validation sandboxes after a patch. That is realistic agent behavior, but future scoring should separate necessary revalidation from wasteful retries.
- The benchmark probes are intentionally disposable Python files so the scenario is portable across C, Rust, and Python repos. This measures repo navigation, edit targeting, sandbox evidence, and isolation; it does not yet measure native-language semantic edits inside each target codebase.

Next benchmark hardening targets:

- Add a stricter failure-recovery assertion that requires `rank_intent_targets -> query_context -> edit_source_code` through a proof-carrying coordinate source before any post-failure slice or second edit.
- Capture full sandbox output records directly from MCP/server-side telemetry so the report does not depend on what OpenCode chooses to echo.
- Add one native-language scenario per repo family after the portable probe suite is stable: Python test fixture for FastAPI/HTTPX/Pydantic, Rust unit test for ripgrep, and C compile/test target for Redis.
