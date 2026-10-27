# Tab5 Codex Micro

把 **M5Stack Tab5** 变成 Codex 的触摸控制台：保留 Micro 的旋钮、摇杆和按键交互，结合 5 英寸 1280×720 触摸屏，加入聊天状态、双主题、音效和小游戏。

A touch-based Codex Micro compatible controller for M5Stack Tab5, with native USB HID, live local chat metadata, and three offline games.

这是第三方实验性兼容固件，与 OpenAI、M5Stack 或 Work Louder 无隶属关系。当前版本：**0.10.2**。

## 界面预览

白天和黑夜界面，图片来自 Tab5 实机帧缓冲；聊天标题与状态使用隔离的演示数据。

| 白天 | 黑夜 |
| --- | --- |
| ![白天控制台](docs/images/day.png) | ![黑夜控制台](docs/images/night.png) |

## 功能

- **原生 Micro 交互**：沿旋钮外环连续转动，中心支持点按与长按；摇杆输出连续方向与行程，松手归中。事件交给桌面端的 Micro 映射处理。
- **聊天快捷键**：六个 Agent 键，显示主机灯光状态和活动霓虹边框；可通过本地 companion 显示明确绑定的聊天标题。
- **两行标题**：保留正常字号，中文与英文自动换行。超过两行后缓慢循环滚动，触摸时暂停，松手后继续。
- **真实聊天状态**：可选桥接同步指定聊天下一次发送所采用的 Fast／Standard、Plan／Default 和权限预设，包括 **Full access**。未同步或过期时明确显示未知。
- **日夜主题**：浅色金属与深色石墨风格，触摸反馈、游戏库和设置页保持统一。
- **轻量音效与设置**：按钮、旋钮和摇杆提供短提示音；可调音量、静音和屏幕亮度，设置在重启后保留。
- **自动方向**：利用 IMU 重力方向在两个横屏方向之间翻转，同时调整触摸坐标；螺丝孔朝下为默认方向。平放、晃动或触摸操作时保持方向。
- **三个离线小游戏**：星际守卫、俄罗斯方块、小恐龙跑酷；支持暂停、继续与重开。

## 日常操作

USB-C 连接电脑后，在 Codex 桌面应用的 **设置 → Codex Micro** 中配置按键、旋钮与语音行为。

| 操作 | 行为 |
| --- | --- |
| 沿旋钮外环转动 | 每圈 24 个步进，支持顺／逆时针 |
| 点按／长按旋钮中心 | 原生编码器按下／释放，由桌面端解释长按 |
| 拖动摇杆 | 连续方向和距离；默认方向提示为计划、前进、侧栏、后退 |
| 点按 Agent 键 | 触发对应的桌面聊天快捷键 |
| 按住／松开命令键 | 保留原生按下／释放行为，支持按住说话等映射 |
| 右上角太阳／月亮 | 切换并保存主题 |
| 右上角齿轮 | 音量、静音和亮度设置 |
| 右上角游戏 | 打开本地游戏库 |

旋钮模式、命令键、摇杆和麦克风行为最终取决于桌面端设置。默认语音操作使用**电脑麦克风**，不使用 Tab5 麦克风。辅助键默认可能未分配，需要在桌面端启用独立麦克风键并配置。

## 构建与首次刷入

目前开发和配套桥接主要面向 **macOS**。需要 Python **3.11+**、Git、可用的 C++ 编译器，以及支持数据传输的 USB-C 线。硬件目标是 ESP32-P4 的 M5Stack Tab5（16MB Flash、32MB PSRAM）；不能刷入 ESP32-S3 设备。

在仓库根目录执行：

```sh
git clone https://github.com/atovk/tab5-codex-micro.git
cd tab5-codex-micro
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python tools/prepare_platform.py
.venv/bin/pio run
.venv/bin/python tools/build_title_font.py
```

`prepare_platform.py` 会获取并校验固定版本的 pioarduino 平台。项目使用 `.pio-core/` 隔离工具链，无需改动全局 SDK。完整动态标题字体由固定版本的 Noto Sans SC 生成，首次生成需要联网。

先查看设备列表，确认 Tab5 的 P4 端口，再刷入：

```sh
.venv/bin/python tools/verify_device.py discover
.venv/bin/python tools/flash.py --port /dev/cu.usbmodemXXXX --skip-build \
  --font-image artifacts/tab5-title-font-v1.bin
```

将 `/dev/cu.usbmodemXXXX` 替换为实际 Tab5 端口。首次刷入时如未出现应用端口，按住 Tab5 Reset 约两秒，看到内部绿灯快速闪烁后松开，进入下载模式。连接多个 Espressif 设备时务必明确选择 P4；不要将其他设备的 ROM 端口作为目标。

首次安装需要同时刷入字体，才能得到完整的动态标题显示。字体独立存放在 `0x510000`，后续普通固件更新会保留它：

```sh
.venv/bin/python tools/flash.py --port /dev/cu.usbmodemXXXX
```

刷入使用项目的 factory 镜像和分区表，会替换原有固件。若需要保留出厂系统，请先自行备份 Flash。

### 工具链

| 组件 | 固定版本 |
| --- | --- |
| pioarduino 平台 | 55.03.312-1 |
| Arduino-ESP32 | 3.3.12 |
| M5Unified | 0.2.28 |
| M5GFX | 0.2.35 |

## 可选：聊天标题和状态桥接

原生 Micro HID 灯光消息不包含聊天标题，也不提供当前聊天的 Fast／Plan／权限文本。本项目通过两个本地进程补充元数据，原生 USB HID 继续负责按键操作。

### 六个 Agent 键的标题

在桌面 **设置 → Codex Micro → Agent keys → Custom assignments** 中为六个键设置本地聊天。companion 只读取这些明确绑定的聊天，不猜测“最近聊天”的实时排序。

```sh
# 只检查配置，不打开硬件；默认不打印聊天标题。
.venv/bin/python tools/title_sync.py --dry-run

# 前台持续同步标题、按键配置和可用的聊天状态。
.venv/bin/python tools/title_sync.py --watch
```

也可安装 macOS 登录服务，替代前台的 `--watch` 进程：

```sh
.venv/bin/python tools/title_sync_service.py install
.venv/bin/python tools/title_sync_service.py status

# 停止并取消登录启动。
.venv/bin/python tools/title_sync_service.py stop --remove
```

服务引用当前仓库的 `.venv` 和脚本路径；移动仓库后需要重新安装。前台同步连接多台运行本固件的 Tab5 时，可用 `--port` 指定端口，或设置 `TAB5_SERIAL_NUMBER` 为 `discover` 列出的完整应用序列号来精确筛选。标题登录服务默认要求唯一设备；多设备服务需在个人 LaunchAgent 中设置该环境变量。

### 指定聊天的真实状态

另开终端，在同一仓库目录中运行：

```sh
.venv/bin/python tools/chat_state_bridge.py --thread-id CHAT_UUID
```

将 `CHAT_UUID` 替换为要跟踪的**本地聊天 UUID**，可从该聊天的链接中取得。保持 Codex 桌面应用和上述 `title_sync.py --watch` 或标题服务运行。桥接读取已有桌面本地 IPC，订阅该聊天接受的下一轮设置；不启动聊天、不发送消息。

设备会显示 **指定聊天** 和跟踪标题。**切换桌面上选中的聊天不会自动改变桥接目标**，也不会改变原生 Micro 命令的目标。应确保跟踪聊天和实际操作的聊天一致。

`chat_state_bridge.py` 当前以前台进程运行，按 Ctrl-C 停止；标题服务不会代为启动它。需要登录启动时，可自行将这条明确目标的命令加入个人 LaunchAgent。

### 元数据与隐私

标题 companion 读取本地保存的按键分配与所需标题；状态桥接仅保留标题、目标标识和有限的设置枚举，不保存对话正文或工具输出。元数据缓存位于 `~/Library/Caches/Tab5 Codex Micro/`。普通日志不打印聊天标题；`--show-titles`、`--once` 状态输出和设备 `STATUS` 是显式诊断，可能包含标题或聊天标识，公开前应检查内容。

## 游戏

| 游戏 | 操作 |
| --- | --- |
| 星际守卫 | 拖动飞船躲避敌机，自动射击；碰撞或漏过敌机会损失生命 |
| 俄罗斯方块 | 左移、旋转、右移、下落与速降；长按支持连移，轮廓提示落点 |
| 小恐龙跑酷 | 点击场地或跳跃键跨越仙人掌，速度随距离增加 |

游戏在 Tab5 本地运行，不向电脑发送游戏操作。返回聊天后保留本局进度，再进入时暂停；最高分保留到设备重启。

## 检查与开发

```sh
# 无需硬件的逻辑测试。
.venv/bin/python -m unittest discover -s tests -v

# 固件状态、原生 HID 状态请求和实机帧缓冲截图。
.venv/bin/python tools/verify_device.py status
.venv/bin/python tools/verify_device.py hid-status
.venv/bin/python tools/verify_device.py screenshot artifacts/tab5-screen.png
```

`hid-status` 仅请求状态，不触发按键。触摸、残影、标题滚动、霓虹与音频的回归工具，以及协议和实现记录见 [开发说明](docs/development.md)。当前整理版已通过 164 项主机测试及实机滚动、像素恢复和音效／触控验证；性能测量说明在开发文档中。

## 当前限制

- 设备是第三方协议兼容实现，桌面应用更新可能改变 Micro 协议或本地 IPC。桌面状态接口不兼容或元数据过期时显示未知，不以默认配置代替真实状态。
- 实时状态仅跟踪明确指定的本地聊天；Agent 标题仅支持已保存的 Custom assignments。未实现“当前选中聊天”自动跟随、远程聊天或动态最近列表的可靠标题匹配。
- 状态表示**下一次发送的设置**，不代表某个已运行回合的模式。“已发送”仅表示设备 USB 传输结果，不代表应用已经执行操作。
- 活动状态由桌面灯光颜色推断；自定义灯色可能影响状态文字。摇杆方向文字是默认提示，自定义映射仍由桌面决定。
- 标题使用 Noto Sans SC 的 BMP 字符覆盖；不支持的字符（例如部分 emoji）逐字符替换。每个标题传输上限为 **191 个 UTF-8 字节**，超出两行的已接收文本会滚动显示。
- 仅支持两个横屏方向的自动翻转，未设计竖屏布局。
- 为控制电脑 USB 供电负载，固件关闭电池充电、闲置无线模块和外部供电输出；运行时不会为电池充电。

## 许可与参考

原创代码采用 [MIT License](LICENSE)。字体采用 SIL Open Font License，TinyUSB 及其他依赖保留各自许可；详见 [第三方说明](THIRD_PARTY_NOTICES.md) 和 [字体许可](licenses/Tab5-UI-Sans-OFL.txt)。

- [M5Stack Tab5 文档](https://docs.m5stack.com/en/core/Tab5)
- [M5Stack USB-C 键盘示例](https://github.com/m5stack/M5Tab5-Keyboard-UserDemo)
- [Codex Micro 功能说明](https://learn.chatgpt.com/docs/features/codex-micro)
