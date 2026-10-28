# 下载版刷机说明

**简体中文** · [English](FLASHING.en.md)

适用版本：**Tab5 Codex Micro 0.12.0**。硬件为 ESP32-P4 的 **M5Stack Tab5，16MB Flash／32MB PSRAM**。本指南使用预编译发布包，无需克隆仓库、安装 PlatformIO 或编译固件。

本版支持 USB／蓝牙双模。**固件和字库仍通过 USB 写入，没有无线刷机功能**；刷入后按 [蓝牙连接说明](BLUETOOTH.md) 完成系统 PIN 配对及可选的标题／状态 companion 配置。本固件使用 Tab5 板载 C6 已有的兼容固件，无需单独刷写 C6，也不要刷写电脑上连接的其他开发板。其他 C6／ESP-Hosted 固件组合尚未验证。

## 1. 下载并解压

从 [0.12.0 Release](https://github.com/atovk/tab5-codex-micro/releases/tag/v0.12.0) 下载 **`tab5-codex-micro-0.12.0.zip`**，完整解压到一个目录。不要直接在压缩包中运行脚本，也不要只下载 GitHub 自动生成的 “Source code” 压缩包。

| 文件 | 用途 |
| --- | --- |
| `firmware.factory.bin` | 完整工厂镜像，包含启动程序、分区表和应用；写入 `0x0` |
| `title-font.bin` | 完整动态标题字库；写入 `0x510000` |
| `flash.py` | 无需源码构建环境的刷机工具 |
| `requirements-flash.txt` | 刷机所需 Python 依赖 |
| `manifest.json`、`SHA256SUMS` | 发布文件和校验信息 |
| `FLASHING.md`、`FLASHING.en.md` | 中英文刷机说明 |
| `BLUETOOTH.md`、`BLUETOOTH.en.md`、`COMPANION.md` | 系统配对、自动重连及可选本地同步说明 |
| `assets/tab5-light-0.12.0.png`、`assets/tab5-dark-0.12.0.png` | 英文示例界面的真实 Tab5 帧缓冲截图 |

请将这些文件保留在同一目录。发布页上的独立 `.bin` 文件也可供已有工作流使用；初次安装推荐完整 ZIP，以免遗漏字库或校验文件。

默认完整刷入会替换 Tab5 原有固件和分区布局，重置 NVS 中的蓝牙绑定、主题、音量、亮度和语言设置；已有蓝牙绑定失效后需要在电脑端删除旧记录并重新配对。若需要恢复出厂系统，请在操作前自行备份 Flash。此镜像不能刷到 ESP32-S3 或其他型号。

## 2. 准备 Python 环境

需要 Python **3.11+** 和一条 USB-C **数据线**。刷机工具支持 macOS／Linux；Windows 仅可离线运行 `--check`，不支持刷写。原生蓝牙和 companion 的本次实机验证在 macOS 上完成，不代表 Linux 的桌面蓝牙联动已经通过。

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

新安装默认使用 **English**。打开右上角齿轮，在 **Language／语言** 中切换中文或英文；语言、主题、声音、亮度和已保存的蓝牙绑定在普通重启后会保留；重新刷入 factory 镜像会重置这些设置与绑定。

在电脑 Codex 桌面应用的 **设置 → Codex Micro** 中配置设备。旋钮、摇杆和按键使用原生 Micro 映射。USB 数据连接优先；蓝牙需要先完成系统配对，步骤见 [蓝牙连接说明](BLUETOOTH.md)。本版保持电池充电关闭，蓝牙只替代数据连接，设备仍需电池或合适电源。

聊天标题和真实 Fast／Plan／权限状态需要额外运行 macOS 本地桥接，不由原生 HID 灯光消息提供。下载包的刷机依赖不包含 companion；按 [蓝牙连接说明](BLUETOOTH.md#标题与状态-companion) 或本版本 [中文 README](https://github.com/atovk/tab5-codex-micro/blob/v0.12.0/README.md#可选聊天标题和状态桥接) 准备源码及完整依赖。

独立只读“指定聊天设置 / Tracked settings”条显示跟踪聊天下一次发送采用的真实设置，包括 Full access；未同步或过期时显示未知。Fast／Plan／Approve 原生按钮不代表该聊天的设置或操作目标，界面因此明确显示“操作目标未核对 / Target not verified”。切换桌面选中的聊天不会改变桥接目标，桥接也不会将原生按键重定向到跟踪聊天。

“USB已提交／蓝牙已提交”只表示完整消息已交给对应传输栈，不是桌面执行回执；“USB未发送／蓝牙未发送”表示提交失败或队列拒绝，不承诺稍后发送。

## 后续更新

更新时推荐下载并完整解压新版本 ZIP，再按对应版本说明操作。若已安装兼容的完整标题字库，可跳过字库写入：

```sh
.venv/bin/python flash.py --port /dev/cu.usbmodemXXXX --skip-font
```

`--skip-font` **仍会写入 factory 镜像并覆盖 NVS，不保留蓝牙绑定或本地偏好**，仅跳过字库。首次安装不要使用它。如果新版本说明要求更新字库，应重新执行默认完整刷入。更新前暂停占用串口的工具，完成后可重新启动 companion。

下载包的 `flash.py` **没有 `--app-only` 模式**。确需保留绑定和偏好时，使用 [v0.12.0 源码工具](https://github.com/atovk/tab5-codex-micro/blob/v0.12.0/tools/flash.py)；先按该版本 [源码构建说明](https://github.com/atovk/tab5-codex-micro/blob/v0.12.0/README.md#从源码构建) 准备并构建，再运行：

```sh
# 在 v0.12.0 源码仓库内执行，不是在下载包目录内。
.venv/bin/python tools/flash.py --port /dev/cu.usbmodemXXXX --skip-build --app-only
```

此路径只写应用分区 `0x10000`。**仅当设备已经安装本项目兼容的分区布局、完整有效的标题字库，且本次更新不要求迁移时适用**；它不会检查设备旧分区表是否兼容，不可用于首次安装、恢复出厂系统或分区／字库升级。上述条件满足时，未改写的 NVS、绑定、偏好和字库可保留。本次实机已验证该应用更新后无需 PIN 重连；这不保证任意未来版本均可跳过完整刷入。

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
| 标题或状态显示未知 | 这是本地桥接配置或新鲜度问题；参考 README，不需因此反复刷机 |
| 刷完仍在下载模式 | 保持线缆连接，短按 Reset；如应用仍未出现，重新进入下载模式并运行工具 |

## 许可

原创代码采用 MIT License；字库采用 SIL Open Font License 1.1，其他组件保留各自许可。发布包附带相应许可文件；源码和第三方说明见 [项目仓库](https://github.com/atovk/tab5-codex-micro)。
