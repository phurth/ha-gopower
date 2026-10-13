"""Tests for a controller that is connected but has stopped answering."""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

from test_parsers import GoPowerCoordinator  # also installs the module stubs

coordinator_module = importlib.import_module("custom_components.ha_gopower.coordinator")
const = importlib.import_module("custom_components.ha_gopower.const")


class _Clock:
    """Stand-in for the ``time`` module with a clock the test moves."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


def _coordinator(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(coordinator_module, "time", clock)
    c = object.__new__(GoPowerCoordinator)
    c._address = "AA:BB:CC:00:00:01"
    c._is_sc = True
    c._connected = True
    c._last_data_time = 0.0
    c._link_started = clock.now
    c._data_this_link = False
    c._silent_links = 0
    c._silent_hold_until = 0.0
    c._bond_cooldown_until = 0.0
    c._poll_sent_at = None
    c.writes = []

    async def write(uuid, data):
        c.writes.append(clock.now)

    c._client = SimpleNamespace(write_gatt_char=write)
    return c, clock


def _new_link(c, clock) -> None:
    """What _start_polling records when a link becomes usable."""
    c._link_started = clock.now
    c._data_this_link = False
    c._poll_sent_at = None


def test_new_link_is_given_time_to_answer(monkeypatch) -> None:
    c, clock = _coordinator(monkeypatch)
    clock.now += const.SILENT_LINK_TIMEOUT - 1
    assert c._check_link_health() is False
    assert c._silent_links == 0


def test_first_silent_link_is_just_reconnected(monkeypatch) -> None:
    c, clock = _coordinator(monkeypatch)
    clock.now += const.SILENT_LINK_TIMEOUT + 1
    assert c._check_link_health() is True
    assert c._silent_links == 1
    assert c._hold_remaining()[0] <= 0  # no hold: reconnect straight away


def test_second_silent_link_is_left_alone_and_the_hold_grows(monkeypatch) -> None:
    c, clock = _coordinator(monkeypatch)
    holds = []
    for _ in range(5):
        clock.now += const.SILENT_LINK_TIMEOUT + 1
        assert c._check_link_health() is True
        remaining, reason = c._hold_remaining()
        holds.append(remaining)
        clock.now += max(remaining, 0) + 5  # wait out the hold, then reconnect
        _new_link(c, clock)

    assert holds[0] <= 0
    assert holds[1:] == [
        const.SILENT_HOLD_BASE,
        const.SILENT_HOLD_BASE * 2,
        const.SILENT_HOLD_CAP,
        const.SILENT_HOLD_CAP,
    ]
    assert reason == "silent-controller hold"


def test_reconnecting_does_not_retrigger_on_the_old_silence(monkeypatch) -> None:
    """The storm: after a forced reconnect the old data age kept firing."""
    c, clock = _coordinator(monkeypatch)
    c._note_data_received()  # data flowed on an earlier link
    clock.now += 4 * 3600  # ...four hours ago
    _new_link(c, clock)

    clock.now += 10
    assert c._check_link_health() is False


def test_link_that_was_working_gets_the_longer_allowance(monkeypatch) -> None:
    c, clock = _coordinator(monkeypatch)
    c._note_data_received()
    clock.now += const.SILENT_LINK_TIMEOUT + 30
    assert c._check_link_health() is False
    clock.now += const.STALE_TIMEOUT
    assert c._check_link_health() is True


def test_data_clears_the_silent_state(monkeypatch) -> None:
    c, clock = _coordinator(monkeypatch)
    for _ in range(3):
        clock.now += const.SILENT_LINK_TIMEOUT + 1
        c._check_link_health()
        _new_link(c, clock)
    assert c._silent_links == 3 and c._hold_remaining()[0] > 0

    c._note_data_received()

    assert c._silent_links == 0
    assert c._hold_remaining()[0] <= 0


def test_stale_bond_cooldown_still_applies(monkeypatch) -> None:
    c, clock = _coordinator(monkeypatch)
    c._bond_cooldown_until = clock.now + 120
    assert c._hold_remaining() == (120, "stale-bond cooldown")


def test_poll_waits_for_the_previous_reply(monkeypatch) -> None:
    c, clock = _coordinator(monkeypatch)

    async def run() -> None:
        await c._poll_once()  # sent
        clock.now += 4
        await c._poll_once()  # previous poll unanswered: skipped
        clock.now += 4
        await c._poll_once()  # still unanswered: skipped
        clock.now += 4.5
        await c._poll_once()  # reply timeout passed: sent again
        c._note_data_received()  # the controller answers
        clock.now += 4
        await c._poll_once()  # answered, so the next poll goes at once

    asyncio.run(run())
    assert c.writes == [1000.0, 1012.5, 1016.5]
