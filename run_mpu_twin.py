#!/usr/bin/env python3
# ==============================================================================
# Host PC Receiver & 3D MPU6050 Orientation Server
# Listens on UDP Port 9998 for QNX Pi MPU sensor packets and hosts a web server
# on Port 8090 with real-time 3D Cuboid rendering & live orientation telemetry.
# ==============================================================================

import socket
import json
import time
import threading
import sys
import os
import http.server
import socketserver

UDP_PORT = 9998
HTTP_PORT = 8090

# Global latest pose state
g_pose = {
    "pitch": 0.0,
    "roll": 0.0,
    "yaw": 0.0,
    "accel": {"x": 0.0, "y": 0.0, "z": 1.0},
    "gyro": {"x": 0.0, "y": 0.0, "z": 0.0},
    "temp": 25.0,
    "mode": "WAITING_FOR_QNX",
    "timestamp": 0,
    "packet_id": 0,
    "last_received": 0
}

# ------------------------------------------------------------------------------
# UDP Listener Thread
# ------------------------------------------------------------------------------
def udp_listener():
    global g_pose
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", UDP_PORT))
    print(f"[UDP LISTENER] Listening for QNX MPU telemetry on UDP port {UDP_PORT}...")

    while True:
        try:
            data, addr = sock.recvfrom(2048)
            payload = json.loads(data.decode('utf-8'))
            payload["last_received"] = time.time()
            g_pose.update(payload)
        except Exception as e:
            pass

# ------------------------------------------------------------------------------
# Web Server & Telemetry API Handler
# ------------------------------------------------------------------------------
class MPUTwinHTTPHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/mpu':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            
            # Add online freshness calculation
            now = time.time()
            data = dict(g_pose)
            if data["last_received"] > 0:
                data["freshness_ms"] = round((now - data["last_received"]) * 1000.0, 1)
                data["online"] = data["freshness_ms"] < 1000.0
            else:
                data["freshness_ms"] = 9999.0
                data["online"] = False

            self.wfile.write(json.dumps(data).encode('utf-8'))
        else:
            super().do_GET()

    def log_message(self, format, *args):
        pass  # Suppress HTTP access logs to keep CLI clean

def start_http_server():
    web_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(web_dir)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", HTTP_PORT), MPUTwinHTTPHandler) as httpd:
        print(f"[HTTP SERVER] Serving 3D Cuboid Visualizer at: http://localhost:{HTTP_PORT}/mpu_3d_visualizer.html")
        httpd.serve_forever()

# ------------------------------------------------------------------------------
# Main Entry Point
# ------------------------------------------------------------------------------
def main():
    print("==========================================================================")
    print("  QNX MPU6050 LIVE 3D ORIENTATION & MOTION TWIN SERVER [HOST PC]")
    print("==========================================================================")

    # Start UDP receiver thread
    udp_thread = threading.Thread(target=udp_listener, daemon=True)
    udp_thread.start()

    # Start HTTP Web server thread
    web_thread = threading.Thread(target=start_http_server, daemon=True)
    web_thread.start()

    time.sleep(0.5)

    print("\n[READY] Server running! Launching CLI monitor...")
    print(f"👉 Open 3D Visualizer: http://localhost:{HTTP_PORT}/mpu_3d_visualizer.html\n")

    try:
        while True:
            now = time.time()
            pitch = g_pose.get("pitch", 0.0)
            roll  = g_pose.get("roll", 0.0)
            yaw   = g_pose.get("yaw", 0.0)
            mode  = g_pose.get("mode", "OFFLINE")
            pkts  = g_pose.get("packet_id", 0)
            last  = g_pose.get("last_received", 0)

            status = "ONLINE" if (now - last) < 1.0 and last > 0 else "WAITING FOR QNX..."

            sys.stdout.write(
                f"\r[HOST TELEMETRY] Status: {status:<18} | Pkts: {pkts:06d} | "
                f"Pitch: {pitch:6.2f}° | Roll: {roll:6.2f}° | Yaw: {yaw:6.2f}° | Source: {mode}"
            )
            sys.stdout.flush()
            time.sleep(0.05)

    except KeyboardInterrupt:
        print("\n\n[SHUTDOWN] Host 3D Twin Server stopped cleanly.")

if __name__ == "__main__":
    main()
