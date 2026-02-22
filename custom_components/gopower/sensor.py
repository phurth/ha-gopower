"""Sensor platform for GoPower Solar BLE integration.

Entities:
  - Solar Voltage (V)
  - Solar Current (A)
  - Solar Power (W)
  - Battery Voltage (V)
  - State of Charge (%)
  - Temperature (°C)
  - Energy (Wh)
  - Model Number (diagnostic)
  - Firmware Version (diagnostic)
  - Serial Number (diagnostic)

Reference: Android GoPowerDevicePlugin.kt MQTT discovery payloads
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_ADDRESS,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GoPowerCoordinator, GoPowerState

PERCENTAGE = "%"


@dataclass(frozen=True, kw_only=True)
class GoPowerSensorDescription(SensorEntityDescription):
    """Describe a GoPower sensor entity."""

    value_fn: Callable[[GoPowerState], float | int | str | None]


SENSOR_DESCRIPTIONS: tuple[GoPowerSensorDescription, ...] = (
    GoPowerSensorDescription(
        key="solar_voltage",
        name="Solar Voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:solar-panel",
        suggested_display_precision=3,
        value_fn=lambda s: s.solar_voltage,
    ),
    GoPowerSensorDescription(
        key="solar_current",
        name="Solar Current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:current-dc",
        suggested_display_precision=3,
        value_fn=lambda s: s.solar_current,
    ),
    GoPowerSensorDescription(
        key="solar_power",
        name="Solar Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:solar-power",
        suggested_display_precision=1,
        value_fn=lambda s: s.solar_power,
    ),
    GoPowerSensorDescription(
        key="battery_voltage",
        name="Battery Voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:car-battery",
        suggested_display_precision=3,
        value_fn=lambda s: s.battery_voltage,
    ),
    GoPowerSensorDescription(
        key="state_of_charge",
        name="State of Charge",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
        value_fn=lambda s: s.state_of_charge,
    ),
    GoPowerSensorDescription(
        key="temperature",
        name="Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer",
        value_fn=lambda s: s.temperature_c,
    ),
    GoPowerSensorDescription(
        key="energy",
        name="Energy Today",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:lightning-bolt",
        value_fn=lambda s: s.energy_wh,
    ),
    # Diagnostic sensors
    GoPowerSensorDescription(
        key="model_number",
        name="Model Number",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: "GP-PWM-30-SB",  # Only known model
    ),
    GoPowerSensorDescription(
        key="firmware_version",
        name="Firmware Version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.firmware,
    ),
    GoPowerSensorDescription(
        key="serial_number",
        name="Serial Number",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.serial,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GoPower sensor entities from a config entry."""
    coordinator: GoPowerCoordinator = hass.data[DOMAIN][entry.entry_id]
    address = entry.data[CONF_ADDRESS]

    async_add_entities(
        GoPowerSensor(coordinator, address, desc)
        for desc in SENSOR_DESCRIPTIONS
    )


class GoPowerSensor(
    CoordinatorEntity[GoPowerCoordinator], SensorEntity
):
    """A GoPower sensor entity."""

    entity_description: GoPowerSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: GoPowerCoordinator,
        address: str,
        description: GoPowerSensorDescription,
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
    def available(self) -> bool:
        """Available when connected and we have data."""
        return self.coordinator.connected and self.coordinator.state is not None

    @property
    def native_value(self) -> float | int | str | None:
        """Return the sensor value."""
        if self.coordinator.state is None:
            return None
        return self.entity_description.value_fn(self.coordinator.state)
