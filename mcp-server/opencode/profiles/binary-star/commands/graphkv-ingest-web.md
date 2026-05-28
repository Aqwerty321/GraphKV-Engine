---
description: Ingest external documentation through Warden and GraphKV before using it as evidence
---
# GraphKV Ingest Web

Ask for or infer the documentation URL, then call the `graphkv-binary-star` MCP tool `ingest_web_documentation` with:

- `url`: the documentation page or article URL
- `context_tag`: a short tag describing the task or subsystem

After ingestion is queued, call `query_context` with the implementation question and the same tag.
