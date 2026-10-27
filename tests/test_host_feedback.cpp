#include "host_feedback.h"

#include <cstring>
#include <iostream>
#include <stdexcept>
#include <string>

namespace {
using HostFeedback::Config;
using HostFeedback::Lights;
using HostFeedback::Signal;
using HostFeedback::classify;

void require(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}

constexpr Lights ambient(uint32_t color, uint8_t effect = 2,
                         uint8_t brightness = 255, uint8_t magic = 0) {
  Lights value{};
  value.known = true;
  value.ambient.color = color;
  value.ambient.effect = effect;
  value.ambient.brightness = brightness;
  value.ambient.speed = 102;  // The current desktop mapper sends 0.4.
  value.ambient.magic = magic;
  return value;
}

void defaultsDoNotInventHostState() {
  const Config config{};
  require(!config.fresh, "bare configuration must not be fresh");
  require(config.defaultTier == HostFeedback::Tier::Unknown,
          "bare configuration must not invent a default speed tier");
  require(config.encoder == HostFeedback::Encoder::Unknown,
          "bare configuration must not invent an encoder mode");
  require(config.voice == HostFeedback::Voice::Unknown,
          "bare configuration must not invent a microphone mapping");
  for (const auto action : config.actions)
    require(action == HostFeedback::Action::Unknown,
            "bare configuration must not invent a command binding");

  const Lights lights{};
  require(!lights.known, "bare lighting must not imply a native update");
  require(classify(lights, true) == Signal::Unknown,
          "a connected device without a lighting report remains unknown");
  require(classify(lights, false) == Signal::Offline,
          "a bare disconnected device is offline");

  const HostFeedback::Chat chat{};
  require(!chat.fresh && chat.source==HostFeedback::ChatSource::Unavailable
              && chat.speed==HostFeedback::Speed::Unknown && chat.mode==HostFeedback::Mode::Unknown
              && chat.permission==HostFeedback::Permission::Unknown
              && !*chat.thread_id && !*chat.title,
          "bare live-chat state must not inherit saved defaults or an identity");
  require(std::strcmp(HostFeedback::speedName(HostFeedback::Speed::Default),"default")==0
              && std::strcmp(HostFeedback::speedName(HostFeedback::Speed::Standard),"standard")==0
              && std::strcmp(HostFeedback::modeName(HostFeedback::Mode::Default),"default")==0,
          "a chat default is distinct from an explicitly standard speed");
  struct PermissionCase { HostFeedback::Permission value; const char* name; };
  const PermissionCase permissions[]={
      {HostFeedback::Permission::Unknown,"unknown"},
      {HostFeedback::Permission::FullAccess,"full-access"},
      {HostFeedback::Permission::WorkspaceWrite,"workspace-write"},
      {HostFeedback::Permission::ReadOnly,"read-only"},
      {HostFeedback::Permission::Custom,"custom"}};
  for (const auto& item:permissions)
    require(std::strcmp(HostFeedback::permissionName(item.value),item.name)==0,
            "permission modes must preserve their independent wire identity");
  require(HostFeedback::validThreadId("12345678-1234-abCD-0123-456789abcdef"),
          "canonical UUID-shaped explicit targets must be accepted");
  for (const char* id : {"", "local:12345678-1234-abcd-0123-456789abcdef",
                         "12345678_1234-abcd-0123-456789abcdef", "12345678-1234-abcd-0123-456789abcdeg"})
    require(!HostFeedback::validThreadId(id),"malformed target identities must be rejected");
  require(HostFeedback::validChatTitle("指定聊天 · title 😀"),"valid UTF8 titles must survive intact");
  require(HostFeedback::validChatTitle(std::string(191,'A').c_str())
              && !HostFeedback::validChatTitle(std::string(192,'A').c_str()),
          "live titles must respect the 191-byte transport boundary");
  for (const char* value : {"line\nbreak", "\x7f", "\xC0\xAF", "\xED\xA0\x80", "\xF4\x90\x80\x80", "\xE4\xB8"})
    require(!HostFeedback::validChatTitle(value),"controls and malformed UTF8 must not reach live labels");
}

void nativeAmbientSignalsRemainQualified() {
  struct Case { uint32_t color; Signal signal; const char* label; };
  const Case cases[] = {
      {0x2E8B57, Signal::Recording, "听写录音灯"},
      {0xFFFFFF, Signal::Processing, "听写处理灯"},
      {0x00FF4C, Signal::DialMenu, "拨轮选择"},
      {0x304FFE, Signal::WorkingOrSelection, "运行／选择"},
  };
  for (const auto& item : cases) {
    for (uint8_t brightness : {1, 102, 255}) {
      const auto result = classify(ambient(item.color, 2, brightness), true);
      require(result == item.signal, "a native ambient signal was lost");
      require(std::strcmp(HostFeedback::signalLabel(result), item.label) == 0,
              "native signal labels must preserve their qualification");
    }
  }
  require(classify(ambient(0xFF0033), true) == Signal::Feedback,
          "the desktop minigame red override must not claim an agent error");
}

void offAndUnknownPrecedeStoredColor() {
  for (uint32_t color : {0u, 0x2E8B57u, 0xFFFFFFu, 0x304FFEu, 0x123456u}) {
    require(classify(ambient(color, 2, 0), true) == Signal::Off,
            "zero brightness must suppress a stored active color");
    require(classify(ambient(color, 0), true) == Signal::Off,
            "effect zero must suppress a stored active color");
    auto unreported = ambient(color);
    unreported.known = false;
    require(classify(unreported, true) == Signal::Unknown,
            "unreported lighting must not inherit a stored color");
  }
}

void unknownColorsCannotBecomeSpecificSignals() {
  // Include a neighboring RGB and extra high bits for each known color.
  for (uint32_t color : {0u, 0x123456u, 0x2E8B56u, 0x2E8B58u,
                         0x12E8B57u, 0xFFFFFEu, 0x1FFFFFFu,
                         0x00FF4Bu, 0x00FF4Du, 0x100FF4Cu,
                         0x304FFDu, 0x304FFFu, 0x1304FFEu,
                         0xFF6D00u, 0xFF0033u, 0xFFFFFFFFu})
    require(classify(ambient(color), true) == Signal::Feedback,
            "unknown or approximate colors must remain generic feedback");
}

void unknownEffectsAndMagicCannotBecomeSpecificSignals() {
  for (uint32_t color : {0x2E8B57u, 0xFFFFFFu, 0x00FF4Cu, 0x304FFEu}) {
    for (unsigned effect = 0; effect <= 255; ++effect) {
      if (effect == 2) continue;
      const auto expected = effect == 0 ? Signal::Off : Signal::Feedback;
      require(classify(ambient(color, static_cast<uint8_t>(effect)), true) ==
                  expected,
              "only the recognized snake effect may produce a specific signal");
    }
    for (unsigned magic = 1; magic <= 255; ++magic)
      require(classify(ambient(color, 2, 255, static_cast<uint8_t>(magic)), true) ==
                  Signal::Feedback,
              "nonzero magic must not be interpreted as a native lifecycle");
  }
}

void ambiguousNativeTuplesStayAmbiguous() {
  // Completed dictation and selected idle share this tuple on the wire.
  require(classify(ambient(0xFFFFFF, 1), true) == Signal::Feedback,
          "solid white must not assert dictation completion");
  require(std::strcmp(HostFeedback::signalLabel(
                          classify(ambient(0xFFFFFF, 1), true)),
                      "主机灯效") == 0,
          "solid white must retain a generic label");
  // Running selection and composer navigation highlight share this tuple.
  require(classify(ambient(0x304FFE), true) == Signal::WorkingOrSelection,
          "snake blue must retain the running-or-selection ambiguity");
  require(std::strcmp(HostFeedback::signalLabel(Signal::WorkingOrSelection),
                      "运行／选择") == 0,
          "snake blue must not claim a definite running agent");
}

void keyZoneDoesNotInventAmbientLifecycle() {
  auto value = ambient(0x123456);
  value.keys = ambient(0x2E8B57).ambient;
  require(classify(value, true) == Signal::Feedback,
          "a key-zone color must not be treated as ambient recording");
  value.ambient = {};
  require(classify(value, true) == Signal::Off,
          "a lit key zone does not make an extinguished ambient ring active");
  value.ambient = ambient(0xFFFFFF).ambient;
  require(classify(value, true) == Signal::Processing,
          "the ambient zone must determine its own native signal");
}

void disconnectOverridesEveryLight() {
  for (uint32_t color : {0u, 0x2E8B57u, 0xFFFFFFu, 0x00FF4Cu,
                         0x304FFEu, 0x123456u}) {
    for (uint8_t effect : {0, 1, 2, 4, 6, 255}) {
      auto value = ambient(color, effect);
      require(classify(value, false) == Signal::Offline,
              "disconnect must override known, off and stale signals");
      value.known = false;
      require(classify(value, false) == Signal::Offline,
              "disconnect must override an unknown light model");
    }
  }
  require(std::strcmp(HostFeedback::signalLabel(Signal::Offline), "桌面离线") == 0,
          "offline must have a distinct connection label");
}

static_assert(classify(Lights{}, true) == Signal::Unknown,
              "classification must work without a runtime dependency");
static_assert(classify(ambient(0x2E8B57), true) == Signal::Recording,
              "native classification must be constexpr");
}  // namespace

int main() {
  try {
    defaultsDoNotInventHostState();
    nativeAmbientSignalsRemainQualified();
    offAndUnknownPrecedeStoredColor();
    unknownColorsCannotBecomeSpecificSignals();
    unknownEffectsAndMagicCannotBecomeSpecificSignals();
    ambiguousNativeTuplesStayAmbiguous();
    keyZoneDoesNotInventAmbientLifecycle();
    disconnectOverridesEveryLight();
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
  std::cout << "8/8 host feedback tests passed\n";
}
