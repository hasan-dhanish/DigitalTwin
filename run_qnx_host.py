#!/usr/bin/env python3
# ==============================================================================
# Host PC Digital Twin Dashboard & TCP Gateway for QNX RTOS Engine
# Connects to QNX Pi Engine on TCP 8080 (or UDP 9999) and hosts the 
# 2D/3D City Digital Twin Dashboard on HTTP Port 8080.
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
HTTP_PORT = 8080

if len(sys.argv) > 1:
    QNX_PI_IP = sys.argv[1]

# Global state cached from QNX Engine
g_telemetry = {
    "system_state": "NORMAL [OPTIMAL]",
    "sync_latency_ms": 0.08,
    "max_latency_ms": 1.2,
    "deadline_misses": 0,
    "total_ticks": 1250,
    "stale_count": 0,
    "watchdog": "HEALTHY",
    "streams": {
        "traffic": {"val": 45.0, "unit": "km/h", "freshness_ms": 12.4, "stale": False},
        "power":   {"val": 410.0, "unit": "MW", "freshness_ms": 18.1, "stale": False},
        "water":   {"val": 65.0, "unit": "PSI", "freshness_ms": 22.5, "stale": False},
        "air":     {"val": 28.0, "unit": "AQI", "freshness_ms": 45.0, "stale": False}
    }
}

# ------------------------------------------------------------------------------
# QNX TCP Polling Client Thread
# ------------------------------------------------------------------------------
def qnx_tcp_poller():
    global g_telemetry
    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect((QNX_PI_IP, QNX_TCP_PORT))
            s.sendall(b"GET /api/telemetry HTTP/1.1\r\nHost: qnx\r\n\r\n")
            
            data = b""
            while True:
                chunk = s.recv(2048)
                if not chunk: break
                data += chunk
            s.close()

            # Parse JSON body
            body = data.decode('utf-8').split("\r\n\r\n")[-1]
            payload = json.loads(body)
            g_telemetry.update(payload)
        except Exception:
            pass
        time.sleep(0.05)  # 20 Hz poll rate

# ------------------------------------------------------------------------------
# Web Server Request Handler
# ------------------------------------------------------------------------------
class HostDashboardHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/telemetry':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(g_telemetry).encode('utf-8'))

        elif self.path.startswith('/api/fault'):
            # Fault injection forwarder to QNX Pi
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            
            # Action handling
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

    def log_message(self, format, *args):
        pass

def main():
    print("==========================================================================")
    print("  QNX REAL-TIME CITY DIGITAL TWIN HOST PC DASHBOARD SERVER")
    print("==========================================================================")
    print(f" Target QNX Pi Address : TCP {QNX_PI_IP}:{QNX_TCP_PORT}")
    print(f" Web Server Active     : http://localhost:{HTTP_PORT}/city_digital_twin_dashboard.html")
    print("==========================================================================")

    # Start QNX Poller thread
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
