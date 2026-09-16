#!/usr/bin/env python3
# ==============================================================================
# Raspberry Pi Physical Sensor Telemetry Publisher for QNX RTOS Engine
# Reads real physical sensors (HC-SR04 ultrasonic, Pi CPU thermal, GPIO switches)
# and streams live UDP telemetry packets to the QNX Sync Engine.
# ==============================================================================

import socket
import json
import time
import os
import sys

# Default Host (Change to your main QNX Engine PC IP address or localhost)
ENGINE_IP = "127.0.0.1" 
ENGINE_PORT = 9999

if len(sys.argv) > 1:
    ENGINE_IP = sys.argv[1]

# Try importing RPi.GPIO
try:
    import RPi.GPIO as GPIO
    HAS_GPIO = True
except ImportError:
    HAS_GPIO = False

print("==========================================================================")
print("  RASPBERRY PI HARDWARE SENSOR TELEMETRY PUBLISHER [QNX RTOS BRIDGE]")
print(f"  Target Engine Address: UDP {ENGINE_IP}:{ENGINE_PORT}")
print("==========================================================================")

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# GPIO Setup for HC-SR04 Ultrasonic (Trigger: 23, Echo: 24)
TRIG_PIN = 23
ECHO_PIN = 24

if HAS_GPIO:
    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(TRIG_PIN, GPIO.OUT)
        GPIO.setup(ECHO_PIN, GPIO.IN)
        GPIO.output(TRIG_PIN, False)
        print("[GPIO] Ultrasonic Distance Sensor initialized on Pins 23/24.")
    except Exception as e:
        print(f"[GPIO WARNING] {e}")

def read_ultrasonic():
    """Reads HC-SR04 distance sensor and maps to traffic speed (km/h)"""
    if not HAS_GPIO:
        return None
    try:
        GPIO.output(TRIG_PIN, True)
        time.sleep(0.00001)
        GPIO.output(TRIG_PIN, False)
        t_start = time.time()
        t_end = time.time()
        timeout = time.time() + 0.04
        while GPIO.input(ECHO_PIN) == 0:
            t_start = time.time()
            if t_start > timeout: return None
        while GPIO.input(ECHO_PIN) == 1:
            t_end = time.time()
            if t_end > timeout: return None
        dist_cm = ((t_end - t_start) * 34300) / 2
        speed_kmh = max(15.0, min(110.0, dist_cm * 1.5))
        return round(speed_kmh, 1)
    except Exception:
        return None

def read_pi_temp():
    """Reads actual Raspberry Pi CPU thermal zone to simulate Power Grid Load"""
    try:
        if os.path.exists("/sys/class/thermal/thermal_zone0/temp"):
            with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                temp_c = float(f.read().strip()) / 1000.0
                power_mw = 350.0 + ((temp_c - 35.0) / 35.0) * 120.0
                return round(max(300.0, min(520.0, power_mw)), 1)
    except Exception:
        pass
    return None

print("\n[STREAMING] Broadcasting physical sensor data... Press Ctrl+C to stop.")

try:
    while True:
        # 1. Read Traffic Ultrasonic Sensor (50ms rate)
        traffic_val = read_ultrasonic()
        if traffic_val is not None:
            pkt = json.dumps({"stream": "traffic", "value": traffic_val})
            sock.sendto(pkt.encode('utf-8'), (ENGINE_IP, ENGINE_PORT))
            print(f"[TX] Traffic Telemetry: {traffic_val} km/h")

        # 2. Read Pi CPU Temp as Power Grid Proxy (100ms rate)
        power_val = read_pi_temp()
        if power_val is not None:
            pkt = json.dumps({"stream": "power", "value": power_val})
            sock.sendto(pkt.encode('utf-8'), (ENGINE_IP, ENGINE_PORT))
            print(f"[TX] Power Grid Load: {power_val} MW")

        time.sleep(0.05)
except KeyboardInterrupt:
    print("\n[STOPPED] Telemetry publisher terminated.")
    if HAS_GPIO:
        GPIO.cleanup()
