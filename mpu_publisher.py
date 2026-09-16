#!/usr/bin/env python3
# ==============================================================================
# QNX MPU6050 Live 3D Orientation & Motion Publisher
# Reads MPU6050 I2C sensor on QNX Pi (/dev/i2c1) at 50Hz, calculates 3D Pitch
# and Roll using a complementary filter, and streams low-latency UDP packets
# to the Host PC for 3D cuboid orientation visualization.
# ==============================================================================

import time
import socket
import json
import sys
import math

# Default Host PC IP and Port
HOST_PC_IP = "10.12.2.121"
HOST_PC_PORT = 9998

if len(sys.argv) > 1:
    HOST_PC_IP = sys.argv[1]

# ------------------------------------------------------------------------------
# Initialize MPU6050 Hardware Driver
# ------------------------------------------------------------------------------
HAS_MPU = False
sensor = None

try:
    from mpu6050 import mpu6050
    sensor = mpu6050(0x68, bus=1)
    HAS_MPU = True
    print("[HARDWARE] MPU6050 initialized successfully on /dev/i2c1 (0x68).")
except Exception as e:
    print(f"[WARNING] MPU6050 not detected on /dev/i2c1: {e}")
    print("[NOTICE] Running in simulation fallback mode. Connect MPU6050 for physical 3D twin.")

# ------------------------------------------------------------------------------
# Complementary Filter Variables
# ------------------------------------------------------------------------------
pitch = 0.0
roll = 0.0
yaw = 0.0
last_time = time.time()

ALPHA = 0.96  # Weight constant for Complementary Filter (96% Gyro, 4% Accel)

def update_orientation():
    """Reads sensor data and computes pitch, roll, yaw using Complementary Filtering"""
    global pitch, roll, yaw, last_time

    now = time.time()
    dt = max(0.001, min(0.1, now - last_time))
    last_time = now

    if HAS_MPU and sensor is not None:
        try:
            accel = sensor.get_accel_data()  # in g
            gyro = sensor.get_gyro_data()    # in deg/s
            temp = sensor.get_temp()         # in deg C

            ax, ay, az = accel['x'], accel['y'], accel['z']
            gx, gy, gz = gyro['x'], gyro['y'], gyro['z']

            # Calculate Accelerometer Pitch & Roll (in degrees)
            accel_pitch = math.atan2(-ax, math.sqrt(ay**2 + az**2)) * (180.0 / math.pi)
            accel_roll  = math.atan2(ay, az) * (180.0 / math.pi)

            # Integrate Gyroscope readings over dt
            gyro_pitch = pitch + gx * dt
            gyro_roll  = roll + gy * dt
            yaw        += gz * dt  # Yaw drifts over time without magnetometer/compass

            # Apply Complementary Filter to prevent gyro drift & accel noise
            pitch = ALPHA * gyro_pitch + (1.0 - ALPHA) * accel_pitch
            roll  = ALPHA * gyro_roll  + (1.0 - ALPHA) * accel_roll

            return {
                "pitch": round(pitch, 2),
                "roll": round(roll, 2),
                "yaw": round(yaw % 360.0, 2),
                "accel": {"x": round(ax, 3), "y": round(ay, 3), "z": round(az, 3)},
                "gyro": {"x": round(gx, 2), "y": round(gy, 2), "z": round(gz, 2)},
                "temp": round(temp, 1),
                "mode": "REAL_HARDWARE"
            }
        except Exception:
            pass

    # Simulation fallback if MPU sensor isn't plugged in
    t = now * 1.5
    sim_pitch = math.sin(t) * 25.0
    sim_roll = math.cos(t * 0.8) * 35.0
    sim_yaw = (t * 10.0) % 360.0
    return {
        "pitch": round(sim_pitch, 2),
        "roll": round(sim_roll, 2),
        "yaw": round(sim_yaw, 2),
        "accel": {"x": round(math.sin(t), 3), "y": round(math.cos(t), 3), "z": 1.0},
        "gyro": {"x": 5.0, "y": -2.0, "z": 10.0},
        "temp": 28.5,
        "mode": "EMULATION"
    }

# ------------------------------------------------------------------------------
# UDP Publisher Main Loop
# ------------------------------------------------------------------------------
def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print("==========================================================================")
    print("  QNX MPU6050 3D ORIENTATION TELEMETRY ENGINE")
    print("==========================================================================")
    print(f" Target Host PC IP : {HOST_PC_IP}:{HOST_PC_PORT}")
    print(f" Sampling Rate     : 50 Hz (20ms interval)")
    print("==========================================================================")
    print("\nStreaming 3D pose data... Press Ctrl+C to stop.\n")

    tx_count = 0
    start_time = time.time()

    try:
        while True:
            data = update_orientation()
            data["packet_id"] = tx_count
            data["timestamp"] = round(time.time(), 3)

            payload = json.dumps(data).encode('utf-8')
            sock.sendto(payload, (HOST_PC_IP, HOST_PC_PORT))
            tx_count += 1

            # Print status line
            sys.stdout.write(
                f"\r[QNX 3D TX #{tx_count:06d}] Pitch: {data['pitch']:6.2f}° | "
                f"Roll: {data['roll']:6.2f}° | Yaw: {data['yaw']:6.2f}° | Mode: {data['mode']}"
            )
            sys.stdout.flush()

            time.sleep(0.02)  # 50 Hz stream rate

    except KeyboardInterrupt:
        print("\n\n[STOPPED] Telemetry publisher terminated.")
        sock.close()

if __name__ == "__main__":
    main()
