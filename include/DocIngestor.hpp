#pragma once

#include "ExpectedCompat.hpp"
#include "GraphKV.hpp"
#include "GraphKVConfig.hpp"

#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <span>
#include <string>
#include <string_view>
#include <type_traits>
#include <vector>

namespace graphify {

enum class DocIngestErrorCode : std::uint8_t {
  empty_payload,
  payload_too_large,
  malformed_json,
  unknown_event_type,
  missing_path,
  missing_chunk_text,
  missing_link_target,
  string_tape_overflow,
  count_overflow,
  no_sources,
};

struct DocIngestError final {
  DocIngestErrorCode code{};
  std::uint32_t line{};
  std::size_t offset{};
  std::string message{};
};

struct DocIngestOptions final {
  std::size_t max_payload_bytes{graphkv_max_ingest_bytes};
  std::size_t max_line_bytes{4U * 1024U * 1024U};
  std::size_t max_preview_bytes{4096U};
};

struct DocSourceRecord final {
  StringRef path{};
  StringRef media_type{};
  StringRef language{};
  StringRef parser{};
  StringRef title{};
  StringRef content_hash{};
  std::uint32_t first_chunk{external_document_index};
  std::uint32_t chunk_count{};
  std::uint32_t first_link{external_document_index};
  std::uint32_t link_count{};
};

struct DocChunkRecord final {
  std::uint32_t source_index{external_document_index};
  StringRef heading{};
  StringRef chunk_type{};
  StringRef text_preview{};
  std::uint32_t line_start{};
  std::uint32_t line_end{};
  std::uint32_t token_estimate{};
  std::uint32_t first_link{external_document_index};
  std::uint32_t link_count{};
};

struct DocCodeLinkEdge final {
  std::uint32_t source_index{external_document_index};
  std::uint32_t chunk_index{external_document_index};
  StringRef target{};
  StringRef link_type{};
  std::uint32_t line_start{};
  std::uint16_t confidence{};
  std::uint16_t reserved{};
};

static_assert(std::is_trivially_copyable_v<DocSourceRecord>);
static_assert(std::is_trivially_copyable_v<DocChunkRecord>);
static_assert(std::is_trivially_copyable_v<DocCodeLinkEdge>);

struct DocIndexCounts final {
  std::uint32_t sources{};
  std::uint32_t chunks{};
  std::uint32_t links{};
  std::uint32_t string_bytes{};
  std::uint64_t token_estimate{};
};

class DocIndexSnapshot final {
 public:
  [[nodiscard]] const DocIndexCounts& counts() const noexcept { return counts_; }
  [[nodiscard]] std::span<const DocSourceRecord> sources() const noexcept { return sources_; }
  [[nodiscard]] std::span<const DocChunkRecord> chunks() const noexcept { return chunks_; }
  [[nodiscard]] std::span<const DocCodeLinkEdge> links() const noexcept { return links_; }
  [[nodiscard]] std::span<const char> string_tape() const noexcept { return string_tape_; }

  [[nodiscard]] std::string_view resolve(StringRef ref) const noexcept;
  [[nodiscard]] Expected<StringRef, DocIngestError> append_string(std::string_view value,
                                                                  std::uint32_t line,
                                                                  std::size_t offset);
  void refresh_counts() noexcept;

  friend class DocIngestor;

  std::vector<DocSourceRecord> sources_{};
  std::vector<DocChunkRecord> chunks_{};
  std::vector<DocCodeLinkEdge> links_{};
  std::string string_tape_{};
  DocIndexCounts counts_{};
};

struct DocIngestSummary final {
  DocIndexCounts counts{};
  std::size_t source_bytes{};
  std::size_t string_bytes_used{};
};

struct DocIngestResult final {
  DocIngestSummary summary{};
  std::unique_ptr<DocIndexSnapshot> snapshot{};
};

class DocIngestor final {
 public:
  explicit DocIngestor(DocIngestOptions options = {}) : options_(options) {}

  [[nodiscard]] const DocIngestOptions& options() const noexcept { return options_; }
  [[nodiscard]] Expected<DocIngestResult, DocIngestError> ingest(std::span<const std::byte> payload) const;

 private:
  DocIngestOptions options_{};
};

class DocSnapshotPublisher final {
 public:
  void publish(std::unique_ptr<DocIndexSnapshot> snapshot);
  [[nodiscard]] std::shared_ptr<const DocIndexSnapshot> acquire() const;

 private:
  mutable std::mutex mutex_{};
  std::shared_ptr<const DocIndexSnapshot> latest_{};
};

[[nodiscard]] std::string_view to_string(DocIngestErrorCode code) noexcept;

} // namespace graphify