"""Run the production microphone worker against deterministic DMA/session stubs."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("c++")


@unittest.skipUnless(COMPILER, "host C++ compiler is unavailable")
class MicroMicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="tab5-mic-worker-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.binary = Path(cls.directory.name) / "micro_mic"
        result = subprocess.run(
            [COMPILER, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
             "-fsanitize=undefined", "-fno-sanitize-recover=all",
             "-Itests/mic_stubs", "-Iinclude", "src/micro_mic.cpp",
             "tests/test_micro_mic.cpp", "-o", str(cls.binary)],
            cwd=ROOT, text=True, capture_output=True, timeout=30,
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def check(self, case):
        result = subprocess.run([str(self.binary), case], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("microphone capture session contract passed", result.stdout)

    def test_real_worker_passes_capture_epoch_after_three_settling_blocks(self):
        self.check("normal")

    def test_optional_epoch_callback_uses_zero(self):
        self.check("no-epoch")

    def test_epoch_change_during_dma_read_discards_block_and_restarts(self):
        self.check("during-read")

    def test_epoch_change_between_reads_resets_dma_and_dsp_history(self):
        self.check("between-reads")

    def test_mute_unmute_between_observations_invalidates_capture(self):
        self.check("mute-cycle")

    def test_monitor_to_stream_transition_does_not_send_monitor_pcm(self):
        self.check("monitor-stream")

    def test_epoch_change_after_dsp_prevents_delivery(self):
        self.check("after-dsp")

    def test_epoch_change_at_final_worker_check_prevents_delivery(self):
        self.check("before-sink")

    def test_transport_can_reject_epoch_change_after_final_worker_check(self):
        self.check("transport-race")

    def test_meter_toggle_preserves_an_existing_stream(self):
        self.check("monitor-streaming")

    def test_receiver_disable_failure_retries_before_new_session(self):
        self.check("disable-retry")
