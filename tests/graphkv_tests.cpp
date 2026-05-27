#include "GraphKV.hpp"

#include <cassert>
#include <cstdint>
#include <string_view>

int main() {
  using namespace graphify;

  GraphCounts counts{
      .documents = 1,
      .symbols = 2,
      .relationships = 1,
      .occurrences = 1,
      .string_bytes = 12,
  };

  auto snapshot = GraphArena::allocate(counts);
  assert(snapshot.has_value());
  assert((*snapshot)->arena_bytes() >= cache_line_size);
  assert(reinterpret_cast<std::uintptr_t>((*snapshot)->documents().data()) % alignof(DocumentRecord) == 0);
  assert(reinterpret_cast<std::uintptr_t>((*snapshot)->symbols().data()) % alignof(SymbolNode) == 0);
  assert(reinterpret_cast<std::uintptr_t>((*snapshot)->relationships().data()) % alignof(RelationshipEdge) == 0);
  assert(reinterpret_cast<std::uintptr_t>((*snapshot)->occurrences().data()) % alignof(OccurrenceRecord) == 0);

  auto first = (*snapshot)->append_string("hello");
  assert(first.has_value());
  auto second = (*snapshot)->append_string(" world");
  assert(second.has_value());
  assert((*snapshot)->resolve(*first) == std::string_view("hello"));
  assert((*snapshot)->resolve(*second) == std::string_view(" world"));
  assert((*snapshot)->string_bytes_used() == 11);

  auto overflow = (*snapshot)->append_string("!!");
  assert(!overflow.has_value());

  SnapshotPublisher publisher;
  publisher.publish(std::move(*snapshot));
  assert(publisher.load() != nullptr);
  auto read_handle = publisher.acquire();
  assert(read_handle);
  assert(read_handle->counts().symbols == 2);
  publisher.collect_retired_without_readers();

  return 0;
}
