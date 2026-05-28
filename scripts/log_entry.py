"""
log_entry.py — transforms running-data.json into a compact training_log entry.

Used by upload_drive.py after each run to append to training_log.json on Drive.
"""

from datetime import datetime


def _map_steps(steps):
    """Recursively map workout steps, preserving repeat blocks."""
    result = []
    for step in steps:
        step_type = step.get("type")
        # Normalize stepType object to string key
        if isinstance(step_type, dict):
            step_type = step_type.get("stepTypeKey")

        if step_type == "repeat":
            nested = _map_steps(step.get("steps", []))
            if nested:
                result.append({
                    "type":  "repeat",
                    "reps":  step.get("reps"),
                    "steps": nested,
                })
        else:
            target = step.get("target")
            result.append({
                "type":       step_type,
                "distance_m": step.get("distance_m"),
                "target":     target,
            })
    return result


def build_log_entry(running_data):
    """
    Build a compact training_log entry from a full running-data dict.

    Returns a dict with: date, workout, result, intervals, sleep, subjective.
    """
    activity = running_data.get("activity", {})
    summary  = activity.get("summary", {})
    workout  = running_data.get("workout")
    sleep    = running_data.get("sleep") or {}
    subj     = running_data.get("subjective") or {}

    # Date from generated_at (ISO string)
    generated_at = running_data.get("generated_at", "")
    try:
        date_str = datetime.fromisoformat(generated_at).strftime("%d.%m.%Y")
    except Exception:
        date_str = "unknown"

    # Workout — compact steps
    workout_out = None
    if workout:
        workout_out = {
            "name":  workout.get("name"),
            "steps": _map_steps(workout.get("steps", [])),
        }

    # Result — key metrics only
    result = {
        "distance_km": summary.get("distance_km"),
        "avg_pace":    summary.get("avg_pace"),
        "avg_hr":      summary.get("avg_hr"),
    }

    # Intervals — compact: type, avg_pace, avg_hr, splits (lap, distance_m, avg_pace, avg_hr)
    intervals_out = []
    for interval in running_data.get("intervals", []):
        interval_type = interval.get("type")
        if interval_type == "post_workout":
            continue

        compact_splits = [
            {
                "lap":        s.get("lap"),
                "distance_m": round(s.get("distance_m", 0)),
                "avg_pace":   s.get("avg_pace"),
                "avg_hr":     s.get("avg_hr"),
            }
            for s in interval.get("splits", [])
        ]

        intervals_out.append({
            "type":     interval_type,
            "avg_pace": interval.get("summary", {}).get("avg_pace"),
            "avg_hr":   interval.get("summary", {}).get("avg_hr"),
            "splits":   compact_splits,
        })

    # Sleep — score and duration only
    sleep_score = sleep.get("score")
    if isinstance(sleep_score, dict):
        sleep_score = sleep_score.get("value")

    sleep_out = None
    if sleep:
        sleep_out = {
            "score":          sleep_score,
            "duration_hours": sleep.get("duration_hours"),
        }

    # Subjective
    subj_out = None
    if subj.get("feeling") is not None:
        subj_out = {
            "feeling":          subj.get("feeling"),
            "perceived_effort": subj.get("perceived_effort"),
        }

    return {
        "date":       date_str,
        "workout":    workout_out,
        "result":     result,
        "intervals":  intervals_out,
        "sleep":      sleep_out,
        "subjective": subj_out,
    }
