#include "micro_ble.h"
#include "firmware_version.h"
#include "ble_pairing_policy.h"
#include "ble_session_policy.h"
#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLESecurity.h>
#include <BLEDescriptor.h>
#include <Preferences.h>
#include <ArduinoJson.h>
#include <esp_random.h>
#include <esp32-hal-hosted.h>
#include <host/ble_hs.h>
#include <host/ble_store.h>
#include <host/ble_gap.h>
#include <host/ble_att.h>
#include <host/ble_gatt.h>
#include <services/gatt/ble_svc_gatt.h>
#include <freertos/semphr.h>
#include <atomic>
#include <algorithm>
#include <cstring>

extern "C" bool tab5_ble_ecc_self_test(void);

namespace MicroBle {
namespace {
constexpr char serviceUuid[]="8f4d1000-7d4b-4b58-9e53-5e4ad07e7a50";
constexpr char rxUuid[]="8f4d1001-7d4b-4b58-9e53-5e4ad07e7a50";
constexpr char txUuid[]="8f4d1002-7d4b-4b58-9e53-5e4ad07e7a50";
constexpr char identityUuid[]="8f4d1003-7d4b-4b58-9e53-5e4ad07e7a50";
constexpr size_t maxLine=6144;
// A standard keyboard collection lets macOS install its HOGP HID driver.
// Native Micro input uses only the vendor collection, report 6.
const uint8_t reportMap[]={
  0x05,0x01,0x09,0x06,0xa1,0x01,0x85,0x01,
  0x05,0x07,0x19,0xe0,0x29,0xe7,0x15,0x00,0x25,0x01,
  0x75,0x01,0x95,0x08,0x81,0x02,0x95,0x01,0x75,0x08,0x81,0x01,
  0x95,0x05,0x75,0x01,0x05,0x08,0x19,0x01,0x29,0x05,0x91,0x02,
  0x95,0x01,0x75,0x03,0x91,0x01,0x95,0x06,0x75,0x08,
  0x15,0x00,0x25,0x65,0x05,0x07,0x19,0x00,0x29,0x65,0x81,0x00,0xc0,
  0x06,0x00,0xff,0x09,0x01,0xa1,0x01,0x85,0x06,
  0x15,0x00,0x26,0xff,0x00,0x75,0x08,0x95,0x3f,
  0x09,0x02,0x81,0x02,0x95,0x3f,0x09,0x03,0x91,0x02,
  0x95,0x3f,0x09,0x04,0xb1,0x02,0xc0
};
portMUX_TYPE mutex=portMUX_INITIALIZER_UNLOCKED;
MicroLink::BleStatus current{};
std::atomic<bool> desiredEnabled{true}, pairRequested{false}, forgetRequested{false};
std::atomic<bool> disconnectRequested{false}, stackStarted{false};
std::atomic<uint32_t> hidSubmitted{0}, hidReceived{0}, metadataReceived{0}, notifyFailed{0};
std::atomic<int> lastAuthenticationResult{-1}, lastSecurityStartResult{-1};
std::atomic<int> lastAuthenticationEvent{-1};
std::atomic<uint8_t> lastAuthenticationFlags{0};
std::atomic<bool> eccSelfTestPassed{false};
uint16_t connection=BLE_HS_CONN_HANDLE_NONE, mtu=23;
bool encrypted=false, authenticated=false, hidSubscribed=false, metadataSubscribed=false;
PairingPolicy pairingPolicy;
BondTrust<CONFIG_BT_NIMBLE_MAX_BONDS> trustedBonds;
ble_addr_t activePeerOta{}, activePeerIdentity{};
uint32_t initStarted=0;
char chipSerial[24]={};
BLEServer* server=nullptr;
BLECharacteristic *input=nullptr, *metadataTx=nullptr;
QueueHandle_t commands=nullptr, replies=nullptr;
struct Command { uint32_t session; char text[maxLine+1]; };
struct Reply { uint32_t session; char text[128]; };
char accumulating[maxLine+1];
Command callbackLine{};
Command mainLine{};
size_t accumulated=0;
bool discardLine=false;
Preferences preferences;
SemaphoreHandle_t txSessionMutex=nullptr;
struct SessionGuard {
  bool locked;
  explicit SessionGuard(bool wait):locked(txSessionMutex &&
      xSemaphoreTake(txSessionMutex,wait ? portMAX_DELAY : 0)==pdTRUE) {}
  ~SessionGuard() { if (locked) xSemaphoreGive(txSessionMutex); }
};

void error(const char* value) {
  portENTER_CRITICAL(&mutex);
  strlcpy(current.error,value,sizeof(current.error));
  portEXIT_CRITICAL(&mutex);
}
void refreshReadyLocked() {
  current.ready=current.enabled && current.connected && encrypted && authenticated
      && hidSubscribed && mtu>=66;
}
bool pairedPeer(const ble_addr_t& peer) {
  ble_addr_t peers[CONFIG_BT_NIMBLE_MAX_BONDS];
  int count=0;
  if (ble_store_util_bonded_peers(peers,&count,CONFIG_BT_NIMBLE_MAX_BONDS)) return false;
  for (int i=0;i<count;++i)
    if (peers[i].type==peer.type && !memcmp(peers[i].val,peer.val,6)) return true;
  return false;
}
void refreshBonded() {
  ble_addr_t peers[CONFIG_BT_NIMBLE_MAX_BONDS]; int count=0;
  ble_store_util_bonded_peers(peers,&count,CONFIG_BT_NIMBLE_MAX_BONDS);
  portENTER_CRITICAL(&mutex); current.bonded=count>0; portEXIT_CRITICAL(&mutex);
}
bool secure(const ble_gap_conn_desc* desc) {
  return desc && desc->sec_state.encrypted && desc->sec_state.authenticated;
}
PeerIdentity identity(const ble_addr_t& address) {
  PeerIdentity result; result.type=address.type;
  memcpy(result.bytes,address.val,sizeof(result.bytes)); return result;
}
bool readStoredPeers(PeerIdentity* output,int& count) {
  ble_addr_t peers[CONFIG_BT_NIMBLE_MAX_BONDS]; count=0;
  if (ble_store_util_bonded_peers(peers,&count,CONFIG_BT_NIMBLE_MAX_BONDS)) return false;
  for (int i=0;i<count;++i) output[i]=identity(peers[i]);
  return true;
}
// Caller holds SessionGuard. All admission paths use the pre-existing trust
// snapshot AND the current bond store. A new SDK-persisted bond cannot grant
// itself the known-peer exemption before the application's policy accepts it.
bool ensureAdmitted(const ble_gap_conn_desc* desc) {
  if (!desc) return false;
  const auto peer=identity(desc->peer_id_addr);
  portENTER_CRITICAL(&mutex);
  const bool trusted=trustedBonds.contains(peer);
  portEXIT_CRITICAL(&mutex);
  const bool known=trusted && pairedPeer(desc->peer_id_addr);
  const uint16_t liveMtu=ble_att_mtu(desc->conn_handle);
  portENTER_CRITICAL(&mutex);
  const bool sameLivePeer=connection==desc->conn_handle &&
      identity(activePeerOta)==identity(desc->peer_ota_addr);
  const auto admitted=admitLink(pairingPolicy,desc->conn_handle,sameLivePeer,known,
                               desiredEnabled && !forgetRequested,millis());
  if (admitted==LinkAdmission::New) {
    connection=desc->conn_handle;
    activePeerOta=desc->peer_ota_addr; activePeerIdentity=desc->peer_id_addr;
    mtu=liveMtu>=23 ? liveMtu : 23;
    encrypted=authenticated=hidSubscribed=metadataSubscribed=false;
    ++current.session; current.connected=true; current.ready=false;
    current.passkeyValid=false;
  } else if (admitted==LinkAdmission::Existing && liveMtu>=23) {
    mtu=liveMtu;
  }
  portEXIT_CRITICAL(&mutex);
  if (admitted==LinkAdmission::Denied) {
    // NimBLE persists fresh keys before AUTH. Remove an unauthorized new bond,
    // while retaining a previously trusted peer's keys on transient failures.
    if (!trusted && desc->sec_state.bonded) ble_store_util_delete_peer(&desc->peer_id_addr);
    ble_gap_terminate(desc->conn_handle,BLE_ERR_AUTH_FAIL);
    return false;
  }
  if (admitted==LinkAdmission::New) {
    accumulated=0; discardLine=false;
    lastAuthenticationResult=-1; lastSecurityStartResult=-1;
  }
  return true;
}
// Caller holds SessionGuard; safe for AUTH before CONNECT and for a CONNECT
// snapshot which is already encrypted. Never relax MITM or bonding checks.
void acceptSecurity(const ble_gap_conn_desc* desc,int result) {
  PeerIdentity stored[CONFIG_BT_NIMBLE_MAX_BONDS]; int count=0;
  const bool storeRead=readStoredPeers(stored,count);
  const auto peer=identity(desc->peer_id_addr);
  bool storedPeer=false;
  for (int i=0;i<count;++i) if (stored[i]==peer) storedPeer=true;
  bool accepted=false,known=false,owned=false;
  portENTER_CRITICAL(&mutex);
  if (desc->conn_handle==connection) {
    owned=true; known=pairingPolicy.knownPeer();
    accepted=pairingPolicy.acceptAuthentication(desc->conn_handle,desiredEnabled && !forgetRequested,millis(),
                     !result && secure(desc) && desc->sec_state.bonded && storeRead && storedPeer);
    if (accepted) {
      trustedBonds.retain(stored,count);
      accepted=trustedBonds.add(peer);
    }
    encrypted=accepted; authenticated=accepted; current.passkeyValid=false;
    if (accepted) {
      activePeerIdentity=desc->peer_id_addr;
      current.pairing=false; current.bonded=true; current.error[0]=0;
    }
    refreshReadyLocked();
  }
  portEXIT_CRITICAL(&mutex);
  if (!owned) return;
  if (!accepted) {
    if (!known && desc->sec_state.bonded) ble_store_util_delete_peer(&desc->peer_id_addr);
    error("Pairing failed; try pairing again");
    ble_gap_terminate(desc->conn_handle,BLE_ERR_AUTH_FAIL);
  }
}
class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer*,ble_gap_conn_desc* desc) override {
    SessionGuard guard(true);
    if (!ensureAdmitted(desc)) return;
    // CONNECT is delayed until remote feature discovery. AUTH/CCCD/MTU can
    // already have occurred; preserve their session and reconcile live state.
    if (secure(desc) && desc->sec_state.bonded) acceptSecurity(desc,0);
    else lastSecurityStartResult=ble_gap_security_initiate(desc->conn_handle);
  }
  void onDisconnect(BLEServer*,ble_gap_conn_desc* desc) override {
    SessionGuard guard(true);
    bool owned=false;
    portENTER_CRITICAL(&mutex);
    if (connection==desc->conn_handle) {
      owned=true;
      connection=BLE_HS_CONN_HANDLE_NONE; current.connected=false;
      pairingPolicy.disconnect(desc->conn_handle);
      current.ready=false; current.passkeyValid=false;
      encrypted=authenticated=hidSubscribed=metadataSubscribed=false;
    }
    portEXIT_CRITICAL(&mutex);
    if (owned) { accumulated=0; discardLine=false; }
  }
  void onMtuChanged(BLEServer*,ble_gap_conn_desc* desc,uint16_t value) override {
    SessionGuard guard(true);
    if (!ensureAdmitted(desc)) return;
    portENTER_CRITICAL(&mutex);
    if (desc->conn_handle==connection) { mtu=value; refreshReadyLocked(); }
    portEXIT_CRITICAL(&mutex);
  }
} serverCallbacks;
class SecurityCallbacks : public BLESecurityCallbacks {
  bool onSecurityRequest() override { return desiredEnabled && !forgetRequested; }
  void onPassKeyNotify(uint32_t key) override {
    portENTER_CRITICAL(&mutex);
    current.passkey=key; current.passkeyValid=current.pairing;
    portEXIT_CRITICAL(&mutex);
  }
  bool onConfirmPIN(uint32_t) override { return false; }
  bool onAuthorizationRequest(uint16_t handle,uint16_t,bool) override {
    ble_gap_conn_desc desc;
    return !ble_gap_conn_find(handle,&desc) && secure(&desc);
  }
  void onAuthenticationComplete(ble_gap_conn_desc* desc,int result) override {
    SessionGuard guard(true);
    // Preserve raw event evidence even when policy rejects this peer.
    lastAuthenticationEvent=result;
    lastAuthenticationFlags=(desc->sec_state.encrypted ? 1 : 0) |
                            (desc->sec_state.authenticated ? 2 : 0) |
                            (desc->sec_state.bonded ? 4 : 0);
    if (!ensureAdmitted(desc)) return;
    lastAuthenticationResult=result;
    acceptSecurity(desc,result);
  }

} securityCallbacks;
class NativeOutput : public BLECharacteristicCallbacks {
  const bool feature;
 public:
  explicit NativeOutput(bool value=false):feature(value){}
  void onWrite(BLECharacteristic* characteristic,ble_gap_conn_desc* desc) override {
    if (!secure(desc)) return;
    portENTER_CRITICAL(&mutex);
    const auto s=current;
    const bool allowed=desiredEnabled && authenticated && encrypted && s.connected
        && desc->conn_handle==connection && pairingPolicy.owns(connection);
    portEXIT_CRITICAL(&mutex);
    if (!allowed) return;
    const String value=characteristic->getValue();
    MicroLink::receiveBleReport(6,reinterpret_cast<const uint8_t*>(value.c_str()),
                               value.length(),s.session,feature);
    ++hidReceived;
  }
} outputCallbacks, featureCallbacks(true);
class MetadataInput : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* characteristic,ble_gap_conn_desc* desc) override {
    if (!secure(desc)) return;
    portENTER_CRITICAL(&mutex);
    const auto s=current;
    const bool allowed=desiredEnabled && authenticated && encrypted && s.connected
        && desc->conn_handle==connection && pairingPolicy.owns(connection);
    portEXIT_CRITICAL(&mutex);
    if (!allowed) return;
    const String value=characteristic->getValue();
    for (size_t i=0;i<value.length();++i) {
      const char ch=value[i];
      if (ch==0x18) { accumulated=0; discardLine=false; }
      else if (ch=='\n') {
        if (!discardLine && accumulated) {
          accumulating[accumulated]=0;
          // Deliberately no diagnostic/input/flash commands on wireless GATT.
          if (!strncmp(accumulating,"TITLES ",7) || !strncmp(accumulating,"HOST_CONFIG ",12)) {
            ++metadataReceived;
            callbackLine.session=s.session;
            memcpy(callbackLine.text,accumulating,accumulated+1);
            if (xQueueSend(commands,&callbackLine,0)!=pdTRUE) acknowledge("BRIDGE_ERROR busy\n",s.session);
          } else acknowledge("BRIDGE_ERROR command\n",s.session);
        } else if (discardLine) acknowledge("BRIDGE_ERROR length\n",s.session);
        accumulated=0; discardLine=false;
      } else if (ch=='\r') {
        // CRLF is allowed, embedded NUL is not.
      } else if (!ch || accumulated>=maxLine) {
        discardLine=true; accumulated=0;
      } else if (!discardLine) accumulating[accumulated++]=ch;
    }
  }
} metadataInput;
int gapListener(ble_gap_event* event,void*) {
  if (event->type==BLE_GAP_EVENT_SUBSCRIBE) {
    // Restored CCCDs can precede CONNECT. Admit the same physical link now;
    // the later callback will not erase these notifications or advance session.
    SessionGuard guard(true);
    ble_gap_conn_desc desc;
    if (ble_gap_conn_find(event->subscribe.conn_handle,&desc) || !ensureAdmitted(&desc)) return 0;
    portENTER_CRITICAL(&mutex);
    if (event->subscribe.conn_handle==connection) {
      if (input && event->subscribe.attr_handle==input->getHandle())
        hidSubscribed=event->subscribe.cur_notify;
      if (metadataTx && event->subscribe.attr_handle==metadataTx->getHandle())
        metadataSubscribed=event->subscribe.cur_notify;
      refreshReadyLocked();
    }
    portEXIT_CRITICAL(&mutex);
  }
  return 0;
}
BLECharacteristic* report(BLEService* service,uint8_t id,uint8_t type,uint32_t properties) {
  auto* characteristic=service->createCharacteristic(uint16_t(0x2a4d),properties);
  auto* descriptor=new BLEDescriptor(BLEUUID(uint16_t(0x2908)));
  descriptor->setAccessPermissions(ESP_GATT_PERM_READ);
  uint8_t value[]={id,type}; descriptor->setValue(value,sizeof(value));
  characteristic->addDescriptor(descriptor);
  return characteristic;
}
bool initialize() {
  eccSelfTestPassed=tab5_ble_ecc_self_test();
  if (!eccSelfTestPassed) { error("Bluetooth key calculation self-test failed"); return false; }
  if (!BLEDevice::init(status().deviceName)) { error("C6 Bluetooth initialization failed"); return false; }
  BLESecurity::setAuthenticationMode(true,true,true);
  BLESecurity::setCapability(ESP_IO_CAP_OUT);
  BLESecurity::setInitEncryptionKey(); BLESecurity::setRespEncryptionKey();
  BLESecurity::setPassKey(false);
  BLESecurity::regenPassKeyOnConnect(true);
  BLESecurity::setForceAuthentication(false);
  BLEDevice::setSecurityCallbacks(&securityCallbacks);
  BLEDevice::setMTU(256);
  server=BLEDevice::createServer();
  server->setCallbacks(&serverCallbacks);
  // Worker manages advertising so disabling BLE cannot be undone by a late disconnect callback.
  server->advertiseOnDisconnect(false);
  auto* info=server->createService(uint16_t(0x180a));
  info->createCharacteristic(uint16_t(0x2a29),BLECharacteristic::PROPERTY_READ)->setValue("Tab5 Custom");
  info->createCharacteristic(uint16_t(0x2a24),BLECharacteristic::PROPERTY_READ)->setValue("Tab5 Codex Micro");
  info->createCharacteristic(uint16_t(0x2a25),BLECharacteristic::PROPERTY_READ)->setValue(chipSerial);
  // DIS PnP ID fields are little endian. BLEHIDDevice::pnp currently swaps them.
  const uint8_t pnp[]={0x02,0x3a,0x30,0x60,0x83,0x01,0x01};
  info->createCharacteristic(uint16_t(0x2a50),BLECharacteristic::PROPERTY_READ)->setValue(pnp,sizeof(pnp));
  auto* hid=server->createService(BLEUUID(uint16_t(0x1812)),64);
  hid->createCharacteristic(uint16_t(0x2a4b),BLECharacteristic::PROPERTY_READ)->setValue(reportMap,sizeof(reportMap));
  const uint8_t hidInfo[]={0x11,0x01,0x00,0x02};
  hid->createCharacteristic(uint16_t(0x2a4a),BLECharacteristic::PROPERTY_READ)->setValue(hidInfo,sizeof(hidInfo));
  hid->createCharacteristic(uint16_t(0x2a4c),BLECharacteristic::PROPERTY_WRITE_NR|BLECharacteristic::PROPERTY_WRITE_ENC);
  auto* protocol=hid->createCharacteristic(uint16_t(0x2a4e),BLECharacteristic::PROPERTY_READ|BLECharacteristic::PROPERTY_WRITE_NR);
  const uint8_t mode=1; protocol->setValue(&mode,1);
  const auto readNotify=BLECharacteristic::PROPERTY_READ|BLECharacteristic::PROPERTY_NOTIFY|BLECharacteristic::PROPERTY_READ_ENC;
  const auto readWrite=BLECharacteristic::PROPERTY_READ|BLECharacteristic::PROPERTY_WRITE|BLECharacteristic::PROPERTY_WRITE_NR|BLECharacteristic::PROPERTY_WRITE_ENC;
  auto* keyboard=report(hid,1,1,readNotify); uint8_t emptyKeys[8]={}; keyboard->setValue(emptyKeys,sizeof(emptyKeys));
  report(hid,1,2,readWrite);
  input=report(hid,6,1,readNotify); uint8_t emptyReport[63]={}; input->setValue(emptyReport,sizeof(emptyReport));
  report(hid,6,2,readWrite)->setCallbacks(&outputCallbacks);
  report(hid,6,3,BLECharacteristic::PROPERTY_READ|BLECharacteristic::PROPERTY_WRITE|BLECharacteristic::PROPERTY_WRITE_ENC)->setCallbacks(&featureCallbacks);
  auto* bridge=server->createService(serviceUuid);
  bridge->createCharacteristic(rxUuid,BLECharacteristic::PROPERTY_WRITE|BLECharacteristic::PROPERTY_WRITE_ENC|BLECharacteristic::PROPERTY_WRITE_AUTHEN)->setCallbacks(&metadataInput);
  metadataTx=bridge->createCharacteristic(txUuid,readNotify|BLECharacteristic::PROPERTY_READ_AUTHEN);
  String identity=String("{\"v\":1,\"serial\":\"")+chipSerial+"\",\"maxLine\":6144,\"firmware\":\"" TAB5_FIRMWARE_ID "\"}";
  bridge->createCharacteristic(identityUuid,BLECharacteristic::PROPERTY_READ|BLECharacteristic::PROPERTY_READ_ENC|BLECharacteristic::PROPERTY_READ_AUTHEN)->setValue(identity);
  info->start(); hid->start(); bridge->start(); server->start();
  // Bonded hosts may cache an older firmware's HID table. Queue the standard
  // indication before advertising so restored peers rediscover report handles.
  ble_svc_gatt_changed(1,0xffff);
  BLEDevice::setCustomGapHandler(gapListener);
  auto* advertising=BLEDevice::getAdvertising();
  BLEAdvertisementData adv;
  adv.setFlags(0x06); adv.setAppearance(0x03c1);
  adv.setCompleteServices(BLEUUID(uint16_t(0x1812)));
  adv.setName(status().deviceName);
  advertising->setAdvertisementData(adv);
  BLEAdvertisementData response;
  response.setCompleteServices(BLEUUID(serviceUuid));
  uint8_t manufacturer[8]={0x3a,0x30};
  for (int i=0;i<6;++i) {
    char byte[3]={chipSerial[5+i*2],chipSerial[6+i*2],0};
    manufacturer[i+2]=strtoul(byte,nullptr,16);
  }
  response.setManufacturerData(String(reinterpret_cast<const char*>(manufacturer),sizeof(manufacturer)));
  advertising->setScanResponseData(response);
  advertising->setMinInterval(160); advertising->setMaxInterval(240);
  PeerIdentity stored[CONFIG_BT_NIMBLE_MAX_BONDS]; int count=0;
  if (!readStoredPeers(stored,count)) { error("Bluetooth bond store unavailable"); return false; }
  portENTER_CRITICAL(&mutex);
  trustedBonds.clear();
  for (int i=0;i<count;++i) trustedBonds.add(stored[i]);
  portEXIT_CRITICAL(&mutex);
  refreshBonded();
  portENTER_CRITICAL(&mutex); current.available=true; current.error[0]=0; portEXIT_CRITICAL(&mutex);
  return true;
}
bool notify(uint16_t handle,const uint8_t* data,size_t size,uint32_t session) {
  SessionGuard guard(false);
  if (!guard.locked) return false;
  // NimBLE invokes GAP lifecycle callbacks without holding its host lock.
  // Serializing those callbacks with this short, non-waiting submission
  // prevents a reused handle from receiving an earlier session's packet.
  uint16_t conn;
  portENTER_CRITICAL(&mutex);
  const bool allowed=desiredEnabled && current.connected && encrypted && authenticated
      && current.session==session && pairingPolicy.owns(connection);
  conn=connection;
  portEXIT_CRITICAL(&mutex);
  if (!allowed) return false;
  os_mbuf* packet=ble_hs_mbuf_from_flat(data,size);
  if (!packet) return false;
  // NimBLE consumes packet even when submission fails.
  const bool sent=ble_gatts_notify_custom(conn,handle,packet)==0;
  if (!sent) ++notifyFailed;
  return sent;
}
void worker(void*) {
  const bool storageReady=preferences.begin("micro-ble",false);
  bool saved=storageReady ? preferences.getBool("enabled",true) : true;
  desiredEnabled=saved;
  portENTER_CRITICAL(&mutex); current.enabled=saved; portEXIT_CRITICAL(&mutex);
  bool initialized=false, attempted=false, advertising=false;
  Reply reply{}; bool haveReply=false; size_t replyOffset=0;
  for (;;) {
    const bool enabled=desiredEnabled.load();
    if (enabled!=saved) {
      if (!storageReady || preferences.putBool("enabled",enabled)==1) saved=enabled;
    }
    portENTER_CRITICAL(&mutex);
    current.enabled=enabled;
    if (!enabled) pairingPolicy.closeWindow();
    current.pairing=pairingPolicy.windowActive(enabled,millis());
    if (!current.pairing) current.passkeyValid=false;
    refreshReadyLocked();
    portEXIT_CRITICAL(&mutex);
    if (enabled && !attempted) {
      attempted=true; initStarted=millis(); stackStarted=true;
      initialized=initialize();
    }
    if (initialized) {
      auto s=status();
      if (forgetRequested.load()) {
        SessionGuard guard(true);
        uint16_t oldConnection;
        portENTER_CRITICAL(&mutex);
        oldConnection=connection;
        pairingPolicy.disconnect(oldConnection);
        trustedBonds.clear();
        encrypted=authenticated=false; current.ready=false;
        portEXIT_CRITICAL(&mutex);
        if (s.connected) ble_gap_terminate(oldConnection,BLE_ERR_REM_USER_CONN_TERM);
        ble_store_clear(); refreshBonded();
        // Keep admission closed while waiting for SessionGuard as well as
        // while deleting NVS, including callbacks with a cached old identity.
        forgetRequested=false;
        pairRequested=true;
      }
      if (pairRequested.exchange(false) && enabled) {
        portENTER_CRITICAL(&mutex);
        pairingPolicy.openWindow(millis());
        current.pairing=true; current.passkeyValid=false; current.error[0]=0;
        portEXIT_CRITICAL(&mutex);
      }
      s=status();
      uint16_t activeConnection; bool expired;
      portENTER_CRITICAL(&mutex);
      activeConnection=connection;
      expired=pairingPolicy.shouldDisconnect(enabled,millis());
      portEXIT_CRITICAL(&mutex);
      if ((!enabled || expired || disconnectRequested.exchange(false)) && s.connected)
        ble_gap_terminate(activeConnection,BLE_ERR_REM_USER_CONN_TERM);
      const bool shouldAdvertise=enabled && !s.connected && (s.pairing || s.bonded);
      if (shouldAdvertise && !ble_gap_adv_active()) advertising=BLEDevice::getAdvertising()->start();
      if (!shouldAdvertise && ble_gap_adv_active()) { BLEDevice::getAdvertising()->stop(); advertising=false; }
      (void)advertising;
      if (!haveReply && xQueueReceive(replies,&reply,0)==pdTRUE) { haveReply=true; replyOffset=0; }
      if (haveReply) {
        bool subscribed; uint16_t packetMtu;
        portENTER_CRITICAL(&mutex); subscribed=metadataSubscribed; packetMtu=mtu; portEXIT_CRITICAL(&mutex);
        if (!s.connected || reply.session!=s.session) haveReply=false;
        else if (subscribed) {
          const size_t remaining=strlen(reply.text)-replyOffset;
          const size_t count=std::min<size_t>(remaining,packetMtu>3?packetMtu-3:20);
          if (count && notify(metadataTx->getHandle(),reinterpret_cast<const uint8_t*>(reply.text)+replyOffset,count,reply.session)) {
            replyOffset+=count;
            if (replyOffset==strlen(reply.text)) haveReply=false;
          }
        }
      }
    }
    vTaskDelay(pdMS_TO_TICKS(10));
  }
}
}  // namespace
void begin(const char* serial) {
  strlcpy(chipSerial,serial,sizeof(chipSerial));
  snprintf(current.deviceName,sizeof(current.deviceName),"Tab5 Micro %.4s",serial+13);
  commands=xQueueCreate(2,sizeof(Command)); replies=xQueueCreate(8,sizeof(Reply));
  txSessionMutex=xSemaphoreCreateMutex();
  if (!commands || !replies || !txSessionMutex || xTaskCreate(worker,"micro-ble",12288,nullptr,1,nullptr)!=pdPASS)
    error("Bluetooth worker allocation failed");
}
void poll() {
  // A missing/mismatched C6 must never freeze touch or USB diagnostics.
  if (stackStarted && !status().available && initStarted && millis()-initStarted>30000)
    error("C6 did not respond; USB remains available");
}
MicroLink::BleStatus status() {
  portENTER_CRITICAL(&mutex);
  auto copy=current;
  copy.authenticated=desiredEnabled && current.connected && encrypted && authenticated
      && pairingPolicy.owns(connection) && !forgetRequested;
  portEXIT_CRITICAL(&mutex);
  return copy;
}
void setEnabled(bool value) { desiredEnabled=value; }
void startPairing() { desiredEnabled=true; pairRequested=true; }
void clearBonds() {
  // This can run from a touch callback: revoke immediately without waiting
  // for a BLE host or NVS operation. The worker serializes actual deletion.
  forgetRequested=true;
  portENTER_CRITICAL(&mutex);
  trustedBonds.clear();
  pairingPolicy.closeWindow();
  current.pairing=false; current.passkeyValid=false;
  pairingPolicy.disconnect(connection);
  encrypted=authenticated=false; current.ready=false;
  portEXIT_CRITICAL(&mutex);
}
void disconnect() { disconnectRequested=true; }
bool nextCommand(char* destination,size_t capacity,uint32_t& session) {
  if (!commands || capacity<maxLine+1) return false;
  if (xQueueReceive(commands,&mainLine,0)!=pdTRUE) return false;
  portENTER_CRITICAL(&mutex);
  const bool allowed=desiredEnabled && current.connected && encrypted && authenticated
      && mainLine.session==current.session && pairingPolicy.owns(connection);
  portEXIT_CRITICAL(&mutex);
  if (!allowed) return false;
  memcpy(destination,mainLine.text,strlen(mainLine.text)+1); session=mainLine.session; return true;
}
bool acknowledge(const char* line,uint32_t session) {
  if (!replies || strlen(line)>=sizeof(Reply::text)) return false;
  Reply reply{}; reply.session=session; strlcpy(reply.text,line,sizeof(reply.text));
  return xQueueSend(replies,&reply,0)==pdTRUE;
}
bool submit(uint8_t reportId,const uint8_t* data,size_t length,uint32_t expectedSession) {
  const auto s=status();
  if (reportId!=6 || length!=63 || !s.ready || s.session!=expectedSession || !input) return false;
  const bool sent=notify(input->getHandle(),data,length,expectedSession);
  if (sent) ++hidSubmitted;
  return sent;
}
void diagnostics(Print& output) {
  JsonDocument document;
  const auto s=status();
  document["available"]=s.available; document["enabled"]=s.enabled;
  document["connected"]=s.connected; document["ready"]=s.ready;
  document["pairing"]=s.pairing; document["bonded"]=s.bonded;
  // Physical USB diagnostics may read the same transient PIN shown on screen.
  document["passkey_valid"]=s.passkeyValid;
  if (s.passkeyValid) document["passkey"]=s.passkey;
  document["session"]=s.session; document["name"]=s.deviceName;
  document["error"]=s.error;
  uint16_t currentMtu,handle; bool hidNotify,metadataNotify,enc,auth;
  portENTER_CRITICAL(&mutex);
  currentMtu=mtu; handle=connection; hidNotify=hidSubscribed;
  metadataNotify=metadataSubscribed; enc=encrypted; auth=authenticated;
  portEXIT_CRITICAL(&mutex);
  document["mtu"]=currentMtu; document["connection_handle"]=handle;
  document["hid_notify"]=hidNotify; document["metadata_notify"]=metadataNotify;
  document["encrypted"]=enc; document["authenticated"]=auth;
  ble_gap_conn_desc live{};
  if (handle!=BLE_HS_CONN_HANDLE_NONE && ble_gap_conn_find(handle,&live)==0) {
    document["link_encrypted"]=bool(live.sec_state.encrypted);
    document["link_authenticated"]=bool(live.sec_state.authenticated);
    document["link_bonded"]=bool(live.sec_state.bonded);
  }
  document["hid_submitted"]=hidSubmitted.load(); document["hid_received"]=hidReceived.load();
  document["metadata_received"]=metadataReceived.load(); document["notify_failed"]=notifyFailed.load();
  document["auth_result"]=lastAuthenticationResult.load();
  document["last_auth_event"]=lastAuthenticationEvent.load();
  document["last_auth_flags"]=lastAuthenticationFlags.load();
  document["security_start_result"]=lastSecurityStartResult.load();
  document["ecc_self_test"]=eccSelfTestPassed.load();
  uint32_t major=0,minor=0,patch=0;
  hostedGetSlaveVersion(&major,&minor,&patch);
  char version[32]; snprintf(version,sizeof(version),"%lu.%lu.%lu",(unsigned long)major,(unsigned long)minor,(unsigned long)patch);
  document["c6_firmware"]=version;
  serializeJson(document,output); output.println();
}
bool allowRepair(uint16_t handle) {
  portENTER_CRITICAL(&mutex);
  const bool allowed=pairingPolicy.allowRepair(handle,desiredEnabled,millis());
  if (allowed) {
    trustedBonds.remove(identity(activePeerIdentity));
    encrypted=authenticated=false; current.ready=false;
  }
  portEXIT_CRITICAL(&mutex);
  return allowed;
}
}  // namespace MicroBle

extern "C" bool tab5_ble_allow_repair(uint16_t handle) { return MicroBle::allowRepair(handle); }
