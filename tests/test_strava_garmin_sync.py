"""Unit tests for the Strava-Garmin sync application."""
# pylint: disable=missing-function-docstring  # test names are self-describing
import json
import os
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from strava_garmin_sync_app import StravaGarminSync
from strava_garmin_sync_app.activity_names import is_auto_generated_name
from strava_garmin_sync_app.strava_garmin_sync import should_give_up_on_match
from strava_garmin_sync_app.backfill import (
    compute_backfill_update,
    parse_args as parse_backfill_args,
)
from strava_garmin_sync_app.models import ActivityData
from strava_garmin_sync_app.garmin_service import (
    _parse_garmin_start_time,
    get_garmin_activities_between,
    process_garmin_activity,
)
from strava_garmin_sync_app.status_server import is_healthy, render_html
from strava_garmin_sync_app.workout_formatter import (
    build_execution_report,
    build_metrics_line,
    build_workout_description,
    flatten_steps,
    fmt_duration,
    fmt_pace,
    is_meaningful_description,
)

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

    def test_is_token_expired_within_safety_margin(self):
        # expires in 100 s: considered expired (5 min margin) to avoid
        # expiry in the middle of a sync
        self.sync.strava.token_expires_at = int(time.time()) + 100
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
            'workout': {'workoutName': 'Seuil 3x10min', 'description': 'Zone 4 !'},
        }
        needs_update, new_name, new_desc = self.sync.should_update_activity(
            strava_activity, garmin_activity)
        self.assertTrue(needs_update)
        self.assertEqual(new_name, 'Seuil 3x10min')
        self.assertEqual(new_desc, 'Zone 4 !')

    def test_slug_workout_description_replaced_by_steps_text(self):
        strava_activity = make_strava_activity(name='Morning Run')
        garmin_activity = {
            'activityName': 'Bruxelles Course à pied',
            'description': '',
            'workout': {
                'workoutName': "7x3' Intervals Run",
                'description': 'vo2max',
                'workoutSegments': [{'workoutSteps': [
                    {'type': 'ExecutableStepDTO',
                     'stepType': {'stepTypeKey': 'warmup'},
                     'endCondition': {'conditionTypeKey': 'time'},
                     'endConditionValue': 900},
                ]}],
            },
        }
        needs_update, new_name, new_desc = self.sync.should_update_activity(
            strava_activity, garmin_activity)
        self.assertTrue(needs_update)
        self.assertEqual(new_name, "7x3' Intervals Run")
        self.assertEqual(new_desc,
                         "Séance : 7x3' Intervals Run (vo2max)\n- Échauffement : 15 min")

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


class TestWorkoutFormatter(unittest.TestCase):
    """Tests for the workout structure -> description text generation."""

    def test_fmt_duration(self):
        self.assertEqual(fmt_duration(45), '45 s')
        self.assertEqual(fmt_duration(900), '15 min')
        self.assertEqual(fmt_duration(1830), '30 min 30')
        self.assertEqual(fmt_duration(4800), '1 h 20 min')

    def test_fmt_pace(self):
        # 3.0 m/s = 5:33/km
        self.assertEqual(fmt_pace(3.0), '5:33/km')

    def test_is_meaningful_description(self):
        self.assertFalse(is_meaningful_description(''))
        self.assertFalse(is_meaningful_description('progressive_run'))
        self.assertFalse(is_meaningful_description('vo2max'))
        self.assertTrue(is_meaningful_description('Séance seuil, rester en zone 4'))

    def test_meaningful_description_is_copied(self):
        workout = {'workoutName': 'Seuil', 'description': 'Bien rester en Z4 !',
                   'workoutSegments': []}
        self.assertEqual(build_workout_description(workout), 'Bien rester en Z4 !')

    def test_repeat_group_rendering(self):
        interval = {'type': 'ExecutableStepDTO',
                    'stepType': {'stepTypeKey': 'interval'},
                    'endCondition': {'conditionTypeKey': 'time'},
                    'endConditionValue': 240,
                    'targetType': {'workoutTargetTypeKey': 'pace.zone'},
                    # m/s: 3.7 = 4:30/km, 3.5 = 4:46/km
                    'targetValueOne': 3.5, 'targetValueTwo': 3.7}
        recovery = {'type': 'ExecutableStepDTO',
                    'stepType': {'stepTypeKey': 'recovery'},
                    'endCondition': {'conditionTypeKey': 'time'},
                    'endConditionValue': 60}
        workout = {
            'workoutName': "4x4' Tempo",
            'description': 'threshold',
            'workoutSegments': [{'workoutSteps': [
                {'type': 'RepeatGroupDTO', 'numberOfIterations': 4,
                 'workoutSteps': [interval, recovery]},
            ]}],
        }
        text = build_workout_description(workout)
        self.assertIn("Séance : 4x4' Tempo (threshold)", text)
        self.assertIn('4 × (Effort : 4 min (allure 4:30/km à 4:46/km) '
                      '+ Récupération : 1 min)', text)

    def test_empty_workout_returns_none(self):
        self.assertIsNone(build_workout_description(
            {'workoutName': 'X', 'description': '', 'workoutSegments': []}))


def make_structured_workout():
    """Warmup + 2x(interval+recovery) + cooldown, with pace targets in m/s."""
    def step(key, seconds, low=None, high=None):
        s = {'type': 'ExecutableStepDTO',
             'stepType': {'stepTypeKey': key},
             'endCondition': {'conditionTypeKey': 'time'},
             'endConditionValue': seconds}
        if low and high:
            s['targetType'] = {'workoutTargetTypeKey': 'pace.zone'}
            s['targetValueOne'], s['targetValueTwo'] = low, high
        return s

    return {
        'workoutName': 'Test 2x4',
        'description': 'threshold',
        'workoutSegments': [{'workoutSteps': [
            step('warmup', 900, 2.6, 2.9),        # ~5:45-6:25/km
            {'type': 'RepeatGroupDTO', 'numberOfIterations': 2,
             'workoutSteps': [
                 step('interval', 240, 3.4, 3.7),  # ~4:30-4:54/km
                 step('recovery', 60, 2.5, 2.7),
             ]},
            step('cooldown', 600, 2.5, 2.7),
        ]}],
    }


def lap(speed):
    return SimpleNamespace(average_speed=speed)


class TestExecutionReport(unittest.TestCase):
    """Tests for the planned-vs-executed comparison."""

    def test_flatten_expands_repeats(self):
        steps = flatten_steps(make_structured_workout())
        self.assertEqual([s['key'] for s in steps],
                         ['warmup', 'interval', 'recovery', 'interval',
                          'recovery', 'cooldown'])
        # both intervals belong to the same repeat group
        self.assertEqual(steps[1]['group'], steps[3]['group'])
        self.assertIsNone(steps[0]['group'])

    def test_report_on_target(self):
        # warmup 6:00, 2 intervals 4:40, recoveries slow, cooldown 6:20
        laps = [lap(2.78), lap(3.57), lap(2.2), lap(3.55), lap(2.3), lap(2.63)]
        lines = build_execution_report(make_structured_workout(), laps)
        self.assertEqual(lines[0], 'Réalisé :')
        self.assertIn('Échauffement : 6:00/km ✅', lines[1])
        self.assertIn('2 × Effort : 4:40 ✅ · 4:42 ✅', lines[2])
        # recoveries listed without verdict (slow recovery is fine)
        self.assertIn('2 × Récupération', lines[3])
        self.assertNotIn('⚠️', lines[3])
        self.assertIn('Retour au calme', lines[4])

    def test_report_flags_missed_target(self):
        # second interval at 5:15/km, way slower than the 4:30-4:54 target
        laps = [lap(2.78), lap(3.57), lap(2.2), lap(3.17), lap(2.3), lap(2.63)]
        lines = build_execution_report(make_structured_workout(), laps)
        self.assertIn('⚠️', lines[2])

    def test_report_none_when_lap_count_mismatch(self):
        self.assertIsNone(
            build_execution_report(make_structured_workout(), [lap(2.8), lap(3.5)]))

    def test_metrics_line(self):
        activity = {'aerobicTrainingEffect': 3.6, 'anaerobicTrainingEffect': 1.7,
                    'activityTrainingLoad': 148.5, 'vO2MaxValue': 53.0}
        line = build_metrics_line(activity, recovery_minutes=1513)
        self.assertEqual(
            line, '📊 TE 3.6 aérobie / 1.7 anaérobie · Charge 148 · VO2max 53 · Récup 25 h')

    def test_metrics_line_empty_activity(self):
        self.assertIsNone(build_metrics_line({}))


class TestStatusServer(unittest.TestCase):
    """Tests for the status page helpers."""

    def test_health_from_last_sync_age(self):
        fresh = {'last_sync': datetime.now(timezone.utc).isoformat()}
        stale = {'last_sync': (datetime.now(timezone.utc)
                               - timedelta(hours=5)).isoformat()}
        self.assertTrue(is_healthy(fresh, 3 * 3600))
        self.assertFalse(is_healthy(stale, 3 * 3600))
        self.assertFalse(is_healthy({}, 3 * 3600))

    def test_render_html_contains_sections(self):
        status = {
            'last_sync': datetime.now(timezone.utc).isoformat(),
            'results': {'updates': 2, 'skipped': 1, 'cached_ignored': 3, 'errors': 0},
            'updated_activities': ["7x3' Intervals Run"],
            'readiness': {'score': 61, 'level': 'MODERATE', 'recovery_minutes': 1513},
            'upcoming_workouts': [{'date': '2026-07-21', 'title': '1h Progressive Run'}],
            'gear': [{'name': 'ASICS Gel pursue 10 bleu', 'distance_km': 410.3}],
        }
        page = render_html(status)
        self.assertIn("7x3&#x27; Intervals Run", page)
        self.assertIn('1h Progressive Run', page)
        self.assertIn('25 h', page)
        self.assertIn('410 km', page)


class TestBackfill(unittest.TestCase):
    """Tests for the backfill decision guards and CLI defaults."""

    def test_hand_written_description_is_never_touched(self):
        do_update, reason = compute_backfill_update(
            'Superbe sortie avec le club !', True, 'Séance : X\n- Effort : 10 min')
        self.assertFalse(do_update)
        self.assertIn('personnalisée', reason)

    def test_slug_description_gets_updated(self):
        do_update, reason = compute_backfill_update(
            'vo2max', False, 'Séance : X\n- Effort : 10 min')
        self.assertTrue(do_update)
        self.assertEqual(reason, 'description')

    def test_app_generated_description_stays_replaceable(self):
        # a previous backfill wrote the structure; a re-run may enrich it
        current = 'Séance : X (vo2max)\n- Effort : 10 min'
        new = current + '\n\nRéalisé :\n- Effort : 4:20/km ✅'
        do_update, reason = compute_backfill_update(current, False, new)
        self.assertTrue(do_update)
        self.assertEqual(reason, 'description')
        # identical output → nothing to do
        do_update, _ = compute_backfill_update(current, False, current)
        self.assertFalse(do_update)

    def test_empty_description_and_name_change(self):
        do_update, reason = compute_backfill_update(
            '', True, 'Séance : X\n- Effort : 10 min')
        self.assertTrue(do_update)
        self.assertEqual(reason, 'nom + description')

    def test_already_up_to_date(self):
        text = 'Séance : X\n- Effort : 10 min'
        do_update, _ = compute_backfill_update(text, False, text)
        # le texte généré contient espaces/accents → "meaningful", donc protégé
        self.assertFalse(do_update)

    def test_parse_args_defaults_to_last_seven_days(self):
        args = parse_backfill_args([])
        self.assertEqual(args.end, date.today())
        self.assertEqual(args.start, date.today() - timedelta(days=7))
        self.assertFalse(args.apply)

    def test_parse_args_explicit_range(self):
        args = parse_backfill_args(
            ['--start', '2026-06-01', '--end', '2026-06-30', '--apply'])
        self.assertEqual(args.start, date(2026, 6, 1))
        self.assertEqual(args.end, date(2026, 6, 30))
        self.assertTrue(args.apply)


class TestAutoGeneratedNames(unittest.TestCase):
    """Auto-name detection, in French and English (regression: FR was ignored)."""

    def test_english_auto_names(self):
        for name in ("Morning Run", "Afternoon Ride", "Lunch Ride", "Evening Walk",
                     "Running", "Cycling", "Workout"):
            self.assertTrue(is_auto_generated_name(name), name)

    def test_french_auto_names(self):
        for name in ("Course à pied le matin", "Vélo en soirée", "Marche le midi",
                     "Marche à pied", "Randonnée"):
            self.assertTrue(is_auto_generated_name(name), name)

    def test_place_plus_sport_is_auto(self):
        for name in ("Maisod Randonnée", "Paris Marche à pied",
                     "Clairvaux-les-Lacs Cyclisme", "Hauts de Bienne Randonnée"):
            self.assertTrue(is_auto_generated_name(name), name)

    def test_personalised_names_are_protected(self):
        for name in ("Maisod Randonnée - la sirène égarée",
                     "Sortie vélo à la cascade de la frasnée",
                     "Semi de Bruxelles"):
            self.assertFalse(is_auto_generated_name(name), name)

    def test_workout_names_are_not_auto(self):
        for name in ("7x3' Intervals Run", "1h20 Long Run", "3x6' Tempo Run",
                     "45' Easy Run", "30' Race Pace"):
            self.assertFalse(is_auto_generated_name(name), name)

    def test_empty_name_counts_as_auto(self):
        self.assertTrue(is_auto_generated_name(""))
        self.assertTrue(is_auto_generated_name("   "))


class TestNameOverwriteProtection(unittest.TestCase):
    """should_update_activity must never replace a personalised name with an
    auto-generated one — the bug that would have destroyed
    'Maisod Randonnée - la sirène égarée'."""

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

    def test_french_generic_does_not_overwrite_custom_name(self):
        activity = make_strava_activity(name='Maisod Randonnée - la sirène égarée')
        garmin = {'activityName': 'Maisod Randonnée', 'description': ''}
        needs_update, new_name, _ = self.sync.should_update_activity(activity, garmin)
        self.assertFalse(needs_update)
        self.assertEqual(new_name, 'Maisod Randonnée - la sirène égarée')

    def test_french_auto_strava_name_is_replaced_by_workout(self):
        activity = make_strava_activity(name='Course à pied le matin')
        garmin = {'activityName': 'Braine-l\'Alleud Course à pied', 'description': '',
                  'workout': {'workoutName': "3x6' Tempo Run", 'description': 'threshold',
                              'workoutSegments': []}}
        needs_update, new_name, _ = self.sync.should_update_activity(activity, garmin)
        self.assertTrue(needs_update)
        self.assertEqual(new_name, "3x6' Tempo Run")

    def test_workout_name_applies_even_when_it_looks_generic(self):
        # a plan may name a session plainly; coming from a workout it is meaningful
        activity = make_strava_activity(name='Sortie du dimanche')
        garmin = {'activityName': 'Paris Course à pied', 'description': '',
                  'workout': {'workoutName': 'Long Run', 'description': 'long_run',
                              'workoutSegments': []}}
        needs_update, new_name, _ = self.sync.should_update_activity(activity, garmin)
        self.assertTrue(needs_update)
        self.assertEqual(new_name, 'Long Run')


class TestGiveUpOnMatch(unittest.TestCase):
    """A missing Garmin match must not be cached as final too early
    (regression: activities were silently lost forever)."""

    @staticmethod
    def _activity(age_hours):
        start = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=age_hours)
        return make_strava_activity(start_date=start)

    def test_recent_activity_is_retried(self):
        self.assertFalse(should_give_up_on_match(self._activity(2), True, 48))

    def test_old_activity_is_given_up(self):
        self.assertTrue(should_give_up_on_match(self._activity(72), True, 48))

    def test_incomplete_garmin_fetch_never_gives_up(self):
        # even an old activity: the Garmin window was not fully fetched
        self.assertFalse(should_give_up_on_match(self._activity(500), False, 48))


class TestGarminFetchCompleteness(unittest.TestCase):
    """A failed day must be reported so callers don't trust a partial result."""

    class _Client:  # pylint: disable=too-few-public-methods
        """Client Garmin minimal qui échoue sur une journée donnée."""

        def __init__(self, failing_day=None):
            self.failing_day = failing_day

        def get_activities_by_date(self, start, _end):
            if start == self.failing_day:
                raise ConnectionError("Garmin indisponible")
            return []

    def _fetch(self, client):
        cache = SimpleNamespace(data={}, duration=3600)
        start = datetime(2026, 7, 1)
        with patch('strava_garmin_sync_app.garmin_service.time.sleep'):
            return get_garmin_activities_between(client, cache, start,
                                                 start + timedelta(days=2))

    def test_all_days_ok_is_complete(self):
        _, complete = self._fetch(self._Client())
        self.assertTrue(complete)

    def test_failed_day_marks_incomplete(self):
        _, complete = self._fetch(self._Client(failing_day='2026-07-02'))
        self.assertFalse(complete)


class TestProcessGarminActivity(unittest.TestCase):
    """Tests for Garmin activity normalization."""

    def test_parsed_start_time_prefers_gmt(self):
        activities = {}
        activity = {
            'activityId': 42,
            'startTimeGMT': '2026-07-17 06:33:55',
            'startTimeLocal': '2026-07-17 08:33:55',
        }
        process_garmin_activity(None, activities, activity)
        self.assertEqual(activities['42']['parsed_start_time'],
                         datetime(2026, 7, 17, 6, 33, 55))

    def test_falls_back_to_local_time_without_gmt(self):
        activities = {}
        activity = {'activityId': 42, 'startTimeLocal': '2026-07-17 08:33:55'}
        process_garmin_activity(None, activities, activity)
        self.assertEqual(activities['42']['parsed_start_time'],
                         datetime(2026, 7, 17, 8, 33, 55))


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
