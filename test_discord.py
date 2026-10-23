import os
import requests
import json
import io
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from datetime import datetime

# Optional loading of .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

def generate_sample_heatmap():
    """Generates a dummy 24x32 thermal matrix for testing."""
    # Create ambient background around 22-25°C
    data = np.random.normal(loc=23.5, scale=1.0, size=(24, 32))
    # Create a hot spot representing a flame/heat source
    data[10:16, 14:20] += np.random.normal(loc=35.0, scale=3.0, size=(6, 6))
    
    fig, ax = plt.subplots(figsize=(5, 3.8))
    im = ax.imshow(data, cmap='inferno', interpolation='bilinear')
    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label('Temperature (°C)', color='white')
    cbar.ax.yaxis.set_tick_params(color='white')
    plt.setp(plt.getp(cbar.ax.axes, 'yticklabels'), color='white')
    
    ax.set_title(f"🔥 Thermal Heatmap - {datetime.now().strftime('%H:%M:%S')}", color='white', pad=10)
    fig.patch.set_facecolor('#1e1f22')
    ax.set_facecolor('#1e1f22')
    ax.tick_params(colors='white')
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', facecolor=fig.get_facecolor(), dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()

def send_test_discord_alert():
    print("[TEST] Generating sample thermal heatmap...")
    img_bytes = generate_sample_heatmap()
    
    embed = {
        "title": "🚨 THERMAL ANOMALY / FIRE DETECTED",
        "description": "**Προσοχή:** Εντοπίστηκε απότομη αύξηση θερμοκρασίας στον θερμικό αισθητήρα!",
        "color": 15158332,  # Vibrant Red
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
    
    if not WEBHOOK_URL:
        print("❌ Σφάλμα: Δεν έχει οριστεί το DISCORD_WEBHOOK_URL (στο .env ή στις μεταβλητές συστήματος).")
        return

    print("[TEST] Sending test notification to Discord Webhook...")
    response = requests.post(WEBHOOK_URL, files=files)
    
    if response.status_code in (200, 204):
        print("✅ Επιτυχία! Το δοκιμαστικό μήνυμα στάλθηκε στο Discord κανάλι #fire-alerts.")
    else:
        print(f"❌ Σφάλμα κατά την αποστολή: {response.status_code} - {response.text}")

if __name__ == "__main__":
    send_test_discord_alert()
