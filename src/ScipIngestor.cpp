#include "ScipIngestor.hpp"

#include <array>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <optional>
#include <string_view>
#include <utility>

#if __has_include(<gsl/gsl>)
#include <gsl/gsl>
#endif

#ifndef Expects
#define Expects(condition)                                                                           \
  do {                                                                                               \
    if (!(condition)) {                                                                              \
      std::abort();                                                                                  \
    }                                                                                                \
  } while (false)
#endif

#ifndef Ensures
#define Ensures(condition) Expects(condition)
#endif

#if defined(GRAPHIFY_HAS_PROTOBUF_COMPAT)
#include <google/protobuf/arena.h>
#include "scip.pb.h"
#endif

namespace graphify {
namespace {

enum class WireType : std::uint8_t {
  varint = 0,
  fixed64 = 1,
  length_delimited = 2,
  fixed32 = 5,
};

enum RelationshipFlags : std::uint32_t {
  relationship_reference = 1U << 0U,
  relationship_implementation = 1U << 1U,
  relationship_type_definition = 1U << 2U,
  relationship_definition = 1U << 3U,
};

struct Field final {
  std::uint32_t number{};
  WireType wire_type{};
  std::uint64_t varint{};
  std::span<const std::byte> bytes{};
  std::size_t offset{};
};

[[nodiscard]] IngestError make_error(IngestErrorCode code, std::string message, std::size_t offset = 0) {
  return IngestError{code, std::move(message), offset};
}

[[nodiscard]] std::string_view bytes_as_string(std::span<const std::byte> bytes) noexcept {
  return {reinterpret_cast<const char*>(bytes.data()), bytes.size()};
}

[[nodiscard]] Expected<std::uint32_t, IngestError> checked_u32(std::uint64_t value, std::string_view field_name, std::size_t offset) {
  if (value > std::numeric_limits<std::uint32_t>::max()) {
    return unexpected(make_error(IngestErrorCode::count_overflow, std::string(field_name) + " exceeds uint32_t", offset));
  }
  return static_cast<std::uint32_t>(value);
}

class WireReader final {
 public:
  explicit WireReader(std::span<const std::byte> bytes) : bytes_(bytes) {}

  [[nodiscard]] std::size_t offset() const noexcept { return offset_; }

  [[nodiscard]] Expected<std::optional<Field>, IngestError> next() {
    if (offset_ == bytes_.size()) {
      return std::optional<Field>{};
    }

    const auto field_offset = offset_;
    auto key = read_varint();
    if (!key) {
      return unexpected(key.error());
    }
    if (*key == 0) {
      return unexpected(make_error(IngestErrorCode::malformed_protobuf, "protobuf field key cannot be zero", field_offset));
    }

    const auto field_number = static_cast<std::uint32_t>(*key >> 3U);
    const auto wire_value = static_cast<std::uint8_t>(*key & 0x7U);
    Field field{.number = field_number, .offset = field_offset};

    switch (wire_value) {
      case 0: {
        field.wire_type = WireType::varint;
        auto value = read_varint();
        if (!value) {
          return unexpected(value.error());
        }
        field.varint = *value;
        return field;
      }
      case 1:
        field.wire_type = WireType::fixed64;
        if (!skip_bytes(8)) {
          return unexpected(make_error(IngestErrorCode::malformed_protobuf, "truncated fixed64 field", field_offset));
        }
        return field;
      case 2: {
        field.wire_type = WireType::length_delimited;
        auto value = read_length_delimited();
        if (!value) {
          return unexpected(value.error());
        }
        field.bytes = *value;
        return field;
      }
      case 5:
        field.wire_type = WireType::fixed32;
        if (!skip_bytes(4)) {
          return unexpected(make_error(IngestErrorCode::malformed_protobuf, "truncated fixed32 field", field_offset));
        }
        return field;
      default:
        return unexpected(make_error(IngestErrorCode::malformed_protobuf, "protobuf groups are not supported by SCIP", field_offset));
    }
  }

 private:
  [[nodiscard]] bool skip_bytes(std::size_t byte_count) noexcept {
    if (byte_count > bytes_.size() - offset_) {
      return false;
    }
    offset_ += byte_count;
    return true;
  }

  [[nodiscard]] Expected<std::uint64_t, IngestError> read_varint() {
    std::uint64_t value = 0;
    for (std::uint32_t shift = 0; shift <= 63; shift += 7) {
      if (offset_ == bytes_.size()) {
        return unexpected(make_error(IngestErrorCode::malformed_protobuf, "truncated varint", offset_));
      }
      const auto byte = static_cast<std::uint8_t>(bytes_[offset_]);
      ++offset_;
      value |= static_cast<std::uint64_t>(byte & 0x7FU) << shift;
      if ((byte & 0x80U) == 0) {
        return value;
      }
    }
    return unexpected(make_error(IngestErrorCode::malformed_protobuf, "varint is too long", offset_));
  }

  [[nodiscard]] Expected<std::span<const std::byte>, IngestError> read_length_delimited() {
    const auto size_offset = offset_;
    auto size = read_varint();
    if (!size) {
      return unexpected(size.error());
    }
    if (*size > bytes_.size() - offset_) {
      return unexpected(make_error(IngestErrorCode::malformed_protobuf, "truncated length-delimited field", size_offset));
    }
    const auto length = static_cast<std::size_t>(*size);
    auto result = bytes_.subspan(offset_, length);
    offset_ += length;
    return result;
  }

  std::span<const std::byte> bytes_{};
  std::size_t offset_{};
};

struct CountState final {
  std::uint64_t documents{};
  std::uint64_t symbols{};
  std::uint64_t relationships{};
  std::uint64_t occurrences{};
  std::uint64_t string_bytes{};

  [[nodiscard]] Expected<GraphCounts, IngestError> finalize() const {
    if (documents > std::numeric_limits<std::uint32_t>::max() ||
        symbols > std::numeric_limits<std::uint32_t>::max() ||
        relationships > std::numeric_limits<std::uint32_t>::max() ||
        occurrences > std::numeric_limits<std::uint32_t>::max() ||
        string_bytes > std::numeric_limits<std::uint32_t>::max()) {
      return unexpected(make_error(IngestErrorCode::count_overflow, "SCIP payload exceeds GraphKV uint32_t limits"));
    }
    return GraphCounts{
        .documents = static_cast<std::uint32_t>(documents),
        .symbols = static_cast<std::uint32_t>(symbols),
        .relationships = static_cast<std::uint32_t>(relationships),
        .occurrences = static_cast<std::uint32_t>(occurrences),
        .string_bytes = static_cast<std::uint32_t>(string_bytes),
    };
  }
};

[[nodiscard]] Expected<void, IngestError> add_count(std::uint64_t& target, std::uint64_t value) {
  if (target > std::numeric_limits<std::uint64_t>::max() - value) {
    return unexpected(make_error(IngestErrorCode::count_overflow, "counter overflow while scanning SCIP payload"));
  }
  target += value;
  return {};
}

[[nodiscard]] Expected<void, IngestError> add_string_bytes(CountState& counts, std::span<const std::byte> bytes) {
  return add_count(counts.string_bytes, bytes.size());
}

[[nodiscard]] Expected<void, IngestError> scan_symbol_counts(std::span<const std::byte> bytes, CountState& counts);
[[nodiscard]] Expected<void, IngestError> scan_occurrence_counts(std::span<const std::byte> bytes, CountState& counts);

[[nodiscard]] Expected<void, IngestError> scan_relationship_counts(std::span<const std::byte> bytes, CountState& counts) {
  WireReader reader(bytes);
  while (true) {
    auto next = reader.next();
    if (!next) {
      return unexpected(next.error());
    }
    if (!*next) {
      return {};
    }
    const auto& field = **next;
    if (field.number == 1 && field.wire_type == WireType::length_delimited) {
      auto added = add_string_bytes(counts, field.bytes);
      if (!added) {
        return unexpected(added.error());
      }
    }
  }
}

[[nodiscard]] Expected<void, IngestError> scan_symbol_counts(std::span<const std::byte> bytes, CountState& counts) {
  bool captured_documentation = false;
  WireReader reader(bytes);
  while (true) {
    auto next = reader.next();
    if (!next) {
      return unexpected(next.error());
    }
    if (!*next) {
      return {};
    }
    const auto& field = **next;
    if ((field.number == 1 || field.number == 6) && field.wire_type == WireType::length_delimited) {
      auto added = add_string_bytes(counts, field.bytes);
      if (!added) {
        return unexpected(added.error());
      }
    } else if (field.number == 3 && field.wire_type == WireType::length_delimited && !captured_documentation) {
      captured_documentation = true;
      auto added = add_string_bytes(counts, field.bytes);
      if (!added) {
        return unexpected(added.error());
      }
    } else if (field.number == 4 && field.wire_type == WireType::length_delimited) {
      auto relationship_count = add_count(counts.relationships, 1);
      if (!relationship_count) {
        return unexpected(relationship_count.error());
      }
      auto scanned = scan_relationship_counts(field.bytes, counts);
      if (!scanned) {
        return unexpected(scanned.error());
      }
    }
  }
}

[[nodiscard]] Expected<void, IngestError> scan_occurrence_counts(std::span<const std::byte> bytes, CountState& counts) {
  WireReader reader(bytes);
  while (true) {
    auto next = reader.next();
    if (!next) {
      return unexpected(next.error());
    }
    if (!*next) {
      return {};
    }
    const auto& field = **next;
    if (field.number == 2 && field.wire_type == WireType::length_delimited) {
      auto added = add_string_bytes(counts, field.bytes);
      if (!added) {
        return unexpected(added.error());
      }
    }
  }
}

[[nodiscard]] Expected<void, IngestError> scan_document_counts(std::span<const std::byte> bytes, CountState& counts) {
  WireReader reader(bytes);
  while (true) {
    auto next = reader.next();
    if (!next) {
      return unexpected(next.error());
    }
    if (!*next) {
      return {};
    }
    const auto& field = **next;
    if ((field.number == 1 || field.number == 4) && field.wire_type == WireType::length_delimited) {
      auto added = add_string_bytes(counts, field.bytes);
      if (!added) {
        return unexpected(added.error());
      }
    } else if (field.number == 2 && field.wire_type == WireType::length_delimited) {
      auto occurrence_count = add_count(counts.occurrences, 1);
      if (!occurrence_count) {
        return unexpected(occurrence_count.error());
      }
      auto scanned = scan_occurrence_counts(field.bytes, counts);
      if (!scanned) {
        return unexpected(scanned.error());
      }
    } else if (field.number == 3 && field.wire_type == WireType::length_delimited) {
      auto symbol_count = add_count(counts.symbols, 1);
      if (!symbol_count) {
        return unexpected(symbol_count.error());
      }
      auto scanned = scan_symbol_counts(field.bytes, counts);
      if (!scanned) {
        return unexpected(scanned.error());
      }
    }
  }
}

[[nodiscard]] Expected<GraphCounts, IngestError> scan_index_counts(std::span<const std::byte> bytes) {
  CountState counts;
  WireReader reader(bytes);
  while (true) {
    auto next = reader.next();
    if (!next) {
      return unexpected(next.error());
    }
    if (!*next) {
      return counts.finalize();
    }
    const auto& field = **next;
    if (field.number == 2 && field.wire_type == WireType::length_delimited) {
      auto document_count = add_count(counts.documents, 1);
      if (!document_count) {
        return unexpected(document_count.error());
      }
      auto scanned = scan_document_counts(field.bytes, counts);
      if (!scanned) {
        return unexpected(scanned.error());
      }
    } else if (field.number == 3 && field.wire_type == WireType::length_delimited) {
      auto symbol_count = add_count(counts.symbols, 1);
      if (!symbol_count) {
        return unexpected(symbol_count.error());
      }
      auto scanned = scan_symbol_counts(field.bytes, counts);
      if (!scanned) {
        return unexpected(scanned.error());
      }
    }
  }
}

struct FillState final {
  ArenaSnapshot& snapshot;
  std::uint32_t document_cursor{};
  std::uint32_t symbol_cursor{};
  std::uint32_t relationship_cursor{};
  std::uint32_t occurrence_cursor{};
};

[[nodiscard]] Expected<StringRef, IngestError> append_snapshot_string(FillState& state, std::span<const std::byte> bytes) {
  auto appended = state.snapshot.append_string(bytes_as_string(bytes));
  if (!appended) {
    return unexpected(make_error(IngestErrorCode::arena_allocation_failed, appended.error().message));
  }
  return *appended;
}

struct RangeAccumulator final {
  std::array<std::uint32_t, 4> values{};
  std::uint32_t count{};

  [[nodiscard]] Expected<void, IngestError> add(std::uint64_t raw_value, std::size_t offset) {
    const auto signed_value = static_cast<std::int32_t>(raw_value);
    if (signed_value < 0) {
      return unexpected(make_error(IngestErrorCode::malformed_protobuf, "SCIP ranges cannot contain negative coordinates", offset));
    }
    if (count >= values.size()) {
      return unexpected(make_error(IngestErrorCode::malformed_protobuf, "SCIP occurrence range has more than four elements", offset));
    }
    values[count] = static_cast<std::uint32_t>(signed_value);
    ++count;
    return {};
  }

  [[nodiscard]] Expected<void, IngestError> apply(OccurrenceRecord& occurrence) const {
    if (count == 0) {
      return {};
    }
    if (count != 3 && count != 4) {
      return unexpected(make_error(IngestErrorCode::malformed_protobuf, "SCIP occurrence range must contain three or four elements"));
    }
    occurrence.start_line = values[0];
    occurrence.start_character = values[1];
    if (count == 3) {
      occurrence.end_line = values[0];
      occurrence.end_character = values[2];
    } else {
      occurrence.end_line = values[2];
      occurrence.end_character = values[3];
    }
    return {};
  }
};

[[nodiscard]] Expected<void, IngestError> parse_packed_range(std::span<const std::byte> bytes, RangeAccumulator& range, std::size_t base_offset) {
  std::size_t offset = 0;
  while (offset < bytes.size()) {
    std::uint64_t value = 0;
    bool completed = false;
    const auto value_offset = offset;
    for (std::uint32_t shift = 0; shift <= 63; shift += 7) {
      if (offset == bytes.size()) {
        return unexpected(make_error(IngestErrorCode::malformed_protobuf, "truncated packed range varint", base_offset + value_offset));
      }
      const auto byte = static_cast<std::uint8_t>(bytes[offset]);
      ++offset;
      value |= static_cast<std::uint64_t>(byte & 0x7FU) << shift;
      if ((byte & 0x80U) == 0) {
        completed = true;
        break;
      }
    }
    if (!completed) {
      return unexpected(make_error(IngestErrorCode::malformed_protobuf, "packed range varint is too long", base_offset + value_offset));
    }
    auto added = range.add(value, base_offset + value_offset);
    if (!added) {
      return unexpected(added.error());
    }
  }
  return {};
}

[[nodiscard]] Expected<void, IngestError> fill_symbol(std::span<const std::byte> bytes, FillState& state, std::uint32_t document_index);
[[nodiscard]] Expected<void, IngestError> fill_occurrence(std::span<const std::byte> bytes, FillState& state, std::uint32_t document_index);

[[nodiscard]] Expected<void, IngestError> fill_relationship(std::span<const std::byte> bytes, FillState& state, std::uint32_t source_symbol) {
  if (state.relationship_cursor >= state.snapshot.counts().relationships) {
    return unexpected(make_error(IngestErrorCode::arena_allocation_failed, "relationship cursor exceeded allocated GraphKV capacity"));
  }
  auto& edge = state.snapshot.relationships()[state.relationship_cursor++];
  edge.source_symbol = source_symbol;

  WireReader reader(bytes);
  while (true) {
    auto next = reader.next();
    if (!next) {
      return unexpected(next.error());
    }
    if (!*next) {
      return {};
    }
    const auto& field = **next;
    if (field.number == 1 && field.wire_type == WireType::length_delimited) {
      auto target = append_snapshot_string(state, field.bytes);
      if (!target) {
        return unexpected(target.error());
      }
      edge.target_symbol = *target;
    } else if (field.wire_type == WireType::varint) {
      if (field.number == 2 && field.varint != 0) {
        edge.flags |= relationship_reference;
      } else if (field.number == 3 && field.varint != 0) {
        edge.flags |= relationship_implementation;
      } else if (field.number == 4 && field.varint != 0) {
        edge.flags |= relationship_type_definition;
      } else if (field.number == 5 && field.varint != 0) {
        edge.flags |= relationship_definition;
      }
    }
  }
}

[[nodiscard]] Expected<void, IngestError> fill_symbol(std::span<const std::byte> bytes, FillState& state, std::uint32_t document_index) {
  if (state.symbol_cursor >= state.snapshot.counts().symbols) {
    return unexpected(make_error(IngestErrorCode::arena_allocation_failed, "symbol cursor exceeded allocated GraphKV capacity"));
  }

  const auto symbol_index = state.symbol_cursor++;
  auto& symbol = state.snapshot.symbols()[symbol_index];
  symbol.document_index = document_index;
  symbol.first_edge = state.relationship_cursor;

  bool captured_documentation = false;
  WireReader reader(bytes);
  while (true) {
    auto next = reader.next();
    if (!next) {
      return unexpected(next.error());
    }
    if (!*next) {
      symbol.edge_count = state.relationship_cursor - symbol.first_edge;
      return {};
    }
    const auto& field = **next;
    if (field.number == 1 && field.wire_type == WireType::length_delimited) {
      auto value = append_snapshot_string(state, field.bytes);
      if (!value) {
        return unexpected(value.error());
      }
      symbol.symbol = *value;
    } else if (field.number == 3 && field.wire_type == WireType::length_delimited && !captured_documentation) {
      captured_documentation = true;
      auto value = append_snapshot_string(state, field.bytes);
      if (!value) {
        return unexpected(value.error());
      }
      symbol.documentation = *value;
    } else if (field.number == 4 && field.wire_type == WireType::length_delimited) {
      auto relationship = fill_relationship(field.bytes, state, symbol_index);
      if (!relationship) {
        return unexpected(relationship.error());
      }
    } else if (field.number == 5 && field.wire_type == WireType::varint) {
      auto kind = checked_u32(field.varint, "SymbolInformation.kind", field.offset);
      if (!kind) {
        return unexpected(kind.error());
      }
      symbol.kind = *kind;
    } else if (field.number == 6 && field.wire_type == WireType::length_delimited) {
      auto value = append_snapshot_string(state, field.bytes);
      if (!value) {
        return unexpected(value.error());
      }
      symbol.display_name = *value;
    }
  }
}

[[nodiscard]] Expected<void, IngestError> fill_occurrence(std::span<const std::byte> bytes, FillState& state, std::uint32_t document_index) {
  if (state.occurrence_cursor >= state.snapshot.counts().occurrences) {
    return unexpected(make_error(IngestErrorCode::arena_allocation_failed, "occurrence cursor exceeded allocated GraphKV capacity"));
  }

  auto& occurrence = state.snapshot.occurrences()[state.occurrence_cursor++];
  occurrence.document_index = document_index;
  RangeAccumulator range;
  WireReader reader(bytes);

  while (true) {
    auto next = reader.next();
    if (!next) {
      return unexpected(next.error());
    }
    if (!*next) {
      return range.apply(occurrence);
    }
    const auto& field = **next;
    if (field.number == 1 && field.wire_type == WireType::varint) {
      auto added = range.add(field.varint, field.offset);
      if (!added) {
        return unexpected(added.error());
      }
    } else if (field.number == 1 && field.wire_type == WireType::length_delimited) {
      auto packed = parse_packed_range(field.bytes, range, field.offset);
      if (!packed) {
        return unexpected(packed.error());
      }
    } else if (field.number == 2 && field.wire_type == WireType::length_delimited) {
      auto value = append_snapshot_string(state, field.bytes);
      if (!value) {
        return unexpected(value.error());
      }
      occurrence.symbol = *value;
    } else if (field.number == 3 && field.wire_type == WireType::varint) {
      auto roles = checked_u32(field.varint, "Occurrence.symbol_roles", field.offset);
      if (!roles) {
        return unexpected(roles.error());
      }
      occurrence.symbol_roles = *roles;
    } else if (field.number == 5 && field.wire_type == WireType::varint) {
      auto syntax_kind = checked_u32(field.varint, "Occurrence.syntax_kind", field.offset);
      if (!syntax_kind) {
        return unexpected(syntax_kind.error());
      }
      occurrence.syntax_kind = *syntax_kind;
    }
  }
}

[[nodiscard]] Expected<void, IngestError> fill_document(std::span<const std::byte> bytes, FillState& state) {
  if (state.document_cursor >= state.snapshot.counts().documents) {
    return unexpected(make_error(IngestErrorCode::arena_allocation_failed, "document cursor exceeded allocated GraphKV capacity"));
  }

  const auto document_index = state.document_cursor++;
  auto& document = state.snapshot.documents()[document_index];
  document.first_symbol = state.symbol_cursor;
  document.first_occurrence = state.occurrence_cursor;

  WireReader reader(bytes);
  while (true) {
    auto next = reader.next();
    if (!next) {
      return unexpected(next.error());
    }
    if (!*next) {
      document.symbol_count = state.symbol_cursor - document.first_symbol;
      document.occurrence_count = state.occurrence_cursor - document.first_occurrence;
      return {};
    }
    const auto& field = **next;
    if (field.number == 1 && field.wire_type == WireType::length_delimited) {
      auto value = append_snapshot_string(state, field.bytes);
      if (!value) {
        return unexpected(value.error());
      }
      document.relative_path = *value;
    } else if (field.number == 2 && field.wire_type == WireType::length_delimited) {
      auto occurrence = fill_occurrence(field.bytes, state, document_index);
      if (!occurrence) {
        return unexpected(occurrence.error());
      }
    } else if (field.number == 3 && field.wire_type == WireType::length_delimited) {
      auto symbol = fill_symbol(field.bytes, state, document_index);
      if (!symbol) {
        return unexpected(symbol.error());
      }
    } else if (field.number == 4 && field.wire_type == WireType::length_delimited) {
      auto value = append_snapshot_string(state, field.bytes);
      if (!value) {
        return unexpected(value.error());
      }
      document.language = *value;
    } else if (field.number == 6 && field.wire_type == WireType::varint) {
      auto encoding = checked_u32(field.varint, "Document.position_encoding", field.offset);
      if (!encoding) {
        return unexpected(encoding.error());
      }
      document.position_encoding = *encoding;
    }
  }
}

[[nodiscard]] Expected<void, IngestError> fill_index(std::span<const std::byte> bytes, ArenaSnapshot& snapshot) {
  FillState state{.snapshot = snapshot};
  WireReader reader(bytes);
  while (true) {
    auto next = reader.next();
    if (!next) {
      return unexpected(next.error());
    }
    if (!*next) {
      Ensures(state.document_cursor == snapshot.counts().documents);
      Ensures(state.symbol_cursor == snapshot.counts().symbols);
      Ensures(state.relationship_cursor == snapshot.counts().relationships);
      Ensures(state.occurrence_cursor == snapshot.counts().occurrences);
      Ensures(snapshot.string_bytes_used() == snapshot.counts().string_bytes);
      return {};
    }
    const auto& field = **next;
    if (field.number == 2 && field.wire_type == WireType::length_delimited) {
      auto document = fill_document(field.bytes, state);
      if (!document) {
        return unexpected(document.error());
      }
    } else if (field.number == 3 && field.wire_type == WireType::length_delimited) {
      auto symbol = fill_symbol(field.bytes, state, external_document_index);
      if (!symbol) {
        return unexpected(symbol.error());
      }
    }
  }
}

[[nodiscard]] Expected<IngestResult, IngestError> build_from_wire(std::span<const std::byte> payload) {
  auto counts = scan_index_counts(payload);
  if (!counts) {
    return unexpected(counts.error());
  }

  auto snapshot = GraphArena::allocate(*counts);
  if (!snapshot) {
    return unexpected(make_error(IngestErrorCode::arena_allocation_failed, snapshot.error().message));
  }

  auto filled = fill_index(payload, **snapshot);
  if (!filled) {
    return unexpected(filled.error());
  }

  IngestSummary summary{
      .counts = (*snapshot)->counts(),
      .arena_bytes = (*snapshot)->arena_bytes(),
      .string_bytes_used = (*snapshot)->string_bytes_used(),
      .mode = IngestMode::strict_wire_scanner,
  };
  return IngestResult{.snapshot = std::move(*snapshot), .summary = summary};
}

#if defined(GRAPHIFY_HAS_PROTOBUF_COMPAT)
[[nodiscard]] Expected<void, IngestError> add_generated_string(CountState& counts, const std::string& value) {
  const auto bytes = std::as_bytes(std::span<const char>(value.data(), value.size()));
  return add_string_bytes(counts, bytes);
}

[[nodiscard]] Expected<void, IngestError> count_generated_symbol(const scip::SymbolInformation& symbol, CountState& counts) {
  auto add_symbol_string = add_generated_string(counts, symbol.symbol());
  if (!add_symbol_string) {
    return unexpected(add_symbol_string.error());
  }
  if (!symbol.documentation().empty()) {
    auto documentation = add_generated_string(counts, symbol.documentation(0));
    if (!documentation) {
      return unexpected(documentation.error());
    }
  }
  auto display_name = add_generated_string(counts, symbol.display_name());
  if (!display_name) {
    return unexpected(display_name.error());
  }
  for (const auto& relationship : symbol.relationships()) {
    auto relationship_count = add_count(counts.relationships, 1);
    if (!relationship_count) {
      return unexpected(relationship_count.error());
    }
    auto relationship_symbol = add_generated_string(counts, relationship.symbol());
    if (!relationship_symbol) {
      return unexpected(relationship_symbol.error());
    }
  }
  return {};
}

[[nodiscard]] Expected<GraphCounts, IngestError> count_generated_index(const scip::Index& index) {
  CountState counts;
  auto document_count = add_count(counts.documents, static_cast<std::uint64_t>(index.documents_size()));
  if (!document_count) {
    return unexpected(document_count.error());
  }
  auto external_count = add_count(counts.symbols, static_cast<std::uint64_t>(index.external_symbols_size()));
  if (!external_count) {
    return unexpected(external_count.error());
  }

  for (const auto& document : index.documents()) {
    auto relative_path = add_generated_string(counts, document.relative_path());
    if (!relative_path) {
      return unexpected(relative_path.error());
    }
    auto language = add_generated_string(counts, document.language());
    if (!language) {
      return unexpected(language.error());
    }
    auto occurrence_count = add_count(counts.occurrences, static_cast<std::uint64_t>(document.occurrences_size()));
    if (!occurrence_count) {
      return unexpected(occurrence_count.error());
    }
    auto symbol_count = add_count(counts.symbols, static_cast<std::uint64_t>(document.symbols_size()));
    if (!symbol_count) {
      return unexpected(symbol_count.error());
    }
    for (const auto& occurrence : document.occurrences()) {
      auto symbol = add_generated_string(counts, occurrence.symbol());
      if (!symbol) {
        return unexpected(symbol.error());
      }
    }
    for (const auto& symbol : document.symbols()) {
      auto counted = count_generated_symbol(symbol, counts);
      if (!counted) {
        return unexpected(counted.error());
      }
    }
  }

  for (const auto& symbol : index.external_symbols()) {
    auto counted = count_generated_symbol(symbol, counts);
    if (!counted) {
      return unexpected(counted.error());
    }
  }

  return counts.finalize();
}

[[nodiscard]] Expected<StringRef, IngestError> append_generated_string(FillState& state, const std::string& value) {
  const auto bytes = std::as_bytes(std::span<const char>(value.data(), value.size()));
  return append_snapshot_string(state, bytes);
}

[[nodiscard]] Expected<void, IngestError> fill_generated_symbol(const scip::SymbolInformation& source, FillState& state, std::uint32_t document_index) {
  if (state.symbol_cursor >= state.snapshot.counts().symbols) {
    return unexpected(make_error(IngestErrorCode::arena_allocation_failed, "generated symbol cursor exceeded capacity"));
  }
  const auto symbol_index = state.symbol_cursor++;
  auto& target = state.snapshot.symbols()[symbol_index];
  target.document_index = document_index;
  target.first_edge = state.relationship_cursor;
  target.kind = static_cast<std::uint32_t>(source.kind());

  auto symbol = append_generated_string(state, source.symbol());
  if (!symbol) {
    return unexpected(symbol.error());
  }
  target.symbol = *symbol;

  if (!source.documentation().empty()) {
    auto documentation = append_generated_string(state, source.documentation(0));
    if (!documentation) {
      return unexpected(documentation.error());
    }
    target.documentation = *documentation;
  }

  auto display_name = append_generated_string(state, source.display_name());
  if (!display_name) {
    return unexpected(display_name.error());
  }
  target.display_name = *display_name;

  for (const auto& relationship : source.relationships()) {
    if (state.relationship_cursor >= state.snapshot.counts().relationships) {
      return unexpected(make_error(IngestErrorCode::arena_allocation_failed, "generated relationship cursor exceeded capacity"));
    }
    auto& edge = state.snapshot.relationships()[state.relationship_cursor++];
    edge.source_symbol = symbol_index;
    if (relationship.is_reference()) {
      edge.flags |= relationship_reference;
    }
    if (relationship.is_implementation()) {
      edge.flags |= relationship_implementation;
    }
    if (relationship.is_type_definition()) {
      edge.flags |= relationship_type_definition;
    }
    if (relationship.is_definition()) {
      edge.flags |= relationship_definition;
    }
    auto relationship_symbol = append_generated_string(state, relationship.symbol());
    if (!relationship_symbol) {
      return unexpected(relationship_symbol.error());
    }
    edge.target_symbol = *relationship_symbol;
  }

  target.edge_count = state.relationship_cursor - target.first_edge;
  return {};
}

[[nodiscard]] Expected<void, IngestError> fill_generated_occurrence(const scip::Occurrence& source, FillState& state, std::uint32_t document_index) {
  if (state.occurrence_cursor >= state.snapshot.counts().occurrences) {
    return unexpected(make_error(IngestErrorCode::arena_allocation_failed, "generated occurrence cursor exceeded capacity"));
  }
  auto& target = state.snapshot.occurrences()[state.occurrence_cursor++];
  target.document_index = document_index;
  target.symbol_roles = static_cast<std::uint32_t>(source.symbol_roles());
  target.syntax_kind = static_cast<std::uint32_t>(source.syntax_kind());

  auto symbol = append_generated_string(state, source.symbol());
  if (!symbol) {
    return unexpected(symbol.error());
  }
  target.symbol = *symbol;

  if (source.range_size() != 0 && source.range_size() != 3 && source.range_size() != 4) {
    return unexpected(make_error(IngestErrorCode::malformed_protobuf, "generated SCIP occurrence range must contain three or four elements"));
  }
  if (source.range_size() >= 3) {
    for (int range_index = 0; range_index < source.range_size(); ++range_index) {
      if (source.range(range_index) < 0) {
        return unexpected(make_error(IngestErrorCode::malformed_protobuf, "generated SCIP occurrence range cannot contain negative coordinates"));
      }
    }
    target.start_line = static_cast<std::uint32_t>(source.range(0));
    target.start_character = static_cast<std::uint32_t>(source.range(1));
    if (source.range_size() == 3) {
      target.end_line = target.start_line;
      target.end_character = static_cast<std::uint32_t>(source.range(2));
    } else {
      target.end_line = static_cast<std::uint32_t>(source.range(2));
      target.end_character = static_cast<std::uint32_t>(source.range(3));
    }
  }
  return {};
}

[[nodiscard]] Expected<void, IngestError> fill_generated_index(const scip::Index& index, ArenaSnapshot& snapshot) {
  FillState state{.snapshot = snapshot};

  for (const auto& source_document : index.documents()) {
    const auto document_index = state.document_cursor++;
    auto& target_document = snapshot.documents()[document_index];
    target_document.first_symbol = state.symbol_cursor;
    target_document.first_occurrence = state.occurrence_cursor;
    target_document.position_encoding = static_cast<std::uint32_t>(source_document.position_encoding());

    auto relative_path = append_generated_string(state, source_document.relative_path());
    if (!relative_path) {
      return unexpected(relative_path.error());
    }
    target_document.relative_path = *relative_path;
    auto language = append_generated_string(state, source_document.language());
    if (!language) {
      return unexpected(language.error());
    }
    target_document.language = *language;

    for (const auto& occurrence : source_document.occurrences()) {
      auto filled = fill_generated_occurrence(occurrence, state, document_index);
      if (!filled) {
        return unexpected(filled.error());
      }
    }
    for (const auto& symbol : source_document.symbols()) {
      auto filled = fill_generated_symbol(symbol, state, document_index);
      if (!filled) {
        return unexpected(filled.error());
      }
    }

    target_document.symbol_count = state.symbol_cursor - target_document.first_symbol;
    target_document.occurrence_count = state.occurrence_cursor - target_document.first_occurrence;
  }

  for (const auto& symbol : index.external_symbols()) {
    auto filled = fill_generated_symbol(symbol, state, external_document_index);
    if (!filled) {
      return unexpected(filled.error());
    }
  }

  Ensures(state.document_cursor == snapshot.counts().documents);
  Ensures(state.symbol_cursor == snapshot.counts().symbols);
  Ensures(state.relationship_cursor == snapshot.counts().relationships);
  Ensures(state.occurrence_cursor == snapshot.counts().occurrences);
  Ensures(snapshot.string_bytes_used() == snapshot.counts().string_bytes);
  return {};
}

[[nodiscard]] Expected<IngestResult, IngestError> build_from_generated_protobuf(std::span<const std::byte> payload) {
  google::protobuf::Arena arena;
  auto* index = google::protobuf::Arena::Create<scip::Index>(&arena);
  if (!index->ParseFromArray(payload.data(), static_cast<int>(payload.size()))) {
    return unexpected(make_error(IngestErrorCode::malformed_protobuf, "generated SCIP protobuf parse failed"));
  }

  auto counts = count_generated_index(*index);
  if (!counts) {
    return unexpected(counts.error());
  }
  auto snapshot = GraphArena::allocate(*counts);
  if (!snapshot) {
    return unexpected(make_error(IngestErrorCode::arena_allocation_failed, snapshot.error().message));
  }
  auto filled = fill_generated_index(*index, **snapshot);
  if (!filled) {
    return unexpected(filled.error());
  }
  IngestSummary summary{
      .counts = (*snapshot)->counts(),
      .arena_bytes = (*snapshot)->arena_bytes(),
      .string_bytes_used = (*snapshot)->string_bytes_used(),
      .mode = IngestMode::generated_protobuf_compatibility,
  };
  return IngestResult{.snapshot = std::move(*snapshot), .summary = summary};
}
#endif

} // namespace

std::string_view to_string(IngestMode mode) noexcept {
  switch (mode) {
    case IngestMode::strict_wire_scanner:
      return "strict-wire-scanner";
    case IngestMode::generated_protobuf_compatibility:
      return "generated-protobuf-compatibility";
  }
  return "unknown";
}

std::string_view to_string(IngestErrorCode code) noexcept {
  switch (code) {
    case IngestErrorCode::empty_payload:
      return "empty-payload";
    case IngestErrorCode::payload_too_large:
      return "payload-too-large";
    case IngestErrorCode::malformed_protobuf:
      return "malformed-protobuf";
    case IngestErrorCode::count_overflow:
      return "count-overflow";
    case IngestErrorCode::arena_allocation_failed:
      return "arena-allocation-failed";
    case IngestErrorCode::unsupported_mode:
      return "unsupported-mode";
  }
  return "unknown";
}

ScipIngestor::ScipIngestor(IngestOptions options) : options_(options) {
  Expects(options_.max_payload_bytes > 0);
}

Expected<IngestResult, IngestError> ScipIngestor::ingest(std::span<const std::byte> payload) const {
  Expects(options_.max_payload_bytes > 0);
  if (payload.empty()) {
    return unexpected(make_error(IngestErrorCode::empty_payload, "SCIP payload is empty"));
  }
  if (payload.size() > options_.max_payload_bytes) {
    return unexpected(make_error(IngestErrorCode::payload_too_large, "SCIP payload exceeds configured maximum"));
  }

  switch (options_.mode) {
    case IngestMode::strict_wire_scanner:
      return build_from_wire(payload);
    case IngestMode::generated_protobuf_compatibility:
#if defined(GRAPHIFY_HAS_PROTOBUF_COMPAT)
      return build_from_generated_protobuf(payload);
#else
      return unexpected(make_error(IngestErrorCode::unsupported_mode, "generated protobuf compatibility mode was not compiled"));
#endif
  }

  return unexpected(make_error(IngestErrorCode::unsupported_mode, "unknown ingest mode"));
}

} // namespace graphify
