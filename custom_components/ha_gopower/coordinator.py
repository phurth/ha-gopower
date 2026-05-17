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
    CONF_DEVICE_TYPE,
    DEVICE_TYPE_SC,
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
    SC_EXPECTED_FIELD_COUNT,
    SC_FIELD_BATTERY_CURRENT,
    SC_FIELD_BATTERY_VOLTAGE,
    SC_FIELD_FIRMWARE,
    SC_FIELD_SOC,
    SC_FIELD_TEMP_C,
    SC_NOTIFY_CHAR_UUID,
    SC_SERVICE_UUID,
    SC_WRITE_CHAR_UUID,
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

    solar_voltage: float | None = None  # V; None when device doesn't report panel voltage
    solar_current: float = 0.0     # A
    solar_power: float = 0.0       # W (calculated)
    battery_voltage: float = 0.0   # V
    state_of_charge: int = 0       # %
    temperature_c: int = 0         # °C
    temperature_f: int = 0         # °F
    energy_wh: int = 0             # Wh (Ah × battery voltage)
    firmware: str = ""
    serial: str = ""
    model_name: str = ""
    raw_fields: list[str] | None = None  # All fields for diagnostics


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

        # Device variant: True = GP-SC (569a GATT, Just Works pairing)
        #                  False = GP-PWM (FFF0 GATT, no pairing)
        self._is_sc: bool = entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_SC

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

    @property
    def model_name(self) -> str:
        """Return a human-readable model name for DeviceInfo."""
        return "GP-PWM-30-UL" if self._is_sc else "GP-PWM-30-SB"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _source_is_local_hci(source: str) -> bool:
        """Return True if *source* looks like a local HCI adapter.

        Local HCI adapter sources are reported as a Bluetooth MAC address
        (e.g. ``AA:BB:CC:DD:EE:FF``).  ESPHome BT proxy sources are
        hostnames or IP addresses, so this simple check correctly
        distinguishes them without requiring D-Bus introspection.
        """
        import re  # noqa: PLC0415
        return bool(re.match(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$', source))

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
        _LOGGER.info(
            "Connecting to GoPower %s (variant=%s)",
            self._address,
            "SC" if self._is_sc else "PWM",
        )

        device = None

        if self._is_sc:
            # GP-SC requires LE Legacy Just Works BLE pairing.  SMP pairing
            # happens at the radio level and ESPHome BT proxies cannot relay
            # the key exchange back to BlueZ on the HA host — so we must
            # connect through a local HCI adapter.  Prefer the first local
            # adapter candidate (MAC-address-format source) over any proxy.
            try:
                candidates = bluetooth.async_scanner_devices_by_address(
                    self.hass, self._address, connectable=True
                )
            except Exception:  # noqa: BLE001
                candidates = []

            local_candidate = next(
                (c for c in candidates if self._source_is_local_hci(c.scanner.source)),
                None,
            )
            if local_candidate is not None:
                device = local_candidate.ble_device
                _LOGGER.info(
                    "SC device %s: connecting via local HCI adapter %s "
                    "(required for Just Works BLE pairing)",
                    self._address,
                    local_candidate.scanner.source,
                )
            else:
                _LOGGER.warning(
                    "SC device %s: no local HCI adapter visible in scanner pool "
                    "(sources: %s) — pairing through a proxy will likely fail; "
                    "ensure the HA host has a direct BLE adapter",
                    self._address,
                    [c.scanner.source for c in candidates],
                )
                device = bluetooth.async_ble_device_from_address(
                    self.hass, self._address, connectable=True
                )
        else:
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
            _LOGGER.warning("BLE connect failed: %s — will retry", exc)
            self._schedule_reconnect()
            return

        self._client = client
        self._connected = True
        self._reconnect_failures = 0
        self._response_buffer = ""
        _LOGGER.info("Connected to GoPower %s", self._address)

        # GP-SC: the 569a notify characteristic requires an encrypted link.
        # Explicitly call pair() to trigger LE Legacy Just Works SMP bonding
        # before accessing any secured characteristics.  BlueZ handles Just
        # Works automatically (NoInputNoOutput) — no PIN agent needed.
        if self._is_sc:
            try:
                await client.pair()
                _LOGGER.info("BLE Just Works pairing completed for %s", self._address)
            except Exception as exc:  # noqa: BLE001
                exc_str = str(exc)
                if any(k in exc_str for k in ("AlreadyExists", "Already Exists", "already")):
                    _LOGGER.info("Device %s already bonded in BlueZ", self._address)
                elif any(k in exc_str for k in ("AuthenticationFailed", "Authentication Failed")):
                    # BlueZ has a stale bond key that the device no longer recognises.
                    # Remove the stale bond so the next reconnect does a fresh Just Works pair.
                    _LOGGER.warning(
                        "GoPower %s: stale bond (AuthenticationFailed) — "
                        "removing BlueZ bond for fresh Just Works pair",
                        self._address,
                    )
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            "bluetoothctl", "remove", self._address,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        await asyncio.wait_for(proc.wait(), timeout=5.0)
                    except Exception:  # noqa: BLE001
                        pass
                    self._reconnect_failures = 0  # fresh start after bond removal
                    # The device immediately disconnects on auth failure; _on_disconnect
                    # will schedule the reconnect.  Return now to avoid dead link use.
                    return
                else:
                    _LOGGER.warning(
                        "BLE pairing attempt for %s returned: %s "
                        "(will continue — device may accept if previously bonded)",
                        self._address, exc,
                    )

        # Discover services
        await asyncio.sleep(SERVICE_DISCOVERY_DELAY)

        services = client.services
        service_uuid = SC_SERVICE_UUID if self._is_sc else SERVICE_UUID
        write_uuid = SC_WRITE_CHAR_UUID if self._is_sc else WRITE_CHAR_UUID
        notify_uuid = SC_NOTIFY_CHAR_UUID if self._is_sc else NOTIFY_CHAR_UUID

        svc = services.get_service(service_uuid)
        if svc is None:
            _LOGGER.error("GoPower service %s not found", service_uuid)
            await client.disconnect()
            return

        write_char = svc.get_characteristic(write_uuid)
        notify_char = svc.get_characteristic(notify_uuid)
        if write_char is None or notify_char is None:
            _LOGGER.error("Required characteristics not found in GoPower service")
            await client.disconnect()
            return

        # Enable notifications.
        # BlueZ may have a stale AcquireNotify session from a prior connection
        # attempt that wasn't cleanly released (common on rapid reconnects).
        # Calling stop_notify first clears that state; ignore errors if it
        # wasn't active.  Then retry start_notify once after a short delay
        # if the first attempt hits NotPermitted.
        await asyncio.sleep(OPERATION_DELAY)
        try:
            try:
                await client.stop_notify(notify_char)
            except Exception:  # noqa: BLE001
                pass  # Not active — expected on first connect
            await asyncio.sleep(OPERATION_DELAY)
            await client.start_notify(notify_char, self._on_notification)
            _LOGGER.info("Notifications enabled on %s", notify_uuid)
        except (BleakError, TimeoutError) as exc:
            if "NotPermitted" in str(exc) or "Notify acquired" in str(exc):
                _LOGGER.warning(
                    "start_notify NotPermitted (stale BlueZ state) — "
                    "waiting 2s and retrying once"
                )
                await asyncio.sleep(2.0)
                try:
                    await client.stop_notify(notify_char)
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(0.5)
                try:
                    await client.start_notify(notify_char, self._on_notification)
                    _LOGGER.info("Notifications enabled on %s (retry)", notify_uuid)
                except (BleakError, TimeoutError) as retry_exc:
                    _LOGGER.error("Failed to enable notifications (retry): %s", retry_exc)
                    await client.disconnect()
                    return
            else:
                _LOGGER.error("Failed to enable notifications: %s", exc)
                await client.disconnect()
                return

        # Start polling and watchdog
        self._start_polling()
        self._start_watchdog()
        self.async_update_listeners()

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
        self._reconnect_task = self._entry.async_create_background_task(
            self.hass, self._reconnect_after(delay), "gopower_reconnect"
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
        self._poll_task = self._entry.async_create_background_task(
            self.hass, self._poll_loop(), "gopower_poll_loop"
        )
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
            write_uuid = SC_WRITE_CHAR_UUID if self._is_sc else WRITE_CHAR_UUID
            await self._client.write_gatt_char(write_uuid, POLL_COMMAND)
        except (BleakError, TimeoutError, OSError) as exc:
            _LOGGER.warning("Poll write failed: %s", exc)

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------

    def _start_watchdog(self) -> None:
        """Start the connection health watchdog."""
        self._stop_watchdog()
        self._watchdog_task = self._entry.async_create_background_task(
            self.hass, self._watchdog_loop(), "gopower_watchdog_loop"
        )

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
        """Handle a BLE notification from the notify characteristic."""
        try:
            chunk = data.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Failed to decode notification chunk")
            return

        self._response_buffer += chunk

        if self._is_sc:
            # SC response is ASCII terminated by \r\n.  Treat any newline as
            # end-of-frame in case the \r is stripped by the BLE stack.
            if "\r\n" in self._response_buffer or "\n" in self._response_buffer:
                raw = self._response_buffer.rstrip("\r\n")
                self._response_buffer = ""
                self._last_data_time = time.monotonic()
                self.hass.async_create_task(self._parse_and_update(raw))
            else:
                semicolons = self._response_buffer.count(FIELD_DELIMITER)
                _LOGGER.debug(
                    "Assembling SC response — %d/%d fields so far",
                    semicolons + 1,
                    SC_EXPECTED_FIELD_COUNT,
                )
        else:
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
        expected = SC_EXPECTED_FIELD_COUNT if self._is_sc else EXPECTED_FIELD_COUNT
        fields = raw.split(FIELD_DELIMITER)
        if len(fields) < expected:
            _LOGGER.warning(
                "Incomplete response: %d fields (expected %d)",
                len(fields),
                expected,
            )
            return

        try:
            state = (
                self._parse_sc_fields(fields)
                if self._is_sc
                else self._parse_fields(fields)
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to parse GoPower response")
            return

        self.state = state

        if not self._first_data_received:
            self._first_data_received = True
            if self._is_sc:
                _LOGGER.info(
                    "First data from GoPower SC %s: battery=%.3fV, current=%.3fA, "
                    "soc=%d%%, temp=%d°C, fw=%s",
                    self._address,
                    state.battery_voltage,
                    state.solar_current,
                    state.state_of_charge,
                    state.temperature_c,
                    state.firmware,
                )
                _LOGGER.debug(
                    "SC raw fields %s: %s",
                    self._address,
                    "|".join(f"{i}={v}" for i, v in enumerate(fields[:30])),
                )
            else:
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
        # field[0]  = dcCurrent  (charge current into the battery, mA)
        # field[2]  = dcVoltage  (battery voltage, mV)
        # field[11] = pvvoltage  (PV panel open-circuit voltage, mV)
        solar_current_a = _float_field(FIELD_SOLAR_CURRENT) / 1000.0
        battery_voltage_v = _float_field(FIELD_BATTERY_VOLTAGE) / 1000.0
        solar_voltage_v = _float_field(FIELD_SOLAR_VOLTAGE) / 1000.0

        # Power calculation uses battery voltage × charge current, not panel
        # voltage × charge current.  For a PWM controller the panel voltage
        # (Voc ~18-22 V) is chopped down to battery voltage; the excess is
        # dissipated as heat in the switching transistor.  What flows into the
        # battery is battery_voltage × charge_current, which is the useful
        # energy delivered.  Using pvvoltage here would overstate by ~Vpv/Vbat
        # (~30 %) and is incorrect for HA energy accounting.
        solar_power_w = battery_voltage_v * solar_current_a

        # Ah → Wh
        # Field[19] is fixed-point Ah×100 (e.g. raw 150 = 1.50 Ah), so divide
        # by 100 first to get whole Ah before converting to Wh.
        amp_hours_today = _int_field(FIELD_AMP_HOURS_TODAY)
        energy_wh = int((amp_hours_today / 100.0) * battery_voltage_v)

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
            model_name="GP-PWM-30-SB",
            raw_fields=fields[:EXPECTED_FIELD_COUNT],
        )

    @staticmethod
    def _parse_sc_fields(fields: list[str]) -> GoPowerState:
        """Parse the GP-SC 30-field 569a-protocol response.

        Field mapping confirmed from HCI capture BT_HCI_2026_0517_130124.cfa
        and SolarControllerDataStorage.updateD1Data() decompile.
        """

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
            """Parse signed temperature like '+23' or '-05'."""
            try:
                return int(fields[idx].lstrip("+"))
            except (ValueError, IndexError):
                return 0

        # Field [0]: raw unit is ~100 mA per count (0027 = 2700 mA = 2.7 A)
        battery_current_a = _float_field(SC_FIELD_BATTERY_CURRENT) / 10.0

        # Field [10]: battery voltage in mV
        battery_voltage_v = _float_field(SC_FIELD_BATTERY_VOLTAGE) / 1000.0

        # Approximate solar power = charging current × battery voltage
        solar_power_w = battery_current_a * battery_voltage_v

        firmware = (
            fields[SC_FIELD_FIRMWARE]
            if SC_FIELD_FIRMWARE < len(fields)
            else ""
        )

        return GoPowerState(
            solar_voltage=None,      # Not reported by SC protocol
            solar_current=round(battery_current_a, 3),
            solar_power=round(solar_power_w, 1),
            battery_voltage=round(battery_voltage_v, 3),
            state_of_charge=_int_field(SC_FIELD_SOC),
            temperature_c=_signed_temp(SC_FIELD_TEMP_C),
            temperature_f=0,         # Not separately available in SC protocol
            energy_wh=0,             # SC amp-hours field units unconfirmed
            firmware=firmware,
            serial="",               # Not available in SC protocol
            model_name="GP-PWM-30-UL",
            raw_fields=fields[:SC_EXPECTED_FIELD_COUNT],
        )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def async_send_command(self, command: bytes) -> None:
        """Send a raw command to the write characteristic."""
        if not self._client or not self._connected:
            _LOGGER.warning("Cannot send command — not connected")
            return
        try:
            write_uuid = SC_WRITE_CHAR_UUID if self._is_sc else WRITE_CHAR_UUID
            await self._client.write_gatt_char(write_uuid, command)
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
