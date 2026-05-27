cmake_minimum_required(VERSION 3.25)

project(graphify_scip_ingest LANGUAGES CXX)

include(FetchContent)
include(CheckCXXSourceCompiles)
include(GNUInstallDirs)

set(CMAKE_CXX_STANDARD 26)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

option(GRAPHIFY_BUILD_SERVER "Build Drogon WebSocket server" ON)
option(GRAPHIFY_BUILD_TESTS "Build focused tests" ON)
option(GRAPHIFY_BUILD_TOOLS "Build/ship smoke tools" ON)
option(GRAPHIFY_REQUIRE_STD_PRINT "Require std::println support" ON)
option(GRAPHIFY_ENABLE_PROTOBUF_COMPAT "Build generated SCIP protobuf compatibility target" ON)
option(GRAPHIFY_REQUIRE_FOLLY "Require an installed Folly package for production-safe snapshot reclamation" OFF)

set(SCIP_PROTO_REF "main" CACHE STRING "Pinned scip-code/scip ref for scip.proto")
set(GRAPHIFY_MAX_INGEST_BYTES "536870912" CACHE STRING "Default max WebSocket ingest payload bytes")
set(GRAPHIFY_GSL_TAG "v4.2.2" CACHE STRING "Microsoft GSL tag")
set(GRAPHIFY_DROGON_TAG "v1.9.11" CACHE STRING "Drogon tag")
set(GRAPHIFY_MDSPAN_TAG "stable" CACHE STRING "Kokkos mdspan tag or branch")
set(GRAPHIFY_FOLLY_TAG "v2026.05.25.00" CACHE STRING "Folly tag used by production Docker builds")

add_compile_options(
  $<$<COMPILE_LANG_AND_ID:CXX,Clang,GNU>:-Wall>
  $<$<COMPILE_LANG_AND_ID:CXX,Clang,GNU>:-Wextra>
  $<$<COMPILE_LANG_AND_ID:CXX,Clang,GNU>:-Wpedantic>
  $<$<COMPILE_LANG_AND_ID:CXX,Clang,GNU>:-O3>
)

set(CMAKE_INTERPROCEDURAL_OPTIMIZATION_RELEASE ON)

check_cxx_source_compiles([[#include <print>
int main() { std::println("ok"); return 0; }]] GRAPHIFY_HAS_STD_PRINT)

if(GRAPHIFY_REQUIRE_STD_PRINT AND NOT GRAPHIFY_HAS_STD_PRINT)
  message(FATAL_ERROR "std::println is required. Use clang-18 with a standard library that provides <print>, or configure with -DGRAPHIFY_REQUIRE_STD_PRINT=OFF -DGRAPHIFY_BUILD_SERVER=OFF for core-only testing.")
endif()

FetchContent_Declare(
  Microsoft.GSL
  GIT_REPOSITORY https://github.com/microsoft/GSL.git
  GIT_TAG ${GRAPHIFY_GSL_TAG}
)
FetchContent_MakeAvailable(Microsoft.GSL)

FetchContent_Declare(
  kokkos_mdspan
  GIT_REPOSITORY https://github.com/kokkos/mdspan.git
  GIT_TAG ${GRAPHIFY_MDSPAN_TAG}
)
FetchContent_MakeAvailable(kokkos_mdspan)

find_package(folly CONFIG QUIET)
if(NOT TARGET Folly::folly AND NOT TARGET folly)
  find_package(Folly CONFIG QUIET)
endif()
set(GRAPHIFY_FOLLY_TARGET "")
if(TARGET Folly::folly)
  set(GRAPHIFY_FOLLY_TARGET Folly::folly)
elseif(TARGET folly)
  set(GRAPHIFY_FOLLY_TARGET folly)
endif()

if(NOT GRAPHIFY_FOLLY_TARGET)
  if(GRAPHIFY_REQUIRE_FOLLY)
    message(FATAL_ERROR "GRAPHIFY_REQUIRE_FOLLY=ON but no Folly CMake package was found. Build/install Folly first and pass CMAKE_PREFIX_PATH to its prefix.")
  endif()
  message(WARNING "Folly was not found as an installed CMake package. GraphKV will use the bounded-retire fallback for local development only; production should configure with -DGRAPHIFY_REQUIRE_FOLLY=ON.")
else()
  message(STATUS "GraphKV snapshot reclamation will use Folly target: ${GRAPHIFY_FOLLY_TARGET}")
endif()

set(GRAPHIFY_GENERATED_DIR ${CMAKE_BINARY_DIR}/generated/scip)
file(MAKE_DIRECTORY ${GRAPHIFY_GENERATED_DIR})

if(GRAPHIFY_ENABLE_PROTOBUF_COMPAT)
  find_package(Protobuf REQUIRED)
  find_package(PkgConfig QUIET)
  if(PkgConfig_FOUND)
    pkg_check_modules(PROTOBUF_PKG QUIET IMPORTED_TARGET protobuf)
  endif()
  set(SCIP_PROTO_FILE ${GRAPHIFY_GENERATED_DIR}/scip.proto)
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
    OUTPUT ${GRAPHIFY_GENERATED_DIR}/scip.pb.cc ${GRAPHIFY_GENERATED_DIR}/scip.pb.h
    COMMAND ${Protobuf_PROTOC_EXECUTABLE}
      --cpp_out=${GRAPHIFY_GENERATED_DIR}
      --proto_path=${GRAPHIFY_GENERATED_DIR}
      ${SCIP_PROTO_FILE}
    DEPENDS ${SCIP_PROTO_FILE}
    VERBATIM
  )

  add_library(scip_proto STATIC
    ${GRAPHIFY_GENERATED_DIR}/scip.pb.cc
    ${GRAPHIFY_GENERATED_DIR}/scip.pb.h
  )
  target_include_directories(scip_proto PUBLIC ${GRAPHIFY_GENERATED_DIR})
  if(TARGET PkgConfig::PROTOBUF_PKG)
    target_link_libraries(scip_proto PUBLIC PkgConfig::PROTOBUF_PKG)
  else()
    target_link_libraries(scip_proto PUBLIC protobuf::libprotobuf)
  endif()
endif()

add_library(graphify_core STATIC
  src/ScipIngestor.cpp
)
target_include_directories(graphify_core
  PUBLIC
    ${PROJECT_SOURCE_DIR}/include
    ${kokkos_mdspan_SOURCE_DIR}/include
)
target_compile_definitions(graphify_core
  PUBLIC
    GRAPHIFY_MAX_INGEST_BYTES=${GRAPHIFY_MAX_INGEST_BYTES}
)
target_link_libraries(graphify_core PUBLIC Microsoft.GSL::GSL)

if(GRAPHIFY_FOLLY_TARGET)
  target_link_libraries(graphify_core PUBLIC ${GRAPHIFY_FOLLY_TARGET})
  target_compile_definitions(graphify_core PUBLIC GRAPHIFY_HAS_FOLLY=1)
endif()

if(GRAPHIFY_ENABLE_PROTOBUF_COMPAT)
  target_link_libraries(graphify_core PUBLIC scip_proto)
  target_compile_definitions(graphify_core PUBLIC GRAPHIFY_HAS_PROTOBUF_COMPAT=1)
endif()

if(GRAPHIFY_BUILD_SERVER)
  set(BUILD_CTL OFF CACHE BOOL "" FORCE)
  set(BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
  set(BUILD_ORM OFF CACHE BOOL "" FORCE)
  set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
  FetchContent_Declare(
    drogon
    GIT_REPOSITORY https://github.com/drogonframework/drogon.git
    GIT_TAG ${GRAPHIFY_DROGON_TAG}
  )
  FetchContent_MakeAvailable(drogon)

  set(GRAPHIFY_DROGON_TARGET "")
  if(TARGET Drogon::Drogon)
    set(GRAPHIFY_DROGON_TARGET Drogon::Drogon)
  elseif(TARGET drogon)
    set(GRAPHIFY_DROGON_TARGET drogon)
  else()
    message(FATAL_ERROR "Drogon target was not found after FetchContent_MakeAvailable")
  endif()

  add_executable(scip_ingest_server
    src/main.cpp
  )
  target_link_libraries(scip_ingest_server PRIVATE graphify_core ${GRAPHIFY_DROGON_TARGET})
  target_compile_definitions(scip_ingest_server PRIVATE GRAPHIFY_HAS_STD_PRINT=$<BOOL:${GRAPHIFY_HAS_STD_PRINT}>)
  install(TARGETS scip_ingest_server RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
endif()

if(GRAPHIFY_BUILD_TESTS)
  enable_testing()
  add_executable(graphkv_tests tests/graphkv_tests.cpp)
  target_link_libraries(graphkv_tests PRIVATE graphify_core)
  add_test(NAME graphkv_tests COMMAND graphkv_tests)

  add_executable(scip_wire_tests tests/scip_wire_tests.cpp)
  target_link_libraries(scip_wire_tests PRIVATE graphify_core)
  add_test(NAME scip_wire_tests COMMAND scip_wire_tests)
endif()

if(GRAPHIFY_BUILD_TOOLS)
  install(PROGRAMS tools/send_scip_ws.py DESTINATION ${CMAKE_INSTALL_BINDIR})
endif()
