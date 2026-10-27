#!/usr/bin/env python3
"""Read Tab5 codec/AMP/driver evidence and play finite reference sounds.

This checks electrical/software configuration, not acoustic output. Native
desktop input is captured throughout, and user volume/mute are restored.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import time
from tab5_transport import open_serial
from verify_neon import Session, require


def hardware(session: Session) -> dict:
    session.send("AUDIO_DIAG")
    deadline = time.monotonic() + session.timeout
    while time.monotonic() < deadline:
        raw = session.line(deadline)
        if raw.startswith(b"{"):
            result = json.loads(raw)
            if result.get("type") == "audio_hardware":
                return result
    raise TimeoutError("No AUDIO_DIAG response")


def check_hardware(value: dict) -> None:
    usb = value["usb"]
    require(usb["software_phy_selection"] and usb["otg_phy0"] and usb["otg_pad"]
            and not usb["jtag_pad"], "USB PHY ownership conflicts with speaker GPIO26/27")
    require(value["registers_read_ok"], "Codec or amplifier register read failed")
    driver = value["i2s"]
    require(driver["tracked"] and driver["enabled"] and driver["sample_rate"] == 48000,
            "Speaker I2S channel is not running at 48kHz")
    require(driver["mclk_hz"] == 12288000 and driver["bclk_hz"] == 1536000,
            "Unexpected speaker clock configuration")
    for pin, signal in ((26,28), (27,25), (29,27), (30,26)):
        gpio = value["gpio"][str(pin)]
        require(gpio["result"] == 0 and gpio["output_enabled"]
                and gpio["signal_out"] == signal, f"Unexpected GPIO{pin} audio routing")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--volume", type=int, default=60, choices=range(1,101), metavar="1..100")
    parser.add_argument("--cue", choices=["TONE","PRESS","CW","CC","DIRECTION","NEUTRAL"], default="TONE")
    args = parser.parse_args()
    report = {"acoustic_output_verified": False}
    try:
        with open_serial(None, 10) as connection:
            session = Session(connection, 10)
            before = session.status()
            require(not before["input_capture"], "A different input test is active")
            report["before"] = before
            report["hardware_before"] = hardware(session)
            check_hardware(report["hardware_before"])
            capture = False
            try:
                session.command("INPUT_TEST_BEGIN", "INPUT_TEST_READY")
                capture = True
                session.command(f"VOLUME {args.volume}", f"VOLUME {args.volume}")
                session.command("SOUND ON", "SOUND ON")
                for _ in range(3):
                    session.command(f"SOUND_TEST {args.cue}", "SOUND_TEST_OK")
                    time.sleep(.5)
                report["after"] = session.status()
                report["hardware_after"] = hardware(session)
                check_hardware(report["hardware_after"])
                old, new = report["hardware_before"]["i2s"], report["hardware_after"]["i2s"]
                require(new["written_bytes"] > old["written_bytes"]
                        and new["nonzero_samples"] > old["nonzero_samples"],
                        "No nonzero PCM was accepted by the I2S driver")
                require(new["blocking_errors"] == old["blocking_errors"]
                        and new["blocking_short_writes"] == old["blocking_short_writes"],
                        "A blocking I2S transfer failed or remained incomplete")
                require(report["after"]["inputs_tx"] == before["inputs_tx"],
                        "Native desktop input changed during audio diagnostics")
            finally:
                session.command(f"VOLUME {before['audio']['volume']}", f"VOLUME {before['audio']['volume']}")
                setting = "SOUND ON" if before["audio"]["enabled"] else "SOUND OFF"
                session.command(setting, setting)
                if capture:
                    session.command("INPUT_TEST_END", "INPUT_TEST_ENDED")
                report["restored"] = session.status()
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report.get("hardware_after", {}), indent=2))


if __name__ == "__main__":
    main()
