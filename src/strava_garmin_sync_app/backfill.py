"""One-shot backfill: apply workout names and structured descriptions to past
Strava activities, bypassing the synced cache.

Usage:
    python -m strava_garmin_sync_app.backfill                # dry run, 7 derniers jours
    python -m strava_garmin_sync_app.backfill --apply        # applique réellement
    python -m strava_garmin_sync_app.backfill --start 2026-06-01 --end 2026-06-30

Safety guards:
- only activities matched to a Garmin activity with an attached planned
  workout are considered (hikes/rides without a plan are never touched)
- an activity whose current Strava description is hand-written (not empty,
  not a training-plan slug like 'vo2max') is never touched
- dry run by default: --apply is required to write anything
"""
import argparse
import logging
import time
from datetime import date, datetime, time as dtime, timedelta, timezone

from dotenv import load_dotenv

from .garmin_service import get_garmin_activities_between
from .strava_garmin_sync import StravaGarminSync, setup_logging
from .strava_service import get_strava_activities_between, get_strava_activity
from .workout_formatter import is_meaningful_description

logger = logging.getLogger(__name__)


def compute_backfill_update(
    current_description,
    needs_name_update: bool,
    new_description,
) -> tuple[bool, str]:
    """Decide whether a backfill update is safe and needed. Returns (do, reason).

    A hand-written Strava description blocks any update: the user's edits
    must survive a backfill.
    """
    current = (current_description or '').strip()
    if is_meaningful_description(current):
        return False, 'description personnalisée sur Strava — non touchée'

    description_outdated = bool(new_description) and new_description.strip() != current
    if needs_name_update and description_outdated:
        return True, 'nom + description'
    if needs_name_update:
        return True, 'nom'
    if description_outdated:
        return True, 'description'
    return False, 'déjà à jour'


def _backfill_one_activity(sync, activity, garmin_activities) -> str:
    """Process one activity; returns 'updates', 'skipped' or 'errors'."""
    garmin_activity = sync.find_matching_garmin_activity(activity, garmin_activities)
    if not garmin_activity:
        logger.info("⏭️ '%s': aucune activité Garmin correspondante", activity.name)
        return "skipped"
    if not garmin_activity.get('workout'):
        logger.info("⏭️ '%s': pas de séance planifiée associée", activity.name)
        return "skipped"

    detailed = get_strava_activity(sync.clients.strava, activity.id)
    if detailed is None:
        return "errors"
    current_description = getattr(detailed, 'description', None)

    needs_name_update, new_name, new_description = sync.should_update_activity(
        activity, garmin_activity)

    do_update, reason = compute_backfill_update(
        current_description, needs_name_update, new_description)
    if not do_update:
        logger.info("⏭️ '%s': %s", activity.name, reason)
        return "skipped"

    logger.info("🔄 '%s' → '%s' (%s)", activity.name, new_name, reason)
    if sync.update_strava_activity(activity.id, new_name, new_description):
        return "updates"
    return "errors"


def run_backfill(start_date: date, end_date: date, apply: bool) -> bool:
    """Backfill activities between start_date and end_date (inclusive)."""
    sync = StravaGarminSync()
    sync.general.dry_run = not apply

    logger.info("🔁 Backfill du %s au %s (%s)", start_date, end_date,
                "APPLICATION RÉELLE" if apply else "dry run")

    if not sync.init_strava_client() or not sync.init_garmin_client():
        return False

    after_dt = datetime.combine(start_date, dtime.min, tzinfo=timezone.utc)
    before_dt = datetime.combine(end_date + timedelta(days=1), dtime.min, tzinfo=timezone.utc)
    strava_activities = get_strava_activities_between(
        sync.clients.strava, after_dt, before_dt)

    # marge d'un jour de chaque côté: les jours Garmin sont en heure locale
    garmin_activities = get_garmin_activities_between(
        sync.clients.garmin, sync.cache,
        datetime.combine(start_date - timedelta(days=1), dtime.min),
        datetime.combine(end_date + timedelta(days=1), dtime.min))

    counts = {"updates": 0, "skipped": 0, "errors": 0}
    updated_ids = []

    for activity in strava_activities:
        outcome = _backfill_one_activity(sync, activity, garmin_activities)
        counts[outcome] += 1
        if outcome == "updates":
            updated_ids.append(activity.id)
            time.sleep(1)

    if apply and updated_ids:
        window_days = max((date.today() - start_date).days, 7)
        sync.mark_activities_synced(updated_ids, window_days)

    logger.info("=" * 50)
    logger.info("✅ Backfill terminé: %s mise(s) à jour, %s ignorée(s), %s erreur(s)",
                counts["updates"], counts["skipped"], counts["errors"])
    if not apply:
        logger.info("ℹ️ Dry run — relancez avec --apply pour écrire sur Strava")
    logger.info("=" * 50)
    return True


def parse_args(argv=None) -> argparse.Namespace:
    """Parse CLI arguments (default range: the last 7 days)."""
    parser = argparse.ArgumentParser(
        description="Backfill des noms et descriptions structurées sur Strava")
    parser.add_argument('--start', type=date.fromisoformat,
                        help="date de début YYYY-MM-DD (défaut: il y a 7 jours)")
    parser.add_argument('--end', type=date.fromisoformat,
                        help="date de fin YYYY-MM-DD incluse (défaut: aujourd'hui)")
    parser.add_argument('--apply', action='store_true',
                        help="applique réellement les modifications (défaut: dry run)")
    args = parser.parse_args(argv)
    if args.end is None:
        args.end = date.today()
    if args.start is None:
        args.start = args.end - timedelta(days=7)
    if args.start > args.end:
        parser.error("--start doit être antérieure à --end")
    return args


def main(argv=None):
    """CLI entry point."""
    load_dotenv()
    setup_logging()
    args = parse_args(argv)
    if not run_backfill(args.start, args.end, args.apply):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
