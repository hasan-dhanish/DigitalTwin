# ==============================================================================
# QNX Real-Time Digital Twin - Sensor Ingestion & Hardware Driver Threads
# Runs multi-rate acquisition threads for traffic, power, water, and AQI,
# supporting physical Raspberry Pi GPIO sensors with automatic simulation fallback.
# ==============================================================================

import time
import random
import math
import threading
import sys
import os

# Import local configuration and buffer modules
sys.path.append(os.path.dirname(__file__))
from 01_config import PRIORITY_SENSORS
from 02_buffers import RingBuffer

# Optional physical hardware integration
try:
    from real_hardware_sensors import RealHardwareSensorDriver
    HAS_HARDWARE_DRIVER = True
except ImportError:
    HAS_HARDWARE_DRIVER = False

class SensorAcquisitionWorker:
    """Manages multi-rate worker threads populating stream ring buffers."""
    def __init__(self, streams_config, ring_buffers):
        self.config = streams_config
        self.ring_buffers = ring_buffers
        self.running = False
        self.threads = []

        # Fault Injection Flags
        self.fault_traffic_disconnected = False
        self.fault_network_delay_ms = 0.0
        self.fault_cpu_overload = False

        # Optional Hardware Driver
        self.hw_driver = RealHardwareSensorDriver() if HAS_HARDWARE_DRIVER else None

    def start(self):
        """Launches dedicated POSIX-style ingestion thread for each sensor stream."""
        self.running = True
        for stream_id, info in self.config.items():
            t = threading.Thread(
                target=self._sensor_loop,
                args=(stream_id, info),
                daemon=True,
                name=f"SensorThread-{stream_id.upper()}"
            )
            self.threads.append(t)
            t.start()
        print(f"[SENSORS] Started {len(self.threads)} multi-rate acquisition threads (Priority {PRIORITY_SENSORS}).")

    def _sensor_loop(self, stream_id, info):
        """Individual rate-controlled ingestion loop."""
        rate_sec = info["rate_ms"] / 1000.0
        buf = self.ring_buffers[stream_id]
        step = 0

        while self.running:
            start_time = time.time()

            # 1. Fault check: Traffic sensor disconnection simulation
            if stream_id == "traffic" and self.fault_traffic_disconnected:
                time.sleep(rate_sec)
                continue  # Skip pushing samples to simulate connection loss

            # 2. Fault check: Network delay injection
            if self.fault_network_delay_ms > 0:
                time.sleep(self.fault_network_delay_ms / 1000.0)

            # 3. Read physical sensor OR simulate nominal sine-wave telemetry
            val = self._read_sensor_value(stream_id, info, step)
            step += 1

            # Push sample to ring buffer
            buf.push(val)

            # 4. Enforce precise multi-rate timing loop
            elapsed = time.time() - start_time
            sleep_time = max(0.001, rate_sec - elapsed)
            time.sleep(sleep_time)

    def _read_sensor_value(self, stream_id, info, step):
        """Reads physical sensor hardware or generates realistic noisy telemetry."""
        # Try real physical hardware drivers first
        if self.hw_driver:
            if stream_id == "traffic":
                dist_val = self.hw_driver.read_traffic_ultrasonic()
                if dist_val is not None:
                    return dist_val
            elif stream_id == "power":
                temp_val = self.hw_driver.read_system_cpu_temp()
                if temp_val is not None:
                    return temp_val

        # Simulation mode with realistic physical noise & oscillation
        low, high = info["nominal_range"]
        mid = (low + high) / 2.0
        amp = (high - low) / 3.0
        noise = random.uniform(-1.5, 1.5)

        if stream_id == "traffic":
            val = mid + amp * math.sin(step * 0.1) + noise * 2.0
        elif stream_id == "power":
            val = mid + amp * math.cos(step * 0.05) + noise * 3.0
        elif stream_id == "water":
            val = mid + amp * 0.5 * math.sin(step * 0.02) + noise * 0.5
        else: # air
            val = mid + amp * 0.3 * math.cos(step * 0.01) + noise * 0.8

        return round(max(low * 0.5, min(high * 1.5, val)), 1)

    def stop(self):
        """Signals worker threads to terminate."""
        self.running = False

if __name__ == "__main__":
    from 01_config import DEFAULT_STREAMS
    print("=== Testing Sensor Acquisition Threads ===")

    buffers = {sid: RingBuffer(sid, cfg["buffer_capacity"]) for sid, cfg in DEFAULT_STREAMS.items()}
    worker = SensorAcquisitionWorker(DEFAULT_STREAMS, buffers)
    worker.start()

    time.sleep(0.5)

    print("\nReading telemetry buffers after 500ms:")
    for sid, buf in buffers.items():
        latest = buf.peek_latest()
        print(f" Stream [{sid.upper()}]: count={buf.size()}, latest={latest}")

    worker.stop()
