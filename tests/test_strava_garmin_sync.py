"""Unit tests for the Strava-Garmin sync application."""
# pylint: disable=missing-function-docstring  # test names are self-describing
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from strava_garmin_sync_app import StravaGarminSync
from strava_garmin_sync_app.models import ActivityData
from strava_garmin_sync_app.garmin_service import _parse_garmin_start_time

DUMMY_ENV = {
    'STRAVA_CLIENT_ID': 'dummy',
    'STRAVA_CLIENT_SECRET': 'dummy',
    'STRAVA_ACCESS_TOKEN': 'dummy',
    'STRAVA_REFRESH_TOKEN': 'dummy',
    'STRAVA_TOKEN_EXPIRES_AT': '9999999999',
    'GARMIN_EMAIL': 'dummy',
    'GARMIN_PASSWORD': 'dummy',
}


def make_strava_activity(**overrides):
    """Build an ActivityData with sensible defaults for tests."""
    defaults = {
        'id': '1',
        'name': 'Morning Run',
        'start_date': datetime(2026, 7, 18, 8, 0, 0),
        'type': 'Run',
    }
    defaults.update(overrides)
    return ActivityData(**defaults)


class TestStravaGarminSync(unittest.TestCase):
    """Tests running against a temporary working directory and dummy env vars."""

    def setUp(self):
        self.env_patcher = patch.dict(os.environ, DUMMY_ENV)
        self.env_patcher.start()
        self.tmpdir = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.old_cwd = os.getcwd()
        os.chdir(self.tmpdir.name)
        os.makedirs('data', exist_ok=True)
        self.sync = StravaGarminSync()

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmpdir.cleanup()
        self.env_patcher.stop()

    # --- Configuration ---

    def test_validate_config_missing_var(self):
        with patch.dict(os.environ, {**DUMMY_ENV, 'GARMIN_EMAIL': ''}):
            with self.assertRaises(ValueError):
                StravaGarminSync()

    def test_is_token_expired(self):
        self.sync.strava.token_expires_at = int(time.time()) + 3600
        self.assertFalse(self.sync.is_token_expired())
        self.sync.strava.token_expires_at = int(time.time()) - 10
        self.assertTrue(self.sync.is_token_expired())

    # --- should_update_activity ---

    def test_no_update_when_names_match(self):
        strava_activity = make_strava_activity(name='Morning Run')
        garmin_activity = {'activityName': 'Morning Run', 'description': 'Nice run'}
        needs_update, new_name, _ = self.sync.should_update_activity(
            strava_activity, garmin_activity)
        self.assertFalse(needs_update)
        self.assertEqual(new_name, 'Morning Run')

    def test_update_when_garmin_name_differs(self):
        strava_activity = make_strava_activity(name='Morning Run')
        garmin_activity = {'activityName': 'Trail Adventure', 'description': 'Fun!'}
        needs_update, new_name, new_desc = self.sync.should_update_activity(
            strava_activity, garmin_activity)
        self.assertTrue(needs_update)
        self.assertEqual(new_name, 'Trail Adventure')
        self.assertEqual(new_desc, 'Fun!')

    def test_generic_garmin_name_does_not_overwrite_custom_name(self):
        strava_activity = make_strava_activity(name='Sortie club du samedi')
        garmin_activity = {'activityName': 'Running', 'description': ''}
        needs_update, _, _ = self.sync.should_update_activity(
            strava_activity, garmin_activity)
        self.assertFalse(needs_update)

    def test_workout_name_takes_priority(self):
        strava_activity = make_strava_activity(name='Morning Run')
        garmin_activity = {
            'activityName': 'Bruxelles Course à pied',
            'description': '',
            'workout': {'workoutName': 'Seuil 3x10min', 'description': 'Zone 4'},
        }
        needs_update, new_name, new_desc = self.sync.should_update_activity(
            strava_activity, garmin_activity)
        self.assertTrue(needs_update)
        self.assertEqual(new_name, 'Seuil 3x10min')
        self.assertEqual(new_desc, 'Zone 4')

    def test_description_only_change_never_triggers_update(self):
        """A user must be able to edit the Strava description after a sync
        without the app overwriting it: a description diff alone is not a sync."""
        strava_activity = make_strava_activity(name='Morning Run')
        garmin_activity = {'activityName': 'Morning Run',
                           'description': 'Une description différente'}
        needs_update, _, _ = self.sync.should_update_activity(
            strava_activity, garmin_activity)
        self.assertFalse(needs_update)

    # --- find_matching_garmin_activity ---

    def test_match_within_time_tolerance(self):
        start = datetime(2026, 7, 18, 8, 0, 0)
        strava_activity = make_strava_activity(start_date=start)
        garmin_activities = {
            '10': {'parsed_start_time': start + timedelta(seconds=30),
                   'activityType': {'typeKey': 'running'},
                   'activityName': 'Run A'},
        }
        match = self.sync.find_matching_garmin_activity(strava_activity, garmin_activities)
        self.assertIsNotNone(match)
        self.assertEqual(match['activityName'], 'Run A')

    def test_no_match_outside_tolerance(self):
        start = datetime(2026, 7, 18, 8, 0, 0)
        strava_activity = make_strava_activity(start_date=start)
        garmin_activities = {
            '10': {'parsed_start_time': start + timedelta(minutes=5),
                   'activityType': {'typeKey': 'running'},
                   'activityName': 'Run A'},
        }
        self.assertIsNone(
            self.sync.find_matching_garmin_activity(strava_activity, garmin_activities))

    def test_type_mismatch_is_rejected(self):
        start = datetime(2026, 7, 18, 8, 0, 0)
        strava_activity = make_strava_activity(start_date=start, type='Run')
        garmin_activities = {
            '10': {'parsed_start_time': start,
                   'activityType': {'typeKey': 'cycling'},
                   'activityName': 'Ride A'},
        }
        self.assertIsNone(
            self.sync.find_matching_garmin_activity(strava_activity, garmin_activities))

    def test_closer_wrong_type_does_not_shadow_correct_match(self):
        """Regression test: a closer activity of the wrong type used to lower
        min_time_diff and prevent the correct, slightly farther match."""
        start = datetime(2026, 7, 18, 8, 0, 0)
        strava_activity = make_strava_activity(start_date=start, type='Run')
        garmin_activities = {
            '10': {'parsed_start_time': start + timedelta(seconds=5),
                   'activityType': {'typeKey': 'cycling'},
                   'activityName': 'Wrong type, closer'},
            '11': {'parsed_start_time': start + timedelta(seconds=40),
                   'activityType': {'typeKey': 'running'},
                   'activityName': 'Right type, farther'},
        }
        match = self.sync.find_matching_garmin_activity(strava_activity, garmin_activities)
        self.assertIsNotNone(match)
        self.assertEqual(match['activityName'], 'Right type, farther')

    def test_treadmill_running_matches_run(self):
        start = datetime(2026, 7, 18, 8, 0, 0)
        strava_activity = make_strava_activity(start_date=start, type='Run')
        garmin_activities = {
            '10': {'parsed_start_time': start,
                   'activityType': {'typeKey': 'treadmill_running'},
                   'activityName': 'Tapis'},
        }
        self.assertIsNotNone(
            self.sync.find_matching_garmin_activity(strava_activity, garmin_activities))

    # --- Synced cache ---

    def test_cache_roundtrip(self):
        cache = {'123': time.time(), '456': time.time()}
        self.sync._save_synced_cache(cache, sync_days=7)  # pylint: disable=protected-access
        loaded = self.sync._load_synced_cache()  # pylint: disable=protected-access
        self.assertEqual(set(loaded), {'123', '456'})

    def test_cache_loads_legacy_list_format(self):
        with open(self.sync.strava.cache_file, 'w', encoding='utf-8') as f:
            json.dump(['123', '456'], f)
        loaded = self.sync._load_synced_cache()  # pylint: disable=protected-access
        self.assertEqual(set(loaded), {'123', '456'})

    def test_cache_purges_old_entries(self):
        old = time.time() - 30 * 86400
        cache = {'old': old, 'recent': time.time()}
        self.sync._save_synced_cache(cache, sync_days=7)  # pylint: disable=protected-access
        loaded = self.sync._load_synced_cache()  # pylint: disable=protected-access
        self.assertEqual(set(loaded), {'recent'})

    # --- Update / dry run ---

    def test_update_strava_activity_dry_run(self):
        self.sync.general.dry_run = True
        self.assertTrue(self.sync.update_strava_activity('123', 'Test', 'Desc'))


class TestParseGarminStartTime(unittest.TestCase):
    """Tests for the Garmin start time parser."""

    def test_iso_format(self):
        self.assertEqual(_parse_garmin_start_time('2026-07-18T08:00:00'),
                         datetime(2026, 7, 18, 8, 0, 0))

    def test_iso_format_with_z(self):
        self.assertEqual(_parse_garmin_start_time('2026-07-18T08:00:00Z'),
                         datetime(2026, 7, 18, 8, 0, 0))

    def test_space_separated_format(self):
        self.assertEqual(_parse_garmin_start_time('2026-07-18 08:00:00'),
                         datetime(2026, 7, 18, 8, 0, 0))

    def test_invalid_returns_none(self):
        self.assertIsNone(_parse_garmin_start_time('not a date'))
        self.assertIsNone(_parse_garmin_start_time(''))


if __name__ == '__main__':
    unittest.main()
