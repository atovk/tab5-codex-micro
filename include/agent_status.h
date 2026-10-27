#pragma once

#include <stdint.h>

// Interpretation of the installed desktop app's default per-slot lighting.
// The wire protocol carries a color/effect, not an explicit task status.
namespace AgentStatus {

enum class Kind : uint8_t {
  Idle,
  Running,
  Unread,
  Attention,
  Error,
  Unknown,
  Off,
  Disconnected,
};

// Effect 4 can mean selected OR an explicit pulse. Effect 6 is the SDK's
// shallow breath, and is not emitted by the app's normal status mapper.
constexpr bool pulseHint(uint8_t effect) {
  return effect == 4 || effect == 6;
}

// Brightness may be normalized (0..1) or the firmware's 0..255 value: only
// whether it is positive is relevant. Do not quantize or mask the RGB input;
// near colors and colors with extra high bits are intentionally unknown.
constexpr Kind classify(uint32_t color, float brightness, uint8_t effect,
                        bool connected) {
  if (!connected) return Kind::Disconnected;
  if (effect == 0 || brightness <= 0) return Kind::Off;
  if (brightness != brightness) return Kind::Unknown;  // NaN.
  if (effect != 1 && effect != 4) return Kind::Unknown;

  switch (color) {
    case 0x304FFEu: return Kind::Running;
    case 0x00FF4Cu: return Kind::Unread;
    case 0xFFFFFFu: return Kind::Idle;
    // Approval and input share a color, so they cannot be distinguished.
    case 0xFF6D00u: return Kind::Attention;
    case 0xFF0033u: return Kind::Error;
    default: return Kind::Unknown;
  }
}

constexpr const char* label(Kind kind) {
  switch (kind) {
    case Kind::Idle: return "待命";
    case Kind::Running: return "运行中";
    case Kind::Unread: return "有更新";
    case Kind::Attention: return "待处理";
    case Kind::Error: return "异常";
    case Kind::Unknown: return "状态灯";
    case Kind::Off: return "";
    case Kind::Disconnected: return "离线";
  }
  return "状态灯";
}

}  // namespace AgentStatus
