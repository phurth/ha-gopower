"""Binary sensor platform for GoPower Solar BLE integration.

Diagnostic binary sensors:
  - Connected: BLE connection to the controller is active
  - Data Healthy: receiving fresh data (not stale)

Reference: Android GoPowerDevicePlugin.kt diagnostic state publishing
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GoPowerCoordinator


@dataclass(frozen=True, kw_only=True)
class GoPowerBinarySensorDescription(BinarySensorEntityDescription):
    """Describe a GoPower binary sensor entity."""

    value_fn: Callable[[GoPowerCoordinator], bool]


BINARY_SENSOR_DESCRIPTIONS: tuple[GoPowerBinarySensorDescription, ...] = (
    GoPowerBinarySensorDescription(
        key="connected",
        name="Connected",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda c: c.connected,
    ),
    GoPowerBinarySensorDescription(
        key="data_healthy",
        name="Data Healthy",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=BinarySensorDeviceClass.PROBLEM,
        icon="mdi:heart-pulse",
        value_fn=lambda c: not c.data_healthy,  # "problem" class: ON = problem
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GoPower binary sensor entities from a config entry."""
    coordinator: GoPowerCoordinator = hass.data[DOMAIN][entry.entry_id]
    address = entry.data[CONF_ADDRESS]

    async_add_entities(
        GoPowerBinarySensor(coordinator, address, desc)
        for desc in BINARY_SENSOR_DESCRIPTIONS
    )


class GoPowerBinarySensor(
    CoordinatorEntity[GoPowerCoordinator], BinarySensorEntity
):
    """A GoPower binary sensor entity."""

    entity_description: GoPowerBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: GoPowerCoordinator,
        address: str,
        description: GoPowerBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        mac = address.replace(":", "").lower()
        self._attr_unique_id = f"{mac}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=f"GoPower {address}",
            manufacturer="Go Power!",
            model="GP-PWM Solar Controller",
            connections={("bluetooth", address)},
        )

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        return self.entity_description.value_fn(self.coordinator)
