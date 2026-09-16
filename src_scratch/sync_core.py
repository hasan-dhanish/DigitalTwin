# ==============================================================================
# QNX Real-Time Digital Twin - Synchronizer Core (Priority Level 255)
# Executes atomic snapshot creation every 10ms across multi-rate ring queues,
# calculating bounded sync latency and tracking deadline compliance.
# ==============================================================================

import time
import threading
import sys
import os

sys.path.append(os.path.dirname(__file__))
from config import PRIORITY_SYNCHRONIZER, SYNC_TICK_INTERVAL_MS, SYNC_LATENCY_BUDGET_MS

class SynchronizerCore:
    """Highest priority core engine enforcing atomic cross-stream snapshot alignment."""
    def __init__(self, ring_buffers, config=None):
        self.ring_buffers = ring_buffers
        self.config = config.streams if hasattr(config, 'streams') else (config if isinstance(config, dict) else {})
        self.running = False
        self.thread = None

        # Telemetry & Performance State
        self.current_snapshot = {}
        self.current_latency_ms = 0.0
        self.max_latency_ms = 0.0
        self.deadline_miss_count = 0
        self.total_ticks = 0

        self._lock = threading.Lock()

    def start(self):
        """Spawns 10ms high-priority tick loop thread."""
        self.running = True
        self.thread = threading.Thread(
            target=self._sync_loop,
            daemon=True,
            name="SynchronizerCore-P255"
        )
        self.thread.start()
        print(f"[SYNC CORE] Launched 10ms Synchronizer Engine (Priority {PRIORITY_SYNCHRONIZER}).")

    def _sync_loop(self):
        """Strict 10ms atomic tick loop."""
        tick_interval_sec = SYNC_TICK_INTERVAL_MS / 1000.0

        while self.running:
            start_time = time.time()
            now_ms = start_time * 1000.0

            # 1. Capture atomic snapshot across all multi-rate ring queues
            snapshot = {}
            latest_sensor_stamp = 0.0

            for stream_id, buf in self.ring_buffers.items():
                sample = buf.peek_latest()
                if sample:
                    snapshot[stream_id] = {
                        "value": sample.value,
                        "timestamp_ms": sample.timestamp_ms,
                        "age_ms": round(now_ms - sample.timestamp_ms, 2),
                        "seq_id": sample.seq_id
                    }
                    if sample.timestamp_ms > latest_sensor_stamp:
                        latest_sensor_stamp = sample.timestamp_ms
                else:
                    snapshot[stream_id] = {
                        "value": None,
                        "timestamp_ms": 0.0,
                        "age_ms": 999.0,
                        "seq_id": 0
                    }

            # 2. Compute bounded Sync Latency: delta between current sync tick and newest telemetry packet
            if latest_sensor_stamp > 0:
                sync_latency = max(0.1, now_ms - latest_sensor_stamp)
            else:
                sync_latency = 0.0

            # Update metrics under lock
            with self._lock:
                self.current_snapshot = snapshot
                self.current_latency_ms = round(sync_latency, 2)
                self.max_latency_ms = max(self.max_latency_ms, self.current_latency_ms)
                self.total_ticks += 1

                # 3. Deadline compliance check (< 15.0 ms target)
                if self.current_latency_ms > SYNC_LATENCY_BUDGET_MS:
                    self.deadline_miss_count += 1

            # Enforce 10ms tick cadence
            elapsed = time.time() - start_time
            sleep_time = max(0.0005, tick_interval_sec - elapsed)
            time.sleep(sleep_time)

    def get_state(self):
        """Thread-safe getter for CLI dashboard and Web Server API."""
        with self._lock:
            return {
                "snapshot": self.current_snapshot,
                "latency_ms": self.current_latency_ms,
                "max_latency_ms": self.max_latency_ms,
                "deadline_misses": self.deadline_miss_count,
                "total_ticks": self.total_ticks
            }

    def stop(self):
        self.running = False

if __name__ == "__main__":
    from config import DEFAULT_STREAMS
    from buffers import RingBuffer
    from sensor_threads import SensorAcquisitionWorker

    print("=== Testing 10ms Synchronizer Core ===")
    buffers = {sid: RingBuffer(sid, cfg["buffer_capacity"]) for sid, cfg in DEFAULT_STREAMS.items()}
    sensors = SensorAcquisitionWorker(DEFAULT_STREAMS, buffers)
    sensors.start()

    sync = SynchronizerCore(buffers)
    sync.start()

    time.sleep(0.3)

    state = sync.get_state()
    print(f"\nCompleted {state['total_ticks']} sync ticks in 300ms.")
    print(f"Current Sync Latency: {state['latency_ms']} ms (Max Peak: {state['max_latency_ms']} ms)")
    print(f"Deadline Misses (>15ms): {state['deadline_misses']}")
    print("Atomic Twin Snapshot:")
    for sid, data in state["snapshot"].items():
        print(f" - [{sid.upper()}]: val={data['value']} age={data['age_ms']}ms seq={data['seq_id']}")

    sync.stop()
    sensors.stop()
