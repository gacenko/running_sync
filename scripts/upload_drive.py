import json
import os
import re
from datetime import datetime

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
LAST_RUN_FILENAME = "last_run.json"


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

print(f"Uploading: {filename}")

folder_id = os.environ["GOOGLE_DRIVE_FOLDER_ID"]


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


# --- Upload named run file ---
upsert_file(filename, "running-data.json", folder_id)

# --- Upload last_run.json (fixed name, always overwritten) ---
upsert_file(LAST_RUN_FILENAME, "running-data.json", folder_id)
