#include "ScipIngestor.hpp"

#include <cassert>
#include <cstddef>
#include <cstdint>
#include <initializer_list>
#include <span>
#include <string_view>
#include <vector>

namespace {

void append_varint(std::vector<std::byte>& out, std::uint64_t value) {
  while (value >= 0x80U) {
    out.push_back(static_cast<std::byte>((value & 0x7FU) | 0x80U));
    value >>= 7U;
  }
  out.push_back(static_cast<std::byte>(value));
}

void append_key(std::vector<std::byte>& out, std::uint32_t field_number, std::uint32_t wire_type) {
  append_varint(out, (static_cast<std::uint64_t>(field_number) << 3U) | wire_type);
}

void append_varint_field(std::vector<std::byte>& out, std::uint32_t field_number, std::uint64_t value) {
  append_key(out, field_number, 0);
  append_varint(out, value);
}

void append_bytes_field(std::vector<std::byte>& out, std::uint32_t field_number, std::string_view value) {
  append_key(out, field_number, 2);
  append_varint(out, value.size());
  for (char character : value) {
    out.push_back(static_cast<std::byte>(static_cast<unsigned char>(character)));
  }
}

void append_message_field(std::vector<std::byte>& out, std::uint32_t field_number, const std::vector<std::byte>& value) {
  append_key(out, field_number, 2);
  append_varint(out, value.size());
  out.insert(out.end(), value.begin(), value.end());
}

void append_packed_int32_field(std::vector<std::byte>& out, std::uint32_t field_number, std::initializer_list<std::uint32_t> values) {
  std::vector<std::byte> packed;
  for (auto value : values) {
    append_varint(packed, value);
  }
  append_message_field(out, field_number, packed);
}

std::vector<std::byte> build_minimal_scip_index() {
  std::vector<std::byte> relationship;
  append_bytes_field(relationship, 1, "cpp target#");
  append_varint_field(relationship, 2, 1);

  std::vector<std::byte> symbol;
  append_bytes_field(symbol, 1, "local 1");
  append_bytes_field(symbol, 3, "documentation");
  append_message_field(symbol, 4, relationship);
  append_varint_field(symbol, 5, 17);
  append_bytes_field(symbol, 6, "foo");

  std::vector<std::byte> occurrence;
  append_packed_int32_field(occurrence, 1, {1, 2, 3, 4});
  append_bytes_field(occurrence, 2, "local 1");
  append_varint_field(occurrence, 3, 1);
  append_varint_field(occurrence, 5, 6);

  std::vector<std::byte> document;
  append_bytes_field(document, 1, "src/a.cpp");
  append_message_field(document, 2, occurrence);
  append_message_field(document, 3, symbol);
  append_bytes_field(document, 4, "cpp");
  append_varint_field(document, 6, 1);

  std::vector<std::byte> index;
  append_message_field(index, 2, document);
  return index;
}

} // namespace

int main() {
  using namespace graphify;

  auto payload = build_minimal_scip_index();
  ScipIngestor ingestor(IngestOptions{.max_payload_bytes = 4096, .mode = IngestMode::strict_wire_scanner});
  auto result = ingestor.ingest(std::span<const std::byte>(payload.data(), payload.size()));
  assert(result.has_value());

  const auto& summary = result->summary;
  assert(summary.counts.documents == 1);
  assert(summary.counts.symbols == 1);
  assert(summary.counts.relationships == 1);
  assert(summary.counts.occurrences == 1);
  assert(summary.mode == IngestMode::strict_wire_scanner);

  const auto& snapshot = *result->snapshot;
  assert(snapshot.resolve(snapshot.documents()[0].relative_path) == std::string_view("src/a.cpp"));
  assert(snapshot.resolve(snapshot.documents()[0].language) == std::string_view("cpp"));
  assert(snapshot.resolve(snapshot.symbols()[0].symbol) == std::string_view("local 1"));
  assert(snapshot.resolve(snapshot.symbols()[0].display_name) == std::string_view("foo"));
  assert(snapshot.resolve(snapshot.symbols()[0].documentation) == std::string_view("documentation"));
  assert(snapshot.resolve(snapshot.relationships()[0].target_symbol) == std::string_view("cpp target#"));
  assert(snapshot.resolve(snapshot.occurrences()[0].symbol) == std::string_view("local 1"));
  assert(snapshot.occurrences()[0].start_line == 1);
  assert(snapshot.occurrences()[0].start_character == 2);
  assert(snapshot.occurrences()[0].end_line == 3);
  assert(snapshot.occurrences()[0].end_character == 4);

  auto empty = ingestor.ingest({});
  assert(!empty.has_value());
  assert(empty.error().code == IngestErrorCode::empty_payload);

  std::byte truncated_varint[] = {std::byte{0x80}};
  auto malformed = ingestor.ingest(std::span<const std::byte>(truncated_varint, 1));
  assert(!malformed.has_value());
  assert(malformed.error().code == IngestErrorCode::malformed_protobuf);

  return 0;
}
