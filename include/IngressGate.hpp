#pragma once

#include "ExpectedCompat.hpp"
#include "ProtocolModels.hpp"

#include <cstddef>
#include <span>

namespace graphify {

[[nodiscard]] Expected<void, IngressError> would_accept_binary_size(std::size_t payload_size,
                                                                    GateLimits limits = {}) noexcept;

[[nodiscard]] Expected<IngressDecision, IngressError> classify_frame(FrameKind kind,
                                                                    std::span<const std::byte> payload,
                                                                    IngressSession& session,
                                                                    GateLimits limits = {});

} // namespace graphify
