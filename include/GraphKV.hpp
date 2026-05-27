#pragma once

#include "ExpectedCompat.hpp"

#include <algorithm>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <memory>
#include <mutex>
#include <new>
#include <span>
#include <string>
#include <string_view>
#include <type_traits>
#include <vector>

#if __has_include(<mdspan>)
#include <mdspan>
#if defined(__cpp_lib_mdspan) && __cpp_lib_mdspan >= 202207L
#define GRAPHIFY_HAS_MDSPAN_HEADER 1
#endif
#endif

#if !defined(GRAPHIFY_HAS_MDSPAN_HEADER) && __has_include(<mdspan/mdspan.hpp>)
#include <mdspan/mdspan.hpp>
#define GRAPHIFY_HAS_KOKKOS_MDSPAN_HEADER 1
#elif !defined(GRAPHIFY_HAS_MDSPAN_HEADER) && __has_include(<experimental/mdspan>)
#include <experimental/mdspan>
#define GRAPHIFY_HAS_EXPERIMENTAL_MDSPAN_HEADER 1
#endif

#if defined(GRAPHIFY_HAS_FOLLY)
#include <folly/synchronization/Hazptr.h>
#endif

namespace graphify {

#if defined(__cpp_lib_hardware_interference_size)
inline constexpr std::size_t cache_line_size = std::hardware_constructive_interference_size;
#else
inline constexpr std::size_t cache_line_size = 64;
#endif

inline constexpr std::uint32_t external_document_index = std::numeric_limits<std::uint32_t>::max();

namespace md {
#if defined(GRAPHIFY_HAS_MDSPAN_HEADER)
using std::default_accessor;
using std::dynamic_extent;
using std::extents;
using std::layout_right;
using std::layout_stride;
using std::mdspan;
#elif defined(GRAPHIFY_HAS_KOKKOS_MDSPAN_HEADER)
using Kokkos::default_accessor;
using Kokkos::dynamic_extent;
using Kokkos::extents;
using Kokkos::layout_right;
using Kokkos::layout_stride;
using Kokkos::mdspan;
#elif defined(GRAPHIFY_HAS_EXPERIMENTAL_MDSPAN_HEADER)
using std::experimental::default_accessor;
using std::experimental::extents;
using std::experimental::layout_right;
using std::experimental::layout_stride;
using std::experimental::mdspan;
using std::dynamic_extent;
#endif
} // namespace md

struct StringRef final {
  std::uint32_t offset{};
  std::uint32_t length{};

  [[nodiscard]] constexpr bool empty() const noexcept { return length == 0; }
};

static_assert(sizeof(StringRef) == 8);
static_assert(std::is_trivially_copyable_v<StringRef>);

struct DocumentRecord final {
  StringRef relative_path{};
  StringRef language{};
  std::uint32_t first_symbol{};
  std::uint32_t symbol_count{};
  std::uint32_t first_occurrence{};
  std::uint32_t occurrence_count{};
  std::uint32_t position_encoding{};
  std::uint32_t reserved{};
};

struct SymbolNode final {
  StringRef symbol{};
  StringRef display_name{};
  StringRef documentation{};
  std::uint32_t kind{};
  std::uint32_t document_index{external_document_index};
  std::uint32_t first_edge{};
  std::uint32_t edge_count{};
  std::uint32_t reserved{};
};

struct RelationshipEdge final {
  StringRef target_symbol{};
  std::uint32_t source_symbol{};
  std::uint32_t flags{};
};

struct OccurrenceRecord final {
  StringRef symbol{};
  std::uint32_t document_index{};
  std::uint32_t symbol_roles{};
  std::uint32_t syntax_kind{};
  std::uint32_t start_line{};
  std::uint32_t start_character{};
  std::uint32_t end_line{};
  std::uint32_t end_character{};
};

static_assert(std::is_trivially_copyable_v<DocumentRecord>);
static_assert(std::is_trivially_copyable_v<SymbolNode>);
static_assert(std::is_trivially_copyable_v<RelationshipEdge>);
static_assert(std::is_trivially_copyable_v<OccurrenceRecord>);

struct GraphCounts final {
  std::uint32_t documents{};
  std::uint32_t symbols{};
  std::uint32_t relationships{};
  std::uint32_t occurrences{};
  std::uint32_t string_bytes{};
};

enum class GraphErrorCode : std::uint8_t {
  count_overflow,
  arena_overflow,
  allocation_failure,
};

struct GraphError final {
  GraphErrorCode code{};
  std::string message{};
};

namespace detail {

[[nodiscard]] constexpr std::size_t align_up(std::size_t value, std::size_t alignment) noexcept {
  return (value + alignment - 1U) & ~(alignment - 1U);
}

template <typename T>
[[nodiscard]] constexpr bool checked_array_bytes(std::size_t count, std::size_t& bytes) noexcept {
  if (count > std::numeric_limits<std::size_t>::max() / sizeof(T)) {
    return false;
  }
  bytes = count * sizeof(T);
  return true;
}

[[nodiscard]] constexpr bool checked_add(std::size_t value, std::size_t addend, std::size_t& result) noexcept {
  if (value > std::numeric_limits<std::size_t>::max() - addend) {
    return false;
  }
  result = value + addend;
  return true;
}

} // namespace detail

class alignas(cache_line_size) ArenaSnapshot
#if defined(GRAPHIFY_HAS_FOLLY)
    : public folly::hazptr_obj_base<ArenaSnapshot>
#endif
{
 public:
  explicit ArenaSnapshot(GraphCounts counts, std::size_t arena_bytes)
      : counts_(counts), arena_bytes_(arena_bytes) {}

  ArenaSnapshot(const ArenaSnapshot&) = delete;
  ArenaSnapshot& operator=(const ArenaSnapshot&) = delete;

  ArenaSnapshot(ArenaSnapshot&&) = delete;
  ArenaSnapshot& operator=(ArenaSnapshot&&) = delete;

  ~ArenaSnapshot() { ::operator delete(arena_, std::align_val_t(cache_line_size)); }

  [[nodiscard]] const GraphCounts& counts() const noexcept { return counts_; }
  [[nodiscard]] std::size_t arena_bytes() const noexcept { return arena_bytes_; }
  [[nodiscard]] std::uint32_t string_bytes_used() const noexcept { return string_cursor_; }

  [[nodiscard]] std::span<DocumentRecord> documents() noexcept { return {documents_, counts_.documents}; }
  [[nodiscard]] std::span<const DocumentRecord> documents() const noexcept { return {documents_, counts_.documents}; }
  [[nodiscard]] std::span<SymbolNode> symbols() noexcept { return {symbols_, counts_.symbols}; }
  [[nodiscard]] std::span<const SymbolNode> symbols() const noexcept { return {symbols_, counts_.symbols}; }
  [[nodiscard]] std::span<RelationshipEdge> relationships() noexcept { return {relationships_, counts_.relationships}; }
  [[nodiscard]] std::span<const RelationshipEdge> relationships() const noexcept { return {relationships_, counts_.relationships}; }
  [[nodiscard]] std::span<OccurrenceRecord> occurrences() noexcept { return {occurrences_, counts_.occurrences}; }
  [[nodiscard]] std::span<const OccurrenceRecord> occurrences() const noexcept { return {occurrences_, counts_.occurrences}; }
  [[nodiscard]] std::span<const char> string_tape() const noexcept { return {string_data_, counts_.string_bytes}; }

#if defined(GRAPHIFY_HAS_MDSPAN_HEADER) || defined(GRAPHIFY_HAS_EXPERIMENTAL_MDSPAN_HEADER) || defined(GRAPHIFY_HAS_KOKKOS_MDSPAN_HEADER)
  using SymbolView = md::mdspan<const SymbolNode, md::extents<std::size_t, md::dynamic_extent>>;
  using RelationshipView = md::mdspan<const RelationshipEdge, md::extents<std::size_t, md::dynamic_extent>>;

  [[nodiscard]] SymbolView symbol_view() const noexcept { return SymbolView(symbols_, counts_.symbols); }
  [[nodiscard]] RelationshipView relationship_view() const noexcept { return RelationshipView(relationships_, counts_.relationships); }
#endif

  [[nodiscard]] Expected<StringRef, GraphError> append_string(std::string_view value) noexcept {
    if (value.size() > std::numeric_limits<std::uint32_t>::max()) {
      return unexpected(GraphError{GraphErrorCode::count_overflow, "string length exceeds uint32_t"});
    }
    if (string_cursor_ > counts_.string_bytes || value.size() > counts_.string_bytes - string_cursor_) {
      return unexpected(GraphError{GraphErrorCode::arena_overflow, "StringTape capacity exhausted"});
    }
    const auto offset = string_cursor_;
    if (!value.empty()) {
      std::memcpy(string_data_ + string_cursor_, value.data(), value.size());
    }
    string_cursor_ += static_cast<std::uint32_t>(value.size());
    return StringRef{offset, static_cast<std::uint32_t>(value.size())};
  }

  [[nodiscard]] std::string_view resolve(StringRef ref) const noexcept {
    if (ref.length == 0) {
      return {};
    }
    if (ref.offset > counts_.string_bytes || ref.length > counts_.string_bytes - ref.offset) {
      return {};
    }
    return {string_data_ + ref.offset, ref.length};
  }

 private:
  friend class GraphArena;

  GraphCounts counts_{};
  std::size_t arena_bytes_{};
  std::byte* arena_{};
  DocumentRecord* documents_{};
  SymbolNode* symbols_{};
  RelationshipEdge* relationships_{};
  OccurrenceRecord* occurrences_{};
  char* string_data_{};
  std::uint32_t string_cursor_{};
};

class GraphArena final {
 public:
  [[nodiscard]] static Expected<std::unique_ptr<ArenaSnapshot>, GraphError> allocate(GraphCounts counts) {
    std::size_t total_bytes = 0;
    auto add_array = [&]<typename T>(std::uint32_t count) -> bool {
      total_bytes = detail::align_up(total_bytes, alignof(T));
      std::size_t array_bytes = 0;
      if (!detail::checked_array_bytes<T>(count, array_bytes)) {
        return false;
      }
      return detail::checked_add(total_bytes, array_bytes, total_bytes);
    };

    if (!add_array.template operator()<DocumentRecord>(counts.documents) ||
        !add_array.template operator()<SymbolNode>(counts.symbols) ||
        !add_array.template operator()<RelationshipEdge>(counts.relationships) ||
        !add_array.template operator()<OccurrenceRecord>(counts.occurrences)) {
      return unexpected(GraphError{GraphErrorCode::count_overflow, "GraphKV array size overflow"});
    }

    total_bytes = detail::align_up(total_bytes, alignof(char));
    if (!detail::checked_add(total_bytes, counts.string_bytes, total_bytes)) {
      return unexpected(GraphError{GraphErrorCode::count_overflow, "StringTape size overflow"});
    }

    total_bytes = std::max<std::size_t>(total_bytes, cache_line_size);
    auto snapshot = std::make_unique<ArenaSnapshot>(counts, total_bytes);

    try {
      snapshot->arena_ = static_cast<std::byte*>(::operator new(total_bytes, std::align_val_t(cache_line_size)));
    } catch (const std::bad_alloc&) {
      return unexpected(GraphError{GraphErrorCode::allocation_failure, "aligned GraphKV allocation failed"});
    }

    std::size_t offset = 0;
    snapshot->documents_ = carve<DocumentRecord>(*snapshot, offset, counts.documents);
    snapshot->symbols_ = carve<SymbolNode>(*snapshot, offset, counts.symbols);
    snapshot->relationships_ = carve<RelationshipEdge>(*snapshot, offset, counts.relationships);
    snapshot->occurrences_ = carve<OccurrenceRecord>(*snapshot, offset, counts.occurrences);
    offset = detail::align_up(offset, alignof(char));
    snapshot->string_data_ = reinterpret_cast<char*>(snapshot->arena_ + offset);
    if (counts.string_bytes > 0) {
      std::memset(snapshot->string_data_, 0, counts.string_bytes);
    }
    return snapshot;
  }

 private:
  template <typename T>
  [[nodiscard]] static T* carve(ArenaSnapshot& snapshot, std::size_t& offset, std::uint32_t count) noexcept {
    offset = detail::align_up(offset, alignof(T));
    auto* pointer = reinterpret_cast<T*>(snapshot.arena_ + offset);
    std::uninitialized_value_construct_n(pointer, count);
    offset += sizeof(T) * static_cast<std::size_t>(count);
    return pointer;
  }
};

class SnapshotPublisher final {
 public:
  class ReadHandle final {
   public:
    ReadHandle(const ReadHandle&) = delete;
    ReadHandle& operator=(const ReadHandle&) = delete;
    ReadHandle(ReadHandle&&) noexcept = default;
    ReadHandle& operator=(ReadHandle&&) noexcept = default;

    [[nodiscard]] const ArenaSnapshot* get() const noexcept { return snapshot_; }
    [[nodiscard]] const ArenaSnapshot* operator->() const noexcept { return snapshot_; }
    [[nodiscard]] const ArenaSnapshot& operator*() const noexcept { return *snapshot_; }
    [[nodiscard]] explicit operator bool() const noexcept { return snapshot_ != nullptr; }

   private:
    friend class SnapshotPublisher;

    explicit ReadHandle(const SnapshotPublisher& publisher) noexcept
#if defined(GRAPHIFY_HAS_FOLLY)
        : holder_(folly::make_hazard_pointer()), snapshot_(holder_.protect(publisher.root_))
#else
        : snapshot_(publisher.root_.load(std::memory_order_acquire))
#endif
    {}

#if defined(GRAPHIFY_HAS_FOLLY)
  folly::hazptr_holder<> holder_{};
#endif
    const ArenaSnapshot* snapshot_{};
  };

  SnapshotPublisher() = default;
  SnapshotPublisher(const SnapshotPublisher&) = delete;
  SnapshotPublisher& operator=(const SnapshotPublisher&) = delete;

  [[nodiscard]] ReadHandle acquire() const noexcept { return ReadHandle(*this); }
  [[nodiscard]] const ArenaSnapshot* load() const noexcept { return root_.load(std::memory_order_acquire); }

  void publish(std::unique_ptr<ArenaSnapshot> snapshot) {
    if (!snapshot) {
      return;
    }
    auto* next_root = snapshot.get();
    std::unique_ptr<ArenaSnapshot> old_root;
    {
      std::scoped_lock lock(owner_mutex_);
      old_root = std::move(current_);
      current_ = std::move(snapshot);
      root_.store(next_root, std::memory_order_release);
    }
    retire(std::move(old_root));
  }

  void collect_retired_without_readers() {
#if !defined(GRAPHIFY_HAS_FOLLY)
    std::scoped_lock lock(retired_mutex_);
    retired_.clear();
#endif
  }

 private:
  void retire(std::unique_ptr<ArenaSnapshot> old_root) {
    if (!old_root) {
      return;
    }
#if defined(GRAPHIFY_HAS_FOLLY)
    old_root.release()->retire();
#else
    std::scoped_lock lock(retired_mutex_);
    retired_.push_back(std::move(old_root));
#endif
  }

  mutable std::atomic<ArenaSnapshot*> root_{nullptr};
  mutable std::mutex owner_mutex_{};
  std::unique_ptr<ArenaSnapshot> current_{};
  std::mutex retired_mutex_{};
  std::vector<std::unique_ptr<ArenaSnapshot>> retired_{};
};

} // namespace graphify
