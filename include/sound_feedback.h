#pragma once

#include "micro_ui.h"

#include <cmath>
#include <cstring>
#include <stdint.h>

// Local feedback only: this policy neither sends native inputs nor plays audio.
namespace Feedback {

enum class Cue : uint8_t {
  None,
  Press,
  DetentCW,
  DetentCC,
  Direction,
  Neutral,
};

class Policy {
 public:
  // Call for every event, including while muted or capturing diagnostics.
  // The caller applies master/capture suppression AFTER this state update,
  // and owns audio timing/cooldowns. Key events already represent UI edges.
  Cue event(const MicroUI::Event& value) {
    if (!std::isfinite(value.angle) || !std::isfinite(value.distance)
        || value.angle < 0 || value.angle > 1
        || value.distance < 0 || value.distance > 1) return Cue::None;

    if (value.joystick) {
      // Native joystick samples have no key or key action. Reject ambiguous
      // mixed events without changing the last valid direction.
      if (value.key != nullptr || value.action != 0) return Cue::None;
      if (value.distance <= .1f) {
        if (direction_ < 0) return Cue::None;
        direction_ = -1;
        return Cue::Neutral;
      }
      // Hysteresis prevents near-center displacement noise from rearming a
      // direction. A different angle in this band also keeps the old sector.
      if (value.distance < .5f) return Cue::None;

      // Clockwise normalized turns: right=0, down=.25, left=.5, up=.75.
      // Boundary ties enter the following sector; angle 1 aliases angle 0.
      const int8_t next = static_cast<int>((value.angle + .125f) * 4) % 4;
      if (next == direction_) return Cue::None;
      direction_ = next;
      return Cue::Direction;
    }

    if (value.key == nullptr) return Cue::None;
    if (std::strcmp(value.key, "ENC_CW") == 0)
      return value.action == 2 ? Cue::DetentCW : Cue::None;
    if (std::strcmp(value.key, "ENC_CC") == 0)
      return value.action == 2 ? Cue::DetentCC : Cue::None;
    if (value.action != 1) return Cue::None;

    static const char* const keys[] = {
        "AG00", "AG01", "AG02", "AG03", "AG04", "AG05",
        "ACT06", "ACT07", "ACT08", "ACT09", "ACT10", "ACT11", "ACT12",
        "ENC",
    };
    for (const char* key : keys)
      if (std::strcmp(value.key, key) == 0) return Cue::Press;
    return Cue::None;
  }

  // Reset is silent. Do not reset merely because audio is being muted.
  void reset() { direction_ = -1; }

 private:
  int8_t direction_ = -1;
};

}  // namespace Feedback
