#!/usr/bin/env python3
"""Optionally snapshot an existing local cache into the project's build core.

This reads the existing shared cache but never changes ~/.platformio. On macOS
APFS copy-on-write clones keep the large compiler and SDK snapshot inexpensive.
This is an offline acceleration helper, not a prerequisite for a fresh clone.
The normal build is ``python tools/prepare_platform.py`` followed by ``pio run``;
the pinned platform installs missing packages into platformio.ini's .pio-core.
"""

from pathlib import Path
import json
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / ".pio-core"
SHARED = Path.home() / ".platformio"
PACKAGES = {
    "framework-arduinoespressif32": ("3.3.12", "framework-arduinoespressif32@src-ccf592cf72512cadab4ff6565a2abeb3"),
    "framework-arduinoespressif32-libs": ("5.5.5+sha.b774170ff46", "framework-arduinoespressif32-libs"),
    "toolchain-riscv32-esp": ("14.2.0+20260121", "toolchain-riscv32-esp@src-38f6e19493df22aa87778d1d73613ea6"),
    "tool-riscv32-esp-elf-gdb": ("17.1.0+20260402", "tool-riscv32-esp-elf-gdb"),
    "tool-esptoolpy": ("5.4.0", "tool-esptoolpy@src-64057b3feeaacfca987b0a078959770a"),
    "tool-esp_install": ("5.3.5", "tool-esp_install"),
    "tool-scons": ("4.41101.0", "tool-scons"),
    "contrib-piohome": ("3.4.4", "contrib-piohome"),
}


def manifest(path):
    return json.loads((path / "package.json").read_text())


def clone(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.run(["/bin/cp", "-cR", str(source), str(destination)], check=True)
    else:
        shutil.copytree(source, destination, symlinks=True)


def prepare():
    CORE.mkdir(exist_ok=True)
    for name, (version, preferred) in PACKAGES.items():
        destination = CORE / "packages" / name
        if destination.exists():
            if manifest(destination).get("version") != version:
                raise RuntimeError("Unexpected version in isolated package: " + name)
            continue
        candidates = [SHARED / "packages" / preferred, SHARED / "packages" / name]
        source = next((p for p in candidates if (p / "package.json").is_file()
                       and manifest(p).get("version") == version), None)
        if source is None:
            raise RuntimeError(
                "Pinned cached package is unavailable: " + name + " " + version
                + ". This optional helper needs an existing local cache. "
                "For a fresh checkout, install requirements.txt into .venv, then run "
                ".venv/bin/python tools/prepare_platform.py and .venv/bin/pio run; "
                "the platform will download its own dependencies."
            )
        clone(source, destination)
        if manifest(destination).get("version") != version:
            raise RuntimeError("Package changed during snapshot: " + name)
        # Rewrite source URIs only in our private package metadata. Compiler
        # metadata otherwise refers to the shared core's IDF tools directory.
        metadata = destination / ".piopm"
        if metadata.exists():
            data = json.loads(metadata.read_text())
            if str(data.get("spec", {}).get("uri", "")).startswith("file:"):
                data["spec"]["uri"] = destination.as_uri()
                metadata.write_text(json.dumps(data))
        print("Isolated " + name + " " + version, flush=True)

    # Use an independent clone of the already project-local Python environment.
    # It includes pioarduino 6.2.0.post1 and the serial/JSON build dependencies.
    penv = CORE / "penv"
    if not penv.exists():
        clone(ROOT / ".venv", penv)
        old_prefix = str(ROOT / ".venv")
        for entry in (penv / "bin").iterdir():
            if entry.is_symlink() or not entry.is_file():
                continue
            content = entry.read_bytes()
            if content.startswith(b"#!") and old_prefix.encode() in content[:512]:
                entry.write_bytes(content.replace(old_prefix.encode(), str(penv).encode()))
        uv_source = SHARED / "penv" / "bin" / "uv"
        if uv_source.is_file() and not (penv / "bin" / "uv").exists():
            shutil.copy2(uv_source, penv / "bin" / "uv")
    # pioarduino may otherwise inherit the shared editable esptool import.
    for pth in penv.glob("lib/python*/site-packages/__editable__.esptool-*.pth"):
        pth.unlink()
    subprocess.run([str(penv / "bin" / "python"), "-c",
                    "import platformio; assert platformio.__version__ == '6.2.0.post1'"], check=True)
    print("Ready: PLATFORMIO_CORE_DIR=" + str(CORE) + " " + str(penv / "bin" / "pio") + " run")


if __name__ == "__main__":
    prepare()
