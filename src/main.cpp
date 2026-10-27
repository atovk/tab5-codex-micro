#include <Arduino.h>
#include <algorithm>
#include <USB.h>
#include <FS.h>
#include <M5Unified.h>
#include "micro_link.h"
#include "micro_ui.h"
#include "micro_games.h"
#include "bootloader.h"
#include "device_settings.h"
#include "micro_sound.h"
#include "micro_orientation.h"
#include <esp_system.h>
#include <esp_heap_caps.h>
#include <esp_mac.h>

USBCDC DebugSerial;

namespace {
String command;
bool uiReady=false;
uint32_t rendered=0;
uint32_t sampledAt=0;
uint8_t *screenCopy=nullptr;
size_t screenOffset=0, screenSize=0;
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

void applyOrientation() {
  M5.Display.setRotation(MicroOrientation::rotation());
  MicroUI::invalidateDisplay();
  rendered=millis()-16;
}

void sendInput(const MicroUI::Event& event) {
  MicroSound::event(event,MicroLink::capturingInputs());
  MicroLink::send(event);
}

void syncLocalSettings(bool feedback=true) {
  const DeviceSettings::Values next{MicroUI::soundEnabled(),MicroUI::volume(),MicroUI::brightness()};
  const bool audioChanged=next.sound!=appliedSettings.sound || next.volume!=appliedSettings.volume;
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
  const bool changed=previousTheme!=MicroUI::theme() || previousPlay!=MicroUI::playing()
      || previousSettings!=MicroUI::settingsOpen();
  if (changed && feedback) MicroSound::localPress(MicroLink::capturingInputs());
  previousTheme=MicroUI::theme(); previousPlay=MicroUI::playing();
  previousSettings=MicroUI::settingsOpen();
}

void drainInputs() {
  MicroUI::Event event;
  while (MicroUI::pollEvent(event)) sendInput(event);
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

void streamScreen() {
  if (!screenCopy) return;
  if (millis()-screenProgress>30000) {
    free(screenCopy); screenCopy=nullptr;
    return;
  }
  int available=DebugSerial.availableForWrite();
  if (available<=0) return;
  size_t n=std::min<size_t>(available,screenSize-screenOffset);
  n=std::min<size_t>(n,1024);
  size_t sent=DebugSerial.write(screenCopy+screenOffset,n);
  screenOffset+=sent;
  if (sent) screenProgress=millis();
  if (screenOffset==screenSize) {
    DebugSerial.println("\nEND_FRAME");
    free(screenCopy); screenCopy=nullptr;
  }
}
void debugCommand() {
  if (screenCopy) return;
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
      else if (command=="SOUND ON" || command=="SOUND OFF") {
        MicroUI::setSoundEnabled(command=="SOUND ON"); syncLocalSettings(false);
        DebugSerial.println(command);
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
      else if (command=="SCREEN" && uiReady) {
        int w=MicroUI::bufferWidth(), h=MicroUI::bufferHeight();
        screenSize=size_t(w)*h*2;
        screenCopy=static_cast<uint8_t*>(heap_caps_malloc(screenSize,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
        if (screenCopy) {
          memcpy(screenCopy,MicroUI::framebuffer(),screenSize);
          screenOffset=0; screenProgress=millis();
          DebugSerial.printf("FRAME %d %d RGB565_BE\n",w,h);
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
      if (screenCopy) break;
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
  // battery charging, the C6 radio, or USB-A/external power outputs.
  M5.Power.setBatteryCharge(false);
  M5.Power.setUsbOutput(false);
  M5.getIOExpander(1).digitalWrite(0,false);  // Tab5 WLAN_PWR_EN
  M5.Display.setRotation(3);
  MicroOrientation::begin(3);
  M5.Display.setBrightness(60);
  uiReady=MicroUI::init();
  appliedSettings=DeviceSettings::begin();
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
  DebugSerial.println("Tab5 Codex Micro 0.10.2 ready");
}
void loop() {
  uint32_t now=micros();
  if (sampledAt) MicroLink::recordInputInterval(now-sampledAt);
  sampledAt=now;
  M5.update();
  MicroLink::poll();
  auto p=M5.Touch.getDetail();
  if (inputTest && millis()-inputTestAt>60000) endInputTest();
  if (inputHandoff && !p.isPressed()) inputHandoff=false;
  if (uiReady) {
    sendInput(inputTest ? MicroUI::touch(testX,testY,testPressed)
                    : inputHandoff ? MicroUI::touch(0,0,false)
                                   : MicroUI::touch(p.x,p.y,p.isPressed()));
    drainInputs();
  }
  // Release the complete old gesture before changing coordinate systems.
  // getCount includes every finger and the final release transition.
  if (MicroOrientation::poll(M5.Touch.getCount()!=0 || inputTest || inputHandoff || screenCopy))
    applyOrientation();
  debugCommand();
  syncLocalSettings();
  DeviceSettings::poll(p.isPressed() || (inputTest && testPressed));
  streamScreen();
  // Let active gestures update at display cadence; the UI keeps its own
  // slower idle/game pacing and skips unchanged regions.
  if (uiReady && millis()-rendered>=16) {
    rendered=millis();
    uint32_t started=micros();
    if (MicroUI::render(MicroLink::state())) MicroLink::recordFrame(micros()-started);
  }
  delay(2);
}
