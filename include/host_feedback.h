#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>

// Native lighting, saved defaults and an explicitly tracked conversation are
// independent sources. Only Chat may describe that conversation's live mode.
namespace HostFeedback {
enum class Tier : uint8_t { Unknown, Standard, Fast, UltraFast };
enum class Encoder : uint8_t { Unknown, Composer, Reasoning, Scroll, Custom };
enum class Voice : uint8_t { Unknown, PushToTalk, Realtime };
enum class Action : uint8_t {
  Unknown, None, Fast, Approve, Reject, Fork, Voice, Aux, Send,
  Plan, Sidebar, Back, Forward, Custom
};
struct Config {
  bool fresh=false;
  Tier defaultTier=Tier::Unknown;
  Encoder encoder=Encoder::Unknown;
  Voice voice=Voice::Unknown;
  Action actions[7]={};  // ACT06 through ACT12; no custom text is transported.
  bool joystickUpPlan=false;  // Validated mapping fact, not chat state or target identity.
};
enum class ChatSource : uint8_t { Unavailable, DesktopIpc };
enum class Speed : uint8_t { Unknown, Default, Standard, Fast, UltraFast };
enum class Mode : uint8_t { Unknown, Default, Plan };
enum class Permission : uint8_t { Unknown, FullAccess, WorkspaceWrite, ReadOnly, Custom };
struct Chat {
  bool fresh=false;
  ChatSource source=ChatSource::Unavailable;
  Speed speed=Speed::Unknown;
  Mode mode=Mode::Unknown;
  Permission permission=Permission::Unknown;
  char thread_id[37]={};
  char title[192]={};
};
constexpr const char* speedName(Speed value) {
  switch (value) {
    case Speed::Default: return "default";
    case Speed::Standard: return "standard";
    case Speed::Fast: return "fast";
    case Speed::UltraFast: return "ultrafast";
    default: return "unknown";
  }
}
constexpr const char* modeName(Mode value) {
  return value==Mode::Plan ? "plan" : value==Mode::Default ? "default" : "unknown";
}
constexpr const char* permissionName(Permission value) {
  switch (value) {
    case Permission::FullAccess: return "full-access";
    case Permission::WorkspaceWrite: return "workspace-write";
    case Permission::ReadOnly: return "read-only";
    case Permission::Custom: return "custom";
    default: return "unknown";
  }
}
inline bool validThreadId(const char* value) {
  if (!value || strlen(value)!=36) return false;
  for (size_t i=0;i<36;++i) {
    const char ch=value[i];
    if (i==8 || i==13 || i==18 || i==23) { if (ch!='-') return false; }
    else if (!((ch>='0' && ch<='9') || (ch>='a' && ch<='f') || (ch>='A' && ch<='F'))) return false;
  }
  return true;
}
// Strict UTF-8, bounded before copying; control characters cannot alter layout.
inline bool validChatTitle(const char* value) {
  if (!value) return false;
  const size_t length=strlen(value);
  if (length>191) return false;
  for (size_t i=0;i<length;) {
    const uint8_t lead=uint8_t(value[i++]);
    if (lead<0x80) { if (lead<0x20 || lead==0x7F) return false; continue; }
    unsigned count; uint32_t cp,minimum;
    if (lead>=0xC2 && lead<=0xDF) { count=1; cp=lead&31; minimum=0x80; }
    else if (lead>=0xE0 && lead<=0xEF) { count=2; cp=lead&15; minimum=0x800; }
    else if (lead>=0xF0 && lead<=0xF4) { count=3; cp=lead&7; minimum=0x10000; }
    else return false;
    if (i+count>length) return false;
    while (count--) {
      const uint8_t ch=uint8_t(value[i++]);
      if ((ch&0xC0)!=0x80) return false;
      cp=(cp<<6)|(ch&63);
    }
    if (cp<minimum || cp>0x10FFFF || (cp>=0xD800 && cp<=0xDFFF)) return false;
  }
  return true;
}
struct Zone {
  uint32_t color=0;
  uint8_t brightness=0, effect=0, speed=0, magic=0;
};
struct Lights {
  bool known=false;
  Zone keys, ambient;
};
enum class Signal : uint8_t {
  Offline, Unknown, Off, Recording, Processing, DialMenu, WorkingOrSelection,
  Feedback
};
constexpr Signal classify(const Lights& value,bool connected) {
  if (!connected) return Signal::Offline;
  if (!value.known) return Signal::Unknown;
  const auto& a=value.ambient;
  if (!a.brightness || !a.effect) return Signal::Off;
  if (!a.magic && a.effect==2) {
    if (a.color==0x2E8B57) return Signal::Recording;
    if (a.color==0xFFFFFF) return Signal::Processing;
    if (a.color==0x00FF4C) return Signal::DialMenu;
    if (a.color==0x304FFE) return Signal::WorkingOrSelection;
  }
  return Signal::Feedback;
}
constexpr const char* signalLabel(Signal value) {
  switch (value) {
    case Signal::Offline: return "桌面离线";
    case Signal::Unknown: return "等待灯效";
    case Signal::Off: return "氛围灯关闭";
    case Signal::Recording: return "听写录音灯";
    case Signal::Processing: return "听写处理灯";
    case Signal::DialMenu: return "拨轮选择";
    case Signal::WorkingOrSelection: return "运行／选择";
    default: return "主机灯效";
  }
}
}
