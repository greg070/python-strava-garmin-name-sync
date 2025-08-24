"""Constants used across the sync application."""
# Mapping Garmin typeKey to Strava type
GARMIN_TO_STRAVA_TYPE = {
    "running": "run",
    "cycling": "ride",
    "swimming": "swim",
    "walking": "walk",
    "hiking": "hike",
    "multi_sport": "workout",
    "fitness_equipment": "workout",
    "other": "workout",
}
