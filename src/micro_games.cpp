#include "micro_games.h"
#include "game_lifecycle.h"
#include "material_gradient.h"
#include "ui_font.h"
#include "ui_text.h"
#include "ui_locale.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <freertos/FreeRTOS.h>
#include <freertos/portmacro.h>

namespace MicroGames {
namespace {
constexpr int W = 1280, H = 720, TOP = 96, TILE = 32;
constexpr int COLS = 40, ROWS = 20;
constexpr uint32_t BG = 0x080F1A, PANEL = 0x111E2E, EDGE = 0x24364B;
constexpr uint32_t INK = 0xE7F4FA, MUTED = 0x8098AC, CYAN = 0x62E9F6;
constexpr uint32_t GREEN = 0xA6F3A4, AMBER = 0xF5C77B, RED = 0xFF8796;
enum class Game : uint8_t { Picker, Plane, Blocks, Dino };
using GameLifecycle::Phase;
Game game = Game::Picker;
Phase phase = Phase::Running;
bool sceneChanged = true;
bool darkTheme = true;
uint32_t previousPress = 0;
uint32_t gameTicks = 0, launchPress = 0;
uint32_t renderedLanguageRevision = 0;
const char* tr(const char* zh, const char* en) { return UiLocale::text(zh, en); }
uint32_t scores[3] = {}, best[3] = {};
bool tiles[ROWS][COLS] = {};
Rect dirty[ROWS * COLS];
Rect drawingClip {0, TOP, W, H - TOP};
size_t dirtyCount = 0;
struct Input { int16_t x = 0, y = 0; bool down = false, dark = true; uint32_t press = 0; };
Input input;
GameLifecycle::Policy lifecycle;
portMUX_TYPE inputLock = portMUX_INITIALIZER_UNLOCKED;

uint16_t color(uint32_t c) {
  return ((c >> 8) & 0xF800) | ((c >> 5) & 0x07E0) | ((c >> 3) & 0x001F);
}
uint16_t nearestColor(uint32_t c) {
  return uint16_t(((((c >> 16) & 255) * 31 + 127) / 255) << 11
      | ((((c >> 8) & 255) * 63 + 127) / 255) << 5
      | ((c & 255) * 31 + 127) / 255);
}
struct Palette {
  uint32_t background, top, bottom, edge, shadow, highlight, ink, muted;
  uint32_t blue, gold, green, red;
};
const Palette lightPalette {0xD3D7DC, 0xFFFFFF, 0xE9EBEE, 0xB6BDC6,
  0xB7BCC4, 0xFFFFFF, 0x252A31, 0x5D6470,
  0x3569A8, 0x9A671A, 0x37754B, 0xAB4054};
const Palette darkPalette {0x1D2026, 0x444951, 0x2C3037, 0x101216,
  0x111317, 0x626972, 0xF2F3F5, 0xB0B7C2,
  0xA4C9F1, 0xECC48C, 0xA7DAB2, 0xF1A0AE};
const Palette& palette() { return darkTheme ? darkPalette : lightPalette; }
uint32_t blend(uint32_t a, uint32_t b, unsigned amount) {
  unsigned inverse = 255 - amount;
  unsigned r = (((a >> 16) & 255) * inverse + ((b >> 16) & 255) * amount) / 255;
  unsigned g = (((a >> 8) & 255) * inverse + ((b >> 8) & 255) * amount) / 255;
  unsigned bl = ((a & 255) * inverse + (b & 255) * amount) / 255;
  return (r << 16) | (g << 8) | bl;
}
uint32_t textColor(uint32_t ink) {
  const auto& p = palette();
  if (ink == INK) return p.ink;
  if (ink == MUTED) return p.muted;
  if (ink == CYAN) return p.blue;
  if (ink == AMBER) return p.gold;
  if (ink == GREEN) return p.green;
  if (ink == RED) return p.red;
  return ink;
}
bool in(int x, int y, int bx, int by, int bw, int bh) {
  return x >= bx && x < bx + bw && y >= by && y < by + bh;
}
bool visible(int x, int y, int w, int h) {
  return x < drawingClip.x + drawingClip.w && x + w > drawingClip.x
      && y < drawingClip.y + drawingClip.h && y + h > drawingClip.y;
}
void mark(int x, int y, int w, int h) {
  int x0 = std::max(0, x), y0 = std::max(TOP, y);
  int x1 = std::min(W, x + w), y1 = std::min(H, y + h);
  if (x1 <= x0 || y1 <= y0) return;
  int firstCol = x0 / TILE, lastCol = (x1 - 1) / TILE;
  int firstRow = (y0 - TOP) / TILE, lastRow = (y1 - TOP - 1) / TILE;
  for (int row = firstRow; row <= lastRow; ++row)
    for (int col = firstCol; col <= lastCol; ++col) tiles[row][col] = true;
}
void collect() {
  dirtyCount = 0;
  for (int row = 0; row < ROWS; ++row) {
    for (int col = 0; col < COLS;) {
      if (!tiles[row][col]) { ++col; continue; }
      int first = col;
      while (col < COLS && tiles[row][col]) ++col;
      Rect next {int16_t(first * TILE), int16_t(TOP + row * TILE),
                 int16_t((col - first) * TILE), int16_t(std::min(TILE, H - TOP - row * TILE))};
      bool merged = false;
      for (size_t i = 0; i < dirtyCount; ++i) {
        if (dirty[i].x == next.x && dirty[i].w == next.w && dirty[i].y + dirty[i].h == next.y) {
          dirty[i].h += next.h; merged = true; break;
        }
      }
      if (!merged) dirty[dirtyCount++] = next;
    }
  }
}
const lgfx::IFont* fontFor(int size) {
  switch (size) {
    case 16: return UiFont::sans16();
    case 20: return UiFont::sans20();
    case 28: return UiFont::sans28();
    case 32: return UiFont::sans32();
    case 40: return UiFont::sans40();
    default: return UiFont::sans24();
  }
}
void label(M5Canvas& c, const char* s, int x, int y, uint32_t ink = INK,
           bool centered = false, int size = 24) {
  if (centered) UiText::drawCentered(c, s, x, y, fontFor(size), textColor(ink));
  else UiText::drawLeft(c, s, x, y, fontFor(size), textColor(ink));
}
void panel(M5Canvas& c, int x, int y, int w, int h, uint32_t fill = PANEL, uint32_t outline = EDGE, int radius = 18) {
  MaterialGradient::solidRoundRect565(c, x, y, w, h, radius, color(fill));
  MaterialGradient::strokeRoundRect(c, x, y, w, h, radius, .8f, outline, 160);
}
void raised(M5Canvas& c, int x, int y, int w, int h, int radius = 18,
            bool held = false, uint32_t accent = CYAN) {
  if (!visible(x, y, w, h)) return;
  const auto& p = palette();
  // The complete shadow and pressed travel stay inside the touch target, so
  // ordinary button damage never leaves an old shadow outside its bounds.
  MaterialGradient::shadowRoundRect(c, x + 5, y + 7, w - 10, h - 12,
    std::max(1, radius - 2), 4.f, darkTheme ? 58 : 30, p.shadow);
  int faceY = y + (held ? 4 : 1), faceH = h - (held ? 7 : 9);
  uint32_t faceTop = held ? blend(p.bottom, textColor(accent), 36) : p.top;
  uint32_t faceBottom = held ? blend(p.bottom, textColor(accent), 18) : blend(p.bottom, p.top, 120);
  MaterialGradient::bevelRoundRect565(c, x + 1, faceY, w - 2, faceH, radius,
    nearestColor(faceTop), nearestColor(faceBottom), 10.f);
  MaterialGradient::strokeRoundRect(c, x + 1, faceY, w - 2, faceH, radius,
    .8f, held ? textColor(accent) : p.edge, held ? 160 : 75);
  MaterialGradient::line(c, x + radius + 3, faceY + 1.8f,
    x + w - radius - 3, faceY + 1.8f, .7f, p.highlight, held ? 40 : 70);
}
void button(M5Canvas& c, const char* s, int x, int y, int w, int h, bool held = false, uint32_t accent = CYAN) {
  raised(c, x, y, w, h, 14, held, accent);
  int size = 28;
  for (int candidate : {28, 24, 20, 16}) {
    size = candidate;
    if (UiText::bounds(fontFor(size), s).w <= w - 20) break;
  }
  label(c, s, x + w / 2, y + h / 2 + (held ? 1 : -3), accent, true, size);
}
int index() { return game == Game::Plane ? 0 : game == Game::Blocks ? 1 : 2; }
void endGame() {
  phase = Phase::Over;
  best[index()] = std::max(best[index()], scores[index()]);
  mark(350, 294, 580, 184);
  mark(195, 108, 760, 52);
}
void scoreChanged() { mark(448, 110, 500, 48); }

// --- Drag to fly, automatic fire ------------------------------------------------
struct Enemy { float x = 0, y = 0, drift = 0; bool live = false; };
struct Bullet { float x = 0, y = 0; bool live = false; };
Enemy enemies[9];
Bullet bullets[18];
float shipX = 640, shipY = 551;
uint8_t lives = 3;
uint32_t lastShot = 0, lastSpawn = 0, hurtUntil = 0;
void planeDamage() {
  mark(int(shipX) - 42, int(shipY) - 42, 85, 85);
  for (const auto& e : enemies) if (e.live) mark(int(e.x) - 25, int(e.y) - 22, 51, 46);
  for (const auto& b : bullets) if (b.live) mark(int(b.x) - 4, int(b.y) - 15, 9, 19);
}
void resetPlane(uint32_t now) {
  for (auto& e : enemies) e = Enemy{};
  for (auto& b : bullets) b = Bullet{};
  shipX = 640; shipY = 551; lives = 3; hurtUntil = 0;
  lastShot = now; lastSpawn = now - 650;
  scores[0] = 0;
}
void hitShip(uint32_t now) {
  if (now < hurtUntil) return;
  if (lives) --lives;
  hurtUntil = now + 1400;
  scoreChanged();
  if (!lives) endGame();
}
void stepPlane(const Input& p, float dt, uint32_t now) {
  planeDamage();
  if (p.down && in(p.x, p.y, 62, 174, 1156, 446)) {
    float tx = std::max(100.f, std::min(1180.f, float(p.x)));
    float ty = std::max(276.f, std::min(572.f, float(p.y) - 32));
    float step = 1100.f * dt;
    shipX += std::max(-step, std::min(step, tx - shipX));
    shipY += std::max(-step, std::min(step, ty - shipY));
  }
  if (now - lastShot >= 230) {
    for (auto& b : bullets) if (!b.live) { b = {shipX, shipY - 35, true}; break; }
    lastShot = now;
  }
  if (now - lastSpawn >= std::max<uint32_t>(300, 850 - std::min<uint32_t>(550, scores[0] * 3))) {
    for (auto& e : enemies) if (!e.live) {
      e = {float(random(110, 1170)), 194.f, random(-40, 41) * .5f, true}; break;
    }
    lastSpawn = now;
  }
  for (auto& b : bullets) if (b.live) { b.y -= 650 * dt; if (b.y < 183) b.live = false; }
  for (auto& e : enemies) if (e.live) {
    e.y += (83 + std::min<uint32_t>(120, scores[0] * 2)) * dt;
    e.x += e.drift * dt;
    if (e.x < 95 || e.x > 1185) e.drift = -e.drift;
    for (auto& b : bullets) if (b.live && std::abs(b.x - e.x) < 25 && std::abs(b.y - e.y) < 26) {
      b.live = false; e.live = false; scores[0] += 10; scoreChanged(); break;
    }
    if (!e.live) continue;
    if (std::abs(e.x - shipX) < 39 && std::abs(e.y - shipY) < 42) { e.live = false; hitShip(now); }
    else if (e.y > 611) { e.live = false; hitShip(now); }
  }
  planeDamage();
}
void drawPlane(M5Canvas& c, uint32_t now) {
  panel(c, 40, 174, 1200, 452, 0x0B1522);
  for (int i = 0; i < 52; ++i) c.drawPixel(76 + (i * 239) % 1130, 193 + (i * 97) % 412, color(i % 3 ? EDGE : MUTED));
  for (const auto& b : bullets) if (b.live)
    MaterialGradient::solidRoundRect565(c, b.x - 3, b.y - 13, 6, 15, 3, color(AMBER));
  for (const auto& e : enemies) if (e.live) {
    int x = e.x, y = e.y;
    c.fillTriangle(x, y + 18, x - 23, y - 13, x + 23, y - 13, color(RED));
    c.fillTriangle(x, y + 9, x - 10, y - 20, x + 10, y - 20, color(0xD65478));
    MaterialGradient::solidRoundRect565(c, x - 5, y - 9, 10, 12, 3, color(INK));
  }
  bool flash = now < hurtUntil && ((now / 110) & 1);
  if (!flash) {
    int x = shipX, y = shipY;
    c.fillTriangle(x, y - 34, x - 29, y + 23, x + 29, y + 23, color(CYAN));
    c.fillTriangle(x, y - 19, x - 10, y + 30, x + 10, y + 30, color(0x2A9EBD));
    MaterialGradient::solidRoundRect565(c, x - 6, y - 11, 12, 24, 5, color(INK));
    c.fillTriangle(x - 8, y + 27, x + 8, y + 27, x, y + 38, color(AMBER));
    if (now < hurtUntil) MaterialGradient::strokeCircle(c, x, y, 37, 1.f, GREEN, 210);
  }
  label(c, tr("拖动飞船躲避敌机 · 自动射击", "Drag to dodge / automatic fire"), 640, 662, MUTED, true, 20);
}

// --- Tetris: 10 columns, 18 visible rows ---------------------------------------
constexpr int BC = 10, BR = 18, CELL = 26, BX = 472, BY = 170;
uint8_t board[BR][BC] = {};
// A visual cell includes both the falling piece and its landing outline.
// Ghost cells carry bit 7; filled cells use their ordinary palette index.
uint8_t boardVisual[BR][BC] = {};
uint8_t previousBoardVisual[BR][BC] = {};
bool boardVisualValid = false;
constexpr uint16_t shapes[] = {0x0F00, 0x6600, 0x4E00, 0x8E00, 0x2E00, 0x6C00, 0xC600};
constexpr uint32_t blockColors[] = {CYAN, AMBER, 0xC69AFF, 0x79A6FF, 0xFFAE78, GREEN, RED};
int piece = 0, nextPiece = 0, rotation = 0, px = 3, py = -1;
uint32_t lastFall = 0, repeatedAt = 0, heldSince = 0;
int heldControl = -1;
uint32_t lines = 0;
bool cell(int type, int r, int x, int y) {
  int sx = x, sy = y;
  if (type == 1) r = 0;
  switch (r & 3) {
    case 1: sx = y; sy = 3 - x; break;
    case 2: sx = 3 - x; sy = 3 - y; break;
    case 3: sx = 3 - y; sy = x; break;
  }
  return shapes[type] & (0x8000 >> (sy * 4 + sx));
}
bool fits(int x, int y, int r) {
  for (int cy = 0; cy < 4; ++cy) for (int cx = 0; cx < 4; ++cx) if (cell(piece, r, cx, cy)) {
    int bx = x + cx, by = y + cy;
    if (bx < 0 || bx >= BC || by >= BR || (by >= 0 && board[by][bx])) return false;
  }
  return true;
}
void updateBoardDamage() {
  memcpy(boardVisual, board, sizeof(boardVisual));
  int ghostY = py;
  while (fits(px, ghostY + 1, rotation)) ++ghostY;
  for (int y = 0; y < 4; ++y) for (int x = 0; x < 4; ++x) {
    if (!cell(piece, rotation, x, y)) continue;
    int gx = px + x, gy = ghostY + y;
    if (gx >= 0 && gx < BC && gy >= 0 && gy < BR)
      boardVisual[gy][gx] = uint8_t(0x80 | (piece + 1));
  }
  // Draw the active cells last so an overlapping landing outline is hidden.
  for (int y = 0; y < 4; ++y) for (int x = 0; x < 4; ++x) {
    if (!cell(piece, rotation, x, y)) continue;
    int gx = px + x, gy = py + y;
    if (gx >= 0 && gx < BC && gy >= 0 && gy < BR)
      boardVisual[gy][gx] = piece + 1;
  }
  for (int y = 0; y < BR; ++y) for (int x = 0; x < BC; ++x) {
    if (!boardVisualValid || boardVisual[y][x] != previousBoardVisual[y][x])
      mark(BX + x * CELL, BY + y * CELL, CELL, CELL);
  }
  memcpy(previousBoardVisual, boardVisual, sizeof(boardVisual));
  boardVisualValid = true;
}
void spawnPiece(uint32_t now) {
  int oldPreview = nextPiece;
  piece = nextPiece; nextPiece = random(0, 7); rotation = 0; px = 3; py = -1; lastFall = now;
  if (nextPiece != oldPreview) mark(833, 255, 134, 134);
  if (!fits(px, py, rotation)) endGame();
}
void resetBlocks(uint32_t now) {
  memset(board, 0, sizeof(board));
  boardVisualValid = false;
  scores[1] = 0; lines = 0; nextPiece = random(0, 7); heldControl = -1;
  spawnPiece(now);
}
void lockPiece(uint32_t now) {
  bool above = false;
  for (int cy = 0; cy < 4; ++cy) for (int cx = 0; cx < 4; ++cx) if (cell(piece, rotation, cx, cy)) {
    int x = px + cx, y = py + cy;
    if (y < 0) above = true;
    else if (x >= 0 && x < BC && y < BR) board[y][x] = piece + 1;
  }
  if (above) { endGame(); return; }
  int cleared = 0;
  for (int y = BR - 1; y >= 0; --y) {
    bool full = true;
    for (int x = 0; x < BC; ++x) full &= board[y][x] != 0;
    if (!full) continue;
    ++cleared;
    for (int yy = y; yy > 0; --yy) memcpy(board[yy], board[yy - 1], BC);
    memset(board[0], 0, BC); ++y;
  }
  const uint16_t points[] = {0, 100, 300, 500, 800};
  uint32_t previousScore = scores[1];
  scores[1] += points[cleared] * (1 + lines / 5); lines += cleared;
  if (scores[1] != previousScore) scoreChanged();
  if (cleared) mark(60, 279, 300, 74);
  spawnPiece(now);
}
void moveBlock(int control, uint32_t now) {
  if (control == 0 || control == 2) {
    int dx = control == 0 ? -1 : 1;
    if (fits(px + dx, py, rotation)) px += dx;
  } else if (control == 1) {
    int r = (rotation + 1) & 3;
    for (int offset : {0, -1, 1, -2, 2}) if (fits(px + offset, py, r)) { px += offset; rotation = r; break; }
  } else if (control == 3) {
    if (fits(px, py + 1, rotation)) { ++py; ++scores[1]; scoreChanged(); }
    else lockPiece(now);
    lastFall = now;
  } else if (control == 4) {
    while (fits(px, py + 1, rotation)) { ++py; scores[1] += 2; }
    lockPiece(now); scoreChanged();
  }
}
int blockControl(const Input& p) {
  if (!p.down) return -1;
  for (int i = 0; i < 5; ++i) if (in(p.x, p.y, 300 + i * 140, 644, 124, 58)) return i;
  return -1;
}
void stepBlocks(const Input& p, bool pressed, uint32_t now) {
  // Preserve a short tap even when both its press and release were sampled
  // before this frame. Repeating and button highlights still follow down.
  if (pressed && !p.down) {
    Input tap = p; tap.down = true;
    int tappedControl = blockControl(tap);
    if (tappedControl >= 0) moveBlock(tappedControl, now);
  }
  int control = blockControl(p);
  if (control != heldControl || (pressed && control >= 0)) {
    if (heldControl >= 0) mark(300 + heldControl * 140, 644, 124, 58);
    if (control >= 0) mark(300 + control * 140, 644, 124, 58);
    heldControl = control; heldSince = repeatedAt = now;
    if (control >= 0) moveBlock(control, now);
  } else if (control >= 0 && (control == 0 || control == 2 || control == 3)
             && now - heldSince >= 260 && now - repeatedAt >= (control == 3 ? 65u : 105u)) {
    moveBlock(control, now); repeatedAt = now;
  }
  if (phase != Phase::Running) return;
  uint32_t interval = std::max<uint32_t>(140, 780 - std::min<uint32_t>(640, (lines / 5) * 65));
  if (now - lastFall >= interval) {
    if (fits(px, py + 1, rotation)) ++py; else lockPiece(now);
    lastFall = now;
  }
}
void block(M5Canvas& c, int x, int y, int size, uint32_t ink, bool ghost = false) {
  if (ghost) {
    MaterialGradient::strokeRoundRect(c, x + 3, y + 3, size - 6, size - 6, 3, 1.f, ink, 205);
    return;
  }
  MaterialGradient::solidRoundRect565(c, x + 1, y + 1, size - 2, size - 2, 4, color(ink));
  MaterialGradient::line(c, x + 4, y + 4.5f, x + size - 4, y + 4.5f, 1.f, INK, 180);
}
void drawBlocks(M5Canvas& c) {
  if (visible(BX - 4, BY - 4, BC * CELL + 8, BR * CELL + 8)) {
    panel(c, BX - 4, BY - 4, BC * CELL + 8, BR * CELL + 8, 0x0B1522, EDGE, 7);
    // A small damaged strip should not issue drawing calls for all 180 cells.
    for (int y = 0; y < BR; ++y) for (int x = 0; x < BC; ++x) {
      int dx = BX + x * CELL, dy = BY + y * CELL;
      if (!visible(dx, dy, CELL, CELL)) continue;
      c.drawRect(dx, dy, CELL, CELL, color(0x17283A));
      uint8_t v = boardVisual[y][x];
      if (v) block(c, dx, dy, CELL, blockColors[(v & 0x7F) - 1], v & 0x80);
    }
  }
  char s[64];
  if (visible(60, 223, 350, 30)) label(c, tr("消除整行，留出空间", "Clear rows to make space"), 62, 225, CYAN, false, 20);
  if (visible(60, 279, 300, 30)) {
    snprintf(s, sizeof(s), tr("已消除 %lu 行", "Lines cleared %lu"), (unsigned long)lines); label(c, s, 62, 281);
  }
  if (visible(60, 322, 300, 30)) {
    snprintf(s, sizeof(s), tr("等级 %lu", "Level %lu"), (unsigned long)(1 + lines / 5)); label(c, s, 62, 324, AMBER);
  }
  if (visible(60, 405, 350, 30)) label(c, tr("轮廓是落点预览", "Outline previews the landing"), 62, 407, MUTED, false, 20);
  if (visible(60, 447, 350, 30)) label(c, tr("长按方向键可连续移动", "Hold an arrow to keep moving"), 62, 449, MUTED, false, 20);
  if (visible(826, 205, 150, 30)) label(c, tr("下一个", "Next"), 828, 207, MUTED, false, 20);
  if (visible(818, 242, 174, 164)) panel(c, 818, 242, 174, 164, 0x0B1522, palette().edge, 18);
  if (visible(833, 255, 134, 134)) {
    for (int y = 0; y < 4; ++y) for (int x = 0; x < 4; ++x)
      if (cell(nextPiece, 0, x, y)) block(c, 836 + x * 32, 258 + y * 32, 32, blockColors[nextPiece]);
  }
  if (visible(826, 456, 350, 30)) label(c, tr("轻点旋转", "Tap to rotate"), 828, 458, CYAN, false, 20);
  if (visible(826, 498, 350, 30)) label(c, tr("速降立即落到底部", "Hard drop lands immediately"), 828, 500, MUTED, false, 20);
  const char* names[] = {tr("左移", "Left"), tr("旋转", "Rotate"), tr("右移", "Right"), tr("下落", "Down"), tr("速降", "Hard drop")};
  for (int i = 0; i < 5; ++i)
    if (visible(300 + i * 140, 644, 124, 58)) button(c, names[i], 300 + i * 140, 644, 124, 58, heldControl == i);
}

// --- Dinosaur runner ----------------------------------------------------------
struct Cactus { float x = 0; int h = 46, w = 25; bool live = false; };
Cactus cacti[6];
float dinoY = 0, dinoV = 0, distance = 0, road = 0;
uint32_t nextCactusAt = 0;
constexpr int DX = 230, GROUND = 528;
void dinoDamage() {
  mark(DX - 33, GROUND - int(dinoY) - 65, 80, 72);
  for (const auto& o : cacti) if (o.live) mark(int(o.x) - 5, GROUND - o.h - 5, o.w + 13, o.h + 11);
  mark(62, GROUND + 1, 1156, 31);
}
void resetDino(uint32_t now) {
  for (auto& o : cacti) o = Cactus{};
  dinoY = dinoV = distance = road = 0;
  nextCactusAt = now + 1000; scores[2] = 0;
}
void stepDino(const Input& p, bool pressed, float dt, uint32_t now) {
  dinoDamage();
  if (pressed && p.y >= 174 && dinoY <= .01f) dinoV = 615;
  dinoV -= 1750 * dt;
  dinoY += dinoV * dt;
  if (dinoY <= 0) { dinoY = 0; dinoV = 0; }
  float speed = 320 + std::min(220.f, distance * .55f);
  road = std::fmod(road + speed * dt, 45.f);
  distance += speed * dt * .035f;
  uint32_t score = uint32_t(distance);
  if (score != scores[2]) { scores[2] = score; scoreChanged(); }
  if (now >= nextCactusAt) {
    for (auto& o : cacti) if (!o.live) { o = {1190.f, int(random(37, 70)), int(random(23, 38)), true}; break; }
    nextCactusAt = now + random(1080, 1680);
  }
  for (auto& o : cacti) if (o.live) {
    o.x -= speed * dt;
    if (o.x + o.w < 68) o.live = false;
    // The visible dinosaur has forgiving hit bounds, including its raised tail.
    if (o.live && o.x < DX + 30 && o.x + o.w > DX - 17 && dinoY < o.h - 8) endGame();
  }
  dinoDamage();
}
void drawDino(M5Canvas& c, uint32_t now) {
  panel(c, 40, 174, 1200, 414, 0x0B1522);
  for (int i = 0; i < 4; ++i) {
    int x = 350 + i * 245, y = 255 + (i % 2) * 60;
    MaterialGradient::strokeRoundRect(c, x, y, 74, 18, 9, .8f, EDGE, 180);
    MaterialGradient::strokeCircle(c, x + 32, y - 4, 16, .8f, EDGE, 180);
  }
  c.drawFastHLine(62, GROUND, 1156, color(MUTED));
  for (int i = 0; i < 29; ++i) {
    int x = 65 + i * 45 - int(road);
    if (x >= 62 && x < 1210) c.drawFastHLine(x, GROUND + 10 + (i % 3) * 6, 10 + (i % 2) * 7, color(EDGE));
  }
  for (const auto& o : cacti) if (o.live) {
    int x = o.x, y = GROUND - o.h;
    MaterialGradient::solidRoundRect565(c, x + o.w / 3, y, o.w / 2, o.h, 5, color(GREEN));
    MaterialGradient::solidRoundRect565(c, x, y + 14, o.w / 3 + 3, o.h / 2, 4, color(GREEN));
    c.fillRect(x + 1, y + o.h / 2, o.w, 8, color(GREEN));
    MaterialGradient::solidRoundRect565(c, x + o.w - 3, y + 10, 9, o.h / 2, 4, color(GREEN));
  }
  int x = DX, y = GROUND - int(dinoY);
  MaterialGradient::solidRoundRect565(c, x - 14, y - 44, 35, 34, 4, color(CYAN));
  MaterialGradient::solidRoundRect565(c, x + 5, y - 64, 36, 31, 5, color(CYAN));
  c.fillRect(x - 27, y - 32, 17, 12, color(CYAN));
  c.fillTriangle(x - 27, y - 20, x - 33, y - 43, x - 11, y - 20, color(CYAN));
  c.fillRect(x + 15, y - 28, 16, 5, color(CYAN));
  c.fillRect(x + 28, y - 58, 5, 5, color(BG));
  c.drawFastHLine(x + 22, y - 42, 17, color(BG));
  bool run = phase == Phase::Running && dinoY < .1f;
  int gait = run ? ((now / 110) & 1) * 6 : 0;
  c.fillRect(x - 9, y - 13, 8, 13 - gait, color(CYAN));
  c.fillRect(x + 8, y - 13, 8, 7 + gait, color(CYAN));
  c.fillRect(x - 9, y - 4 - gait, 15, 4, color(CYAN));
  c.fillRect(x + 8, y - 10 + gait, 15, 4, color(CYAN));
  label(c, tr("轻点场地或右侧按钮跳跃", "Tap the field or the button to jump"), 65, 629, MUTED, false, 20);
  label(c, tr("越过仙人掌，跑得越远分数越高", "Jump over cacti and run for a higher score"), 65, 666, MUTED, false, 20);
  button(c, tr("跳跃", "Jump"), 947, 613, 270, 80, false, GREEN);
}

void resetGame(Game next, uint32_t now) {
  game = next; phase = Phase::Running; gameTicks = 0;
  launchPress = previousPress;
  if (game == Game::Plane) resetPlane(now);
  else if (game == Game::Blocks) resetBlocks(now);
  else if (game == Game::Dino) resetDino(now);
  sceneChanged = true;
}
void drawPicker(M5Canvas& c) {
  label(c, tr("休息一下，来一局", "Take a little break"), 640, 154, INK, true, 40);
  label(c, tr("选择游戏 · 随时返回聊天", "Pick a game / return to chat anytime"), 640, 201, MUTED, true, 20);
  const char* names[] = {tr("星际守卫", "Star Guard"), tr("俄罗斯方块", "Blocks"), tr("小恐龙跑酷", "Dino Run")};
  const char* captions[] = {tr("拖动飞船 · 自动射击", "Drag to fly / auto fire"), tr("移动旋转 · 消除整行", "Move, rotate / clear rows"), tr("轻点跳跃 · 越跑越快", "Tap to jump / keep running")};
  uint32_t accents[] = {CYAN, AMBER, GREEN};
  for (int i = 0; i < 3; ++i) {
    int x = 40 + i * 414, cx = x + 186;
    raised(c, x, 245, 372, 332, 24, false, accents[i]);
    // A recessed illustration window gives each card the feel of a cartridge.
    MaterialGradient::solidRoundRect565(c, x + 22, 266, 328, 168, 20, color(0x080D16));
    MaterialGradient::strokeRoundRect(c, x + 22, 266, 328, 168, 20, .8f, palette().edge, 90);
    if (i == 0) {
      c.fillTriangle(cx, 286, cx - 48, 385, cx + 48, 385, color(CYAN));
      MaterialGradient::solidRoundRect565(c, cx - 10, 319, 20, 43, 8, color(INK));
      c.fillTriangle(cx - 13, 390, cx + 13, 390, cx, 418, color(AMBER));
      MaterialGradient::solidRoundRect565(c, cx - 75, 285, 5, 21, 2, color(AMBER));
      MaterialGradient::solidRoundRect565(c, cx + 72, 330, 5, 21, 2, color(AMBER));
    } else if (i == 1) {
      block(c, cx - 58, 306, 39, AMBER); block(c, cx - 19, 306, 39, AMBER);
      block(c, cx + 20, 306, 39, AMBER); block(c, cx - 19, 345, 39, AMBER);
      block(c, cx - 58, 384, 39, CYAN); block(c, cx - 19, 384, 39, CYAN);
    } else {
      MaterialGradient::solidRoundRect565(c, cx - 29, 340, 56, 48, 5, color(GREEN));
      MaterialGradient::solidRoundRect565(c, cx + 3, 300, 58, 49, 6, color(GREEN));
      c.fillRect(cx + 40, 310, 7, 7, color(BG));
      c.fillTriangle(cx - 27, 380, cx - 57, 340, cx - 45, 380, color(GREEN));
      c.fillRect(cx - 21, 382, 12, 26, color(GREEN)); c.fillRect(cx + 12, 382, 12, 26, color(GREEN));
      c.drawFastHLine(cx - 84, 410, 170, color(EDGE));
    }
    label(c, names[i], cx, 464, INK, true, 28);
    label(c, captions[i], cx, 508, MUTED, true, 20);
    char s[64]; snprintf(s, sizeof(s), tr("本次最高 %lu", "Session best %lu"), (unsigned long)best[i]);
    label(c, s, cx, 548, accents[i], true, 20);
  }
  label(c, tr("随时暂停，返回聊天也会保留本局进度", "Pause anytime. Your game waits while you chat."), 640, 653, MUTED, true, 20);
}
void drawNavigation(M5Canvas& c) {
  button(c, tr("游戏库", "Games"), 28, 109, 138, 48);
  const char* name = game == Game::Plane ? tr("星际守卫", "Star Guard") : game == Game::Blocks ? tr("俄罗斯方块", "Blocks") : tr("小恐龙跑酷", "Dino Run");
  label(c, name, 198, 121);
  char score[112];
  if (game == Game::Plane) snprintf(score, sizeof(score), tr("得分 %lu   生命 %u   最高 %lu", "Score %lu   Lives %u   Best %lu"), (unsigned long)scores[0], lives, (unsigned long)best[0]);
  else snprintf(score, sizeof(score), tr("得分 %lu   最高 %lu", "Score %lu   Best %lu"), (unsigned long)scores[index()], (unsigned long)best[index()]);
  label(c, score, 484, 124, AMBER, false,
    UiText::bounds(UiFont::sans20(), score).w > 470 ? 16 : 20);
  button(c, phase == Phase::Over ? tr("已结束", "Finished") : phase == Phase::Paused ? tr("继续", "Resume") : tr("暂停", "Pause"), 984, 109, 112, 48,
    false, phase == Phase::Over ? MUTED : CYAN);
  button(c, tr("重开", "Restart"), 1110, 109, 142, 48);
}
void drawOverlay(M5Canvas& c) {
  raised(c, 350, 294, 580, 184, 24, false, phase == Phase::Over ? AMBER : CYAN);
  label(c, phase == Phase::Over ? tr("游戏结束", "Game over") : tr("已暂停", "Paused"), 640, 342, phase == Phase::Over ? AMBER : CYAN, true, 32);
  label(c, phase == Phase::Over ? tr("点右上角「重开」，再来一局", "Tap Restart at the top right to play again") : tr("点右上角「继续」回到游戏", "Tap Resume at the top right to continue"), 640, 399, INK, true);
  label(c, tr("也可以随时返回游戏库或聊天", "Or return to the games library or chat"), 640, 444, MUTED, true, 20);
}
void drawScene(M5Canvas& c, uint32_t now) {
  if (game == Game::Picker) { drawPicker(c); return; }
  if (visible(28, 109, 1224, 48)) drawNavigation(c);
  if (game == Game::Plane) drawPlane(c, now);
  else if (game == Game::Blocks) drawBlocks(c);
  else drawDino(c, now);
  if (phase != Phase::Running) drawOverlay(c);
}
}  // namespace

void init() {
  game = Game::Picker; phase = Phase::Running; sceneChanged = true;
  previousPress = gameTicks = launchPress = 0; dirtyCount = 0;
  renderedLanguageRevision = UiLocale::revision();
  portENTER_CRITICAL(&inputLock);
  input = Input{};
  lifecycle = GameLifecycle::Policy{};
  portEXIT_CRITICAL(&inputLock);
}
void setActive(bool active) {
  portENTER_CRITICAL(&inputLock);
  if (active != lifecycle.active()) input.down = false;
  lifecycle.setActive(active);
  portEXIT_CRITICAL(&inputLock);
}
void setDarkTheme(bool dark) {
  portENTER_CRITICAL(&inputLock); input.dark = dark; portEXIT_CRITICAL(&inputLock);
}
void touch(int16_t x, int16_t y, bool pressed) {
  portENTER_CRITICAL(&inputLock);
  if (!lifecycle.active()) pressed = false;
  if (pressed) { input.x = x; input.y = y; }
  if (pressed && !input.down) ++input.press;
  input.down = pressed;
  portEXIT_CRITICAL(&inputLock);
}
bool render(M5Canvas& c, uint32_t now, bool force) {
  memset(tiles, 0, sizeof(tiles)); dirtyCount = 0;
  if (renderedLanguageRevision != UiLocale::revision()) {
    renderedLanguageRevision = UiLocale::revision(); sceneChanged = true;
  }
  Input p;
  portENTER_CRITICAL(&inputLock);
  p = input;
  const auto lifecycleFrame = lifecycle.frame(now, game != Game::Picker, phase);
  portEXIT_CRITICAL(&inputLock);
  phase = lifecycleFrame.phase;
  if (lifecycleFrame.redraw) sceneChanged = true;
  bool pressed = !lifecycleFrame.discardInput && p.press != previousPress;
  previousPress = p.press;
  if (!lifecycleFrame.active) return false;
  if (lifecycleFrame.discardInput) p.down = false;
  if (p.dark != darkTheme) { darkTheme = p.dark; sceneChanged = true; }
  float dt = lifecycleFrame.elapsedMs * .001f;
  if (pressed) {
    if (game == Game::Picker) {
      for (int i = 0; i < 3; ++i)
        if (in(p.x, p.y, 40 + i * 414, 245, 372, 332)) { resetGame(Game(i + 1), now); break; }
    } else if (in(p.x, p.y, 28, 109, 138, 48)) {
      best[index()] = std::max(best[index()], scores[index()]);
      game = Game::Picker; sceneChanged = true;
    } else if (in(p.x, p.y, 1110, 109, 142, 48)) {
      best[index()] = std::max(best[index()], scores[index()]); resetGame(game, now);
    } else if (in(p.x, p.y, 984, 109, 112, 48) && phase != Phase::Over) {
      phase = phase == Phase::Paused ? Phase::Running : Phase::Paused;
      // Restore the board / sprites hidden by the pause card when continuing.
      mark(350, 294, 580, 184); mark(984, 109, 112, 48);
      lastFall = lastShot = lastSpawn = now;
      nextCactusAt = now + 900;
    }
  }
  if (lifecycleFrame.canAdvance(game != Game::Picker, phase)) {
    ++gameTicks;
    // A tap that launched/restarted the game must not also move, rotate, or
    // jump. The next distinct touch press belongs to gameplay.
    Input controls = p;
    if (p.press == launchPress) { controls.down = false; pressed = false; }
    if (game == Game::Plane) stepPlane(controls, dt, now);
    else if (game == Game::Blocks) stepBlocks(controls, pressed, now);
    else stepDino(controls, pressed, dt, now);
  } else if (game == Game::Blocks && heldControl >= 0) {
    mark(300 + heldControl * 140, 644, 124, 58); heldControl = -1;
  }
  if (game == Game::Blocks) updateBoardDamage();
  if (force || sceneChanged) { mark(0, TOP, W, H - TOP); sceneChanged = false; }
  collect();
  if (!dirtyCount) return false;
  for (size_t i = 0; i < dirtyCount; ++i) {
    const auto& r = dirty[i];
    drawingClip = r;
    c.setClipRect(r.x, r.y, r.w, r.h);
    MaterialGradient::solid565(c, r.x, r.y, r.w, r.h, color(palette().background));
    drawScene(c, now);
  }
  c.clearClipRect();
  return true;
}
const Rect* damage(size_t& count) { count = dirtyCount; return dirty; }
Diagnostics diagnostics() {
  return {int(game), int(phase), game == Game::Picker ? 0 : scores[index()], gameTicks};
}
}  // namespace MicroGames
