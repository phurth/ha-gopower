# GoPower Solar HACS Integration — Technical Specification

## 1. Purpose and Scope

`ha_gopower` integrates Go Power GP-PWM BLE solar controllers with Home Assistant. It provides real-time telemetry and selected maintenance controls.

## 2. Integration Snapshot

- **Domain:** `ha_gopower`
- **Primary runtime component:** `GoPowerCoordinator`
- **Platforms:** `binary_sensor`, `button`, `sensor`
- **Transport:** BLE GATT (`FFF0` service family)
- **Coordinator mode:** connected poll loop + notify assembly

## 3. Configuration and Entry Setup

- config flow supports BLE auto-discovery (service UUID and name-prefix matching)
- manual MAC address path is available when discovery is incomplete
- setup forwards platforms and starts initial connection in background

## 4. Runtime Lifecycle

1. Entry setup creates and stores coordinator.
2. Coordinator connects to configured BLE address.
3. Service/characteristics are validated.
4. Notifications are enabled.
5. Poll loop sends periodic status request command.
6. Response fragments are assembled and parsed.
7. Entity state updates from parsed coordinator data.

## 5. Protocol and Transport Model

### 5.1 Poll and response behavior

- poll command is single-byte ASCII space (`0x20`)
- response format is semicolon-delimited ASCII field sequence
- parser requires expected field count before committing state

GATT layout:

- **Service:** `0000fff0-0000-1000-8000-00805f9b34fb`
- **Write characteristic:** `0000fff2-0000-1000-8000-00805f9b34fb`
- **Notify characteristic:** `0000fff1-0000-1000-8000-00805f9b34fb`

Parser expects `32` delimited fields. Important indices include:

- solar current (`0`, mA→A)
- battery voltage (`2`, mV→V)
- SOC (`10`)
- solar voltage (`11`, mV→V)
- serial (`14`, hex→decimal string)
- temp C/F (`16`/`17`)
- amp-hours today (`19`, used with battery voltage for Wh)

### 5.2 Command path

Writable maintenance operations include reboot and history reset, using unlock + delayed operation sequencing.

Control command bytes:

- unlock: `&G++0900`
- reboot: `&LDD0100`
- reset history: `&LDD0200`
- factory reset constant exists but is not surfaced as a standard entity path.

## 6. State and Entity Model

- `GoPowerState` stores electrical telemetry, temperature, SOC, firmware, serial, and raw field payload.
- sensor entities expose normalized engineering units.
- binary sensors represent connection and data freshness.
- button entities map maintenance operations.

## 7. Command and Control Surface

- reboot controller
- reset history counters

Writes are sequenced to avoid conflict with poll/notify handling.

## 8. Reliability and Recovery

- reconnect loop with exponential backoff
- watchdog checks for stale data windows
- `data_healthy` requires active connection and recent data age
- non-blocking startup avoids HA boot stalls

Timing constants:

- poll interval: `4.0s`
- unlock delay: `0.2s`
- operation delay: `0.1s`
- service discovery delay: `0.2s`
- watchdog interval: `60s`
- stale timeout: `300s`

## 9. Diagnostics and Observability

- connection and data-health signals
- firmware/serial/model diagnostics
- raw field retention to support parser troubleshooting
- last-data-age tracking for stale-stream analysis

## 10. Security and Safety Notes

- local BLE control path only
- limited control scope reduces accidental high-impact writes
- strict parser validation helps prevent malformed-state updates

## 11. Evolution Notes (Commit History)

Recent trajectory includes:

- initial BLE integration and parser foundation
- startup/connect hardening
- discovery and adapter behavior refinement
- migration to `ha_gopower` domain naming

## 12. Known Constraints

- protocol is telemetry-centric; control operations are intentionally narrow
- incomplete fragment assembly prevents state updates by design
- RF contention can produce delayed polls and temporary stale status

## 13. Extension Guidelines

1. Keep field index mappings centralized in constants.
2. Preserve strict field-count validation on parser commit.
3. Gate new writable commands behind explicit safety checks.
4. Keep control sequencing compatible with poll loop cadence.
5. Add diagnostics first before promoting new values to default entities.
