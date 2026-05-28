#pragma once

#include "GraphKV.hpp"
#include "GraphKVConfig.hpp"

#include <cstddef>
#include <cstdint>
#include <memory>
#include <span>
#include <string>

namespace graphify {

enum class IngestMode : std::uint8_t {
  strict_wire_scanner,
  generated_protobuf_compatibility,
};

enum class IngestErrorCode : std::uint8_t {
  empty_payload,
  payload_too_large,
  malformed_protobuf,
  count_overflow,
  arena_allocation_failed,
  unsupported_mode,
};

struct IngestError final {
  IngestErrorCode code{};
  std::string message{};
  std::size_t offset{};
};

struct IngestOptions final {
  std::size_t max_payload_bytes{graphkv_max_ingest_bytes};
  IngestMode mode{IngestMode::strict_wire_scanner};
};

struct IngestSummary final {
  GraphCounts counts{};
  std::size_t arena_bytes{};
  std::uint32_t string_bytes_used{};
  IngestMode mode{};
};

struct IngestResult final {
  std::unique_ptr<ArenaSnapshot> snapshot{};
  IngestSummary summary{};
};

[[nodiscard]] std::string_view to_string(IngestMode mode) noexcept;
[[nodiscard]] std::string_view to_string(IngestErrorCode code) noexcept;

class ScipIngestor final {
 public:
  explicit ScipIngestor(IngestOptions options = {});

  [[nodiscard]] const IngestOptions& options() const noexcept { return options_; }
  [[nodiscard]] Expected<IngestResult, IngestError> ingest(std::span<const std::byte> payload) const;

 private:
  IngestOptions options_{};
};

} // namespace graphify
