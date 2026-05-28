#include "OpenCodeProtocol.hpp"

#include <cassert>
#include <string_view>

int main() {
  using namespace graphify;

  auto missing_session = serialize_session_message_request(OpenCodeSessionMessageRequest{
      .context = SynthesizedContext{.query = "query", .dense_summary = "summary"},
  });
  assert(!missing_session);
  assert(missing_session.error().code == OpenCodeErrorCode::missing_session_id);

  auto invalid_session = serialize_session_message_request(OpenCodeSessionMessageRequest{
      .session_id = "bad/session",
      .context = SynthesizedContext{.query = "query", .dense_summary = "summary"},
  });
  assert(!invalid_session);
  assert(invalid_session.error().code == OpenCodeErrorCode::invalid_session_id);

  auto request = serialize_session_message_request(OpenCodeSessionMessageRequest{
      .session_id = "sess_01",
      .message_id = "msg_01",
      .agent = "build",
      .system = "Use the GraphKV context only.",
      .model = OpenCodeModelRef{.provider_id = "anthropic", .model_id = "claude-sonnet-4"},
      .context = SynthesizedContext{
          .query = "Explain \"ingest\" latency",
          .dense_summary = "node A -> node B\nedge: calls",
          .tenant_id = 42,
          .source_bytes = 2048,
          .estimated_input_tokens = 15000,
          .node_count = 12,
          .edge_count = 19,
      },
  });
  assert(request);
  assert(request->method == "POST");
  assert(request->path == "/session/sess_01/message");
  assert(request->body.find(R"("messageID":"msg_01")") != std::string_view::npos);
  assert(request->body.find(R"("model":{"providerID":"anthropic","modelID":"claude-sonnet-4"})") != std::string_view::npos);
  assert(request->body.find(R"("format":{"type":"json_schema")") != std::string_view::npos);
  assert(request->body.find(R"("parts":[{"type":"text","synthetic":true)") != std::string_view::npos);
  assert(request->body.find(R"(Explain \"ingest\" latency)") != std::string_view::npos);

  return 0;
}
