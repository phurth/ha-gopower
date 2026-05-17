"""Constants for the GoPower Solar BLE integration."""

DOMAIN = "ha_gopower"

# ---------------------------------------------------------------------------
# BLE Service & Characteristic UUIDs — GP-PWM (FFF0 protocol)
# ---------------------------------------------------------------------------
SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"

# ---------------------------------------------------------------------------
# BLE Service & Characteristic UUIDs — GP-SC (569a protocol)
# Advertises as GPPWM30BLE; uses a distinct vendor service.
# Requires LE Legacy Just Works BLE pairing (must connect via local HCI).
# ---------------------------------------------------------------------------
SC_SERVICE_UUID = "569a1101-b87f-490c-92cb-11ba5ea5167c"
SC_WRITE_CHAR_UUID = "569a2001-b87f-490c-92cb-11ba5ea5167c"
SC_NOTIFY_CHAR_UUID = "569a2000-b87f-490c-92cb-11ba5ea5167c"

# ---------------------------------------------------------------------------
# Device type — stored in config entry data
# ---------------------------------------------------------------------------
CONF_DEVICE_TYPE = "device_type"
DEVICE_TYPE_PWM = "PWM"   # GP-PWM family (FFF0 protocol, no pairing)
DEVICE_TYPE_SC = "SC"     # GP-SC family (569a protocol, Just Works pairing)

# ---------------------------------------------------------------------------
# Device name prefixes for BLE discovery
# ---------------------------------------------------------------------------
DEVICE_NAME_PREFIXES = ("GP-PWM", "GoPower", "GPPWM")

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------
POLL_COMMAND = b" "  # Single ASCII space (0x20)
FIELD_DELIMITER = ";"
EXPECTED_FIELD_COUNT = 32       # GP-PWM: 32 semicolon-delimited fields
SC_EXPECTED_FIELD_COUNT = 30    # GP-SC: 30 fields, response ends with \r\n

# ---------------------------------------------------------------------------
# Response field indices (semicolon-delimited ASCII)
# ---------------------------------------------------------------------------
FIELD_SOLAR_CURRENT = 0      # mA — divide by 1000 → A
FIELD_BATTERY_VOLTAGE = 2    # mV — divide by 1000 → V
FIELD_FIRMWARE = 8            # integer
FIELD_SOC = 10                # % (0–100)
FIELD_SOLAR_VOLTAGE = 11     # mV — divide by 1000 → V
FIELD_SERIAL = 14             # hex string → int → decimal string
FIELD_TEMP_C = 16             # signed int (e.g. "+06" or "-05")
FIELD_TEMP_F = 17             # signed int
FIELD_AMP_HOURS_TODAY = 19   # Ah×100 (fixed-point) — divide by 100 for Ah, then multiply by battery voltage → Wh; resets daily at midnight
FIELD_AMP_HOURS_YESTERDAY = 20  # Ah (not published in Android app)
FIELD_AMP_HOURS_WEEK = 24       # Ah (not published in Android app)

# ---------------------------------------------------------------------------
# GP-SC response field indices (30-field 569a protocol)
# Confirmed from HCI capture (BT_HCI_2026_0517_130124.cfa) and
# cross-referenced with SolarControllerDataStorage.updateD1Data().
# ---------------------------------------------------------------------------
SC_FIELD_BATTERY_CURRENT = 0   # raw ÷ 10 → A (charging current into battery)
SC_FIELD_FIRMWARE = 6          # firmware version string
SC_FIELD_BATTERY_VOLTAGE = 10  # mV → divide by 1000 → V
SC_FIELD_SOC = 12              # state of charge (%)
SC_FIELD_TEMP_C = 13           # signed temperature string e.g. "+23" or "-05"
SC_FIELD_AMP_HOURS = 28        # cumulative battery amp-hours (units unconfirmed)

# ---------------------------------------------------------------------------
# Command byte strings (ASCII)
# ---------------------------------------------------------------------------
UNLOCK_COMMAND = b"&G++0900"
REBOOT_COMMAND = b"&LDD0100"
FACTORY_RESET_COMMAND = b"&LDD0000"
RESET_HISTORY_COMMAND = b"&LDD0200"

# ---------------------------------------------------------------------------
# Timing (seconds)
# ---------------------------------------------------------------------------
POLL_INTERVAL = 4.0           # Status poll every 4 seconds
UNLOCK_DELAY = 0.2            # Wait after unlock before command
OPERATION_DELAY = 0.1         # Delay between BLE operations
SERVICE_DISCOVERY_DELAY = 0.2
RECONNECT_BACKOFF_BASE = 5.0
RECONNECT_BACKOFF_CAP = 120.0
STALE_TIMEOUT = 300.0         # 5 min without data → stale
WATCHDOG_INTERVAL = 60.0      # Connection health-check every 60s
