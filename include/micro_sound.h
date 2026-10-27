#pragma once
#include "micro_ui.h"
#include "sound_feedback.h"
#include <cstdint>
class Print;

namespace MicroSound {
struct Diagnostics {
  bool available=false, enabled=true, running=false;
  uint8_t volume=45, master=0;
  uint32_t queued=0, played=0, released=0, dropped=0, suppressed=0;
  uint32_t enqueueMax=0, startMax=0;
};
bool begin(bool enabled, uint8_t volume);
void configure(bool enabled, uint8_t volume);
void event(const MicroUI::Event& event, bool capture);
void localPress(bool capture);
bool test(Feedback::Cue cue);
bool referenceTone();
void hardwareDiagnostics(Print& output);
Diagnostics diagnostics();
}
