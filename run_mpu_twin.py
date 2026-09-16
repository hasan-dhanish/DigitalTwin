#!/usr/bin/env python3
# ==============================================================================
# Host PC Receiver & 3D MPU6050 Orientation Server (UDP + MQTT Subscriber)
# Receives MPU sensor packets via UDP (Port 9998) or MQTT (broker.emqx.io)
# and hosts WebServer on Port 8090 with instant real-time 3D rendering.
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
MQTT_BROKER = "broker.emqx.io"
MQTT_TOPIC = "digitaltwin/mpu6050"

# Global latest pose state
g_pose = {
    "pitch": 0.0,
    "roll": 0.0,
    "yaw": 0.0,
    "accel": {"x": 0.0, "y": 0.0, "z": 1.0},
    "gyro": {"x": 0.0, "y": 0.0, "z": 0.0},
    "temp": 25.0,
    "mode": "WAITING_FOR_STREAM",
    "timestamp": 0,
    "packet_id": 0,
    "last_received": 0
}

# ------------------------------------------------------------------------------
# 1. UDP Listener Thread (Port 9998)
# ------------------------------------------------------------------------------
def udp_listener():
    global g_pose
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", UDP_PORT))
        print(f"[UDP LISTENER] Active on UDP port {UDP_PORT}")
    except Exception as e:
        print(f"[UDP NOTICE] Could not bind port {UDP_PORT}: {e}")
        return

    while True:
        try:
            data, addr = sock.recvfrom(2048)
            payload = json.loads(data.decode('utf-8'))
            payload["last_received"] = time.time()
            g_pose.update(payload)
        except Exception:
            pass

# ------------------------------------------------------------------------------
# 2. MQTT Subscriber Thread (broker.emqx.io)
# ------------------------------------------------------------------------------
def mqtt_listener():
    global g_pose
    try:
        import paho.mqtt.client as mqtt
        
        def on_connect(client, userdata, flags, rc, properties=None):
            print(f"[MQTT SUBSCRIBER] Connected to {MQTT_BROKER}! Subscribing to topic: {MQTT_TOPIC}")
            client.subscribe(MQTT_TOPIC)

        def on_message(client, userdata, msg):
            try:
                payload = json.loads(msg.payload.decode('utf-8'))
                payload["last_received"] = time.time()
                g_pose.update(payload)
            except Exception:
                pass

        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="HostPC_3DTwin_Sub")
        except AttributeError:
            client = mqtt.Client(client_id="HostPC_3DTwin_Sub")

        client.on_connect = on_connect
        client.on_message = on_message

        client.connect(MQTT_BROKER, 1883, 60)
        client.loop_forever()

    except Exception as e:
        print(f"[MQTT NOTICE] Could not start MQTT subscriber ({e})")

# ------------------------------------------------------------------------------
# 3. Web Server & SSE Stream Handler
# ------------------------------------------------------------------------------
class MPUTwinHTTPHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/mpu/stream':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

            try:
                while True:
                    now = time.time()
                    data = dict(g_pose)
                    data["freshness_ms"] = round((now - data["last_received"]) * 1000.0, 1) if data["last_received"] > 0 else 9999.0
                    data["online"] = data["freshness_ms"] < 1500.0

                    msg = f"data: {json.dumps(data)}\n\n"
                    self.wfile.write(msg.encode('utf-8'))
                    self.wfile.flush()
                    time.sleep(0.02)
            except Exception:
                pass

        elif self.path == '/api/mpu':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            
            now = time.time()
            data = dict(g_pose)
            data["freshness_ms"] = round((now - data["last_received"]) * 1000.0, 1) if data["last_received"] > 0 else 9999.0
            data["online"] = data["freshness_ms"] < 1500.0
            self.wfile.write(json.dumps(data).encode('utf-8'))
        else:
            super().do_GET()

    def log_message(self, format, *args):
        pass

def start_http_server():
    web_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(web_dir)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", HTTP_PORT), MPUTwinHTTPHandler) as httpd:
        print(f"[HTTP SERVER] Serving 3D Cuboid Visualizer at: http://localhost:{HTTP_PORT}/mpu_3d_visualizer.html")
        httpd.serve_forever()

def main():
    print("==========================================================================")
    print("  QNX MPU6050 LIVE 3D ORIENTATION SERVER [UDP + MQTT ACTIVE]")
    print("==========================================================================")

    # Start UDP listener
    threading.Thread(target=udp_listener, daemon=True).start()

    # Start MQTT subscriber (subscribes to broker.emqx.io)
    threading.Thread(target=mqtt_listener, daemon=True).start()

    # Start HTTP Web server
    threading.Thread(target=start_http_server, daemon=True).start()

    time.sleep(0.5)

    print("\n[READY] Server listening on UDP 9998 and MQTT topic: digitaltwin/mpu6050")
    print(f"👉 Open 3D Visualizer: http://localhost:{HTTP_PORT}/mpu_3d_visualizer.html\n")

    try:
        while True:
            now = time.time()
            pitch = g_pose.get("pitch", 0.0)
            roll  = g_pose.get("roll", 0.0)
            yaw   = g_pose.get("yaw", 0.0)
            mode  = g_pose.get("protocol", g_pose.get("mode", "OFFLINE"))
            pkts  = g_pose.get("packet_id", 0)
            last  = g_pose.get("last_received", 0)

            status = "MQTT STREAM LIVE" if (now - last) < 2.0 and last > 0 else "WAITING..."

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
