#pragma once

#include "DocIngestor.hpp"

#include <cstddef>
#include <cstdint>
#include <memory>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace graphify {

enum class RetrievalErrorCode : std::uint8_t {
  invalid_dimension,
  dense_backend_failed,
  allocation_failure,
};

struct RetrievalError final {
  RetrievalErrorCode code{};
  std::string message{};
};

struct RetrievalOptions final {
  std::uint32_t dense_dimensions{64};
  std::uint32_t dense_candidate_limit{32};
  std::uint32_t sparse_candidate_limit{32};
  float rrf_k{60.0F};
  float dense_weight{1.0F};
  float sparse_weight{1.0F};
  float graph_weight{0.35F};
};

struct RetrievalHit final {
  std::uint32_t chunk_index{};
  float score{};
  std::uint16_t rank{};
  std::uint16_t source_mask{};
  bool graph_overlap{};
};

struct SparseTermRecord final {
  std::uint64_t token_hash{};
  std::uint32_t first_posting{};
  std::uint32_t posting_count{};
  float idf{};
};

struct SparsePostingRecord final {
  std::uint64_t token_hash{};
  std::uint32_t chunk_index{};
  std::uint32_t term_frequency{};
};

class DenseVectorBackend;

class DocRetrievalIndex final {
 public:
  DocRetrievalIndex() = default;
  DocRetrievalIndex(DocRetrievalIndex&&) noexcept;
  DocRetrievalIndex& operator=(DocRetrievalIndex&&) noexcept;
  ~DocRetrievalIndex();

  DocRetrievalIndex(const DocRetrievalIndex&) = delete;
  DocRetrievalIndex& operator=(const DocRetrievalIndex&) = delete;

  [[nodiscard]] static Expected<DocRetrievalIndex, RetrievalError> build(const DocIndexSnapshot& snapshot,
                                                                         RetrievalOptions options = {});

  [[nodiscard]] std::vector<RetrievalHit> sparse_search(const DocIndexSnapshot& snapshot,
                                                        std::string_view query,
                                                        std::size_t limit) const;
  [[nodiscard]] std::vector<RetrievalHit> dense_search(std::string_view query, std::size_t limit) const;
  [[nodiscard]] std::vector<RetrievalHit> rrf_search(const DocIndexSnapshot& docs,
                                                     const ArenaSnapshot* graph,
                                                     std::string_view query,
                                                     std::size_t limit) const;

  [[nodiscard]] std::string_view dense_backend_name() const noexcept;
  [[nodiscard]] bool dense_uses_usearch() const noexcept;
  [[nodiscard]] const RetrievalOptions& options() const noexcept { return options_; }
  [[nodiscard]] std::size_t term_count() const noexcept { return terms_.size(); }
  [[nodiscard]] std::size_t posting_count() const noexcept { return postings_.size(); }

 private:
  RetrievalOptions options_{};
  std::vector<SparseTermRecord> terms_{};
  std::vector<SparsePostingRecord> postings_{};
  std::vector<std::uint32_t> chunk_lengths_{};
  float average_chunk_length_{};
  std::vector<float> dense_vectors_{};
  std::unique_ptr<DenseVectorBackend> dense_backend_{};
};

[[nodiscard]] std::string_view to_string(RetrievalErrorCode code) noexcept;

} // namespace graphify