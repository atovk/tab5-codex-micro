"""Narrow, fail-closed link workaround for the pinned P4 TinyCrypt ECDH defect.

No framework files are changed. Source implementation: src/ble_ecc_compat.c.
Review/remove this workaround when changing SDKs; do not extend the allowlist
without inspecting the new archive and running the device crypto self-test.

Upstream fix: espressif/esp-idf@9fd7cb7e606a06111e1b14be7f4e00d77d9cf3dd
The pinned archive predates that fix despite its numerical 5.5.5 version.
"""

import hashlib
import json
from pathlib import Path


EXPECTED_VERSION = "5.5.5+sha.b774170ff46"
EXPECTED_VARIANT = "esp32p4_es"
EXPECTED_LIBBT_SHA256 = "2467f511f5884d2feebb7451391950b02eacb4111313ad8d9dd904856daf2fef"


def validate_sdk(framework_libs, chip_variant):
    root = Path(framework_libs)
    if chip_variant != EXPECTED_VARIANT:
        raise RuntimeError("Tab5 ECC workaround requires the reviewed esp32p4_es archive")
    if json.loads((root / "package.json").read_text(encoding="utf-8")).get("version") != EXPECTED_VERSION:
        raise RuntimeError("Tab5 ECC workaround requires the reviewed IDF b774170ff46 package")
    archive = root / chip_variant / "lib" / "libbt.a"
    if hashlib.sha256(archive.read_bytes()).hexdigest() != EXPECTED_LIBBT_SHA256:
        raise RuntimeError("Tab5 ECC workaround: libbt.a SHA-256 changed; review before building")


def register_platformio(environment):
    if environment.BoardConfig().get("build.mcu") != "esp32p4":
        raise RuntimeError("Tab5 ECC workaround is only supported for ESP32-P4")
    framework_libs = environment.PioPlatform().get_package_dir("framework-arduinoespressif32-libs")
    validate_sdk(framework_libs, environment.BoardConfig().get("build.chip_variant"))
    environment.AppendUnique(CPPDEFINES=[("TAB5_BLE_ECC_PINNED", 1)])
    environment.AppendUnique(LINKFLAGS=["-Wl,--wrap=uECC_shared_secret"])
    print("Tab5 BLE: pinned P-256 ECDH compatibility enabled; startup crypto self-test required")


try:
    Import("env")
except NameError:
    pass
else:
    register_platformio(env)
