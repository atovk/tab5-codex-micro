#!/usr/bin/env python3
"""Read local configuration defaults for Tab5; never infer active chat state.

Only bounded enums leave this module. Prompt text, URLs, skills, profile names
and chat identities are intentionally absent from both packets and summaries.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import tomllib

import chat_sync


LAYOUT_KEY = "codex-micro-layout"
ACTION_SLOTS = tuple(f"ACT{i:02d}" for i in range(6, 13))
TIERS = frozenset(("unknown", "standard", "fast", "ultrafast"))
ENCODERS = frozenset(("unknown", "composer-navigation", "reasoning", "conversation-scroll", "custom"))
VOICES = frozenset(("unknown", "push-to-talk", "realtime"))
ACTIONS = frozenset(("unknown", "none", "fast", "approve", "reject", "fork", "voice", "aux", "send", "plan", "sidebar", "back", "forward", "custom"))
UNKNOWN_ACTIONS = ("unknown",) * len(ACTION_SLOTS)
KEYCAPS = frozenset("FAST.APPR.REJ.SPLIT.MIC.MIC1.CODEX.BUG.OAI.TERM.DWN.DEL.NEW.NAV.MAGIC.DIFF.PLAY.GIT.BRCH.BRANCH.MRG.PR.PAINT.LAB.PARTY.TIME.MIND+.MIND-.EMPT1.EMPT2.EMPT3.EMPT4.SETUP.FOLD.UPL.APPS.YOLO.YEET.EMPT5".split("."))
COMMAND_ACTIONS = {
    "composer.toggleFastMode": "fast", "approval.approve": "approve",
    "approval.decline": "reject", "forkThread": "fork", "composer.submit": "send",
    "composer.togglePlanMode": "plan", "toggleSidebar": "sidebar",
    "navigateBack": "back", "navigateForward": "forward",
}
AUX_COMMANDS = frozenset((
    "feedback", "toggleTerminal", "copyConversationMarkdown", "archiveThread",
    "newTask", "newThread", "newTab", "toggleThreadPin", "toggleReviewTab",
    "environmentAction1", "git.commit", "git.createDraftPullRequest",
    "git.createBranch", "git.mergePullRequest", "git.createPullRequest",
    "composer.addPhotos", "settings", "openSideChat", "manageTasks",
    "composer.increaseReasoningEffort", "composer.decreaseReasoningEffort",
    "openFolder", "composer.addFiles", "openSkills",
))
DEFAULT_LAYOUT = {
    "version": 1,
    "slots": {
        "ACT06": {"keycapId": "FAST"}, "ACT07": {"keycapId": "APPR"},
        "ACT08": {"keycapId": "REJ"}, "ACT09": {"keycapId": "SPLIT"},
        "ACT10": {"keycapId": "MIC1"}, "ACT11": {"keycapId": "EMPT1"},
        "ACT10_ACT11": {"keycapId": "MIC"}, "ACT12": {"keycapId": "CODEX"},
    },
    "encoderMode": "composer-navigation", "voiceButtonMode": "push-to-talk",
    "separateMicrophoneKeys": False,
}


@dataclass(frozen=True)
class DesktopSnapshot:
    source: str
    tier: str
    encoder: str
    voice: str
    actions: tuple[str, ...]
    reason: str | None = None
    chat: chat_sync.ChatSnapshot | None = None


def unavailable(reason: str | None = None) -> DesktopSnapshot:
    return DesktopSnapshot("unavailable", "unknown", "unknown", "unknown", UNKNOWN_ACTIONS, reason)


def _tier(config: dict) -> str:
    """A saved default, not the selected conversation/next-turn service tier.

    The installed desktop's ni()/lp() normalize priority to fast and preserve
    ultrafast. Its 'default'/missing value can use a model-specific default, so
    neither implies standard without a model catalog and execution context.
    """
    settings = config
    if "profile" in config:
        profile = config["profile"]
        profiles = config.get("profiles")
        if not isinstance(profile, str) or not profile or not isinstance(profiles, dict):
            return "unknown"
        selected = profiles.get(profile)
        if not isinstance(selected, dict):
            return "unknown"
        if "service_tier" in selected:
            settings = selected
    raw = settings.get("service_tier")
    if not isinstance(raw, str):
        return "unknown"
    # 'standard' is a wire/UI enum, but is not an accepted local service_tier
    # in this desktop's request adapter. Do not bless an invalid hand edit.
    return {"priority": "fast", "fast": "fast", "ultrafast": "ultrafast"}.get(raw, "unknown")


def _valid_action(action: object, *, composer: bool = False) -> bool:
    if not isinstance(action, dict):
        return False
    kind = action.get("type")
    if kind == "command":
        return isinstance(action.get("commandId"), str) and bool(action["commandId"])
    if kind == "skill":
        return all(isinstance(action.get(k), str) and bool(action[k]) for k in ("skillName", "skillPath"))
    return composer and kind == "composer-text" and isinstance(action.get("text"), str) and bool(action["text"])


def _slot_action(slot: dict) -> str:
    action = slot.get("action")
    if action is not None and action["type"] in ("skill", "composer-text"):
        return "custom"
    command = action["commandId"] if action is not None else slot.get("commandId")
    if command is not None:
        return COMMAND_ACTIONS.get(command, "aux" if command in AUX_COMMANDS else "unknown")
    keycap = slot["keycapId"]
    if keycap.startswith("EMPT"):
        return "none"
    return {
        "FAST": "fast", "APPR": "approve", "REJ": "reject", "SPLIT": "fork",
        "MIC": "voice", "MIC1": "voice", "CODEX": "send",
        "YOLO": "custom", "YEET": "custom",
    }.get(keycap, "aux")


def _layout(layout: object) -> tuple[str, str, tuple[str, ...]]:
    """Mirror the desktop v1 schema without silently repairing invalid input."""
    unknown = ("unknown", "unknown", UNKNOWN_ACTIONS)
    if not isinstance(layout, dict) or type(layout.get("version")) is not int or layout["version"] != 1:
        return unknown
    slots = layout.get("slots")
    if not isinstance(slots, dict):
        return unknown
    normalized = {}
    for name in (*ACTION_SLOTS, "ACT10_ACT11"):
        slot = slots.get(name, DEFAULT_LAYOUT["slots"][name] if name in ("ACT10", "ACT11") else None)
        if not isinstance(slot, dict) or not isinstance(slot.get("keycapId"), str) or slot["keycapId"] not in KEYCAPS:
            return unknown
        if "commandId" in slot and not isinstance(slot["commandId"], str):
            return unknown
        if "action" in slot and not _valid_action(slot["action"], composer=True):
            return unknown
        normalized[name] = slot
    encoder = layout.get("encoderMode", "composer-navigation")
    voice = layout.get("voiceButtonMode", "push-to-talk")
    separate = layout.get("separateMicrophoneKeys", False)
    if not isinstance(encoder, str) or encoder not in ENCODERS - {"unknown"}:
        return unknown
    if not isinstance(voice, str) or voice not in VOICES - {"unknown"} or not isinstance(separate, bool):
        return unknown
    # These fields are not transmitted, but malformed mappings invalidate the
    # desktop's whole v1 layout rather than selecting a plausible default.
    for key, directions in (("analogStick", ("up", "right", "down", "left")),
                            ("encoder", ("left", "right", "click", "longPress"))):
        if key not in layout:
            continue
        mappings = layout[key]
        if not isinstance(mappings, dict) or any(direction not in mappings for direction in directions):
            return unknown
        if any(value is not None and not _valid_action(value) for value in (mappings[d] for d in directions)):
            return unknown
    actions = tuple(_slot_action(normalized[name]) for name in ACTION_SLOTS)
    if not separate:
        # Native bridge routes ACT10 through the shared key and deliberately
        # ignores ACT11, even when the shared key carries a custom action.
        actions = (*actions[:4], _slot_action(normalized["ACT10_ACT11"]), "none", actions[6])
    return encoder, voice, actions


def build_snapshot(state_path: Path, config_path: Path | None = None) -> DesktopSnapshot:
    """Read only configuration, with the registered desktop preference first."""
    state_path = state_path.expanduser()
    config_path = (config_path or state_path.parent / "config.toml").expanduser()
    config = {}
    config_readable = False
    try:
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)
        config_readable = True
    except FileNotFoundError:
        pass
    except (OSError, ValueError, UnicodeError):
        return unavailable("Local configuration is unavailable or invalid")
    state = None
    try:
        parsed = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            state = parsed
    except (OSError, ValueError, UnicodeError):
        pass
    if not config_readable and state is None:
        return unavailable("Local configuration is unavailable or invalid")
    desktop = config.get("desktop", {})
    reason = None
    if not isinstance(desktop, dict):
        encoder, voice, actions = "unknown", "unknown", UNKNOWN_ACTIONS
        reason = "Desktop layout is unavailable or invalid"
    elif LAYOUT_KEY in desktop:
        encoder, voice, actions = _layout(desktop[LAYOUT_KEY])
    elif state is None:
        encoder, voice, actions = "unknown", "unknown", UNKNOWN_ACTIONS
        reason = "Desktop layout is unavailable or invalid"
    else:
        encoder, voice, actions = _layout(state.get(LAYOUT_KEY, DEFAULT_LAYOUT))
    if encoder == "unknown" and reason is None:
        reason = "Desktop layout is unavailable or invalid"
    return DesktopSnapshot("local-config", _tier(config), encoder, voice, actions, reason)


def encode_command(snapshot: DesktopSnapshot, seq: int) -> bytes:
    if type(seq) is not int or not 0 <= seq <= 0xFFFFFFFF:
        raise ValueError("Sequence must be a uint32")
    if snapshot.source not in ("local-config", "unavailable"):
        raise ValueError("Unsupported desktop source")
    if not isinstance(snapshot.tier, str) or snapshot.tier not in TIERS:
        raise ValueError("Invalid desktop tier")
    if not isinstance(snapshot.encoder, str) or snapshot.encoder not in ENCODERS:
        raise ValueError("Invalid desktop encoder")
    if not isinstance(snapshot.voice, str) or snapshot.voice not in VOICES:
        raise ValueError("Invalid desktop voice")
    if not isinstance(snapshot.actions, (tuple, list)) or len(snapshot.actions) != len(ACTION_SLOTS) or any(not isinstance(v, str) or v not in ACTIONS for v in snapshot.actions):
        raise ValueError("Exactly seven bounded desktop actions are required")
    if snapshot.source == "unavailable" and (snapshot.tier, snapshot.encoder, snapshot.voice, tuple(snapshot.actions)) != ("unknown", "unknown", "unknown", UNKNOWN_ACTIONS):
        raise ValueError("Unavailable desktop snapshots must clear every field")
    payload = {"v": 1, "seq": seq, "source": snapshot.source, "tier": snapshot.tier,
               "encoder": snapshot.encoder, "voice": snapshot.voice, "actions": list(snapshot.actions)}
    if snapshot.chat is not None:
        payload["chat"] = chat_sync.payload(snapshot.chat)
    return b"HOST_CONFIG " + json.dumps(payload, separators=(",", ":")).encode("ascii") + b"\n"


def summary(snapshot: DesktopSnapshot) -> dict:
    result = {"source": snapshot.source, "scope": "configuration-defaults", "tier": snapshot.tier,
              "encoder": snapshot.encoder, "voice": snapshot.voice, "actions": list(snapshot.actions),
              "known_actions": sum(v != "unknown" for v in snapshot.actions),
              "command_bytes": len(encode_command(snapshot, 1))}
    if snapshot.reason:
        result["reason"] = snapshot.reason
    if snapshot.chat is not None:
        result["chat"] = chat_sync.summary(snapshot.chat)
    return result
