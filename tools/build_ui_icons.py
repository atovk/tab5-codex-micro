#!/usr/bin/env python3
"""Build original, supersampled vector icon alpha masks for the Tab5 UI."""
from __future__ import annotations

import argparse
from pathlib import Path
import math

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SCALE = 8
SIZES = (16, 20, 24, 28, 32, 40, 44, 48)
ICONS = ("Logo", "Lightning", "Check", "Close", "Fork", "Microphone", "Send",
         "Sliders", "Sun", "Moon", "ChevronUp", "ChevronDown", "ChevronLeft", "ChevronRight",
         "SpeakerOn", "SpeakerOff", "Gear")


def vector(name: str) -> Image.Image:
    image = Image.new("L", (64*SCALE, 64*SCALE))
    draw = ImageDraw.Draw(image)

    def points(values):
        return [(round(x*SCALE), round(y*SCALE)) for x, y in values]

    def circle(x, y, radius, fill=255):
        draw.ellipse(((x-radius)*SCALE, (y-radius)*SCALE,
                      (x+radius)*SCALE, (y+radius)*SCALE), fill=fill)

    def line(values, width=3.5):
        draw.line(points(values), fill=255, width=round(width*SCALE), joint="curve")
        for x, y in values:
            circle(x, y, width/2)

    def arc(box, start, end, width=3.5):
        draw.arc(tuple(round(v*SCALE) for v in box), start, end,
                 fill=255, width=round(width*SCALE))
        cx, cy = (box[0]+box[2])/2, (box[1]+box[3])/2
        rx, ry = (box[2]-box[0]-width)/2, (box[3]-box[1]-width)/2
        for angle in (start, end):
            a = math.radians(angle)
            circle(cx+rx*math.cos(a), cy+ry*math.sin(a), width/2)

    if name == "Logo":
        # A balanced terminal/cursor symbol, drawn as geometry rather than text.
        line(((11, 12), (30, 30), (11, 48)), 6)
        line(((34, 48), (55, 48)), 6)
    elif name == "Lightning":
        draw.polygon(points(((36, 7), (15, 35), (29, 35), (25, 57),
                             (49, 27), (35, 27))), fill=255)
    elif name == "Check":
        line(((12, 32), (26, 46), (52, 18)), 3.6)
    elif name == "Close":
        line(((16, 16), (48, 48)), 3.1)
        line(((48, 16), (16, 48)), 3.1)
    elif name == "Fork":
        line(((23, 15), (23, 49)), 4.2)
        line(((23, 35), (42, 24), (42, 15)), 4.2)
        for x, y in ((23, 12), (23, 52), (42, 12)):
            circle(x, y, 5)
            circle(x, y, 1.7, 0)
    elif name == "Microphone":
        draw.rounded_rectangle((23*SCALE, 8*SCALE, 41*SCALE, 38*SCALE),
                               radius=9*SCALE, outline=255, width=round(4.2*SCALE))
        arc((15, 20, 49, 49), 0, 180, 4.2)
        line(((32, 48), (32, 56)), 4.2)
        line(((23, 56), (41, 56)), 4.2)
    elif name == "Send":
        line(((8, 25), (55, 8), (39, 56), (29, 35), (8, 25)), 4.2)
        line(((29, 35), (55, 8)), 4.2)
    elif name == "Sliders":
        for y, knob in ((16, 25), (32, 41), (48, 23)):
            line(((10, y), (54, y)), 4.1)
            circle(knob, y, 6)
            circle(knob, y, 2.6, 0)
    elif name == "Sun":
        draw.ellipse((22*SCALE, 22*SCALE, 42*SCALE, 42*SCALE),
                     outline=255, width=round(4.2*SCALE))
        for index in range(8):
            angle = index*math.tau/8
            line(((32+19*math.cos(angle), 32+19*math.sin(angle)),
                  (32+25*math.cos(angle), 32+25*math.sin(angle))), 4.2)
    elif name == "Moon":
        circle(30, 33, 23)
        circle(41, 23, 22, 0)
    elif name in ("SpeakerOn", "SpeakerOff"):
        line(((10, 25), (22, 25), (35, 14), (35, 50),
              (22, 39), (10, 39), (10, 25)), 3.1)
        if name == "SpeakerOn":
            arc((14, 17, 44, 47), -42, 42, 3.1)
            arc((6, 9, 52, 55), -42, 42, 3.1)
        else:
            line(((43, 26), (55, 38)), 3.1)
            line(((55, 26), (43, 38)), 3.1)
    elif name == "Gear":
        teeth = []
        for index in range(8):
            for offset, radius in ((-22.5, 21), (-12.5, 26), (12.5, 26), (22.5, 21)):
                angle = math.radians(index*45+offset)
                teeth.append((32+radius*math.cos(angle), 32+radius*math.sin(angle)))
        line((*teeth, teeth[0]), 3.1)
        draw.ellipse((23.5*SCALE, 23.5*SCALE, 40.5*SCALE, 40.5*SCALE),
                     outline=255, width=round(3.1*SCALE))
    else:
        values = {"ChevronUp": ((15, 40), (32, 23), (49, 40)),
                  "ChevronDown": ((15, 24), (32, 41), (49, 24)),
                  "ChevronLeft": ((40, 15), (23, 32), (40, 49)),
                  "ChevronRight": ((24, 15), (41, 32), (24, 49))}[name]
        line(values, 3.1)
    return image


def mask(name: str, size: int) -> Image.Image:
    full = vector(name)
    cropped = full.crop(full.getbbox())
    ratio = (size-2)/max(cropped.size)
    result = cropped.resize((max(1, round(cropped.width*ratio)),
                             max(1, round(cropped.height*ratio))), Image.Resampling.LANCZOS)
    return result.crop(result.getbbox())


RUNTIME = r'''
// Draw at the center of the actual ink, independent of a nominal SVG viewbox.
inline void draw(M5Canvas& canvas, Icon icon, float cx, float cy, int size,
                 uint32_t ink, uint8_t opacity = 255) {
  const unsigned index = static_cast<unsigned>(icon);
  if (index >= static_cast<unsigned>(Icon::Count) || !opacity) return;
  unsigned selected = 0;
  for (unsigned i = 1; i < detail::sizeCount; ++i)
    if (std::abs(size - detail::sizes[i]) < std::abs(size - detail::sizes[selected])) selected = i;
  const auto& mask = detail::masks[index][selected];
  const int x0 = int(std::lround(cx - (mask.w - 1) * .5f));
  const int y0 = int(std::lround(cy - (mask.h - 1) * .5f));
  int32_t clipX, clipY, clipW, clipH;
  canvas.getClipRect(&clipX, &clipY, &clipW, &clipH);
  const int left = std::max<int32_t>(std::max(0, x0), clipX);
  const int top = std::max<int32_t>(std::max(0, y0), clipY);
  const int right = std::min<int32_t>(std::min<int32_t>(canvas.width(), x0 + mask.w), clipX + clipW);
  const int bottom = std::min<int32_t>(std::min<int32_t>(canvas.height(), y0 + mask.h), clipY + clipH);
  auto* pixels = canvas.getRotation() == 0 && canvas.getColorDepth() == lgfx::rgb565_2Byte
      ? static_cast<uint16_t*>(canvas.getBuffer()) : nullptr;
  const auto foreground = MaterialGradient::detail::levels(ink);
  for (int y = top; y < bottom; ++y) {
    for (int x = left; x < right; ++x) {
      const unsigned alpha = (unsigned(mask.alpha[(y-y0)*mask.w + x-x0])*opacity+127)/255;
      if (!alpha) continue;
      if (pixels) {
        auto& output = pixels[y*canvas.width()+x];
        const uint16_t previous = uint16_t((output >> 8) | (output << 8));
        MaterialGradient::detail::Channels base {
          int32_t((previous >> 11) & 31) << 8,
          int32_t((previous >> 5) & 63) << 8,
          int32_t(previous & 31) << 8};
        base.r += (foreground.r-base.r)*int(alpha)/255;
        base.g += (foreground.g-base.g)*int(alpha)/255;
        base.b += (foreground.b-base.b)*int(alpha)/255;
        const uint16_t pixel = MaterialGradient::detail::pixelAt(base, x, y);
        output = uint16_t((pixel >> 8) | (pixel << 8));
      } else {
        const auto old = canvas.readPixelRGB(x,y);
        const auto mix = [alpha](unsigned a, unsigned b) { return (a*(255-alpha)+b*alpha+127)/255; };
        canvas.drawPixel(x,y, (mix(old.r,(ink>>16)&255)<<16)
            | (mix(old.g,(ink>>8)&255)<<8) | mix(old.b,ink&255));
      }
    }
  }
}
} // namespace UiIcons
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--header-only", action="store_true",
                        help="Generate the runtime header without updating the preview artifact")
    parser.add_argument("--check", action="store_true",
                        help="Verify the checked-in header without writing any files")
    args = parser.parse_args()
    lines = ["// Original Tab5 vector icons; generated by tools/build_ui_icons.py.",
             "// Eight-times supersampled alpha, rounded caps, optical ink centering.",
             "#pragma once", '#include "material_gradient.h"', "#include <cmath>",
             "namespace UiIcons {", "enum class Icon : uint8_t { " + ", ".join(ICONS) + ", Count };",
             "namespace detail {", "struct Mask { uint8_t w, h; const uint8_t* alpha; };",
             "constexpr int sizes[] = {"+",".join(map(str, SIZES))+"};",
             "constexpr unsigned sizeCount = sizeof(sizes)/sizeof(sizes[0]);"]
    entries = []
    total = 0
    for name in ICONS:
        row = []
        for size in SIZES:
            item = mask(name, size)
            data = item.tobytes()
            total += len(data)
            variable = f"mask{name}{size}"
            lines.append(f"inline const uint8_t {variable}[] PROGMEM = {{")
            lines.extend("  "+",".join(f"0x{v:02x}" for v in data[i:i+24])+"," for i in range(0,len(data),24))
            lines.append("};")
            row.append(f"{{{item.width},{item.height},{variable}}}")
        entries.append("{"+",".join(row)+"}")
    lines.extend(["inline const Mask masks[][sizeCount] = {", ",\n".join(entries), "};", "} // namespace detail", RUNTIME])
    header = ROOT / "include/ui_icons.h"
    source = "\n".join(lines).rstrip()+"\n"
    if args.check:
        if not header.exists() or header.read_text() != source:
            raise SystemExit("Icon header is stale; run tools/build_ui_icons.py --header-only")
        print(f"Verified {len(ICONS)} icons / {len(SIZES)} sizes / {total} alpha bytes")
        return
    header.write_text(source)
    if not args.header_only:
        theme_height = 100*math.ceil(len(ICONS)/7)
        preview = Image.new("RGB", (840, 2*theme_height), "#ECEDEF")
        painter = ImageDraw.Draw(preview)
        for theme in range(2):
            origin = theme*theme_height
            if theme:
                painter.rectangle((0,origin,840,origin+theme_height),fill="#202329")
            for index,name in enumerate(ICONS):
                x,y = index%7*120+60,index//7*100+40+origin
                item = mask(name,40)
                preview.paste("#242932" if not theme else "#EEF0F4",
                              (x-item.width//2,y-item.height//2),item)
                painter.text((x-36,y+30),name,fill="#242932" if not theme else "#EEF0F4")
        target = ROOT / "artifacts/ui-icons-preview.png"
        preview.save(target)
    print(f"Generated {len(ICONS)} icons / {len(SIZES)} sizes / {total} alpha bytes")


if __name__ == "__main__":
    main()
