#include "RetrievalIndex.hpp"

#include <algorithm>
#include <cmath>
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

#if defined(GRAPHKV_HAS_USEARCH)
#include <usearch/index.hpp>
#include <usearch/index_dense.hpp>
#endif

namespace graphify {

class DenseVectorBackend {
 public:
  virtual ~DenseVectorBackend() = default;
  [[nodiscard]] virtual Expected<void, RetrievalError> build(std::span<const float> vectors,
                                                            std::uint32_t dimensions,
                                                            std::uint32_t count) = 0;
  [[nodiscard]] virtual std::vector<RetrievalHit> search(std::span<const float> query, std::size_t limit) const = 0;
  [[nodiscard]] virtual std::string_view name() const noexcept = 0;
  [[nodiscard]] virtual bool uses_usearch() const noexcept = 0;
};

namespace {

inline constexpr std::uint16_t dense_source_mask = 1U << 0U;
inline constexpr std::uint16_t sparse_source_mask = 1U << 1U;
inline constexpr std::uint16_t graph_source_mask = 1U << 2U;

[[nodiscard]] RetrievalError retrieval_error(RetrievalErrorCode code, std::string message) {
  return RetrievalError{.code = code, .message = std::move(message)};
}

[[nodiscard]] char ascii_lower(char value) noexcept {
  return value >= 'A' && value <= 'Z' ? static_cast<char>(value + ('a' - 'A')) : value;
}

[[nodiscard]] bool is_token_character(char value) noexcept {
  const auto lower = ascii_lower(value);
  return (lower >= 'a' && lower <= 'z') || (lower >= '0' && lower <= '9') || lower == '_' || lower == '-' || lower == '.' || lower == '/';
}

[[nodiscard]] std::uint64_t fnv1a_append(std::uint64_t hash, char value) noexcept {
  hash ^= static_cast<unsigned char>(ascii_lower(value));
  hash *= 1099511628211ULL;
  return hash;
}

template <typename Callback>
void scan_token_hashes(std::string_view text, Callback&& callback) {
  std::uint64_t hash = 1469598103934665603ULL;
  std::uint32_t length = 0;
  auto flush = [&] {
    if (length >= 2U) {
      callback(hash);
    }
    hash = 1469598103934665603ULL;
    length = 0;
  };

  for (char character : text) {
    if (!is_token_character(character)) {
      flush();
      continue;
    }
    hash = fnv1a_append(hash, character);
    ++length;
  }
  flush();
}

[[nodiscard]] std::string chunk_text_for_projection(const DocIndexSnapshot& snapshot, const DocChunkRecord& chunk) {
  std::string text;
  if (chunk.source_index < snapshot.sources().size()) {
    const auto& source = snapshot.sources()[chunk.source_index];
    text += snapshot.resolve(source.path);
    text.push_back(' ');
    text += snapshot.resolve(source.title);
    text.push_back(' ');
  }
  text += snapshot.resolve(chunk.heading);
  text.push_back(' ');
  text += snapshot.resolve(chunk.chunk_type);
  text.push_back(' ');
  text += snapshot.resolve(chunk.text_preview);
  return text;
}

void project_text_to_dense(std::string_view text, std::span<float> vector) {
  std::fill(vector.begin(), vector.end(), 0.0F);
  if (vector.empty()) {
    return;
  }
  scan_token_hashes(text, [&](std::uint64_t hash) {
    const auto index = static_cast<std::size_t>(hash % vector.size());
    const auto sign = ((hash >> 63U) != 0U) ? -1.0F : 1.0F;
    vector[index] += sign;
  });
  float norm = 0.0F;
  for (float value : vector) {
    norm += value * value;
  }
  if (norm <= 0.0F) {
    return;
  }
  const auto inverse = 1.0F / std::sqrt(norm);
  for (auto& value : vector) {
    value *= inverse;
  }
}

[[nodiscard]] float dot(std::span<const float> left, std::span<const float> right) noexcept {
  float sum = 0.0F;
  const auto count = std::min(left.size(), right.size());
  for (std::size_t index = 0; index < count; ++index) {
    sum += left[index] * right[index];
  }
  return sum;
}

[[nodiscard]] bool path_overlaps(std::string_view left, std::string_view right) noexcept {
  if (left.empty() || right.empty()) {
    return false;
  }
  return left == right || left.ends_with(right) || right.ends_with(left) || left.find(right) != std::string_view::npos ||
         right.find(left) != std::string_view::npos;
}

[[nodiscard]] bool chunk_overlaps_graph(const DocIndexSnapshot& docs,
                                        const ArenaSnapshot& graph,
                                        std::uint32_t chunk_index) noexcept {
  const auto chunks = docs.chunks();
  if (chunk_index >= chunks.size()) {
    return false;
  }
  const auto& chunk = chunks[chunk_index];
  const auto sources = docs.sources();
  if (chunk.source_index >= sources.size()) {
    return false;
  }
  const auto source_path = docs.resolve(sources[chunk.source_index].path);
  for (const auto& document : graph.documents()) {
    if (path_overlaps(source_path, graph.resolve(document.relative_path))) {
      return true;
    }
  }

  if (chunk.first_link == external_document_index || chunk.link_count == 0U) {
    return false;
  }
  const auto links = docs.links();
  const auto link_end = std::min<std::uint32_t>(chunk.first_link + chunk.link_count, static_cast<std::uint32_t>(links.size()));
  for (auto link_index = chunk.first_link; link_index < link_end; ++link_index) {
    const auto target = docs.resolve(links[link_index].target);
    for (const auto& document : graph.documents()) {
      if (path_overlaps(target, graph.resolve(document.relative_path))) {
        return true;
      }
    }
  }
  return false;
}

void sort_and_trim(std::vector<RetrievalHit>& hits, std::size_t limit) {
  std::sort(hits.begin(), hits.end(), [](const RetrievalHit& left, const RetrievalHit& right) {
    if (left.score != right.score) {
      return left.score > right.score;
    }
    return left.chunk_index < right.chunk_index;
  });
  if (hits.size() > limit) {
    hits.resize(limit);
  }
  for (std::size_t index = 0; index < hits.size(); ++index) {
    hits[index].rank = static_cast<std::uint16_t>(std::min<std::size_t>(index + 1U, std::numeric_limits<std::uint16_t>::max()));
  }
}

class ExactDenseBackend final : public DenseVectorBackend {
 public:
  [[nodiscard]] Expected<void, RetrievalError> build(std::span<const float> vectors,
                                                     std::uint32_t dimensions,
                                                     std::uint32_t count) override {
    dimensions_ = dimensions;
    count_ = count;
    vectors_.assign(vectors.begin(), vectors.end());
    return {};
  }

  [[nodiscard]] std::vector<RetrievalHit> search(std::span<const float> query, std::size_t limit) const override {
    std::vector<RetrievalHit> hits;
    hits.reserve(std::min<std::size_t>(limit, count_));
    for (std::uint32_t chunk_index = 0; chunk_index < count_; ++chunk_index) {
      const auto offset = static_cast<std::size_t>(chunk_index) * dimensions_;
      const auto score = dot(query, std::span<const float>(vectors_.data() + offset, dimensions_));
      hits.push_back(RetrievalHit{.chunk_index = chunk_index, .score = score, .source_mask = dense_source_mask});
    }
    sort_and_trim(hits, limit);
    return hits;
  }

  [[nodiscard]] std::string_view name() const noexcept override { return "exact-dense-fallback"; }
  [[nodiscard]] bool uses_usearch() const noexcept override { return false; }

 private:
  std::uint32_t dimensions_{};
  std::uint32_t count_{};
  std::vector<float> vectors_{};
};

#if defined(GRAPHKV_HAS_USEARCH)
class USearchDenseBackend final : public DenseVectorBackend {
 public:
  [[nodiscard]] Expected<void, RetrievalError> build(std::span<const float> vectors,
                                                     std::uint32_t dimensions,
                                                     std::uint32_t count) override {
    dimensions_ = dimensions;
    unum::usearch::metric_punned_t metric(dimensions, unum::usearch::metric_kind_t::cos_k, unum::usearch::scalar_kind_t::f32_k);
    index_ = unum::usearch::index_dense_t::make(metric);
    if (!index_) {
      return unexpected(retrieval_error(RetrievalErrorCode::dense_backend_failed, "USearch index construction failed"));
    }
    if (!index_.try_reserve(count)) {
      return unexpected(retrieval_error(RetrievalErrorCode::allocation_failure, "USearch reserve failed"));
    }
    for (std::uint32_t chunk_index = 0; chunk_index < count; ++chunk_index) {
      const auto offset = static_cast<std::size_t>(chunk_index) * dimensions;
      auto added = index_.add(chunk_index, vectors.data() + offset);
      if (!added) {
        return unexpected(retrieval_error(RetrievalErrorCode::dense_backend_failed, "USearch add failed"));
      }
    }
    return {};
  }

  [[nodiscard]] std::vector<RetrievalHit> search(std::span<const float> query, std::size_t limit) const override {
    std::vector<RetrievalHit> hits;
    if (query.empty() || limit == 0U || !index_) {
      return hits;
    }
    auto results = index_.search(query.data(), limit);
    if (!results) {
      return hits;
    }
    hits.reserve(results.size());
    for (std::size_t index = 0; index < results.size(); ++index) {
      const auto result = results[index];
      const auto distance = static_cast<float>(result.distance);
      hits.push_back(RetrievalHit{
          .chunk_index = static_cast<std::uint32_t>(result.member.key),
          .score = 1.0F / (1.0F + std::max(0.0F, distance)),
          .source_mask = dense_source_mask,
      });
    }
    sort_and_trim(hits, limit);
    return hits;
  }

  [[nodiscard]] std::string_view name() const noexcept override { return "usearch-hnsw"; }
  [[nodiscard]] bool uses_usearch() const noexcept override { return true; }

 private:
  std::uint32_t dimensions_{};
  unum::usearch::index_dense_t index_{};
};
#endif

[[nodiscard]] std::unique_ptr<DenseVectorBackend> make_dense_backend() {
#if defined(GRAPHKV_HAS_USEARCH)
  return std::make_unique<USearchDenseBackend>();
#else
  return std::make_unique<ExactDenseBackend>();
#endif
}

void add_rrf_scores(std::unordered_map<std::uint32_t, RetrievalHit>& fused,
                    std::span<const RetrievalHit> hits,
                    float weight,
                    float rrf_k,
                    std::uint16_t source_mask) {
  for (std::size_t index = 0; index < hits.size(); ++index) {
    auto& hit = fused[hits[index].chunk_index];
    hit.chunk_index = hits[index].chunk_index;
    hit.score += weight / (rrf_k + static_cast<float>(index + 1U));
    hit.source_mask |= source_mask;
  }
}

} // namespace

DocRetrievalIndex::DocRetrievalIndex(DocRetrievalIndex&&) noexcept = default;
DocRetrievalIndex& DocRetrievalIndex::operator=(DocRetrievalIndex&&) noexcept = default;
DocRetrievalIndex::~DocRetrievalIndex() = default;

std::string_view to_string(RetrievalErrorCode code) noexcept {
  switch (code) {
    case RetrievalErrorCode::invalid_dimension:
      return "invalid-dimension";
    case RetrievalErrorCode::dense_backend_failed:
      return "dense-backend-failed";
    case RetrievalErrorCode::allocation_failure:
      return "allocation-failure";
  }
  return "unknown";
}

Expected<DocRetrievalIndex, RetrievalError> DocRetrievalIndex::build(const DocIndexSnapshot& snapshot,
                                                                    RetrievalOptions options) {
  if (options.dense_dimensions == 0U) {
    return unexpected(retrieval_error(RetrievalErrorCode::invalid_dimension, "dense dimension must be non-zero"));
  }

  DocRetrievalIndex index;
  index.options_ = options;
  const auto chunks = snapshot.chunks();
  index.chunk_lengths_.resize(chunks.size());
  index.dense_vectors_.resize(static_cast<std::size_t>(options.dense_dimensions) * chunks.size());

  std::vector<SparsePostingRecord> postings;
  postings.reserve(chunks.size() * 32U);
  std::uint64_t total_length = 0;
  for (std::uint32_t chunk_index = 0; chunk_index < chunks.size(); ++chunk_index) {
    const auto text = chunk_text_for_projection(snapshot, chunks[chunk_index]);
    std::unordered_map<std::uint64_t, std::uint32_t> frequencies;
    scan_token_hashes(text, [&](std::uint64_t hash) { ++frequencies[hash]; });
    index.chunk_lengths_[chunk_index] = static_cast<std::uint32_t>(frequencies.size());
    total_length += frequencies.size();
    for (const auto& [hash, frequency] : frequencies) {
      postings.push_back(SparsePostingRecord{.token_hash = hash, .chunk_index = chunk_index, .term_frequency = frequency});
    }

    const auto offset = static_cast<std::size_t>(chunk_index) * options.dense_dimensions;
    project_text_to_dense(text, std::span<float>(index.dense_vectors_.data() + offset, options.dense_dimensions));
  }
  index.average_chunk_length_ = chunks.empty() ? 0.0F : static_cast<float>(total_length) / static_cast<float>(chunks.size());

  std::sort(postings.begin(), postings.end(), [](const SparsePostingRecord& left, const SparsePostingRecord& right) {
    if (left.token_hash != right.token_hash) {
      return left.token_hash < right.token_hash;
    }
    return left.chunk_index < right.chunk_index;
  });
  index.postings_ = std::move(postings);
  for (std::uint32_t posting_index = 0; posting_index < index.postings_.size();) {
    const auto token_hash = index.postings_[posting_index].token_hash;
    auto next = posting_index + 1U;
    while (next < index.postings_.size() && index.postings_[next].token_hash == token_hash) {
      ++next;
    }
    const auto posting_count = next - posting_index;
    const auto document_count = static_cast<float>(std::max<std::size_t>(chunks.size(), 1U));
    const auto document_frequency = static_cast<float>(posting_count);
    const auto idf = std::log(1.0F + ((document_count - document_frequency + 0.5F) / (document_frequency + 0.5F)));
    index.terms_.push_back(SparseTermRecord{.token_hash = token_hash, .first_posting = posting_index, .posting_count = posting_count, .idf = idf});
    posting_index = next;
  }

  index.dense_backend_ = make_dense_backend();
  auto built = index.dense_backend_->build(index.dense_vectors_, options.dense_dimensions, static_cast<std::uint32_t>(chunks.size()));
  if (!built) {
#if defined(GRAPHKV_HAS_USEARCH)
    auto fallback = std::make_unique<ExactDenseBackend>();
    auto fallback_built = fallback->build(index.dense_vectors_, options.dense_dimensions, static_cast<std::uint32_t>(chunks.size()));
    if (!fallback_built) {
      return unexpected(fallback_built.error());
    }
    index.dense_backend_ = std::move(fallback);
#else
    return unexpected(built.error());
#endif
  }
  return std::move(index);
}

std::vector<RetrievalHit> DocRetrievalIndex::sparse_search(const DocIndexSnapshot& snapshot,
                                                           std::string_view query,
                                                           std::size_t limit) const {
  std::vector<RetrievalHit> hits;
  if (query.empty() || terms_.empty() || snapshot.chunks().empty() || limit == 0U) {
    return hits;
  }

  std::vector<std::uint64_t> query_hashes;
  scan_token_hashes(query, [&](std::uint64_t hash) { query_hashes.push_back(hash); });
  std::sort(query_hashes.begin(), query_hashes.end());
  query_hashes.erase(std::unique(query_hashes.begin(), query_hashes.end()), query_hashes.end());

  std::unordered_map<std::uint32_t, float> scores;
  constexpr float k1 = 1.2F;
  constexpr float b = 0.75F;
  for (const auto hash : query_hashes) {
    auto term = std::lower_bound(terms_.begin(), terms_.end(), hash, [](const SparseTermRecord& record, std::uint64_t value) {
      return record.token_hash < value;
    });
    if (term == terms_.end() || term->token_hash != hash) {
      continue;
    }
    const auto end = term->first_posting + term->posting_count;
    for (auto posting_index = term->first_posting; posting_index < end; ++posting_index) {
      const auto& posting = postings_[posting_index];
      const auto chunk_length = static_cast<float>(std::max<std::uint32_t>(chunk_lengths_[posting.chunk_index], 1U));
      const auto denominator = static_cast<float>(posting.term_frequency) +
                               k1 * (1.0F - b + b * (chunk_length / std::max(average_chunk_length_, 1.0F)));
      scores[posting.chunk_index] += term->idf * (static_cast<float>(posting.term_frequency) * (k1 + 1.0F)) / denominator;
    }
  }

  hits.reserve(scores.size());
  for (const auto& [chunk_index, score] : scores) {
    hits.push_back(RetrievalHit{.chunk_index = chunk_index, .score = score, .source_mask = sparse_source_mask});
  }
  sort_and_trim(hits, limit);
  return hits;
}

std::vector<RetrievalHit> DocRetrievalIndex::dense_search(std::string_view query, std::size_t limit) const {
  if (!dense_backend_ || query.empty() || limit == 0U) {
    return {};
  }
  std::vector<float> query_vector(options_.dense_dimensions);
  project_text_to_dense(query, query_vector);
  return dense_backend_->search(query_vector, limit);
}

std::vector<RetrievalHit> DocRetrievalIndex::rrf_search(const DocIndexSnapshot& docs,
                                                        const ArenaSnapshot* graph,
                                                        std::string_view query,
                                                        std::size_t limit) const {
  const auto dense_hits = dense_search(query, options_.dense_candidate_limit);
  const auto sparse_hits = sparse_search(docs, query, options_.sparse_candidate_limit);
  std::unordered_map<std::uint32_t, RetrievalHit> fused;
  fused.reserve(dense_hits.size() + sparse_hits.size());
  add_rrf_scores(fused, dense_hits, options_.dense_weight, options_.rrf_k, dense_source_mask);
  add_rrf_scores(fused, sparse_hits, options_.sparse_weight, options_.rrf_k, sparse_source_mask);

  bool has_graph_overlap = false;
  if (graph != nullptr) {
    for (auto& [_, hit] : fused) {
      hit.graph_overlap = chunk_overlaps_graph(docs, *graph, hit.chunk_index);
      if (hit.graph_overlap) {
        hit.source_mask |= graph_source_mask;
        hit.score += options_.graph_weight / options_.rrf_k;
        has_graph_overlap = true;
      }
    }
  }

  std::vector<RetrievalHit> hits;
  hits.reserve(fused.size());
  for (const auto& [_, hit] : fused) {
    if (has_graph_overlap && !hit.graph_overlap) {
      continue;
    }
    hits.push_back(hit);
  }
  sort_and_trim(hits, limit);
  return hits;
}

std::string_view DocRetrievalIndex::dense_backend_name() const noexcept {
  return dense_backend_ ? dense_backend_->name() : "none";
}

bool DocRetrievalIndex::dense_uses_usearch() const noexcept {
  return dense_backend_ != nullptr && dense_backend_->uses_usearch();
}

} // namespace graphify