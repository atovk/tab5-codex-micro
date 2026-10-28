#include "ble_pairing_policy.h"
#include <cassert>
#include <cstdint>

using MicroBle::PairingPolicy;

int main() {
  {
    PairingPolicy p;
    assert(!p.windowActive(true, 0));
    assert(!p.admit(1, false, true, 0)); // No implicit stranger pairing.
    assert(!p.admit(1, true, false, 0)); // Disabled rejects even known peers.
    assert(!p.admit(PairingPolicy::noConnection, true, true, 0));
    assert(p.admit(1, true, true, 0));
    assert(!p.admit(2, true, true, 0)); // One owned physical session.
    assert(!p.acceptAuthentication(2, true, 0, true));
    assert(p.acceptAuthentication(1, true, 0, true));
    assert(!p.shouldDisconnect(true, 500000));
    assert(!p.allowRepair(1, true, 1)); // Known identity cannot silently re-pair.
    p.disconnect(2);
    assert(p.owns(1));
    p.disconnect(1);
    assert(!p.owns(1));
  }
  {
    PairingPolicy p;
    p.openWindow(100);
    assert(p.windowActive(true, 100));
    assert(!p.windowActive(false, 100));
    assert(p.admit(1, false, true, 100));
    assert(!p.knownPeer());
    assert(!p.shouldDisconnect(true, 120099));
    assert(p.shouldDisconnect(true, 120100));
    assert(!p.acceptAuthentication(1, true, 120100, true));
    // Reopening does not extend the authorization granted to an old connection.
    p.openWindow(120100);
    assert(!p.acceptAuthentication(1, true, 120101, true));
    assert(p.shouldDisconnect(true, 120101));
  }
  {
    PairingPolicy p;
    p.openWindow(50);
    assert(p.admit(7, false, true, 50));
    assert(!p.acceptAuthentication(7, true, 51, false)); // Unencrypted/non-MITM/nonbonded reject.
    assert(p.acceptAuthentication(7, true, 52, true));
    assert(p.knownPeer());
    assert(!p.windowActive(true, 52));
    assert(!p.shouldDisconnect(true, 500000)); // Successful bond survives the old deadline.
    assert(p.acceptAuthentication(7, true, 500001, true)); // Duplicate success/re-encryption is safe.
    assert(p.shouldDisconnect(false, 500002));
    assert(!p.acceptAuthentication(7, false, 500003, true));
  }
  {
    PairingPolicy p;
    assert(p.admit(7, true, true, 0));
    assert(p.acceptAuthentication(7, true, 0, true));
    p.openWindow(10);
    assert(!p.allowRepair(8, true, 10));
    assert(!p.allowRepair(7, false, 10));
    assert(p.allowRepair(7, true, 10));
    assert(!p.knownPeer());
    assert(p.shouldDisconnect(true, 120010));
    assert(!p.acceptAuthentication(7, true, 120010, true));
  }
  {
    PairingPolicy p;
    p.openWindow(20);
    assert(p.admit(1, false, true, 20));
    p.closeWindow();
    assert(p.shouldDisconnect(true, 21));
    assert(!p.acceptAuthentication(1, true, 21, true));
    p.disconnect(1);
    assert(!p.admit(1, false, true, 22)); // Reused handle inherits no old permission.
  }
  {
    PairingPolicy p;
    const uint32_t start = UINT32_MAX - 59999;
    p.openWindow(start);
    assert(p.admit(1, false, true, start));
    assert(p.windowActive(true, 0));
    assert(!p.shouldDisconnect(true, start + PairingPolicy::windowMs - 1));
    assert(p.shouldDisconnect(true, start + PairingPolicy::windowMs));
    assert(!p.acceptAuthentication(1, true, start + PairingPolicy::windowMs, true));
    p.disconnect(1);
    // An exact zero deadline is also handled as a real future time.
    p.openWindow(uint32_t(0u - PairingPolicy::windowMs));
    assert(p.admit(1, false, true, UINT32_MAX));
    assert(!p.acceptAuthentication(1, true, 0, true));
  }
}
