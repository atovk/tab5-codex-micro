#include "micro_sound.h"
#include "ui_sounds.h"
#include "audio_probe.h"
#include <ArduinoJson.h>
#include <Arduino.h>
#include <M5Unified.h>
#include <atomic>
#include <driver/gpio.h>
#include <hal/usb_serial_jtag_ll.h>
#include <soc/usb_wrap_struct.h>
#include <hal/usb_wrap_ll.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>

namespace MicroSound {
namespace {
struct Request { Feedback::Cue cue; uint8_t variant; uint32_t at; bool tone=false; };
QueueHandle_t queue=nullptr;
Feedback::Policy policy;
std::atomic<bool> available{false}, enabled{true};
std::atomic<bool> microphoneActive{false};
std::atomic<uint8_t> volume{45};
std::atomic<uint32_t> queued{0}, played{0}, released{0}, dropped{0}, suppressed{0};
std::atomic<uint32_t> enqueueMax{0}, startMax{0};
uint8_t note=0;
uint32_t lastAt=0;
bool hasPlayed=false;

void maximum(std::atomic<uint32_t>& value,uint32_t next) {
  uint32_t prior=value.load();
  while (prior<next && !value.compare_exchange_weak(prior,next)) {}
}
uint8_t master() { return enabled.load() && !microphoneActive.load() ? (volume.load()*160+50)/100 : 0; }
UiSounds::Clip clip(const Request& request) {
  switch (request.cue) {
    case Feedback::Cue::Press: return request.variant ? UiSounds::PressHigh : UiSounds::Press;
    case Feedback::Cue::DetentCW: case Feedback::Cue::DetentCC:
      return UiSounds::Ticks[request.variant%5];
    case Feedback::Cue::Direction: return UiSounds::Direction;
    default: return UiSounds::Neutral;
  }
}
void consumed(void*,const void*,uint8_t) { released.fetch_add(1); }
void worker(void*) {
  Request request;
  for (;;) {
    if (xQueueReceive(queue,&request,portMAX_DELAY)!=pdTRUE) continue;
    if (request.cue==Feedback::Cue::None) { M5.Speaker.stop(0); continue; }
    if (!enabled.load() || microphoneActive.load() || !volume.load() || millis()-request.at>45
        || M5.Speaker.isPlaying(0)) { dropped.fetch_add(1); continue; }
    const uint32_t began=micros();
    const auto sample=clip(request);
    const bool ok=request.tone ? M5.Speaker.tone(1000,250,0,false)
        : M5.Speaker.playRaw(sample.data,sample.size,UiSounds::SampleRate,false,1,0,false);
    maximum(startMax,micros()-began);
    if (ok) played.fetch_add(1); else dropped.fetch_add(1);
  }
}
bool submit(Feedback::Cue cue,uint8_t variant=0,bool tone=false) {
  if (cue==Feedback::Cue::None) return false;
  if (!available.load() || !enabled.load() || microphoneActive.load() || !volume.load()) { suppressed.fetch_add(1); return false; }
  const uint32_t now=millis();
  const uint32_t cooldown=(cue==Feedback::Cue::DetentCW || cue==Feedback::Cue::DetentCC) ? 25 : 45;
  if (hasPlayed && now-lastAt<cooldown) { dropped.fetch_add(1); return false; }
  lastAt=now; hasPlayed=true;
  const Request request{cue,variant,now,tone};
  const uint32_t began=micros();
  // One replaceable request: fast gestures never leave a queue of old notes.
  if (uxQueueMessagesWaiting(queue)) dropped.fetch_add(1);
  const bool ok=xQueueOverwrite(queue,&request)==pdTRUE;
  maximum(enqueueMax,micros()-began);
  if (ok) queued.fetch_add(1); else dropped.fetch_add(1);
  return ok;
}
}
bool begin(bool sound,uint8_t level) {
  enabled.store(sound); volume.store(constrain(level,0,100));
  auto config=M5.Speaker.config();
  config.sample_rate=UiSounds::SampleRate;
  config.stereo=false; config.dma_buf_len=128; config.dma_buf_count=4;
  config.task_priority=2; config.task_pinned_core=0;
  M5.Speaker.config(config);
  M5.Speaker.setVolume(master()); M5.Speaker.setChannelVolume(0,255);
  if (!M5.Speaker.begin()) { M5.Speaker.end(); return false; }
  if (!M5.In_I2C.scanID(0x10)) { M5.Speaker.end(); return false; }
  queue=xQueueCreate(1,sizeof(Request));
  if (!queue) { M5.Speaker.end(); return false; }
  M5.Speaker.setBufferReleaseCallback(nullptr,consumed);
  if (xTaskCreatePinnedToCore(worker,"ui-feedback",3072,nullptr,1,nullptr,0)!=pdPASS) {
    vQueueDelete(queue); queue=nullptr; M5.Speaker.end(); return false;
  }
  available.store(true);
  return true;
}
void configure(bool sound,uint8_t level) {
  enabled.store(sound); volume.store(constrain(level,0,100));
  M5.Speaker.setVolume(master());
  if ((!sound || !level) && queue) {
    const Request stop{Feedback::Cue::None,0,millis()};
    xQueueOverwrite(queue,&stop);
  }
}
void setMicrophoneActive(bool active) {
  if (microphoneActive.exchange(active)==active) return;
  M5.Speaker.setVolume(master());
  if (active && queue) {
    const Request stop{Feedback::Cue::None,0,millis()};
    xQueueOverwrite(queue,&stop);
  }
}
void event(const MicroUI::Event& event,bool capture) {
  const auto cue=policy.event(event);
  if (cue==Feedback::Cue::None) return;
  if (capture) { suppressed.fetch_add(1); return; }
  uint8_t variant=0;
  if (cue==Feedback::Cue::DetentCW) { note=(note+1)%5; variant=note; }
  else if (cue==Feedback::Cue::DetentCC) { note=(note+4)%5; variant=note; }
  else if (cue==Feedback::Cue::Press && event.key) {
    const char* last=event.key;
    while (*last) ++last;
    variant=(last[-1]-'0')&1;
  }
  submit(cue,variant);
}
void localPress(bool capture) {
  if (capture) return;
  submit(Feedback::Cue::Press);
}
bool test(Feedback::Cue cue) {
  if (cue==Feedback::Cue::DetentCW) note=(note+1)%5;
  else if (cue==Feedback::Cue::DetentCC) note=(note+4)%5;
  return submit(cue,note);
}
bool referenceTone() { return submit(Feedback::Cue::Press,0,true); }
void hardwareDiagnostics(Print& output) {
  JsonDocument d;
  d["type"]="audio_hardware";
  auto read=[&](uint8_t address,uint8_t reg,JsonObject target) {
    uint8_t value=0;
    const bool ok=M5.In_I2C.readRegister(address,reg,&value,1,400000);
    char key[5]; snprintf(key,sizeof(key),"%02X",reg);
    if (ok) target[key]=value; else target[key]=nullptr;
    return ok;
  };
  bool ok=true;
  JsonObject codec=d["codec"].to<JsonObject>();
  for (uint8_t reg:{0,1,2,3,4,5,6,7,8,23,24,25,26,27,28,29,38,39,42,43,45,46,47,48,49})
    ok=read(0x10,reg,codec)&&ok;
  JsonObject amp=d["ioe43"].to<JsonObject>();
  for (uint8_t reg:{3,5,7,15}) ok=read(0x43,reg,amp)&&ok;
  d["registers_read_ok"]=ok;
  const auto cfg=M5.Speaker.config();
  d["sample_rate"]=cfg.sample_rate; d["stereo"]=cfg.stereo;
  d["magnification"]=cfg.magnification;
  d["usb"]["software_phy_selection"]=bool(LP_SYS.usb_ctrl.sw_hw_usb_phy_sel);
  d["usb"]["otg_phy0"]=bool(LP_SYS.usb_ctrl.sw_usb_phy_sel);
  d["usb"]["otg_pad"]=bool(USB_WRAP.otg_conf.usb_pad_enable);
  d["usb"]["jtag_pad"]=usb_serial_jtag_ll_phy_is_pad_enabled();
  for (uint8_t pin:{26,27,29,30}) {
    gpio_io_config_t config{};
    JsonObject gpio=d["gpio"][String(pin)].to<JsonObject>();
    const auto result=gpio_get_io_config(gpio_num_t(pin),&config);
    gpio["result"]=result;
    if (result==ESP_OK) {
      gpio["function"]=config.fun_sel; gpio["signal_out"]=config.sig_out;
      gpio["output_enabled"]=config.oe; gpio["input_enabled"]=config.ie;
    }
  }
  d["pins"]["mclk"]=cfg.pin_mck; d["pins"]["bclk"]=cfg.pin_bck;
  d["pins"]["lrck"]=cfg.pin_ws; d["pins"]["data"]=cfg.pin_data_out;
  const auto p=AudioProbe::snapshot();
  // Driver results are distinct from PCM queue retirement and acoustic output.
  JsonObject i=d["i2s"].to<JsonObject>();
  i["tracked"]=p.tracked; i["info_ok"]=p.infoOk; i["enabled"]=p.enabled;
  i["info_result"]=p.infoResult; i["port"]=p.port; i["direction"]=p.direction; i["mode"]=p.mode;
  i["source_hz"]=p.sourceHz; i["mclk_hz"]=p.mclkHz; i["bclk_hz"]=p.bclkHz; i["sample_rate"]=p.sampleRate;
  i["enable_calls"]=p.enableCalls; i["enable_errors"]=p.enableErrors; i["last_enable_result"]=p.lastEnableResult;
  i["write_calls"]=p.writeCalls; i["write_errors"]=p.writeErrors; i["write_timeouts"]=p.writeTimeouts;
  i["short_writes"]=p.shortWrites; i["requested_bytes"]=p.requestedBytes; i["written_bytes"]=p.writtenBytes;
  i["nonblocking_retries"]=p.nonblockingRetries; i["blocking_errors"]=p.blockingErrors;
  i["blocking_short_writes"]=p.blockingShortWrites;
  i["nonzero_samples"]=p.nonzeroSamples; i["peak"]=p.peak;
  i["last_write_result"]=p.lastWriteResult; i["first_write_error"]=p.firstWriteError;
  i["last_write_error"]=p.lastWriteError; i["other_enable_calls"]=p.otherEnableCalls; i["other_write_calls"]=p.otherWriteCalls;
  serializeJson(d,output); output.println();
}
Diagnostics diagnostics() {
  Diagnostics d;
  d.available=available.load(); d.enabled=enabled.load(); d.volume=volume.load();
  d.master=master(); d.running=M5.Speaker.isRunning();
  d.queued=queued.load(); d.played=played.load(); d.released=released.load();
  d.dropped=dropped.load(); d.suppressed=suppressed.load();
  d.enqueueMax=enqueueMax.load(); d.startMax=startMax.load();
  return d;
}
}
