#include "AgentWebSocket.hpp"

#include <drogon/drogon.h>

#include <algorithm>
#include <cstdlib>
#include <cstdint>
#include <print>
#include <thread>

int main() {
  constexpr auto listen_address = "0.0.0.0";
  constexpr std::uint16_t default_listen_port = 8888;
  auto listen_port = default_listen_port;
  if (const char* port_env = std::getenv("GRAPHIFY_PORT")) {
    char* end = nullptr;
    const auto parsed = std::strtoul(port_env, &end, 10);
    if (*port_env != '\0' && end != port_env && *end == '\0' && parsed > 0 && parsed <= 65535) {
      listen_port = static_cast<std::uint16_t>(parsed);
    } else {
      std::println("ignoring invalid GRAPHIFY_PORT='{}'; using {}", port_env, default_listen_port);
    }
  }
  const auto thread_count = std::max(2U, std::thread::hardware_concurrency());

  std::println("graphify SCIP ingest server listening on {}:{} with {} Drogon threads",
               listen_address,
               listen_port,
               thread_count);

  drogon::app()
      .addListener(listen_address, listen_port)
      .setThreadNum(static_cast<size_t>(thread_count))
      .run();
  return 0;
}
