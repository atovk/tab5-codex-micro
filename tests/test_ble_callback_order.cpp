// Production callbacks are extracted unchanged by test_ble_callback_order.py.
#include "ble_session_policy.h"
#include <algorithm>
#include <cassert>
#include <cstring>
#include <vector>
#define CONFIG_BT_NIMBLE_MAX_BONDS 3
#define portENTER_CRITICAL(x) ((void)(x))
#define portEXIT_CRITICAL(x) ((void)(x))
using namespace MicroBle;
constexpr uint16_t BLE_HS_CONN_HANDLE_NONE=0xffff;
constexpr int BLE_ERR_AUTH_FAIL=5, BLE_GAP_EVENT_SUBSCRIBE=1;
struct ble_addr_t { uint8_t type=0; uint8_t val[6]={}; };
struct ble_gap_conn_desc {
  uint16_t conn_handle=0;
  ble_addr_t peer_id_addr{},peer_ota_addr{};
  struct { bool encrypted=false,authenticated=false,bonded=false; } sec_state;
};
struct ble_gap_event {
  int type=BLE_GAP_EVENT_SUBSCRIBE;
  struct { uint16_t conn_handle,attr_handle; bool cur_notify; } subscribe{};
};
struct BLEServer {};
struct BLEServerCallbacks {
  virtual void onConnect(BLEServer*,ble_gap_conn_desc*) {}
  virtual void onDisconnect(BLEServer*,ble_gap_conn_desc*) {}
  virtual void onMtuChanged(BLEServer*,ble_gap_conn_desc*,uint16_t) {}
};
struct BLESecurityCallbacks {
  virtual bool onSecurityRequest() { return false; }
  virtual void onPassKeyNotify(uint32_t) {}
  virtual bool onConfirmPIN(uint32_t) { return false; }
  virtual bool onAuthorizationRequest(uint16_t,uint16_t,bool) { return false; }
  virtual void onAuthenticationComplete(ble_gap_conn_desc*,int) {}
};
struct Characteristic { uint16_t handle; uint16_t getHandle() const { return handle; } };
struct SessionGuard { explicit SessionGuard(bool) {} };
int mutex=0;
bool desiredEnabled=true,forgetRequested=false;
struct State {
  bool enabled=true,connected=false,ready=false,passkeyValid=false,pairing=false,bonded=false;
  uint32_t session=0,passkey=0;
  char error[96]={};
} current;
uint16_t connection=BLE_HS_CONN_HANDLE_NONE,mtu=23;
bool encrypted=false,authenticated=false,hidSubscribed=false,metadataSubscribed=false;
PairingPolicy pairingPolicy;
BondTrust<CONFIG_BT_NIMBLE_MAX_BONDS> trustedBonds;
ble_addr_t activePeerOta{},activePeerIdentity{};
size_t accumulated=0;
bool discardLine=false;
int lastAuthenticationResult=-1,lastAuthenticationEvent=-1,lastAuthenticationFlags=0,lastSecurityStartResult=-1;
Characteristic hid{6},metadata{1002};
Characteristic *input=&hid,*metadataTx=&metadata;
std::vector<ble_addr_t> storedBonds;
ble_gap_conn_desc live;
uint16_t liveMtu=23;
uint32_t now=0;
int terminated=0,securityRequests=0;
void (*pairedLookupHook)()=nullptr;
uint32_t millis() { return now; }
bool same(const ble_addr_t& a,const ble_addr_t& b) { return a.type==b.type && !memcmp(a.val,b.val,6); }
bool pairedPeer(const ble_addr_t& a) {
  const bool found=std::any_of(storedBonds.begin(),storedBonds.end(),[&](const auto& b){return same(a,b);});
  if (pairedLookupHook) { auto hook=pairedLookupHook; pairedLookupHook=nullptr; hook(); }
  return found;
}
int ble_store_util_bonded_peers(ble_addr_t* peers,int* count,int capacity) {
  assert(storedBonds.size()<=size_t(capacity)); *count=storedBonds.size();
  std::copy(storedBonds.begin(),storedBonds.end(),peers); return 0;
}
int ble_store_util_delete_peer(const ble_addr_t* peer) {
  storedBonds.erase(std::remove_if(storedBonds.begin(),storedBonds.end(),[&](const auto& p){return same(*peer,p);}),storedBonds.end());
  return 0;
}
uint16_t ble_att_mtu(uint16_t) { return liveMtu; }
int ble_gap_terminate(uint16_t,int) { ++terminated; return 0; }
int ble_gap_security_initiate(uint16_t) { ++securityRequests; return 0; }
int ble_gap_conn_find(uint16_t handle,ble_gap_conn_desc* desc) {
  if (handle!=live.conn_handle) return -1; *desc=live; return 0;
}
bool secure(const ble_gap_conn_desc* desc) { return desc && desc->sec_state.encrypted && desc->sec_state.authenticated; }
void error(const char* text) { std::strncpy(current.error,text,sizeof(current.error)-1); }
void refreshReadyLocked() {
  current.ready=current.enabled && current.connected && encrypted && authenticated && hidSubscribed && mtu>=66;
}
#include "ble_callbacks.inc"

ble_addr_t peer(unsigned value) { ble_addr_t p{}; p.val[0]=value; return p; }
void reset() {
  desiredEnabled=true; forgetRequested=false; current=State{};
  connection=BLE_HS_CONN_HANDLE_NONE; mtu=liveMtu=23;
  encrypted=authenticated=hidSubscribed=metadataSubscribed=false;
  pairingPolicy=PairingPolicy{}; trustedBonds.clear(); storedBonds={peer(1)};
  trustedBonds.add(identity(peer(1))); activePeerOta=activePeerIdentity=ble_addr_t{};
  live=ble_gap_conn_desc{}; live.conn_handle=1; live.peer_id_addr=live.peer_ota_addr=peer(1);
  lastAuthenticationResult=lastAuthenticationEvent=lastSecurityStartResult=-1;
  lastAuthenticationFlags=0; now=0; terminated=securityRequests=0; pairedLookupHook=nullptr;
}
void connect() { static_cast<BLEServerCallbacks*>(&serverCallbacks)->onConnect(nullptr,&live); }
void disconnect() { static_cast<BLEServerCallbacks*>(&serverCallbacks)->onDisconnect(nullptr,&live); }
void auth(int result=0) { static_cast<BLESecurityCallbacks*>(&securityCallbacks)->onAuthenticationComplete(&live,result); }
void setMtu(uint16_t value) {
  liveMtu=value;
  static_cast<BLEServerCallbacks*>(&serverCallbacks)->onMtuChanged(nullptr,&live,value);
}
void subscribe(uint16_t handle,bool enabled=true) {
  ble_gap_event event; event.subscribe={live.conn_handle,handle,enabled}; gapListener(&event,nullptr);
}
void secureLive() { live.sec_state={true,true,true}; }

int main() {
  // Observed failure: bonded AUTH/CCCD/MTU all precede delayed CONNECT.
  reset(); secureLive(); auth(); subscribe(6); subscribe(1002); setMtu(90);
  assert(current.session==1 && authenticated && current.ready && metadataSubscribed);
  connect();
  assert(current.session==1 && authenticated && current.ready && metadataSubscribed);
  assert(lastAuthenticationResult==0 && securityRequests==0 && terminated==0);
  now=500000; auth(); connect(); // Duplicate success after closed/expired window.
  assert(current.ready && terminated==0 && current.session==1);

  // CCCD/MTU may arrive before AUTH as well. They do not grant native readiness.
  reset(); setMtu(90); subscribe(6); assert(!current.ready && !authenticated);
  secureLive(); auth(); assert(current.ready); connect(); assert(current.ready && current.session==1);
  subscribe(6,false); connect(); assert(!current.ready); // Late CONNECT cannot resurrect a subscription.

  // SDK persisted a fresh unknown bond before AUTH; it is not startup-trusted.
  reset(); live.peer_id_addr=live.peer_ota_addr=peer(2); storedBonds.push_back(peer(2));
  secureLive(); auth();
  assert(terminated==1 && current.session==0 && !authenticated);
  assert(!pairedPeer(peer(2)) && pairedPeer(peer(1)) && !trustedBonds.contains(identity(peer(2))));
  connect(); assert(current.session==0); // Later CONNECT cannot upgrade rejected trust.

  // A physical window permits new AUTH before CONNECT, then trusted reconnect.
  reset(); pairingPolicy.openWindow(now); current.pairing=true;
  live.peer_id_addr=live.peer_ota_addr=peer(2); storedBonds.push_back(peer(2));
  secureLive(); auth(); connect();
  assert(authenticated && trustedBonds.contains(identity(peer(2))) && current.session==1);
  assert(!pairingPolicy.windowActive(true,now));
  subscribe(6); setMtu(90); assert(current.ready);
  disconnect(); live.sec_state={}; connect();
  assert(current.session==2 && !authenticated && !hidSubscribed && !metadataSubscribed);
  secureLive(); auth(); subscribe(6); assert(current.ready && current.session==2);

  // Early MTU does not extend a fresh peer's physical pairing deadline.
  reset(); pairingPolicy.openWindow(now); live.peer_id_addr=live.peer_ota_addr=peer(2);
  setMtu(90); now=PairingPolicy::windowMs;
  storedBonds.push_back(peer(2)); secureLive(); auth();
  assert(!authenticated && terminated==1 && !pairedPeer(peer(2)) && pairedPeer(peer(1)));

  // MITM, encryption, bond store and result checks remain strict on reconnect.
  for (int failure=0;failure<4;++failure) {
    reset(); secureLive();
    if (failure==0) live.sec_state.authenticated=false;
    if (failure==1) live.sec_state.encrypted=false;
    if (failure==2) live.sec_state.bonded=false;
    auth(failure==3 ? 7 : 0);
    assert(!authenticated && !current.ready && terminated==1 && pairedPeer(peer(1)));
  }
  reset(); storedBonds.clear(); connect(); assert(current.session==0 && terminated==1);
  reset(); desiredEnabled=false; secureLive(); auth(); assert(!authenticated && pairedPeer(peer(1)));

  // A second physical peer cannot reuse an active handle and retain its state.
  reset(); secureLive(); auth(); live.peer_ota_addr=peer(2); connect();
  assert(terminated==1 && current.session==1);

  // Authorized repair revokes the old exemption; failed replacement removes
  // that peer's new store entry without deleting an unrelated trusted bond.
  reset(); storedBonds.push_back(peer(2)); trustedBonds.add(identity(peer(2)));
  secureLive(); auth(); pairingPolicy.openWindow(now);
  assert(allowRepair(live.conn_handle)); assert(!authenticated);
  assert(!trustedBonds.contains(identity(peer(1))));
  auth(7); assert(!pairedPeer(peer(1)) && pairedPeer(peer(2)));
  assert(trustedBonds.contains(identity(peer(2))));

  // Forget races after ensureAdmitted snapshots trusted=true and during its
  // NVS lookup. Cached known must never override the still-pending revocation.
  reset(); pairingPolicy.openWindow(now); current.pairing=true;
  pairedLookupHook=clearBonds; secureLive(); auth();
  assert(forgetRequested && !authenticated && current.session==0 && terminated==1);
  assert(!trustedBonds.contains(identity(peer(1))) && !pairingPolicy.windowActive(true,now));

  // NVS can evict one old bond when storing a new one. Keep only previously
  // accepted surviving peers, leaving capacity for the newly authorized peer.
  reset(); storedBonds={peer(1),peer(2),peer(3)};
  trustedBonds.add(identity(peer(2))); trustedBonds.add(identity(peer(3)));
  pairingPolicy.openWindow(now); storedBonds={peer(2),peer(3),peer(4)};
  live.peer_id_addr=live.peer_ota_addr=peer(4); secureLive(); auth();
  assert(authenticated && trustedBonds.contains(identity(peer(4))));
  assert(!trustedBonds.contains(identity(peer(1))) && trustedBonds.contains(identity(peer(2))));
  return 0;
}
