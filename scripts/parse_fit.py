import json
import datetime
import os
import fitparse
from zoneinfo import ZoneInfo

fit = fitparse.FitFile("activity.fit")

# ---------- RECORDS (speed per second, used for best_pace calc) ----------

records = []

for r in fit.get_messages("record"):
    d = {field.name: field.value for field in r}
    ts = d.get("timestamp")
    speed = d.get("enhanced_speed") or d.get("speed")
    if ts and speed is not None and speed > 0:
        records.append({"timestamp": ts, "speed": speed})


# ---------- HELPERS ----------

def speed_to_pace(speed):
    if not speed:
        return None
    sec = 1000 / speed
    mins = int(sec // 60)
    secs = round(sec % 60)
    if secs == 60:
        mins += 1
        secs = 0
    return f"{mins}:{secs:02d}"


def int_or_none(value):
    """Конвертує float→int, None залишає None."""
    if value is None:
        return None
    return int(round(value))


# Типи лапів де best_pace не інформативний:
# хвости від сусідніх інтервалів спотворюють показник
NO_BEST_PACE_TYPES = {"INTERVAL_RECOVERY", "INTERVAL_COOLDOWN"}

# ---------- LAPS ----------


# ---------- LOAD FILES ----------

with open("activity.json", "r", encoding="utf-8") as f:
    activity = json.load(f)

summary = activity.get("summaryDTO", {})

workout = None
if os.path.exists("workout.json"):
    with open("workout.json", "r", encoding="utf-8") as f:
        workout = json.load(f)

KYIV_TZ = ZoneInfo("Europe/Kyiv")

def utc_ms_to_kyiv(ms):
    """Конвертує Unix timestamp у мілісекундах → рядок ISO в київському часі."""
    if ms is None:
        return None
    dt = datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)
    return dt.astimezone(KYIV_TZ).strftime("%H:%M")

sleep_data = None
if os.path.exists("sleep.json"):
    with open("sleep.json", "r", encoding="utf-8") as f:
        raw_sleep = json.load(f)
    try:
        sd = raw_sleep.get("dailySleepDTO", {})
        sleep_data = {
            "duration_hours": round(sd.get("sleepTimeSeconds", 0) / 3600, 2),
            "deep_hours":     round(sd.get("deepSleepSeconds", 0) / 3600, 2),
            "light_hours":    round(sd.get("lightSleepSeconds", 0) / 3600, 2),
            "rem_hours":      round(sd.get("remSleepSeconds", 0) / 3600, 2),
            "awake_minutes":  round(sd.get("awakeSleepSeconds", 0) / 60),
            "score":          sd.get("sleepScores", {}).get("overall"),
            "sleep_start":    utc_ms_to_kyiv(sd.get("sleepStartTimestampGMT")),
            "sleep_end":      utc_ms_to_kyiv(sd.get("sleepEndTimestampGMT")),
        }
    except Exception as e:
        print(f"Sleep parse error: {e}")


# ---------- WORKOUT ----------

parsed_workout = None
workout_steps = []

if workout:
    steps = []
    for segment in workout.get("workoutSegments", []):
        for step in segment.get("workoutSteps", []):
            target_type = step.get("targetType")
            target_key = (target_type or {}).get("workoutTargetTypeKey")
            v1 = step.get("targetValueOne")   # повільніша межа (м/с)
            v2 = step.get("targetValueTwo")   # швидша межа (м/с)

            if target_key == "pace.zone" and v1 and v2:
                target = {
                    "type":      "pace.zone",
                    "min_pace":  speed_to_pace(v2),   # швидший темп = мінімальний (менше сек/км)
                    "max_pace":  speed_to_pace(v1),   # повільніший темп = максимальний (більше сек/км)
                }
            elif target_key and target_key != "no.target":
                target = {"type": target_key}
            else:
                target = None

            step_data = {
                "index":       len(workout_steps),
                "type":        step.get("stepType"),
                "description": step.get("description"),
                "distance_m":  step.get("endConditionValue"),
                "target":      target,
            }
            workout_steps.append(step_data)
            steps.append(step_data)

    parsed_workout = {
        "id":    workout.get("workoutId"),
        "name":  workout.get("workoutName"),
        "steps": steps,
    }

# Кількість активних кроків у плані — щоб відрізнити
# планові інтервали від пробіжки після завершення плану
planned_active_count = sum(
    1 for s in workout_steps
    if isinstance(s.get("type"), dict)
    and s["type"].get("stepTypeKey") == "interval"
) if workout_steps else None


# ---------- SUBJECTIVE ----------

feeling_map = {0: "very_weak", 25: "weak", 50: "normal", 75: "strong", 100: "very_strong"}

rpe = summary.get("directWorkoutRpe")
subjective = {
    "feeling_score":    summary.get("directWorkoutFeel"),
    "feeling":          feeling_map.get(summary.get("directWorkoutFeel")),
    "perceived_effort": int(rpe / 10) if rpe is not None else None,
    "scale":            10,
}


# ---------- LAPS ----------

laps = []
lap_number = 1
cumulative_time = 0

for lap in fit.get_messages("lap"):
    data = {field.name: field.value for field in lap}

    duration = round(data.get("total_elapsed_time", 0), 1)
    moving_time = round(data.get("total_timer_time", duration), 1)
    cumulative_time += duration
    distance = data.get("total_distance", 0)

    # avg_speed: FIT lap не містить enhanced_avg_speed —
    # рахуємо з distance / timer_time
    avg_speed = (distance / moving_time) if (distance and moving_time and moving_time > 0) else None
    moving_speed = avg_speed

    # best_pace: беремо з record-ів в межах lap,
    # rolling 5-секундне вікно щоб згладити піки
    lap_start = data.get("start_time")
    lap_end = data.get("timestamp")
    max_speed = None

    if lap_start and lap_end:
        lap_rec_speeds = [
            r["speed"] for r in records
            if lap_start <= r["timestamp"] <= lap_end
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
        "lap":                    lap_number,
        "duration_sec":           duration,
        "cumulative_duration_sec": round(cumulative_time, 1),
        "distance_m":             round(distance, 2),
        "avg_pace":               speed_to_pace(avg_speed),
        "avg_hr":                 int_or_none(data.get("avg_heart_rate")),
        "max_hr":                 int_or_none(data.get("max_heart_rate")),
        "elevation_gain":         int_or_none(data.get("total_ascent")),
        "elevation_loss":         int_or_none(data.get("total_descent")),
        "avg_running_cadence":    int_or_none(cadence * 2) if cadence else None,
        "avg_stride_length_cm":   stride_length,
        "calories":               int_or_none(data.get("total_calories")),
        "avg_temperature":        data.get("avg_temperature"),
        "best_pace":              speed_to_pace(max_speed),
        "max_running_cadence":    int_or_none(max_cadence * 2) if max_cadence else None,
        "moving_time_sec":        moving_time,
        "nonstop_pace":           speed_to_pace(moving_speed),
    })

    lap_number += 1

lap_map = {x["lap"]: x for x in laps}


# ---------- INTERVALS ----------

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
        # Якщо відомо кількість планових інтервалів — позначаємо зайві як post_workout
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
            # Обрізаємо хвостовий лап (<100м) в активному інтервалі —
            # це залишок після завершення дистанції, не повноцінний кілометр
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


# ---------- OUTPUT ----------

running_data = {
    "generated_at": datetime.datetime.now().isoformat(),
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
    "sleep":      sleep_data,
    "subjective": subjective,
    "intervals":  intervals,
}

with open("running-data.json", "w", encoding="utf-8") as f:
    json.dump(running_data, f, ensure_ascii=False, indent=2, default=str)

print("running-data.json created")
