"""Parser tests using frames captured from live controllers.

These exercise the real code path end to end.  A syntax check cannot catch a
lost ``@staticmethod`` or a renamed state field — calling the parsers can.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

# Stub the third-party modules the coordinator imports at module scope, then
# register the package so its relative imports resolve without executing
# __init__.py (which pulls in Home Assistant's entity registry).
_ROOT = Path(__file__).resolve().parent.parent


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _DUCMeta(type):
    def __getitem__(cls, _item):
        return cls


class _DUC(metaclass=_DUCMeta):
    def __init__(self, *a, **k):
        pass


_stub("bleak", BleakClient=object, BleakError=Exception, BleakGATTCharacteristic=object)
_stub("bleak_retry_connector", establish_connection=None,
      BleakClientWithServiceCache=object)
ha = _stub("homeassistant")
ha.__path__ = []
_stub("homeassistant.components", bluetooth=types.ModuleType("bluetooth"))
_stub("homeassistant.components.bluetooth")
_stub("homeassistant.config_entries", ConfigEntry=object)
_stub("homeassistant.const", CONF_ADDRESS="address")
_stub("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
helpers = _stub("homeassistant.helpers")
helpers.__path__ = []
_stub("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=_DUC)

for _pkg, _sub in (("custom_components", "custom_components"),
                   ("custom_components.ha_gopower", "custom_components/ha_gopower")):
    _m = types.ModuleType(_pkg)
    _m.__path__ = [str(_ROOT / _sub)]
    sys.modules[_pkg] = _m

import importlib  # noqa: E402

GoPowerCoordinator = importlib.import_module(
    "custom_components.ha_gopower.coordinator"
).GoPowerCoordinator


# Captured 2026-09-11 from a GP-PWM-30-SB (32 fields, FFF0 protocol).
PWM_FRAME = (
    " 00476;035;13896;14400;00000;00003;00000;31585;00007;002;100;22171;000;"
    "00000;00177;00049;+32;+90;+31;00004;00017;00012;00018;00115;00324;00000;"
    "04189;02023;010;012;008;"
)

# Captured 2026-09-11 from a GP-PWM-30-UL (30 fields, 569a protocol).
SC_FRAME = (
    " 0110;000;0058;127;1299;2130;00052;00002;00005;0028;14068;157;100;+32;"
    "+28;09433;00046;48;213;03167;02811;03769;1312;000;03867;2129;15748;129;"
    "056173;000000"
)


def _coordinator(is_sc: bool = False):
    """A bare coordinator instance.

    The parsers must be invoked the way the coordinator invokes them — bound,
    as ``self._parse_fields(fields)``.  Calling them unbound off the class
    hides a lost ``@staticmethod``, because an undecorated function accessed
    via the class is just a plain function and takes the frame happily.
    """
    c = object.__new__(GoPowerCoordinator)
    c._is_sc = is_sc
    return c


def test_pwm_frame_parses_to_expected_values() -> None:
    state = _coordinator()._parse_fields(PWM_FRAME.split(";"))
    assert round(state.battery_voltage, 3) == 13.896
    assert round(state.solar_voltage, 3) == 22.171
    assert round(state.solar_current, 3) == 0.476
    assert state.state_of_charge == 100
    assert state.temperature_c == 32
    assert state.serial == "375"          # field 14 is hex 0x177


def test_sc_frame_parses_to_expected_values() -> None:
    state = _coordinator(is_sc=True)._parse_sc_fields(SC_FRAME.split(";"))
    assert round(state.battery_voltage, 3) == 14.068
    assert round(state.solar_current, 1) == 11.0
    assert state.state_of_charge == 100
    assert state.temperature_c == 32
    assert state.solar_voltage is None    # 569a carries no panel voltage


def test_power_is_battery_side_not_panel_side() -> None:
    """A PWM controller stores Vbat x I, not Vpanel x I."""
    state = _coordinator()._parse_fields(PWM_FRAME.split(";"))
    assert round(state.solar_power, 1) == round(13.896 * 0.476, 1)


def test_both_parsers_callable_bound() -> None:
    """Regression guard: a lost @staticmethod breaks exactly one variant."""
    assert _coordinator()._parse_fields(PWM_FRAME.split(";")) is not None
    assert _coordinator(is_sc=True)._parse_sc_fields(SC_FRAME.split(";")) is not None
