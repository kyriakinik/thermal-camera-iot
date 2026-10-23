# ==============================================================================
# ECE490: Internet of Things - University of Thessaly
# Term Project: Adaptive Thermal Fire Detection Node
# Student: Kyriaki Nikolopoulou
# Hardware: Raspberry Pi 4 (edge08) + Adafruit MLX90640 (32x24 FIR camera)
# ==============================================================================
# Hey! This script runs on our Raspberry Pi edge node.
# It talks to the MLX90640 thermal sensor over I2C, extracts statistical features
# from each 32x24 frame (min/avg/max, hot pixels, rate of temp rise), and dynamically
# adapts its sampling frequency depending on how hot things get.
#
# Note on Architecture:
# To keep the Raspberry Pi from overheating and wasting battery/power on heavy
# HTTP/HTTPS requests, we keep this edge script super lightweight: it ONLY publishes
# data to MQTT! The companion script 'subscriber_bridge.py' running on a laptop or
# server picks up the MQTT feed and takes care of InfluxDB writes and Discord alerts.
# ==============================================================================

import time
import math
import socket
import board
import busio
import adafruit_mlx90640
import paho.mqtt.client as mqtt
import json
import base64
import io
import numpy as np
import matplotlib
matplotlib.use('Agg')  # Headless mode: Raspberry Pi doesn't have a monitor attached!
import matplotlib.pyplot as plt
import csv
from datetime import datetime
import random
import requests
from requests.auth import HTTPBasicAuth
import os

# Optional loading of .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ==========================================
# CONFIGURATION & SETTINGS
# ==========================================

# Public MQTT broker at UTH (accessible from home without university VPN)
BROKER = os.getenv("MQTT_BROKER", "194.177.207.38")
PORT   = int(os.getenv("MQTT_PORT", "1883"))

# Automatically grab our Raspberry Pi hostname (resolves to 'edge08' on our board)
TEAM = os.getenv("TEAM", socket.gethostname())

# MQTT broker credentials provided for lab team 1
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "team1")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")

# MQTT Topics: we organize them by team so messages don't collide on the shared broker
TELEMETRY_TOPIC = f"iot/{TEAM}/telemetry/thermal"
ALERT_TOPIC     = f"iot/{TEAM}/alerts/thermal"
HEATMAP_TOPIC   = f"iot/{TEAM}/heatmaps/thermal"

# InfluxDB database settings (for our Grafana dashboard panels)
INFLUXDB_URL = os.getenv("INFLUXDB_URL", "http://194.177.207.38:8086")
DB_NAME      = os.getenv("DB_NAME", f"{TEAM}_db")
DB_USERNAME  = os.getenv("DB_USERNAME", TEAM)
DB_PASSWORD  = os.getenv("DB_PASSWORD", "")

# Discord Webhook URL for our #fire-alerts channel
# (Used directly only if someone turns on ENABLE_DIRECT_CLOUD_PUSH for testing)
DISCORD_WEBHOOK_URL      = os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "10"))  # Wait at least 10s between Discord alerts so we don't spam the chat!

# ==========================================
# SYSTEM ARCHITECTURE TOGGLE
# ==========================================
# ENABLE_DIRECT_CLOUD_PUSH:
# - False:
#   Pure lightweight Edge mode! The Raspberry Pi ONLY publishes through MQTT.
#   Our central 'subscriber_bridge.py' script handles InfluxDB and Discord alerts.
#   This saves tons of CPU cycles and power on the Pi.
# - True:
#   Edge node makes direct HTTP POST calls to InfluxDB and Discord directly.
#   Handy for quick standalone testing when running without the subscriber.
ENABLE_DIRECT_CLOUD_PUSH = False

# Detection thresholds (temperatures in Celsius)
HOT_PIXEL_THRESHOLD    = 35.0   # A pixel is considered "warm/hot" if > 35°C
WARNING_THRESHOLD      = 40.0   # If max temp exceeds 40°C, switch from NORMAL to WATCH mode
ALERT_THRESHOLD        = 50.0   # If max temp exceeds 50°C, we have potential fire!
ALERT_HOLD_SECONDS     = 3.0    # Flame must persist for at least 3 seconds to confirm it's real

# Spatial constraint: flame must have some actual size on the sensor!
# If only 1 pixel is hot, it's probably sensor noise or a momentary reflection,
# so we require at least 3 hot pixels (approx 0.39% of the frame) to trigger ALERT.
MIN_ALERT_HOT_PIXELS   = 3      # Minimum hot pixels needed to confirm fire
MIN_ALERT_AREA_RATIO   = 0.0039 # 3 / 768 total pixels ≈ 0.39% of the camera view

# Adaptive sampling intervals (in seconds)
# Slower when room is cold/safe to save power; faster when things get suspicious!
RATE_NORMAL = 5.0   # Normal safe state: sample once every 5 seconds
RATE_WATCH  = 2.0   # Watch state (warm): sample every 2 seconds to keep an eye on it
RATE_ALERT  = 0.5   # Fire alert: sample 2 times per second (0.5s) for real-time tracking

# Local CSV backup files stored right on the Pi
LOCAL_LOG_FILE         = "thermal_measurements.csv"  # All periodic measurements
INFLUX_FAILED_LOG_FILE = "influx_failed_queue.csv"    # Failed InfluxDB writes (store-and-forward)
ALERTS_LOG_FILE        = "alerts_history.csv"         # Permanent history of confirmed fire events

# ==========================================
# GLOBAL VARIABLES (Tracking State)
# ==========================================
current_state             = "NORMAL"  # Starts at NORMAL on boot
high_condition_started_at = None      # Timestamp when temp first crossed 50°C
last_discord_alert_time   = 0         # Cooldown timer tracker for Discord
prev_max_temp             = None      # Previous frame's max temp (for rate of rise)
prev_sample_time          = None      # Previous frame's timestamp (for rate of rise)
client_id                 = f"thermal_node_{random.randint(0, 1000)}"

# ==========================================
# HELPER FUNCTIONS & LOGGING
# ==========================================

def init_csv_logs():
    """
    Creates our local CSV files with proper headers if they don't exist yet.
    We keep three separate files to make debugging and grading super easy:
    1. thermal_measurements.csv -> full telemetry
    2. influx_failed_queue.csv  -> only failed database writes
    3. alerts_history.csv       -> log of fire alerts
    """
    # 1. Main Telemetry Log
    try:
        with open(LOCAL_LOG_FILE, mode='a', newline='') as file:
            writer = csv.writer(file)
            if file.tell() == 0:
                writer.writerow([
                    "Timestamp", "State", "MinTemp", "AvgTemp", "MaxTemp", 
                    "HotPixels", "HotAreaRatio", "RateOfRise_C_per_s"
                ])
    except Exception as e:
        print(f"[ERROR] Could not init telemetry CSV: {e}")

    # 2. InfluxDB Failed Writes Backup Queue
    try:
        with open(INFLUX_FAILED_LOG_FILE, mode='a', newline='') as file:
            writer = csv.writer(file)
            if file.tell() == 0:
                writer.writerow([
                    "Timestamp", "Team", "MinTemp", "AvgTemp", "MaxTemp", 
                    "HotPixels", "HotAreaRatio", "LineProtocol", "ErrorMessage"
                ])
    except Exception as e:
        print(f"[ERROR] Could not init InfluxDB failed queue CSV: {e}")

    # 3. Dedicated Alerts History Log
    try:
        with open(ALERTS_LOG_FILE, mode='a', newline='') as file:
            writer = csv.writer(file)
            if file.tell() == 0:
                writer.writerow([
                    "Timestamp", "Team", "AlertState", "TriggerReason", 
                    "MaxTemp", "HotPixels", "HotAreaRatio", "RateOfRise_C_per_s"
                ])
    except Exception as e:
        print(f"[ERROR] Could not init alerts CSV: {e}")


def log_to_csv(state: str, metrics: dict):
    """Saves one row of telemetry into our local CSV file as a backup."""
    try:
        with open(LOCAL_LOG_FILE, mode='a', newline='') as file:
            writer = csv.writer(file)
            writer.writerow([
                datetime.now().isoformat(),
                state,
                f"{metrics['min_temp']:.2f}",
                f"{metrics['avg_temp']:.2f}",
                f"{metrics['max_temp']:.2f}",
                metrics['hot_pixel_count'],
                f"{metrics['hot_area_ratio']:.4f}",
                f"{metrics.get('rate_of_rise', 0.0):.2f}"
            ])
    except Exception as e:
        print(f"[ERROR] Could not write to CSV: {e}")


def log_failed_influx(metrics: dict, line_protocol: str, error_msg: str):
    """
    Store-and-forward fail-safe!
    If InfluxDB is unreachable or times out, we don't want to throw the data away.
    Instead, we log it into 'influx_failed_queue.csv' so we can replay it later.
    """
    try:
        with open(INFLUX_FAILED_LOG_FILE, mode='a', newline='') as file:
            writer = csv.writer(file)
            writer.writerow([
                datetime.now().isoformat(),
                TEAM,
                f"{metrics['min_temp']:.2f}",
                f"{metrics['avg_temp']:.2f}",
                f"{metrics['max_temp']:.2f}",
                metrics['hot_pixel_count'],
                f"{metrics['hot_area_ratio']:.4f}",
                line_protocol,
                str(error_msg)
            ])
        print(f"  [STORE-AND-FORWARD] 💾 InfluxDB write failed -> safely saved to '{INFLUX_FAILED_LOG_FILE}'")
    except Exception as e:
        print(f"[ERROR] Could not log to failed Influx CSV: {e}")


def log_alert_event(reason: str, metrics: dict):
    """
    Logs confirmed fire incidents into alerts_history.csv.
    This fulfills Course Objective 11 ('store alerts locally for evaluation').
    """
    try:
        with open(ALERTS_LOG_FILE, mode='a', newline='') as file:
            writer = csv.writer(file)
            writer.writerow([
                datetime.now().isoformat(),
                TEAM,
                "ALERT",
                reason,
                f"{metrics['max_temp']:.2f}",
                metrics['hot_pixel_count'],
                f"{metrics['hot_area_ratio']:.4f}",
                f"{metrics.get('rate_of_rise', 0.0):.2f}"
            ])
        print(f"  [AUDIT] 🚨 Fire incident saved to '{ALERTS_LOG_FILE}'")
    except Exception as e:
        print(f"[ERROR] Could not log alert event: {e}")


def write_to_influx(metrics: dict) -> bool:
    """
    Formats the measurement into InfluxDB Line Protocol and sends HTTP POST.
    If the network fails, it immediately diverts the record to our fallback CSV!
    """
    min_temp = metrics['min_temp']
    avg_temp = metrics['avg_temp']
    max_temp = metrics['max_temp']
    hot_pixels = metrics['hot_pixel_count']
    rate_of_rise = metrics.get('rate_of_rise', 0.0)

    # InfluxDB line protocol format: measurement,tags fields timestamp
    line = (
        f"thermal_data,team={TEAM} "
        f"min_temp={min_temp:.2f},"
        f"avg_temp={avg_temp:.2f},"
        f"max_temp={max_temp:.2f},"
        f"hot_pixels={hot_pixels}i,"
        f"rate_of_rise={rate_of_rise:.2f}"
    )

    try:
        response = requests.post(
            f"{INFLUXDB_URL}/write",
            params={"db": DB_NAME},
            auth=HTTPBasicAuth(DB_USERNAME, DB_PASSWORD),
            data=line,
            timeout=2.0
        )
        if response.status_code in (200, 204):
            return True
        else:
            log_failed_influx(metrics, line, f"HTTP Status {response.status_code}")
            return False
    except Exception as e:
        print(f"  [INFLUX] Write failed: {e}")
        log_failed_influx(metrics, line, str(e))
        return False


def generate_heatmap_bytes(frame_data) -> bytes:
    """
    Creates a false-color thermal heatmap image directly in RAM memory (io.BytesIO).
    
    Why we do this:
    A classic rookie mistake on Raspberry Pi projects is saving temporary PNG files 
    to the MicroSD card. Constant writes wear out flash memory cells and can corrupt 
    the SD card within weeks! By using BytesIO, the image stays 100% in RAM and 
    never touches the disk.
    """
    try:
        # Reshape the 768 float values into the camera's physical 24x32 pixel grid
        array = np.array(frame_data).reshape((24, 32))
        
        fig = plt.figure(figsize=(4, 3))
        # 'inferno' colormap works great: cold areas are dark purple, flames shine bright yellow!
        plt.imshow(array, cmap='inferno', interpolation='nearest')
        plt.colorbar(label='Temperature (°C)')
        plt.title(f'Thermal Anomaly - {datetime.now().strftime("%H:%M:%S")}')
        
        # Save image straight into our in-memory buffer
        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight')
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        print(f"[ERROR] Heatmap rendering failed: {e}")
        return None


def send_discord_alert(metrics: dict, image_bytes: bytes = None):
    """
    Builds a styled Discord emergency embed card with live stats and attaches
    the thermal heatmap image. (Used directly if running in standalone push mode).
    """
    if not DISCORD_WEBHOOK_URL:
        return
    
    embed = {
        "title": "🚨 THERMAL ANOMALY / FIRE DETECTED",
        "description": f"**Προσοχή:** Εντοπίστηκε κρίσιμη αύξηση θερμοκρασίας στον κόμβο `{TEAM}`!",
        "color": 15158332,  # Crimson Red
        "fields": [
            {
                "name": "🌡️ Μέγιστη Θερμοκρασία",
                "value": f"`{metrics['max_temp']} °C`",
                "inline": True
            },
            {
                "name": "🔥 Hot Pixels (>35°C)",
                "value": f"`{metrics['hot_pixel_count']} / 768`",
                "inline": True
            },
            {
                "name": "📈 Ρυθμός Ανόδου (ΔT/Δt)",
                "value": f"`{metrics.get('rate_of_rise', 0.0):+.2f} °C/s`",
                "inline": True
            },
            {
                "name": "📊 Κατάσταση",
                "value": "🔴 `ALERT (FIRE)`",
                "inline": True
            },
            {
                "name": "🏷️ Edge Node",
                "value": f"`{TEAM}`",
                "inline": True
            },
            {
                "name": "⏰ Ώρα Συμβάντος",
                "value": f"`{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`",
                "inline": True
            },
            {
                "name": "📈 Grafana Dashboard",
                "value": "[Άνοιγμα Dashboard](http://194.177.207.38:3000)",
                "inline": True
            }
        ],
        "footer": {
            "text": "ECE490 IoT • Adaptive Thermal Fire Detection Node",
            "icon_url": "https://cdn-icons-png.flaticon.com/512/785/785116.png"
        }
    }

    if image_bytes:
        embed["image"] = {"url": "attachment://heatmap.png"}

    payload_json = {
        "username": "🔥 Thermal Fire Bot",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/785/785116.png",
        "embeds": [embed]
    }

    try:
        if image_bytes:
            # Send multipart form data to upload the in-memory PNG file
            files = {
                "payload_json": (None, json.dumps(payload_json), "application/json"),
                "files[0]": ("heatmap.png", image_bytes, "image/png")
            }
            res = requests.post(DISCORD_WEBHOOK_URL, files=files, timeout=6)
        else:
            res = requests.post(DISCORD_WEBHOOK_URL, json=payload_json, timeout=6)
        
        if res.status_code in (200, 204):
            print("  [DISCORD] 🚀 Emergency alert and heatmap posted to Discord channel!")
        else:
            print(f"  [DISCORD] ❌ Webhook error: {res.status_code}")
    except Exception as e:
        print(f"  [DISCORD] ❌ Could not send alert: {e}")

# ==========================================
# MQTT CLIENT SETUP
# ==========================================

def on_connect(client, userdata, flags, rc, properties=None):
    """Callback triggered as soon as we connect to the Mosquitto broker."""
    if rc == 0:
        print(f"[MQTT] Connected cleanly to broker {BROKER}:{PORT}")
    else:
        print(f"[MQTT] Connection failed with error code: {rc}")


def connect_mqtt():
    """Initializes and returns our Paho MQTT v2 client."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id)
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.connect(BROKER, PORT)
    return client

# ==========================================
# MAIN EXECUTION LOOP
# ==========================================

def main():
    global current_state, high_condition_started_at, last_discord_alert_time, prev_max_temp, prev_sample_time
    
    print("=" * 65)
    print(f"🔥 Adaptive Thermal Edge Node Starting on [{TEAM}] 🔥")
    print(f"Mode: {'DIRECT PUSH (Standalone)' if ENABLE_DIRECT_CLOUD_PUSH else 'MQTT EDGE MODE (Recommended: Offloaded to Subscriber)'}")
    print("=" * 65)

    # Make sure all our CSV files (telemetry, failed queue, alerts) are ready
    init_csv_logs()

    # Start the background network thread for MQTT
    mqtt_client = connect_mqtt()
    mqtt_client.loop_start()

    # Initialize MLX90640 thermal sensor over I2C bus 1 (Fast-Mode 400kHz)
    print("[CAMERA] Initializing MLX90640 over I2C bus...")
    try:
        # 400 kHz fast mode is needed because each thermal frame transfers 768 16-bit values!
        i2c = busio.I2C(board.SCL, board.SDA, frequency=400000)
        mlx = adafruit_mlx90640.MLX90640(i2c)
        mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_2_HZ
        frame = [0] * 768  # Flat array to hold our 24x32 temperature points
        print("[CAMERA] Sensor communication established.")
        
        # Sensor warm-up trick:
        # The MLX90640 needs 2 to 3 frames after power-up to settle its internal calibration registers.
        # If you read right away on boot, it sometimes spits out bogus 180°C values!
        print("[CAMERA] Warming up sensor (discarding first 3 calibration frames)...")
        for _ in range(3):
            try:
                mlx.getFrame(frame)
            except (ValueError, RuntimeError, OSError):
                pass
            time.sleep(0.4)
        print("[CAMERA] Sensor ready to monitor!\n")
    except Exception as e:
        print(f"[ERROR] Camera init failed: {e}")
        print("Tip: Check your jumper wires on pins 3 (SDA) and 5 (SCL), and verify I2C is enabled in raspi-config.")
        return

    measurement_idx = 0
    print("Edge monitoring active. Press Ctrl+C to stop.\n")
    
    try:
        while True:
            # -------------------------------------------------------------
            # STEP 1: Grab thermal frame with retry loop
            # -------------------------------------------------------------
            # Reading 768 floats over I2C can occasionally hit a timing hiccup or clock stretching.
            # We give it up to 5 attempts before giving up on a frame, so our script never crashes!
            captured = False
            for attempt in range(5):
                try:
                    mlx.getFrame(frame)
                    captured = True
                    break
                except (ValueError, RuntimeError, OSError):
                    time.sleep(0.15)  # Brief pause before retrying
            
            if not captured:
                # If all 5 attempts failed, skip this cycle cleanly and try again
                time.sleep(0.3)
                continue

            measurement_idx += 1

            # -------------------------------------------------------------
            # STEP 2: Extract Statistical Features from the 768 Pixels
            # -------------------------------------------------------------
            min_temp = round(float(np.min(frame)), 2)
            max_temp = round(float(np.max(frame)), 2)
            avg_temp = round(float(np.mean(frame)), 2)
            
            # Count how many pixels are warmer than our 35°C threshold
            hot_pixels     = int(sum(1 for temp in frame if temp > HOT_PIXEL_THRESHOLD))
            hot_area_ratio = round(hot_pixels / 768.0, 4)

            # Calculate Rate of Rise (ΔT / Δt) in °C/sec (Course Objective 5!)
            # This captures rapid heating dynamics: a lighter flame jumps up by +15°C/s,
            # whereas normal environmental warming is very slow (<0.1°C/s).
            now = time.time()
            rate_of_rise = 0.0
            if prev_max_temp is not None and prev_sample_time is not None:
                dt = now - prev_sample_time
                if dt > 0.05:
                    rate_of_rise = round((max_temp - prev_max_temp) / dt, 2)
            
            prev_max_temp = max_temp
            prev_sample_time = now

            # Bundle all metrics into a clean dictionary
            metrics = {
                "min_temp": min_temp,
                "max_temp": max_temp,
                "avg_temp": avg_temp,
                "hot_pixel_count": hot_pixels,
                "hot_area_ratio": hot_area_ratio,
                "rate_of_rise": rate_of_rise
            }

            # -------------------------------------------------------------
            # STEP 3: Adaptive 3-State Machine with Dual Confirmation Filter
            # -------------------------------------------------------------
            # Dual-criterion fire confirmation:
            # 1) Temperature must reach >= 50°C continuously for at least 3 seconds
            # 2) Flame area must NOT be a tiny single-pixel artifact (hot_pixels >= 3)
            # This filters out spurious dead pixels or momentary reflections!
            has_fire_temp = (max_temp >= ALERT_THRESHOLD)
            has_sufficient_area = (hot_pixels >= MIN_ALERT_HOT_PIXELS and hot_area_ratio >= MIN_ALERT_AREA_RATIO)

            new_state = current_state

            if has_fire_temp and has_sufficient_area:
                if high_condition_started_at is None:
                    high_condition_started_at = now
                    new_state = "WATCH"  # Temp is high -> enter watch mode and start our 3-second timer
                elif (now - high_condition_started_at) >= ALERT_HOLD_SECONDS:
                    new_state = "ALERT"  # Flame stayed for 3 seconds AND had sufficient area -> CONFIRM FIRE!
                else:
                    new_state = "WATCH"
            elif max_temp >= WARNING_THRESHOLD or (has_fire_temp and not has_sufficient_area):
                # Mildly warm (>= 40°C) or single-pixel noise: stay alert in WATCH mode
                new_state = "WATCH"
                high_condition_started_at = None
            else:
                # Safe ambient room temperature
                new_state = "NORMAL"
                high_condition_started_at = None

            # Print state transition to console and log incident if we just entered ALERT
            if new_state != current_state:
                print(f"\n⚠️ [STATE CHANGE] {current_state} ➜ {new_state} (Max: {max_temp}°C, Hot Pixels: {hot_pixels})")
                if new_state == "ALERT":
                    log_alert_event(f"Fire confirmed: >= {ALERT_THRESHOLD}°C for >= {ALERT_HOLD_SECONDS}s with {hot_pixels} hot pixels", metrics)
                current_state = new_state

            # -------------------------------------------------------------
            # STEP 4: Adaptive Sampling Rate Selection
            # -------------------------------------------------------------
            # We adapt our sampling speed according to threat level:
            if current_state == "ALERT":
                sleep_time = RATE_ALERT  # 0.5s: high-frequency real-time fire tracking!
            elif current_state == "WATCH" or high_condition_started_at is not None:
                sleep_time = RATE_WATCH  # 2.0s: observation mode
            else:
                sleep_time = RATE_NORMAL # 5.0s: low-power safe baseline

            # -------------------------------------------------------------
            # STEP 5: Publish Periodic Telemetry via MQTT
            # -------------------------------------------------------------
            # We use QoS 0 here because telemetry is sent frequently; minimal network overhead!
            telemetry_payload = {
                "team": TEAM,
                "timestamp": datetime.now().isoformat(),
                "state": current_state,
                "metrics": metrics
            }
            mqtt_client.publish(TELEMETRY_TOPIC, json.dumps(telemetry_payload), qos=0)

            # -------------------------------------------------------------
            # STEP 6: InfluxDB Storage & Local CSV Logging
            # -------------------------------------------------------------
            # In our new decoupled architecture, InfluxDB is handled by subscriber_bridge.py
            # But if someone runs standalone mode, write_to_influx() is called here
            influx_status_str = "MQTT-Delegated"
            if ENABLE_DIRECT_CLOUD_PUSH:
                influx_ok = write_to_influx(metrics)
                influx_status_str = "OK" if influx_ok else "FAILED (Saved to CSV)"

            # Save full telemetry to our local CSV file on the Pi
            log_to_csv(current_state, metrics)

            # Print a neat summary line to the terminal
            print(f"#{measurement_idx:04d} [{current_state:6s}] Min: {min_temp:.1f}°C | Avg: {avg_temp:.1f}°C | Max: {max_temp:.1f}°C | ΔT/Δt: {rate_of_rise:+.2f}°C/s | Hot: {hot_pixels:2d}/768 ({hot_area_ratio*100:.1f}%) | Influx: {influx_status_str}")

            # -------------------------------------------------------------
            # STEP 7: Fire Alarm Broadcast & Heatmap Transmission
            # -------------------------------------------------------------
            if current_state == "ALERT":
                # We publish the fire alarm with QoS 1 so MQTT guarantees delivery
                alert_payload = {
                    "alert_type": "POSSIBLE_FIRE",
                    "team": TEAM,
                    "max_temp": max_temp,
                    "hot_pixels": hot_pixels,
                    "hot_area_ratio": hot_area_ratio,
                    "rate_of_rise": rate_of_rise,
                    "timestamp": datetime.now().isoformat()
                }
                mqtt_client.publish(ALERT_TOPIC, json.dumps(alert_payload), qos=1)
                
                print("  🔥 [FIRE ALERT] Rendering in-memory heatmap...")
                image_bytes = generate_heatmap_bytes(frame)
                if image_bytes:
                    base64_image = base64.b64encode(image_bytes).decode('utf-8')
                    heatmap_payload = {
                        "team": TEAM,
                        "image_base64": base64_image,
                        "max_temp": max_temp,
                        "hot_pixels": hot_pixels,
                        "timestamp": datetime.now().isoformat()
                    }
                    mqtt_client.publish(HEATMAP_TOPIC, json.dumps(heatmap_payload), qos=0)

                # If standalone direct-push mode is enabled, dispatch Discord webhook directly
                if ENABLE_DIRECT_CLOUD_PUSH:
                    if (now - last_discord_alert_time) >= DISCORD_COOLDOWN_SECONDS:
                        last_discord_alert_time = now
                        send_discord_alert(metrics, image_bytes)

            # -------------------------------------------------------------
            # STEP 8: Sleep for our adaptive interval
            # -------------------------------------------------------------
            time.sleep(sleep_time)

    except KeyboardInterrupt:
        print("\n[SYSTEM] Stopped by user (Ctrl+C). Cleaning up...")
    finally:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        print("[MQTT] Disconnected from broker cleanly. Bye!")


if __name__ == "__main__":
    main()
