#include "RetrievalIndex.hpp"

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
      R"({"type":"doc_start","path":"repo/README.md","media_type":"text/markdown","language":"markdown","parser":"docling","title":"Readme"})" "\n"
      R"({"type":"chunk","path":"repo/README.md","heading":"Install","chunk_type":"markdown","text":"Use src/main.cpp to start the C++ Drogon WebSocket server.","line_start":3,"line_end":4})" "\n"
      R"({"type":"link","path":"repo/README.md","target":"src/main.cpp","link_type":"path_mention","line_start":3,"confidence":900})" "\n"
      R"({"type":"doc_start","path":"repo/docs/python.md","media_type":"text/markdown","language":"markdown","parser":"docling","title":"Python"})" "\n"
      R"({"type":"chunk","path":"repo/docs/python.md","heading":"Scanner","chunk_type":"markdown","text":"The Python SCIP scanner emits symbols for browser-use and vllm repositories.","line_start":5,"line_end":7})" "\n";

  DocIngestor ingestor(DocIngestOptions{.max_payload_bytes = 4096, .max_line_bytes = 2048, .max_preview_bytes = 512});
  auto docs = ingestor.ingest(bytes(bundle));
  assert(docs.has_value());
  auto index = DocRetrievalIndex::build(*docs->snapshot, RetrievalOptions{.dense_dimensions = 32, .dense_candidate_limit = 8, .sparse_candidate_limit = 8});
  assert(index.has_value());
  assert(index->term_count() > 0);
  assert(index->posting_count() > 0);

  auto sparse = index->sparse_search(*docs->snapshot, "Drogon WebSocket src/main.cpp", 4);
  assert(!sparse.empty());
  assert(sparse.front().chunk_index == 0);

  auto dense = index->dense_search("browser-use Python SCIP symbols", 4);
  assert(!dense.empty());

  auto fused = index->rrf_search(*docs->snapshot, nullptr, "start C++ server", 4);
  assert(!fused.empty());
  assert((fused.front().source_mask & 0x3U) != 0U);

  auto graph = GraphArena::allocate(GraphCounts{.documents = 1, .string_bytes = 32});
  assert(graph.has_value());
  auto path = (*graph)->append_string("src/main.cpp");
  assert(path.has_value());
  (*graph)->documents()[0].relative_path = *path;

  auto graph_fused = index->rrf_search(*docs->snapshot, graph->get(), "start C++ server", 4);
  assert(!graph_fused.empty());
  assert(graph_fused.front().graph_overlap);
  assert((graph_fused.front().source_mask & (1U << 2U)) != 0U);

  return 0;
}