#pragma once

#include "IngressGate.hpp"

#include <drogon/WebSocketController.h>

#include <string>

namespace graphify {

class IngestController final : public drogon::WebSocketController<IngestController> {
 public:
  void handleNewMessage(const drogon::WebSocketConnectionPtr& connection,
                        std::string&& message,
                        const drogon::WebSocketMessageType& type) override;

  void handleNewConnection(const drogon::HttpRequestPtr& request,
                           const drogon::WebSocketConnectionPtr& connection) override;

  void handleConnectionClosed(const drogon::WebSocketConnectionPtr& connection) override;

  WS_PATH_LIST_BEGIN
  WS_PATH_ADD("/v1/ingest_scip");
  WS_PATH_ADD("/v1/ingest_docs");
  WS_PATH_ADD("/v1/ingest");
  WS_PATH_LIST_END
};

} // namespace graphify
