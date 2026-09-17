#!/usr/bin/env python3
# ==============================================================================
#  QNX Real-Time Sensor Reader — Raspberry Pi 4 (BCM2711)
#
#  GPIO Method: ctypes + libc mmap(MAP_PHYS) — QNX Neutrino native approach
#               NO /dev/mem, NO pigpiod, NO Linux libraries needed.
#               MAP_PHYS maps physical hardware registers directly into
#               process address space (QNX equivalent of /dev/mem).
#
#  Sensor Wiring (BCM numbering):
#    DHT11  (Temp/Humidity)    DATA → GPIO 4  (Header Pin 7)
#    IR Sensor (Traffic)       OUT  → GPIO 17 (Header Pin 11) [Active LOW]
#    MQ135  (Air Quality/Gas)  DO   → GPIO 27 (Header Pin 13) [Active LOW]
#
#  Usage:
#    sudo python3 pi_sensor_reader.py <HOST_IP> [UDP_PORT]
#    sudo python3 pi_sensor_reader.py 10.12.2.208
# ==============================================================================

import sys
import os
import time
import json
import socket
import threading
import math
import ctypes
import ctypes.util

# ------------------------------------------------------------------------------
#  Configuration
# ------------------------------------------------------------------------------

PIN_DHT11  = 4    # GPIO 4  — DHT11 DATA
PIN_IR     = 17   # GPIO 17 — IR Sensor OUT  (Active LOW: 0 = beam blocked)
PIN_MQ135  = 27   # GPIO 27 — MQ135 DO       (Active LOW: 0 = gas detected)

DHT11_INTERVAL_SEC  = 2.0    # Min stable interval for DHT11 (spec: >=1s)
IR_INTERVAL_SEC     = 0.02   # 50 Hz — edge detection for vehicles
MQ135_INTERVAL_SEC  = 0.5    # 2 Hz  — gas threshold monitoring
UDP_INTERVAL_SEC    = 0.05   # 20 Hz — UDP telemetry stream

HOST_PC_IP = sys.argv[1] if len(sys.argv) > 1 else "10.12.2.208"
UDP_PORT   = int(sys.argv[2]) if len(sys.argv) > 2 else 9999

# ==============================================================================
#  BCM2711 Physical Register Map (Raspberry Pi 4)
# ==============================================================================

BCM2711_GPIO_BASE = 0xFE200000   # GPIO peripheral physical base address
BLOCK_SIZE        = 4096         # One page covers all GPIO registers

# Register byte offsets from GPIO_BASE
GPFSEL0   = 0x00   # Function select: GPIO 0-9   (3 bits each, 10 per register)
GPFSEL1   = 0x04   # Function select: GPIO 10-19
GPFSEL2   = 0x08   # Function select: GPIO 20-29
GPSET0    = 0x1C   # Output set:  write 1 -> drive HIGH
GPCLR0    = 0x28   # Output clear: write 1 -> drive LOW
GPLEV0    = 0x34   # Pin level: read current state of GPIO 0-31

# BCM2711 uses NEW pull-up/down registers (not the old GPPUD+GPPUDCLK)
GPPUPPDN0 = 0xE4   # Pull control: GPIO 0-15  (2 bits each)
GPPUPPDN1 = 0xE8   # Pull control: GPIO 16-31

# Direction and pull constants
GPIO_INPUT  = 0b000
GPIO_OUTPUT = 0b001
PUD_OFF     = 0b00
PUD_UP      = 0b01
PUD_DOWN    = 0b10

# ==============================================================================
#  QNX Physical Memory Driver  (ctypes + libc mmap with MAP_PHYS)
# ==============================================================================

# QNX Neutrino mmap() flags
PROT_READ  = 0x1
PROT_WRITE = 0x2
MAP_SHARED = 0x0001
MAP_PHYS   = 0x10000    # QNX-specific: map physical address without a file fd
NOFD       = -1          # Used instead of a file descriptor with MAP_PHYS

MAP_FAILED_VALUE = ctypes.c_void_p(-1).value


class BCM2711GPIO:
    """
    Raspberry Pi 4 GPIO driver using QNX-native physical memory mapping.

    QNX does not have /dev/mem. Instead, mmap() is called with MAP_PHYS
    to directly map physical hardware registers into process address space.
    This is the standard QNX Neutrino pattern for MMIO device access.

    Requires: root privileges (to map physical memory)
    """

    def __init__(self):
        self._gpio_ptr = None    # ctypes pointer to mapped GPIO register block
        self._libc     = None
        self._available = False
        self._open()

    def _open(self):
        """Map BCM2711 GPIO registers via libc mmap(MAP_PHYS)."""
        try:
            # Load the C runtime library (libc.so on QNX)
            lib_name = ctypes.util.find_library("c")
            if lib_name is None:
                lib_name = "libc.so"    # QNX default
            self._libc = ctypes.CDLL(lib_name)

            # Configure mmap() signature explicitly for correct calling convention
            self._libc.mmap.restype  = ctypes.c_void_p
            self._libc.mmap.argtypes = [
                ctypes.c_void_p,   # addr hint (NULL = let OS choose)
                ctypes.c_size_t,   # length
                ctypes.c_int,      # prot  (PROT_READ | PROT_WRITE)
                ctypes.c_int,      # flags (MAP_SHARED | MAP_PHYS on QNX)
                ctypes.c_int,      # fd    (NOFD = -1 with MAP_PHYS)
                ctypes.c_ulong,    # offset = physical address of GPIO block
            ]

            ptr = self._libc.mmap(
                None,
                BLOCK_SIZE,
                PROT_READ | PROT_WRITE,
                MAP_SHARED | MAP_PHYS,
                NOFD,
                BCM2711_GPIO_BASE
            )

            if ptr == MAP_FAILED_VALUE or ptr is None:
                # Try Linux /dev/mem as secondary fallback (Raspbian etc.)
                self._open_devmem()
                return

            self._gpio_ptr = ptr
            self._available = True
            print(f"[GPIO] BCM2711 registers mapped via mmap(MAP_PHYS) @ 0x{BCM2711_GPIO_BASE:08X}")

        except Exception as e:
            print(f"[GPIO] ctypes mmap failed: {e}")
            # Try /dev/mem fallback (works on Linux/Raspbian)
            self._open_devmem()

    def _open_devmem(self):
        """Fallback: map GPIO via /dev/mem (Linux / Raspbian)."""
        dev_mem_path = "/dev/mem"
        try:
            import mmap as mmap_mod
            fd = os.open(dev_mem_path, os.O_RDWR | os.O_SYNC)
            self._mmap_obj = mmap_mod.mmap(
                fd, BLOCK_SIZE,
                mmap_mod.MAP_SHARED,
                mmap_mod.PROT_READ | mmap_mod.PROT_WRITE,
                offset=BCM2711_GPIO_BASE
            )
            os.close(fd)
            # Wrap as a ctypes array for consistent register access
            self._gpio_ptr = None           # Will use _mmap_obj path
            self._available = True
            self._use_devmem = True
            print("[GPIO] BCM2711 registers mapped via /dev/mem (Linux fallback)")
            return
        except PermissionError:
            print()
            print("[GPIO] ===================================================")
            print("[GPIO] ERROR: Permission denied.")
            print("[GPIO] Re-run with:  sudo python3 pi_sensor_reader.py")
            print("[GPIO] ===================================================")
            print()
            sys.exit(1)
        except FileNotFoundError:
            print("[GPIO] ERROR: Neither MAP_PHYS (QNX) nor /dev/mem (Linux) worked.")
            print("[GPIO] Make sure you are running ON the Raspberry Pi 4.")
            sys.exit(1)
        except Exception as e:
            print(f"[GPIO] ERROR: {e}")
            sys.exit(1)

        self._use_devmem = False

    # -------------------------------------------------------------------------
    #  Register Read / Write
    # -------------------------------------------------------------------------

    def _reg_read(self, offset):
        """Read 32-bit value from GPIO register at given byte offset."""
        if getattr(self, "_use_devmem", False):
            import struct
            self._mmap_obj.seek(offset)
            return struct.unpack("<I", self._mmap_obj.read(4))[0]
        else:
            addr = self._gpio_ptr + offset
            return ctypes.c_uint32.from_address(addr).value

    def _reg_write(self, offset, value):
        """Write 32-bit value to GPIO register at given byte offset."""
        if getattr(self, "_use_devmem", False):
            import struct
            self._mmap_obj.seek(offset)
            self._mmap_obj.write(struct.pack("<I", value & 0xFFFFFFFF))
        else:
            addr = self._gpio_ptr + offset
            ctypes.c_uint32.from_address(addr).value = value & 0xFFFFFFFF

    # -------------------------------------------------------------------------
    #  Pin Configuration
    # -------------------------------------------------------------------------

    def set_direction(self, pin, direction):
        """Set pin as GPIO_INPUT or GPIO_OUTPUT via GPFSEL registers."""
        fsel_offset = GPFSEL0 + (pin // 10) * 4
        bit_shift   = (pin % 10) * 3
        val = self._reg_read(fsel_offset)
        val &= ~(0b111 << bit_shift)              # Clear 3 bits
        val |=  (direction & 0b111) << bit_shift  # Set direction
        self._reg_write(fsel_offset, val)

    def set_pull(self, pin, pud):
        """
        Set pull-up/down for a pin using BCM2711 GPPUPPDN registers.
        BCM2711 (Pi 4) uses 2-bit fields: 00=off, 01=pull-up, 10=pull-down
        This is DIFFERENT from Pi 3 (which used GPPUD + GPPUDCLK sequence).
        """
        if pin < 16:
            reg, shift = GPPUPPDN0, pin * 2
        else:
            reg, shift = GPPUPPDN1, (pin - 16) * 2
        val = self._reg_read(reg)
        val &= ~(0b11 << shift)
        val |=  (pud & 0b11) << shift
        self._reg_write(reg, val)

    # -------------------------------------------------------------------------
    #  Pin I/O
    # -------------------------------------------------------------------------

    def output_high(self, pin):
        """Drive pin HIGH via GPSET0."""
        self._reg_write(GPSET0, 1 << pin)

    def output_low(self, pin):
        """Drive pin LOW via GPCLR0."""
        self._reg_write(GPCLR0, 1 << pin)

    def input(self, pin):
        """Read current pin level from GPLEV0. Returns 0 or 1."""
        return (self._reg_read(GPLEV0) >> pin) & 1

    def cleanup(self):
        """Unmap physical GPIO memory."""
        try:
            if getattr(self, "_use_devmem", False) and self._mmap_obj:
                self._mmap_obj.close()
            elif self._gpio_ptr and self._libc:
                self._libc.munmap(self._gpio_ptr, BLOCK_SIZE)
            print("[GPIO] Physical memory unmapped cleanly.")
        except Exception:
            pass

    @property
    def available(self):
        return self._available


# Instantiate global GPIO driver
gpio = BCM2711GPIO()


# ==============================================================================
#  Pin Initialization
# ==============================================================================

def init_sensor_pins():
    """Set IR and MQ135 as inputs with pull-ups. DHT11 managed dynamically."""
    gpio.set_direction(PIN_IR,    GPIO_INPUT);  gpio.set_pull(PIN_IR,    PUD_UP)
    gpio.set_direction(PIN_MQ135, GPIO_INPUT);  gpio.set_pull(PIN_MQ135, PUD_UP)
    gpio.set_direction(PIN_DHT11, GPIO_INPUT);  gpio.set_pull(PIN_DHT11, PUD_UP)
    print(f"[GPIO] Pins configured: DHT11=GPIO{PIN_DHT11}, IR=GPIO{PIN_IR}, MQ135=GPIO{PIN_MQ135}")


# ==============================================================================
#  DHT11 Bit-Bang Driver
# ==============================================================================

class DHT11Driver:
    """
    DHT11 single-wire protocol via direct BCM2711 register access.

    Timing (from DHT11 datasheet):
      Start:  Host LOW >= 18ms, release HIGH
      ACK:    DHT LOW ~80us, HIGH ~80us
      Bit 0:  LOW ~50us, HIGH ~26-28us
      Bit 1:  LOW ~50us, HIGH ~70us
      Data:   [8b RH int][8b RH dec][8b T int][8b T dec][8b checksum]
    """

    MAX_ATTEMPTS = 3

    def __init__(self, pin):
        self.pin = pin
        self._last_temp = None
        self._last_hum  = None

    def _wait_for(self, expected_level, timeout_us):
        """Busy-wait until pin reaches expected level. Returns True on success."""
        deadline = time.monotonic() + timeout_us / 1_000_000
        while gpio.input(self.pin) != expected_level:
            if time.monotonic() >= deadline:
                return False
        return True

    def _read_raw(self):
        pin = self.pin

        # --- Start: drive LOW >= 18ms ---
        gpio.set_direction(pin, GPIO_OUTPUT)
        gpio.output_low(pin)
        time.sleep(0.018)

        # Release — DHT takes control of the line
        gpio.output_high(pin)
        gpio.set_direction(pin, GPIO_INPUT)
        gpio.set_pull(pin, PUD_UP)

        # --- DHT ACK: LOW ~80us ---
        if not self._wait_for(0, 150):
            return None, None

        # --- DHT ACK: HIGH ~80us ---
        if not self._wait_for(1, 150):
            return None, None

        # --- End of ACK HIGH ---
        if not self._wait_for(0, 150):
            return None, None

        # --- Read 40 bits ---
        bits = []
        for _ in range(40):
            # Rising edge: start of HIGH pulse
            if not self._wait_for(1, 100):
                return None, None
            t_start = time.monotonic()

            # Falling edge: end of HIGH pulse
            if not self._wait_for(0, 150):
                return None, None
            pulse_us = (time.monotonic() - t_start) * 1_000_000

            bits.append(1 if pulse_us >= 40 else 0)

        # --- Decode 5 bytes ---
        raw = []
        for b in range(5):
            v = 0
            for i in range(8):
                v = (v << 1) | bits[b * 8 + i]
            raw.append(v)

        # --- Verify checksum ---
        if (raw[0] + raw[1] + raw[2] + raw[3]) & 0xFF != raw[4]:
            return None, None

        hum  = raw[0] + raw[1] * 0.1
        temp = raw[2] + raw[3] * 0.1

        if not (0 <= temp <= 60) or not (0 <= hum <= 100):
            return None, None

        return round(temp, 1), round(hum, 1)

    def read(self):
        """Read with retries. Returns cached value on failure."""
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                temp, hum = self._read_raw()
                if temp is not None:
                    self._last_temp, self._last_hum = temp, hum
                    return temp, hum
            except Exception:
                pass
            if attempt < self.MAX_ATTEMPTS - 1:
                time.sleep(0.05)
        return self._last_temp, self._last_hum


# ==============================================================================
#  Shared State
# ==============================================================================

_lock  = threading.Lock()
_state = {
    "temperature_c":   None,
    "humidity_pct":    None,
    "dht_ok":          False,
    "dht_errors":      0,

    "vehicle_count":   0,
    "ir_blocked":      False,
    "traffic_speed":   0.0,

    "gas_detected":    False,
    "gas_alert_count": 0,
}

_start = time.monotonic()
dht11  = DHT11Driver(PIN_DHT11)


# ==============================================================================
#  Sensor Threads
# ==============================================================================

def thread_dht11():
    print(f"[THREAD] DHT11  GPIO{PIN_DHT11}  @ 0.5 Hz  (Temp + Humidity)")
    while True:
        temp, hum = dht11.read()
        with _lock:
            if temp is not None:
                _state["temperature_c"] = temp
                _state["humidity_pct"]  = hum
                _state["dht_ok"]        = True
            else:
                _state["dht_errors"] += 1
                _state["dht_ok"]      = False
        time.sleep(DHT11_INTERVAL_SEC)


def thread_ir():
    print(f"[THREAD] IR     GPIO{PIN_IR}  @ 50 Hz (Vehicle Detection, Active LOW)")
    last_level    = 1
    vehicle_times = []
    WINDOW        = 30   # seconds for rolling speed estimate

    while True:
        level = gpio.input(PIN_IR)

        if level == 0 and last_level == 1:       # Falling edge = beam blocked
            now = time.monotonic()
            vehicle_times.append(now)
            vehicle_times = [t for t in vehicle_times if t >= now - WINDOW]

            vpm   = (len(vehicle_times) / WINDOW) * 60
            speed = round(min(120.0, max(10.0, vpm * 2.0)), 1)

            with _lock:
                _state["vehicle_count"] += 1
                _state["ir_blocked"]     = True
                _state["traffic_speed"]  = speed

        elif level == 1 and last_level == 0:     # Rising edge = beam cleared
            with _lock:
                _state["ir_blocked"] = False

        last_level = level
        time.sleep(IR_INTERVAL_SEC)


def thread_mq135():
    print(f"[THREAD] MQ135  GPIO{PIN_MQ135}  @ 2 Hz  (Gas Alert, Active LOW)")
    prev = False
    while True:
        alert = gpio.input(PIN_MQ135) == 0   # LOW = gas above threshold
        with _lock:
            _state["gas_detected"] = alert
            if alert and not prev:
                _state["gas_alert_count"] += 1
        prev = alert
        time.sleep(MQ135_INTERVAL_SEC)


# ==============================================================================
#  UDP Publisher — 20 Hz JSON stream
# ==============================================================================

def thread_udp(sock):
    print(f"[THREAD] UDP    -> {HOST_PC_IP}:{UDP_PORT}  @ 20 Hz")
    while True:
        with _lock:
            s = dict(_state)
        ts  = time.time()
        up  = round(time.monotonic() - _start, 1)

        pkts = [
            {"stream": "environment",
             "temperature": s["temperature_c"], "humidity": s["humidity_pct"],
             "temp_unit": "C", "hum_unit": "%RH", "dht_ok": s["dht_ok"], "ts": ts},

            {"stream": "traffic",
             "value": s["traffic_speed"], "unit": "km/h",
             "vehicle_count": s["vehicle_count"], "beam_blocked": s["ir_blocked"], "ts": ts},

            {"stream": "air",
             "value": 85.0 if s["gas_detected"] else 22.0, "unit": "AQI",
             "gas_alert": s["gas_detected"], "alert_count": s["gas_alert_count"], "ts": ts},

            {"stream": "pi_sensors",
             "temperature_c": s["temperature_c"], "humidity_pct": s["humidity_pct"],
             "traffic_speed": s["traffic_speed"], "vehicle_count": s["vehicle_count"],
             "gas_alert": s["gas_detected"], "gas_alert_count": s["gas_alert_count"],
             "dht_errors": s["dht_errors"], "uptime_sec": up, "ts": ts},
        ]

        for p in pkts:
            try:
                sock.sendto(json.dumps(p).encode(), (HOST_PC_IP, UDP_PORT))
            except Exception:
                pass

        time.sleep(UDP_INTERVAL_SEC)


# ==============================================================================
#  Console Renderer
# ==============================================================================

def thread_console():
    while True:
        with _lock:
            s = dict(_state)
        up = round(time.monotonic() - _start)

        t_s = f"{s['temperature_c']:5.1f}C" if s["temperature_c"] else "  N/A C"
        h_s = f"{s['humidity_pct']:5.1f}%"  if s["humidity_pct"]  else "   N/A%"
        g_s = "!! GAS ALERT !!" if s["gas_detected"] else "  CLEAN AIR  "
        dht_s = "OK" if s["dht_ok"] else f"ERR({s['dht_errors']})"

        sys.stdout.write(
            f"\r[HARDWARE] "
            f"Temp:{t_s} Hum:{h_s} [DHT:{dht_s}] | "
            f"Traffic:{s['traffic_speed']:6.1f}km/h Vehicles:{s['vehicle_count']:4d} | "
            f"Air:{g_s} Alerts:{s['gas_alert_count']:3d} | "
            f"Up:{up}s   "
        )
        sys.stdout.flush()
        time.sleep(0.2)


# ==============================================================================
#  Main
# ==============================================================================

BANNER = """\
+==============================================================================+
|  QNX SENSOR READER  -  Raspberry Pi 4 BCM2711  (ctypes MAP_PHYS / /dev/mem)|
|  DHT11 -> GPIO 4   |   IR Sensor -> GPIO 17   |   MQ135 -> GPIO 27         |
+==============================================================================+"""

def main():
    os.system("clear")
    print(BANNER)
    print()
    print(f"  GPIO Method  : {'QNX MAP_PHYS' if not getattr(gpio, '_use_devmem', False) else 'Linux /dev/mem'}")
    print(f"  DHT11        : GPIO {PIN_DHT11}  (Pin 7)   Temp + Humidity  @ 0.5 Hz")
    print(f"  IR Sensor    : GPIO {PIN_IR} (Pin 11)  Vehicle Detection @ 50 Hz")
    print(f"  MQ135        : GPIO {PIN_MQ135} (Pin 13)  Gas / Smoke Alert @ 2 Hz")
    print(f"  UDP Target   : {HOST_PC_IP}:{UDP_PORT}  @ 20 Hz")
    print()

    init_sensor_pins()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    threads = [
        threading.Thread(target=thread_dht11, daemon=True, name="DHT11"),
        threading.Thread(target=thread_ir,    daemon=True, name="IR"),
        threading.Thread(target=thread_mq135, daemon=True, name="MQ135"),
        threading.Thread(target=thread_udp,   args=(sock,), daemon=True, name="UDP"),
        threading.Thread(target=thread_console, daemon=True, name="Console"),
    ]
    for t in threads:
        t.start()

    print(f"[READY] {len(threads)} threads running. Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(10)
            with _lock:
                s = dict(_state)
            up = round(time.monotonic() - _start)
            print(
                f"\n[SUMMARY @{up}s]  "
                f"Temp={s['temperature_c']}C  Hum={s['humidity_pct']}%  "
                f"Traffic={s['traffic_speed']}km/h  Vehicles={s['vehicle_count']}  "
                f"Gas={'ALERT' if s['gas_detected'] else 'OK'}  "
                f"DHTerrs={s['dht_errors']}"
            )
    except KeyboardInterrupt:
        print("\n\n[SHUTDOWN] Stopping...")
        gpio.cleanup()
        sock.close()
        print("[DONE] All resources released.")


if __name__ == "__main__":
    main()
