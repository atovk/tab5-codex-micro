# USB and Bluetooth connections

[简体中文](BLUETOOTH.md) · **English**

This page covers the **0.12.0 USB/Bluetooth release**. Hardware validation covers PIN pairing, reconnection after application updates that preserve bonds, Fast/Plan desktop state changes, dial navigation highlighting and native blue lighting, encrypted metadata stress tests, automatic USB/BLE switching, and reconnection after toggling Tab5 Bluetooth. Specific DOM focus, control identity, other lighting colors, and other cases listed below remain unconfirmed. Historical 0.11.1 firmware supports USB only. Interface presentation screenshots and older results are not Bluetooth execution evidence. Download the [0.12.0 Release](https://github.com/atovk/tab5-codex-micro/releases/tag/v0.12.0), or see [Build from source](../README.en.md#build-from-source).

Onboard microphone audio is not carried over Bluetooth. The 0.13.0-dev.1 source adds a standard USB microphone. Tab5’s ESP32-C6 does not support the Classic/HFP protocol used by traditional Bluetooth headset microphones, and custom BLE audio would require an additional host audio bridge, so it is deferred. See the [microphone guide](MICROPHONE.en.md) for setup and details.

## Two data channels

| Content | USB | Bluetooth |
| --- | --- | --- |
| Native keys, dial, joystick, and host lighting | Micro USB HID | Micro BLE HID |
| Assigned titles, configuration, and real settings for a designated chat | Local companion → USB CDC | Same companion → separate encrypted GATT service |
| Flashing, framebuffer screenshots, and general diagnostics | USB tools | No wireless flashing or screenshot channel |

The Codex desktop app still interprets native Micro mappings. Bluetooth does not replace dial and joystick events with generic keyboard shortcuts. Titles and Fast/Plan/permission text are not part of native lighting messages. Native HID connection and successful companion synchronization must be checked separately.

## First pairing

1. Power Tab5, open **Gear → Connections**, and turn Bluetooth on. The onboard C6 uses compatible firmware already on the tested Tab5; no separate C6 or other development-board flash is required. Pairing becomes available after it initializes. If initialization reports an error, retain USB for investigation; do not flash an image for another chip model.
2. Tap **Start pairing**. A new computer can establish a bond only during this **two-minute window**. Ordinary startup does not automatically open pairing to new computers.
3. **Unlock the computer first**, open system Bluetooth settings, and select the name shown on Tab5, such as `Tab5 Micro XXXX`. The suffix helps identify it but is not its complete chip identity.
4. When the computer prompts, **immediately enter the six-digit PIN displayed on Tab5**, including leading zeroes. The code appears during the pairing handshake; a placeholder is shown beforehand. There is no shared default PIN. Do not guess a code before one appears. The SMP pairing transaction itself times out after about **30 seconds**. The two-minute window above allows a new computer to initiate pairing; it does not give each PIN prompt two minutes. If pairing fails or times out, close the old prompt and tap Start pairing again.
5. After system pairing, open Codex **Settings → Codex Micro**, check the device, and configure key, dial, and voice mappings. If the system is paired but Codex cannot see it, native HID still needs investigation; the title companion cannot substitute for that connection.

On macOS, the PIN prompt may be a **standalone system dialog from BluetoothUIServer**, rather than a sheet inside System Settings. If Tab5 shows a code but settings has no input field, immediately check for a system prompt behind other windows or on another desktop. Keep the computer unlocked instead of waiting only in settings. If the old prompt has timed out, close it and restart pairing with the newly displayed PIN.

Initiate first pairing from macOS system Bluetooth settings and complete the system prompt before allowing the wireless companion to continue. macOS may also request Bluetooth permission. The bridge does not call pairing or unpairing APIs. The system handles authentication prompts when an authenticated GATT characteristic is accessed; the device still requires bonding and PIN authentication. Identity reads allow up to **60 seconds**, a host read timeout that **does not extend SMP's approximately 30-second pairing timeout**. A locked screen, missing PIN entry, or hidden prompt can still cause failure rather than being treated as paired. C6 availability, discovery, or negotiated MTU cannot substitute for successful authentication.

Early hardware validation encountered a Secure Connections DHKey check failure (`SMP reason 11`). [Upstream issue #241](https://github.com/espressif/esp-hosted-mcu/issues/241) documents the same class of ECC calculation defect on ESP32-P4/C6 and identifies ESP-IDF fix `9fd7cb7e606`. This project adds a narrow compatibility fix for its pinned SDK: only `uECC_shared_secret` computation is routed through the SDK's mbedTLS ECDH interface. TinyCrypt key generation and the Secure Connections protocol remain in place. The build script verifies the pinned SDK identity and the SHA-256 of `libbt.a`; an identity or archive change stops the build and requires review of the compatibility fix.

The ESP32-P4 hardware has now passed the RFC 5903 P-256 known-answer self-test and real six-digit PIN pairing. The bond was confirmed in NVS, and the connection was encrypted and authenticated. This validates the pairing and encryption foundation; it does not establish that every native action and disconnect recovery case has passed.

## USB priority and reconnection

A mounted USB data interface takes priority for native input. Unplugging it allows ready Bluetooth to take over; reconnecting USB restores wired input. Switching first handles releases and joystick neutral on the previous connection. Old actions are never migrated or replayed on another connection. If the old Bluetooth session cannot complete releases, firmware may disconnect that session to end held controls.

Ordinary restarts, temporary loss of connection, and turning Bluetooth off and on preserve bonds. The device and companion attempt reconnection. Native connection and title/settings recovery are separate steps: expired metadata becomes unknown, and reconnection rereads current data instead of replaying an old snapshot.

Repeated hardware tests have confirmed that restarting after application-only (`app-only`) updates restores bonded, encrypted, and authenticated state without entering another PIN, followed by fresh BLE metadata acknowledgements. The same production auto companion completed USB→BLE→USB. After turning Tab5 Bluetooth off for three seconds, native HID recovered about 1.5 seconds after enabling it; GATT also recovered in the new session, with strict sequence acknowledgements for both metadata types continuing for 90 seconds. No new PIN was needed, and native input counts did not increase or replay old input. These tests temporarily disconnected USB data and toggled Tab5 Bluetooth; they do not establish recovery after physical power loss, computer Bluetooth toggling, or long sleep.

If the computer or Tab5 has a stale bond, tap **Forget pairing** on Tab5, read and confirm the dialog, remove the old record on the computer, and pair again. This disconnects Bluetooth; cancelling preserves the bond. A complete factory flash may clear NVS bonds and preferences, requiring pairing again.

Bluetooth replaces the data link. Tab5 still needs a battery or suitable power source. The firmware powers the onboard C6 while keeping battery charging and external power outputs disabled. Battery runtime and additional power consumption have not yet been validated on hardware. Other C6/ESP-Hosted firmware combinations are unverified.

## Title and settings companion

Prepare `.venv` using the [README bridge instructions](../README.en.md#optional-chat-titles-and-settings-bridge), then install the full `requirements.txt`, including pinned `bleak==3.0.1`. The download bundle's `requirements-flash.txt` is only for flashing and does not install the wireless companion.

While connected over USB, this read-only command identifies the P4 application:

```sh
.venv/bin/python tools/verify_device.py discover
```

Replace the example `TAB5-0123456789AB` below with that actual P4 application serial:

```sh
# One foreground instance; prefer USB, then paired BLE when matching USB is absent.
.venv/bin/python tools/title_sync.py --watch --transport auto \
  --device-serial TAB5-0123456789AB
```

The login service can replace the foreground process:

```sh
.venv/bin/python tools/title_sync_service.py install --transport auto \
  --device-serial TAB5-0123456789AB
.venv/bin/python tools/title_sync_service.py status
```

The serial passed during installation is saved in the service configuration, allowing startup without USB. `TAB5_SERIAL_NUMBER` is also supported; explicit `--device-serial` takes priority. There must be one candidate, followed by checks of Identity protocol fields and the P4 serial; a selected serial must match exactly. Ambiguous candidates are not selected by name or signal strength. This selects a device and matches public identifiers; it is not cryptographic device authentication.

The installed login service was also tested independently. During a 30-second USB data absence with only BLE listed, metadata notifications recovered automatically and the device received 20 metadata commands, with zero notification failures and the BLE session preserved. Titles, configuration, and chat state were all fresh after USB returned.

The serial returned by BLE Identity must match the **P4 chip serial used by USB**, not the C6 radio MAC. Advertisement manufacturer data only narrows candidates; the Identity response is checked after connection. The real project firmware requires an encrypted, MITM-authenticated link for this read, but a public serial alone cannot establish the responding peripheral's authenticity. If a screenshot, diagnostic, or flashing tool holds the USB lock, auto skips that heartbeat instead of bypassing the lock over BLE. Explicit `--port` confines synchronization to that USB port and disables wireless fallback when it disappears.

`title_sync.py` is the only companion that accesses hardware. A cross-process lock protects its persistent BLE connection. Do not run foreground watch and the login service together; a second process will not take over or disconnect the first one's session. The separate `chat_state_bridge.py --thread-id CHAT_UUID` only subscribes to a designated chat and updates a cache, so it can run concurrently. The title service does not start that state process.

Native USB input priority and companion transport selection are independent: `--transport ble` forces only **metadata** onto BLE, not native key events. `--transport usb` does not establish a Bluetooth metadata connection.

### Device identity boundary

The real project firmware requires link encryption and MITM authentication for Identity, RX, and TX, with explicit system PIN pairing for the first bond. The companion's subsequent P4 serial check provides no application-level cryptographic proof of device identity. Service UUIDs, advertisement data, and serials are public identifiers. An active impersonator can copy them and answer the same GATT interface without encryption to obtain metadata. The host currently does not address this threat; serial matching and this firmware's encryption requirements do not establish clone resistance.

## Checks and troubleshooting

When no other BLE companion owns the connection, send one metadata batch and wait for acknowledgements:

```sh
.venv/bin/python tools/title_sync.py --once --transport ble \
  --device-serial TAB5-0123456789AB
```

Only after both `TITLES` and `HOST_CONFIG` return acknowledgements with the correct sequence does output include `delivery.status: acknowledged` and the actual `delivery.transport`. This establishes device receipt of metadata, not desktop execution of native keys. Default output omits titles; `--show-titles` is unnecessary for this check.

Hardware validation received correctly sequenced acknowledgements for both commands over BLE. Stress testing also passed 20 rounds of new seven-title content and 20 rounds of identical content: 80/80 correct ACKs, zero parse/state/drop/ACK errors, and restoration of original metadata, preferences, and capture state. This verifies metadata traffic; native desktop execution requires separate evidence and cannot be inferred from an ACK.

| Symptom | What to check |
| --- | --- |
| The system cannot find Tab5 | Confirm power, C6 availability, and Bluetooth being on. A new computer needs the two-minute pairing window |
| Tab5 shows a PIN, but settings has no input field | Look for the standalone BluetoothUIServer system dialog and check other desktops. Enter all six digits promptly; restart if it times out |
| `SMP reason 11` after entering the PIN | Confirm that the build includes the ECC compatibility fix and passes the pinned SDK/archive checks. The current fix has passed hardware self-testing and real pairing; a longer read timeout cannot replace it |
| Titles or settings remain unknown after pairing | Native HID lacks these texts. Check the title companion, required state bridge, and local Custom assignments |
| Metadata is busy | Check for watch plus a login service, or a USB tool holding the shared lock. Starting more instances will not resolve this |
| Device not found with a selected serial | Use the P4 application serial, not a C6 MAC, BLE name, or another ESP32-S3's serial |
| A paired device will not reconnect | Restore power and computer Bluetooth first. If necessary, remove stale bonds on both sides and pair with a new PIN |
| Native keys act on another chat | Tracked settings do not identify the desktop's active target. Target not verified still applies; Micro commands are not redirected to the tracked chat |

The NimBLE callback-order issue is fixed: when an `ENC_CHANGE` encryption event or CCCD notification subscription arrives before `CONNECT`, later connection initialization preserves confirmed state for the same session. Repeated application update restarts with this fix have passed the no-PIN reconnection and metadata checks described above.

A separate fix addresses Arduino NimBLE deduplicating characteristics by UUID and dropping distinct Report characteristics in the same HID service. The project's local `BLEService` patch preserves all five Report characteristics and their read-only `0x2908` Report Reference descriptors. Standard Service Changed indications allow bonded hosts to rediscover the updated GATT service without removing their bonds.

macOS subscribed to HID notifications and the device reached `ready`. The Codex desktop app received BLE `device.status` and `rgbcfg` replies, and the device observed a native session. Further controlled tests, with USB data absent from the system and only Bluetooth listed, observed the designated chat change **Fast → Standard → Fast** and **Default → Plan → Default**. Both cases confirmed BLE transport and desktop execution, without writing those changes through settings IPC.

The four-frame dial test also ran with only BLE, in the same authenticated session, with preview and frozen modes disabled. Baseline, post-clockwise, post-counterclockwise, and restored frames showed native ambient lighting switch from off to a blue `#304FFE` snake effect, triggered separately by both turns, then return to off. Visible framebuffer colors matched RGB565 quantization of the returned lighting color. The desktop generates this native feedback only after finding a visible, enabled composer control and entering its navigation-selection branch; input submission receipts do not generate it. This confirms execution of navigation highlighting and actual lighting feedback/display, but not a specific DOM `activeElement`, control name, or two different selected controls. A single candidate may be highlighted twice. Agent slot state remained unchanged while running-slot border pixels and neon frame counts changed, also confirming animation.

After USB returned during automatic switching tests, BLE remained authenticated, bonded, and ready; titles/config/chat state was fresh; queues were empty; no keys or joystick displacement remained held; and failure, drop, and stale-message counters were zero. This covers the tested bounded transition sequence, not extremes such as holding a key across power loss.

The firmware hardware-validation baseline passed **337 host tests**, built successfully, and passed app-only flash verification on the same Tab5, preserving bonds, preferences, and fonts. Release preparation, including packaging regressions, passed all **341 host tests in 50.550 seconds**. Four-frame verification with cooperative chunked capture passed: the maximum gap between input services during copying was **1.168ms**, while the independent BLE test window recorded a maximum input-sampling interval of **16.253ms**. See the [development notes](development.en.md) for evidence, total copy time per frame, and measurement scope. Unmeasured or unconfirmed cases include amber/red/green and other lighting mappings, specific DOM focus or control identity, other uncovered mappings, physical power loss and battery runtime, computer Bluetooth toggling/long sleep, and keys held across power loss. Controlled execution results do not change the general meanings of submitted and Target not verified in the UI.
