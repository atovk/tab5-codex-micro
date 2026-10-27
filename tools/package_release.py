#!/usr/bin/env python3
"""Assemble a checked, self-contained Tab5 binary release and ZIP archive."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from zipfile import ZipFile, ZIP_DEFLATED

from flash_release import FORMAT, FLASH_BYTES, FONT_OFFSET, inspect_firmware, inspect_font, validate_bundle


ROOT = Path(__file__).resolve().parents[1]


def tinyusb_license(source: str) -> str:
    comment = source.split("*/", 1)[0]
    lines = [re.sub(r"^\s*\* ?", "", line) for line in comment.splitlines()[1:]]
    text = "\n".join(lines).strip()
    if "Copyright (c) 2019 Ha Thach" not in text or "THE SOFTWARE IS PROVIDED" not in text:
        raise ValueError("TinyUSB copyright/license header is missing")
    return text + "\n"


def package(version: str, output: Path, firmware: Path, font: Path,
            source_commit: str | None = None, root: Path = ROOT) -> tuple[Path, Path]:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version):
        raise ValueError("Expected a semantic release version, without a leading v")
    if source_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("Source commit must be a full 40-character commit hash")
    firmware_data, font_data = firmware.read_bytes(), font.read_bytes()
    inspect_firmware(firmware_data, version)
    glyphs = inspect_font(font_data)
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
    args = parser.parse_args()
    output = args.output or ROOT / "artifacts" / ("release-" + args.version)
    directory, archive = package(args.version, output, args.firmware, args.font, args.source_commit)
    print(json.dumps({"directory": str(directory), "archive": str(archive),
                      "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}))
    return 0


if __name__ == "__main__":
    main()
