#pragma once

#include "ScipIngestor.hpp"

#include <drogon/WebSocketController.h>

#include <cstddef>
#include <memory>
#include <span>
#include <string>
#include <thread>

namespace graphify {

class AgentWebSocket final : public drogon::WebSocketController<AgentWebSocket> {
 public:
  void handleNewMessage(const drogon::WebSocketConnectionPtr& connection,
                        std::string&& message,
                        const drogon::WebSocketMessageType& type) override {
    if (type != drogon::WebSocketMessageType::Binary) {
      connection->send(error_json("text messages are not accepted"));
      return;
    }
    if (message.empty()) {
      connection->send(error_json("empty payload"));
      return;
    }
    if (message.size() > GRAPHIFY_MAX_INGEST_BYTES) {
      connection->send(error_json("payload exceeds configured maximum"));
      return;
    }

    auto payload = std::make_shared<std::string>(std::move(message));
    std::thread([connection, payload = std::move(payload)]() {
      ScipIngestor ingestor(IngestOptions{.max_payload_bytes = GRAPHIFY_MAX_INGEST_BYTES,
                                          .mode = IngestMode::strict_wire_scanner});
      const auto bytes = std::as_bytes(std::span<const char>(payload->data(), payload->size()));
      auto result = ingestor.ingest(bytes);
      if (!result) {
        connection->send(error_json(result.error().message));
        return;
      }

      auto summary = result->summary;
      publisher().publish(std::move(result->snapshot));
      connection->send(summary_json(summary));
    }).detach();
  }

  void handleNewConnection(const drogon::HttpRequestPtr&, const drogon::WebSocketConnectionPtr&) override {}
  void handleConnectionClosed(const drogon::WebSocketConnectionPtr&) override {}

  WS_PATH_LIST_BEGIN
  WS_PATH_ADD("/v1/ingest_scip");
  WS_PATH_LIST_END

 private:
  static SnapshotPublisher& publisher() {
    static SnapshotPublisher instance;
    return instance;
  }

  static std::string error_json(std::string_view message) {
    std::string out = "{\"ok\":false,\"error\":\"";
    for (char character : message) {
      if (character == '\\' || character == '"') {
        out.push_back('\\');
      }
      out.push_back(character);
    }
    out += "\"}";
    return out;
  }

  static std::string summary_json(const IngestSummary& summary) {
    std::string out = "{\"ok\":true";
    out += ",\"mode\":\"";
    out += to_string(summary.mode);
    out += "\"";
    out += ",\"documents\":" + std::to_string(summary.counts.documents);
    out += ",\"symbols\":" + std::to_string(summary.counts.symbols);
    out += ",\"relationships\":" + std::to_string(summary.counts.relationships);
    out += ",\"occurrences\":" + std::to_string(summary.counts.occurrences);
    out += ",\"arena_bytes\":" + std::to_string(summary.arena_bytes);
    out += ",\"string_bytes\":" + std::to_string(summary.string_bytes_used);
    out += "}";
    return out;
  }
};

} // namespace graphify
