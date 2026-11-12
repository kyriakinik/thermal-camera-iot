"""
ECE490: Internet of Things - University of Thessaly
Term Project: Adaptive Thermal Fire Detection Node
Student: Kyriaki Nikolopoulou
Hardware: Raspberry Pi 4 (edge08) + Adafruit MLX90640 (32x24 FIR camera)

Description:
Edge computing node running on Raspberry Pi. Interfaces with the MLX90640
thermal sensor via I2C, computes real-time statistical metrics, dynamically
adjusts sampling intervals based on threat level, and publishes telemetry
and fire alerts over MQTT.
"""

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
matplotlib.use('Agg')  # Headless mode for display-less Raspberry Pi
import matplotlib.pyplot as plt
import csv
from datetime import datetime
import random
import requests
from requests.auth import HTTPBasicAuth
import os

# Optional environment variable support
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ==============================================================================
# CONFIGURATION & THRESHOLDS
# ==============================================================================

# MQTT Broker connection settings
BROKER = os.getenv("MQTT_BROKER", "194.177.207.38")
PORT   = int(os.getenv("MQTT_PORT", "1883"))

# Device hostname resolution (defaults to node hostname, e.g. 'edge08')
TEAM = os.getenv("TEAM", socket.gethostname())

# Broker authentication credentials
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "team1")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")

# Dedicated MQTT topics per node
TELEMETRY_TOPIC = f"iot/{TEAM}/telemetry/thermal"
ALERT_TOPIC     = f"iot/{TEAM}/alerts/thermal"
HEATMAP_TOPIC   = f"iot/{TEAM}/heatmaps/thermal"

# InfluxDB settings (used in standalone direct-push mode)
INFLUXDB_URL = os.getenv("INFLUXDB_URL", "http://194.177.207.38:8086")
DB_NAME      = os.getenv("DB_NAME", f"{TEAM}_db")
DB_USERNAME  = os.getenv("DB_USERNAME", TEAM)
DB_PASSWORD  = os.getenv("DB_PASSWORD", "")

# Discord Webhook settings (used in standalone mode)
DISCORD_WEBHOOK_URL      = os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "10"))

# Architecture mode:
# False: Pure Edge mode (publishes MQTT only; processing is offloaded to subscriber_bridge)
# True:  Standalone mode (executes HTTP POST directly to InfluxDB and Discord)
ENABLE_DIRECT_CLOUD_PUSH = False

# Thermal detection thresholds (°C)
HOT_PIXEL_THRESHOLD    = 35.0   # Temperature above which a pixel is considered hot
WARNING_THRESHOLD      = 40.0   # Threshold to transition from NORMAL to WATCH state
ALERT_THRESHOLD        = 50.0   # Threshold to evaluate potential fire conditions
ALERT_HOLD_SECONDS     = 3.0    # Required duration of high temperature before confirming fire

# Spatial validation constraint (filters out single-pixel transient noise)
MIN_ALERT_HOT_PIXELS   = 3      # Minimum contiguous/active hot pixels required for alert
MIN_ALERT_AREA_RATIO   = 0.0039 # 3 / 768 total pixels (~0.39% of frame area)

# Adaptive sampling intervals (seconds)
RATE_NORMAL = 5.0   # Baseline monitoring under safe conditions
RATE_WATCH  = 2.0   # Elevated surveillance when abnormal warmth is detected
RATE_ALERT  = 0.5   # High-frequency tracking during confirmed fire events

# Local persistent log files
LOCAL_LOG_FILE         = "thermal_measurements.csv"  # Full telemetry backup
INFLUX_FAILED_LOG_FILE = "influx_failed_queue.csv"    # Store-and-forward failed writes queue
ALERTS_LOG_FILE        = "alerts_history.csv"         # Confirmed fire incident history

# Global state trackers
current_state             = "NORMAL"
high_condition_started_at = None
last_discord_alert_time   = 0
prev_max_temp             = None
prev_sample_time          = None
client_id                 = f"thermal_node_{random.randint(0, 1000)}"

# ==============================================================================
# LOGGING & FAIL-SAFE HELPERS
# ==============================================================================

def init_csv_logs():
    """Initializes local CSV log files and writes headers if files are new."""
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
        print(f"[ERROR] Telemetry CSV initialization failed: {e}")

    # 2. InfluxDB Store-and-Forward Backup Queue
    try:
        with open(INFLUX_FAILED_LOG_FILE, mode='a', newline='') as file:
            writer = csv.writer(file)
            if file.tell() == 0:
                writer.writerow([
                    "Timestamp", "Team", "MinTemp", "AvgTemp", "MaxTemp", 
                    "HotPixels", "HotAreaRatio", "LineProtocol", "ErrorMessage"
                ])
    except Exception as e:
        print(f"[ERROR] Failed InfluxDB queue CSV initialization failed: {e}")

    # 3. Incident History Audit Log
    try:
        with open(ALERTS_LOG_FILE, mode='a', newline='') as file:
            writer = csv.writer(file)
            if file.tell() == 0:
                writer.writerow([
                    "Timestamp", "Team", "AlertState", "TriggerReason", 
                    "MaxTemp", "HotPixels", "HotAreaRatio", "RateOfRise_C_per_s"
                ])
    except Exception as e:
        print(f"[ERROR] Alerts CSV initialization failed: {e}")


def log_to_csv(state: str, metrics: dict):
    """Appends a single telemetry record to local storage."""
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
        print(f"[ERROR] Writing to telemetry CSV failed: {e}")


def log_failed_influx(metrics: dict, line_protocol: str, error_msg: str):
    """Saves failed InfluxDB write requests for offline store-and-forward recovery."""
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
        print(f"  [FAIL-SAFE] Record saved to offline queue: '{INFLUX_FAILED_LOG_FILE}'")
    except Exception as e:
        print(f"[ERROR] Writing to InfluxDB backup CSV failed: {e}")


def log_alert_event(reason: str, metrics: dict):
    """Records confirmed fire incidents to local audit log."""
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
        print(f"  [AUDIT] Fire incident recorded in '{ALERTS_LOG_FILE}'")
    except Exception as e:
        print(f"[ERROR] Writing to alerts CSV failed: {e}")


def write_to_influx(metrics: dict) -> bool:
    """Formats telemetry to InfluxDB Line Protocol and submits via HTTP POST."""
    min_temp = metrics['min_temp']
    avg_temp = metrics['avg_temp']
    max_temp = metrics['max_temp']
    hot_pixels = metrics['hot_pixel_count']
    rate_of_rise = metrics.get('rate_of_rise', 0.0)

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
        print(f"  [INFLUX] Direct write failed: {e}")
        log_failed_influx(metrics, line, str(e))
        return False


def generate_heatmap_bytes(frame_data) -> bytes:
    """
    Renders a false-color thermal heatmap in memory (io.BytesIO).
    Avoids writing temporary image files to MicroSD to eliminate flash wear.
    """
    try:
        # Reshape 768 elements into the physical 24x32 sensor matrix
        array = np.array(frame_data).reshape((24, 32))
        
        fig = plt.figure(figsize=(4, 3))
        plt.imshow(array, cmap='inferno', interpolation='nearest')
        plt.colorbar(label='Temperature (°C)')
        plt.title(f'Thermal Anomaly - {datetime.now().strftime("%H:%M:%S")}')
        
        # Save output into memory buffer
        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight')
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        print(f"[ERROR] Heatmap rendering failed: {e}")
        return None


def send_discord_alert(metrics: dict, image_bytes: bytes = None):
    """Dispatches a formatted emergency card to Discord (standalone fallback)."""
    if not DISCORD_WEBHOOK_URL:
        return
    
    embed = {
        "title": "🚨 THERMAL ANOMALY / FIRE DETECTED",
        "description": f"**Προσοχή:** Εντοπίστηκε κρίσιμη αύξηση θερμοκρασίας στον κόμβο `{TEAM}`!",
        "color": 15158332,
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
            files = {
                "payload_json": (None, json.dumps(payload_json), "application/json"),
                "files[0]": ("heatmap.png", image_bytes, "image/png")
            }
            res = requests.post(DISCORD_WEBHOOK_URL, files=files, timeout=6)
        else:
            res = requests.post(DISCORD_WEBHOOK_URL, json=payload_json, timeout=6)
        
        if res.status_code in (200, 204):
            print("  [DISCORD] Emergency notification posted successfully.")
        else:
            print(f"  [DISCORD] Webhook returned status code: {res.status_code}")
    except Exception as e:
        print(f"  [DISCORD] Webhook dispatch error: {e}")

# ==============================================================================
# MQTT CLIENT SETUP
# ==============================================================================

def on_connect(client, userdata, flags, rc, properties=None):
    """Callback executed upon establishing MQTT broker connection."""
    if rc == 0:
        print(f"[MQTT] Connected cleanly to broker {BROKER}:{PORT}")
    else:
        print(f"[MQTT] Connection failed with error code: {rc}")


def connect_mqtt():
    """Configures and connects Paho MQTT v2 client instance."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id)
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.connect(BROKER, PORT)
    return client

# ==============================================================================
# MAIN APPLICATION LOOP
# ==============================================================================

def main():
    global current_state, high_condition_started_at, last_discord_alert_time, prev_max_temp, prev_sample_time
    
    print("=" * 65)
    print(f"Adaptive Thermal Edge Node Starting on [{TEAM}]")
    print(f"Architecture Mode: {'Standalone Direct Push' if ENABLE_DIRECT_CLOUD_PUSH else 'MQTT Edge Offloaded (Recommended)'}")
    print("=" * 65)

    # Initialize local CSV logs
    init_csv_logs()

    # Establish MQTT connection loop
    mqtt_client = connect_mqtt()
    mqtt_client.loop_start()

    # Initialize MLX90640 thermal sensor via I2C Bus 1 (Fast-Mode 400kHz)
    print("[CAMERA] Initializing MLX90640 over I2C bus...")
    try:
        i2c = busio.I2C(board.SCL, board.SDA, frequency=400000)
        mlx = adafruit_mlx90640.MLX90640(i2c)
        mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_2_HZ
        frame = [0] * 768
        print("[CAMERA] Sensor communication established.")
        
        # Sensor stabilization: discard initial frames while internal calibration registers settle
        print("[CAMERA] Warming up sensor (discarding initial 3 frames)...")
        for _ in range(3):
            try:
                mlx.getFrame(frame)
            except (ValueError, RuntimeError, OSError):
                pass
            time.sleep(0.4)
        print("[CAMERA] Sensor ready for monitoring.\n")
    except Exception as e:
        print(f"[ERROR] Camera initialization failed: {e}")
        print("Tip: Verify physical wiring on SDA (Pin 3) and SCL (Pin 5), and ensure I2C is enabled.")
        return

    measurement_idx = 0
    print("Edge monitoring active. Press Ctrl+C to terminate.\n")
    
    try:
        while True:
            # ------------------------------------------------------------------
            # 1. Capture thermal frame with retry mechanism
            # ------------------------------------------------------------------
            captured = False
            for attempt in range(5):
                try:
                    mlx.getFrame(frame)
                    captured = True
                    break
                except (ValueError, RuntimeError, OSError):
                    time.sleep(0.15)
            
            if not captured:
                time.sleep(0.3)
                continue

            measurement_idx += 1

            # ------------------------------------------------------------------
            # 2. Extract statistical temperature metrics
            # ------------------------------------------------------------------
            min_temp = round(float(np.min(frame)), 2)
            max_temp = round(float(np.max(frame)), 2)
            avg_temp = round(float(np.mean(frame)), 2)
            
            # Hot pixels count and area coverage ratio
            hot_pixels     = int(sum(1 for temp in frame if temp > HOT_PIXEL_THRESHOLD))
            hot_area_ratio = round(hot_pixels / 768.0, 4)

            # Rate of temperature rise (ΔT / Δt) in °C/s
            now = time.time()
            rate_of_rise = 0.0
            if prev_max_temp is not None and prev_sample_time is not None:
                dt = now - prev_sample_time
                if dt > 0.05:
                    rate_of_rise = round((max_temp - prev_max_temp) / dt, 2)
            
            prev_max_temp = max_temp
            prev_sample_time = now

            metrics = {
                "min_temp": min_temp,
                "max_temp": max_temp,
                "avg_temp": avg_temp,
                "hot_pixel_count": hot_pixels,
                "hot_area_ratio": hot_area_ratio,
                "rate_of_rise": rate_of_rise
            }

            # ------------------------------------------------------------------
            # 3. 3-State Machine with Dual Confirmation Filter
            # ------------------------------------------------------------------
            # Dual criteria: temperature threshold AND spatial footprint (prevents noise triggers)
            has_fire_temp = (max_temp >= ALERT_THRESHOLD)
            has_sufficient_area = (hot_pixels >= MIN_ALERT_HOT_PIXELS and hot_area_ratio >= MIN_ALERT_AREA_RATIO)

            new_state = current_state

            if has_fire_temp and has_sufficient_area:
                if high_condition_started_at is None:
                    high_condition_started_at = now
                    new_state = "WATCH"  # Begin temporal confirmation window
                elif (now - high_condition_started_at) >= ALERT_HOLD_SECONDS:
                    new_state = "ALERT"  # Sustained heat confirmed
                else:
                    new_state = "WATCH"
            elif max_temp >= WARNING_THRESHOLD or (has_fire_temp and not has_sufficient_area):
                new_state = "WATCH"      # Elevated observation state
                high_condition_started_at = None
            else:
                new_state = "NORMAL"     # Baseline ambient state
                high_condition_started_at = None

            # State transition notification and incident logging
            if new_state != current_state:
                print(f"\n[STATE TRANSITION] {current_state} -> {new_state} (Max: {max_temp}°C, Hot Pixels: {hot_pixels})")
                if new_state == "ALERT":
                    log_alert_event(f"Fire confirmed: >= {ALERT_THRESHOLD}°C for >= {ALERT_HOLD_SECONDS}s with {hot_pixels} hot pixels", metrics)
                current_state = new_state

            # ------------------------------------------------------------------
            # 4. Adaptive sampling interval selection
            # ------------------------------------------------------------------
            if current_state == "ALERT":
                sleep_time = RATE_ALERT   # 0.5s: fast tracking
            elif current_state == "WATCH" or high_condition_started_at is not None:
                sleep_time = RATE_WATCH   # 2.0s: active observation
            else:
                sleep_time = RATE_NORMAL  # 5.0s: low-power baseline

            # ------------------------------------------------------------------
            # 5. Publish telemetry via MQTT (QoS 0)
            # ------------------------------------------------------------------
            telemetry_payload = {
                "team": TEAM,
                "timestamp": datetime.now().isoformat(),
                "state": current_state,
                "metrics": metrics
            }
            mqtt_client.publish(TELEMETRY_TOPIC, json.dumps(telemetry_payload), qos=0)

            # ------------------------------------------------------------------
            # 6. Database persistence & local logging
            # ------------------------------------------------------------------
            influx_status_str = "MQTT-Delegated"
            if ENABLE_DIRECT_CLOUD_PUSH:
                influx_ok = write_to_influx(metrics)
                influx_status_str = "OK" if influx_ok else "FAILED (Saved to CSV)"

            log_to_csv(current_state, metrics)

            # Console telemetry summary
            print(f"#{measurement_idx:04d} [{current_state:6s}] Min: {min_temp:.1f}°C | Avg: {avg_temp:.1f}°C | Max: {max_temp:.1f}°C | ΔT/Δt: {rate_of_rise:+.2f}°C/s | Hot: {hot_pixels:2d}/768 ({hot_area_ratio*100:.1f}%) | Influx: {influx_status_str}")

            # ------------------------------------------------------------------
            # 7. Fire alert broadcast & in-memory heatmap transmission
            # ------------------------------------------------------------------
            if current_state == "ALERT":
                # Fire alarms are published with QoS 1 to guarantee delivery
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
                
                # Render heatmap in RAM and encode as Base64 string
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

                # Standalone mode: send direct Discord notification with rate limiting
                if ENABLE_DIRECT_CLOUD_PUSH:
                    if (now - last_discord_alert_time) >= DISCORD_COOLDOWN_SECONDS:
                        last_discord_alert_time = now
                        send_discord_alert(metrics, image_bytes)

            # ------------------------------------------------------------------
            # 8. Adaptive pause
            # ------------------------------------------------------------------
            time.sleep(sleep_time)

    except KeyboardInterrupt:
        print("\n[SYSTEM] Terminating monitoring loop...")
    finally:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        print("[MQTT] Disconnected from broker cleanly.")


if __name__ == "__main__":
    main()
