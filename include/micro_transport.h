#pragma once

#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

// Scheduling only: no Arduino, physical transport, or execution assumptions. A
// successful submission means the transport accepted a report, not that the
// desktop application consumed or executed it.
namespace MicroTransport {

enum class Kind : uint8_t { Response, Key, Stick };
constexpr const char* keys[] = {
  "AG00", "AG01", "AG02", "AG03", "AG04", "AG05",
  "ACT06", "ACT07", "ACT08", "ACT09", "ACT10", "ACT11", "ACT12", "ENC",
  "ENC_CW", "ENC_CC"
};
constexpr uint8_t heldKeyCount = 14;
inline int keyIndex(const char* key) {
  if (key) for (unsigned i=0;i<sizeof(keys)/sizeof(*keys);++i)
    if (!strcmp(key,keys[i])) return int(i);
  return -1;
}
struct Tag {
  Kind kind=Kind::Response;
  int8_t key=-1;
  uint8_t action=0;
  // 0 = neutral, 1 = below the action threshold, 2..5 = cardinal sectors.
  uint8_t stickZone=0;
};
inline uint8_t stickZone(float angle,float distance) {
  if (distance<=0) return 0;
  if (distance<.5f) return 1;
  return uint8_t(2+(int((angle+.125f)*4)%4));
}
struct Message {
  static constexpr size_t capacity=320;
  char bytes[capacity]={};
  uint16_t length=0, offset=0;
  uint32_t at=0;
  Tag tag;
};
struct Stats {
  uint32_t queued=0, submitted=0, reports=0, failed=0;
  uint32_t coalesced=0, dropped=0, stale=0, recoveries=0, disconnects=0;
};

enum class Receipt : uint8_t { Clear, Submitted, NotSent };
// A click receipt requires both edges of the same gesture. A cleanup release
// after cancellation must not turn the failed gesture back into a success.
class KeyReceipts {
public:
  Receipt begin(uint8_t key) {
    if (key<heldKeyCount) pressSubmitted_[key]=false;
    return Receipt::Clear;
  }
  Receipt failed(uint8_t key) {
    if (key<heldKeyCount) pressSubmitted_[key]=false;
    return Receipt::NotSent;
  }
  Receipt submitted(uint8_t key,uint8_t action) {
    if (key>=heldKeyCount || action>1) return Receipt::Clear;
    if (action==1) { pressSubmitted_[key]=true; return Receipt::Clear; }
    const bool paired=pressSubmitted_[key]; pressSubmitted_[key]=false;
    return paired ? Receipt::Submitted : Receipt::NotSent;
  }
  void reset() { memset(pressSubmitted_,0,sizeof(pressSubmitted_)); }
private:
  bool pressSubmitted_[heldKeyCount]={};
};

class Queue {
public:
  static constexpr unsigned capacity=32;
  static constexpr uint32_t inputLifetimeMs=250;
  const Stats& stats() const { return stats_; }
  unsigned size() const { return size_; }
  uint16_t heldKeys() const { return held_; }
  bool stickActive() const { return stick_; }
  bool recoveryPending() const { return recoveryKeys_ || recoveryStick_; }
  bool nativeIdle() const {
    if (held_ || stick_ || recoveryPending() || cleanup_.length) return false;
    for (unsigned i=0;i<size_;++i) if (items_[i].tag.kind!=Kind::Response) return false;
    return true;
  }
  // Cancellation is asynchronous to enqueue(). Report affected native keys
  // once, before a cleanup submission can update its gesture receipt.
  uint16_t takeDroppedKeys() { const auto keys=droppedKeys_; droppedKeys_=0; return keys; }

  bool enqueue(const char* text,size_t length,Tag tag,uint32_t now) {
    if (!text || !length || length+1>Message::capacity) { ++stats_.dropped; return false; }
    const bool input=tag.kind!=Kind::Response;
    // Native events fit one report. Keeping this invariant lets cancellation
    // discard queued input without leaving a half JSON message at the host.
    if (input && length+1>61) { ++stats_.dropped; return false; }
    if (input && (suspended_ || recoveryPending())) { ++stats_.dropped; return false; }
    if (tag.kind==Kind::Key && tag.action==0 && tag.key>=0 && tag.key<heldKeyCount) {
      bool pressed=(held_&(1u<<tag.key))!=0;
      for (unsigned i=0;i<size_;++i) {
        const auto& pending=items_[i].tag;
        if (pending.kind==Kind::Key && pending.key==tag.key && pending.action<=1)
          pressed=pending.action==1;
      }
      // Touches beginning in diagnostic capture cannot leak an orphan release
      // when capture ends, even if the caller misses its finger-lift handoff.
      if (!pressed) { ++stats_.dropped; return false; }
    }
    // Preserve neutral, threshold crossings, and direction changes. Only the
    // newest movement within the same sector may replace an unsent tail.
    if (tag.kind==Kind::Stick && tag.stickZone && size_) {
      auto& tail=items_[size_-1];
      if (!tail.offset && tail.tag.kind==Kind::Stick && tail.tag.stickZone==tag.stickZone) {
        assign(tail,text,length,tag,now); ++stats_.coalesced; return true;
      }
    }
    if (size_==capacity) {
      ++stats_.dropped;
      if (input) cancelInputs();
      return false;
    }
    assign(items_[size_++],text,length,tag,now); ++stats_.queued;
    return true;
  }

  // Called once per endpoint poll; a failed or busy endpoint leaves the exact next
  // fragment intact. An active multi-report reply is never interleaved.
  const Message* next(uint32_t now) {
    servingCleanup_=false;
    for (unsigned i=0;i<size_;++i) {
      if (items_[i].tag.kind!=Kind::Response && uint32_t(now-items_[i].at)>inputLifetimeMs) {
        ++stats_.stale; cancelInputs(); break;
      }
    }
    if (size_ && items_[0].offset) return &items_[0];
    if (!suspended_ && recoveryPending()) {
      if (!cleanup_.length) makeCleanup(now);
      servingCleanup_=true;
      return &cleanup_;
    }
    return size_ ? &items_[0] : nullptr;
  }
  void failed() { ++stats_.failed; }
  // Returns true only after the entire message has been submitted. The caller
  // may show a transport receipt for completed.tag, never an execution ack.
  bool submitted(size_t count,Message& completed) {
    Message* current=servingCleanup_ ? &cleanup_ : (size_ ? &items_[0] : nullptr);
    if (!current || !count || count>size_t(current->length-current->offset)) return false;
    current->offset+=count; ++stats_.reports;
    if (current->offset!=current->length) return false;
    completed=*current;
    const auto tag=current->tag;
    if (tag.kind==Kind::Key && tag.key>=0 && tag.key<heldKeyCount && tag.action<=1) {
      const uint16_t bit=uint16_t(1u<<tag.key);
      if (tag.action) held_|=bit;
      else { held_&=~bit; recoveryKeys_&=~bit; }
    } else if (tag.kind==Kind::Stick) {
      stick_=tag.stickZone!=0;
      if (!stick_) recoveryStick_=false;
    }
    if (current==&cleanup_) { cleanup_={}; ++stats_.recoveries; }
    else remove(0);
    ++stats_.submitted;
    return true;
  }
  // Cancel queued actions in this link session, then release only controls that
  // were actually submitted. Cleanup bypasses a full queue and precedes new
  // actions; no new presses are accepted until cleanup has been submitted.
  void cancelInputs() {
    droppedKeys_|=held_;
    for (unsigned i=0;i<size_;) {
      if (items_[i].tag.kind!=Kind::Response) {
        const auto& tag=items_[i].tag;
        if (tag.kind==Kind::Key && tag.key>=0 && tag.key<heldKeyCount && tag.action<=1)
          droppedKeys_|=uint16_t(1u<<tag.key);
        remove(i); ++stats_.dropped;
      }
      else ++i;
    }
    recoveryKeys_|=held_; recoveryStick_|=stick_;
  }
  void suspendInputs(bool enabled) {
    if (enabled && !suspended_) cancelInputs();
    suspended_=enabled;
  }
  // A new link session must not inherit stale presses, turns, releases, or
  // partially framed replies. In particular, a release can itself trigger a
  // desktop action, so recovery is never replayed across a disconnect.
  void disconnect() {
    stats_.dropped+=size_; ++stats_.disconnects;
    size_=0; held_=recoveryKeys_=droppedKeys_=0; stick_=recoveryStick_=false; cleanup_={};
    servingCleanup_=false;
  }

private:
  Message items_[capacity],cleanup_;
  unsigned size_=0;
  uint16_t held_=0,recoveryKeys_=0,droppedKeys_=0;
  bool stick_=false,recoveryStick_=false,suspended_=false,servingCleanup_=false;
  Stats stats_;
  static void assign(Message& dst,const char* text,size_t length,Tag tag,uint32_t now) {
    dst={}; memcpy(dst.bytes,text,length); dst.bytes[length]='\n';
    dst.length=length+1; dst.tag=tag; dst.at=now;
  }
  void remove(unsigned index) {
    for (unsigned i=index+1;i<size_;++i) items_[i-1]=items_[i];
    --size_;
  }
  void makeCleanup(uint32_t now) {
    char line[80]; Tag tag;
    if (recoveryKeys_) {
      unsigned key=0; while (!(recoveryKeys_&(1u<<key))) ++key;
      tag={Kind::Key,int8_t(key),0,0};
      snprintf(line,sizeof(line),"{\"m\":\"v.oai.hid\",\"p\":{\"k\":\"%s\",\"act\":0}}",keys[key]);
    } else {
      tag={Kind::Stick,-1,0,0};
      strcpy(line,"{\"m\":\"v.oai.rad\",\"p\":{\"a\":0,\"d\":0}}");
    }
    assign(cleanup_,line,strlen(line),tag,now);
  }
};

// Whole diagnostic lines are retained while a binary SCREEN frame owns CDC.
// Overflow drops a complete line and is observable; it never inserts a partial
// JSON record into the RGB565 stream or grows memory with the capture duration.
class DeferredLines {
public:
  static constexpr size_t capacity=8, lineCapacity=160;
  bool push(const char* line,size_t length) {
    if (size_==capacity || !line || length+1>lineCapacity) { ++dropped_; return false; }
    const auto index=(head_+size_)%capacity;
    memcpy(lines_[index],line,length); lines_[index][length]='\n';
    lengths_[index]=length+1; ++size_; return true;
  }
  const char* data() const { return size_ ? lines_[head_]+offset_ : nullptr; }
  size_t remaining() const { return size_ ? lengths_[head_]-offset_ : 0; }
  void consumed(size_t count) {
    if (!size_ || count>remaining()) return;
    offset_+=count;
    if (offset_==lengths_[head_]) { head_=(head_+1)%capacity; --size_; offset_=0; }
  }
  size_t size() const { return size_; }
  uint32_t dropped() const { return dropped_; }
private:
  char lines_[capacity][lineCapacity]={};
  size_t lengths_[capacity]={},head_=0,size_=0,offset_=0;
  uint32_t dropped_=0;
};
} // namespace MicroTransport
