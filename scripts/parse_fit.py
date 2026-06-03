import json
import datetime
import os
import fitparse
from zoneinfo import ZoneInfo

fit = fitparse.FitFile("activity.fit")

# --- Records (per-second data for best_pace and time series) ---

records = []

for r in fit.get_messages("record"):
    d = {field.name: field.value for field in r}
    ts = d.get("timestamp")
    speed = d.get("enhanced_speed") or d.get("speed")
    if ts:
        records.append({
            "timestamp":   ts,
            "speed":       speed,
            "hr":          d.get("heart_rate"),
            "cadence":     d.get("cadence"),
            "respiration": d.get("unknown_108"),  # respiration rate * 100, undocumented FIT field
        })


# --- Helpers ---

def speed_to_pace(speed):
    if not speed or speed <= 0:
        return None
    sec = 1000 / speed
    mins = int(sec // 60)
    secs = round(sec % 60)
    if secs == 60:
        mins += 1
        secs = 0
    return f"{mins}:{secs:02d}"


def int_or_none(value):
    if value is None:
        return None
    return int(round(value))


# best_pace is suppressed for recovery and cooldown laps —
# neighbouring interval speeds distort the rolling window result
NO_BEST_PACE_TYPES = {"INTERVAL_RECOVERY", "INTERVAL_COOLDOWN"}


# --- Load files ---

with open("activity.json", "r", encoding="utf-8") as f:
    activity = json.load(f)

summary = activity.get("summaryDTO", {})

workout = None
if os.path.exists("workout.json"):
    with open("workout.json", "r", encoding="utf-8") as f:
        workout = json.load(f)

KYIV_TZ = ZoneInfo("Europe/Kyiv")


def utc_ms_to_kyiv(ms):
    """Convert Unix timestamp in milliseconds to HH:MM string in Kyiv timezone."""
    if ms is None:
        return None
    dt = datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)
    return dt.astimezone(KYIV_TZ).strftime("%H:%M")


weather_data = None
if os.path.exists("weather.json"):
    with open("weather.json", "r", encoding="utf-8") as f:
        weather_data = json.load(f)

sleep_data = None
if os.path.exists("sleep.json"):
    with open("sleep.json", "r", encoding="utf-8") as f:
        raw_sleep = json.load(f)
    try:
        sd = raw_sleep.get("dailySleepDTO", {})
        sleep_data = {
            "duration_hours":      round(sd.get("sleepTimeSeconds", 0) / 3600, 2),
            "deep_hours":          round(sd.get("deepSleepSeconds", 0) / 3600, 2),
            "light_hours":         round(sd.get("lightSleepSeconds", 0) / 3600, 2),
            "rem_hours":           round(sd.get("remSleepSeconds", 0) / 3600, 2),
            "awake_minutes":       round(sd.get("awakeSleepSeconds", 0) / 60),
            "score":               sd.get("sleepScores", {}).get("overall"),
            "sleep_start":         utc_ms_to_kyiv(sd.get("sleepStartTimestampGMT")),
            "sleep_end":           utc_ms_to_kyiv(sd.get("sleepEndTimestampGMT")),
            "hrv_overnight_avg":   raw_sleep.get("avgOvernightHrv"),
            "body_battery_change": raw_sleep.get("bodyBatteryChange"),
            "resting_hr":          raw_sleep.get("restingHeartRate"),
        }
    except Exception as e:
        print(f"Sleep parse error: {e}")


# --- Workout ---

parsed_workout = None
workout_steps = []

if workout:
    def parse_step_fit(step, index):
        target_type = step.get("targetType")
        target_key = (target_type or {}).get("workoutTargetTypeKey")
        v1 = step.get("targetValueOne")
        v2 = step.get("targetValueTwo")

        if target_key == "pace.zone" and v1 and v2:
            # Garmin may return v1/v2 in either order — always assign by speed value
            faster, slower = (v1, v2) if v1 > v2 else (v2, v1)
            target = {
                "type":     "pace.zone",
                "min_pace": speed_to_pace(faster),
                "max_pace": speed_to_pace(slower),
            }
        elif target_key and target_key != "no.target":
            target = {"type": target_key}
        else:
            target = None

        return {
            "index":       index,
            "type":        step.get("stepType"),
            "description": step.get("description"),
            "distance_m":  step.get("endConditionValue"),
            "target":      target,
        }

    def collect_steps_fit(raw_steps):
        for step in raw_steps:
            step_type = step.get("type", "")
            if "repeat" in step_type.lower() or step.get("numberOfIterations"):
                collect_steps_fit(step.get("workoutSteps", []))
            else:
                step_data = parse_step_fit(step, len(workout_steps))
                workout_steps.append(step_data)
                steps.append(step_data)

    steps = []
    for segment in workout.get("workoutSegments", []):
        collect_steps_fit(segment.get("workoutSteps", []))

    parsed_workout = {
        "id":    workout.get("workoutId"),
        "name":  workout.get("workoutName"),
        "steps": steps,
    }

# Number of planned active intervals — used to tag extra laps as post_workout
planned_active_count = sum(
    1 for s in workout_steps
    if isinstance(s.get("type"), dict)
    and s["type"].get("stepTypeKey") == "interval"
) if workout_steps else None


# --- Subjective ---

FEELING_MAP = {0: "very_weak", 25: "weak", 50: "normal", 75: "strong", 100: "very_strong"}

rpe = summary.get("directWorkoutRpe")
subjective = {
    "feeling_score":    summary.get("directWorkoutFeel"),
    "feeling":          FEELING_MAP.get(summary.get("directWorkoutFeel")),
    "perceived_effort": int(rpe / 10) if rpe is not None else None,
    "scale":            10,
}


# --- Laps ---

laps = []
lap_number = 1
cumulative_time = 0

for lap in fit.get_messages("lap"):
    data = {field.name: field.value for field in lap}

    duration = round(data.get("total_elapsed_time", 0), 1)
    moving_time = round(data.get("total_timer_time", duration), 1)
    cumulative_time += duration
    distance = data.get("total_distance", 0)

    # FIT lap has no enhanced_avg_speed — derive from distance / timer_time
    avg_speed = (distance / moving_time) if (distance and moving_time and moving_time > 0) else None

    # best_pace: 5-second rolling window over per-second records within the lap
    lap_start = data.get("start_time")
    lap_end = data.get("timestamp")
    max_speed = None

    if lap_start and lap_end:
        lap_rec_speeds = [
            r["speed"] for r in records
            if lap_start <= r["timestamp"] <= lap_end
            and r["speed"] is not None and r["speed"] > 0
        ]
        if lap_rec_speeds:
            n = len(lap_rec_speeds)
            if n >= 5:
                rolling = [sum(lap_rec_speeds[i:i+5]) / 5 for i in range(n - 4)]
                max_speed = max(rolling)
            else:
                max_speed = max(lap_rec_speeds)

    strides = data.get("total_strides")
    stride_length = None
    if strides and strides > 0 and distance >= 200:
        stride_length = round((distance / (strides * 2)) * 100, 1)

    cadence = data.get("avg_running_cadence")
    max_cadence = data.get("max_running_cadence")

    laps.append({
        "lap":                     lap_number,
        "duration_sec":            duration,
        "cumulative_duration_sec": round(cumulative_time, 1),
        "distance_m":              round(distance, 2),
        "avg_pace":                speed_to_pace(avg_speed),
        "avg_hr":                  int_or_none(data.get("avg_heart_rate")),
        "max_hr":                  int_or_none(data.get("max_heart_rate")),
        "elevation_gain":          int_or_none(data.get("total_ascent")),
        "elevation_loss":          int_or_none(data.get("total_descent")),
        "avg_running_cadence":     int_or_none(cadence * 2) if cadence else None,
        "avg_stride_length_cm":    stride_length,
        "calories":                int_or_none(data.get("total_calories")),
        "avg_temperature":         data.get("avg_temperature"),
        "best_pace":               speed_to_pace(max_speed),
        "max_running_cadence":     int_or_none(max_cadence * 2) if max_cadence else None,
        "moving_time_sec":         moving_time,
        "nonstop_pace":            speed_to_pace(avg_speed),
    })

    lap_number += 1

lap_map = {x["lap"]: x for x in laps}


# --- Intervals ---

with open("typedsplits.json", "r", encoding="utf-8") as f:
    typed_splits = json.load(f)

INTERVAL_TYPE_MAP = {
    "INTERVAL_WARMUP":   "warmup",
    "INTERVAL_ACTIVE":   "active",
    "INTERVAL_RECOVERY": "recovery",
    "INTERVAL_COOLDOWN": "cooldown",
}

intervals = []
logical_interval = 1
active_counter = 0

for split in typed_splits["splits"]:
    split_type = split.get("type")

    if not split_type.startswith("INTERVAL_"):
        continue

    lap_indexes = split.get("lapIndexes", [])
    interval_type = INTERVAL_TYPE_MAP.get(split_type)

    if split_type == "INTERVAL_ACTIVE":
        active_counter += 1
        # Tag extra active intervals beyond the planned count as post_workout
        if planned_active_count is not None and active_counter > planned_active_count:
            interval_type = "post_workout"

    interval = {
        "interval": 0 if split_type == "INTERVAL_WARMUP" else logical_interval,
        "type":     interval_type,
        "summary": {
            "laps":                  lap_indexes,
            "duration_sec":          round(split.get("duration", 0), 1),
            "distance_m":            split.get("distance"),
            "avg_pace":              speed_to_pace(split.get("averageSpeed")),
            "avg_hr":                int_or_none(split.get("averageHR")),
            "max_hr":                int_or_none(split.get("maxHR")),
            "elevation_gain":        int_or_none(split.get("elevationGain")),
            "elevation_loss":        int_or_none(split.get("elevationLoss")),
            "avg_running_cadence":   int_or_none(split.get("averageRunCadence")),
            "avg_stride_length_cm":  split.get("strideLength"),
            "calories":              int_or_none(split.get("calories")),
            "avg_temperature":       split.get("averageTemperature"),
            "best_pace":             speed_to_pace(split.get("maxSpeed")),
            "max_running_cadence":   int_or_none(split.get("maxRunCadence")),
            "moving_time_sec":       round(split.get("movingDuration", 0), 1),
            "nonstop_pace":          speed_to_pace(split.get("averageMovingSpeed")),
        },
        "splits": [
            {
                **lap_map[x],
                "best_pace": None if split_type in NO_BEST_PACE_TYPES else lap_map[x]["best_pace"],
            }
            for x in lap_indexes if x in lap_map
            # Drop tail lap under 100m — distance remainder after completing planned workout
            if not (
                split_type == "INTERVAL_ACTIVE"
                and x == lap_indexes[-1]
                and lap_map[x]["distance_m"] < 100
            )
        ],
    }

    intervals.append(interval)

    if split_type != "INTERVAL_WARMUP":
        logical_interval += 1


# --- Time series ---

SAMPLE_INTERVAL = 10  # seconds

time_series = []

if records:
    t0 = records[0]["timestamp"]
    t_last = records[-1]["timestamp"]
    total_sec = int((t_last - t0).total_seconds())

    # Index records by elapsed second for fast lookup
    rec_by_sec = {}
    for rec in records:
        sec = int((rec["timestamp"] - t0).total_seconds())
        rec_by_sec[sec] = rec

    for sec in range(0, total_sec + 1, SAMPLE_INTERVAL):
        # Find nearest record within ±5 seconds
        closest = None
        for offset in range(0, 6):
            for delta in ([0, -offset, offset] if offset > 0 else [0]):
                r = rec_by_sec.get(sec + delta)
                if r is not None:
                    closest = r
                    break
            if closest:
                break

        if closest is None:
            time_series.append({
                "hr":              None,
                "pace_sec_per_km": None,
                "cadence":         None,
                "respiration":     None,
            })
        else:
            speed = closest.get("speed")
            pace_sec = round(1000 / speed) if speed and speed > 0 else None
            cad = closest.get("cadence")
            resp_raw = closest.get("respiration")

            time_series.append({
                "hr":              closest.get("hr"),
                "pace_sec_per_km": pace_sec,
                "cadence":         int(cad * 2) if cad is not None else None,
                "respiration":     round(resp_raw / 100, 1) if resp_raw is not None else None,
            })


# --- Output ---

running_data = {
    "generated_at": datetime.datetime.now().isoformat(),
    "activity_start": summary.get("startTimeLocal", "").split(".")[0],
    "activity": {
        "id":   activity.get("activityId"),
        "name": activity.get("activityName"),
        "summary": {
            "distance_km":           round(summary.get("distance", 0) / 1000, 2),
            "duration_sec":          round(summary.get("duration", 0), 1),
            "moving_time_sec":       round(summary.get("movingDuration", 0), 1),
            "avg_pace":              speed_to_pace(summary.get("averageSpeed")),
            "best_pace":             speed_to_pace(summary.get("maxSpeed")),
            "avg_hr":                int_or_none(summary.get("averageHR")),
            "max_hr":                int_or_none(summary.get("maxHR")),
            "training_load":         round(summary.get("activityTrainingLoad", 0), 1),
            "aerobic_effect":        round(summary.get("trainingEffect", 0), 1),
            "anaerobic_effect":      round(summary.get("anaerobicTrainingEffect", 0), 1),
            "training_effect_label": summary.get("trainingEffectLabel"),
            "avg_cadence":           int_or_none(summary.get("averageRunCadence")),
            "max_cadence":           int_or_none(summary.get("maxRunCadence")),
            "avg_stride_length_cm":  round(summary.get("strideLength", 0), 1) or None,
            "elevation_gain_m":      round(summary.get("elevationGain", 0), 1) or None,
            "elevation_loss_m":      round(summary.get("elevationLoss", 0), 1) or None,
            "avg_temperature":       round(summary.get("averageTemperature", 0), 1) or None,
            "min_temperature":       int_or_none(summary.get("minTemperature")),
            "max_temperature":       int_or_none(summary.get("maxTemperature")),
            "respiration_avg":       round(summary.get("avgRespirationRate", 0), 1) or None,
            "respiration_max":       round(summary.get("maxRespirationRate", 0), 1) or None,
            "fluid_loss_ml":         int_or_none(summary.get("waterEstimated")),
        },
    },
    "workout":    parsed_workout,
    "weather":    weather_data,
    "sleep":      sleep_data,
    "subjective": subjective,
    "intervals":  intervals,
    "time_series": {
        "sample_interval_sec": SAMPLE_INTERVAL,
        "data": time_series,
    },
}

with open("running-data.json", "w", encoding="utf-8") as f:
    json.dump(running_data, f, ensure_ascii=False, indent=2, default=str)

print("running-data.json created")
