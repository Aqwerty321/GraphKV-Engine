# Shipped Architecture Summary

## Scope

Phase 1 shipped a headless C++26 WebSocket ingestion server that accepts binary SCIP indexes at `/v1/ingest_scip`, parses the payload through the strict wire-scanner path, maps the decoded data into an immutable GraphKV arena snapshot, and publishes that snapshot for read-side consumers. The current implementation adds the GraphKV-Engine network ingestion boundary around that scanner, treats binary WebSocket frames as Zstd-compressed SCIP for the code plane, and accepts Zstd-compressed normalized document bundles at `/v1/ingest_docs` for the document plane. The macro-architecture is now a High-Speed Context Synthesizer: RAM holds the 2M-token-equivalent code graph and resident doc metadata, the local RTX path pages only relevant subgraphs for dense synthesis, and the cloud model receives OpenCode-shaped JSON rather than raw repository bulk.

The production shape is intentionally biased toward deterministic parsing, durable string ownership, and a validated container path over speculative abstraction purity.

## Shipped Path

- Transport: Drogon WebSocket endpoints at `/v1/ingest_scip`, `/v1/ingest_docs`, and `/v1/ingest`.
- Boundary: transport-free `IngressGate` for frame classification, Glaze JSON control parsing, and size-gated compressed binary SCIP admission. The worker calls `ZSTD_getFrameContentSize` before allocation and rejects unknown decompressed size or anything above the strict 250 MiB VRAM payload limit.
- Dispatch: bounded worker handoff for SCIP ingestion; Drogon callbacks no longer spawn detached per-message threads.
- Telemetry: SPSC `TelemetryRingBuffer` for fixed-size ingest metrics.
- OpenCode bridge: `OpenCodeProtocol` serializes synthesized GraphKV context into the current OpenCode `POST /session/{id}/message` request body with `parts: [{ type: "text", synthetic: true, text: ... }]` and an optional `format: { type: "json_schema", schema: ... }` structured-output contract. The current synthesis pass reads the latest immutable snapshots, emits graph/doc counts, ranks graph documents by query overlap with graph-density tie-breakers, and ranks doc chunks through dense+sparse RRF.
- Document retrieval: each published `DocIndexSnapshot` gets a `DocRetrievalIndex`. Dense ANN delegates to Unum USearch when `GRAPHKV_ENABLE_USEARCH=ON`; sparse BM25-style token postings remain native C++ metadata; native Reciprocal Rank Fusion blends the two and promotes chunks that overlap the SCIP graph through doc-code links or paths.
- Ingest: strict protobuf wire scanner is the production-default path after Zstd decompression into an exact-size buffer guarded by GSL preconditions.
- Compatibility: generated protobuf remains available as a non-default compatibility mode.
- Ownership: GraphKV snapshots own all durable string bytes through `StringTape` and refer to them with compact `StringRef` handles.
- Publication: a single immutable root is published atomically, with Folly hazard pointers used when Folly is present and a bounded-retire fallback reserved for local-only development.
- Deployment: the NVIDIA TensorRT-LLM Docker path has been built and smoke-tested end to end.

## Plan vs. Shipped

| Original Decision | Implementation Status | Technical Justification |
| --- | --- | --- |
| Use an immutable GraphKV snapshot as the published read model | Kept | The shipped path still publishes a single immutable arena snapshot and serves readers from that root. The implementation in `GraphKV.hpp` made that concrete with atomic publication and protected reads. |
| Keep generated protobuf support available | Kept | The protobuf path still exists as a compatibility mode in `ScipIngestor.cpp` and is wired in `CMakeLists.txt`. We did not remove the escape hatch. |
| Make strict wire parsing the serious long-term ingest path | Improved | This became the production-default path rather than a future optimization. That gives us direct control over parse cost, memory ownership, and schema handling in `ScipIngestor.cpp`. |
| Avoid protobuf-owned string lifetime leakage into the published graph | Improved | The shipped design uses `StringTape` plus `StringRef` in `GraphKV.hpp`, so snapshot data is self-owned and durable after ingest. That is cleaner than the earlier protobuf-adjacent sketches. |
| Use Folly for production-grade reader safety and reclamation | Improved | We kept real Folly in the production path, and `GraphKV.hpp` now uses Folly hazard pointers when available. The policy is fail-closed through `GRAPHKV_REQUIRE_FOLLY` in `CMakeLists.txt`, which is stricter than the original concept. |
| Keep the network event loop thin | Improved | The new `IngestController` runs admission and cheap control parsing only, then hands binary SCIP work to a bounded dispatcher instead of detached threads. |
| Add a fuzzable protocol boundary | Improved | `IngressGate` and `ProtocolModels` are independent of Drogon and covered by focused tests plus the `ingest_boundary_fuzzer` target. |
| Speak OpenCode natively at the boundary | Improved | The `synthesize` control route now validates OpenCode target fields and emits a schema-versioned OpenCode session message request that can be handed to the OpenCode harness without reformatting. |
| Build Folly through a cleaner repo-managed dependency flow | Changed | The superbuild story did not hold up well against the NVIDIA base image and current Folly dependency behavior. The shipped solution installs pinned FastFloat and Folly directly in Docker and then consumes installed Folly from `CMakeLists.txt`. Less elegant, more reliable. |
| Stay close to pure standard C++26 facilities | Changed | Toolchain reality forced a compatibility layer. `ExpectedCompat.hpp` exists because the current Clang 18 container path could not reliably provide uniform `std::expected`, and `GraphKV.hpp` has fallback mdspan selection logic for the same reason. |
| Use a richer RCU-style reclamation story if needed | Changed | The shipped implementation simplified this to atomic-root publication plus Folly hazptr-protected reads in `GraphKV.hpp`. For a single published root, that is a pragmatic simplification with no obvious loss today. |
| Prove the NVIDIA production path later in the project | Improved | This stopped being a plan item and became a validated path. The Dockerfile now builds successfully, and the WebSocket ingest flow has been exercised end to end. |
| Pin external inputs for reproducibility | Improved | Dependency pinning improved materially for Folly and FastFloat, and the SCIP schema fetch is now pinned to the immutable commit `99236e35450ccd8b87fe58c38d31fd499d0ffdfa` in `CMakeLists.txt`. |

## Immediate Tech Debt

1. Remove the standard-library compatibility scaffolding once the compiler and standard library are actually ready.
   `ExpectedCompat.hpp` and the mdspan fallback logic are correct for the current environment, but they are pure environment debt and should be deleted when the toolchain catches up.
2. Revisit the Folly and low-level I/O integration on a newer NVIDIA base image.
   The current direct-build path is correct, but it is heavier than ideal and intentionally sidesteps newer `io_uring`-related codepaths by omitting `liburing-dev` in the builder.
3. Tighten the performance harness beyond the current focused ingest-latency baseline.
   The new benchmark isolates strict-wire ingest latency, but once the environment is more stable we should add larger representative SCIP fixtures, allocator sensitivity checks, and concurrency-aware read/publication measurements.
4. Replace deterministic dense chunk projections with real sidecar embeddings.
   GraphKV now has the USearch-backed dense index boundary and native fusion path. The next retrieval step is feeding it model vectors from the parser/embedding sidecar instead of local lexical projections.
5. Attach the real GPU-backed subgraph synthesizer behind `OpenCodeProtocol`.
   The network boundary now preserves and serializes the OpenCode request shape, and the CPU fallback already emits snapshot counts plus ranked document evidence. The next subsystem should replace the bounded CPU ranking with JIT graph-query output, node/edge counts, token estimates, and dense source citations from the resident arena.

## Context Synthesizer Contract

GraphKV-Engine is not a standalone local LLM server. It is a hardware filter for cloud reasoning:

- RAM is the durable context window: SCIP, AST, dependency, symbol, and relationship data remain resident in the GraphKV arena.
- The local GPU path should never try to load the full graph as prompt tokens. It should page a small relevant subgraph into Unified Memory and synthesize a high-signal context packet.
- The Drogon boundary must stay below local latency budgets by admitting, validating, and serializing. Zstd decompression, strict-wire scanning, heavy graph selection, GPU execution, and cloud network I/O must remain outside the event loop.
- OpenCode is the cloud harness contract. Outbound prompt bodies use the server API shape `POST /session/{id}/message` with text/file/agent/subtask parts as defined by the current OpenCode OpenAPI types.
- Cloud models should receive dense topological evidence and a structured-output schema, not raw file dumps.

## Smoke Test

```sh
docker build -t graphkv-engine .
docker run --rm -it --gpus all -p 8888:8888 graphkv-engine
zstd -q -c benchmarks/data/index.scip | websocat -b ws://localhost:8888/v1/ingest_scip
```

If `websocat` is not installed locally, use the existing Python smoke sender instead:

```sh
python3 tools/send_scip_ws.py benchmarks/data/index.scip --port 8888
```

## Benchmark Baseline

The repository now includes `ingest_latency_benchmark`, which loads `benchmarks/data/index.scip` into memory, runs the strict wire-scanner ingest path 100 times, and reports mean, median, and P99 latency in microseconds.
The Docker profile harness can also run persistent WebSocket sequential and concurrent binary ingest against explicit real SCIP fixtures, including local home-repo corpora generated by `benchmarks/generate_local_repo_corpora.sh`.

The current measured baseline on this workstation is recorded in `benchmarks/BASELINE.md`, including checked-in real SCIP corpora emitted by the pinned official `scip-python` indexer and a five-run variance summary for each real fixture.

```sh
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DGRAPHKV_BUILD_SERVER=OFF -DGRAPHKV_REQUIRE_STD_PRINT=OFF -DGRAPHKV_ENABLE_PROTOBUF_COMPAT=OFF
cmake --build build --target ingest_latency_benchmark
./build/ingest_latency_benchmark
```
