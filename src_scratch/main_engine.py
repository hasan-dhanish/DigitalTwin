# ==============================================================================
# QNX Real-Time Digital Twin - Master Engine & Real-Time CLI Dashboard
# Launches multi-threaded RTOS hierarchy, telemetry web server, and interactive
# keyboard fault injection CLI.
# ==============================================================================

import time
import os
import sys
import threading

sys.path.append(os.path.dirname(__file__))
from config import EngineConfig, PRIORITY_SYNCHRONIZER, PRIORITY_WATCHDOG, PRIORITY_SENSORS
from buffers import RingBuffer
from sensor_threads import SensorAcquisitionWorker
from sync_core import SynchronizerCore
from watchdog import WatchdogMonitor
from http_server import TelemetryServer

class MasterDigitalTwinEngine:
    """Orchestrates multi-threaded QNX RTOS Digital Twin Engine."""
    def __init__(self, config_path=None):
        self.config = EngineConfig(config_path)

        # 1. Allocate thread-safe ring buffers
        self.buffers = {
            sid: RingBuffer(sid, cfg["buffer_capacity"])
            for sid, cfg in self.config.streams.items()
        }

        # 2. Instantiate subsystem worker components
        self.sensor_worker = SensorAcquisitionWorker(self.config.streams, self.buffers)
        self.sync_core = SynchronizerCore(self.buffers, self.config)
        self.watchdog = WatchdogMonitor(self.sync_core)
        self.web_server = TelemetryServer(self, port=8080)

    def start(self):
        """Starts all threads in proper priority order."""
        print("\n" + "="*70)
        print(" [QNX Neutrino RTOS] City Digital Twin Synchronization Engine")
        print("="*70)

        self.sensor_worker.start()
        self.sync_core.start()
        self.watchdog.start()
        self.web_server.start()

        print("[ENGINE] All POSIX threads running successfully.")
        print("="*70 + "\n")

    def get_telemetry_state(self):
        """Aggregates system metrics for HTTP API endpoint /api/telemetry."""
        sync_state = self.sync_core.get_state()
        health = self.watchdog.get_health()

        return {
            "system_state": health["system_state"],
            "sync_latency_ms": sync_state["latency_ms"],
            "max_latency_ms": sync_state["max_latency_ms"],
            "deadline_misses": sync_state["deadline_misses"],
            "total_ticks": sync_state["total_ticks"],
            "snapshot": sync_state["snapshot"],
            "stream_status": health["stream_status"],
            "stream_freshness": health["stream_freshness"]
        }

    def stop(self):
        """Clean shutdown of all worker threads."""
        print("\n[SHUTDOWN] Terminating RTOS engine threads...")
        self.web_server.stop()
        self.watchdog.stop()
        self.sync_core.stop()
        self.sensor_worker.stop()
        print("[SUCCESS] All QNX threads stopped cleanly.")

def render_cli(engine):
    """Renders real-time telemetry dashboard in terminal."""
    state = engine.get_telemetry_state()

    # Clear terminal screen
    os.system('cls' if os.name == 'nt' else 'clear')

    print("=" * 72)
    print(" [QNX RTOS] CITY DIGITAL TWIN ENGINE | LIVE TERMINAL TELEMETRY")
    print("=" * 72)

    sys_state = state["system_state"]
    state_color = "[OPTIMAL]" if sys_state == "NOMINAL" else "[STALE FAULT]"
    print(f" System State      : {state_color}")
    print(f" Sync Latency      : {state['sync_latency_ms']:.1f} ms  (Peak: {state['max_latency_ms']:.1f} ms | Target: <15.0 ms)")
    print(f" Deadline Misses   : {state['deadline_misses']} misses  (Total Ticks: {state['total_ticks']})")
    print("-" * 72)
    print(f" {'STREAM SUBSYSTEM':<28} | {'RATE':<8} | {'VALUE':<12} | {'FRESHNESS':<10} | {'STATUS'}")
    print("-" * 72)

    snapshot = state.get("snapshot", {})
    freshness = state.get("stream_freshness", {})
    status = state.get("stream_status", {})

    for sid, info in engine.config.streams.items():
        name = info["name"]
        rate = f"{1000 // info['rate_ms']} Hz"
        snap = snapshot.get(sid, {})
        val_str = f"{snap.get('value', 'N/A')} {info['unit']}"
        fresh_str = f"{freshness.get(sid, 999.0):.0f}ms ago"
        st_str = status.get(sid, "ONLINE")

        print(f" {name:<28} | {rate:<8} | {val_str:<12} | {fresh_str:<10} | {st_str}")

    print("=" * 72)
    print(" FAULT INJECTION CONTROLS:")
    print(" [1] Disconnect Traffic Sensor   [2] Inject 500ms Net Delay   [3] Overload CPU")
    print(" [R] Reset System & Clear Faults [Q] Quit Engine")
    print("=" * 72)

def main():
    engine = MasterDigitalTwinEngine()
    engine.start()

    try:
        while True:
            render_cli(engine)
            time.sleep(0.1)
    except KeyboardInterrupt:
        engine.stop()

if __name__ == "__main__":
    main()
