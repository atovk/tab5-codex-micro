#include "micro_ui.h"
#include "micro_games.h"
#include "micro_controls.h"
#include "material_gradient.h"
#include "ui_font.h"
#include "title_font.h"
#include "title_renderer.h"
#include "title_scroll.h"
#include "ui_text.h"
#include "ui_icons.h"
#include "ui_locale.h"
#include "agent_status.h"
#include "agent_neon.h"

#include <M5Unified.h>
#include <Preferences.h>
#include <esp_heap_caps.h>
#include <math.h>
#include <cstring>
#include <freertos/FreeRTOS.h>
#include <freertos/portmacro.h>

namespace MicroUI {
namespace {

constexpr int W = 1280;
constexpr int H = 720;
struct Materials {
  uint32_t bodyTop, bodyBottom, trayTop, trayBottom, keyTop, keyBottom;
  uint32_t edge, highlight, shadow, deepShadow, ink, secondary, accent;
  uint32_t metalTop, metalBottom, recess, keyPressed;
};
constexpr Materials silver = {
  0xEFF3F7, 0xEFF3F7, 0xE7EBEF, 0xE7EBEF, 0xFFFFFF, 0xF7F9FC,
  0xC3CCD8, 0xFFFFFF, 0xA5AFBD, 0x728096, 0x242B35, 0x697586, 0x3977C5,
  0xFAFCFF, 0xB9C4D3, 0xC6CFDC, 0xE1E8F1,
};
constexpr Materials graphite = {
  0x181C21, 0x181C21, 0x212429, 0x212429, 0x37414D, 0x303A46,
  0x465261, 0x718096, 0x0D1117, 0x05080D, 0xF3F6FA, 0xA4B1C3, 0xB2D1FA,
  0x8A98AC, 0x3E4B60, 0x151C27, 0x2A3648,
};
const Materials* material = &silver;
uint32_t INK = silver.ink;
uint32_t MUTED = silver.secondary;
Theme renderedTheme = Theme::Light;
Theme persistedTheme = Theme::Light;
uint32_t appliedThemeRevision = 0;
uint32_t appliedLocaleRevision = 0;
uint32_t appliedSoundRevision = 0;
uint32_t appliedSettingsRevision = 0;
bool renderedSoundEnabled = true;
uint8_t renderedVolume = 45, renderedBrightness = 24;
uint8_t previousVolume = 45, previousBrightness = 24;
uint8_t renderedSettingsControl = 0, previousSettingsControl = 0;
uint8_t headerHeld = 0;
uint8_t previousHeaderHeld = 0;
Preferences preferences;
bool preferencesReady = false;
uint32_t persistedRevision = 0;
constexpr int DIAL_X = 126, DIAL_Y = 236, DIAL_W = 236, DIAL_H = 236;
constexpr int STICK_X = 680, STICK_Y = 238, STICK_W = 244, STICK_H = 228;
constexpr int CENTER_X = 208, CENTER_Y = 326, CENTER_W = 72, CENTER_H = 64;
constexpr int COUNTER_X = 376, COUNTER_Y = 310, COUNTER_W = 120, COUNTER_H = 49;
constexpr int FEEDBACK_X = 988, FEEDBACK_Y = 274, FEEDBACK_W = 238, FEEDBACK_H = 84;
uint16_t* dialBackdrops[2] = {};
uint16_t* stickBackdrop = nullptr;
uint16_t* longPressCenter = nullptr;
uint16_t* counterBackdrop = nullptr;
bool backdropsReady = false;
uint32_t fxLastStep = 0, lastInteraction = 0;
uint8_t fxCursor = 0;
uint8_t animatedSlots = 0;
bool effectsFrozen = false;
bool previousEffectsFrozen = false;
uint8_t renderedActionFeedback[7] = {};
uint16_t renderedHoldMs = 0;

M5Canvas canvas(&M5.Display);
bool ready = false;
bool playMode = false;
bool settingsMode = false;
bool forceFullPresent = false;
bool finger = false;
int held = -1;
uint32_t lastFrame = 0;
uint32_t frame = 0;
bool displayInvalidated = false;

// Input never waits for a frame or copies pixels.
// The renderer takes a short snapshot so it is also safe to sample touch from
// another task when the display is busy presenting a changed region.
struct InputState {
  bool finger = false;
  bool playMode = false;
  bool settingsMode = false;
  bool fullPresent = false;
  int16_t x = W / 2;
  int16_t y = H / 2;
  int held = -1;
  bool gameTouch = false;
  bool headerTouch = false;
  uint8_t headerControl = 0;  // 1: theme, 2: games/back, 3: settings.
  Theme theme = Theme::Light;
  uint32_t themeRevision = 0;
  bool soundEnabled = true;
  uint32_t soundRevision = 0;
  uint8_t volume = 45;
  uint8_t brightness = 24;
  uint8_t settingsControl = 0;  // 1: volume, 2: brightness, 3: mute, 4: blank, 5: language.
  uint32_t settingsRevision = 0;
  bool effectsFrozen = false;
};
portMUX_TYPE inputMux = portMUX_INITIALIZER_UNLOCKED;
InputState input;
State previousState;
bool previousMode = false;
bool previousSettingsMode = false;
int previousHeld = -1;
MicroControls::Visual controls;
MicroControls::Visual previousControls;
RenderProfile profiling;

struct Button {
  int x, y, w, h;
  const char* title;
  const char* englishTitle = nullptr;
};

const Button buttons[] = {
  {28, 110, 194, 82, "01"},
  {234, 110, 194, 82, "02"},
  {440, 110, 194, 82, "03"},
  {646, 110, 194, 82, "04"},
  {852, 110, 194, 82, "05"},
  {1058, 110, 194, 82, "06"},
  {28, 542, 150, 150, "快速", "Fast"},
  {190, 542, 150, 150, "批准", "Approve"},
  {352, 542, 150, 150, "拒绝", "Reject"},
  {514, 542, 150, 150, "分支", "Fork"},
  {676, 542, 340, 150, "语音", "Voice"},
  {1028, 542, 224, 150, "发送", "Send"},
};

const char* tr(const char* chinese, const char* englishText) {
  return UiLocale::text(chinese, englishText);
}

uint32_t mix(uint32_t a, uint32_t b, float amount) {
  amount = fminf(1, fmaxf(0, amount));
  int r = ((a >> 16) & 255) * (1 - amount) + ((b >> 16) & 255) * amount;
  int g = ((a >> 8) & 255) * (1 - amount) + ((b >> 8) & 255) * amount;
  int bl = (a & 255) * (1 - amount) + (b & 255) * amount;
  return (r << 16) | (g << 8) | bl;
}

void text(const char* value, int x, int y, uint32_t color = INK,
          const lgfx::IFont* font = UiFont::sans20()) {
  UiText::drawLeft(canvas, value, x, y, font, color);
}

void center(const char* value, int x, int y, uint32_t color = INK,
            const lgfx::IFont* font = UiFont::sans20()) {
  UiText::drawCentered(canvas, value, x, y, font, color);
}

void centerFitted(const char* value, int x, int y, int width, uint32_t color,
                  const lgfx::IFont* preferred = UiFont::sans28()) {
  const lgfx::IFont* font = preferred;
  if (UiText::bounds(font, value).w > width) font = UiFont::sans24();
  if (UiText::bounds(font, value).w > width) font = UiFont::sans20();
  if (UiText::bounds(font, value).w > width) font = UiFont::sans16();
  center(value, x, y, color, font);
}

const char* localizedSignalLabel(HostFeedback::Signal value) {
  if (!UiLocale::english()) return HostFeedback::signalLabel(value);
  using S = HostFeedback::Signal;
  switch (value) {
    case S::Offline: return "Host offline";
    case S::Unknown: return "Waiting for lights";
    case S::Off: return "Ambient off";
    case S::Recording: return "Dictation recording";
    case S::Processing: return "Dictation processing";
    case S::DialMenu: return "Dial selection";
    case S::WorkingOrSelection: return "Working / selected";
    default: return "Host feedback";
  }
}

const char* agentLabel(AgentStatus::Kind value) {
  if (!UiLocale::english()) return AgentStatus::label(value);
  using K = AgentStatus::Kind;
  switch (value) {
    case K::Idle: return "Ready";
    case K::Running: return "Running";
    case K::Unread: return "Updated";
    case K::Attention: return "Attention";
    case K::Error: return "Error";
    case K::Off: return "";
    case K::Disconnected: return "Offline";
    default: return "Status";
  }
}

bool sameConfig(const HostFeedback::Config& a, const HostFeedback::Config& b) {
  if (a.fresh != b.fresh || a.defaultTier != b.defaultTier
      || a.encoder != b.encoder || a.voice != b.voice) return false;
  for (int i = 0; i < 7; ++i) if (a.actions[i] != b.actions[i]) return false;
  return true;
}
bool sameChat(const HostFeedback::Chat& a, const HostFeedback::Chat& b) {
  return a.fresh==b.fresh && a.source==b.source && a.speed==b.speed && a.mode==b.mode
      && a.permission==b.permission
      && !strncmp(a.thread_id,b.thread_id,sizeof(a.thread_id))
      && !strncmp(a.title,b.title,sizeof(a.title));
}

bool sameZone(const HostFeedback::Zone& a, const HostFeedback::Zone& b) {
  return a.color == b.color && a.brightness == b.brightness
      && a.effect == b.effect && a.speed == b.speed && a.magic == b.magic;
}

bool sameLights(const HostFeedback::Lights& a, const HostFeedback::Lights& b) {
  return a.known == b.known && sameZone(a.keys, b.keys) && sameZone(a.ambient, b.ambient);
}

bool sameReceipts(const State& a, const State& b) {
  for (int i = 0; i < 7; ++i)
    if (a.actionFeedbackAt[i] != b.actionFeedbackAt[i]
        || a.actionFeedbackSent[i] != b.actionFeedbackSent[i]) return false;
  return true;
}

// A transport receipt says only whether both native key edges were sent.
// It is independent of desktop execution, speed tier and recording state.
uint8_t actionReceipt(const State& state, int index, uint32_t now) {
  if (!state.actionFeedbackAt[index]
      || uint32_t(now - state.actionFeedbackAt[index]) >= 1200) return 0;
  return state.actionFeedbackSent[index] ? 1 : 2;
}

HostFeedback::Action mappedAction(const State& state, int index) {
  return state.hostConfig.fresh ? state.hostConfig.actions[index] : HostFeedback::Action::Unknown;
}

bool actionConfigChanged(const State& a, const State& b, int index) {
  if (a.hostConfig.fresh != b.hostConfig.fresh
      || mappedAction(a, index) != mappedAction(b, index)) return true;
  const auto action = mappedAction(a, index);
  if ((action==HostFeedback::Action::Fast || action==HostFeedback::Action::Plan
       || action==HostFeedback::Action::Approve
       || ((index==0 || index==1) && action==HostFeedback::Action::Unknown))
      && !sameChat(a.hostChat,b.hostChat)) return true;
  if ((action == HostFeedback::Action::Fast || (index == 0 && action == HostFeedback::Action::Unknown))
      && a.hostConfig.defaultTier != b.hostConfig.defaultTier) return true;
  if ((action == HostFeedback::Action::Voice || (index == 4 && action == HostFeedback::Action::Unknown))
      && (a.hostConfig.voice != b.hostConfig.voice || a.connected != b.connected
          || HostFeedback::classify(a.hostLights, a.connected)
             != HostFeedback::classify(b.hostLights, b.connected))) return true;
  return a.connected != b.connected;
}

struct TitlePanel {
  TitleRenderer::Cache cache;
  TitleScroll::Clock clock;
  uint32_t generation = 0, frames = 0;
  uint16_t desired = 0;
  int16_t x = 0, y = 0;
  bool visible = false;
};
TitlePanel titles[7];
uint32_t titleRenderNow = 0, titleTestNow = 0;
uint32_t titleTestRevision = 0, appliedTitleTestRevision = 0;
int8_t titleTestSlot = -1;

bool manualTitle(int slot) { return titleTestSlot == slot; }
uint32_t titleNow(int slot) { return manualTitle(slot) ? titleTestNow : titleRenderNow; }
uint16_t titleOffset(int slot) {
  const auto& panel = titles[slot];
  if (!panel.visible || (effectsFrozen && !manualTitle(slot))) return 0;
  return TitleScroll::offsetAt(panel.clock.elapsed(), panel.cache.lineCount());
}

bool paintTitle(int slot, const char* source, int x, int y, uint16_t width,
                uint32_t rgb, bool available) {
  auto& panel = titles[slot];
  panel.cache.prepare(source, UiTitleFont::font(), width);
  if (panel.generation != panel.cache.generation()) {
    panel.generation = panel.cache.generation();
    panel.clock.reset(titleNow(slot));
  }
  panel.x = x; panel.y = y;
  panel.visible = available && panel.cache.ready();
  panel.desired = titleOffset(slot);
  return panel.cache.paint(canvas, x, y, rgb, panel.desired);
}

void advanceTitleClocks(const InputState& snapshot, uint32_t now) {
  titleRenderNow = now;
  for (int i = 0; i < 7; ++i) {
    auto& panel = titles[i];
    const bool paused = snapshot.finger || snapshot.playMode || snapshot.settingsMode
        || !panel.visible || (snapshot.effectsFrozen && !manualTitle(i));
    panel.clock.advance(titleNow(i), paused);
    // Snapshot effectsFrozen is applied below before a structural repaint.
    panel.desired = !panel.visible || (snapshot.effectsFrozen && !manualTitle(i)) ? 0
        : TitleScroll::offsetAt(panel.clock.elapsed(), panel.cache.lineCount());
  }
}

bool titleWorkDue() {
  for (const auto& panel : titles)
    if (panel.visible && panel.cache.ready() && panel.desired != panel.cache.offset()) return true;
  return false;
}

void applyMaterial(Theme value) {
  renderedTheme = value;
  material = value == Theme::Dark ? &graphite : &silver;
  INK = material->ink;
  MUTED = material->secondary;
}

bool dark() { return renderedTheme == Theme::Dark; }

uint16_t surface565(uint32_t rgb) {
  return uint16_t(((((rgb >> 16) & 255) * 31 + 127) / 255) << 11
      | ((((rgb >> 8) & 255) * 63 + 127) / 255) << 5
      | (((rgb & 255) * 31 + 127) / 255));
}

void paintBody(int x, int y, int w, int h) {
  // Broad surfaces are exact 565 solids: no noise, bands, or repeated texture.
  MaterialGradient::solid565(canvas, x, y, w, h, surface565(material->bodyTop));
}

void paintTray(int x, int y, int w, int h) {
  MaterialGradient::solid565(canvas, x, y, w, h, surface565(material->trayTop));
}

void roundGradient(float x, float y, float w, float h, float radius, uint32_t top, uint32_t bottom) {
  MaterialGradient::roundRect(canvas, x, y, w, h, radius, top, bottom);
}

void circleGradient(float cx, float cy, float radius, uint32_t top, uint32_t bottom) {
  if (top == bottom) MaterialGradient::circle565(canvas, cx, cy, radius, surface565(top));
  else MaterialGradient::circle(canvas, cx, cy, radius, top, bottom);
}

void metalFace(float cx, float cy, float radius, uint32_t face, float bevel = 2.f,
               bool inset = false) {
  MaterialGradient::metalCircle565(canvas, cx, cy, radius, surface565(face),
      surface565(inset ? material->shadow : material->highlight),
      surface565(inset ? material->highlight : material->metalBottom), bevel);
}

void keycap(int x, int y, int w, int h, bool pressed = false, uint32_t accent = 0) {
  paintBody(x, y, w, h);
  // The entire soft shadow stays inside the unchanged touch/dirty rectangle.
  MaterialGradient::shadowRoundRect(canvas, x + 8, y + 8, w - 16, h - 16,
      18, 7, pressed ? 30 : 58, material->deepShadow);
  const int drop = pressed ? 3 : 0;
  uint32_t top = pressed ? material->keyPressed : material->keyTop;
  uint32_t bottom = pressed ? mix(material->keyPressed, material->keyBottom, .35f) : material->keyBottom;
  if (accent) { top = mix(top, accent, pressed ? .10f : .018f); bottom = mix(bottom, accent, .04f); }
  MaterialGradient::bevelRoundRect565(canvas, x + 3, y + 2 + drop,
      w - 6, h - 8 - drop, 19, surface565(top), surface565(bottom), 10);
  MaterialGradient::strokeRoundRect(canvas, x + 3, y + 2 + drop,
      w - 6, h - 8 - drop, 19, accent ? 1.2f : .8f,
      accent ? mix(top, accent, .5f) : mix(top, material->edge, dark() ? .40f : .22f));
}

void tray(int x, int y, int w, int h) {
  paintBody(x, y, w, h);
  MaterialGradient::shadowRoundRect(canvas, x + 3, y + 4, w - 6, h - 7,
      26, 3, 35, material->deepShadow);
  MaterialGradient::solidRoundRect565(canvas, x + 1, y + 2, w - 2, h - 5,
      26, surface565(material->trayTop));
  MaterialGradient::strokeRoundRect(canvas, x + 1, y + 2, w - 2, h - 5,
      26, .8f, mix(material->trayTop, material->edge, .35f));
}

uint32_t actionAccent(int i) {
  switch (i) {
    case 6: return dark() ? 0xE2BF80 : 0x9B6B28;
    case 7: case 11: return dark() ? 0xA8D6B6 : 0x387154;
    case 8: return dark() ? 0xE9A6A8 : 0xA84C51;
    default: return material->accent;
  }
}

void saveBackdrop(uint16_t* output, int x, int y, int w, int h) {
  if (!output) return;
  const auto* pixels = static_cast<const uint16_t*>(canvas.getBuffer());
  for (int row = 0; row < h; ++row)
    memcpy(output + row * w, pixels + (y + row) * W + x, size_t(w) * 2);
}

bool restoreBackdrop(const uint16_t* source, int x, int y, int w, int h) {
  if (!source || !backdropsReady) return false;
  auto* pixels = static_cast<uint16_t*>(canvas.getBuffer());
  for (int row = 0; row < h; ++row)
    memcpy(pixels + (y + row) * W + x, source + row * w, size_t(w) * 2);
  return true;
}

void drawDialCenter(bool pressed, bool longPress) {
  constexpr int cx = 244, cy = 352;
  metalFace(cx, cy + (pressed ? 2 : 0), 48,
      pressed ? material->keyPressed : material->keyTop, 1.5f);
  MaterialGradient::strokeCircle(canvas, cx, cy + (pressed ? 2 : 0), 48, .8f,
      mix(material->keyTop, material->edge, pressed ? .50f : .22f));
  center(pressed ? longPress ? tr("长按", "Long") : tr("按住", "Held") : tr("按下", "Press"),
         cx, cy + (pressed ? 2 : 0), INK, UiFont::sans28());
}

void drawDialMaterial(bool pressed, bool longPress = false) {
  constexpr int cx = 244, cy = 352;
  paintTray(DIAL_X, DIAL_Y, DIAL_W, DIAL_H);
  MaterialGradient::shadowCircle(canvas, cx, cy + 4, 106, 7, 100, material->deepShadow);
  metalFace(cx, cy, 106, mix(material->metalTop, material->metalBottom, .50f), 3.f);
  MaterialGradient::strokeCircle(canvas, cx, cy, 106, .9f,
      mix(material->metalTop, material->highlight, .55f));
  constexpr float tau = 6.28318530718f;
  for (int i = 0; i < 24; ++i) {
    float angle = i * tau / 24;
    int inner = i % 6 == 0 ? 94 : 98;
    MaterialGradient::line(canvas, cx + cosf(angle) * inner, cy + sinf(angle) * inner,
        cx + cosf(angle) * 102, cy + sinf(angle) * 102,
        i % 6 == 0 ? 1.4f : 1.0f, mix(material->metalBottom, INK, .32f));
  }
  metalFace(cx, cy, 60, material->recess, 3.f, true);
  drawDialCenter(pressed, longPress);
}

void drawCounterMaterial() {
  paintTray(COUNTER_X, COUNTER_Y, COUNTER_W, COUNTER_H);
}

void drawStickMaterial() {
  constexpr int cx = 802, cy = 352;
  paintTray(STICK_X, STICK_Y, STICK_W, STICK_H);
  metalFace(cx, cy, 93, material->recess, 2.5f, true);
  MaterialGradient::strokeCircle(canvas, cx, cy, 93, .9f,
      mix(material->recess, material->highlight, .40f));
  MaterialGradient::strokeCircle(canvas, cx, cy, 62, .7f,
      mix(material->recess, material->highlight, .13f));
  uint32_t axis = mix(material->recess, material->highlight, .16f);
  MaterialGradient::line(canvas, cx - 81, cy, cx + 81, cy, .7f, axis);
  MaterialGradient::line(canvas, cx, cy - 81, cx, cy + 81, .7f, axis);
  UiIcons::draw(canvas, UiIcons::Icon::ChevronUp, cx, 247, 16, MUTED);
  UiIcons::draw(canvas, UiIcons::Icon::ChevronDown, cx, 457, 16, MUTED);
  UiIcons::draw(canvas, UiIcons::Icon::ChevronLeft, 697, cy, 16, MUTED);
  UiIcons::draw(canvas, UiIcons::Icon::ChevronRight, 907, cy, 16, MUTED);
}

void rebuildBackdrops() {
  backdropsReady = false;
  drawDialMaterial(false);
  saveBackdrop(dialBackdrops[0], DIAL_X, DIAL_Y, DIAL_W, DIAL_H);
  drawDialMaterial(true);
  saveBackdrop(dialBackdrops[1], DIAL_X, DIAL_Y, DIAL_W, DIAL_H);
  drawDialCenter(true, true);
  saveBackdrop(longPressCenter, CENTER_X, CENTER_Y, CENTER_W, CENTER_H);
  drawCounterMaterial();
  saveBackdrop(counterBackdrop, COUNTER_X, COUNTER_Y, COUNTER_W, COUNTER_H);
  drawStickMaterial();
  saveBackdrop(stickBackdrop, STICK_X, STICK_Y, STICK_W, STICK_H);
  backdropsReady = true;
}

void present(int x, int y, int w, int h) {
  // pushSprite honors the destination clip before entering P4's software
  // rotation/copy loop. Keeping the full canvas retains screenshot support.
  if (forceFullPresent || displayInvalidated) return;
  M5.Display.setClipRect(x, y, w, h);
  canvas.pushSprite(0, 0);
  M5.Display.clearClipRect();
}

constexpr int SLIDER_LEFT = 320, SLIDER_RIGHT = 1120;
constexpr int VOLUME_Y = 300, BRIGHTNESS_Y = 500;

int sliderPosition(uint8_t value, uint8_t minimum) {
  const int range = 100 - minimum;
  return SLIDER_LEFT + ((int(value) - minimum) * (SLIDER_RIGHT - SLIDER_LEFT) + range / 2) / range;
}

uint8_t sliderValue(int x, uint8_t minimum) {
  const int offset = std::max(0, std::min(SLIDER_RIGHT - SLIDER_LEFT, x - SLIDER_LEFT));
  return uint8_t(minimum + (offset * (100 - minimum) + 400) / 800);
}

void drawSettingsSlider(int y, uint8_t value, uint8_t minimum, bool active) {
  const int knob = sliderPosition(value, minimum);
  MaterialGradient::solidRoundRect565(canvas, SLIDER_LEFT, y - 6,
      SLIDER_RIGHT - SLIDER_LEFT, 12, 6, surface565(material->recess));
  if (knob > SLIDER_LEFT)
    MaterialGradient::solidRoundRect565(canvas, SLIDER_LEFT, y - 6,
        knob - SLIDER_LEFT, 12, 6, surface565(material->accent));
  MaterialGradient::shadowCircle(canvas, knob, y + 2, 20, 3, 35, material->deepShadow);
  MaterialGradient::circle565(canvas, knob, y, 20, surface565(material->keyTop));
  MaterialGradient::strokeCircle(canvas, knob, y, 20, active ? 1.8f : .9f,
      active ? material->accent : material->edge);
  MaterialGradient::circle565(canvas, knob, y, 5, surface565(material->accent));
}

void drawSettingsValue(int y, uint8_t value) {
  char label[8];
  snprintf(label, sizeof(label), "%u%%", unsigned(value));
  center(label, 1194, y, INK, UiFont::sans32());
}

void drawMuteButton() {
  constexpr int x = 72, y = 288, w = 184, h = 72;
  const bool pressed = renderedSettingsControl == 3;
  const int drop = pressed ? 2 : 0;
  paintTray(x, y, w, h);
  MaterialGradient::shadowRoundRect(canvas, x + 8, y + 8, w - 16, h - 16,
      18, 5, pressed ? 24 : 40, material->deepShadow);
  const uint32_t face = pressed ? material->keyPressed : material->keyTop;
  MaterialGradient::bevelRoundRect565(canvas, x + 3, y + 2 + drop, w - 6,
      h - 8 - drop, 18, surface565(face), surface565(material->keyBottom), 8);
  MaterialGradient::strokeRoundRect(canvas, x + 3, y + 2 + drop, w - 6,
      h - 8 - drop, 18, .8f, material->edge);
  const uint32_t ink = renderedSoundEnabled ? INK : MUTED;
  UiIcons::draw(canvas, renderedSoundEnabled ? UiIcons::Icon::SpeakerOn : UiIcons::Icon::SpeakerOff,
      103, 324 + drop, 28, ink);
  center(renderedSoundEnabled ? tr("声音开启", "Sound on") : tr("已静音", "Muted"), 185, 324 + drop, ink, UiFont::sans20());
}

void drawLanguageButton() {
  constexpr int x = 918, y = 124, w = 306, h = 72;
  const bool pressed = renderedSettingsControl == 5;
  paintBody(x, 96, w, 100);
  text(tr("界面语言", "Language"), x + 12, 100, MUTED, UiFont::sans16());
  keycap(x, y, w, h, pressed, material->accent);
  center(tr("中文 / English", "English / 中文"), x + w / 2,
      y + 35 + (pressed ? 2 : 0), INK, UiFont::sans24());
}

void drawSettings() {
  text(tr("声音与显示", "Sound & display"), 64, 132, INK, UiFont::sans32());
  text(tr("拖动滑杆，找到舒服的状态", "Make the controls feel right for you"), 64, 182, MUTED, UiFont::sans20());
  drawLanguageButton();
  for (int y : {216, 416}) {
    MaterialGradient::solidRoundRect565(canvas, 40, y, 1200, 176, 24,
        surface565(material->trayTop));
    MaterialGradient::strokeRoundRect(canvas, 40, y, 1200, 176, 24, .8f,
        mix(material->trayTop, material->edge, .4f));
  }
  text(tr("音量", "Volume"), 76, 238, INK, UiFont::sans28());
  drawMuteButton();
  drawSettingsSlider(VOLUME_Y, renderedVolume, 0, renderedSettingsControl == 1);
  drawSettingsValue(VOLUME_Y, renderedVolume);
  text(tr("静音不会改变音量", "Muting keeps your volume setting"), 320, 354, MUTED, UiFont::sans16());
  text(tr("屏幕亮度", "Brightness"), 76, 438, INK, UiFont::sans28());
  UiIcons::draw(canvas, UiIcons::Icon::Sun, 164, BRIGHTNESS_Y, 40, MUTED);
  drawSettingsSlider(BRIGHTNESS_Y, renderedBrightness, 10, renderedSettingsControl == 2);
  drawSettingsValue(BRIGHTNESS_Y, renderedBrightness);
  text(tr("最低亮度 10%", "Minimum brightness: 10%"), 320, 554, MUTED, UiFont::sans16());
  center(tr("右上角返回控制台", "Tap Back above to return to the console"), 640, 654, MUTED, UiFont::sans20());
}

void updateSettingsSlider(int y, uint8_t value, uint8_t oldValue, uint8_t minimum,
                          bool active, bool oldActive) {
  if (value == oldValue && active == oldActive) return;
  const int oldX = sliderPosition(oldValue, minimum), newX = sliderPosition(value, minimum);
  const int x = std::min(oldX, newX) - 27;
  const int w = std::abs(newX - oldX) + 54;
  // Every moving element, its shadow and the changed track fit this strip.
  // Clipping the complete slider draw also keeps AA phase stable on repairs.
  canvas.setClipRect(x, y - 27, w, 56);
  paintTray(x, y - 27, w, 56);
  drawSettingsSlider(y, value, minimum, active);
  canvas.clearClipRect();
  present(x, y - 27, w, 56);
  if (value != oldValue) {
    paintTray(1150, y - 27, 88, 56);
    canvas.setClipRect(1150, y - 27, 88, 56);
    drawSettingsValue(y, value);
    canvas.clearClipRect();
    present(1150, y - 27, 88, 56);
  }
}

bool updateSettings(bool soundChanged) {
  bool changed = renderedVolume != previousVolume || renderedBrightness != previousBrightness
      || renderedSettingsControl != previousSettingsControl || soundChanged;
  updateSettingsSlider(VOLUME_Y, renderedVolume, previousVolume, 0,
      renderedSettingsControl == 1, previousSettingsControl == 1);
  updateSettingsSlider(BRIGHTNESS_Y, renderedBrightness, previousBrightness, 10,
      renderedSettingsControl == 2, previousSettingsControl == 2);
  if (soundChanged || (renderedSettingsControl == 3) != (previousSettingsControl == 3)) {
    drawMuteButton();
    present(72, 288, 184, 72);
  }
  if ((renderedSettingsControl == 5) != (previousSettingsControl == 5)) {
    drawLanguageButton();
    present(918, 96, 306, 100);
  }
  return changed;
}

void drawHostSignal(const State& state) {
  const auto signal = HostFeedback::classify(state.hostLights, state.connected);
  const bool known = state.connected && state.hostLights.known;
  auto zoneColor = [&](const HostFeedback::Zone& zone) {
    return known && zone.brightness && zone.effect
        ? mix(material->bodyTop, zone.color, zone.brightness / 255.f) : material->edge;
  };
  const uint32_t keys = zoneColor(state.hostLights.keys);
  const uint32_t ambient = zoneColor(state.hostLights.ambient);
  circleGradient(432, 49, 3, keys, keys);
  circleGradient(446, 49, 3, ambient, ambient);
  text(localizedSignalLabel(signal), 462, 41, MUTED, UiFont::sans16());
}

void drawHeader(const State& state) {
  UiIcons::draw(canvas, UiIcons::Icon::Logo, 51, 49, 44, INK);
  text("CODEX", 88, 27, INK, UiFont::sans32());
  text("micro", 90, 63, MUTED, UiFont::sans16());
  center(settingsMode ? tr("设置", "Settings") : state.lightingPreview ? tr("状态演示", "Preview") : playMode ? tr("游戏", "Play") : tr("控制台", "Console"),
         340, 49, INK, UiFont::sans24());
  drawHostSignal(state);
  const bool synced = state.connected && state.rpc_count;
  uint32_t connection = synced ? (dark() ? 0x9DD6BB : 0x3F8065) : MUTED;
  circleGradient(687, 49, 4, connection, connection);
  text(synced ? tr("主机已连接", "Connected") : tr("等待主机", "Waiting for host"), 704, 39,
       connection, UiLocale::english() && !synced ? UiFont::sans16() : UiFont::sans20());
  keycap(844, 13, 88, 72, headerHeld == 3);
  UiIcons::draw(canvas, UiIcons::Icon::Gear, 888, 49 + (headerHeld == 3 ? 2 : 0), 32,
                settingsMode ? material->accent : INK);
  keycap(950, 13, 130, 72, headerHeld == 1);
  const int drop = headerHeld == 1 ? 2 : 0;
  UiIcons::draw(canvas, dark() ? UiIcons::Icon::Moon : UiIcons::Icon::Sun,
                977, 49 + drop, 24, INK);
  center(dark() ? tr("黑夜", "Night") : tr("白天", "Day"), 1034, 49 + drop, INK, UiFont::sans24());
  keycap(1111, 13, 141, 72, headerHeld == 2);
  center(playMode || settingsMode ? tr("返回", "Back") : tr("游戏", "Play"), 1181, 49 + (headerHeld == 2 ? 2 : 0), INK,
         UiFont::sans24());
}

AgentStatus::Kind slotKind(const Slot& slot, bool connected) {
  return AgentStatus::classify(slot.color, slot.brightness, slot.effect, connected);
}

AgentNeon::Style slotStyle(int i, AgentStatus::Kind kind) {
  static constexpr uint32_t hues[] = {0x12BDD8,0xA76AFF,0x27C99A,0xEBA63C,0xF06EC7,0x579BFF};
  AgentNeon::Style style;
  style.strength = dark() ? 255 : 230;
  switch (kind) {
    case AgentStatus::Kind::Running:
      style.rgb = hues[i]; style.periodMs = 2400; style.pattern = AgentNeon::Pattern::Chase; break;
    case AgentStatus::Kind::Unread:
      style.rgb = 0x20CD83; style.periodMs = 3000; style.pattern = AgentNeon::Pattern::Breathe; break;
    case AgentStatus::Kind::Attention:
      style.rgb = 0xF3A42F; style.periodMs = 2000; style.pattern = AgentNeon::Pattern::Segments; break;
    case AgentStatus::Kind::Error:
      style.rgb = 0xFF4C6C; style.pattern = AgentNeon::Pattern::Steady; break;
    default: break;
  }
  return style;
}

void drawSlot(int i, const Slot& slot, bool connected, bool titlesFresh) {
  const auto& b = buttons[i];
  const bool lit = connected && slot.color && slot.effect && slot.brightness;
  const auto kind = slotKind(slot, connected);
  const auto style = slotStyle(i, kind);
  const bool pulseHint = lit && AgentStatus::pulseHint(slot.effect);
  const uint32_t accent = lit ? slot.color : material->accent;
  keycap(b.x, b.y, b.w, b.h, held == i,
      pulseHint ? (style.pattern == AgentNeon::Pattern::Off ? MUTED : style.rgb) : 0);
  const int drop = held == i ? 2 : 0;
  text(b.title, b.x + 18, b.y + 8 + drop, MUTED, UiFont::sans16());
  const uint32_t labelInk = style.pattern == AgentNeon::Pattern::Off ? MUTED
      : mix(INK, style.rgb, dark() ? .40f : .25f);
  UiText::drawRight(canvas, agentLabel(kind), b.x + b.w - 34,
      b.y + 5 + drop, UiFont::sans16(), labelInk);
  circleGradient(b.x + b.w - 19, b.y + 13 + drop, 3,
      lit ? accent : material->edge, lit ? accent : material->edge);
  if (!paintTitle(i, slot.title, b.x + 16, b.y + 23 + drop, 162,
                  titlesFresh ? INK : MUTED, titlesFresh)) {
    char fallback[20];
    snprintf(fallback, sizeof(fallback), "Agent %s", b.title);
    center(fallback, b.x + b.w / 2, b.y + 50 + drop, INK, UiFont::sans24());
  }
  const bool animated = style.pattern != AgentNeon::Pattern::Off
      && style.pattern != AgentNeon::Pattern::Steady;
  if (animated) animatedSlots |= 1 << i;
  else animatedSlots &= ~(1 << i);
  if (held == i) AgentNeon::invalidate(i);
  else if (AgentNeon::capture(i, canvas, {b.x,b.y,b.w,b.h}, style)) {
    AgentNeon::Rect ignored[AgentNeon::MaxDamage];
    AgentNeon::step(i, canvas, effectsFrozen ? 0 : millis() + i * 151,
                    ignored, AgentNeon::MaxDamage);
  }
}

const char* actionLabel(HostFeedback::Action action, const char* fallback) {
  using A = HostFeedback::Action;
  switch (action) {
    case A::None: return tr("未分配", "Unassigned");
    case A::Fast: return tr("快速", "Fast");
    case A::Approve: return tr("批准", "Approve");
    case A::Reject: return tr("拒绝", "Reject");
    case A::Fork: return tr("分支", "Fork");
    case A::Voice: return tr("语音", "Voice");
    case A::Aux: return tr("辅助", "Aux");
    case A::Send: return tr("发送", "Send");
    case A::Plan: return tr("计划", "Plan");
    case A::Sidebar: return tr("侧栏", "Sidebar");
    case A::Back: return tr("后退", "Back");
    case A::Forward: return tr("前进", "Forward");
    case A::Custom: return tr("自定义", "Custom");
    default: return fallback;
  }
}

UiIcons::Icon actionIcon(HostFeedback::Action action, int index) {
  using A = HostFeedback::Action;
  if (action == A::Unknown) {
    static constexpr UiIcons::Icon defaults[] = {
      UiIcons::Icon::Lightning, UiIcons::Icon::Check, UiIcons::Icon::Close,
      UiIcons::Icon::Fork, UiIcons::Icon::Microphone, UiIcons::Icon::Sliders,
      UiIcons::Icon::Send
    };
    return defaults[index];
  }
  switch (action) {
    case A::Fast: return UiIcons::Icon::Lightning;
    case A::Approve: return UiIcons::Icon::Check;
    case A::Reject: return UiIcons::Icon::Close;
    case A::Fork: return UiIcons::Icon::Fork;
    case A::Voice: return UiIcons::Icon::Microphone;
    case A::Send: return UiIcons::Icon::Send;
    case A::Back: return UiIcons::Icon::ChevronLeft;
    case A::Forward: return UiIcons::Icon::ChevronRight;
    default: return UiIcons::Icon::Sliders;
  }
}

const char* actionCaption(const State& state, int index, HostFeedback::Action action) {
  using A = HostFeedback::Action;
  const bool fast = action == A::Fast || (index == 0 && action == A::Unknown);
  if (fast) {
    if (!state.connected) return tr("桌面离线", "Host offline");
    if (state.hostChat.fresh) {
      switch (state.hostChat.speed) {
        case HostFeedback::Speed::Fast: return tr("快速·已开启", "Fast: on");
        case HostFeedback::Speed::UltraFast: return tr("极速·已开启", "Ultra: on");
        case HostFeedback::Speed::Standard: return tr("快速·已关闭", "Fast: off");
        case HostFeedback::Speed::Default: return tr("聊天默认", "Chat default");
        default: return tr("聊天档位未知", "Speed unknown");
      }
    }
    return tr("聊天未同步", "Not synced");
  }
  if (action==A::Plan && state.hostChat.fresh) {
    switch (state.hostChat.mode) {
      case HostFeedback::Mode::Plan: return tr("计划·已开启", "Plan: on");
      case HostFeedback::Mode::Default: return tr("计划·已关闭", "Plan: off");
      default: return tr("聊天模式未知", "Mode unknown");
    }
  }
  const bool approve=action==A::Approve || (index==1 && action==A::Unknown);
  if (approve) {
    if (!state.hostChat.fresh) return tr("权限未同步", "Access unknown");
    switch (state.hostChat.permission) {
      case HostFeedback::Permission::FullAccess: return "Full access";
      case HostFeedback::Permission::WorkspaceWrite: return tr("工作区权限", "Workspace write");
      case HostFeedback::Permission::ReadOnly: return tr("只读权限", "Read only");
      case HostFeedback::Permission::Custom: return tr("自定义权限", "Custom access");
      default: return tr("权限未同步", "Access unknown");
    }
  }
  const bool voice = action == A::Voice || (index == 4 && action == A::Unknown);
  if (voice) {
    const auto signal = HostFeedback::classify(state.hostLights, state.connected);
    if (signal == HostFeedback::Signal::Recording)
      return tr(HostFeedback::signalLabel(signal), "Recording");
    if (signal == HostFeedback::Signal::Processing)
      return tr(HostFeedback::signalLabel(signal), "Processing");
    if (action == A::Unknown) return state.connected ? tr("映射未同步", "Not synced") : tr("桌面离线", "Host offline");
    if (!state.hostConfig.fresh) return state.connected ? tr("配置未同步", "Not synced") : tr("桌面离线", "Host offline");
    switch (state.hostConfig.voice) {
      case HostFeedback::Voice::PushToTalk: return tr("配置·按住说话", "Hold to talk");
      case HostFeedback::Voice::Realtime: return tr("配置·实时语音", "Realtime voice");
      default: return tr("语音模式未知", "Voice unknown");
    }
  }
  if (!state.hostConfig.fresh || action == A::Unknown)
    return state.connected ? tr("配置未同步", "Not synced") : tr("桌面离线", "Host offline");
  if (action == A::None) return tr("桌面未绑定", "Not assigned");
  return tr("桌面映射", "Desktop action");
}

void drawMappedAction(const State& state, int index, int layoutIndex,
                      int x, int w, int cx, int heldIndex, const char* fallback) {
  const auto action = mappedAction(state, index);
  const bool active = held == heldIndex;
  const bool voice = action == HostFeedback::Action::Voice
      || (index == 4 && action == HostFeedback::Action::Unknown);
  const bool unassigned = action == HostFeedback::Action::None;
  const bool modeToggle=action==HostFeedback::Action::Fast || action==HostFeedback::Action::Plan
      || (index==0 && action==HostFeedback::Action::Unknown);
  const bool permissionCaption=action==HostFeedback::Action::Approve
      || (index==1 && action==HostFeedback::Action::Unknown);
  const bool persistentCaption=modeToggle || permissionCaption;
  const bool actualActive=state.hostChat.fresh && (action==HostFeedback::Action::Plan
      ? state.hostChat.mode==HostFeedback::Mode::Plan
      : modeToggle && (state.hostChat.speed==HostFeedback::Speed::Fast
          || state.hostChat.speed==HostFeedback::Speed::UltraFast));
  const uint8_t receipt = active ? 0 : actionReceipt(state, index, millis());
  const uint32_t accent = receipt == 2 ? (dark() ? 0xE9A6A8 : 0xA84C51)
      : receipt || actualActive ? material->accent : actionAccent(layoutIndex);
  keycap(x, 542, w, 150, active, active || receipt || actualActive ? accent : 0);
  const int drop = active ? 2 : 0;
  const uint32_t ink = unassigned ? MUTED : INK;
  UiIcons::draw(canvas, actionIcon(action, index), cx, 588 + drop, 32,
      active || actualActive ? accent : ink);
  if (persistentCaption && receipt)
    center(receipt==1 ? tr("已发送", "Sent") : tr("发送未确认", "Unconfirmed"),cx,557+drop,accent,UiFont::sans16());
  centerFitted(active && voice ? tr("已按住", "Held") : actionLabel(action, fallback),
      cx, 635 + drop, w - 24, ink, UiFont::sans28());
  const uint32_t captionInk=permissionCaption && state.hostChat.fresh
      && state.hostChat.permission==HostFeedback::Permission::FullAccess
      ? (dark() ? 0xDAB785 : 0x876027) : actualActive ? material->accent : MUTED;
  center(receipt && !persistentCaption ? receipt == 1 ? tr("已发送", "Sent") : tr("发送未确认", "Unconfirmed") : actionCaption(state, index, action),
      cx, 668 + drop, receipt && !persistentCaption ? accent : captionInk, UiFont::sans16());
  renderedActionFeedback[index] = receipt;
}

void drawAction(int i, const State& state) {
  const auto& b = buttons[i];
  if (i == 10) {
    paintBody(b.x, b.y, b.w, b.h);
    drawMappedAction(state, 4, i, 680, 162, 761, 10, tr("语音", "Voice"));
    drawMappedAction(state, 5, i, 850, 162, 931, 13, tr("辅助", "Aux"));
  } else {
    const int index = i == 11 ? 6 : i - 6;
    drawMappedAction(state, index, i, b.x, b.w, b.x + b.w / 2, i,
                     tr(b.title, b.englishTitle ? b.englishTitle : b.title));
  }
}

void drawCounter() {
  uint32_t started = micros();
  if (!restoreBackdrop(counterBackdrop, COUNTER_X, COUNTER_Y, COUNTER_W, COUNTER_H))
    drawCounterMaterial();
  profiling.counterRestore = max(profiling.counterRestore, micros() - started);
  started = micros();
  char count[24];
  snprintf(count, sizeof(count), "%+ld", (long)controls.detent);
  canvas.setClipRect(380, 311, 112, 47);
  const auto* font = UiFont::sans40();
  if (UiText::bounds(font, count).w > 108) font = UiFont::sans32();
  if (UiText::bounds(font, count).w > 108) font = UiFont::sans24();
  center(count, 436, 334, INK, font);
  canvas.clearClipRect();
  profiling.counterText = max(profiling.counterText, micros() - started);
}

void drawRotary(bool updateCounter = true) {
  constexpr int cx = 244, cy = 352;
  constexpr float tau = 6.28318530718f;
  const uint32_t age = controls.dialPressed ? millis() - controls.pressStarted : 0;
  const bool longPress = controls.dialPressed && age >= 500;
  renderedHoldMs = controls.dialPressed ? uint16_t(min(age, uint32_t(500))) : 0;
  uint32_t started = micros();
  if (!restoreBackdrop(dialBackdrops[controls.dialPressed ? 1 : 0], DIAL_X, DIAL_Y, DIAL_W, DIAL_H))
    drawDialMaterial(controls.dialPressed, longPress);
  else if (longPress && !restoreBackdrop(longPressCenter, CENTER_X, CENTER_Y, CENTER_W, CENTER_H))
    drawDialCenter(true, true);
  profiling.dialRestore = max(profiling.dialRestore, micros() - started);
  started = micros();
  int position = (int(lroundf(controls.dialAngle * 24 / tau)) % 24 + 24) % 24;
  for (int offset = -1; offset <= 1; ++offset) {
    const int tick = (position + offset + 24) % 24;
    float a = tick * tau / 24;
    uint32_t ink = offset == 0 ? material->accent : mix(MUTED, material->accent, .35f);
    int inner = tick % 6 == 0 ? 94 : 98;
    MaterialGradient::line(canvas, cx + cosf(a) * inner, cy + sinf(a) * inner,
        cx + cosf(a) * 102, cy + sinf(a) * 102, offset == 0 ? 1.8f : 1.2f, ink);
  }
  circleGradient(cx + cosf(controls.dialAngle) * 78,
                 cy + sinf(controls.dialAngle) * 78, 4, material->accent, material->accent);
  profiling.dialMarks = max(profiling.dialMarks, micros() - started);
  started = micros();
  if (age) MaterialGradient::arc(canvas, cx, cy, 55,
      -90, -90 + min(age, uint32_t(500)) * .72f, 2.7f,
      age >= 500 ? (dark() ? 0x9DD6BB : 0x3F8065) : material->accent);
  profiling.dialArc = max(profiling.dialArc, micros() - started);
  if (updateCounter) drawCounter();
}

const char* stickDirection() {
  const float x = controls.stickX, y = controls.stickY;
  if (sqrtf(x * x + y * y) < .05f) return tr("归中", "Centered");
  if (fabsf(x) < .25f) return y < 0 ? tr("向上", "Up") : tr("向下", "Down");
  if (fabsf(y) < .25f) return x < 0 ? tr("向左", "Left") : tr("向右", "Right");
  if (x < 0) return y < 0 ? tr("左上", "Up-left") : tr("左下", "Down-left");
  return y < 0 ? tr("右上", "Up-right") : tr("右下", "Down-right");
}

void drawStick() {
  constexpr int cx = 802, cy = 352;
  if (!restoreBackdrop(stickBackdrop, STICK_X, STICK_Y, STICK_W, STICK_H)) drawStickMaterial();
  const uint32_t accent = material->accent;
  if (controls.stickY < -.2f) UiIcons::draw(canvas, UiIcons::Icon::ChevronUp, cx, 247, 16, accent);
  if (controls.stickY > .2f) UiIcons::draw(canvas, UiIcons::Icon::ChevronDown, cx, 457, 16, accent);
  if (controls.stickX < -.2f) UiIcons::draw(canvas, UiIcons::Icon::ChevronLeft, 697, cy, 16, accent);
  if (controls.stickX > .2f) UiIcons::draw(canvas, UiIcons::Icon::ChevronRight, 907, cy, 16, accent);
  const int x = cx + controls.stickX * 60, y = cy + controls.stickY * 60;
  MaterialGradient::line(canvas, cx, cy, x, y, 1.4f, accent);
  MaterialGradient::shadowCircle(canvas, x, y + 3, 29, 5, 100, material->deepShadow);
  metalFace(x, y, 29, mix(material->metalTop, material->metalBottom, .50f), 1.5f);
  MaterialGradient::strokeCircle(canvas, x, y, 29, .8f,
      mix(material->metalTop, material->highlight, .5f));
  metalFace(x, y, 23, controls.stickActive ? material->keyPressed : material->keyTop, 1.f);
  uint32_t point = controls.stickActive ? accent : MUTED;
  circleGradient(x, y, 2, point, point);
  paintTray(FEEDBACK_X, FEEDBACK_Y, FEEDBACK_W, FEEDBACK_H);
  center(stickDirection(), 1107, 296, INK, UiFont::sans28());
  char travel[24];
  int distance = min(100, int(lroundf(sqrtf(controls.stickX * controls.stickX
      + controls.stickY * controls.stickY) * 100)));
  snprintf(travel, sizeof(travel), "%d%%", distance);
  center(travel, 1107, 336, MUTED, UiFont::sans32());
}

void drawEncoderConfig(const State& state) {
  const auto mode = state.hostConfig.fresh ? state.hostConfig.encoder : HostFeedback::Encoder::Unknown;
  const char* label = tr("转动外环", "Turn the ring");
  switch (mode) {
    case HostFeedback::Encoder::Composer: label = tr("输入导航", "Navigation"); break;
    case HostFeedback::Encoder::Reasoning: label = tr("推理强度", "Reasoning"); break;
    case HostFeedback::Encoder::Scroll: label = tr("对话滚动", "Chat scroll"); break;
    case HostFeedback::Encoder::Custom: label = tr("自定义", "Custom"); break;
    default: break;
  }
  paintTray(372, 376, 128, 74);
  center(label, 436, 398, INK, UiFont::sans20());
  const char* caption = mode != HostFeedback::Encoder::Unknown ? tr("配置·随桌面", "Desktop config")
      : !state.connected ? tr("桌面离线", "Host offline")
      : state.hostConfig.fresh ? tr("配置项未知", "Mode unknown") : tr("配置未同步", "Not synced");
  center(caption,
         436, 432, MUTED, UiFont::sans16());
}

void drawChatFeedback(const State& state) {
  const auto& chat=state.hostChat;
  paintTray(710,210,184,26);
  const char* plan=!chat.fresh ? tr("计划状态未同步", "Plan not synced")
      : chat.mode==HostFeedback::Mode::Plan ? tr("计划·已开启", "Plan: on")
      : chat.mode==HostFeedback::Mode::Default ? tr("计划·已关闭", "Plan: off") : tr("聊天模式未知", "Mode unknown");
  center(plan,802,223,chat.fresh && chat.mode==HostFeedback::Mode::Plan ? material->accent : MUTED,
      UiFont::sans20());
  paintTray(1008,378,198,75);
  center(chat.fresh ? tr("指定聊天", "Tracked chat") : tr("聊天状态未同步", "Chat not synced"),1107,390,MUTED,UiFont::sans16());
  if (chat.fresh) {
    if (!paintTitle(6, chat.title, 1016, 401, 182, INK, true))
      center(tr("未命名聊天", "Untitled chat"),1107,426,MUTED,UiFont::sans20());
  } else titles[6].visible = false;
}

void drawDashboard(const State& state) {
  for (int i = 0; i < 6; ++i) drawSlot(i, state.slots[i], state.connected, state.titlesFresh);
  tray(28, 208, 484, 288);
  tray(528, 208, 724, 288);
  text(tr("旋钮", "Dial"), 52, 226, INK, UiFont::sans24());
  center(tr("步进", "Steps"), 436, 290, MUTED, UiFont::sans16());
  drawEncoderConfig(state);
  text(tr("摇杆", "Joystick"), 552, 226, INK, UiFont::sans24());
  center(tr("后退", "Back"), 654, 352, MUTED, UiFont::sans20());
  // Keep the English caption between the right chevron and the readout's
  // restored rectangle at x=988, including after the stick moves.
  center(tr("前进", "Next"), 956, 352, MUTED, UiFont::sans20());
  center(tr("侧栏", "Sidebar"), 802, 481, MUTED, UiFont::sans20());
  drawChatFeedback(state);
  rebuildBackdrops();
  drawRotary();
  drawStick();
  for (int i = 6; i < 12; ++i) drawAction(i, state);
}

bool sameSlot(const Slot& a, const Slot& b) {
  return a.color == b.color && a.brightness == b.brightness
      && a.effect == b.effect && a.speed == b.speed
      && strncmp(a.title, b.title, sizeof(a.title)) == 0
      && strncmp(a.identity, b.identity, sizeof(a.identity)) == 0;
}

bool updateHeader(const State& state) {
  const bool structural = state.connected != previousState.connected
      || bool(state.rpc_count) != bool(previousState.rpc_count)
      || state.lightingPreview != previousState.lightingPreview
      || headerHeld != previousHeaderHeld;
  if (structural) {
    paintBody(28, 5, 1224, 91);
    drawHeader(state);
    present(28, 5, 1224, 91);
  } else if (!sameLights(state.hostLights, previousState.hostLights)) {
    paintBody(420, 31, 230, 38);
    drawHostSignal(state);
    present(420, 31, 230, 38);
  } else return false;
  return true;
}

bool updateDashboard(const State& state) {
  bool drew = false;
  for (int i = 0; i < 6; ++i) {
    const auto& b = buttons[i];
    const auto& slot = state.slots[i];
    bool changed = state.connected != previousState.connected
        || state.titlesFresh != previousState.titlesFresh
        || !sameSlot(slot, previousState.slots[i])
        || effectsFrozen != previousEffectsFrozen
        || ((held == i || previousHeld == i) && held != previousHeld);
    if (changed) {
      drawSlot(i, slot, state.connected, state.titlesFresh);
      present(b.x, b.y, b.w, b.h);
      drew = true;
    }
  }
  for (int i = 6; i < 12; ++i) {
    bool active = held == i || (i == 10 && held == 13);
    bool wasActive = previousHeld == i || (i == 10 && previousHeld == 13);
    const int index = i == 11 ? 6 : i - 6;
    const uint32_t now = millis();
    const uint8_t receipt = held == i ? 0 : actionReceipt(state, index, now);
    bool changed = (held != previousHeld && (active || wasActive))
        || actionConfigChanged(state, previousState, index)
        || receipt != renderedActionFeedback[index]
        || state.actionFeedbackAt[index] != previousState.actionFeedbackAt[index]
        || state.actionFeedbackSent[index] != previousState.actionFeedbackSent[index];
    if (i == 10) {
      const uint8_t rightReceipt = held == 13 ? 0 : actionReceipt(state, 5, now);
      changed |= actionConfigChanged(state, previousState, 5)
          || rightReceipt != renderedActionFeedback[5]
          || state.actionFeedbackAt[5] != previousState.actionFeedbackAt[5]
          || state.actionFeedbackSent[5] != previousState.actionFeedbackSent[5];
    }
    if (changed) {
      const auto& b = buttons[i];
      drawAction(i, state);
      present(b.x, b.y, b.w, b.h);
      drew = true;
    }
  }
  if (state.hostConfig.fresh != previousState.hostConfig.fresh
      || state.hostConfig.encoder != previousState.hostConfig.encoder) {
    drawEncoderConfig(state);
    present(372, 376, 128, 74);
    drew = true;
  }
  if (!sameChat(state.hostChat,previousState.hostChat)
      || effectsFrozen != previousEffectsFrozen) {
    drawChatFeedback(state);
    present(710,210,184,26);
    present(1008,378,198,75);
    drew=true;
  }
  // Compare the completed gesture with what was actually painted. A slow
  // frame can cross 500ms without ever visiting a narrow 500..550ms window.
  const uint16_t holdMs = controls.dialPressed
      ? uint16_t(min(uint32_t(millis() - controls.pressStarted), uint32_t(500))) : 0;
  const bool holdAnimating = controls.dialPressed && holdMs != renderedHoldMs;
  if (controls.dialAngle != previousControls.dialAngle
      || controls.detent != previousControls.detent
      || controls.dialPressed != previousControls.dialPressed
      || controls.dialTurning != previousControls.dialTurning || holdAnimating) {
    const bool countChanged = controls.detent != previousControls.detent;
    drawRotary(countChanged);
    const uint32_t presentStarted = micros();
    present(DIAL_X, DIAL_Y, DIAL_W, DIAL_H);
    if (countChanged) present(COUNTER_X, COUNTER_Y, COUNTER_W, COUNTER_H);
    profiling.dialPresent = max(profiling.dialPresent, micros() - presentStarted);
    drew = true;
  }
  if (controls.stickX != previousControls.stickX || controls.stickY != previousControls.stickY
      || controls.stickActive != previousControls.stickActive) {
    drawStick();
    present(STICK_X, STICK_Y, STICK_W, STICK_H);
    present(FEEDBACK_X, FEEDBACK_Y, FEEDBACK_W, FEEDBACK_H);
    drew = true;
  }
  return drew;
}

bool inside(int x, int y, int bx, int by, int bw, int bh) {
  return x >= bx && x < bx + bw && y >= by && y < by + bh;
}

}  // namespace

const RenderProfile& renderProfile() { return profiling; }
void resetRenderProfile() { profiling = {}; }

TitleScrollStatus titleScrollStatus(uint8_t index) {
  TitleScrollStatus result;
  if (index >= 7) return result;
  const auto& panel = titles[index];
  result.lines = panel.cache.lineCount();
  result.visibleLines = min(uint8_t(2), result.lines);
  result.width = panel.cache.width();
  result.height = TitleRenderer::ViewHeight;
  result.lineHeight = TitleScroll::LineHeight;
  result.offset = panel.cache.offset();
  result.maxOffset = result.lines > 2 ? result.lines * TitleScroll::LineHeight - 1 : 0;
  result.elapsed = panel.clock.elapsed();
  result.cycle = TitleScroll::cycleMs(result.lines);
  result.paused = panel.clock.paused();
  result.frames = panel.frames;
  result.cachedBytes = panel.cache.bytes();
  result.ready = panel.cache.ready();
  result.manual = manualTitle(index);
  return result;
}
bool setTitleScrollTest(uint8_t slot, uint32_t now) {
  if (slot >= 7 || now > 600000) return false;
  if (titleTestSlot >= 0 && !manualTitle(slot))
    titles[titleTestSlot].clock.reset(millis());
  if (!manualTitle(slot) || now < titleTestNow) titles[slot].clock.reset(now);
  titleTestSlot = slot; titleTestNow = now; ++titleTestRevision;
  return true;
}
void clearTitleScrollTest() {
  if (titleTestSlot >= 0) titles[titleTestSlot].clock.reset(millis());
  titleTestSlot = -1; ++titleTestRevision;
}

bool init() {
  canvas.setColorDepth(16);
  canvas.setPsram(true);
  ready = canvas.createSprite(W, H) != nullptr;
  UiFont::begin();
  UiTitleFont::begin();
  preferencesReady = preferences.begin("micro-ui", false);
  uint8_t saved = preferencesReady ? preferences.getUChar("theme", 0) : 0;
  input.theme = saved == uint8_t(Theme::Dark) ? Theme::Dark : Theme::Light;
  persistedTheme = input.theme;
  applyMaterial(input.theme);
  dialBackdrops[0] = static_cast<uint16_t*>(heap_caps_malloc(DIAL_W * DIAL_H * 2,
      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  dialBackdrops[1] = static_cast<uint16_t*>(heap_caps_malloc(DIAL_W * DIAL_H * 2,
      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  stickBackdrop = static_cast<uint16_t*>(heap_caps_malloc(STICK_W * STICK_H * 2,
      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  longPressCenter = static_cast<uint16_t*>(heap_caps_malloc(CENTER_W * CENTER_H * 2,
      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  counterBackdrop = static_cast<uint16_t*>(heap_caps_malloc(COUNTER_W * COUNTER_H * 2,
      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  MicroControls::reset();
  MicroGames::init();
  MicroGames::setDarkTheme(input.theme == Theme::Dark);
  AgentNeon::init();
  return ready;
}

void invalidateDisplay() { displayInvalidated=true; }

bool render(const State& state) {
  uint32_t now = millis();
  if (!ready) return false;
  const uint32_t localeRevision = UiLocale::revision();
  const bool localeChanged = localeRevision != appliedLocaleRevision;
  InputState snapshot;
  MicroControls::Visual visual;
  portENTER_CRITICAL(&inputMux);
  snapshot = input;
  visual = MicroControls::visual();
  portEXIT_CRITICAL(&inputMux);
  advanceTitleClocks(snapshot, now);
  const bool immediate = localeChanged || titleTestRevision != appliedTitleTestRevision || displayInvalidated || snapshot.held != held || snapshot.playMode != playMode
      || snapshot.settingsMode != settingsMode
      || snapshot.finger != finger || snapshot.fullPresent != forceFullPresent
      || visual.revision != controls.revision
      || snapshot.effectsFrozen != effectsFrozen
      || snapshot.themeRevision != appliedThemeRevision
      || snapshot.soundRevision != appliedSoundRevision
      || snapshot.settingsRevision != appliedSettingsRevision
      || snapshot.settingsControl != renderedSettingsControl
      || !sameConfig(state.hostConfig, previousState.hostConfig)
      || !sameChat(state.hostChat, previousState.hostChat)
      || !sameLights(state.hostLights, previousState.hostLights)
      || !sameReceipts(state, previousState);
  if (immediate || snapshot.finger) lastInteraction = now;
  const bool fxDue = animatedSlots && !snapshot.playMode && !snapshot.settingsMode && !snapshot.effectsFrozen
      && !snapshot.finger && now - lastInteraction >= 80 && now - fxLastStep >= 16;
  const bool titleDue = !snapshot.playMode && !snapshot.settingsMode && !snapshot.finger
      && titleWorkDue() && now - fxLastStep >= 16
      && (titleTestSlot >= 0 || now - lastInteraction >= 80);
  if (frame && now - lastFrame < 50 && !immediate && !fxDue && !titleDue) return false;
  const bool themeChanged = snapshot.themeRevision != appliedThemeRevision;
  const bool soundChanged = snapshot.soundRevision != appliedSoundRevision;
  const bool full = !frame || snapshot.playMode != previousMode
      || snapshot.settingsMode != previousSettingsMode || themeChanged || localeChanged;
  finger = snapshot.finger;
  controls = visual;
  held = visual.held;
  playMode = snapshot.playMode;
  settingsMode = snapshot.settingsMode;
  forceFullPresent = snapshot.fullPresent;
  effectsFrozen = snapshot.effectsFrozen;
  renderedSoundEnabled = snapshot.soundEnabled;
  renderedVolume = snapshot.volume;
  renderedBrightness = snapshot.brightness;
  renderedSettingsControl = snapshot.settingsControl;
  headerHeld = snapshot.headerTouch ? snapshot.headerControl : 0;
  if (themeChanged) {
    applyMaterial(snapshot.theme);
    MicroGames::setDarkTheme(snapshot.theme == Theme::Dark);
  }
  // NVS may block, so persist a render snapshot outside the input critical
  // section. Touch and command callers only request a new theme revision.
  if (persistedRevision != snapshot.themeRevision) {
    persistedRevision = snapshot.themeRevision;
    if (preferencesReady && persistedTheme != snapshot.theme
        && preferences.putUChar("theme", uint8_t(snapshot.theme)) == 1)
      persistedTheme = snapshot.theme;
  }
  lastFrame = now;
  frame++;
  // Batch the P4 framebuffer cache writeback once per frame. Otherwise each
  // small rotated strip can flush a much larger physical row span separately.
  M5.Display.startWrite();
  bool drew = full;
  if (full) {
    paintBody(0, 0, W, H);
    drawHeader(state);
    if (settingsMode) drawSettings();
    else if (playMode) MicroGames::render(canvas, now, true);
    else drawDashboard(state);
    present(0, 0, W, H);
  } else {
    drew |= updateHeader(state);
    if (settingsMode) drew |= updateSettings(soundChanged);
    else if (playMode) {
      if (MicroGames::render(canvas, now, false)) {
        size_t count = 0;
        const auto* rectangles = MicroGames::damage(count);
        for (size_t i = 0; i < count; ++i) {
          const auto& r = rectangles[i];
          present(r.x, r.y, r.w, r.h);
        }
        drew = true;
      }
    } else drew |= updateDashboard(state);
  }
  // One retained title pane or one neon card per idle frame. Input-driven
  // paint always wins; both effects skip their work while a finger is down.
  if (!drew && (fxDue || titleDue)) {
    fxLastStep = now;
    for (uint8_t n = 0; n < 13; ++n) {
      const uint8_t item = fxCursor;
      fxCursor = (fxCursor + 1) % 13;
      if (item < 7) {
        auto& panel = titles[item];
        if (!titleDue || !panel.visible || panel.desired == panel.cache.offset()) continue;
        const uint32_t began = micros();
        const bool changed = panel.cache.scroll(canvas, panel.desired);
        profiling.titleDraw = max(profiling.titleDraw, micros() - began);
        if (changed) {
          const uint32_t presenting = micros();
          present(panel.x, panel.y, panel.cache.width(), TitleRenderer::ViewHeight);
          profiling.titlePresent = max(profiling.titlePresent, micros() - presenting);
          ++panel.frames; ++profiling.titleFrames; drew = true;
        }
      } else {
        const uint8_t i = item - 7;
        if (!fxDue || !(animatedSlots & (1 << i)) || held == i) continue;
        AgentNeon::Rect damage[AgentNeon::MaxDamage];
        const uint32_t began = micros();
        const size_t count = AgentNeon::step(i, canvas, now + i * 151,
                                            damage, AgentNeon::MaxDamage);
        profiling.neonDraw = max(profiling.neonDraw, micros() - began);
        const uint32_t presenting = micros();
        for (size_t j = 0; j < count; ++j)
          present(damage[j].x, damage[j].y, damage[j].w, damage[j].h);
        profiling.neonPresent = max(profiling.neonPresent, micros() - presenting);
        if (count) { ++profiling.neonFrames; drew = true; }
      }
      if (drew) break;
    }
  }
  if (forceFullPresent || displayInvalidated) {
    M5.Display.clearClipRect();
    canvas.pushSprite(0, 0);
    displayInvalidated=false;
    drew = true;
  }
  const uint32_t writebackStarted = micros();
  M5.Display.endWrite();
  profiling.writeback = max(profiling.writeback, micros() - writebackStarted);
  previousState = state;
  previousHeld = held;
  previousControls = controls;
  previousMode = playMode;
  previousSettingsMode = settingsMode;
  previousHeaderHeld = headerHeld;
  previousEffectsFrozen = effectsFrozen;
  appliedTitleTestRevision = titleTestRevision;
  appliedThemeRevision = snapshot.themeRevision;
  appliedLocaleRevision = localeRevision;
  appliedSoundRevision = snapshot.soundRevision;
  appliedSettingsRevision = snapshot.settingsRevision;
  previousVolume = renderedVolume;
  previousBrightness = renderedBrightness;
  previousSettingsControl = renderedSettingsControl;
  return drew;
}

Event touch(int16_t x, int16_t y, bool pressed) {
  Event event;
  portENTER_CRITICAL(&inputMux);
  const bool wasDown = input.finger;
  if (pressed) { input.x = x; input.y = y; }
  input.finger = pressed;
  const bool themeButton = inside(x, y, 950, 5, 130, 88);
  const bool gameButton = inside(x, y, 1111, 5, 141, 88);
  const bool settingsButton = inside(x, y, 844, 5, 88, 88);
  if (pressed && !wasDown && (themeButton || gameButton || settingsButton)) {
    if (themeButton) {
      input.theme = input.theme == Theme::Light ? Theme::Dark : Theme::Light;
      ++input.themeRevision;
      input.headerControl = 1;
    } else if (gameButton) {
      if (input.settingsMode) {
        input.settingsMode = false;
        input.playMode = false;
      } else input.playMode = !input.playMode;
      input.headerControl = 2;
    } else {
      input.settingsMode = !input.settingsMode;
      input.playMode = false;
      input.headerControl = 3;
    }
    input.settingsControl = 0;
    input.gameTouch = false;
    input.headerTouch = true;
    MicroControls::cancel();
    input.held = MicroControls::visual().held;
    MicroControls::nextEvent(event);
    portEXIT_CRITICAL(&inputMux);
    MicroGames::touch(x, y, false);
    return event;
  }
  // Header gestures remain captured until release, including drags across
  // native keys or the game arena.
  if (input.headerTouch) {
    if (!pressed) {
      input.headerTouch = false;
      input.headerControl = 0;
    }
    portEXIT_CRITICAL(&inputMux);
    return event;
  }
  if (input.settingsMode) {
    if (pressed && !wasDown) {
      if (inside(x, y, 72, 280, 184, 88)) {
        input.settingsControl = 3;
        if (!input.soundEnabled && !input.volume) {
          input.volume = 45;
          ++input.settingsRevision;
        }
        input.soundEnabled = !input.soundEnabled;
        ++input.soundRevision;
      } else if (inside(x, y, 918, 116, 306, 88)) {
        input.settingsControl = 5;
        UiLocale::setLanguage(UiLocale::english() ? UiLocale::Language::Chinese
                                                 : UiLocale::Language::English);
      } else if (inside(x, y, 280, 256, 944, 88)) input.settingsControl = 1;
      else if (inside(x, y, 280, 456, 944, 88)) input.settingsControl = 2;
      else input.settingsControl = 4;
    }
    // Capture the selected slider until release, even across header/native
    // controls. Releases can arrive at (0,0), so only pressed samples set values.
    if (pressed && input.settingsControl == 1) {
      const uint8_t value = sliderValue(x, 0);
      if (input.volume != value) {
        input.volume = value;
        ++input.settingsRevision;
      }
      const bool enabled = value != 0;
      if (input.soundEnabled != enabled) {
        input.soundEnabled = enabled;
        ++input.soundRevision;
      }
    } else if (pressed && input.settingsControl == 2) {
      const uint8_t value = sliderValue(x, 10);
      if (input.brightness != value) {
        input.brightness = value;
        ++input.settingsRevision;
      }
    }
    if (!pressed) input.settingsControl = 0;
    portEXIT_CRITICAL(&inputMux);
    return event;
  }
  if (input.playMode) {
    const bool wasGameTouch = input.gameTouch;
    if (pressed && !wasDown) input.gameTouch = y >= 96;
    if (!pressed) input.gameTouch = false;
    const bool forward = wasGameTouch || input.gameTouch;
    portEXIT_CRITICAL(&inputMux);
    if (forward) MicroGames::touch(x, y, pressed);
    return event;
  }
  MicroControls::sample(x, y, pressed, millis());
  input.held = MicroControls::visual().held;
  MicroControls::nextEvent(event);
  portEXIT_CRITICAL(&inputMux);
  return event;
}

bool pollEvent(Event& event) {
  portENTER_CRITICAL(&inputMux);
  const bool available = MicroControls::nextEvent(event);
  portEXIT_CRITICAL(&inputMux);
  return available;
}

bool playing() {
  portENTER_CRITICAL(&inputMux);
  bool active = input.playMode;
  portEXIT_CRITICAL(&inputMux);
  return active;
}

Theme theme() {
  portENTER_CRITICAL(&inputMux);
  const Theme value = input.theme;
  portEXIT_CRITICAL(&inputMux);
  return value;
}

void setTheme(Theme value) {
  if (value != Theme::Light && value != Theme::Dark) return;
  portENTER_CRITICAL(&inputMux);
  if (input.theme != value) {
    input.theme = value;
    ++input.themeRevision;
  }
  portEXIT_CRITICAL(&inputMux);
}

void toggleTheme() {
  portENTER_CRITICAL(&inputMux);
  input.theme = input.theme == Theme::Light ? Theme::Dark : Theme::Light;
  ++input.themeRevision;
  portEXIT_CRITICAL(&inputMux);
}
void setSoundEnabled(bool enabled) {
  portENTER_CRITICAL(&inputMux);
  if (input.soundEnabled != enabled) {
    input.soundEnabled = enabled;
    ++input.soundRevision;
  }
  portEXIT_CRITICAL(&inputMux);
}
bool soundEnabled() {
  portENTER_CRITICAL(&inputMux);
  const bool enabled = input.soundEnabled;
  portEXIT_CRITICAL(&inputMux);
  return enabled;
}
uint8_t volume() {
  portENTER_CRITICAL(&inputMux);
  const uint8_t value = input.volume;
  portEXIT_CRITICAL(&inputMux);
  return value;
}
uint8_t brightness() {
  portENTER_CRITICAL(&inputMux);
  const uint8_t value = input.brightness;
  portEXIT_CRITICAL(&inputMux);
  return value;
}
void setVolume(uint8_t value) {
  value = std::min<uint8_t>(100, value);
  portENTER_CRITICAL(&inputMux);
  if (input.volume != value) {
    input.volume = value;
    ++input.settingsRevision;
  }
  portEXIT_CRITICAL(&inputMux);
}
void setBrightness(uint8_t value) {
  value = std::max<uint8_t>(10, std::min<uint8_t>(100, value));
  portENTER_CRITICAL(&inputMux);
  if (input.brightness != value) {
    input.brightness = value;
    ++input.settingsRevision;
  }
  portEXIT_CRITICAL(&inputMux);
}
bool settingsOpen() {
  portENTER_CRITICAL(&inputMux);
  const bool open = input.settingsMode;
  portEXIT_CRITICAL(&inputMux);
  return open;
}
void setFullPresent(bool enabled) {
  portENTER_CRITICAL(&inputMux);
  input.fullPresent = enabled;
  portEXIT_CRITICAL(&inputMux);
}
void setEffectsFrozen(bool frozen) {
  portENTER_CRITICAL(&inputMux);
  input.effectsFrozen = frozen;
  portEXIT_CRITICAL(&inputMux);
}
bool effectsAreFrozen() {
  portENTER_CRITICAL(&inputMux);
  const bool frozen = input.effectsFrozen;
  portEXIT_CRITICAL(&inputMux);
  return frozen;
}
const uint16_t* framebuffer() { return static_cast<const uint16_t*>(canvas.getBuffer()); }
int bufferWidth() { return W; }
int bufferHeight() { return H; }

}  // namespace MicroUI
