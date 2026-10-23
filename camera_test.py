"""
Thermal Camera - First Test
===========================
This script reads temperature data from an MLX90640 thermal camera
connected to a Raspberry Pi via I2C, prints temperature statistics,
and saves heatmap images (PNG) for each measurement.

Hardware:
- Raspberry Pi (any model with I2C)
- MLX90640 thermal camera (connected to SDA/SCL pins)

How to run:
    python3 camera_test.py

Output:
- Prints min/avg/max temperature for each measurement to the console
- Saves PNG heatmap images to the 'thermal_photos' folder
"""

import time                         # For adding delays between measurements
import numpy as np                  # For numerical operations (min, max, mean, reshape)
import matplotlib                   # Plotting library (used to generate heatmap images)
matplotlib.use('Agg')               # Use non-interactive backend (no display required on headless Pi)
import matplotlib.pyplot as plt     # Provides the actual plotting functions
import board                        # Provides pin definitions for the Raspberry Pi (SCL, SDA)
import busio                        # Provides I2C communication support
import adafruit_mlx90640            # Official Adafruit driver for the MLX90640 thermal camera
from datetime import datetime       # For generating timestamps in filenames
import os                           # For creating directories and building file paths

# ==========================================
# SETTINGS — Edit these to change behavior
# ==========================================

NUM_MEASUREMENTS = 5    # How many thermal images to capture in one run (1m → 5m)
DELAY_SECONDS    = 15   # Seconds to wait between each measurement (time to reposition)
OUTPUT_FOLDER    = "thermal_photos"  # Folder where PNG images will be saved

# Distance in meters for each measurement (1m, 2m, 3m, 4m, 5m)
DISTANCES = [1.0, 2.0, 3.0, 4.0, 5.0]

# ==========================================
# INITIALIZATION
# ==========================================

# Create the output folder if it does not already exist
if not os.path.exists(OUTPUT_FOLDER):
    os.makedirs(OUTPUT_FOLDER)
    print(f"Created output folder: {OUTPUT_FOLDER}")

# Open the I2C bus at 400 kHz (fast mode — required by MLX90640)
# board.SCL and board.SDA refer to the physical I2C pins on the Raspberry Pi
print("Initializing MLX90640 thermal camera...")
i2c = busio.I2C(board.SCL, board.SDA, frequency=400000)

# Create the MLX90640 object using the I2C bus
mlx = adafruit_mlx90640.MLX90640(i2c)

# Set the camera refresh rate to 2 Hz (2 frames per second)
# Higher rates are possible (4, 8, 16 Hz) but can be less stable over I2C
mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_2_HZ

print("Camera ready!\n")

# ==========================================
# MAIN MEASUREMENT LOOP
# ==========================================

# Pre-allocate a flat list of 768 floats (32 columns × 24 rows = 768 pixels)
# The camera driver will fill this list with temperature values on each call
frame = [0] * 768

print(f"Starting {NUM_MEASUREMENTS} measurements (every {DELAY_SECONDS}s)...")
print("You will be prompted to stand at the correct distance before each shot.")
print("=" * 50)

for i in range(1, NUM_MEASUREMENTS + 1):
    distance = DISTANCES[i - 1]   # Current target distance in meters

    # --- Distance prompt + countdown ---
    print(f"\n{'='*50}")
    print(f"  MEASUREMENT {i}/{NUM_MEASUREMENTS}  →  Stand {distance:.1f} m away")
    print(f"  Hold the LIGHTER at {distance:.1f} m from the camera")
    print(f"{'='*50}")
    for t in range(DELAY_SECONDS if i > 1 else 5, 0, -1):
        print(f"  Capturing in {t:2d}s ...", end="\r")
        time.sleep(1)
    print(f"  📸 CAPTURING NOW at {distance:.1f} m!              ")

    # Read one frame from the camera (retry up to 3 times on failure)
    # ValueError can occur occasionally due to I2C timing issues
    for attempt in range(3):
        try:
            mlx.getFrame(frame)   # Fills `frame` with 768 temperature values (°C)
            break                 # Exit retry loop on success
        except ValueError:
            print(f"  Retrying read ({attempt + 1}/3)...")
            time.sleep(0.5)       # Short wait before retrying

    # Reshape the flat list of 768 values into a 2D array: 24 rows × 32 columns
    # This represents the actual pixel grid of the MLX90640 sensor
    array = np.array(frame).reshape((24, 32))

    # Compute basic temperature statistics across all 768 pixels
    min_temp  = np.min(array)           # Coldest pixel in the frame
    max_temp  = np.max(array)           # Hottest pixel in the frame
    avg_temp  = np.mean(array)          # Average temperature of all pixels
    hot_pixels = np.sum(array > 30)    # Count of pixels above 30°C (potential heat sources)

    # Print statistics to the console
    print(f"  Min temperature   : {min_temp:.1f} °C")
    print(f"  Avg temperature   : {avg_temp:.1f} °C")
    print(f"  Max temperature   : {max_temp:.1f} °C")
    print(f"  Hot pixels (>30°C): {hot_pixels} / 768")

    # ---- Generate and save the heatmap image ----

    # Build a unique filename using the current timestamp and measurement index
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dist_str  = f"{distance:.1f}m".replace(".", "_")   # e.g. "1_5m" (safe for filenames)
    filename  = os.path.join(OUTPUT_FOLDER, f"thermal_{timestamp}_{dist_str}_measurement{i}.png")

    # Create a figure with a single axes panel (8×7 inches — taller to fit the distance label)
    fig, ax = plt.subplots(figsize=(8, 7))

    # ── Large distance label at the very top of the image ──────────────────
    fig.suptitle(
        f"📏  Distance: {distance:.1f} m",
        fontsize=22,
        fontweight='bold',
        color='white',
        backgroundcolor='#B22222',   # Deep red banner so it stands out
        y=0.98
    )

    # Display the 24×32 temperature array as a colored image
    # cmap='inferno': black (cold) → red → yellow → white (hot)
    # vmin/vmax: fix the color scale so the range is consistent within each image
    img = ax.imshow(array, cmap='inferno', interpolation='nearest',
                    vmin=min_temp - 2, vmax=max_temp + 2)

    # Add a color bar on the right side showing the temperature scale
    cbar = plt.colorbar(img, ax=ax)
    cbar.set_label('Temperature (°C)', fontsize=12)

    # Add subtitle with temperature stats (below the distance banner)
    ax.set_title(
        f"Measurement {i}/{NUM_MEASUREMENTS}\n"
        f"Min: {min_temp:.1f}°C | Avg: {avg_temp:.1f}°C | Max: {max_temp:.1f}°C",
        fontsize=11,
        pad=10
    )
    ax.set_xlabel("Pixel (horizontal, 32 columns)", fontsize=10)
    ax.set_ylabel("Pixel (vertical, 24 rows)", fontsize=10)

    # Save the figure as a PNG file at 100 DPI, then close it to free memory
    plt.tight_layout()
    plt.savefig(filename, dpi=100)
    plt.close()   # Important: prevents memory buildup when taking many measurements

    print(f"  Saved: {filename}")

    # (Delay is handled by the countdown at the top of the next iteration)

# ==========================================
# DONE
# ==========================================
print("\n" + "=" * 50)
print(f"Completed {NUM_MEASUREMENTS} measurements!")
print(f"Photos saved in folder: {OUTPUT_FOLDER}/")
print("Distances tested: " + ", ".join(f"{d:.1f}m" for d in DISTANCES))
print("You can now transfer them to your PC using: scp -r pi@<IP>:~/thermal_photos .")
