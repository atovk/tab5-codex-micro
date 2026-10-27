# Development notes

[简体中文](development.md) · **English**

This page describes the project structure, protocol boundaries, validation tools, and main implementation milestones. See the [README](../README.en.md) for installation and everyday use. Test screenshots, serial records, and build outputs go into the locally ignored `artifacts/` directory. The public repository excludes personal chat metadata and device backups.

## Project structure

| Path | Purpose |
| --- | --- |
| `src/micro_ui.cpp` | Console, settings, themes, title scrolling, and partial redraws |
| `src/micro_controls.cpp` | Touch events for the dial, joystick, and buttons |
| `src/micro_link.cpp` | Native Micro HID, local metadata, and diagnostics |
| `src/micro_games.cpp` | Three local touch games |
| `src/micro_sound.cpp` | Sound queue and I2S playback |
| `src/micro_orientation.cpp` | IMU landscape orientation and synchronized touch coordinates |
| `src/device_settings.cpp` | Deferred persistence of device preferences |
| `src/ui_locale.cpp`, `include/ui_locale.h` | Runtime English and Chinese UI selection without changing titles or native protocol values |
| `src/title_renderer.cpp` | Glyph-mask caching and title-scroll compositing |
| `include/title_layout.h` | Allocation-free UTF-8 wrapping |
| `include/title_scroll.h` | Dwell, motion, pause, and resume clocks |
| `tools/title_sync.py` | Serial synchronization of explicit Agent assignments, configuration, and chat state |
| `tools/chat_state_bridge.py` | Live desktop-state subscription for an explicit chat |
| `tools/desktop_sync.py`, `tools/chat_sync.py` | Strict configuration parsing and bridge-metadata validation |
| `tests/` | Python and Arduino-free C++ regression tests |

## Precompiled release bundle

Releases include the complete firmware, a separate title font, a flashing helper that needs no compilation, and checksums. See the [flashing guide](FLASHING.en.md) for installation. Bundles exclude personal NVS, chat caches, serial logs, and full-flash backups. Firmware and font are written at `0x0` and `0x510000` respectively.

## Builds and USB adaptation

See the README for the toolchain, first installation, and font flashing. `platformio.ini` uses an isolated `.pio-core/` directory. `prepare_platform.py` obtains a project-local platform with a pinned SHA and validates its patches. `prepare_core.py` only copies an existing matching cache; a fresh installation does not require it.

Tab5's USB-C connector uses ESP32-P4 full-speed PHY0 on GPIO24/25. `tools/usb_fs_patch.py` compiles project-local Arduino USB files and the TinyUSB 0.21.0 HID class with a 64-byte HID OUT buffer. This prevents the SDK's high-speed buffering configuration from delaying full-speed host requests. The patch verifies the SDK version, source anchors, and vendored-file checksum without modifying the global framework.

CDC starts explicitly after USB identity is configured, with an 8192-byte receive buffer. Each main-loop pass parses at most 256 bytes, preventing long metadata lines from monopolizing touch handling. Synchronization, screenshots, and flashing share a serial lock. Diagnostic tools select the application VID/PID and a valid Tab5 application serial number; they do not fall back to an arbitrary Espressif ROM device.

`partitions.csv` targets 16MB flash and includes a 5MB factory application partition. The dynamic title font occupies a separate region at `0x510000`. Flash the factory image for a first installation or partition change. Ordinary application updates can retain the font.

## Input contract

| Touch action | Native event |
| --- | --- |
| Clockwise/counterclockwise outer-ring motion | 24 steps per revolution; `ENC_CW` / `ENC_CC`, `act:2` |
| Dial center | `ENC`, press `act:1`, release `act:0` |
| Joystick drag | `v.oai.rad`, normalized angle and distance; release sends the centered state |
| Agent/Command button | Paired press and release events |

A gesture remains captured from first contact until release. Crossing another button does not trigger it. Crossing the dial center reanchors its angle to prevent jumps. There is no inertial input after release. The desktop application interprets native dial modes, long presses, microphone behavior, and custom actions; firmware preserves those events rather than substituting generic keyboard shortcuts.

`INPUT_TEST` uses the same touch path and event serializer, but returns generated events through CDC and suppresses computer-side HID actions. It exits after 60 seconds without test input. After capture ends, an existing finger must lift before computer control resumes. Captured events alone do not prove that the desktop application executed an action.

## Metadata boundaries

Native Micro lighting messages retain color, brightness, effect, speed, and magic fields for six Agent slots and the `keys` and `ambient` regions. Status labels are inferred from the standard palette; custom colors can change their meaning.

The title companion resolves explicit local Custom assignments and reads only their required titles by ID. It does not infer slot order from recent-access timestamps. Received titles have a 191-byte UTF-8 limit; truncation occurs only at complete character boundaries. `TITLES` and `HOST_CONFIG` have independent acknowledgements and share one uint32 sequence number per batch. A failed acknowledgement for either packet does not prevent the other packet from being attempted, and failed batches can be retried.

Normal watch mode checks for changes once per second and sends a heartbeat approximately every two seconds. Each message has its own 1.5-second acknowledgement budget, and partial acknowledgement lines survive read timeouts. The device marks metadata stale after 7.5 seconds without a valid update or when USB unmounts. Retained RGB state and metadata freshness are separate concepts.

### Real settings for an explicit chat

`chat_state_bridge.py` connects to the desktop's existing Unix coordination socket, registers a local data client, and subscribes to consecutive revisions for an explicit chat. It does not start an app server, resume the chat, send a message, or discover the currently selected desktop chat.

Accepted next-turn settings determine Fast/Standard, Plan/Default, and the permission preset. Missing values remain Unknown; an explicit `serviceTier:null` or `default` means Standard. Full access requires matching the complete permission preset and cannot be inferred from `approvalPolicy:never` alone. Custom configurations remain Custom; workspace paths and custom profile names are not retained in the cache.

The bridge retains only the required settings projection, without writing conversation bodies, tool output, or raw IPC frames. Its cache has 0600 permissions and updates atomically. Disconnection, loss of the owner, or a revision gap invalidates state. The companion rejects state caches older than six seconds. Desktop IPC is an internal, versioned interface; incompatible application upgrades should produce an explicit unknown state.

The native HID command target and the displayed bridge target are independent. The screen's explicit-chat label does not mean that it follows desktop selection automatically. A USB “Sent” indicator reports device transport status, not an application execution acknowledgement.

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
```

These touch-regression tools use diagnostic capture to prevent synthetic touches from triggering computer actions. Cleanup exits capture, clears previews, and restores preferences. Orientation validation may require manually repositioning the device.

The state and titles used by `verify_host_feedback.py` and `verify_title_font.py` are isolated synthetic demonstrations. `verify_chat_bridge.py` actually changes an explicit test chat's next-turn settings, then restores their original values. Read its `--help` and use a dedicated test chat. It verifies state feedback; it cannot independently prove native HID execution.

Common CDC commands:

| Command | Purpose |
| --- | --- |
| `STATUS` | Firmware, input, font, metadata, and rendering statistics; may include chat title/ID |
| `SCREEN` | Copy the current framebuffer into PSRAM and export RGB565 in chunks |
| `THEME LIGHT` / `THEME DARK` | Change the local theme |
| `LANGUAGE EN` / `LANGUAGE ZH` | Switch the interface to English / Chinese |
| `PLAY` / `MICRO` | Switch between the game library and console |
| `PERF_RESET` | Clear performance counters |
| `PERF_FULL` / `PERF_DIRTY` | Select full presentation as a baseline or normal partial presentation |
| `BOOTLOADER` / `RESTART` | Enter ROM download mode or restart |

During screenshot transfer, the companion cannot acquire the serial lock, so metadata observed after transfer may exceed its TTL. This does not change the image copied when `SCREEN` began. To validate live state, save a fresh `STATUS` before the screenshot; release the lock after transfer and allow the companion to refresh.

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
