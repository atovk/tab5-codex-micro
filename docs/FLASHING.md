# 下载版刷机说明

**简体中文** · [English](FLASHING.en.md)

适用版本：**Tab5 Codex Micro 0.13.1**。硬件为 ESP32-P4 的 **M5Stack Tab5，16MB Flash／32MB PSRAM**。本指南使用预编译发布包，无需克隆仓库、安装 PlatformIO 或编译固件。

本版改进三栏控制台、触摸确认和动态 Agent 列表的未同步显示，保留 USB／蓝牙双模控制和标准 USB 麦克风。麦克风音频仅通过 USB 传输，见 [麦克风说明](MICROPHONE.md)。**固件和字库仍通过 USB 写入，没有无线刷机功能**；刷入后按 [蓝牙连接说明](BLUETOOTH.md) 完成系统 PIN 配对及可选的标题／状态 companion 配置。本固件使用 Tab5 板载 C6 已有的兼容固件，无需单独刷写 C6，也不要刷写电脑上连接的其他开发板。其他 C6／ESP-Hosted 固件组合尚未验证。

## 1. 下载并解压

从 [0.13.1 Release](https://github.com/atovk/tab5-codex-micro/releases/tag/v0.13.1) 下载 **`tab5-codex-micro-0.13.1.zip`**，完整解压到一个目录。不要直接在压缩包中运行脚本，也不要只下载 GitHub 自动生成的 “Source code” 压缩包。

| 文件 | 用途 |
| --- | --- |
| `firmware.factory.bin` | 完整工厂镜像，包含启动程序、分区表和应用；写入 `0x0` |
| `title-font.bin` | 完整动态标题字库；写入 `0x510000` |
| `flash.py` | 无需源码构建环境的刷机工具 |
| `requirements-flash.txt` | 刷机所需 Python 依赖 |
| `manifest.json`、`SHA256SUMS` | 发布文件和校验信息 |
| `FLASHING.md`、`FLASHING.en.md` | 中英文刷机说明 |
| `BLUETOOTH.md`、`BLUETOOTH.en.md`、`COMPANION.md` | 系统配对、自动重连及可选本地同步说明 |
| `MICROPHONE.md`、`MICROPHONE.en.md` | 中英文 USB 麦克风说明 |
| `images/microphone-day.png`、`images/microphone-night.png` | 供离线查看的麦克风说明插图 |
| `assets/tab5-0.13.1-light-en.png`、`assets/tab5-0.13.1-dark-en.png` | English 界面的正常实机截图；真实项目聊天标题保留中文原文 |

请将这些文件保留在同一目录。发布页上的独立 `.bin` 文件也可供已有工作流使用；初次安装推荐完整 ZIP，以免遗漏字库或校验文件。

默认完整刷入会替换 Tab5 原有固件和分区布局，重置 NVS 中的蓝牙绑定、主题、音量、亮度和语言设置；已有蓝牙绑定失效后需要在电脑端删除旧记录并重新配对。若需要恢复出厂系统，请在操作前自行备份 Flash。此镜像不能刷到 ESP32-S3 或其他型号。

## 2. 准备 Python 环境

需要 Python **3.11+** 和一条 USB-C **数据线**。刷机工具支持 macOS／Linux；Windows 仅可离线运行 `--check`，不支持刷写。0.13.1 已完成 UI 与输入捕获回归；USB 麦克风沿用 0.13.0 的 macOS 验证，原生蓝牙和 companion 的详细结果沿用 0.12.0 基线。本轮未重跑全部蓝牙桌面联动，也未确认 Linux 桌面兼容性。

在终端进入解压目录，执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-flash.txt
.venv/bin/python flash.py --check
```

`--check` 只检查本地发布文件，不刷写设备；校验包含文件 SHA-256、P4 镜像布局、完整固件版本和字库格式。固件版本必须与发布清单精确匹配，不能将 RC 或更长版本号当作正式版。若提示校验失败，重新下载完整 ZIP；不要忽略或绕过失败继续刷入。

也可使用系统工具检查 `SHA256SUMS`：

```sh
# macOS
shasum -a 256 -c SHA256SUMS

# Linux
sha256sum -c SHA256SUMS
```

## 3. 连接并确认 Tab5 端口

将数据线接到 **Tab5 机身 USB-C**，保持供电。先关闭占用该串口的串口监视器和截图工具。如果已运行本项目的标题 companion，可停止前台进程，或用源码仓库中的 `tools/title_sync_service.py stop` 暂停登录服务；刷机工具也会使用兼容的串口锁协调访问。

```sh
.venv/bin/python flash.py --list-ports
```

确认哪个端口对应 Tab5。macOS 常见 `/dev/cu.usbmodemXXXX`，Linux 常见 `/dev/ttyACM0`。同时连接多个 Espressif 设备时请明确选择 P4 Tab5，不要仅凭 USB 厂商 ID 选择设备。

- 已运行本项目固件：选择 `Tab5 Codex Micro` 应用端口，工具会请求进入下载模式。
- 首次安装或应用端口不可用：按住 Tab5 **Reset 约两秒**，直到内部绿灯快速闪烁后松开，再运行 `--list-ports`，选择新出现的 P4 下载端口。

## 4. 刷入固件和字库

将示例端口替换为刚确认的实际端口：

```sh
# macOS 示例
.venv/bin/python flash.py --port /dev/cu.usbmodemXXXX

# Linux 示例
.venv/bin/python flash.py --port /dev/ttyACM0
```

工具会先校验发布文件，再使用 ESP32-P4 下载器写入：

| 地址 | 内容 |
| --- | --- |
| `0x0` | `firmware.factory.bin` |
| `0x510000` | `title-font.bin` |

请在工具完成之前保持连接和供电。工具负责处理 P4 下载模式的复位状态；无需手动操作寄存器。出现成功提示并重启后，Tab5 应进入控制台。

## 5. 首次启动

新安装默认使用 **English**。打开右上角齿轮，在 **Language／语言** 中切换中文或英文；语言、主题、声音、亮度、麦克风增益／静音和已保存的蓝牙绑定在普通重启后会保留；重新刷入 factory 镜像会重置这些设置与绑定。

在电脑 Codex 桌面应用的 **设置 → Codex Micro** 中配置设备。旋钮、摇杆和按键使用原生 Micro 映射。USB 数据连接优先；蓝牙需要先完成系统配对，步骤见 [蓝牙连接说明](BLUETOOTH.md)。本版保持电池充电关闭，蓝牙只替代数据连接，设备仍需电池或合适电源。

使用 USB 麦克风时，在 macOS 声音输入或录音应用中选择 **Tab5 Microphone**，再打开 Tab5 的 **齿轮 → 麦克风** 调整电平、静音与增益。无需 companion 或虚拟音频驱动；Voice 保留桌面映射，不自动切换输入。蓝牙传输控制与元数据，不传输麦克风音频，详见 [麦克风说明](MICROPHONE.md)。

聊天标题和真实 Fast／Plan／权限状态需要额外运行 macOS 本地桥接，不由原生 HID 灯光消息提供。下载包的刷机依赖不包含 companion；按 [蓝牙连接说明](BLUETOOTH.md#标题与状态-companion) 或本版本 [中文 README](https://github.com/atovk/tab5-codex-micro/blob/v0.13.1/README.md#可选聊天标题和状态桥接) 准备源码及完整依赖。

控制台采用三个等宽面板，左右圆盘同尺寸；较深的摇杆增加对比度，圆内箭头可点按，与拖动共用方向阈值。中央标题充分用宽，单行显示，超长时横向滚动。尚未真实按过旋钮中心时，每次开机显示最多 8 秒操作提示；真实按下后持久记住，诊断捕获与冻结不消耗提示。未分配键使用平面空态，只显示一次“未分配”。已同步且仍新鲜、明确为 Approve／Reject／Send 的映射需要在键内松手才提交，滑出可取消未提交动作；Voice、旋钮中心、Agent 和自定义映射沿用原有按下／释放行为。

无需把动态最近 Agent 列表改成固定六个聊天。首次 Agent 操作前，中央“指定聊天 / Tracked chat”和按钮上方的连续只读状态条可显示显式桥接聊天的标题及下一次发送的 Fast／Plan／权限设置，包括 Full access；未同步或过期时显示未知。Agent 消息实际交给 USB／BLE 传输栈后，清除旧标题与设置并显示 **Agent NN / Title not synced**，锁存到重启，后续桥接心跳不会恢复旧聊天。桌面没有导出可靠的动态槽位标题映射，此空态不证明桌面焦点；桥接不自动跟随选中聊天，也不重定向原生按键。状态条保留“操作目标未核对 / Target not verified”。

主屏区分 USB／BLE 链路和 Tracked chat 同步，连接详情标明桌面执行未知（原生协议无 ACK）。底部按钮图标与主标题固定；传输提交用短暂描边表示消息已交给传输栈，不表示执行成功。“USB未发送／蓝牙未发送”失败提示仍保留，表示提交失败或队列拒绝，不承诺稍后发送。[白天截图](images/tab5-0.13.1-light-en.png) 和 [黑夜截图](images/tab5-0.13.1-dark-en.png) 的界面为 English，真实跟踪标题“部署 Codex micro 到 Tab5”保留原文；截图不证明原生桌面执行。

## 后续更新

更新时推荐下载并完整解压新版本 ZIP，再按对应版本说明操作。若已安装兼容的完整标题字库，可跳过字库写入：

```sh
.venv/bin/python flash.py --port /dev/cu.usbmodemXXXX --skip-font
```

`--skip-font` **仍会写入 factory 镜像并覆盖 NVS，不保留蓝牙绑定或本地偏好**，仅跳过字库。首次安装不要使用它。如果新版本说明要求更新字库，应重新执行默认完整刷入。更新前暂停占用串口的工具，完成后可重新启动 companion。

下载包的 `flash.py` **没有 `--app-only` 模式**。确需保留绑定和偏好时，使用 [v0.13.1 源码工具](https://github.com/atovk/tab5-codex-micro/blob/v0.13.1/tools/flash.py)；先按该版本 [源码构建说明](https://github.com/atovk/tab5-codex-micro/blob/v0.13.1/README.md#从源码构建) 准备并构建，再运行：

```sh
# 在 v0.13.1 源码仓库内执行，不是在下载包目录内。
.venv/bin/python tools/flash.py --port /dev/cu.usbmodemXXXX --skip-build --app-only
```

此路径只写应用分区 `0x10000`。**仅当设备已经安装本项目兼容的分区布局、完整有效的标题字库，且本次更新不要求迁移时适用**；它不会检查设备旧分区表是否兼容，不可用于首次安装、恢复出厂系统或分区／字库升级。上述条件满足时，未改写的 NVS、绑定、偏好和字库可保留。0.12.0 基线已验证该应用更新后无需 PIN 重连；麦克风更新确认了绑定与麦克风偏好保留，没有重跑全部 BLE 联动。这不保证任意未来版本均可跳过完整刷入。

## 故障处理

| 情况 | 处理 |
| --- | --- |
| 未列出 Tab5 | 确认使用数据线、插在机身 USB-C，重新连接；必要时进入下载模式后重新列出端口 |
| 找到多个设备 | 用 `--port` 指定已确认的 P4 Tab5，不拔动其他设备或尝试未知端口 |
| 串口忙 | 关闭串口监视器、截图脚本，暂停 companion 后重试 |
| Linux 提示权限不足 | 确认当前用户拥有该串口设备访问权限，再重新连接 |
| 校验失败或缺文件 | 重新下载完整发布 ZIP，全部解压到同一目录 |
| 下载器识别为 ESP32-S3 | 停止操作，重新确认 Tab5 的 P4 端口 |
| 字体或标题不正常 | 首次安装应包含 `title-font.bin`；重新执行不带 `--skip-font` 的完整刷入 |
| 标题或状态显示未知 | 检查桥接配置及新鲜度；提交动态 Agent 操作后的 Title not synced 是预期行为，锁存到重启，不需反复刷机 |
| Tab5 界面能动，但原生操作无响应 | 确认电脑已解锁、桌面位于目标聊天且 Micro 映射正确；仍无响应时可完全退出并重新打开桌面应用，⌘R 可能无效。历史故障根因尚未确认，本版不宣称永久修复；companion 正常或输入计数增加不证明桌面已执行 |
| 刷完仍在下载模式 | 保持线缆连接，短按 Reset；如应用仍未出现，重新进入下载模式并运行工具 |

## 许可

原创代码采用 MIT License；字库采用 SIL Open Font License 1.1，其他组件保留各自许可。发布包附带相应许可文件；源码和第三方说明见 [项目仓库](https://github.com/atovk/tab5-codex-micro)。
