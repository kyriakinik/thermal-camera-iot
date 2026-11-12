"""
ECE490: Internet of Things - University of Thessaly
Discord Webhook Alert Test Utility
Student: Kyriaki Nikolopoulou

Description:
Standalone test script to verify Discord Webhook configuration. Generates a
synthetic 24x32 thermal frame with a simulated heat signature in RAM (BytesIO)
and dispatches an emergency notification card with image attachment.
"""

import os
import requests
import json
import io
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from datetime import datetime

# Optional environment variable support
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

def generate_sample_heatmap() -> bytes:
    """Generates a synthetic 24x32 thermal matrix for testing."""
    # Ambient baseline temperature (22-25°C)
    data = np.random.normal(loc=23.5, scale=1.0, size=(24, 32))
    # Simulated localized heat source (flame anomaly)
    data[10:16, 14:20] += np.random.normal(loc=35.0, scale=3.0, size=(6, 6))
    
    fig, ax = plt.subplots(figsize=(5, 3.8))
    im = ax.imshow(data, cmap='inferno', interpolation='bilinear')
    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label('Temperature (°C)', color='white')
    cbar.ax.yaxis.set_tick_params(color='white')
    plt.setp(plt.getp(cbar.ax.axes, 'yticklabels'), color='white')
    
    ax.set_title(f"Thermal Heatmap - {datetime.now().strftime('%H:%M:%S')}", color='white', pad=10)
    fig.patch.set_facecolor('#1e1f22')
    ax.set_facecolor('#1e1f22')
    ax.tick_params(colors='white')
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', facecolor=fig.get_facecolor(), dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()

def send_test_discord_alert():
    """Dispatches a mock emergency notification card to Discord."""
    if not WEBHOOK_URL:
        print("[ERROR] DISCORD_WEBHOOK_URL environment variable is not configured.")
        return

    print("[TEST] Generating synthetic thermal heatmap...")
    img_bytes = generate_sample_heatmap()
    
    embed = {
        "title": "🚨 THERMAL ANOMALY / FIRE DETECTED",
        "description": "**Προσοχή:** Εντοπίστηκε απότομη αύξηση θερμοκρασίας στον θερμικό αισθητήρα!",
        "color": 15158332,
        "fields": [
            {
                "name": "🌡️ Μέγιστη Θερμοκρασία",
                "value": "`58.4 °C`",
                "inline": True
            },
            {
                "name": "🔥 Hot Pixels (>35°C)",
                "value": "`36 / 768 (4.7%)`",
                "inline": True
            },
            {
                "name": "📊 Κατάσταση Κόμβου",
                "value": "🔴 `ALERT`",
                "inline": True
            },
            {
                "name": "🏷️ Edge Node",
                "value": "`edge08 (team1)`",
                "inline": True
            },
            {
                "name": "⏰ Ώρα Συμβάντος",
                "value": f"`{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`",
                "inline": True
            },
            {
                "name": "📈 Grafana Live Dashboard",
                "value": "[Άνοιγμα Dashboard](http://194.177.207.38:3000)",
                "inline": True
            }
        ],
        "image": {
            "url": "attachment://heatmap.png"
        },
        "footer": {
            "text": "ECE490 IoT • Adaptive Thermal Monitoring Node",
            "icon_url": "https://cdn-icons-png.flaticon.com/512/785/785116.png"
        }
    }
    
    payload_json = {
        "username": "🔥 Thermal Fire Bot",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/785/785116.png",
        "embeds": [embed]
    }
    
    files = {
        "payload_json": (None, json.dumps(payload_json), "application/json"),
        "files[0]": ("heatmap.png", img_bytes, "image/png")
    }
    
    print(f"[TEST] Sending Webhook request to Discord...")
    try:
        response = requests.post(WEBHOOK_URL, files=files, timeout=10)
        if response.status_code in (200, 204):
            print("[SUCCESS] Discord notification card delivered successfully!")
        else:
            print(f"[FAILED] Webhook returned status code: {response.status_code}")
            print(f"Response: {response.text}")
    except Exception as e:
        print(f"[ERROR] Webhook request exception: {e}")

if __name__ == "__main__":
    send_test_discord_alert()
