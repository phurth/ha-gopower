"""Tests for pinning GP-SC connections to the adapter that holds the bond."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from types import SimpleNamespace

from test_parsers import GoPowerCoordinator  # also installs the module stubs

coordinator_module = importlib.import_module("custom_components.ha_gopower.coordinator")

ADDRESS = "AA:BB:CC:00:00:01"
HCI0 = "11:11:11:11:11:11"
HCI1 = "22:22:22:22:22:22"


def _v(value):
    return SimpleNamespace(value=value)


def _objects(paired_on=()):
    objects = {
        "/org/bluez/hci0": {"org.bluez.Adapter1": {"Address": _v(HCI0)}},
        "/org/bluez/hci1": {"org.bluez.Adapter1": {"Address": _v(HCI1)}},
    }
    for hci in ("hci0", "hci1"):
        objects[f"/org/bluez/{hci}/dev_AA_BB_CC_00_00_01"] = {
            "org.bluez.Device1": {"Address": _v(ADDRESS), "Paired": _v(hci in paired_on)}
        }
    return objects


def test_bonded_adapters_are_named() -> None:
    names = coordinator_module._bonded_adapter_names(_objects(("hci1",)), ADDRESS)

    assert names == {HCI1: "hci1"}


def _coordinator(objects, local_macs=frozenset({HCI0, HCI1})):
    c = object.__new__(GoPowerCoordinator)
    c._address = ADDRESS
    c.bonded_adapters = {}
    c.updates = 0

    async def bluez_objects():
        return objects

    async def local():
        return set(local_macs)

    def update_listeners():
        c.updates += 1

    c._async_bluez_objects = bluez_objects
    c._async_get_local_hci_macs = local
    c.async_update_listeners = update_listeners
    return c


def test_bonded_controller_is_pinned_to_the_bonded_adapter() -> None:
    c = _coordinator(_objects(("hci1",)))

    assert asyncio.run(c._async_sc_pin()) == frozenset({HCI1})
    assert c.bonded_adapters == {HCI1: "hci1"}
    assert c.updates == 1, "the diagnostic sensor is told about the bond"


def test_unbonded_controller_is_kept_off_proxies() -> None:
    """Before a bond exists any local adapter may pair; a proxy cannot."""
    c = _coordinator(_objects())

    assert asyncio.run(c._async_sc_pin()) == frozenset({HCI0, HCI1})


def test_no_bluez_means_no_restriction_beyond_local_adapters() -> None:
    c = _coordinator(None, local_macs=frozenset())

    assert asyncio.run(c._async_sc_pin()) == frozenset()


def _connect_with(monkeypatch, pin, pinned_cls):
    calls = []

    async def establish(client_class, device, name, **kwargs):
        calls.append((client_class, kwargs))
        raise coordinator_module.BleakError("stop here")

    monkeypatch.setattr(coordinator_module, "establish_connection", establish)
    monkeypatch.setattr(coordinator_module, "pinned_client_class", lambda: pinned_cls)

    c = object.__new__(GoPowerCoordinator)
    c._address = ADDRESS
    c._is_sc = True
    c._next_attempt_immediate = False
    c._immediate_retry_pending = False
    c.bonded_adapters = {HCI1: "hci1"} if pin else {}
    c.pinned_adapters = frozenset()

    async def sc_pin():
        return pin

    async def pick():
        return object(), HCI1

    c._async_sc_pin = sc_pin
    c._async_pick_sc_device = pick
    c._schedule_reconnect = lambda: None
    asyncio.run(c._do_connect())
    return c, calls


class _Pinned:
    pass


def test_connect_uses_the_pinned_client(monkeypatch) -> None:
    c, calls = _connect_with(monkeypatch, frozenset({HCI1}), _Pinned)

    (client_class, kwargs), = calls
    assert client_class is _Pinned
    assert kwargs["pinned_sources"] == frozenset({HCI1})
    assert c.pinned_adapters == frozenset({HCI1})


def test_connect_without_pinning_support_is_unchanged(monkeypatch) -> None:
    _, calls = _connect_with(monkeypatch, frozenset({HCI1}), None)

    (client_class, kwargs), = calls
    assert client_class is coordinator_module.BleakClient
    assert "pinned_sources" not in kwargs


# ── Diagnostic sensor ─────────────────────────────────────────────────


def _load_sensor_module():
    """Import sensor.py against minimal Home Assistant stand-ins."""
    const = sys.modules["homeassistant.const"]
    for name in (
        "EntityCategory",
        "UnitOfElectricCurrent",
        "UnitOfElectricPotential",
        "UnitOfPower",
        "UnitOfTemperature",
    ):
        setattr(const, name, SimpleNamespace(DIAGNOSTIC="diagnostic", VOLT="V",
                                             AMPERE="A", WATT="W", CELSIUS="°C"))

    class _Entity:
        def __init__(self, *args, **kwargs):
            pass

    class _CoordinatorEntity(_Entity):
        def __init__(self, coordinator):
            self.coordinator = coordinator

        def __class_getitem__(cls, item):
            return cls

    sensor = types.ModuleType("homeassistant.components.sensor")
    sensor.SensorDeviceClass = SimpleNamespace(VOLTAGE="v", CURRENT="c", POWER="p",
                                               TEMPERATURE="t", BATTERY="b")
    sensor.SensorEntity = _Entity
    import dataclasses  # noqa: PLC0415

    sensor.SensorEntityDescription = dataclasses.make_dataclass(
        "SensorEntityDescription",
        [
            (name, object, dataclasses.field(default=None))
            for name in (
                "key", "name", "native_unit_of_measurement", "device_class",
                "state_class", "icon", "suggested_display_precision",
                "entity_category",
            )
        ],
        frozen=True,
        kw_only=True,
    )
    sensor.SensorStateClass = SimpleNamespace(MEASUREMENT="m", TOTAL_INCREASING="ti")
    sys.modules["homeassistant.components.sensor"] = sensor
    sys.modules["homeassistant.helpers.device_registry"] = types.SimpleNamespace(
        DeviceInfo=dict)
    sys.modules["homeassistant.helpers.entity_platform"] = types.SimpleNamespace(
        AddEntitiesCallback=object)
    sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = (
        _CoordinatorEntity)
    sys.modules.pop("custom_components.ha_gopower.sensor", None)
    return importlib.import_module("custom_components.ha_gopower.sensor")


def test_bonded_adapter_sensor_reports_the_bond_and_the_link() -> None:
    sensor_module = _load_sensor_module()
    coordinator = SimpleNamespace(
        model_name="GP-PWM-30-UL",
        bonded_adapters={HCI1: "hci1"},
        pinned_adapters=frozenset({HCI1}),
        link_adapter=HCI1,
    )

    sensor = sensor_module.GoPowerBondedAdapterSensor(coordinator, ADDRESS)

    assert sensor._attr_unique_id == "aabbcc000001_bonded_adapter"
    assert sensor.native_value == HCI1
    assert sensor.available is True
    assert sensor.extra_state_attributes == {
        "adapters": {HCI1: "hci1"},
        "pinned": True,
        "connected_through": HCI1,
    }
