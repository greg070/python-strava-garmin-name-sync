"""Build a human-readable description from a Garmin workout structure.

Used to fill the Strava activity description with the planned session
(target paces, repeats, warmup/cooldown) instead of the slug that training
plans put in the Garmin workout description field.
"""
import re
from typing import List, Optional

STEP_LABELS = {
    "warmup": "Échauffement",
    "cooldown": "Retour au calme",
    "interval": "Effort",
    "recovery": "Récupération",
    "rest": "Repos",
    "other": "Étape",
}

# Tolérance sur l'allure réalisée vs la cible avant de marquer ⚠️ (s/km)
PACE_TOLERANCE_SEC = 5


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


def is_app_generated_description(description: str) -> bool:
    """True when the description was written by this app (our 'Séance :' header).

    Backfill may replace its own output (e.g. to add the executed report) but
    must never touch a hand-written description.
    """
    return bool(description) and description.strip().startswith("Séance : ")


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


def fmt_pace_short(mps: float) -> str:
    """Speed in m/s -> 'M:SS' (no /km suffix, for compact lists)."""
    if not mps:
        return "?"
    minutes, secs = divmod(round(1000 / mps), 60)
    return f"{minutes}:{secs:02d}"


def flatten_steps(workout: dict) -> List[dict]:
    """Flatten the workout structure into executed order (repeats expanded).

    Each entry: {'key', 'label', 'low', 'high', 'group'} where low/high are the
    target speeds in m/s (None without a pace target) and group identifies the
    repeat block the step belongs to (None outside repeats).
    """
    steps: List[dict] = []
    group_counter = [0]

    def add_step(step: dict, group: Optional[int]) -> None:
        key = (step.get("stepType") or {}).get("stepTypeKey", "other")
        target_key = (step.get("targetType") or {}).get("workoutTargetTypeKey", "")
        low = high = None
        if target_key == "pace.zone":
            one, two = step.get("targetValueOne"), step.get("targetValueTwo")
            if one and two:
                low, high = min(one, two), max(one, two)
        steps.append({
            "key": key,
            "label": STEP_LABELS.get(key, "Étape"),
            "low": low,
            "high": high,
            "group": group,
        })

    def walk(children: list, group: Optional[int]) -> None:
        for step in children or []:
            if step.get("type") == "RepeatGroupDTO" or \
                    (step.get("stepType") or {}).get("stepTypeKey") == "repeat":
                count = step.get("numberOfIterations", 1)
                group_counter[0] += 1
                gid = group_counter[0]
                for _ in range(count):
                    walk(step.get("workoutSteps"), gid)
            else:
                add_step(step, group)

    for segment in workout.get("workoutSegments") or []:
        walk(segment.get("workoutSteps"), None)
    return steps


def _pace_verdict(step: dict, speed: float) -> str:
    """' ✅'/' ⚠️' vs the step target; '' for recoveries or targetless steps.

    A slow recovery is physiologically fine, so recovery/rest steps never get
    a verdict.
    """
    if step["key"] in ("recovery", "rest") or not step["low"] or not speed:
        return ""
    pace = 1000 / speed
    fastest = 1000 / step["high"]
    slowest = 1000 / step["low"]
    if fastest - PACE_TOLERANCE_SEC <= pace <= slowest + PACE_TOLERANCE_SEC:
        return " ✅"
    return " ⚠️"


def build_execution_report(workout: dict, laps: list) -> Optional[List[str]]:
    """Compare the planned steps to the executed Strava laps, line by line.

    Watches record one lap per workout step, so laps are aligned positionally
    with the flattened steps (trailing extra laps are ignored). Returns None
    when the shapes don't match — better no report than a wrong one.
    """
    steps = flatten_steps(workout)
    if not steps or not laps or len(laps) < len(steps):
        return None

    entries = []
    for step, executed_lap in zip(steps, laps):
        speed = float(getattr(executed_lap, "average_speed", 0) or 0)
        if not speed:
            return None
        entries.append({**step, "pace": fmt_pace_short(speed),
                        "verdict": _pace_verdict(step, speed)})
    return ["Réalisé :"] + _render_report_lines(entries)


def _render_report_lines(entries: List[dict]) -> List[str]:
    """Render report entries, grouping repeat blocks into one line per label."""
    lines = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        if entry["group"] is None:
            lines.append(f"- {entry['label']} : {entry['pace']}/km{entry['verdict']}")
            i += 1
            continue
        # Repeat block: one line per step label with all iteration paces
        block = []
        while i < len(entries) and entries[i]["group"] == entry["group"]:
            block.append(entries[i])
            i += 1
        by_label: dict = {}
        for item in block:
            by_label.setdefault(item["label"], []).append(item)
        for label, items in by_label.items():
            values = " · ".join(f"{x['pace']}{x['verdict']}" for x in items)
            lines.append(f"- {len(items)} × {label} : {values}")
    return lines


def build_metrics_line(garmin_activity: dict,
                       recovery_minutes: Optional[int] = None) -> Optional[str]:
    """'📊 TE 3.6 aérobie / 1.7 anaérobie · Charge 149 · VO2max 53 · Récup 25 h'."""
    parts = []
    aero = garmin_activity.get("aerobicTrainingEffect")
    ana = garmin_activity.get("anaerobicTrainingEffect")
    if aero:
        text = f"TE {aero:.1f} aérobie"
        if ana:
            text += f" / {ana:.1f} anaérobie"
        parts.append(text)
    load = garmin_activity.get("activityTrainingLoad")
    if load:
        parts.append(f"Charge {round(load)}")
    vo2 = garmin_activity.get("vO2MaxValue")
    if vo2:
        parts.append(f"VO2max {vo2:g}")
    if recovery_minutes:
        parts.append(f"Récup {round(recovery_minutes / 60)} h")
    if not parts:
        return None
    return "📊 " + " · ".join(parts)
