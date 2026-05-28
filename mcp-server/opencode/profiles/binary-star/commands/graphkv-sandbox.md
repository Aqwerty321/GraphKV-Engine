---
description: Run a risky command in an ephemeral Docker sandbox and ingest the logs into GraphKV
---
# GraphKV Sandbox

Use the `graphkv-binary-star` MCP tool `execute_sandbox` for the proposed command. Keep `timeout_seconds` tight, inspect the returned stdout/stderr summaries, and then call `query_context` if the logs should inform follow-up work.
