# ==============================================================================
# QNX Real-Time Digital Twin - Raspberry Pi Hardware GPIO Controller
# Interfaces physical buttons, LEDs, and hardware sensors on Pi GPIO pins.
# ==============================================================================

import threading
import time

try:
    import RPi.GPIO as GPIO
    HAS_GPIO = True
except ImportError:
    HAS_GPIO = False

# Pin Definitions (BCM Numbering)
PIN_BTN_FAULT_TRAFFIC = 17  # Pushbutton 1: Simulate Traffic Disconnection
PIN_BTN_FAULT_DELAY   = 27  # Pushbutton 2: Inject 500ms Delay Spike
PIN_BTN_RESET         = 22  # Pushbutton 3: Reset System State
PIN_LED_GREEN         = 23  # LED Green: System Optimal / Normal State
PIN_LED_RED           = 24  # LED Red: Fault / Deadline Breach Alarm

class HardwareGPIOController(threading.Thread):
    def __init__(self, engine):
        super().__init__(daemon=True)
        self.engine = engine
        self.running = True

        if HAS_GPIO:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)

            # Setup Input Pushbuttons with Internal Pull-Up Resistors
            GPIO.setup(PIN_BTN_FAULT_TRAFFIC, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.setup(PIN_BTN_FAULT_DELAY, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.setup(PIN_BTN_RESET, GPIO.IN, pull_up_down=GPIO.PUD_UP)

            # Setup Output Status LEDs
            GPIO.setup(PIN_LED_GREEN, GPIO.OUT)
            GPIO.setup(PIN_LED_RED, GPIO.OUT)
            
            GPIO.output(PIN_LED_GREEN, GPIO.HIGH)
            GPIO.output(PIN_LED_RED, GPIO.LOW)

    def run(self):
        if not HAS_GPIO:
            print("[GPIO] RPi.GPIO not detected. Running in software emulation mode.")
            return

        print("[GPIO] Hardware GPIO controller active. Monitoring pins 17, 27, 22...")

        while self.running:
            # Read Physical Pushbuttons (Active LOW)
            if GPIO.input(PIN_BTN_FAULT_TRAFFIC) == GPIO.LOW:
                print("\n[HARDWARE EVENT] Physical Button 1 Pressed -> Disconnecting Traffic Sensor!")
                if "traffic" in self.engine.simulators:
                    self.engine.simulators["traffic"].inject_fault(True)
                time.sleep(0.3)

            if GPIO.input(PIN_BTN_FAULT_DELAY) == GPIO.LOW:
                print("\n[HARDWARE EVENT] Physical Button 2 Pressed -> Injecting 500ms Delay Spike!")
                if "traffic" in self.engine.simulators:
                    self.engine.simulators["traffic"].inject_delay(0.5)
                time.sleep(0.3)

            if GPIO.input(PIN_BTN_RESET) == GPIO.LOW:
                print("\n[HARDWARE EVENT] Physical Button 3 Pressed -> Resetting System State!")
                for sim in self.engine.simulators.values():
                    sim.inject_fault(False)
                time.sleep(0.3)

            # Update Physical Status LEDs based on RTOS Engine State
            if self.engine.system_state.startswith("NORMAL"):
                GPIO.output(PIN_LED_GREEN, GPIO.HIGH)
                GPIO.output(PIN_LED_RED, GPIO.LOW)
            else:
                GPIO.output(PIN_LED_GREEN, GPIO.LOW)
                GPIO.output(PIN_LED_RED, GPIO.HIGH)

            time.sleep(0.05)

    def cleanup(self):
        self.running = False
        if HAS_GPIO:
            GPIO.output(PIN_LED_GREEN, GPIO.LOW)
            GPIO.output(PIN_LED_RED, GPIO.LOW)
            GPIO.cleanup()
