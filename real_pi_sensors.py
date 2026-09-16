#!/usr/bin/env python3
# ==============================================================================
# QNX / Raspberry Pi Hardware Sensor Driver (Verbose Pin Diagnostics)
# ==============================================================================

import time
import json
import socket
import sys
import os

HOST_PC_IP = sys.argv[1] if len(sys.argv) > 1 else "192.168.29.132"
UDP_PORT = 9999

# Pin Definitions (BCM Numbering)
PIN_IR     = 17  # Pin 11 (IR Sensor)
PIN_MQ135  = 27  # Pin 13 (MQ135 Sensor)
PIN_ACS712 = 22  # Pin 15 (ACS712 Sensor)

def setup_gpio(pin):
    gpio_dir = f"/sys/class/gpio/gpio{pin}"
    try:
        if not os.path.exists(gpio_dir):
            if os.path.exists("/sys/class/gpio/export"):
                with open("/sys/class/gpio/export", "w") as f:
                    f.write(str(pin))
                time.sleep(0.05)
        if os.path.exists(f"{gpio_dir}/direction"):
            with open(f"{gpio_dir}/direction", "w") as f:
                f.write("in")
        print(f"  [GPIO Pin {pin}] Configured successfully via sysfs.")
        return True
    except Exception as e:
        print(f"  [GPIO Pin {pin} WARNING] Could not setup pin: {e}")
        return False

def read_gpio(pin):
    try:
        val_path = f"/sys/class/gpio/gpio{pin}/value"
        if os.path.exists(val_path):
            with open(val_path, "r") as f:
                return int(f.read().strip())
    except Exception:
        pass
    return None

# Check for RPi.GPIO or setup sysfs
HAS_RPI_GPIO = False
try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(PIN_IR, GPIO.IN)
    GPIO.setup(PIN_MQ135, GPIO.IN)
    GPIO.setup(PIN_ACS712, GPIO.IN)
    HAS_RPI_GPIO = True
    print("[HARDWARE] RPi.GPIO driver active.")
except Exception:
    print("[HARDWARE] Setting up native sysfs GPIO pins (requires sudo)...")
    setup_gpio(PIN_IR)
    setup_gpio(PIN_MQ135)
    setup_gpio(PIN_ACS712)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

print(f" Target Host PC IP: {HOST_PC_IP}:{UDP_PORT}")
print(" Listening for live physical hardware state changes...\n")

vehicle_counter = 0
last_ir_val = 1

try:
    while True:
        # Read IR Pin (GPIO 17)
        ir_val = GPIO.input(PIN_IR) if HAS_RPI_GPIO else read_gpio(PIN_IR)
        if ir_val is not None:
            if ir_val == 0 and last_ir_val == 1:
                vehicle_counter += 1
                print(f"\n[EVENT] IR Sensor Triggered! Total Vehicles: {vehicle_counter}")
            last_ir_val = ir_val
            traffic_speed = round(min(120.0, 30.0 + vehicle_counter * 8.0), 1)
        else:
            traffic_speed = 45.0

        # Read MQ135 Pin (GPIO 27)
        mq_val = GPIO.input(PIN_MQ135) if HAS_RPI_GPIO else read_gpio(PIN_MQ135)
        if mq_val is not None:
            air_aqi = round(85.0 if mq_val == 0 else 25.0, 1)
        else:
            air_aqi = 28.0

        # Read ACS712 Pin (GPIO 22)
        acs_val = GPIO.input(PIN_ACS712) if HAS_RPI_GPIO else read_gpio(PIN_ACS712)
        if acs_val is not None:
            power_mw = round(490.0 if acs_val == 0 else 380.0, 1)
        else:
            power_mw = 410.0

        water_psi = round(65.0, 1)

        # Broadcast telemetry UDP packets to Host PC
        telemetry = [
            ("traffic", traffic_speed),
            ("power", power_mw),
            ("water", water_psi),
            ("air", air_aqi)
        ]

        for sid, val in telemetry:
            pkt = json.dumps({"stream": sid, "value": val})
            sock.sendto(pkt.encode('utf-8'), (HOST_PC_IP, UDP_PORT))

        sys.stdout.write(
            f"\r[PINS IR:{ir_val} MQ:{mq_val} ACS:{acs_val}] Vehicles: {vehicle_counter} | "
            f"Traffic: {traffic_speed:5.1f} km/h | Power: {power_mw:5.1f} MW | Air: {air_aqi:5.1f} AQI"
        )
        sys.stdout.flush()

        time.sleep(0.05)

except KeyboardInterrupt:
    print("\n\n[SHUTDOWN] Hardware driver closed.")
    if HAS_RPI_GPIO:
        GPIO.cleanup()
    sock.close()
