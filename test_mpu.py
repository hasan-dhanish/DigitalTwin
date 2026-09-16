#!/usr/bin/env python3
# ==============================================================================
# MPU6050 I2C Sensor Test Script for QNX / Raspberry Pi
# Reads 3-axis accelerometer and gyro data and prints live stream
# ==============================================================================

import time
import sys

try:
    from mpu6050 import mpu6050
except ImportError:
    print("[ERROR] 'mpu6050-raspberrypi' library is not installed.")
    print("Run: pip3 install smbus2 mpu6050-raspberrypi --break-system-packages")
    sys.exit(1)

def main():
    try:
        # Initialize MPU6050 at standard I2C address 0x68 on bus 1 (/dev/i2c1)
        sensor = mpu6050(0x68, bus=1)
        print("==========================================================================")
        print("  MPU6050 I2C SENSOR TEST STREAM [QNX / RASPBERRY PI]")
        print("  Reading /dev/i2c1 at address 0x68...")
        print("  Move/tilt the sensor to see live changing values below!")
        print("==========================================================================")

        while True:
            accel = sensor.get_accel_data()
            gyro = sensor.get_gyro_data()
            temp = sensor.get_temp()

            print(f"[ACCEL] X: {accel['x']:6.2f} g | Y: {accel['y']:6.2f} g | Z: {accel['z']:6.2f} g  ||  [TEMP] {temp:.1f}°C")
            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n[STOPPED] Sensor test finished.")
    except Exception as e:
        print(f"\n[ERROR] Failed to read MPU6050: {e}")
        print("Check hardware wiring (VCC->3.3V, GND->GND, SDA->Pin3, SCL->Pin5).")

if __name__ == "__main__":
    main()
