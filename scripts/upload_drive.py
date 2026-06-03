import json
import os
import re
import copy
from datetime import datetime, timedelta

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from io import BytesIO

from log_entry import build_log_entry

SCOPES = ["https://www.googleapis.com/auth/drive"]
LAST_RUN_FILENAME = "last_run.json"
TRAINING_LOG_FILENAME = "training_log.json"
RUNS_SUBFOLDER = "detailed_runs"
LOG_WINDOW_WEEKS = 8


def safe_name(text):
    if not text:
        return "Run"
    text = text.replace("×", "x").replace("х", "x")
    return re.sub(r'[<>:"/\\|?*]', "", text).strip()


def build_last_run(data):
    """
    Stripped version of running-data for last_run.json:
    - no time_series (317 records — зайве для аналізу)
    - no temperature fields (дублюють weather block)
    """
    d = copy.deepcopy(data)

    # Прибираємо time_series
    d.pop("time_series", None)

    # Прибираємо температуру з activity.summary
    summary = d.get("activity", {}).get("summary", {})
    for key in ("avg_temperature", "min_temperature", "max_temperature"):
        summary.pop(key, None)

    # Прибираємо avg_temperature з кожного interval.summary і splits
    for interval in d.get("intervals", []):
        interval.get("summary", {}).pop("avg_temperature", None)
        for split in interval.get("splits", []):
            split.pop("avg_temperature", None)

    return d


# --- Auth ---

creds = Credentials(
    token=None,
    refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
    token_uri="https://oauth2.googleapis.com/token",
    client_id=os.environ["GOOGLE_CLIENT_ID"],
    client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
    scopes=SCOPES,
)

service = build("drive", "v3", credentials=creds, cache_discovery=False)


# --- Load files ---

with open("running-data.json", "r", encoding="utf-8") as f:
    running_data = json.load(f)

with open("activity.json", "r", encoding="utf-8") as f:
    activity = json.load(f)


# --- Build filename ---

activity_date = activity.get("summaryDTO", {}).get("startTimeLocal")
date_string = "unknown-date"
if activity_date:
    date_string = datetime.fromisoformat(activity_date.split(".")[0]).strftime("%d.%m.%Y")

workout = running_data.get("workout")
workout_name = workout.get("name") if workout else activity.get("activityName", "Run")
filename = f"{safe_name(workout_name)} - {date_string}.json"

folder_id = os.environ["GOOGLE_DRIVE_FOLDER_ID"]


# --- Helpers ---

def get_or_create_subfolder(name, parent_id):
    existing = service.files().list(
        q=f"name='{name}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id, name)",
    ).execute().get("files", [])

    if existing:
        return existing[0]["id"]

    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    result = service.files().create(body=metadata, fields="id").execute()
    print(f"Created subfolder: {name} (id: {result['id']})")
    return result["id"]


def upsert_file(name, local_path, folder):
    existing = service.files().list(
        q=f"name='{name}' and '{folder}' in parents and trashed=false",
        fields="files(id, name)",
    ).execute().get("files", [])

    media = MediaFileUpload(local_path, mimetype="application/json")

    if existing:
        file_id = existing[0]["id"]
        service.files().update(fileId=file_id, media_body=media).execute()
        print(f"Updated: {name} (id: {file_id})")
    else:
        metadata = {"name": name, "parents": [folder]}
        result = service.files().create(body=metadata, media_body=media, fields="id").execute()
        print(f"Created: {name} (id: {result['id']})")


def upsert_json(name, data, folder):
    """Serialize dict to temp file and upsert to Drive."""
    tmp_path = f"_tmp_{name}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    upsert_file(name, tmp_path, folder)
    os.remove(tmp_path)


def download_json(name, folder):
    existing = service.files().list(
        q=f"name='{name}' and '{folder}' in parents and trashed=false",
        fields="files(id, name)",
    ).execute().get("files", [])

    if not existing:
        return None

    file_id = existing[0]["id"]
    request = service.files().get_media(fileId=file_id)
    buffer = BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    buffer.seek(0)
    return json.loads(buffer.read().decode("utf-8"))


# --- Update training_log.json ---

print("Updating training_log.json...")

existing_log = download_json(TRAINING_LOG_FILENAME, folder_id) or []

new_entry = build_log_entry(running_data, date_string)

cutoff = datetime.now() - timedelta(weeks=LOG_WINDOW_WEEKS)
updated = False
updated_log = []

for entry in existing_log:
    try:
        entry_date = datetime.strptime(entry["date"], "%d.%m.%Y")
    except Exception:
        continue

    if entry_date < cutoff:
        continue

    if entry["date"] == new_entry["date"]:
        updated_log.append(new_entry)
        updated = True
    else:
        updated_log.append(entry)

if not updated:
    updated_log.append(new_entry)

updated_log.sort(key=lambda e: datetime.strptime(e["date"], "%d.%m.%Y"), reverse=True)

print(f"training_log: {len(updated_log)} entries (window: {LOG_WINDOW_WEEKS} weeks)")

with open("training_log.json", "w", encoding="utf-8") as f:
    json.dump(updated_log, f, ensure_ascii=False, indent=2)

upsert_file(TRAINING_LOG_FILENAME, "training_log.json", folder_id)


# --- Upload full running-data → detailed_runs/ subfolder ---

runs_folder_id = get_or_create_subfolder(RUNS_SUBFOLDER, folder_id)
print(f"Uploading: {filename} → {RUNS_SUBFOLDER}/")
upsert_file(filename, "running-data.json", runs_folder_id)


# --- Upload stripped last_run.json → root folder ---
# No time_series, no temperature duplicates (weather block is the source of truth)

print(f"Uploading: {LAST_RUN_FILENAME}")
last_run_data = build_last_run(running_data)
upsert_json(LAST_RUN_FILENAME, last_run_data, folder_id)
