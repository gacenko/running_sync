from garminconnect import Garmin
import json
import os
import zipfile
from datetime import datetime, timedelta

print("Logging into Garmin...")

email = os.environ["GARMIN_EMAIL"]
password = os.environ["GARMIN_PASSWORD"]

client = Garmin(email, password)
client.login()

print("Login successful")

# ===== LOAD LATEST ACTIVITY =====

print()
print("Loading latest activity...")

activities = client.get_activities(0, 1)

if not activities:
    raise Exception("No activities found")

latest = activities[0]
activity_id = latest["activityId"]
workout_id = latest.get("workoutId")

print(f"Activity ID: {activity_id}")
print(f"Workout ID: {workout_id}")

# ===== RAW ACTIVITY =====

print()
print("Loading activity...")

activity = client.get_activity(activity_id)

with open("activity.json", "w", encoding="utf-8") as f:
    json.dump(activity, f, ensure_ascii=False, indent=2)

print("activity.json saved")

# ===== TYPED SPLITS =====

try:
    print()
    print("Loading typed splits...")
    typed_splits = client.connectapi(f"/activity-service/activity/{activity_id}/typedsplits")
    with open("typedsplits.json", "w", encoding="utf-8") as f:
        json.dump(typed_splits, f, ensure_ascii=False, indent=2)
    print("typedsplits.json saved")
except Exception as e:
    print(f"Typed splits failed: {e}")

# ===== SUBJECTIVE =====

try:
    print()
    print("Loading subjective...")
    raw_activity = client.connectapi(f"/activity-service/activity/{activity_id}")
    subjective = {
        "feeling_score":    raw_activity.get("directWorkoutFeel"),
        "perceived_effort": int(raw_activity.get("directWorkoutRpe", 0) / 10),
        "scale":            10,
    }
    with open("subjective.json", "w", encoding="utf-8") as f:
        json.dump(subjective, f, ensure_ascii=False, indent=2)
    print("subjective.json saved")
except Exception as e:
    print(f"Subjective failed: {e}")

# ===== FIT =====

print()
print("Downloading FIT...")

data = client.download_activity(activity_id, dl_fmt=client.ActivityDownloadFormat.ORIGINAL)

temp_file = "download.bin"
with open(temp_file, "wb") as f:
    f.write(data)

with open(temp_file, "rb") as f:
    header = f.read(4)

fit_filename = None

if header[:2] == b"PK":
    print("ZIP detected")
    with zipfile.ZipFile(temp_file, "r") as z:
        for file in z.namelist():
            print(f"Archive file: {file}")
            if file.lower().endswith(".fit"):
                fit_filename = file
                z.extract(file)
else:
    fit_filename = "activity.fit"
    os.rename(temp_file, fit_filename)

if fit_filename != "activity.fit":
    os.rename(fit_filename, "activity.fit")

print("FIT saved: activity.fit")

# ===== WORKOUT =====

try:
    if workout_id:
        print()
        print(f"Loading workout {workout_id}")
        workout = client.get_workout_by_id(workout_id)
        with open("workout.json", "w", encoding="utf-8") as f:
            json.dump(workout, f, ensure_ascii=False, indent=2)
        print("Workout saved")
    else:
        print()
        print("No workout attached")
except Exception as e:
    print(f"Workout failed: {e}")

# ===== SLEEP =====
# Garmin attaches sleep to the wake-up day.
# Sleep from 22:30 May 14 to 05:30 May 15 has date May 15 — same as the activity.

try:
    start_local = activity["summaryDTO"]["startTimeLocal"]
    # Garmin returns startTimeLocal as "2026-05-26T06:00:00.0" or "2026-05-26T06:00:00"
    activity_date = datetime.fromisoformat(start_local.split(".")[0])
    sleep_date = activity_date.strftime("%Y-%m-%d")

    print()
    print(f"Loading sleep for {sleep_date}")
    sleep = client.get_sleep_data(sleep_date)
    with open("sleep.json", "w", encoding="utf-8") as f:
        json.dump(sleep, f, ensure_ascii=False, indent=2)
    print("Sleep saved")
except Exception as e:
    print(f"Sleep failed: {e}")

# ===== WEATHER =====
# Open-Meteo historical API — no API key required.
# Uses GMT start time to get hourly weather at the activity location.

try:
    import urllib.request

    start_gmt = activity["summaryDTO"]["startTimeGMT"]
    lat = activity["summaryDTO"]["startLatitude"]
    lon = activity["summaryDTO"]["startLongitude"]

    dt_gmt = datetime.fromisoformat(start_gmt.split(".")[0])
    date_str = dt_gmt.strftime("%Y-%m-%d")
    hour = dt_gmt.hour

    url = (
        f"https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={date_str}&end_date={date_str}"
        f"&hourly=temperature_2m,wind_speed_10m,wind_direction_10m,precipitation,weather_code"
        f"&wind_speed_unit=kmh"
        f"&timezone=UTC"
    )

    print()
    print(f"Loading weather for {date_str} hour {hour} UTC...")

    with urllib.request.urlopen(url, timeout=10) as r:
        raw = json.loads(r.read())

    hourly = raw["hourly"]
    # WMO weather code → human-readable description
    WMO_CODES = {
        0: "clear",
        1: "mostly_clear", 2: "partly_cloudy", 3: "overcast",
        45: "fog", 48: "fog",
        51: "light_drizzle", 53: "drizzle", 55: "heavy_drizzle",
        61: "light_rain", 63: "rain", 65: "heavy_rain",
        71: "light_snow", 73: "snow", 75: "heavy_snow",
        77: "snow_grains",
        80: "light_showers", 81: "showers", 82: "heavy_showers",
        85: "snow_showers", 86: "heavy_snow_showers",
        95: "thunderstorm", 96: "thunderstorm_with_hail", 99: "thunderstorm_with_hail",
    }

    code = hourly["weather_code"][hour]
    weather = {
        "temperature":    hourly["temperature_2m"][hour],
        "wind_speed_kmh": hourly["wind_speed_10m"][hour],
        "wind_direction": hourly["wind_direction_10m"][hour],
        "precipitation":  hourly["precipitation"][hour],
        "conditions":     WMO_CODES.get(code, f"unknown_{code}"),
    }

    with open("weather.json", "w", encoding="utf-8") as f:
        json.dump(weather, f, ensure_ascii=False, indent=2)
    print(f"Weather saved: {weather['temperature']}°C, wind {weather['wind_speed_kmh']} km/h")

except Exception as e:
    print(f"Weather failed: {e}")

print()
print("Generated files:")
for file in os.listdir():
    print(file)

print()
print("Done")
