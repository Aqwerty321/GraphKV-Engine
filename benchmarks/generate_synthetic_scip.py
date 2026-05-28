#!/usr/bin/env python3

from __future__ import annotations

import argparse
from pathlib import Path


def append_varint(buf: bytearray, value: int) -> None:
    while value >= 0x80:
        buf.append((value & 0x7F) | 0x80)
        value >>= 7
    buf.append(value)


def append_key(buf: bytearray, field_number: int, wire_type: int) -> None:
    append_varint(buf, (field_number << 3) | wire_type)


def append_varint_field(buf: bytearray, field_number: int, value: int) -> None:
    append_key(buf, field_number, 0)
    append_varint(buf, value)


def append_bytes_field(buf: bytearray, field_number: int, value: str) -> None:
    data = value.encode("utf-8")
    append_key(buf, field_number, 2)
    append_varint(buf, len(data))
    buf.extend(data)


def append_message_field(buf: bytearray, field_number: int, value: bytearray) -> None:
    append_key(buf, field_number, 2)
    append_varint(buf, len(value))
    buf.extend(value)


def append_packed_int32_field(buf: bytearray, field_number: int, values: list[int]) -> None:
    packed = bytearray()
    for value in values:
        append_varint(packed, value)
    append_message_field(buf, field_number, packed)


def build_document(doc_index: int) -> bytearray:
    relationship = bytearray()
    append_bytes_field(relationship, 1, f"cpp target#{doc_index}")
    append_varint_field(relationship, 2, 1)

    symbol = bytearray()
    append_bytes_field(symbol, 1, f"local {doc_index}")
    append_bytes_field(symbol, 3, "documentation")
    append_message_field(symbol, 4, relationship)
    append_varint_field(symbol, 5, 17)
    append_bytes_field(symbol, 6, f"foo{doc_index}")

    line = doc_index + 1
    occurrence = bytearray()
    append_packed_int32_field(occurrence, 1, [line, 1, line, 8])
    append_bytes_field(occurrence, 2, f"local {doc_index}")
    append_varint_field(occurrence, 3, 1)
    append_varint_field(occurrence, 5, 6)

    document = bytearray()
    append_bytes_field(document, 1, f"src/file_{doc_index}.cpp")
    append_message_field(document, 2, occurrence)
    append_message_field(document, 3, symbol)
    append_bytes_field(document, 4, "cpp")
    append_varint_field(document, 6, 1)
    return document


def build_index(document_count: int) -> bytearray:
    index = bytearray()
    for doc_index in range(document_count):
        append_message_field(index, 2, build_document(doc_index))
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic synthetic SCIP payloads for ingest benchmarking.")
    parser.add_argument("output", type=Path, help="Output .scip path")
    parser.add_argument("--documents", type=int, required=True, help="Number of repeated synthetic documents to encode")
    args = parser.parse_args()

    if args.documents <= 0:
        parser.error("--documents must be greater than zero")

    payload = build_index(args.documents)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload)
    print(f"wrote {len(payload)} bytes for {args.documents} documents to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())