#include <Arduino.h>
#include <algorithm>
#include <USB.h>
#include <FS.h>
#include <M5Unified.h>
#include "micro_link.h"
#include "micro_ble.h"
#include "micro_ble_diagnostics.h"
#include "deferred_frame_capture.h"
#include "micro_ui.h"
#include "micro_games.h"
#include "bootloader.h"
#include "device_settings.h"
#include "micro_sound.h"
#include "micro_orientation.h"
#include "ui_locale.h"
#include "render_service.h"
#include <esp_system.h>
#include <esp_heap_caps.h>
#include <esp_mac.h>
#include <ArduinoJson.h>
#include <tusb.h>

USBCDC DebugSerial;

namespace {
String command;
bool uiReady=false;
uint32_t rendered=0;
uint32_t sampledAt=0;
uint8_t *screenCopy=nullptr;
size_t screenOffset=0, screenSize=0;
char screenHeader[64];
size_t screenHeaderOffset=0, screenHeaderSize=0;
bool screenSlow=false;
uint32_t screenPixelStart=0;
uint32_t screenProgress=0;
bool inputTest=false, testPressed=false;
bool inputHandoff=false;
bool effectsBeforeTest=false;
int16_t testX=0, testY=0;
uint32_t inputTestAt=0;
DeviceSettings::Values appliedSettings;
MicroUI::Theme previousTheme=MicroUI::Theme::Light;
bool previousPlay=false, previousSettings=false;
uint32_t volumePreviewAt=0;
uint32_t usbPauseAt=0, usbResumeAt=0;
uint32_t inputGeneration=0;
bool inputGenerationKnown=false;
MicroBleDiagnostics::Schedule bleTest;
MicroBleDiagnostics::Plan bleTestPlan=MicroBleDiagnostics::Plan::FastPair;
char bleTestTarget[37]={};
MicroBleDiagnostics::Context bleTestLastContext{}, bleTestAbortContext{};
bool bleTestHaveLastContext=false, bleTestHaveAbortContext=false;
uint32_t bleTestLastContextAt=0, bleTestAbortContextAt=0;
DeferredFrameCapture::Capture frameCapture;
void serviceInputs();

DeferredFrameCapture::Context frameCaptureContext() {
  DeferredFrameCapture::Context context;
  const auto bluetooth=MicroBle::status();
  const auto& state=MicroLink::state();
  context.session=bluetooth.session; context.connected=bluetooth.connected;
  context.authenticated=bluetooth.authenticated; context.bonded=bluetooth.bonded;
  context.generation=MicroLink::transportGeneration();
  context.activeBluetooth=state.activeTransport==MicroUI::Transport::Bluetooth && state.connected;
  context.switching=MicroLink::transportSwitching();
  context.blocked=!uiReady || inputTest || screenCopy || MicroLink::capturingInputs()
      || M5.Touch.getCount()!=0 || MicroUI::playing() || MicroUI::settingsOpen()
      || state.lightingPreview || MicroUI::effectsAreFrozen()
      || MicroUI::bufferWidth()!=int(DeferredFrameCapture::Capture::width)
      || MicroUI::bufferHeight()!=int(DeferredFrameCapture::Capture::height) || !MicroUI::framebuffer();
  return context;
}
void* allocateFrameCapture(size_t bytes) {
  return heap_caps_malloc(bytes,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
}
DeferredFrameCapture::Context serviceFrameCaptureInputs() {
  serviceInputs();
  return frameCaptureContext();
}
void releaseFrameCapture(void* pixels) {
  serviceInputs(); free(pixels); serviceInputs();
}
uint32_t frameCaptureMicros() { return micros(); }
uint32_t frameCaptureMillis() { return millis(); }
const DeferredFrameCapture::Hooks frameCaptureHooks{
    serviceFrameCaptureInputs,frameCaptureMicros,frameCaptureMillis};
void serviceFrameCapture(const DeferredFrameCapture::Context& beforeRender) {
  if (!frameCapture.active()) return;
  auto context=frameCaptureContext();
  const auto& state=MicroUI::renderedState();
  context.renderedBluetooth=state.activeTransport==MicroUI::Transport::Bluetooth && state.connected;
  // RenderService may process transport/input changes while drawing. Only
  // accept a framebuffer bracketed by one stable authenticated BLE context.
  context.coherent=beforeRender.session==context.session
      && beforeRender.generation==context.generation && beforeRender.connected
      && beforeRender.authenticated && beforeRender.bonded && beforeRender.activeBluetooth
      && !beforeRender.switching && !beforeRender.blocked;
  // Copy only public lighting evidence by value before cooperative input/RX;
  // those callbacks can mutate the live model but never the retained canvas.
  DeferredFrameCapture::Evidence evidence;
  evidence.session=context.session; evidence.generation=context.generation;
  evidence.bluetooth=context.renderedBluetooth; evidence.nativeSessionSeen=state.nativeSessionSeen;
  evidence.preview=state.lightingPreview; evidence.frozen=MicroUI::effectsAreFrozen();
  evidence.neonFrames=MicroUI::renderProfile().neonFrames;
  for (size_t i=0;i<6;++i) {
    const auto& slot=state.slots[i];
    evidence.slots[i]={slot.color,slot.brightness,slot.effect,slot.speed,0};
  }
  evidence.hostLightsKnown=state.hostLights.known;
  const auto& keys=state.hostLights.keys; const auto& ambient=state.hostLights.ambient;
  evidence.keys={keys.color,keys.brightness,keys.effect,keys.speed,keys.magic};
  evidence.ambient={ambient.color,ambient.brightness,ambient.effect,ambient.speed,ambient.magic};
  frameCapture.capture(millis(),context,reinterpret_cast<const uint8_t*>(MicroUI::framebuffer()),
                       evidence,frameCaptureHooks);
}
void writeCapturedLight(JsonObject output,const DeferredFrameCapture::Light& light,bool magic=false) {
  output["c"]=light.color; output["b"]=light.brightness;
  output["e"]=light.effect; output["s"]=light.speed;
  if (magic) output["m"]=light.magic;
}
void frameCaptureStatus() {
  static const char* states[]={"idle","armed","complete","aborted","expired"};
  static const char* reasons[]={"none","invalid-request","allocation-failed","session-changed",
      "disconnected","unsafe-frame","transport-changed","deadline","ttl"};
  frameCapture.poll(millis(),frameCaptureContext());
  JsonDocument status;
  status["state"]=states[uint8_t(frameCapture.state())];
  status["reason"]=reasons[uint8_t(frameCapture.reason())];
  status["session"]=frameCapture.session(); status["armed_at_ms"]=frameCapture.armedAt();
  status["arm_us"]=frameCapture.armUs(); status["arm_max_step_us"]=frameCapture.armMaxStepUs();
  status["captured"]=frameCapture.captured(); status["stored"]=frameCapture.stored();
  status["width"]=DeferredFrameCapture::Capture::width;
  status["height"]=DeferredFrameCapture::Capture::height;
  status["bytes_per_frame"]=DeferredFrameCapture::Capture::bytes;
  auto frames=status["frames"].to<JsonArray>();
  for (size_t i=0;i<DeferredFrameCapture::Capture::count;++i) {
    const auto& frame=frameCapture.frame(i);
    auto output=frames.add<JsonObject>();
    output["index"]=i; output["delay_ms"]=frame.delay;
    output["captured"]=frame.captured; output["available"]=frame.captured && frame.pixels;
    output["read"]=frame.read;
    output["copy_us"]=frame.copyUs; output["max_chunk_us"]=frame.maxChunkUs;
    output["chunks"]=frame.chunks;
    if (!frame.captured) continue;
    const auto& evidence=frame.evidence;
    auto value=output["evidence"].to<JsonObject>();
    value["transport"]=evidence.bluetooth ? "bluetooth" : "none";
    value["session"]=evidence.session; value["generation"]=evidence.generation;
    value["captured_at_ms"]=evidence.capturedAt; value["elapsed_ms"]=evidence.elapsed;
    value["nativeSessionSeen"]=evidence.nativeSessionSeen;
    value["preview"]=evidence.preview; value["frozen"]=evidence.frozen;
    value["neon_frames"]=evidence.neonFrames;
    auto slots=value["slots"].to<JsonArray>();
    for (const auto& slot:evidence.slots) writeCapturedLight(slots.add<JsonObject>(),slot);
    auto lights=value["hostLights"].to<JsonObject>();
    lights["known"]=evidence.hostLightsKnown;
    writeCapturedLight(lights["keys"].to<JsonObject>(),evidence.keys,true);
    writeCapturedLight(lights["ambient"].to<JsonObject>(),evidence.ambient,true);
  }
  DebugSerial.print("FRAME_CAPTURE_STATUS "); serializeJson(status,DebugSerial); DebugSerial.println();
}

MicroBleDiagnostics::Context bleTestContext(MicroBleDiagnostics::Plan plan,const char* target) {
  MicroBleDiagnostics::Context context;
  const auto bluetooth=MicroBle::status();
  const auto& state=MicroLink::state();
  context.session=bluetooth.session; context.connected=bluetooth.connected;
  context.authenticated=bluetooth.authenticated; context.bonded=bluetooth.bonded;
  context.ready=bluetooth.ready;
  context.activeBle=state.activeTransport==MicroUI::Transport::Bluetooth && state.connected;
  context.transportGeneration=MicroLink::transportGeneration();
  context.switching=MicroLink::transportSwitching();
  context.suppressed=inputTest || screenCopy || MicroLink::capturingInputs()
      || M5.Touch.getCount()!=0 || MicroUI::playing() || state.lightingPreview;
  context.mappingKnown=state.hostConfig.fresh && state.hostChat.fresh;
  context.mappingSafe=context.mappingKnown && !strcmp(state.hostChat.thread_id,target);
  context.fastKey=6;
  if (plan==MicroBleDiagnostics::Plan::FastPair)
    context.mappingSafe &= state.hostConfig.actions[0]==HostFeedback::Action::Fast;
  else if (plan==MicroBleDiagnostics::Plan::DialPair)
    context.mappingSafe &= state.hostConfig.encoder==HostFeedback::Encoder::Composer;
  else context.mappingSafe &= state.hostConfig.joystickUpPlan;
  return context;
}
void cleanupBleTest() {
  if (bleTest.takeCleanupRequired()) MicroLink::cancelBleDiagnostic(bleTest.session());
}
void rememberBleTestAbort() {
  if (bleTest.state()==MicroBleDiagnostics::State::Aborted
      && bleTestHaveLastContext && !bleTestHaveAbortContext) {
    bleTestAbortContext=bleTestLastContext; bleTestAbortContextAt=bleTestLastContextAt;
    bleTestHaveAbortContext=true;
  }
}
void cancelBleTest() {
  if (bleTest.active()) {
    bleTestLastContext=bleTestContext(bleTestPlan,bleTestTarget);
    bleTestLastContextAt=millis(); bleTestHaveLastContext=true;
  }
  bleTest.cancel(); rememberBleTestAbort(); cleanupBleTest();
}
void serviceBleTest() {
  if (!bleTest.active()) return;
  const auto context=bleTestContext(bleTestPlan,bleTestTarget);
  bleTestLastContext=context; bleTestLastContextAt=millis(); bleTestHaveLastContext=true;
  MicroBleDiagnostics::Step step;
  if (bleTest.next(bleTestLastContextAt,context,step)) {
    MicroUI::Event event;
    switch (step.kind) {
      case MicroBleDiagnostics::Kind::FastKey: event.key="ACT06"; event.action=step.action; break;
      case MicroBleDiagnostics::Kind::DialClockwise: event.key="ENC_CW"; event.action=2; break;
      case MicroBleDiagnostics::Kind::DialCounterclockwise: event.key="ENC_CC"; event.action=2; break;
      case MicroBleDiagnostics::Kind::StickUp: event.joystick=true; event.angle=.75f; event.distance=.8f; break;
      case MicroBleDiagnostics::Kind::StickNeutral: event.joystick=true; event.angle=0; event.distance=0; break;
    }
    bleTest.completeStep(MicroLink::sendBleDiagnostic(event,step.session));
  }
  rememberBleTestAbort();
  cleanupBleTest();
}
void writeBleTestContext(JsonObject output,const MicroBleDiagnostics::Context& context,uint32_t at) {
  output["sampled_at_ms"]=at;
  output["connected"]=context.connected; output["authenticated"]=context.authenticated;
  output["bonded"]=context.bonded; output["ready"]=context.ready;
  output["activeBle"]=context.activeBle; output["switching"]=context.switching;
  output["suppressed"]=context.suppressed; output["mappingKnown"]=context.mappingKnown;
  output["mappingSafe"]=context.mappingSafe; output["session"]=context.session;
  output["generation"]=context.transportGeneration;
}
void bleTestStatus() {
  static const char* states[]={"idle","armed","running","complete","aborted"};
  static const char* reasons[]={"none","invalid-request","session-changed","disconnected","suppressed",
      "mapping-changed","transport-changed","start-timeout","deadline","submit-failed","cancelled"};
  JsonDocument status;
  status["state"]=states[uint8_t(bleTest.state())]; status["reason"]=reasons[uint8_t(bleTest.reason())];
  status["plan"]=bleTestPlan==MicroBleDiagnostics::Plan::FastPair ? "fast"
      : bleTestPlan==MicroBleDiagnostics::Plan::DialPair ? "dial" : "plan";
  status["session"]=bleTest.session(); status["queued_steps"]=bleTest.acceptedSteps();
  status["execution"]="unconfirmed; inspect desktop evidence";
  if (bleTestHaveLastContext)
    writeBleTestContext(status["last_context"].to<JsonObject>(),bleTestLastContext,bleTestLastContextAt);
  else status["last_context"]=nullptr;
  if (bleTestHaveAbortContext)
    writeBleTestContext(status["abort_context"].to<JsonObject>(),bleTestAbortContext,bleTestAbortContextAt);
  else status["abort_context"]=nullptr;
  DebugSerial.print("BLE_TEST_STATUS "); serializeJson(status,DebugSerial); DebugSerial.println();
}

void applyOrientation() {
  M5.Display.setRotation(MicroOrientation::rotation());
  MicroUI::invalidateDisplay();
  rendered=millis()-16;
}

void sendInput(const MicroUI::Event& event) {
  MicroSound::event(event,MicroLink::capturingInputs());
  if (event.key && !strncmp(event.key,"ble_",4)) {
    if (event.action==1 && !MicroLink::capturingInputs()) {
      if (!strcmp(event.key,"ble_toggle")) MicroBle::setEnabled(!MicroBle::status().enabled);
      else if (!strcmp(event.key,"ble_pair")) MicroBle::startPairing();
      else if (!strcmp(event.key,"ble_forget")) MicroBle::clearBonds();
    }
    return;
  }
  MicroLink::send(event);
}

void serviceWirelessMetadata() {
  static char line[6145]; uint32_t session;
  if (!MicroBle::nextCommand(line,sizeof(line),session)) return;
  const bool titles=!strncmp(line,"TITLES ",7);
  const char* payload=line+(titles ? 7 : 12);
  const bool ok=titles ? MicroLink::applyTitles(payload,false)
                       : MicroLink::applyHostConfig(payload,false);
  JsonDocument document;
  const bool hasSequence=!deserializeJson(document,payload) && document["seq"].is<uint32_t>();
  char reply[80];
  if (ok && hasSequence)
    snprintf(reply,sizeof(reply),"%s_OK %lu\n",titles ? "TITLES" : "HOST_CONFIG",
             static_cast<unsigned long>(document["seq"].as<uint32_t>()));
  else snprintf(reply,sizeof(reply),"%s_ERROR\n",titles ? "TITLES" : "HOST_CONFIG");
  MicroBle::acknowledge(reply,session);
}

void syncLocalSettings(bool feedback=true) {
  const DeviceSettings::Values next{MicroUI::soundEnabled(),MicroUI::volume(),MicroUI::brightness(),
                                   UiLocale::english()};
  const bool audioChanged=next.sound!=appliedSettings.sound || next.volume!=appliedSettings.volume;
  const bool languageChanged=next.english!=appliedSettings.english;
  if (audioChanged) {
    MicroSound::configure(next.sound,next.volume);
    if (feedback && millis()-volumePreviewAt>=120) {
      volumePreviewAt=millis(); MicroSound::localPress(MicroLink::capturingInputs());
    }
  }
  if (next.brightness!=appliedSettings.brightness)
    M5.Display.setBrightness(DeviceSettings::backlight(next.brightness));
  DeviceSettings::update(next);
  appliedSettings=next;
  const bool changed=languageChanged || previousTheme!=MicroUI::theme() || previousPlay!=MicroUI::playing()
      || previousSettings!=MicroUI::settingsOpen();
  if (changed && feedback) MicroSound::localPress(MicroLink::capturingInputs());
  previousTheme=MicroUI::theme(); previousPlay=MicroUI::playing();
  previousSettings=MicroUI::settingsOpen();
}

void drainInputs() {
  MicroUI::Event event;
  while (MicroUI::pollEvent(event)) sendInput(event);
}

// Safe cooperative work during rendering: no drawing, persistence, orientation
// changes, or command dispatch. The UI renders an immutable state snapshot.
void serviceInputs() {
  uint32_t now=micros();
  if (sampledAt) MicroLink::recordInputInterval(now-sampledAt);
  sampledAt=now;
  M5.update();
  MicroLink::poll();
  const uint32_t generation=MicroLink::transportGeneration();
  if (uiReady && inputGenerationKnown && generation!=inputGeneration) {
    // The old link owns its release/neutral cleanup. Do not migrate the
    // remainder of a physical gesture into the newly selected connection.
    MicroUI::touch(0,0,false);
    MicroUI::Event discarded;
    while (MicroUI::pollEvent(discarded)) {}
    testPressed=false; inputHandoff=true;
  }
  inputGeneration=generation; inputGenerationKnown=true;
  serviceBleTest();
  auto p=M5.Touch.getDetail();
  if (inputHandoff && !p.isPressed()) inputHandoff=false;
  if (uiReady) {
    sendInput(inputTest ? MicroUI::touch(testX,testY,testPressed)
                    : inputHandoff ? MicroUI::touch(0,0,false)
                                   : MicroUI::touch(p.x,p.y,p.isPressed()));
    drainInputs();
  }
}

void endInputTest() {
  if (!inputTest && !MicroLink::capturingInputs()) return;
  sendInput(MicroUI::touch(0,0,false)); drainInputs();
  testPressed=false; inputTest=false;
  // A finger already resting on the screen during diagnostics must lift
  // before normal desktop control resumes.
  inputHandoff=true;
  MicroLink::clearLightingPreview();
  MicroUI::clearTitleScrollTest();
  MicroUI::setEffectsFrozen(effectsBeforeTest);
  MicroLink::captureInputs(false);
}

void finishScreen() {
  free(screenCopy); screenCopy=nullptr; screenSlow=false;
  // Cancel touches while diagnostic output still owns the HID capture gate.
  if (!inputTest) {
    sendInput(MicroUI::touch(0,0,false)); drainInputs();
    inputHandoff=true;
  }
  MicroLink::setDiagnosticFrameActive(false);
}
void streamScreen() {
  if (!screenCopy) return;
  if (millis()-screenProgress>30000) {
    finishScreen();
    return;
  }
  int available=DebugSerial.availableForWrite();
  if (available<=0) return;
  if (screenHeaderOffset<screenHeaderSize) {
    const size_t n=std::min<size_t>(available,screenHeaderSize-screenHeaderOffset);
    const size_t sent=DebugSerial.write(reinterpret_cast<const uint8_t*>(screenHeader)+screenHeaderOffset,n);
    screenHeaderOffset+=sent;
    if (screenHeaderOffset==screenHeaderSize) screenPixelStart=millis();
    if (sent) screenProgress=millis();
    return;
  }
  static constexpr char trailer[]="\nEND_FRAME\n";
  const bool pixels=screenOffset<screenSize;
  const size_t remaining=pixels ? screenSize-screenOffset
      : sizeof(trailer)-1-(screenOffset-screenSize);
  size_t n=std::min<size_t>(1024,std::min<size_t>(available,remaining));
  // Diagnostic-only pacing creates a real in-flight frame at the 60s capture
  // watchdog. Continuous host reads avoid macOS serial-buffer overflow.
  if (screenSlow && pixels) {
    const size_t allowed=std::min<size_t>(screenSize,uint64_t(millis()-screenPixelStart)*28);
    if (allowed<=screenOffset) return;
    n=std::min(n,allowed-screenOffset);
  }
  const uint8_t* bytes=pixels ? screenCopy+screenOffset
      : reinterpret_cast<const uint8_t*>(trailer)+screenOffset-screenSize;
  const size_t sent=DebugSerial.write(bytes,n);
  screenOffset+=sent;
  if (sent) screenProgress=millis();
  if (screenOffset==screenSize+sizeof(trailer)-1) finishScreen();

}
void debugCommand() {
  if (screenCopy || MicroLink::diagnosticTextPending()) return;
  size_t budget=256;
  while (budget-- && DebugSerial.available()) {
    char c=DebugSerial.read();
    if (c=='\n') {
      command.trim();
      if (command=="STATUS") MicroLink::diagnostics();
      else if (command=="TITLE_SCROLL_TEST CLEAR") {
        if (inputTest) {
          inputTestAt=millis(); MicroUI::clearTitleScrollTest();
          DebugSerial.println("TITLE_SCROLL_TEST_CLEARED");
        } else DebugSerial.println("TITLE_SCROLL_TEST_ERROR");
      } else if (command.startsWith("TITLE_SCROLL_TEST ")) {
        int slot; unsigned long now; char extra;
        if (inputTest && sscanf(command.c_str()+18,"%d %lu %c",&slot,&now,&extra)==2
            && slot>=0 && slot<7 && now<=600000
            && MicroUI::setTitleScrollTest(uint8_t(slot),uint32_t(now))) {
          inputTestAt=millis(); DebugSerial.println("TITLE_SCROLL_TEST_OK");
        } else DebugSerial.println("TITLE_SCROLL_TEST_ERROR");
      }
      else if (command=="ORIENTATION_TEST 1" || command=="ORIENTATION_TEST 3") {
        if (inputTest && !testPressed && M5.Touch.getCount()==0) {
          inputTestAt=millis();
          MicroOrientation::setTestRotation(command.endsWith("3") ? 3 : 1);
          applyOrientation(); DebugSerial.println("ORIENTATION_TEST_OK");
        } else DebugSerial.println("ORIENTATION_TEST_ERROR");
      }
      else if (command.startsWith("ORIENTATION_MAP ")) {
        int x,y; char extra;
        if (inputTest && sscanf(command.c_str()+16,"%d %d %c",&x,&y,&extra)==2
            && x>=0 && x<720 && y>=0 && y<1280) {
          inputTestAt=millis();
          m5gfx::touch_point_t point{}; point.x=x; point.y=y;
          M5.Display.convertRawXY(&point,1);
          DebugSerial.printf("ORIENTATION_MAP %d %d\n",point.x,point.y);
        } else DebugSerial.println("ORIENTATION_MAP_ERROR");
      }
      else if (command=="AUDIO_DIAG") MicroSound::hardwareDiagnostics(DebugSerial);
      else if (command=="BLE_INFO") MicroBle::diagnostics(DebugSerial);
      else if (command=="BLE_ON" || command=="BLE_OFF") {
        MicroBle::setEnabled(command=="BLE_ON"); DebugSerial.println("BLE_OK");
      }
      else if (command=="BLE_PAIR") { MicroBle::startPairing(); DebugSerial.println("BLE_PAIR_OK"); }
      else if (command=="BLE_FORGET CONFIRM") { MicroBle::clearBonds(); DebugSerial.println("BLE_FORGET_OK"); }
      else if (command=="BLE_TEST STATUS") bleTestStatus();
      else if (command=="FRAME_CAPTURE STATUS") frameCaptureStatus();
      else if (command=="FRAME_CAPTURE CLEAR") {
        frameCapture.clear(); DebugSerial.println("FRAME_CAPTURE_CLEARED");
      }
      else if (command.startsWith("FRAME_CAPTURE ARM ")) {
        uint32_t arguments[5];
        bool valid=tud_mounted() && !usbPauseAt && !usbResumeAt
            && DeferredFrameCapture::parseUnsignedValues(
                command.c_str()+strlen("FRAME_CAPTURE ARM "),arguments,5);
        if (valid) {
          const uint32_t schedule[4]={arguments[1],arguments[2],arguments[3],arguments[4]};
          valid=frameCapture.arm(arguments[0],schedule,millis(),frameCaptureContext(),
                                 allocateFrameCapture,releaseFrameCapture,frameCaptureHooks);
        }
        DebugSerial.println(valid ? "FRAME_CAPTURE_ARMED" : "FRAME_CAPTURE_ERROR");
      }
      else if (command.startsWith("FRAME_CAPTURE READ ")) {
        uint32_t index[1];
        frameCapture.poll(millis(),frameCaptureContext());
        if (tud_mounted() && !bleTest.active() && MicroLink::bleDiagnosticIdle()
            && !inputTest && !MicroLink::capturingInputs() && !M5.Touch.getCount()
            && DeferredFrameCapture::parseUnsignedValues(
                command.c_str()+strlen("FRAME_CAPTURE READ "),index,1)
            && (screenCopy=frameCapture.take(index[0]))) {
          screenSize=DeferredFrameCapture::Capture::bytes; screenOffset=0; screenProgress=millis();
          screenSlow=false; MicroLink::setDiagnosticFrameActive(true); screenHeaderOffset=0;
          screenHeaderSize=snprintf(screenHeader,sizeof(screenHeader),"FRAME %u %u RGB565_BE\n",
              unsigned(DeferredFrameCapture::Capture::width),unsigned(DeferredFrameCapture::Capture::height));
        } else DebugSerial.println("FRAME_CAPTURE_ERROR");
      }
      else if (command.startsWith("FRAME_CAPTURE ")) DebugSerial.println("FRAME_CAPTURE_ERROR");
      else if (command=="BLE_TEST CANCEL") { cancelBleTest(); DebugSerial.println("BLE_TEST_CANCELLED"); }
      else if (command.startsWith("BLE_TEST ARM HOST_VERIFIED ")) {
        // USB-only, one-shot explicit authorization after the host tool checked
        // real desktop mappings and the unlocked active target. Firmware still
        // checks its fresh bounded metadata, session and every execution edge.
        char name[8],target[37],extra; unsigned long delayMs,session;
        bool valid=sscanf(command.c_str()+strlen("BLE_TEST ARM HOST_VERIFIED "),
            "%7s %lu %lu %36s %c",name,&delayMs,&session,target,&extra)==4
            && HostFeedback::validThreadId(target) && tud_mounted()
            && !usbPauseAt && !usbResumeAt && MicroLink::bleDiagnosticIdle();
        MicroBleDiagnostics::Plan plan=MicroBleDiagnostics::Plan::FastPair;
        if (valid) {
          if (!strcmp(name,"FAST")) plan=MicroBleDiagnostics::Plan::FastPair;
          else if (!strcmp(name,"DIAL")) plan=MicroBleDiagnostics::Plan::DialPair;
          else if (!strcmp(name,"PLAN")) plan=MicroBleDiagnostics::Plan::PlanPair;
          else valid=false;
        }
        if (valid) {
          const auto context=bleTestContext(plan,target);
          const uint32_t at=millis();
          valid=context.session==session && bleTest.arm(plan,uint32_t(delayMs),at,context);
          if (valid) {
            bleTestLastContext=context; bleTestLastContextAt=at; bleTestHaveLastContext=true;
            bleTestAbortContext={}; bleTestAbortContextAt=0; bleTestHaveAbortContext=false;
          }
        }
        if (valid) {
          bleTestPlan=plan; strlcpy(bleTestTarget,target,sizeof(bleTestTarget));
          DebugSerial.println("BLE_TEST_ARMED");
        } else DebugSerial.println("BLE_TEST_ERROR");
      }
      else if (command.startsWith("BLE_TEST ")) DebugSerial.println("BLE_TEST_ERROR");
      else if (command.startsWith("USB_PAUSE ")) {
        unsigned seconds; char extra;
        if (!inputTest && !screenCopy && !M5.Touch.getCount()
            && MicroBle::status().connected
            && sscanf(command.c_str()+10,"%u %c",&seconds,&extra)==1 && seconds>=10 && seconds<=120) {
          // Test an actual USB topology removal with the power cable retained.
          // Reconnect automatically even if wireless verification fails.
          usbPauseAt=millis()+250; usbResumeAt=usbPauseAt+seconds*1000;
          DebugSerial.println("USB_PAUSE_OK");
        } else DebugSerial.println("USB_PAUSE_ERROR");
      }
      else if (command=="SOUND ON" || command=="SOUND OFF") {
        MicroUI::setSoundEnabled(command=="SOUND ON"); syncLocalSettings(false);
        DebugSerial.println(command);
      } else if (command=="LANGUAGE EN" || command=="LANGUAGE ZH") {
        UiLocale::setLanguage(command=="LANGUAGE EN" ? UiLocale::Language::English
                                                   : UiLocale::Language::Chinese);
        syncLocalSettings(false); DebugSerial.println(command);
      } else if (command.startsWith("VOLUME ") || command.startsWith("BRIGHTNESS ")) {
        const bool brightness=command.startsWith("BRIGHTNESS ");
        int value; char extra;
        if (sscanf(command.c_str()+(brightness?11:7),"%d %c",&value,&extra)==1
            && value>=(brightness?10:0) && value<=100) {
          if (brightness) MicroUI::setBrightness(value); else MicroUI::setVolume(value);
          syncLocalSettings(false); DebugSerial.println(command);
        } else DebugSerial.println("SETTINGS_ERROR");
      } else if (command=="SETTINGS OPEN" || command=="SETTINGS CLOSE") {
        if ((command=="SETTINGS OPEN")!=MicroUI::settingsOpen()) {
          sendInput(MicroUI::touch(888,49,false));
          sendInput(MicroUI::touch(888,49,true));
          sendInput(MicroUI::touch(888,49,false)); drainInputs();
        }
        syncLocalSettings(false); DebugSerial.println(command);
      } else if (command.startsWith("SOUND_TEST ")) {
        Feedback::Cue cue=Feedback::Cue::None;
        const String name=command.substring(11);
        if (name=="PRESS") cue=Feedback::Cue::Press;
        else if (name=="CW") cue=Feedback::Cue::DetentCW;
        else if (name=="CC") cue=Feedback::Cue::DetentCC;
        else if (name=="DIRECTION") cue=Feedback::Cue::Direction;
        else if (name=="NEUTRAL") cue=Feedback::Cue::Neutral;
        if (inputTest && (cue!=Feedback::Cue::None || name=="TONE")) {
          inputTestAt=millis();
          if (name=="TONE") MicroSound::referenceTone(); else MicroSound::test(cue);
          DebugSerial.println("SOUND_TEST_OK");
        } else DebugSerial.println("SOUND_TEST_ERROR");
      }
      else if (command=="INPUT_TEST_BEGIN") {
        if (bleTest.active() || !MicroLink::bleDiagnosticIdle()) {
          cancelBleTest(); DebugSerial.println("INPUT_TEST_ERROR"); command=""; continue;
        }
        if (M5.Touch.getDetail().isPressed() || MicroUI::playing()) {
          DebugSerial.println("INPUT_TEST_ERROR");
        } else {
          if (!inputTest) effectsBeforeTest=MicroUI::effectsAreFrozen();
          inputTest=true; testPressed=false; inputTestAt=millis();
          MicroLink::captureInputs(true);
          MicroUI::setEffectsFrozen(true);
          sendInput(MicroUI::touch(0,0,false)); drainInputs();
          DebugSerial.println("INPUT_TEST_READY");
        }
      } else if (command=="INPUT_TEST_END") {
        endInputTest(); DebugSerial.println("INPUT_TEST_ENDED");
      } else if (command.startsWith("PREVIEW_LIGHTS ")) {
        if (inputTest) inputTestAt=millis();
        DebugSerial.println(MicroLink::previewLights(command.c_str()+15)
            ? "PREVIEW_LIGHTS_OK" : "PREVIEW_LIGHTS_ERROR");
      } else if (command=="PREVIEW_LIGHTS_CLEAR") {
        MicroLink::clearLightingPreview(); DebugSerial.println("PREVIEW_LIGHTS_CLEARED");
      } else if (command=="FX LIVE" || command=="FX FREEZE") {
        if (inputTest) {
          inputTestAt=millis();
          MicroUI::setEffectsFrozen(command=="FX FREEZE");
          DebugSerial.println(command);
        } else DebugSerial.println("FX_ERROR");
      } else if (command.startsWith("INPUT_TOUCH ")) {
        int x,y,pressed; char extra;
        if (inputTest && sscanf(command.c_str()+12,"%d %d %d %c",&x,&y,&pressed,&extra)==3
            && x>=0 && x<1280 && y>=0 && y<720 && (pressed==0 || pressed==1)) {
          testX=x; testY=y; testPressed=pressed;
          inputTestAt=millis();
          sendInput(MicroUI::touch(testX,testY,testPressed)); drainInputs();
          DebugSerial.println("INPUT_TOUCH_OK");
        } else DebugSerial.println("INPUT_TOUCH_ERROR");
      }
      else if (command.startsWith("TITLES ")) {
        if (!MicroLink::applyTitles(command.c_str()+7)) DebugSerial.println("TITLES_ERROR");
      }
      else if (command.startsWith("HOST_CONFIG ")) {
        if (!MicroLink::applyHostConfig(command.c_str()+12)) DebugSerial.println("HOST_CONFIG_ERROR");
      }
      else if (command.startsWith("PREVIEW_HOST ")) {
        if (inputTest) inputTestAt=millis();
        DebugSerial.println(MicroLink::previewHostState(command.c_str()+13)
            ? "PREVIEW_HOST_OK" : "PREVIEW_HOST_ERROR");
      }
      else if (command.startsWith("GAME_TOUCH ")) {
        int x,y,pressed;
        if (MicroUI::playing() && sscanf(command.c_str()+11,"%d %d %d",&x,&y,&pressed)==3
            && x>=0 && x<1280 && y>=96 && y<720 && (pressed==0 || pressed==1)) {
          MicroGames::touch(x,y,pressed!=0); DebugSerial.println("OK");
        } else DebugSerial.println("GAME_TOUCH_ERROR");
      }
      else if ((command=="SCREEN" || command=="SCREEN_TEST_SLOW") && uiReady) {
        if (bleTest.active() || !MicroLink::bleDiagnosticIdle()) {
          cancelBleTest(); DebugSerial.println("SCREEN_ERROR"); command=""; continue;
        }
        if (command=="SCREEN_TEST_SLOW" && !inputTest) {
          DebugSerial.println("SCREEN_TEST_ERROR"); command=""; continue;
        }
        if (M5.Touch.getCount()!=0) {
          DebugSerial.println("ERROR: lift touch before snapshot"); command=""; continue;
        }
        int w=MicroUI::bufferWidth(), h=MicroUI::bufferHeight();
        screenSize=size_t(w)*h*2;
        screenCopy=static_cast<uint8_t*>(heap_caps_malloc(screenSize,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
        if (screenCopy) {
          memcpy(screenCopy,MicroUI::framebuffer(),screenSize);
          screenOffset=0; screenProgress=millis();
          screenSlow=command=="SCREEN_TEST_SLOW";
          MicroLink::setDiagnosticFrameActive(true);
          screenHeaderOffset=0;
          screenHeaderSize=snprintf(screenHeader,sizeof(screenHeader),"FRAME %d %d RGB565_BE\n",w,h);
        } else {
          DebugSerial.println("ERROR: snapshot allocation failed");
        }
      } else if (command=="THEME LIGHT" || command=="THEME DARK" || command=="THEME TOGGLE") {
        if (command=="THEME TOGGLE") MicroUI::toggleTheme();
        else MicroUI::setTheme(command=="THEME DARK" ? MicroUI::Theme::Dark : MicroUI::Theme::Light);
        DebugSerial.println(MicroUI::theme()==MicroUI::Theme::Dark ? "THEME DARK" : "THEME LIGHT");
      } else if (command=="PLAY" || command=="MICRO") {
        if (MicroUI::settingsOpen()) {
          sendInput(MicroUI::touch(888,49,false));
          sendInput(MicroUI::touch(888,49,true));
          sendInput(MicroUI::touch(888,49,false)); drainInputs();
        }
        if ((command=="PLAY")!=MicroUI::playing()) {
          sendInput(MicroUI::touch(1180,50,false));
          sendInput(MicroUI::touch(1180,50,true));
          sendInput(MicroUI::touch(1180,50,false));
          drainInputs();
        }
        DebugSerial.println(MicroUI::playing()?"PLAY":"MICRO");
      } else if (command=="BOOTLOADER") {
        DebugSerial.println("Restarting into download mode"); DebugSerial.flush();
        tab5EnterRomBootloader();
      } else if (command=="PERF_RESET") {
        MicroLink::resetMetrics(); DebugSerial.println("OK");
      } else if (command=="PERF_FULL" || command=="PERF_DIRTY") {
        MicroUI::setFullPresent(command=="PERF_FULL");
        MicroLink::resetMetrics(); DebugSerial.println("OK");
      } else if (command=="RESTART") ESP.restart();
      command="";
      if (screenCopy || MicroLink::diagnosticTextPending()) break;
    } else if (command.length()<6144) command+=c;
  }
}
}
void setup() {
  USB.VID(0x303a); USB.PID(0x8360); USB.firmwareVersion(0x0100);
  USB.productName("Tab5 Codex Micro"); USB.manufacturerName("Tab5 Custom");
  uint8_t mac[6];
  ESP_ERROR_CHECK(esp_efuse_mac_get_default(mac));
  static char usbSerial[18];
  snprintf(usbSerial,sizeof(usbSerial),"TAB5-%02X%02X%02X%02X%02X%02X",
           mac[0],mac[1],mac[2],mac[3],mac[4],mac[5]);
  USB.serialNumber(usbSerial); USB.usbPower(500);
  DebugSerial.enableReboot(false);
  DebugSerial.setTxTimeoutMs(20);
  // A metadata/preview command can exceed the CDC default 256-byte queue.
  // Buffer one complete bounded line while a partial UI redraw is in flight.
  DebugSerial.setRxBufferSize(8192);
  command.reserve(6144);
  DebugSerial.begin(115200);
  MicroLink::begin();
  auto cfg=M5.config();
  cfg.serial_baudrate=0; cfg.internal_spk=true; cfg.internal_mic=false;
  cfg.external_display_value=0; cfg.internal_imu=true; cfg.internal_rtc=false;
  cfg.output_power=false;
  M5.begin(cfg);
  // Stay within a computer USB port's budget: this controller does not need
  // battery charging or USB-A/external power outputs. C6 is reserved for BLE.
  M5.Power.setBatteryCharge(false);
  M5.Power.setUsbOutput(false);
  M5.getIOExpander(1).digitalWrite(0,true);  // C6 supplies the BLE controller over SDIO.
  M5.Display.setRotation(3);
  MicroOrientation::begin(3);
  M5.Display.setBrightness(60);
  uiReady=MicroUI::init();
  appliedSettings=DeviceSettings::begin();
  UiLocale::setLanguage(appliedSettings.english ? UiLocale::Language::English
                                              : UiLocale::Language::Chinese);
  MicroUI::setSoundEnabled(appliedSettings.sound);
  MicroUI::setVolume(appliedSettings.volume);
  MicroUI::setBrightness(appliedSettings.brightness);
  M5.Display.setBrightness(DeviceSettings::backlight(appliedSettings.brightness));
  // Initialize I2S before USB: the P4 SDK GPIO helper still assumes the
  // default PHY mapping and would disable OTG pads when setting GPIO26/27.
  MicroSound::begin(appliedSettings.sound,appliedSettings.volume);
  previousTheme=MicroUI::theme();
  if (!uiReady) { M5.Display.fillScreen(TFT_BLACK); M5.Display.setTextColor(TFT_RED); M5.Display.println("Canvas allocation failed"); }
  USB.begin();
  MicroBle::begin(usbSerial);
  MicroLink::attachBle({MicroBle::status,MicroBle::submit,MicroBle::disconnect});
  RenderService::setCallback(serviceInputs);
  DebugSerial.println("Tab5 Codex Micro 0.12.0 ready");
}
void loop() {
  if (usbPauseAt && int32_t(millis()-usbPauseAt)>=0) {
    usbPauseAt=0; MicroLink::setUsbDiagnosticPaused(true); tud_disconnect();
  }
  if (usbResumeAt && int32_t(millis()-usbResumeAt)>=0) {
    usbResumeAt=0; MicroLink::setUsbDiagnosticPaused(false); tud_connect();
  }
  if (inputTest && millis()-inputTestAt>60000) endInputTest();
  serviceInputs();
  auto p=M5.Touch.getDetail();
  // Release the complete old gesture before changing coordinate systems.
  // getCount includes every finger and the final release transition.
  if (MicroOrientation::poll(M5.Touch.getCount()!=0 || inputTest || inputHandoff || screenCopy))
    applyOrientation();
  debugCommand();
  MicroBle::poll();
  serviceWirelessMetadata();
  syncLocalSettings();
  DeviceSettings::poll(p.isPressed() || (inputTest && testPressed));
  streamScreen();
  // Let active gestures update at display cadence; the UI keeps its own
  // slower idle/game pacing and skips unchanged regions.
  if (uiReady && millis()-rendered>=16) {
    rendered=millis();
    const auto captureBefore=frameCapture.active() ? frameCaptureContext() : DeferredFrameCapture::Context{};
    uint32_t started=micros();
    if (MicroUI::render(MicroLink::state())) MicroLink::recordFrame(micros()-started);
    serviceFrameCapture(captureBefore);
  }
  if (frameCapture.state()!=DeferredFrameCapture::State::Idle)
    frameCapture.poll(millis(),frameCapture.active() ? frameCaptureContext() : DeferredFrameCapture::Context{});
  delay(2);
}
