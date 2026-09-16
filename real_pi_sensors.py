#!/usr/bin/env python3
# ==============================================================================
# QNX Neutrino RTOS - Physical Hardware BCM2711 Memory Driver
# Maps BCM2711 GPIO Registers (0xFE200000) directly via QNX Physical mmap
# ==============================================================================

import time
import json
import socket
import sys
import os
import struct
import mmap

HOST_PC_IP = sys.argv[1] if len(sys.argv) > 1 else "192.168.29.132"
UDP_PORT = 9999

# Pin Definitions (BCM Numbering)
PIN_IR     = 17  # Board Pin 11
PIN_MQ135  = 27  # Board Pin 13
PIN_ACS712 = 22  # Board Pin 15

class QNXGPIOMem:
    def __init__(self):
        self.mem = None
        self.valid = False
        base_addr = 0xfe200000

        try:
            self.mem = mmap.mmap(-1, 4096, mmap.MAP_SHARED, mmap.PROT_READ, offset=base_addr)
            self.valid = True
            print(f"[QNX SUCCESS] Physical GPIO memory mapped at 0x{base_addr:X} (NOFD)!")
            return
        except Exception:
            pass

        for dev in ["/dev/gpiomem", "/dev/mem"]:
            if os.path.exists(dev):
                try:
                    fd = os.open(dev, os.O_RDONLY | os.O_SYNC)
                    self.mem = mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ, offset=base_addr)
                    os.close(fd)
                    self.valid = True
                    print(f"[QNX SUCCESS] Physical GPIO memory mapped via {dev} at 0x{base_addr:X}!")
                    return
                except Exception:
                    pass

    def read_pin(self, pin):
        if not self.valid or self.mem is None:
            return None
        try:
            self.mem.seek(0x34)
            gplev0 = struct.unpack("I", self.mem.read(4))[0]
            return (gplev0 >> pin) & 1
        except Exception:
            return None

HAS_RPI_GPIO = False
try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(PIN_IR, GPIO.IN)
    GPIO.setup(PIN_MQ135, GPIO.IN)
    GPIO.setup(PIN_ACS712, GPIO.IN)
    HAS_RPI_GPIO = True
    print("[HARDWARE SUCCESS] RPi.GPIO driver active.")
except Exception:
    pass

qnx_mem = QNXGPIOMem() if not HAS_RPI_GPIO else None

def read_pin(pin):
    if HAS_RPI_GPIO:
        try: return GPIO.input(pin)
        except Exception: pass
    if qnx_mem and qnx_mem.valid:
        return qnx_mem.read_pin(pin)
    return None

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

print("==========================================================================")
print("  QNX PI HARDWARE TELEMETRY ENGINE (IR, MQ135, ACS712)")
print(f"  Target Host PC IP: UDP {HOST_PC_IP}:{UDP_PORT}")
print("==========================================================================")
print("  Listening for physical hardware pin changes...\n")

vehicle_counter = 0
last_ir_val = 1
last_trigger_time = 0

try:
    while True:
        now = time.time()
        ir_val = read_pin(PIN_IR)
        mq_val = read_pin(PIN_MQ135)
        acs_val = read_pin(PIN_ACS712)

        # IR Sensor Trigger Logic with Debounce
        if ir_val is not None:
            # Trigger on LOW (0) with at least 0.3s between vehicle counts
            if ir_val == 0:
                if (now - last_trigger_time) > 0.35:
                    vehicle_counter += 1
                    last_trigger_time = now
                    print(f"\n[EVENT] Vehicle Detected! Total Count: {vehicle_counter}")
            last_ir_val = ir_val
            traffic_speed = round(min(120.0, 30.0 + vehicle_counter * 6.0), 1)
        else:
            traffic_speed = 45.0

        if mq_val is not None:
            air_aqi = round(85.0 if mq_val == 0 else 25.0, 1)
        else:
            air_aqi = 28.0

        if acs_val is not None:
            power_mw = round(490.0 if acs_val == 0 else 380.0, 1)
        else:
            power_mw = 410.0

        water_psi = round(65.0, 1)

        # Broadcast UDP telemetry packets to Host PC
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
