import time
import json
import base64
import os
import requests
import paho.mqtt.client as mqtt
from datetime import datetime

# Optional loading of .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ==========================================
# CONFIGURATION
# ==========================================
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

BROKER = os.getenv("MQTT_BROKER", "194.177.207.38")
PORT   = int(os.getenv("MQTT_PORT", "1883"))
TEAM   = os.getenv("TEAM", "team1")  # Or your Raspberry Pi hostname (e.g. edge08)

MQTT_USERNAME = os.getenv("MQTT_USERNAME", "team1")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")

# MQTT Topics
ALERT_TOPIC   = f"iot/{TEAM}/alerts/thermal"
HEATMAP_TOPIC = f"iot/{TEAM}/heatmaps/thermal"

# Cooldown to avoid spamming Discord messages continuously during an alert
ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "10"))
last_alert_time = 0
pending_alert_data = None

def send_discord_notification(alert_info: dict, image_bytes: bytes = None):
    """Sends a rich styled alert embed to Discord with optional heatmap image."""
    max_t = alert_info.get("max_temp", "N/A")
    hot_pix = alert_info.get("hot_pixels", "N/A")
    timestamp = alert_info.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    team_name = alert_info.get("team", TEAM)

    embed = {
        "title": "🚨 THERMAL ANOMALY / FIRE DETECTED",
        "description": f"**Προσοχή:** Εντοπίστηκε κρίσιμη αύξηση θερμοκρασίας στον κόμβο `{team_name}`!",
        "color": 15158332,  # Vibrant Red
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
        print("[DISCORD] ⚠️ DISCORD_WEBHOOK_URL is not set. Skipping notification.")
        return False

    try:
        if image_bytes:
            files = {
                "payload_json": (None, json.dumps(payload_json), "application/json"),
                "files[0]": ("heatmap.png", image_bytes, "image/png")
            }
            res = requests.post(DISCORD_WEBHOOK_URL, files=files, timeout=10)
        else:
            res = requests.post(DISCORD_WEBHOOK_URL, json=payload_json, timeout=10)

        if res.status_code in (200, 204):
            print(f"[DISCORD] ✅ Notification successfully delivered to Discord!")
            return True
        else:
            print(f"[DISCORD] ❌ Error sending: {res.status_code} - {res.text}")
            return False
    except Exception as e:
        print(f"[DISCORD] ❌ Exception during webhook request: {e}")
        return False

# ==========================================
# MQTT CALLBACKS
# ==========================================
def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"[MQTT] Connected to broker {BROKER}:{PORT}")
        print(f"[MQTT] Subscribed to:\n  - {ALERT_TOPIC}\n  - {HEATMAP_TOPIC}")
        client.subscribe([(ALERT_TOPIC, 0), (HEATMAP_TOPIC, 0)])
    else:
        print(f"[MQTT] Connection failed with code {rc}")

def on_message(client, userdata, msg):
    global last_alert_time, pending_alert_data
    now = time.time()

    try:
        payload = json.loads(msg.payload.decode('utf-8'))

        if msg.topic == ALERT_TOPIC:
            if now - last_alert_time >= ALERT_COOLDOWN_SECONDS:
                pending_alert_data = payload
                print(f"\n🔥 [ALERT RECEIVED] Max Temp: {payload.get('max_temp')}°C")

        elif msg.topic == HEATMAP_TOPIC:
            if pending_alert_data and (now - last_alert_time >= ALERT_COOLDOWN_SECONDS):
                last_alert_time = now
                b64_image = payload.get("image_base64", "")
                img_bytes = base64.b64decode(b64_image) if b64_image else None
                
                print("[DISCORD] Forwarding Alert + Thermal Heatmap to Discord...")
                send_discord_notification(pending_alert_data, img_bytes)
                pending_alert_data = None

    except Exception as e:
        print(f"[ERROR] Failed processing MQTT message: {e}")

def main():
    print("=" * 60)
    print("🤖 Discord Fire Alert Notifier Bridge Starting...")
    print(f"Target Team: {TEAM}")
    print("=" * 60)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, f"discord_bridge_{TEAM}")
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(BROKER, PORT)
    print("[RUNNING] Listening for fire alerts from MQTT broker. Press Ctrl+C to exit.\n")
    
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\n[STOP] Shutting down Discord Notifier...")
    finally:
        client.disconnect()

if __name__ == "__main__":
    main()
