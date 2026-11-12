"""
ECE490: Internet of Things - University of Thessaly
Thermal Camera Sensor Bring-up & Calibration Test
Student: Kyriaki Nikolopoulou
Hardware: Raspberry Pi 4 + Adafruit MLX90640 (32x24 FIR sensor)

Description:
Standalone test script to verify I2C communication with the MLX90640 sensor.
Captures thermal frames across predefined distances (1.0m to 5.0m), computes
temperature metrics, and saves false-color heatmap PNG images to disk.
"""

import time
import numpy as np
import matplotlib
matplotlib.use('Agg')  # Headless mode for display-less operation
import matplotlib.pyplot as plt
import board
import busio
import adafruit_mlx90640
from datetime import datetime
import os

# ==============================================================================
# CONFIGURATION
# ==============================================================================

NUM_MEASUREMENTS = 5                 # Total measurement captures
DELAY_SECONDS    = 15                # Interval between captures (allows physical repositioning)
OUTPUT_FOLDER    = "thermal_photos"  # Directory for saved heatmap images
DISTANCES        = [1.0, 2.0, 3.0, 4.0, 5.0]  # Target distances in meters

# ==============================================================================
# SENSOR INITIALIZATION
# ==============================================================================

# Ensure output directory exists
if not os.path.exists(OUTPUT_FOLDER):
    os.makedirs(OUTPUT_FOLDER)
    print(f"Created output folder: {OUTPUT_FOLDER}")

# Initialize I2C bus at 400 kHz (Fast-Mode required for MLX90640)
print("Initializing MLX90640 thermal sensor...")
i2c = busio.I2C(board.SCL, board.SDA, frequency=400000)

mlx = adafruit_mlx90640.MLX90640(i2c)
mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_2_HZ

print("Sensor ready.\n")

# ==============================================================================
# MEASUREMENT LOOP
# ==============================================================================

# 768 elements corresponding to 32x24 pixels
frame = [0] * 768

print(f"Starting {NUM_MEASUREMENTS} test measurements (every {DELAY_SECONDS}s)...")
print("Position heat source at specified distances when prompted.\n")

for i in range(1, NUM_MEASUREMENTS + 1):
    distance = DISTANCES[i - 1]

    print("=" * 55)
    print(f"  MEASUREMENT {i}/{NUM_MEASUREMENTS} | Distance: {distance:.1f} m")
    print(f"  Position heat source at {distance:.1f} m from camera")
    print("=" * 55)

    # Countdown delay before capture
    countdown = DELAY_SECONDS if i > 1 else 5
    for t in range(countdown, 0, -1):
        print(f"  Capturing in {t:2d}s ...", end="\r")
        time.sleep(1)
    print(f"  Capturing frame at {distance:.1f} m...          ")

    # Read frame with retry mechanism (handles occasional I2C timing glitches)
    captured = False
    for attempt in range(3):
        try:
            mlx.getFrame(frame)
            captured = True
            break
        except ValueError:
            print(f"  Retry attempt {attempt + 1}/3...")
            time.sleep(0.5)

    if not captured:
        print("  [ERROR] Frame capture failed. Skipping measurement.")
        continue

    # Reshape 1D array into 24x32 matrix
    thermal_data = np.array(frame).reshape((24, 32))

    # Calculate summary metrics
    min_temp = np.min(thermal_data)
    avg_temp = np.mean(thermal_data)
    max_temp = np.max(thermal_data)

    print(f"  Min: {min_temp:.2f}°C | Avg: {avg_temp:.2f}°C | Max: {max_temp:.2f}°C")

    # Generate and save false-color heatmap
    fig, ax = plt.subplots(figsize=(6, 4.5))
    im = ax.imshow(thermal_data, cmap='inferno', interpolation='bilinear')
    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label('Temperature (°C)')

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ax.set_title(f"Thermal Scan ({distance:.1f}m) - {timestamp}\nMax: {max_temp:.1f}°C | Avg: {avg_temp:.1f}°C")

    filename = f"thermal_{timestamp}_{str(distance).replace('.', '_')}m_measurement{i}.png"
    filepath = os.path.join(OUTPUT_FOLDER, filename)

    plt.savefig(filepath, format='png', bbox_inches='tight', dpi=100)
    plt.close(fig)
    print(f"  Saved heatmap to: {filepath}\n")

print("All measurements completed successfully.")
