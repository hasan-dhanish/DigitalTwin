# ==============================================================================
# QNX Real-Time Digital Twin - System Configuration & RTOS Parameters
# Defines thread priority hierarchy, multi-rate ingestion frequencies,
# ring buffer capacities, and strict real-time deadline thresholds.
# ==============================================================================

import json
import os

# Priority Hierarchy (POSIX / QNX Style: 1 to 255)
PRIORITY_SYNCHRONIZER = 255  # Highest: Enforces atomic 10ms snapshots
PRIORITY_WATCHDOG     = 200  # High: Scans for stale streams (>150ms lag)
PRIORITY_SENSORS      = 150  # Medium: Data ingestion threads
PRIORITY_DASHBOARD    = 50   # Low: CLI & Web server rendering

# Real-Time Thresholds
SYNC_TICK_INTERVAL_MS = 10.0   # 10ms synchronization period
SYNC_LATENCY_BUDGET_MS = 15.0   # Maximum allowed sync latency limit
WATCHDOG_CHECK_MS      = 50.0   # Watchdog execution frequency
STALE_TIMEOUT_MS       = 150.0  # Telemetry staleness cutoff threshold

# Multi-Rate Sensor Definitions
DEFAULT_STREAMS = {
    "traffic": {
        "name": "Traffic Telemetry Subsystem",
        "rate_ms": 50,          # 10 Hz
        "buffer_capacity": 64,
        "unit": "km/h",
        "nominal_range": (30.0, 90.0)
    },
    "power": {
        "name": "Smart Power Grid Substation",
        "rate_ms": 100,         # 5 Hz
        "buffer_capacity": 32,
        "unit": "MW",
        "nominal_range": (350.0, 480.0)
    },
    "water": {
        "name": "Water Pressure Reservoir",
        "rate_ms": 500,         # 2 Hz
        "buffer_capacity": 16,
        "unit": "PSI",
        "nominal_range": (55.0, 75.0)
    },
    "air": {
        "name": "Environmental AQI Station",
        "rate_ms": 1000,        # 1 Hz
        "buffer_capacity": 16,
        "unit": "AQI",
        "nominal_range": (20.0, 50.0)
    }
}

class EngineConfig:
    """Loads and encapsulates QNX RTOS engine parameters."""
    def __init__(self, config_path=None):
        self.streams = DEFAULT_STREAMS
        self.sync_interval_ms = SYNC_TICK_INTERVAL_MS
        self.latency_budget_ms = SYNC_LATENCY_BUDGET_MS
        self.watchdog_check_ms = WATCHDOG_CHECK_MS
        self.stale_timeout_ms = STALE_TIMEOUT_MS

        if config_path and os.path.exists(config_path):
            self.load_from_file(config_path)

    def load_from_file(self, config_path):
        """Loads configuration from JSON file if available."""
        try:
            with open(config_path, 'r') as f:
                data = json.load(f)
                if "streams" in data:
                    self.streams = data["streams"]
                if "sync_interval_ms" in data:
                    self.sync_interval_ms = data["sync_interval_ms"]
                if "stale_timeout_ms" in data:
                    self.stale_timeout_ms = data["stale_timeout_ms"]
                print(f"[CONFIG] Loaded RTOS config successfully from {config_path}")
        except Exception as e:
            print(f"[CONFIG WARNING] Could not load JSON config ({e}), using default RTOS specs.")

if __name__ == "__main__":
    cfg = EngineConfig()
    print("=== QNX RTOS Engine Configuration ===")
    print(f"Sync Core Tick: {cfg.sync_interval_ms} ms (Priority {PRIORITY_SYNCHRONIZER})")
    print(f"Watchdog Interval: {cfg.watchdog_check_ms} ms (Priority {PRIORITY_WATCHDOG})")
    print(f"Stale Timeout Cutoff: {cfg.stale_timeout_ms} ms")
    print("Multi-rate Ingestion Streams:")
    for sid, info in cfg.streams.items():
        print(f" - [{sid.upper()}] {info['name']}: {info['rate_ms']}ms rate ({1000//info['rate_ms']} Hz)")
