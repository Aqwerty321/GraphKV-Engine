# GraphKV-Engine SCIP Ingestion Server

Headless C++26 WebSocket ingestion server for binary SCIP indexes and normalized long-document bundles. The current ingress boundary receives Zstd-compressed SCIP protobuf payloads at `/v1/ingest_scip`, Zstd-compressed document NDJSON at `/v1/ingest_docs`, also exposes `/v1/ingest` for the GraphKV-Engine route, converts admitted binary SCIP into an immutable GraphKV CSR snapshot, and publishes that snapshot for read-side consumers.

The production ingest path uses a custom protobuf wire scanner for the SCIP fields needed by GraphKV. Generated SCIP protobuf is still built for compatibility, tests, and schema validation, but durable GraphKV snapshots own their string bytes in a contiguous `StringTape`. The network callback now admits and classifies frames through a transport-free ingress gate, parses small JSON control frames with Glaze, and hands binary SCIP work to a bounded worker dispatcher instead of spawning detached per-message threads.

GraphKV-Engine is also the local context synthesizer for OpenCode. The resident arena is treated as the large context store, while the network boundary emits OpenCode `POST /session/{id}/message` JSON bodies containing dense synthesized context instead of raw repository sprawl. Document synthesis uses a resident retrieval index: dense ANN delegates to Unum USearch, sparse BM25-style postings stay native in GraphKV metadata, and C++ Reciprocal Rank Fusion intersects the results with SCIP/doc-code graph evidence.

## Build

```sh
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build --target scip_ingest_server
ctest --test-dir build --output-on-failure
```

The default configuration requires `std::println` support because the server startup path uses `<print>`. For core-only local testing on a host standard library that does not provide `<print>`, configure with:

```sh
cmake -S . -B build -G Ninja -DGRAPHKV_BUILD_SERVER=OFF -DGRAPHKV_REQUIRE_STD_PRINT=OFF
```

Legacy `GRAPHIFY_*` CMake options are still accepted as aliases during the naming migration, but new configuration should use `GRAPHKV_*`. The default binary ingest gate is `GRAPHKV_MAX_INGEST_BYTES=262144000`. `GRAPHKV_ENABLE_USEARCH=ON` pulls the pinned USearch release used for dense document retrieval.
The Drogon WebSocket message cap is set from the same value at server startup, so the transport boundary and the scanner gate reject the same maximum payload size.
Binary WebSocket frames are treated as Zstd-compressed SCIP. The worker reads `ZSTD_getFrameContentSize` before allocation and rejects frames whose decompressed size is unknown or above the strict `250 * 1024 * 1024` byte VRAM admission limit.

## Document Plane

Long documents are ingested through `/v1/ingest_docs` as Zstd-compressed NDJSON bundles. The helper below scans mixed-language repositories for Markdown-like docs and source comments, emits normalized doc/chunk/link events, and the sender compresses the bundle before opening the WebSocket route:

```sh
python3 tools/build_doc_bundle.py /path/to/repo-a /path/to/repo-b \
  --output benchmarks/data/local_repos/mixed_docs.ndjson \
  --max-files 500
python3 tools/send_docs_ws.py benchmarks/data/local_repos/mixed_docs.ndjson --port 8888
```

Every accepted doc bundle publishes a `DocIndexSnapshot` and a `DocRetrievalIndex`. The retrieval index stores native sparse postings over chunk previews, builds dense chunk vectors for USearch, and uses native RRF to blend dense and sparse results. During synthesis, linked chunks that overlap the latest SCIP graph are promoted as graph-connected evidence.

## Docker

```sh
docker build -t graphkv-engine .
docker run --rm -p 8888:8888 graphkv-engine
```

The Dockerfile uses NVIDIA TensorRT-LLM devel/runtime images, clang-22, matching LLVM binutils, Ninja, CMake, Drogon, Protobuf, Kokkos mdspan, Microsoft GSL, and a pinned Folly build.

Production builds should use real Folly for snapshot reclamation. The Docker builder clones pinned upstream `fast_float` and Folly sources, installs Folly into `/opt/folly`, and configures this project with `-DGRAPHKV_REQUIRE_FOLLY=ON`. Local development builds may omit Folly and use the bounded-retire fallback, but that path is not the production concurrency model.

The runtime image includes the extra shared libraries Folly links transitively in this environment, including `libdwarf` and `libelf`. The full Docker path has been validated end to end with the built image and the WebSocket ingest route.
The runtime image also includes the Rust Warden profiling binary at `/opt/graphkv/bin/warden_profile`, so Docker profiling covers both the C++ ingress boundary and the document-sidecar serialization/compression path.

To smoke test the running container with an existing binary SCIP payload, use the helper; it compresses the file with Zstd before sending the WebSocket frame:

```sh
docker run --rm -p 8890:8888 graphkv-engine
python3 tools/send_scip_ws.py /path/to/index.scip.bin --port 8890
```

To rebuild, smoke test both WebSocket routes, and save a Docker profile report with durable build logs:

```sh
python3 benchmarks/docker_e2e_profile.py \
  --image graphkv-engine:e2e \
  --build-log /tmp/graphkv-docker-build.log \
  --json-output benchmarks/results/docker_e2e_latest.json \
  --doc-fixture benchmarks/data/local_repos/mixed_docs.ndjson \
  --warden-profile-records 256 \
  --warden-profile-runs 20 \
  --require-doc-retrieval-markers
```

For local real-world corpus testing, generate SCIP fixtures from dense Python repos and pass them explicitly to the profiler:

```sh
benchmarks/generate_local_repo_corpora.sh /home/Aaditya/browser-use /home/Aaditya/GLM-OCR /home/Aaditya/vllm
python3 benchmarks/docker_e2e_profile.py \
  --image graphkv-engine:e2e \
  --build-log /tmp/graphkv-docker-build.log \
  --json-output benchmarks/results/docker_e2e_local_repos.json \
  --doc-fixture benchmarks/data/local_repos/mixed_docs.ndjson \
  --warden-profile-records 256 \
  --warden-profile-runs 20 \
  --require-doc-retrieval-markers \
  --fixture benchmarks/data/local_repos/GLM-OCR.scip \
  --fixture benchmarks/data/local_repos/browser-use.scip \
  --fixture benchmarks/data/local_repos/vllm.scip \
  --network-fixture benchmarks/data/local_repos/GLM-OCR.scip \
  --network-fixture benchmarks/data/local_repos/browser-use.scip \
  --network-fixture benchmarks/data/local_repos/vllm.scip
```

The generated local corpus directory is intentionally ignored by Git because it can include large machine-specific indexes.

The validated response shape is:

```json
{"ok":true,"mode":"strict-wire-scanner","compressed_bytes":104,"uncompressed_bytes":91,"documents":1,"symbols":1,"relationships":1,"occurrences":1,"arena_bytes":189,"string_bytes":53}
```

The server listens on port `8888` by default. For local smoke tests when that port is already occupied, set `GRAPHKV_PORT`, for example `GRAPHKV_PORT=8899 ./build/scip_ingest_server`. `GRAPHIFY_PORT` remains a temporary fallback while local scripts migrate.

## Boundary Fuzzing

```sh
cmake -S . -B build-fuzz -G Ninja \
  -DCMAKE_CXX_COMPILER=clang++ \
  -DGRAPHKV_BUILD_FUZZERS=ON \
  -DGRAPHKV_BUILD_SERVER=OFF \
  -DGRAPHKV_ENABLE_PROTOBUF_COMPAT=OFF
cmake --build build-fuzz --target ingest_boundary_fuzzer
./build-fuzz/ingest_boundary_fuzzer -runs=10000 -max_len=65536
```
