#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>

// USB-command diagnostics only. Owns bounded storage, never input or rendering
// state. The caller supplies a real, completed framebuffer and its evidence.
namespace DeferredFrameCapture {
// sscanf's unsigned conversions accept signs and overflowing tokens. Local
// diagnostic requests require bounded ASCII decimal integers instead.
inline bool parseUnsignedValues(const char* text,uint32_t* values,size_t count) {
  if (!text || !values) return false;
  for (size_t i=0;i<count;++i) {
    while (*text==' ' || *text=='\t') ++text;
    if (*text<'0' || *text>'9') return false;
    uint32_t value=0;
    do {
      const uint32_t digit=uint32_t(*text-'0');
      if (value>(UINT32_MAX-digit)/10) return false;
      value=value*10+digit; ++text;
    } while (*text>='0' && *text<='9');
    if (*text && *text!=' ' && *text!='\t') return false;
    values[i]=value;
  }
  while (*text==' ' || *text=='\t') ++text;
  return !*text;
}
enum class State : uint8_t { Idle, Armed, Complete, Aborted, Expired };
enum class Reason : uint8_t {
  None, InvalidRequest, AllocationFailed, SessionChanged, Disconnected,
  UnsafeFrame, TransportChanged, Deadline, Ttl
};
struct Light {
  uint32_t color=0;
  uint8_t brightness=0, effect=0, speed=0, magic=0;
};
// Deliberately contains no titles, conversation identity, or pairing PIN.
struct Evidence {
  uint32_t session=0, generation=0, capturedAt=0, elapsed=0, neonFrames=0;
  bool bluetooth=false, nativeSessionSeen=false, preview=false, frozen=false;
  bool hostLightsKnown=false;
  Light slots[6], keys, ambient;
};
struct Context {
  uint32_t session=0, generation=0;
  bool connected=false, authenticated=false, bonded=false;
  bool activeBluetooth=false, switching=false, blocked=false;
  bool renderedBluetooth=false, coherent=false;
};
struct Hooks {
  // May service input/RX only: never render, dispatch commands, or rotate.
  Context (*service)()=nullptr;
  uint32_t (*micros)()=nullptr;
  uint32_t (*millis)()=nullptr;
};
struct Frame {
  uint8_t* pixels=nullptr;
  uint32_t delay=0;
  uint32_t copyUs=0, maxChunkUs=0;
  uint16_t chunks=0;
  bool captured=false, read=false;
  Evidence evidence;
};
class Capture {
public:
  using Allocate=void* (*)(size_t);
  using Release=void (*)(void*);
  static constexpr size_t count=4, width=1280, height=720, bytes=width*height*2;
  static constexpr size_t chunkBytes=16*1024;
  static constexpr uint32_t minDelay=1000, maxDelay=30000, deadline=45000, ttl=120000;
  Capture()=default;
  ~Capture() { releaseAll(); }
  Capture(const Capture&)=delete;
  Capture& operator=(const Capture&)=delete;
  State state() const { return state_; }
  Reason reason() const { return reason_; }
  bool active() const { return state_==State::Armed; }
  uint32_t session() const { return session_; }
  uint32_t armedAt() const { return armedAt_; }
  uint32_t armUs() const { return armUs_; }
  uint32_t armMaxStepUs() const { return armMaxStepUs_; }
  uint8_t captured() const { return next_; }
  const Frame& frame(size_t index) const { return frames_[index]; }
  size_t stored() const {
    size_t result=0;
    for (const auto& frame:frames_) if (frame.captured && frame.pixels) ++result;
    return result;
  }
  bool arm(uint32_t expectedSession,const uint32_t (&delays)[count],uint32_t now,
           const Context& context,Allocate allocate,Release release,Hooks hooks={}) {
    // Do not replace pending or unread evidence, even with an invalid request.
    if (active() || stored()) return false;
    clear();
    bool valid=allocate && release && expectedSession && expectedSession==context.session
        && context.connected && context.authenticated && context.bonded
        && !context.switching && !context.blocked;
    for (size_t i=0;i<count;++i)
      valid &= delays[i]>=minDelay && delays[i]<=maxDelay && (!i || delays[i]>delays[i-1]);
    if (!valid) { state_=State::Aborted; reason_=Reason::InvalidRequest; return false; }
    release_=release;
    const uint32_t started=hooks.micros ? hooks.micros() : 0;
    uint32_t previous=started;
    auto checkpoint=[&]() {
      const uint32_t at=hooks.micros ? hooks.micros() : 0;
      if (uint32_t(at-previous)>armMaxStepUs_) armMaxStepUs_=at-previous;
      previous=at;
      const Reason reason=hooks.service ? validate(hooks.service(),expectedSession,context.generation,false)
                                       : Reason::None;
      if (hooks.micros) armUs_=hooks.micros()-started;
      if (reason!=Reason::None) { abort(reason); return false; }
      return true;
    };
    if (!checkpoint()) return false;
    for (size_t i=0;i<count;++i) {
      frames_[i].pixels=static_cast<uint8_t*>(allocate(bytes));
      if (!checkpoint()) return false;
      if (!frames_[i].pixels) {
        abort(Reason::AllocationFailed); return false;
      }
      frames_[i].delay=delays[i];
    }
    session_=expectedSession; armedAt_=hooks.millis ? hooks.millis() : now; state_=State::Armed;
    return true;
  }
  // Runs even when no frame renders, so expiry cannot depend on UI activity.
  void poll(uint32_t now,const Context& context) {
    if (state_==State::Idle || state_==State::Expired) return;
    const uint32_t elapsed=now-armedAt_;
    if (elapsed>=ttl) {
      state_=State::Expired; reason_=Reason::Ttl; releaseAll(); return;
    }
    if (!active()) return;
    if (context.session!=session_) { abort(Reason::SessionChanged); return; }
    if (!context.connected || !context.authenticated || !context.bonded) {
      abort(Reason::Disconnected); return;
    }
    if (elapsed>=deadline) abort(Reason::Deadline);
  }
  // One real copy at most per call. Unsafe due frames terminate the remaining
  // schedule rather than silently substituting a later UI moment or USB image.
  bool capture(uint32_t now,const Context& context,const uint8_t* framebuffer,
               Evidence evidence,Hooks hooks={}) {
    poll(now,context);
    if (!active() || uint32_t(now-armedAt_)<frames_[next_].delay) return false;
    if (size_t(next_)+1<count && uint32_t(now-armedAt_)>=frames_[next_+1].delay) {
      abort(Reason::Deadline); return false; // no catch-up series of mislabeled moments
    }
    if (!context.activeBluetooth || context.switching
        || (next_ && context.generation!=generation_)) {
      abort(Reason::TransportChanged); return false;
    }
    if (context.blocked || !context.renderedBluetooth || !context.coherent || !framebuffer
        || !evidence.bluetooth || evidence.preview || evidence.frozen
        || evidence.session!=session_ || evidence.generation!=context.generation) {
      abort(Reason::UnsafeFrame); return false;
    }
    auto& frame=frames_[next_];
    const uint32_t started=hooks.micros ? hooks.micros() : 0;
    uint32_t previous=started;
    auto checkpoint=[&]() {
      const uint32_t at=hooks.micros ? hooks.micros() : 0;
      if (uint32_t(at-previous)>frame.maxChunkUs) frame.maxChunkUs=at-previous;
      previous=at;
      const Context current=hooks.service ? hooks.service() : context;
      if (hooks.micros) frame.copyUs=hooks.micros()-started;
      poll(hooks.millis ? hooks.millis() : now,current);
      if (!active()) return false;
      const Reason reason=validate(current,session_,context.generation,true);
      if (reason!=Reason::None) { abort(reason); return false; }
      return true;
    };
    if (!checkpoint()) return false;
    for (size_t offset=0;offset<bytes;offset+=chunkBytes) {
      const size_t length=bytes-offset<chunkBytes ? bytes-offset : chunkBytes;
      memcpy(frame.pixels+offset,framebuffer+offset,length);
      ++frame.chunks;
      // The source retained framebuffer cannot change in this callback. Live
      // input/model state may change, so revalidate even after the final chunk.
      if (!checkpoint()) return false;
    }
    frame.evidence=evidence; frame.evidence.capturedAt=now; frame.evidence.elapsed=now-armedAt_;
    frame.captured=true; generation_=context.generation;
    if (++next_==count) state_=State::Complete;
    return true;
  }
  // Ownership moves to the existing FRAME streamer; TTL/CLEAR cannot free it.
  uint8_t* take(size_t index) {
    if (active() || index>=count || !frames_[index].captured) return nullptr;
    auto& frame=frames_[index];
    uint8_t* pixels=frame.pixels; frame.pixels=nullptr;
    if (pixels) frame.read=true;
    return pixels;
  }
  void clear() {
    state_=State::Idle; reason_=Reason::None;
    releaseAll();
    for (auto& frame:frames_) frame=Frame{};
    session_=generation_=armedAt_=0; next_=0;
    armUs_=armMaxStepUs_=0;
    release_=nullptr;
  }
private:
  Frame frames_[count];
  State state_=State::Idle;
  Reason reason_=Reason::None;
  uint32_t session_=0,generation_=0,armedAt_=0;
  uint32_t armUs_=0,armMaxStepUs_=0;
  uint8_t next_=0;
  Release release_=nullptr;
  static Reason validate(const Context& context,uint32_t session,uint32_t generation,bool requireBle) {
    if (context.session!=session) return Reason::SessionChanged;
    if (!context.connected || !context.authenticated || !context.bonded) return Reason::Disconnected;
    if (context.switching || context.generation!=generation || (requireBle && !context.activeBluetooth))
      return Reason::TransportChanged;
    return context.blocked ? Reason::UnsafeFrame : Reason::None;
  }
  void releaseAll() {
    for (auto& frame:frames_) {
      uint8_t* pixels=frame.pixels;
      frame.pixels=nullptr;
      if (pixels && release_) release_(pixels);
    }
  }
  void abort(Reason why) {
    state_=State::Aborted; reason_=why;
    for (auto& frame:frames_) if (!frame.captured && frame.pixels) {
      uint8_t* pixels=frame.pixels; frame.pixels=nullptr; release_(pixels);
    }
  }
};
} // namespace DeferredFrameCapture
