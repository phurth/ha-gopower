# GoPower Solar — Home Assistant HACS Integration

Native BLE integration for **Go Power! PWM solar charge controllers** (GP-PWM series).

Connects directly to the controller over Bluetooth Low Energy — no cloud, no MQTT bridge, no internet required.

> **Disclaimer:** This is an independent community integration and is not affiliated with, endorsed by, or supported by Go Power! or any of its affiliates. Use it at your own risk.

## Supported Devices

Two hardware variants use different BLE protocols and expose different data:

| Model | BLE name | Protocol | Pairing required |
|-------|----------|----------|-----------------|
| GP-PWM-30-SB | `GP-PWM*` / `GoPower*` | FFF0 service, 32-field ASCII | No |
| GP-PWM-30-UL | `GPPWM*` (e.g. `GPPWM30BLE`) | 569a service, 30-field ASCII | Yes (LE Just Works) |

## Entities

| Entity | GP-PWM-30-SB | GP-PWM-30-UL | Notes |
|--------|:---:|:---:|-------|
| Solar Voltage | ✓ | — | Panel open-circuit voltage (field 11, mV). Not transmitted by GP-PWM-30-UL. |
| Charge Current | ✓ | ✓ | Current flowing into the battery (not panel current). |
| Charge Power | ✓ | ✓ | `battery_voltage × charge_current` — energy delivered to battery. |
| Battery Voltage | ✓ | ✓ | |
| State of Charge | ✓ | ✓ | |
| Temperature | ✓ | ✓ | |
| Energy Today | ✓ | — | Amp-hours × battery voltage. Not available on GP-PWM-30-UL. |
| Connected | ✓ | ✓ | Binary sensor |
| Data Healthy | ✓ | ✓ | Binary sensor |
| Model Number | ✓ | ✓ | Diagnostic |
| Firmware Version | ✓ | ✓ | Diagnostic |
| Serial Number | ✓ | — | Diagnostic; not transmitted by GP-PWM-30-UL. |
| Reboot Controller | ✓ | ✓ | Button |
| Reset History | ✓ | ✓ | Button |

### Note on Charge Power vs Solar Power

For a PWM controller the solar panel connects directly to the battery during the on-phase of the PWM cycle. The panel open-circuit voltage (~18–22 V) is higher than the battery voltage (~12–14 V); the voltage difference is dissipated as heat in the switching transistor. The energy actually stored in the battery is `battery_voltage × charge_current`, not `panel_voltage × charge_current`. Using panel voltage for power would overstate by roughly `Vpanel / Vbattery` (~30–60 %). Charge Power uses the battery-side calculation for accurate HA energy statistics.

## Requirements

- Home Assistant 2024.1+ with Bluetooth integration
- Bluetooth adapter on the HA host (or ESPHome BT proxy for GP-PWM-30-SB; **local adapter required** for GP-PWM-30-UL pairing)
- GoPower GP-PWM solar controller within BLE range

## Installation (HACS)

1. Add this repository as a custom HACS repository
2. Install "GoPower Solar"
3. Restart Home Assistant
4. The controller should auto-discover — or add manually by MAC address

## Protocol

### GP-PWM-30-SB (FFF0)
- **Service**: `0000FFF0-0000-1000-8000-00805F9B34FB`
- **Write** (`FFF2`): ASCII poll command (`0x20`) or settings commands
- **Notify** (`FFF1`): 32-field semicolon-delimited ASCII response
- **Pairing**: None required

### GP-PWM-30-UL (569a)
- **Service**: `569a1101-b87f-490c-92cb-11ba5ea5167c`
- **Write** (`569a2001`): ASCII space (`0x20`) poll command
- **Notify** (`569a2000`): 30-field semicolon-delimited ASCII response, terminated `\r\n`
- **Pairing**: LE Legacy Just Works (BlueZ handles automatically via local HCI)

## License

MIT
