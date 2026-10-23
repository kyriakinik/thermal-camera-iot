"""
ECE490 Internet of Things - Central MQTT Subscriber Bridge
Student: Kyriaki Nikolopoulou
Course: ECE490, University of Thessaly

Role:
- Subscribes to MQTT telemetry, alerts, and heatmaps from Edge Nodes.
- InfluxDB Storage: Automatically ingests sensor metrics into InfluxDB for Grafana.
- Store-and-Forward Fail-Safe: If InfluxDB fails, records are saved into 'influx_failed_queue.csv'.
- Discord Incident Alerts: Forwards fire alarms and embedded thermal heatmaps to Discord.
- Offloads heavy HTTP/HTTPS requests and image uploads from edge nodes (Raspberry Pi).
"""

import time
import json
import base64
import csv
import os
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime
import paho.mqtt.client as mqtt

# Optional loading of .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ==========================================
# CONFIGURATION
# ==========================================

BROKER = os.getenv("MQTT_BROKER", "194.177.207.38")
PORT   = int(os.getenv("MQTT_PORT", "1883"))

# MQTT Credentials
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "team1")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")

# MQTT Topics (Wildcard '+' listens to edge08 or any team node)
TELEMETRY_TOPIC_FILTER = "iot/+/telemetry/thermal"
ALERT_TOPIC_FILTER     = "iot/+/alerts/thermal"
HEATMAP_TOPIC_FILTER   = "iot/+/heatmaps/thermal"

# InfluxDB Configuration
INFLUXDB_URL = os.getenv("INFLUXDB_URL", "http://194.177.207.38:8086")
DB_USERNAME  = os.getenv("DB_USERNAME", "edge08")
DB_PASSWORD  = os.getenv("DB_PASSWORD", "")
DEFAULT_DB   = os.getenv("DB_NAME", "edge08_db")

# Discord Webhook for Emergency Fire Notifications
DISCORD_WEBHOOK_URL    = os.getenv("DISCORD_WEBHOOK_URL", "")
ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "10"))  # Prevent spamming alerts within 10s

# Local Fail-Safe CSV files
INFLUX_FAILED_FILE = "influx_failed_queue.csv"
ALERTS_LOG_FILE    = "alerts_history.csv"

# Global state tracking
last_alert_time_by_team = {}
pending_alert_by_team   = {}

# ==========================================
# INITIALIZATION & LOGGING HELPERS
# ==========================================

def init_csv_storage():
    """Ensure dedicated fallback and alert CSV files exist with headers."""
    try:
        if not os.path.exists(INFLUX_FAILED_FILE) or os.path.getsize(INFLUX_FAILED_FILE) == 0:
            with open(INFLUX_FAILED_FILE, mode='a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    "Timestamp", "Team", "MinTemp", "AvgTemp", "MaxTemp", 
                    "HotPixels", "HotAreaRatio", "RateOfRise", "LineProtocol", "ErrorReason"
                ])
    except Exception as e:
        print(f"[ERROR] Could not initialize {INFLUX_FAILED_FILE}: {e}")

    try:
        if not os.path.exists(ALERTS_LOG_FILE) or os.path.getsize(ALERTS_LOG_FILE) == 0:
            with open(ALERTS_LOG_FILE, mode='a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    "Timestamp", "Team", "AlertState", "TriggerReason", 
                    "MaxTemp", "HotPixels", "HotAreaRatio", "RateOfRise"
                ])
    except Exception as e:
        print(f"[ERROR] Could not initialize {ALERTS_LOG_FILE}: {e}")


def log_failed_influx(team: str, metrics: dict, line_protocol: str, error_msg: str):
    """Store failed InfluxDB records in separate fallback CSV file."""
    try:
        with open(INFLUX_FAILED_FILE, mode='a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now().isoformat(),
                team,
                metrics.get('min_temp', 'N/A'),
                metrics.get('avg_temp', 'N/A'),
                metrics.get('max_temp', 'N/A'),
                metrics.get('hot_pixel_count', 'N/A'),
                metrics.get('hot_area_ratio', 'N/A'),
                metrics.get('rate_of_rise', 0.0),
                line_protocol,
                str(error_msg)
            ])
        print(f"  [FALLBACK] 💾 Failed InfluxDB write recorded into '{INFLUX_FAILED_FILE}'")
    except Exception as e:
        print(f"[ERROR] Writing to {INFLUX_FAILED_FILE} failed: {e}")


def log_alert_history(team: str, alert_info: dict):
    """Store detected incident into persistent alerts history CSV."""
    try:
        with open(ALERTS_LOG_FILE, mode='a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now().isoformat(),
                team,
                "ALERT",
                alert_info.get('alert_type', 'POSSIBLE_FIRE'),
                alert_info.get('max_temp', 'N/A'),
                alert_info.get('hot_pixels', 'N/A'),
                alert_info.get('hot_area_ratio', 'N/A'),
                alert_info.get('rate_of_rise', 0.0)
            ])
        print(f"  [AUDIT] 🚨 Incident for node '{team}' logged to '{ALERTS_LOG_FILE}'")
    except Exception as e:
        print(f"[ERROR] Writing to {ALERTS_LOG_FILE} failed: {e}")

# ==========================================
# INFLUXDB INGESTION
# ==========================================

def forward_to_influx(team: str, metrics: dict) -> bool:
    """Writes telemetry received from MQTT into InfluxDB."""
    min_temp = metrics.get('min_temp', 0.0)
    avg_temp = metrics.get('avg_temp', 0.0)
    max_temp = metrics.get('max_temp', 0.0)
    hot_pixels = metrics.get('hot_pixel_count', 0)
    rate_of_rise = metrics.get('rate_of_rise', 0.0)

    line = (
        f"thermal_data,team={team} "
        f"min_temp={min_temp:.2f},"
        f"avg_temp={avg_temp:.2f},"
        f"max_temp={max_temp:.2f},"
        f"hot_pixels={hot_pixels}i,"
        f"rate_of_rise={rate_of_rise:.2f}"
    )

    db_target = f"{team}_db" if team else DEFAULT_DB

    try:
        res = requests.post(
            f"{INFLUXDB_URL}/write",
            params={"db": db_target},
            auth=HTTPBasicAuth(team or DB_USERNAME, DB_PASSWORD),
            data=line,
            timeout=3.0
        )
        if res.status_code in (200, 204):
            return True
        else:
            log_failed_influx(team, metrics, line, f"HTTP Status {res.status_code} - {res.text}")
            return False
    except Exception as e:
        print(f"  [INFLUX ERROR] Connection failure: {e}")
        log_failed_influx(team, metrics, line, str(e))
        return False

# ==========================================
# DISCORD INCIDENT ALERT DISPATCH
# ==========================================

def send_discord_alert(alert_info: dict, image_bytes: bytes = None):
    """Sends styled rich embed card to Discord with attached thermal heatmap."""
    if not DISCORD_WEBHOOK_URL:
        return

    team_name = alert_info.get("team", "edge08")
    max_t = alert_info.get("max_temp", "N/A")
    hot_pix = alert_info.get("hot_pixels", "N/A")
    rate_rise = alert_info.get("rate_of_rise", 0.0)
    timestamp = alert_info.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    embed = {
        "title": "🚨 THERMAL ANOMALY / FIRE DETECTED",
        "description": f"**Προσοχή:** Επιβεβαιώθηκε κρίσιμη θερμική ανωμαλία στον κόμβο `{team_name}`!",
        "color": 15158332,  # Crimson Red
        "fields": [
            {
                "name": "🌡️ Μέγιστη Θερμοκρασία",
                "value": f"`{max_t} °C`",
                "inline": True
            },
            {
                "name": "🔥 Hot Pixels (>35°C)",
                "value": f"`{hot_pix} / 768`",
                "inline": True
            },
            {
                "name": "📈 Ρυθμός Ανόδου (ΔT/Δt)",
                "value": f"`{rate_rise:+.2f} °C/s`",
                "inline": True
            },
            {
                "name": "🏷️ Κόμβος Αισθητήρα",
                "value": f"`{team_name}`",
                "inline": True
            },
            {
                "name": "⏰ Ώρα Συμβάντος",
                "value": f"`{timestamp}`",
                "inline": True
            },
            {
                "name": "📊 Grafana Dashboard",
                "value": "[Live Dashboard](http://194.177.207.38:3000)",
                "inline": True
            }
        ],
        "footer": {
            "text": "ECE490 IoT • Subscriber Alerting Bridge",
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

    if not DISCORD_WEBHOOK_URL:
        print(f"  [DISCORD] ⚠️ DISCORD_WEBHOOK_URL is not set. Skipping Discord dispatch.")
        return False

    try:
        if image_bytes:
            files = {
                "payload_json": (None, json.dumps(payload_json), "application/json"),
                "files[0]": ("heatmap.png", image_bytes, "image/png")
            }
            res = requests.post(DISCORD_WEBHOOK_URL, files=files, timeout=8)
        else:
            res = requests.post(DISCORD_WEBHOOK_URL, json=payload_json, timeout=8)

        if res.status_code in (200, 204):
            print(f"  [DISCORD] 🚀 Emergency alert card dispatched successfully!")
            log_alert_history(team_name, alert_info)
            return True
        else:
            print(f"  [DISCORD] ❌ Webhook error: {res.status_code}")
            return False
    except Exception as e:
        print(f"  [DISCORD] ❌ Webhook exception: {e}")
        return False

# ==========================================
# MQTT CALLBACKS
# ==========================================

def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"[MQTT] Connected successfully to broker {BROKER}:{PORT}")
        subscriptions = [
            (TELEMETRY_TOPIC_FILTER, 0),
            (ALERT_TOPIC_FILTER, 1),
            (HEATMAP_TOPIC_FILTER, 0)
        ]
        client.subscribe(subscriptions)
        print(f"[MQTT] Subscribed to:")
        print(f"  - {TELEMETRY_TOPIC_FILTER}")
        print(f"  - {ALERT_TOPIC_FILTER}")
        print(f"  - {HEATMAP_TOPIC_FILTER}")
    else:
        print(f"[MQTT] Connection failed with error code: {rc}")


def on_message(client, userdata, msg):
    global last_alert_time_by_team, pending_alert_by_team
    now = time.time()
    topic = msg.topic

    try:
        payload = json.loads(msg.payload.decode('utf-8'))
        parts = topic.split('/')
        team = parts[1] if len(parts) >= 2 else "unknown"

        # Case 1: Periodic Telemetry -> forward to InfluxDB
        if "telemetry" in topic:
            metrics = payload.get("metrics", {})
            state   = payload.get("state", "UNKNOWN")
            influx_ok = forward_to_influx(team, metrics)
            print(f"[TELEMETRY] Node '{team}' [{state}] Max: {metrics.get('max_temp')}°C | ΔT/Δt: {metrics.get('rate_of_rise', 0.0):+.2f}°C/s | InfluxDB: {'OK' if influx_ok else 'FAILED (Saved to CSV)'}")

        # Case 2: Fire Alert Received
        elif "alerts" in topic:
            print(f"\n🔥 [FIRE ALERT RECEIVED] From '{team}' | Max: {payload.get('max_temp')}°C | Hot Pixels: {payload.get('hot_pixels')}")
            pending_alert_by_team[team] = payload

            # If no heatmap arrives within cooldown or image is delayed, send text card
            last_alert_time = last_alert_time_by_team.get(team, 0)
            if (now - last_alert_time) >= ALERT_COOLDOWN_SECONDS and not payload.get("has_heatmap"):
                # We can wait briefly for heatmap or dispatch if none expected
                pass

        # Case 3: Thermal Heatmap Received
        elif "heatmaps" in topic:
            last_alert_time = last_alert_time_by_team.get(team, 0)
            if (now - last_alert_time) >= ALERT_COOLDOWN_SECONDS:
                last_alert_time_by_team[team] = now
                
                alert_data = pending_alert_by_team.get(team, {
                    "team": team,
                    "max_temp": payload.get("max_temp", "N/A"),
                    "hot_pixels": payload.get("hot_pixels", "N/A"),
                    "timestamp": payload.get("timestamp")
                })

                b64_image = payload.get("image_base64", "")
                image_bytes = base64.b64decode(b64_image) if b64_image else None

                print(f"[DISCORD] Dispatching Alert + Thermal Heatmap for '{team}' to Discord...")
                send_discord_alert(alert_data, image_bytes)
                if team in pending_alert_by_team:
                    del pending_alert_by_team[team]

    except Exception as e:
        print(f"[ERROR] Failed processing MQTT message on topic '{topic}': {e}")

# ==========================================
# MAIN ENTRY POINT
# ==========================================

def main():
    print("=" * 65)
    print("🚀 Central MQTT Subscriber Bridge Starting")
    print(f"Target Broker: {BROKER}:{PORT}")
    print(f"InfluxDB:      {INFLUXDB_URL}")
    print(f"Fallback CSV:  {INFLUX_FAILED_FILE}")
    print(f"Alerts History:{ALERTS_LOG_FILE}")
    print("=" * 65)

    init_csv_storage()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, f"central_subscriber_{int(time.time())}")
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(BROKER, PORT)
    print("\nBridge is actively listening. Press Ctrl+C to stop.\n")

    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\n[STOP] Shutting down Subscriber Bridge...")
    finally:
        client.disconnect()
        print("[MQTT] Disconnected cleanly.")

if __name__ == "__main__":
    main()
