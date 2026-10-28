#pragma once
#include "micro_ui.h"
#include <USBCDC.h>
extern USBCDC DebugSerial;
namespace MicroLink {
struct BleStatus {
  bool available=false, enabled=false, connected=false, pairing=false, ready=false, bonded=false;
  bool authenticated=false;
  bool passkeyValid=false;
  uint32_t session=0, passkey=0;
  char deviceName[40]={}, error[96]={};
};
struct BleEndpoint {
  // Return an internally consistent snapshot under the backend's lock.
  BleStatus (*status)()=nullptr;
  // Nonblocking, copies all 63 bytes before returning. True means submitted,
  // never a desktop execution acknowledgement. reportId is not in the payload.
  // Backend must reject a different connection session, even if reconnection
  // happened between status() and this call.
  bool (*submit)(uint8_t reportId,const uint8_t* data,size_t length,uint32_t session)=nullptr;
  void (*disconnect)()=nullptr;
};
void attachBle(const BleEndpoint& endpoint);
// Mirror an explicit USB_PAUSE electrical detach in the test's route state.
// TinyUSB retains its configuration flag after software disconnect.
void setUsbDiagnosticPaused(bool paused);
// BLE task-safe: validates and copies a report into the bounded RX queue.
// Every physical BLE connection gets a new backend session number.
void receiveBleReport(uint8_t reportId,const uint8_t* data,size_t length,
                      uint32_t session,bool feature=false);
void begin();
void poll();
// Changes when the selected physical link/session changes. Main must discard
// the old captured gesture and wait for finger lift before sending new input.
uint32_t transportGeneration();
bool transportSwitching();
void send(const MicroUI::Event &event);
// Explicit diagnostic command only: real BLE submission follows the normal
// queue. Fails while USB has priority, capture is active, or BLE is not ready.
bool sendBleDiagnostic(const MicroUI::Event& event,uint32_t expectedSession);
// Abort only the original live BLE session. Keeps unrelated replies and never
// sends cleanup into a reconnected peer or another physical transport.
void cancelBleDiagnostic(uint32_t expectedSession);
// Capture/another diagnostic may start only after queued native edges and
// safety cleanup have drained. Replies do not make this false.
bool bleDiagnosticIdle();
// Diagnostics use the actual serializer while suppressing desktop actions.
void captureInputs(bool enabled);
bool capturingInputs();
// Call before the FRAME header and after END_FRAME (also on abort). Keeps
// native input disabled and defers text until the binary CDC frame is over.
void setDiagnosticFrameActive(bool active);
// The serial command dispatcher waits until deferred lines finish before
// printing a new ACK or FRAME header, so partial CDC writes cannot interleave.
bool diagnosticTextPending();
bool previewLights(const char *payload);
void clearLightingPreview();
const MicroUI::State &state();
void diagnostics();
void recordFrame(uint32_t microseconds);
void recordInputInterval(uint32_t microseconds);
void resetMetrics();
// Apply an independently sourced, complete six-slot metadata snapshot.
bool applyTitles(const char *payload,bool usbAck=true);
bool applyHostConfig(const char *payload,bool usbAck=true);
// Synthetic state stays in the explicit, capture-only preview model.
bool previewHostState(const char *payload);
}
