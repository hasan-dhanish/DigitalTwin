#!/usr/bin/env python3
# ==============================================================================
# Host PC Digital Twin Dashboard & Gateway for QNX RTOS Engine
# Receives TCP snapshots (Port 8080) and UDP telemetry streams (Port 9999)
# and hosts the 2D City Digital Twin Dashboard on HTTP Port 8080.
# ==============================================================================

import socket
import json
import time
import threading
import sys
import os
import http.server
import socketserver

QNX_PI_IP = "127.0.0.1"
QNX_TCP_PORT = 8080
UDP_PORT = 9999
HTTP_PORT = 8080

if len(sys.argv) > 1:
    QNX_PI_IP = sys.argv[1]

# Global state cached from QNX Engine
g_telemetry = {
    "system_state": "NORMAL [OPTIMAL]",
    "sync_latency_ms": 0.08,
    "max_latency_ms": 1.2,
    "deadline_misses": 0,
    "jitter_ms": 0.0,
    "total_ticks": 1250,
    "stale_count": 0,
    "watchdog": "HEALTHY",
    "streams": {
        "traffic": {"val": 45.0, "unit": "km/h", "vehicle_count": 0, "beam_blocked": False, "freshness_ms": 12.4, "stale": False},
        "power":   {"val": 410.0, "unit": "MW",   "freshness_ms": 18.1, "stale": False},
        "water":   {"val": 65.0,  "unit": "PSI",  "freshness_ms": 22.5, "stale": False},
        "air":     {"val": 28.0,  "unit": "AQI",  "gas_alert": False, "alert_count": 0, "freshness_ms": 45.0, "stale": False},
        "environment": {"temperature": 24.5, "humidity": 55.0, "dht_ok": True, "freshness_ms": 50.0, "stale": False}
    }
}

# ------------------------------------------------------------------------------
# 1. UDP Receiver Listener (Port 9999)
# ------------------------------------------------------------------------------
def udp_receiver():
    global g_telemetry
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", UDP_PORT))
        print(f"[UDP RECEIVER] Listening for Pi QNX telemetry packets on UDP port {UDP_PORT}...")
    except Exception as e:
        print(f"[UDP NOTICE] Could not bind UDP port {UDP_PORT}: {e}")
        return

    pkt_count = 0
    while True:
        try:
            data, addr = sock.recvfrom(4096)
            payload = json.loads(data.decode('utf-8'))
            sid = payload.get("stream")
            pkt_count += 1

            if sid == "traffic":
                val = payload.get("value")
                if val is not None:
                    g_telemetry["streams"]["traffic"]["val"]           = float(val)
                    g_telemetry["streams"]["traffic"]["vehicle_count"] = payload.get("vehicle_count", 0)
                    g_telemetry["streams"]["traffic"]["beam_blocked"]  = payload.get("beam_blocked", False)
                    g_telemetry["streams"]["traffic"]["freshness_ms"]  = payload.get("freshness_ms", 5.0)
                    g_telemetry["streams"]["traffic"]["stale"]         = payload.get("stale", False)

            elif sid == "air":
                val = payload.get("value")
                if val is not None:
                    g_telemetry["streams"]["air"]["val"]          = float(val)
                    g_telemetry["streams"]["air"]["gas_alert"]    = payload.get("gas_alert", False)
                    g_telemetry["streams"]["air"]["alert_count"]  = payload.get("alert_count", 0)
                    g_telemetry["streams"]["air"]["freshness_ms"] = payload.get("freshness_ms", 10.0)
                    g_telemetry["streams"]["air"]["stale"]        = payload.get("stale", False)

            elif sid == "environment":
                t = payload.get("temperature")
                h = payload.get("humidity")
                if t is not None:
                    g_telemetry["streams"]["environment"]["temperature"] = float(t)
                if h is not None:
                    g_telemetry["streams"]["environment"]["humidity"]    = float(h)
                g_telemetry["streams"]["environment"]["dht_ok"]          = payload.get("dht_ok", False)
                g_telemetry["streams"]["environment"]["freshness_ms"]    = payload.get("freshness_ms", 20.0)
                g_telemetry["streams"]["environment"]["stale"]           = payload.get("stale", False)

            elif sid == "qnx_telemetry":
                g_telemetry["system_state"]    = payload.get("system_state", "NORMAL [OPTIMAL]")
                g_telemetry["sync_latency_ms"] = float(payload.get("sync_latency_ms", 0.08))
                g_telemetry["max_latency_ms"]  = float(payload.get("max_latency_ms", 1.2))
                g_telemetry["deadline_misses"] = int(payload.get("deadline_misses", 0))
                g_telemetry["jitter_ms"]       = float(payload.get("jitter_ms", 0.0))
                g_telemetry["total_ticks"]     = int(payload.get("total_ticks", 0))
                g_telemetry["stale_count"]     = int(payload.get("stale_count", 0))
                g_telemetry["watchdog"]        = payload.get("watchdog", "HEALTHY")

            # Live CLI Feedback every 10 packets (~0.5s)
            if pkt_count % 10 == 0:
                s = g_telemetry["streams"]
                t_str = f"{s['environment']['temperature']:.1f}C" if s['environment'].get('temperature') else "N/A"
                h_str = f"{s['environment']['humidity']:.1f}%" if s['environment'].get('humidity') else "N/A"
                v_cnt = s['traffic'].get('vehicle_count', 0)
                spd   = s['traffic'].get('val', 0.0)
                aqi   = s['air'].get('val', 0.0)
                gas   = "ALERT" if s['air'].get('gas_alert') else "Clean"
                lat   = g_telemetry["sync_latency_ms"]
                miss  = g_telemetry["deadline_misses"]
                state = g_telemetry["system_state"]

                sys.stdout.write(
                    f"\r[HOST RX @ {addr[0]}] "
                    f"Traffic: {spd:4.1f}km/h ({v_cnt:2d} veh) | "
                    f"Air: {aqi:3.0f} AQI ({gas}) | "
                    f"DHT: {t_str}, {h_str} | "
                    f"Latency: {lat:4.2f}ms Misses: {miss} | "
                    f"State: {state}   "
                )
                sys.stdout.flush()

        except Exception:
            pass

# ------------------------------------------------------------------------------
# 2. QNX TCP Polling Client Thread (Port 8080)
# ------------------------------------------------------------------------------
def qnx_tcp_poller():
    global g_telemetry
    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.8)
            s.connect((QNX_PI_IP, QNX_TCP_PORT))
            s.sendall(b"GET /api/telemetry HTTP/1.1\r\nHost: qnx\r\n\r\n")
            
            data = b""
            while True:
                chunk = s.recv(2048)
                if not chunk: break
                data += chunk
            s.close()

            body = data.decode('utf-8').split("\r\n\r\n")[-1]
            payload = json.loads(body)
            g_telemetry.update(payload)
        except Exception:
            pass
        time.sleep(0.05)

# ------------------------------------------------------------------------------
# 3. Web Server Request Handler (With Socket Error Protection)
# ------------------------------------------------------------------------------
class HostDashboardHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        try:
            if self.path == '/api/telemetry':
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps(g_telemetry).encode('utf-8'))

            elif self.path.startswith('/api/fault'):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                
                action = self.path.split("action=")[-1] if "action=" in self.path else "none"
                if action == "disconnect":
                    g_telemetry["streams"]["traffic"]["stale"] = True
                    g_telemetry["streams"]["traffic"]["freshness_ms"] = 623.5
                    g_telemetry["stale_count"] = 1
                    g_telemetry["system_state"] = "DEGRADED [STALE FAULT]"
                elif action == "reset":
                    g_telemetry["streams"]["traffic"]["stale"] = False
                    g_telemetry["streams"]["traffic"]["freshness_ms"] = 12.0
                    g_telemetry["stale_count"] = 0
                    g_telemetry["system_state"] = "NORMAL [OPTIMAL]"
                    
                self.wfile.write(json.dumps({"status": "ok", "action": action}).encode('utf-8'))
            else:
                super().do_GET()
        except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        pass

def main():
    print("==========================================================================")
    print("  QNX REAL-TIME CITY DIGITAL TWIN HOST PC DASHBOARD SERVER")
    print("==========================================================================")
    print(f" Target QNX Pi Address : TCP {QNX_PI_IP}:{QNX_TCP_PORT}")
    print(f" UDP Listener Active   : UDP 0.0.0.0:{UDP_PORT}")
    print(f" Web Dashboard Active  : http://localhost:{HTTP_PORT}/city_digital_twin_dashboard.html")
    print("==========================================================================")

    threading.Thread(target=udp_receiver, daemon=True).start()
    threading.Thread(target=qnx_tcp_poller, daemon=True).start()

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", HTTP_PORT), HostDashboardHandler) as httpd:
        print("\n[STREAMING] Serving Digital Twin Dashboard...")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[STOPPED] Host Server terminated cleanly.")

if __name__ == "__main__":
    main()
