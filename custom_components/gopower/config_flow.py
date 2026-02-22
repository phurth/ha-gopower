"""Config flow for GoPower Solar BLE integration.

Supports automatic discovery via BLE advertisements matching:
  - Service UUID 0000FFF0 (standard GoPower GATT service)
  - Device name prefix "GP-PWM" or "GoPower"

Also supports manual entry by MAC address.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import DEVICE_NAME_PREFIXES, DOMAIN, SERVICE_UUID

_LOGGER = logging.getLogger(__name__)


class GoPowerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for GoPower Solar."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}
        self._discovery_info: BluetoothServiceInfoBleak | None = None

    # ------------------------------------------------------------------
    # Bluetooth auto-discovery
    # ------------------------------------------------------------------

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a Bluetooth discovery."""
        _LOGGER.debug(
            "GoPower BLE discovery: %s (%s)",
            discovery_info.name,
            discovery_info.address,
        )

        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        name = discovery_info.name or discovery_info.address
        self.context["title_placeholders"] = {"name": name}

        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm Bluetooth discovery."""
        assert self._discovery_info is not None

        if user_input is not None:
            return self.async_create_entry(
                title=self._discovery_info.name or self._discovery_info.address,
                data={CONF_ADDRESS: self._discovery_info.address},
            )

        name = self._discovery_info.name or self._discovery_info.address
        self.context["title_placeholders"] = {"name": name}

        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"name": name},
        )

    # ------------------------------------------------------------------
    # Manual user flow
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle user-initiated config flow."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"GoPower {address}",
                data={CONF_ADDRESS: address},
            )

        # Show discovered devices if any
        self._discovered_devices = {}
        for info in async_discovered_service_info(self.hass):
            if info.address in self._discovered_devices:
                continue
            name = info.name or ""
            if (
                SERVICE_UUID.lower() in [s.lower() for s in info.service_uuids]
                or any(name.startswith(p) for p in DEVICE_NAME_PREFIXES)
            ):
                self._discovered_devices[info.address] = info

        if self._discovered_devices:
            # Let user pick from discovered devices or enter manually
            addresses = {
                addr: f"{info.name or 'GoPower'} ({addr})"
                for addr, info in self._discovered_devices.items()
            }
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {vol.Required(CONF_ADDRESS): vol.In(addresses)}
                ),
                errors=errors,
            )

        # No discovered devices — manual MAC entry
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): str}),
            errors=errors,
        )
