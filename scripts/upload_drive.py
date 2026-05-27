import json
import os
import re
from datetime import datetime

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]


def safe_name(text):
    if not text:
        return "Run"
    text = text.replace("×", "x")
    text = text.replace("х", "x")  # кирилична х теж
    return re.sub(r'[<>:"/\\|?*]', "", text).strip()


# ---------- AUTH ----------

creds = Credentials(
    token=None,
    refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
    token_uri="https://oauth2.googleapis.com/token",
    client_id=os.environ["GOOGLE_CLIENT_ID"],
    client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
    scopes=SCOPES,
)

service = build("drive", "v3", credentials=creds, cache_discovery=False)


# ---------- LOAD FILES ----------

with open("running-data.json", "r", encoding="utf-8") as f:
    running_data = json.load(f)

with open("activity.json", "r", encoding="utf-8") as f:
    activity = json.load(f)


# ---------- FILE NAME ----------

activity_name = activity.get("activityName", "Run")
activity_date = activity.get("summaryDTO", {}).get("startTimeLocal")

date_string = "unknown-date"
if activity_date:
    date_string = datetime.fromisoformat(activity_date.split(".")[0]).strftime("%d.%m.%Y")

workout = running_data.get("workout")
workout_name = workout.get("name") if workout else activity_name
filename = f"{safe_name(workout_name)} - {date_string}.json"

print(f"Uploading: {filename}")


# ---------- FIND EXISTING FILE ----------

folder_id = os.environ["GOOGLE_DRIVE_FOLDER_ID"]

existing = service.files().list(
    q=f"name='{filename}' and '{folder_id}' in parents and trashed=false",
    fields="files(id, name)",
).execute()

existing_files = existing.get("files", [])


# ---------- UPLOAD OR UPDATE ----------

media = MediaFileUpload("running-data.json", mimetype="application/json")

if existing_files:
    file_id = existing_files[0]["id"]
    print(f"File exists (id: {file_id}), updating...")
    service.files().update(fileId=file_id, media_body=media).execute()
    print("Updated successfully")
else:
    metadata = {"name": filename, "parents": [folder_id]}
    result = service.files().create(body=metadata, media_body=media, fields="id").execute()
    print(f"Uploaded successfully")
    print(f"File ID: {result['id']}")
