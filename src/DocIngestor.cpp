#include "DocIngestor.hpp"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <memory>
#include <span>
#include <string>
#include <string_view>
#include <unordered_map>
#include <utility>
#include <vector>

#include <glaze/glaze.hpp>

namespace graphify::detail {

struct DocBundleEvent final {
  std::string_view type{};
  std::string_view path{};
  std::string_view media_type{};
  std::string_view language{};
  std::string_view parser{};
  std::string_view title{};
  std::string_view content_hash{};
  std::string_view heading{};
  std::string_view chunk_type{};
  std::string_view text{};
  std::string_view target{};
  std::string_view link_type{};
  std::uint32_t line_start{};
  std::uint32_t line_end{};
  std::uint32_t token_estimate{};
  std::uint32_t confidence{};
};

} // namespace graphify::detail

template <>
struct glz::meta<graphify::detail::DocBundleEvent> {
  using T = graphify::detail::DocBundleEvent;
  static constexpr auto value = object(
      "type",
      &T::type,
      "path",
      &T::path,
      "media_type",
      &T::media_type,
      "language",
      &T::language,
      "parser",
      &T::parser,
      "title",
      &T::title,
      "content_hash",
      &T::content_hash,
      "heading",
      &T::heading,
      "chunk_type",
      &T::chunk_type,
      "text",
      &T::text,
      "target",
      &T::target,
      "link_type",
      &T::link_type,
      "line_start",
      &T::line_start,
      "line_end",
      &T::line_end,
      "token_estimate",
      &T::token_estimate,
      "confidence",
      &T::confidence);
};

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

[[nodiscard]] DocIngestError make_error(DocIngestErrorCode code,
                                        std::uint32_t line,
                                        std::size_t offset,
                                        std::string message = {}) {
  return DocIngestError{.code = code, .line = line, .offset = offset, .message = std::move(message)};
}

[[nodiscard]] Expected<std::uint32_t, DocIngestError> next_index(std::size_t size,
                                                                 std::uint32_t line,
                                                                 std::size_t offset) {
  if (size >= std::numeric_limits<std::uint32_t>::max()) {
    return unexpected(make_error(DocIngestErrorCode::count_overflow, line, offset, "document index exceeds uint32_t"));
  }
  return static_cast<std::uint32_t>(size);
}

[[nodiscard]] std::uint32_t estimate_tokens(std::string_view text) noexcept {
  if (text.empty()) {
    return 0;
  }
  return std::max<std::uint32_t>(1U, static_cast<std::uint32_t>((text.size() + 3U) / 4U));
}

[[nodiscard]] Expected<void, DocIngestError> assign_string(DocIndexSnapshot& snapshot,
                                                          StringRef& target,
                                                          std::string_view value,
                                                          std::uint32_t line,
                                                          std::size_t offset) {
  if (value.empty()) {
    return {};
  }
  auto stored = snapshot.append_string(value, line, offset);
  if (!stored) {
    return unexpected(stored.error());
  }
  target = *stored;
  return {};
}

[[nodiscard]] Expected<std::uint32_t, DocIngestError> ensure_source(
    DocIndexSnapshot& snapshot,
    std::unordered_map<std::string, std::uint32_t>& source_by_path,
    std::vector<std::uint32_t>& last_chunk_by_source,
    std::string_view path,
    std::uint32_t line,
    std::size_t offset) {
  if (path.empty()) {
    return unexpected(make_error(DocIngestErrorCode::missing_path, line, offset, "document event is missing path"));
  }

  if (auto found = source_by_path.find(std::string(path)); found != source_by_path.end()) {
    return found->second;
  }

  auto index = next_index(snapshot.sources_.size(), line, offset);
  if (!index) {
    return unexpected(index.error());
  }

  DocSourceRecord source;
  auto path_ref = snapshot.append_string(path, line, offset);
  if (!path_ref) {
    return unexpected(path_ref.error());
  }
  source.path = *path_ref;
  auto parser_ref = snapshot.append_string("graphkv-doc-bundle-v1", line, offset);
  if (!parser_ref) {
    return unexpected(parser_ref.error());
  }
  source.parser = *parser_ref;

  snapshot.sources_.push_back(source);
  source_by_path.emplace(std::string(path), *index);
  last_chunk_by_source.push_back(external_document_index);
  return *index;
}

[[nodiscard]] Expected<void, DocIngestError> apply_doc_event(DocIndexSnapshot& snapshot,
                                                            const detail::DocBundleEvent& event,
                                                            std::unordered_map<std::string, std::uint32_t>& source_by_path,
                                                            std::vector<std::uint32_t>& last_chunk_by_source,
                                                            std::uint32_t line,
                                                            std::size_t offset) {
  auto source_index = ensure_source(snapshot, source_by_path, last_chunk_by_source, event.path, line, offset);
  if (!source_index) {
    return unexpected(source_index.error());
  }

  auto& source = snapshot.sources_[*source_index];
  if (auto assigned = assign_string(snapshot, source.media_type, event.media_type, line, offset); !assigned) {
    return unexpected(assigned.error());
  }
  if (auto assigned = assign_string(snapshot, source.language, event.language, line, offset); !assigned) {
    return unexpected(assigned.error());
  }
  if (auto assigned = assign_string(snapshot, source.parser, event.parser, line, offset); !assigned) {
    return unexpected(assigned.error());
  }
  if (auto assigned = assign_string(snapshot, source.title, event.title, line, offset); !assigned) {
    return unexpected(assigned.error());
  }
  if (auto assigned = assign_string(snapshot, source.content_hash, event.content_hash, line, offset); !assigned) {
    return unexpected(assigned.error());
  }
  return {};
}

[[nodiscard]] Expected<void, DocIngestError> apply_chunk_event(DocIndexSnapshot& snapshot,
                                                              const detail::DocBundleEvent& event,
                                                              const DocIngestOptions& options,
                                                              std::unordered_map<std::string, std::uint32_t>& source_by_path,
                                                              std::vector<std::uint32_t>& last_chunk_by_source,
                                                              std::uint32_t line,
                                                              std::size_t offset) {
  if (event.text.empty()) {
    return unexpected(make_error(DocIngestErrorCode::missing_chunk_text, line, offset, "chunk event is missing text"));
  }
  auto source_index = ensure_source(snapshot, source_by_path, last_chunk_by_source, event.path, line, offset);
  if (!source_index) {
    return unexpected(source_index.error());
  }
  auto chunk_index = next_index(snapshot.chunks_.size(), line, offset);
  if (!chunk_index) {
    return unexpected(chunk_index.error());
  }

  DocChunkRecord chunk;
  chunk.source_index = *source_index;
  const auto preview_size = std::min(event.text.size(), options.max_preview_bytes);
  auto preview = snapshot.append_string(event.text.substr(0, preview_size), line, offset);
  if (!preview) {
    return unexpected(preview.error());
  }
  chunk.text_preview = *preview;
  auto heading = snapshot.append_string(event.heading, line, offset);
  if (!heading) {
    return unexpected(heading.error());
  }
  chunk.heading = *heading;
  auto chunk_type = snapshot.append_string(event.chunk_type.empty() ? std::string_view("text") : event.chunk_type, line, offset);
  if (!chunk_type) {
    return unexpected(chunk_type.error());
  }
  chunk.chunk_type = *chunk_type;
  chunk.line_start = event.line_start;
  chunk.line_end = event.line_end != 0U ? event.line_end : event.line_start;
  chunk.token_estimate = event.token_estimate != 0U ? event.token_estimate : estimate_tokens(event.text);

  snapshot.chunks_.push_back(chunk);
  auto& source = snapshot.sources_[*source_index];
  if (source.chunk_count == 0U) {
    source.first_chunk = *chunk_index;
  }
  ++source.chunk_count;
  last_chunk_by_source[*source_index] = *chunk_index;
  return {};
}

[[nodiscard]] Expected<void, DocIngestError> apply_link_event(DocIndexSnapshot& snapshot,
                                                             const detail::DocBundleEvent& event,
                                                             std::unordered_map<std::string, std::uint32_t>& source_by_path,
                                                             std::vector<std::uint32_t>& last_chunk_by_source,
                                                             std::uint32_t line,
                                                             std::size_t offset) {
  if (event.target.empty()) {
    return unexpected(make_error(DocIngestErrorCode::missing_link_target, line, offset, "link event is missing target"));
  }
  auto source_index = ensure_source(snapshot, source_by_path, last_chunk_by_source, event.path, line, offset);
  if (!source_index) {
    return unexpected(source_index.error());
  }
  auto link_index = next_index(snapshot.links_.size(), line, offset);
  if (!link_index) {
    return unexpected(link_index.error());
  }

  DocCodeLinkEdge link;
  link.source_index = *source_index;
  link.chunk_index = last_chunk_by_source[*source_index];
  auto target = snapshot.append_string(event.target, line, offset);
  if (!target) {
    return unexpected(target.error());
  }
  link.target = *target;
  auto link_type = snapshot.append_string(event.link_type.empty() ? std::string_view("mention") : event.link_type, line, offset);
  if (!link_type) {
    return unexpected(link_type.error());
  }
  link.link_type = *link_type;
  link.line_start = event.line_start;
  link.confidence = static_cast<std::uint16_t>(std::min<std::uint32_t>(event.confidence != 0U ? event.confidence : 500U, 1000U));

  snapshot.links_.push_back(link);
  auto& source = snapshot.sources_[*source_index];
  if (source.link_count == 0U) {
    source.first_link = *link_index;
  }
  ++source.link_count;
  if (link.chunk_index != external_document_index && link.chunk_index < snapshot.chunks_.size()) {
    auto& chunk = snapshot.chunks_[link.chunk_index];
    if (chunk.link_count == 0U) {
      chunk.first_link = *link_index;
    }
    ++chunk.link_count;
  }
  return {};
}

[[nodiscard]] Expected<void, DocIngestError> apply_event(DocIndexSnapshot& snapshot,
                                                        const detail::DocBundleEvent& event,
                                                        const DocIngestOptions& options,
                                                        std::unordered_map<std::string, std::uint32_t>& source_by_path,
                                                        std::vector<std::uint32_t>& last_chunk_by_source,
                                                        std::uint32_t line,
                                                        std::size_t offset) {
  if (event.type == "doc_start" || event.type == "doc") {
    return apply_doc_event(snapshot, event, source_by_path, last_chunk_by_source, line, offset);
  }
  if (event.type == "chunk" || event.type == "section" || event.type == "table" || event.type == "code_block" ||
      event.type == "image_caption") {
    return apply_chunk_event(snapshot, event, options, source_by_path, last_chunk_by_source, line, offset);
  }
  if (event.type == "link") {
    return apply_link_event(snapshot, event, source_by_path, last_chunk_by_source, line, offset);
  }
  if (event.type == "doc_end") {
    return {};
  }
  return unexpected(make_error(DocIngestErrorCode::unknown_event_type, line, offset, "unsupported document event type"));
}

} // namespace

std::string_view to_string(DocIngestErrorCode code) noexcept {
  switch (code) {
    case DocIngestErrorCode::empty_payload:
      return "empty-payload";
    case DocIngestErrorCode::payload_too_large:
      return "payload-too-large";
    case DocIngestErrorCode::malformed_json:
      return "malformed-json";
    case DocIngestErrorCode::unknown_event_type:
      return "unknown-event-type";
    case DocIngestErrorCode::missing_path:
      return "missing-path";
    case DocIngestErrorCode::missing_chunk_text:
      return "missing-chunk-text";
    case DocIngestErrorCode::missing_link_target:
      return "missing-link-target";
    case DocIngestErrorCode::string_tape_overflow:
      return "string-tape-overflow";
    case DocIngestErrorCode::count_overflow:
      return "count-overflow";
    case DocIngestErrorCode::no_sources:
      return "no-sources";
  }
  return "unknown";
}

std::string_view DocIndexSnapshot::resolve(StringRef ref) const noexcept {
  if (ref.length == 0U) {
    return {};
  }
  if (ref.offset > string_tape_.size() || ref.length > string_tape_.size() - ref.offset) {
    return {};
  }
  return {string_tape_.data() + ref.offset, ref.length};
}

Expected<StringRef, DocIngestError> DocIndexSnapshot::append_string(std::string_view value,
                                                                    std::uint32_t line,
                                                                    std::size_t offset) {
  if (value.empty()) {
    return StringRef{};
  }
  if (value.size() > std::numeric_limits<std::uint32_t>::max() ||
      string_tape_.size() > std::numeric_limits<std::uint32_t>::max() - value.size()) {
    return unexpected(make_error(DocIngestErrorCode::string_tape_overflow, line, offset, "document string tape exceeds uint32_t"));
  }
  const auto string_offset = static_cast<std::uint32_t>(string_tape_.size());
  string_tape_.append(value);
  return StringRef{.offset = string_offset, .length = static_cast<std::uint32_t>(value.size())};
}

void DocIndexSnapshot::refresh_counts() noexcept {
  counts_.sources = static_cast<std::uint32_t>(sources_.size());
  counts_.chunks = static_cast<std::uint32_t>(chunks_.size());
  counts_.links = static_cast<std::uint32_t>(links_.size());
  counts_.string_bytes = static_cast<std::uint32_t>(string_tape_.size());
  counts_.token_estimate = 0;
  for (const auto& chunk : chunks_) {
    counts_.token_estimate += chunk.token_estimate;
  }
}

Expected<DocIngestResult, DocIngestError> DocIngestor::ingest(std::span<const std::byte> payload) const {
  if (payload.empty()) {
    return unexpected(make_error(DocIngestErrorCode::empty_payload, 0, 0));
  }
  if (payload.size() > options_.max_payload_bytes) {
    return unexpected(make_error(DocIngestErrorCode::payload_too_large, 0, 0));
  }

  const auto text = std::string_view(reinterpret_cast<const char*>(payload.data()), payload.size());
  auto snapshot = std::make_unique<DocIndexSnapshot>();
  std::unordered_map<std::string, std::uint32_t> source_by_path;
  std::vector<std::uint32_t> last_chunk_by_source;

  std::size_t offset = 0;
  std::uint32_t line_number = 1;
  while (offset < text.size()) {
    const auto newline = text.find('\n', offset);
    auto raw_end = newline == std::string_view::npos ? text.size() : newline;
    auto line_end = raw_end;
    if (line_end > offset && text[line_end - 1U] == '\r') {
      --line_end;
    }
    const auto line = text.substr(offset, line_end - offset);
    if (line.size() > options_.max_line_bytes) {
      return unexpected(make_error(DocIngestErrorCode::payload_too_large, line_number, offset, "document event line is too large"));
    }
    if (!line.empty()) {
      detail::DocBundleEvent event;
      auto parsed = glz::read<parse_options>(event, line);
      if (parsed) {
        return unexpected(make_error(DocIngestErrorCode::malformed_json, line_number, offset + parsed.count, "malformed document event"));
      }
      auto applied = apply_event(*snapshot, event, options_, source_by_path, last_chunk_by_source, line_number, offset);
      if (!applied) {
        return unexpected(applied.error());
      }
    }
    if (newline == std::string_view::npos) {
      break;
    }
    offset = newline + 1U;
    ++line_number;
  }

  if (snapshot->sources_.empty()) {
    return unexpected(make_error(DocIngestErrorCode::no_sources, line_number, offset, "document bundle contained no sources"));
  }

  snapshot->refresh_counts();
  auto summary = DocIngestSummary{
      .counts = snapshot->counts(),
      .source_bytes = payload.size(),
      .string_bytes_used = snapshot->string_tape_.size(),
  };
  return DocIngestResult{.summary = summary, .snapshot = std::move(snapshot)};
}

void DocSnapshotPublisher::publish(std::unique_ptr<DocIndexSnapshot> snapshot) {
  if (!snapshot) {
    return;
  }
  std::shared_ptr<const DocIndexSnapshot> shared(std::move(snapshot));
  std::scoped_lock lock(mutex_);
  latest_ = std::move(shared);
}

std::shared_ptr<const DocIndexSnapshot> DocSnapshotPublisher::acquire() const {
  std::scoped_lock lock(mutex_);
  return latest_;
}

} // namespace graphify