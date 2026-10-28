"""Exercise release integrity and target selection without opening any USB port."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile
from urllib.parse import urlsplit
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import flash_release as flash
import package_release as packaging


def font_fixture():
    payload = struct.pack(">6I", 1, 11, 24, 0, 1, 0) + struct.pack(">7i", 65, 1, 1, 1, 1, 0, 0) + b"\xf0"
    return (b"T5VLW4\0\0" + struct.pack("<6I", 1, 64 + len(payload), 53, 52, 1,
                                       zlib.crc32(payload))).ljust(64, b"\0") + payload


def firmware_fixture(version="0.11.0", *, terminated=True):
    data = bytearray(b"\xff" * 0x10100)
    for offset in (0x2000, 0x10000):
        data[offset] = 0xE9
        struct.pack_into("<H", data, offset + 12, 18)
    entries = ((0, 0, 0x10000, 0x500000, b"app0"),
               (1, 0x82, flash.FONT_OFFSET, flash.FONT_CAPACITY, b"spiffs"))
    for index, (kind, subtype, offset, size, name) in enumerate(entries):
        struct.pack_into("<HBBII16sI", data, 0x8000 + 32 * index, 0x50AA, kind, subtype,
                         offset, size, name, 0)
    identifier = ("tab5-micro-" + version).encode("ascii") + (b"\0" if terminated else b"")
    data[0x10020:0x10020 + len(identifier)] = identifier
    return bytes(data)


def screenshot_fixture():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">2I5B", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0\x30\x4f\xfe")) + chunk(b"IEND", b""))


def port(name, pid=flash.APP_PID, serial_number="TAB5-0123456789AB", location="1-2"):
    return SimpleNamespace(device=name, vid=flash.VID, pid=pid, serial_number=serial_number,
                           description="Synthetic device", location=location)


class ReleaseFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tab5-release-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        for name in ("tools", "tools/vendor", "docs", "licenses"):
            (self.source / name).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "tools/flash_release.py", self.source / "tools/flash_release.py")
        shutil.copyfile(ROOT / "tools/vendor/hid_device.c", self.source / "tools/vendor/hid_device.c")
        for name in ("docs/FLASHING.md", "docs/FLASHING.en.md", "LICENSE", "THIRD_PARTY_NOTICES.md",
                     "licenses/Tab5-UI-Sans-OFL.txt"):
            (self.source / name).write_text("Synthetic release test documentation.\n")
        self.firmware = self.root / "firmware.bin"
        self.firmware.write_bytes(firmware_fixture())
        self.font = self.root / "font.bin"
        self.font.write_bytes(font_fixture())
        self.bundle, self.archive = packaging.package("0.11.0", self.root / "bundle", self.firmware,
                                                       self.font, "a" * 40, self.source)

    def prepare_ble_release(self):
        (self.source / "assets").mkdir()
        for theme in ("light", "dark"):
            (self.source / "assets" / f"tab5-{theme}-0.12.0.png").write_bytes(screenshot_fixture())
        for name in ("BLUETOOTH.md", "BLUETOOTH.en.md"):
            (self.source / "docs" / name).write_text(
                "蓝牙 guide: [中文](../README.md#可选聊天标题和状态桥接) "
                "[English](../README.en.md#optional-chat-titles-and-settings-bridge) "
                "[evidence](development.en.md).\n", encoding="utf-8")
        shutil.copyfile(ROOT / "tools/vendor/RFC5903-LICENSE", self.source / "tools/vendor/RFC5903-LICENSE")
        (self.source / "THIRD_PARTY_NOTICES.md").write_text(
            "[RFC5903](tools/vendor/RFC5903-LICENSE)\n", encoding="utf-8")
        self.firmware.write_bytes(firmware_fixture("0.12.0"))

    def prepare_microphone_release(self):
        self.prepare_ble_release()
        self.firmware.write_bytes(firmware_fixture("0.13.0"))
        (self.source / "docs/images").mkdir()
        for theme, page in (("light", "day"), ("dark", "night")):
            (self.source / "assets" / f"tab5-microphone-{theme}-0.13.0.png").write_bytes(screenshot_fixture())
            (self.source / "docs/images" / f"microphone-{page}.png").write_bytes(screenshot_fixture())
        for language in ("", ".en"):
            other_language = ".en" if not language else ""
            for guide in ("MICROPHONE", "BLUETOOTH", "FLASHING"):
                (self.source / "docs" / f"{guide}{language}.md").write_text(
                    "USB microphone uses the standard system audio input.\n"
                    f"[Other language]({guide}{other_language}.md)\n"
                    f"[Microphone](../docs/MICROPHONE{language}.md#input) "
                    f"[Bluetooth](BLUETOOTH{language}.md#pairing) "
                    f"[Flashing](FLASHING{language}.md)\n"
                    "![Day](images/microphone-day.png) ![Night](images/microphone-night.png)\n"
                    f"[Source](../README{language}.md#build) [Source tool](../tools/flash.py) "
                    f"[Development](development{language}.md#results)\n"
                    "[External](https://example.com/documentation) [Same page](#input)\n",
                    encoding="utf-8")

    def resign(self, manifest_change=None):
        manifest_path = self.bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        if manifest_change:
            manifest_change(manifest)
        for image in manifest["images"]:
            path = self.bundle / image["file"]
            image["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            image["bytes"] = path.stat().st_size
        manifest_path.write_text(json.dumps(manifest))
        paths = sorted(path for path in self.bundle.rglob("*") if path.is_file() and path.name != "SHA256SUMS")
        (self.bundle / "SHA256SUMS").write_text("".join(
            hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.relative_to(self.bundle).as_posix() + "\n"
            for path in paths))

    def test_complete_package_and_standalone_offline_check(self):
        bundle = flash.validate_bundle(self.bundle)
        self.assertEqual((bundle.version, bundle.glyph_count), ("0.11.0", 1))
        with ZipFile(self.archive) as archive:
            self.assertEqual(set(name.removeprefix("bundle/") for name in archive.namelist()),
                             flash.REQUIRED_FILES | {"SHA256SUMS"})
        result = subprocess.run([sys.executable, "-S", str(self.bundle / "flash.py"), "--check"],
                                cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Release 0.11.0 verified", result.stdout)
        manifest_text = (self.bundle / "manifest.json").read_text()
        self.assertNotIn(str(self.root), manifest_text)
        self.assertEqual(json.loads(manifest_text)["source_commit"], "a" * 40)

    def test_ble_package_includes_guides_companion_path_license_and_current_screenshots(self):
        self.prepare_ble_release()
        bundle, archive_path = packaging.package("0.12.0", self.root / "ble-bundle", self.firmware,
                                                  self.font, "b" * 40, self.source)
        screenshots = {f"assets/tab5-{theme}-0.12.0.png" for theme in ("light", "dark")}
        expected = flash.REQUIRED_FILES | {"SHA256SUMS", "BLUETOOTH.md", "BLUETOOTH.en.md",
                                           "COMPANION.md", "licenses/RFC5903-LICENSE"} | screenshots
        with ZipFile(archive_path) as archive:
            self.assertEqual({name.removeprefix("ble-bundle/") for name in archive.namelist()}, expected)
            for name in expected:
                self.assertEqual(archive.read("ble-bundle/" + name), (bundle / name).read_bytes())
        sums = dict(line.split("  ", 1)[::-1] for line in (bundle / "SHA256SUMS").read_text().splitlines())
        self.assertEqual(set(sums), expected - {"SHA256SUMS"})
        self.assertEqual((bundle / "licenses/RFC5903-LICENSE").read_bytes(),
                         (self.source / "tools/vendor/RFC5903-LICENSE").read_bytes())
        self.assertIn("(licenses/RFC5903-LICENSE)", (bundle / "THIRD_PARTY_NOTICES.md").read_text())
        for name in ("BLUETOOTH.md", "BLUETOOTH.en.md"):
            text = (bundle / name).read_text(encoding="utf-8")
            self.assertIn("https://github.com/atovk/tab5-codex-micro/blob/v0.12.0/README", text)
            self.assertNotIn("(../README", text)
            self.assertNotIn("(development.", text)
        companion = (bundle / "COMPANION.md").read_text(encoding="utf-8")
        for instruction in ("--branch v0.12.0", "-m pip install -r requirements.txt", "--app-only",
                            "requirements-flash.txt", "NVS", "macOS", "**简体中文**", "**English**"):
            self.assertIn(instruction, companion)
        manifest = json.loads((bundle / "manifest.json").read_text())
        self.assertEqual(set(manifest["screenshots"]), screenshots)
        self.assertEqual({image["file"] for image in manifest["images"]},
                         {"firmware.factory.bin", "title-font.bin"})
        self.assertEqual(flash.validate_bundle(bundle).version, "0.12.0")
        result = subprocess.run([sys.executable, "-S", str(bundle / "flash.py"), "--check"],
                                cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ble_package_requires_current_screenshots_and_license_without_partial_output(self):
        self.prepare_ble_release()
        screenshot = self.source / "assets/tab5-dark-0.12.0.png"
        data = screenshot.read_bytes()
        screenshot.unlink()
        output = self.root / "missing-assets"
        with self.assertRaises(FileNotFoundError):
            packaging.package("0.12.0", output, self.firmware, self.font, root=self.source)
        screenshot.write_bytes(data)
        (self.source / "tools/vendor/RFC5903-LICENSE").unlink()
        with self.assertRaises(FileNotFoundError):
            packaging.package("0.12.0", output, self.firmware, self.font, root=self.source)
        self.assertFalse(output.exists())
        self.assertFalse((self.root / "tab5-codex-micro-0.12.0.zip").exists())

    def test_ble_assets_are_covered_by_offline_checksums(self):
        self.prepare_ble_release()
        bundle, _ = packaging.package("0.12.0", self.root / "ble-bundle", self.firmware,
                                       self.font, root=self.source)
        for name in ("assets/tab5-light-0.12.0.png", "assets/tab5-dark-0.12.0.png",
                     "BLUETOOTH.md", "BLUETOOTH.en.md", "COMPANION.md", "licenses/RFC5903-LICENSE"):
            path = bundle / name
            original = path.read_bytes()
            path.write_bytes(original + b"corruption")
            with self.subTest(name=name), self.assertRaisesRegex(flash.ReleaseError, "SHA-256 mismatch"):
                flash.validate_bundle(bundle)
            path.write_bytes(original)

    def test_microphone_package_has_offline_guides_images_hashes_and_source_tag_links(self):
        self.prepare_microphone_release()
        bundle, archive_path = packaging.package("0.13.0", self.root / "mic-bundle", self.firmware,
                                                  self.font, "c" * 40, self.source)
        previews = {f"assets/tab5-microphone-{theme}-0.13.0.png" for theme in ("light", "dark")}
        guide_images = {f"images/microphone-{theme}.png" for theme in ("day", "night")}
        guides = {f"{guide}{language}.md" for guide in ("MICROPHONE", "BLUETOOTH", "FLASHING")
                  for language in ("", ".en")}
        expected = (flash.REQUIRED_FILES | {"SHA256SUMS", "COMPANION.md", "licenses/RFC5903-LICENSE"}
                    | guides | previews | guide_images)
        with ZipFile(archive_path) as archive:
            self.assertEqual({name.removeprefix("mic-bundle/") for name in archive.namelist()}, expected)
            for name in expected:
                self.assertEqual(archive.read("mic-bundle/" + name), (bundle / name).read_bytes())
            # All packaged guide navigation and inline images remain usable
            # after extraction. Source-only links resolve to this exact tag.
            for name in guides:
                text = archive.read("mic-bundle/" + name).decode("utf-8")
                for target in re.findall(r"!?\[[^\]\n]*\]\(([^)\s]+)\)", text):
                    parsed = urlsplit(target)
                    if not parsed.scheme and parsed.path:
                        self.assertIn("mic-bundle/" + parsed.path, archive.namelist())
                self.assertIn("(MICROPHONE" + (".en" if ".en." in name else "") + ".md#input)", text)
                self.assertIn("(images/microphone-day.png)", text)
                for path in ("README", "tools/flash.py", "docs/development"):
                    self.assertIn(f"https://github.com/atovk/tab5-codex-micro/blob/v0.13.0/{path}", text)
                self.assertNotIn("(../", text)
                self.assertIn("(https://example.com/documentation)", text)
                self.assertIn("(#input)", text)
        manifest = json.loads((bundle / "manifest.json").read_text())
        self.assertEqual(set(manifest["screenshots"]), previews)
        self.assertEqual({image["file"] for image in manifest["images"]},
                         {"firmware.factory.bin", "title-font.bin"})
        sums = dict(line.split("  ", 1)[::-1] for line in (bundle / "SHA256SUMS").read_text().splitlines())
        self.assertEqual(set(sums), expected - {"SHA256SUMS"})
        for name in sums:
            self.assertEqual(sums[name], hashlib.sha256((bundle / name).read_bytes()).hexdigest())
        # This is a native USB microphone release, without an experimental BLE
        # microphone bridge, virtual audio driver, or extra flashing dependency.
        self.assertEqual((bundle / "requirements-flash.txt").read_text(), "esptool==5.4.0\npyserial==3.5\n")
        self.assertFalse(any("ble_mic" in name.lower() or name.endswith((".swift", ".driver")) for name in expected))
        self.assertNotIn("BlackHole", (bundle / "COMPANION.md").read_text())
        result = subprocess.run([sys.executable, "-S", str(bundle / "flash.py"), "--check"],
                                cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Release 0.13.0 verified", result.stdout)

    def test_microphone_docs_and_images_are_verified_and_required_without_partial_output(self):
        self.prepare_microphone_release()
        bundle, _ = packaging.package("0.13.0", self.root / "mic-bundle", self.firmware,
                                       self.font, root=self.source)
        for name in ("MICROPHONE.md", "MICROPHONE.en.md", "images/microphone-day.png",
                     "images/microphone-night.png", "assets/tab5-microphone-light-0.13.0.png"):
            path = bundle / name
            original = path.read_bytes()
            path.write_bytes(original + b"corruption")
            with self.subTest(name=name), self.assertRaisesRegex(flash.ReleaseError, "SHA-256 mismatch"):
                flash.validate_bundle(bundle)
            path.write_bytes(original)
        for name in ("docs/MICROPHONE.md", "docs/MICROPHONE.en.md", "docs/images/microphone-day.png",
                     "docs/images/microphone-night.png"):
            path = self.source / name
            original = path.read_bytes()
            path.unlink()
            output = self.root / "missing" / "mic-bundle"
            with self.subTest(name=name), self.assertRaises(FileNotFoundError):
                packaging.package("0.13.0", output, self.firmware, self.font, root=self.source)
            self.assertFalse(output.exists())
            self.assertFalse((output.parent / "tab5-codex-micro-0.13.0.zip").exists())
            path.write_bytes(original)

    def test_microphone_release_accepts_explicit_screenshots_and_rejects_invalid_guide_image(self):
        self.prepare_microphone_release()
        explicit = self.root / "new-microphone-preview.png"
        explicit.write_bytes(screenshot_fixture())
        bundle, _ = packaging.package("0.13.0", self.root / "explicit" / "bundle", self.firmware,
                                       self.font, root=self.source, screenshots=[explicit])
        self.assertEqual(json.loads((bundle / "manifest.json").read_text())["screenshots"],
                         ["assets/new-microphone-preview.png"])
        self.assertEqual((bundle / "images/microphone-night.png").read_bytes(), screenshot_fixture())
        (self.source / "docs/images/microphone-day.png").write_bytes(b"not a PNG")
        with self.assertRaisesRegex(ValueError, "PNG screenshot"):
            packaging.package("0.13.0", self.root / "bad-image", self.firmware, self.font,
                              root=self.source, screenshots=[explicit])

    def test_explicit_console_screenshots_keep_bilingual_flashing_navigation_offline(self):
        self.prepare_microphone_release()
        self.firmware.write_bytes(firmware_fixture("0.13.1"))
        screenshots = [self.source / "docs/images" / f"tab5-0.13.1-{theme}-en.png"
                       for theme in ("light", "dark")]
        for path in screenshots:
            path.write_bytes(screenshot_fixture())
        for language in ("", ".en"):
            # Use the actual published guide structure, including links to
            # the current screenshots from both languages.
            guide = f"FLASHING{language}.md"
            shutil.copyfile(ROOT / "docs" / guide, self.source / "docs" / guide)
        bundle, archive_path = packaging.package(
            "0.13.1", self.root / "console-bundle", self.firmware, self.font,
            root=self.source, screenshots=screenshots)
        expected_previews = {"assets/" + path.name for path in screenshots}
        manifest = json.loads((bundle / "manifest.json").read_text())
        self.assertEqual(set(manifest["screenshots"]), expected_previews)
        with ZipFile(archive_path) as archive:
            for language in ("", ".en"):
                guide = f"FLASHING{language}.md"
                text = archive.read("console-bundle/" + guide).decode("utf-8")
                for preview in expected_previews:
                    self.assertIn(f"({preview})", text)
                    self.assertIn("console-bundle/" + preview, archive.namelist())
                    self.assertEqual(archive.read("console-bundle/" + preview), screenshot_fixture())
                self.assertIn("(FLASHING" + (".en" if not language else "") + ".md)", text)
                for target in re.findall(r"!?\[[^\]\n]*\]\(([^)\s]+)\)", text):
                    parsed = urlsplit(target)
                    if not parsed.scheme and parsed.path:
                        self.assertIn("console-bundle/" + parsed.path, archive.namelist(),
                                      f"Broken offline link in {guide}: {target}")
                self.assertNotIn("/blob/v0.13.1/docs/images/tab5-0.13.1-", text)
        # Guide rewrites and copied screenshots are all covered by the bundle's
        # standalone check; neither source checkout nor network is required.
        result = subprocess.run([sys.executable, "-S", str(bundle / "flash.py"), "--check"],
                                cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Release 0.13.1 verified", result.stdout)

    def test_explicit_screenshot_inputs_validate_format_and_reject_filename_collision(self):
        screenshot = self.root / "custom.png"
        screenshot.write_bytes(b"not a screenshot")
        with self.assertRaisesRegex(ValueError, "PNG screenshot"):
            packaging.package("0.11.0", self.root / "bad-png", self.firmware, self.font,
                              root=self.source, screenshots=[screenshot])
        screenshot.write_bytes(screenshot_fixture())
        duplicate = self.source / screenshot.name
        duplicate.write_bytes(screenshot_fixture())
        with self.assertRaisesRegex(ValueError, "Duplicate screenshot filename"):
            packaging.package("0.11.0", self.root / "duplicate-png", self.firmware, self.font,
                              root=self.source, screenshots=[screenshot, duplicate])
        bundle, _ = packaging.package("0.11.0", self.root / "custom" / "bundle", self.firmware, self.font,
                                       root=self.source, screenshots=[screenshot])
        self.assertEqual((bundle / "assets/custom.png").read_bytes(), screenshot_fixture())

    def test_corrupt_firmware_fails_before_usb_dependencies(self):
        path = self.bundle / "firmware.factory.bin"
        path.write_bytes(path.read_bytes() + b"corruption")
        with self.assertRaisesRegex(flash.ReleaseError, "SHA-256 mismatch"):
            flash.validate_bundle(self.bundle)

    def test_resigned_font_with_invalid_crc_is_rejected(self):
        path = self.bundle / "title-font.bin"
        data = bytearray(path.read_bytes())
        data[-1] ^= 0x10
        path.write_bytes(data)
        self.resign()
        with self.assertRaisesRegex(flash.ReleaseError, "CRC"):
            flash.validate_bundle(self.bundle)

    def test_wrong_image_target_and_partition_layout_are_rejected(self):
        path = self.bundle / "firmware.factory.bin"
        for offset, value, expected in ((0x1000C, 9, "ESP32-P4"), (0x8004, 1, "partition layout")):
            data = bytearray(firmware_fixture())
            struct.pack_into("<H" if offset == 0x1000C else "<I", data, offset, value)
            path.write_bytes(data)
            self.resign()
            with self.subTest(offset=offset), self.assertRaisesRegex(flash.ReleaseError, expected):
                flash.validate_bundle(self.bundle)

    def test_manifest_range_and_duplicate_image_are_rejected(self):
        original = (self.bundle / "manifest.json").read_text()
        changes = [lambda m: m["images"][1].update(offset=0x10000),
                   lambda m: m["images"].__setitem__(1, dict(m["images"][0])),
                   lambda m: m.update(chip="esp32s3"), lambda m: m.update(schema=True)]
        for change in changes:
            (self.bundle / "manifest.json").write_text(original)
            self.resign(change)
            with self.subTest(change=change), self.assertRaises(flash.ReleaseError):
                flash.validate_bundle(self.bundle)

    def test_checksums_reject_traversal_duplicate_and_missing_file(self):
        path = self.bundle / "SHA256SUMS"
        original = path.read_text()
        for invalid in (original + "0" * 64 + "  ../escape\n", original + original.splitlines()[0] + "\n",
                        "\n".join(line for line in original.splitlines() if not line.endswith("  flash.py")) + "\n"):
            path.write_text(invalid)
            with self.subTest(invalid=invalid[-80:]), self.assertRaises(flash.ReleaseError):
                flash.validate_bundle(self.bundle)

    def test_package_rejects_wrong_version_and_existing_output(self):
        with self.assertRaisesRegex(flash.ReleaseError, "version"):
            packaging.package("0.11.1", self.root / "new", self.firmware, self.font, root=self.source)
        with self.assertRaises(FileExistsError):
            packaging.package("0.11.0", self.bundle, self.firmware, self.font, root=self.source)

    def test_resigned_firmware_must_match_the_entire_release_version(self):
        path = self.bundle / "firmware.factory.bin"
        for embedded in ("0.11.0-rc.1", "0.11.01", "0.11.0.1", "0.11.0+other"):
            path.write_bytes(firmware_fixture(embedded))
            self.resign()
            with self.subTest(embedded=embedded), self.assertRaisesRegex(flash.ReleaseError, "version"):
                flash.validate_bundle(self.bundle)

    def test_exact_prerelease_is_valid_but_unterminated_or_mixed_versions_are_not(self):
        flash.inspect_firmware(firmware_fixture("0.11.0-rc.1"), "0.11.0-rc.1")
        invalid = [firmware_fixture(terminated=False),
                   firmware_fixture() + b"\0tab5-micro-0.11.1\0",
                   firmware_fixture().replace(b"tab5-micro-", b"xtab5-micro-")]
        for data in invalid:
            with self.subTest(data=data[-40:]), self.assertRaisesRegex(flash.ReleaseError, "version"):
                flash.inspect_firmware(data, "0.11.0")

    def test_chip_check_precedes_all_writes_and_failure_aborts(self):
        bundle = flash.validate_bundle(self.bundle)
        runner = Mock(side_effect=subprocess.CalledProcessError(2, ["esptool", "chip-id"]))
        with self.assertRaises(subprocess.CalledProcessError):
            flash.flash_verified(bundle, "/dev/cu.synthetic-s3", runner=runner)
        self.assertEqual(runner.call_count, 1)
        command = runner.call_args.args[0]
        self.assertIn("chip-id", command)
        self.assertEqual(command[command.index("--chip") + 1], "esp32p4")
        self.assertNotIn("write-mem", command)
        self.assertNotIn("write-flash", command)

    def test_flash_offsets_and_skip_font(self):
        bundle = flash.validate_bundle(self.bundle)
        for skip in (False, True):
            runner = Mock()
            flash.flash_verified(bundle, "/dev/cu.synthetic-p4", skip, runner=runner)
            commands = [call.args[0] for call in runner.call_args_list]
            self.assertEqual(len(commands), 4)
            self.assertIn("chip-id", commands[0])
            self.assertIn("write-flash", commands[2])
            self.assertIn("0x0", commands[2])
            self.assertEqual("0x510000" in commands[2], not skip)
            self.assertIn("watchdog-reset", commands[-1])


class PortSelectionTests(unittest.TestCase):
    def test_one_application_is_selected_but_rom_never_is(self):
        application, rom = port("app"), port("rom", flash.ROM_PID)
        self.assertIs(flash.choose_port([application], None), application)
        for ports in ([rom], [application, rom], [application, port("app2")], []):
            with self.subTest(ports=ports), self.assertRaises(flash.ReleaseError):
                flash.choose_port(ports, None)
        self.assertIs(flash.choose_port([application, rom], "rom"), rom)

    def test_wrong_application_identity_and_explicit_missing_port_fail(self):
        for target in (port("bad", serial_number="OTHER"), port("bad", 0x9999)):
            with self.subTest(target=target), self.assertRaises(flash.ReleaseError):
                flash.choose_port([target], "bad")
        with self.assertRaises(flash.ReleaseError):
            flash.choose_port([port("app")], "missing")

    def test_software_bootloader_excludes_preexisting_s3_rom(self):
        application = port("application")
        s3, p4 = port("s3", flash.ROM_PID, location="1-3"), port("new-p4", flash.ROM_PID)
        provider = Mock(side_effect=[[application, s3], [s3, p4]])
        serial_module = SimpleNamespace(Serial=Mock())
        with patch.object(flash.time, "sleep"):
            self.assertEqual(flash.enter_rom(application, serial_module, provider), "new-p4")
        connection = serial_module.Serial.return_value
        connection.write.assert_called_once_with(b"BOOTLOADER\n")
        connection.close.assert_called_once()

    def test_software_bootloader_rejects_wrong_usb_location_and_ambiguous_roms(self):
        application = port("application")
        wrong = port("wrong-rom", flash.ROM_PID, location="1-9")
        expected = port("expected-rom", flash.ROM_PID)
        for arrived in ([wrong], [wrong, expected]):
            provider = Mock(side_effect=[[application], arrived])
            serial_module = SimpleNamespace(Serial=Mock())
            with self.subTest(arrived=arrived), patch.object(flash.time, "sleep"):
                with self.assertRaises(flash.ReleaseError):
                    flash.enter_rom(application, serial_module, provider)
            serial_module.Serial.return_value.close.assert_called_once()

    def test_bootloader_serial_write_failure_always_closes_connection(self):
        serial_module = SimpleNamespace(Serial=Mock())
        serial_module.Serial.return_value.write.side_effect = OSError("synthetic write failure")
        with self.assertRaises(OSError):
            flash.enter_rom(port("application"), serial_module, lambda: [])
        serial_module.Serial.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
