# ==============================================================================
# QNX Real-Time Digital Twin - Web Server & Telemetry REST API
# Serves static 3D visualizer assets and provides /api/telemetry HTTP endpoint
# for real-time WebGL rendering on local browser or Raspberry Pi kiosk.
# ==============================================================================

import json
import os
import http.server
import socketserver
import threading

class TelemetryServer:
    """Embedded HTTP server bridging RTOS engine state to 3D visualizer."""
    def __init__(self, engine, port=8080):
        self.engine = engine
        self.port = port
        self.httpd = None
        self.thread = None

    def start(self):
        """Starts HTTP server in background thread."""
        engine_ref = self.engine

        class TelemetryHandler(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/api/telemetry':
                    self.send_response(200)
                    self.send_header('Content-type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()

                    state = engine_ref.get_telemetry_state()
                    self.wfile.write(json.dumps(state).encode('utf-8'))
                else:
                    super().do_GET()

            def log_message(self, format, *args):
                pass  # Suppress HTTP access logs from CLI terminal output

        web_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        os.chdir(web_dir)

        def serve():
            try:
                socketserver.TCPServer.allow_reuse_address = True
                with socketserver.TCPServer(("", self.port), TelemetryHandler) as httpd:
                    self.httpd = httpd
                    httpd.serve_forever()
            except Exception as e:
                print(f"[HTTP SERVER WARNING] Port {self.port} error: {e}")

        self.thread = threading.Thread(target=serve, daemon=True, name="HTTPServer-P50")
        self.thread.start()
        print(f"[WEB SERVER] Telemetry API & 3D Visualizer live at http://localhost:{self.port}/city_3d_visualizer.html")

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
