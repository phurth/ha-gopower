"""BLE coordinator for GoPower Solar controllers.

Lifecycle:
  1. Connect to controller via BLE GATT
  2. Discover services, find FFF0 service
  3. Enable notifications on FFF1 (notify characteristic)
  4. Poll by writing 0x20 to FFF2 every 4 seconds
  5. Assemble multi-packet ASCII response until ≥31 semicolons
  6. Parse 32 semicolon-delimited fields → GoPowerState
  7. Fire HA coordinator update for entities

Reference: Android GoPowerDevicePlugin.kt
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from bleak import BleakClient, BleakError, BleakGATTCharacteristic
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    EXPECTED_FIELD_COUNT,
    FIELD_AMP_HOURS_TODAY,
    FIELD_BATTERY_VOLTAGE,
    FIELD_DELIMITER,
    FIELD_FIRMWARE,
    FIELD_SERIAL,
    FIELD_SOC,
    FIELD_SOLAR_CURRENT,
    FIELD_SOLAR_VOLTAGE,
    FIELD_TEMP_C,
    FIELD_TEMP_F,
    NOTIFY_CHAR_UUID,
    OPERATION_DELAY,
    POLL_COMMAND,
    POLL_INTERVAL,
    RECONNECT_BACKOFF_BASE,
    RECONNECT_BACKOFF_CAP,
    SERVICE_DISCOVERY_DELAY,
    SERVICE_UUID,
    STALE_TIMEOUT,
    UNLOCK_COMMAND,
    UNLOCK_DELAY,
    WATCHDOG_INTERVAL,
    WRITE_CHAR_UUID,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parsed state
# ---------------------------------------------------------------------------

@dataclass
class GoPowerState:
    """Parsed state from a GoPower solar controller."""

    solar_voltage: float = 0.0     # V
    solar_current: float = 0.0     # A
    solar_power: float = 0.0       # W (calculated)
    battery_voltage: float = 0.0   # V
    state_of_charge: int = 0       # %
    temperature_c: int = 0         # °C
    temperature_f: int = 0         # °F
    energy_wh: int = 0             # Wh (Ah × battery voltage)
    firmware: str = ""
    serial: str = ""
    raw_fields: list[str] | None = None  # All 32 fields for diagnostics


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class GoPowerCoordinator(DataUpdateCoordinator[GoPowerState | None]):
    """Manage BLE connection and polling for a GoPower solar controller."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"GoPower {entry.data[CONF_ADDRESS]}",
        )
        self._address: str = entry.data[CONF_ADDRESS]
        self._entry = entry

        # BLE client
        self._client: BleakClient | None = None
        self._connected = False

        # Response assembly
        self._response_buffer = ""

        # Parsed state
        self.state: GoPowerState | None = None
        self._first_data_received = False

        # Timing / health
        self._last_data_time: float = 0
        self._poll_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._reconnect_failures: int = 0

        # Locks
        self._connect_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Return True if BLE connection is active."""
        return self._connected

    @property
    def data_healthy(self) -> bool:
        """Return True if connected and receiving fresh data."""
        if not self._connected or self.state is None:
            return False
        if self._last_data_time == 0:
            return False
        return (time.monotonic() - self._last_data_time) < STALE_TIMEOUT

    @property
    def last_data_age(self) -> float | None:
        """Seconds since last data, or None if never received."""
        if self._last_data_time == 0:
            return None
        return time.monotonic() - self._last_data_time

    @property
    def address(self) -> str:
        """Return the BLE address."""
        return self._address

    # ------------------------------------------------------------------
    # Connect / disconnect
    # ------------------------------------------------------------------

    async def async_connect(self) -> None:
        """Establish BLE connection, discover services, start polling."""
        async with self._connect_lock:
            if self._connected:
                return
            await self._do_connect()

    async def _do_connect(self) -> None:
        """Internal connect logic."""
        _LOGGER.info("Connecting to GoPower %s", self._address)

        device = bluetooth.async_ble_device_from_address(
            self.hass, self._address, connectable=True
        )
        if device is None:
            _LOGGER.warning("GoPower device %s not found in BLE scan", self._address)
            self._schedule_reconnect()
            return

        try:
            client = await establish_connection(
                BleakClient,
                device,
                self._address,
                disconnected_callback=self._on_disconnect,
            )
        except (BleakError, TimeoutError, OSError) as exc:
            _LOGGER.warning("BLE connect via HA router failed: %s", exc)
            # Fallback: try direct connection on local adapter (hci0)
            # This handles the case where the ESPHome proxy has no free
            # slots but a local USB/onboard adapter can reach the device.
            client = await self._try_direct_adapter(exc)
            if client is None:
                self._schedule_reconnect()
                return

        self._client = client
        self._connected = True
        self._reconnect_failures = 0
        self._response_buffer = ""
        _LOGGER.info("Connected to GoPower %s", self._address)

        # Discover services
        await asyncio.sleep(SERVICE_DISCOVERY_DELAY)

        services = client.services
        svc = services.get_service(SERVICE_UUID)
        if svc is None:
            _LOGGER.error("GoPower service %s not found", SERVICE_UUID)
            await client.disconnect()
            return

        write_char = svc.get_characteristic(WRITE_CHAR_UUID)
        notify_char = svc.get_characteristic(NOTIFY_CHAR_UUID)
        if write_char is None or notify_char is None:
            _LOGGER.error("Required characteristics not found in GoPower service")
            await client.disconnect()
            return

        # Enable notifications
        await asyncio.sleep(OPERATION_DELAY)
        try:
            await client.start_notify(notify_char, self._on_notification)
            _LOGGER.info("Notifications enabled on FFF1")
        except (BleakError, TimeoutError) as exc:
            _LOGGER.error("Failed to enable notifications: %s", exc)
            await client.disconnect()
            return

        # Start polling and watchdog
        self._start_polling()
        self._start_watchdog()
        self.async_update_listeners()

    async def _try_direct_adapter(self, original_exc: Exception) -> BleakClient | None:
        """Try connecting directly via local HCI adapters when proxy is full.

        Iterates hci0..hci3 looking for a local adapter that can reach the
        device.  Returns a connected BleakClient or None.
        """
        for adapter in ("hci0", "hci1", "hci2", "hci3"):
            _LOGGER.info(
                "Attempting direct BLE connect to %s via %s",
                self._address,
                adapter,
            )
            try:
                client = BleakClient(
                    self._address,
                    disconnected_callback=self._on_disconnect,
                    adapter=adapter,
                )
                await asyncio.wait_for(client.connect(), timeout=15.0)
                if client.is_connected:
                    _LOGGER.info(
                        "Direct connect succeeded via %s for %s",
                        adapter,
                        self._address,
                    )
                    return client
            except (BleakError, TimeoutError, OSError, asyncio.TimeoutError) as exc:
                _LOGGER.debug(
                    "Direct connect via %s failed: %s", adapter, exc
                )
                continue
        _LOGGER.warning(
            "All direct adapter attempts failed for %s (original: %s)",
            self._address,
            original_exc,
        )
        return None

    async def async_disconnect(self) -> None:
        """Disconnect from the controller."""
        self._stop_polling()
        self._stop_watchdog()
        self._cancel_reconnect()
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._client = None
        self._connected = False
        self.async_update_listeners()

    @callback
    def _on_disconnect(self, client: BleakClient) -> None:
        """Handle BLE disconnection."""
        _LOGGER.warning("GoPower %s disconnected", self._address)
        self._stop_polling()
        self._stop_watchdog()
        self._connected = False
        self._client = None
        self.async_update_listeners()
        self._schedule_reconnect()

    # ------------------------------------------------------------------
    # Reconnection
    # ------------------------------------------------------------------

    def _schedule_reconnect(self) -> None:
        """Schedule a reconnection attempt with exponential backoff."""
        self._cancel_reconnect()
        self._reconnect_failures += 1
        delay = min(
            RECONNECT_BACKOFF_BASE * (2 ** (self._reconnect_failures - 1)),
            RECONNECT_BACKOFF_CAP,
        )
        _LOGGER.info(
            "Reconnecting in %.0fs (attempt %d)", delay, self._reconnect_failures
        )
        self._reconnect_task = self.hass.async_create_task(
            self._reconnect_after(delay)
        )

    async def _reconnect_after(self, delay: float) -> None:
        """Wait then reconnect."""
        await asyncio.sleep(delay)
        try:
            await self.async_connect()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Reconnect failed")

    def _cancel_reconnect(self) -> None:
        """Cancel pending reconnect."""
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            self._reconnect_task = None

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------

    def _start_polling(self) -> None:
        """Start the 4-second polling loop."""
        self._stop_polling()
        self._poll_task = self.hass.async_create_task(self._poll_loop())
        _LOGGER.info("Polling started (every %.0fs)", POLL_INTERVAL)

    def _stop_polling(self) -> None:
        """Stop polling loop."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            self._poll_task = None

    async def _poll_loop(self) -> None:
        """Poll the controller every POLL_INTERVAL seconds."""
        try:
            # Small initial delay before first poll
            await asyncio.sleep(OPERATION_DELAY)
            while self._connected and self._client:
                await self._poll_once()
                await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Poll loop error")

    async def _poll_once(self) -> None:
        """Send a single poll command."""
        if not self._client or not self._connected:
            return
        try:
            await self._client.write_gatt_char(WRITE_CHAR_UUID, POLL_COMMAND)
        except (BleakError, TimeoutError, OSError) as exc:
            _LOGGER.warning("Poll write failed: %s", exc)

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------

    def _start_watchdog(self) -> None:
        """Start the connection health watchdog."""
        self._stop_watchdog()
        self._watchdog_task = self.hass.async_create_task(self._watchdog_loop())

    def _stop_watchdog(self) -> None:
        """Stop watchdog."""
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            self._watchdog_task = None

    async def _watchdog_loop(self) -> None:
        """Check connection health every WATCHDOG_INTERVAL."""
        try:
            while self._connected:
                await asyncio.sleep(WATCHDOG_INTERVAL)
                if not self._connected:
                    break

                # Stale data detection
                if self._last_data_time > 0:
                    age = time.monotonic() - self._last_data_time
                    if age > STALE_TIMEOUT:
                        _LOGGER.warning(
                            "No data for %.0fs — connection stale, forcing reconnect",
                            age,
                        )
                        if self._client:
                            try:
                                await self._client.disconnect()
                            except Exception:  # noqa: BLE001
                                pass
                        break
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Notification handler + ASCII parser
    # ------------------------------------------------------------------

    def _on_notification(
        self, characteristic: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle a BLE notification from FFF1."""
        try:
            chunk = data.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Failed to decode notification chunk")
            return

        self._response_buffer += chunk
        semicolons = self._response_buffer.count(FIELD_DELIMITER)

        if semicolons >= EXPECTED_FIELD_COUNT - 1:
            # Complete response
            raw = self._response_buffer
            self._response_buffer = ""
            self._last_data_time = time.monotonic()
            self.hass.async_create_task(self._parse_and_update(raw))
        else:
            _LOGGER.debug(
                "Assembling response — %d/%d fields so far",
                semicolons + 1,
                EXPECTED_FIELD_COUNT,
            )

    async def _parse_and_update(self, raw: str) -> None:
        """Parse the complete ASCII response and update state."""
        fields = raw.split(FIELD_DELIMITER)
        if len(fields) < EXPECTED_FIELD_COUNT:
            _LOGGER.warning(
                "Incomplete response: %d fields (expected %d)",
                len(fields),
                EXPECTED_FIELD_COUNT,
            )
            return

        try:
            state = self._parse_fields(fields)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to parse GoPower response")
            return

        self.state = state

        if not self._first_data_received:
            self._first_data_received = True
            _LOGGER.info(
                "First data from GoPower %s: battery=%.3fV, solar=%.3fV/%.3fA, "
                "soc=%d%%, temp=%d°C, fw=%s, serial=%s",
                self._address,
                state.battery_voltage,
                state.solar_voltage,
                state.solar_current,
                state.state_of_charge,
                state.temperature_c,
                state.firmware,
                state.serial,
            )

        self.async_set_updated_data(state)

    @staticmethod
    def _parse_fields(fields: list[str]) -> GoPowerState:
        """Parse semicolon-delimited fields into a GoPowerState."""

        def _float_field(idx: int) -> float:
            try:
                return float(fields[idx])
            except (ValueError, IndexError):
                return 0.0

        def _int_field(idx: int) -> int:
            try:
                return int(fields[idx])
            except (ValueError, IndexError):
                return 0

        def _signed_temp(idx: int) -> int:
            """Parse signed temperature like '+06' or '-05'."""
            try:
                return int(fields[idx].lstrip("+"))
            except (ValueError, IndexError):
                return 0

        # Raw values in mV/mA — scale to V/A
        solar_current_a = _float_field(FIELD_SOLAR_CURRENT) / 1000.0
        battery_voltage_v = _float_field(FIELD_BATTERY_VOLTAGE) / 1000.0
        solar_voltage_v = _float_field(FIELD_SOLAR_VOLTAGE) / 1000.0

        # Derived
        solar_power_w = solar_voltage_v * solar_current_a

        # Ah → Wh
        amp_hours_today = _int_field(FIELD_AMP_HOURS_TODAY)
        energy_wh = int(amp_hours_today * battery_voltage_v)

        # Serial: hex string → decimal
        serial_str = ""
        try:
            serial_str = str(int(fields[FIELD_SERIAL], 16))
        except (ValueError, IndexError):
            serial_str = fields[FIELD_SERIAL] if FIELD_SERIAL < len(fields) else ""

        return GoPowerState(
            solar_voltage=round(solar_voltage_v, 3),
            solar_current=round(solar_current_a, 3),
            solar_power=round(solar_power_w, 1),
            battery_voltage=round(battery_voltage_v, 3),
            state_of_charge=_int_field(FIELD_SOC),
            temperature_c=_signed_temp(FIELD_TEMP_C),
            temperature_f=_signed_temp(FIELD_TEMP_F),
            energy_wh=energy_wh,
            firmware=fields[FIELD_FIRMWARE] if FIELD_FIRMWARE < len(fields) else "",
            serial=serial_str,
            raw_fields=fields[:EXPECTED_FIELD_COUNT],
        )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def async_send_command(self, command: bytes) -> None:
        """Send a raw command to FFF2."""
        if not self._client or not self._connected:
            _LOGGER.warning("Cannot send command — not connected")
            return
        try:
            await self._client.write_gatt_char(WRITE_CHAR_UUID, command)
        except (BleakError, TimeoutError, OSError) as exc:
            _LOGGER.error("Command write failed: %s", exc)
            raise

    async def async_reboot(self) -> None:
        """Send unlock + reboot sequence to the controller."""
        from .const import REBOOT_COMMAND

        _LOGGER.info("Sending reboot sequence to GoPower %s", self._address)
        await self.async_send_command(UNLOCK_COMMAND)
        await asyncio.sleep(UNLOCK_DELAY)
        await self.async_send_command(REBOOT_COMMAND)
        _LOGGER.info("Reboot command sent")

    async def async_reset_history(self) -> None:
        """Send unlock + reset history sequence."""
        from .const import RESET_HISTORY_COMMAND

        _LOGGER.info("Sending reset history to GoPower %s", self._address)
        await self.async_send_command(UNLOCK_COMMAND)
        await asyncio.sleep(UNLOCK_DELAY)
        await self.async_send_command(RESET_HISTORY_COMMAND)
        _LOGGER.info("Reset history command sent")

    # ------------------------------------------------------------------
    # DataUpdateCoordinator required method
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> GoPowerState | None:
        """Return the latest state (polling is BLE-driven, not HA-driven)."""
        return self.state
