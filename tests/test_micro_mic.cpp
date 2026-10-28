// Execute the production capture worker with deterministic DMA/RTOS boundaries.
// No microphone behavior is reimplemented here: the stubs only supply PCM and
// inject host/control changes while the real worker runs in the calling thread.
#include "micro_mic.h"
#include <Arduino.h>
#include <M5Unified.h>
#include <driver/i2s_std.h>
#include <freertos/task.h>

#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <functional>
#include <vector>

namespace {
constexpr size_t framesPerRead=480;
constexpr uint64_t firstEpoch=0x1234567800000041ULL;
constexpr double pi=3.14159265358979323846;
struct EndWorker {};
enum class Signal { transient,tone,silence };
struct Block {
  Signal signal=Signal::transient;
  unsigned expectedEnables=1;
  std::function<void()> duringRead;
};
struct Delivery {
  size_t read;
  uint64_t epoch;
  size_t accepted;
  std::vector<int16_t> pcm;
};
std::vector<Block> blocks;
std::vector<Delivery> deliveries;
TaskFunction_t captureWorker=nullptr;
uint64_t hostEpoch=firstEpoch;
uint32_t now=0;
unsigned enabled=0,disabled=0,disableFailures=0,speakerBegins=0,speakerEnds=0;
unsigned created=0,initialized=0,epochChecksAfterRead=0,notifications=0,waits=0;
size_t completedReads=0;
bool receiverEnabled=false,staleDmaQueued=false,conditionalSink=true;
std::function<void(size_t,unsigned)> onEpoch;
std::function<void()> onSink;
i2s_chan_config_t channelConfig{};
i2s_std_config_t standardConfig{};

uint64_t epoch() {
  if (completedReads) {
    ++epochChecksAfterRead;
    if (onEpoch) onEpoch(completedReads,epochChecksAfterRead);
  }
  return hostEpoch;
}
size_t sink(const int16_t* pcm,size_t count,uint64_t captureEpoch) {
  assert(count==160 && pcm);
  if (onSink) onSink();
  const size_t accepted=(!conditionalSink || captureEpoch==hostEpoch) ? count : 0;
  deliveries.push_back({completedReads,captureEpoch,accepted,{pcm,pcm+count}});
  return accepted;
}
void initialize(bool provideEpoch=true) {
  assert(MicroMic::begin(sink,provideEpoch ? epoch : nullptr));
  assert(captureWorker && created==1 && initialized==1);
  assert(channelConfig.id==I2S_NUM_1 && channelConfig.role==I2S_ROLE_SLAVE);
  assert(channelConfig.dma_frame_num==framesPerRead && channelConfig.dma_desc_num>=2);
  assert(standardConfig.clk_cfg.sample_rate_hz==48000);
  assert(standardConfig.slot_cfg.data_bit_width==16 && standardConfig.slot_cfg.slot_mode==2);
  // RX must share the existing speaker clocks, without routing a second MCLK or
  // taking ownership of GPIO26 (speaker/USB debug PHY regression).
  assert(standardConfig.gpio_cfg.mclk==GPIO_NUM_NC);
  assert(standardConfig.gpio_cfg.bclk==27 && standardConfig.gpio_cfg.ws==29);
  assert(standardConfig.gpio_cfg.din==28 && standardConfig.gpio_cfg.dout==GPIO_NUM_NC);
}
void addStartup(unsigned expectedEnables=1) {
  for (unsigned n=0;n<3;++n) blocks.push_back({Signal::transient,expectedEnables,{}});
}
void run() {
  bool ended=false;
  try { captureWorker(nullptr); } catch (const EndWorker&) { ended=true; }
  assert(ended && completedReads==blocks.size());
  assert(speakerBegins==0 && speakerEnds==0);
  assert(MicroMic::status().available);
}
void assertSilence(const Delivery& value) {
  assert(std::all_of(value.pcm.begin(),value.pcm.end(),[](int16_t sample) { return sample==0; }));
}
void assertTone(const Delivery& value) {
  assert(std::any_of(value.pcm.begin(),value.pcm.end(),[](int16_t sample) { return sample>1000; }));
  assert(std::any_of(value.pcm.begin(),value.pcm.end(),[](int16_t sample) { return sample<-1000; }));
}
void assertRestarted() {
  assert(enabled==2 && disabled>=1 && !staleDmaQueued);
  assert(MicroMic::status().readErrors==0 && MicroMic::status().lastError==ESP_OK);
}
void normalSession(bool provideEpoch=true) {
  initialize(provideEpoch);
  conditionalSink=provideEpoch;
  MicroMic::setStreaming(true);
  addStartup();
  blocks.push_back({Signal::tone,1,{}});
  run();
  assert(enabled==1 && disabled==0);
  assert(deliveries.size()==1 && deliveries[0].read==4);
  assert(deliveries[0].epoch==(provideEpoch ? firstEpoch : 0));
  assert(deliveries[0].accepted==160);
  assertTone(deliveries[0]);
  const auto value=MicroMic::status();
  assert(value.frames==4*480 && value.samples==160 && value.sentSamples==160);
  assert(value.droppedSamples==0 && value.level>0 && value.rms>1000);
}
void invalidateDuringRead(bool muteCycle=false,bool monitorToStream=false) {
  initialize();
  if (monitorToStream) MicroMic::setMonitoring(true);
  else MicroMic::setStreaming(true);
  addStartup();
  blocks.push_back({Signal::tone,1,[=] {
    if (muteCycle) {
      // The worker observes unmuted both before and after this callback.
      MicroMic::setMuted(true); MicroMic::setMuted(false);
    } else if (monitorToStream) {
      MicroMic::setMonitoring(false); MicroMic::setStreaming(true);
    } else ++hostEpoch;
    staleDmaQueued=true;
  }});
  addStartup(2);
  blocks.push_back({Signal::silence,2,{}});
  run();
  assertRestarted();
  // A block captured under the previous request may never become new-session PCM.
  assert(deliveries.size()==1 && deliveries[0].read==8);
  assert(deliveries[0].epoch==hostEpoch && deliveries[0].accepted==160);
  assertSilence(deliveries[0]);
  assert(!MicroMic::status().muted && MicroMic::status().streaming);
}
void invalidateBetweenReads() {
  initialize(); MicroMic::setStreaming(true);
  addStartup(); blocks.push_back({Signal::tone,1,{}});
  bool changed=false;
  // Change immediately after acceptance, before the worker can start another
  // read. Old DMA remains queued unless the receiver is restarted.
  onEpoch=[&](size_t read,unsigned check) {
    if (!changed && read==4 && check==4) {
      ++hostEpoch; staleDmaQueued=true; changed=true;
    }
  };
  addStartup(2); blocks.push_back({Signal::silence,2,{}});
  run();
  assert(changed); assertRestarted();
  assert(deliveries.size()==2 && deliveries[0].read==4 && deliveries[1].read==8);
  assert(deliveries[0].epoch==firstEpoch && deliveries[1].epoch==firstEpoch+1);
  assertTone(deliveries[0]);
  // This also catches DSP history leaking from the first session after a reset.
  assertSilence(deliveries[1]);
}
void invalidateAtCheck(unsigned targetCheck) {
  initialize(); MicroMic::setStreaming(true);
  addStartup(); blocks.push_back({Signal::tone,1,{}});
  bool changed=false;
  onEpoch=[&](size_t read,unsigned check) {
    if (!changed && read==4 && check==targetCheck) {
      ++hostEpoch; staleDmaQueued=true; changed=true;
    }
  };
  addStartup(2); blocks.push_back({Signal::silence,2,{}});
  run();
  assert(changed); assertRestarted();
  assert(deliveries.size()==1 && deliveries[0].read==8 && deliveries[0].epoch==hostEpoch);
  assertSilence(deliveries[0]);
}
void invalidateAtTransport() {
  initialize(); MicroMic::setStreaming(true);
  addStartup(); blocks.push_back({Signal::tone,1,{}});
  bool changed=false;
  onSink=[&] {
    if (!changed) { ++hostEpoch; staleDmaQueued=true; changed=true; }
  };
  addStartup(2); blocks.push_back({Signal::silence,2,{}});
  run();
  assert(changed); assertRestarted();
  assert(deliveries.size()==2 && deliveries[0].read==4 && deliveries[1].read==8);
  assert(deliveries[0].epoch==firstEpoch && deliveries[0].accepted==0);
  assert(deliveries[1].epoch==hostEpoch && deliveries[1].accepted==160);
  assertSilence(deliveries[1]);
  const auto value=MicroMic::status();
  assert(value.sentSamples==160 && value.droppedSamples==160);
}
void monitorDoesNotInterruptStream() {
  initialize(); MicroMic::setStreaming(true);
  addStartup();
  blocks.push_back({Signal::tone,1,[] {
    MicroMic::setMonitoring(true); MicroMic::setMonitoring(false);
  }});
  run();
  assert(enabled==1 && disabled==0);
  assert(deliveries.size()==1 && deliveries[0].read==4 && deliveries[0].accepted==160);
  assert(deliveries[0].epoch==firstEpoch);
  assertTone(deliveries[0]);
}
void disableRetry() {
  disableFailures=1;
  invalidateDuringRead();
  assert(disabled==2 && waits>=1);
}
}

MicTest::Device M5;
MicTest::SpeakerConfig MicTest::Speaker::config() const { return {}; }
bool MicTest::Speaker::isRunning() const { return true; }
bool MicTest::Speaker::begin() { ++speakerBegins; return true; }
void MicTest::Speaker::end() { ++speakerEnds; }
bool MicTest::I2c::scanID(uint8_t address) const { assert(address==0x40); return true; }
bool MicTest::I2c::writeRegister8(uint8_t address,uint8_t,uint8_t,uint32_t rate) {
  assert(address==0x40 && rate==400000); return true;
}
uint32_t millis() { return now; }
BaseType_t xTaskCreatePinnedToCore(TaskFunction_t function,const char*,uint32_t stack,
                                 void* argument,UBaseType_t priority,TaskHandle_t* task,BaseType_t core) {
  assert(stack>=4096 && argument==nullptr && priority==2 && core==0);
  assert(function && task && !captureWorker);
  captureWorker=function; *task=reinterpret_cast<void*>(uintptr_t(2)); return pdPASS;
}
BaseType_t xTaskNotifyGive(TaskHandle_t task) { assert(task); ++notifications; return pdPASS; }
uint32_t ulTaskNotifyTake(BaseType_t clear,TickType_t timeout) {
  assert(clear==pdTRUE && timeout<=20);
  now+=timeout;
  if (++waits>100) throw EndWorker{};
  return 0;
}
esp_err_t i2s_new_channel(const i2s_chan_config_t* config,i2s_chan_handle_t* tx,i2s_chan_handle_t* rx) {
  assert(config && !tx && rx); channelConfig=*config;
  ++created; *rx=reinterpret_cast<void*>(uintptr_t(1)); return ESP_OK;
}
esp_err_t i2s_del_channel(i2s_chan_handle_t) { return ESP_OK; }
esp_err_t i2s_channel_init_std_mode(i2s_chan_handle_t channel,const i2s_std_config_t* config) {
  assert(channel && config); standardConfig=*config; ++initialized; return ESP_OK;
}
esp_err_t i2s_channel_enable(i2s_chan_handle_t channel) {
  assert(channel && !receiverEnabled);
  receiverEnabled=true; staleDmaQueued=false; ++enabled; return ESP_OK;
}
esp_err_t i2s_channel_disable(i2s_chan_handle_t channel) {
  assert(channel && receiverEnabled); ++disabled;
  if (disableFailures) { --disableFailures; return ESP_FAIL; }
  receiverEnabled=false; return ESP_OK;
}
esp_err_t i2s_channel_read(i2s_chan_handle_t channel,void* destination,size_t capacity,
                         size_t* received,uint32_t timeout) {
  assert(channel && receiverEnabled && destination && received);
  assert(capacity==framesPerRead*2*sizeof(int16_t) && timeout<=20);
  if (completedReads==blocks.size()) throw EndWorker{};
  const auto& block=blocks[completedReads];
  assert(enabled==block.expectedEnables);
  auto* pcm=static_cast<int16_t*>(destination);
  for (size_t frame=0;frame<framesPerRead;++frame) {
    int16_t value=0;
    if (staleDmaQueued || block.signal==Signal::transient) value=INT16_MAX;
    else if (block.signal==Signal::tone)
      value=int16_t(std::lround(12000*std::sin(2*pi*1000*frame/48000)));
    pcm[2*frame]=pcm[2*frame+1]=value;
  }
  staleDmaQueued=false;
  ++completedReads; epochChecksAfterRead=0; now+=10;
  *received=capacity;
  if (block.duringRead) block.duringRead();
  return ESP_OK;
}

int main(int argc,char** argv) {
  assert(argc==2);
  if (!std::strcmp(argv[1],"normal")) normalSession();
  else if (!std::strcmp(argv[1],"no-epoch")) normalSession(false);
  else if (!std::strcmp(argv[1],"during-read")) invalidateDuringRead();
  else if (!std::strcmp(argv[1],"between-reads")) invalidateBetweenReads();
  else if (!std::strcmp(argv[1],"mute-cycle")) invalidateDuringRead(true);
  else if (!std::strcmp(argv[1],"monitor-stream")) invalidateDuringRead(false,true);
  else if (!std::strcmp(argv[1],"after-dsp")) invalidateAtCheck(2);
  else if (!std::strcmp(argv[1],"before-sink")) invalidateAtCheck(3);
  else if (!std::strcmp(argv[1],"transport-race")) invalidateAtTransport();
  else if (!std::strcmp(argv[1],"monitor-streaming")) monitorDoesNotInterruptStream();
  else if (!std::strcmp(argv[1],"disable-retry")) disableRetry();
  else assert(false);
  std::puts("microphone capture session contract passed");
}
