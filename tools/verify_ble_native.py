#!/usr/bin/env python3
"""Opt-in native BLE test; default dry-run never opens USB/Bluetooth.

--execute arms one verified bounded plan and temporarily removes USB topology.
Real native events are sent by firmware. This tool never changes chat settings
through IPC. Queue acceptance, BLE submission and observed desktop effects are
reported separately. --confirm-target-active is an explicit operator assertion;
the metadata cache follows its named target and does not reveal desktop focus.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time
import tomllib

import desktop_sync
import chat_sync

PLANS = ("fast", "dial", "plan")
DEFAULT_FIRMWARE = "tab5-micro-0.13.1"
DEFAULT_STICK = {
    "up": {"type": "command", "commandId": "composer.togglePlanMode"},
    "right": {"type": "command", "commandId": "navigateForward"},
    "down": {"type": "command", "commandId": "toggleSidebar"},
    "left": {"type": "command", "commandId": "navigateBack"},
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def firmware_name(version: str) -> str:
    """Pin one exact firmware ID, never a prefix, range or wildcard."""
    if not isinstance(version, str):
        raise ValueError("Firmware must be an exact version such as 0.13.0")
    version = version.removeprefix("tab5-micro-")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Firmware must be an exact version such as 0.13.0")
    return "tab5-micro-" + version


def mapping_summary(state_path: Path, config_path: Path, plan: str) -> dict:
    require(plan in PLANS, "Unknown native diagnostic plan")
    state = json.loads(state_path.read_text())
    config = tomllib.loads(config_path.read_text())
    require(isinstance(state, dict) and isinstance(config.get("desktop", {}), dict), "Invalid desktop configuration")
    desktop = config.get("desktop", {})
    raw_layout = desktop.get(desktop_sync.LAYOUT_KEY,
                  state.get(desktop_sync.LAYOUT_KEY, desktop_sync.DEFAULT_LAYOUT))
    source = ("desktop-config" if desktop_sync.LAYOUT_KEY in desktop else
              "global-state" if desktop_sync.LAYOUT_KEY in state else "native-default")
    encoder, _voice, actions, _joystick_up = desktop_sync._layout(raw_layout)
    invalid = encoder == "unknown"
    # Native schema.safeParse(raw).data ?? DEFAULT_LAYOUT discards the whole
    # invalid layout. Keep its raw fingerprint so even two invalid mappings
    # with identical effective defaults cannot change unnoticed during a test.
    layout = desktop_sync.DEFAULT_LAYOUT if invalid else raw_layout
    if invalid:
        encoder, _voice, actions, _joystick_up = desktop_sync._layout(layout)
    require(encoder != "unknown" and "unknown" not in actions, "Desktop mapping is incomplete or invalid")
    require(encoder != "custom" and "custom" not in actions, "Custom native mappings are excluded from this test")
    stick = layout.get("analogStick", DEFAULT_STICK)
    require(isinstance(stick, dict), "Invalid joystick mapping")
    require(all(v is None or (isinstance(v, dict) and v.get("type") == "command") for v in stick.values()),
            "Custom joystick skills are excluded from this test")
    plan_up = stick.get("up") == DEFAULT_STICK["up"]
    if plan == "fast":
        require(actions[0] == "fast", "ACT06 must map to Fast; refusing an arbitrary action slot")
    elif plan == "dial":
        require(encoder == "composer-navigation", "Dial test requires composer-navigation mode")
    else:
        require(plan_up, "Joystick Up must map exactly to composer.togglePlanMode")
    # Persist only bounded semantic fields and a fingerprint, never custom text.
    normalized = {"encoder": encoder, "actions": list(actions), "joystick_up": "plan" if plan_up else "unknown",
                  "layout_source": source, "layout_invalid": invalid}
    normalized["fingerprint"] = hashlib.sha256(json.dumps(raw_layout, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return normalized


def unlocked_from_registry(value, uid: int) -> bool:
    roots = value if isinstance(value, list) else [value]
    for root in roots:
        if not isinstance(root, dict) or root.get("IOConsoleLocked") is True:
            continue
        for session in root.get("IOConsoleUsers", []):
            if not isinstance(session, dict):
                continue
            if session.get("kCGSSessionUserIDKey") != uid or session.get("kCGSSessionOnConsoleKey") is not True:
                continue
            locked = session.get("CGSSessionScreenIsLocked")
            if locked is False or (locked is None and root.get("IOConsoleLocked") is False):
                return True
    return False


def mac_unlocked() -> bool:
    require(sys.platform == "darwin", "This verifier requires macOS lock-state evidence")
    data = subprocess.check_output(["ioreg", "-n", "Root", "-d", "1", "-a"], timeout=5)
    return unlocked_from_registry(plistlib.loads(data), os.getuid())


def cache_summary(path: Path, target: str, now=None) -> dict:
    require(path.stat().st_size <= 8192, "Metadata cache is oversized")
    data = json.loads(path.read_text())
    stamp = data.get("observed_at")
    now = time.time() if now is None else now
    require(data.get("v") == 1 and data.get("source") == "desktop-ipc"
            and data.get("scope") == "explicit-target" and data.get("host_id") == "local"
            and data.get("connection") == "live" and data.get("thread_id") == target,
            "Metadata cache does not follow the explicit local target")
    require(type(stamp) in (int, float) and -1 <= now - stamp <= 6, "Target metadata cache is stale")
    require(data.get("tier") in chat_sync.SPEEDS and data.get("mode") in chat_sync.MODES,
            "Invalid target state enums")
    return {"tier": data["tier"], "mode": data["mode"], "revision": data.get("revision"), "observed_at": stamp}


def topology_from_registry(value) -> list[str]:
    found = []
    def has_vendor_collection(item):
        if type(item.get("PrimaryUsagePage")) is int and item["PrimaryUsagePage"] == 0xFF00:
            return True
        # macOS can publish one HID device for both the keyboard and native
        # collections, with keyboard as PrimaryUsagePage/PrimaryUsage.
        pairs = item.get("DeviceUsagePairs")
        return isinstance(pairs, list) and any(
            isinstance(pair, dict) and type(pair.get("DeviceUsagePage")) is int
            and pair["DeviceUsagePage"] == 0xFF00 for pair in pairs)

    def visit(item):
        if isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            if (item.get("VendorID") == 0x303A and item.get("ProductID") in (0x8360, 0x8297, 0x8298)
                    and has_vendor_collection(item)):
                raw_transport = item.get("Transport")
                transport = raw_transport.strip().lower() if isinstance(raw_transport, str) else ""
                # Retain unknown matches: dropping one could falsely make a
                # mixed/ambiguous topology appear Bluetooth-only.
                found.append({"usb": "usb", "bluetooth": "bluetooth",
                              "bluetooth low energy": "bluetooth"}.get(transport, "unknown"))
            children = item.get("IORegistryEntryChildren")
            if isinstance(children, list):
                visit(children)
    visit(value)
    return sorted(found)


def topology() -> list[str]:
    raw = subprocess.check_output(["ioreg", "-r", "-c", "IOHIDDevice", "-a"], timeout=5)
    return topology_from_registry(plistlib.loads(raw))


def effect_summary(observations: list[dict], baseline: dict, plan: str, *, not_before_ms=None) -> dict:
    if plan == "dial":
        return {"confirmed": False, "reason": "Composer focus/navigation requires separate visual desktop evidence"}
    field = "tier" if plan == "fast" else "mode"
    initial = baseline.get(field)
    transitions = ({"fast": {"standard", "default"}, "standard": {"fast"}, "default": {"fast"}}
                   if plan == "fast" else {"default": {"plan"}, "plan": {"default"}})
    opposite = transitions.get(initial, set())
    # "default" is the saved tier override, not a claim about a model's
    # effective speed. Unknown/ultrafast cannot establish this binary test.
    # USB removal and recovery are expected outside the BLE-only interval.
    # Inside that interval, never discard an invalid observation and join the
    # two sides: that could attribute a manual/USB change to this native test.
    # The cache still does not establish desktop focus.
    ble_indices = [i for i, o in enumerate(observations)
                   if o.get("transports") and set(o["transports"]) == {"bluetooth"}]
    isolated = observations[ble_indices[0]:ble_indices[-1] + 1] if ble_indices else []
    uninterrupted = bool(isolated) and all(
        o.get("unlocked") is True and o.get("mapping_unchanged") is True
        and "error" not in o and o.get("transports")
        and set(o["transports"]) == {"bluetooth"}
        and isinstance(o.get("cache"), dict) for o in isolated)
    changed = None
    previous = None
    if uninterrupted:
        for i, observation in enumerate(isolated):
            current = observation["cache"].get(field)
            elapsed = observation.get("elapsed_ms")
            after_first_step = (not_before_ms is None or
                (type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= not_before_ms))
            # Observe the transition itself after native emission could first
            # occur. A state already changed before ARM's delay is not evidence
            # even when the same value persists into the later event window.
            if after_first_step and previous == initial and current in opposite:
                changed = i
                break
            previous = current
    restored = changed is not None and any(
        o["cache"].get(field) == initial and (not_before_ms is None or
            (type(o.get("elapsed_ms")) in (int, float) and math.isfinite(o["elapsed_ms"])
             and o["elapsed_ms"] >= not_before_ms)) for o in isolated[changed+1:])
    return {"confirmed": bool(restored), "field": field, "changed": changed is not None,
            "restored": bool(restored), "expected_intermediate": sorted(opposite),
            "uninterrupted_ble_interval": uninterrupted,
            "basis": "Correlated exact-target cache change during Bluetooth-only topology; no direct settings writes"}


def attributed_effect(observations: list[dict], baseline: dict, plan: str, transport_verified: bool,
                      *, not_before_ms=None) -> dict:
    """Keep an observed state cycle separate from a completed native test."""
    time_known = (type(not_before_ms) in (int, float) and math.isfinite(not_before_ms)
                  and not_before_ms >= 0)
    effect = effect_summary(observations, baseline, plan,
                            not_before_ms=not_before_ms if time_known else None)
    effect["state_cycle_observed"] = effect["confirmed"]
    effect["transport_attribution_verified"] = transport_verified is True
    effect["native_emission_lower_bound_known"] = time_known
    effect["confirmed"] = effect["confirmed"] and transport_verified is True and time_known
    return effect


def sample_observation(args, expected_mapping: dict, started: float) -> dict:
    """Timestamp the completed cache read, not the start of slow OS queries."""
    observation = {"sample_started_elapsed_ms": (time.monotonic() - started) * 1000}
    try:
        observation["unlocked"] = mac_unlocked()
        observation["transports"] = topology()
        # ioreg can cross the native emission boundary. Read the live cache
        # after those queries and timestamp immediately afterwards so a newly
        # observed state is not mislabeled with the earlier sample-start time.
        observation["cache"] = cache_summary(args.cache, args.thread_id)
        observation["elapsed_ms"] = (time.monotonic() - started) * 1000
        current = mapping_summary(args.state, args.config, args.plan)
        observation["mapping_unchanged"] = current == expected_mapping
    except Exception as error:
        observation["error"] = type(error).__name__
    if "elapsed_ms" not in observation:
        observation["elapsed_ms"] = (time.monotonic() - started) * 1000
    return observation


class DeviceSession:
    def __init__(self, connection, timeout=8):
        self.connection, self.timeout = connection, timeout

    def request(self, command: str, *, prefix=None, exact=None):
        from verify_device import read_line
        self.connection.write((command + "\n").encode("ascii"))
        self.connection.flush()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            raw = read_line(self.connection, deadline)
            require(not raw.startswith(b"FRAME "), "Unexpected binary frame owner; refusing diagnostic commands")
            text = raw.decode("utf-8", errors="strict")
            require("ERROR" not in text, "Device rejected native diagnostic command")
            if exact is not None and text == exact:
                return None
            if prefix is not None and text.startswith(prefix):
                return json.loads(text[len(prefix):])
            if command == "STATUS" and text.startswith("{"):
                data = json.loads(text)
                if "firmware" in data:
                    return data
        raise TimeoutError("Device diagnostic acknowledgement timed out")


def device_projection(value: dict) -> dict:
    bt = value.get("bluetooth", {})
    return {"firmware": value.get("firmware"), "active_transport": value.get("active_transport"),
            "input_capture": value.get("input_capture"), "lighting_preview": value.get("lighting_preview"),
            "bluetooth": {k: bt.get(k) for k in ("session", "connected", "authenticated", "bonded", "ready", "inputs_submitted")}}


def check_device(value: dict, args):
    expected_firmware = firmware_name(getattr(args, "firmware", DEFAULT_FIRMWARE))
    require(value.get("firmware") == expected_firmware, f"Expected firmware {expected_firmware}")
    require(value.get("active_transport") == "usb", "USB must own native input before the test")
    require(not value.get("input_capture") and not value.get("lighting_preview"), "Another diagnostic capture owns input")
    bt = value.get("bluetooth", {})
    require(bt.get("enabled") is True and bt.get("connected") is True
            and bt.get("authenticated") is True and bt.get("bonded") is True
            and type(bt.get("ready")) is bool and type(bt.get("session")) is int and bt["session"] > 0,
            "The pinned BLE session must be connected, authenticated and bonded before USB pause")
    # Native HID subscription may start only after desktop USB discovery goes
    # away. Firmware waits for ready in this same session before any emission.
    feedback = value.get("host_feedback", {})
    chat = feedback.get("chat", {})
    require(feedback.get("config_fresh") is True and chat.get("fresh") is True
            and chat.get("thread_id") == args.thread_id, "Device lacks fresh configuration for the explicit target")
    if args.plan == "fast":
        require(feedback.get("actions", [None])[0] == 2, "Device ACT06 is not Fast")
    elif args.plan == "dial":
        require(feedback.get("encoder") == 1, "Device encoder is not composer navigation")
    else:
        require(feedback.get("joystick_up") == "plan", "Device cannot verify joystick Up is Plan")


def preflight(args) -> dict:
    result = {"plan": args.plan, "execute_requested": args.execute,
              "expected_firmware": firmware_name(getattr(args, "firmware", DEFAULT_FIRMWARE)),
              "target_hash": hashlib.sha256(args.thread_id.encode()).hexdigest()[:12],
              "active_target": "operator-asserted" if args.confirm_target_active else "not-confirmed",
              "scope": "No settings IPC writes; queued, submitted and desktop effects are separate", "checks": {}, "errors": []}
    for name, operation in (("mapping", lambda: mapping_summary(args.state, args.config, args.plan)),
                            ("unlocked", mac_unlocked),
                            ("target_cache", lambda: cache_summary(args.cache, args.thread_id))):
        try:
            result["checks"][name] = operation()
        except Exception as error:
            result["errors"].append(f"{name}: {type(error).__name__}: {error}")
    if result["checks"].get("unlocked") is not True:
        result["errors"].append("Mac must be unlocked before arming native input")
    if not args.confirm_target_active:
        result["errors"].append("Operator must explicitly confirm the supplied target is active in the primary desktop window")
    result["ready"] = not result["errors"]
    return result


def execute(args, report):
    from tab5_transport import open_serial, application_ports
    require(report["ready"], "Preflight failed; no native action was armed")
    before = None
    armed = pause_attempted = False
    identity = None
    observations = report.setdefault("observations", [])
    started = time.monotonic()
    deadline = started
    try:
        with open_serial(args.port, args.timeout) as connection:
            matches = [p for p in application_ports() if p.device == connection.port]
            require(len(matches) == 1, "Cannot pin the selected chip identity")
            identity = matches[0].serial_number
            session = DeviceSession(connection, args.timeout)
            before = session.request("STATUS"); check_device(before, args)
            status = session.request("BLE_TEST STATUS", prefix="BLE_TEST_STATUS ")
            require(status.get("state") not in ("armed", "running"), "A native BLE test is already active")
            require(mac_unlocked(), "Mac locked during preflight")
            require(mapping_summary(args.state, args.config, args.plan) == report["checks"]["mapping"], "Mapping changed before arming")
            cache_summary(args.cache, args.thread_id)
            report["before"] = device_projection(before)
            armed = True  # ARM acknowledgement can time out after being accepted.
            report["event_window"] = {
                "first_step_not_before_elapsed_ms": (time.monotonic() - started) * 1000 + args.delay_ms,
                "basis": "Host monotonic ARM-request lower bound plus firmware delay; actual scheduling and desktop delivery may occur later",
            }
            session.request(f"BLE_TEST ARM HOST_VERIFIED {args.plan.upper()} {args.delay_ms} {before['bluetooth']['session']} {args.thread_id}", exact="BLE_TEST_ARMED")
            pause_attempted = True
            deadline = time.monotonic() + args.pause_seconds + 3
            session.request(f"USB_PAUSE {args.pause_seconds}", exact="USB_PAUSE_OK")
        # Release CDC ownership so the companion can use its persistent BLE
        # metadata channel during the expected USB absence.
        signature = None
        while time.monotonic() < deadline:
            observation = sample_observation(args, report["checks"]["mapping"], started)
            visible = {k: v for k, v in observation.items()
                       if k not in ("elapsed_ms", "sample_started_elapsed_ms")}
            if "cache" in visible:
                visible["cache"] = {k: v for k, v in visible["cache"].items() if k not in ("revision", "observed_at")}
            next_signature = json.dumps(visible, sort_keys=True)
            if next_signature != signature:
                observations.append(observation); signature = next_signature
                print(json.dumps({"event": "ble-observation", **visible}), flush=True)
            time.sleep(min(.25, max(0, deadline-time.monotonic())))
    finally:
        # Never send a release through another session. Firmware owns bounded
        # cleanup and automatic USB restoration. Wait before reopening CDC if
        # the pause may have begun even without its acknowledgement.
        if armed:
            if pause_attempted:
                while time.monotonic() < deadline:
                    time.sleep(min(.25, deadline-time.monotonic()))
            after = None
            for _ in range(5):
                try:
                    with open_serial(None, args.timeout, serial_number=identity) as connection:
                        session = DeviceSession(connection, args.timeout)
                        schedule = session.request("BLE_TEST STATUS", prefix="BLE_TEST_STATUS ")
                        if schedule.get("state") in ("armed", "running"):
                            session.request("BLE_TEST CANCEL", exact="BLE_TEST_CANCELLED")
                            schedule = session.request("BLE_TEST STATUS", prefix="BLE_TEST_STATUS ")
                        report["schedule"] = schedule
                        after = session.request("STATUS")
                        report["after"] = device_projection(after)
                    break
                except Exception as error:
                    report["recovery_error"] = type(error).__name__
                    time.sleep(1)
            require(after is not None, "Could not verify bounded test completion after USB resumed")
            report.pop("recovery_error", None)
    expected = 2 if args.plan == "dial" else 4
    after = report["after"]
    report["queued"] = {"expected": expected, "accepted": report["schedule"].get("queued_steps"),
                        "complete": report["schedule"].get("state") == "complete"}
    report["submitted"] = {"expected_native_messages": expected,
                           "native_message_delta": after["bluetooth"]["inputs_submitted"] - before["bluetooth"]["inputs_submitted"],
                           "same_ble_session": after["bluetooth"]["session"] == before["bluetooth"]["session"],
                           "meaning": "BLE stack acceptance, not desktop execution"}
    report["transport_verified"] = (report["queued"]["complete"] and report["queued"]["accepted"] == expected
        and report["submitted"]["native_message_delta"] == expected and report["submitted"]["same_ble_session"]
        and any(o.get("transports") and set(o["transports"]) == {"bluetooth"} for o in observations)
        and after["active_transport"] == "usb")
    report["desktop_effect"] = attributed_effect(observations, report["checks"]["target_cache"],
        args.plan, report["transport_verified"],
        not_before_ms=report.get("event_window", {}).get("first_step_not_before_elapsed_ms"))


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    home = Path(os.environ.get("CODEX_HOME", str(Path.home()/".codex")))
    parser.add_argument("--plan", choices=PLANS, required=True)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--confirm-target-active", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Actually arm one native BLE plan and USB_PAUSE; default is dry-run")
    parser.add_argument("--port")
    parser.add_argument("--firmware", type=firmware_name, default=DEFAULT_FIRMWARE,
                        help="Exact expected firmware version or ID (default: %(default)s)")
    parser.add_argument("--delay-ms", type=int, default=15000)
    parser.add_argument("--pause-seconds", type=int, default=35)
    parser.add_argument("--timeout", type=float, default=8)
    parser.add_argument("--state", type=Path, default=home/".codex-global-state.json")
    parser.add_argument("--config", type=Path, default=home/"config.toml")
    parser.add_argument("--cache", type=Path, default=chat_sync.DEFAULT_CACHE)
    parser.add_argument("--output", type=Path, default=Path("artifacts/ble-native-dry-run.json"))
    args = parser.parse_args(argv)
    require(chat_sync.THREAD_ID.fullmatch(args.thread_id), "Explicit target must be a UUID")
    require(1000 <= args.delay_ms <= 30000 and 10 <= args.pause_seconds <= 120
            and args.pause_seconds*1000 >= args.delay_ms + 14000, "Pause must cover the delayed test deadline plus USB recovery margin")
    require(0 < args.timeout <= 15, "Timeout must be 0..15 seconds")
    return args


def main(argv=None):
    args = arguments(argv)
    report = preflight(args)
    code = 0
    try:
        if args.execute:
            execute(args, report)
            if not report.get("transport_verified"):
                code = 1
    except Exception as error:
        report["errors"].append(f"{type(error).__name__}: {error}"); code = 1
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"report": str(args.output.resolve()), "dry_run": not args.execute,
                      "ready": report["ready"], "transport_verified": report.get("transport_verified"),
                      "desktop_execution_confirmed": (report.get("transport_verified") is True
                          and not report["errors"] and report.get("desktop_effect", {}).get("confirmed") is True)}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
