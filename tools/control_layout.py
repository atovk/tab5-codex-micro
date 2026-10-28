"""Explicit logical layouts for capture-only hardware QA.

Version selection is exact so a new layout is never used against an older
firmware, or guessed for an unverified future release.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re

VERSION = "0.13.1"
FIRMWARE = "tab5-micro-" + VERSION


@dataclass(frozen=True)
class Layout:
    dial_x: int
    dial_y: int
    stick_x: int
    stick_y: int
    stick_radius: int
    stick_arrow_radius: int
    chat_x: int
    chat_y: int
    chat_width: int
    chat_height: int
    title_box: tuple[int, int, int, int]


CURRENT = Layout(226, 352, 1054, 352, 106, 92, 442, 208, 396, 288,
                 (466, 264, 814, 314))
LEGACY = Layout(244, 352, 802, 352, 92, 105, 1008, 378, 198, 75,
                (1016, 401, 1198, 451))
LEGACY_VERSIONS = frozenset(("0.11.1", "0.12.0", "0.13.0"))


def for_firmware(firmware: str) -> Layout:
    version = firmware.removeprefix("tab5-micro-")
    if version == VERSION:
        return CURRENT
    if version in LEGACY_VERSIONS:
        return LEGACY
    raise ValueError(f"No verified control layout for firmware {firmware!r}")


def _integer_expression(node: ast.AST, values: dict[str, int]) -> int:
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if isinstance(node, ast.Name) and node.id in values:
        return values[node.id]
    if isinstance(node, ast.BinOp):
        left = _integer_expression(node.left, values)
        right = _integer_expression(node.right, values)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div) and right:
            # C++ integer division truncates toward zero.
            return (abs(left) // abs(right)) * (-1 if (left < 0) != (right < 0) else 1)
    raise ValueError("Unsupported integer expression in control layout")


def check_cpp_layout(path: Path | None = None) -> None:
    """Resolve integer constants and simple arithmetic without executing source."""
    if path is None:
        path = Path(__file__).resolve().parents[1] / "include/control_layout.h"
    values: dict[str, int] = {}
    for declaration in re.findall(r"\bconstexpr\s+int\s+([^;]+);", path.read_text(encoding="utf-8")):
        for binding in declaration.split(","):
            match = re.fullmatch(r"\s*([A-Za-z_]\w*)\s*=\s*(.+)\s*", binding)
            if not match:
                raise ValueError("Unsupported constant declaration in control layout")
            name, expression = match.groups()
            try:
                values[name] = _integer_expression(ast.parse(expression, mode="eval").body, values)
            except SyntaxError as error:
                raise ValueError("Invalid integer expression in control layout") from error
    expected = {"DialX": CURRENT.dial_x, "DialY": CURRENT.dial_y,
                "StickX": CURRENT.stick_x, "StickY": CURRENT.stick_y,
                "StickRadius": CURRENT.stick_radius,
                "StickArrowRadius": CURRENT.stick_arrow_radius,
                "ChatX": CURRENT.chat_x, "ChatY": CURRENT.chat_y,
                "ChatWidth": CURRENT.chat_width, "ChatHeight": CURRENT.chat_height}
    if {name: values.get(name) for name in expected} != expected:
        raise ValueError("Hardware QA coordinates differ from include/control_layout.h")
