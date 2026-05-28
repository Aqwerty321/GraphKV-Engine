#pragma once

#include "ExpectedCompat.hpp"

#include <cstdint>
#include <string>
#include <string_view>

namespace graphify {

enum class OpenCodeErrorCode : std::uint8_t {
  missing_session_id,
  invalid_session_id,
  missing_query,
};

struct OpenCodeError final {
  OpenCodeErrorCode code{};
};

struct OpenCodeModelRef final {
  std::string_view provider_id{};
  std::string_view model_id{};
};

struct SynthesizedContext final {
  std::string_view query{};
  std::string_view dense_summary{};
  std::uint64_t tenant_id{};
  std::uint64_t source_bytes{};
  std::uint64_t estimated_input_tokens{};
  std::uint32_t node_count{};
  std::uint32_t edge_count{};
};

struct OpenCodeSessionMessageRequest final {
  std::string_view session_id{};
  std::string_view message_id{};
  std::string_view agent{};
  std::string_view system{};
  OpenCodeModelRef model{};
  SynthesizedContext context{};
  bool no_reply{};
  bool structured_output{true};
};

struct SerializedOpenCodeRequest final {
  std::string method{};
  std::string path{};
  std::string body{};
};

[[nodiscard]] std::string_view to_string(OpenCodeErrorCode code) noexcept;
void append_json_string(std::string& out, std::string_view value);
[[nodiscard]] Expected<SerializedOpenCodeRequest, OpenCodeError> serialize_session_message_request(
    const OpenCodeSessionMessageRequest& request);

} // namespace graphify
