#!/usr/bin/env python3
# ==============================================================================
# Host PC Digital Twin Dashboard & Gateway for QNX RTOS Engine
# Receives UDP telemetry streams on Port 9999
# and hosts the 2D City Digital Twin Dashboard on HTTP Port 8080.
# ==============================================================================

import socket
import json
import time
import threading
import sys
import os
import math
import http.server
import socketserver

HTTP_PORT = 8080
UDP_PORT  = 9999

# Global telemetry state updated by UDP listener
g_telemetry = {
    "system_state":    "AWAITING DATA",
    "sync_latency_ms": 0.0,
    "max_latency_ms":  0.0,
    "deadline_misses": 0,
    "total_ticks":     0,
    "stale_count":     0,
    "watchdog":        "HEALTHY",
    "streams": {
        "traffic": {"val": 0.0,   "unit": "km/h", "freshness_ms": 0.0, "stale": True},
        "power":   {"val": 0.0,   "unit": "MW",   "freshness_ms": 0.0, "stale": True},
        "water":   {"val": 0.0,   "unit": "PSI",  "freshness_ms": 0.0, "stale": True},
        "air":     {"val": 0.0,   "unit": "AQI",  "freshness_ms": 0.0, "stale": True},
    },
    "last_rx_time": 0,
    "connected":    False,
}

# Track when each stream was last updated for stale detection
_stream_last_rx = {k: 0.0 for k in ["traffic", "power", "water", "air"]}
_lock = threading.Lock()

# ------------------------------------------------------------------------------
# 1. UDP Receiver — the ONLY inbound path from the Pi
# ------------------------------------------------------------------------------
def udp_receiver():
    global g_telemetry, _stream_last_rx
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", UDP_PORT))
        print(f"[UDP RECEIVER] Listening for Pi telemetry packets on UDP port {UDP_PORT}...")
    except Exception as e:
        print(f"[UDP ERROR] Could not bind UDP port {UDP_PORT}: {e}")
        return

    while True:
        try:
            data, addr = sock.recvfrom(4096)
            now = time.time()
            payload = json.loads(data.decode('utf-8'))

            sid = payload.get("stream")
            val = payload.get("value")
            unit = payload.get("unit", "")

            with _lock:
                if sid in g_telemetry["streams"] and val is not None:
                    # Compute per-packet latency in ms (rough round-trip proxy)
                    prev_t = _stream_last_rx.get(sid, now)
                    freshness = round((now - prev_t) * 1000, 2) if prev_t > 0 else 0.0
                    _stream_last_rx[sid] = now

                    g_telemetry["streams"][sid]["val"]          = round(float(val), 2)
                    g_telemetry["streams"][sid]["freshness_ms"] = freshness
                    g_telemetry["streams"][sid]["stale"]        = False
                    if unit:
                        g_telemetry["streams"][sid]["unit"]     = unit

                    g_telemetry["last_rx_time"] = now
                    g_telemetry["connected"]    = True
                    g_telemetry["total_ticks"] += 1
                    g_telemetry["sync_latency_ms"] = freshness
                    if freshness > g_telemetry["max_latency_ms"]:
                        g_telemetry["max_latency_ms"] = freshness

        except Exception:
            pass

# ------------------------------------------------------------------------------
# 2. Stale-detection watchdog — marks streams stale if no packet in 1000 ms
# ------------------------------------------------------------------------------
def stale_watchdog():
    global g_telemetry, _stream_last_rx
    STALE_TIMEOUT = 1.0  # seconds
    while True:
        now = time.time()
        stale_n = 0
        with _lock:
            for sid in g_telemetry["streams"]:
                last = _stream_last_rx.get(sid, 0)
                is_stale = (now - last) > STALE_TIMEOUT if last > 0 else True
                g_telemetry["streams"][sid]["stale"] = is_stale
                if is_stale:
                    stale_n += 1
            g_telemetry["stale_count"] = stale_n
            if stale_n == 0:
                g_telemetry["system_state"] = "NORMAL [OPTIMAL]"
                g_telemetry["watchdog"]     = "HEALTHY"
            elif stale_n < 4:
                g_telemetry["system_state"] = f"DEGRADED [{stale_n} STALE]"
                g_telemetry["watchdog"]     = "WARNING"
            else:
                g_telemetry["system_state"] = "OFFLINE [NO DATA]"
                g_telemetry["watchdog"]     = "FAIL"
                g_telemetry["connected"]    = False
            g_telemetry["deadline_misses"] = stale_n
        time.sleep(0.1)

# ------------------------------------------------------------------------------
# 3. Web Server Handler
# ------------------------------------------------------------------------------
class HostDashboardHandler(http.server.SimpleHTTPRequestHandler):

    def do_GET(self):
        try:
            if self.path == '/api/telemetry':
                with _lock:
                    payload = json.dumps(g_telemetry)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(payload.encode('utf-8'))

            elif self.path.startswith('/api/fault'):
                action = self.path.split("action=")[-1] if "action=" in self.path else "none"
                with _lock:
                    if action == "disconnect":
                        g_telemetry["streams"]["traffic"]["stale"]        = True
                        g_telemetry["streams"]["traffic"]["freshness_ms"] = 999.9
                        g_telemetry["stale_count"]   = 1
                        g_telemetry["system_state"]  = "DEGRADED [STALE FAULT]"
                        g_telemetry["watchdog"]      = "WARNING"
                    elif action == "reset":
                        for sid in g_telemetry["streams"]:
                            g_telemetry["streams"][sid]["stale"] = False
                        g_telemetry["stale_count"]   = 0
                        g_telemetry["system_state"]  = "NORMAL [OPTIMAL]"
                        g_telemetry["watchdog"]      = "HEALTHY"
                        g_telemetry["deadline_misses"] = 0
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok", "action": action}).encode('utf-8'))
            else:
                super().do_GET()

        except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            pass

    def log_message(self, format, *args):
        pass  # suppress noisy access logs


def main():
    print("==========================================================================")
    print("  QNX REAL-TIME CITY DIGITAL TWIN HOST PC DASHBOARD SERVER")
    print("==========================================================================")
    print(f" UDP Telemetry Listener : UDP 0.0.0.0:{UDP_PORT}  (← Pi sends here)")
    print(f" Web Dashboard          : http://localhost:{HTTP_PORT}/city_digital_twin_dashboard.html")
    print(f" Telemetry API          : http://localhost:{HTTP_PORT}/api/telemetry")
    print("==========================================================================")

    threading.Thread(target=udp_receiver,  daemon=True).start()
    threading.Thread(target=stale_watchdog, daemon=True).start()

    socketserver.TCPServer.allow_reuse_address = True
    os.chdir(os.path.dirname(os.path.abspath(__file__)))  # serve files from project root
    with socketserver.TCPServer(("", HTTP_PORT), HostDashboardHandler) as httpd:
        print("\n[STREAMING] Serving Digital Twin Dashboard...")
        print(f"[WAITING]  Open browser → http://localhost:{HTTP_PORT}/city_digital_twin_dashboard.html")
        print("[WAITING]  Waiting for Pi UDP packets on port 9999...\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[STOPPED] Host Server terminated cleanly.")


if __name__ == "__main__":
    main()
