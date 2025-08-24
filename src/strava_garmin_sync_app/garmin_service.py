"""Garmin service helpers for fetching and normalizing activities."""

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, Optional

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

    parsed_start = _parse_garmin_start_time(activity.get('startTimeLocal', ''))
    if not parsed_start:
        return
    activity['parsed_start_time'] = parsed_start

    _maybe_attach_workout(garmin_client, activity)
    activities[activity_id] = activity


def get_garmin_activities_for_period(garmin_client, cache, days: int = 7) -> Dict[str, Dict]:
    """Fetch all Garmin activities for a date range using a simple in-memory cache."""
    cache_key = f"garmin_activities_{days}"
    current_time = time.time()

    cached = cache.data.get(cache_key)
    if cached and (current_time - cached.get('timestamp', 0) < cache.duration):
        logger.info("Utilisation du cache activités Garmin (%s jours)", days)
        return cached.get('data', {})

    activities: Dict[str, Dict] = {}
    start_date = datetime.now() - timedelta(days=days)
    current_date = start_date

    while current_date <= datetime.now():
        date_str = current_date.strftime('%Y-%m-%d')
        try:
            daily = garmin_client.get_activities_by_date(date_str, date_str)
            if daily:
                for act in daily:
                    process_garmin_activity(garmin_client, activities, act)
            time.sleep(1)
        except Exception as e:  # pylint: disable=broad-except
            logger.warning("Erreur récupération activités Garmin pour %s: %s", date_str, e)
        current_date += timedelta(days=1)

    cache.data[cache_key] = {
        'data': activities,
        'timestamp': current_time,
    }
    logger.info("Récupéré %s activités Garmin sur %s jours", len(activities), days)
    return activities


__all__ = [
    "GARMIN_TO_STRAVA_TYPE",
    "get_garmin_activities_for_period",
    "process_garmin_activity",
]
