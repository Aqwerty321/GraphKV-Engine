#include "TelemetryRingBuffer.hpp"

#include <cassert>
#include <cstdint>
#include <type_traits>

int main() {
  using namespace graphify;

  static_assert(std::is_trivially_copyable_v<IngestMetric>);

  TelemetryRingBuffer<IngestMetric, 4> ring;
  assert(ring.empty());
  assert(ring.try_push(IngestMetric{.query_id = 1}));
  assert(ring.try_push(IngestMetric{.query_id = 2}));
  assert(ring.try_push(IngestMetric{.query_id = 3}));
  assert(!ring.try_push(IngestMetric{.query_id = 4}));
  assert(ring.dropped_count() == 1);

  IngestMetric metric;
  assert(ring.try_pop(metric));
  assert(metric.query_id == 1);
  assert(ring.try_pop(metric));
  assert(metric.query_id == 2);
  assert(ring.try_push(IngestMetric{.query_id = 5}));

  std::uint64_t sum = 0;
  const auto popped = ring.pop_all([&](const IngestMetric& value) { sum += value.query_id; });
  assert(popped == 2);
  assert(sum == 8);
  assert(ring.empty());

  return 0;
}
