#pragma once

#include <M5GFX.h>
#include <stddef.h>
#include <stdint.h>

// Retained title masks. prepare() may allocate/rasterize only when its content,
// font or width key changes. paint()/scroll() allocate nothing and draw no text.
namespace TitleRenderer {
constexpr uint16_t RowHeight=25;
constexpr uint16_t ViewHeight=50;
constexpr uint16_t MaxWidth=256;

class Cache {
 public:
  Cache()=default;
  ~Cache();
  Cache(const Cache&)=delete;
  Cache& operator=(const Cache&)=delete;

  bool sourceChanged(const char* source,const lgfx::IFont* font,uint16_t width) const;
  // Empty titles and resource/layout failures return false. A failed unchanged
  // key is not repeatedly allocated; the caller may draw its simple fallback.
  bool prepare(const char* source,const lgfx::IFont* font,uint16_t width);
  // Caller must first repaint the complete clean width() x ViewHeight area.
  // Save that background, then compose the title at the requested offset.
  bool paint(M5Canvas& canvas,int16_t x,int16_t y,uint32_t rgb,uint16_t offset=0);
  // Recompose from the saved clean background; caller owns display presents.
  bool scroll(M5Canvas& canvas,uint16_t offset);

  bool ready() const { return ready_; }
  uint8_t lineCount() const { return lineCount_; }
  uint16_t offset() const { return offset_; }
  uint16_t width() const { return width_; }
  uint32_t generation() const { return generation_; }
  size_t bytes() const { return maskBytes_+size_t(width_)*ViewHeight*2*(background_ ? 1 : 0); }

 private:
  void release();
  bool compose(M5Canvas& canvas,uint16_t offset);
  uint16_t normalizedOffset(uint16_t value) const;

  char source_[192]={};
  const lgfx::IFont* font_=nullptr;
  M5Canvas* canvas_=nullptr;
  uint8_t* mask_=nullptr;
  uint16_t* background_=nullptr;
  size_t maskBytes_=0;
  uint32_t generation_=0;
  uint16_t width_=0,offset_=0,ink565_=0;
  int16_t x_=0,y_=0;
  uint8_t lineCount_=0;
  bool attempted_=false,ready_=false,painted_=false,pendingClip_=false;
};
} // namespace TitleRenderer
