#!/usr/bin/env python3
"""Prepare the pinned, project-local pioarduino platform for Tab5 builds.

Run before PlatformIO. No global package or toolchain files are changed.
The two patches accept the fork's post-release version spelling and preserve
the installed tool path after the platform's fresh-tool installation branch.
"""

from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from urllib.request import urlopen
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / ".pio" / "platform-tab5"
URL = (
    "https://github.com/pioarduino/platform-espressif32/releases/download/"
    "55.03.312-1/platform-espressif32.zip"
)
ARCHIVE_SHA256 = "f147d4ec6d657f451742df497811ebe3377db583ffd2e321e34b3793ff6b20a2"
ARCHIVE_PREFIX = "platform-espressif32-55.03.312-1"
MARKER = ".tab5-platform-patches.json"
PATCH_REVISION = 1


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def check_manifest(directory):
    manifest = json.loads((directory / "platform.json").read_text())
    if manifest.get("name") != "espressif32" or manifest.get("version") != "55.03.312":
        raise RuntimeError("Unexpected platform name/version in pinned archive")
    if manifest.get("repository", {}).get("url") != (
        "https://github.com/pioarduino/platform-espressif32.git"
    ):
        raise RuntimeError("Unexpected platform repository")
    packages = manifest["packages"]
    if not packages["framework-arduinoespressif32"]["version"].endswith(
        "/3.3.12/esp32-core-3.3.12.tar.xz"
    ):
        raise RuntimeError("Unexpected Arduino core dependency")
    if packages["toolchain-riscv32-esp"].get("package-version") != "14.2.0+20260121":
        raise RuntimeError("Unexpected RISC-V toolchain dependency")
    if not (directory / "boards" / "m5stack-tab5-p4.json").is_file():
        raise RuntimeError("Pinned platform is missing the Tab5 board")
    return manifest


def validate_ready(directory):
    marker_path = directory / MARKER
    if not marker_path.is_file():
        raise RuntimeError("Existing local platform has no preparation marker; refusing to overwrite it")
    metadata = json.loads(marker_path.read_text())
    if metadata.get("revision") != PATCH_REVISION or metadata.get("archive_sha256") != ARCHIVE_SHA256:
        raise RuntimeError("Existing local platform has a different preparation revision/archive")
    manifest = check_manifest(directory)
    if manifest["engines"]["platformio"] != ">=6.2.0-post.1":
        raise RuntimeError("Local platform engine patch is missing")
    for filename, expected in metadata.get("patched_files", {}).items():
        if filename not in ("platform.json", "platform.py") or digest(directory / filename) != expected:
            raise RuntimeError("Prepared platform changed unexpectedly: " + filename)
    if set(metadata.get("patched_files", {})) != {"platform.json", "platform.py"}:
        raise RuntimeError("Prepared platform marker is incomplete")


def obtain_archive(temporary_directory):
    core_directory = Path(os.environ.get("PLATFORMIO_CORE_DIR", Path.home() / ".platformio"))
    cache = core_directory / ".cache" / "downloads"
    preferred = cache / "9148c2f915639895a85d501d7f3c0c258e0b011a"
    candidates = [preferred]
    if cache.is_dir():
        candidates.extend(
            path for path in cache.iterdir()
            if path != preferred and path.is_file() and 1_000_000 <= path.stat().st_size <= 4_000_000
        )
    for path in candidates:
        if path.is_file() and digest(path) == ARCHIVE_SHA256:
            return path
    archive = temporary_directory / "platform.zip"
    with urlopen(URL, timeout=60) as response, archive.open("wb") as output:
        shutil.copyfileobj(response, output)
    if digest(archive) != ARCHIVE_SHA256:
        raise RuntimeError("Pinned platform archive SHA-256 mismatch")
    return archive


def extract_safely(archive, destination):
    destination.mkdir()
    with ZipFile(archive) as bundle:
        if sum(info.file_size for info in bundle.infolist()) > 64 * 1024 * 1024:
            raise RuntimeError("Pinned platform archive is unexpectedly large")
        for info in bundle.infolist():
            name = PurePosixPath(info.filename)
            if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != ARCHIVE_PREFIX:
                raise RuntimeError("Unsafe or unexpected platform archive path")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise RuntimeError("Platform archive contains a symbolic link")
            relative = Path(*name.parts[1:])
            target = destination / relative
            if not target.resolve().is_relative_to(destination.resolve()):
                raise RuntimeError("Platform archive path escapes destination")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                mode = (info.external_attr >> 16) & 0o777
                if mode:
                    target.chmod(mode)


def replace_once(source, original, replacement):
    if source.count(original) != 1:
        raise RuntimeError("Pinned platform patch anchor does not occur exactly once: " + repr(original))
    return source.replace(original, replacement, 1)


def main():
    if DESTINATION.exists():
        validate_ready(DESTINATION)
        print("Tab5 pinned platform ready: " + str(DESTINATION))
        return
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".prepare-platform-", dir=DESTINATION.parent) as temporary:
        temporary = Path(temporary)
        archive = obtain_archive(temporary)
        prepared = temporary / "platform"
        extract_safely(archive, prepared)
        check_manifest(prepared)
        manifest_path = prepared / "platform.json"
        manifest_path.write_text(replace_once(
            manifest_path.read_text(),
            '"platformio": ">=6.2.0"',
            '"platformio": ">=6.2.0-post.1"',
        ))
        platform_path = prepared / "platform.py"
        platform_path.write_text(replace_once(
            platform_path.read_text(),
            "        pm.install(tl_path)\n\n        logger.info(f\"Tool {tool_name} successfully installed\")",
            "        pm.install(tl_path)\n"
            "        self.packages[tool_name][\"version\"] = paths[\"tool_path\"]\n"
            "        self.packages[tool_name][\"optional\"] = False\n\n"
            "        logger.info(f\"Tool {tool_name} successfully installed\")",
        ))
        (prepared / MARKER).write_text(json.dumps({
            "revision": PATCH_REVISION,
            "upstream_url": URL,
            "archive_sha256": ARCHIVE_SHA256,
            "patched_files": {name: digest(prepared / name) for name in ("platform.json", "platform.py")},
        }, indent=2) + "\n")
        validate_ready(prepared)
        prepared.rename(DESTINATION)
    print("Tab5 pinned platform ready: " + str(DESTINATION))


if __name__ == "__main__":
    main()
