#include "IngressGate.hpp"
#include "TelemetryRingBuffer.hpp"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <span>

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t* data, std::size_t size) {
  graphify::IngressSession session;
  const graphify::GateLimits limits{.max_binary_bytes = 1024, .max_control_bytes = 512, .max_failures = 16};
  const auto payload = std::as_bytes(std::span<const std::uint8_t>(data, size));

  const auto kind = (size > 0 && (data[0] & 1U) != 0U) ? graphify::FrameKind::text : graphify::FrameKind::binary;
  auto decision = graphify::classify_frame(kind, payload, session, limits);

  static_cast<void>(graphify::would_accept_binary_size(size, limits));
  static_cast<void>(graphify::would_accept_binary_size(static_cast<std::size_t>(GRAPHKV_MAX_INGEST_BYTES) + 1U));

  graphify::TelemetryRingBuffer<graphify::IngestMetric, 8> metrics;
  static_cast<void>(metrics.try_push(graphify::IngestMetric{
      .query_id = size,
      .payload_size = size,
      .route_kind = static_cast<std::uint16_t>(decision ? decision->route_kind : graphify::RouteKind::unknown),
      .error_code = static_cast<std::uint16_t>(decision ? graphify::BoundaryErrorCode::none : decision.error().code),
  }));
  graphify::IngestMetric metric;
  while (metrics.try_pop(metric)) {
  }

  return 0;
}
