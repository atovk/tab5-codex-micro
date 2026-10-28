#include "game_lifecycle.h"

#include <cstdint>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>

namespace {
using GameLifecycle::Phase;
using GameLifecycle::Policy;

void require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}

void firstEntryAndPicker() {
  Policy policy;
  auto frame = policy.frame(1000, false, Phase::Running);
  require(!frame.active && !frame.canAdvance(false, frame.phase), "starts offscreen");
  policy.setActive(true);
  frame = policy.frame(1050, false, Phase::Running);
  require(frame.active && frame.redraw && frame.discardInput && frame.elapsedMs == 0,
          "first entry must draw without a stale touch or elapsed time");
  require(frame.phase == Phase::Running && !frame.canAdvance(false, frame.phase),
          "picker must not pause or advance a game");
  policy.setActive(false);
  policy.setActive(true);
  frame = policy.frame(1150, false, Phase::Running);
  require(frame.phase == Phase::Running, "re-entering picker must not create a paused game");
}

void leaveAndReturnBoundaries() {
  // Includes same-frame departure, the old 500ms boundary, and a long absence.
  for (uint32_t absence : {0u, 100u, 499u, 500u, 501u, 5000u}) {
    Policy policy;
    policy.setActive(true);
    policy.frame(1000, true, Phase::Running);
    policy.setActive(false);
    policy.setActive(true);  // No offscreen render: the departure must be latched.
    auto frame = policy.frame(1000 + absence, true, Phase::Running);
    require(frame.phase == Phase::Paused && !frame.canAdvance(true, frame.phase),
            "every departure must pause before the returning game can advance");
    require(frame.elapsedMs == 0 && frame.discardInput && frame.redraw,
            "return must reset time and discard cross-page touch input");
    frame = policy.frame(1020 + absence, true, frame.phase);
    require(frame.phase == Phase::Paused && !frame.canAdvance(true, frame.phase),
            "return pause must persist until explicit Resume");
    require(!frame.discardInput, "subsequent fresh touches must be accepted");
    require(frame.canAdvance(true, Phase::Running), "explicit Resume must allow play");
  }
}

void offscreenFramesCannotAdvanceProgress() {
  Policy policy;
  policy.setActive(true);
  Phase phase = Phase::Running;
  uint32_t ticks = 0, simulatedPosition = 0;
  auto tick = [&](uint32_t now) {
    const auto frame = policy.frame(now, true, phase);
    phase = frame.phase;
    if (frame.canAdvance(true, phase)) {
      ++ticks;
      simulatedPosition += frame.elapsedMs;
    }
    return frame;
  };
  tick(1000);
  tick(1020);
  const uint32_t savedTicks = ticks, savedPosition = simulatedPosition;
  policy.setActive(false);
  for (uint32_t now : {1120u, 1520u, 6020u, 12020u}) {
    const auto frame = tick(now);
    require(!frame.active && frame.elapsedMs == 0, "hidden frames cannot accumulate time");
    require(ticks == savedTicks && simulatedPosition == savedPosition,
            "ticks and game progress must be preserved offscreen");
  }
  policy.setActive(true);
  const auto frame = tick(12100);
  require(phase == Phase::Paused && frame.elapsedMs == 0,
          "return after offscreen frames must remain paused");
  require(ticks == savedTicks && simulatedPosition == savedPosition,
          "first returning frame must preserve progress");
  phase = Phase::Running;
  tick(12120);
  require(ticks == savedTicks + 1 && simulatedPosition == savedPosition + 20,
          "Resume must advance by one fresh frame, without time spent away");
}

void longVisibleFramesDoNotPause() {
  Policy policy;
  policy.setActive(true);
  policy.frame(100, true, Phase::Running);
  for (uint32_t now : {200u, 700u, 5700u}) {
    policy.setActive(true);  // Repeated visibility reports are not transitions.
    const auto frame = policy.frame(now, true, Phase::Running);
    require(frame.phase == Phase::Running && frame.canAdvance(true, frame.phase),
            "a slow redraw while visible must not be treated as leaving");
    require(!frame.discardInput && !frame.redraw && frame.elapsedMs == 80,
            "long visible frames must retain the existing simulation timestep cap");
  }
}

void pausedAndFinishedGamesKeepTheirState() {
  for (Phase phase : {Phase::Paused, Phase::Over}) {
    Policy policy;
    policy.setActive(true);
    policy.frame(100, true, phase);
    policy.setActive(false);
    policy.setActive(true);
    auto frame = policy.frame(200, true, phase);
    require(frame.phase == phase && !frame.canAdvance(true, frame.phase),
            "departure must not resume paused games or convert Game over into Paused");
  }
}

void multipleTransitionsBeforeRendering() {
  Policy policy;
  policy.setActive(true);
  policy.frame(0, true, Phase::Running);
  for (int i = 0; i < 3; ++i) {
    policy.setActive(false);
    policy.setActive(false);
    policy.setActive(true);
  }
  auto frame = policy.frame(1, true, Phase::Running);
  require(frame.active && frame.phase == Phase::Paused && frame.discardInput,
          "coalesced page switches must retain a pending pause");
  frame = policy.frame(21, true, Phase::Running);  // User explicitly resumed.
  require(frame.phase == Phase::Running && !frame.discardInput,
          "a consumed departure must not repeatedly pause a resumed game");
}

void hiddenRunningStateIsAlwaysGated() {
  Policy policy;
  for (uint32_t now : {0u, 100u, 500u, 5000u}) {
    const auto frame = policy.frame(now, true, Phase::Running);
    require(!frame.canAdvance(true, Phase::Running) && frame.elapsedMs == 0,
            "visibility must independently gate simulation, even without a pause transition");
  }
}

void timestampWrapAndReentry() {
  Policy policy;
  const uint32_t start = std::numeric_limits<uint32_t>::max() - 10;
  policy.setActive(true);
  policy.frame(start, true, Phase::Running);
  auto frame = policy.frame(start + uint32_t(20), true, Phase::Running);
  require(frame.elapsedMs == 20 && frame.canAdvance(true, frame.phase),
          "normal frame time must survive millis wrap");
  policy.setActive(false);
  policy.setActive(true);
  frame = policy.frame(start + uint32_t(100), true, Phase::Running);
  require(frame.elapsedMs == 0 && frame.phase == Phase::Paused,
          "crossing millis wrap must not change return behavior");
}
}  // namespace

int main() {
  struct Test { const char* name; void (*run)(); };
  const Test tests[] = {
    {"initial entry and picker", firstEntryAndPicker},
    {"0/100/499/500/501/5000ms return", leaveAndReturnBoundaries},
    {"offscreen progress preservation", offscreenFramesCannotAdvanceProgress},
    {"long visible frame", longVisibleFramesDoNotPause},
    {"paused and finished state", pausedAndFinishedGamesKeepTheirState},
    {"coalesced page switches", multipleTransitionsBeforeRendering},
    {"independent visibility gate", hiddenRunningStateIsAlwaysGated},
    {"millis wrap", timestampWrapAndReentry},
  };
  for (const auto& test : tests) {
    try { test.run(); }
    catch (const std::exception& error) {
      std::cerr << test.name << ": " << error.what() << '\n';
      return 1;
    }
  }
  std::cout << "8/8 game lifecycle tests passed\n";
  return 0;
}
