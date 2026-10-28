#include "material_gradient.h"
#include <cassert>
#include <cstdio>

static unsigned checkpoints = 0;
namespace RenderService { void checkpoint() { ++checkpoints; } }

int main() {
  unsigned cases = 0;
  for (bool direct : {false, true})
    for (bool clipped : {false, true})
      for (bool dark : {false, true})
        for (bool pressed : {false, true})
          for (float shift : {0.f, .25f, .75f}) {
            M5Canvas original(300,280,direct), optimized(300,280,direct);
            for (size_t i=0; i<original.pixels.size(); ++i)
              original.pixels[i] = optimized.pixels[i] = uint16_t(i*73+19);
            if (clipped) {
              original.setClipRect(27,35,207,181);
              optimized.setClipRect(27,35,207,181);
            }
            const float x=25+shift, y=31+shift;
            const int drop=pressed ? 3 : 0;
            MaterialGradient::OpaqueCover face(x+3,y+2+drop,224,222-drop,19);
            const uint32_t shadow=dark ? 0x05080D : 0x728096;
            for (M5Canvas* target : {&original,&optimized}) {
              MaterialGradient::shadowRoundRect(*target,x+8,y+8,214,214,18,7,
                  pressed?30:58,shadow,target==&optimized ? &face : nullptr);
              MaterialGradient::bevelRoundRect565(*target,x+3,y+2+drop,224,222-drop,19,
                  dark?0x39a7:0xffff,dark?0x3186:0xf7be,10);
            }
            assert(original.pixels==optimized.pixels);
            if (!direct) assert(optimized.reads < original.reads/3);
            ++cases;
          }
  for (float radius : {29.f,48.f,106.f}) {
    M5Canvas original(250,250), optimized(250,250);
    const MaterialGradient::OpaqueCover face(125-radius,125-radius,radius*2,radius*2,radius);
    for (M5Canvas* target : {&original,&optimized}) {
      target->fillRect(0,0,250,250,0xdefb);
      MaterialGradient::shadowCircle(*target,125,129,radius,7,100,0x728096,
          target==&optimized ? &face : nullptr);
      MaterialGradient::metalCircle565(*target,125,125,radius,0xbdf7,0xffff,0xa514,3);
    }
    assert(original.pixels==optimized.pixels);
    ++cases;
  }
  assert(checkpoints>100);
  std::printf("%u material occlusion pixel comparisons passed\n",cases);
}
