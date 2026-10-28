#!/usr/bin/env python3
"""Assemble a checked, self-contained Tab5 binary release and ZIP archive."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import tempfile
from zipfile import ZipFile, ZIP_DEFLATED

from flash_release import FORMAT, FLASH_BYTES, FONT_OFFSET, inspect_firmware, inspect_font, validate_bundle


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/atovk/tab5-codex-micro"


def tinyusb_license(source: str) -> str:
    comment = source.split("*/", 1)[0]
    lines = [re.sub(r"^\s*\* ?", "", line) for line in comment.splitlines()[1:]]
    text = "\n".join(lines).strip()
    if "Copyright (c) 2019 Ha Thach" not in text or "THE SOFTWARE IS PROVIDED" not in text:
        raise ValueError("TinyUSB copyright/license header is missing")
    return text + "\n"


def companion_instructions(version: str) -> str:
    source = f"{REPOSITORY}/tree/v{version}"
    return f"""# Companion / 本地桥接

**简体中文**

本下载包包含固件、字库和 USB 刷机工具。`flash.py` 与
`requirements-flash.txt` 仅用于刷机，不会安装聊天标题、真实 Fast／Plan／权限状态
或蓝牙元数据同步所需的 companion。原生 Micro 按键和灯效由 Codex 桌面应用处理。

在 macOS 上另取 [v{version} 完整源码]({source})，在源码目录按以下步骤安装：

```sh
git clone --branch v{version} --depth 1 {REPOSITORY}.git
cd tab5-codex-micro
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

请使用 Python 3.11–3.14，安装完整 `requirements.txt`（包括固定版本 Bleak），
不要将此下载包的 `flash.py` 当作源码的 `tools/flash.py`。
随后按 [该版本中文 README]({REPOSITORY}/blob/v{version}/README.md#可选聊天标题和状态桥接)
配置标题 companion 与指定聊天状态桥接。配对和自动重连见 [蓝牙指南](BLUETOOTH.md)。

此包的 factory 刷入从地址 0 开始，会重置 NVS 内的蓝牙绑定和本地偏好；
`--skip-font` 只保留字库。已有安装需要保留绑定、偏好和字库时，请按该版本 README
使用完整源码中的 `tools/flash.py --app-only` 更新；只适用于分区布局兼容、
现有字库有效且该版本不要求数据迁移的情况，具体条件见该版本 README。

**English**

This download contains firmware, fonts, and a USB flashing helper. `flash.py`
and `requirements-flash.txt` only support flashing. They do not install the
companion needed for chat titles, real Fast/Plan/permission state, or Bluetooth
metadata synchronization. The Codex desktop app handles native Micro inputs
and lighting.

On macOS, obtain the [complete v{version} source]({source}) and run the commands
above from that source checkout. Use Python 3.11–3.14 and install the complete
`requirements.txt`, including pinned Bleak. The download's `flash.py` is distinct
from the source checkout's `tools/flash.py`. Follow the
[versioned English README]({REPOSITORY}/blob/v{version}/README.en.md#optional-chat-titles-and-settings-bridge)
to configure both the title companion and the designated-chat state bridge.
See the [Bluetooth guide](BLUETOOTH.en.md) for pairing and reconnection.

The factory image writes from address 0 and resets NVS Bluetooth bonds and
local preferences. `--skip-font` preserves only the font. To preserve bonds,
preferences, and fonts on an existing installation, use the complete source
checkout's `tools/flash.py --app-only` as documented in that version's README.
This requires a compatible partition layout, a valid existing font, and no
required data migration; check the version's update requirements first.
"""


def bluetooth_instructions(source: str, version: str) -> str:
    """Keep guides useful without pretending the binary bundle is a source checkout."""
    for name in ("README.md", "README.en.md"):
        source = source.replace(f"(../{name}", f"({REPOSITORY}/blob/v{version}/{name}")
    for name in ("development.md", "development.en.md"):
        source = source.replace(f"({name}", f"({REPOSITORY}/blob/v{version}/docs/{name}")
    return source


def screenshot_data(path: Path) -> bytes:
    data = path.read_bytes()
    if (not 33 <= len(data) <= 32 * 1024 * 1024 or path.suffix.lower() != ".png"
            or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR"
            or struct.unpack_from(">I", data, 8)[0] != 13
            or not all(0 < size <= 8192 for size in struct.unpack_from(">II", data, 16))):
        raise ValueError("Expected a PNG screenshot with a valid image header: " + str(path))
    return data


def package(version: str, output: Path, firmware: Path, font: Path,
            source_commit: str | None = None, root: Path = ROOT, *,
            screenshots: list[Path] | None = None) -> tuple[Path, Path]:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version):
        raise ValueError("Expected a semantic release version, without a leading v")
    if source_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("Source commit must be a full 40-character commit hash")
    firmware_data, font_data = firmware.read_bytes(), font.read_bytes()
    inspect_firmware(firmware_data, version)
    glyphs = inspect_font(font_data)
    includes_ble = tuple(map(int, version.split("-", 1)[0].split("."))) >= (0, 12, 0)
    if screenshots is None:
        screenshots = [root / "assets" / f"tab5-{theme}-{version}.png"
                       for theme in ("light", "dark")] if includes_ble else []
    if includes_ble and not screenshots:
        raise ValueError("USB/BLE releases must include current screenshots")
    screenshot_images = {}
    for path in screenshots:
        name = "assets/" + path.name
        if name in screenshot_images:
            raise ValueError("Duplicate screenshot filename: " + path.name)
        screenshot_images[name] = screenshot_data(path)
    output = output.resolve()
    archive = output.parent / f"tab5-codex-micro-{version}.zip"
    if output.exists() or archive.exists():
        raise FileExistsError("Release destination already exists; choose a new output directory/archive")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".tab5-release-", dir=output.parent) as temp:
        prepared = Path(temp) / output.name
        (prepared / "licenses").mkdir(parents=True)
        (prepared / "firmware.factory.bin").write_bytes(firmware_data)
        (prepared / "title-font.bin").write_bytes(font_data)
        sources = {
            "flash.py": "tools/flash_release.py",
            "FLASHING.md": "docs/FLASHING.md", "FLASHING.en.md": "docs/FLASHING.en.md",
            "LICENSE": "LICENSE", "THIRD_PARTY_NOTICES.md": "THIRD_PARTY_NOTICES.md",
            "licenses/Tab5-UI-Sans-OFL.txt": "licenses/Tab5-UI-Sans-OFL.txt",
        }
        for destination, source in sources.items():
            shutil.copyfile(root / source, prepared / destination)
        if includes_ble:
            shutil.copyfile(root / "tools/vendor/RFC5903-LICENSE", prepared / "licenses/RFC5903-LICENSE")
            notices = (prepared / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
            (prepared / "THIRD_PARTY_NOTICES.md").write_text(notices.replace(
                "(tools/vendor/RFC5903-LICENSE)", "(licenses/RFC5903-LICENSE)"), encoding="utf-8")
            for name in ("BLUETOOTH.md", "BLUETOOTH.en.md"):
                (prepared / name).write_text(bluetooth_instructions(
                    (root / "docs" / name).read_text(encoding="utf-8"), version), encoding="utf-8")
            (prepared / "COMPANION.md").write_text(companion_instructions(version), encoding="utf-8")
        for name, data in screenshot_images.items():
            (prepared / name).parent.mkdir(parents=True, exist_ok=True)
            (prepared / name).write_bytes(data)
        (prepared / "licenses/TinyUSB-MIT.txt").write_text(
            tinyusb_license((root / "tools/vendor/hid_device.c").read_text()), encoding="utf-8")
        (prepared / "requirements-flash.txt").write_text("esptool==5.4.0\npyserial==3.5\n", encoding="ascii")
        images = [{"file": name, "offset": offset, "bytes": len(data),
                   "sha256": hashlib.sha256(data).hexdigest()}
                  for name, offset, data in (("firmware.factory.bin", 0, firmware_data),
                                             ("title-font.bin", FONT_OFFSET, font_data))]
        manifest = {"format": FORMAT, "schema": 1, "version": version, "chip": "esp32p4",
                    "board": "M5Stack Tab5", "flash_bytes": FLASH_BYTES, "images": images,
                    "font": {"format": "T5VLW4", "pixels": 24, "glyphs": glyphs},
                    "flashing_platforms": ["macOS", "Linux"]}
        if source_commit is not None:
            manifest["source_commit"] = source_commit
        if screenshot_images:
            manifest["screenshots"] = sorted(screenshot_images)
        (prepared / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="ascii")
        files = sorted(path for path in prepared.rglob("*") if path.is_file())
        sums = "".join(hashlib.sha256(path.read_bytes()).hexdigest() + "  "
                       + path.relative_to(prepared).as_posix() + "\n" for path in files)
        (prepared / "SHA256SUMS").write_text(sums, encoding="ascii")
        validate_bundle(prepared)
        temporary_zip = Path(temp) / "release.zip"
        with ZipFile(temporary_zip, "w", compression=ZIP_DEFLATED, compresslevel=9) as bundle:
            for path in sorted(prepared.rglob("*")):
                if path.is_file():
                    bundle.write(path, output.name + "/" + path.relative_to(prepared).as_posix())
        prepared.rename(output)
        temporary_zip.rename(archive)
    return output, archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Version without the v prefix")
    parser.add_argument("--output", type=Path, help="New directory; defaults to artifacts/release-VERSION")
    parser.add_argument("--firmware", type=Path, default=ROOT / ".pio/build/tab5/firmware.factory.bin")
    parser.add_argument("--font", type=Path, default=ROOT / "artifacts/tab5-title-font-v1.bin")
    parser.add_argument("--source-commit", help="Full commit hash used to build the release")
    parser.add_argument("--screenshot", action="append", type=Path,
                        help="PNG to include; repeat for themes. USB/BLE releases default to assets/tab5-{light,dark}-VERSION.png")
    args = parser.parse_args()
    output = args.output or ROOT / "artifacts" / ("release-" + args.version)
    directory, archive = package(args.version, output, args.firmware, args.font, args.source_commit,
                                 screenshots=args.screenshot)
    print(json.dumps({"directory": str(directory), "archive": str(archive),
                      "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}))
    return 0


if __name__ == "__main__":
    main()
