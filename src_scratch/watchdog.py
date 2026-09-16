# ==============================================================================
# QNX Real-Time Digital Twin - Real-Time Watchdog Monitor (Priority Level 200)
# Scans multi-rate data streams every 50ms for staleness (>150ms latency cutoff).
# Triggers fail-safe state transitions and alerts upon hardware/network faults.
# ==============================================================================

import time
import threading
import sys
import os

sys.path.append(os.path.dirname(__file__))
from config import PRIORITY_WATCHDOG, WATCHDOG_CHECK_MS, STALE_TIMEOUT_MS

class WatchdogMonitor:
    """High-priority monitor detecting sensor dropouts & system degradation."""
    def __init__(self, sync_core):
        self.sync_core = sync_core
        self.running = False
        self.thread = None

        self.system_state = "NOMINAL"
        self.stream_status = {}
        self.stream_freshness = {}
        self._lock = threading.Lock()

    def start(self):
        """Spawns 50ms Watchdog thread."""
        self.running = True
        self.thread = threading.Thread(
            target=self._watchdog_loop,
            daemon=True,
            name="WatchdogMonitor-P200"
        )
        self.thread.start()
        print(f"[WATCHDOG] Launched 50ms Safety Monitor (Priority {PRIORITY_WATCHDOG}).")

    def _watchdog_loop(self):
        """Executes periodic staleness checks."""
        check_interval_sec = WATCHDOG_CHECK_MS / 1000.0

        while self.running:
            start_time = time.time()
            now_ms = start_time * 1000.0

            sync_state = self.sync_core.get_state()
            snapshot = sync_state.get("snapshot", {})

            has_fault = False
            statuses = {}
            freshness = {}

            for stream_id, data in snapshot.items():
                timestamp_ms = data.get("timestamp_ms", 0.0)
                if timestamp_ms > 0:
                    age_ms = round(now_ms - timestamp_ms, 1)
                else:
                    age_ms = 0.0

                freshness[stream_id] = age_ms

                # Multi-rate aware staleness threshold: rate_ms * 2.5 or STALE_TIMEOUT_MS
                nominal_rate = 50.0
                if hasattr(self.sync_core, 'config') and stream_id in self.sync_core.config:
                    nominal_rate = self.sync_core.config[stream_id]["rate_ms"]
                
                allowed_cutoff = max(STALE_TIMEOUT_MS, nominal_rate * 2.5)

                if timestamp_ms == 0:
                    statuses[stream_id] = "INITIALIZING"
                elif age_ms > allowed_cutoff:
                    statuses[stream_id] = "STALE_FAULT"
                    has_fault = True
                else:
                    statuses[stream_id] = "ONLINE"

            with self._lock:
                self.stream_status = statuses
                self.stream_freshness = freshness

                if has_fault:
                    self.system_state = "STALE_FAULT"
                elif sync_state.get("latency_ms", 0) > 15.0:
                    self.system_state = "DEGRADED"
                else:
                    self.system_state = "NOMINAL"

            elapsed = time.time() - start_time
            sleep_time = max(0.005, check_interval_sec - elapsed)
            time.sleep(sleep_time)

    def get_health(self):
        """Returns watchdog state summary."""
        with self._lock:
            return {
                "system_state": self.system_state,
                "stream_status": self.stream_status,
                "stream_freshness": self.stream_freshness
            }

    def stop(self):
        self.running = False

if __name__ == "__main__":
    from config import DEFAULT_STREAMS
    from buffers import RingBuffer
    from sensor_threads import SensorAcquisitionWorker
    from sync_core import SynchronizerCore

    print("=== Testing 50ms Watchdog Monitor ===")
    buffers = {sid: RingBuffer(sid, cfg["buffer_capacity"]) for sid, cfg in DEFAULT_STREAMS.items()}
    sensors = SensorAcquisitionWorker(DEFAULT_STREAMS, buffers)
    sensors.start()

    sync = SynchronizerCore(buffers)
    sync.start()

    watchdog = WatchdogMonitor(sync)
    watchdog.start()

    time.sleep(0.3)

    print("\nInitial Watchdog Health Check:")
    print(watchdog.get_health())

    # Simulate fault injection: disconnect traffic sensor
    print("\nSimulating Traffic Sensor Disconnection (Fault Injection)...")
    sensors.fault_traffic_disconnected = True

    time.sleep(0.3)

    print("Watchdog Health Check After Disconnection:")
    print(watchdog.get_health())

    watchdog.stop()
    sync.stop()
    sensors.stop()
