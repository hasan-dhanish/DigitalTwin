#!/usr/bin/env python3
# ==============================================================================
#  QNX Real-Time Sensor Reader — Raspberry Pi 4 (BCM2711)
#  NO external GPIO library required. Uses /dev/mem direct register access.
#
#  Sensor Wiring (BCM pin numbering):
#    DHT11  (Temp/Humidity)    DATA  → GPIO 4  (Header Pin 7)
#    IR Sensor (Traffic)       OUT   → GPIO 17 (Header Pin 11)  [Active LOW]
#    MQ135  (Air Quality/Gas)  DO    → GPIO 27 (Header Pin 13)  [Active LOW]
#
#  Tested on: QNX Neutrino RTOS 7.x on Raspberry Pi 4 (4GB)
#
#  Usage:
#    python3 pi_sensor_reader.py <HOST_IP> [UDP_PORT]
#    python3 pi_sensor_reader.py 10.12.2.121
#    python3 pi_sensor_reader.py 10.12.2.121 9999
# ==============================================================================

import sys
import os
import time
import json
import socket
import threading
import math
import struct
import ctypes

# ------------------------------------------------------------------------------
#  Configuration
# ------------------------------------------------------------------------------

PIN_DHT11  = 4    # GPIO 4  → Header Pin 7   DHT11 DATA
PIN_IR     = 17   # GPIO 17 → Header Pin 11  IR sensor OUT (Active LOW)
PIN_MQ135  = 27   # GPIO 27 → Header Pin 13  MQ135 DO (Active LOW = gas alert)

DHT11_INTERVAL_SEC  = 2.0   # DHT11 spec: minimum 1s between reads, use 2s
IR_INTERVAL_SEC     = 0.02  # 50 Hz polling for vehicle edge detection
MQ135_INTERVAL_SEC  = 0.5   # 2 Hz gas alert check
UDP_INTERVAL_SEC    = 0.05  # 20 Hz UDP telemetry stream

HOST_PC_IP = sys.argv[1] if len(sys.argv) > 1 else "10.12.2.208"
UDP_PORT   = int(sys.argv[2]) if len(sys.argv) > 2 else 9999

# ==============================================================================
#  BCM2711 GPIO Driver — /dev/mem direct register access
#  Works on any OS that exposes /dev/mem including QNX Neutrino
# ==============================================================================

# BCM2711 (Raspberry Pi 4) GPIO peripheral base address
# NOTE: On Pi 4 the bus address is 0x7E200000 but physical is 0xFE200000
BCM2711_PERI_BASE  = 0xFE000000
GPIO_BASE          = BCM2711_PERI_BASE + 0x200000   # 0xFE200000
BLOCK_SIZE         = 4096

# GPIO Register offsets (each register is 32-bit / 4 bytes)
# Function Select (sets pin as INPUT=0b000 or OUTPUT=0b001)
GPFSEL0 = 0x00   # GPIO 0-9   function select
GPFSEL1 = 0x04   # GPIO 10-19 function select
GPFSEL2 = 0x08   # GPIO 20-29 function select

# Pin set / clear / level
GPSET0  = 0x1C   # Set   GPIO 0-31  HIGH
GPCLR0  = 0x28   # Clear GPIO 0-31  LOW
GPLEV0  = 0x34   # Level GPIO 0-31  (read)

# BCM2711 Pull-up/down (new registers — different from Pi 3!)
GPPUPPDN0 = 0xE4  # GPIO 0-15
GPPUPPDN1 = 0xE8  # GPIO 16-31
GPPUPPDN2 = 0xEC  # GPIO 32-47
GPPUPPDN3 = 0xF0  # GPIO 48-57

# Pull-up/down constants for BCM2711
PUD_OFF  = 0   # No pull
PUD_UP   = 1   # Pull-up
PUD_DOWN = 2   # Pull-down

# Pin direction constants
GPIO_INPUT  = 0
GPIO_OUTPUT = 1


class BCM2711GPIO:
    """
    Direct /dev/mem GPIO driver for Raspberry Pi 4 (BCM2711).
    Works on QNX, Linux, and any OS that exposes /dev/mem.

    Requires: root / mmap privileges (run with sudo on QNX)
    """

    def __init__(self):
        self._mmap = None
        self._gpio_base_ptr = None
        self._mem_fd = None
        self._available = False
        self._sim_pins = {}   # Simulation state when /dev/mem unavailable
        self._open()

    def _open(self):
        try:
            # Open /dev/mem for physical memory access
            self._mem_fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)

            # mmap the GPIO register block into our process address space
            import mmap
            self._mmap = mmap.mmap(
                self._mem_fd,
                BLOCK_SIZE,
                mmap.MAP_SHARED,
                mmap.PROT_READ | mmap.PROT_WRITE,
                offset=GPIO_BASE
            )
            self._available = True
            print("[GPIO] /dev/mem BCM2711 GPIO mapped successfully at 0xFE200000")
        except PermissionError:
            print()
            print("[GPIO] =====================================================")
            print("[GPIO] ERROR: /dev/mem access DENIED — not running as root.")
            print("[GPIO]")
            print("[GPIO] Fix: re-run with:")
            print("[GPIO]   sudo python3 pi_sensor_reader.py")
            print("[GPIO] =====================================================")
            print()
            sys.exit(1)   # Hard exit — do not silently simulate
        except FileNotFoundError:
            print("[GPIO] ERROR: /dev/mem not found. Are you on the Pi?")
            sys.exit(1)
        except Exception as e:
            print(f"[GPIO] ERROR: mmap failed ({e})")
            print("[GPIO] Try: sudo python3 pi_sensor_reader.py")
            sys.exit(1)

    def _reg_read(self, offset):
        """Read a 32-bit GPIO register at byte offset."""
        self._mmap.seek(offset)
        return struct.unpack("<I", self._mmap.read(4))[0]

    def _reg_write(self, offset, value):
        """Write a 32-bit value to GPIO register at byte offset."""
        self._mmap.seek(offset)
        self._mmap.write(struct.pack("<I", value & 0xFFFFFFFF))

    def set_direction(self, pin, direction):
        """
        Set GPIO pin direction: GPIO_INPUT (0) or GPIO_OUTPUT (1).
        Each GPFSEL register controls 10 pins (3 bits each).
        """
        if not self._available:
            return
        fsel_reg    = GPFSEL0 + (pin // 10) * 4
        bit_offset  = (pin % 10) * 3
        val = self._reg_read(fsel_reg)
        val &= ~(0b111 << bit_offset)        # Clear 3 bits for this pin
        val |=  (direction & 0b111) << bit_offset  # Set new direction
        self._reg_write(fsel_reg, val)

    def set_pull(self, pin, pud):
        """
        Set pull-up/down resistor for a pin (BCM2711 method).
        BCM2711 uses GPPUPPDN registers, NOT the old GPPUD+GPPUDCLK method.
        2 bits per pin: 00=off, 01=pull-up, 10=pull-down
        """
        if not self._available:
            return
        if pin < 16:
            reg = GPPUPPDN0
            shift = pin * 2
        elif pin < 32:
            reg = GPPUPPDN1
            shift = (pin - 16) * 2
        elif pin < 48:
            reg = GPPUPPDN2
            shift = (pin - 32) * 2
        else:
            reg = GPPUPPDN3
            shift = (pin - 48) * 2

        val = self._reg_read(reg)
        val &= ~(0b11 << shift)
        val |=  (pud  & 0b11) << shift
        self._reg_write(reg, val)

    def output_high(self, pin):
        """Set GPIO pin HIGH via GPSET0."""
        if not self._available:
            self._sim_pins[pin] = 1
            return
        self._reg_write(GPSET0, 1 << pin)

    def output_low(self, pin):
        """Set GPIO pin LOW via GPCLR0."""
        if not self._available:
            self._sim_pins[pin] = 0
            return
        self._reg_write(GPCLR0, 1 << pin)

    def input(self, pin):
        """Read current level of a GPIO pin from GPLEV0. Returns 0 or 1."""
        if not self._available:
            return self._sim_pins.get(pin, 1)   # Default HIGH (pull-up)
        val = self._reg_read(GPLEV0)
        return (val >> pin) & 1

    def cleanup(self):
        """Release mmap and close /dev/mem file descriptor."""
        try:
            if self._mmap:
                self._mmap.close()
            if self._mem_fd is not None:
                os.close(self._mem_fd)
            print("[GPIO] /dev/mem resources released cleanly.")
        except Exception:
            pass

    @property
    def available(self):
        return self._available


# Instantiate the global GPIO driver
gpio = BCM2711GPIO()


# ==============================================================================
#  GPIO Initialization — Configure all sensor pins
# ==============================================================================

def init_sensor_pins():
    """Configure IR and MQ135 as inputs with pull-ups. DHT11 managed dynamically."""
    if not gpio.available:
        print("[GPIO] Simulation mode — no hardware pin configuration needed.")
        return

    # IR Sensor: digital input with pull-up (Active LOW)
    gpio.set_direction(PIN_IR,    GPIO_INPUT)
    gpio.set_pull(PIN_IR,         PUD_UP)

    # MQ135: digital input with pull-up (Active LOW)
    gpio.set_direction(PIN_MQ135, GPIO_INPUT)
    gpio.set_pull(PIN_MQ135,      PUD_UP)

    # DHT11 pin starts as INPUT; DHT11Driver toggles it dynamically
    gpio.set_direction(PIN_DHT11, GPIO_INPUT)
    gpio.set_pull(PIN_DHT11,      PUD_UP)

    print(f"[GPIO] Pins initialized: DHT11=GPIO{PIN_DHT11}, IR=GPIO{PIN_IR}, MQ135=GPIO{PIN_MQ135}")


# ==============================================================================
#  DHT11 Pure-Python Bit-Bang Driver (BCM2711 /dev/mem)
# ==============================================================================

class DHT11Driver:
    """
    DHT11 single-wire protocol implemented via direct /dev/mem register access.

    Protocol timing:
      START:  Host pulls LOW >= 18ms, then releases (HIGH)
      ACK:    DHT pulls LOW ~80us, then HIGH ~80us
      BITS:   40 bits — LOW ~50us then:
                HIGH ~26-28us = 0
                HIGH ~70us    = 1
      DATA:   [8-bit RH int][8-bit RH dec][8-bit T int][8-bit T dec][8-bit checksum]
    """

    MAX_RETRIES = 3

    def __init__(self, pin):
        self.pin = pin
        self._last_temp = None
        self._last_hum  = None

    def _wait_level(self, expected, timeout_us):
        """Busy-wait for pin to reach expected level (0 or 1). Returns elapsed us."""
        deadline = time.monotonic() + timeout_us / 1_000_000
        while gpio.input(self.pin) != expected:
            if time.monotonic() >= deadline:
                return -1
        return 0

    def _read_raw(self):
        """Execute one full DHT11 read sequence. Returns (temp, hum) or (None, None)."""
        pin = self.pin

        # === STEP 1: Send start signal — drive LOW for 18ms ===
        gpio.set_direction(pin, GPIO_OUTPUT)
        gpio.output_low(pin)
        time.sleep(0.018)          # 18 ms start pulse

        # Release line — DHT takes control
        gpio.output_high(pin)
        gpio.set_direction(pin, GPIO_INPUT)
        gpio.set_pull(pin, PUD_UP)
        time.sleep(0.00004)        # Wait 40us before sampling

        # === STEP 2: DHT ACK — LOW ~80us ===
        if self._wait_level(0, 100) < 0:
            return None, None      # No ACK LOW

        # === STEP 3: DHT ACK HIGH — ~80us ===
        if self._wait_level(1, 100) < 0:
            return None, None      # No ACK HIGH

        if self._wait_level(0, 100) < 0:
            return None, None      # ACK HIGH didn't end

        # === STEP 4: Read 40 data bits ===
        bits = []
        for _ in range(40):
            # Wait for bit HIGH pulse start (rising edge after 50us LOW)
            if self._wait_level(1, 80) < 0:
                return None, None

            t_start = time.monotonic()

            # Wait for bit HIGH pulse end (falling edge)
            if self._wait_level(0, 100) < 0:
                return None, None

            pulse_us = (time.monotonic() - t_start) * 1_000_000
            bits.append(1 if pulse_us >= 40 else 0)

        # === STEP 5: Decode 5 bytes ===
        bytes_data = []
        for byte_idx in range(5):
            val = 0
            for bit_idx in range(8):
                val = (val << 1) | bits[byte_idx * 8 + bit_idx]
            bytes_data.append(val)

        # === STEP 6: Verify checksum ===
        expected_cs = (bytes_data[0] + bytes_data[1] +
                       bytes_data[2] + bytes_data[3]) & 0xFF
        if expected_cs != bytes_data[4]:
            return None, None   # Checksum mismatch

        # Decode: integer + decimal parts
        hum  = bytes_data[0] + bytes_data[1] * 0.1
        temp = bytes_data[2] + bytes_data[3] * 0.1

        # Sanity check (DHT11 range: 0-50C, 20-90%RH)
        if not (0 <= temp <= 60) or not (0 <= hum <= 100):
            return None, None

        return round(temp, 1), round(hum, 1)

    def read(self):
        """
        Read DHT11 with up to MAX_RETRIES attempts.
        Returns cached last-good value on all failures.
        """
        if not gpio.available:
            # Simulation fallback
            t = time.monotonic()
            return round(28.0 + 4.0 * math.sin(t * 0.01), 1), \
                   round(62.0 + 8.0 * math.cos(t * 0.007), 1)

        for attempt in range(self.MAX_RETRIES):
            try:
                temp, hum = self._read_raw()
                if temp is not None:
                    self._last_temp = temp
                    self._last_hum  = hum
                    return temp, hum
            except Exception:
                pass
            if attempt < self.MAX_RETRIES - 1:
                time.sleep(0.05)  # 50ms between retries

        # Return last good reading (or None on first failure)
        return self._last_temp, self._last_hum


# ==============================================================================
#  Shared Telemetry State
# ==============================================================================

_lock  = threading.Lock()
_state = {
    # Environmental — DHT11
    "temperature_c":   None,
    "humidity_pct":    None,
    "dht_ok":          False,

    # Traffic — IR Sensor
    "vehicle_count":   0,
    "ir_blocked":      False,
    "traffic_speed":   0.0,

    # Air Quality — MQ135
    "gas_detected":    False,
    "gas_alert_count": 0,

    # Diagnostics
    "dht_errors":      0,
    "uptime_sec":      0.0,
}

_start_time = time.monotonic()
dht11 = DHT11Driver(PIN_DHT11)


# ==============================================================================
#  Sensor Threads
# ==============================================================================

def thread_dht11():
    """Reads DHT11 temperature & humidity every 2 seconds."""
    print(f"[THREAD-DHT11]  GPIO{PIN_DHT11}  @ 0.5 Hz  — Temp + Humidity")
    while True:
        temp, hum = dht11.read()
        with _lock:
            if temp is not None:
                _state["temperature_c"] = temp
                _state["humidity_pct"]  = hum
                _state["dht_ok"]        = True
            else:
                _state["dht_errors"]   += 1
                _state["dht_ok"]        = False
        time.sleep(DHT11_INTERVAL_SEC)


def thread_ir_sensor():
    """
    Monitors IR OUT pin for vehicle detections at 50 Hz.
    Active LOW: pin goes LOW when IR beam is interrupted (vehicle present).
    Tracks falling edges to count vehicles and estimate traffic speed.
    """
    print(f"[THREAD-IR]     GPIO{PIN_IR}  @ 50 Hz  — Vehicle Detection")

    last_level    = 1           # Start assuming beam clear (HIGH)
    vehicle_times = []          # Timestamps of recent detections
    WINDOW_SEC    = 30          # Rolling window for speed estimation

    while True:
        level = gpio.input(PIN_IR) if gpio.available else _sim_ir()

        if level == 0 and last_level == 1:
            # Falling edge → vehicle detected
            now = time.monotonic()
            vehicle_times.append(now)

            # Prune old entries outside rolling window
            cutoff = now - WINDOW_SEC
            vehicle_times = [t for t in vehicle_times if t >= cutoff]

            # Speed estimate: vehicles/min * 2.0 = km/h proxy
            vpm   = (len(vehicle_times) / WINDOW_SEC) * 60.0
            speed = round(min(120.0, max(10.0, vpm * 2.0)), 1)

            with _lock:
                _state["vehicle_count"] += 1
                _state["ir_blocked"]     = True
                _state["traffic_speed"]  = speed

        elif level == 1 and last_level == 0:
            # Rising edge → vehicle has passed
            with _lock:
                _state["ir_blocked"] = False

        last_level = level
        time.sleep(IR_INTERVAL_SEC)


def thread_mq135():
    """
    Monitors MQ135 digital output pin at 2 Hz.
    Active LOW: pin goes LOW when gas concentration exceeds the
    onboard potentiometer threshold (adjusted via MQ135 trimmer).
    """
    print(f"[THREAD-MQ135]  GPIO{PIN_MQ135}  @ 2 Hz   — Gas / Smoke Alert")

    prev_alert = False
    while True:
        if gpio.available:
            alert = gpio.input(PIN_MQ135) == 0  # Active LOW
        else:
            # Simulation: brief spike every ~30s
            alert = (int(time.monotonic()) % 30) < 2

        with _lock:
            _state["gas_detected"] = alert
            if alert and not prev_alert:
                _state["gas_alert_count"] += 1  # Count new alert events only

        prev_alert = alert
        time.sleep(MQ135_INTERVAL_SEC)


# Simulation helper for IR when no hardware
def _sim_ir():
    """Simulation-mode IR: always returns 1 (beam clear). No fake vehicles."""
    return 1   # Beam always clear in simulation — count only real hardware edges


# ==============================================================================
#  UDP Publisher Thread — 20 Hz JSON Telemetry Stream
# ==============================================================================

def thread_udp_publisher(sock):
    """
    Publishes sensor data as UDP JSON packets at 20 Hz.
    Sends 4 stream packets per cycle:
      'environment' — DHT11 temp + humidity
      'traffic'     — IR speed + vehicle count
      'air'         — MQ135 gas alert mapped to AQI
      'pi_sensors'  — full combined state snapshot
    """
    print(f"[THREAD-UDP]    Streaming -> {HOST_PC_IP}:{UDP_PORT}  @ 20 Hz")

    while True:
        with _lock:
            snap = dict(_state)

        now = time.monotonic()
        uptime = round(now - _start_time, 1)
        ts     = time.time()  # Wall clock for timestamps

        packets = [
            {
                "stream":       "environment",
                "temperature":  snap["temperature_c"],
                "humidity":     snap["humidity_pct"],
                "temp_unit":    "C",
                "hum_unit":     "%RH",
                "dht_ok":       snap["dht_ok"],
                "ts":           ts
            },
            {
                "stream":        "traffic",
                "value":         snap["traffic_speed"],
                "unit":          "km/h",
                "vehicle_count": snap["vehicle_count"],
                "beam_blocked":  snap["ir_blocked"],
                "ts":            ts
            },
            {
                "stream":       "air",
                # Digital MQ135 maps to AQI: ALERT -> 85 (Unhealthy), CLEAR -> 22 (Good)
                "value":        85.0 if snap["gas_detected"] else 22.0,
                "unit":         "AQI",
                "gas_alert":    snap["gas_detected"],
                "alert_count":  snap["gas_alert_count"],
                "ts":           ts
            },
            {
                "stream":           "pi_sensors",
                "temperature_c":    snap["temperature_c"],
                "humidity_pct":     snap["humidity_pct"],
                "traffic_speed":    snap["traffic_speed"],
                "vehicle_count":    snap["vehicle_count"],
                "gas_alert":        snap["gas_detected"],
                "gas_alert_count":  snap["gas_alert_count"],
                "dht_errors":       snap["dht_errors"],
                "uptime_sec":       uptime,
                "ts":               ts
            }
        ]

        for pkt in packets:
            try:
                sock.sendto(json.dumps(pkt).encode("utf-8"), (HOST_PC_IP, UDP_PORT))
            except Exception:
                pass

        time.sleep(UDP_INTERVAL_SEC)


# ==============================================================================
#  Console Status Renderer — 5 Hz live display
# ==============================================================================

BANNER = """\
+==============================================================================+
|   QNX REAL-TIME SENSOR READER  -  Raspberry Pi 4 (BCM2711 /dev/mem)        |
|   DHT11 -> GPIO 4  |  IR Sensor -> GPIO 17  |  MQ135 -> GPIO 27            |
+==============================================================================+"""

def thread_console():
    """Renders live single-line status to stdout at 5 Hz."""
    while True:
        with _lock:
            snap = dict(_state)

        uptime = round(time.monotonic() - _start_time, 0)
        hw_tag = "HARDWARE /dev/mem" if gpio.available else "SIMULATION"

        temp_s  = f"{snap['temperature_c']:5.1f}C" if snap["temperature_c"] is not None else "  N/A C"
        hum_s   = f"{snap['humidity_pct']:5.1f}%"  if snap["humidity_pct"]  is not None else "   N/A%"
        speed_s = f"{snap['traffic_speed']:6.1f}km/h"
        gas_s   = "!! GAS ALERT !!" if snap["gas_detected"] else "  CLEAN AIR  "

        sys.stdout.write(
            f"\r[{hw_tag}] "
            f"Temp:{temp_s} Hum:{hum_s} | "
            f"Traffic:{speed_s} Vehicles:{snap['vehicle_count']:4d} | "
            f"Air:{gas_s} Alerts:{snap['gas_alert_count']:3d} | "
            f"Up:{uptime:.0f}s   "
        )
        sys.stdout.flush()
        time.sleep(0.2)


# ==============================================================================
#  Main Entry Point
# ==============================================================================

def main():
    os.system("clear")
    print(BANNER)
    print()
    print(f"  GPIO Backend : {'BCM2711 /dev/mem (hardware)' if gpio.available else 'SIMULATION (no /dev/mem access)'}")
    print(f"  DHT11        : GPIO {PIN_DHT11}  (Header Pin 7 ) — Temp + Humidity  @ 0.5 Hz")
    print(f"  IR Sensor    : GPIO {PIN_IR} (Header Pin 11) — Vehicle Detection @ 50 Hz")
    print(f"  MQ135        : GPIO {PIN_MQ135} (Header Pin 13) — Gas / Air Alert  @ 2 Hz")
    print(f"  UDP Target   : {HOST_PC_IP}:{UDP_PORT}  @ 20 Hz")
    print()

    if not gpio.available:
        print("  [!] WARNING: No /dev/mem access. Did you run with sudo?")
        print("  [!]          Running in simulation mode.\n")

    init_sensor_pins()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    threads = [
        threading.Thread(target=thread_dht11,                        daemon=True, name="DHT11"),
        threading.Thread(target=thread_ir_sensor,                    daemon=True, name="IR-Traffic"),
        threading.Thread(target=thread_mq135,                        daemon=True, name="MQ135-Air"),
        threading.Thread(target=thread_udp_publisher, args=(sock,),  daemon=True, name="UDP-Pub"),
        threading.Thread(target=thread_console,                       daemon=True, name="Console"),
    ]

    for t in threads:
        t.start()

    print(f"[READY] {len(threads)} threads running. Press Ctrl+C to stop.\n")

    try:
        while True:
            # Print periodic summary every 10 seconds
            time.sleep(10.0)
            with _lock:
                snap = dict(_state)
            uptime = round(time.monotonic() - _start_time, 0)
            print(
                f"\n[SUMMARY @{uptime:.0f}s]"
                f"  Temp={snap['temperature_c']}C"
                f"  Hum={snap['humidity_pct']}%"
                f"  Traffic={snap['traffic_speed']}km/h"
                f"  Vehicles={snap['vehicle_count']}"
                f"  Gas={'ALERT' if snap['gas_detected'] else 'OK'}"
                f"  GasAlerts={snap['gas_alert_count']}"
                f"  DHTerrs={snap['dht_errors']}"
            )
    except KeyboardInterrupt:
        print("\n\n[SHUTDOWN] Stopping...")
        gpio.cleanup()
        sock.close()
        print("[DONE] GPIO released cleanly.")


if __name__ == "__main__":
    main()
