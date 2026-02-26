"""Button platform for GoPower Solar BLE integration.

Buttons:
  - Reboot Controller: sends unlock + reboot sequence
  - Reset History: sends unlock + reset history sequence (clears Ah counters)

Reference: Android GoPowerConstants.kt command sequences
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GoPowerCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GoPower button entities from a config entry."""
    coordinator: GoPowerCoordinator = hass.data[DOMAIN][entry.entry_id]
    address = entry.data[CONF_ADDRESS]

    async_add_entities([
        GoPowerRebootButton(coordinator, address),
        GoPowerResetHistoryButton(coordinator, address),
    ])


class GoPowerRebootButton(
    CoordinatorEntity[GoPowerCoordinator], ButtonEntity
):
    """Button to reboot the GoPower solar controller.

    Sends the unlock (&G++0900) → 200ms delay → reboot (&LDD0100) sequence.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Reboot Controller"
    _attr_icon = "mdi:restart"

    def __init__(self, coordinator: GoPowerCoordinator, address: str) -> None:
        super().__init__(coordinator)
        mac = address.replace(":", "").lower()
        self._attr_unique_id = f"{mac}_reboot"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=f"GoPower {address}",
            manufacturer="Go Power!",
            model="GP-PWM Solar Controller",
            connections={("bluetooth", address)},
        )

    @property
    def available(self) -> bool:
        """Available when connected."""
        return self.coordinator.connected

    async def async_press(self) -> None:
        """Send reboot sequence."""
        _LOGGER.info("Reboot button pressed")
        await self.coordinator.async_reboot()


class GoPowerResetHistoryButton(
    CoordinatorEntity[GoPowerCoordinator], ButtonEntity
):
    """Button to reset the amp-hour history counters.

    Sends the unlock (&G++0900) → 200ms delay → reset history (&LDD0200).
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Reset History"
    _attr_icon = "mdi:history"

    def __init__(self, coordinator: GoPowerCoordinator, address: str) -> None:
        super().__init__(coordinator)
        mac = address.replace(":", "").lower()
        self._attr_unique_id = f"{mac}_reset_history"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=f"GoPower {address}",
            manufacturer="Go Power!",
            model="GP-PWM Solar Controller",
            connections={("bluetooth", address)},
        )

    @property
    def available(self) -> bool:
        """Available when connected."""
        return self.coordinator.connected

    async def async_press(self) -> None:
        """Send reset history sequence."""
        _LOGGER.info("Reset History button pressed")
        await self.coordinator.async_reset_history()
