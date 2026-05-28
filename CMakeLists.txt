cmake_minimum_required(VERSION 3.25)

project(graphkv_engine LANGUAGES CXX)

include(FetchContent)
include(CheckCXXSourceCompiles)
include(GNUInstallDirs)
find_package(Python3 COMPONENTS Interpreter QUIET)

set(CMAKE_CXX_STANDARD 26)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

function(graphkv_bool_option new_name legacy_name description default_value)
  if(DEFINED ${legacy_name} AND NOT DEFINED ${new_name})
    set(_default_value "${${legacy_name}}")
  else()
    set(_default_value "${default_value}")
  endif()
  option(${new_name} "${description}" "${_default_value}")
  set(${legacy_name} "${${new_name}}" CACHE BOOL "Legacy alias for ${new_name}" FORCE)
endfunction()

function(graphkv_cache_string new_name legacy_name default_value description)
  if(DEFINED ${legacy_name} AND NOT DEFINED ${new_name})
    set(_default_value "${${legacy_name}}")
  else()
    set(_default_value "${default_value}")
  endif()
  set(${new_name} "${_default_value}" CACHE STRING "${description}")
  set(${legacy_name} "${${new_name}}" CACHE STRING "Legacy alias for ${new_name}" FORCE)
endfunction()

graphkv_bool_option(GRAPHKV_BUILD_SERVER GRAPHIFY_BUILD_SERVER "Build Drogon WebSocket server" ON)
graphkv_bool_option(GRAPHKV_BUILD_TESTS GRAPHIFY_BUILD_TESTS "Build focused tests" ON)
graphkv_bool_option(GRAPHKV_BUILD_TOOLS GRAPHIFY_BUILD_TOOLS "Build/ship smoke tools" ON)
graphkv_bool_option(GRAPHKV_BUILD_BENCHMARKS GRAPHIFY_BUILD_BENCHMARKS "Build focused ingest benchmarks" ON)
graphkv_bool_option(GRAPHKV_BUILD_FUZZERS GRAPHIFY_BUILD_FUZZERS "Build LLVM LibFuzzer targets" OFF)
graphkv_bool_option(GRAPHKV_REQUIRE_STD_PRINT GRAPHIFY_REQUIRE_STD_PRINT "Require std::println support" ON)
graphkv_bool_option(GRAPHKV_ENABLE_PROTOBUF_COMPAT GRAPHIFY_ENABLE_PROTOBUF_COMPAT "Build generated SCIP protobuf compatibility target" ON)
graphkv_bool_option(GRAPHKV_ENABLE_USEARCH GRAPHIFY_ENABLE_USEARCH "Use Unum USearch for dense vector retrieval" ON)
graphkv_bool_option(GRAPHKV_REQUIRE_FOLLY GRAPHIFY_REQUIRE_FOLLY "Require an installed Folly package for production-safe snapshot reclamation" OFF)

set(SCIP_PROTO_REF "99236e35450ccd8b87fe58c38d31fd499d0ffdfa" CACHE STRING "Pinned immutable scip-code/scip commit for scip.proto")
graphkv_cache_string(GRAPHKV_MAX_INGEST_BYTES GRAPHIFY_MAX_INGEST_BYTES "262144000" "Default max WebSocket ingest payload bytes")
graphkv_cache_string(GRAPHKV_GSL_TAG GRAPHIFY_GSL_TAG "v4.2.2" "Microsoft GSL tag")
graphkv_cache_string(GRAPHKV_DROGON_TAG GRAPHIFY_DROGON_TAG "v1.9.11" "Drogon tag")
graphkv_cache_string(GRAPHKV_GLAZE_TAG GRAPHIFY_GLAZE_TAG "v7.7.0" "Glaze tag")
graphkv_cache_string(GRAPHKV_MDSPAN_TAG GRAPHIFY_MDSPAN_TAG "stable" "Kokkos mdspan tag or branch")
graphkv_cache_string(GRAPHKV_FOLLY_TAG GRAPHIFY_FOLLY_TAG "v2026.05.25.00" "Folly tag used by production Docker builds")
graphkv_cache_string(GRAPHKV_ZSTD_TAG GRAPHIFY_ZSTD_TAG "v1.5.7" "Facebook zstd tag")
graphkv_cache_string(GRAPHKV_USEARCH_TAG GRAPHIFY_USEARCH_TAG "v2.25.3" "Unum USearch tag")
set(GRAPHKV_BENCHMARK_FIXTURE "${PROJECT_SOURCE_DIR}/benchmarks/data/index.scip" CACHE FILEPATH "Default SCIP fixture for ingest latency benchmark")
if(DEFINED GRAPHIFY_BENCHMARK_FIXTURE AND NOT DEFINED GRAPHKV_BENCHMARK_FIXTURE)
  set(GRAPHKV_BENCHMARK_FIXTURE "${GRAPHIFY_BENCHMARK_FIXTURE}" CACHE FILEPATH "Default SCIP fixture for ingest latency benchmark" FORCE)
endif()
set(GRAPHIFY_BENCHMARK_FIXTURE "${GRAPHKV_BENCHMARK_FIXTURE}" CACHE FILEPATH "Legacy alias for GRAPHKV_BENCHMARK_FIXTURE" FORCE)

add_compile_options(
  $<$<COMPILE_LANG_AND_ID:CXX,Clang,GNU>:-Wall>
  $<$<COMPILE_LANG_AND_ID:CXX,Clang,GNU>:-Wextra>
  $<$<COMPILE_LANG_AND_ID:CXX,Clang,GNU>:-Wpedantic>
  $<$<COMPILE_LANG_AND_ID:CXX,Clang,GNU>:-O3>
)

set(CMAKE_INTERPROCEDURAL_OPTIMIZATION_RELEASE ON)

check_cxx_source_compiles([[#include <print>
int main() { std::println("ok"); return 0; }]] GRAPHKV_HAS_STD_PRINT)

if(GRAPHKV_REQUIRE_STD_PRINT AND NOT GRAPHKV_HAS_STD_PRINT)
  message(FATAL_ERROR "std::println is required. Use clang-18 with a standard library that provides <print>, or configure with -DGRAPHKV_REQUIRE_STD_PRINT=OFF -DGRAPHKV_BUILD_SERVER=OFF for core-only testing.")
endif()

FetchContent_Declare(
  Microsoft.GSL
  GIT_REPOSITORY https://github.com/microsoft/GSL.git
  GIT_TAG ${GRAPHKV_GSL_TAG}
)
FetchContent_MakeAvailable(Microsoft.GSL)

FetchContent_Declare(
  glaze
  GIT_REPOSITORY https://github.com/stephenberry/glaze.git
  GIT_TAG ${GRAPHKV_GLAZE_TAG}
)
FetchContent_MakeAvailable(glaze)

set(GRAPHKV_GLAZE_TARGET "")
if(TARGET glaze::glaze)
  set(GRAPHKV_GLAZE_TARGET glaze::glaze)
elseif(TARGET glaze)
  set(GRAPHKV_GLAZE_TARGET glaze)
else()
  add_library(graphkv_glaze INTERFACE)
  target_include_directories(graphkv_glaze INTERFACE ${glaze_SOURCE_DIR}/include)
  set(GRAPHKV_GLAZE_TARGET graphkv_glaze)
endif()

set(GRAPHKV_USEARCH_INCLUDE_DIR "")
if(GRAPHKV_ENABLE_USEARCH)
  FetchContent_Declare(
    usearch
    GIT_REPOSITORY https://github.com/unum-cloud/USearch.git
    GIT_TAG ${GRAPHKV_USEARCH_TAG}
    GIT_SUBMODULES_RECURSE TRUE
  )
  FetchContent_MakeAvailable(usearch)
  set(GRAPHKV_USEARCH_INCLUDE_DIR ${usearch_SOURCE_DIR}/include)
endif()

set(ZSTD_BUILD_PROGRAMS OFF CACHE BOOL "" FORCE)
set(ZSTD_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(ZSTD_BUILD_CONTRIB OFF CACHE BOOL "" FORCE)
set(ZSTD_BUILD_SHARED OFF CACHE BOOL "" FORCE)
set(ZSTD_BUILD_STATIC ON CACHE BOOL "" FORCE)
FetchContent_Declare(
  zstd
  GIT_REPOSITORY https://github.com/facebook/zstd.git
  GIT_TAG ${GRAPHKV_ZSTD_TAG}
  SOURCE_SUBDIR build/cmake
)
FetchContent_MakeAvailable(zstd)
if(NOT TARGET zstd::libzstd_static AND TARGET libzstd_static)
  add_library(zstd::libzstd_static ALIAS libzstd_static)
endif()
if(NOT TARGET zstd::libzstd_static)
  message(FATAL_ERROR "zstd::libzstd_static target was not found after FetchContent_MakeAvailable")
endif()

FetchContent_Declare(
  kokkos_mdspan
  GIT_REPOSITORY https://github.com/kokkos/mdspan.git
  GIT_TAG ${GRAPHKV_MDSPAN_TAG}
)
FetchContent_MakeAvailable(kokkos_mdspan)

find_package(folly CONFIG QUIET)
if(NOT TARGET Folly::folly AND NOT TARGET folly)
  find_package(Folly CONFIG QUIET)
endif()
set(GRAPHKV_FOLLY_TARGET "")
if(TARGET Folly::folly)
  set(GRAPHKV_FOLLY_TARGET Folly::folly)
elseif(TARGET folly)
  set(GRAPHKV_FOLLY_TARGET folly)
endif()

if(NOT GRAPHKV_FOLLY_TARGET)
  if(GRAPHKV_REQUIRE_FOLLY)
    message(FATAL_ERROR "GRAPHKV_REQUIRE_FOLLY=ON but no Folly CMake package was found. Build/install Folly first and pass CMAKE_PREFIX_PATH to its prefix.")
  endif()
  message(WARNING "Folly was not found as an installed CMake package. GraphKV will use the bounded-retire fallback for local development only; production should configure with -DGRAPHKV_REQUIRE_FOLLY=ON.")
else()
  message(STATUS "GraphKV snapshot reclamation will use Folly target: ${GRAPHKV_FOLLY_TARGET}")
endif()

set(GRAPHKV_GENERATED_DIR ${CMAKE_BINARY_DIR}/generated/scip)
file(MAKE_DIRECTORY ${GRAPHKV_GENERATED_DIR})

if(GRAPHKV_ENABLE_PROTOBUF_COMPAT)
  find_package(Protobuf REQUIRED)
  find_package(PkgConfig QUIET)
  if(PkgConfig_FOUND)
    pkg_check_modules(PROTOBUF_PKG QUIET IMPORTED_TARGET protobuf)
  endif()
  set(SCIP_PROTO_FILE ${GRAPHKV_GENERATED_DIR}/scip.proto)
  file(DOWNLOAD
    https://raw.githubusercontent.com/scip-code/scip/${SCIP_PROTO_REF}/scip.proto
    ${SCIP_PROTO_FILE}
    TLS_VERIFY ON
    STATUS SCIP_PROTO_DOWNLOAD_STATUS
  )
  list(GET SCIP_PROTO_DOWNLOAD_STATUS 0 SCIP_PROTO_DOWNLOAD_CODE)
  if(NOT SCIP_PROTO_DOWNLOAD_CODE EQUAL 0)
    message(FATAL_ERROR "Failed to download scip.proto from ref ${SCIP_PROTO_REF}: ${SCIP_PROTO_DOWNLOAD_STATUS}")
  endif()

  add_custom_command(
    OUTPUT ${GRAPHKV_GENERATED_DIR}/scip.pb.cc ${GRAPHKV_GENERATED_DIR}/scip.pb.h
    COMMAND ${Protobuf_PROTOC_EXECUTABLE}
      --cpp_out=${GRAPHKV_GENERATED_DIR}
      --proto_path=${GRAPHKV_GENERATED_DIR}
      ${SCIP_PROTO_FILE}
    DEPENDS ${SCIP_PROTO_FILE}
    VERBATIM
  )

  add_library(scip_proto STATIC
    ${GRAPHKV_GENERATED_DIR}/scip.pb.cc
    ${GRAPHKV_GENERATED_DIR}/scip.pb.h
  )
  target_include_directories(scip_proto PUBLIC ${GRAPHKV_GENERATED_DIR})
  if(TARGET PkgConfig::PROTOBUF_PKG)
    target_link_libraries(scip_proto PUBLIC PkgConfig::PROTOBUF_PKG)
  else()
    target_link_libraries(scip_proto PUBLIC protobuf::libprotobuf)
  endif()
endif()

add_library(graphkv_core STATIC
  src/ScipIngestor.cpp
)
add_library(graphify_core ALIAS graphkv_core)
target_include_directories(graphkv_core
  PUBLIC
    ${PROJECT_SOURCE_DIR}/include
    ${kokkos_mdspan_SOURCE_DIR}/include
)
target_compile_definitions(graphkv_core
  PUBLIC
    GRAPHKV_MAX_INGEST_BYTES=${GRAPHKV_MAX_INGEST_BYTES}
    GRAPHIFY_MAX_INGEST_BYTES=${GRAPHKV_MAX_INGEST_BYTES}
)
target_link_libraries(graphkv_core PUBLIC Microsoft.GSL::GSL)

if(GRAPHKV_FOLLY_TARGET)
  target_link_libraries(graphkv_core PUBLIC ${GRAPHKV_FOLLY_TARGET})
  target_compile_definitions(graphkv_core PUBLIC GRAPHIFY_HAS_FOLLY=1)
endif()

if(GRAPHKV_ENABLE_PROTOBUF_COMPAT)
  target_link_libraries(graphkv_core PUBLIC scip_proto)
  target_compile_definitions(graphkv_core PUBLIC GRAPHIFY_HAS_PROTOBUF_COMPAT=1)
endif()

add_library(graphkv_ingress_boundary STATIC
  src/DocIngestor.cpp
  src/IngressGate.cpp
  src/OpenCodeProtocol.cpp
  src/RetrievalIndex.cpp
)
target_include_directories(graphkv_ingress_boundary PUBLIC ${PROJECT_SOURCE_DIR}/include)
target_link_libraries(graphkv_ingress_boundary PUBLIC graphkv_core Microsoft.GSL::GSL ${GRAPHKV_GLAZE_TARGET})
if(GRAPHKV_ENABLE_USEARCH)
  target_include_directories(graphkv_ingress_boundary PUBLIC ${GRAPHKV_USEARCH_INCLUDE_DIR})
  target_compile_definitions(graphkv_ingress_boundary PUBLIC GRAPHKV_HAS_USEARCH=1)
endif()

if(GRAPHKV_BUILD_SERVER)
  set(BUILD_CTL OFF CACHE BOOL "" FORCE)
  set(BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
  set(BUILD_ORM OFF CACHE BOOL "" FORCE)
  set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
  FetchContent_Declare(
    drogon
    GIT_REPOSITORY https://github.com/drogonframework/drogon.git
    GIT_TAG ${GRAPHKV_DROGON_TAG}
  )
  FetchContent_MakeAvailable(drogon)

  set(GRAPHKV_DROGON_TARGET "")
  if(TARGET Drogon::Drogon)
    set(GRAPHKV_DROGON_TARGET Drogon::Drogon)
  elseif(TARGET drogon)
    set(GRAPHKV_DROGON_TARGET drogon)
  else()
    message(FATAL_ERROR "Drogon target was not found after FetchContent_MakeAvailable")
  endif()

  add_executable(scip_ingest_server
    src/IngestController.cpp
    src/main.cpp
  )
  target_link_libraries(scip_ingest_server PRIVATE graphkv_ingress_boundary ${GRAPHKV_DROGON_TARGET} zstd::libzstd_static)
  target_compile_definitions(scip_ingest_server PRIVATE GRAPHKV_HAS_STD_PRINT=$<BOOL:${GRAPHKV_HAS_STD_PRINT}>)
  install(TARGETS scip_ingest_server RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
endif()

if(GRAPHKV_BUILD_TESTS)
  enable_testing()
  add_executable(graphkv_tests tests/graphkv_tests.cpp)
  target_link_libraries(graphkv_tests PRIVATE graphkv_core)
  add_test(NAME graphkv_tests COMMAND graphkv_tests)

  add_executable(scip_wire_tests tests/scip_wire_tests.cpp)
  target_link_libraries(scip_wire_tests PRIVATE graphkv_core)
  add_test(NAME scip_wire_tests COMMAND scip_wire_tests)

  add_executable(ingress_boundary_tests tests/ingress_boundary_tests.cpp)
  target_link_libraries(ingress_boundary_tests PRIVATE graphkv_ingress_boundary)
  add_test(NAME ingress_boundary_tests COMMAND ingress_boundary_tests)

  add_executable(doc_ingestor_tests tests/doc_ingestor_tests.cpp)
  target_link_libraries(doc_ingestor_tests PRIVATE graphkv_ingress_boundary)
  add_test(NAME doc_ingestor_tests COMMAND doc_ingestor_tests)

  add_executable(retrieval_index_tests tests/retrieval_index_tests.cpp)
  target_link_libraries(retrieval_index_tests PRIVATE graphkv_ingress_boundary)
  add_test(NAME retrieval_index_tests COMMAND retrieval_index_tests)

  add_executable(telemetry_ring_buffer_tests tests/telemetry_ring_buffer_tests.cpp)
  target_link_libraries(telemetry_ring_buffer_tests PRIVATE graphkv_ingress_boundary)
  add_test(NAME telemetry_ring_buffer_tests COMMAND telemetry_ring_buffer_tests)

  add_executable(opencode_protocol_tests tests/opencode_protocol_tests.cpp)
  target_link_libraries(opencode_protocol_tests PRIVATE graphkv_ingress_boundary)
  add_test(NAME opencode_protocol_tests COMMAND opencode_protocol_tests)
endif()

if(GRAPHKV_BUILD_FUZZERS)
  if(NOT CMAKE_CXX_COMPILER_ID MATCHES "Clang")
    message(FATAL_ERROR "GRAPHKV_BUILD_FUZZERS=ON requires clang++ for -fsanitize=fuzzer")
  endif()
  add_executable(ingest_boundary_fuzzer fuzz/fuzz_target.cpp)
  target_link_libraries(ingest_boundary_fuzzer PRIVATE graphkv_ingress_boundary)
  target_compile_options(ingest_boundary_fuzzer PRIVATE -fsanitize=fuzzer,address,undefined)
  target_link_options(ingest_boundary_fuzzer PRIVATE -fsanitize=fuzzer,address,undefined)
endif()

if(GRAPHKV_BUILD_BENCHMARKS)
  add_executable(ingest_latency_benchmark benchmarks/ingest_latency.cpp)
  target_link_libraries(ingest_latency_benchmark PRIVATE graphkv_core)
  target_compile_definitions(
    ingest_latency_benchmark
    PRIVATE GRAPHIFY_DEFAULT_BENCHMARK_FIXTURE="${GRAPHKV_BENCHMARK_FIXTURE}"
  )

  if(Python3_Interpreter_FOUND)
    add_custom_target(
      record_benchmarks
      COMMAND ${Python3_EXECUTABLE} ${PROJECT_SOURCE_DIR}/benchmarks/run_and_record.py --build-dir ${CMAKE_BINARY_DIR}
      WORKING_DIRECTORY ${PROJECT_SOURCE_DIR}
      DEPENDS ingest_latency_benchmark
      USES_TERMINAL
    )
  endif()

  install(TARGETS ingest_latency_benchmark RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
endif()

if(GRAPHKV_BUILD_TOOLS)
  install(PROGRAMS
    tools/build_doc_bundle.py
    tools/send_docs_ws.py
    tools/send_scip_ws.py
    DESTINATION ${CMAKE_INSTALL_BINDIR})
endif()
