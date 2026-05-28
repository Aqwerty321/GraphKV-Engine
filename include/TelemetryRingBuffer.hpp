#pragma once

#include <array>
#include <atomic>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <type_traits>
#include <utility>

namespace graphify {

#if defined(__cpp_lib_hardware_interference_size)
inline constexpr std::size_t telemetry_cache_line_size = std::hardware_destructive_interference_size;
#else
inline constexpr std::size_t telemetry_cache_line_size = 64;
#endif

struct IngestMetric final {
  std::uint64_t timestamp_ns{};
  std::uint64_t query_id{};
  std::uint64_t tenant_id{};
  std::uint64_t payload_size{};
  std::uint64_t compressed_payload_size{};
  std::uint64_t uncompressed_payload_size{};
  std::uint64_t latency_us{};
  std::uint16_t route_kind{};
  std::uint16_t error_code{};
  std::uint32_t flags{};
};

static_assert(std::is_trivially_copyable_v<IngestMetric>);

template <typename T, std::size_t Capacity>
class TelemetryRingBuffer final {
 public:
  static_assert(Capacity >= 2);
  static_assert(std::has_single_bit(Capacity));
  static_assert(std::is_trivially_copyable_v<T>);

  [[nodiscard]] bool try_push(const T& item) noexcept {
    const auto head = producer_.index.load(std::memory_order_relaxed);
    const auto next = increment(head);
    if (next == consumer_.index.load(std::memory_order_acquire)) {
      dropped_.fetch_add(1, std::memory_order_relaxed);
      return false;
    }
    slots_[head] = item;
    producer_.index.store(next, std::memory_order_release);
    return true;
  }

  [[nodiscard]] bool try_pop(T& item) noexcept {
    const auto tail = consumer_.index.load(std::memory_order_relaxed);
    if (tail == producer_.index.load(std::memory_order_acquire)) {
      return false;
    }
    item = slots_[tail];
    consumer_.index.store(increment(tail), std::memory_order_release);
    return true;
  }

  template <typename Callback>
  std::size_t pop_all(Callback&& callback) {
    std::size_t popped = 0;
    T item{};
    while (try_pop(item)) {
      std::forward<Callback>(callback)(item);
      ++popped;
    }
    return popped;
  }

  [[nodiscard]] std::uint64_t dropped_count() const noexcept {
    return dropped_.load(std::memory_order_relaxed);
  }

  [[nodiscard]] bool empty() const noexcept {
    return consumer_.index.load(std::memory_order_relaxed) == producer_.index.load(std::memory_order_acquire);
  }

 private:
  struct alignas(telemetry_cache_line_size) Cursor final {
    std::atomic<std::size_t> index{};
  };

  [[nodiscard]] static constexpr std::size_t increment(std::size_t value) noexcept {
    return (value + 1U) & (Capacity - 1U);
  }

  std::array<T, Capacity> slots_{};
  Cursor producer_{};
  Cursor consumer_{};
  alignas(telemetry_cache_line_size) std::atomic<std::uint64_t> dropped_{};
};

} // namespace graphify
