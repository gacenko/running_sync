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
# Garmin прив'язує сон до дня пробудження, тому передаємо дату активності.
# Якщо активність рано вранці (до ~10:00) — сон міг завершитись в той же день.
# Якщо активність після ~10:00 — також беремо ту ж дату (сон вже завершився).
# Окремий кейс: нічна активність (після 22:00) — сон ще не завершився,
# тому беремо наступний день. Межа: година старту < 10 або >= 22.

try:
    start_local = activity["summaryDTO"]["startTimeLocal"]
    # Garmin повертає "2026-05-26T06:00:00.0" або "2026-05-26T06:00:00"
    activity_date = datetime.fromisoformat(start_local.split(".")[0])
    hour = activity_date.hour
    if hour >= 22:
        # Нічна активність — сон ще попереду, беремо наступний день
        sleep_date = (activity_date + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        # Вранці або вдень — сон вже завершився в день активності
        sleep_date = activity_date.strftime("%Y-%m-%d")

    print()
    print(f"Loading sleep for {sleep_date}")
    sleep = client.get_sleep_data(sleep_date)
    with open("sleep.json", "w", encoding="utf-8") as f:
        json.dump(sleep, f, ensure_ascii=False, indent=2)
    print("Sleep saved")
except Exception as e:
    print(f"Sleep failed: {e}")

print()
print("Generated files:")
for file in os.listdir():
    print(file)

print()
print("Done")
