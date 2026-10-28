#pragma once
#include <stdint.h>

// Explicit USB-command diagnostics only. This schedules native events; it does
// not send them or claim desktop execution. The owner must serialize each Step
// via the normal BLE input queue and immediately call completeStep(accepted).
// Never arm from pairing, boot, reconnect, or a normal UI gesture.
namespace MicroBleDiagnostics {
enum class Plan : uint8_t { FastPair, DialPair, PlanPair };
enum class State : uint8_t { Idle, Armed, Running, Complete, Aborted };
enum class Reason : uint8_t {
  None, InvalidRequest, SessionChanged, Disconnected, Suppressed, MappingChanged,
  TransportChanged, StartTimeout, Deadline, SubmitFailed, Cancelled
};
enum class Kind : uint8_t { FastKey, DialClockwise, DialCounterclockwise, StickUp, StickNeutral };
struct Step {
  Kind kind=Kind::FastKey;
  uint8_t action=0;  // Key: 1 press, 0 release. Dial: 2. Stick: unused.
  uint8_t fastKey=0; // Only ACT06..ACT12 after a verified Fast mapping.
  uint8_t index=0;
  uint32_t session=0;
};
struct Context {
  uint32_t session=0, transportGeneration=0;
  bool connected=false, authenticated=false, bonded=false, ready=false;
  bool activeBle=false, switching=false;
  bool suppressed=false; // diagnostic capture, physical touch, or other replay
  // The integration must verify actual desktop mappings, not merely copy an
  // arbitrary command argument. A custom command/skill is never a safe test.
  // PlanPair requires joystick Up == composer.togglePlanMode. Down is a
  // different default action (sidebar), not an inverse. DialPair permits only
  // verified harmless navigation, with no encoder-center click.
  // Keep this true only while the same mapping/target remains valid.
  bool mappingKnown=false, mappingSafe=false;
  uint8_t fastKey=0;
};
class Schedule {
public:
  static constexpr uint32_t minDelayMs=1000, maxDelayMs=30000;
  static constexpr uint32_t startGraceMs=3000, runDeadlineMs=10000;
  State state() const { return state_; }
  Reason reason() const { return reason_; }
  uint32_t session() const { return session_; }
  uint8_t acceptedSteps() const { return index_; }
  bool active() const { return state_==State::Armed || state_==State::Running; }
  // Root must call the transport's session-local cancelInputs on abort after
  // emission, even when the next gesture edge was never enqueued. The queue
  // handles release/neutral cleanup only on its original live connection.
  bool takeCleanupRequired() { const bool value=cleanup_; cleanup_=false; return value; }
  bool arm(Plan plan,uint32_t delayMs,uint32_t now,const Context& context) {
    if (active() || cleanup_) return false; // cannot replace an in-flight test
    if ((plan!=Plan::FastPair && plan!=Plan::DialPair && plan!=Plan::PlanPair)
        || delayMs<minDelayMs || delayMs>maxDelayMs || !context.session
        || !context.connected || !context.authenticated || !context.bonded || context.suppressed
        || !context.mappingKnown || !context.mappingSafe
        || (plan==Plan::FastPair && (context.fastKey<6 || context.fastKey>12))) {
      state_=State::Aborted; reason_=Reason::InvalidRequest; return false;
    }
    plan_=plan; armedAt_=now; delay_=delayMs; session_=context.session;
    fastKey_=context.fastKey; index_=0; offered_=false; cleanup_=false;
    state_=State::Armed; reason_=Reason::None;
    return true;
  }
  // At most one event per call, never catch up in a burst after a stalled loop.
  // USB may disappear during the armed delay. Once emission starts, any
  // transport generation change cancels the rest rather than migrating it.
  bool next(uint32_t now,const Context& context,Step& step) {
    if (!active()) return false;
    if (context.session!=session_) return abort(Reason::SessionChanged);
    if (!context.connected || !context.authenticated || !context.bonded)
      return abort(Reason::Disconnected);
    if (context.suppressed) return abort(Reason::Suppressed);
    // The expected USB->BLE handoff clears metadata freshness. Wait while
    // armed for the companion's BLE snapshot; never run on missing metadata.
    if (!context.mappingKnown) {
      if (state_==State::Running) return abort(Reason::MappingChanged);
      if (uint32_t(now-armedAt_)>delay_+startGraceMs) return abort(Reason::StartTimeout);
      return false;
    }
    if (!context.mappingSafe || (plan_==Plan::FastPair && context.fastKey!=fastKey_))
      return abort(Reason::MappingChanged);
    // macOS may subscribe to native HID only after USB disappears. Arming
    // pins an already authenticated peer, but never emits before HID is ready.
    // Once any event starts, losing readiness cancels the remaining edges.
    if (!context.ready) {
      if (state_==State::Running) return abort(Reason::Disconnected);
      if (uint32_t(now-armedAt_)>delay_+startGraceMs) return abort(Reason::StartTimeout);
      return false;
    }
    if (state_==State::Armed) {
      const uint32_t elapsed=now-armedAt_;
      if (elapsed<delay_) return false;
      if (elapsed>delay_+startGraceMs) return abort(Reason::StartTimeout);
      if (!context.activeBle || context.switching) return false;
      state_=State::Running; startedAt_=lastAcceptedAt_=now;
      generation_=context.transportGeneration;
    }
    if (!context.activeBle || context.switching || context.transportGeneration!=generation_)
      return abort(Reason::TransportChanged);
    if (uint32_t(now-startedAt_)>runDeadlineMs) return abort(Reason::Deadline);
    if (offered_ || (index_ && uint32_t(now-lastAcceptedAt_)<intervalBefore(index_))) return false;
    step=stepAt(index_); step.index=index_; step.session=session_; step.fastKey=fastKey_;
    offered_=true; offeredAt_=now;
    return true;
  }
  // accepted means queued in the pinned BLE session, not delivered/executed.
  // No retry of a rejected event: a transport API may have uncertain outcome.
  bool completeStep(bool accepted) {
    if (state_!=State::Running || !offered_) return false;
    offered_=false;
    if (!accepted) { cleanup_=true; return abort(Reason::SubmitFailed); }
    lastAcceptedAt_=offeredAt_;
    if (++index_==stepCount()) state_=State::Complete;
    return true;
  }
  void cancel() { if (active()) abort(Reason::Cancelled); }
private:
  Plan plan_=Plan::FastPair;
  State state_=State::Idle;
  Reason reason_=Reason::None;
  uint32_t session_=0,generation_=0,armedAt_=0,delay_=0,startedAt_=0;
  uint32_t lastAcceptedAt_=0,offeredAt_=0;
  uint8_t fastKey_=0,index_=0;
  bool offered_=false,cleanup_=false;
  bool abort(Reason why) {
    cleanup_|=index_!=0 || offered_;
    state_=State::Aborted; reason_=why; offered_=false; return false;
  }
  uint8_t stepCount() const { return plan_==Plan::DialPair ? 2 : 4; }
  uint32_t intervalBefore(uint8_t index) const {
    if (plan_==Plan::DialPair) return 2000;
    return index==2 ? 3000 : 100; // observable pair, short bounded hold
  }
  Step stepAt(uint8_t index) const {
    Step step;
    if (plan_==Plan::FastPair) { step.kind=Kind::FastKey; step.action=index%2==0 ? 1 : 0; }
    else if (plan_==Plan::DialPair) {
      step.kind=index==0 ? Kind::DialClockwise : Kind::DialCounterclockwise; step.action=2;
    } else step.kind=index%2==0 ? Kind::StickUp : Kind::StickNeutral;
    return step;
  }
};
} // namespace MicroBleDiagnostics
