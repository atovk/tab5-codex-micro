#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

namespace MicProcessing {

// 48 kHz stereo input -> 16 kHz mono. The history and decimation phase stay
// alive between DMA blocks. No allocation or floating-point work is needed.
class Decimator {
 public:
  void reset() {
    history_.fill(0);
    next_=0;
    phase_=0;
    previous_=0;
    highpass_=0;
    clipped_=0;
  }

  bool push(int16_t left,int16_t right,uint16_t gain,int16_t& output) {
    const int32_t mono=(int32_t(left)+int32_t(right))/2;
    // One-pole DC blocker, approximately 75 Hz at the 48 kHz input rate.
    // Division truncates toward zero, so a silent tail cannot retain a DC
    // offset through negative signed right-shift rounding.
    // Keep the feedback's fractional bits: rounding it to whole PCM samples
    // at 48 kHz would introduce a small DC bias for some periodic inputs.
    highpass_=int64_t(mono-previous_)*32768+(highpass_*32446)/32768;
    previous_=mono;
    history_[next_]=int32_t(roundedDivide(highpass_,32768));
    if (++next_==history_.size()) next_=0;
    if (++phase_<3) return false;
    phase_=0;

    // 31-tap Blackman-windowed sinc, 6 kHz cutoff at 48 kHz, normalized
    // to unity DC gain in Q15. It rejects 10 kHz by approximately 60 dB
    // before that input can alias into the 16 kHz speech stream.
    int64_t filtered=0;
    size_t index=next_;
    for (const int16_t coefficient:coefficients_) {
      if (index==0) index=history_.size();
      filtered+=int64_t(history_[--index])*coefficient;
    }
    int64_t value=roundedDivide(filtered,32768);
    if (gain>200) gain=200;
    value=roundedDivide(value*gain,100);
    if (value>32767) { value=32767; ++clipped_; }
    else if (value<-32768) { value=-32768; ++clipped_; }
    output=int16_t(value);
    return true;
  }

  uint32_t clipped() const { return clipped_; }

 private:
  static int64_t roundedDivide(int64_t value,int64_t divisor) {
    return (value+(value>=0 ? divisor/2 : -divisor/2))/divisor;
  }
  inline static constexpr std::array<int16_t,31> coefficients_{{
    0,-3,-10,0,52,136,164,0,-415,-886,-929,0,2087,4851,7242,8190,
    7242,4851,2087,0,-929,-886,-415,0,164,136,52,0,-10,-3,0
  }};
  std::array<int32_t,31> history_{};
  size_t next_=0;
  uint8_t phase_=0;
  int32_t previous_=0;
  int64_t highpass_=0;
  uint32_t clipped_=0;
};
}
