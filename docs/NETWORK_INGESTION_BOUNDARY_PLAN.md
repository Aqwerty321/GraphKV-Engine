# GraphKV-Engine Network Ingestion Boundary Plan

Research snapshot: 2026-05-27

## Executive Summary

The next ingestion boundary should keep the Phase 1 SCIP wire scanner and arena
publisher intact, but move every transport decision around it into a small,
fuzzable boundary library. Drogon should become a thin WebSocket adapter. It
should receive frames, run cheap admission checks, parse small JSON control
messages with Glaze, enqueue admitted binary SCIP work onto bounded workers, and
publish telemetry without blocking the event loop.

The most valuable upgrade is not a larger networking stack. It is a bounded,
observable async handoff that replaces the current detached-thread-per-message
path. C++26 `std::execution`, standard hazard pointers, RCU, contracts, and
`inplace_vector` are promising, but compiler/library support is still uneven.
Use their design direction, not their headers as required dependencies yet.

The macro-architecture is now a High-Speed Context Synthesizer for OpenCode,
not a standalone local LLM server. GraphKV owns the RAM-resident code graph and
acts as a 2M-token-equivalent hardware filter. The local GPU path should later
page only relevant subgraphs into Unified Memory, synthesize dense context, and
hand the cloud reasoner an OpenCode-native `POST /session/{id}/message` request
body. The Drogon boundary must preserve sub-100us local hot-path behavior by
validating, classifying, and serializing only; graph selection, GPU work, and
cloud network I/O stay out of the event loop.

GNN analysis should not run on the hot network path. If it becomes useful, it
belongs in an asynchronous telemetry or graph-snapshot sidecar that scores
behavior after admission and ingestion. Radix/ART-style structures are also not
default hot-path requirements; they are justified only for bounded prefix,
tenant, token, or route lookup tables that benchmarks show are beating simple
flat maps or fixed dispatch.

## Baseline To Preserve

The repo already has the important Phase 1 pieces:

- Strict SCIP wire scanning in `ScipIngestor`.
- Arena-backed immutable graph snapshots in `GraphKV.hpp`.
- Optional Folly hazard-pointer publication behavior.
- A Drogon WebSocket endpoint at `/v1/ingest_scip`.

The boundary work should integrate those pieces, not redesign them. The scanner
should still receive a `std::span<const std::byte>` after admission. Snapshot
publication should remain in the existing publisher. Scanner errors should be
mapped into boundary telemetry and responses without losing offsets or error
codes.

## Hard Requirements

- Build as C++26, with clang-oriented support, while feature-gating library
  facilities that are not widely implemented.
- Use Glaze for JSON control/auth parsing on the control path.
- Avoid `std::string` ownership on the hot JSON path; parse views over the
  inbound buffer and copy only the minimal session state that must outlive it.
- Do not use exceptions on the boundary hot path.
- Reject binary payloads larger than the named max-ingest constant before
  casting, parsing, or worker dispatch.
- Use `gsl::Expects` only after runtime validation has established invariants.
- Keep Drogon event-loop callbacks thin and nonblocking.
- Speak OpenCode-native JSON for context synthesis controls: the `synthesize`
  route should emit a session-message envelope containing `model`, `agent`,
  `system`, `parts`, and a structured-output `format` contract.
- Emit telemetry through nonblocking SPSC queues using acquire/release index
  publication.
- Add a deterministic LLVM LibFuzzer target for the transport-free boundary.

## Naming Decision

Use GraphKV or GraphKV-Engine for user-facing names going forward.

Recommended staged approach:

1. Rename public wording first: README, DESIGN, server logs, executable names,
   CMake options, environment variables, and comments.
2. Introduce `GRAPHKV_*` CMake options and macros, especially the ingest-size
   limit.
3. Rename the C++ namespace from `graphify` to `graphkv` only in a separate
   mechanical pass if we want full internal consistency. That rename is broad
   enough to obscure behavioral changes if mixed into the ingress patch.

Because this repo is still early, prefer a direct move to GraphKV names instead
of carrying compatibility aliases indefinitely. A short transitional alias is
reasonable only if an existing local smoke tool still needs it during the same
patch series.

## 2026 C++26 Research Notes

C++26 is useful as a target mode, but not yet a portable dependency surface.
Current compiler/library support is partial across the features most tempting
for this boundary.

Relevant features and how to treat them:

- `std::execution` / P2300 senders-receivers: important design direction for
  structured async, typed completion, explicit scheduler hops, and cancellation.
  Do not require the standard header yet. If we want to experiment, use
  `stdexec` behind an optional build flag, not in the default server path.
- `std::hazard_pointer` and `std::rcu`: relevant to snapshot publication, but
  not a reason to replace the existing Folly hazard-pointer path until standard
  library support is real in the chosen toolchain.
- `std::inplace_vector`: useful for tiny bounded arrays, but gate with feature
  tests or use a local fixed buffer until libc++/libstdc++ support is dependable.
- Contracts: keep using runtime validation plus `gsl::Expects`; do not depend
  on C++26 contracts semantics for production safety yet.
- `std::expected`: prefer it when available, but keep `ExpectedCompat.hpp` as
  the project portability layer until the toolchain matrix is settled.
- `std::hardware_destructive_interference_size`: appropriate for queue index
  padding if available; provide a conservative fallback cache-line constant.

Design implication: model the boundary as if P2300 exists, but implement the
first pass with a small bounded dispatcher using `std::jthread`, `stop_token`,
and project-local SPSC queues. Detached work should disappear.

## Glaze Control Path

Glaze should parse small text control frames only. Binary SCIP remains the
scanner path.

Use `glz::opts` explicitly:

- `null_terminated = false` for raw WebSocket frame buffers.
- `error_on_unknown_keys = true` unless a real OpenCode or Toolhouse payload
  requires extension behavior.
- `error_on_missing_keys = true` for auth handshakes and strict control verbs.
- Set bounded string and array limits for untrusted control input.

Recommended initial control limit: 64 KiB. It is intentionally much smaller
than the binary SCIP limit because auth/control messages should not be large.

The parsed models should use `std::string_view` for fields such as token, type,
tool, and request id. Those views are valid only while the inbound message
buffer is alive. Session state must copy only durable fields, such as a tenant
id, a small authenticated principal id, failure counters, and feature flags.

## Binary SCIP Path

The binary path should be admitted before it reaches `ScipIngestor`:

1. Reject non-binary frames for the binary route.
2. Reject empty payloads.
3. Reject payloads above the hard size limit.
4. Push the admitted payload into a bounded worker shard.
5. In the worker, create `std::span<const std::byte>` and call the existing
   strict `ScipIngestor`.
6. Publish successful snapshots through the existing `SnapshotPublisher`.
7. Map scanner errors to boundary errors and telemetry.

Size constant decision:

- Use one named constant for the boundary, for example
  `GRAPHKV_MAX_INGEST_BYTES`.
- Recommended value: `250ULL * 1024ULL * 1024ULL` bytes, documented as 250 MiB.
- If product policy means decimal MB, change only that constant to
  `250000000ULL` and keep all call sites using the name.

## Proposed Architecture

Create a transport-free ingress boundary library and keep Drogon outside it.

```text
WebSocket frame
  -> Drogon IngestController
  -> IngressGate admission/classification
  -> Glaze control parser or binary SCIP admission
  -> bounded per-loop work queue
  -> SCIP worker
  -> existing ScipIngestor
  -> existing SnapshotPublisher
  -> response and telemetry
```

The key boundary is between `IngestController` and `IngressGate`. Everything
below `IngressGate` should be fuzzable without Drogon.

## Deliverable Plan

### 1. CMakeLists.txt

Primary changes:

- Rename public project/build options from `GRAPHIFY_*` to `GRAPHKV_*` where
  practical.
- Add `GRAPHKV_MAX_INGEST_BYTES` with the 250 MiB default.
- Fetch Glaze once and link it only where needed.
- Add a static `graphkv_ingress_boundary` target for protocol models,
  admission, telemetry, and parse helpers.
- Keep Drogon linked only to the server/controller target.
- Add `GRAPHKV_BUILD_FUZZERS` default OFF.
- Add a Clang-only fuzzer target using `-fsanitize=fuzzer,address,undefined`.

Keep generated build directories untouched.

### 2. include/TelemetryRingBuffer.hpp

Add a header-only SPSC ring for trivially copyable metric records.

Expected shape:

- Power-of-two capacity.
- `try_push` on the producer side.
- `try_pop` or `pop_all(callback)` on the consumer side.
- `dropped_count()` using relaxed atomics for telemetry loss.
- Producer and consumer indices padded to separate destructive cache lines.
- Release store when publishing a produced index.
- Acquire load when observing a producer-published index.

Do not use one global SPSC queue for all Drogon threads. If multiple event loops
produce metrics, use one SPSC shard per producer or switch explicitly to a real
MPMC/MPSC design.

Initial `IngestMetric` fields:

- monotonic timestamp in nanoseconds
- query id or request sequence
- tenant id
- payload size
- route kind
- decision or error code
- latency microseconds
- flags

### 3. include/ProtocolModels.hpp

Define the small Glaze-mapped JSON surface:

- `AuthHandshake`
- `ControlMessage`
- `RouteKind`
- `BoundaryErrorCode`
- Optional `ControlAck` / `ErrorResponse` if keeping response generation typed

Use fixed-width integers and `std::string_view` fields. Provide `glz::meta`
specializations for stable key mapping. Keep the schema deliberately small
until real OpenCode/Toolhouse payload examples require more fields.

### 4. include/IngressGate.hpp and src/IngressGate.cpp

This is the transport-independent admission and parse layer.

Responsibilities:

- Validate pointer and length before contracts.
- Enforce text-control and binary-SCIP size gates.
- Classify frame kind.
- Parse JSON control frames with Glaze.
- Maintain or update small session state supplied by the controller.
- Return typed errors through `std::expected` or `ExpectedCompat` semantics.
- Produce telemetry-ready decisions without knowing Drogon types.

The gate should expose helpers that let the fuzzer simulate oversize decisions
without allocating a 250 MiB input.

### 5. include/IngestController.hpp and src/IngestController.cpp

Replace the current binary-only controller behavior with a thin adapter.

Controller responsibilities:

- Register `/v1/ingest_scip` to preserve current smoke tooling.
- Optionally register `/v1/ingest` as the future neutral GraphKV-Engine route.
- Initialize per-connection session state.
- In `handleNewMessage`, do only admission, lightweight parse/classification,
  nonblocking enqueue, response scheduling, and telemetry push.
- Never call `ScipIngestor::ingest` on a Drogon event loop.
- Never spawn a detached thread per message.

For binary SCIP work, move the framework-provided payload storage into a work
item only after the gate has admitted it. The worker owns the payload while the
scanner runs.

### 6. Bounded Dispatcher

This can live inside the controller implementation at first. Avoid a larger
abstraction until the shape is proven.

Recommended first implementation:

- One bounded SPSC work shard per Drogon event-loop producer.
- One worker thread per shard, or a small fixed worker set with explicit queue
  ownership.
- Nonblocking `try_push` from the event loop.
- Reject with `queue_saturated` if a shard is full.
- Use `std::jthread` and `stop_token` for shutdown.

If one worker per event loop is too many, switch deliberately to a bounded MPSC
queue. Do not call an SPSC queue multi-producer safe by convention.

### 7. fuzz/fuzz_target.cpp

The fuzzer should target `IngressGate`, not Drogon.

Entry point:

```cpp
extern "C" int LLVMFuzzerTestOneInput(const uint8_t* data, size_t size);
```

Fuzz scope:

- Text control classification.
- Glaze JSON parsing and error mapping.
- Unknown-key rejection.
- Empty input handling.
- Binary admission and cheap SCIP route validation.
- Simulated oversize checks through helper APIs.
- Optional tiny telemetry ring push/pop exercise.

Fuzzer rules:

- No persistent worker threads.
- No logging.
- No `exit()`.
- No global mutable state except immutable configuration.
- Keep max input length small for JSON/control fuzzing, for example 64 KiB.

## Async Design Decision

The current detached-thread approach avoids blocking the event loop, but it has
unbounded concurrency, weak shutdown behavior, difficult telemetry, and poor
backpressure. Replace it with bounded admission.

P2300 reinforces the right shape: async work should be structured, cancellation
aware, and explicit about scheduler transitions. The initial implementation can
follow those principles without requiring standard `std::execution` support.

Practical rule: every inbound frame must end in exactly one of these outcomes:

- rejected before parse
- parsed as JSON control and handled synchronously without blocking
- admitted to a bounded binary worker queue
- rejected because the worker queue is saturated

There should be no hidden fifth state where work escapes into an untracked
detached thread.

## Radix / ART Decision

Do not add an Adaptive Radix Tree in the first implementation.

Where it could help later:

- Tenant or tool-prefix routing with many stable prefixes.
- Token prefix classification if authentication evolves beyond a small fixed
  schema.
- Fast lookup over canonical symbol prefixes after SCIP ingestion, outside the
  network callback.

Where it is unlikely to help now:

- A handful of route strings.
- A small enum-based JSON control verb set.
- Per-connection state lookup keyed by one connection pointer or session id.

Decision rule: use fixed dispatch or flat hash maps first. Add radix/ART only
behind a narrow lookup interface after a benchmark shows lookup cost or
allocation churn is material.

## GNN Decision

Do not put a GNN in the ingestion hot path.

Why:

- GNN inference adds model loading, feature extraction, memory pressure, and
  latency variance exactly where the boundary needs deterministic behavior.
- The first boundary problem is admission, parsing, backpressure, and scanner
  integration, not semantic graph risk scoring.
- The useful graph context appears after SCIP ingestion and snapshot
  publication, not before the payload has been validated.

Minimal-cost path if we want GNN value later:

- Emit telemetry and graph-change features asynchronously.
- Drain them into an offline or sidecar analyzer.
- Score tenant/tool/session behavior after ingestion.
- Feed only coarse policy updates back to the boundary, such as denylisted
  tenant ids or dynamic rate limits.

This keeps the boundary deterministic while leaving room for research-grade
anomaly detection.

## Optional Future Transport Tiers

### io_uring

`io_uring` is a future transport backend candidate for Linux-specific raw TCP or
HTTP/WebSocket handling. It can reduce syscall overhead through shared
submission/completion rings and batching, but it also adds ordering and lifetime
complexity.

Do not add it to the Drogon-based implementation. Keep the boundary library
transport-free so a future `io_uring` adapter can reuse `IngressGate`.

### AF_XDP / XDP

AF_XDP is valuable for raw packet processing, early source filtering, rate
limiting, and packet telemetry. It is not a direct replacement for an
application-level WebSocket parser, especially when TLS or reverse proxies are
involved.

Use it later only as an outer L3/L4 prefilter or telemetry source. Do not let it
infect the core control/binary admission logic.

## Error Model

Use a compact boundary error enum with stable response mapping:

- `empty_payload`
- `payload_too_large`
- `control_payload_too_large`
- `unsupported_message_type`
- `malformed_json`
- `unknown_control_key`
- `unauthorized`
- `circuit_open`
- `queue_saturated`
- `malformed_scip`
- `scanner_error`
- `internal_error`

Scanner-specific `IngestErrorCode` should be preserved in telemetry or response
detail when safe.

## Testing Plan

Add focused tests instead of broad fragile integration tests first.

Boundary tests:

- empty input
- oversized binary payload decision without huge allocation
- control payload over JSON limit
- malformed JSON
- unknown JSON keys
- valid auth handshake
- invalid auth handshake
- binary route classification
- queue saturation response path through a fake dispatcher
- circuit-breaker/failure-count behavior

Telemetry tests:

- push/pop ordering
- wraparound
- full-queue drop count
- consumer drain callback count
- trivial-copy and alignment static assertions

Controller smoke tests can stay light because Drogon itself is not the unit
under test. Preserve the existing binary SCIP smoke sender path.

## Verification Commands

Normal build:

```bash
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build
ctest --test-dir build --output-on-failure
```

Fuzzer build:

```bash
cmake -S . -B build-fuzz -G Ninja \
  -DCMAKE_CXX_COMPILER=clang++ \
  -DGRAPHKV_BUILD_FUZZERS=ON \
  -DGRAPHKV_BUILD_SERVER=OFF
cmake --build build-fuzz --target ingest_boundary_fuzzer
./build-fuzz/ingest_boundary_fuzzer -runs=10000 -max_len=65536
```

## Implementation Order

1. Add the doc and agree on the boundary decisions.
2. Apply GraphKV naming to public repo surfaces.
3. Update CMake and introduce the boundary target.
4. Add `TelemetryRingBuffer.hpp` with tests.
5. Add `ProtocolModels.hpp` and Glaze parse helpers with tests.
6. Add `IngressGate` with tests and fuzzer harness.
7. Add the bounded dispatcher and new Drogon `IngestController`.
8. Wire server entrypoint to the new controller.
9. Run focused tests and smoke the existing SCIP route.

## Non-Goals For The First Patch

- Rewriting `ScipIngestor`.
- Replacing `SnapshotPublisher`.
- Adding AF_XDP or `io_uring` transport code.
- Requiring standard `std::execution` headers.
- Adding a GNN model or inference runtime.
- Adding ART/radix indexes without a benchmark-backed lookup bottleneck.
- Editing generated build directories.
