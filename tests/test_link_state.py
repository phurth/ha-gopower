"""Tests for verifying which adapter a GP-SC link actually landed on."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
from types import SimpleNamespace

from test_parsers import GoPowerCoordinator  # also installs the module stubs

coordinator_module = importlib.import_module("custom_components.ha_gopower.coordinator")
_bluez_link_state = coordinator_module._bluez_link_state

ADDRESS = "AA:BB:CC:00:00:01"
AGENT_USES: list[bool] = []
HCI0 = "11:11:11:11:11:11"
HCI1 = "22:22:22:22:22:22"


def _v(value):
    """Stand-in for a dbus_fast Variant."""
    return SimpleNamespace(value=value)


def _objects(connected_on=(), paired_on=()):
    objects = {
        "/org/bluez/hci0": {"org.bluez.Adapter1": {"Address": _v(HCI0)}},
        "/org/bluez/hci1": {"org.bluez.Adapter1": {"Address": _v(HCI1)}},
        "/org/bluez/hci0/dev_AA": {
            "org.bluez.Device1": {"Address": _v("AA:AA:AA:AA:AA:AA"), "Connected": _v(True)}
        },
    }
    for hci in ("hci0", "hci1"):
        objects[f"/org/bluez/{hci}/dev_AA_BB_CC_00_00_01"] = {
            "org.bluez.Device1": {
                "Address": _v(ADDRESS),
                "Connected": _v(hci in connected_on),
                "Paired": _v(hci in paired_on),
            }
        }
    return objects


def test_link_state_reports_adapter_and_bonds() -> None:
    assert _bluez_link_state(_objects(("hci1",), ("hci0",)), ADDRESS) == (HCI1, {HCI0})
    assert _bluez_link_state(_objects(("hci0",), ("hci0", "hci1")), ADDRESS) == (
        HCI0,
        {HCI0, HCI1},
    )


def test_link_state_is_none_when_no_local_adapter_carries_the_link() -> None:
    # Another device being connected on hci0 must not count.
    assert _bluez_link_state(_objects(), ADDRESS) == (None, set())


class _Client:
    def __init__(self, pair_error: Exception | None = None) -> None:
        self.pair_error = pair_error
        self.paired = False
        self.disconnected = False
        self.is_connected = True

    async def pair(self) -> None:
        if self.pair_error:
            raise self.pair_error
        self.paired = True

    async def disconnect(self) -> None:
        self.disconnected = True


def _run_connect(monkeypatch, link_state, client, attempt_is_immediate=False):
    removed: list[str | None] = []
    scheduled: list[bool] = []
    AGENT_USES.clear()

    @contextlib.asynccontextmanager
    async def agent():
        AGENT_USES.append(True)
        yield True

    monkeypatch.setattr(coordinator_module, "just_works_agent", agent)

    async def establish(*args, **kwargs):
        return client

    monkeypatch.setattr(coordinator_module, "establish_connection", establish)

    c = object.__new__(GoPowerCoordinator)
    c._address = ADDRESS
    c._is_sc = True
    c._next_attempt_immediate = attempt_is_immediate
    c._immediate_retry_pending = False
    c._bond_failures = 0
    c._early_drops = 0
    c._frame_chunks = []
    c._repeat_tail = ""
    c._repeat_tail_left = 0
    c._repeat_warned = False
    c._reconnect_task = None
    c._bond_cooldown_until = 0.0
    c._client = None
    c._connected = False

    async def pick():
        return object(), HCI1

    async def state():
        return link_state

    async def remove(only_adapter=None):
        removed.append(only_adapter)
        return True

    c._async_pick_sc_device = pick
    c._async_bluez_link_state = state
    c._async_remove_bluez_bond = remove
    c._schedule_reconnect = lambda: scheduled.append(c._immediate_retry_pending)

    try:
        asyncio.run(c._do_connect())
    except AttributeError:
        # Past the pairing step the bare instance has no GATT services; the
        # behaviour under test has already happened by then.
        pass
    return c, removed, scheduled


def test_proxy_link_is_dropped_without_pairing(monkeypatch) -> None:
    client = _Client()
    c, removed, _ = _run_connect(monkeypatch, (None, set()), client)

    assert client.disconnected is True
    assert client.paired is False
    assert removed == []
    assert c._current_connect_source is None


def test_link_on_unbonded_adapter_is_redirected_not_paired(monkeypatch) -> None:
    client = _Client()
    c, removed, _ = _run_connect(monkeypatch, (HCI1, {HCI0}), client)

    assert client.paired is False
    assert client.disconnected is True
    # Only the adapter that carried the link is cleared; hci0 keeps its bond.
    assert removed == [HCI1]
    assert c._immediate_retry_pending is True


def test_redirect_does_not_chain_immediate_retries(monkeypatch) -> None:
    client = _Client()
    c, removed, _ = _run_connect(
        monkeypatch, (HCI1, {HCI0}), client, attempt_is_immediate=True
    )

    assert removed == [HCI1]
    assert c._immediate_retry_pending is False


def test_link_on_bonded_adapter_pairs_and_records_the_real_adapter(monkeypatch) -> None:
    client = _Client()
    c, removed, _ = _run_connect(monkeypatch, (HCI0, {HCI0}), client)

    assert client.paired is True
    assert removed == []
    # The picker asked for hci1; the link really is on hci0.
    assert c._current_connect_source == HCI0


def test_stale_bond_clears_only_the_adapter_that_failed(monkeypatch) -> None:
    client = _Client(pair_error=Exception("org.bluez.Error.AuthenticationFailed"))
    c, removed, scheduled = _run_connect(monkeypatch, (HCI0, {HCI0}), client)

    assert removed == [HCI0]
    assert scheduled == [True]


def test_unverifiable_link_keeps_previous_behaviour(monkeypatch) -> None:
    client = _Client(pair_error=Exception("org.bluez.Error.AuthenticationFailed"))
    c, removed, _ = _run_connect(monkeypatch, None, client)

    assert c._current_connect_source == HCI1
    # No adapter known, so every adapter's bond is cleared, as before.
    assert removed == [None]


def test_link_already_dropped_is_not_mistaken_for_a_proxy(monkeypatch) -> None:
    client = _Client()
    client.is_connected = False
    c, removed, scheduled = _run_connect(monkeypatch, (None, set()), client)

    assert client.paired is False
    assert removed == []
    # No disconnect callback fires for a dead link, so the retry is queued here.
    assert len(scheduled) == 1


def test_disconnect_timeout_still_queues_a_retry(monkeypatch) -> None:
    client = _Client()

    async def hang() -> None:
        raise TimeoutError

    client.disconnect = hang
    _, _, scheduled = _run_connect(monkeypatch, (None, set()), client)

    assert len(scheduled) == 1


def test_repeated_early_drops_with_a_bond_clear_it(monkeypatch) -> None:
    client = _Client()
    client.is_connected = False
    c, removed, _ = _run_connect(monkeypatch, (None, {HCI0}), client)
    assert removed == [] and c._early_drops == 1

    c._early_drops = 2
    asyncio.run(c._do_connect())

    assert removed == [None]
    assert c._early_drops == 0


def test_fresh_pairing_holds_an_agent(monkeypatch) -> None:
    client = _Client()
    _run_connect(monkeypatch, (HCI0, set()), client)

    assert client.paired is True
    assert AGENT_USES == [True]


def test_existing_bond_needs_no_agent(monkeypatch) -> None:
    client = _Client()
    _run_connect(monkeypatch, (HCI0, {HCI0}), client)

    assert client.paired is True
    assert AGENT_USES == []


def test_unverifiable_link_holds_an_agent(monkeypatch) -> None:
    client = _Client()
    _run_connect(monkeypatch, None, client)

    assert AGENT_USES == [True]
