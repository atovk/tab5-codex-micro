#pragma once
#include <algorithm>
#include <cstdint>
#include <cstring>
#include <vector>

namespace lgfx {
constexpr int rgb565_2Byte = 16;
struct swap565_t { uint16_t raw = 0; explicit swap565_t(uint16_t v = 0) : raw(v) {} };
}

// Deliberately small host surface; only the material helper's 16-bit API.
class M5Canvas {
 public:
  M5Canvas(int w, int h, bool direct = true)
      : pixels(w * h), w_(w), h_(h), cw_(w), ch_(h), direct_(direct) {}
  int width() const { return w_; }
  int height() const { return h_; }
  int getRotation() const { return direct_ ? 0 : 1; }
  int getColorDepth() const { return lgfx::rgb565_2Byte; }
  void* getBuffer() { return pixels.data(); }
  void getClipRect(int32_t* x, int32_t* y, int32_t* w, int32_t* h) const {
    *x = cx_; *y = cy_; *w = cw_; *h = ch_;
  }
  void setClipRect(int x, int y, int w, int h) { cx_=x; cy_=y; cw_=w; ch_=h; }
  void readRect(int x, int y, int w, int h, lgfx::swap565_t* out) {
    reads += size_t(w) * h;
    for (int row=0; row<h; ++row)
      std::memcpy(out+row*w, pixels.data()+(y+row)*w_+x, size_t(w)*2);
  }
  void pushImage(int x, int y, int w, int h, const lgfx::swap565_t* source) {
    for (int row=0; row<h; ++row)
      std::memcpy(pixels.data()+(y+row)*w_+x, source+row*w, size_t(w)*2);
  }
  void fillRect(int x, int y, int w, int h, uint16_t value) {
    value = uint16_t((value >> 8) | (value << 8));
    for (int row=std::max(y, std::max(0,cy_)); row<std::min(y+h,std::min(h_,cy_+ch_)); ++row)
      for (int col=std::max(x, std::max(0,cx_)); col<std::min(x+w,std::min(w_,cx_+cw_)); ++col)
        pixels[row*w_+col] = value;
  }
  std::vector<uint16_t> pixels;
  size_t reads = 0;
 private:
  int w_, h_, cx_=0, cy_=0, cw_, ch_;
  bool direct_;
};
