#pragma once

#include "micro_ui.h"
#include <stdint.h>

// Touch mechanics only. The desktop remains responsible for the meaning of
// native Micro events, including dial modes, long presses and voice gestures.
namespace MicroControls {

enum class StickDirection : int8_t { Neutral = -1, Right = 0, Down = 1, Left = 2, Up = 3 };
constexpr float StickActivationThreshold = .5f;
// Normalized screen-space vector. Painting and input use the same activation
// threshold and single cardinal sector, including diagonal boundary ties.
StickDirection stickDirection(float x, float y);

struct Visual {
  int held = -1;
  int32_t detent = 0;
  float dialAngle = 0;
  bool dialPressed = false;
  bool dialTurning = false;
  float stickX = 0;
  float stickY = 0;
  bool stickActive = false;
  uint32_t pressStarted = 0;
  uint32_t revision = 0;
};

void reset();
// ACT06..ACT12 bits. Only the UI's fresh, explicitly known single-shot
// Approve/Reject/Send mappings may opt in. Other keys retain native timing.
void setReleaseActionMask(uint8_t mask);
// Call when a mapping changes without changing the mask, or its link is lost.
// Cancels only a not-yet-submitted action; it never releases a native hold.
void cancelDeferredAction();
void sample(int16_t x, int16_t y, bool pressed, uint32_t now);
// Release the captured key / return the stick to neutral before changing page.
// Pending turns are discarded, but a release is retained if needed.
void cancel();
bool nextEvent(MicroUI::Event& event);
Visual visual();

}  // namespace MicroControls
