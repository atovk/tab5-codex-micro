"""Scheduling, wraparound and failed-NVS retry contracts without hardware."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("c++")


@unittest.skipUnless(COMPILER, "host C++ compiler is unavailable")
class RenderServiceTests(unittest.TestCase):
    def test_game_damage_clear_services_input_without_changing_pixels(self):
        source = (ROOT / "src/micro_games.cpp").read_text()
        begin = source.index("  for (size_t i = 0; i < dirtyCount; ++i) {", source.index("bool render("))
        end = source.index("\n  c.clearClipRect();", begin)
        # Exercise the production damage loop, including its choice of clear
        # primitive. Testing solid565 alone would miss the game's bypass.
        harness = r'''
#include "material_gradient.h"
#include <cassert>
#include <cstdio>
#include <vector>
struct Rect { int16_t x, y, w, h; };
struct Palette { uint32_t background = 0xA4B2C5; };
const Palette& palette() { static Palette value; return value; }
uint16_t color(uint32_t c) {
  return ((c >> 8) & 0xF800) | ((c >> 5) & 0x07E0) | ((c >> 3) & 0x001F);
}
std::vector<Rect> dirty;
size_t dirtyCount;
Rect drawingClip;
M5Canvas* active;
std::vector<uint16_t> previous;
unsigned checkpoints = 0, scenes = 0;
namespace RenderService {
void checkpoint() {
  size_t changed = 0;
  for (size_t i = 0; i < previous.size(); ++i)
    changed += previous[i] != active->pixels[i];
  // The complete 1280x624 clear must yield during bounded row writes,
  // with the final short strip preserving the original damage rectangle.
  assert(changed > 0 && changed <= 1280 * 32);
  previous = active->pixels;
  ++checkpoints;
}
}
void drawScene(M5Canvas& c, uint32_t) {
  int32_t x, y, w, h;
  c.getClipRect(&x, &y, &w, &h);
  assert(x == drawingClip.x && y == drawingClip.y);
  assert(w == drawingClip.w && h == drawingClip.h);
  ++scenes;
}
void renderDamage(M5Canvas& c, uint32_t now) {
''' + source[begin:end] + r'''
}
int main() {
  for (bool direct : {false, true}) {
    for (bool full : {false, true}) {
      M5Canvas actual(1280, 720, direct), expected(1280, 720, direct);
      actual.fillRect(0, 0, 1280, 720, 0x1234);
      expected.fillRect(0, 0, 1280, 720, 0x1234);
      dirty = full ? std::vector<Rect>{{0, 96, 1280, 624}}
                   : std::vector<Rect>{{32, 128, 320, 64}, {64, 224, 288, 16}};
      dirtyCount = dirty.size();
      active = &actual; previous = actual.pixels;
      checkpoints = scenes = 0;
      for (const auto& r : dirty) {
        expected.setClipRect(r.x, r.y, r.w, r.h);
        expected.fillRect(r.x, r.y, r.w, r.h, color(palette().background));
      }
      renderDamage(actual, 123);
      assert(actual.pixels == expected.pixels);
      assert(checkpoints == (full ? 20u : 3u));
      assert(scenes == dirtyCount);
    }
  }
  std::puts("game background pixels and cooperative checkpoints passed");
}
'''
        with tempfile.TemporaryDirectory(prefix="tab5-game-render-") as directory:
            path = Path(directory)
            cpp = path / "game_render.cpp"
            binary = path / "game_render"
            cpp.write_text(harness)
            result = subprocess.run(
                [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
                 "-Itests/stubs/material", "-Iinclude", str(cpp), "-o", str(binary)],
                cwd=ROOT, text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(binary)], text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_scheduling_and_retry(self):
        with tempfile.TemporaryDirectory(prefix="tab5-render-") as directory:
            binary = Path(directory) / "test_render_service"
            result = subprocess.run(
                [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
                 "-Iinclude", "tests/test_render_service.cpp", "-o", str(binary)],
                cwd=ROOT, text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(binary)], text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_shadow_occlusion_preserves_every_pixel(self):
        with tempfile.TemporaryDirectory(prefix="tab5-material-") as directory:
            binary = Path(directory) / "test_material_occlusion"
            result = subprocess.run(
                [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
                 "-Itests/stubs/material", "-Iinclude", "tests/test_material_occlusion.cpp",
                 "-o", str(binary)], cwd=ROOT, text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(binary)], text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("51 material occlusion pixel comparisons passed", result.stdout)
