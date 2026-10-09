"""Tests for assembling GP-SC frames from notifications."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

from test_parsers import GoPowerCoordinator

coordinator_module = importlib.import_module("custom_components.ha_gopower.coordinator")
_collapse = coordinator_module._collapse_repeated_chunks

# One poll's worth of notifications as captured from a GP-PWM-30-UL on
# 2026-10-09: the echoed poll byte, then the frame in 20-byte pieces.
CHUNKS = [
    " ",
    " 0115;000;0052;115;13",
    "00;2120;00052;00002;",
    "00005;0029;14147;169",
    ";100;+32;+32;07633;0",
    "0008;06;255;03159;02",
    "801;03659;1332;000;0",
    "2990;2122;00003;115;",
    "057587;000000;\r\n",
]
FRAME = "".join(CHUNKS).rstrip("\r\n")


def _repeated(copies: int) -> list[str]:
    return [chunk for chunk in CHUNKS for _ in range(copies)]


def test_collapse_leaves_a_normal_frame_alone() -> None:
    assert _collapse(CHUNKS) == (CHUNKS, 1)


def test_collapse_undoes_double_and_triple_delivery() -> None:
    # The frame ends on the first copy of its final chunk.
    assert _collapse(_repeated(2)[:-1]) == (CHUNKS, 2)
    assert _collapse(_repeated(3)[:-2]) == (CHUNKS, 3)


def _feed(chunks: list[str]) -> list[str]:
    frames: list[str] = []
    c = object.__new__(GoPowerCoordinator)
    c._address = "AA:BB:CC:00:00:01"
    c._is_sc = True
    c._response_buffer = ""
    c._frame_chunks = []
    c._repeat_tail = ""
    c._repeat_tail_left = 0
    c._repeat_warned = False
    c._last_data_time = 0.0
    c._parse_and_update = frames.append
    c.hass = SimpleNamespace(async_create_task=lambda _result: None)
    for chunk in chunks:
        c._on_notification(None, bytearray(chunk.encode()))
    return frames


def test_normal_delivery_yields_one_frame_per_poll() -> None:
    assert _feed(CHUNKS + CHUNKS) == [FRAME, FRAME]


def test_double_delivery_yields_clean_frames() -> None:
    assert _feed(_repeated(2) + _repeated(2)) == [FRAME, FRAME]


def test_triple_delivery_yields_clean_frames() -> None:
    assert _feed(_repeated(3) + _repeated(3)) == [FRAME, FRAME]


def test_delivery_returning_to_normal_is_followed() -> None:
    assert _feed(_repeated(2) + CHUNKS) == [FRAME, FRAME]
