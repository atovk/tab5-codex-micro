#include "micro_controls.h"
#include "control_layout.h"

#include <math.h>

namespace MicroControls {
namespace {
constexpr float PI_F = 3.14159265358979323846f;
constexpr float TAU = 2 * PI_F;
constexpr float STEP = TAU / 24;
// ESP32 atan2f and repeated float subtraction can leave a complete revolution
// a few ULPs below its final detent. This tolerance is far below touch noise.
constexpr float ANGLE_EPSILON = .00001f;
constexpr int DIAL_X = ControlLayout::DialX, DIAL_Y = ControlLayout::DialY;
constexpr int STICK_X = ControlLayout::StickX, STICK_Y = ControlLayout::StickY;
constexpr float STICK_RADIUS = ControlLayout::StickRadius;
constexpr int QUEUE_SIZE = 32;

struct Button { int x, y, w, h; const char* key; int held; };
constexpr Button buttons[] = {
  {28,106,194,90,"AG00",0}, {234,106,194,90,"AG01",1},
  {440,106,194,90,"AG02",2}, {646,106,194,90,"AG03",3},
  {852,106,194,90,"AG04",4}, {1058,106,194,90,"AG05",5},
  {28,542,150,150,"ACT06",6}, {190,542,150,150,"ACT07",7},
  {352,542,150,150,"ACT08",8}, {514,542,150,150,"ACT09",9},
  {676,542,170,150,"ACT10",10}, {846,542,170,150,"ACT11",13},
  {1028,542,224,150,"ACT12",11},
};
enum class Capture { None, Blocked, Key, DeferredKey, Ring, Center, Stick };
Capture capture = Capture::None;
bool finger = false;
const char* capturedKey = nullptr;
const Button* capturedButton = nullptr;
uint8_t releaseActionMask = 0;
Visual state;
MicroUI::Event queue[QUEUE_SIZE];
unsigned readAt = 0, queued = 0;
float previousAngle = 0, remainder = 0;
bool angleValid = false;
uint32_t stickAt = 0;
float sentStickX = 0, sentStickY = 0;

float radius(float x, float y) { return sqrtf(x*x+y*y); }
void push(const MicroUI::Event& event) {
  // A single sampled half-turn produces at most12 turns. The main loop drains
  // all events after each sample, so the queue cannot grow during normal use.
  if (queued >= QUEUE_SIZE) return;
  queue[(readAt + queued) % QUEUE_SIZE] = event;
  ++queued;
}
void key(const char* name, uint8_t action) {
  MicroUI::Event event;
  event.key = name; event.action = action; push(event);
}
void stick(float x, float y, uint32_t now, bool force = false) {
  float d = radius(x,y);
  if (d > 1) { x /= d; y /= d; d = 1; }
  // Match the native service deadzone and explicitly rearm direction changes.
  if (d <= .05f) { x = y = d = 0; }
  const bool neutral = d == 0;
  const bool crossedNeutral = neutral != (sentStickX == 0 && sentStickY == 0);
  const bool moved = radius(x-sentStickX,y-sentStickY) >= .025f;
  const auto direction = stickDirection(x,y);
  const auto oldDirection = stickDirection(sentStickX,sentStickY);
  const bool crossedThreshold = (direction == StickDirection::Neutral)
      != (oldDirection == StickDirection::Neutral);
  float a = atan2f(y,x) / TAU;
  if (a < 0) a += 1;
  const bool changedDirection = direction != StickDirection::Neutral
      && oldDirection != StickDirection::Neutral && direction != oldDirection;
  if (force || crossedNeutral || crossedThreshold || changedDirection
      || (moved && uint32_t(now-stickAt) >= 20)) {
    MicroUI::Event event;
    event.joystick = true; event.angle = neutral ? 0 : a; event.distance = d;
    push(event);
    sentStickX = x; sentStickY = y; stickAt = now;
  }
  if (state.stickX != x || state.stickY != y) {
    state.stickX = x; state.stickY = y; ++state.revision;
  }
}
bool inside(const Button& button, int x, int y) {
  return x >= button.x && x < button.x + button.w
      && y >= button.y && y < button.y + button.h;
}
bool releaseAction(const Button& button) {
  if (button.key[0] != 'A' || button.key[1] != 'C' || button.key[2] != 'T') return false;
  const int index = (button.key[3] - '0') * 10 + button.key[4] - '0' - 6;
  return index >= 0 && index < 7 && (releaseActionMask & (1 << index));
}
void release(bool submitDeferred = false) {
  if (capture == Capture::Key || capture == Capture::Center) key(capturedKey,0);
  else if (capture == Capture::DeferredKey && submitDeferred && queued <= QUEUE_SIZE - 2) {
    // A single-shot action still uses the native balanced key pair. Reserve
    // both queue slots so a partial pair can never leave a key held down.
    key(capturedKey,1); key(capturedKey,0);
  }
  else if (capture == Capture::Stick) stick(0,0,0,true);
  capture = Capture::None; capturedKey = nullptr; capturedButton = nullptr;
  state.held = -1; state.dialPressed = false; state.dialTurning = false;
  state.stickActive = false; state.pressStarted = 0;
  angleValid = false; remainder = 0; ++state.revision;
}
void turn(int16_t x, int16_t y) {
  const float dx = x-DIAL_X, dy = y-DIAL_Y;
  const float r = radius(dx,dy);
  // Crossing the center or leaving the ring reanchors instead of making a
  // discontinuous half-turn. Reentering the ring never jumps the selection.
  if (r < 60 || r > 132) { angleValid = false; remainder = 0; return; }
  const float angle = atan2f(dy,dx);
  state.dialAngle = angle;
  if (angleValid) {
    float delta = angle-previousAngle;
    while (delta > PI_F) delta -= TAU;
    while (delta < -PI_F) delta += TAU;
    // A lost sample or accidental finger teleport must not select a dozen
    // controls. Deliberate rotation is sampled well inside a quarter-turn.
    if (fabsf(delta) <= PI_F / 2) {
      remainder += delta;
      while (remainder >= STEP - ANGLE_EPSILON) {
        key("ENC_CW",2); remainder -= STEP; ++state.detent;
      }
      while (remainder <= -STEP + ANGLE_EPSILON) {
        key("ENC_CC",2); remainder += STEP; --state.detent;
      }
    } else remainder = 0;
  }
  previousAngle = angle; angleValid = true; ++state.revision;
}
}  // namespace

StickDirection stickDirection(float x, float y) {
  if (radius(x,y) < StickActivationThreshold) return StickDirection::Neutral;
  float angle = atan2f(y,x) / TAU;
  if (angle < 0) angle += 1;
  return static_cast<StickDirection>(int(floorf((angle + .125f) * 4)) % 4);
}

void reset() {
  capture = Capture::None; finger = false; capturedKey = nullptr; capturedButton = nullptr;
  releaseActionMask = 0;
  state = Visual{}; readAt = queued = 0;
  previousAngle = remainder = 0; angleValid = false;
  stickAt = 0; sentStickX = sentStickY = 0;
}

void cancelDeferredAction() {
  if (capture != Capture::DeferredKey) return;
  // Keep this finger blocked until it lifts, including after it reenters.
  capture = Capture::Blocked; capturedKey = nullptr; capturedButton = nullptr;
  state.held = -1; ++state.revision;
}

void setReleaseActionMask(uint8_t mask) {
  mask &= 0x7f;
  if (releaseActionMask != mask) cancelDeferredAction();
  releaseActionMask = mask;
}

void sample(int16_t x, int16_t y, bool pressed, uint32_t now) {
  if (!pressed) {
    // M5Unified retains the last touch coordinates on release. A synthetic
    // (0,0) release intentionally cancels a pending click during handoff/QA.
    if (finger) release(capturedButton && inside(*capturedButton,x,y));
    finger = false;
    return;
  }
  if (finger) {
    if (capture == Capture::DeferredKey && !inside(*capturedButton,x,y)) cancelDeferredAction();
    else if (capture == Capture::Ring) turn(x,y);
    else if (capture == Capture::Stick)
      stick(float(x-STICK_X)/STICK_RADIUS,float(y-STICK_Y)/STICK_RADIUS,now);
    return;
  }
  finger = true;
  capture = Capture::Blocked;
  for (const auto& b : buttons) {
    if (inside(b,x,y)) {
      capture = releaseAction(b) ? Capture::DeferredKey : Capture::Key;
      capturedKey = b.key; capturedButton = &b; state.held = b.held;
      ++state.revision;
      if (capture == Capture::Key) key(capturedKey,1);
      return;
    }
  }
  const float dialR = radius(x-DIAL_X,y-DIAL_Y);
  if (dialR <= 48) {
    capture = Capture::Center; capturedKey = "ENC";
    state.held = 12; state.dialPressed = true; state.pressStarted = now;
    ++state.revision; key("ENC",1); return;
  }
  if (dialR >= 60 && dialR <= 116) {
    capture = Capture::Ring; state.dialTurning = true;
    angleValid = false; remainder = 0; turn(x,y); return;
  }
  if (radius(x-STICK_X,y-STICK_Y) <= STICK_RADIUS+8) {
    capture = Capture::Stick; state.stickActive = true; ++state.revision;
    stick(float(x-STICK_X)/STICK_RADIUS,float(y-STICK_Y)/STICK_RADIUS,now,true);
  }
}

void cancel() {
  readAt = queued = 0;
  if (finger) release();
  finger = false;
}

bool nextEvent(MicroUI::Event& event) {
  if (!queued) { event = MicroUI::Event{}; return false; }
  event = queue[readAt]; readAt = (readAt+1) % QUEUE_SIZE; --queued;
  return true;
}
Visual visual() { return state; }
}  // namespace MicroControls
