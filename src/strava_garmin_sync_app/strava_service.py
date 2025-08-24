"""Strava service helpers for fetching and updating activities."""
import logging
from datetime import datetime, timedelta
from typing import List

from .models import ActivityData

logger = logging.getLogger(__name__)


def get_recent_strava_activities(strava_client, days: int = 7) -> List[ActivityData]:
    """Fetch recent Strava activities and map to ActivityData list."""
    after_date = datetime.now() - timedelta(days=days)
    activities = strava_client.get_activities(after=after_date, limit=200)

    results: List[ActivityData] = []
    for activity in activities:
        start_date = activity.start_date_local
        if hasattr(start_date, 'replace') and start_date.tzinfo:
            start_date = start_date.replace(tzinfo=None)

        logger.info(
            "Strava Activity: ID=%s, Name='%s', Start=%s, Type=%s, sport_type=%s",
            activity.id,
            activity.name,
            start_date,
            activity.type.root,
            activity.sport_type.root,
        )
        logger.debug("Strava Activity: %s", activity)

        results.append(
            ActivityData(
                id=str(activity.id),
                name=activity.name or "",
                start_date=start_date,
                type=activity.type.root or "Unknown",
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
