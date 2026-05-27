# Graphify SCIP Ingestion Server

Headless C++26 WebSocket ingestion server for binary SCIP indexes. Phase 1 receives SCIP protobuf payloads at `/v1/ingest_scip`, converts them into an immutable GraphKV CSR snapshot, and publishes that snapshot for read-side consumers.

The production ingest path uses a custom protobuf wire scanner for the SCIP fields needed by GraphKV. Generated SCIP protobuf is still built for compatibility, tests, and schema validation, but durable GraphKV snapshots own their string bytes in a contiguous `StringTape`.

## Build

```sh
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build --target scip_ingest_server
ctest --test-dir build --output-on-failure
```

The default configuration requires `std::println` support because the server startup path uses `<print>`. For core-only local testing on a host standard library that does not provide `<print>`, configure with:

```sh
cmake -S . -B build -G Ninja -DGRAPHIFY_BUILD_SERVER=OFF -DGRAPHIFY_REQUIRE_STD_PRINT=OFF
```

## Docker

```sh
docker build -t graphify-scip-ingest .
docker run --rm -p 8888:8888 graphify-scip-ingest
```

The Dockerfile uses NVIDIA TensorRT-LLM devel/runtime images, clang-18, clang-tools-18, Ninja, CMake, Drogon, Protobuf, Kokkos mdspan, Microsoft GSL, and a pinned Folly build.

Production builds should use real Folly for snapshot reclamation. The Docker builder clones pinned upstream `fast_float` and Folly sources, installs Folly into `/opt/folly`, and configures this project with `-DGRAPHIFY_REQUIRE_FOLLY=ON`. Local development builds may omit Folly and use the bounded-retire fallback, but that path is not the production concurrency model.

The runtime image includes the extra shared libraries Folly links transitively in this environment, including `libdwarf` and `libelf`. The full Docker path has been validated end to end with the built image and the WebSocket ingest route.

To smoke test the running container with an existing binary SCIP payload:

```sh
docker run --rm -p 8890:8888 graphify-scip-ingest
python3 tools/send_scip_ws.py /path/to/index.scip.bin --port 8890
```

The validated response shape is:

```json
{"ok":true,"mode":"strict-wire-scanner","documents":1,"symbols":1,"relationships":1,"occurrences":1,"arena_bytes":189,"string_bytes":53}
```

The server listens on port `8888` by default. For local smoke tests when that port is already occupied, set `GRAPHIFY_PORT`, for example `GRAPHIFY_PORT=8899 ./build/scip_ingest_server`.
