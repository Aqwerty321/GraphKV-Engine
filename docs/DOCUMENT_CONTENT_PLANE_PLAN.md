# GraphKV-Engine Document Content Plane Plan

Research snapshot: 2026-05-28

## Decision

Long unstructured documents are a parallel content plane, not SCIP. SCIP remains
the code graph contract. Document parsers emit normalized document events that
GraphKV stores as compact metadata, source spans, chunk previews, and doc-code
links. Raw bodies stay outside the hot arena in zstd-compressed sidecar blobs or
rebuildable bundles.

## 2026 Parser Stack

- Docling is the default high-fidelity local parser for PDFs, Office documents,
  HTML, tables, layout, OCR/VLM, and lossless JSON/Markdown export.
- Unstructured is the mature ETL path when broad connectors, partitioning, and
  pipeline breadth matter more than maximal layout fidelity.
- MarkItDown is the lightweight converter for simple Markdown-like pipelines and
  quick Office/document conversion. It should run sandboxed for untrusted input.
- Tree-sitter and SCIP remain the structured code plane for source files,
  comments, examples, docstrings, and symbol-aware linking.

Heavy parsing, OCR, embedding, and reranking are sidecar work. Drogon only
admits, validates, dispatches, and serializes.

## Ingestion Boundary

Add `/v1/ingest_docs` beside `/v1/ingest_scip`.

The wire contract is a zstd-compressed normalized document bundle. The bundle is
NDJSON in the first implementation because it is easy to generate and inspect;
Protobuf or CBOR can replace it later if profiling says parse overhead matters.

Supported events:

- `doc_start`: path, media type, language, parser, title, content hash.
- `chunk`: path, heading, chunk type, text preview/body, line/page range, token estimate.
- `table`, `code_block`, `image_caption`: accepted as chunk-like events.
- `link`: path, target, link type, source span, confidence.
- `doc_end`: terminator for streaming-friendly bundles.

The same zstd preflight discipline applies: read `ZSTD_getFrameContentSize`,
reject unknown decompressed size, reject payloads above the configured cap, then
decompress and hand the buffer to bounded workers.

## Hot Data Model

`DocSourceRecord`
: path, media type, parser, language, title, content hash, chunk range, link range.

`DocChunkRecord`
: source index, heading, chunk type, bounded text preview, line range, token estimate, link range.

`DocCodeLinkEdge`
: source/chunk to code path, symbol, package, URL, config key, or error code with confidence and evidence span.

GraphKV keeps this metadata hot and small. Full raw text, extracted page images,
tables, and embeddings should remain in a sidecar store addressed by content hash
or bundle offset.

## Retrieval Plan

The retrieval stack now has three distinct responsibilities:

`Dense vectors`
: GraphKV does not implement a vector database. Dense ANN is delegated to Unum
  USearch (`GRAPHKV_ENABLE_USEARCH=ON`, pinned by `GRAPHKV_USEARCH_TAG`). The
  first local implementation builds deterministic chunk projections so the C++
  ingestion/retrieval path is testable before a sidecar embedding service emits
  real model vectors. If USearch is unavailable, GraphKV falls back to exact
  dense scoring only for local correctness tests.

`Sparse vectors`
: GraphKV owns a compact BM25-style inverted index. It hashes normalized query
  and chunk tokens into `SparseTermRecord` and `SparsePostingRecord` arrays that
  map token hashes directly to document chunk IDs and term frequencies. This is
  small, arena-friendly metadata, not a separate search server.

`Fusion`
: GraphKV fuses dense USearch hits and sparse BM25-style hits with native
  Reciprocal Rank Fusion. SCIP graph evidence and doc-code links are applied as
  graph overlap priors; when any fused hit intersects the code graph, synthesis
  keeps that graph-connected slice first.

The broader retrieval stack should still combine:

- exact symbol/path/package/error-code matching,
- native BM25-style sparse lexical retrieval,
- USearch-backed dense embeddings over contextualized chunks,
- graph priors from SCIP and doc-code links,
- native RRF as the safe default fusion method,
- weighted RRF once an eval set exists,
- DBSF only when retriever scores are trusted,
- reranking of top 100-200 candidates down to roughly top 20.

Contextual Retrieval is the default preprocessing pattern: generate a short
document-aware prefix for every chunk, prepend it before embedding and BM25
indexing, and preserve provenance for citations.

## First Implementation

1. Build a normalized NDJSON bundle from mixed-language repos and docs.
2. Compress the bundle with zstd and send it to `/v1/ingest_docs`.
3. Parse doc metadata/chunks/links into a resident `DocIndexSnapshot`.
4. Publish the latest doc snapshot beside the SCIP arena snapshot.
5. Include top-ranked doc chunks and link evidence in the OpenCode synthesis text.

6. Build a resident `DocRetrievalIndex` beside every published doc snapshot.
7. Use USearch for dense retrieval when available, native sparse postings for
  BM25-style retrieval, and native RRF for synthesis ranking.

This is intentionally still not a custom vector database. GraphKV owns the small
sparse and fusion structures that benefit from arena locality, while dense ANN
belongs to USearch and real embeddings belong to the sidecar pipeline.