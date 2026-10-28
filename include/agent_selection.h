#pragma once

#include "host_feedback.h"
#include <stdint.h>

// Native dynamic Agent order is not exported by the desktop. A transmitted
// slot press is a selection intent, not proof of desktop focus or identity.
namespace AgentSelection {
inline bool submitted(int8_t& selected, int keyIndex, uint8_t action, bool suppressed) {
  if (suppressed || keyIndex < 0 || keyIndex > 5 || action != 1) return false;
  selected = static_cast<int8_t>(keyIndex);
  return true;
}

inline void filterExplicitTarget(int8_t selected, HostFeedback::Chat& chat, bool& available) {
  if (selected < 0) return;
  // Neither another metadata heartbeat nor a transport reconnect identifies
  // the selected dynamic slot. Keep its old fixed target cleared until reboot.
  chat = {};
  available = false;
}
}  // namespace AgentSelection
