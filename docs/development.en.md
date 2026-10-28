# Development notes

[简体中文](development.md) · **English**

This page describes the project structure, protocol boundaries, validation tools, and main implementation milestones. See the [README](../README.en.md) for installation and everyday use. Test screenshots, serial records, and build outputs go into the locally ignored `artifacts/` directory. The public repository excludes personal chat metadata and device backups.

Download precompiled **0.12.0 USB/BLE** firmware, the font, and bilingual flashing guides from the [0.12.0 Release](https://github.com/atovk/tab5-codex-micro/releases/tag/v0.12.0). This page records the hardware checks completed before release and their boundaries. Historical results apply only to their labeled version and test cases and are not Bluetooth evidence for other versions. Usage steps are in the [Bluetooth guide](BLUETOOTH.en.md).

## Project structure

| Path | Purpose |
| --- | --- |
| `src/micro_ui.cpp` | Console, settings, themes, title scrolling, and partial redraws |
| `src/micro_controls.cpp` | Touch events for the dial, joystick, and buttons |
| `src/micro_link.cpp` | Native Micro HID, local metadata, and diagnostics |
| `src/micro_ble.cpp`, `include/ble_pairing_policy.h` | C6 BLE HID, encrypted GATT, PIN pairing window, and bond policy |
| `src/ble_ecc_compat.c`, `tools/ble_ecc_patch.py`, `tools/ble_security_patch.py` | Pinned-SDK ECC compatibility and local NimBLE authentication/HID Report fixes |
| `include/micro_connection.h` | USB/BLE session isolation, input selection, and release handling on switches |
| `src/micro_games.cpp` | Three local touch games |
| `src/micro_sound.cpp` | Sound queue and I2S playback |
| `src/micro_orientation.cpp` | IMU landscape orientation and synchronized touch coordinates |
| `src/device_settings.cpp` | Deferred persistence of device preferences |
| `src/ui_locale.cpp`, `include/ui_locale.h` | Runtime English and Chinese UI selection without changing titles or native protocol values |
| `src/title_renderer.cpp` | Glyph-mask caching and title-scroll compositing |
| `include/title_layout.h` | Allocation-free UTF-8 wrapping |
| `include/title_scroll.h` | Dwell, motion, pause, and resume clocks |
| `include/micro_transport.h` | Bounded HID queue and release/neutral recovery within one transport session |
| `src/render_service.cpp`, `include/render_service.h` | Checkpoints that service touch and USB cooperatively during drawing |
| `include/game_lifecycle.h` | Game visibility, pause on exit, and explicit resume |
| `tools/title_sync.py` | USB/BLE synchronization of explicit Agent assignments, configuration, and chat state |
| `tools/tab5_ble.py`, `tools/tab5_metadata.py` | Persistent BLE metadata, P4 identity checks, USB priority, and cross-process connection ownership |
| `tools/chat_state_bridge.py` | Live desktop-state subscription for an explicit chat |
| `tools/desktop_sync.py`, `tools/chat_sync.py` | Strict configuration parsing and bridge-metadata validation |
| `tools/verify_transition_performance.py` | Isolated checks for complete page transitions and new title-cache construction |
| `tools/verify_screenshot_stream.py` | Binary screenshot integrity and cleanup across the capture watchdog |
| `tools/verify_ble_native.py` | Explicitly enabled native BLE checks separating queueing, transport, and desktop state changes |
| `tests/` | Python and Arduino-free C++ regression tests |

## Precompiled release bundle

Releases include the complete firmware, a separate title font, a flashing helper that needs no compilation, and checksums. See the [flashing guide](FLASHING.en.md) for installation. Bundles exclude personal NVS, chat caches, serial logs, and full-flash backups. Firmware and font are written at `0x0` and `0x510000` respectively.

Release validation in 0.11.1 requires one unambiguous, NUL-terminated firmware version in the factory image, exactly equal to the manifest version. Neither `0.11.0-rc.1` nor `0.11.01` is accepted as `0.11.0`; mixed embedded versions are rejected too. The standalone bundle's `--check` continues to require only the Python standard library.

Before any write, the source helper `tools/flash.py` uses `esptool --chip esp32p4 --no-stub read-mac` to check the selected chip and read its base MAC. When moving from the application into ROM, that MAC must match the original `TAB5-…` application serial. The port name may change after reset, but verification succeeds only when the same chip's application serial returns an exact matching `STATUS` firmware version. Other connected Tab5 devices cannot satisfy the reconnect check. ROM selection is explicit, an S3 cannot pass the P4 check, and the existing shared serial lock covers the operation. Version verification does not print chat metadata.

## Builds and USB adaptation

See the README for the toolchain, first installation, and font flashing. `platformio.ini` uses an isolated `.pio-core/` directory. `prepare_platform.py` obtains a project-local platform with a pinned SHA and validates its patches. `prepare_core.py` only copies an existing matching cache; a fresh installation does not require it.

Tab5's USB-C connector uses ESP32-P4 full-speed PHY0 on GPIO24/25. `tools/usb_fs_patch.py` compiles project-local Arduino USB files and the TinyUSB 0.21.0 HID class with a 64-byte HID OUT buffer. This prevents the SDK's high-speed buffering configuration from delaying full-speed host requests. The patch verifies the SDK version, source anchors, and vendored-file checksum without modifying the global framework.

CDC starts explicitly after USB identity is configured, with an 8192-byte receive buffer. Each main-loop pass parses at most 256 bytes, preventing long metadata lines from monopolizing touch handling. Synchronization, screenshots, and flashing share a serial lock. Diagnostic tools select the application VID/PID and a valid Tab5 application serial number; they do not fall back to an arbitrary Espressif ROM device.

`partitions.csv` targets 16MB flash and includes a 5MB factory application partition. The dynamic title font occupies a separate region at `0x510000`. Flash the factory image for a first installation or partition change. Ordinary application updates can retain the font.

## 0.12.0 Bluetooth implementation and validation boundaries

P4 runs the UI and native protocol; the onboard C6 supplies the BLE controller through ESP-Hosted/SDIO. Wireless initialization runs in a separate worker and leaves USB available on failure. C6 availability, visible advertising, and negotiated MTU do not establish pairing: PIN entry, bonding, encryption, and authentication must complete. First unlock macOS, pair in system Bluetooth settings, and then allow the companion to continue.

BLE exposes HID service `0x1812`, vendor usage page `0xFF00`, native report ID 6, and 63-byte report payloads. Device Information PnP ID corresponds to `0x303A:0x8360`. Matching VID/PID is only one identification condition and cannot establish desktop execution. USB and BLE keep separate receive assembly, transmit queues, and session identifiers to prevent splicing or replay across connections.

Native input goes to one selected connection, with mounted USB taking priority. Switching gives the previous connection bounded time to release held controls and center the joystick, disconnecting an old Bluetooth session if cleanup cannot complete. A partial gesture does not continue on the new connection. Normal USB insertion preserves BLE connection and bonding. **USB/BLE submitted** means transport acceptance, not desktop execution.

Pairing uses a displayed random six-digit PIN, encryption, MITM authentication, and bonding. New identities can complete pairing only within the user-opened 120-second window; bonded identities can reconnect automatically. Authentication completion rechecks the window and session so an expired handshake cannot establish a new bond. Replacing an existing bond also requires a pairing window. Connections settings provide Bluetooth on/off, Start pairing, and confirmed Forget pairing.

The pinned-SDK ECC compatibility fix routes only `uECC_shared_secret` through the SDK's mbedTLS ECDH interface, retaining TinyCrypt key generation and Secure Connections. SDK identity and the `libbt.a` SHA-256 are checked before building; changes stop the build. P4 startup testing uses the RFC 5903 P-256 known answer. NimBLE connection handling preserves `ENC_CHANGE` and CCCD state received before `CONNECT`. The local `BLEService` patch avoids dropping HID Reports by UUID deduplication, retaining five Report characteristics and read-only `0x2908` descriptors. Standard Service Changed indications ask bonded hosts to refresh GATT.

### Separate metadata GATT service

These UUIDs share the suffix `-7d4b-4b58-9e53-5e4ad07e7a50`:

| UUID prefix | Purpose |
| --- | --- |
| `8f4d1000` | Metadata service |
| `8f4d1001` | RX, writes with response; accepts line fragments after authenticated encryption |
| `8f4d1002` | TX, notifications after authenticated encryption; replies may span MTU-sized fragments |
| `8f4d1003` | Identity, read after authenticated encryption |

Identity is `{"v":1,"serial":"TAB5-0123456789AB","maxLine":6144,"firmware":"tab5-micro-0.12.0"}`. The example serial must be replaced by the actual **P4 USB chip identity**. `firmware` is diagnostic and is not an exclusive version gate. Six P4 MAC bytes in advertisement manufacturer data under `0x303A` only narrow candidates; Identity protocol fields and the public serial are still checked after connection.

The real project firmware requires encrypted, MITM-authenticated links for Identity, RX, and TX, with system PIN pairing for the first bond. Public serial matching on the host provides no application-level cryptographic device authentication. It cannot prevent an active impersonator from copying UUIDs/advertised serials and answering GATT without encryption to obtain metadata; this threat is currently unaddressed. Hardware encryption and pairing results apply to this firmware, not to companion clone resistance. See the [device identity boundary](BLUETOOTH.en.md#device-identity-boundary).

RX accepts only `TITLES <json>\n` and `HOST_CONFIG <json>\n`, without input, screenshot, flashing, or general diagnostic commands. The desktop fragments writes by ATT MTU and firmware assembles newline-delimited messages. Successful replies are `TITLES_OK <seq>` and `HOST_CONFIG_OK <seq>`. A single `0x18` byte silently aborts an incomplete line, preventing a later newline from applying old state after a write timeout. Commands and replies belong to one BLE session and are not migrated after disconnection.

Only `title_sync.py` connects to hardware; `chat_state_bridge.py` remains cache-only. A separate cross-process BLE owner lock protects the persistent GATT connection so another process cannot take over or close the connection shared with native HID. Auto prefers USB with the same serial and skips a heartbeat when its lock is busy. Normal USB insertion does not disconnect GATT. Sources are reread after connection and before each packet, without retaining chat snapshots for replay.

macOS uses Bleak 3.0.1 and CoreBluetooth. Discovery merges scanning with the public `retrieveConnectedPeripheralsWithServices` API so a peripheral already connected through system HID remains discoverable after it stops advertising. The BLEDevice `(peripheral, manager)` adapter depends on the pinned backend contract; review it when upgrading Bleak. Unknown identities require a unique candidate; names, RSSI, and C6 addresses are not used to guess the target. Link establishment has a 15-second budget. The encrypted Identity read separately allows 60 seconds without holding the USB serial lock. This does not extend SMP's approximately 30-second transaction timeout. macOS may show the PIN prompt in a standalone BluetoothUIServer dialog; unlock the computer and enter the code promptly.

Synthetic coverage includes identity checks, MTU fragmentation, notification assembly, interrupted writes, reconnect without replay, USB priority, owner locking, cache refresh, and PIN-window policy. Synthetic tests do not replace hardware evidence. Confirmed pairing, Fast/Plan desktop execution, metadata, and automatic fallback, along with remaining acceptance checks, are separated in the 0.12.0 results below.

## Input contract

| Touch action | Native event |
| --- | --- |
| Clockwise/counterclockwise outer-ring motion | 24 steps per revolution; `ENC_CW` / `ENC_CC`, `act:2` |
| Dial center | `ENC`, press `act:1`, release `act:0` |
| Joystick drag | `v.oai.rad`, normalized angle and distance; release sends the centered state |
| Agent/Command button | Paired press and release events |

A gesture remains captured from first contact until release. Crossing another button does not trigger it. Crossing the dial center reanchors its angle to prevent jumps. There is no inertial input after release. The desktop application interprets native dial modes, long presses, microphone behavior, and custom actions; firmware preserves those events rather than substituting generic keyboard shortcuts.

Version 0.11.1 puts native inputs and protocol replies in a bounded queue and attempts one HID fragment per poll. A busy endpoint or failed submission preserves the current position instead of waiting for USB completion in the touch path. Multi-fragment replies remain contiguous. Only unsent joystick movements within the same direction sector may be coalesced; threshold crossings, direction changes, and neutral events remain distinct.

Within the same USB session, expired inputs, a full queue, or diagnostic suspension cancel unsent actions. Releases and joystick neutral for already-submitted controls take priority, and new presses are rejected until recovery is submitted. A true USB unmount clears the queue, session, and recovery state. Reconnection never replays old presses, turns, releases, or partial replies. A release can itself trigger a desktop action, so recovery must not cross USB sessions.

The **USB not sent** button receipt means submission failed or the queue rejected the event. It does not mean that the event remains queued or promise later delivery. **USB submitted** appears only after a complete message has been successfully handed to the USB stack. Submission does not prove that the desktop received the message or executed its mapping.

`INPUT_TEST` uses the same touch path and event serializer, but returns generated events through CDC and suppresses computer-side HID actions. It exits after 60 seconds without test input. After capture ends, an existing finger must lift before computer control resumes. Captured events alone do not prove that the desktop application executed an action.

## Metadata boundaries

Native Micro lighting messages retain color, brightness, effect, speed, and magic fields for six Agent slots and the `keys` and `ambient` regions. Status labels are inferred from the standard palette; custom colors can change their meaning.

The title companion resolves explicit local Custom assignments and reads only their required titles by ID. It does not infer slot order from recent-access timestamps. Received titles have a 191-byte UTF-8 limit; truncation occurs only at complete character boundaries. `TITLES` and `HOST_CONFIG` have independent acknowledgements and share one uint32 sequence number per batch. A failed acknowledgement for either packet does not prevent the other packet from being attempted, and failed batches can be retried.

The optional `HOST_CONFIG` field `joystick_up` accepts only `unknown` or `plan`. The companion sends `plan` only when Up maps to `composer.togglePlanMode` in a fully valid desktop v1 layout; an omitted `analogStick` uses the verified desktop default. Custom or invalid mappings, unavailable configuration sources, and older packets without the field resolve to `unknown`. This reports a saved mapping, not the live action target or proof of execution, and exposes no custom command text.

Normal watch mode checks for changes once per second and sends a heartbeat approximately every two seconds. Each message has its own 1.5-second acknowledgement budget, and partial acknowledgement lines survive read timeouts. Metadata becomes stale after 7.5 seconds without a valid update, when the active connection is lost, or when it changes.

Connection evidence is kept separate: `usb_mounted` describes the current USB mount; `native_session_seen` means native host traffic was observed in the currently selected transport session, not that the host remains live. Titles, configuration, and chat settings each have freshness state. The header distinguishes USB/BLE connection and **Last** lighting. Retained RGB values cannot prove current desktop state. A lost or changed connection invalidates the corresponding session and metadata.

### Real settings for an explicit chat

`chat_state_bridge.py` connects to the desktop's existing Unix coordination socket, registers a local data client, and subscribes to consecutive revisions for an explicit chat. It does not start an app server, resume the chat, send a message, or discover the currently selected desktop chat.

Accepted next-turn settings determine Fast/Standard, Plan/Default, and the permission preset. Missing values remain Unknown; an explicit `serviceTier:null` or `default` means Standard. Full access requires matching the complete permission preset and cannot be inferred from `approvalPolicy:never` alone. Custom configurations remain Custom; workspace paths and custom profile names are not retained in the cache.

The bridge retains only the required settings projection, without writing conversation bodies, tool output, or raw IPC frames. Its cache has 0600 permissions and updates atomically. Disconnection, loss of the owner, or a revision gap invalidates state. The companion rejects state caches older than six seconds. Desktop IPC is an internal, versioned interface; incompatible application upgrades should produce an explicit unknown state.

If a permission patch touches an unknown approval field or structure that the current projection cannot safely reconstruct, the bridge immediately invalidates the projection and requests a full snapshot. A reduced custom-approval marker also cannot reconstruct its original object from an incremental patch. Until resynchronization, the state is unknown instead of retaining an old preset such as Full access. Snapshots still retain only filtered settings, without caching raw new field values, paths, or profile names.

Version 0.11.1 displays bridge values in a separate read-only **Tracked settings** strip with an explicit **Target not verified** notice. Native Fast/Plan/Approve buttons retain desktop mappings and no longer change their active appearance or names according to tracked chat settings. The tracked title also does not prove that HID commands target that chat. Desktop selection and the bridge target remain independent.

## Runtime interface language

Version 0.11.0 adds English and Chinese UI labels through `UiLocale`. A full factory flash defaults to English. The image includes padding across NVS, so flashing it again resets theme, volume, brightness, and language preferences; skipping the font does not preserve NVS. The language control on the settings page changes the interface at runtime, and the selected preference survives a restart.

Localization applies to console, settings, and game labels, using the existing antialiased fonts and visible-ink metrics. English labels select an appropriate size for the available width. Chat titles retain their original text. UI language changes do not translate native protocol enums or machine-readable diagnostic fields, so host integration and existing protocol assertions remain independent of the display language.

## Fonts and two-line scrolling

Static labels and dynamic titles use a pinned Noto Sans SC Medium source. Static faces contain native 16/20/24/26/28/32/40px antialiased subsets. Dynamic titles use a separate 24px BMP face covering 30,445 glyphs. Unsupported characters are replaced individually, rather than making the entire line fall back to an older font.

The `T5VLW4` asset format retains VLW metric tables and stores bitmaps as 4-bit alpha. A memory-mapped reader expands data on demand for M5GFX without per-glyph heap allocation. Loading validates the header, dimensions, lengths, and CRC. The generator writes a manifest containing the pinned upstream SHA, asset SHA, glyph count, dimensions, and Pillow version.

```sh
.venv/bin/python tools/build_ui_font.py --check
.venv/bin/python tools/build_title_font.py
.venv/bin/python tools/build_title_font.py --check
```

Each title viewport is 50px high, with two 25px rows. English wrapping prefers complete words. Chinese wrapping permits character boundaries while avoiding unsuitable punctuation at line starts and ends where possible. Short titles are centered; titles occupying at most two rows remain static without added ellipses.

Longer titles dwell for 2.2 seconds, then move upward by one row over one second. Appending the first two rows to the strip makes the loop continuous. Any touch pauses the clock. Release preserves a one-second reading pause before motion resumes. Settings and games pause title clocks; returning does not skip content based on elapsed wall time.

Text is rasterized into a cached alpha strip only when its content, font, or width changes. Scrolling restores a clean background and composites that strip without redrawing glyphs or allocating memory. Theme and press-background changes recapture the background while retaining scroll position. Titles and neon borders share an idle-update budget: at most one idle region is refreshed per frame, while input-related redraws take priority.

`TITLE_SCROLL_TEST` works only inside `INPUT_TEST` and supplies a repeatable virtual absolute clock. Ending capture clears it automatically. Production scrolling uses the real clock. `FX FREEZE` holds ordinary titles at their first two rows for pixel comparison.

## Materials, neon, and partial updates

Broad backgrounds, panels, and key faces use deterministic solid RGB565 colors, with shadows concentrated at edges to avoid full-screen grain. Rounded corners, dial edges, ticks, and icons use subpixel coverage. Limited gradients use fixed blue-noise thresholds; full and partial redraws use the same coordinates for consistent results.

Activity neon stays on keycap edges, leaving the text region outside the animation. Border and title updates do not redraw the entire card. Presses and theme changes invalidate and recapture the relevant background caches.

Version 0.11.1 adds `RenderService` checkpoints between longer material drawing, title rasterization, and pixel writeback operations. They cooperatively sample touch, handle bounded USB reception, and advance the transmit queue. Rendering uses a state snapshot. Checkpoints do not reenter drawing, persist preferences, change orientation, or dispatch CDC commands that could reenter rendering. This reduces gaps in input servicing during drawing; it does not make a complete redraw instantaneous. Complete-frame duration is still recorded separately.

Game lifecycle changes apply immediately when page visibility changes, without relying on a later render to observe an intermediate page. A quick exit to the console and return still leaves the game paused. Simulation resumes only after an explicit resume action, and hidden games do not advance the run.

Touch regions extend slightly beyond the visible keycaps while gaps preserve separate gesture boundaries. This layout has been reviewed against screen dimensions and target sizes; it has not undergone a formal user study or usability certification.

## Sound and orientation

Audio uses the onboard codec's I2S/PCM path. Short sounds are throttled according to dial, button, and joystick events. Adjusting volume in settings provides a preview. Volume, mute, brightness, and other device preferences are saved after touch release with a delay, avoiding repeated flash writes during dragging.

The P4 SDK's default USB debug PHY pin handling conflicts with audio GPIO26/27. Preserve the debug-PHY release in `micro_sound.cpp`, and initialize audio before starting USB. Do not arbitrarily rebuild Speaker or USB drivers at runtime.

The orientation policy combines the acceleration gravity projection and gyroscope stability to switch between two landscape directions. Screw holes down corresponds to rotation 3, and the inverse orientation to rotation 1. Touch, screenshots, and diagnostic capture keep the coordinate system stable. Flat placement, portrait placement, vigorous motion, and sensor failure retain the current direction.

## Tests and diagnostics

Host logic tests need no device. Arduino-free C++ tests require a working compiler:

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tools/build_ui_font.py --check
.venv/bin/python tools/build_ui_icons.py --check
.venv/bin/python tools/build_material_noise.py --check
.venv/bin/python tools/build_ui_sounds.py --check
```

With a Tab5 running this firmware, run the relevant device checks as needed:

```sh
.venv/bin/python tools/verify_controls.py
.venv/bin/python tools/verify_ui_pixels.py
.venv/bin/python tools/verify_title_scroll.py
.venv/bin/python tools/verify_audio_performance.py
.venv/bin/python tools/verify_neon.py
.venv/bin/python tools/verify_orientation.py
.venv/bin/python tools/verify_transition_performance.py --firmware 0.12.0
.venv/bin/python tools/verify_screenshot_stream.py --firmware 0.12.0
```

These touch-regression tools use diagnostic capture to prevent synthetic touches from triggering computer actions. Cleanup exits capture, clears previews, and restores preferences. Orientation validation may require manually repositioning the device.

`verify_transition_performance.py` separately measures day/night, English/Chinese, settings open/close, game-library/console transitions, and new title-cache construction for all six slots and the tracked chat. Each phase clears performance counters, sends short commands, waits without polling, then reads the first `STATUS`, requiring synchronous drawing to have returned. It records command ACK time, complete rendering, and input-service intervals separately; an ACK is not treated as a completed frame.

The tool owns an isolated `INPUT_TEST` capture and uses synthetic previews. It does not write real titles or `HOST_CONFIG`, transfer `SCREEN`, or send native computer actions, and it restores the page and preferences on exit. Output retains only whitelisted timing and local-state fields. By default it records a baseline; `--max-input-us` sets an input-service interval threshold for that run. A cold title means a new layout/raster cache entry; font storage itself may already be warm. These counters do not prove physical touch-to-pixel latency or desktop execution of native actions.

`verify_screenshot_stream.py` checks binary screenshot integrity across the 60-second capture watchdog. It obtains a reference frame from a frozen synthetic preview with an isolated held key, then uses the capture-only `SCREEN_TEST_SLOW` command to pace device transmission at **28,000 B/s**. The host reads continuously rather than relying on host serial buffering to apply device backpressure. The tool checks the header, exact pixel byte count, trailer, equality with the reference frame, and the deferred release event and cleanup `STATUS` after the frame.

The state and titles used by `verify_host_feedback.py` and `verify_title_font.py` are isolated synthetic demonstrations. `verify_chat_bridge.py` actually changes an explicit test chat's next-turn settings, then restores their original values. Read its `--help` and use a dedicated test chat. It verifies state feedback; it cannot independently prove native HID execution.

`verify_ble_native.py` defaults to local preflight without opening USB/BLE. Explicit `--execute` checks mappings, unlocked state, the designated chat's cache, and the operator's confirmation of the active target before scheduling bounded native events and temporarily disconnecting USB data. Its report separates queue completion, submitted messages in the same BLE session, macOS topology showing only Bluetooth, and actual changes/restoration in the designated chat. It does not write settings through IPC to manufacture the expected change. Interpret `transport_verified` separately from `desktop_execution_confirmed`. Dial navigation highlighting is evidenced by native ambient feedback generated by that desktop branch and paired device frames; it does not establish a specific DOM focus or control identity.

Fast/Plan passed again with strict timing checks: the complete execution interval remained BLE-only without errors, and each plan submitted exactly four expected native messages in the same session. Desktop changes must come from actual target-cache reads after ARM, including the intermediate state and restoration; cache changes before scheduling input cannot count as success.

Dial lighting diagnostics are scheduled over USB before capturing baseline, post-clockwise, post-counterclockwise, and restored framebuffers with matching render state in the same authenticated BLE session while USB data is disconnected. Frames are retrieved afterward. Preview and frozen modes are disabled, and Agent slot state is compared so simulated lighting, submission receipts, or background running colors are not mistaken for desktop navigation feedback. Each frame is copied in 113 chunks of at most 16KiB, servicing input and checking session/UI state throughout. Final hardware verification passed.

Common CDC commands:

| Command | Purpose |
| --- | --- |
| `STATUS` | Firmware, input, font, metadata, and rendering statistics; may include chat title/ID |
| `SCREEN` | Copy the current framebuffer into PSRAM and export RGB565 in chunks |
| `SCREEN_TEST_SLOW` | Send a test screenshot at 28,000 B/s only during `INPUT_TEST` capture; rejected otherwise |
| `THEME LIGHT` / `THEME DARK` | Change the local theme |
| `LANGUAGE EN` / `LANGUAGE ZH` | Switch the interface to English / Chinese |
| `PLAY` / `MICRO` | Switch between the game library and console |
| `PERF_RESET` | Clear performance counters |
| `PERF_FULL` / `PERF_DIRTY` | Select full presentation as a baseline or normal partial presentation |
| `BOOTLOADER` / `RESTART` | Enter ROM download mode or restart |

During screenshot transfer, the companion cannot acquire the serial lock, so metadata observed after transfer may exceed its TTL. This does not change the image copied when `SCREEN` began. To validate live state, save a fresh `STATUS` before the screenshot; release the lock after transfer and allow the companion to refresh.

### 0.12.0 release validation results

The firmware hardware-validation baseline passed **337 host tests in 49.060 seconds**. The corresponding PlatformIO build succeeded in 37.87 seconds. App-only flashing verified the same Tab5 reporting exactly version 0.12.0 while preserving bonds, preferences, and fonts. Release preparation, including packaging regressions, passed all **341 host tests in 50.550 seconds**. The factory image contains the same application as the hardware-validation build. The following evidence applies to this device and macOS desktop version, not every mapping, host, or recovery scenario.

| Test | Observed result and boundary |
| --- | --- |
| Pairing and restart | P4 RFC 5903 self-test, real six-digit PIN pairing, NVS bonding, encryption, and authentication passed. Repeated app-only update restarts reconnected without a PIN and received metadata acknowledgements |
| Native HID handshake | macOS subscribed to HID and reached ready. The desktop received BLE `device.status`/`rgbcfg` replies, and the device observed a native session |
| Fast | With USB data disconnected and only Bluetooth listed by the system, the designated chat changed Fast → Standard → Fast. Both transport and desktop execution were confirmed |
| Plan | Under the same BLE-only conditions, the designated chat changed Default → Plan → Default. Both transport and desktop execution were confirmed |
| Dial navigation highlight | With only BLE in the same authenticated session, clockwise/counterclockwise turns separately triggered the desktop's navigation-selection branch for visible, enabled composer controls, returning blue `#304FFE`, effect 2 (snake), ambient feedback. This confirms navigation highlighting, not a specific DOM `activeElement`, control name, or two different targets |
| Four lighting frames | Baseline off → clockwise blue → counterclockwise blue again → restored off. Matching render state and framebuffer colors agree with RGB565 quantization. Preview/frozen modes were off and Agent slot state stayed unchanged, excluding background running-color confusion |
| Running animation | Border pixels for the running slot changed across the four frames while neon frame counts advanced, confirming actual animation rendering. Amber/red/green and other mappings remain untested |
| Metadata stress | 20 rounds of new seven-title content and 20 rounds of identical content each sent `TITLES` and `HOST_CONFIG`: 80/80 correctly sequenced ACKs, with zero parse, state, drop, or ACK errors |
| Stress performance | New content: maximum input-service interval 10,178µs, maximum complete render 280,275µs, and 40 renders. Identical content: maximum input interval 4,000µs and no additional renders. Free heap changed by zero during the observation window |
| Stress cleanup | Original metadata, preferences, and capture state were restored, with zero cleanup errors |
| Companion switching | The same production `--watch --transport auto` process completed USB→BLE→USB, with strict sequence ACKs for both metadata types in all three phases. BLE first succeeded about seven seconds after USB went offline, without a PIN. After USB returned, the BLE session remained ready, metadata/config/chat state was fresh, and no inputs were replayed |
| Installed login service | An independently installed auto service restored notifications during a 30-second USB data absence with only BLE listed. The device received 20 metadata commands with zero notification failures and the BLE session preserved; titles/config/chat state was fresh after USB returned |
| Bluetooth off/on | After Tab5 Bluetooth was off for three seconds, native HID recovered in a new session about 1.5 seconds after enabling it, encrypted, authenticated, and bonded without a PIN. The same auto process then restored GATT in that session, with strict sequence ACKs for both metadata types continuing for 90 seconds before switching back to USB |
| Post-switch cleanup | Native input count remained 10 with no replay. USB/BLE queues were empty; no keys, joystick displacement, or release recovery remained pending. Failure/drop/stale and parse/state error counters were zero; titles, configuration, and chat state were fresh |
| Unconfirmed | Specific DOM focus and control identity, amber/red/green and other lighting mappings, other uncovered mappings, physical power loss and battery runtime, computer Bluetooth toggling/long sleep, and keys held across power loss |

Input intervals and heap changes are finite samples, not universal performance bounds or long-term memory conclusions. Fast/Plan have desktop state-change evidence; the dial has navigation-selection feedback and device-frame evidence, but a single candidate may be highlighted twice. These conclusions do not extend to other actions with only ACKs or submission counts. The UI's submitted and Target not verified meanings remain unchanged.

The final four frames shared the same authenticated BLE session and connection generation, with preview/frozen modes off and a native session observed. Times are relative to ARM. Total copy time and input-service intervals are separate measurements:

| Frame | Time after ARM (ms) | Ambient | Total copy time (µs) | Maximum input-service gap during copy (µs) | Neon frame count |
| --- | ---: | --- | ---: | ---: | ---: |
| Baseline | 6,011 | Off | 57,061 | 1,117 | 291 |
| After clockwise | 16,508 | Blue snake | 58,354 | 1,157 | 826 |
| After counterclockwise | 18,511 | Blue snake | 57,546 | 1,157 | 918 |
| Restored | 24,515 | Off | 58,109 | 1,168 | 1,227 |

The header ambient center pixel was `[198,203,214] → [49,81,255] → [49,81,255] → [198,203,214]`, consistent with RGB565 quantization of native `#304FFE`. The running-slot border comparison covered 3,346 pixels; the later frames differed from baseline by 336/508/508 pixels. Slot configuration state was unchanged, and advancing neon counts further confirmed animation. ARM took 179µs, with a maximum preparation step of 44µs.

A separate measurement began with `PERF_RESET` immediately before `USB_PAUSE` and used the first `STATUS` snapshot after USB returned. The complete BLE test window recorded a maximum input-sampling interval of **16,253µs** and maximum rendering duration of **185,108µs**. Rendering included transition redraws and is not input blocking time. This window excluded synchronous preflight `STATUS` JSON output. An earlier approximately 245ms statistic included that diagnostic output, so it cannot be compared with 16.253ms as a like-for-like improvement. The table independently demonstrates continued input service during chunked copying.

### Historical 0.11.1 validation results (USB)

- The full **233-test host suite passed**, as did all generated-asset checks. The final PlatformIO build succeeded, and the firmware was flashed to Tab5.
- All ten complete device transitions passed the **50ms input-service interval** threshold, with a maximum of **25,011µs**. The same ten-phase 0.11.0 baseline reached **1,109,016µs**. These measurements cover themes, languages, settings, game pages, and new title-cache construction.
- Day/night theme changes recorded maximum input-service intervals of **7,009/6,680µs**. Complete theme redraws took approximately **0.39 seconds**, versus approximately **1.1 seconds** previously. Complete-frame duration and input-service interval are measured separately.
- The pixel-restoration cases in this run recorded **zero changed pixels** between partial restoration and a complete redraw.
- Consecutive `MICRO` and `PLAY` commands returned the game to a paused state with unchanged simulation ticks.
- With sound active, dial/joystick samples recorded maximum input-sampling intervals of **10,352/11,644µs**.
- The slow-frame watchdog check passed: all **1,843,200 bytes** arrived in **65.832 seconds**, with the same SHA as the fast reference frame and `END_FRAME` at the exact boundary. Only one captured press/release pair was observed, with the release line after the frame trailer. The native computer-input counter increased by **zero**.

These results apply to this device and these test cases, not universal performance limits. Diagnostic capture and device counters do not prove desktop execution of native actions. The slow-frame check establishes transmission integrity for that frozen reference frame; it does not replace pixel-restoration tests.

### 0.11.0 validation summary

- 177 host tests passed, including regressions for bilingual text selection, language state, and release-bundle validation.
- After extracting the ZIP into an independent directory, `python -S flash.py --check` passed. Release files can be verified without the source checkout or loading site-packages.
- The extracted standalone helper successfully flashed both the factory image and title font. The device restarted and reconnected to the native desktop USB host; the dynamic font loaded 30,445 glyphs.
- Both language controls passed captured touch checks without sending native inputs to the computer. Chinese was saved and retained across an ordinary restart, then English was restored.

- Captured control regression passed: 24 clockwise and 24 counterclockwise dial steps, 13 balanced key press/release sequences, a 650ms center hold, and joystick cardinal directions plus neutral.
- Partial restoration matched a full redraw with zero changed pixels, excluding only the six native LED regions that can change asynchronously.
- During audio overlap, the device captured 40 events and played eight PCM notes through its audio worker, with zero native inputs sent to the computer. Dial samples recorded maximum render/input intervals of 14.874/19ms; joystick samples recorded 21.783/26ms.

The control tests use gated simulation and do not prove that the desktop executed native actions. Audio counters confirm queue/worker playback, not measured acoustic output. Timing figures are short device-side samples, not universal limits or computer-side latency.

### Historical 0.10.2 validation

- The public-source cleanup passed 164 host tests covering input, state parsing, permission presets, font assets, UTF-8 wrapping, and scroll clocks.
- Long titles scrolled using the normal wall clock. Virtual-clock checks covered dwell, motion, looping, touch pause, resume hold, and title-replacement boundaries.
- Scroll differences stayed within the title viewport. Replacing a long title with a short one and operating buttons/joystick left no pixel differences between partial restoration and full reconstruction.
- After seven long-title caches and the scene had settled, a four-second sample without `STATUS` polling recorded 66 title updates, a maximum render time of 2.433ms, and a maximum input-sampling interval of 5.004ms.
- With audio active, dial samples recorded maximum render/input intervals of 14.820/19.002ms; joystick samples recorded 21.628/25.002ms.

These are short device-side samples, not universal performance limits or computer-side action latency. Full-screen transitions, initial font preparation, and diagnostic transfers should be measured separately from settled interaction. Repeated `STATUS` polling itself changes USB and main-loop timing.

## Main version milestones

| Version | Changes |
| --- | --- |
| 0.12.0 | Native USB/BLE protocol, PIN pairing and reconnection, and encrypted metadata GATT; Fast/Plan desktop execution, metadata stress, and automatic switching have case-specific evidence in the validation table |
| 0.11.1 | Separate tracked settings and native targets, explicit connection evidence, nonblocking HID and release recovery, cooperative input during drawing, pause on game exit, permission-patch resynchronization, and strict flashing verification |
| 0.11.0 | Runtime English/Chinese interface, bilingual documentation, and a prebuilt firmware/font download bundle |
| 0.10.2 | Two-line titles, automatic overflow scrolling, touch pause, glyph-mask caching, and scroll regression tests |
| 0.10.1 | Complete BMP title font, consistent typography, per-character missing-glyph handling, and ink-based alignment |
| 0.10.0 | Real Fast/Plan/permission state for an explicit chat, revision continuity, and expiry handling |
| 0.9.0 | Native RGB-region feedback, configuration-driven labels, separate USB transport receipts, and stale-state indicators |
| 0.8.x | Persistent preferences, interaction sounds, audio/USB coexistence, and IMU landscape correction |
| 0.7.0 | Partial neon-border animation for active Agents |
| 0.6.0 | Removal of broad grid textures, larger touch regions, improved shadows, and partial restoration |
| 0.5.0 | Consistent antialiased fonts, icons, rounded corners, and visible-ink centering |
| 0.4.0 | Metal/graphite themes, persistent theme selection, and game-library styling |
| 0.3.0 | Continuous touch dial, joystick distance, native press/release, and gesture capture |
| 0.2.0 | Explicit-assignment title companion and three local games |
| 0.1.x | USB HID integration, discovery, and partial-redraw foundations |
