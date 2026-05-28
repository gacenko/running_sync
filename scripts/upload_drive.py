import json
import os
import re
import base64
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
    """Return the ID of a subfolder, creating it if it doesn't exist."""
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
    """Upload file to Drive, overwriting if it already exists."""
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


def download_json(name, folder):
    """Download a JSON file from Drive, returns parsed dict or None if not found."""
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

# Check if entry for this date already exists — update it if so
cutoff = datetime.now() - timedelta(weeks=LOG_WINDOW_WEEKS)
updated = False
updated_log = []

for entry in existing_log:
    try:
        entry_date = datetime.strptime(entry["date"], "%d.%m.%Y")
    except Exception:
        continue

    # Skip entries outside the window
    if entry_date < cutoff:
        continue

    # Replace existing entry for same date
    if entry["date"] == new_entry["date"]:
        updated_log.append(new_entry)
        updated = True
    else:
        updated_log.append(entry)

if not updated:
    updated_log.append(new_entry)

# Sort by date descending — newest first
updated_log.sort(key=lambda e: datetime.strptime(e["date"], "%d.%m.%Y"), reverse=True)

print(f"training_log: {len(updated_log)} entries (window: {LOG_WINDOW_WEEKS} weeks)")

with open("training_log.json", "w", encoding="utf-8") as f:
    json.dump(updated_log, f, ensure_ascii=False, indent=2)

upsert_file(TRAINING_LOG_FILENAME, "training_log.json", folder_id)


# --- Upload named run file → detailed_runs/ subfolder ---

runs_folder_id = get_or_create_subfolder(RUNS_SUBFOLDER, folder_id)
print(f"Uploading: {filename} → {RUNS_SUBFOLDER}/")
upsert_file(filename, "running-data.json", runs_folder_id)


# --- Upload last_run.json → root folder ---

print(f"Uploading: {LAST_RUN_FILENAME}")
upsert_file(LAST_RUN_FILENAME, "running-data.json", folder_id)
