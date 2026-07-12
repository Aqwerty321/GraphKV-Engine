# Execution Memory Architecture

GraphKV treats sandbox execution as an evidence source, not just as a safety wrapper. A validation run should produce retrievable facts that can steer the next edit: implicated files, symbols, stack frames, compiler diagnostics, test failures, policy tier, snapshot identity, patch identity, duration, and a stable failure signature.

## Hot Path

1. `edit_source_code` returns proof metadata for the edited range.
2. `execute_sandbox` runs the validation command in an ephemeral workspace snapshot.
3. The result includes `execution_id`, `snapshot_id`, `execution_policy`, `evidence`, and `failure_memory`.
4. Failure memory records compact signatures locally under `.graphkv/failure_memory.jsonl`.
5. The full result is published to NATS on `graphkv.sandbox.logs.ingest.requests` without blocking the MCP response.

The MCP response stays small enough for agent use while preserving enough structured evidence for ranking and review.

In the failure-recovery path, the ordering can invert on purpose:

1. `execute_sandbox` fails first and emits the bug report.
2. `rank_intent_targets` or `query_context` translates that failure into an edit target.
3. `edit_source_code` applies a proof-carrying patch.
4. `execute_sandbox` validates again against the patched snapshot.

That is the loop the current benchmark suite exercises.

## Snapshot And Tier Policy

The default sandbox mode copies the workspace into a temporary snapshot and skips local runtime/build state such as `.git`, `.graphkv`, build directories, caches, and dependency folders. This makes validation reproducible without giving the command direct write access to the live checkout.

Execution tiers are intentionally provider-neutral:

- `tier_1_validation`: local tests, compilers, linters, and scripts; network disabled.
- `tier_2_dependency_install`: dependency installation or fetch commands; network enabled only for this explicit class.
- `tier_3_network`: network-facing commands such as `curl`, `wget`, `git clone`, or remote fetches.
- `tier_4_human_approval`: commands that should be surfaced as high risk before production use.

The current backend is Docker because it is local and already available. The evidence contract is separate from the backend so future providers can be added without changing agent behavior.

If `query_context` already returns exact proof-carrying coordinates for the target symbol, the shortest valid path is `query_context -> edit_source_code -> execute_sandbox`. A mandatory extra source lookup is not required in that case. The benchmark runner treats this as a valid proof-coordinate path.

## Warden Ingestion

The Rust Warden router subscribes to sandbox logs and converts them into `execution_log` chunks. Those chunks flow through the same reasoning-prefix and embedding path as documentation, making execution failures searchable as compact retrieval material instead of raw log archives.

The minimum useful log document contains:

- command, tier, exit code, timeout, duration, patch ID, and snapshot ID
- failure signature and error excerpt
- implicated files and symbols
- stderr/stdout backing text, truncated at the MCP boundary

## What The Benchmark Validated

The current complex-suite benchmark runs five repositories and three scenarios per repository: survey, proof edit, and failure recovery. The final measured run in `benchmarks/results/field-trials/latest_complex_suite.json` showed:

- 5/5 repositories passed both behavioral and protocol gates.
- 20 sandbox executions were requested across 15 OpenCode turns.
- the agent used sandbox execution as both a validator and a primary failure-evidence source.
- raw repo text was reduced from 9,863,899 estimated tokens to 17,678 MCP context tokens, a weighted 557.98x reduction.

The benchmark also forced a useful design clarification: when `query_context` already returns `evidence_id` and `range_sha256`, a direct proof edit is acceptable without a mandatory `search_source` hop.

## Known Gaps

- OpenCode event logs do not always echo every later sandbox result object, even when the agent reports the evidence fields in its final answer.
- Repeated post-patch sandbox validations still happen in some repair flows; this is realistic, but the scoring model should eventually distinguish necessary revalidation from wasteful retries.
- The portable benchmark probes are Python so they can run across C, Rust, and Python repos. They validate repo navigation, edit targeting, and execution evidence, but they are not yet native-language semantic tasks for every repo family.

## Why `awesome-ai-sandboxes` Helps

The cloned `tizkovatereza/awesome-ai-sandboxes` repo is not a runtime dependency. Its value is as a provider and evaluation taxonomy. It highlights the dimensions GraphKV should keep abstract:

- isolation backend: Docker/runc, gVisor, Firecracker, QEMU, full VM, browser sandbox
- state model: ephemeral, persistent, snapshot, fork, rollback, hibernate/resume
- startup and resume time: cold-start and time-to-interactive measurements
- filesystem and network controls: egress policy, mounted workspace, volumes, credentials brokers
- agent ergonomics: file APIs, git APIs, LSP, execute APIs, desktop/browser sessions
- evaluation shape: success rate, interruption rate, cost, startup latency, and filesystem I/O

That makes it genuinely useful as a decision matrix for future sandbox backends. The near-term GraphKV architecture should stay provider-neutral: keep Docker snapshot execution local today, but require every future provider to emit the same execution evidence contract.