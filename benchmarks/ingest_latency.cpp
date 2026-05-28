#include "ScipIngestor.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <numeric>
#include <span>
#include <stdexcept>
#include <string>
#include <vector>

#ifndef GRAPHIFY_DEFAULT_BENCHMARK_FIXTURE
#define GRAPHIFY_DEFAULT_BENCHMARK_FIXTURE "benchmarks/data/index.scip"
#endif

namespace {

constexpr std::size_t kBenchmarkIterations = 100;

[[nodiscard]] std::filesystem::path default_fixture_path() {
  return std::filesystem::path(GRAPHIFY_DEFAULT_BENCHMARK_FIXTURE);
}

[[nodiscard]] std::vector<std::byte> load_fixture(const std::filesystem::path& fixture_path) {
  std::ifstream input(fixture_path, std::ios::binary | std::ios::ate);
  if (!input) {
    throw std::runtime_error("failed to open SCIP fixture: " + fixture_path.string());
  }

  const auto byte_count = input.tellg();
  if (byte_count <= 0) {
    throw std::runtime_error("SCIP fixture is empty: " + fixture_path.string());
  }

  std::vector<std::byte> payload(static_cast<std::size_t>(byte_count));
  input.seekg(0, std::ios::beg);
  if (!input.read(reinterpret_cast<char*>(payload.data()), static_cast<std::streamsize>(payload.size()))) {
    throw std::runtime_error("failed to read SCIP fixture bytes: " + fixture_path.string());
  }

  return payload;
}

[[nodiscard]] double median_microseconds(const std::vector<double>& sorted_latencies) {
  const auto midpoint = sorted_latencies.size() / 2;
  if ((sorted_latencies.size() % 2U) == 0U) {
    return (sorted_latencies[midpoint - 1] + sorted_latencies[midpoint]) / 2.0;
  }
  return sorted_latencies[midpoint];
}

[[nodiscard]] double nearest_rank_percentile_microseconds(const std::vector<double>& sorted_latencies, double percentile) {
  const auto rank = static_cast<std::size_t>(std::ceil(percentile * static_cast<double>(sorted_latencies.size())));
  const auto index = std::min(sorted_latencies.size() - 1, std::max<std::size_t>(1, rank) - 1);
  return sorted_latencies[index];
}

} // namespace

int main(int argc, char** argv) {
  try {
    const auto fixture_path = argc > 1 ? std::filesystem::path(argv[1]) : default_fixture_path();
    const auto payload = load_fixture(fixture_path);

    graphify::ScipIngestor ingestor(graphify::IngestOptions{
        .max_payload_bytes = std::max<std::size_t>(GRAPHIFY_MAX_INGEST_BYTES, payload.size()),
        .mode = graphify::IngestMode::strict_wire_scanner,
    });

    std::vector<double> latency_microseconds;
    latency_microseconds.reserve(kBenchmarkIterations);
    std::uint64_t checksum = 0;

    for (std::size_t iteration = 0; iteration < kBenchmarkIterations; ++iteration) {
      const auto started_at = std::chrono::high_resolution_clock::now();
      auto ingest_result = ingestor.ingest(std::span<const std::byte>(payload.data(), payload.size()));
      const auto finished_at = std::chrono::high_resolution_clock::now();

      if (!ingest_result) {
        std::cerr << "ingest failed on iteration " << iteration << ": " << ingest_result.error().message << '\n';
        return 1;
      }

      latency_microseconds.push_back(
          std::chrono::duration<double, std::micro>(finished_at - started_at).count());
      checksum += ingest_result->summary.arena_bytes;
      checksum += ingest_result->summary.string_bytes_used;
      checksum += ingest_result->summary.counts.documents;
      checksum += ingest_result->summary.counts.symbols;
      checksum += ingest_result->summary.counts.relationships;
      checksum += ingest_result->summary.counts.occurrences;
    }

    std::sort(latency_microseconds.begin(), latency_microseconds.end());
    const auto mean_microseconds =
        std::accumulate(latency_microseconds.begin(), latency_microseconds.end(), 0.0) /
        static_cast<double>(latency_microseconds.size());
    const auto median_value = median_microseconds(latency_microseconds);
    const auto p99_microseconds = nearest_rank_percentile_microseconds(latency_microseconds, 0.99);

    std::cout << std::fixed << std::setprecision(2);
    std::cout << "fixture=" << fixture_path << '\n';
    std::cout << "bytes=" << payload.size() << '\n';
    std::cout << "iterations=" << kBenchmarkIterations << '\n';
    std::cout << "mean_us=" << mean_microseconds << '\n';
    std::cout << "median_us=" << median_value << '\n';
    std::cout << "p99_us=" << p99_microseconds << '\n';
    std::cout << "checksum=" << checksum << '\n';
    return 0;
  } catch (const std::exception& exception) {
    std::cerr << exception.what() << '\n';
    return 1;
  }
}