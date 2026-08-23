"""Garmin service helpers for fetching and normalizing activities."""

import json
import logging
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from .constants import GARMIN_TO_STRAVA_TYPE

logger = logging.getLogger(__name__)


def _parse_garmin_start_time(start_time_str: str) -> Optional[datetime]:
    """Parse start time string from Garmin into a datetime if possible."""
    if not start_time_str:
        return None
    try:
        if 'T' in start_time_str:
            return datetime.fromisoformat(start_time_str.replace('Z', ''))
        return datetime.strptime(start_time_str, '%Y-%m-%d %H:%M:%S')
    except Exception as err:  # pylint: disable=broad-except
        logger.warning("Format de date non reconnu: %s (%s)", start_time_str, err)
        return None


def _maybe_attach_workout(garmin_client, activity: Dict) -> None:
    """Attach workout details to an activity dict when a workoutId is present."""
    associated_workout_id = activity.get('workoutId')
    logger.info("Workout ID associé à l'activité Garmin: %s", associated_workout_id)
    if not associated_workout_id:
        return
    try:
        workout = garmin_client.get_workout_by_id(str(associated_workout_id))
        logger.debug("workout garmin: %s", json.dumps(workout, indent=2, ensure_ascii=False))
        activity['workout'] = workout
    except Exception as e:  # pylint: disable=broad-except
        logger.warning("Impossible de récupérer le workout %s: %s", associated_workout_id, e)


def process_garmin_activity(garmin_client, activities: Dict[str, Dict], activity: Dict) -> None:
    """Normalize and add a Garmin activity to the collected dict when valid."""
    logger.info(
        "Garmin Activity: ID=%s, Name='%s', Start=%s, Type=%s",
        activity.get("activityId"),
        activity.get("activityName"),
        activity.get("startTimeLocal"),
        activity.get("activityType", {}).get("typeKey"),
    )
    logger.debug("Garmin Activity: %s", json.dumps(activity, indent=2, ensure_ascii=False))

    activity_id = str(activity.get('activityId', ''))
    if not activity_id:
        return

    # Prefer GMT so matching against Strava (start_date in UTC) is insensitive
    # to timezones and DST; fall back to local time if GMT is missing.
    start_time_str = activity.get('startTimeGMT') or activity.get('startTimeLocal') or ''
    if not activity.get('startTimeGMT'):
        logger.warning(
            "Activité Garmin %s sans startTimeGMT, repli sur l'heure locale "
            "(le matching peut échouer)", activity_id)
    parsed_start = _parse_garmin_start_time(start_time_str)
    if not parsed_start:
        return
    activity['parsed_start_time'] = parsed_start

    _maybe_attach_workout(garmin_client, activity)
    activities[activity_id] = activity


def get_garmin_activities_for_period(garmin_client, cache,
                                     days: int = 7) -> tuple[Dict[str, Dict], bool]:
    """Fetch all Garmin activities for the last `days` days.

    Returns (activities, complete) — see get_garmin_activities_between.
    """
    end_date = datetime.now()
    return get_garmin_activities_between(
        garmin_client, cache, end_date - timedelta(days=days), end_date)


def get_garmin_activities_between(garmin_client, cache, start_date,
                                  end_date) -> tuple[Dict[str, Dict], bool]:
    """Fetch all Garmin activities between two dates, with an in-memory cache.

    Returns (activities, complete) where `complete` is False when at least one
    day could not be fetched. A partial result must never be taken as proof
    that an activity has no Garmin counterpart, otherwise a transient Garmin
    error would permanently mark the activity as unmatchable.
    """
    cache_key = f"garmin_activities_{start_date:%Y-%m-%d}_{end_date:%Y-%m-%d}"
    current_time = time.time()

    cached = cache.data.get(cache_key)
    if cached and (current_time - cached.get('timestamp', 0) < cache.duration):
        logger.info("Utilisation du cache activités Garmin (%s)", cache_key)
        return cached.get('data', {}), cached.get('complete', True)

    activities: Dict[str, Dict] = {}
    failed_days = []
    current_date = start_date

    while current_date <= end_date:
        date_str = current_date.strftime('%Y-%m-%d')
        try:
            daily = garmin_client.get_activities_by_date(date_str, date_str)
            if daily:
                for act in daily:
                    process_garmin_activity(garmin_client, activities, act)
            time.sleep(1)
        except Exception as e:  # pylint: disable=broad-except
            logger.warning("Erreur récupération activités Garmin pour %s: %s", date_str, e)
            failed_days.append(date_str)
        current_date += timedelta(days=1)

    complete = not failed_days
    if not complete:
        logger.warning(
            "⚠️ Récupération Garmin incomplète (%s jour(s) en échec: %s) — "
            "les activités sans correspondance ne seront pas mises en cache",
            len(failed_days), ", ".join(failed_days))

    cache.data[cache_key] = {
        'data': activities,
        'timestamp': current_time,
        'complete': complete,
    }
    logger.info("Récupéré %s activités Garmin entre %s et %s",
                len(activities), f"{start_date:%Y-%m-%d}", f"{end_date:%Y-%m-%d}")
    return activities, complete


def get_training_readiness_snapshot(garmin_client) -> Optional[Dict]:
    """Current training readiness: score, level and recovery time in minutes."""
    try:
        data = garmin_client.get_training_readiness(datetime.now().strftime('%Y-%m-%d'))
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            return {
                'score': item.get('score'),
                'level': item.get('level'),
                'recovery_minutes': item.get('recoveryTime'),
            }
    except Exception as e:  # pylint: disable=broad-except
        logger.warning("Impossible de récupérer le training readiness: %s", e)
    return None


def get_scheduled_workouts(garmin_client, start: date, end: date) -> List[Dict]:
    """Scheduled workout calendar items between start and end (inclusive)."""
    items: List[Dict] = []
    # calendar-service months are 0-based; cover both months if the range spans two
    months = {(start.year, start.month), (end.year, end.month)}
    try:
        for year, month in sorted(months):
            calendar = garmin_client.connectapi(
                f"/calendar-service/year/{year}/month/{month - 1}")
            items.extend(calendar.get("calendarItems") or [])
    except Exception as e:  # pylint: disable=broad-except
        logger.warning("Impossible de récupérer le calendrier Garmin: %s", e)
        return []

    scheduled = []
    for item in items:
        if item.get("itemType") != "workout":
            continue
        item_date = item.get("date")
        if item_date and start.isoformat() <= item_date <= end.isoformat():
            scheduled.append(item)
    scheduled.sort(key=lambda i: i.get("date", ""))
    return scheduled


__all__ = [
    "GARMIN_TO_STRAVA_TYPE",
    "get_garmin_activities_for_period",
    "get_garmin_activities_between",
    "get_scheduled_workouts",
    "get_training_readiness_snapshot",
    "process_garmin_activity",
]
