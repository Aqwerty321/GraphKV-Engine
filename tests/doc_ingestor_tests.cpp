#include "DocIngestor.hpp"

#include <cassert>
#include <span>
#include <string>
#include <string_view>

namespace {

[[nodiscard]] std::span<const std::byte> bytes(std::string_view value) noexcept {
  return std::as_bytes(std::span<const char>(value.data(), value.size()));
}

} // namespace

int main() {
  using namespace graphify;

  const std::string bundle =
      R"({"type":"doc_start","path":"repo/README.md","media_type":"text/markdown","language":"markdown","parser":"docling","title":"Readme","content_hash":"abc"})" "\n"
      R"({"type":"chunk","path":"repo/README.md","heading":"Install","chunk_type":"markdown","text":"Use src/main.cpp to start the C++ server.","line_start":3,"line_end":4})" "\n"
      R"({"type":"link","path":"repo/README.md","target":"src/main.cpp","link_type":"path_mention","line_start":3,"confidence":900})" "\n"
      R"({"type":"doc_end","path":"repo/README.md"})" "\n";

  DocIngestor ingestor(DocIngestOptions{.max_payload_bytes = 4096, .max_line_bytes = 2048, .max_preview_bytes = 128});
  auto result = ingestor.ingest(bytes(bundle));
  assert(result.has_value());
  assert(result->summary.counts.sources == 1);
  assert(result->summary.counts.chunks == 1);
  assert(result->summary.counts.links == 1);
  assert(result->summary.counts.token_estimate > 0);

  const auto& snapshot = *result->snapshot;
  assert(snapshot.resolve(snapshot.sources()[0].path) == std::string_view("repo/README.md"));
  assert(snapshot.resolve(snapshot.sources()[0].media_type) == std::string_view("text/markdown"));
  assert(snapshot.resolve(snapshot.sources()[0].parser) == std::string_view("docling"));
  assert(snapshot.sources()[0].first_chunk == 0);
  assert(snapshot.sources()[0].chunk_count == 1);
  assert(snapshot.resolve(snapshot.chunks()[0].heading) == std::string_view("Install"));
  assert(snapshot.resolve(snapshot.chunks()[0].chunk_type) == std::string_view("markdown"));
  assert(snapshot.resolve(snapshot.chunks()[0].text_preview).find("src/main.cpp") != std::string_view::npos);
  assert(snapshot.chunks()[0].first_link == 0);
  assert(snapshot.resolve(snapshot.links()[0].target) == std::string_view("src/main.cpp"));
  assert(snapshot.resolve(snapshot.links()[0].link_type) == std::string_view("path_mention"));
  assert(snapshot.links()[0].confidence == 900);

  auto empty = ingestor.ingest({});
  assert(!empty.has_value());
  assert(empty.error().code == DocIngestErrorCode::empty_payload);

  const std::string missing_text = R"({"type":"chunk","path":"repo/README.md"})";
  auto bad = ingestor.ingest(bytes(missing_text));
  assert(!bad.has_value());
  assert(bad.error().code == DocIngestErrorCode::missing_chunk_text);

  return 0;
}