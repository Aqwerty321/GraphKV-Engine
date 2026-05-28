#pragma once

#include <cstddef>

#ifndef GRAPHKV_MAX_INGEST_BYTES
#if defined(GRAPHIFY_MAX_INGEST_BYTES)
#define GRAPHKV_MAX_INGEST_BYTES GRAPHIFY_MAX_INGEST_BYTES
#else
#define GRAPHKV_MAX_INGEST_BYTES 262144000ULL
#endif
#endif

#ifndef GRAPHIFY_MAX_INGEST_BYTES
#define GRAPHIFY_MAX_INGEST_BYTES GRAPHKV_MAX_INGEST_BYTES
#endif

namespace graphify {

inline constexpr std::size_t graphkv_max_ingest_bytes = static_cast<std::size_t>(GRAPHKV_MAX_INGEST_BYTES);
inline constexpr std::size_t graphkv_max_control_bytes = 64U * 1024U;

} // namespace graphify
