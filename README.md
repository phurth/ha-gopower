# GoPower Solar — Home Assistant HACS Integration

Native BLE integration for **Go Power! PWM solar charge controllers** (GP-PWM series).

Connects directly to the controller over Bluetooth Low Energy — no cloud, no MQTT bridge, no internet required.

> **Disclaimer:** This is an independent community integration and is not affiliated with, endorsed by, or supported by Go Power! or any of its affiliates. Use it at your own risk.

## Features

- **Auto-discovery** via BLE advertisements (service UUID `FFF0` or name prefix `GP-PWM` / `GoPower`)
- **Real-time solar monitoring**: voltage, current, power, battery SOC, temperature, energy
- **Controller commands**: reboot, reset history counters
- **Diagnostics**: connection health, firmware version, serial number, raw field dump

## Entities

| Entity | Type | Device Class | Unit |
|--------|------|-------------|------|
| Solar Voltage | Sensor | voltage | V |
| Solar Current | Sensor | current | A |
| Solar Power | Sensor | power | W |
| Battery Voltage | Sensor | voltage | V |
| State of Charge | Sensor | battery | % |
| Temperature | Sensor | temperature | °C |
| Energy Today | Sensor | energy | Wh |
| Connected | Binary Sensor | connectivity | — |
| Data Healthy | Binary Sensor | problem | — |
| Model Number | Sensor (diag) | — | — |
| Firmware Version | Sensor (diag) | — | — |
| Serial Number | Sensor (diag) | — | — |
| Reboot Controller | Button | — | — |
| Reset History | Button | — | — |

## Requirements

- Home Assistant 2024.1+ with Bluetooth integration
- Bluetooth adapter on the HA host (or ESPHome BT proxy)
- GoPower GP-PWM solar controller within BLE range

## Installation (HACS)

1. Add this repository as a custom HACS repository
2. Install "GoPower Solar"
3. Restart Home Assistant
4. The controller should auto-discover — or add manually by MAC address

## Protocol

The integration communicates via BLE GATT:
- **Service**: `0000FFF0-0000-1000-8000-00805F9B34FB`
- **Write** (`FFF2`): Send ASCII poll command (`0x20`) or settings commands
- **Notify** (`FFF1`): Receive ASCII semicolon-delimited status response (32 fields)
- **Polling**: Every 4 seconds

No authentication or pairing required.

## License

MIT
