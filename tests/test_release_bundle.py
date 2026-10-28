"""Exercise release integrity and target selection without opening any USB port."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile
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
