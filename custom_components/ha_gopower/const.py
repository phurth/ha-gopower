"""Constants for the GoPower Solar BLE integration."""

DOMAIN = "ha_gopower"

# ---------------------------------------------------------------------------
# BLE Service & Characteristic UUIDs (standard BT SIG base)
# ---------------------------------------------------------------------------
SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"

# ---------------------------------------------------------------------------
# Device name prefixes for BLE discovery
# ---------------------------------------------------------------------------
DEVICE_NAME_PREFIXES = ("GP-PWM", "GoPower")

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------
POLL_COMMAND = b" "  # Single ASCII space (0x20)
FIELD_DELIMITER = ";"
EXPECTED_FIELD_COUNT = 32

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
FIELD_AMP_HOURS_TODAY = 19   # Ah — multiply by battery voltage → Wh
FIELD_AMP_HOURS_YESTERDAY = 20  # Ah (not published in Android app)
FIELD_AMP_HOURS_WEEK = 24       # Ah (not published in Android app)

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
