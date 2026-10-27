#pragma once
#include <cstdint>

// Local display language only. Native HID, diagnostics and chat text retain
// their original values. Setters do not allocate or touch persistent storage.
namespace UiLocale {
enum class Language : uint8_t { Chinese = 0, English = 1 };
Language language();
void setLanguage(Language value);
bool english();
const char* text(const char* chinese, const char* english);
uint32_t revision();
}  // namespace UiLocale
