#pragma once

// Logical landscape coordinates shared by retained painting and touch capture.
namespace ControlLayout {
constexpr int TrayWidth = 396, TrayHeight = 288, TrayGap = 18, TrayY = 208;
constexpr int DialTrayX = 28;
constexpr int ChatX = DialTrayX + TrayWidth + TrayGap, ChatY = TrayY;
constexpr int ChatWidth = TrayWidth, ChatHeight = TrayHeight;
constexpr int StickTrayX = ChatX + ChatWidth + TrayGap;
constexpr int DialX = DialTrayX + TrayWidth / 2, DialY = 352;
constexpr int StickX = StickTrayX + TrayWidth / 2, StickY = 352;
constexpr int FaceRadius = 106, StickRadius = FaceRadius, StickArrowRadius = 92;
constexpr int ChatCenterX = ChatX + ChatWidth / 2;
static_assert(DialX + StickX == 1280, "The physical controls stay symmetric around the chat");
static_assert(ChatCenterX == 640, "The tracked chat stays centered on the landscape screen");
static_assert(StickTrayX + TrayWidth == 1252, "The equal trays retain the outer screen margin");
static_assert(StickArrowRadius + 8 <= StickRadius, "Each full arrow is inside the stick touch area");
static_assert(StickX - StickRadius - 8 > ChatX + ChatWidth, "The read-only chat cannot overlap stick capture");
}  // namespace ControlLayout
