#include "IngestController.hpp"

#include "DocIngestor.hpp"
#include "OpenCodeProtocol.hpp"
#include "RetrievalIndex.hpp"
#include "ScipIngestor.hpp"
#include "TelemetryRingBuffer.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <limits>
#include <memory>
#include <mutex>
#include <span>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

#include <gsl/gsl>
#include <zstd.h>

namespace graphify {
namespace {

struct WebSocketSession final {
  IngressSession ingress{};
  std::string path{};
  std::uint64_t next_query_id{};
};

struct BinaryWorkItem final {
  drogon::WebSocketConnectionPtr connection{};
  RouteKind route_kind{RouteKind::unknown};
  std::string compressed_payload{};
  std::uint64_t query_id{};
  std::uint64_t received_ns{};
  std::uint64_t tenant_id{};
};

struct DecompressedPayload final {
  std::unique_ptr<char[]> bytes{};
  std::size_t size{};
};

struct RankedDocument final {
  std::uint32_t index{};
  std::uint32_t score{};
};

struct RankedDocChunk final {
  std::uint32_t index{};
  float score{};
  std::uint16_t source_mask{};
  bool graph_overlap{};
};

struct DocEvidenceState final {
  std::shared_ptr<const DocIndexSnapshot> snapshot{};
  std::shared_ptr<const DocRetrievalIndex> retrieval{};
};

class DocEvidencePublisher final {
 public:
  void publish(std::unique_ptr<DocIndexSnapshot> snapshot, DocRetrievalIndex index) {
    auto published = std::make_shared<DocEvidenceState>();
    published->snapshot = std::shared_ptr<const DocIndexSnapshot>(std::move(snapshot));
    published->retrieval = std::make_shared<DocRetrievalIndex>(std::move(index));
    std::lock_guard lock(mutex_);
    latest_ = std::move(published);
  }

  [[nodiscard]] std::shared_ptr<const DocEvidenceState> acquire() const {
    std::lock_guard lock(mutex_);
    return latest_;
  }

 private:
  mutable std::mutex mutex_{};
  std::shared_ptr<const DocEvidenceState> latest_{};
};

inline constexpr std::size_t strict_vram_payload_limit_bytes = 250ULL * 1024ULL * 1024ULL;

[[nodiscard]] std::uint64_t steady_timestamp_ns() noexcept {
  const auto now = std::chrono::steady_clock::now().time_since_epoch();
  return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(now).count());
}

[[nodiscard]] std::uint64_t elapsed_us_since(std::uint64_t start_ns) noexcept {
  const auto now = steady_timestamp_ns();
  if (now <= start_ns) {
    return 0;
  }
  return (now - start_ns) / 1000U;
}

TelemetryRingBuffer<IngestMetric, 1024>& local_metrics() {
  thread_local TelemetryRingBuffer<IngestMetric, 1024> metrics;
  return metrics;
}

void record_metric(IngestMetric metric) noexcept {
  static_cast<void>(local_metrics().try_push(metric));
}

void record_binary_metric(const BinaryWorkItem& item,
                          std::uint64_t compressed_size,
                          std::uint64_t uncompressed_size,
                          BoundaryErrorCode error_code) noexcept {
  record_metric(IngestMetric{
      .timestamp_ns = steady_timestamp_ns(),
      .query_id = item.query_id,
      .tenant_id = item.tenant_id,
      .payload_size = compressed_size,
      .compressed_payload_size = compressed_size,
      .uncompressed_payload_size = uncompressed_size,
      .latency_us = elapsed_us_since(item.received_ns),
      .route_kind = static_cast<std::uint16_t>(item.route_kind),
      .error_code = static_cast<std::uint16_t>(error_code),
  });
}

[[nodiscard]] FrameKind frame_kind_from_drogon(drogon::WebSocketMessageType type) noexcept {
  switch (type) {
    case drogon::WebSocketMessageType::Text:
      return FrameKind::text;
    case drogon::WebSocketMessageType::Binary:
      return FrameKind::binary;
    default:
      return FrameKind::other;
  }
}

[[nodiscard]] GateLimits limits_for_session(const WebSocketSession& session) noexcept {
  auto limits = GateLimits{};
  if (session.path == "/v1/ingest_docs") {
    limits.binary_route = RouteKind::binary_docs;
  }
  return limits;
}

[[nodiscard]] std::string error_json(BoundaryErrorCode code) {
  std::string out = "{\"ok\":false,\"error\":\"";
  out += to_string(code);
  out += "\"}";
  return out;
}

[[nodiscard]] std::string compression_error_json(BoundaryErrorCode code,
                                                 std::size_t compressed_size,
                                                 std::uint64_t declared_uncompressed_size = 0) {
  std::string out = "{\"ok\":false,\"error\":\"";
  out += to_string(code);
  out += "\",\"compressed_bytes\":";
  out += std::to_string(compressed_size);
  out += ",\"declared_uncompressed_bytes\":";
  out += std::to_string(declared_uncompressed_size);
  out += "}";
  return out;
}

[[nodiscard]] std::string scanner_error_json(const IngestError& error) {
  std::string out = "{\"ok\":false,\"error\":\"";
  out += to_string(BoundaryErrorCode::scanner_error);
  out += "\",\"scanner_code\":\"";
  out += to_string(error.code);
  out += "\",\"offset\":";
  out += std::to_string(error.offset);
  out += ",\"message\":";
  append_json_string(out, error.message);
  out += "}";
  return out;
}

[[nodiscard]] std::string doc_ingest_error_json(const DocIngestError& error) {
  std::string out = "{\"ok\":false,\"error\":\"doc-ingest-error\",\"doc_code\":\"";
  out += to_string(error.code);
  out += "\",\"line\":";
  out += std::to_string(error.line);
  out += ",\"offset\":";
  out += std::to_string(error.offset);
  out += ",\"message\":";
  append_json_string(out, error.message);
  out += "}";
  return out;
}

[[nodiscard]] std::string retrieval_error_json(const RetrievalError& error) {
  std::string out = "{\"ok\":false,\"error\":\"doc-retrieval-error\",\"retrieval_code\":\"";
  out += to_string(error.code);
  out += "\",\"message\":";
  append_json_string(out, error.message);
  out += "}";
  return out;
}

[[nodiscard]] std::string control_json(const IngressDecision& decision) {
  std::string out = "{\"ok\":true,\"route\":\"";
  out += to_string(decision.route_kind);
  out += "\",\"tenant_id\":";
  out += std::to_string(decision.tenant_id);
  out += "}";
  return out;
}

SnapshotPublisher& publisher();
DocEvidencePublisher& doc_evidence_publisher();
[[nodiscard]] std::uint32_t query_score(std::string_view haystack, std::string_view query) noexcept;
void append_ranked_document(std::string& out, const ArenaSnapshot& snapshot, RankedDocument ranked);
void append_ranked_doc_chunk(std::string& out, const DocIndexSnapshot& snapshot, RankedDocChunk ranked);
void append_document_evidence(std::string& out, std::string_view query);

[[nodiscard]] std::string synthesized_context_summary(const IngressDecision& decision) {
  std::string out;
  out.reserve(decision.control.query.size() + 2048U);
  out += "GraphKV context synthesis v1\n";
  out += "role: high-speed-context-synthesizer\n";
  out += "contract: OpenCode receives bounded graph evidence, not raw repository bulk.\n";
  out += "requested_query: ";
  out += decision.control.query;
  out += "\n";

  const auto snapshot = publisher().acquire();
  if (!snapshot) {
    out += "snapshot: unavailable\n";
    out += "graph_next_stage: ingest a SCIP graph before expecting symbol-ranked context.\n";
    append_document_evidence(out, decision.control.query);
    return out;
  }

  const auto& counts = snapshot->counts();
  out += "snapshot: available\n";
  out += "documents: ";
  out += std::to_string(counts.documents);
  out += "\nsymbols: ";
  out += std::to_string(counts.symbols);
  out += "\nrelationships: ";
  out += std::to_string(counts.relationships);
  out += "\noccurrences: ";
  out += std::to_string(counts.occurrences);
  out += "\narena_bytes: ";
  out += std::to_string(snapshot->arena_bytes());
  out += "\nstring_bytes: ";
  out += std::to_string(snapshot->string_bytes_used());
  out += "\n";

  std::array<RankedDocument, 8> top_documents{};
  const auto documents = snapshot->documents();
  for (std::uint32_t index = 0; index < documents.size(); ++index) {
    const auto& document = documents[index];
    const auto path = snapshot->resolve(document.relative_path);
    const auto language = snapshot->resolve(document.language);
    const auto relevance = query_score(path, decision.control.query) +
                 query_score(language, decision.control.query);
    const auto density = std::min<std::uint32_t>(250U,
                          (document.symbol_count / 16U) +
                            (document.occurrence_count / 128U));
    const auto score = (relevance * 1000U) + density;
    if (score == 0) {
      continue;
    }
    auto candidate = RankedDocument{.index = index, .score = score};
    for (auto& ranked : top_documents) {
      if (candidate.score <= ranked.score) {
        continue;
      }
      std::swap(candidate, ranked);
    }
  }

  out += "top_documents:\n";
  bool emitted = false;
  for (const auto ranked : top_documents) {
    if (ranked.score == 0) {
      continue;
    }
    append_ranked_document(out, *snapshot, ranked);
    emitted = true;
  }
  if (!emitted) {
    out += "- none ranked above zero; query did not overlap document paths or languages.\n";
  }
  append_document_evidence(out, decision.control.query);
  out += "next_stage: replace this CPU ranking with GPU-paged subgraph expansion and citation packing.\n";
  return out;
}

[[nodiscard]] std::string opencode_request_json(const SerializedOpenCodeRequest& request) {
  std::string out;
  out.reserve(request.body.size() + request.path.size() + 96U);
  out += "{\"ok\":true,\"route\":\"opencode-synthesis\",\"opencode\":{\"method\":";
  append_json_string(out, request.method);
  out += ",\"path\":";
  append_json_string(out, request.path);
  out += ",\"body\":";
  out += request.body;
  out += "}}";
  return out;
}

[[nodiscard]] std::string summary_json(const IngestSummary& summary,
                                       std::size_t compressed_size,
                                       std::size_t uncompressed_size) {
  std::string out = "{\"ok\":true";
  out += ",\"mode\":\"";
  out += to_string(summary.mode);
  out += "\"";
  out += ",\"compressed_bytes\":" + std::to_string(compressed_size);
  out += ",\"uncompressed_bytes\":" + std::to_string(uncompressed_size);
  out += ",\"documents\":" + std::to_string(summary.counts.documents);
  out += ",\"symbols\":" + std::to_string(summary.counts.symbols);
  out += ",\"relationships\":" + std::to_string(summary.counts.relationships);
  out += ",\"occurrences\":" + std::to_string(summary.counts.occurrences);
  out += ",\"arena_bytes\":" + std::to_string(summary.arena_bytes);
  out += ",\"string_bytes\":" + std::to_string(summary.string_bytes_used);
  out += "}";
  return out;
}

[[nodiscard]] std::string summary_json(const DocIngestSummary& summary,
                                       std::size_t compressed_size,
                                       std::size_t uncompressed_size) {
  std::string out = "{\"ok\":true";
  out += ",\"mode\":\"doc-bundle-v1\"";
  out += ",\"compressed_bytes\":" + std::to_string(compressed_size);
  out += ",\"uncompressed_bytes\":" + std::to_string(uncompressed_size);
  out += ",\"sources\":" + std::to_string(summary.counts.sources);
  out += ",\"chunks\":" + std::to_string(summary.counts.chunks);
  out += ",\"links\":" + std::to_string(summary.counts.links);
  out += ",\"token_estimate\":" + std::to_string(summary.counts.token_estimate);
  out += ",\"string_bytes\":" + std::to_string(summary.string_bytes_used);
  out += "}";
  return out;
}

SnapshotPublisher& publisher() {
  static SnapshotPublisher instance;
  return instance;
}

DocEvidencePublisher& doc_evidence_publisher() {
  static DocEvidencePublisher instance;
  return instance;
}

[[nodiscard]] std::uint32_t query_score(std::string_view haystack, std::string_view query) noexcept {
  if (haystack.empty() || query.empty()) {
    return 0;
  }
  std::uint32_t score = 0;
  std::size_t token_start = 0;
  while (token_start < query.size()) {
    while (token_start < query.size() && query[token_start] == ' ') {
      ++token_start;
    }
    auto token_end = token_start;
    while (token_end < query.size() && query[token_end] != ' ') {
      ++token_end;
    }
    const auto token = query.substr(token_start, token_end - token_start);
    if (token.size() >= 2 && haystack.find(token) != std::string_view::npos) {
      score += static_cast<std::uint32_t>(token.size());
    }
    token_start = token_end + 1U;
  }
  return score;
}

void append_ranked_document(std::string& out, const ArenaSnapshot& snapshot, RankedDocument ranked) {
  const auto documents = snapshot.documents();
  if (ranked.index >= documents.size()) {
    return;
  }
  const auto& document = documents[ranked.index];
  out += "- path: ";
  out += snapshot.resolve(document.relative_path);
  out += " | language: ";
  out += snapshot.resolve(document.language);
  out += " | symbols: ";
  out += std::to_string(document.symbol_count);
  out += " | occurrences: ";
  out += std::to_string(document.occurrence_count);
  out += " | score: ";
  out += std::to_string(ranked.score);
  out += "\n";
}

void append_one_line_preview(std::string& out, std::string_view value, std::size_t limit) {
  std::size_t emitted = 0;
  for (char character : value) {
    if (emitted >= limit) {
      out += "...";
      return;
    }
    out.push_back(character == '\n' || character == '\r' || character == '\t' ? ' ' : character);
    ++emitted;
  }
}

void append_ranked_doc_chunk(std::string& out, const DocIndexSnapshot& snapshot, RankedDocChunk ranked) {
  const auto chunks = snapshot.chunks();
  if (ranked.index >= chunks.size()) {
    return;
  }
  const auto& chunk = chunks[ranked.index];
  const auto sources = snapshot.sources();
  if (chunk.source_index >= sources.size()) {
    return;
  }
  const auto& source = sources[chunk.source_index];
  out += "- path: ";
  out += snapshot.resolve(source.path);
  out += " | heading: ";
  out += snapshot.resolve(chunk.heading);
  out += " | type: ";
  out += snapshot.resolve(chunk.chunk_type);
  out += " | lines: ";
  out += std::to_string(chunk.line_start);
  out += "-";
  out += std::to_string(chunk.line_end);
  out += " | tokens: ";
  out += std::to_string(chunk.token_estimate);
  out += " | score: ";
  out += std::to_string(ranked.score);
  if (ranked.source_mask != 0U) {
    out += " | retrieval:";
    if ((ranked.source_mask & (1U << 0U)) != 0U) {
      out += " dense";
    }
    if ((ranked.source_mask & (1U << 1U)) != 0U) {
      out += " sparse";
    }
    if ((ranked.source_mask & (1U << 2U)) != 0U) {
      out += " graph";
    }
  }
  if (ranked.graph_overlap) {
    out += " | graph_overlap: yes";
  }
  out += "\n  preview: ";
  append_one_line_preview(out, snapshot.resolve(chunk.text_preview), 240U);
  out += "\n";

  if (chunk.first_link == external_document_index || chunk.link_count == 0U) {
    return;
  }
  const auto links = snapshot.links();
  out += "  links:";
  const auto end = std::min<std::uint32_t>(chunk.first_link + chunk.link_count, static_cast<std::uint32_t>(links.size()));
  for (auto index = chunk.first_link; index < end; ++index) {
    out += " ";
    out += snapshot.resolve(links[index].target);
  }
  out += "\n";
}

void append_document_evidence(std::string& out, std::string_view query) {
  const auto evidence = doc_evidence_publisher().acquire();
  if (!evidence || !evidence->snapshot) {
    out += "doc_snapshot: unavailable\n";
    out += "doc_next_stage: ingest a normalized doc bundle at /v1/ingest_docs for long-document evidence.\n";
    return;
  }

  const auto& snapshot = *evidence->snapshot;
  const auto& counts = snapshot.counts();
  out += "doc_snapshot: available\n";
  out += "doc_sources: ";
  out += std::to_string(counts.sources);
  out += "\ndoc_chunks: ";
  out += std::to_string(counts.chunks);
  out += "\ndoc_links: ";
  out += std::to_string(counts.links);
  out += "\ndoc_token_estimate: ";
  out += std::to_string(counts.token_estimate);
  const auto retrieval = evidence->retrieval;
  out += "\ndoc_retrieval_backend: ";
  out += retrieval ? retrieval->dense_backend_name() : std::string_view("unavailable");
  out += "\ntop_doc_chunks:\n";

  const auto chunks = snapshot.chunks();
  if (retrieval) {
    const auto graph = publisher().acquire();
    const auto hits = retrieval->rrf_search(snapshot, graph.get(), query, 8U);
    bool emitted = false;
    for (const auto& hit : hits) {
      append_ranked_doc_chunk(out,
                              snapshot,
                              RankedDocChunk{.index = hit.chunk_index,
                                             .score = hit.score,
                                             .source_mask = hit.source_mask,
                                             .graph_overlap = hit.graph_overlap});
      emitted = true;
    }
    if (emitted) {
      return;
    }
  }

  if (!chunks.empty()) {
    append_ranked_doc_chunk(out, snapshot, RankedDocChunk{.index = 0, .score = 1.0F});
  } else {
    out += "- no document chunks available.\n";
  }
}

[[nodiscard]] Expected<DecompressedPayload, BoundaryErrorCode> decompress_zstd_payload(
    std::string_view compressed_payload,
    std::size_t max_payload_bytes) {
  const auto content_size = ZSTD_getFrameContentSize(compressed_payload.data(), compressed_payload.size());
  if (content_size == ZSTD_CONTENTSIZE_ERROR) {
    return unexpected(BoundaryErrorCode::zstd_decompression_failed);
  }
  if (content_size == ZSTD_CONTENTSIZE_UNKNOWN) {
    return unexpected(BoundaryErrorCode::compressed_content_size_unknown);
  }

  const auto max_uncompressed_size = std::min(strict_vram_payload_limit_bytes, max_payload_bytes);
  if (content_size > static_cast<unsigned long long>(max_uncompressed_size) ||
      content_size > static_cast<unsigned long long>(std::numeric_limits<std::size_t>::max())) {
    return unexpected(BoundaryErrorCode::decompressed_payload_too_large);
  }

  const auto uncompressed_size = static_cast<std::size_t>(content_size);
  auto decompressed = std::make_unique_for_overwrite<char[]>(uncompressed_size);
  const auto actual_size = ZSTD_decompress(decompressed.get(),
                                           uncompressed_size,
                                           compressed_payload.data(),
                                           compressed_payload.size());
  if (ZSTD_isError(actual_size) || actual_size != uncompressed_size) {
    return unexpected(BoundaryErrorCode::zstd_decompression_failed);
  }

  Expects(uncompressed_size <= strict_vram_payload_limit_bytes);
  Expects(uncompressed_size <= max_payload_bytes);
  Expects(decompressed.get() != nullptr || uncompressed_size == 0U);
  return DecompressedPayload{.bytes = std::move(decompressed), .size = uncompressed_size};
}

class BoundedBinaryDispatcher final {
 public:
  BoundedBinaryDispatcher() {
    const auto hardware_threads = std::max(1U, std::thread::hardware_concurrency());
    const auto worker_count = std::clamp(hardware_threads / 2U, 1U, 4U);
    shards_.reserve(worker_count);
    for (auto index = 0U; index < worker_count; ++index) {
      auto shard = std::make_unique<WorkerShard>();
      auto* raw_shard = shard.get();
      raw_shard->worker = std::jthread([this, raw_shard](std::stop_token token) { worker_loop(*raw_shard, token); });
      shards_.push_back(std::move(shard));
    }
  }

  BoundedBinaryDispatcher(const BoundedBinaryDispatcher&) = delete;
  BoundedBinaryDispatcher& operator=(const BoundedBinaryDispatcher&) = delete;

  ~BoundedBinaryDispatcher() {
    for (auto& shard : shards_) {
      shard->worker.request_stop();
      shard->queue_changed.notify_all();
    }
  }

  [[nodiscard]] bool try_push(BinaryWorkItem item) {
    if (shards_.empty()) {
      return false;
    }

    const auto start = next_shard_.fetch_add(1, std::memory_order_relaxed);
    for (std::size_t offset = 0; offset < shards_.size(); ++offset) {
      auto& shard = *shards_[(start + offset) % shards_.size()];
      {
        std::scoped_lock lock(shard.queue_mutex);
        if (shard.queue.size() >= queue_capacity_per_worker) {
          continue;
        }
        shard.queue.push_back(std::move(item));
      }
      shard.queue_changed.notify_one();
      return true;
    }
    return false;
  }

 private:
  static constexpr std::size_t queue_capacity_per_worker = 64;

  struct WorkerShard final {
    std::mutex queue_mutex{};
    std::condition_variable_any queue_changed{};
    std::deque<BinaryWorkItem> queue{};
    std::jthread worker{};
  };

  void worker_loop(WorkerShard& shard, std::stop_token token) {
    ScipIngestor ingestor(IngestOptions{.max_payload_bytes = graphkv_max_ingest_bytes,
                                        .mode = IngestMode::strict_wire_scanner});
    DocIngestor doc_ingestor(DocIngestOptions{.max_payload_bytes = graphkv_max_ingest_bytes});
    while (!token.stop_requested()) {
      BinaryWorkItem item;
      {
        std::unique_lock lock(shard.queue_mutex);
        const auto has_work = shard.queue_changed.wait(lock, token, [&shard] { return !shard.queue.empty(); });
        if (!has_work || shard.queue.empty()) {
          continue;
        }
        item = std::move(shard.queue.front());
        shard.queue.pop_front();
      }
      process(std::move(item), ingestor, doc_ingestor);
    }
  }

  static void process(BinaryWorkItem item, const ScipIngestor& ingestor, const DocIngestor& doc_ingestor) {
    const auto compressed_size = item.compressed_payload.size();
    const auto declared_content_size = ZSTD_getFrameContentSize(item.compressed_payload.data(), item.compressed_payload.size());
    const auto declared_uncompressed_size =
        declared_content_size != ZSTD_CONTENTSIZE_ERROR && declared_content_size != ZSTD_CONTENTSIZE_UNKNOWN
            ? static_cast<std::uint64_t>(declared_content_size)
            : 0U;
    const auto max_uncompressed_bytes = item.route_kind == RouteKind::binary_docs ? doc_ingestor.options().max_payload_bytes
                                                                                  : ingestor.options().max_payload_bytes;
    auto decompressed = decompress_zstd_payload(item.compressed_payload, max_uncompressed_bytes);
    if (!decompressed) {
      record_binary_metric(item, compressed_size, 0, decompressed.error());
      item.connection->send(compression_error_json(decompressed.error(), compressed_size, declared_uncompressed_size));
      return;
    }

    const auto bytes = std::as_bytes(std::span<const char>(decompressed->bytes.get(), decompressed->size));
    if (item.route_kind == RouteKind::binary_docs) {
      auto result = doc_ingestor.ingest(bytes);
      if (!result) {
        record_binary_metric(item, compressed_size, decompressed->size, BoundaryErrorCode::scanner_error);
        item.connection->send(doc_ingest_error_json(result.error()));
        return;
      }

      auto summary = result->summary;
      auto retrieval = DocRetrievalIndex::build(*result->snapshot);
      if (!retrieval) {
        record_binary_metric(item, compressed_size, decompressed->size, BoundaryErrorCode::scanner_error);
        item.connection->send(retrieval_error_json(retrieval.error()));
        return;
      }
      doc_evidence_publisher().publish(std::move(result->snapshot), std::move(*retrieval));
      record_binary_metric(item, compressed_size, decompressed->size, BoundaryErrorCode::none);
      item.connection->send(summary_json(summary, compressed_size, decompressed->size));
      return;
    }

    auto result = ingestor.ingest(bytes);
    if (!result) {
      record_binary_metric(item, compressed_size, decompressed->size, BoundaryErrorCode::scanner_error);
      item.connection->send(scanner_error_json(result.error()));
      return;
    }

    auto summary = result->summary;
    publisher().publish(std::move(result->snapshot));
    record_binary_metric(item, compressed_size, decompressed->size, BoundaryErrorCode::none);
    item.connection->send(summary_json(summary, compressed_size, decompressed->size));
  }

  std::atomic<std::size_t> next_shard_{};
  std::vector<std::unique_ptr<WorkerShard>> shards_{};
};

BoundedBinaryDispatcher& dispatcher() {
  static BoundedBinaryDispatcher instance;
  return instance;
}

} // namespace

void IngestController::handleNewConnection(const drogon::HttpRequestPtr& request,
                                           const drogon::WebSocketConnectionPtr& connection) {
  auto session = std::make_shared<WebSocketSession>();
  if (request) {
    session->path = request->path();
  }
  connection->setContext(std::move(session));
}

void IngestController::handleConnectionClosed(const drogon::WebSocketConnectionPtr& connection) {
  connection->clearContext();
}

void IngestController::handleNewMessage(const drogon::WebSocketConnectionPtr& connection,
                                        std::string&& message,
                                        const drogon::WebSocketMessageType& type) {
  auto session = connection->getContext<WebSocketSession>();
  if (!session) {
    session = std::make_shared<WebSocketSession>();
    connection->setContext(session);
  }

  if (type == drogon::WebSocketMessageType::Ping || type == drogon::WebSocketMessageType::Pong ||
      type == drogon::WebSocketMessageType::Close) {
    return;
  }

  const auto query_id = ++session->next_query_id;
  const auto received_ns = steady_timestamp_ns();
  const auto bytes = std::as_bytes(std::span<const char>(message.data(), message.size()));
  auto decision = classify_frame(frame_kind_from_drogon(type), bytes, session->ingress, limits_for_session(*session));

  if (!decision) {
    record_metric(IngestMetric{
        .timestamp_ns = steady_timestamp_ns(),
        .query_id = query_id,
        .tenant_id = session->ingress.tenant_id,
        .payload_size = message.size(),
        .compressed_payload_size = type == drogon::WebSocketMessageType::Binary ? message.size() : 0U,
        .uncompressed_payload_size = 0,
        .latency_us = elapsed_us_since(received_ns),
        .route_kind = static_cast<std::uint16_t>(RouteKind::unknown),
        .error_code = static_cast<std::uint16_t>(decision.error().code),
    });
    connection->send(error_json(decision.error().code));
    return;
  }

  if (decision->route_kind == RouteKind::binary_scip || decision->route_kind == RouteKind::binary_docs) {
    auto item = BinaryWorkItem{
        .connection = connection,
        .route_kind = decision->route_kind,
        .compressed_payload = std::move(message),
        .query_id = query_id,
        .received_ns = received_ns,
        .tenant_id = decision->tenant_id,
    };
    if (!dispatcher().try_push(std::move(item))) {
      record_metric(IngestMetric{
          .timestamp_ns = steady_timestamp_ns(),
          .query_id = query_id,
          .tenant_id = session->ingress.tenant_id,
          .payload_size = decision->payload_size,
          .compressed_payload_size = decision->payload_size,
          .uncompressed_payload_size = 0,
          .latency_us = elapsed_us_since(received_ns),
          .route_kind = static_cast<std::uint16_t>(decision->route_kind),
          .error_code = static_cast<std::uint16_t>(BoundaryErrorCode::queue_saturated),
      });
      connection->send(error_json(BoundaryErrorCode::queue_saturated));
    }
    return;
  }

  if (decision->route_kind == RouteKind::opencode_synthesis) {
    const auto summary = synthesized_context_summary(*decision);
    auto request = serialize_session_message_request(OpenCodeSessionMessageRequest{
        .session_id = decision->control.session_id,
        .message_id = decision->control.message_id,
        .agent = decision->control.agent,
        .system = "You are the cloud reasoning stage for GraphKV-Engine. Use only the supplied GraphKV context synthesis payload as evidence.",
        .model = OpenCodeModelRef{.provider_id = decision->control.provider_id, .model_id = decision->control.model_id},
        .context = SynthesizedContext{
            .query = decision->control.query,
            .dense_summary = summary,
            .tenant_id = decision->tenant_id,
            .source_bytes = decision->payload_size,
            .estimated_input_tokens = static_cast<std::uint64_t>(summary.size() / 4U),
        },
        .no_reply = decision->control.no_reply,
        .structured_output = true,
    });
    if (!request) {
      record_metric(IngestMetric{
          .timestamp_ns = steady_timestamp_ns(),
          .query_id = query_id,
          .tenant_id = decision->tenant_id,
          .payload_size = decision->payload_size,
          .latency_us = elapsed_us_since(received_ns),
          .route_kind = static_cast<std::uint16_t>(RouteKind::opencode_synthesis),
          .error_code = static_cast<std::uint16_t>(BoundaryErrorCode::opencode_serialization_failed),
      });
      connection->send(error_json(BoundaryErrorCode::opencode_serialization_failed));
      return;
    }
    record_metric(IngestMetric{
        .timestamp_ns = steady_timestamp_ns(),
        .query_id = query_id,
        .tenant_id = decision->tenant_id,
        .payload_size = decision->payload_size,
        .latency_us = elapsed_us_since(received_ns),
        .route_kind = static_cast<std::uint16_t>(RouteKind::opencode_synthesis),
        .error_code = static_cast<std::uint16_t>(BoundaryErrorCode::none),
    });
    connection->send(opencode_request_json(*request));
    return;
  }

  record_metric(IngestMetric{
      .timestamp_ns = steady_timestamp_ns(),
      .query_id = query_id,
      .tenant_id = decision->tenant_id,
      .payload_size = decision->payload_size,
      .latency_us = elapsed_us_since(received_ns),
      .route_kind = static_cast<std::uint16_t>(decision->route_kind),
      .error_code = static_cast<std::uint16_t>(BoundaryErrorCode::none),
  });
  connection->send(control_json(*decision));
}

} // namespace graphify
