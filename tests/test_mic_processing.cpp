#include "mic_processing.h"

#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>

namespace {
constexpr double pi=3.14159265358979323846;

std::vector<int16_t> tone(double frequency,double amplitude,uint16_t gain=100) {
  MicProcessing::Decimator filter;
  std::vector<int16_t> output;
  for (int frame=0;frame<48000;++frame) {
    const int16_t value=int16_t(std::lround(amplitude*std::sin(2*pi*frequency*frame/48000)));
    int16_t sample=0;
    if (filter.push(value,value,gain,sample)) output.push_back(sample);
  }
  assert(output.size()==16000);
  return output;
}

double rms(const std::vector<int16_t>& values) {
  // Discard the start-up transient; score the steady-state response.
  double sum=0;
  for (size_t i=1000;i<values.size();++i) sum+=double(values[i])*values[i];
  return std::sqrt(sum/(values.size()-1000));
}

void silenceAndPhase() {
  MicProcessing::Decimator filter;
  size_t frames=0,samples=0;
  // Deliberately split work at boundaries which are not multiples of three.
  for (size_t chunk:{1u,127u,480u,5u,512u,2u}) {
    for (size_t i=0;i<chunk;++i) {
      int16_t sample=123;
      const bool ready=filter.push(0,0,100,sample);
      ++frames;
      assert(ready==(frames%3==0));
      if (ready) { ++samples; assert(sample==0); }
    }
    assert(samples==frames/3);
  }
  assert(filter.clipped()==0);
  filter.reset();
  int16_t sample=0;
  assert(!filter.push(0,0,100,sample));
  assert(!filter.push(0,0,100,sample));
  assert(filter.push(0,0,100,sample));
  assert(sample==0);
}

void dcAndStereo() {
  MicProcessing::Decimator dc,opposite;
  int16_t sample=0;
  for (int i=0;i<48000;++i) {
    if (dc.push(20000,20000,100,sample) && i>24000) assert(std::abs(int(sample))<=1);
    const int16_t value=int16_t(30000*std::sin(2*pi*731*i/48000));
    if (opposite.push(value,int16_t(-value),200,sample)) assert(sample==0);
  }
  assert(dc.clipped()==0 && opposite.clipped()==0);
}

void gainAndMute() {
  const auto reference=tone(1000,6000,100);
  const auto amplified=tone(1000,6000,200);
  const auto clamped=tone(1000,6000,65535);
  const auto quiet=tone(1000,6000,50);
  const auto muted=tone(1000,30000,0);
  for (size_t i=0;i<reference.size();++i) {
    assert(amplified[i]==2*reference[i]);
    assert(clamped[i]==amplified[i]);
    assert(std::abs(2*int(quiet[i])-reference[i])<=1);
    assert(muted[i]==0);
  }
  assert(std::abs(rms(amplified)/rms(reference)-2)<0.001);
}

void saturationAndReset() {
  MicProcessing::Decimator filter;
  size_t positive=0,negative=0,clipped=0;
  for (int frame=0;frame<48000;++frame) {
    const int16_t value=int16_t(std::lround(30000*std::sin(2*pi*1000*frame/48000)));
    int16_t sample=0;
    if (filter.push(value,value,200,sample)) {
      positive+=sample==32767;
      negative+=sample==-32768;
      clipped+=(sample==32767 || sample==-32768);
    }
  }
  assert(positive>1000 && negative>1000);
  assert(filter.clipped()==clipped);
  filter.reset();
  assert(filter.clipped()==0);
  int16_t sample=0;
  for (int i=0;i<96;++i) if (filter.push(0,0,100,sample)) assert(sample==0);

  // Full-range sign changes exercise the wider DC-blocker and MAC arithmetic.
  for (int i=0;i<48000;++i) {
    const int16_t left=(i%37)<18 ? INT16_MIN : INT16_MAX;
    filter.push(left,left,200,sample);
  }
  assert(filter.clipped()>0);
}

void response() {
  const double pass=rms(tone(1000,12000));
  const double expected=12000/std::sqrt(2.0);
  assert(pass>expected*0.98 && pass<expected*1.02);
  for (double hz:{10000.,12000.,16000.,20000.}) {
    const double stop=rms(tone(hz,12000));
    assert(stop/pass<0.003); // At least 50 dB rejection after integer rounding.
  }
  const double low=rms(tone(10,12000));
  assert(low/pass<0.15);
  std::printf("passband RMS %.2f; 10 kHz alias RMS %.2f\n",pass,rms(tone(10000,12000)));
}
}

int main(int argc,char** argv) {
  assert(argc==2);
  if (!std::strcmp(argv[1],"phase")) silenceAndPhase();
  else if (!std::strcmp(argv[1],"dc")) dcAndStereo();
  else if (!std::strcmp(argv[1],"gain")) gainAndMute();
  else if (!std::strcmp(argv[1],"clip")) saturationAndReset();
  else if (!std::strcmp(argv[1],"response")) response();
  else return 2;
  std::puts("microphone processing contract passed");
}
