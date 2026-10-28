#pragma once
#include "ble_pairing_policy.h"
#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace MicroBle {

struct PeerIdentity {
  uint8_t type = 0;
  uint8_t bytes[6] = {};
  bool operator==(const PeerIdentity& other) const {
    return type == other.type && memcmp(bytes, other.bytes, sizeof(bytes)) == 0;
  }
};

// The live SDK bond store changes before AUTH is delivered. It must not itself
// grant a newly persisted peer the pre-existing-bond exemption. Seed this set
// once before advertising; add new peers only after policy accepts their AUTH.
template<size_t Capacity> class BondTrust {
 public:
  bool contains(const PeerIdentity& peer) const {
    for (size_t i=0;i<count_;++i) if (peers_[i]==peer) return true;
    return false;
  }
  bool add(const PeerIdentity& peer) {
    if (contains(peer)) return true;
    if (count_==Capacity) return false;
    peers_[count_++]=peer;
    return true;
  }
  void remove(const PeerIdentity& peer) {
    for (size_t i=0;i<count_;++i) if (peers_[i]==peer) {
      peers_[i]=peers_[--count_]; return;
    }
  }
  void retain(const PeerIdentity* stored,size_t count) {
    for (size_t i=0;i<count_;) {
      bool found=false;
      for (size_t j=0;j<count;++j) if (peers_[i]==stored[j]) { found=true; break; }
      if (found) ++i; else peers_[i]=peers_[--count_];
    }
  }
  void clear() { count_=0; }
 private:
  PeerIdentity peers_[Capacity]{};
  size_t count_=0;
};

enum class LinkAdmission { Denied, Existing, New };

// NimBLE can deliver AUTH/MTU/CCCD before CONNECT while it reads remote
// version/features. Those events must admit exactly one session; a late
// CONNECT must preserve that session's authentication and subscription state.
inline LinkAdmission admitLink(PairingPolicy& policy,uint16_t handle,
                               bool sameLivePeer,bool trustedAndStored,
                               bool enabled,uint32_t now) {
  if (policy.owns(handle))
    return sameLivePeer && enabled ? LinkAdmission::Existing : LinkAdmission::Denied;
  return policy.admit(handle,trustedAndStored,enabled,now)
      ? LinkAdmission::New : LinkAdmission::Denied;
}

} // namespace MicroBle
