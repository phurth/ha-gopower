"""BlueZ pairing agent for GP-SC Just Works bonding.

BlueZ only lets an adapter bond while some process has a pairing agent
registered: with none, ``Adapter1.Pairable`` is false and ``pair()`` fails with
``AuthenticationFailed`` within a few tens of milliseconds, before the
controller is ever asked.  Just Works needs no input from the agent, but the
agent has to exist.

NOTE: This file intentionally does NOT use ``from __future__ import annotations``
because dbus_fast's ``@method()`` decorator inspects annotation strings at
class-definition time, and PEP 563 would break its parsing of the D-Bus type
characters.
"""

import contextlib
import logging
import platform
from typing import Any

_LOGGER = logging.getLogger(__name__)

AGENT_PATH = "/org/homeassistant/gopower/pairing_agent"
BLUEZ_SERVICE = "org.bluez"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"

_DBUS_AVAILABLE = False
if platform.system() == "Linux":
    try:
        from dbus_fast import BusType, Message, MessageType  # noqa: F401
        from dbus_fast.aio import MessageBus  # noqa: F401
        from dbus_fast.service import ServiceInterface, method  # noqa: F401

        _DBUS_AVAILABLE = True
    except ImportError:
        pass


if _DBUS_AVAILABLE:

    class _JustWorksAgent(ServiceInterface):  # type: ignore[misc]
        """org.bluez.Agent1 that accepts Just Works pairing."""

        def __init__(self) -> None:
            super().__init__("org.bluez.Agent1")

        @method()  # type: ignore[misc]
        def Release(self) -> None:  # noqa: N802
            _LOGGER.debug("Pairing agent: Release")

        @method()  # type: ignore[misc]
        def RequestConfirmation(self, device: "o", passkey: "u") -> None:  # type: ignore[name-defined]  # noqa: N802, F821
            _LOGGER.debug("Pairing agent: RequestConfirmation for %s — accepting", device)

        @method()  # type: ignore[misc]
        def RequestAuthorization(self, device: "o") -> None:  # type: ignore[name-defined]  # noqa: N802, F821
            _LOGGER.debug("Pairing agent: RequestAuthorization for %s — accepting", device)

        @method()  # type: ignore[misc]
        def AuthorizeService(self, device: "o", uuid: "s") -> None:  # type: ignore[name-defined]  # noqa: N802, F821
            _LOGGER.debug("Pairing agent: AuthorizeService %s on %s — accepting", uuid, device)

        @method()  # type: ignore[misc]
        def Cancel(self) -> None:  # noqa: N802
            _LOGGER.debug("Pairing agent: Cancel")


async def _call(bus: Any, member: str, signature: str, body: list) -> Any:
    return await bus.call(
        Message(
            destination=BLUEZ_SERVICE,
            path="/org/bluez",
            interface=AGENT_MANAGER_IFACE,
            member=member,
            signature=signature,
            body=body,
        )
    )


@contextlib.asynccontextmanager
async def just_works_agent():
    """Keep a NoInputNoOutput agent registered for the duration of the block.

    Yields True when the agent is in place.  Never raises on failure to
    register: pairing is then attempted as before, and BlueZ decides.

    The agent is requested as the default so that it applies to the pairing
    call, which goes out on Home Assistant's own D-Bus connection.  It is held
    only while pairing because, for those moments, it also takes precedence
    over any other integration's default agent.
    """
    bus: Any = None
    registered = False
    if _DBUS_AVAILABLE:
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            bus.export(AGENT_PATH, _JustWorksAgent())
            reply = await _call(bus, "RegisterAgent", "os", [AGENT_PATH, "NoInputNoOutput"])
            if reply.message_type == MessageType.ERROR:
                _LOGGER.warning(
                    "Could not register a pairing agent with BlueZ (%s) — "
                    "pairing may be refused",
                    reply.error_name,
                )
            else:
                registered = True
                await _call(bus, "RequestDefaultAgent", "o", [AGENT_PATH])
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Could not register a pairing agent with BlueZ (%s: %s) — "
                "pairing may be refused",
                type(exc).__name__, exc,
            )
    try:
        yield registered
    finally:
        if bus is not None:
            if registered:
                try:
                    await _call(bus, "UnregisterAgent", "o", [AGENT_PATH])
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("Pairing agent cleanup failed: %s", exc)
            bus.disconnect()
