#include "ui_locale.h"
#include <cassert>
#include <cstdio>

int main() {
  using UiLocale::Language;
  static constexpr char chinese[] = "Chinese text";
  static constexpr char english[] = "English text";
  assert(UiLocale::language() == Language::English);
  assert(UiLocale::english());
  assert(UiLocale::text(chinese, english) == english);
  const uint32_t initial = UiLocale::revision();

  UiLocale::setLanguage(Language::English);
  assert(UiLocale::revision() == initial);
  UiLocale::setLanguage(Language::Chinese);
  assert(UiLocale::language() == Language::Chinese);
  assert(!UiLocale::english());
  assert(UiLocale::text(chinese, english) == chinese);
  assert(UiLocale::revision() == initial + 1);
  UiLocale::setLanguage(Language::Chinese);
  assert(UiLocale::revision() == initial + 1);

  UiLocale::setLanguage(static_cast<Language>(2));
  UiLocale::setLanguage(static_cast<Language>(255));
  assert(UiLocale::language() == Language::Chinese);
  assert(UiLocale::revision() == initial + 1);

  UiLocale::setLanguage(Language::English);
  assert(UiLocale::english());
  assert(UiLocale::revision() == initial + 2);
  for (uint32_t index = 0; index != 256; ++index) {
    UiLocale::setLanguage(Language::Chinese);
    UiLocale::setLanguage(Language::English);
  }
  assert(UiLocale::text(chinese, english) == english);
  assert(UiLocale::revision() == initial + 514);
  std::puts("UI locale contracts passed");
}
