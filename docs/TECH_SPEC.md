# GoPower Solar HACS Integration — Technical Specification

## 1. Purpose and Scope

`ha_gopower` integrates Go Power GP-PWM BLE solar controllers with Home Assistant. It provides real-time telemetry and selected maintenance controls for two hardware variants that use distinct BLE protocols.

## 2. Integration Snapshot

- **Domain:** `ha_gopower`
- **Primary runtime component:** `GoPowerCoordinator`
- **Platforms:** `binary_sensor`, `button`, `sensor`
- **Transport:** BLE GATT (FFF0 service family for GP-PWM-30-SB; 569a service family for GP-PWM-30-UL)
- **Coordinator mode:** connected poll loop + notify assembly

## 3. Configuration and Entry Setup

- config flow supports BLE auto-discovery (service UUID and name-prefix matching)
- device type (PWM or SC) is inferred at config time from the advertised BLE local name:
  - `GPPWM*` → SC (GP-PWM-30-UL, 569a protocol)
  - `GP-PWM*` / `GoPower*` → PWM (GP-PWM-30-SB, FFF0 protocol)
- manual MAC address path is available when discovery is incomplete
- setup forwards platforms and starts initial connection in background

## 4. Runtime Lifecycle

1. Entry setup creates and stores coordinator.
2. Coordinator connects to configured BLE address.
   - SC devices: local HCI adapter preferred; `client.pair()` called to establish LE Legacy Just Works bond before accessing secured characteristics.
3. Service/characteristics are validated.
4. Notifications are enabled.
5. Poll loop sends periodic status request command.
6. Response fragments are assembled and parsed.
7. Entity state updates from parsed coordinator data.

## 5. Protocol and Transport Model

### 5.1 GP-PWM-30-SB — FFF0 Protocol

- **Service:** `0000fff0-0000-1000-8000-00805f9b34fb`
- **Write characteristic:** `0000fff2-0000-1000-8000-00805f9b34fb`
- **Notify characteristic:** `0000fff1-0000-1000-8000-00805f9b34fb`
- **Poll command:** ASCII space (`0x20`)
- **Response format:** semicolon-delimited ASCII, 32 fields, no explicit terminator
- **Pairing:** none required

Key field indices:

| Index | Name | Raw unit | Scaled unit | Notes |
|-------|------|----------|-------------|-------|
| 0 | dcCurrent | mA | ÷1000 → A | Charge current **into battery** |
| 2 | dcVoltage | mV | ÷1000 → V | Battery terminal voltage |
| 8 | firmware | int | — | |
| 10 | stateOfCharge | % | — | |
| 11 | pvVoltage | mV | ÷1000 → V | PV panel open-circuit voltage |
| 14 | serial | hex string | →decimal | |
| 16 | temperatureC | signed int | — | |
| 17 | temperatureF | signed int | — | |
| 19 | ampHoursToday | Ah | ×Vbat → Wh | |

### 5.2 GP-PWM-30-UL — 569a Protocol

- **Service:** `569a1101-b87f-490c-92cb-11ba5ea5167c`
- **Write characteristic:** `569a2001-b87f-490c-92cb-11ba5ea5167c`
- **Notify characteristic:** `569a2000-b87f-490c-92cb-11ba5ea5167c`
- **Poll command:** ASCII space (`0x20`)
- **Response format:** semicolon-delimited ASCII, 30 fields, terminated with `\r\n`
- **Pairing:** LE Legacy Just Works required (notify characteristic requires encrypted link)

Key field indices:

| Index | Name | Raw unit | Scaled unit | Notes |
|-------|------|----------|-------------|-------|
| 0 | batteryCurrent | raw ×100 mA | ÷10 → A | Charge current **into battery** |
| 6 | firmwareVersion | string | — | |
| 10 | batteryVoltage | mV | ÷1000 → V | Battery terminal voltage |
| 12 | stateOfCharge | % | — | |
| 13 | temperatureC | signed string | — | e.g. `+23` or `-05` |
| 28 | batteryAmpHours | cumulative Ah | — | Units unconfirmed |

### 5.3 Power Calculation

For both device types, **Charge Power = battery_voltage × charge_current**.

A PWM controller connects the panel directly to the battery during the on-phase of the switching cycle. The panel open-circuit voltage (~18–22 V) is higher than the battery voltage (~12–14 V); the difference is dissipated as heat in the switching transistor. The energy delivered to the battery is `Vbattery × Icharge`, not `Vpanel × Icharge`. Using panel voltage would overstate by `Vpanel / Vbattery` (~30–60 %) and produce incorrect HA energy accounting.

Panel voltage (`pvVoltage`) is exposed as the **Solar Voltage** sensor on GP-PWM-30-SB for informational monitoring only. It is not available in the GP-PWM-30-UL protocol and that sensor reports unavailable for that device type.

### 5.4 Command path

Writable maintenance operations include reboot and history reset, using unlock + delayed operation sequencing.

Control command bytes:

- unlock: `&G++0900`
- reboot: `&LDD0100`
- reset history: `&LDD0200`
- factory reset constant exists but is not surfaced as a standard entity path.

## 6. Entity Availability by Device Type

| Entity | GP-PWM-30-SB | GP-PWM-30-UL |
|--------|:---:|:---:|
| Solar Voltage | ✓ | — (unavailable) |
| Charge Current | ✓ | ✓ |
| Charge Power | ✓ | ✓ |
| Battery Voltage | ✓ | ✓ |
| State of Charge | ✓ | ✓ |
| Temperature | ✓ | ✓ |
| Energy Today | ✓ | — |
| Serial Number | ✓ | — |

## 7. State and Entity Model

- `GoPowerState` stores electrical telemetry, temperature, SOC, firmware, serial, and raw field payload.
- `solar_voltage` is `float | None`; sensor reports unavailable when `None`.
- sensor entities expose normalized engineering units.
- binary sensors represent connection and data freshness.
- button entities map maintenance operations.

## 8. Command and Control Surface

- reboot controller
- reset history counters

Writes are sequenced to avoid conflict with poll/notify handling.

## 9. Reliability and Recovery

- reconnect loop with exponential backoff
- watchdog checks for stale data windows
- `data_healthy` requires active connection and recent data age
- non-blocking startup avoids HA boot stalls
- `stop_notify` before `start_notify` clears stale BlueZ AcquireNotify state on rapid reconnect

Timing constants:

- poll interval: `4.0s`
- unlock delay: `0.2s`
- operation delay: `0.1s`
- service discovery delay: `0.2s`
- watchdog interval: `60s`
- stale timeout: `300s`

## 10. Security and Safety Notes

- local BLE control path only
- GP-PWM-30-UL pairing uses LE Legacy Just Works (NoInputNoOutput); BlueZ manages the bond
- limited control scope reduces accidental high-impact writes
- strict parser validation helps prevent malformed-state updates

## 11. Known Constraints

- GP-PWM-30-UL requires local HCI adapter on the HA host; pairing cannot be performed via an ESPHome BLE proxy
- protocol is telemetry-centric; control operations are intentionally narrow
- GP-PWM-30-UL panel-side voltage and energy-today figures are not transmitted by the device

- incomplete fragment assembly prevents state updates by design
- RF contention can produce delayed polls and temporary stale status

## 13. Extension Guidelines

1. Keep field index mappings centralized in constants.
2. Preserve strict field-count validation on parser commit.
3. Gate new writable commands behind explicit safety checks.
4. Keep control sequencing compatible with poll loop cadence.
5. Add diagnostics first before promoting new values to default entities.
