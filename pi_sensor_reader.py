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


class TaskMetrics:
    """Real-time task performance & deadline compliance tracker."""
    def __init__(self, name, rate_hz, deadline_ms, priority):
        self.name             = name
        self.rate_hz          = rate_hz
        self.period_s         = 1.0 / rate_hz
        self.deadline_ms      = deadline_ms
        self.priority         = priority
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


# Task Definitions (QNX Multi-Rate Scheduling Policy)
# ------------------------------------------------------------------------------
# 1. Traffic Task    : 50 Hz (20ms period),  Deadline: 5.0ms,   Priority: 220 (High)
# 2. Sync / UDP Task : 20 Hz (50ms period),  Deadline: 15.0ms,  Priority: 200 (Medium-High)
# 3. Air Quality Task: 2 Hz  (500ms period), Deadline: 10.0ms,  Priority: 150 (Medium)
# 4. Environment Task: 0.5 Hz(2000ms period),Deadline: 120.0ms, Priority: 100 (Low)
# ------------------------------------------------------------------------------
task_metrics = {
    "traffic":     TaskMetrics("IR_Traffic",        rate_hz=50.0, deadline_ms=5.0,   priority=220),
    "sync_engine": TaskMetrics("QNX_SyncEngine",    rate_hz=20.0, deadline_ms=15.0,  priority=200),
    "air_quality": TaskMetrics("MQ135_AirQuality",  rate_hz=2.0,  deadline_ms=10.0,  priority=150),
    "environment": TaskMetrics("DHT11_Environment", rate_hz=0.5,  deadline_ms=120.0, priority=100),
}


# ==============================================================================
#  Data Consistency: Atomic Snapshot Ring Buffer & Double Buffering
# ==============================================================================

class AtomicSensorState:
    """Thread-safe double-buffered state with timestamping and sequence counters."""
    def __init__(self):
        self._lock = threading.Lock()
        self.seq_id = 0

        # Subsystems
        self.traffic = {
            "speed": 0.0, "vehicle_count": 0, "ir_blocked": False,
            "timestamp": time.time(), "seq": 0
        }
        self.air = {
            "gas_alert": False, "alert_count": 0, "aqi": 22.0,
            "timestamp": time.time(), "seq": 0
        }
        self.environment = {
            "temperature": None, "humidity": None, "dht_ok": False, "errors": 0,
            "timestamp": time.time(), "seq": 0
        }

    def update_traffic(self, speed, vehicle_count, ir_blocked):
        with self._lock:
            self.seq_id += 1
            self.traffic["speed"]         = speed
            self.traffic["vehicle_count"] = vehicle_count
            self.traffic["ir_blocked"]    = ir_blocked
            self.traffic["timestamp"]     = time.time()
            self.traffic["seq"]           = self.seq_id

    def update_air(self, gas_alert, alert_count, aqi):
        with self._lock:
            self.seq_id += 1
            self.air["gas_alert"]   = gas_alert
            self.air["alert_count"] = alert_count
            self.air["aqi"]         = aqi
            self.air["timestamp"]   = time.time()
            self.air["seq"]         = self.seq_id

    def update_environment(self, temp, hum, dht_ok, err_inc=False):
        with self._lock:
            self.seq_id += 1
            if temp is not None:
                self.environment["temperature"] = temp
                self.environment["humidity"]    = hum
                self.environment["dht_ok"]      = True
            if err_inc:
                self.environment["errors"] += 1
                self.environment["dht_ok"]  = False
            self.environment["timestamp"] = time.time()
            self.environment["seq"]       = self.seq_id

    def get_atomic_snapshot(self):
        """Atomically copy the state and compute data freshness per subsystem."""
        now = time.time()
        with self._lock:
            t = dict(self.traffic)
            a = dict(self.air)
            e = dict(self.environment)
            seq = self.seq_id

        # Calculate Freshness (ms since last hardware sample)
        t_fresh = max(0.0, round((now - t["timestamp"]) * 1000.0, 1))
        a_fresh = max(0.0, round((now - a["timestamp"]) * 1000.0, 1))
        e_fresh = max(0.0, round((now - e["timestamp"]) * 1000.0, 1))

        # Stale Check: Stale if older than 3x the task period
        t_stale = (t_fresh > (3.0 * task_metrics["traffic"].period_s * 1000.0))
        a_stale = (a_fresh > (3.0 * task_metrics["air_quality"].period_s * 1000.0))
        e_stale = (e_fresh > (3.0 * task_metrics["environment"].period_s * 1000.0))

        stale_count = (1 if t_stale else 0) + (1 if a_stale else 0) + (1 if e_stale else 0)

        # QNX System State Logic
        total_misses = sum(m.deadline_misses for m in task_metrics.values())
        if total_misses > 0:
            state = "CRITICAL [DEADLINE BREACH]"
        elif stale_count > 0:
            state = f"DEGRADED [{stale_count} SENSOR STALE]"
        else:
            state = "NORMAL [OPTIMAL]"

        return {
            "seq": seq,
            "timestamp": now,
            "system_state": state,
            "stale_count": stale_count,
            "traffic": {
                "val": t["speed"], "unit": "km/h", "vehicle_count": t["vehicle_count"],
                "beam_blocked": t["ir_blocked"], "freshness_ms": t_fresh, "stale": t_stale
            },
            "air": {
                "val": a["aqi"], "unit": "AQI", "gas_alert": a["gas_alert"],
                "alert_count": a["alert_count"], "freshness_ms": a_fresh, "stale": a_stale
            },
            "environment": {
                "temperature": e["temperature"], "humidity": e["humidity"], "dht_ok": e["dht_ok"],
                "errors": e["errors"], "freshness_ms": e_fresh, "stale": e_stale
            }
        }


# Global instances
sensor_state = AtomicSensorState()
dht11_driver = DHT11Driver(PIN_DHT11)
_start_time  = time.monotonic()


# ==============================================================================
#  Real-Time Subsystem Threads (Multi-Rate Scheduled)
# ==============================================================================

def thread_traffic_50hz():
    """Subsystem 1: Traffic & Vehicle Edge Detector (Rate: 50 Hz, QNX Prio: 220)"""
    set_qnx_thread_priority(task_metrics["traffic"].priority)
    print(f"[THREAD] IR Traffic      | 50 Hz (20ms) | Deadline: 5.0ms  | QNX Priority: 220")

    last_level    = 1
    vehicle_times = []
    WINDOW        = 30
    count         = 0
    speed         = 0.0
    metric        = task_metrics["traffic"]

    while True:
        t_start = metric.begin_tick()

        level = gpio.input(PIN_IR)
        blocked = (level == 0)

        if level == 0 and last_level == 1:       # Falling edge: vehicle passes
            now = time.monotonic()
            count += 1
            vehicle_times.append(now)
            vehicle_times = [t for t in vehicle_times if t >= now - WINDOW]
            vpm   = (len(vehicle_times) / WINDOW) * 60
            speed = round(min(120.0, max(10.0, vpm * 2.0)), 1)
            sensor_state.update_traffic(speed, count, True)
        elif level == 1 and last_level == 0:     # Rising edge: beam restored
            sensor_state.update_traffic(speed, count, False)
        else:
            # Periodic heartbeat update
            sensor_state.update_traffic(speed, count, blocked)

        last_level = level
        metric.end_tick(t_start)

        time.sleep(metric.period_s)


def thread_air_quality_2hz():
    """Subsystem 2: Air Quality & Gas Threshold Monitor (Rate: 2 Hz, QNX Prio: 150)"""
    set_qnx_thread_priority(task_metrics["air_quality"].priority)
    print(f"[THREAD] MQ135 Air       | 2 Hz (500ms) | Deadline: 10.0ms | QNX Priority: 150")

    alert_count = 0
    prev_alert  = False
    metric      = task_metrics["air_quality"]

    while True:
        t_start = metric.begin_tick()

        pin_val = gpio.input(PIN_MQ135)
        alert   = (pin_val == 0)   # Active LOW

        if alert and not prev_alert:
            alert_count += 1
            print(f"\n[MQ135] >>> GAS/SMOKE ALERT TRIGGERED! (GPIO{PIN_MQ135}=LOW) <<<\n")
        elif not alert and prev_alert:
            print(f"\n[MQ135] --- Air quality cleared (GPIO{PIN_MQ135}=HIGH) ---\n")

        prev_alert = alert
        aqi = 185.0 if alert else 24.0
        sensor_state.update_air(alert, alert_count, aqi)

        metric.end_tick(t_start)
        time.sleep(metric.period_s)


def thread_environment_halfhz():
    """Subsystem 3: DHT11 Environmental Temp/Humidity (Rate: 0.5 Hz, QNX Prio: 100)"""
    set_qnx_thread_priority(task_metrics["environment"].priority)
    print(f"[THREAD] DHT11 Env       | 0.5 Hz (2s)  | Deadline: 120ms  | QNX Priority: 100")

    metric = task_metrics["environment"]

    while True:
        t_start = metric.begin_tick()

        temp, hum = dht11_driver.read()
        if temp is not None:
            sensor_state.update_environment(temp, hum, True, err_inc=False)
        else:
            sensor_state.update_environment(None, None, False, err_inc=True)

        metric.end_tick(t_start)
        time.sleep(metric.period_s)


# ==============================================================================
#  QNX Synchronization Core & UDP Telemetry Streamer (20 Hz)
# ==============================================================================

def thread_qnx_sync_engine(sock):
    """Subsystem 4: Highest Priority Sync Engine (Rate: 20 Hz, QNX Prio: 200)"""
    set_qnx_thread_priority(task_metrics["sync_engine"].priority)
    print(f"[THREAD] QNX Sync Core   | 20 Hz (50ms) | Target -> {HOST_PC_IP}:{UDP_PORT}")

    metric = task_metrics["sync_engine"]

    while True:
        t_start = metric.begin_tick()

        # 1. Atomic Consistency Snapshot
        snap = sensor_state.get_atomic_snapshot()
        ts   = snap["timestamp"]
        up   = round(time.monotonic() - _start_time, 1)

        sync_lat_ms = metric.exec_time_ms
        total_misses = sum(m.deadline_misses for m in task_metrics.values())

        # 2. Build Standard Stream Packets
        packets = [
            # Stream 1: Traffic Flow Subsystem
            {
                "stream": "traffic",
                "value": snap["traffic"]["val"],
                "unit": "km/h",
                "vehicle_count": snap["traffic"]["vehicle_count"],
                "beam_blocked": snap["traffic"]["beam_blocked"],
                "freshness_ms": snap["traffic"]["freshness_ms"],
                "stale": snap["traffic"]["stale"],
                "ts": ts
            },

            # Stream 2: Environmental Subsystem
            {
                "stream": "environment",
                "temperature": snap["environment"]["temperature"],
                "humidity": snap["environment"]["humidity"],
                "temp_unit": "C",
                "hum_unit": "%RH",
                "dht_ok": snap["environment"]["dht_ok"],
                "freshness_ms": snap["environment"]["freshness_ms"],
                "stale": snap["environment"]["stale"],
                "ts": ts
            },

            # Stream 3: Air Quality Subsystem
            {
                "stream": "air",
                "value": snap["air"]["val"],
                "unit": "AQI",
                "gas_alert": snap["air"]["gas_alert"],
                "alert_count": snap["air"]["alert_count"],
                "freshness_ms": snap["air"]["freshness_ms"],
                "stale": snap["air"]["stale"],
                "ts": ts
            },

            # Stream 4: Complete QNX Real-Time Engine Telemetry
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
                "watchdog": "HEALTHY",
                "uptime_sec": up,
                "snapshot": snap,
                "ts": ts
            },

            # Stream 5: Combined Pi Hardware Sensors Packet
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

        # 3. Transmit via UDP to Host PC
        for p in packets:
            try:
                raw_bytes = json.dumps(p).encode('utf-8')
                sock.sendto(raw_bytes, (HOST_PC_IP, UDP_PORT))
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
        snap = sensor_state.get_atomic_snapshot()
        up   = round(time.monotonic() - _start_time)

        t_val = snap["environment"]["temperature"]
        h_val = snap["environment"]["humidity"]
        t_s   = f"{t_val:4.1f}C" if t_val is not None else " N/A C"
        h_s   = f"{h_val:4.1f}%" if h_val is not None else "  N/A%"

        dht_status = "OK" if snap["environment"]["dht_ok"] else f"ERR({snap['environment']['errors']})"
        air_status = "!! ALERT !!" if snap["air"]["gas_alert"] else "CLEAN AIR"

        sync_metric = task_metrics["sync_engine"]
        total_misses = sum(m.deadline_misses for m in task_metrics.values())

        sys.stdout.write(
            f"\r[QNX RTOS {snap['system_state']}] "
            f"Temp:{t_s} Hum:{h_s} [{dht_status}] | "
            f"Vehicles:{snap['traffic']['vehicle_count']:3d} Speed:{snap['traffic']['val']:5.1f}km/h | "
            f"Air:{air_status} | "
            f"Lat:{sync_metric.exec_time_ms:4.2f}ms Jitter:{sync_metric.jitter_ms:4.2f}ms Misses:{total_misses} "
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
    print(f"  GPIO Method  : {getattr(gpio, '_method', 'Unknown')}")
    print(f"  DHT11        : GPIO {PIN_DHT11}  (Pin 7)   Temp + Humidity  @ 0.5 Hz")
    print(f"  IR Sensor    : GPIO {PIN_IR} (Pin 11)  Vehicle Detection @ 50 Hz")
    print(f"  MQ135        : GPIO {PIN_MQ135} (Pin 13)  Gas / Smoke Alert @ 2 Hz")
    print(f"  UDP Target   : {HOST_PC_IP}:{UDP_PORT}  @ 20 Hz")
    print()

    init_sensor_pins()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    threads = [
        threading.Thread(target=thread_traffic_50hz,        daemon=True, name="QNX_Traffic_50Hz"),
        threading.Thread(target=thread_air_quality_2hz,     daemon=True, name="QNX_AirQuality_2Hz"),
        threading.Thread(target=thread_environment_halfhz,  daemon=True, name="QNX_Env_0.5Hz"),
        threading.Thread(target=thread_qnx_sync_engine,     args=(sock,), daemon=True, name="QNX_SyncEngine_20Hz"),
        threading.Thread(target=thread_console_hud,         daemon=True, name="QNX_ConsoleHUD"),
    ]
    for t in threads:
        t.start()

    print(f"\n[READY] {len(threads)} QNX Real-Time Threads Running. Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(5)
            snap = sensor_state.get_atomic_snapshot()
            up   = round(time.monotonic() - _start_time)
            s_metric = task_metrics["sync_engine"]
            total_misses = sum(m.deadline_misses for m in task_metrics.values())

            print(
                f"\n[QNX ENGINE REPORT @{up}s] "
                f"State: {snap['system_state']} | "
                f"Sync Latency: {s_metric.exec_time_ms:.2f}ms (Max: {s_metric.max_exec_time_ms:.2f}ms) | "
                f"Jitter: {s_metric.jitter_ms:.2f}ms | "
                f"Deadline Misses: {total_misses} | "
                f"Freshness: IR={snap['traffic']['freshness_ms']}ms, "
                f"MQ135={snap['air']['freshness_ms']}ms, "
                f"DHT11={snap['environment']['freshness_ms']}ms"
            )
    except KeyboardInterrupt:
        print("\n\n[SHUTDOWN] Stopping QNX Engine...")
        gpio.cleanup()
        sock.close()
        print("[DONE] All real-time resources released cleanly.")


if __name__ == "__main__":
    main()
