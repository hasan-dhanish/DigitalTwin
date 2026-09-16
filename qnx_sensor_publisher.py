#!/usr/bin/env python3
# ==============================================================================
# QNX RTOS Telemetry Publisher Engine for Raspberry Pi
# Reads physical sensors (MPU6050 I2C, CPU Thermal, HC-SR04 Ultrasonic)
# and streams low-latency UDP JSON telemetry to the Host PC Digital Twin.
# ==============================================================================

import socket
import json
import time
import sys
import os
import math
import threading

# Default settings
HOST_PC_IP = "127.0.0.1"  # Will be overridden by command line arg
HOST_PC_PORT = 9999

if len(sys.argv) > 1:
    HOST_PC_IP = sys.argv[1]

# ------------------------------------------------------------------------------
# Sensor Driver Initialization
# ------------------------------------------------------------------------------

# 1. MPU6050 I2C Driver Check
HAS_MPU = False
mpu_sensor = None
try:
    from mpu6050 import mpu6050
    mpu_sensor = mpu6050(0x68, bus=1)
    HAS_MPU = True
    print("[HARDWARE] MPU6050 6-DOF Sensor detected on /dev/i2c1 (0x68).")
except Exception as e:
    print(f"[HARDWARE NOTICE] MPU6050 not initialized ({e}). Will fallback to CPU/Synthetic.")

# 2. RPi.GPIO Check (HC-SR04 Ultrasonic)
HAS_GPIO = False
TRIG_PIN = 23
ECHO_PIN = 24
try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(TRIG_PIN, GPIO.OUT)
    GPIO.setup(ECHO_PIN, GPIO.IN)
    GPIO.output(TRIG_PIN, False)
    HAS_GPIO = True
    print("[HARDWARE] HC-SR04 Ultrasonic Distance Sensor initialized on Pins 23/24.")
except Exception:
    pass

# ------------------------------------------------------------------------------
# Sensor Reading Functions
# ------------------------------------------------------------------------------

def read_mpu6050():
    """Reads MPU6050 Accelerometer/Gyroscope and computes motion magnitude"""
    if not HAS_MPU or mpu_sensor is None:
        return None, None
    try:
        accel = mpu_sensor.get_accel_data()
        gyro = mpu_sensor.get_gyro_data()
        
        # Calculate total acceleration magnitude (g)
        mag = math.sqrt(accel['x']**2 + accel['y']**2 + accel['z']**2)
        
        # Map acceleration tilt/movement to Traffic Speed (20 to 110 km/h)
        # 1g baseline = ~35 km/h, tilting/shaking increases speed up to 110 km/h
        traffic_speed = max(20.0, min(110.0, 35.0 + (abs(mag - 1.0) * 80.0) + abs(accel['x']) * 15.0))
        
        # Map Gyro rotation speed to AQI level (15 to 90 AQI)
        gyro_mag = abs(gyro['x']) + abs(gyro['y']) + abs(gyro['z'])
        air_aqi = max(15.0, min(90.0, 25.0 + gyro_mag * 0.2))

        return round(traffic_speed, 1), round(air_aqi, 1)
    except Exception:
        return None, None

def read_cpu_temp():
    """Reads QNX / Pi CPU Thermal Zone to map Power Grid Load (MW)"""
    try:
        if os.path.exists("/sys/class/thermal/thermal_zone0/temp"):
            with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                temp_c = float(f.read().strip()) / 1000.0
                power_mw = 350.0 + ((temp_c - 35.0) / 35.0) * 130.0
                return round(max(300.0, min(520.0, power_mw)), 1)
    except Exception:
        pass
    # Fallback simulation if sysfs path not available
    t = time.time()
    return round(410.0 + math.sin(t * 0.5) * 35.0, 1)

def read_ultrasonic():
    """Reads HC-SR04 Ultrasonic Distance Sensor to map Water Pressure (PSI)"""
    if not HAS_GPIO:
        t = time.time()
        return round(60.0 + math.cos(t * 0.3) * 15.0, 1)
    try:
        GPIO.output(TRIG_PIN, True)
        time.sleep(0.00001)
        GPIO.output(TRIG_PIN, False)
        t_start = time.time()
        t_end = time.time()
        timeout = time.time() + 0.03
        while GPIO.input(ECHO_PIN) == 0:
            t_start = time.time()
            if t_start > timeout: break
        while GPIO.input(ECHO_PIN) == 1:
            t_end = time.time()
            if t_end > timeout: break
        dist_cm = ((t_end - t_start) * 34300) / 2
        water_psi = max(30.0, min(95.0, dist_cm * 1.8))
        return round(water_psi, 1)
    except Exception:
        t = time.time()
        return round(60.0 + math.cos(t * 0.3) * 15.0, 1)

# ------------------------------------------------------------------------------
# Main QNX Telemetry Engine Publisher Loop
# ------------------------------------------------------------------------------

def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx_count = 0
    start_time = time.time()

    os.system('cls' if os.name == 'nt' else 'clear')
    print("==========================================================================")
    print("  QNX REAL-TIME TELEMETRY ENGINE [RASPBERRY PI -> HOST DIGITAL TWIN]")
    print("==========================================================================")
    print(f" Target Digital Twin Host : UDP {HOST_PC_IP}:{HOST_PC_PORT}")
    print(f" MPU6050 Sensor Status    : {'ONLINE (/dev/i2c1)' if HAS_MPU else 'OFFLINE (Fallback)'}")
    print(f" GPIO Sensor Status       : {'ONLINE (Pins 23/24)' if HAS_GPIO else 'OFFLINE'}")
    print("==========================================================================")
    print("\n[STREAMING LIVE TELEMETRY PACKETS] Press Ctrl+C to stop.\n")

    try:
        while True:
            now = time.time()

            # 1. Read MPU6050 Motion Data (Traffic & AQI)
            traffic_val, air_val = read_mpu6050()
            if traffic_val is None:
                traffic_val = round(55.0 + math.sin(now * 1.2) * 20.0, 1)
            if air_val is None:
                air_val = round(35.0 + math.cos(now * 0.8) * 10.0, 1)

            # 2. Read Pi CPU Temp (Power Grid Load)
            power_val = read_cpu_temp()

            # 3. Read Water Pressure
            water_val = read_ultrasonic()

            # Broadcast Streams to Host PC Digital Twin Engine
            streams_to_send = [
                ("traffic", traffic_val, "km/h"),
                ("power", power_val, "MW"),
                ("water", water_val, "PSI"),
                ("air", air_val, "AQI")
            ]

            for sid, val, unit in streams_to_send:
                pkt = json.dumps({"stream": sid, "value": val, "unit": unit})
                sock.sendto(pkt.encode('utf-8'), (HOST_PC_IP, HOST_PC_PORT))
                tx_count += 1

            # Console status pulse (overwrite line)
            rate = round(tx_count / max(1, (now - start_time)), 1)
            sys.stdout.write(
                f"\r[TX STATS] Packets Sent: {tx_count} ({rate} pkts/s) | "
                f"Traffic: {traffic_val:5.1f} km/h | Power: {power_val:5.1f} MW | "
                f"Water: {water_val:5.1f} PSI | Air: {air_val:5.1f} AQI"
            )
            sys.stdout.flush()

            time.sleep(0.05)  # 20 Hz transmission rate

    except KeyboardInterrupt:
        print("\n\n[SHUTDOWN] QNX Telemetry Engine stopped cleanly.")
        if HAS_GPIO:
            GPIO.cleanup()
        sock.close()

if __name__ == "__main__":
    main()
