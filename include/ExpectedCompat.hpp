#pragma once

#include <memory>
#include <type_traits>
#include <utility>
#include <variant>

namespace graphify {

template <typename E>
class Unexpected final {
 public:
  using error_type = E;

  Unexpected(const Unexpected&) = default;
  Unexpected(Unexpected&&) noexcept(std::is_nothrow_move_constructible_v<E>) = default;
  Unexpected& operator=(const Unexpected&) = default;
  Unexpected& operator=(Unexpected&&) noexcept(std::is_nothrow_move_assignable_v<E>) = default;

  explicit Unexpected(const E& error) : error_(error) {}
  explicit Unexpected(E&& error) noexcept(std::is_nothrow_move_constructible_v<E>) : error_(std::move(error)) {}

  [[nodiscard]] E& error() & noexcept { return error_; }
  [[nodiscard]] const E& error() const& noexcept { return error_; }
  [[nodiscard]] E&& error() && noexcept { return std::move(error_); }

 private:
  E error_;
};

template <typename E>
[[nodiscard]] Unexpected<std::decay_t<E>> unexpected(E&& error) {
  return Unexpected<std::decay_t<E>>(std::forward<E>(error));
}

template <typename T, typename E>
class Expected final {
 public:
  using value_type = T;
  using error_type = E;
  template <typename U>
  using RemoveCvRef = std::remove_cv_t<std::remove_reference_t<U>>;

  template <typename U = T, typename = std::enable_if_t<std::is_default_constructible_v<U>>>
  Expected() : storage_(std::in_place_index<0>) {}

  Expected(const Expected&) = default;
  Expected(Expected&&) noexcept = default;
  Expected& operator=(const Expected&) = default;
  Expected& operator=(Expected&&) noexcept = default;

  template <
      typename U,
      typename = std::enable_if_t<
          !std::is_same_v<RemoveCvRef<U>, Expected> &&
          !std::is_same_v<RemoveCvRef<U>, Unexpected<E>> &&
          std::is_constructible_v<T, U&&>>>
  Expected(U&& value) : storage_(std::in_place_index<0>, std::forward<U>(value)) {}

  Expected(Unexpected<E> error) : storage_(std::in_place_index<1>, std::move(error).error()) {}

  [[nodiscard]] bool has_value() const noexcept { return storage_.index() == 0; }

  [[nodiscard]] explicit operator bool() const noexcept { return has_value(); }

  [[nodiscard]] T& value() & { return std::get<0>(storage_); }
  [[nodiscard]] const T& value() const& { return std::get<0>(storage_); }
  [[nodiscard]] T&& value() && { return std::get<0>(std::move(storage_)); }
  [[nodiscard]] const T&& value() const&& { return std::get<0>(std::move(storage_)); }

  [[nodiscard]] E& error() & { return std::get<1>(storage_); }
  [[nodiscard]] const E& error() const& { return std::get<1>(storage_); }
  [[nodiscard]] E&& error() && { return std::get<1>(std::move(storage_)); }

  [[nodiscard]] T& operator*() & { return value(); }
  [[nodiscard]] const T& operator*() const& { return value(); }
  [[nodiscard]] T&& operator*() && { return std::get<0>(std::move(storage_)); }
  [[nodiscard]] const T&& operator*() const&& { return std::get<0>(std::move(storage_)); }

  [[nodiscard]] T* operator->() { return std::addressof(value()); }
  [[nodiscard]] const T* operator->() const { return std::addressof(value()); }

 private:
  std::variant<T, E> storage_;
};

template <typename E>
class Expected<void, E> final {
 public:
  using value_type = void;
  using error_type = E;

  Expected() = default;

  Expected(const Expected&) = default;
  Expected(Expected&&) noexcept = default;
  Expected& operator=(const Expected&) = default;
  Expected& operator=(Expected&&) noexcept = default;

  Expected(Unexpected<E> error) : storage_(std::in_place_index<1>, std::move(error).error()) {}

  [[nodiscard]] bool has_value() const noexcept { return storage_.index() == 0; }

  [[nodiscard]] explicit operator bool() const noexcept { return has_value(); }

  void value() const { static_cast<void>(std::get<0>(storage_)); }

  [[nodiscard]] E& error() & { return std::get<1>(storage_); }
  [[nodiscard]] const E& error() const& { return std::get<1>(storage_); }
  [[nodiscard]] E&& error() && { return std::get<1>(std::move(storage_)); }

 private:
  std::variant<std::monostate, E> storage_{};
};

} // namespace graphify