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
IR_INTERVAL_SEC     = 0.01   # 100 Hz — high-resolution edge detection for vehicle transit timing
MQ135_INTERVAL_SEC  = 0.5    # 2 Hz   — gas threshold monitoring
UDP_INTERVAL_SEC    = 0.05   # 20 Hz  — UDP telemetry stream

HOST_PC_IP = sys.argv[1] if len(sys.argv) > 1 else "10.12.2.208"
UDP_PORT   = int(sys.argv[2]) if len(sys.argv) > 2 else 9999
MQTT_PORT  = 1883

# ==============================================================================
#  Pure Python Zero-Dependency MQTT 3.1.1 Publisher for QNX Neutrino RTOS
# ==============================================================================
class MiniMQTTClient:
    def __init__(self, host, port=1883, client_id="QNX_Pi_Publisher"):
        self.host = host
        self.port = port
        self.cid = client_id.encode('utf-8')
        self.sock = None
        self.connected = False
        self.lock = threading.Lock()

    def connect(self):
        try:
            if self.sock:
                try: self.sock.close()
                except Exception: pass
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(1.5)
            self.sock.connect((self.host, self.port))
            # MQTT 3.1.1 CONNECT variable header: Protocol Name 'MQTT', Level 4, Clean Session (0x02), Keepalive 60s
            vh = b'\x00\x04MQTT\x04\x02\x00\x3c'
            payload = bytes([len(self.cid) >> 8, len(self.cid) & 0xff]) + self.cid
            body = vh + payload
            rem = self._encode_len(len(body))
            self.sock.sendall(b'\x10' + rem + body)
            ack = self.sock.recv(4)
            if len(ack) >= 4 and ack[0] == 0x20 and ack[1] == 0x02 and ack[3] == 0x00:
                self.connected = True
                return True
        except Exception:
            self.connected = False
        return False

    def _encode_len(self, length):
        encoded = bytearray()
        while True:
            digit = length % 128
            length //= 128
            if length > 0:
                digit |= 128
            encoded.append(digit)
            if length <= 0:
                break
        return bytes(encoded)

    def publish(self, topic, msg):
        with self.lock:
            if not self.connected:
                if not self.connect():
                    return False
            try:
                tb = topic.encode('utf-8')
                mb = msg.encode('utf-8') if isinstance(msg, str) else msg
                body = bytes([len(tb) >> 8, len(tb) & 0xff]) + tb + mb
                rem = self._encode_len(len(body))
                self.sock.sendall(b'\x30' + rem + body)
                return True
            except Exception:
                self.connected = False
                try: self.sock.close()
                except Exception: pass
                return False

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
#  QNX Physical Memory Driver  (ctypes + ThreadCtl + mmap_device_memory / MAP_PHYS)
# ==============================================================================

# QNX Neutrino constants
_NTO_TCTL_IO      = 1       # sys/neutrino.h: grant hardware I/O privileges to thread
_NTO_TCTL_IO_PRIV = 2

PROT_READ    = 0x01
PROT_WRITE   = 0x02
PROT_NOCACHE = 0x800        # sys/mman.h: disable caching for MMIO registers
MAP_SHARED   = 0x0001
MAP_PHYS     = 0x00010000   # sys/mman.h: map physical memory
NOFD         = -1


class BCM2711GPIO:
    """
    Raspberry Pi 4 GPIO driver for QNX Neutrino RTOS (and Linux fallback).

    On QNX Neutrino:
      1. Calls ThreadCtl(_NTO_TCTL_IO, 0) to grant hardware I/O privilege.
      2. Calls mmap_device_memory() (or mmap with MAP_PHYS) to map BCM2711
         GPIO peripheral registers (0xFE200000) directly into process space.
      3. If direct MMIO is unavailable, tries QNX's 'rpi_gpio' Python module.

    On Linux / Raspbian:
      Falls back to /dev/gpiomem or /dev/mem.
    """

    def __init__(self):
        self._gpio_ptr      = None
        self._libc          = None
        self._available     = False
        self._use_devmem    = False
        self._use_rpi_gpio  = False
        self._rpi_gpio      = None
        self._method        = "None"
        self._mapped_base   = None
        self._open()

    @staticmethod
    def _is_valid_ptr(ptr):
        """Check if pointer returned from mmap/mmap_device_memory is valid."""
        if ptr is None:
            return False
        val = ptr if isinstance(ptr, int) else ptr.value
        if val is None or val == 0 or val == -1:
            return False
        # Invert checking for MAP_FAILED ((void*)-1) across 32-bit and 64-bit
        bits = ctypes.sizeof(ctypes.c_void_p) * 8
        if val == ((1 << bits) - 1) or val == 0xFFFFFFFF or val == 0xFFFFFFFFFFFFFFFF:
            return False
        return True

    @staticmethod
    def _to_uintptr(ptr):
        """Convert a ctypes pointer or int to an unsigned integer address."""
        val = ptr if isinstance(ptr, int) else ptr.value
        bits = ctypes.sizeof(ctypes.c_void_p) * 8
        return val & ((1 << bits) - 1)

    def _open(self):
        """Initialize GPIO access using QNX native calls or fallbacks."""
        print("[GPIO] Initializing hardware interface...")

        # 1. Load libc
        libc = None
        for name in [None, "libc.so", "libc.so.5", "libc.so.6", "libc.so.7", "libc.so.8"]:
            try:
                libc = ctypes.CDLL(name, use_errno=True) if name else ctypes.CDLL(None, use_errno=True)
                if hasattr(libc, "mmap_device_memory") or hasattr(libc, "mmap"):
                    print(f"[GPIO] Loaded C library: {name or 'process default'}")
                    break
            except Exception:
                continue

        self._libc = libc

        # 2. On QNX: request I/O hardware privileges via ThreadCtl
        if self._libc and hasattr(self._libc, "ThreadCtl"):
            for cmd, cname in [(_NTO_TCTL_IO, "_NTO_TCTL_IO"), (_NTO_TCTL_IO_PRIV, "_NTO_TCTL_IO_PRIV")]:
                try:
                    self._libc.ThreadCtl.argtypes = [ctypes.c_int, ctypes.c_void_p]
                    self._libc.ThreadCtl.restype  = ctypes.c_int
                    ret = self._libc.ThreadCtl(cmd, None)
                    if ret == 0:
                        print(f"[GPIO] QNX ThreadCtl({cname}): I/O privilege granted.")
                        break
                    else:
                        err = ctypes.get_errno()
                        print(f"[GPIO] QNX ThreadCtl({cname}) returned {ret}, errno={err} ({os.strerror(err)})")
                except Exception as e:
                    print(f"[GPIO] ThreadCtl({cname}) failed: {e}")

        # 3. Attempt QNX mmap_device_memory()
        prot = PROT_READ | PROT_WRITE | PROT_NOCACHE
        candidate_bases = [BCM2711_GPIO_BASE, 0x3F200000]

        if self._libc and hasattr(self._libc, "mmap_device_memory"):
            try:
                self._libc.mmap_device_memory.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_size_t,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_uint64,
                ]
                self._libc.mmap_device_memory.restype = ctypes.c_void_p

                for base in candidate_bases:
                    ptr = self._libc.mmap_device_memory(None, BLOCK_SIZE, prot, 0, base)
                    if self._is_valid_ptr(ptr):
                        self._gpio_ptr    = self._to_uintptr(ptr)
                        self._available   = True
                        self._mapped_base = base
                        self._method      = "QNX mmap_device_memory"
                        print(f"[GPIO] SUCCESS: BCM2711 mapped via mmap_device_memory() @ 0x{base:08X} (vaddr=0x{self._gpio_ptr:X})")
                        return
                    else:
                        err = ctypes.get_errno()
                        print(f"[GPIO] mmap_device_memory(0x{base:08X}) failed: errno={err} ({os.strerror(err)})")
            except Exception as e:
                print(f"[GPIO] mmap_device_memory exception: {e}")

        # 4. Attempt QNX mmap(MAP_PHYS)
        if self._libc and hasattr(self._libc, "mmap"):
            try:
                self._libc.mmap.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_size_t,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_uint64,
                ]
                self._libc.mmap.restype = ctypes.c_void_p

                for base in candidate_bases:
                    ptr = self._libc.mmap(None, BLOCK_SIZE, prot, MAP_SHARED | MAP_PHYS, NOFD, base)
                    if self._is_valid_ptr(ptr):
                        self._gpio_ptr    = self._to_uintptr(ptr)
                        self._available   = True
                        self._mapped_base = base
                        self._method      = "QNX mmap(MAP_PHYS)"
                        print(f"[GPIO] SUCCESS: BCM2711 mapped via mmap(MAP_PHYS) @ 0x{base:08X} (vaddr=0x{self._gpio_ptr:X})")
                        return
                    else:
                        err = ctypes.get_errno()
                        print(f"[GPIO] mmap(MAP_PHYS, 0x{base:08X}) failed: errno={err} ({os.strerror(err)})")
            except Exception as e:
                print(f"[GPIO] mmap(MAP_PHYS) exception: {e}")

        # 5. Check if QNX BSP 'rpi_gpio' module is installed
        for mod_name in ["rpi_gpio", "RPi.GPIO"]:
            try:
                mod = __import__(mod_name)
                self._rpi_gpio     = mod
                self._rpi_gpio.setmode(self._rpi_gpio.BCM)
                self._use_rpi_gpio = True
                self._available    = True
                self._method       = f"Python module '{mod_name}'"
                print(f"[GPIO] SUCCESS: Using '{mod_name}' module driver.")
                return
            except Exception:
                pass

        # 6. Fallback: Linux /dev/gpiomem or /dev/mem
        for dev_path in ["/dev/gpiomem", "/dev/mem"]:
            if os.path.exists(dev_path):
                try:
                    import mmap as mmap_mod
                    fd = os.open(dev_path, os.O_RDWR | os.O_SYNC)
                    offset = 0 if dev_path == "/dev/gpiomem" else BCM2711_GPIO_BASE
                    self._mmap_obj   = mmap_mod.mmap(fd, BLOCK_SIZE, mmap_mod.MAP_SHARED,
                                                     mmap_mod.PROT_READ | mmap_mod.PROT_WRITE,
                                                     offset=offset)
                    os.close(fd)
                    self._use_devmem  = True
                    self._available   = True
                    self._method      = f"Linux {dev_path}"
                    print(f"[GPIO] SUCCESS: BCM2711 mapped via {dev_path}")
                    return
                except Exception as e:
                    print(f"[GPIO] {dev_path} open failed: {e}")

        # 7. Complete Diagnostic Report on Failure
        print()
        print("[GPIO] ================= DIAGNOSTIC REPORT =================")
        print(f"[GPIO] OS platform   : {sys.platform}")
        if hasattr(os, "uname"):
            print(f"[GPIO] OS uname      : {os.uname()}")
        if hasattr(os, "geteuid"):
            euid = os.geteuid()
            print(f"[GPIO] Effective UID : {euid} {'(ROOT)' if euid == 0 else '(NON-ROOT — run with sudo!)'}")
        if os.path.exists("/dev"):
            try:
                devs = [f for f in os.listdir("/dev") if any(k in f.lower() for k in ["gpio", "mem", "bcm"])]
                print(f"[GPIO] /dev entries  : {devs if devs else 'none matching gpio/mem'}")
            except Exception:
                pass
        print("[GPIO] =====================================================")
        print("[GPIO] ERROR: Could not map GPIO registers through any method.")
        print("[GPIO] Action: Please make sure to run: sudo python3 pi_sensor_reader.py")
        print("[GPIO] =====================================================")
        sys.exit(1)

    # -------------------------------------------------------------------------
    #  Register Read / Write
    # -------------------------------------------------------------------------

    def _reg_read(self, offset):
        """Read 32-bit value from GPIO register at given byte offset."""
        if getattr(self, "_use_devmem", False):
            import struct
            self._mmap_obj.seek(offset)
            return struct.unpack("<I", self._mmap_obj.read(4))[0]
        elif getattr(self, "_use_rpi_gpio", False):
            return 0
        else:
            addr = self._gpio_ptr + offset
            return ctypes.c_uint32.from_address(addr).value

    def _reg_write(self, offset, value):
        """Write 32-bit value to GPIO register at given byte offset."""
        if getattr(self, "_use_devmem", False):
            import struct
            self._mmap_obj.seek(offset)
            self._mmap_obj.write(struct.pack("<I", value & 0xFFFFFFFF))
        elif getattr(self, "_use_rpi_gpio", False):
            return
        else:
            addr = self._gpio_ptr + offset
            ctypes.c_uint32.from_address(addr).value = value & 0xFFFFFFFF

    # -------------------------------------------------------------------------
    #  Pin Configuration
    # -------------------------------------------------------------------------

    def set_direction(self, pin, direction):
        """Set pin as GPIO_INPUT or GPIO_OUTPUT."""
        if getattr(self, "_use_rpi_gpio", False):
            mode = self._rpi_gpio.OUT if direction == GPIO_OUTPUT else self._rpi_gpio.IN
            self._rpi_gpio.setup(pin, mode)
            return
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
        """
        if getattr(self, "_use_rpi_gpio", False):
            p = self._rpi_gpio.PUD_UP if pud == PUD_UP else (self._rpi_gpio.PUD_DOWN if pud == PUD_DOWN else self._rpi_gpio.PUD_OFF)
            self._rpi_gpio.setup(pin, self._rpi_gpio.IN, pull_up_down=p)
            return
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
        """Drive pin HIGH."""
        if getattr(self, "_use_rpi_gpio", False):
            self._rpi_gpio.output(pin, self._rpi_gpio.HIGH)
            return
        self._reg_write(GPSET0, 1 << pin)

    def output_low(self, pin):
        """Drive pin LOW."""
        if getattr(self, "_use_rpi_gpio", False):
            self._rpi_gpio.output(pin, self._rpi_gpio.LOW)
            return
        self._reg_write(GPCLR0, 1 << pin)

    def input(self, pin):
        """Read current pin level. Returns 0 or 1."""
        if getattr(self, "_use_rpi_gpio", False):
            return 1 if self._rpi_gpio.input(pin) else 0
        return (self._reg_read(GPLEV0) >> pin) & 1

    def cleanup(self):
        """Clean up GPIO resources."""
        try:
            if getattr(self, "_use_rpi_gpio", False) and self._rpi_gpio:
                self._rpi_gpio.cleanup()
            elif getattr(self, "_use_devmem", False) and self._mmap_obj:
                self._mmap_obj.close()
            elif self._gpio_ptr and self._libc:
                if hasattr(self._libc, "munmap_device_memory"):
                    self._libc.munmap_device_memory(ctypes.c_void_p(self._gpio_ptr), BLOCK_SIZE)
                elif hasattr(self._libc, "munmap"):
                    self._libc.munmap(ctypes.c_void_p(self._gpio_ptr), BLOCK_SIZE)
            print("[GPIO] Resources released cleanly.")
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
    High-Speed Bit-Bang DHT11 Driver via Direct BCM2711 GPLEV0 Register Access.

    Uses micro-cycle duration sampling with zero system-call overhead inside
    the critical 40-bit transmission window, followed by dynamic pulse-width
    thresholding between bit-0 (26-28us) and bit-1 (70us).
    """

    MAX_ATTEMPTS = 5

    def __init__(self, pin):
        self.pin = pin
        self._last_temp = None
        self._last_hum  = None
        self._first_ok  = False

    def _read_raw(self):
        pin = self.pin

        # 1. Drive LOW for 20ms to initiate start signal
        gpio.set_direction(pin, GPIO_OUTPUT)
        gpio.output_low(pin)
        time.sleep(0.020)

        # 2. Release line and configure as input with pull-up
        gpio.output_high(pin)
        gpio.set_direction(pin, GPIO_INPUT)
        gpio.set_pull(pin, PUD_UP)

        # Pre-bind variables into local scope for maximum execution speed
        pin_mask = 1 << pin
        if getattr(gpio, "_gpio_ptr", None):
            gplev_addr = gpio._gpio_ptr + GPLEV0
            from_addr = ctypes.c_uint32.from_address
            def read_pin():
                return (from_addr(gplev_addr).value & pin_mask) != 0
        else:
            read_pin = lambda: gpio.input(pin) == 1

        # 3. Wait for DHT11 ACK: pin pulled LOW by sensor (~80us)
        count = 0
        while read_pin():
            count += 1
            if count > 30000:
                return None, None

        # 4. Wait for DHT11 ACK: pin pulled HIGH by sensor (~80us)
        count = 0
        while not read_pin():
            count += 1
            if count > 30000:
                return None, None

        # 5. Wait for DHT11 ACK finish: pin goes LOW before bit 0
        count = 0
        while read_pin():
            count += 1
            if count > 30000:
                return None, None

        # 6. Sample 40 bits (each bit = 50us LOW + variable HIGH)
        pulse_lengths = []
        for _ in range(40):
            # Wait while pin is LOW (bit start)
            count = 0
            while not read_pin():
                count += 1
                if count > 30000:
                    return None, None

            # Measure duration of HIGH pulse
            high_cycles = 0
            while read_pin():
                high_cycles += 1
                if high_cycles > 30000:
                    return None, None

            pulse_lengths.append(high_cycles)

        if len(pulse_lengths) != 40:
            return None, None

        # 7. Dynamic threshold: '0' is ~28us, '1' is ~70us
        p_min = min(pulse_lengths)
        p_max = max(pulse_lengths)
        if p_max <= p_min:
            return None, None
        threshold = (p_min + p_max) / 2.0

        bits = [1 if p > threshold else 0 for p in pulse_lengths]

        # 8. Decode 5 bytes
        raw = [0, 0, 0, 0, 0]
        for i in range(40):
            raw[i // 8] = (raw[i // 8] << 1) | bits[i]

        # 9. Verify checksum: byte 4 == (byte0 + byte1 + byte2 + byte3) & 0xFF
        calc_checksum = (raw[0] + raw[1] + raw[2] + raw[3]) & 0xFF
        if calc_checksum != raw[4]:
            return None, None

        hum  = raw[0] + raw[1] * 0.1
        temp = raw[2] + raw[3] * 0.1

        if not (0.0 <= temp <= 70.0 and 0.0 <= hum <= 100.0):
            return None, None

        return round(temp, 1), round(hum, 1)

    def read(self):
        """Read DHT11 with retries. Caches and returns last valid reading."""
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                temp, hum = self._read_raw()
                if temp is not None and hum is not None:
                    self._last_temp, self._last_hum = temp, hum
                    if not self._first_ok:
                        self._first_ok = True
                        print(f"\n[DHT11] VALID HARDWARE DATA: Temperature={temp}C, Humidity={hum}%\n")
                    return temp, hum
            except Exception:
                pass
            if attempt < self.MAX_ATTEMPTS - 1:
                time.sleep(0.04)
        return self._last_temp, self._last_hum


# ==============================================================================
#  QNX Real-Time Architecture: Priorities, Multi-Rate Schedulers & Deadlines
# ==============================================================================

# QNX POSIX Real-Time Scheduling Constants
SCHED_FIFO  = 1
SCHED_RR    = 2
SCHED_OTHER = 3

_NTO_TCTL_RUNMASK = 4   # QNX sys/neutrino.h: ThreadCtl command to set CPU affinity mask

def set_qnx_thread_priority(priority=100, policy=SCHED_FIFO):
    """Assign deterministic real-time scheduling priority to calling thread."""
    try:
        if getattr(gpio, "_libc", None) and hasattr(gpio._libc, "pthread_setschedparam"):
            class SchedParam(ctypes.Structure):
                _fields_ = [("sched_priority", ctypes.c_int)]
            param = SchedParam(priority)
            tid = gpio._libc.pthread_self()
            res = gpio._libc.pthread_setschedparam(tid, policy, ctypes.byref(param))
            if res == 0:
                return True
    except Exception:
        pass
    return False

def set_qnx_thread_affinity(core_mask=0x01):
    """
    Assign CPU Core Affinity mask to calling thread (QNX ThreadCtl / Linux fallback).
    RPi 4 Quad-Core Cortex-A72:
      0x01 = Core 0 | 0x02 = Core 1 | 0x04 = Core 2 | 0x08 = Core 3
    """
    try:
        if getattr(gpio, "_libc", None) and hasattr(gpio._libc, "ThreadCtl"):
            res = gpio._libc.ThreadCtl(_NTO_TCTL_RUNMASK, ctypes.c_void_p(core_mask))
            if res == 0:
                return True
    except Exception:
        pass
    try:
        if hasattr(os, "sched_setaffinity"):
            cores = [i for i in range(4) if (core_mask & (1 << i))]
            os.sched_setaffinity(0, set(cores))
            return True
    except Exception:
        pass
    return False


class TaskMetrics:
    """Real-time task performance, CPU affinity & deadline compliance tracker."""
    def __init__(self, name, rate_hz, deadline_ms, priority, core_mask=0x01, core_name="Core 0"):
        self.name             = name
        self.rate_hz          = rate_hz
        self.period_s         = 1.0 / rate_hz
        self.deadline_ms      = deadline_ms
        self.priority         = priority
        self.core_mask        = core_mask
        self.core_name        = core_name
        self.total_ticks      = 0
        self.deadline_misses  = 0
        self.exec_time_ms     = 0.0
        self.max_exec_time_ms = 0.0
        self.jitter_ms        = 0.0
        self.max_jitter_ms    = 0.0
        self.last_start_t     = None

    def begin_tick(self):
        now = time.perf_counter()
        if self.last_start_t is not None:
            dt = now - self.last_start_t
            self.jitter_ms = abs(dt - self.period_s) * 1000.0
            if self.jitter_ms > self.max_jitter_ms:
                self.max_jitter_ms = self.jitter_ms
        self.last_start_t = now
        self.total_ticks += 1
        return now

    def end_tick(self, start_t):
        elapsed_ms = (time.perf_counter() - start_t) * 1000.0
        self.exec_time_ms = elapsed_ms
        if elapsed_ms > self.max_exec_time_ms:
            self.max_exec_time_ms = elapsed_ms
        if elapsed_ms > self.deadline_ms:
            self.deadline_misses += 1
            return False  # Deadline missed
        return True       # Met deadline


# Task Definitions (Exact QNX 4-Tier Priority Architecture with CPU Core Isolation)
# ------------------------------------------------------------------------------
# Priority 1 (Highest : Level 250) - Twin Synchronizer : Core 0 (0x01) - 20 Hz, <15ms
# Priority 2 (High    : Level 200) - Fault Monitor     : Core 1 (0x02) - 20 Hz, <5ms
# Priority 3 (Medium  : Level 150) - Hardware DAQ      : Core 2 (0x04) - IR 50Hz, MQ 2Hz, DHT 0.5Hz
# Priority 4 (Low     : Level  80) - City Analytics    : Core 3 (0x08) - 5 Hz, <50ms
# ------------------------------------------------------------------------------
task_metrics = {
    "p1_synchronizer": TaskMetrics("P1_Twin_Synchronizer", rate_hz=20.0, deadline_ms=15.0,  priority=250, core_mask=0x01, core_name="Core 0"),
    "p2_fault_monitor":TaskMetrics("P2_Fault_Monitor",     rate_hz=20.0, deadline_ms=5.0,   priority=200, core_mask=0x02, core_name="Core 1"),
    "p3_acq_traffic":  TaskMetrics("P3_Acq_Traffic_IR",    rate_hz=100.0, deadline_ms=5.0,  priority=150, core_mask=0x04, core_name="Core 2"),
    "p3_acq_air":      TaskMetrics("P3_Acq_Air_MQ135",     rate_hz=2.0,  deadline_ms=10.0,  priority=150, core_mask=0x04, core_name="Core 2"),
    "p3_acq_env":      TaskMetrics("P3_Acq_Env_DHT11",     rate_hz=0.5,  deadline_ms=120.0, priority=150, core_mask=0x04, core_name="Core 2"),
    "p4_analytics":    TaskMetrics("P4_City_Analytics",    rate_hz=5.0,  deadline_ms=50.0,  priority=80,  core_mask=0x08, core_name="Core 3"),
}


# ==============================================================================
#  Data Consistency: Decoupled Ring Buffers & Atomic Snapshot Engine
# ==============================================================================

class AtomicSensorState:
    """Thread-safe double-buffered state with timestamping and sequence counters."""
    def __init__(self):
        self._lock = threading.Lock()
        self.seq_id = 0

        # Subsystems (Raw Acquisition Data)
        self.traffic_raw = {
            "beam_blocked": False,
            "event_times": [],
            "last_event_time": 0.0,
            "total_vehicles": 0,
            "last_speed_kmh": 0.0,
            "last_transit_ms": 0.0,
            "raw_phys_kmh": 0.0,
            "vehicle_length_cm": 6.0,
            "timestamp": time.time()
        }
        self.air_raw = {
            "gas_alert": False, "alert_count": 0,
            "timestamp": time.time()
        }
        self.env_raw = {
            "temperature": None, "humidity": None, "dht_ok": False, "errors": 0,
            "timestamp": time.time()
        }

        # Computed Analytics (P4 Output)
        self.analytics = {
            "traffic_speed": 0.0, "congestion_pct": 0.0, "congestion_level": "FREE FLOW",
            "density_pct": 0.0, "density_level": "FREE FLOW",
            "sectors": {"downtown": 0.0, "commercial": 0.0, "waterfront": 0.0, "industrial": 0.0},
            "aqi": 24.0, "air_quality_label": "GOOD",
            "heat_index_c": 24.5, "comfort_label": "COMFORTABLE",
            "estimated_power_mw": 412.0
        }

        # Fault Monitor State (P2 Output)
        self.fault_status = {
            "system_state": "NORMAL [OPTIMAL]",
            "stale_count": 0,
            "watchdog": "HEALTHY",
            "traffic_freshness_ms": 0.0,
            "air_freshness_ms": 0.0,
            "env_freshness_ms": 0.0,
            "traffic_stale": False,
            "air_stale": False,
            "env_stale": False,
        }

    # P3 Data Acquisition Writers
    def update_traffic_raw(self, blocked, new_event=False, transit_ms=0.0, calculated_speed=None, raw_phys_kmh=None):
        with self._lock:
            now = time.monotonic()
            self.seq_id += 1
            self.traffic_raw["beam_blocked"] = blocked
            self.traffic_raw["timestamp"]    = time.time()
            if new_event:
                self.traffic_raw["total_vehicles"] += 1
                self.traffic_raw["event_times"].append(now)
                self.traffic_raw["last_event_time"] = now
                if transit_ms > 0:
                    self.traffic_raw["last_transit_ms"] = transit_ms
                if calculated_speed is not None:
                    self.traffic_raw["last_speed_kmh"] = calculated_speed
                    self.traffic_raw["raw_phys_kmh"]   = raw_phys_kmh or calculated_speed
                    # Immediately propagate the calculated speed to analytics
                    self.analytics["traffic_speed"]     = calculated_speed

    def update_air_raw(self, alert, is_new=False):
        with self._lock:
            self.seq_id += 1
            self.air_raw["gas_alert"] = alert
            self.air_raw["timestamp"] = time.time()
            if is_new:
                self.air_raw["alert_count"] += 1

    def update_env_raw(self, temp, hum, ok):
        with self._lock:
            self.seq_id += 1
            if temp is not None:
                self.env_raw["temperature"] = temp
                self.env_raw["humidity"]    = hum
                self.env_raw["dht_ok"]      = True
            else:
                self.env_raw["errors"] += 1
                self.env_raw["dht_ok"]  = False
            self.env_raw["timestamp"] = time.time()

    # P4 Analytics Writer
    def update_analytics(self, speed, congestion, aqi, heat_idx, comfort, pwr, density_pct=None, density_level=None, sectors=None):
        with self._lock:
            dpct = density_pct if density_pct is not None else congestion
            dlevel = density_level or ("GRIDLOCK" if dpct > 75 else ("HEAVY" if dpct > 50 else ("MODERATE" if dpct > 28 else "FREE FLOW")))
            sec = sectors or {
                "downtown": round(dpct, 1),
                "commercial": round(dpct * 0.72, 1),
                "waterfront": round(dpct * 0.38, 1),
                "industrial": round(dpct * 0.58, 1)
            }
            self.analytics["traffic_speed"]     = speed
            self.analytics["congestion_pct"]    = dpct
            self.analytics["density_pct"]       = dpct
            self.analytics["density_level"]     = dlevel
            self.analytics["congestion_level"]  = dlevel
            self.analytics["sectors"]           = sec
            self.analytics["aqi"]               = aqi
            self.analytics["air_quality_label"] = "HAZARDOUS" if aqi > 150 else ("POOR" if aqi > 100 else "GOOD")
            self.analytics["heat_index_c"]      = heat_idx
            self.analytics["comfort_label"]     = comfort
            self.analytics["estimated_power_mw"]= pwr

    # P2 Fault Monitor Writer
    def update_fault_status(self, state, stale_cnt, t_f, a_f, e_f, t_s, a_s, e_s):
        with self._lock:
            self.fault_status["system_state"]          = state
            self.fault_status["stale_count"]           = stale_cnt
            self.fault_status["traffic_freshness_ms"]  = t_f
            self.fault_status["air_freshness_ms"]      = a_f
            self.fault_status["env_freshness_ms"]      = e_f
            self.fault_status["traffic_stale"]         = t_s
            self.fault_status["air_stale"]             = a_s
            self.fault_status["env_stale"]             = e_s

    # P1 Twin Synchronizer Reader (Atomic Consistent Snapshot)
    def get_consistent_snapshot(self):
        with self._lock:
            snap = {
                "seq": self.seq_id,
                "timestamp": time.time(),
                "system_state": self.fault_status["system_state"],
                "stale_count": self.fault_status["stale_count"],
                "watchdog": self.fault_status["watchdog"],
                "traffic": {
                    "val": self.analytics["traffic_speed"],
                    "unit": "km/h",
                    "vehicle_count": self.traffic_raw["total_vehicles"],
                    "beam_blocked": self.traffic_raw["beam_blocked"],
                    "transit_time_ms": self.traffic_raw.get("last_transit_ms", 0.0),
                    "raw_phys_kmh": self.traffic_raw.get("raw_phys_kmh", 0.0),
                    "vehicle_length_cm": self.traffic_raw.get("vehicle_length_cm", 6.0),
                    "density_pct": self.analytics.get("density_pct", 0.0),
                    "density_level": self.analytics.get("density_level", "FREE FLOW"),
                    "sectors": self.analytics.get("sectors", {"downtown": 0.0, "commercial": 0.0, "waterfront": 0.0, "industrial": 0.0}),
                    "congestion_pct": self.analytics.get("density_pct", 0.0),
                    "congestion_level": self.analytics.get("density_level", "FREE FLOW"),
                    "freshness_ms": self.fault_status["traffic_freshness_ms"],
                    "stale": self.fault_status["traffic_stale"]
                },
                "air": {
                    "val": self.analytics["aqi"],
                    "unit": "AQI",
                    "gas_alert": self.air_raw["gas_alert"],
                    "alert_count": self.air_raw["alert_count"],
                    "quality_label": self.analytics["air_quality_label"],
                    "freshness_ms": self.fault_status["air_freshness_ms"],
                    "stale": self.fault_status["air_stale"]
                },
                "environment": {
                    "temperature": self.env_raw["temperature"],
                    "humidity": self.env_raw["humidity"],
                    "dht_ok": self.env_raw["dht_ok"],
                    "errors": self.env_raw["errors"],
                    "heat_index_c": self.analytics["heat_index_c"],
                    "comfort_label": self.analytics["comfort_label"],
                    "freshness_ms": self.fault_status["env_freshness_ms"],
                    "stale": self.fault_status["env_stale"]
                },
                "power": {
                    "val": self.analytics["estimated_power_mw"],
                    "unit": "MW",
                    "freshness_ms": 12.0,
                    "stale": False
                }
            }
            return snap


# Global Instances
sensor_state = AtomicSensorState()
dht11_driver = DHT11Driver(PIN_DHT11)
_start_time  = time.monotonic()


# ==============================================================================
#  Priority 3 [RTOS Priority: Medium (150)] - Data Acquisition Workers
#  Purpose: Read/receive traffic, air, environment physical sensors
# ==============================================================================

def thread_p3_acq_traffic():
    """P3 Data Acquisition: IR Sensor (100 Hz, 10ms period, Priority: 150, Core 2)"""
    metric = task_metrics["p3_acq_traffic"]
    set_qnx_thread_priority(metric.priority)
    set_qnx_thread_affinity(metric.core_mask)
    print(f"[P3 Acquisition] IR Traffic     | Rate: 100 Hz| Deadline: 5.0ms  | Prio: 150 | {metric.core_name}")

    VEHICLE_LENGTH_M = 0.06   # 6.0 cm assumed vehicle length
    MIN_TRANSIT_SEC  = 0.006  # 6ms minimum debounce filter to eliminate optical jitter

    last_lvl = 1
    t_block_start = None

    while True:
        t_start = metric.begin_tick()

        lvl = gpio.input(PIN_IR)
        t_now = time.perf_counter()

        # 1. Falling edge: Object enters sensor beam (HIGH -> LOW)
        if lvl == 0 and last_lvl == 1:
            t_block_start = t_now
            sensor_state.update_traffic_raw(blocked=True, new_event=False)

        # 2. Rising edge: Object clears sensor beam (LOW -> HIGH)
        elif lvl == 1 and last_lvl == 0:
            if t_block_start is not None:
                duration_s = t_now - t_block_start
                if duration_s >= MIN_TRANSIT_SEC:
                    transit_ms = duration_s * 1000.0

                    # Speed = Distance / Time -> (0.06m / duration_s) * 3.6 km/h = 0.216 / duration_s
                    # Longer blocking duration = slower speed; Faster crossing = higher speed
                    raw_phys_kmh = round(0.216 / duration_s, 2)

                    # Calibrated City Digital Twin Speed:
                    # Maps human hand sweeps / desktop toy-car crossings (50ms - 900ms)
                    # to realistic urban street speeds (10 km/h - 80 km/h).
                    calibrated_kmh = round(min(88.0, max(5.0, (0.216 / duration_s) * 18.0)), 1)

                    # Immediately update traffic state & calculated speed
                    sensor_state.update_traffic_raw(
                        blocked=False,
                        new_event=True,
                        transit_ms=round(transit_ms, 1),
                        calculated_speed=calibrated_kmh,
                        raw_phys_kmh=raw_phys_kmh
                    )

                    total_veh = sensor_state.traffic_raw["total_vehicles"]
                    print(
                        f"\n[IR SENSOR 🚗] Vehicle #{total_veh:03d} Detected! "
                        f"Blocked Time: {transit_ms:6.1f} ms | "
                        f"Speed: {calibrated_kmh:4.1f} km/h (Physical: {raw_phys_kmh:4.2f} km/h | L=6cm)\n"
                    )
                else:
                    sensor_state.update_traffic_raw(blocked=False, new_event=False)
                t_block_start = None
            else:
                sensor_state.update_traffic_raw(blocked=False, new_event=False)

        # 3. Object Still Blocking: Queueing or slow crawl at gate
        elif lvl == 0 and last_lvl == 0:
            if t_block_start is not None:
                duration_so_far = t_now - t_block_start
                if duration_so_far > 1.2:
                    crawl_speed = max(3.0, round(20.0 / (duration_so_far * 1.5), 1))
                    with sensor_state._lock:
                        sensor_state.analytics["traffic_speed"] = crawl_speed
            sensor_state.update_traffic_raw(blocked=True, new_event=False)

        # 4. Sensor clear
        else:
            sensor_state.update_traffic_raw(blocked=False, new_event=False)

        last_lvl = lvl
        metric.end_tick(t_start)
        time.sleep(0.01)  # 10ms sampling interval = 100 Hz precision timing


def thread_p3_acq_air():
    """P3 Data Acquisition: MQ135 Air Quality (2 Hz, 500ms period, Priority: 150, Core 2)"""
    metric = task_metrics["p3_acq_air"]
    set_qnx_thread_priority(metric.priority)
    set_qnx_thread_affinity(metric.core_mask)
    print(f"[P3 Acquisition] MQ135 Air      | Rate: 2 Hz  | Deadline: 10.0ms | Prio: 150 | {metric.core_name}")

    prev_alert = False

    while True:
        t_start = metric.begin_tick()

        pin_val = gpio.input(PIN_MQ135)
        alert   = (pin_val == 0)   # Active LOW

        is_new = False
        if alert and not prev_alert:
            is_new = True
            print(f"\n[MQ135 ALERT] >>> Gas/Smoke Detected on GPIO{PIN_MQ135} (Active LOW) <<<\n")
        elif not alert and prev_alert:
            print(f"\n[MQ135 CLEAR] --- Clean Air Restored on GPIO{PIN_MQ135} (HIGH) ---\n")

        sensor_state.update_air_raw(alert, is_new=is_new)
        prev_alert = alert

        metric.end_tick(t_start)
        time.sleep(metric.period_s)


def thread_p3_acq_environment():
    """P3 Data Acquisition: DHT11 Sensor (0.5 Hz, 2000ms period, Priority: 150, Core 2)"""
    metric = task_metrics["p3_acq_env"]
    set_qnx_thread_priority(metric.priority)
    set_qnx_thread_affinity(metric.core_mask)
    print(f"[P3 Acquisition] DHT11 Env      | Rate: 0.5 Hz| Deadline: 120ms  | Prio: 150 | {metric.core_name}")


    while True:
        t_start = metric.begin_tick()

        temp, hum = dht11_driver.read()
        if temp is not None:
            sensor_state.update_env_raw(temp, hum, ok=True)
        else:
            sensor_state.update_env_raw(None, None, ok=False)

        metric.end_tick(t_start)
        time.sleep(metric.period_s)


# ==============================================================================
#  Priority 4 [RTOS Priority: Low (80)] - Analytics
#  Purpose: Calculate traffic, energy, and environmental metrics from synchronized data
# ==============================================================================

def thread_p4_analytics():
    """Priority 4 Task: Real-Time Analytics & Sensor Fusion Engine (5 Hz, 200ms period, Core 3)"""
    metric = task_metrics["p4_analytics"]
    set_qnx_thread_priority(metric.priority)
    set_qnx_thread_affinity(metric.core_mask)
    print(f"[P4 Analytics]   City Analytics | Rate: 5 Hz  | Deadline: 50.0ms | Prio: 80  | {metric.core_name}")

    WINDOW = 30.0  # 30-second rolling window for vehicle speed and flow

    while True:
        t_start = metric.begin_tick()
        now = time.monotonic()

        # 1. Traffic Density & Flow Analytics (Greenshields Traffic Flow Model)
        with sensor_state._lock:
            # Filter vehicles in 30-second window
            sensor_state.traffic_raw["event_times"] = [
                t for t in sensor_state.traffic_raw["event_times"] if t >= now - WINDOW
            ]
            recent_count = len(sensor_state.traffic_raw["event_times"])
            beam_blocked = sensor_state.traffic_raw["beam_blocked"]
            gas_alert    = sensor_state.air_raw["gas_alert"]
            temp         = sensor_state.env_raw["temperature"]
            hum          = sensor_state.env_raw["humidity"]
            total_veh    = sensor_state.traffic_raw["total_vehicles"]
            last_speed   = sensor_state.traffic_raw.get("last_speed_kmh", 0.0)
            last_event_t = sensor_state.traffic_raw.get("last_event_time", 0.0)

        vpm = (recent_count / WINDOW) * 60.0

        if beam_blocked:
            # Active obstacle/standstill queue detected at sensor gate
            speed = max(0.0, round(sensor_state.analytics.get("traffic_speed", 25.0) * 0.4, 1))
            density_pct = min(100.0, 84.0 + (recent_count * 2.0))
        elif recent_count > 0 and (now - last_event_t) <= 4.0 and last_speed > 0:
            # When a vehicle was detected within the last 4 seconds: display its measured speed
            speed = last_speed
            density_pct = round(min(100.0, max(5.0, (vpm / 22.0) * 55.0 + (recent_count * 1.2))), 1)
        else:
            # When NO vehicle is detected (idle road, count 0, or no crossing in > 4s):
            # Speed stays strictly at 0.0 km/h!
            speed = 0.0
            density_pct = round(min(100.0, (vpm / 22.0) * 40.0), 1) if recent_count > 0 else 0.0

        # Standard Level of Service (LOS) Density Categorization
        if density_pct <= 0.0:
            density_level = "FREE FLOW"
            sectors = {
                "downtown": 0.0,
                "commercial": 0.0,
                "waterfront": 0.0,
                "industrial": 0.0
            }
        else:
            if density_pct >= 75.0:
                density_level = "GRIDLOCK"
            elif density_pct >= 50.0:
                density_level = "HEAVY"
            elif density_pct >= 28.0:
                density_level = "MODERATE"
            else:
                density_level = "FREE FLOW"

            # Multi-Sector Density Distribution (Google Maps Area Breakdown)
            sectors = {
                "downtown":   round(density_pct, 1),
                "commercial": round(max(0.0, min(100.0, density_pct * 0.72)), 1),
                "waterfront": round(max(0.0, min(100.0, density_pct * 0.38)), 1),
                "industrial": round(max(0.0, min(100.0, density_pct * 0.58)), 1)
            }

        # 2. Air Quality Analytics
        aqi = 195.0 if gas_alert else (22.0 + (recent_count * 1.5))

        # 3. Environmental Heat / Comfort Analytics
        if temp is not None and hum is not None:
            heat_idx = round(temp + 0.05 * hum, 1)
            comfort  = "COMFORTABLE" if (20.0 <= temp <= 26.0 and 40.0 <= hum <= 65.0) else "SUB-OPTIMAL"
        else:
            heat_idx = 24.0
            comfort  = "NORMAL"

        # 4. Energy Load Estimation (Grid MW driven by vehicle flow and temperature)
        t_factor = (temp or 25.0) / 25.0
        power_mw = round(390.0 + (vpm * 0.8) + (t_factor * 15.0), 1)

        sensor_state.update_analytics(speed, density_pct, aqi, heat_idx, comfort, power_mw,
                                      density_pct=density_pct, density_level=density_level, sectors=sectors)

        metric.end_tick(t_start)
        time.sleep(metric.period_s)


# ==============================================================================
#  Priority 2 [RTOS Priority: High (200)] - Fault Monitor
#  Purpose: Detect stale data, missed deadlines, and system faults quickly
# ==============================================================================

def thread_p2_fault_monitor():
    """Priority 2 Task: Safety Watchdog & Deadline Compliance Monitor (20 Hz, 50ms, Core 1)"""
    metric = task_metrics["p2_fault_monitor"]
    set_qnx_thread_priority(metric.priority)
    set_qnx_thread_affinity(metric.core_mask)
    print(f"[P2 Monitor]     Fault Watchdog | Rate: 20 Hz | Deadline: 5.0ms  | Prio: 200 | {metric.core_name}")


    while True:
        t_start = metric.begin_tick()
        now = time.time()

        with sensor_state._lock:
            t_ts = sensor_state.traffic_raw["timestamp"]
            a_ts = sensor_state.air_raw["timestamp"]
            e_ts = sensor_state.env_raw["timestamp"]

        # Measure Freshness
        t_fresh = max(0.0, round((now - t_ts) * 1000.0, 1))
        a_fresh = max(0.0, round((now - a_ts) * 1000.0, 1))
        e_fresh = max(0.0, round((now - e_ts) * 1000.0, 1))

        # Stale Threshold: older than 3x the task period
        t_stale = (t_fresh > (3.0 * task_metrics["p3_acq_traffic"].period_s * 1000.0))
        a_stale = (a_fresh > (3.0 * task_metrics["p3_acq_air"].period_s * 1000.0))
        e_stale = (e_fresh > (3.0 * task_metrics["p3_acq_env"].period_s * 1000.0))

        stale_cnt = (1 if t_stale else 0) + (1 if a_stale else 0) + (1 if e_stale else 0)

        # Check Deadlines Across All Tasks
        total_misses = sum(m.deadline_misses for m in task_metrics.values())

        if total_misses > 0:
            state = f"CRITICAL [DEADLINE BREACH: {total_misses} MISSES]"
        elif stale_cnt > 0:
            state = f"DEGRADED [{stale_cnt} STALE SENSORS]"
        else:
            state = "NORMAL [OPTIMAL]"

        sensor_state.update_fault_status(state, stale_cnt, t_fresh, a_fresh, e_fresh, t_stale, a_stale, e_stale)

        metric.end_tick(t_start)
        time.sleep(metric.period_s)


# ==============================================================================
#  Priority 1 [RTOS Priority: Highest (250)] - Twin Synchronizer
#  Purpose: Maintain consistent city-state snapshot within the timing deadline
# ==============================================================================

def thread_p1_twin_synchronizer(sock):
    """Priority 1 Task: Hard Real-Time Snapshot Synchronizer & Dual UDP/MQTT Publisher (20 Hz, 50ms, Core 0)"""
    metric = task_metrics["p1_synchronizer"]
    set_qnx_thread_priority(metric.priority)
    set_qnx_thread_affinity(metric.core_mask)
    print(f"[P1 Synchronizer]Twin Engine    | Rate: 20 Hz | Deadline: 15.0ms | Prio: 250 (HIGHEST) | {metric.core_name}")
    print(f"[P1 Synchronizer]Dual Channels  | UDP: {HOST_PC_IP}:{UDP_PORT} | MQTT: {HOST_PC_IP}:{MQTT_PORT}")

    mqtt_client = MiniMQTTClient(host=HOST_PC_IP, port=MQTT_PORT, client_id="QNX_Pi_Synchronizer")

    while True:
        t_start = metric.begin_tick()

        # 1. Atomic Snapshot Alignment across all subsystems
        snap = sensor_state.get_consistent_snapshot()
        ts   = snap["timestamp"]
        up   = round(time.monotonic() - _start_time, 1)

        sync_lat_ms = metric.exec_time_ms
        total_misses = sum(m.deadline_misses for m in task_metrics.values())

        # 2. Package Multi-Subsystem Telemetry Streams
        packets = [
            # Traffic Stream (Greenshields Density Model + 6cm Precision Transit Speed)
            {
                "stream": "traffic",
                "value": snap["traffic"]["val"],
                "unit": "km/h",
                "vehicle_count": snap["traffic"]["vehicle_count"],
                "beam_blocked": snap["traffic"]["beam_blocked"],
                "transit_time_ms": snap["traffic"].get("transit_time_ms", 0.0),
                "raw_phys_kmh": snap["traffic"].get("raw_phys_kmh", 0.0),
                "vehicle_length_cm": 6.0,
                "density_pct": snap["traffic"]["density_pct"],
                "density_level": snap["traffic"]["density_level"],
                "sectors": snap["traffic"]["sectors"],
                "congestion_pct": snap["traffic"]["congestion_pct"],
                "congestion_level": snap["traffic"]["congestion_level"],
                "freshness_ms": snap["traffic"]["freshness_ms"],
                "stale": snap["traffic"]["stale"],
                "ts": ts
            },

            # Air Quality Stream
            {
                "stream": "air",
                "value": snap["air"]["val"],
                "unit": "AQI",
                "gas_alert": snap["air"]["gas_alert"],
                "alert_count": snap["air"]["alert_count"],
                "quality_label": snap["air"]["quality_label"],
                "freshness_ms": snap["air"]["freshness_ms"],
                "stale": snap["air"]["stale"],
                "ts": ts
            },

            # Environmental Stream
            {
                "stream": "environment",
                "temperature": snap["environment"]["temperature"],
                "humidity": snap["environment"]["humidity"],
                "temp_unit": "C",
                "hum_unit": "%RH",
                "dht_ok": snap["environment"]["dht_ok"],
                "heat_index_c": snap["environment"]["heat_index_c"],
                "comfort_label": snap["environment"]["comfort_label"],
                "freshness_ms": snap["environment"]["freshness_ms"],
                "stale": snap["environment"]["stale"],
                "ts": ts
            },

            # Complete Synchronized QNX Telemetry
            {
                "stream": "qnx_telemetry",
                "system_state": snap["system_state"],
                "sync_latency_ms": round(sync_lat_ms, 3),
                "max_latency_ms": round(metric.max_exec_time_ms, 3),
                "deadline_misses": total_misses,
                "jitter_ms": round(metric.jitter_ms, 3),
                "max_jitter_ms": round(metric.max_jitter_ms, 3),
                "total_ticks": metric.total_ticks,
                "stale_count": snap["stale_count"],
                "watchdog": snap["watchdog"],
                "uptime_sec": up,
                "snapshot": snap,
                "tasks": {
                    name: {
                        "prio": m.priority, "deadline_ms": m.deadline_ms,
                        "lat_ms": round(m.exec_time_ms, 2), "max_lat_ms": round(m.max_exec_time_ms, 2),
                        "misses": m.deadline_misses, "ticks": m.total_ticks
                    }
                    for name, m in task_metrics.items()
                },
                "ts": ts
            },

            # Combined Hardware Sensors Frame
            {
                "stream": "pi_sensors",
                "temperature_c": snap["environment"]["temperature"],
                "humidity_pct": snap["environment"]["humidity"],
                "traffic_speed": snap["traffic"]["val"],
                "vehicle_count": snap["traffic"]["vehicle_count"],
                "gas_alert": snap["air"]["gas_alert"],
                "gas_alert_count": snap["air"]["alert_count"],
                "dht_errors": snap["environment"]["errors"],
                "sync_latency_ms": round(sync_lat_ms, 3),
                "deadline_misses": total_misses,
                "uptime_sec": up,
                "ts": ts
            }
        ]

        # 3. Transmit via Dual Stream: UDP (Port 9999) + MQTT (Port 1883)
        for p in packets:
            try:
                raw = json.dumps(p).encode('utf-8')
                # Channel A: UDP Datagram
                sock.sendto(raw, (HOST_PC_IP, UDP_PORT))
                # Channel B: MQTT Pub/Sub
                mqtt_client.publish(f"qnx/city/{p['stream']}", raw)
            except Exception:
                pass

        metric.end_tick(t_start)
        time.sleep(metric.period_s)



# ==============================================================================
#  Real-Time Terminal Console Renderer
# ==============================================================================

def thread_console_hud():
    """Display real-time terminal HUD with sensor readings & QNX metrics."""
    while True:
        snap = sensor_state.get_consistent_snapshot()
        up   = round(time.monotonic() - _start_time)

        t_val = snap["environment"]["temperature"]
        h_val = snap["environment"]["humidity"]
        t_s   = f"{t_val:4.1f}C" if t_val is not None else " N/A C"
        h_s   = f"{h_val:4.1f}%" if h_val is not None else "  N/A%"

        dht_status = "OK" if snap["environment"]["dht_ok"] else f"ERR({snap['environment']['errors']})"
        air_status = "!! ALERT !!" if snap["air"]["gas_alert"] else "CLEAN"

        sync_metric = task_metrics["p1_synchronizer"]
        total_misses = sum(m.deadline_misses for m in task_metrics.values())

        transit_ms = snap["traffic"].get("transit_time_ms", 0.0)
        sys.stdout.write(
            f"\r[P1:{sync_metric.priority}|{snap['system_state'][:14]}] "
            f"Vehicles:{snap['traffic']['vehicle_count']:3d} ({snap['traffic']['val']:4.1f}km/h | {transit_ms:3.0f}ms) | "
            f"Air:{snap['air']['val']:3.0f}AQI [{air_status}] | "
            f"DHT:{t_s},{h_s} | "
            f"SyncLat:{sync_metric.exec_time_ms:4.2f}ms Misses:{total_misses} "
        )
        sys.stdout.flush()
        time.sleep(0.2)


# ==============================================================================
#  Main
# ==============================================================================

BANNER = """\
+==============================================================================+
|  QNX REAL-TIME DIGITAL TWIN SYNCHRONIZER  -  Raspberry Pi 4 (BCM2711)       |
|  RTOS Priority Hierarchy:                                                    |
|    [Priority 1 - Highest : 250] Twin Synchronizer (Atomic Snapshot & UDP)    |
|    [Priority 2 - High    : 200] Fault Monitor (Stale Detection & Watchdog)   |
|    [Priority 3 - Medium  : 150] Data Acquisition (IR 50Hz, MQ 2Hz, DHT 0.5Hz)|
|    [Priority 4 - Low     :  80] Analytics (Speed, AQI, Congestion, Power)    |
+==============================================================================+"""

def main():
    os.system("clear")
    print(BANNER)
    print()
    print(f"  GPIO Method  : {getattr(gpio, '_method', 'Unknown')}")
    print(f"  DHT11        : GPIO {PIN_DHT11}  (Pin 7)   Temp + Humidity  @ 0.5 Hz  [P3]")
    print(f"  IR Sensor    : GPIO {PIN_IR} (Pin 11)  Vehicle Detection @ 50 Hz   [P3]")
    print(f"  MQ135        : GPIO {PIN_MQ135} (Pin 13)  Gas / Smoke Alert @ 2 Hz    [P3]")
    print(f"  UDP Target   : {HOST_PC_IP}:{UDP_PORT}  @ 20 Hz [P1 Synchronizer]")
    print()

    init_sensor_pins()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    threads = [
        # Priority 3: Data Acquisition Workers
        threading.Thread(target=thread_p3_acq_traffic,     daemon=True, name="P3_Acq_Traffic_50Hz"),
        threading.Thread(target=thread_p3_acq_air,         daemon=True, name="P3_Acq_Air_2Hz"),
        threading.Thread(target=thread_p3_acq_environment, daemon=True, name="P3_Acq_Env_0.5Hz"),

        # Priority 4: City Analytics Worker
        threading.Thread(target=thread_p4_analytics,       daemon=True, name="P4_Analytics_5Hz"),

        # Priority 2: Fault Monitor & Safety Watchdog
        threading.Thread(target=thread_p2_fault_monitor,   daemon=True, name="P2_Fault_Monitor_20Hz"),

        # Priority 1 (Highest): Twin Synchronizer & Telemetry Core
        threading.Thread(target=thread_p1_twin_synchronizer, args=(sock,), daemon=True, name="P1_Twin_Sync_20Hz"),

        # HUD Terminal Renderer
        threading.Thread(target=thread_console_hud,        daemon=True, name="HUD_Console"),
    ]
    for t in threads:
        t.start()

    print(f"\n[READY] 4-Tier QNX Priority Architecture active ({len(threads)} threads). Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(5)
            snap = sensor_state.get_consistent_snapshot()
            up   = round(time.monotonic() - _start_time)
            s_metric = task_metrics["p1_synchronizer"]
            total_misses = sum(m.deadline_misses for m in task_metrics.values())

            print(
                f"\n[QNX PRIORITY REPORT @{up}s] "
                f"State: {snap['system_state']} | "
                f"P1 Sync Latency: {s_metric.exec_time_ms:.2f}ms (Peak: {s_metric.max_exec_time_ms:.2f}ms) | "
                f"Jitter: {s_metric.jitter_ms:.2f}ms | "
                f"Deadline Misses: {total_misses} | "
                f"Freshness: IR={snap['traffic']['freshness_ms']}ms, "
                f"MQ={snap['air']['freshness_ms']}ms, "
                f"DHT={snap['environment']['freshness_ms']}ms"
            )
    except KeyboardInterrupt:
        print("\n\n[SHUTDOWN] Stopping QNX Engine...")
        gpio.cleanup()
        sock.close()
        print("[DONE] All real-time resources released cleanly.")


if __name__ == "__main__":
    main()
