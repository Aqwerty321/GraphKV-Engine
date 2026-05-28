# Binary Star OpenCode Mandate

You are operating through the GraphKV Binary Star architecture: a cloud reasoning model connected to the local GraphKV C++/Rust engine through the `graphkv-binary-star` MCP server.

Before broad repository reasoning, call `query_context` with the user's intent and any known symbol/path as `target_node`. Treat GraphKV synthesis as the primary local context filter, not as a decorative search result.

Use `read_directory_lens` for read-only structure checks before editing unfamiliar paths. Use `execute_sandbox` for risky shell experiments so logs can be captured and ingested back into GraphKV. Use `inspect_git_state` before summarizing edits.

For long external documentation, call `ingest_web_documentation` with a concise `context_tag` before relying on the document for implementation choices. For source edits, prefer `edit_source_code` when the requested change is an exact line-range replacement.

The local GraphKV MCP is multi-tenant ready. Do not assume a single static local user; preserve request identity and OAuth workflow-token context when the client provides it.

This profile is intentionally isolated. The only MCP available in Binary Star mode is `graphkv-binary-star`.
