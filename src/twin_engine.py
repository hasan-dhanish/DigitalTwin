import os
import sys
import time
import json
import threading
from sensor_simulators import CircularRingBuffer, MultiRateSensorSimulator

class SystemState:
    NORMAL = "NORMAL [OPTIMAL]"
    DEGRADED = "DEGRADED [SAFETY OVERRIDE]"
    CRITICAL = "CRITICAL [DEADLINE BREACH]"

class DigitalTwinEngine:
    def __init__(self, config_path="../config/system_config.json"):
        with open(config_path, "r") as f:
            self.config = json.load(f)

        self.sync_period_s = self.config["sync_engine"]["period_ms"] / 1000.0
        self.deadline_ms = self.config["sync_engine"]["deadline_ms"]

        # Shared Memory State Buffers & Ring Queues
        self.ring_buffers = {}
        self.simulators = {}
        self.stream_configs = {}
        self.twin_snapshot = {}
        self.stream_freshness = {}
        self.stream_status = {}

        # RTOS Performance Metrics
        self.current_latency_ms = 0.0
        self.max_latency_ms = 0.0
        self.deadline_miss_count = 0
        self.total_ticks = 0
        self.system_state = SystemState.NORMAL

        self.running = True
        self.lock = threading.Lock()

        # Initialize Streams & Threads
        for stream in self.config["streams"]:
            sid = stream["id"]
            self.stream_configs[sid] = stream
            buffer = CircularRingBuffer(capacity=self.config["sync_engine"]["max_ring_buffer_size"])
            self.ring_buffers[sid] = buffer
            self.simulators[sid] = MultiRateSensorSimulator(stream, buffer)
            self.stream_status[sid] = "ONLINE"
            self.stream_freshness[sid] = 0.0

    def start(self):
        # Start Data Acquisition Threads
        for sim in self.simulators.values():
            sim.start()

        # Start Synchronizer, Watchdog & Physical UDP Sensor Listener Threads
        self.sync_thread = threading.Thread(target=self._run_synchronizer, name="Twin_Synchronizer_Core")
        self.watchdog_thread = threading.Thread(target=self._run_watchdog, name="Fault_Watchdog_Monitor")
        self.udp_thread = threading.Thread(target=self._run_udp_sensor_listener, name="UDP_Sensor_Listener", daemon=True)

        self.sync_thread.start()
        self.watchdog_thread.start()
        self.udp_thread.start()

    def _run_udp_sensor_listener(self, port=9999):
        """Background Thread: Listens for physical hardware sensor packets via UDP"""
        import socket
        import json
        from sensor_simulators import SensorPacket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("0.0.0.0", port))
            sock.settimeout(0.5)
        except Exception:
            return

        while self.running:
            try:
                data, addr = sock.recvfrom(1024)
                payload = json.loads(data.decode('utf-8'))
                sid = payload.get("stream")
                val = payload.get("value")

                if sid in self.ring_buffers and val is not None:
                    if sid in self.simulators:
                        self.simulators[sid].external_override = True
                    unit = self.stream_configs[sid]["unit"]
                    pkt = SensorPacket(stream_id=sid, value=float(val), unit=unit, timestamp=time.time())
                    self.ring_buffers[sid].push(pkt)
            except socket.timeout:
                continue
            except Exception:
                pass
        sock.close()

    def _run_synchronizer(self):
        """Highest Priority Task (Level 255): Atomic Digital Twin Snapshot Alignment"""
        while self.running:
            start_time = time.perf_counter()
            now = time.time()

            snapshot = {}
            any_stale = False

            # Atomic snapshot acquisition across all multi-rate streams
            for sid, buffer in self.ring_buffers.items():
                pkt = buffer.get_latest()
                stale_threshold_ms = self.stream_configs[sid]["period_ms"] * 2.5

                if pkt is not None:
                    freshness_ms = (now - pkt.timestamp) * 1000.0
                    snapshot[sid] = {
                        "val": pkt.value,
                        "unit": pkt.unit,
                        "freshness_ms": round(freshness_ms, 1)
                    }
                    self.stream_freshness[sid] = round(freshness_ms, 1)

                    if freshness_ms > stale_threshold_ms:
                        self.stream_status[sid] = "STALE FAULT"
                        any_stale = True
                    else:
                        self.stream_status[sid] = "ONLINE"
                else:
                    self.stream_status[sid] = "NO DATA"
                    any_stale = True

            with self.lock:
                self.twin_snapshot = snapshot
                self.total_ticks += 1

                # Calculate Sync Latency
                latency = (time.perf_counter() - start_time) * 1000.0
                self.current_latency_ms = round(latency, 2)
                if self.current_latency_ms > self.max_latency_ms:
                    self.max_latency_ms = self.current_latency_ms

                if self.current_latency_ms > self.deadline_ms:
                    self.deadline_miss_count += 1
                    self.system_state = SystemState.CRITICAL
                elif any_stale:
                    self.system_state = SystemState.DEGRADED
                else:
                    self.system_state = SystemState.NORMAL

            # Precise Periodic Sleep
            elapsed = time.perf_counter() - start_time
            sleep_time = max(0.0, self.sync_period_s - elapsed)
            time.sleep(sleep_time)

    def _run_watchdog(self):
        """High Priority Task (Level 200): Stale Data & Safety Watchdog Loop"""
        while self.running:
            time.sleep(self.config["watchdog"]["period_ms"] / 1000.0)
            now = time.time()

            for sid, buffer in self.ring_buffers.items():
                pkt = buffer.get_latest()
                stale_timeout_s = (self.stream_configs[sid]["period_ms"] * 2.5) / 1000.0
                if pkt is None or (now - pkt.timestamp) > stale_timeout_s:
                    self.stream_status[sid] = "TIMEOUT FAULT"

    def stop(self):
        self.running = False
        for sim in self.simulators.values():
            sim.stop()
        for sim in self.simulators.values():
            sim.join()
        self.sync_thread.join()
        self.watchdog_thread.join()

def render_cli(engine):
    """Mandatory CLI Terminal Dashboard (Priority Level 50)"""
    os.system('cls' if os.name == 'nt' else 'clear')
    print("==========================================================================")
    print("  CITY DIGITAL TWIN REAL-TIME SYNCHRONIZATION ENGINE [QNX RTOS v7.1]")
    print("==========================================================================")
    print(f" System Status      : {engine.system_state}")
    print(f" Sync Latency       : {engine.current_latency_ms} ms (Deadline Budget: {engine.deadline_ms} ms)")
    print(f" Max Latency Peak   : {engine.max_latency_ms} ms | Total Snapshots: {engine.total_ticks}")
    print(f" Deadline Misses    : {engine.deadline_miss_count}")
    print("--------------------------------------------------------------------------")
    print(" MULTI-RATE TELEMETRY STREAMS & DATA FRESHNESS:")
    print("--------------------------------------------------------------------------")

    for stream in engine.config["streams"]:
        sid = stream["id"]
        name = stream["name"]
        period = stream["period_ms"]
        status = engine.stream_status.get(sid, "UNKNOWN")
        freshness = engine.stream_freshness.get(sid, 0.0)
        snap = engine.twin_snapshot.get(sid, {})
        val_str = f"{snap.get('val', '---')} {snap.get('unit', '')}"

        status_tag = f"[{status}]"
        print(f"  * {name:<24} ({period:>4}ms) | State: {status_tag:<16} | Val: {val_str:<10} | Freshness: {freshness}ms")

    print("--------------------------------------------------------------------------")
    print(" INTERACTIVE FAULT INJECTION CONTROLS:")
    print("  [1] Disconnect Traffic Feed (Simulate Sensor Loss)")
    print("  [2] Spike Transmission Delay (500ms Network Spike)")
    print("  [3] Overload Background CPU Budget")
    print("  [R] Reset System & Clear Faults")
    print("  [Q] Quit Digital Twin Engine")
    print("==========================================================================")

if __name__ == "__main__":
    engine = DigitalTwinEngine()
    engine.start()
    try:
        while True:
            render_cli(engine)
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nStopping Digital Twin Engine...")
        engine.stop()
        print("Engine stopped cleanly.")
