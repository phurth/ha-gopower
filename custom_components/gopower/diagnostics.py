"""Diagnostics support for GoPower Solar BLE integration.

Provides a "Download diagnostics" dump with all coordinator state
for troubleshooting BLE connectivity and data parsing issues.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import GoPowerCoordinator

# No secrets to redact for GoPower (no auth), but structure supports it
TO_REDACT_CONFIG: set[str] = set()


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: GoPowerCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Connection state
    connection: dict[str, Any] = {
        "connected": coordinator.connected,
        "data_healthy": coordinator.data_healthy,
        "last_data_age_seconds": (
            round(coordinator.last_data_age, 1)
            if coordinator.last_data_age is not None
            else None
        ),
        "reconnect_failures": coordinator._reconnect_failures,
    }

    # Parsed state
    state_data: dict[str, Any] = {}
    if coordinator.state:
        s = coordinator.state
        state_data = {
            "solar_voltage_v": s.solar_voltage,
            "solar_current_a": s.solar_current,
            "solar_power_w": s.solar_power,
            "battery_voltage_v": s.battery_voltage,
            "state_of_charge_pct": s.state_of_charge,
            "temperature_c": s.temperature_c,
            "temperature_f": s.temperature_f,
            "energy_wh": s.energy_wh,
            "firmware": s.firmware,
            "serial": s.serial,
        }

    # Raw fields for protocol debugging
    raw_fields: list[str] | None = None
    if coordinator.state and coordinator.state.raw_fields:
        raw_fields = coordinator.state.raw_fields

    return {
        "config_entry": async_redact_data(dict(entry.data), TO_REDACT_CONFIG),
        "connection": connection,
        "state": state_data,
        "raw_fields": raw_fields,
    }
