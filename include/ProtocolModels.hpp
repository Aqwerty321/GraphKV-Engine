#pragma once

#include "GraphKVConfig.hpp"

#include <cstddef>
#include <cstdint>
#include <string_view>

#include <glaze/glaze.hpp>

namespace graphify {

enum class FrameKind : std::uint8_t {
  text,
  binary,
  other,
};

enum class RouteKind : std::uint8_t {
  unknown,
  auth,
  control,
  opencode_synthesis,
  binary_scip,
  binary_docs,
};

enum class BoundaryErrorCode : std::uint8_t {
  none,
  empty_payload,
  payload_too_large,
  control_payload_too_large,
  unsupported_message_type,
  malformed_json,
  unauthorized,
  circuit_open,
  queue_saturated,
  malformed_scip,
  scanner_error,
  compressed_content_size_unknown,
  decompressed_payload_too_large,
  zstd_decompression_failed,
  opencode_serialization_failed,
  internal_error,
};

struct GateLimits final {
  std::size_t max_binary_bytes{graphkv_max_ingest_bytes};
  std::size_t max_control_bytes{graphkv_max_control_bytes};
  std::uint32_t max_failures{8};
  RouteKind binary_route{RouteKind::binary_scip};
};

struct AuthHandshake final {
  std::string_view type{};
  std::string_view token{};
  std::uint64_t tenant_id{};
};

struct ControlMessage final {
  std::string_view type{};
  std::string_view token{};
  std::string_view request_id{};
  std::string_view session_id{};
  std::string_view message_id{};
  std::string_view agent{};
  std::string_view provider_id{};
  std::string_view model_id{};
  std::string_view query{};
  std::uint64_t tenant_id{};
  std::uint64_t payload_size{};
  bool no_reply{};
};

struct IngressSession final {
  bool authenticated{};
  std::uint64_t tenant_id{};
  std::uint32_t failure_count{};
};

struct IngressError final {
  BoundaryErrorCode code{};
  std::size_t offset{};
};

struct IngressDecision final {
  RouteKind route_kind{RouteKind::unknown};
  std::uint64_t tenant_id{};
  std::uint64_t payload_size{};
  ControlMessage control{};
};

[[nodiscard]] std::string_view to_string(FrameKind kind) noexcept;
[[nodiscard]] std::string_view to_string(RouteKind kind) noexcept;
[[nodiscard]] std::string_view to_string(BoundaryErrorCode code) noexcept;

} // namespace graphify

template <>
struct glz::meta<graphify::ControlMessage> {
  using T = graphify::ControlMessage;
  static constexpr auto value = object(
      "type",
      &T::type,
      "token",
      &T::token,
      "request_id",
      &T::request_id,
      "session_id",
      &T::session_id,
      "message_id",
      &T::message_id,
      "agent",
      &T::agent,
      "provider_id",
      &T::provider_id,
      "model_id",
      &T::model_id,
      "query",
      &T::query,
      "tenant_id",
      &T::tenant_id,
      "payload_size",
      &T::payload_size,
      "no_reply",
      &T::no_reply);
};

template <>
struct glz::meta<graphify::AuthHandshake> {
  using T = graphify::AuthHandshake;
  static constexpr auto value = object("type", &T::type, "token", &T::token, "tenant_id", &T::tenant_id);
};
