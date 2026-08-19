"""Test app: fetch next week's scheduled Garmin workouts and show the
description text the sync would write on the matching Strava activity.

For each scheduled workout: a meaningful description is copied; a slug-only
description ('progressive_run', ...) is replaced by a text generated from the
workout steps structure.

Read-only: nothing is written to Garmin or Strava.
"""
import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent / "src"))

load_dotenv()

# pylint: disable=wrong-import-position  # imports need the sys.path insert above
from garminconnect import Garmin  # noqa: E402
from strava_garmin_sync_app.garmin_service import get_scheduled_workouts  # noqa: E402
from strava_garmin_sync_app.workout_formatter import (  # noqa: E402
    build_workout_description,
    is_meaningful_description,
)

logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def next_week_range(today: date) -> tuple[date, date]:
    """Monday to Sunday of the week after `today`'s week."""
    next_monday = today + timedelta(days=7 - today.weekday())
    return next_monday, next_monday + timedelta(days=6)


def main():
    """Fetch and display next week's scheduled workouts with their note text."""
    tokenstore = os.getenv("GARMIN_TOKENS_FILE_LOC") or "data/.garminconnect"
    garmin = Garmin()
    garmin.login(tokenstore)
    logger.info("Connecté à Garmin: %s", garmin.get_full_name())

    start, end = next_week_range(date.today())
    logger.info("Semaine prochaine: %s → %s", start, end)

    scheduled = get_scheduled_workouts(garmin, start, end)
    logger.info("%s entraînement(s) planifié(s) trouvé(s)", len(scheduled))

    for item in scheduled:
        workout_id = item.get("workoutId")
        title = item.get("title")
        print("\n" + "=" * 60)
        print(f"📅 {item.get('date')}  —  {title}  (workoutId={workout_id})")
        print("=" * 60)

        if not workout_id:
            print("⚠️ Pas de workoutId sur cet élément de calendrier:")
            print(json.dumps(item, indent=2, ensure_ascii=False))
            continue

        workout = garmin.get_workout_by_id(str(workout_id))
        logger.debug("workout: %s", json.dumps(workout, indent=2, ensure_ascii=False))

        source = "description copiée" \
            if is_meaningful_description((workout.get("description") or "").strip()) \
            else "générée depuis les étapes"
        print(f"📝 Description Strava ({source}):\n")
        print(build_workout_description(workout) or "(rien d'utilisable dans ce workout)")


if __name__ == "__main__":
    main()
