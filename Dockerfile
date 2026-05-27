ARG TENSORRT_LLM_VERSION=1.3.0rc16

FROM nvcr.io/nvidia/tensorrt-llm/devel:${TENSORRT_LLM_VERSION} AS builder

ARG LLVM_VERSION=18
ARG SCIP_PROTO_REF=main
ARG FOLLY_TAG=v2026.05.25.00
ARG FASTFLOAT_TAG=v8.2.5
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    wget \
    gnupg \
    software-properties-common \
    && wget -qO- https://apt.llvm.org/llvm-snapshot.gpg.key | gpg --dearmor > /usr/share/keyrings/llvm-snapshot.gpg \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/keyrings/llvm-snapshot.gpg] http://apt.llvm.org/${UBUNTU_CODENAME}/ llvm-toolchain-${UBUNTU_CODENAME}-${LLVM_VERSION} main" > /etc/apt/sources.list.d/llvm-${LLVM_VERSION}.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    clang-${LLVM_VERSION} \
    clang-tools-${LLVM_VERSION} \
    lld-${LLVM_VERSION} \
    build-essential \
    ninja-build \
    cmake \
    git \
    python3 \
    python3-dev \
    python3-pip \
    python3-setuptools \
    pkg-config \
    autoconf \
    automake \
    bison \
    flex \
    libtool \
    protobuf-compiler \
    libprotobuf-dev \
    binutils-dev \
    libssl-dev \
    zlib1g-dev \
    uuid-dev \
    libjsoncpp-dev \
    libbrotli-dev \
    libboost-all-dev \
    libdouble-conversion-dev \
    libevent-dev \
    libfmt-dev \
    libgflags-dev \
    libgoogle-glog-dev \
    libaio-dev \
    libdwarf-dev \
    libiberty-dev \
    libsodium-dev \
    libunwind-dev \
    liblz4-dev \
    liblzma-dev \
    libsnappy-dev \
    libzstd-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch ${FASTFLOAT_TAG} https://github.com/fastfloat/fast_float.git /tmp/fast_float \
        && mkdir -p /opt/fast_float/include \
        && cp -R /tmp/fast_float/include/fast_float /opt/fast_float/include/ \
        && git clone --depth 1 --branch ${FOLLY_TAG} https://github.com/facebook/folly.git /tmp/folly \
        && cmake -S /tmp/folly -B /tmp/folly-build -G Ninja \
            -DCMAKE_BUILD_TYPE=Release \
            -DCMAKE_C_COMPILER=clang-${LLVM_VERSION} \
            -DCMAKE_CXX_COMPILER=clang++-${LLVM_VERSION} \
            -DCMAKE_INSTALL_PREFIX=/opt/folly \
            -DCMAKE_CXX_STANDARD=20 \
            -DFASTFLOAT_INCLUDE_DIR=/opt/fast_float/include \
            -DBUILD_SHARED_LIBS=OFF \
            -DBUILD_TESTS=OFF \
            -DCMAKE_PREFIX_PATH=/usr \
        && cmake --build /tmp/folly-build --target install --parallel $(nproc) \
        && rm -rf /tmp/fast_float /tmp/folly /tmp/folly-build

WORKDIR /src
COPY . /src

RUN cmake -S /src -B /build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_COMPILER=clang-${LLVM_VERSION} \
    -DCMAKE_CXX_COMPILER=clang++-${LLVM_VERSION} \
    -DCMAKE_EXE_LINKER_FLAGS="-fuse-ld=lld-${LLVM_VERSION}" \
    -DCMAKE_SHARED_LINKER_FLAGS="-fuse-ld=lld-${LLVM_VERSION}" \
    -DCMAKE_PREFIX_PATH=/opt/folly \
    -DSCIP_PROTO_REF=${SCIP_PROTO_REF} \
    -DGRAPHIFY_REQUIRE_FOLLY=ON \
    -DGRAPHIFY_BUILD_TESTS=OFF \
    -DGRAPHIFY_BUILD_TOOLS=ON \
    -DGRAPHIFY_BUILD_SERVER=ON \
    -DGRAPHIFY_REQUIRE_STD_PRINT=ON \
    && cmake --build /build --target scip_ingest_server \
    && cmake --install /build --prefix /opt/graphify \
    && ldd /opt/graphify/bin/scip_ingest_server | tee /opt/graphify/ldd.scip_ingest_server.txt

FROM nvcr.io/nvidia/tensorrt-llm/release:${TENSORRT_LLM_VERSION} AS runtime

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libprotobuf-dev \
    libssl3 \
    zlib1g \
    libuuid1 \
    libjsoncpp25 \
    libbrotli1 \
    libboost-context-dev \
    libboost-filesystem-dev \
    libboost-program-options-dev \
    libboost-regex-dev \
    libboost-system-dev \
    libboost-thread-dev \
    libdouble-conversion3 \
    libevent-2.1-7 \
    libfmt9 \
    libgflags2.2 \
    libgoogle-glog0v6 \
    libaio1t64 \
    libdwarf1 \
    libelf1t64 \
    libsodium23 \
    libunwind8 \
    liblz4-1 \
    liblzma5 \
    libsnappy1v5 \
    libzstd1 \
    liburing2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/graphify /opt/graphify

EXPOSE 8888
ENTRYPOINT ["/opt/graphify/bin/scip_ingest_server"]
