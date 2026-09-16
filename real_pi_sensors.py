#!/usr/bin/env python3
# ==============================================================================
# QNX Neutrino RTOS - Physical Hardware Sensor Publisher
# Sensors: IR Module (BCM 17), MQ135 Gas (BCM 27), DHT11 (BCM 22)
#
# WIRING REFERENCE:
#   IR Module  → BCM 17 (Board Pin 11)  | VCC=3.3V, GND=GND, OUT=BCM17
#   MQ135      → BCM 27 (Board Pin 13)  | VCC=5V,   GND=GND, D0=BCM27
#   DHT11      → BCM 22 (Board Pin 15)  | VCC=3.3V, GND=GND, DATA=BCM22
#
# IR SENSOR LOGIC:
#   Most IR obstacle modules output:
#     HIGH (1) = No object (clear)
#     LOW  (0) = Object detected (obstacle in front)
#   If your IR is stuck at 0 → turn the BLUE POTENTIOMETER on the module
#   clockwise to REDUCE sensitivity until it reads HIGH with no object.
#
# Run: python3 real_pi_sensors.py <HOST_PC_IP>
# ==============================================================================

import time
import json
import socket
import sys
import os
import math

HOST_PC_IP = sys.argv[1] if len(sys.argv) > 1 else "192.168.29.132"
UDP_PORT   = 9999

# ── Pin Definitions (BCM numbering) ────────────────────────────────────────────
PIN_IR    = 17   # IR Obstacle Module
PIN_MQ135 = 27   # MQ135 Gas Sensor (digital D0 output)
PIN_DHT11 = 22   # DHT11 Temperature & Humidity

# ── RPi.GPIO Setup ─────────────────────────────────────────────────────────────
HAS_GPIO = False
try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(PIN_IR,    GPIO.IN, pull_up_down=GPIO.PUD_UP)   # Pull-UP so default=HIGH
    GPIO.setup(PIN_MQ135, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    HAS_GPIO = True
    print("[OK] RPi.GPIO initialized with PULL-UP resistors.")
except Exception as e:
    print(f"[WARN] RPi.GPIO unavailable ({e}). Simulation mode ON.")

# ── DHT11 Library ─────────────────────────────────────────────────────────────
HAS_DHT     = False
dht_device  = None
try:
    import adafruit_dht
    import board
    _pin_map   = {4: board.D4, 17: board.D17, 22: board.D22, 27: board.D27}
    dht_device = adafruit_dht.DHT11(_pin_map.get(PIN_DHT11, board.D22), use_pulseio=False)
    HAS_DHT    = True
    print(f"[OK] DHT11 initialized on BCM {PIN_DHT11}.")
except Exception as e:
    print(f"[WARN] DHT11 unavailable ({e}). Simulation ON.")

# ── UDP Socket ─────────────────────────────────────────────────────────────────
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

print("=" * 70)
print("  QNX PI HARDWARE TELEMETRY ENGINE")
print(f"  Host PC Target  : {HOST_PC_IP}:{UDP_PORT}")
print(f"  IR  Sensor      : BCM {PIN_IR}  (HIGH=clear, LOW=vehicle detected)")
print(f"  MQ135 Sensor    : BCM {PIN_MQ135} (HIGH=clean air, LOW=gas detected)")
print(f"  DHT11 Sensor    : BCM {PIN_DHT11}")
print(f"  GPIO Available  : {HAS_GPIO}")
print(f"  DHT11 Available : {HAS_DHT}")
print("=" * 70)
print()
print("  ⚠  IR CALIBRATION: If IR reads 0 with no object in front,")
print("     adjust the BLUE potentiometer on the IR module (turn clockwise)")
print("     until the ON-BOARD LED turns OFF with nothing in front of it.")
print()

# ── State ─────────────────────────────────────────────────────────────────────
vehicle_counter  = 0
last_ir_state    = -1    # -1 = not yet read
last_trigger_t   = 0.0
DEBOUNCE_SEC     = 1.0   # Minimum 1 second between vehicle counts (prevents noise spikes)

# DHT11 cache
dht_temp         = 28.0
dht_hum          = 55.0
last_dht_read    = 0.0
DHT_INTERVAL     = 2.5

# Simulation
sim_t0           = time.time()
tx_count         = 0

# ── Diagnostics at start: read raw IR state ────────────────────────────────────
if HAS_GPIO:
    raw_ir = GPIO.input(PIN_IR)
    print(f"  [INIT] Raw IR pin state = {raw_ir}  ({'CLEAR (good)' if raw_ir == 1 else 'STUCK LOW - calibrate potentiometer!'})")
    raw_mq = GPIO.input(PIN_MQ135)
    print(f"  [INIT] Raw MQ pin state = {raw_mq}  ({'Clean air' if raw_mq == 1 else 'Gas detected or pin floating'})")
    print()


def read_pin(pin):
    if HAS_GPIO:
        try:
            return GPIO.input(pin)
        except Exception:
            return None
    return None


def read_dht11():
    global dht_temp, dht_hum, last_dht_read
    now = time.time()
    if HAS_DHT and dht_device and (now - last_dht_read) >= DHT_INTERVAL:
        try:
            t = dht_device.temperature
            h = dht_device.humidity
            if t is not None and h is not None:
                dht_temp = round(float(t), 1)
                dht_hum  = round(float(h), 1)
        except RuntimeError:
            pass  # DHT checksum error — use cached value
        except Exception:
            pass
        last_dht_read = now
    elif not HAS_DHT:
        elapsed  = now - sim_t0
        dht_temp = round(28.0 + 4.0 * math.sin(elapsed * 0.05), 1)
        dht_hum  = round(55.0 + 10.0 * math.cos(elapsed * 0.03), 1)
    return dht_temp, dht_hum


def send_udp(stream, value, unit=""):
    pkt = json.dumps({"stream": stream, "value": value, "unit": unit})
    try:
        sock.sendto(pkt.encode('utf-8'), (HOST_PC_IP, UDP_PORT))
    except Exception as e:
        print(f"\n[UDP ERR] {e}")


# ── Main Loop ─────────────────────────────────────────────────────────────────
print("[RUNNING] Streaming sensor data... Press Ctrl+C to stop.\n")

try:
    while True:
        now = time.time()

        # ── 1. IR Module → Vehicle Counter → Traffic Speed ────────────────────
        ir_val = read_pin(PIN_IR)

        if ir_val is not None:
            # Only count on HIGH→LOW transition (rising-edge object detection)
            # and only if enough time has passed since last count (debounce)
            if ir_val == 0 and last_ir_state == 1:
                if (now - last_trigger_t) >= DEBOUNCE_SEC:
                    vehicle_counter += 1
                    last_trigger_t  = now
                    print(f"\n  [EVENT] 🚗 Vehicle #{vehicle_counter} detected! (IR: HIGH→LOW)")
            last_ir_state = ir_val

            # Map vehicle count to traffic speed
            # 0 vehicles → 30 km/h (empty road baseline)
            # every vehicle adds 5.5 km/h, capped at 120 km/h
            traffic_speed = round(min(120.0, 30.0 + vehicle_counter * 5.5), 1)

        else:
            # Simulation: oscillate slowly so dashboard visibly changes
            traffic_speed = round(55.0 + 30.0 * math.sin(now * 0.3), 1)

        # ── 2. MQ135 → Air Quality Index ──────────────────────────────────────
        mq_val = read_pin(PIN_MQ135)
        if mq_val is not None:
            # LOW  (0) = gas detected → high AQI (bad air)
            # HIGH (1) = clean air   → low AQI (good)
            air_aqi = 95.0 if mq_val == 0 else 22.0
        else:
            air_aqi = round(30.0 + 20.0 * abs(math.sin(now * 0.12)), 1)

        # ── 3. DHT11 → Power Load + Water Pressure proxy ─────────────────────
        temp_c, humidity = read_dht11()
        power_mw  = round(max(300.0, min(550.0, 350.0 + (temp_c - 20.0) * 5.0)), 1)
        water_psi = round(max(30.0,  min(95.0,  50.0 + humidity * 0.3)), 1)

        # ── 4. Transmit to Host PC ────────────────────────────────────────────
        send_udp("traffic", traffic_speed, "km/h")
        send_udp("power",   power_mw,      "MW")
        send_udp("water",   water_psi,     "PSI")
        send_udp("air",     air_aqi,       "AQI")
        tx_count += 4

        # ── 5. Console status line ────────────────────────────────────────────
        ir_label  = f"{'BLOCKED' if ir_val == 0 else 'CLEAR  '}" if ir_val is not None else "SIM"
        mq_label  = f"{'GAS!   ' if mq_val == 0 else 'CLEAN  '}" if mq_val is not None else "SIM"
        sys.stdout.write(
            f"\r  TX:{tx_count:5d} | "
            f"IR:{ir_label}({vehicle_counter}v) "
            f"MQ:{mq_label} "
            f"DHT:{temp_c:.1f}°C/{humidity:.0f}% | "
            f"Traffic:{traffic_speed:5.1f}km/h "
            f"Power:{power_mw:5.1f}MW "
            f"Water:{water_psi:4.1f}PSI "
            f"AQI:{air_aqi:4.0f}"
        )
        sys.stdout.flush()

        time.sleep(0.1)   # 10 Hz

except KeyboardInterrupt:
    print("\n\n[SHUTDOWN] Sensor engine stopped cleanly.")
    if HAS_GPIO:
        GPIO.cleanup()
    if dht_device:
        try:
            dht_device.exit()
        except Exception:
            pass
    sock.close()
