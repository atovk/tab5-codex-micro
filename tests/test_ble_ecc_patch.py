"""SDK identity guard plus execution of production ECDH using host mbedTLS."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ble_ecc_patch", ROOT / "tools/ble_ecc_patch.py")
PATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCH)
SDK = ROOT / ".pio-core/packages/framework-arduinoespressif32-libs"


class IdentityTests(unittest.TestCase):
    def test_installed_sdk_identity(self):
        if not SDK.is_dir():
            self.skipTest("pinned PlatformIO dependencies are not installed")
        PATCH.validate_sdk(SDK, PATCH.EXPECTED_VARIANT)

    def fixture(self, directory, version=PATCH.EXPECTED_VERSION, archive=b"modified"):
        root = Path(directory)
        (root / "package.json").write_text(json.dumps({"version": version}))
        path = root / PATCH.EXPECTED_VARIANT / "lib/libbt.a"
        path.parent.mkdir(parents=True)
        path.write_bytes(archive)
        return root

    def test_unknown_variant_fails_before_read(self):
        for variant in (None, "esp32p4", "esp32s3", "../esp32p4_es"):
            with self.subTest(variant=variant), self.assertRaises(RuntimeError):
                PATCH.validate_sdk("/nonexistent", variant)

    def test_unknown_version_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory, "5.5.5+sha.unreviewed")
            with self.assertRaisesRegex(RuntimeError, "package"):
                PATCH.validate_sdk(root, PATCH.EXPECTED_VARIANT)

    def test_archive_change_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                PATCH.validate_sdk(root, PATCH.EXPECTED_VARIANT)

    def test_only_reviewed_identity_adds_wrapper(self):
        class Environment:
            flags = []
            def BoardConfig(self): return {"build.mcu": "esp32p4", "build.chip_variant": PATCH.EXPECTED_VARIANT}
            def PioPlatform(self): return self
            def get_package_dir(self, name): return "sdk"
            def AppendUnique(self, **kwargs): self.flags.append(kwargs)
        environment = Environment()
        with patch.object(PATCH, "validate_sdk", side_effect=RuntimeError("unreviewed")):
            with self.assertRaises(RuntimeError):
                PATCH.register_platformio(environment)
        self.assertEqual(environment.flags, [])
        with patch.object(PATCH, "validate_sdk") as validate:
            PATCH.register_platformio(environment)
            validate.assert_called_once_with("sdk", PATCH.EXPECTED_VARIANT)
        self.assertEqual(environment.flags, [
            {"CPPDEFINES": [("TAB5_BLE_ECC_PINNED", 1)]},
            {"LINKFLAGS": ["-Wl,--wrap=uECC_shared_secret"]},
        ])


class CryptoExecutionTests(unittest.TestCase):
    def test_known_answers_and_rejections(self):
        compiler = shutil.which("cc")
        candidates = [Path("/opt/homebrew/opt/mbedtls"), Path("/usr/local/opt/mbedtls"), Path("/usr")]
        mbedtls = next((p for p in candidates if (p / "include/mbedtls/ecdh.h").is_file()), None)
        tinycrypt = SDK / "esp32p4_es/include/bt/common/tinycrypt/include"
        if not compiler or not mbedtls or not tinycrypt.is_dir():
            self.skipTest("C compiler, host mbedTLS development files and SDK headers are required")
        with tempfile.TemporaryDirectory(prefix="tab5-ecc-test-") as directory:
            root = Path(directory)
            (root / "sdkconfig.h").write_text(
                "#define CONFIG_IDF_TARGET_ESP32P4 1\n"
                "#define CONFIG_BT_SMP_CRYPTO_STACK_TINYCRYPT 1\n"
                "#define CONFIG_BT_NIMBLE_SM_SC 1\n"
                "#define CONFIG_MBEDTLS_ECDH_C 1\n")
            (root / "esp_random.h").write_text("#include <stddef.h>\nvoid esp_fill_random(void *, size_t);\n")
            idf = SDK / "esp32p4_es/include/esp_common/include"
            binary = root / "probe"
            command = [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", "-pedantic",
                       "-DTAB5_BLE_ECC_PINNED=1", "-I", str(root), "-I", str(tinycrypt),
                       "-I", str(idf), "-I", str(mbedtls / "include"),
                       str(ROOT / "tests/test_ble_ecc_compat.c"), "-L", str(mbedtls / "lib"),
                       "-lmbedcrypto", "-o", str(binary)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("tests passed", result.stdout)

    def test_independent_leading_zero_vector(self):
        try:
            from cryptography.hazmat.primitives.asymmetric import ec
        except ImportError:
            self.skipTest("Python cryptography is unavailable")
        x = ec.derive_private_key(379, ec.SECP256R1()).public_key().public_numbers().x
        self.assertEqual(x.to_bytes(32, "big").hex(),
                         "005543894af3d00ed7d740abdbd75c96b06877b787db5f70eea78b90a8d7c00a")


if __name__ == "__main__":
    unittest.main()
