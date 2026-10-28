#include "micro_mic.h"
#include "mic_processing.h"

#include <Arduino.h>
#include <M5Unified.h>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <driver/i2s_std.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

namespace MicroMic {
namespace {
constexpr uint32_t InputRate=48000;
constexpr size_t InputFrames=480;
constexpr uint8_t CodecAddress=0x40;
i2s_chan_handle_t receiver=nullptr;
TaskHandle_t task=nullptr;
Sink output=nullptr;
Epoch sessionEpoch=nullptr;
std::atomic<bool> available{false},active{false},muted{false},streaming{false},monitoring{false};
std::atomic<uint32_t> requestGeneration{0};
std::atomic<uint16_t> gain{100},rms{0},rawPeak{0};
std::atomic<uint8_t> level{0},peak{0};
std::atomic<uint32_t> frames{0},samples{0},sentSamples{0},droppedSamples{0},clippedSamples{0};
std::atomic<uint32_t> readErrors{0},lastFrameAt{0};
std::atomic<int32_t> lastError{ESP_OK};

void notify() { if (task) xTaskNotifyGive(task); }
bool wanted() { return !muted.load() && (streaming.load() || monitoring.load()); }
void clearMeter() { rms.store(0); rawPeak.store(0); level.store(0); peak.store(0); }

struct CaptureToken {
  uint64_t epoch;
  uint32_t request;
  bool operator==(const CaptureToken& other) const {
    return epoch==other.epoch && request==other.request;
  }
};
CaptureToken captureToken() {
  return {sessionEpoch ? sessionEpoch() : 0,requestGeneration.load()};
}
bool current(const CaptureToken& token) { return wanted() && token==captureToken(); }

bool codecWrite(uint8_t reg,uint8_t value) {
  return M5.In_I2C.writeRegister8(CodecAddress,reg,value,400000);
}
bool codecEnable() {
  // Tab5 ES7210 configuration from M5Unified::_microphone_enabled_cb_tab5
  // (M5Stack, MIT). The ADC is a slave of the existing 48 kHz speaker clock.
  static constexpr uint8_t values[][2]={
    {0x00,0xFF},{0x00,0x41},{0x01,0x1F},{0x06,0x00},{0x07,0x20},{0x08,0x10},
    {0x09,0x30},{0x0A,0x30},{0x20,0x0A},{0x21,0x2A},{0x22,0x0A},{0x23,0x2A},
    {0x02,0xC1},{0x04,0x01},{0x05,0x00},{0x11,0x60},{0x40,0x42},
    {0x41,0x70},{0x42,0x70},{0x43,0x1B},{0x44,0x1B},{0x45,0x00},{0x46,0x00},
    {0x47,0x00},{0x48,0x00},{0x49,0x00},{0x4A,0x00},{0x4B,0x00},{0x4C,0xFF},
    {0x01,0x14}
  };
  for (const auto& value:values) if (!codecWrite(value[0],value[1])) return false;
  return true;
}
bool codecDisable() {
  const bool ok=codecWrite(0x00,0xFF);
  if (!ok) lastError.store(ESP_FAIL);
  return ok;
}
uint8_t meter(uint16_t amplitude) {
  if (!amplitude) return 0;
  const float decibels=20.f*log10f(float(amplitude)/32768.f);
  return uint8_t(std::max(0.f,std::min(100.f,(decibels+60.f)*(100.f/60.f))));
}
void worker(void*) {
  int16_t input[InputFrames*2];
  int16_t pcm[InputFrames/3];
  MicProcessing::Decimator decimator;
  bool running=false;
  CaptureToken runningToken{};
  unsigned settling=0;
  uint32_t retryAt=0;
  auto stop=[&]() {
    active.store(false); clearMeter(); decimator.reset(); settling=0;
    if (!running) return;
    const auto result=i2s_channel_disable(receiver);
    if (result!=ESP_OK) {
      lastError.store(result);
      ulTaskNotifyTake(pdTRUE,pdMS_TO_TICKS(20));
      return;
    }
    running=false;
    codecDisable();
  };
  for (;;) {
    if (!wanted()) {
      stop();
      ulTaskNotifyTake(pdTRUE,pdMS_TO_TICKS(20));
      continue;
    }
    // A receiver may close and reopen between worker iterations. Restart RX even
    // when wanted() stayed true so no queued DMA or DSP history crosses sessions.
    if (running && !(runningToken==captureToken())) { stop(); continue; }
    if (!running) {
      if (retryAt && int32_t(millis()-retryAt)<0) {
        ulTaskNotifyTake(pdTRUE,pdMS_TO_TICKS(20)); continue;
      }
      const CaptureToken startingToken=captureToken();
      if (!codecEnable()) {
        lastError.store(ESP_FAIL); codecDisable(); retryAt=millis()+1000;
        continue;
      }
      const auto result=i2s_channel_enable(receiver);
      if (result!=ESP_OK) {
        lastError.store(result); codecDisable(); retryAt=millis()+1000;
        continue;
      }
      // ESP-IDF 5.5.5 disable resets curr_ptr/rw_pos; enable resets the RX queue,
      // DMA and FIFO. These operations affect I2S1 only; I2S0 stays clocked.
      decimator.reset(); running=true; runningToken=startingToken; settling=3; retryAt=0;
      lastError.store(ESP_OK); active.store(true);
    }
    const CaptureToken blockToken=captureToken();
    if (!(blockToken==runningToken) || !current(blockToken)) { stop(); continue; }
    size_t received=0;
    const auto result=i2s_channel_read(receiver,input,sizeof(input),&received,20);
    if (result!=ESP_OK || received%4) {
      readErrors.fetch_add(1); lastError.store(result==ESP_OK ? ESP_ERR_INVALID_SIZE : result);
    }
    if (!current(blockToken)) { stop(); continue; }
    const size_t count=received/(2*sizeof(int16_t));
    if (!count) continue;
    frames.fetch_add(count);
    // Starting the ADC can leave a short transient. Never send old/settling PCM.
    if (settling) { --settling; continue; }
    size_t produced=0;
    const auto currentGain=gain.load();
    const uint32_t clippedBefore=decimator.clipped();
    for (size_t i=0;i<count;++i) {
      int16_t value=0;
      if (decimator.push(input[2*i],input[2*i+1],currentGain,value)) pcm[produced++]=value;
    }
    clippedSamples.fetch_add(decimator.clipped()-clippedBefore);
    if (!produced) continue;
    if (!current(blockToken)) { stop(); continue; }
    uint64_t squares=0;
    uint16_t maximum=0;
    for (size_t i=0;i<produced;++i) {
      const int32_t value=pcm[i];
      const uint16_t magnitude=value<0 ? uint16_t(-value) : uint16_t(value);
      maximum=std::max(maximum,magnitude);
      squares+=int64_t(value)*value;
    }
    const uint16_t average=uint16_t(sqrt(double(squares)/produced));
    rms.store(average); rawPeak.store(maximum);
    level.store(meter(average)); peak.store(meter(maximum));
    samples.fetch_add(produced); lastFrameAt.store(millis());
    if (streaming.load() && !muted.load() && output) {
      if (!current(blockToken)) { stop(); continue; }
      const size_t accepted=std::min(produced,output(pcm,produced,blockToken.epoch));
      sentSamples.fetch_add(accepted); droppedSamples.fetch_add(produced-accepted);
    }
  }
}
}

bool begin(Sink sink,Epoch epoch) {
  if (available.load()) return true;
  const auto speaker=M5.Speaker.config();
  // Do not let a different clock/pin configuration silently create wrong PCM.
  if (!M5.Speaker.isRunning() || speaker.sample_rate!=InputRate
      || speaker.pin_mck!=30 || speaker.pin_bck!=27 || speaker.pin_ws!=29
      || speaker.pin_data_out!=26 || !M5.In_I2C.scanID(CodecAddress)) {
    lastError.store(ESP_ERR_INVALID_STATE); return false;
  }
  if (!codecEnable()) { codecDisable(); lastError.store(ESP_FAIL); return false; }
  if (!codecDisable()) return false; // Remain in reset until host capture or monitoring.
  i2s_chan_config_t channel=I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1,I2S_ROLE_SLAVE);
  channel.dma_desc_num=6; channel.dma_frame_num=InputFrames;
  auto result=i2s_new_channel(&channel,nullptr,&receiver);
  if (result!=ESP_OK) { lastError.store(result); return false; }
  i2s_std_config_t config{};
  config.clk_cfg=I2S_STD_CLK_DEFAULT_CONFIG(InputRate);
  config.slot_cfg=I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,I2S_SLOT_MODE_STEREO);
  // Slave GPIO inputs preserve the I2S0 output matrix. Never route another MCLK.
  config.gpio_cfg.mclk=GPIO_NUM_NC;
  config.gpio_cfg.bclk=GPIO_NUM_27; config.gpio_cfg.ws=GPIO_NUM_29;
  config.gpio_cfg.dout=GPIO_NUM_NC; config.gpio_cfg.din=GPIO_NUM_28;
  result=i2s_channel_init_std_mode(receiver,&config);
  if (result!=ESP_OK) {
    i2s_del_channel(receiver); receiver=nullptr; lastError.store(result); return false;
  }
  output=sink; sessionEpoch=epoch;
  if (xTaskCreatePinnedToCore(worker,"tab5-mic",6144,nullptr,2,&task,0)!=pdPASS) {
    i2s_del_channel(receiver); receiver=nullptr; lastError.store(ESP_ERR_NO_MEM); return false;
  }
  available.store(true); lastError.store(ESP_OK); return true;
}
void setStreaming(bool value) {
  if (streaming.exchange(value)!=value) { requestGeneration.fetch_add(1); notify(); }
}
void setMonitoring(bool value) {
  if (monitoring.exchange(value)!=value) {
    // Opening/closing the local meter must not interrupt an existing host stream.
    if (!streaming.load()) requestGeneration.fetch_add(1);
    notify();
  }
}
void setMuted(bool value) {
  if (muted.exchange(value)!=value) {
    requestGeneration.fetch_add(1); if (value) clearMeter(); notify();
  }
}
void setGain(uint16_t value) { gain.store(std::min<uint16_t>(200,value)); }
Status status() {
  Status value;
  value.available=available.load(); value.active=active.load(); value.muted=muted.load();
  value.streaming=streaming.load(); value.monitoring=monitoring.load(); value.gain=gain.load();
  value.level=level.load(); value.peak=peak.load(); value.rms=rms.load(); value.rawPeak=rawPeak.load();
  value.frames=frames.load(); value.samples=samples.load(); value.sentSamples=sentSamples.load();
  value.droppedSamples=droppedSamples.load(); value.clippedSamples=clippedSamples.load();
  value.readErrors=readErrors.load(); value.lastFrameAt=lastFrameAt.load(); value.lastError=lastError.load();
  if (value.muted) { value.level=value.peak=0; value.rms=value.rawPeak=0; }
  return value;
}
}
