#!/usr/bin/env python3
# ==============================================================================
# QNX / Raspberry Pi 4 Direct Hardware GPIO Register & Pin Driver
# Accesses hardware registers directly via BCM2711 memory map or sysfs
# ==============================================================================

import time
import json
import socket
import sys
import os
import struct

HOST_PC_IP = sys.argv[1] if len(sys.argv) > 1 else "192.168.29.132"
UDP_PORT = 9999

# BCM Pin Definitions
PIN_IR     = 17  # Board Pin 11
PIN_MQ135  = 27  # Board Pin 13
PIN_ACS712 = 22  # Board Pin 15

# ------------------------------------------------------------------------------
# Direct Physical Hardware Register Reader (BCM2711 GPIO GPLEV0)
# ------------------------------------------------------------------------------
class DirectGPIOMem:
    def __init__(self):
        self.mem = None
        self.valid = False
        
        # Try opening /dev/gpiomem or /dev/mem
        dev_path = "/dev/gpiomem" if os.path.exists("/dev/gpiomem") else "/dev/mem"
        base_addr = 0xfe200000 if os.path.exists("/dev/gpiomem") or os.path.exists("/dev/mem") else 0x3f200000
        
        try:
            fd = os.open(dev_path, os.O_RDWR | os.O_SYNC)
            import mmap
            self.mem = mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=base_addr)
            os.close(fd)
            self.valid = True
            print(f"[HARDWARE SUCCESS] Mapped physical BCM2711 GPIO registers via {dev_path}!")
        except Exception as e:
            print(f"[NOTICE] Direct memory access ({dev_path}): {e}")

    def read_pin(self, pin):
        if not self.valid or self.mem is None:
            return None
        try:
            # GPLEV0 register is at offset 0x34 (words: 13)
            self.mem.seek(0x34)
            gplev0 = struct.unpack("I", self.mem.read(4))[0]
            return (gplev0 >> pin) & 1
        except Exception:
            return None

# Try RPi.GPIO first
HAS_RPI_GPIO = False
try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(PIN_IR, GPIO.IN)
    GPIO.setup(PIN_MQ135, GPIO.IN)
    GPIO.setup(PIN_ACS712, GPIO.IN)
    HAS_RPI_GPIO = True
    print("[HARDWARE SUCCESS] RPi.GPIO driver initialized.")
except Exception:
    pass

# Direct memory mapper fallback
direct_mem = DirectGPIOMem() if not HAS_RPI_GPIO else None

def read_pin_state(pin):
    if HAS_RPI_GPIO:
        try:
            return GPIO.input(pin)
        except Exception:
            pass
    if direct_mem and direct_mem.valid:
        return direct_mem.read_pin(pin)
    
    # Sysfs fallback
    val_path = f"/sys/class/gpio/gpio{pin}/value"
    if os.path.exists(val_path):
        try:
            with open(val_path, "r") as f:
                return int(f.read().strip())
        except Exception:
            pass
    return None

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

print("==========================================================================")
print("  QNX PI PHYSICAL HARDWARE SENSOR STREAMER")
print(f"  Target Host PC IP: UDP {HOST_PC_IP}:{UDP_PORT}")
print("==========================================================================")
print("  Listening for physical hardware pin changes...\n")

vehicle_counter = 0
last_ir_val = 1

try:
    while True:
        # Read IR Pin (GPIO 17)
        ir_val = read_pin_state(PIN_IR)
        if ir_val is not None:
            if ir_val == 0 and last_ir_val == 1:
                vehicle_counter += 1
                print(f"\n[EVENT] IR Sensor Triggered! Vehicles: {vehicle_counter}")
            last_ir_val = ir_val
            traffic_speed = round(min(120.0, 30.0 + vehicle_counter * 8.0), 1)
        else:
            traffic_speed = 45.0

        # Read MQ135 Pin (GPIO 27)
        mq_val = read_pin_state(PIN_MQ135)
        if mq_val is not None:
            air_aqi = round(85.0 if mq_val == 0 else 25.0, 1)
        else:
            air_aqi = 28.0

        # Read ACS712 Pin (GPIO 22)
        acs_val = read_pin_state(PIN_ACS712)
        if acs_val is not None:
            power_mw = round(490.0 if acs_val == 0 else 380.0, 1)
        else:
            power_mw = 410.0

        water_psi = round(65.0, 1)

        # Transmit UDP telemetry stream to Host PC
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
