"""Mini status page served over HTTP.

The sync writes its state to data/.status.json after each run; a small
threaded HTTP server renders it as HTML on '/' and exposes '/health' for
container healthchecks (503 when the last sync is too old).
"""
import html
import json
import logging
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

logger = logging.getLogger(__name__)

STATUS_FILE = "data/.status.json"
DEFAULT_MAX_AGE_SECONDS = 3 * 3600


def write_status(updates: dict) -> None:
    """Merge updates into the persisted status file."""
    status = read_status()
    status.update(updates)
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(status, f, indent=2, ensure_ascii=False)
    except Exception as e:  # pylint: disable=broad-except
        logger.warning("Impossible d'écrire le fichier de statut: %s", e)


def read_status() -> dict:
    """Read the persisted status file, empty dict if absent/invalid."""
    try:
        if os.path.exists(STATUS_FILE):
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:  # pylint: disable=broad-except
        logger.warning("Impossible de lire le fichier de statut: %s", e)
    return {}


def last_sync_age_seconds(status: dict) -> Optional[float]:
    """Seconds since the last sync, None if never synced."""
    last_sync = status.get("last_sync")
    if not last_sync:
        return None
    try:
        then = datetime.fromisoformat(last_sync)
        return (datetime.now(timezone.utc) - then).total_seconds()
    except ValueError:
        return None


def is_healthy(status: dict, max_age_seconds: int) -> bool:
    """Healthy when a sync completed within max_age_seconds."""
    age = last_sync_age_seconds(status)
    return age is not None and age <= max_age_seconds


def _fmt_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "jamais"
    if seconds < 90:
        return "il y a moins de 2 min"
    if seconds < 3600:
        return f"il y a {int(seconds // 60)} min"
    return f"il y a {seconds / 3600:.1f} h"


_PAGE_HEAD = (
    "<!doctype html><html lang='fr'><head><meta charset='utf-8'>"
    "<meta http-equiv='refresh' content='60'>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    "<title>Strava-Garmin Sync</title>"
    "<style>body{font-family:-apple-system,sans-serif;max-width:640px;"
    "margin:2rem auto;padding:0 1rem;line-height:1.5}h1{font-size:1.3rem}"
    "h2{font-size:1rem;margin-top:1.5rem;border-bottom:1px solid #ddd}"
    "table{border-collapse:collapse}td{padding:.15rem .8rem .15rem 0}"
    ".ok{color:#2a7}.warn{color:#c60}</style></head><body>"
    "<h1>🏃 Synchronisateur Strava-Garmin</h1>"
)


def _results_section(status: dict) -> list:
    e = html.escape
    results = status.get("results") or {}
    if not results:
        return []
    parts = ["<h2>Dernier passage</h2><table>"]
    for label, key in (("Mises à jour", "updates"), ("Ignorées", "skipped"),
                       ("Ignorées (cache)", "cached_ignored"), ("Erreurs", "errors")):
        parts.append(f"<tr><td>{label}</td><td>{e(str(results.get(key, 0)))}</td></tr>")
    parts.append("</table>")
    updated = status.get("updated_activities") or []
    if updated:
        parts.append("<h2>Activités mises à jour</h2><ul>")
        parts.extend(f"<li>{e(name)}</li>" for name in updated)
        parts.append("</ul>")
    return parts


def _garmin_sections(status: dict) -> list:
    e = html.escape
    parts = []
    readiness = status.get("readiness") or {}
    if readiness:
        parts.append("<h2>Récupération</h2><table>")
        if readiness.get("recovery_minutes") is not None:
            hours = round(readiness["recovery_minutes"] / 60)
            parts.append(f"<tr><td>Temps de récupération</td><td>{hours} h</td></tr>")
        if readiness.get("score") is not None:
            level = f" ({e(str(readiness.get('level', '')))})" if readiness.get("level") else ""
            parts.append("<tr><td>Training readiness</td>"
                         f"<td>{e(str(readiness['score']))}{level}</td></tr>")
        parts.append("</table>")
    upcoming = status.get("upcoming_workouts") or []
    if upcoming:
        parts.append("<h2>Séances à venir</h2><table>")
        for workout in upcoming:
            parts.append(f"<tr><td>{e(str(workout.get('date', '')))}</td>"
                         f"<td>{e(str(workout.get('title', '')))}</td></tr>")
        parts.append("</table>")
    gear = status.get("gear") or []
    if gear:
        parts.append("<h2>Matériel</h2><table>")
        for item in gear:
            km = item.get("distance_km")
            if km is not None:
                parts.append(f"<tr><td>{e(str(item.get('name', '')))}</td>"
                             f"<td>{km:.0f} km</td></tr>")
        parts.append("</table>")
    return parts


def render_html(status: dict) -> str:
    """Render the status page (self-contained HTML, auto-refresh)."""
    age = last_sync_age_seconds(status)
    state_class = "ok" if age is not None and age <= DEFAULT_MAX_AGE_SECONDS else "warn"
    dry = " <span class='warn'>(DRY RUN)</span>" if status.get("dry_run") else ""
    parts = [
        _PAGE_HEAD,
        f"<p class='{state_class}'>Dernière synchronisation : "
        f"{html.escape(_fmt_age(age))}{dry}</p>",
        *_results_section(status),
        *_garmin_sections(status),
        "</body></html>",
    ]
    return "".join(parts)


class _StatusHandler(BaseHTTPRequestHandler):
    """Serves the status page and the healthcheck endpoint."""

    max_age_seconds = DEFAULT_MAX_AGE_SECONDS

    def do_GET(self):  # pylint: disable=invalid-name
        """Handle GET requests for '/' and '/health'."""
        status = read_status()
        if self.path == "/health":
            healthy = is_healthy(status, self.max_age_seconds)
            body = b"ok" if healthy else b"stale"
            self.send_response(200 if healthy else 503)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)
            return
        body = render_html(status).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # pylint: disable=redefined-builtin
        logger.debug("status server: " + format, *args)


def start_status_server(port: int) -> Optional[ThreadingHTTPServer]:
    """Start the status HTTP server in a daemon thread (None if disabled)."""
    if not port:
        return None
    _StatusHandler.max_age_seconds = int(
        os.getenv("STATUS_MAX_AGE_SECONDS", str(DEFAULT_MAX_AGE_SECONDS)))
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), _StatusHandler)
    except OSError as e:
        logger.error("Impossible de démarrer la page de statut sur le port %s: %s", port, e)
        return None
    thread = threading.Thread(target=server.serve_forever,
                              name="status-server", daemon=True)
    thread.start()
    logger.info("📟 Page de statut disponible sur le port %s", port)
    return server


def utcnow_iso() -> str:
    """Current UTC time in ISO format (for last_sync)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
