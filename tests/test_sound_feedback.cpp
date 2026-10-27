#include "sound_feedback.h"

#include <cmath>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>

namespace {
using Feedback::Cue;
using Feedback::Policy;

void require(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}

MicroUI::Event key(const char* name, uint8_t action) {
  MicroUI::Event value;
  value.key = name;
  value.action = action;
  return value;
}

MicroUI::Event stick(float angle, float distance) {
  MicroUI::Event value;
  value.joystick = true;
  value.angle = angle;
  value.distance = distance;
  return value;
}

void expected(Policy& policy, const MicroUI::Event& value, Cue cue,
              const char* message) {
  require(policy.event(value) == cue, message);
}

void legalKeyEdges() {
  Policy policy;
  const char* keys[] = {
      "AG00", "AG01", "AG02", "AG03", "AG04", "AG05",
      "ACT06", "ACT07", "ACT08", "ACT09", "ACT10", "ACT11", "ACT12", "ENC",
  };
  for (const char* name : keys) {
    expected(policy, key(name, 1), Cue::Press, "legal key press must produce a tap");
    // A held UI key produces empty events, not repeated action=1 edges.
    for (int sample = 0; sample < 20; ++sample)
      expected(policy, MicroUI::Event{}, Cue::None, "holding must stay silent");
    expected(policy, key(name, 0), Cue::None, "key release must stay silent");
    expected(policy, key(name, 2), Cue::None, "wrong key action must stay silent");
    expected(policy, key(name, 255), Cue::None, "unknown action must stay silent");
    expected(policy, key(name, 1), Cue::Press, "a subsequent press must sound again");
  }
}

void directionalDetents() {
  Policy policy;
  for (int detent = 0; detent < 48; ++detent) {
    expected(policy, key("ENC_CW", 2), Cue::DetentCW, "clockwise detent was lost");
    expected(policy, key("ENC_CC", 2), Cue::DetentCC, "counterclockwise detent was lost");
  }
  for (uint8_t action : {0, 1, 3, 255}) {
    expected(policy, key("ENC_CW", action), Cue::None, "wrong CW action must be ignored");
    expected(policy, key("ENC_CC", action), Cue::None, "wrong CC action must be ignored");
  }
  expected(policy, key("ENC", 1), Cue::Press, "encoder center press must sound");
  expected(policy, key("ENC", 0), Cue::None, "encoder center release must stay silent");
}

void strictKeyNames() {
  Policy policy;
  const char* invalid[] = {
      nullptr, "", "AG0", "AG06", "AG000", "AG05x", "ag00", " AG00", "AG00 ",
      "ACT05", "ACT13", "ACT6", "ACT060", "act06", "ACT10_ACT11",
      "ENC_CWjunk", "ENC_CC ", "ENC_CW\n", "ENC_LONG", "enc", "ArrowUp",
  };
  for (const char* name : invalid)
    for (uint8_t action : {0, 1, 2, 255})
      expected(policy, key(name, action), Cue::None, "unknown key name produced a cue");
}

void stickHysteresisAndNeutral() {
  Policy policy;
  expected(policy, stick(0, 0), Cue::None, "initial center must stay silent");
  expected(policy, stick(.75f, .1f), Cue::None, "initial near-center must stay silent");
  expected(policy, stick(0, .4999f), Cue::None, "subthreshold travel must stay silent");
  expected(policy, stick(0, .5f), Cue::Direction, "threshold entry must sound once");
  expected(policy, stick(.10f, 1), Cue::None, "same-sector travel must stay silent");
  expected(policy, stick(.25f, .3f), Cue::None, "intermediate travel must not switch sectors");
  expected(policy, stick(0, .5f), Cue::None, "intermediate travel must preserve the old sector");
  expected(policy, stick(.25f, .5f), Cue::Direction, "new-sector entry must sound");
  expected(policy, stick(.5f, .1001f), Cue::None, "above-neutral travel must keep its sector");
  expected(policy, stick(.25f, .5f), Cue::None, "hysteresis must not rearm the same sector");
  expected(policy, stick(.75f, .1f), Cue::Neutral, "returning to neutral must sound once");
  expected(policy, stick(0, 0), Cue::None, "repeated center must stay silent");
  expected(policy, stick(.25f, .5f), Cue::Direction, "entry after neutral must sound again");
}

void sectorBoundariesAndWrap() {
  // Each tie belongs to the sector centered clockwise from the tie.
  const float centers[] = {0, .25f, .5f, .75f};
  const float boundaries[] = {.125f, .375f, .625f, .875f};
  for (int index = 0; index < 4; ++index) {
    Policy policy;
    expected(policy, stick(centers[index], 1), Cue::Direction, "sector entry must sound");
    expected(policy, stick(boundaries[index] - .0001f, 1), Cue::None,
             "angle before a boundary must keep its sector");
    expected(policy, stick(boundaries[index], 1), Cue::Direction,
             "exact sector boundary must enter the next sector");
    expected(policy, stick(centers[(index + 1) % 4], 1), Cue::None,
             "boundary and following center must belong to the same sector");
  }
  Policy policy;
  expected(policy, stick(0, 1), Cue::Direction, "right entry must sound");
  expected(policy, stick(1, 1), Cue::None, "angle one must alias angle zero");
  expected(policy, stick(.9999f, 1), Cue::None, "angle wrap must not create a false sector");
}

void malformedSamplesDoNotChangeState() {
  Policy policy;
  const float nan = std::numeric_limits<float>::quiet_NaN();
  const float inf = std::numeric_limits<float>::infinity();
  expected(policy, stick(.25f, 1), Cue::Direction, "initial direction must sound");
  const MicroUI::Event invalid[] = {
      stick(nan, 1), stick(inf, 1), stick(-inf, 1),
      stick(-.0001f, 1), stick(1.0001f, 1),
      stick(0, nan), stick(0, inf), stick(0, -inf),
      stick(0, -.0001f), stick(0, 1.0001f), stick(nan, 0),
  };
  for (const auto& value : invalid) {
    expected(policy, value, Cue::None, "malformed joystick sample produced a cue");
    expected(policy, stick(.25f, 1), Cue::None,
             "malformed sample changed the last valid sector");
  }
  auto mixed = stick(0, 0);
  mixed.key = "AG00";
  expected(policy, mixed, Cue::None, "mixed key/joystick event must be ignored");
  mixed.key = nullptr;
  mixed.action = 1;
  expected(policy, mixed, Cue::None, "joystick with key action must be ignored");
  expected(policy, stick(.25f, 1), Cue::None, "mixed event changed joystick state");
  auto badKey = key("AG00", 1);
  badKey.angle = nan;
  expected(policy, badKey, Cue::None, "nonfinite event fields must stay silent");
  expected(policy, stick(0, 0), Cue::Neutral, "valid neutral after malformed data must still work");
}

void independentInputsAndMutedTracking() {
  Policy policy;
  expected(policy, stick(.5f, 1), Cue::Direction, "initial left entry must sound");
  expected(policy, key("ACT10", 1), Cue::Press, "a legal key should sound while stick is active");
  expected(policy, key("ACT10", 0), Cue::None, "key release must stay silent");
  expected(policy, key("ENC_CW", 2), Cue::DetentCW, "detent should sound while stick is active");
  expected(policy, stick(.5f, 1), Cue::None, "key and detent must not reset joystick state");

  // Audio is muted externally; policy must still consume all these samples.
  (void)policy.event(stick(.75f, 1));
  (void)policy.event(stick(0, 0));
  (void)policy.event(stick(0, 1));
  // Unmute: continued displacement cannot replay an entry that occurred muted.
  expected(policy, stick(.01f, .8f), Cue::None, "unmute replayed an old direction edge");
  expected(policy, stick(.25f, 1), Cue::Direction, "a new direction after unmute must sound");
  (void)policy.event(stick(0, 0));
  expected(policy, stick(0, 0), Cue::None, "unmute replayed an old neutral edge");
  expected(policy, stick(.25f, .5f), Cue::Direction, "new entry after muted neutral must sound");
  policy.reset();
  expected(policy, stick(0, 0), Cue::None, "reset must silently return to neutral");
  expected(policy, stick(.25f, .5f), Cue::Direction, "entry after reset must sound");
}
}  // namespace

int main() {
  try {
    legalKeyEdges();
    directionalDetents();
    strictKeyNames();
    stickHysteresisAndNeutral();
    sectorBoundariesAndWrap();
    malformedSamplesDoNotChangeState();
    independentInputsAndMutedTracking();
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
  std::cout << "7/7 sound feedback tests passed\n";
}
