"""
ECE490: Internet of Things - University of Thessaly
MQTT Discord Notifier Bridge (Alternative Standalone Listener)
Student: Kyriaki Nikolopoulou

Description:
Lightweight background listener that subscribes specifically to MQTT fire
alerts and heatmap topics, dispatching emergency cards to Discord.
"""

import time
import json
import base64
import os
import requests
import paho.mqtt.client as mqtt
from datetime import datetime

# Optional environment variable support
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ==============================================================================
# CONFIGURATION
# ==============================================================================

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

BROKER = os.getenv("MQTT_BROKER", "194.177.207.38")
PORT   = int(os.getenv("MQTT_PORT", "1883"))
TEAM   = os.getenv("TEAM", "team1")

MQTT_USERNAME = os.getenv("MQTT_USERNAME", "team1")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")

ALERT_TOPIC   = f"iot/{TEAM}/alerts/thermal"
HEATMAP_TOPIC = f"iot/{TEAM}/heatmaps/thermal"

ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "10"))
last_alert_time        = 0
pending_alert_data     = None

# ==============================================================================
# NOTIFICATION DISPATCH
# ==============================================================================

def send_discord_notification(alert_info: dict, image_bytes: bytes = None):
    """Sends a formatted emergency embed to Discord with optional heatmap image."""
    max_t = alert_info.get("max_temp", "N/A")
    hot_pix = alert_info.get("hot_pixels", "N/A")
    timestamp = alert_info.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    team_name = alert_info.get("team", TEAM)

    embed = {
        "title": "🚨 THERMAL ANOMALY / FIRE DETECTED",
        "description": f"**Προσοχή:** Εντοπίστηκε κρίσιμη αύξηση θερμοκρασίας στον κόμβο `{team_name}`!",
        "color": 15158332,
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
                "name": "📊 Κατάσταση",
                "value": "🔴 `ALERT (FIRE)`",
                "inline": True
            },
            {
                "name": "🏷️ Edge Node",
                "value": f"`{team_name}`",
                "inline": True
            },
            {
                "name": "⏰ Ώρα Συμβάντος",
                "value": f"`{timestamp}`",
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

    if not DISCORD_WEBHOOK_URL:
        print("[DISCORD] DISCORD_WEBHOOK_URL not configured. Skipping notification.")
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
            print("[DISCORD] Emergency notification delivered.")
            return True
        else:
            print(f"[DISCORD] Webhook returned status code: {res.status_code}")
            return False
    except Exception as e:
        print(f"[DISCORD] Webhook exception: {e}")
        return False

# ==============================================================================
# MQTT CALLBACKS
# ==============================================================================

def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"[MQTT] Connected cleanly to broker {BROKER}:{PORT}")
        client.subscribe([(ALERT_TOPIC, 1), (HEATMAP_TOPIC, 0)])
        print(f"[MQTT] Subscribed to {ALERT_TOPIC} and {HEATMAP_TOPIC}")
    else:
        print(f"[MQTT] Connection failed with error code: {rc}")


def on_message(client, userdata, msg):
    global last_alert_time, pending_alert_data
    now = time.time()

    try:
        payload = json.loads(msg.payload.decode('utf-8'))
        
        if msg.topic == ALERT_TOPIC:
            print(f"\n[ALERT] Received fire alert: Max={payload.get('max_temp')}°C, Pixels={payload.get('hot_pixels')}")
            pending_alert_data = payload
            
        elif msg.topic == HEATMAP_TOPIC:
            if (now - last_alert_time) >= ALERT_COOLDOWN_SECONDS:
                last_alert_time = now
                b64_image = payload.get("image_base64", "")
                image_bytes = base64.b64decode(b64_image) if b64_image else None
                
                alert_info = pending_alert_data or payload
                send_discord_notification(alert_info, image_bytes)
                pending_alert_data = None
            else:
                print(f"[DISCORD] Alert received during cooldown period. Suppressing duplicate.")
    except Exception as e:
        print(f"[ERROR] Error processing message: {e}")

# ==============================================================================
# MAIN APPLICATION LOOP
# ==============================================================================

def main():
    print("=" * 60)
    print("MQTT Discord Alert Listener Starting")
    print(f"Target Broker: {BROKER}:{PORT}")
    print(f"Monitoring:    {ALERT_TOPIC} | {HEATMAP_TOPIC}")
    print("=" * 60)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, f"discord_notifier_{int(time.time())}")
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(BROKER, PORT)

    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\n[SYSTEM] Terminating Discord Notifier...")
    finally:
        client.disconnect()
        print("[MQTT] Disconnected from broker cleanly.")

if __name__ == "__main__":
    main()
