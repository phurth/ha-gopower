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
| Connected | ✓ | ✓ | Binary sensor |
| Data Healthy | ✓ | ✓ | Binary sensor |
| Model Number | ✓ | ✓ | Diagnostic |
| Firmware Version | ✓ | ✓ | Diagnostic |
| Serial Number | ✓ | — | Diagnostic; not transmitted by GP-PWM-30-UL. |
| Reboot Controller | ✓ | ✓ | Button |

### Note on Charge Power vs Solar Power

For a PWM controller the solar panel connects directly to the battery during the on-phase of the PWM cycle. The panel open-circuit voltage (~18–22 V) is higher than the battery voltage (~12–14 V); the voltage difference is dissipated as heat in the switching transistor. The energy actually stored in the battery is `battery_voltage × charge_current`, not `panel_voltage × charge_current`. Using panel voltage for power would overstate by roughly `Vpanel / Vbattery` (~30–60 %). Charge Power uses the battery-side calculation for accurate HA energy statistics.

## Energy figures

The integration reports **power, current and voltage**, not energy. For kWh — and for the
Energy dashboard — add a Riemann sum helper over the Charge Power sensor:

> Settings → Devices & Services → **Helpers** → **Create helper** →
> **Integration - Riemann sum integral sensor**

| Field | Value |
|-------|-------|
| Name | anything, e.g. `Solar Energy` |
| Input sensor | `sensor.<device>_solar_power` |
| Integration method | **Trapezoidal rule** (the default) |
| Metric prefix | **k** for kWh — leave blank for Wh |
| Time unit | **Hours** |
| Precision | 2 |

The resulting sensor carries `device_class: energy` and `state_class: total`, which is what
the Energy dashboard requires.

Two things worth knowing:

- **The entity ID is `..._solar_power`, not `..._charge_power`.** The sensor is *named*
  "Charge Power", but its ID predates that rename and was deliberately left alone so
  existing automations and statistics kept working.
- **Trapezoidal, not Left Riemann sum.** A left sum suits sources that hold a value between
  changes; charge power varies continuously and is sampled every few seconds, so averaging
  across each interval tracks it more closely.

The controller's own amp-hour counters are **not** exposed. They accumulate on the
controller's schedule — through the day on a GP-PWM-30-SB, across its whole service life on a
GP-PWM-30-UL — and neither reconciles with Home Assistant's statistics model, which expects
either an instantaneous measurement or a total it can attribute to a known period. Converting
them to Wh would be worse still: it needs a battery voltage that did not hold across the
window being summed.

They remain visible in the integration's diagnostics output for protocol debugging.

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

## Troubleshooting GP-PWM-30-UL pairing

The 569a variant bonds at the radio level, which an ESPHome Bluetooth proxy cannot
relay — it must connect through a Bluetooth adapter attached to the Home Assistant
host itself. If entities stay unavailable and the log repeats
`stale bond (AuthenticationFailed)`:

- **Check which source is being used.** `connecting via local HCI adapter …` means
  a direct adapter was found. A warning naming only proxy sources means the
  controller is out of range of the host's own adapter.
- **Set the adapter's scanning mode to Active** (Settings → Devices & Services →
  Bluetooth → Configure). On Linux the default "auto" mode resolves to passive
  scanning, which makes BlueZ slower to re-register a device after a bond is
  cleared.
- **Expect one or two rejections after a Home Assistant restart.** The controller
  rejects a reconnect whenever its bond no longer matches. The integration clears
  the BlueZ bond and immediately re-pairs, which normally recovers within a couple
  of seconds — look for `retrying at once while the clear is fresh` followed by
  `BLE Just Works pairing completed`.
- **If rejections continue,** close the Go Power Connect app (a phone holding the
  link keeps the controller busy) and power-cycle the controller.

## License

MIT
