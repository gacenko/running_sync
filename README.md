# running_sync

Automated pipeline that captures every run from Garmin, enriches it with sleep and subjective data, and delivers a structured JSON to Google Drive — fully hands-free.

The output file serves as context for an AI running coach that analyzes training history, sleep patterns, and subjective feel to provide personalized recommendations.

---

## How it works

```
Strava webhook
     │
     ▼
Cloudflare Worker          ← validates event, filters non-runs, triggers CI
     │
     ▼
GitHub Actions
     ├── download_fit.py   ← logs into Garmin, fetches FIT file + metadata
     ├── parse_fit.py      ← parses FIT, builds structured JSON
     └── upload_drive.py   ← upserts file to Google Drive
```

A new activity on Garmin triggers a Strava webhook. The Cloudflare Worker receives it, checks that it's a run, and dispatches a GitHub Actions workflow. The workflow downloads the raw FIT file from Garmin Connect, parses it into a clean JSON, and uploads the result to Google Drive — overwriting the file if it already exists.

---

## Output format

Each run produces a single JSON file named `{workout name} - {date}.json`:

```json
{
  "activity": {
    "summary": {
      "distance_km": 9.01,
      "avg_pace": "5:41",
      "best_pace": "4:41",
      "avg_hr": 115,
      "aerobic_effect": 2.8,
      "training_effect_label": "AEROBIC_BASE",
      "elevation_gain_m": 84.3,
      "respiration_avg": 37.6,
      "fluid_loss_ml": 537
    }
  },
  "workout": {
    "steps": [
      {
        "type": "interval",
        "distance_m": 9000,
        "target": { "type": "pace.zone", "min_pace": "5:40", "max_pace": "5:55" }
      }
    ]
  },
  "sleep": {
    "duration_hours": 4.35,
    "score": { "value": 57, "qualifierKey": "POOR" },
    "sleep_start": "02:30",
    "sleep_end": "07:02"
  },
  "subjective": {
    "feeling": "strong",
    "perceived_effort": 2,
    "scale": 10
  },
  "intervals": [
    {
      "type": "active",
      "summary": { "avg_pace": "5:41", "avg_hr": 116 },
      "splits": [
        { "lap": 1, "avg_pace": "6:04", "avg_hr": 105 }
      ]
    }
  ],
  "time_series": {
    "sample_interval_sec": 10,
    "data": [
      { "hr": 105, "pace_sec_per_km": 364, "cadence": 162, "respiration": 31.2 }
    ]
  }
}
```

---

## Stack

| Layer | Technology |
|---|---|
| Webhook receiver | Cloudflare Workers |
| CI/CD | GitHub Actions |
| Data source | Garmin Connect (via `garminconnect`) |
| FIT parsing | `fitparse` |
| Storage | Google Drive API v3 |

---

## Setup

### 1. Cloudflare Worker

```bash
npm install -g wrangler
wrangler deploy
```

Set secrets in the Cloudflare dashboard:

```
GH_TOKEN      # GitHub personal access token with workflow scope
```

`GITHUB_OWNER` and `GITHUB_REPO` are set as plain vars in `wrangler.jsonc`.

### 2. GitHub Actions secrets

| Secret | Description |
|---|---|
| `GARMIN_EMAIL` | Garmin Connect account email |
| `GARMIN_PASSWORD` | Garmin Connect account password |
| `GOOGLE_CLIENT_ID` | OAuth 2.0 client ID |
| `GOOGLE_CLIENT_SECRET` | OAuth 2.0 client secret |
| `GOOGLE_REFRESH_TOKEN` | Offline refresh token |
| `GOOGLE_DRIVE_FOLDER_ID` | Target folder ID in Google Drive |

### 3. Strava webhook

Register the Cloudflare Worker URL as a Strava webhook subscription via the [Strava API](https://developers.strava.com/docs/webhooks/). The Worker handles the `GET` verification challenge automatically.

---

## Data notes

**Sleep** is fetched for the same calendar date as the activity (Garmin attaches sleep to the wake-up day). For activities starting after 22:00 the next day's sleep record is used instead.

**Respiration** is sourced from an undocumented FIT field (`unknown_108`, scaled ×100). Values are validated against Garmin Connect summary stats.

**Time series** is downsampled from per-second FIT records to 10-second intervals. Index position in the array encodes elapsed time — no timestamps stored per sample.

**Tail laps** under 100m at the end of an active interval are excluded from splits (distance remainder after completing the planned workout).
