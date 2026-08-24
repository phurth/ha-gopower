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
    ADVERTISEMENT_WAIT,
    BOND_RETRY_COOLDOWN,
    BOND_RETRY_COOLDOWN_CAP,
    IMMEDIATE_RETRY_DELAY,
    CONF_BONDED_SOURCE,
    CONF_DEVICE_TYPE,
    DEVICE_TYPE_PWM,
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
    LOCAL_HCI_CACHE_TTL,
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
    SC_FIELD_AMP_HOURS,
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
        # Deadline (time.monotonic) before which no reconnect may be attempted.
        # Set after clearing a stale BlueZ bond so the controller gets an
        # uninterrupted idle window to drop its own bond entry.
        self._bond_cooldown_until: float = 0.0
        # Energy high-water mark; see _monotonic_energy_wh.
        self._energy_high_water: int = 0
        self._last_amp_hours_raw: int = 0

        # Consecutive stale-bond clears, used to escalate the cooldown.
        self._bond_failures: int = 0
        # One retry straight after a confirmed bond clear, per normal attempt:
        # _immediate_retry_pending is set when such a retry is queued, and
        # _next/_attempt_is_immediate track whether the attempt now running is
        # that retry, so an immediate retry never spawns another one.
        self._immediate_retry_pending = False
        self._next_attempt_immediate = False
        self._attempt_is_immediate = False

        # Local BlueZ adapter MACs, with the time they were enumerated.  Only a
        # non-empty result is ever cached — see _async_get_local_hci_macs.
        self._local_hci_cache: tuple[set[str], float] = (set(), 0.0)
        self._local_hci_warned = False

        # Scanner source (adapter MAC) used by the in-flight connection, so a
        # successful GP-SC pairing can be pinned to the adapter that holds it.
        self._current_connect_source: str | None = None

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

    async def _async_get_local_hci_macs(self) -> set[str]:
        """Return the MAC addresses of local BlueZ HCI adapters via D-Bus.

        Only a *successful* (non-empty) lookup is cached.  An empty result
        disables adapter pinning altogether, so caching one turned a momentary
        D-Bus hiccup into a 60-second window where every GP-SC connect silently
        fell through to whatever source the manager offered — including an
        ESPHome proxy, which can never complete SMP pairing and so guaranteed
        an AuthenticationFailed / bond-wipe loop.

        Returns an empty set if D-Bus is unavailable (non-Linux host).
        """
        now = time.monotonic()
        cached_macs, cached_at = self._local_hci_cache
        if cached_macs and now - cached_at < LOCAL_HCI_CACHE_TTL:
            return cached_macs

        macs: set[str] = set()
        failure: str | None = None
        try:
            from dbus_fast import BusType, Message, MessageType  # noqa: PLC0415
            from dbus_fast.aio import MessageBus  # noqa: PLC0415

            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            try:
                reply = await bus.call(
                    Message(
                        destination="org.bluez",
                        path="/",
                        interface="org.freedesktop.DBus.ObjectManager",
                        member="GetManagedObjects",
                    )
                )
                if reply.message_type == MessageType.ERROR or not reply.body:
                    failure = getattr(reply, "error_name", None) or "empty reply"
                else:
                    for interfaces in reply.body[0].values():
                        adapter = interfaces.get("org.bluez.Adapter1")
                        if adapter is not None:
                            addr = adapter.get("Address")
                            if addr is not None:
                                if hasattr(addr, "value"):
                                    addr = addr.value
                                macs.add(str(addr).upper())
            finally:
                bus.disconnect()
        except Exception as exc:  # noqa: BLE001
            failure = f"{type(exc).__name__}: {exc}"

        if macs:
            self._local_hci_cache = (macs, now)
            self._local_hci_warned = False
            return macs

        # Warn once per outage rather than on every reconnect attempt.
        if not self._local_hci_warned:
            self._local_hci_warned = True
            if failure is not None:
                _LOGGER.warning(
                    "Could not enumerate local HCI adapters via D-Bus (%s) — "
                    "adapter pinning is unavailable until this recovers",
                    failure,
                )
            else:
                _LOGGER.warning(
                    "BlueZ reports no local HCI adapters on this host — "
                    "GP-SC pairing requires a direct BLE adapter",
                )
        return macs

    async def _async_remove_bluez_bond(self) -> bool:
        """Remove the BlueZ device object (and its bond) for this address.

        Talks to BlueZ over D-Bus rather than shelling out to bluetoothctl:
        that binary is not guaranteed to exist inside the Home Assistant
        container, and when it is missing the removal silently does nothing —
        leaving the controller in the AuthenticationFailed loop this is meant
        to break.  Returns True if BlueZ removed at least one device object.
        """
        removed = False
        try:
            from dbus_fast import BusType, Message, MessageType  # noqa: PLC0415
            from dbus_fast.aio import MessageBus  # noqa: PLC0415

            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            try:
                reply = await bus.call(
                    Message(
                        destination="org.bluez",
                        path="/",
                        interface="org.freedesktop.DBus.ObjectManager",
                        member="GetManagedObjects",
                    )
                )
                if reply.message_type == MessageType.ERROR or not reply.body:
                    _LOGGER.warning(
                        "Could not list BlueZ objects to clear the bond for %s (%s)",
                        self._address,
                        getattr(reply, "error_name", "no reply"),
                    )
                    return False

                target = self._address.upper()
                for path, interfaces in reply.body[0].items():
                    device = interfaces.get("org.bluez.Device1")
                    if device is None:
                        continue
                    addr = device.get("Address")
                    if addr is not None and hasattr(addr, "value"):
                        addr = addr.value
                    if addr is None or str(addr).upper() != target:
                        continue
                    adapter_path = path.rsplit("/", 1)[0]
                    rm = await bus.call(
                        Message(
                            destination="org.bluez",
                            path=adapter_path,
                            interface="org.bluez.Adapter1",
                            member="RemoveDevice",
                            signature="o",
                            body=[path],
                        )
                    )
                    if rm.message_type == MessageType.ERROR:
                        _LOGGER.warning(
                            "BlueZ refused to remove %s from %s: %s",
                            path, adapter_path, rm.error_name,
                        )
                    else:
                        removed = True
                        _LOGGER.info(
                            "Cleared BlueZ bond for %s on adapter %s",
                            self._address, adapter_path,
                        )
            finally:
                bus.disconnect()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Could not clear the BlueZ bond for %s via D-Bus (%s: %s) — "
                "the controller may keep rejecting pairing until the bond is "
                "removed manually",
                self._address, type(exc).__name__, exc,
            )
            return False

        if not removed:
            _LOGGER.info(
                "No BlueZ device object for %s — nothing to unbond; the stale "
                "bond is on the controller's side and needs an idle period",
                self._address,
            )
        return removed

    def _source_is_local_hci(self, source: str, local_macs: set[str]) -> bool:
        """Return True if *source* is a known local BlueZ HCI adapter MAC."""
        return source.upper() in local_macs

    def _sc_scanner_candidates(self) -> list[Any]:
        """Return the connectable scanners whose *live* cache holds this address.

        This is deliberately not the manager's advertisement history: the
        history keeps an address for minutes after bleak has dropped it from
        the scanner cache (BlueZ emits InterfacesRemoved when a bond is
        cleared, which is exactly what the stale-bond path does).  A BLEDevice
        rebuilt from history has no backend that can reach it, which surfaces
        as "No backend with an available connection slot".
        """
        try:
            return bluetooth.async_scanner_devices_by_address(
                self.hass, self._address, connectable=True
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Scanner lookup failed for %s (%s: %s) — treating as no live sources",
                self._address, type(exc).__name__, exc,
            )
            return []

    async def _async_wait_for_advertisement(
        self, timeout: float = ADVERTISEMENT_WAIT
    ) -> bool:
        """Wait for a fresh advertisement from this address.

        Also requests active scanning for the duration, which is what prompts
        BlueZ to recreate a device object it was told to remove.  On Linux,
        Home Assistant's default "auto" scanning mode resolves to passive.
        """
        _LOGGER.debug(
            "Waiting up to %.0fs for an advertisement from %s", timeout, self._address
        )
        try:
            await bluetooth.async_process_advertisements(
                self.hass,
                lambda service_info: True,
                {"address": self._address, "connectable": True},
                bluetooth.BluetoothScanningMode.ACTIVE,
                int(timeout),
            )
        except TimeoutError:
            _LOGGER.debug("No advertisement from %s within %.0fs", self._address, timeout)
            return False
        return True

    async def _async_pick_sc_device(self) -> tuple[Any | None, str | None]:
        """Pick the BLE device and source to use for a GP-SC connect.

        GP-SC requires LE Legacy Just Works pairing.  SMP runs at the radio
        level and ESPHome BT proxies cannot relay the key exchange back to
        BlueZ on the HA host, so the connection must go through a local HCI
        adapter.  Returns (device, source); source is None when no local
        adapter could be confirmed and a last-resort device is returned.
        """
        candidates = self._sc_scanner_candidates()
        if not candidates:
            # Most likely the address was just dropped from bleak's cache by a
            # bond clear.  Wait for it to be advertised again rather than
            # connecting to a history entry that cannot be reached.
            if await self._async_wait_for_advertisement():
                candidates = self._sc_scanner_candidates()

        local_macs = await self._async_get_local_hci_macs()
        local_candidates = [
            c for c in candidates
            if self._source_is_local_hci(c.scanner.source, local_macs)
        ]

        # A BLE bond is keyed to the adapter that created it.  On a host with
        # several HCI adapters, reconnecting through a different one presents
        # an unfamiliar peer and the controller rejects pairing with
        # AuthenticationFailed.  Prefer the adapter that completed the pairing
        # last time; fall back to any local adapter if it is gone.
        bonded_source: str | None = self._entry.options.get(CONF_BONDED_SOURCE)
        local_candidate = None
        if bonded_source:
            local_candidate = next(
                (c for c in local_candidates if c.scanner.source == bonded_source),
                None,
            )
            if local_candidate is None and local_candidates:
                _LOGGER.warning(
                    "SC device %s: bonded adapter %s unavailable (present: %s) — "
                    "using another local adapter; a re-pair will likely be needed",
                    self._address,
                    bonded_source,
                    [c.scanner.source for c in local_candidates],
                )
        if local_candidate is None:
            local_candidate = local_candidates[0] if local_candidates else None

        if local_candidate is not None:
            _LOGGER.info(
                "SC device %s: connecting via local HCI adapter %s%s "
                "(required for Just Works BLE pairing)",
                self._address,
                local_candidate.scanner.source,
                " [pinned]" if local_candidate.scanner.source == bonded_source else "",
            )
            return local_candidate.ble_device, local_candidate.scanner.source

        # No local adapter to use.  Say which of the three distinct situations
        # this is — they need very different responses from the operator.
        sources = [c.scanner.source for c in candidates]
        if not candidates:
            _LOGGER.warning(
                "SC device %s: not in any connectable scanner's live cache and no "
                "advertisement seen within %.0fs — the controller may be out of "
                "range, asleep, or still connected to the vendor app",
                self._address, ADVERTISEMENT_WAIT,
            )
        elif not local_macs:
            _LOGGER.warning(
                "SC device %s: seen by %s, but the local BlueZ adapter list is "
                "unavailable, so none of them can be confirmed as a direct "
                "adapter — attempting anyway; if this repeats, check that the "
                "Home Assistant container can reach the BlueZ D-Bus system bus",
                self._address, sources,
            )
        else:
            # Clearing a bond briefly removes the address from the local
            # adapter's live cache (BlueZ InterfacesRemoved), so on a post-clear
            # retry this is expected and the fallback still normally lands on a
            # local adapter — it is not evidence of a range problem.
            log = _LOGGER.debug if self._attempt_is_immediate else _LOGGER.warning
            log(
                "SC device %s: currently seen only by %s, none of which is a local "
                "adapter (local adapters here: %s) — falling back to the source "
                "Home Assistant picks. Just Works pairing cannot be relayed through "
                "an ESPHome proxy, so if this repeats the controller may be out of "
                "range of the host's own adapter",
                self._address, sources, sorted(local_macs),
            )

        # Last resort: whatever the manager offers.  It may still be the right
        # adapter (e.g. the D-Bus list was unavailable), so this is worth one
        # attempt — but do not pin a bond to a source we could not verify.
        return (
            bluetooth.async_ble_device_from_address(
                self.hass, self._address, connectable=True
            ),
            None,
        )

    # ------------------------------------------------------------------
    # Connect / disconnect
    # ------------------------------------------------------------------

    async def async_connect(self) -> None:
        """Establish BLE connection, discover services, start polling."""
        async with self._connect_lock:
            if self._connected:
                return
            # Re-check the stale-bond hold under the lock.  Several reconnect
            # tasks can be in flight at once, and one that cleared its own
            # pre-connect check microseconds before the cooldown was recorded
            # would otherwise connect straight through the idle window the
            # controller needs to drop its bond — restarting the thrash.
            remaining = self._bond_cooldown_until - time.monotonic()
            if remaining > 0:
                _LOGGER.info(
                    "Skipping connect to %s: %.0fs of stale-bond cooldown remain",
                    self._address, remaining,
                )
                if self._reconnect_task is None:
                    self._schedule_reconnect()
                return
            await self._do_connect()

    async def _do_connect(self) -> None:
        """Internal connect logic."""
        _LOGGER.info(
            "Connecting to GoPower %s (variant=%s)",
            self._address,
            "SC" if self._is_sc else "PWM",
        )
        # Cleared per attempt so a failed connect can never persist a stale source.
        self._current_connect_source = None
        self._attempt_is_immediate = self._next_attempt_immediate
        self._next_attempt_immediate = False
        self._immediate_retry_pending = False

        device = None

        if self._is_sc:
            device, self._current_connect_source = await self._async_pick_sc_device()
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
        self._response_buffer = ""
        _LOGGER.info("Connected to GoPower %s", self._address)

        # GP-SC: the 569a notify characteristic requires an encrypted link.
        # Explicitly call pair() to trigger LE Legacy Just Works SMP bonding
        # before accessing any secured characteristics.  BlueZ handles Just
        # Works automatically (NoInputNoOutput) — no PIN agent needed.
        if self._is_sc:
            try:
                await client.pair()
                self._bond_failures = 0
                _LOGGER.info("BLE Just Works pairing completed for %s", self._address)
            except Exception as exc:  # noqa: BLE001
                exc_str = str(exc)
                if any(k in exc_str for k in ("AlreadyExists", "Already Exists", "already")):
                    self._bond_failures = 0
                    _LOGGER.info("Device %s already bonded in BlueZ", self._address)
                elif any(k in exc_str for k in ("AuthenticationFailed", "Authentication Failed")):
                    # BlueZ has a stale bond key that the device no longer recognises.
                    # Remove the stale bond so the next reconnect does a fresh Just Works pair.
                    _LOGGER.warning(
                        "GoPower %s: stale bond (AuthenticationFailed) — "
                        "removing BlueZ bond for fresh Just Works pair",
                        self._address,
                    )
                    # Record the cooldown *before* any await.  The controller
                    # only purges its own stale bond entry after a prolonged
                    # idle period — field logs show upwards of ten minutes — so
                    # the wait escalates with each consecutive failure instead
                    # of hammering a fixed short retry.  Setting the deadline
                    # first means a cancellation landing inside the removal
                    # cannot discard it.  A deadline rather than a sleep here:
                    # the device drops the link immediately on auth failure, so
                    # _on_disconnect may already have queued a reconnect, and
                    # both _schedule_reconnect and _reconnect_after enforce it.
                    self._bond_failures += 1
                    cooldown = min(
                        BOND_RETRY_COOLDOWN * (2 ** (self._bond_failures - 1)),
                        BOND_RETRY_COOLDOWN_CAP,
                    )
                    self._bond_cooldown_until = time.monotonic() + cooldown
                    removed = await self._async_remove_bluez_bond()

                    # The only pair this controller has ever accepted came
                    # immediately after a completed RemoveDevice, with no gap;
                    # every attempt made seconds or minutes after a clear has
                    # been rejected.  So when a clear is confirmed, spend one
                    # retry on that pattern before falling back to the idle
                    # window.  An immediate retry never queues another, so it
                    # costs at most one extra attempt per cooldown cycle.
                    if removed and not self._attempt_is_immediate:
                        self._bond_cooldown_until = 0.0
                        self._immediate_retry_pending = True
                        _LOGGER.info(
                            "Bond for %s cleared — retrying at once while the "
                            "clear is fresh (consecutive failures: %d)",
                            self._address, self._bond_failures,
                        )
                        # Schedule it here rather than relying on the disconnect
                        # callback: that callback can run either side of this
                        # handler, and when it runs first it queues the ordinary
                        # cooldown retry and nothing would consume the flag.
                        self._schedule_reconnect()
                        return

                    _LOGGER.info(
                        "Holding %s idle for %.0fs so the controller can drop its "
                        "own stale bond (consecutive failures: %d)",
                        self._address, cooldown, self._bond_failures,
                    )
                    # Deliberately leave _reconnect_failures alone: resetting it
                    # pinned the backoff at the base delay, so repeated bond
                    # failures hammered the adapter instead of backing off.
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
            # The advertised name does not reliably encode the protocol — some
            # units badged GP-PWM-30-SB expose the 569a service.  If the other
            # variant's service is present, correct the stored device type so
            # the entry self-heals instead of failing on every reconnect.
            other_uuid = SERVICE_UUID if self._is_sc else SC_SERVICE_UUID
            if services.get_service(other_uuid) is not None:
                corrected = DEVICE_TYPE_PWM if self._is_sc else DEVICE_TYPE_SC
                _LOGGER.warning(
                    "GoPower %s: service %s absent but %s present — device is "
                    "actually variant %s, correcting stored device type and "
                    "reconnecting via the matching protocol",
                    self._address,
                    service_uuid,
                    other_uuid,
                    corrected,
                )
                self._is_sc = corrected == DEVICE_TYPE_SC
                self.hass.config_entries.async_update_entry(
                    self._entry,
                    data={**self._entry.data, CONF_DEVICE_TYPE: corrected},
                )
                # Reconnect from scratch rather than continuing on this link:
                # the GP-SC path must pair before touching the 569a notify
                # characteristic, and that step was skipped on this attempt.
                await client.disconnect()
                return
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

        # Only now is the connection actually usable, so clear the backoff here
        # rather than at link establishment.  A GP-SC link establishes fine and
        # then fails to pair; resetting earlier let that path restart the
        # backoff from the base delay on every failed attempt.
        self._reconnect_failures = 0

        # Pin the adapter that carries the bond.  Persist only now, on a fully
        # working connection, so a failed pairing attempt can never lock future
        # connects to the wrong adapter.  GP-PWM does not pair, so has no bond
        # to pin.
        if self._is_sc and self._current_connect_source is not None:
            if self._entry.options.get(CONF_BONDED_SOURCE) != self._current_connect_source:
                _LOGGER.info(
                    "Pinning GoPower %s to bonded adapter %s",
                    self._address,
                    self._current_connect_source,
                )
                self.hass.config_entries.async_update_entry(
                    self._entry,
                    options={
                        **self._entry.options,
                        CONF_BONDED_SOURCE: self._current_connect_source,
                    },
                )

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
        if self._immediate_retry_pending:
            # Post-clear retry: deliberately skips both the backoff and the
            # stale-bond hold, which is the entire point of the attempt.  The
            # flag is cleared when the attempt starts, not here, so a later
            # disconnect callback re-scheduling cannot downgrade it back to an
            # ordinary cooldown retry.
            self._next_attempt_immediate = True
            _LOGGER.info(
                "Reconnecting to %s in %.1fs (post-clear retry)",
                self._address, IMMEDIATE_RETRY_DELAY,
            )
            self._reconnect_task = self._entry.async_create_background_task(
                self.hass,
                self._reconnect_after(IMMEDIATE_RETRY_DELAY),
                "gopower_reconnect",
            )
            return
        delay = min(
            RECONNECT_BACKOFF_BASE * (2 ** (self._reconnect_failures - 1)),
            RECONNECT_BACKOFF_CAP,
        )
        # Never retry before a pending stale-bond cooldown expires — the
        # controller needs that idle window to drop its own bond entry.
        cooldown_remaining = self._bond_cooldown_until - time.monotonic()
        if cooldown_remaining > delay:
            _LOGGER.info(
                "Holding reconnect for %.0fs (stale-bond cooldown) instead of %.0fs",
                cooldown_remaining,
                delay,
            )
            delay = cooldown_remaining
        _LOGGER.info(
            "Reconnecting in %.0fs (attempt %d)", delay, self._reconnect_failures
        )
        self._reconnect_task = self._entry.async_create_background_task(
            self.hass, self._reconnect_after(delay), "gopower_reconnect"
        )

    async def _reconnect_after(self, delay: float) -> None:
        """Wait then reconnect, honouring any stale-bond cooldown."""
        await asyncio.sleep(delay)
        # The cooldown may have been set *after* this retry was queued: the
        # device drops the link the instant pairing fails, so _on_disconnect
        # can reach _schedule_reconnect before the pairing error handler runs.
        # Re-check the deadline here so the hold applies regardless of ordering.
        remaining = self._bond_cooldown_until - time.monotonic()
        if remaining > 0:
            _LOGGER.info(
                "Deferring reconnect a further %.0fs (stale-bond cooldown)", remaining
            )
            await asyncio.sleep(remaining)
        # Past this point the task is no longer a *pending* reconnect — it is the
        # connect itself.  Detach it so that a disconnect arriving mid-connect
        # cancels nothing: a failed pair drops the link instantly, and cancelling
        # here killed the handler that records the stale-bond cooldown and could
        # cut the BlueZ bond removal in half.  Home Assistant still owns the task
        # (async_create_background_task) and cancels it on unload.
        if self._reconnect_task is asyncio.current_task():
            self._reconnect_task = None
        try:
            await self.async_connect()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Reconnect failed")

    def _cancel_reconnect(self) -> None:
        """Cancel a *pending* reconnect.

        A reconnect task detaches itself from _reconnect_task once it stops
        waiting and starts connecting (see _reconnect_after), so an in-flight
        connect is never cancelled from here.  It used to be: a failed pair
        makes the controller drop the link instantly, and the resulting
        _on_disconnect -> _schedule_reconnect -> _cancel_reconnect chain killed
        the very task running _do_connect, discarding the stale-bond
        bookkeeping that follows and sometimes cutting the bond removal in
        half.  That is why the cooldown never applied and retries ran at the
        plain backoff cap.
        """
        task = self._reconnect_task
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
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

        # Smooth the voltage-driven wobble out of the energy total before it
        # reaches the recorder (both variants derive Wh from an Ah counter).
        ah_index = SC_FIELD_AMP_HOURS if self._is_sc else FIELD_AMP_HOURS_TODAY
        try:
            raw_ah_int = int(state.raw_fields[ah_index])
        except (IndexError, TypeError, ValueError):
            # Unparseable counter: treat as unchanged so the mark is only held,
            # never released on bad data.
            raw_ah_int = self._last_amp_hours_raw
        state.energy_wh = self._monotonic_energy_wh(raw_ah_int, state.energy_wh)

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

    def _monotonic_energy_wh(self, amp_hours_raw: int, energy_wh: int) -> int:
        """Return an energy total that only falls when the counter itself resets.

        energy_wh is an Ah counter scaled by the *live* battery voltage, so it
        drifts down whenever the battery sags even though no energy was
        un-delivered.  A sensor with state_class total_increasing treats any
        decrease as a counter wrap and adds the whole new value to the
        long-term sum, so a 0.1 % voltage dip used to inject a spurious
        multi-kWh jump into statistics.

        Hold a high-water mark and release it only when the underlying Ah
        counter drops — a real reset (daily rollover, or the Reset History
        button), which total_increasing is designed to handle.
        """
        if amp_hours_raw < self._last_amp_hours_raw:
            self._energy_high_water = energy_wh
        else:
            self._energy_high_water = max(self._energy_high_water, energy_wh)
        self._last_amp_hours_raw = amp_hours_raw
        return self._energy_high_water

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

        # Field [28]: cumulative Ah × 100 (same encoding as PWM daily Ah field).
        # Divide by 100 to get whole Ah, then multiply by battery voltage for Wh.
        amp_hours_cumulative = _int_field(SC_FIELD_AMP_HOURS)
        energy_wh = int((amp_hours_cumulative / 100.0) * battery_voltage_v)

        return GoPowerState(
            solar_voltage=None,      # Not reported by SC protocol
            solar_current=round(battery_current_a, 3),
            solar_power=round(solar_power_w, 1),
            battery_voltage=round(battery_voltage_v, 3),
            state_of_charge=_int_field(SC_FIELD_SOC),
            temperature_c=_signed_temp(SC_FIELD_TEMP_C),
            temperature_f=0,         # Not separately available in SC protocol
            energy_wh=energy_wh,
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
