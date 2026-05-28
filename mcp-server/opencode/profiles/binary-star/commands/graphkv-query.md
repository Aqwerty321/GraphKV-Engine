---
description: Query the local GraphKV context synthesizer before broad repository reasoning
---
# GraphKV Query

Use the `graphkv-binary-star` MCP tool `query_context` with the user's current task as `query`. If a symbol, file, or package is known, pass it as `target_node`; otherwise leave `target_node` empty and use `depth: 1`.

Summarize the returned structural and semantic context before choosing edits or shell commands.
