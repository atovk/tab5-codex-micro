#include "agent_status.h"

#include <cstring>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>

namespace {
using AgentStatus::Kind;
using AgentStatus::classify;

void require(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}

void expect(uint32_t color, float brightness, uint8_t effect, bool connected,
            Kind expected, const char* description) {
  require(classify(color, brightness, effect, connected) == expected,
          description);
}

void defaultStatusColors() {
  struct Case { uint32_t color; Kind kind; };
  constexpr Case cases[] = {
      {0x304FFE, Kind::Running}, {0x00FF4C, Kind::Unread},
      {0xFFFFFF, Kind::Idle}, {0xFF6D00, Kind::Attention},
      {0xFF0033, Kind::Error},
  };
  for (const auto& item : cases) {
    for (uint8_t effect : {1, 4}) {
      for (float brightness : {0.01f, 1.0f, 255.0f})
        expect(item.color, brightness, effect, true, item.kind,
               "default color status changed with selection or brightness");
    }
  }
  expect(0xFFFFFF, 255, 4, true, Kind::Idle,
         "selected white is idle, not running");
  expect(0x304FFE, 255, 1, true, Kind::Running,
         "steady blue must retain the running signal");
  expect(0x304FFE, 255, 4, true, Kind::Running,
         "breathing blue must retain the running signal");
  // Both app attention states use the same wire tuple and have one label.
  require(std::strcmp(AgentStatus::label(classify(0xFF6D00, 1, 1, true)),
                      "待处理") == 0,
          "orange must use the merged attention label");
}

void offIsNotIdle() {
  expect(0x304FFE, 0, 4, true, Kind::Off,
         "zero brightness must suppress a stale blue running signal");
  expect(0xFFFFFF, 0, 1, true, Kind::Off,
         "unlit white must not claim idle");
  expect(0xFF0033, 255, 0, true, Kind::Off,
         "effect zero must suppress a stale error color");
  expect(0x304FFE, -1, 1, true, Kind::Off,
         "nonpositive brightness must not claim running");
  expect(0, 0, 0, true, Kind::Off, "an all-zero slot is off");
  require(AgentStatus::label(Kind::Off)[0] == '\0',
          "off must not display an idle or task-state badge");
}

void unknownSignalsStayUnknown() {
  for (uint32_t color : {0u, 0x304FFDu, 0x304FFFu, 0x314FFEu,
                         0xFF304FFEu, 0xFFFFFFFFu, 0x123456u})
    expect(color, 1, 1, true, Kind::Unknown,
           "unrecognized or approximate color must remain unknown");

  for (unsigned effect = 0; effect <= 255; ++effect) {
    const Kind expected = effect == 0 ? Kind::Off
        : effect == 1 || effect == 4 ? Kind::Running : Kind::Unknown;
    expect(0x304FFE, 1, static_cast<uint8_t>(effect), true, expected,
           "only the app's normal effects may classify a default color");
  }
  expect(0xFFFFFF, 1, 6, true, Kind::Unknown,
         "SDK shallow breath is not a normal selected-idle report");
  expect(0x304FFE, std::numeric_limits<float>::quiet_NaN(), 1, true,
         Kind::Unknown, "invalid brightness must not claim running");
}

void disconnectTakesPrecedence() {
  for (uint32_t color : {0u, 0x304FFEu, 0xFFFFFFu, 0x123456u}) {
    for (uint8_t effect : {0, 1, 4, 6, 255}) {
      for (float brightness : {0.0f, 1.0f, 255.0f})
        expect(color, brightness, effect, false, Kind::Disconnected,
               "disconnect must override off, stale and unknown signals");
    }
  }
}

void pulseDoesNotMeanRunning() {
  for (unsigned effect = 0; effect <= 255; ++effect)
    require(AgentStatus::pulseHint(static_cast<uint8_t>(effect)) ==
                (effect == 4 || effect == 6),
            "pulse hint must only identify breathing effects");
  expect(0xFF6D00, 1, 4, true, Kind::Attention,
         "breathing attention must not become running");
  expect(0xFF0033, 1, 4, true, Kind::Error,
         "breathing error must not become running");
  expect(0x304FFE, 1, 6, true, Kind::Unknown,
         "shallow breath must not establish running");
}

void labelsRemainDistinct() {
  struct Case { Kind kind; const char* label; };
  const Case cases[] = {
      {Kind::Idle, "待命"}, {Kind::Running, "运行中"},
      {Kind::Unread, "有更新"}, {Kind::Attention, "待处理"},
      {Kind::Error, "异常"}, {Kind::Unknown, "状态灯"},
      {Kind::Off, ""}, {Kind::Disconnected, "离线"},
  };
  for (const auto& item : cases)
    require(std::strcmp(AgentStatus::label(item.kind), item.label) == 0,
            "status label changed unexpectedly");
}

static_assert(classify(0xFFFFFF, 1, 4, true) == Kind::Idle,
              "classification must also work at compile time");
static_assert(AgentStatus::pulseHint(6), "SDK shallow breath is a pulse hint");
}  // namespace

int main() {
  try {
    defaultStatusColors();
    offIsNotIdle();
    unknownSignalsStayUnknown();
    disconnectTakesPrecedence();
    pulseDoesNotMeanRunning();
    labelsRemainDistinct();
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
  std::cout << "6/6 agent status tests passed\n";
}
