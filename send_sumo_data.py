import requests
import time
import random

# Dev Tunnels URL (Port 8000)
URL = "https://7q6p2pgp-8000.inc1.devtunnels.ms/api/v1/predict"

# Microsoft Tunnel splash-screen bypass header
HEADERS = {
    "Content-Type": "application/json",
    "X-Tunnel-Skip-Anti-Phishing-Page": "true"
}

print(f"🚀 AeroMesh SUMO Data Bridge chalu ho gaya hai...")
print(f"Target URL: {URL}\n")

# Initial values
prev_pm25 = 48.0
current_pm25 = 52.0

while True:
    try:
        # Realistic fluctuating readings (SUMO telemetry simulation)
        prev_pm25 = current_pm25
        delta = random.uniform(-4.0, 6.0)
        current_pm25 = max(25.0, min(220.0, current_pm25 + delta))
        
        current_queue = round(current_pm25 / 3.8 + random.uniform(-2, 3))
        current_speed = max(8.0, round(38.0 - (current_queue * 0.7), 1))
        vpm = random.randint(58, 76)

        payload = {
            "pm25_lag_1": round(current_pm25, 2),
            "pm25_lag_2": round(prev_pm25, 2),
            "zone_id": "MI_ROAD",
            "queue_length": float(current_queue),
            "avg_speed_kmh": float(current_speed),
            "vehicles_per_min": float(vpm)
        }

        res = requests.post(URL, json=payload, headers=HEADERS, timeout=6)

        if res.status_code == 200:
            resp_data = res.json()
            flush_status = "ACTIVE 🟢" if resp_data.get("trigger_signal_flush") else "STANDBY ⚪"
            pred_aqi = resp_data.get("predicted_pm25_next_hour", "N/A")
            print(f"✅ [200 OK] Sent: PM2.5={payload['pm25_lag_1']} µg/m³, Queue={payload['queue_length']} veh | Predicted PM2.5: {pred_aqi} | Flush: {flush_status}")
        else:
            print(f"⚠️ Server returned [{res.status_code}]: {res.text[:100]}")

    except requests.exceptions.RequestException as e:
        print(f"❌ Connection Error (Tunnel ya Server check karo): {e}")

    # Har 1.5 second me update bhejega
    time.sleep(1.5)