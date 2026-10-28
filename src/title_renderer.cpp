#include "title_renderer.h"

#include "title_layout.h"
#include "title_scroll.h"
#include "ui_text.h"
#include "render_service.h"
#include <esp_heap_caps.h>
#include <algorithm>
#include <cstdlib>
#include <cstring>

namespace TitleRenderer {
namespace {
uint16_t swapped(uint16_t value) { return uint16_t((value>>8)|(value<<8)); }

int32_t measure(const char* text,void* context) {
  return UiText::bounds(static_cast<const lgfx::IFont*>(context),text).w;
}

void normalize(char (&output)[192],const char* source,const lgfx::IFont* font) {
  size_t written=0;
  bool previousSpace=true;
  const char* cursor=source;
  while (*cursor) {
    RenderService::checkpoint();
    const char* start=cursor;
    uint32_t code=0;
    bool valid=true;
    if (!UiText::detail::next(cursor,code,valid)) break;
    if (code==0x200D || (code>=0xFE00 && code<=0xFE0F)) continue;
    const bool space=valid && (code<0x20 || code==0x7F || code==0xA0 || code==0x3000 || code==' ');
    lgfx::FontMetrics metrics{};
    font->getDefaultMetric(&metrics);
    const bool supported=valid && code<=0xFFFF && font->updateFontMetric(&metrics,uint16_t(code));
    if (space && previousSpace) continue;
    const char* data=space ? " " : supported ? start : "?";
    const size_t length=space || !supported ? 1 : size_t(cursor-start);
    if (written+length>=sizeof(output)) break;
    memcpy(output+written,data,length);
    written+=length;
    previousSpace=space;
  }
  if (written && output[written-1]==' ') --written;
  output[written]=0;
}

uint16_t foreground(uint32_t rgb) {
  return uint16_t(((((rgb>>16)&255)*31+127)/255<<11)
      | ((((rgb>>8)&255)*63+127)/255<<5) | ((rgb&255)*31+127)/255);
}
} // namespace

Cache::~Cache() { release(); }

void Cache::release() {
  std::free(mask_); std::free(background_);
  mask_=nullptr; background_=nullptr;
  canvas_=nullptr; maskBytes_=0;
  ready_=painted_=pendingClip_=false; lineCount_=0; maskWidth_=0; offset_=0;
}

bool Cache::sourceChanged(const char* source,const lgfx::IFont* font,uint16_t width,bool singleLine) const {
  return !attempted_ || font_!=font || width_!=width || singleLine_!=singleLine
      || strncmp(source_,source ? source : "",sizeof(source_)-1)!=0;
}

bool Cache::prepare(const char* source,const lgfx::IFont* font,uint16_t width,bool singleLine) {
  if (!sourceChanged(source,font,width,singleLine)) return ready_;
  release();
  attempted_=true; font_=font; width_=width; singleLine_=singleLine; ++generation_;
  strncpy(source_,source ? source : "",sizeof(source_)-1);
  source_[sizeof(source_)-1]=0;
  if (!font || !width || width>MaxWidth) return false;

  char normalized[192]={};
  normalize(normalized,source_,font);
  if (!*normalized) return false;
  TitleLayout::Layout layout;
  uint16_t rasterWidth=width;
  if (singleLine) {
    const int32_t textWidth=measure(normalized,const_cast<lgfx::IFont*>(font));
    // Bounded by the 191-byte source and native title font in normal use.
    // Reject pathological external font metrics before allocating a raster.
    if (textWidth<=0 || textWidth>8192) return false;
    rasterWidth=uint16_t(std::max<int32_t>(width,textWidth));
    layout.count=1;
  } else if (!TitleLayout::wrap(layout,normalized,width,measure,const_cast<lgfx::IFont*>(font))
      || layout.overflow || !layout.count) return false;
  const uint16_t rows=layout.count>2 ? uint16_t(layout.count+2) : 2;
  const uint16_t height=rows*RowHeight;
  const size_t maskSize=size_t(rasterWidth)*height;
  auto* mask=static_cast<uint8_t*>(heap_caps_malloc(maskSize,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
  auto* background=static_cast<uint16_t*>(heap_caps_malloc(size_t(width)*ViewHeight*2,
      MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
  if (!mask || !background) { std::free(mask); std::free(background); return false; }

  M5Canvas raster;
  raster.setColorDepth(16);
  raster.setPsram(true);
  if (!raster.createSprite(rasterWidth,height)) {
    std::free(mask); std::free(background); return false;
  }
  raster.fillScreen(0);
  char line[192];
  const uint16_t drawRows=layout.count>2 ? rows : layout.count;
  for (uint16_t row=0;row<drawRows;++row) {
    const uint16_t index=row%layout.count;
    if (singleLine) memcpy(line,normalized,sizeof(line));
    else if (!TitleLayout::copyLine(layout,index,line,sizeof(line))) {
      raster.deleteSprite(); std::free(mask); std::free(background); return false;
    }
    const int centerY=layout.count==1 ? ViewHeight/2 : int(row)*RowHeight+RowHeight/2;
    if (layout.count>1) raster.setClipRect(0,int(row)*RowHeight,width,RowHeight);
    UiText::drawCentered(raster,line,rasterWidth/2,centerY,font,0xFFFFFF);
  }
  raster.clearClipRect();
  const auto* pixels=static_cast<const uint16_t*>(raster.getBuffer());
  for (size_t index=0;index<maskSize;++index) {
    if ((index & 2047) == 0) RenderService::checkpoint();
    const unsigned green=(swapped(pixels[index])>>5)&63;
    mask[index]=uint8_t((green*255+31)/63);
  }
  raster.deleteSprite();
  mask_=mask; background_=background; maskBytes_=maskSize; maskWidth_=rasterWidth;
  lineCount_=layout.count; ready_=true;
  return true;
}

uint16_t Cache::normalizedOffset(uint16_t value) const {
  if (singleLine_) return std::min<uint16_t>(value,overflowWidth());
  return lineCount_>2 ? uint16_t(value%(uint16_t(lineCount_)*RowHeight)) : 0;
}

uint16_t Cache::offsetAt(uint32_t elapsed) const {
  return singleLine_ ? TitleScroll::horizontalOffsetAt(elapsed,overflowWidth())
      : TitleScroll::offsetAt(elapsed,lineCount_);
}

uint16_t Cache::maxOffset() const {
  return singleLine_ ? overflowWidth() : lineCount_>2 ? uint16_t(lineCount_*RowHeight-1) : 0;
}

uint32_t Cache::cycleMs() const {
  return singleLine_ ? TitleScroll::horizontalCycleMs(overflowWidth()) : TitleScroll::cycleMs(lineCount_);
}

bool Cache::paint(M5Canvas& canvas,int16_t x,int16_t y,uint32_t rgb,uint16_t offset) {
  if (!ready_ || x<0 || y<0 || int(x)+width_>canvas.width()
      || int(y)+ViewHeight>canvas.height()) return false;
  x_=x; y_=y; ink565_=foreground(rgb); canvas_=&canvas;
  const bool direct=canvas.getRotation()==0 && canvas.getColorDepth()==lgfx::rgb565_2Byte;
  const auto* pixels=direct ? static_cast<const uint16_t*>(canvas.getBuffer()) : nullptr;
  for (uint16_t row=0;row<ViewHeight;++row) {
    auto* destination=background_+size_t(row)*width_;
    if (pixels) memcpy(destination,pixels+size_t(y+row)*canvas.width()+x,size_t(width_)*2);
    else canvas.readRect(x,y+row,width_,1,reinterpret_cast<lgfx::swap565_t*>(destination));
  }
  painted_=true;
  return compose(canvas,normalizedOffset(offset));
}

bool Cache::scroll(M5Canvas& canvas,uint16_t offset) {
  if (!ready_ || !painted_ || canvas_!=&canvas) return false;
  offset=normalizedOffset(offset);
  if (offset==offset_ && !pendingClip_) return false;
  return compose(canvas,offset);
}

bool Cache::compose(M5Canvas& canvas,uint16_t offset) {
  int32_t clipX,clipY,clipW,clipH;
  canvas.getClipRect(&clipX,&clipY,&clipW,&clipH);
  const int left=std::max<int>(x_,std::max<int32_t>(0,clipX));
  const int top=std::max<int>(y_,std::max<int32_t>(0,clipY));
  const int right=std::min<int>(x_+width_,std::min<int32_t>(canvas.width(),clipX+clipW));
  const int bottom=std::min<int>(y_+ViewHeight,std::min<int32_t>(canvas.height(),clipY+clipH));
  offset_=offset;
  pendingClip_=left!=x_ || top!=y_ || right!=x_+width_ || bottom!=y_+ViewHeight;
  if (left>=right || top>=bottom) return false;
  auto* direct=canvas.getRotation()==0 && canvas.getColorDepth()==lgfx::rgb565_2Byte
      ? static_cast<uint16_t*>(canvas.getBuffer()) : nullptr;
  lgfx::swap565_t output[MaxWidth],existing[MaxWidth];
  const unsigned foreR=ink565_>>11,foreG=(ink565_>>5)&63,foreB=ink565_&31;
  bool changed=false;
  const size_t count=right-left;
  for (int y=top;y<bottom;++y) {
    if ((y & 7) == 0) RenderService::checkpoint();
    const size_t local=size_t(y-y_)*width_+left-x_;
    const auto* alpha=singleLine_
        ? mask_+size_t(y-y_)*maskWidth_+left-x_+offset
        : mask_+size_t(offset)*width_+local;
    const auto* background=background_+local;
    for (size_t col=0;col<count;++col) {
      const unsigned coverage=alpha[col];
      uint16_t raw=background[col];
      if (coverage==255) raw=swapped(ink565_);
      else if (coverage) {
        const uint16_t base=swapped(raw);
        const unsigned r=((base>>11)*(255-coverage)+foreR*coverage+127)/255;
        const unsigned g=(((base>>5)&63)*(255-coverage)+foreG*coverage+127)/255;
        const unsigned b=((base&31)*(255-coverage)+foreB*coverage+127)/255;
        raw=swapped(uint16_t((r<<11)|(g<<5)|b));
      }
      output[col].raw=raw;
    }
    auto* destination=direct ? direct+size_t(y)*canvas.width()+left : nullptr;
    if (destination) {
      if (!memcmp(destination,output,count*2)) continue;
      memcpy(destination,output,count*2);
    } else {
      canvas.readRect(left,y,count,1,existing);
      if (!memcmp(existing,output,count*2)) continue;
      canvas.pushImage(left,y,count,1,output);
    }
    changed=true;
  }
  return changed;
}
} // namespace TitleRenderer
