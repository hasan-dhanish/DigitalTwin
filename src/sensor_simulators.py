import time
import random
import threading
from collections import deque

class SensorPacket:
    def __init__(self, stream_id, value, unit, timestamp):
        self.stream_id = stream_id
        self.value = value
        self.unit = unit
        self.timestamp = timestamp

class CircularRingBuffer:
    """Thread-safe lock-free style atomic ring buffer for RTOS telemetry."""
    def __init__(self, capacity=256):
        self.capacity = capacity
        self.buffer = deque(maxlen=capacity)
        self.lock = threading.Lock()

    def push(self, packet):
        with self.lock:
            self.buffer.append(packet)

    def get_latest(self):
        with self.lock:
            return self.buffer[-1] if len(self.buffer) > 0 else None

    def size(self):
        with self.lock:
            return len(self.buffer)

class MultiRateSensorSimulator(threading.Thread):
    def __init__(self, stream_config, ring_buffer):
        super().__init__()
        self.stream_id = stream_config["id"]
        self.name_label = stream_config["name"]
        self.period_s = stream_config["period_ms"] / 1000.0
        self.priority = stream_config["priority_level"]
        self.unit = stream_config["unit"]
        self.range = stream_config["nominal_range"]
        self.ring_buffer = ring_buffer
        self.running = True
        self.fault_active = False
        self.delay_spike = 0.0
        self.external_override = False

    def inject_fault(self, active=True):
        self.fault_active = active

    def inject_delay(self, extra_delay_s=0.5):
        self.delay_spike = extra_delay_s

    def run(self):
        from real_hardware_sensors import RealHardwareSensorDriver
        hw_driver = RealHardwareSensorDriver()

        while self.running:
            start_time = time.perf_counter()

            if self.external_override:
                time.sleep(self.period_s)
                continue

            if self.delay_spike > 0:
                time.sleep(self.delay_spike)
                self.delay_spike = 0.0

            if not self.fault_active:
                val = None
                # Try reading real physical hardware sensor first
                if self.stream_id == "traffic":
                    val = hw_driver.read_traffic_ultrasonic()
                elif self.stream_id == "power":
                    val = hw_driver.read_system_cpu_temp()

                # Fallback to nominal range generator if physical sensor unattached
                if val is None:
                    val = round(random.uniform(self.range[0], self.range[1]), 2)

                packet = SensorPacket(
                    stream_id=self.stream_id,
                    value=val,
                    unit=self.unit,
                    timestamp=time.time()
                )
                self.ring_buffer.push(packet)

            # Precise multi-rate periodic sleep
            elapsed = time.perf_counter() - start_time
            sleep_time = max(0.0, self.period_s - elapsed)
            time.sleep(sleep_time)


    def stop(self):
        self.running = False
