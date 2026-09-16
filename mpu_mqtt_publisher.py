#!/usr/bin/env python3
# ==============================================================================
# QNX MPU6050 Live Telemetry Publisher via MQTT Protocol
# Reads MPU6050 I2C sensor on QNX Pi (/dev/i2c1) and publishes live JSON
# telemetry to an MQTT Broker (e.g. Host PC or Public Broker).
# ==============================================================================

import time
import json
import sys
import math

# MQTT Configuration
MQTT_BROKER = "10.12.2.121"  # Default Host PC IP (or broker.emqx.io)
MQTT_PORT = 1883
MQTT_TOPIC = "digitaltwin/mpu6050"

if len(sys.argv) > 1:
    MQTT_BROKER = sys.argv[1]

# Try importing paho.mqtt
try:
    import paho.mqtt.client as mqtt
    HAS_MQTT = True
except ImportError:
    print("[ERROR] 'paho-mqtt' library not found.")
    print("Install via: pip3 install paho-mqtt --break-system-packages")
    sys.exit(1)

# Initialize MPU6050
HAS_MPU = False
sensor = None
try:
    from mpu6050 import mpu6050
    sensor = mpu6050(0x68, bus=1)
    HAS_MPU = True
    print("[HARDWARE] MPU6050 detected on /dev/i2c1 (0x68).")
except Exception as e:
    print(f"[WARNING] MPU6050 not detected: {e}")

pitch, roll, yaw = 0.0, 0.0, 0.0
last_time = time.time()
ALPHA = 0.96

def read_orientation():
    global pitch, roll, yaw, last_time
    now = time.time()
    dt = max(0.001, min(0.1, now - last_time))
    last_time = now

    if HAS_MPU and sensor is not None:
        try:
            accel = sensor.get_accel_data()
            gyro = sensor.get_gyro_data()
            ax, ay, az = accel['x'], accel['y'], accel['z']
            gx, gy, gz = gyro['x'], gyro['y'], gyro['z']

            accel_pitch = math.atan2(-ax, math.sqrt(ay**2 + az**2)) * (180.0 / math.pi)
            accel_roll  = math.atan2(ay, az) * (180.0 / math.pi)

            pitch = ALPHA * (pitch + gx * dt) + (1.0 - ALPHA) * accel_pitch
            roll  = ALPHA * (roll  + gy * dt) + (1.0 - ALPHA) * accel_roll
            yaw   += gz * dt

            return {
                "pitch": round(pitch, 2),
                "roll": round(roll, 2),
                "yaw": round(yaw % 360.0, 2),
                "accel": {"x": round(ax, 3), "y": round(ay, 3), "z": round(az, 3)},
                "gyro": {"x": round(gx, 2), "y": round(gy, 2), "z": round(gz, 2)},
                "protocol": "MQTT",
                "timestamp": round(now, 3)
            }
        except Exception:
            pass

    t = now * 1.5
    return {
        "pitch": round(math.sin(t) * 25.0, 2),
        "roll": round(math.cos(t * 0.8) * 35.0, 2),
        "yaw": round((t * 10.0) % 360.0, 2),
        "accel": {"x": 0.0, "y": 0.0, "z": 1.0},
        "gyro": {"x": 0.0, "y": 0.0, "z": 0.0},
        "protocol": "MQTT-EMULATION",
        "timestamp": round(now, 3)
    }

def main():
    print("==========================================================================")
    print("  QNX MPU6050 MQTT TELEMETRY PUBLISHER")
    print("==========================================================================")
    print(f" Target MQTT Broker : {MQTT_BROKER}:{MQTT_PORT}")
    print(f" MQTT Topic         : {MQTT_TOPIC}")
    print("==========================================================================")

    client = mqtt.Client(client_id="QNX_Pi_MPU_Publisher")
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
        print("[MQTT] Connected to MQTT Broker successfully!")
    except Exception as e:
        print(f"[MQTT ERROR] Failed to connect to broker {MQTT_BROKER}: {e}")
        return

    count = 0
    try:
        while True:
            payload = read_orientation()
            payload["packet_id"] = count
            msg = json.dumps(payload)

            client.publish(MQTT_TOPIC, msg, qos=0)
            count += 1

            sys.stdout.write(
                f"\r[MQTT TX #{count:06d}] Topic: {MQTT_TOPIC} | Pitch: {payload['pitch']:6.2f}° | Roll: {payload['roll']:6.2f}°"
            )
            sys.stdout.flush()
            time.sleep(0.02)  # 50 Hz MQTT stream rate

    except KeyboardInterrupt:
        print("\n\n[SHUTDOWN] MQTT Publisher closed.")
        client.loop_stop()
        client.disconnect()

if __name__ == "__main__":
    main()
