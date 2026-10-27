#include "ui_locale.h"
#include <atomic>

namespace UiLocale {
namespace {
// Low bit is the language; upper bits are a change generation. One atomic
// snapshot makes language changes safe across render and input sampling.
std::atomic<uint32_t> state {1};
}
Language language() { return Language(state.load(std::memory_order_relaxed) & 1u); }
bool english() { return language() == Language::English; }
const char* text(const char* chinese, const char* englishText) {
  return english() ? englishText : chinese;
}
uint32_t revision() { return state.load(std::memory_order_relaxed) >> 1; }
void setLanguage(Language value) {
  if (value != Language::Chinese && value != Language::English) return;
  uint32_t current = state.load(std::memory_order_relaxed);
  while ((current & 1u) != uint32_t(value)) {
    const uint32_t next = ((current + 2u) & ~1u) | uint32_t(value);
    if (state.compare_exchange_weak(current, next, std::memory_order_relaxed)) return;
  }
}
}  // namespace UiLocale
