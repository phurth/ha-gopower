"""GoPower Solar BLE integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .coordinator import GoPowerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = [
    "binary_sensor",
    "button",
    "sensor",
]


# Entities removed in 1.1.0.  Both derived Wh from an amp-hour counter times the
# *present* battery voltage — a figure the controller never measured, over a
# window (a day, or the controller's whole life) during which that voltage did
# not hold.  Left in place they linger as "unavailable" rows on every
# dashboard, so they are cleaned out of the registry on upgrade.
_REMOVED_SENSOR_KEYS: tuple[str, ...] = ("cumulative_energy", "energy")


def _purge_removed_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop registry entries for sensors this version no longer creates."""
    address = entry.data.get(CONF_ADDRESS)
    if not address:
        return
    registry = er.async_get(hass)
    mac = address.replace(":", "").lower()
    for key in _REMOVED_SENSOR_KEYS:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{mac}_{key}")
        if entity_id:
            _LOGGER.info("Removing obsolete entity %s", entity_id)
            registry.async_remove(entity_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up GoPower from a config entry."""
    _purge_removed_entities(hass, entry)

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
