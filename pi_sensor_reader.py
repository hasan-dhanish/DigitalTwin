#!/usr/bin/env python3
# ==============================================================================
#  QNX Real-Time Sensor Reader for Raspberry Pi 4
#  Sensor Wiring:
#    DHT11  (Temp/Humidity)    DATA  → GPIO 4  (Pin 7)
#    IR Sensor (Traffic/Obs.)  OUT   → GPIO 17 (Pin 11)
#    MQ135  (Air Quality/Gas)  DO    → GPIO 27 (Pin 13)
#
#  GPIO Library: pigpio  (works on QNX via /dev/mem + pigpiod daemon)
#  Protocol:     DHT11 implemented as pure-Python bit-bang over pigpio
#  Transport:    UDP JSON stream → Host PC Digital Twin
#
#  Usage:
#    sudo pigpiod                        # Start pigpio daemon (must run first)
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

# ------------------------------------------------------------------------------
#  Configuration — Pin numbers use BCM (Broadcom) numbering
# ------------------------------------------------------------------------------

PIN_DHT11  = 4    # GPIO 4  → Pin 7  — DHT11 DATA line
PIN_IR     = 17   # GPIO 17 → Pin 11 — IR Sensor OUT (Active LOW on detection)
PIN_MQ135  = 27   # GPIO 27 → Pin 13 — MQ135 Digital Output (Active LOW = gas)

DHT11_READ_INTERVAL_SEC = 2.0   # DHT11 minimum safe poll interval (spec: >=1s)
IR_POLL_INTERVAL_SEC    = 0.02  # 50 Hz — fast enough to catch vehicle pulses
MQ135_POLL_INTERVAL_SEC = 0.5   # 2 Hz — gas detection

UDP_PUBLISH_INTERVAL_SEC = 0.05  # 20 Hz UDP telemetry stream rate

HOST_PC_IP   = sys.argv[1] if len(sys.argv) > 1 else "10.12.2.121"
UDP_PORT     = int(sys.argv[2]) if len(sys.argv) > 2 else 9999

# ------------------------------------------------------------------------------
#  pigpio Initialization  (preferred GPIO library for QNX / bare-metal access)
# ------------------------------------------------------------------------------

HAS_PIGPIO = False
pi = None

try:
    import pigpio
    pi = pigpio.pi()                    # Connect to local pigpiod daemon
    if not pi.connected:
        raise RuntimeError("pigpiod daemon not reachable — run: sudo pigpiod")
    HAS_PIGPIO = True
    print("[HARDWARE] pigpio connected to pigpiod daemon successfully.")
except ImportError:
    print("[NOTICE] pigpio not installed (pip3 install pigpio). Falling back to RPi.GPIO.")
except Exception as e:
    print(f"[NOTICE] pigpio daemon error: {e}")

# Fallback: try RPi.GPIO if pigpio is unavailable
HAS_GPIO = False
GPIO = None

if not HAS_PIGPIO:
    try:
        import RPi.GPIO as GPIO_lib
        GPIO = GPIO_lib
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        # IR and MQ135 are digital inputs
        GPIO.setup(PIN_IR,    GPIO.IN)
        GPIO.setup(PIN_MQ135, GPIO.IN)
        # DHT11 is bidirectional — set up dynamically during reads
        HAS_GPIO = True
        print("[HARDWARE] RPi.GPIO initialized (pigpio unavailable).")
    except Exception as e:
        print(f"[NOTICE] No GPIO library available ({e}). Running in simulation mode.")

# ------------------------------------------------------------------------------
#  Shared Telemetry State  (thread-safe via Lock)
# ------------------------------------------------------------------------------

_lock = threading.Lock()

_state = {
    # DHT11 — Environmental Subsystem
    "temperature_c":  None,   # deg C
    "humidity_pct":   None,   # %RH

    # IR Sensor — Traffic Flow Subsystem
    "vehicle_count":  0,      # Cumulative vehicles detected
    "ir_blocked":     False,  # True = beam currently interrupted
    "traffic_speed":  0.0,    # Estimated km/h from pulse rate

    # MQ135 — Air Quality Subsystem
    "gas_detected":   False,  # True = DO pin LOW (threshold exceeded)
    "gas_alert_count": 0,     # Cumulative alert events

    # System
    "last_dht_read":  0.0,
    "last_ir_read":   0.0,
    "last_mq_read":   0.0,
    "uptime_sec":     0.0,
    "errors":         0,
}

_start_time = time.time()

# ------------------------------------------------------------------------------
#  DHT11 Bit-Bang Driver  (pure Python, works with pigpio waveform timing)
# ------------------------------------------------------------------------------

class DHT11Driver:
    """
    Implements the DHT11 single-wire protocol via pigpio bit-bang GPIO.

    DHT11 Protocol:
      1. Host pulls DATA LOW for >=18ms  (start signal)
      2. Host releases HIGH, waits 20-40us
      3. DHT pulls LOW for 80us, then HIGH for 80us  (ACK)
      4. 40 bits transmitted: 8 humidity int + 8 humidity dec
                               + 8 temp int + 8 temp dec + 8 checksum
      Each bit: LOW 50us -> HIGH 26-28us (bit=0) or HIGH 70us (bit=1)
    """

    def __init__(self, gpio_pin, pi_handle=None):
        self.pin = gpio_pin
        self.pi = pi_handle          # pigpio handle
        self._last_temp = None
        self._last_hum  = None

    def _read_pigpio(self):
        """Read DHT11 using pigpio precise timing (preferred method)."""
        p = self.pi

        # Step 1: Send start signal — pull LOW for 18ms
        p.set_mode(self.pin, pigpio.OUTPUT)
        p.write(self.pin, pigpio.LOW)
        time.sleep(0.018)             # 18ms

        # Step 2: Release the line — MCU pulls HIGH
        p.set_mode(self.pin, pigpio.INPUT)
        p.set_pull_up_down(self.pin, pigpio.PUD_UP)

        # Step 3: Capture raw edge timing using pigpio callbacks
        edges = []
        cb = p.callback(self.pin, pigpio.EITHER_EDGE,
                         lambda gpio, level, tick: edges.append((level, tick)))

        time.sleep(0.003)            # Wait 3ms to capture all 40 bits

        cb.cancel()

        return self._decode_edges(edges)

    def _decode_edges(self, edges):
        """
        Decode DHT11 timing edges into temperature and humidity.
        HIGH pulse < 40us  -> bit 0
        HIGH pulse >= 40us -> bit 1
        """
        if len(edges) < 84:
            # Need at least 2 edges per bit x 40 bits + preamble edges
            return None, None

        bits = []
        i = 0
        # Find HIGH pulses between consecutive falling edges
        while i < len(edges) - 1:
            if edges[i][0] == 1 and edges[i + 1][0] == 0:
                # Measure HIGH pulse duration in us
                duration_us = (edges[i + 1][1] - edges[i][1]) & 0xFFFFFFFF
                if duration_us > 10:   # Filter noise
                    bits.append(1 if duration_us >= 40 else 0)
                if len(bits) == 40:
                    break
            i += 1

        if len(bits) < 40:
            return None, None

        # Reconstruct 5 bytes
        bytes_data = []
        for byte_idx in range(5):
            byte_val = 0
            for bit_idx in range(8):
                byte_val = (byte_val << 1) | bits[byte_idx * 8 + bit_idx]
            bytes_data.append(byte_val)

        # Verify checksum
        checksum = (bytes_data[0] + bytes_data[1] +
                    bytes_data[2] + bytes_data[3]) & 0xFF
        if checksum != bytes_data[4]:
            return None, None   # Checksum mismatch -> discard

        humidity    = bytes_data[0] + bytes_data[1] * 0.1
        temperature = bytes_data[2] + bytes_data[3] * 0.1

        # Sanity bounds: DHT11 range is 0-50 deg C, 20-90% RH
        if not (0 <= temperature <= 60) or not (0 <= humidity <= 100):
            return None, None

        return round(temperature, 1), round(humidity, 1)

    def _read_gpio_bitbang(self):
        """
        Fallback DHT11 bit-bang using RPi.GPIO (less precise timing).
        Uses Python busy-loop timing — functional but may miss bits at high CPU load.
        """
        pin = self.pin

        # Send start pulse
        GPIO.setup(pin, GPIO.OUT)
        GPIO.output(pin, GPIO.LOW)
        time.sleep(0.018)
        GPIO.output(pin, GPIO.HIGH)
        time.sleep(0.00004)           # 40us

        # Switch to input and read response
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        # Wait for DHT ACK LOW
        timeout = time.time() + 0.001
        while GPIO.input(pin) == GPIO.HIGH:
            if time.time() > timeout:
                return None, None

        # Wait for ACK HIGH
        timeout = time.time() + 0.001
        while GPIO.input(pin) == GPIO.LOW:
            if time.time() > timeout:
                return None, None

        # Wait for ACK HIGH end
        timeout = time.time() + 0.001
        while GPIO.input(pin) == GPIO.HIGH:
            if time.time() > timeout:
                return None, None

        # Read 40 bits
        bits = []
        for _ in range(40):
            # Wait for LOW -> HIGH (start of bit HIGH pulse)
            timeout = time.time() + 0.0005
            while GPIO.input(pin) == GPIO.LOW:
                if time.time() > timeout:
                    return None, None
            high_start = time.time()

            # Wait for HIGH -> LOW (end of bit)
            timeout = time.time() + 0.0005
            while GPIO.input(pin) == GPIO.HIGH:
                if time.time() > timeout:
                    return None, None
            high_duration = (time.time() - high_start) * 1_000_000  # us

            bits.append(1 if high_duration >= 40 else 0)

        # Reconstruct bytes
        bytes_data = []
        for byte_idx in range(5):
            val = 0
            for bit_idx in range(8):
                val = (val << 1) | bits[byte_idx * 8 + bit_idx]
            bytes_data.append(val)

        # Verify checksum
        if ((bytes_data[0] + bytes_data[1] +
             bytes_data[2] + bytes_data[3]) & 0xFF) != bytes_data[4]:
            return None, None

        hum  = bytes_data[0] + bytes_data[1] * 0.1
        temp = bytes_data[2] + bytes_data[3] * 0.1

        if not (0 <= temp <= 60) or not (0 <= hum <= 100):
            return None, None

        return round(temp, 1), round(hum, 1)

    def read(self):
        """
        Read DHT11. Returns (temperature_c, humidity_pct) or (None, None) on failure.
        Caches last valid reading to return on transient failure.
        """
        try:
            if self.pi is not None and HAS_PIGPIO:
                temp, hum = self._read_pigpio()
            elif HAS_GPIO:
                temp, hum = self._read_gpio_bitbang()
            else:
                # Simulation fallback
                t = time.time()
                temp = round(28.0 + 5.0 * math.sin(t * 0.01), 1)
                hum  = round(60.0 + 10.0 * math.cos(t * 0.007), 1)
                return temp, hum

            if temp is not None:
                self._last_temp = temp
                self._last_hum  = hum

        except Exception:
            pass  # Return cached values on error

        return self._last_temp, self._last_hum


# ------------------------------------------------------------------------------
#  DHT11 Thread  — Environmental Subsystem
# ------------------------------------------------------------------------------

dht11_driver = DHT11Driver(PIN_DHT11, pi_handle=pi if HAS_PIGPIO else None)

def thread_dht11():
    """Reads DHT11 every 2 seconds (sensor minimum stable interval)."""
    print(f"[THREAD] DHT11 Environmental Thread started (GPIO {PIN_DHT11}, 0.5 Hz)")
    while True:
        temp_c, hum_pct = dht11_driver.read()

        with _lock:
            if temp_c is not None:
                _state["temperature_c"] = temp_c
                _state["humidity_pct"]  = hum_pct
            _state["last_dht_read"] = time.time()

        time.sleep(DHT11_READ_INTERVAL_SEC)


# ------------------------------------------------------------------------------
#  IR Sensor Thread  — Traffic Flow Subsystem
# ------------------------------------------------------------------------------

def _read_ir_digital():
    """Returns True if IR beam is currently blocked (vehicle present)."""
    if HAS_PIGPIO and pi:
        return pi.read(PIN_IR) == 0   # Active LOW: 0 = object detected
    elif HAS_GPIO:
        return GPIO.input(PIN_IR) == GPIO.LOW
    else:
        # Simulate occasional vehicle pulses
        t = time.time()
        return (int(t * 3) % 7) == 0   # Pulse for ~50ms every ~2.3s

def thread_ir_sensor():
    """
    Detects vehicles by monitoring falling edges on the IR OUT pin (Active LOW).
    Computes estimated traffic speed from the pulse rate (vehicles per minute).

    Speed estimation logic:
      - Count vehicles passing in a rolling 30-second window
      - vehicles_per_min x 2.0 = estimated km/h
      - Clamped to 10 - 120 km/h range
    """
    print(f"[THREAD] IR Traffic Sensor Thread started (GPIO {PIN_IR}, 50 Hz)")
    last_state       = False
    vehicle_times    = []   # Rolling timestamps of recent vehicle detections
    SPEED_WINDOW_SEC = 30   # Compute speed over last 30 seconds

    while True:
        current_state = _read_ir_digital()

        if current_state and not last_state:
            # Falling edge: beam became blocked -> new vehicle detected
            now = time.time()
            vehicle_times.append(now)

            # Prune timestamps older than the rolling window
            cutoff = now - SPEED_WINDOW_SEC
            vehicle_times = [t for t in vehicle_times if t >= cutoff]

            # vehicles/min x calibration_factor = km/h estimate
            vpm = (len(vehicle_times) / SPEED_WINDOW_SEC) * 60
            traffic_speed = round(min(120.0, max(10.0, vpm * 2.0)), 1)

            with _lock:
                _state["vehicle_count"]  += 1
                _state["ir_blocked"]      = True
                _state["traffic_speed"]   = traffic_speed

        elif not current_state and last_state:
            # Rising edge: beam cleared -> vehicle fully passed
            with _lock:
                _state["ir_blocked"] = False

        last_state = current_state

        with _lock:
            _state["last_ir_read"] = time.time()

        time.sleep(IR_POLL_INTERVAL_SEC)


# ------------------------------------------------------------------------------
#  MQ135 Thread  — Air Quality / Gas Alert Subsystem
# ------------------------------------------------------------------------------

def _read_mq135_digital():
    """Returns True if gas concentration exceeds threshold (DO pin LOW = alert)."""
    if HAS_PIGPIO and pi:
        return pi.read(PIN_MQ135) == 0   # Active LOW: 0 = gas detected
    elif HAS_GPIO:
        return GPIO.input(PIN_MQ135) == GPIO.LOW
    else:
        # Simulate brief gas spikes every ~30 seconds
        t = time.time()
        return (int(t) % 30) < 2

def thread_mq135():
    """Monitors MQ135 Digital Output for gas/smoke threshold crossing alerts."""
    print(f"[THREAD] MQ135 Air Quality Thread started (GPIO {PIN_MQ135}, 2 Hz)")
    was_alert = False

    while True:
        alert = _read_mq135_digital()

        with _lock:
            _state["gas_detected"] = alert
            if alert and not was_alert:
                # New alert event — increment counter
                _state["gas_alert_count"] += 1
            _state["last_mq_read"] = time.time()

        was_alert = alert
        time.sleep(MQ135_POLL_INTERVAL_SEC)


# ------------------------------------------------------------------------------
#  UDP Publisher Thread  — Streams Telemetry JSON to Host PC
# ------------------------------------------------------------------------------

def thread_udp_publisher(sock):
    """
    Publishes sensor telemetry as UDP JSON packets at 20 Hz.
    Emits 4 stream packets per cycle for Digital Twin receiver compatibility:
      - 'environment'  : temp + humidity (DHT11)
      - 'traffic'      : speed + vehicle count (IR sensor)
      - 'air'          : AQI value + gas alert flag (MQ135)
      - 'pi_sensors'   : combined full-state snapshot
    """
    print(f"[THREAD] UDP Publisher Thread started -> {HOST_PC_IP}:{UDP_PORT} @ 20 Hz")

    while True:
        with _lock:
            snap = dict(_state)   # Atomic snapshot

        now = time.time()
        snap["uptime_sec"] = round(now - _start_time, 1)

        # Environmental Subsystem Packet (DHT11)
        env_packet = {
            "stream":       "environment",
            "temperature":  snap["temperature_c"],
            "humidity":     snap["humidity_pct"],
            "temp_unit":    "C",
            "hum_unit":     "%RH",
            "ts":           now
        }

        # Traffic Flow Subsystem Packet (IR Sensor)
        traffic_packet = {
            "stream":          "traffic",
            "value":           snap["traffic_speed"],
            "unit":            "km/h",
            "vehicle_count":   snap["vehicle_count"],
            "beam_blocked":    snap["ir_blocked"],
            "ts":              now
        }

        # Air Quality Subsystem Packet (MQ135)
        # Map digital alert to AQI: 85 AQI (unhealthy) if alert, 22 AQI (good) otherwise
        air_packet = {
            "stream":       "air",
            "value":        85.0 if snap["gas_detected"] else 22.0,
            "unit":         "AQI",
            "gas_alert":    snap["gas_detected"],
            "alert_count":  snap["gas_alert_count"],
            "ts":           now
        }

        # Full-State Snapshot Packet
        full_packet = {
            "stream":           "pi_sensors",
            "temperature_c":    snap["temperature_c"],
            "humidity_pct":     snap["humidity_pct"],
            "traffic_speed":    snap["traffic_speed"],
            "vehicle_count":    snap["vehicle_count"],
            "gas_alert":        snap["gas_detected"],
            "gas_alert_count":  snap["gas_alert_count"],
            "uptime_sec":       snap["uptime_sec"],
            "ts":               now
        }

        for pkt in [env_packet, traffic_packet, air_packet, full_packet]:
            try:
                data = json.dumps(pkt).encode("utf-8")
                sock.sendto(data, (HOST_PC_IP, UDP_PORT))
            except Exception:
                pass

        time.sleep(UDP_PUBLISH_INTERVAL_SEC)


# ------------------------------------------------------------------------------
#  Console Status Renderer
# ------------------------------------------------------------------------------

BANNER = """\
+==============================================================================+
|       QNX  REAL-TIME  SENSOR  READER  -  Raspberry Pi 4 (4GB RAM)          |
|  Sensors: DHT11 (GPIO 4) | IR Sensor (GPIO 17) | MQ135 (GPIO 27)           |
+==============================================================================+"""

def render_console():
    """Renders a live single-line status line to stdout at 5 Hz."""
    while True:
        with _lock:
            snap = dict(_state)

        temp  = f"{snap['temperature_c']:5.1f}C"  if snap["temperature_c"] is not None else "  N/A C"
        hum   = f"{snap['humidity_pct']:5.1f}%"   if snap["humidity_pct"]  is not None else "   N/A%"
        spd   = f"{snap['traffic_speed']:6.1f} km/h"
        cnt   = snap["vehicle_count"]
        gas   = "!! GAS ALERT !!" if snap["gas_detected"] else "  CLEAN AIR  "
        alerts = snap["gas_alert_count"]
        uptime = round(time.time() - _start_time, 0)
        hw     = "HARDWARE" if (HAS_PIGPIO or HAS_GPIO) else "SIMULATED"

        sys.stdout.write(
            f"\r[{hw}] "
            f"Temp:{temp} Hum:{hum} | "
            f"Traffic:{spd} Cars:{cnt:4d} | "
            f"Air:{gas} GasAlerts:{alerts:3d} | "
            f"Up:{uptime:.0f}s      "
        )
        sys.stdout.flush()
        time.sleep(0.2)


# ------------------------------------------------------------------------------
#  GPIO Pin Setup
# ------------------------------------------------------------------------------

def init_pigpio_pins():
    """Configure GPIO pin modes via pigpio before launching threads."""
    if not HAS_PIGPIO or not pi:
        return
    pi.set_mode(PIN_IR,    pigpio.INPUT)
    pi.set_pull_up_down(PIN_IR,    pigpio.PUD_UP)
    pi.set_mode(PIN_MQ135, pigpio.INPUT)
    pi.set_pull_up_down(PIN_MQ135, pigpio.PUD_UP)
    # DHT11 direction is managed dynamically inside DHT11Driver.read()
    print(f"[HARDWARE] pigpio pins configured: IR=GPIO{PIN_IR}, MQ135=GPIO{PIN_MQ135}, DHT11=GPIO{PIN_DHT11}")


# ------------------------------------------------------------------------------
#  Entry Point
# ------------------------------------------------------------------------------

def main():
    os.system("cls" if os.name == "nt" else "clear")
    print(BANNER)
    print()
    print(f"  GPIO Backend : {'pigpio (daemon)' if HAS_PIGPIO else 'RPi.GPIO (fallback)' if HAS_GPIO else 'SIMULATION (no hardware)'}")
    print(f"  DHT11        : GPIO {PIN_DHT11}  (Header Pin 7)  - Temp + Humidity @ 0.5 Hz")
    print(f"  IR Sensor    : GPIO {PIN_IR} (Header Pin 11) - Vehicle Detection @ 50 Hz")
    print(f"  MQ135        : GPIO {PIN_MQ135} (Header Pin 13) - Gas / Smoke Alert @ 2 Hz")
    print(f"  UDP Stream   : -> {HOST_PC_IP}:{UDP_PORT}  @ 20 Hz")
    print()

    init_pigpio_pins()

    # UDP socket — non-blocking, fire-and-forget
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    threads = [
        threading.Thread(target=thread_dht11,                        daemon=True, name="DHT11-Env"),
        threading.Thread(target=thread_ir_sensor,                    daemon=True, name="IR-Traffic"),
        threading.Thread(target=thread_mq135,                        daemon=True, name="MQ135-Air"),
        threading.Thread(target=thread_udp_publisher, args=(sock,),  daemon=True, name="UDP-Pub"),
        threading.Thread(target=render_console,                       daemon=True, name="Console"),
    ]

    for t in threads:
        t.start()

    print(f"[READY] {len(threads)} threads running. Press Ctrl+C to stop.\n")

    try:
        while True:
            # Main thread prints a summary every 10 seconds
            time.sleep(10.0)
            with _lock:
                snap = dict(_state)
            print(
                f"\n[SUMMARY @{round(time.time() - _start_time)}s] "
                f"Temp={snap['temperature_c']}C  "
                f"Hum={snap['humidity_pct']}%  "
                f"Traffic={snap['traffic_speed']}km/h  "
                f"Vehicles={snap['vehicle_count']}  "
                f"Gas={'ALERT' if snap['gas_detected'] else 'OK'}  "
                f"GasAlerts={snap['gas_alert_count']}"
            )
    except KeyboardInterrupt:
        print("\n\n[SHUTDOWN] Stopping sensor reader cleanly...")
        if HAS_PIGPIO and pi:
            pi.stop()
        elif HAS_GPIO:
            GPIO.cleanup()
        sock.close()
        print("[DONE] All GPIO resources released.")


if __name__ == "__main__":
    main()
