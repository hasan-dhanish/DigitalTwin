import sys
import os
import time
import json
import threading
import http.server
import socketserver

# Add src to Python Path
sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))

from twin_engine import DigitalTwinEngine, render_cli
from hardware_gpio import HardwareGPIOController

g_engine = None



def start_http_server(port=8080):
    class TelemetryHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/api/telemetry':
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                if g_engine:
                    data = {
                        "system_state": g_engine.system_state,
                        "sync_latency_ms": g_engine.current_latency_ms,
                        "max_latency_ms": g_engine.max_latency_ms,
                        "deadline_misses": g_engine.deadline_miss_count,
                        "total_ticks": g_engine.total_ticks,
                        "snapshot": g_engine.twin_snapshot,
                        "stream_status": g_engine.stream_status,
                        "stream_freshness": g_engine.stream_freshness
                    }
                else:
                    data = {"status": "initializing"}
                self.wfile.write(json.dumps(data).encode('utf-8'))
            else:
                super().do_GET()

        def log_message(self, format, *args):
            pass  # Suppress HTTP access logs in CLI terminal

    web_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(web_dir)
    try:
        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("", port), TelemetryHTTPRequestHandler) as httpd:
            httpd.serve_forever()
    except Exception as e:
        print(f"[HTTP SERVER WARNING] Could not start web server on port {port}: {e}")


def main():
    global g_engine
    print("Initializing QNX City Digital Twin Synchronization Engine...")
    
    # Start HTTP Server thread on port 8080 for 3D Visualizer
    web_thread = threading.Thread(target=start_http_server, daemon=True)
    web_thread.start()

    config_path = os.path.join(os.path.dirname(__file__), 'config', 'system_config.json')
    engine = DigitalTwinEngine(config_path=config_path)
    g_engine = engine
    engine.start()

    # Start Hardware GPIO Button & LED Controller
    gpio_ctrl = HardwareGPIOController(engine)
    gpio_ctrl.start()

    print("Engine started. 3D Web Visualizer available at: http://10.86.181.19:8080/city_3d_visualizer.html")

    time.sleep(1)

    try:
        while True:
            render_cli(engine)
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n[STOPPING] Shutting down Digital Twin threads...")
        engine.stop()
        gpio_ctrl.cleanup()
        print("[SUCCESS] All RTOS threads & GPIO pins terminated safely.")


if __name__ == "__main__":
    main()

