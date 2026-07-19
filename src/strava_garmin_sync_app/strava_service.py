"""Strava service helpers for fetching and updating activities."""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import List

from .models import ActivityData

logger = logging.getLogger(__name__)


def _to_naive_utc(value):
    """Normalize a datetime to naive UTC for timezone-insensitive comparisons."""
    if hasattr(value, 'tzinfo') and value.tzinfo:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def get_recent_strava_activities(strava_client, days: int = 7) -> List[ActivityData]:
    """Fetch recent Strava activities and map to ActivityData list.

    start_date is normalized to naive UTC so matching against Garmin
    (startTimeGMT) is insensitive to timezones and DST shifts.
    """
    after_date = datetime.now(timezone.utc) - timedelta(days=days)
    activities = strava_client.get_activities(after=after_date, limit=200)

    results: List[ActivityData] = []
    for activity in activities:
        start_date = _to_naive_utc(activity.start_date)

        logger.info(
            "Strava Activity: ID=%s, Name='%s', Start(UTC)=%s, Type=%s, sport_type=%s",
            activity.id,
            activity.name,
            start_date,
            activity.type.root,
            activity.sport_type.root,
        )
        has_polyline = bool(activity.map and activity.map.summary_polyline)
        if has_polyline:
            logger.info("Strava Activity %s has a map polyline", activity.id)

        logger.debug("Strava Activity: %s", activity)

        results.append(
            ActivityData(
                id=str(activity.id),
                name=activity.name or "",
                start_date=start_date,
                type=activity.type.root or "Unknown",
                has_polyline=has_polyline,
                trainer=bool(getattr(activity, "trainer", False)),
                manual=bool(getattr(activity, "manual", False)),
            )
        )
    logger.info("Récupéré %s activités Strava", len(results))
    return results


def update_strava_activity(
    strava_client,
    dry_run: bool,
    activity_id: str,
    name: str,
    description: str,
) -> bool:
    """Update a Strava activity's name and description, honoring dry-run."""
    if dry_run:
        logger.info(
            "[DRY RUN] 🚫 Simuler mise à jour activité Strava %s: '%s' '%s'",
            activity_id,
            name,
            description,
        )
        return True
    try:
        strava_client.update_activity(
            activity_id=int(activity_id),
            name=name,
            description=description,
        )
        logger.info("✅ Activité Strava %s mise à jour: '%s'", activity_id, name)
        return True
    except Exception as e:  # pylint: disable=broad-except
        logger.error("❌ Erreur mise à jour activité Strava %s: %s", activity_id, e)
        return False


def get_strava_activity(strava_client, activity_id: str):
    """Fetch a single Strava activity (detailed) by ID.

    Returns the detailed activity object from stravalib on success, or None on error.
    """
    try:
        act = strava_client.get_activity(int(activity_id))
        start_date = _to_naive_utc(getattr(act, "start_date", None))
        logger.info(
            "Strava Detailed: ID=%s, Name='%s', Start(UTC)=%s, Type=%s, sport_type=%s",
            activity_id,
            getattr(act, "name", None),
            start_date,
            getattr(getattr(act, "type", None), "root", None),
            getattr(getattr(act, "sport_type", None), "root", None),
        )
        if act.map and act.map.summary_polyline:
            logger.info("Strava Activity %s has a map polyline", activity_id)
        logger.debug("Fetched Strava activity %s: %s", activity_id, act)
        return act
    except Exception as e:  # pylint: disable=broad-except
        logger.error("❌ Erreur récupération activité Strava %s: %s", activity_id, e)
        return None


def wait_until_processed(strava_client, activity_id: int, max_retries: int = 5, delay: int = 120):
    """
    Poll Strava until the activity is fully processed (map, achievements, etc. available).

    Args:
        strava_client (Client): Authenticated stravalib client.
        activity_id (int): The Strava activity ID.
        max_retries (int): Number of polling attempts before giving up.
        delay (int): Delay in seconds between retries.

    Returns:
        Activity: The processed activity object, the last fetched one if still
        unprocessed, or None if it could never be fetched.
    """
    activity = None
    for attempt in range(max_retries):
        activity = get_strava_activity(strava_client, activity_id)

        # Check if the activity looks "processed"
        if activity is not None and activity.map and activity.map.summary_polyline:
            logger.info("✅ Activity %s processed after %d attempts.", activity_id, attempt + 1)
            return activity

        logger.info("⏳ Activity %s not ready (attempt %d/%d). Retrying in %s seconds...",
                    activity_id, attempt + 1, max_retries, delay)
        time.sleep(delay)

    logger.warning("⚠️ Activity %s still not fully processed after %d attempts.",
                   activity_id, max_retries)
    return activity
