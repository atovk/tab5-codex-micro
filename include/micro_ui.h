#pragma once

#include <stdint.h>
#include "host_feedback.h"

namespace MicroUI {

enum class Theme : uint8_t { Light, Dark };

struct Slot {
  uint32_t color = 0;
  uint8_t brightness = 0;
  uint8_t effect = 0;
  uint8_t speed = 0;
  char title[192] = {};
  char identity[96] = {};
};

struct State {
  Slot slots[6];
  bool connected = false;
  bool titlesFresh = false;
  uint32_t rpc_count = 0;
  bool lightingPreview = false;
  HostFeedback::Config hostConfig;
  HostFeedback::Chat hostChat;
  HostFeedback::Lights hostLights;
  uint32_t actionFeedbackAt[7]={};
  bool actionFeedbackSent[7]={};
};

struct Event {
  const char* key = nullptr;
  uint8_t action = 0;
  bool joystick = false;
  float angle = 0;
  float distance = 0;
};

struct RenderProfile {
  uint32_t dialRestore = 0, dialMarks = 0, dialArc = 0;
  uint32_t counterRestore = 0, counterText = 0, dialPresent = 0, writeback = 0;
  uint32_t neonDraw = 0, neonPresent = 0, neonFrames = 0;
  uint32_t titleDraw = 0, titlePresent = 0, titleFrames = 0;
};
const RenderProfile& renderProfile();
void resetRenderProfile();

struct TitleScrollStatus {
  uint8_t lines=0, visibleLines=0;
  uint16_t width=0, height=0, lineHeight=0, offset=0, maxOffset=0;
  uint32_t elapsed=0, cycle=0, frames=0, cachedBytes=0;
  bool paused=false, ready=false, manual=false;
};
TitleScrollStatus titleScrollStatus(uint8_t index);
// Diagnostic clock injection is gated by INPUT_TEST in the serial dispatcher.
bool setTitleScrollTest(uint8_t slot, uint32_t now);
void clearTitleScrollTest();

bool init();
bool render(const State& state);
// Present the complete retained scene again after display orientation changes.
void invalidateDisplay();
Event touch(int16_t x, int16_t y, bool pressed);
bool pollEvent(Event& event);
bool playing();
Theme theme();
void setTheme(Theme value);
void toggleTheme();
// Local switch state; the sound engine owns playback and persistence.
void setSoundEnabled(bool enabled);
bool soundEnabled();
// Percentage values; brightness stays in 10..100. Setters do not toggle sound.
uint8_t volume();
uint8_t brightness();
void setVolume(uint8_t value);
void setBrightness(uint8_t value);
bool settingsOpen();
// Diagnostic baseline: retain scene updates, but present all 921,600 pixels.
void setFullPresent(bool enabled);
void setEffectsFrozen(bool frozen);
bool effectsAreFrozen();
const uint16_t* framebuffer();
int bufferWidth();
int bufferHeight();

}  // namespace MicroUI
