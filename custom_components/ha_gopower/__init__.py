"""GoPower Solar BLE integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import GoPowerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = [
    "binary_sensor",
    "button",
    "sensor",
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up GoPower from a config entry."""
    coordinator = GoPowerCoordinator(hass, entry)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Connect in the background so we don't block HA startup.
    # Entities will show "unavailable" until the BLE connection succeeds.
    async def _bg_connect() -> None:
        try:
            await coordinator.async_connect()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to connect to GoPower controller")

    entry.async_create_background_task(hass, _bg_connect(), "gopower_initial_connect")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: GoPowerCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_disconnect()

    return unload_ok
