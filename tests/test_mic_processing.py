"""Exercise microphone sample timing, filtering, gain and saturation on the host."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("c++")


@unittest.skipUnless(COMPILER, "host C++ compiler is unavailable")
class MicProcessingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="tab5-mic-dsp-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.binary = Path(cls.directory.name) / "mic_processing"
        result = subprocess.run(
            [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
             "-fsanitize=undefined", "-fno-sanitize-recover=all", "-Iinclude",
             "tests/test_mic_processing.cpp", "-o", str(cls.binary)],
            cwd=ROOT, text=True, capture_output=True, timeout=30,
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def check(self, case):
        result = subprocess.run([str(self.binary), case], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("microphone processing contract passed", result.stdout)

    def test_silence_and_cross_block_sample_phase(self):
        self.check("phase")

    def test_dc_rejection_and_stereo_cancellation(self):
        self.check("dc")

    def test_gain_mute_and_gain_limit(self):
        self.check("gain")

    def test_saturation_count_reset_and_full_range_arithmetic(self):
        self.check("clip")

    def test_voice_passband_and_alias_rejection(self):
        self.check("response")
