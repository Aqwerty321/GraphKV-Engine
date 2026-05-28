#include "IngressGate.hpp"

#include <algorithm>
#include <cstddef>
#include <string_view>

#if __has_include(<gsl/gsl>)
#include <gsl/gsl>
#endif

#ifndef Expects
#define Expects(condition)                                                                           \
  do {                                                                                               \
    if (!(condition)) {                                                                              \
      __builtin_trap();                                                                              \
    }                                                                                                \
  } while (false)
#endif

namespace graphify {
namespace {

[[nodiscard]] constexpr auto make_parse_options() {
  auto options = glz::opts{};
  options.null_terminated = false;
  options.error_on_unknown_keys = true;
  options.error_on_missing_keys = false;
  return options;
}

inline constexpr auto parse_options = make_parse_options();

[[nodiscard]] Expected<void, IngressError> fail(IngressSession& session,
                                               BoundaryErrorCode code,
                                               std::size_t offset = 0,
                                               GateLimits limits = {}) noexcept {
  if (session.failure_count < limits.max_failures) {
    ++session.failure_count;
  }
  return unexpected(IngressError{code, offset});
}

[[nodiscard]] Expected<IngressDecision, IngressError> fail_decision(IngressSession& session,
                                                                   BoundaryErrorCode code,
                                                                   std::size_t offset = 0,
                                                                   GateLimits limits = {}) noexcept {
  auto failed = fail(session, code, offset, limits);
  return unexpected(failed.error());
}

void mark_success(IngressSession& session) noexcept {
  session.failure_count = 0;
}

[[nodiscard]] std::string_view bytes_as_string(std::span<const std::byte> payload) noexcept {
  return {reinterpret_cast<const char*>(payload.data()), payload.size()};
}

[[nodiscard]] bool is_auth_type(std::string_view type) noexcept {
  return type == "auth" || type == "authenticate";
}

[[nodiscard]] bool is_control_type(std::string_view type) noexcept {
  return type == "control" || type == "ping" || type == "ack";
}

[[nodiscard]] bool is_opencode_synthesis_type(std::string_view type) noexcept {
  return type == "synthesize" || type == "opencode.session.message";
}

} // namespace

std::string_view to_string(FrameKind kind) noexcept {
  switch (kind) {
    case FrameKind::text:
      return "text";
    case FrameKind::binary:
      return "binary";
    case FrameKind::other:
      return "other";
  }
  return "unknown";
}

std::string_view to_string(RouteKind kind) noexcept {
  switch (kind) {
    case RouteKind::unknown:
      return "unknown";
    case RouteKind::auth:
      return "auth";
    case RouteKind::control:
      return "control";
    case RouteKind::opencode_synthesis:
      return "opencode-synthesis";
    case RouteKind::binary_scip:
      return "binary-scip";
    case RouteKind::binary_docs:
      return "binary-docs";
  }
  return "unknown";
}

std::string_view to_string(BoundaryErrorCode code) noexcept {
  switch (code) {
    case BoundaryErrorCode::none:
      return "none";
    case BoundaryErrorCode::empty_payload:
      return "empty-payload";
    case BoundaryErrorCode::payload_too_large:
      return "payload-too-large";
    case BoundaryErrorCode::control_payload_too_large:
      return "control-payload-too-large";
    case BoundaryErrorCode::unsupported_message_type:
      return "unsupported-message-type";
    case BoundaryErrorCode::malformed_json:
      return "malformed-json";
    case BoundaryErrorCode::unauthorized:
      return "unauthorized";
    case BoundaryErrorCode::circuit_open:
      return "circuit-open";
    case BoundaryErrorCode::queue_saturated:
      return "queue-saturated";
    case BoundaryErrorCode::malformed_scip:
      return "malformed-scip";
    case BoundaryErrorCode::scanner_error:
      return "scanner-error";
    case BoundaryErrorCode::compressed_content_size_unknown:
      return "compressed-content-size-unknown";
    case BoundaryErrorCode::decompressed_payload_too_large:
      return "decompressed-payload-too-large";
    case BoundaryErrorCode::zstd_decompression_failed:
      return "zstd-decompression-failed";
    case BoundaryErrorCode::opencode_serialization_failed:
      return "opencode-serialization-failed";
    case BoundaryErrorCode::internal_error:
      return "internal-error";
  }
  return "unknown";
}

Expected<void, IngressError> would_accept_binary_size(std::size_t payload_size, GateLimits limits) noexcept {
  IngressSession scratch;
  if (payload_size == 0) {
    return fail(scratch, BoundaryErrorCode::empty_payload, 0, limits);
  }
  if (payload_size > limits.max_binary_bytes) {
    return fail(scratch, BoundaryErrorCode::payload_too_large, 0, limits);
  }
  return {};
}

Expected<IngressDecision, IngressError> classify_frame(FrameKind kind,
                                                      std::span<const std::byte> payload,
                                                      IngressSession& session,
                                                      GateLimits limits) {
  if (session.failure_count >= limits.max_failures) {
    return unexpected(IngressError{BoundaryErrorCode::circuit_open, 0});
  }
  if (payload.empty()) {
    return fail_decision(session, BoundaryErrorCode::empty_payload, 0, limits);
  }
  if (payload.data() == nullptr) {
    return fail_decision(session, BoundaryErrorCode::internal_error, 0, limits);
  }
  Expects(payload.data() != nullptr);

  if (kind == FrameKind::binary) {
    if (payload.size() > limits.max_binary_bytes) {
      return fail_decision(session, BoundaryErrorCode::payload_too_large, 0, limits);
    }
    mark_success(session);
    return IngressDecision{
      .route_kind = limits.binary_route,
        .tenant_id = session.tenant_id,
        .payload_size = payload.size(),
    };
  }

  if (kind != FrameKind::text) {
    return fail_decision(session, BoundaryErrorCode::unsupported_message_type, 0, limits);
  }
  if (payload.size() > limits.max_control_bytes) {
    return fail_decision(session, BoundaryErrorCode::control_payload_too_large, 0, limits);
  }

  ControlMessage control;
  const auto json = bytes_as_string(payload);
  auto parsed = glz::read<parse_options>(control, json);
  if (parsed) {
    return fail_decision(session, BoundaryErrorCode::malformed_json, parsed.count, limits);
  }

  if (is_auth_type(control.type)) {
    if (control.token.empty() || control.tenant_id == 0) {
      return fail_decision(session, BoundaryErrorCode::unauthorized, 0, limits);
    }
    session.authenticated = true;
    session.tenant_id = control.tenant_id;
    mark_success(session);
    return IngressDecision{
        .route_kind = RouteKind::auth,
        .tenant_id = session.tenant_id,
        .payload_size = payload.size(),
        .control = control,
    };
  }

  if (is_opencode_synthesis_type(control.type)) {
    if (!session.authenticated || control.session_id.empty() || control.query.empty()) {
      return fail_decision(session, BoundaryErrorCode::unauthorized, 0, limits);
    }
    mark_success(session);
    return IngressDecision{
        .route_kind = RouteKind::opencode_synthesis,
        .tenant_id = session.tenant_id,
        .payload_size = payload.size(),
        .control = control,
    };
  }

  if (is_control_type(control.type)) {
    mark_success(session);
    return IngressDecision{
        .route_kind = RouteKind::control,
        .tenant_id = session.tenant_id,
        .payload_size = payload.size(),
        .control = control,
    };
  }

  return fail_decision(session, BoundaryErrorCode::unsupported_message_type, 0, limits);
}

} // namespace graphify
