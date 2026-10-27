#pragma once

#include "micro_ui.h"
#include <stdint.h>

// Touch mechanics only. The desktop remains responsible for the meaning of
// native Micro events, including dial modes, long presses and voice gestures.
namespace MicroControls {

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
void sample(int16_t x, int16_t y, bool pressed, uint32_t now);
// Release the captured key / return the stick to neutral before changing page.
// Pending turns are discarded, but a release is retained if needed.
void cancel();
bool nextEvent(MicroUI::Event& event);
Visual visual();

}  // namespace MicroControls
