#pragma once
#include <stdint.h>

namespace MicroBle {

// Pure authorization state. The caller serializes every access with the same
// lock as its BLE connection/session state. This class performs no I/O.
class PairingPolicy {
public:
  static constexpr uint32_t windowMs = 120000;
  static constexpr uint16_t noConnection = 0xffff;

  void openWindow(uint32_t now) {
    windowOpen_ = true;
    windowUntil_ = now + windowMs;
  }
  void closeWindow() { windowOpen_ = false; }
  bool windowActive(bool enabled, uint32_t now) const {
    return enabled && windowOpen_ && before(now, windowUntil_);
  }

  // An already bonded identity may reconnect without opening pairing. A new
  // identity receives only the remaining time in this physical pairing window.
  bool admit(uint16_t handle, bool known, bool enabled, uint32_t now) {
    if (handle == noConnection || connected_ || !enabled || (!known && !windowActive(enabled, now))) return false;
    connected_ = true;
    handle_ = handle;
    known_ = known;
    authenticated_ = false;
    admittedUntil_ = known ? 0 : windowUntil_;
    return true;
  }
  bool owns(uint16_t handle) const { return connected_ && handle_ == handle; }
  bool knownPeer() const { return connected_ && known_; }

  // Re-pairing replaces existing trust. It must never inherit the known-peer
  // exemption, even if a previous encrypted connection used the same identity.
  bool allowRepair(uint16_t handle, bool enabled, uint32_t now) {
    if (!owns(handle) || !windowActive(enabled, now)) return false;
    known_ = false;
    authenticated_ = false;
    admittedUntil_ = windowUntil_;
    return true;
  }

  // securityValid means the SDK reported success, encryption, MITM
  // authentication, and bonding. Recheck policy at completion, not only connect.
  bool acceptAuthentication(uint16_t handle, bool enabled, uint32_t now, bool securityValid) {
    if (!owns(handle)) return false;
    const bool permitted = enabled && (known_ || newPeerAllowed(enabled, now));
    authenticated_ = permitted && securityValid;
    if (authenticated_) {
      known_ = true;  // This connection has now established a verified bond.
      closeWindow();
    }
    return authenticated_;
  }

  bool shouldDisconnect(bool enabled, uint32_t now) const {
    return connected_ && (!enabled || (!known_ && !authenticated_ && !newPeerAllowed(enabled, now)));
  }

  void disconnect(uint16_t handle) {
    if (!owns(handle)) return;
    connected_ = known_ = authenticated_ = false;
    handle_ = noConnection;
    admittedUntil_ = 0;
  }

private:
  // All windows are shorter than 2^31 ms, so unsigned subtraction works across
  // millis() rollover without using zero as an ambiguous deadline sentinel.
  static bool before(uint32_t now, uint32_t deadline) {
    return uint32_t(deadline - now) != 0 && uint32_t(deadline - now) < 0x80000000u;
  }
  bool newPeerAllowed(bool enabled, uint32_t now) const {
    return windowActive(enabled, now) && before(now, admittedUntil_);
  }
  bool windowOpen_ = false, connected_ = false, known_ = false, authenticated_ = false;
  uint16_t handle_ = noConnection;
  uint32_t windowUntil_ = 0, admittedUntil_ = 0;
};

} // namespace MicroBle
