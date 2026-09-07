"""
backfill.py — відновлює пропущені активності з 24.08.2026.

Запуск з кореня репо:
  export GARMIN_EMAIL=...
  export GARMIN_PASSWORD=...
  export GOOGLE_CLIENT_ID=...
  export GOOGLE_CLIENT_SECRET=...
  export GOOGLE_REFRESH_TOKEN=...
  export GOOGLE_DRIVE_FOLDER_ID=...
  python backfill.py
"""

import json
import os
import re
import copy
import zipfile
import urllib.request
import subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from io import BytesIO

from garminconnect import Garmin
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from scripts.log_entry import build_log_entry

# ---------- Активності для відновлення ----------
# Garmin activity_id  →  workout_id береться автоматично з метаданих
# has_workout=False → пропускаємо завантаження workout.json
ACTIVITIES = [
    # 24.08 — 5k тест, без прикріпленого workout
    {"garmin_id": 24092806927, "has_workout": False},

    # 26.08 — Night Run = week 20 Recovery, workout є
    {"garmin_id": 24128503005, "has_workout": True},

    # 28.08 — week 20 easy, workout є
    {"garmin_id": 24144262450, "has_workout": True},

    # 30.08 — week 20 long (половинка), workout є
    {"garmin_id": 24167814722, "has_workout": True},

    # 01.09 — easy без workout (немає workoutId в метаданих)
    {"garmin_id": 24192249461, "has_workout": False},

    # 03.09 — Intervals 5x1000, workout є
    {"garmin_id": 24218870784, "has_workout": True},

    # 06.09 — week 21 long, workout є
    {"garmin_id": 24254150909, "has_workout": True},
]

KYIV_TZ     = ZoneInfo("Europe/Kyiv")
SCOPES      = ["https://www.googleapis.com/auth/drive"]
RUNS_SUBFOLDER     = "detailed_runs"
LAST_RUN_FILENAME  = "last_run.json"
TRAINING_LOG_FILENAME = "training_log.json"
LOG_WINDOW_WEEKS   = 8

WMO_CODES = {
    0: "clear", 1: "mostly_clear", 2: "partly_cloudy", 3: "overcast",
    45: "fog", 48: "fog",
    51: "light_drizzle", 53: "drizzle", 55: "heavy_drizzle",
    61: "light_rain", 63: "rain", 65: "heavy_rain",
    71: "light_snow", 73: "snow", 75: "heavy_snow", 77: "snow_grains",
    80: "light_showers", 81: "showers", 82: "heavy_showers",
    85: "snow_showers", 86: "heavy_snow_showers",
    95: "thunderstorm", 96: "thunderstorm_with_hail", 99: "thunderstorm_with_hail",
}


def safe_name(text):
    if not text:
        return "Run"
    text = text.replace("×", "x").replace("х", "x")
    return re.sub(r'[<>:"/\\|?*]', "", text).strip()


def utc_ms_to_kyiv(ms):
    if ms is None:
        return None
    import datetime as dt_module
    d = dt_module.datetime.fromtimestamp(ms / 1000, tz=dt_module.timezone.utc)
    return d.astimezone(KYIV_TZ).strftime("%H:%M")


def fetch_weather(lat, lon, start_gmt_str):
    try:
        dt_gmt = datetime.fromisoformat(start_gmt_str.split(".")[0])
        date_str = dt_gmt.strftime("%Y-%m-%d")
        hour = dt_gmt.hour
        url = (
            f"https://archive-api.open-meteo.com/v1/archive"
            f"?latitude={lat}&longitude={lon}"
            f"&start_date={date_str}&end_date={date_str}"
            f"&hourly=temperature_2m,wind_speed_10m,wind_direction_10m,precipitation,weather_code"
            f"&wind_speed_unit=kmh&timezone=UTC"
        )
        with urllib.request.urlopen(url, timeout=10) as r:
            raw = json.loads(r.read())
        h = raw["hourly"]
        code = h["weather_code"][hour]
        return {
            "temperature_c":  h["temperature_2m"][hour],
            "wind_speed_kmh": h["wind_speed_10m"][hour],
            "wind_direction": h["wind_direction_10m"][hour],
            "precipitation":  h["precipitation"][hour],
            "conditions":     WMO_CODES.get(code, f"unknown_{code}"),
        }
    except Exception as e:
        print(f"  Weather failed: {e}")
        return None


def fetch_sleep(client, start_local_str):
    """Такий самий підхід як у download_fit.py."""
    try:
        activity_dt = datetime.fromisoformat(start_local_str.split(".")[0])
        sleep_date  = activity_dt.strftime("%Y-%m-%d")
        print(f"  Loading sleep for {sleep_date}")
        raw = client.get_sleep_data(sleep_date)
        sd  = raw.get("dailySleepDTO", {})
        sleep_score = sd.get("sleepScores", {}).get("overall")
        return {
            "duration_hours":      round(sd.get("sleepTimeSeconds", 0) / 3600, 2),
            "deep_hours":          round(sd.get("deepSleepSeconds",  0) / 3600, 2),
            "light_hours":         round(sd.get("lightSleepSeconds", 0) / 3600, 2),
            "rem_hours":           round(sd.get("remSleepSeconds",   0) / 3600, 2),
            "awake_minutes":       round(sd.get("awakeSleepSeconds", 0) / 60),
            "score":               sleep_score,
            "sleep_start":         utc_ms_to_kyiv(sd.get("sleepStartTimestampGMT")),
            "sleep_end":           utc_ms_to_kyiv(sd.get("sleepEndTimestampGMT")),
            "hrv_overnight_avg":   raw.get("avgOvernightHrv"),
            "body_battery_change": raw.get("bodyBatteryChange"),
            "resting_hr":          raw.get("restingHeartRate"),
        }
    except Exception as e:
        print(f"  Sleep failed: {e}")
        return None


def download_fit_file(client, activity_id):
    """Точна копія логіки download_fit.py."""
    data = client.download_activity(
        activity_id, dl_fmt=client.ActivityDownloadFormat.ORIGINAL
    )
    tmp = "download.bin"
    with open(tmp, "wb") as f:
        f.write(data)
    with open(tmp, "rb") as f:
        header = f.read(4)
    if header[:2] == b"PK":
        print("  ZIP detected")
        with zipfile.ZipFile(tmp, "r") as z:
            for name in z.namelist():
                if name.lower().endswith(".fit"):
                    z.extract(name)
                    if name != "activity.fit":
                        os.rename(name, "activity.fit")
                    break
        os.remove(tmp)
    else:
        os.rename(tmp, "activity.fit")
    print("  FIT saved")


def strip_last_run(data):
    """Та сама логіка що в upload_drive.py → build_last_run."""
    d = copy.deepcopy(data)
    d.pop("time_series", None)
    s = d.get("activity", {}).get("summary", {})
    for k in ("avg_temperature", "min_temperature", "max_temperature"):
        s.pop(k, None)
    for iv in d.get("intervals", []):
        iv.get("summary", {}).pop("avg_temperature", None)
        for sp in iv.get("splits", []):
            sp.pop("avg_temperature", None)
    return d


# ---------- Google Drive helpers ----------

def build_drive(env):
    creds = Credentials(
        token=None,
        refresh_token=env["GOOGLE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=env["GOOGLE_CLIENT_ID"],
        client_secret=env["GOOGLE_CLIENT_SECRET"],
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_or_create_subfolder(svc, name, parent):
    ex = svc.files().list(
        q=f"name='{name}' and '{parent}' in parents "
          f"and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id)"
    ).execute().get("files", [])
    if ex:
        return ex[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent]}
    return svc.files().create(body=meta, fields="id").execute()["id"]


def upsert_json(svc, name, data, folder):
    tmp = f"_tmp__{name}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    ex = svc.files().list(
        q=f"name='{name}' and '{folder}' in parents and trashed=false",
        fields="files(id)"
    ).execute().get("files", [])
    media = MediaFileUpload(tmp, mimetype="application/json")
    if ex:
        svc.files().update(fileId=ex[0]["id"], media_body=media).execute()
        print(f"  Updated: {name}")
    else:
        svc.files().create(
            body={"name": name, "parents": [folder]},
            media_body=media, fields="id"
        ).execute()
        print(f"  Created: {name}")
    os.remove(tmp)


def download_json(svc, name, folder):
    ex = svc.files().list(
        q=f"name='{name}' and '{folder}' in parents and trashed=false",
        fields="files(id)"
    ).execute().get("files", [])
    if not ex:
        return None
    buf = BytesIO()
    dl = MediaIoBaseDownload(buf, svc.files().get_media(fileId=ex[0]["id"]))
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.seek(0)
    return json.loads(buf.read().decode("utf-8"))


# ---------- Main ----------

def main():
    env = os.environ

    print("Logging into Garmin...")
    client = Garmin(env["GARMIN_EMAIL"], env["GARMIN_PASSWORD"])
    client.login()
    print("Login OK")

    drive      = build_drive(env)
    folder_id  = env["GOOGLE_DRIVE_FOLDER_ID"]
    runs_folder = get_or_create_subfolder(drive, RUNS_SUBFOLDER, folder_id)

    existing_log   = download_json(drive, TRAINING_LOG_FILENAME, folder_id) or []
    existing_dates = {e["date"] for e in existing_log}
    print(f"Existing training_log: {len(existing_log)} entries, dates: {sorted(existing_dates)}")

    last_running_data = None

    for entry in ACTIVITIES:
        garmin_id = entry["garmin_id"]
        print(f"\n{'='*55}")
        print(f"Processing Garmin activity {garmin_id}...")

        try:
            # 1. activity.json (summaryDTO)
            activity     = client.get_activity(garmin_id)
            summary_dto  = activity.get("summaryDTO", {})
            start_local  = summary_dto.get("startTimeLocal", "")
            start_gmt    = summary_dto.get("startTimeGMT", "")
            lat  = summary_dto.get("startLatitude")
            lon  = summary_dto.get("startLongitude")
            workout_id   = activity.get("metadataDTO", {}).get("associatedWorkoutId") \
                           or summary_dto.get("workoutId")

            activity_dt  = datetime.fromisoformat(start_local.split(".")[0])
            date_string  = activity_dt.strftime("%d.%m.%Y")
            print(f"  Date: {date_string}  ({start_local})")

            if date_string in existing_dates:
                print(f"  Already in training_log — skipping")
                continue

            with open("activity.json", "w", encoding="utf-8") as f:
                json.dump(activity, f, ensure_ascii=False, indent=2)

            # 2. typedsplits
            try:
                ts = client.connectapi(
                    f"/activity-service/activity/{garmin_id}/typedsplits"
                )
                with open("typedsplits.json", "w", encoding="utf-8") as f:
                    json.dump(ts, f, ensure_ascii=False, indent=2)
                print("  typedsplits.json saved")
            except Exception as e:
                print(f"  typedsplits failed: {e}")

            # 3. subjective (directWorkoutFeel / directWorkoutRpe)
            try:
                raw_act = client.connectapi(
                    f"/activity-service/activity/{garmin_id}"
                )
                feel = raw_act.get("directWorkoutFeel")
                rpe  = raw_act.get("directWorkoutRpe")
                subj = {
                    "feeling_score":    feel,
                    "perceived_effort": int(rpe / 10) if rpe is not None else None,
                    "scale":            10,
                }
                with open("subjective.json", "w", encoding="utf-8") as f:
                    json.dump(subj, f, ensure_ascii=False, indent=2)
                print(f"  subjective: feel={feel} rpe={rpe}")
            except Exception as e:
                print(f"  subjective failed: {e}")
                if os.path.exists("subjective.json"):
                    os.remove("subjective.json")

            # 4. workout.json
            if entry["has_workout"] and workout_id:
                try:
                    # connectapi напряму — зберігає структуру RepeatGroupDTO
                    wkt = client.connectapi(
                        f"/workout-service/workout/{workout_id}"
                    )
                    with open("workout.json", "w", encoding="utf-8") as f:
                        json.dump(wkt, f, ensure_ascii=False, indent=2)
                    print(f"  workout.json saved (id={workout_id})")
                except Exception as e:
                    print(f"  workout failed: {e}")
                    if os.path.exists("workout.json"):
                        os.remove("workout.json")
            else:
                # Без workout — прибираємо щоб parse_fit не підхопив старий
                if os.path.exists("workout.json"):
                    os.remove("workout.json")
                print("  No workout")

            # 5. FIT
            download_fit_file(client, garmin_id)

            # 6. Sleep — та сама логіка що в download_fit.py
            sleep_data = fetch_sleep(client, start_local)
            if sleep_data:
                # parse_fit.py читає sleep.json через dailySleepDTO
                # тому пишемо у той самий формат що повертає get_sleep_data
                raw_sleep = client.get_sleep_data(
                    activity_dt.strftime("%Y-%m-%d")
                )
                with open("sleep.json", "w", encoding="utf-8") as f:
                    json.dump(raw_sleep, f, ensure_ascii=False, indent=2)

            # 7. Weather
            if lat and lon and start_gmt:
                weather = fetch_weather(lat, lon, start_gmt)
                if weather:
                    with open("weather.json", "w", encoding="utf-8") as f:
                        json.dump(weather, f, ensure_ascii=False, indent=2)
                    print(f"  weather: {weather['temperature_c']}°C {weather['conditions']}")

            # 8. parse_fit.py
            print("  Running parse_fit.py...")
            result = subprocess.run(
                ["python", "scripts/parse_fit.py"],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                print(f"  parse_fit FAILED:\n{result.stderr}")
                continue
            print(f"  {result.stdout.strip()}")

            # 9. Читаємо результат
            with open("running-data.json", "r", encoding="utf-8") as f:
                running_data = json.load(f)
            last_running_data = running_data

            # 10. Назва файлу
            wkt_name  = running_data.get("workout", {}) or {}
            wkt_name  = wkt_name.get("name") or activity.get("activityName", "Run")
            filename  = f"{safe_name(wkt_name)} - {date_string}.json"

            # 11. detailed_runs — повний файл
            upsert_json(drive, filename, running_data, runs_folder)
            print(f"  → detailed_runs/{filename}")

            # 12. training_log
            new_entry = build_log_entry(running_data, date_string)
            existing_log.append(new_entry)
            existing_dates.add(date_string)
            print(f"  Added to training_log: {date_string}")

        except Exception as e:
            import traceback
            print(f"  ERROR: {e}")
            traceback.print_exc()

    # ---------- Фінальне оновлення Drive ----------

    # training_log.json — сортуємо, обрізаємо за вікном
    cutoff = datetime.now() - timedelta(weeks=LOG_WINDOW_WEEKS)
    updated_log = [
        e for e in existing_log
        if datetime.strptime(e["date"], "%d.%m.%Y") >= cutoff
    ]
    updated_log.sort(
        key=lambda e: datetime.strptime(e["date"], "%d.%m.%Y"),
        reverse=True
    )
    upsert_json(drive, TRAINING_LOG_FILENAME, updated_log, folder_id)
    print(f"\ntraining_log updated: {len(updated_log)} entries")

    # last_run.json — остання оброблена активність, стрипована
    if last_running_data:
        stripped = strip_last_run(last_running_data)
        upsert_json(drive, LAST_RUN_FILENAME, stripped, folder_id)
        print("last_run.json updated")

    print("\nDone!")


if __name__ == "__main__":
    main()
