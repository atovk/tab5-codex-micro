#pragma once
#include <stdint.h>
#include <stddef.h>

namespace MicroTransport {
enum class Link : uint8_t { None, Usb, Bluetooth };

// A UI gesture belongs to one selected endpoint/session. Reconnecting an
// inactive endpoint must not cancel a touch on the still-selected endpoint.
class SessionGeneration {
public:
  uint32_t value() const { return generation_; }
  void update(Link link,uint32_t session) {
    if (link!=link_ || (link!=Link::None && session!=session_)) ++generation_;
    link_=link; session_=session;
  }
private:
  Link link_=Link::None;
  uint32_t session_=0,generation_=0;
};

enum class Assembly : uint8_t { Pending, Complete, Overflow };
// Each endpoint owns its own framing buffer. Alternating USB and BLE reports
// must never splice two JSON-RPC messages together.
class JsonAssembler {
public:
  static constexpr size_t capacity=4096;
  const char* data() const { return bytes_; }
  size_t size() const { return size_; }
  void reset() { size_=0; depth_=0; string_=escaped_=false; }
  Assembly feed(uint8_t ch) {
    if (!size_ && ch!='{') return Assembly::Pending;
    if (size_>=capacity-1) { reset(); return Assembly::Overflow; }
    bytes_[size_++]=char(ch);
    if (string_) {
      if (escaped_) escaped_=false;
      else if (ch=='\\') escaped_=true;
      else if (ch=='"') string_=false;
    } else {
      if (ch=='"') string_=true;
      else if (ch=='{' || ch=='[') ++depth_;
      else if (ch=='}' || ch==']') --depth_;
    }
    if (depth_) return Assembly::Pending;
    bytes_[size_]=0; return Assembly::Complete;
  }
private:
  char bytes_[capacity]={};
  size_t size_=0;
  int depth_=0;
  bool string_=false,escaped_=false;
};

// Only one endpoint may receive native inputs. A live BLE endpoint gets a
// bounded opportunity to release its controls before USB takes priority. No
// input, release, or partially framed response is migrated to another link.
class Selection {
public:
  static constexpr uint32_t cleanupTimeoutMs=180;
  Link active() const { return active_; }
  bool switching() const { return switching_; }
  bool beginCleanup() const { return beginCleanup_; }
  bool forceBleDisconnect() const { return forceBleDisconnect_; }
  bool changed() const { return changed_; }

  void update(bool usb,bool ble,bool cleanupPending,uint32_t now) {
    beginCleanup_=forceBleDisconnect_=changed_=false;
    const Link desired=usb ? Link::Usb : ble ? Link::Bluetooth : Link::None;
    const bool oldConnected=active_==Link::Usb ? usb : active_==Link::Bluetooth && ble;
    if (active_==desired) { switching_=false; return; }
    if (!oldConnected) { select(desired); return; }
    if (!switching_) {
      switching_=true; since_=now; beginCleanup_=true;
    }
    if (!cleanupPending) { select(desired); return; }
    if (uint32_t(now-since_)>=cleanupTimeoutMs) {
      forceBleDisconnect_=active_==Link::Bluetooth;
      select(desired);
    }
  }
private:
  Link active_=Link::None;
  uint32_t since_=0;
  bool switching_=false,beginCleanup_=false,forceBleDisconnect_=false,changed_=false;
  void select(Link value) { changed_=active_!=value; active_=value; switching_=false; }
};
} // namespace MicroTransport
