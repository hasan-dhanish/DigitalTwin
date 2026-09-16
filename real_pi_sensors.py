#!/usr/bin/env python3
# ==============================================================================
# QNX Neutrino RTOS - Physical Hardware Sensor Publisher
# Uses DIRECT BCM2711 GPIO register access via /dev/gpiomem (no RPi.GPIO needed)
# Works natively on QNX RTOS, Raspberry Pi OS, Ubuntu — any Linux on Pi 4.
#
# Sensors:
#   IR Module  → BCM 17 (Board Pin 11)  | VCC=3.3V  GND=GND  OUT→BCM17
#   MQ135      → BCM 27 (Board Pin 13)  | VCC=5V    GND=GND  D0→BCM27
#   DHT11      → BCM 22 (Board Pin 15)  | VCC=3.3V  GND=GND  DATA→BCM22
#
# Run: python3 real_pi_sensors.py <HOST_PC_IP>
# ==============================================================================

import time
import json
import socket
import sys
import os
import math
import struct
import mmap
import ctypes

HOST_PC_IP = sys.argv[1] if len(sys.argv) > 1 else "192.168.29.132"
UDP_PORT   = 9999

# ── BCM2711 GPIO Register Map ──────────────────────────────────────────────────
BCM2711_GPIO_BASE = 0xFE200000   # Pi 4 peripheral base
BLOCK_SIZE        = 4096

# Register offsets (bytes from GPIO base)
GPFSEL0   = 0x00   # Function select registers (GPFSEL0..5)
GPSET0    = 0x1C   # Pin output SET (write 1 to set HIGH)
GPCLR0    = 0x28   # Pin output CLR (write 1 to set LOW)
GPLEV0    = 0x34   # Pin level READ
GPPUPPDN0 = 0xE4   # Pull-up/down control (BCM2711 new register)

# Pin definitions (BCM numbering)
PIN_IR    = 17
PIN_MQ135 = 27
PIN_DHT11 = 22

# ── Direct BCM2711 GPIO Driver ─────────────────────────────────────────────────
class QNXGPIODriver:
    """Direct /dev/gpiomem access — works on QNX and any Linux on Pi 4."""

    def __init__(self):
        self.mem   = None
        self.valid = False
        self.dev   = None

        for dev_path in ['/dev/gpiomem', '/dev/mem']:
            if not os.path.exists(dev_path):
                continue
            try:
                fd     = os.open(dev_path, os.O_RDWR | os.O_SYNC)
                offset = BCM2711_GPIO_BASE if dev_path == '/dev/mem' else 0
                self.mem = mmap.mmap(
                    fd, BLOCK_SIZE,
                    mmap.MAP_SHARED,
                    mmap.PROT_READ | mmap.PROT_WRITE,
                    offset=offset
                )
                os.close(fd)
                self.valid = True
                self.dev   = dev_path
                print(f"[QNX GPIO] ✅ BCM2711 mapped via {dev_path} (offset=0x{offset:08X})")
                return
            except Exception as e:
                print(f"[QNX GPIO] {dev_path} failed: {e}")
                try:
                    os.close(fd)
                except Exception:
                    pass

        print("[QNX GPIO] ⚠ No /dev/gpiomem or /dev/mem access. Simulation mode.")

    def _rd(self, reg_offset):
        if not self.valid:
            return 0
        self.mem.seek(reg_offset)
        return struct.unpack('<I', self.mem.read(4))[0]

    def _wr(self, reg_offset, value):
        if not self.valid:
            return
        self.mem.seek(reg_offset)
        self.mem.write(struct.pack('<I', value))

    def set_input(self, pin):
        """Configure pin as input (GPFSEL bits = 000)."""
        reg   = GPFSEL0 + (pin // 10) * 4
        shift = (pin % 10) * 3
        val   = self._rd(reg)
        val  &= ~(0b111 << shift)   # Clear 3 bits → input
        self._wr(reg, val)

    def set_output(self, pin):
        """Configure pin as output (GPFSEL bits = 001)."""
        reg   = GPFSEL0 + (pin // 10) * 4
        shift = (pin % 10) * 3
        val   = self._rd(reg)
        val  &= ~(0b111 << shift)
        val  |=  (0b001 << shift)   # 001 = output
        self._wr(reg, val)

    def set_pull_up(self, pin):
        """Enable pull-up on pin (BCM2711 GPPUPPDN register)."""
        reg   = GPPUPPDN0 + (pin // 16) * 4
        shift = (pin % 16) * 2
        val   = self._rd(reg)
        val  &= ~(0b11 << shift)
        val  |=  (0b01 << shift)    # 01 = pull-up
        self._wr(reg, val)

    def read(self, pin):
        """Read current pin level. Returns 0 or 1."""
        reg = GPLEV0 + (pin // 32) * 4
        val = self._rd(reg)
        return (val >> (pin % 32)) & 1

    def set_high(self, pin):
        reg = GPSET0 + (pin // 32) * 4
        self._wr(reg, 1 << (pin % 32))

    def set_low(self, pin):
        reg = GPCLR0 + (pin // 32) * 4
        self._wr(reg, 1 << (pin % 32))


# ── DHT11 Bit-Bang Driver (pure Python, no library needed) ────────────────────
class DHT11Driver:
    """
    Software bit-bang DHT11 over direct GPIO.
    DHT11 protocol:
      1. Pull DATA LOW ≥ 18 ms  (start signal)
      2. Release → HIGH 20-40 µs
      3. DHT responds: LOW 80µs → HIGH 80µs
      4. 40 data bits: each = LOW 50µs then HIGH 26µs(0) or 70µs(1)
    """

    def __init__(self, gpio: QNXGPIODriver, pin: int):
        self.gpio = gpio
        self.pin  = pin
        self._last_temp = 28.0
        self._last_hum  = 55.0

    def read(self):
        """Returns (temp_c, humidity_pct) or (None, None) on failure."""
        if not self.gpio.valid:
            return None, None

        pin  = self.pin
        gpio = self.gpio

        try:
            # 1. Send start signal: OUTPUT LOW for 20 ms
            gpio.set_output(pin)
            gpio.set_low(pin)
            time.sleep(0.020)

            # 2. Release: INPUT (pull-up brings it HIGH)
            gpio.set_input(pin)
            gpio.set_pull_up(pin)
            time.sleep(0.00004)   # 40 µs

            # 3. Wait for DHT response LOW
            timeout = time.monotonic() + 0.001
            while gpio.read(pin) == 1:
                if time.monotonic() > timeout:
                    return None, None

            # 4. Wait for DHT response HIGH
            timeout = time.monotonic() + 0.001
            while gpio.read(pin) == 0:
                if time.monotonic() > timeout:
                    return None, None

            # 5. Wait for HIGH to end → start reading bits
            timeout = time.monotonic() + 0.001
            while gpio.read(pin) == 1:
                if time.monotonic() > timeout:
                    return None, None

            # 6. Read 40 bits
            bits = []
            for _ in range(40):
                # Wait for LOW → HIGH transition (bit start)
                timeout = time.monotonic() + 0.001
                while gpio.read(pin) == 0:
                    if time.monotonic() > timeout:
                        return None, None

                # Measure HIGH pulse width
                t_start = time.monotonic()
                timeout  = time.monotonic() + 0.001
                while gpio.read(pin) == 1:
                    if time.monotonic() > timeout:
                        return None, None
                pulse_us = (time.monotonic() - t_start) * 1_000_000

                # >40 µs → bit=1, else bit=0
                bits.append(1 if pulse_us > 40 else 0)

            # 7. Parse 40 bits → 5 bytes
            if len(bits) < 40:
                return None, None

            byte_vals = []
            for i in range(5):
                b = 0
                for j in range(8):
                    b = (b << 1) | bits[i * 8 + j]
                byte_vals.append(b)

            hum_int, hum_dec, tmp_int, tmp_dec, checksum = byte_vals
            if checksum != ((hum_int + hum_dec + tmp_int + tmp_dec) & 0xFF):
                return None, None   # checksum failed

            self._last_temp = float(tmp_int) + float(tmp_dec) / 10.0
            self._last_hum  = float(hum_int) + float(hum_dec) / 10.0
            return self._last_temp, self._last_hum

        except Exception:
            return None, None

    def read_cached(self):
        """Try fresh read; return cached values on failure."""
        t, h = self.read()
        if t is None:
            return self._last_temp, self._last_hum
        return t, h


# ── CPU Temperature Fallback (QNX sysfs) ──────────────────────────────────────
def read_cpu_temp():
    for path in [
        '/dev/thermal',
        '/sys/class/thermal/thermal_zone0/temp',
        '/proc/thermal',
    ]:
        try:
            with open(path, 'r') as f:
                raw = f.read().strip()
                val = float(raw)
                if val > 1000:
                    val /= 1000.0
                return round(val, 1)
        except Exception:
            pass
    return None


# ── Initialization ─────────────────────────────────────────────────────────────
gpio = QNXGPIODriver()

if gpio.valid:
    gpio.set_input(PIN_IR)
    gpio.set_input(PIN_MQ135)
    gpio.set_pull_up(PIN_IR)
    gpio.set_pull_up(PIN_MQ135)

dht = DHT11Driver(gpio, PIN_DHT11)

# UDP socket
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

print("=" * 70)
print("  QNX PI HARDWARE TELEMETRY ENGINE  (BCM2711 Direct Register Access)")
print(f"  Host PC Target  : {HOST_PC_IP}:{UDP_PORT}")
print(f"  GPIO Driver     : {'✅ /dev/gpiomem DIRECT' if gpio.valid else '⚠ SIMULATION'}")
print(f"  IR  (BCM {PIN_IR})    : HIGH=clear  LOW=vehicle detected")
print(f"  MQ135 (BCM {PIN_MQ135}) : HIGH=clean  LOW=gas detected")
print(f"  DHT11 (BCM {PIN_DHT11}) : Bit-bang driver")
print("=" * 70)

# Print raw pin state at boot
if gpio.valid:
    ir_boot  = gpio.read(PIN_IR)
    mq_boot  = gpio.read(PIN_MQ135)
    print(f"\n  [INIT] IR  pin = {ir_boot} → {'CLEAR ✅' if ir_boot == 1 else 'STUCK LOW ⚠ → adjust IR potentiometer!'}")
    print(f"  [INIT] MQ  pin = {mq_boot} → {'Clean air ✅' if mq_boot == 1 else 'Gas/floating ⚠'}")
    print()

# ── State ─────────────────────────────────────────────────────────────────────
vehicle_counter = 0
last_ir_state   = gpio.read(PIN_IR) if gpio.valid else 1
last_trigger_t  = 0.0
DEBOUNCE_SEC    = 1.0

dht_cache_temp  = 28.0
dht_cache_hum   = 55.0
last_dht_t      = 0.0
DHT_INTERVAL    = 3.0   # DHT11 is slow; read every 3 s

sim_t0          = time.time()
tx_count        = 0


def send_udp(stream, value, unit=""):
    pkt = json.dumps({"stream": stream, "value": round(float(value), 2), "unit": unit})
    try:
        sock.sendto(pkt.encode('utf-8'), (HOST_PC_IP, UDP_PORT))
    except Exception as e:
        print(f"\n[UDP ERR] {e}")


# ── Main Loop ─────────────────────────────────────────────────────────────────
print("[RUNNING] Streaming... Press Ctrl+C to stop.\n")

try:
    while True:
        now = time.time()

        # ── IR Sensor → Traffic ────────────────────────────────────────────────
        if gpio.valid:
            ir_val = gpio.read(PIN_IR)
            if ir_val == 0 and last_ir_state == 1:   # HIGH→LOW edge = vehicle
                if (now - last_trigger_t) >= DEBOUNCE_SEC:
                    vehicle_counter += 1
                    last_trigger_t   = now
                    print(f"\n  [EVENT] 🚗 Vehicle #{vehicle_counter} (IR HIGH→LOW)")
            last_ir_state  = ir_val
            traffic_speed  = round(min(120.0, 30.0 + vehicle_counter * 5.5), 1)
            ir_label       = "BLOCKED" if ir_val == 0 else "CLEAR  "
        else:
            ir_val        = None
            traffic_speed = round(55.0 + 30.0 * math.sin(now * 0.3), 1)
            ir_label      = f"SIM({traffic_speed:.0f})"

        # ── MQ135 → Air Quality ────────────────────────────────────────────────
        if gpio.valid:
            mq_val  = gpio.read(PIN_MQ135)
            air_aqi = 95.0 if mq_val == 0 else 22.0
            mq_label = "GAS!  " if mq_val == 0 else "CLEAN "
        else:
            mq_val   = None
            air_aqi  = round(30.0 + 20.0 * abs(math.sin(now * 0.12)), 1)
            mq_label = f"SIM({air_aqi:.0f})"

        # ── DHT11 → Temperature + Humidity ────────────────────────────────────
        if (now - last_dht_t) >= DHT_INTERVAL:
            t, h = dht.read_cached() if gpio.valid else (None, None)
            if t is not None:
                dht_cache_temp = t
                dht_cache_hum  = h
            else:
                # Try CPU temp as fallback
                cpu_t = read_cpu_temp()
                if cpu_t is not None:
                    dht_cache_temp = cpu_t
                else:
                    # Pure simulation
                    elapsed        = now - sim_t0
                    dht_cache_temp = round(28.0 + 4.0 * math.sin(elapsed * 0.05), 1)
                    dht_cache_hum  = round(55.0 + 10.0 * math.cos(elapsed * 0.03), 1)
            last_dht_t = now

        temp_c   = dht_cache_temp
        humidity = dht_cache_hum

        # Map DHT11 → Power + Water
        power_mw  = round(max(300.0, min(550.0, 350.0 + (temp_c - 20.0) * 5.0)), 1)
        water_psi = round(max(30.0,  min(95.0,  50.0 + humidity * 0.3)),  1)

        # ── Transmit ───────────────────────────────────────────────────────────
        send_udp("traffic", traffic_speed, "km/h")
        send_udp("power",   power_mw,      "MW")
        send_udp("water",   water_psi,     "PSI")
        send_udp("air",     air_aqi,       "AQI")
        tx_count += 4

        # ── Console ────────────────────────────────────────────────────────────
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
        time.sleep(0.1)

except KeyboardInterrupt:
    print("\n\n[SHUTDOWN] Engine stopped cleanly.")
    sock.close()
