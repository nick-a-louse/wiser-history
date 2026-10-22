"""Fill a database with made-up readings and publish the site's data files from it,
for trying the website without a Wiser hub.

    python scripts/demo_data.py /tmp/wiser-demo          # 60 days of data
    cd /tmp/wiser-demo/published && python -m http.server 8080
    # copy web/ files next to published/, or see README "Development"
"""
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from collector.store import Store          # noqa: E402
from collector import publish              # noqa: E402

ROOMS = [  # id, name, base temp, sun gain, has RoomStat (humidity)
    (1, "Lounge", 19.5, 0.6, False), (2, "Kitchen", 19.0, 0.4, False),
    (3, "Conservatory", 15.5, 6.0, True), (4, "Hall", 17.5, 0.2, False),
    (5, "Master bedroom", 18.0, 0.8, False), (6, "Bedroom 2", 18.5, 0.5, True),
    (7, "Bedroom 3", 18.5, 0.5, False), (8, "Office", 19.0, 1.2, False),
    (9, "Bathroom", 20.0, 0.3, False), (10, "Landing", 17.0, 0.4, False),
]


def build(out_dir, days=60, end=None, seed=7):
    random.seed(seed)
    os.makedirs(out_dir, exist_ok=True)
    db = os.path.join(out_dir, "wiser.db")
    if os.path.exists(db):
        os.remove(db)
    store = Store(db)
    end = (end or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    end -= timedelta(minutes=end.minute % 5)
    t = end - timedelta(days=days)
    gap_from, gap_to = end - timedelta(days=9, hours=3), end - timedelta(days=9)   # a visible outage
    hourly, raining = [], False
    while t <= end:
        ts = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        if gap_from <= t < gap_to:
            t += timedelta(minutes=5); continue
        hour = (t.hour + 1) % 24                                   # roughly UK local
        day_frac = (t - (end - timedelta(days=days))).total_seconds() / (days * 86400)
        outside = 12 - 8 * day_frac + 4 * math.sin((hour - 9) / 24 * 2 * math.pi) + random.gauss(0, 0.3)
        heating = 6 <= hour < 9 or 16 <= hour < 22
        sun = max(0.0, math.sin((hour - 7) / 12 * math.pi)) if 7 <= hour <= 19 else 0.0
        rooms = []
        for rid, name, base, gain, stat in ROOMS:
            target = base + 1.5 if heating else 16.0
            temp = target - 0.8 + 0.25 * outside / 4 + gain * sun + random.gauss(0, 0.15)
            rooms.append({"room_id": rid, "name": name, "temp_c": round(temp, 1), "temp_source": "RoomStat" if stat else "TRV",
                          "setpoint_c": target, "scheduled_setpoint_c": target, "demand_pct": 60 if heating and temp < target else 0,
                          "output_state": "On" if heating and temp < target else "Off", "mode": "Auto", "effective_mode": "Auto",
                          "setpoint_origin": "FromSchedule", "window_state": "Closed",
                          "humidity_pct": int(55 + 10 * math.sin(hour / 24 * 6.28) + random.gauss(0, 2)) if stat else None,
                          "heating_rate": 36, "comfort_score": 0})
        store.record_rooms(ts, rooms)
        store.record_sample(ts, {"hub_unix_time": int(t.timestamp()), "boiler_relay": "On" if heating else "Off",
                                 "boiler_demand": 60 if heating else 0, "system_mode": "Normal", "away_mode": 0}, rooms, [])
        if t.minute == 0:                                          # spells of rain lasting a few hours
            raining = random.random() < (0.75 if raining else 0.06)
        rain = round(random.uniform(0.2, 2.5), 1) if raining else 0.0
        if t.minute % 15 == 0:
            store.record_weather(ts, {"observed_unix": int(t.timestamp()), "temp_c": round(outside, 1), "feels_like_c": round(outside - 2, 1),
                                      "humidity_pct": int(80 + random.gauss(0, 4)), "clouds_pct": 95 if raining else 60,
                                      "rain_1h_mm": rain, "snow_1h_mm": 0.0, "condition_id": 501 if raining else 803,
                                      "condition": "Rain" if raining else "Clouds"})
        if t.minute == 0:                                          # Open-Meteo's hourly table, as the archive fills it
            sw = round(450 * sun * (0.25 if raining else 0.7), 1)
            hourly.append({"ts_utc": ts, "temp_c": round(outside, 1), "humidity_pct": 90 if raining else 78,
                           "cloud_pct": 95 if raining else 60, "shortwave_wm2": sw, "direct_wm2": round(sw * 0.4, 1),
                           "diffuse_wm2": round(sw * 0.6, 1), "precipitation_mm": rain, "rain_mm": rain, "snowfall_cm": 0.0,
                           "wind_speed": round(abs(random.gauss(4, 2)), 1), "wind_gust": round(abs(random.gauss(8, 3)), 1),
                           "wind_deg": random.choice([200, 225, 250, 270])})
        store.record_run(ts, True, t.minute % 15 == 0, True, 900)
        t += timedelta(minutes=5)
    store.record_weather_hourly(hourly, "archive", end.strftime("%Y-%m-%dT%H:%M:%SZ"))
    store.conn.commit()
    written = publish.generate(store, os.path.join(out_dir, "published"), 300, 900)
    store.close()
    return written


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/wiser-demo"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    print("\n".join(build(out, days)))
