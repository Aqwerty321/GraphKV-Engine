#include "IngressGate.hpp"

#include <cassert>
#include <cstddef>
#include <span>
#include <string_view>

namespace {

[[nodiscard]] std::span<const std::byte> bytes(std::string_view value) noexcept {
  return std::as_bytes(std::span<const char>(value.data(), value.size()));
}

} // namespace

int main() {
  using namespace graphify;

  GateLimits limits{.max_binary_bytes = 8, .max_control_bytes = 512, .max_failures = 3};
  IngressSession session;

  auto empty = classify_frame(FrameKind::binary, {}, session, limits);
  assert(!empty);
  assert(empty.error().code == BoundaryErrorCode::empty_payload);

  auto size_check = would_accept_binary_size(9, limits);
  assert(!size_check);
  assert(size_check.error().code == BoundaryErrorCode::payload_too_large);

  const std::byte binary_payload[4]{std::byte{0x0A}, std::byte{0x01}, std::byte{0x78}, std::byte{0x00}};
  auto binary = classify_frame(FrameKind::binary, binary_payload, session, limits);
  assert(binary);
  assert(binary->route_kind == RouteKind::binary_scip);
  assert(binary->payload_size == 4);

  auto doc_limits = limits;
  doc_limits.binary_route = RouteKind::binary_docs;
  auto doc_binary = classify_frame(FrameKind::binary, binary_payload, session, doc_limits);
  assert(doc_binary);
  assert(doc_binary->route_kind == RouteKind::binary_docs);

  constexpr std::string_view auth = R"({"type":"auth","token":"dev","tenant_id":42})";
  auto auth_result = classify_frame(FrameKind::text, bytes(auth), session, limits);
  assert(auth_result);
  assert(auth_result->route_kind == RouteKind::auth);
  assert(session.authenticated);
  assert(session.tenant_id == 42);

  constexpr std::string_view control = R"({"type":"ping","request_id":"r1"})";
  auto control_result = classify_frame(FrameKind::text, bytes(control), session, limits);
  assert(control_result);
  assert(control_result->route_kind == RouteKind::control);
  assert(control_result->control.request_id == "r1");

  constexpr std::string_view synthesize = R"({"type":"synthesize","session_id":"sess_01","query":"why is ingest slow?","provider_id":"anthropic","model_id":"claude-sonnet-4"})";
  auto synthesize_result = classify_frame(FrameKind::text, bytes(synthesize), session, limits);
  assert(synthesize_result);
  assert(synthesize_result->route_kind == RouteKind::opencode_synthesis);
  assert(synthesize_result->control.session_id == "sess_01");
  assert(synthesize_result->control.query == "why is ingest slow?");

  constexpr std::string_view unknown_key = R"({"type":"ping","surprise":true})";
  auto unknown = classify_frame(FrameKind::text, bytes(unknown_key), session, limits);
  assert(!unknown);
  assert(unknown.error().code == BoundaryErrorCode::malformed_json);

  constexpr std::string_view bad_auth = R"({"type":"auth","tenant_id":42})";
  auto unauthorized = classify_frame(FrameKind::text, bytes(bad_auth), session, limits);
  assert(!unauthorized);
  assert(unauthorized.error().code == BoundaryErrorCode::unauthorized);

  session.failure_count = limits.max_failures;
  auto circuit = classify_frame(FrameKind::text, bytes(control), session, limits);
  assert(!circuit);
  assert(circuit.error().code == BoundaryErrorCode::circuit_open);

  return 0;
}
