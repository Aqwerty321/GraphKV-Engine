#include "OpenCodeProtocol.hpp"

#include <cctype>
#include <string>

namespace graphify {
namespace {

[[nodiscard]] bool is_safe_session_id(std::string_view value) noexcept {
  if (value.empty()) {
    return false;
  }
  for (unsigned char character : value) {
    if (std::isalnum(character) != 0 || character == '-' || character == '_') {
      continue;
    }
    return false;
  }
  return true;
}

void append_json_property(std::string& out, std::string_view key, std::string_view value) {
  append_json_string(out, key);
  out.push_back(':');
  append_json_string(out, value);
}

void append_json_u64_property(std::string& out, std::string_view key, std::uint64_t value) {
  append_json_string(out, key);
  out.push_back(':');
  out += std::to_string(value);
}

void append_graphkv_context_text(std::string& out, const SynthesizedContext& context) {
  out += "GRAPHKV_CONTEXT_SYNTHESIS_V1\n";
  out += "role: high-speed-context-synthesizer\n";
  out += "query: ";
  out += context.query;
  out += "\n";
  out += "tenant_id: ";
  out += std::to_string(context.tenant_id);
  out += "\n";
  out += "arena_source_bytes: ";
  out += std::to_string(context.source_bytes);
  out += "\n";
  out += "estimated_input_tokens: ";
  out += std::to_string(context.estimated_input_tokens);
  out += "\n";
  out += "selected_nodes: ";
  out += std::to_string(context.node_count);
  out += "\n";
  out += "selected_edges: ";
  out += std::to_string(context.edge_count);
  out += "\n";
  out += "dense_summary:\n";
  out += context.dense_summary;
}

void append_structured_output_format(std::string& out) {
  out += R"("format":{"type":"json_schema","schema":{"type":"object","additionalProperties":false,"properties":{"answer":{"type":"string","description":"Final reasoning answer grounded only in the GraphKV context payload."},"citations":{"type":"array","items":{"type":"string"},"description":"Workspace paths, symbols, or graph node identifiers used as evidence."},"risks":{"type":"array","items":{"type":"string"},"description":"Ambiguities, missing graph evidence, or implementation risks."},"actions":{"type":"array","items":{"type":"string"},"description":"Concrete next engineering actions."}},"required":["answer","citations","risks","actions"]}})";
}

} // namespace

std::string_view to_string(OpenCodeErrorCode code) noexcept {
  switch (code) {
    case OpenCodeErrorCode::missing_session_id:
      return "missing-session-id";
    case OpenCodeErrorCode::invalid_session_id:
      return "invalid-session-id";
    case OpenCodeErrorCode::missing_query:
      return "missing-query";
  }
  return "unknown";
}

void append_json_string(std::string& out, std::string_view value) {
  out.push_back('"');
  for (unsigned char character : value) {
    switch (character) {
      case '"':
        out += "\\\"";
        break;
      case '\\':
        out += "\\\\";
        break;
      case '\b':
        out += "\\b";
        break;
      case '\f':
        out += "\\f";
        break;
      case '\n':
        out += "\\n";
        break;
      case '\r':
        out += "\\r";
        break;
      case '\t':
        out += "\\t";
        break;
      default:
        if (character < 0x20U) {
          constexpr char hex[] = "0123456789abcdef";
          out += "\\u00";
          out.push_back(hex[(character >> 4U) & 0x0FU]);
          out.push_back(hex[character & 0x0FU]);
        } else {
          out.push_back(static_cast<char>(character));
        }
        break;
    }
  }
  out.push_back('"');
}

Expected<SerializedOpenCodeRequest, OpenCodeError> serialize_session_message_request(
    const OpenCodeSessionMessageRequest& request) {
  if (request.session_id.empty()) {
    return unexpected(OpenCodeError{OpenCodeErrorCode::missing_session_id});
  }
  if (!is_safe_session_id(request.session_id)) {
    return unexpected(OpenCodeError{OpenCodeErrorCode::invalid_session_id});
  }
  if (request.context.query.empty()) {
    return unexpected(OpenCodeError{OpenCodeErrorCode::missing_query});
  }

  SerializedOpenCodeRequest serialized{.method = "POST"};
  serialized.path.reserve(request.session_id.size() + 18U);
  serialized.path += "/session/";
  serialized.path += request.session_id;
  serialized.path += "/message";

  std::string context_text;
  context_text.reserve(request.context.query.size() + request.context.dense_summary.size() + 256U);
  append_graphkv_context_text(context_text, request.context);

  auto& out = serialized.body;
  out.reserve(context_text.size() + request.message_id.size() + request.agent.size() + request.system.size() + 768U);
  out.push_back('{');
  bool needs_comma = false;
  auto comma = [&] {
    if (needs_comma) {
      out.push_back(',');
    }
    needs_comma = true;
  };

  if (!request.message_id.empty()) {
    comma();
    append_json_property(out, "messageID", request.message_id);
  }
  if (!request.model.provider_id.empty() && !request.model.model_id.empty()) {
    comma();
    out += R"("model":{)";
    append_json_property(out, "providerID", request.model.provider_id);
    out.push_back(',');
    append_json_property(out, "modelID", request.model.model_id);
    out.push_back('}');
  }
  if (!request.agent.empty()) {
    comma();
    append_json_property(out, "agent", request.agent);
  }
  if (request.no_reply) {
    comma();
    out += R"("noReply":true)";
  }
  if (!request.system.empty()) {
    comma();
    append_json_property(out, "system", request.system);
  }
  if (request.structured_output) {
    comma();
    append_structured_output_format(out);
  }
  comma();
  out += R"("parts":[{"type":"text","synthetic":true,"metadata":{)";
  append_json_u64_property(out, "tenant_id", request.context.tenant_id);
  out.push_back(',');
  append_json_u64_property(out, "source_bytes", request.context.source_bytes);
  out.push_back(',');
  append_json_u64_property(out, "estimated_input_tokens", request.context.estimated_input_tokens);
  out.push_back(',');
  append_json_u64_property(out, "node_count", request.context.node_count);
  out.push_back(',');
  append_json_u64_property(out, "edge_count", request.context.edge_count);
  out += R"(},"text":)";
  append_json_string(out, context_text);
  out += R"(}])";
  out.push_back('}');

  return serialized;
}

} // namespace graphify
