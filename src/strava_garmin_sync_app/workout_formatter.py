"""Build a human-readable description from a Garmin workout structure.

Used to fill the Strava activity description with the planned session
(target paces, repeats, warmup/cooldown) instead of the slug that training
plans put in the Garmin workout description field.
"""
import re
from typing import Optional

STEP_LABELS = {
    "warmup": "Échauffement",
    "cooldown": "Retour au calme",
    "interval": "Effort",
    "recovery": "Récupération",
    "rest": "Repos",
    "other": "Étape",
}


def fmt_duration(seconds: float) -> str:
    """1830 -> '30 min 30', 3600 -> '1 h', 45 -> '45 s'."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours} h")
    if minutes:
        parts.append(f"{minutes} min")
    if secs:
        parts.append(f"{secs:02d}" if minutes and not hours else f"{secs} s")
    return " ".join(parts)


def fmt_distance(meters: float) -> str:
    """5000 -> '5 km', 400 -> '400 m'."""
    if meters >= 1000:
        return f"{meters / 1000:g} km"
    return f"{int(meters)} m"


def fmt_pace(mps: float) -> str:
    """Speed in m/s -> 'M:SS/km'."""
    if not mps:
        return "?"
    sec_per_km = 1000 / mps
    minutes, secs = divmod(round(sec_per_km), 60)
    return f"{minutes}:{secs:02d}/km"


def fmt_target(step: dict) -> str:  # pylint: disable=too-many-return-statements
    """Human-readable target of an executable step ('' if no target)."""
    target_key = (step.get("targetType") or {}).get("workoutTargetTypeKey", "no.target")
    low = step.get("targetValueOne")
    high = step.get("targetValueTwo")
    zone = step.get("zoneNumber")

    if target_key == "pace.zone" and low and high:
        # values are speeds in m/s; display the range fastest pace first
        return f"allure {fmt_pace(max(low, high))} à {fmt_pace(min(low, high))}"
    if target_key == "heart.rate.zone":
        if zone:
            return f"FC zone {zone}"
        if low and high:
            return f"FC {int(low)}-{int(high)} bpm"
    if target_key == "power.zone":
        if zone:
            return f"puissance zone {zone}"
        if low and high:
            return f"{int(low)}-{int(high)} W"
    if target_key == "cadence" and low and high:
        return f"cadence {int(low)}-{int(high)}"
    return ""


def fmt_end_condition(step: dict) -> str:
    """Human-readable duration/distance of an executable step."""
    condition = (step.get("endCondition") or {}).get("conditionTypeKey", "")
    value = step.get("endConditionValue")
    if condition == "time" and value:
        return fmt_duration(value)
    if condition == "distance" and value:
        return fmt_distance(value)
    if condition == "lap.button":
        return "jusqu'au bouton lap"
    if condition == "calories" and value:
        return f"{int(value)} kcal"
    return ""


def fmt_step(step: dict) -> str:
    """One executable step -> 'Effort : 10 min (allure 4:30/km à 4:15/km)'."""
    label = STEP_LABELS.get(
        (step.get("stepType") or {}).get("stepTypeKey", "other"), "Étape")
    parts = [label]
    end = fmt_end_condition(step)
    if end:
        parts.append(f": {end}")
    target = fmt_target(step)
    if target:
        parts.append(f"({target})")
    note = (step.get("description") or "").strip()
    if note:
        parts.append(f"— {note}")
    return " ".join(parts)


def build_steps_text(steps: list) -> list:
    """Recursively render workout steps (handles repeat groups) as lines."""
    lines = []
    for step in steps or []:
        if step.get("type") == "RepeatGroupDTO" or \
                (step.get("stepType") or {}).get("stepTypeKey") == "repeat":
            count = step.get("numberOfIterations", 1)
            sub = build_steps_text(step.get("workoutSteps"))
            joined = " + ".join(line.lstrip("- ") for line in sub)
            lines.append(f"- {count} × ({joined})")
        else:
            lines.append(f"- {fmt_step(step)}")
    return lines


def is_meaningful_description(description: str) -> bool:
    """False for empty or slug-like descriptions ('progressive_run', 'threshold')
    that training plans put in the field — those are not worth copying."""
    if not description:
        return False
    return not re.fullmatch(r"[a-z0-9_.-]+", description)


def build_workout_description(workout: dict) -> Optional[str]:
    """The description text for the Strava activity.

    A meaningful (human-written) workout description is copied as-is.
    Otherwise the text is generated from the steps structure, with a header
    line like 'Séance : 7x3' Intervals Run (vo2max)'.
    Returns None when the workout has nothing usable.
    """
    description = (workout.get("description") or "").strip()
    if is_meaningful_description(description):
        return description

    lines = []
    for segment in workout.get("workoutSegments") or []:
        lines.extend(build_steps_text(segment.get("workoutSteps")))
    if not lines:
        return description or None

    name = (workout.get("workoutName") or "").strip()
    if name:
        header = f"Séance : {name}" + (f" ({description})" if description else "")
        return "\n".join([header] + lines)
    return "\n".join(lines)
