#!/usr/bin/env python3
"""
Application de synchronisation Strava-Garmin
Synchronise les noms et descriptions des activités Strava avec les données Garmin Connect
"""

import os
import time
import logging
from datetime import datetime
from typing import Optional, List, Dict
from zoneinfo import ZoneInfo
import json
import schedule
from stravalib import Client as StravaClient
from garth.exc import GarthHTTPError

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError
)

from .models import (
    ActivityData,
    GeneralConfig,
    StravaConfig,
    GarminConfig,
    ActivityCache,
    SyncState,
    Clients,
)
from .constants import GARMIN_TO_STRAVA_TYPE
from .garmin_service import get_garmin_activities_for_period
from .strava_service import (
    get_recent_strava_activities as fetch_recent_strava,
    update_strava_activity as apply_strava_update,
    wait_until_processed,
)

logger = logging.getLogger(__name__)


def setup_logging():
    """Configure logging to file and console. Call once at application startup."""
    os.makedirs('logs', exist_ok=True)
    os.makedirs('data', exist_ok=True)
    logging.basicConfig(
        level=os.getenv('LOG_LEVEL', 'INFO').upper(),
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('logs/strava_garmin_sync.log'),
            logging.StreamHandler()
        ]
    )


class StravaGarminSync:
    """Classe principale pour la synchronisation Strava-Garmin"""
    def __init__(self):
        # General
        self.general = GeneralConfig(
            dry_run=os.getenv('DRY_RUN', 'false').lower() in ['1', 'true', 'yes'],
            sync_tz=os.getenv('SYNC_TZ', 'Europe/Brussels'),
            quiet_hours_start=int(os.getenv('QUIET_HOURS_START', '0')),
            quiet_hours_end=int(os.getenv('QUIET_HOURS_END', '6')),
        )

        # Strava
        self.strava = StravaConfig(
            client_id=os.getenv('STRAVA_CLIENT_ID'),
            client_secret=os.getenv('STRAVA_CLIENT_SECRET'),
            access_token=os.getenv('STRAVA_ACCESS_TOKEN'),
            refresh_token=os.getenv('STRAVA_REFRESH_TOKEN'),
            token_expires_at=int(os.getenv('STRAVA_TOKEN_EXPIRES_AT', '0')),
            token_path="data/.strava_token.json",
            cache_file="data/.strava_synced_cache.json",
        )

        # Garmin
        self.garmin = GarminConfig(
            email=os.getenv('GARMIN_EMAIL'),
            password=os.getenv('GARMIN_PASSWORD'),
            tokenstore=os.getenv("GARMIN_TOKENS_FILE_LOC") or "data/.garminconnect",
        )

        # Clients
        self.clients = Clients()

        # Cache
        self.cache = ActivityCache(data={}, duration=3600)

        # State
        self.state = SyncState()

        # Charger le token Strava depuis un fichier si disponible
        self.load_strava_token()

        self.validate_config()

    def load_strava_token(self):
        """Charge le token Strava depuis un fichier"""
        if os.path.exists(self.strava.token_path):
            try:
                with open(self.strava.token_path, "r", encoding="utf-8") as f:
                    token_data = json.load(f)
                    self.strava.access_token = token_data.get("access_token",
                                                               self.strava.access_token)
                    self.strava.refresh_token = token_data.get("refresh_token",
                                                               self.strava.refresh_token)
                    self.strava.token_expires_at = token_data.get("expires_at",
                                                                  self.strava.token_expires_at)
                logger.info("Token Strava chargé depuis %s", self.strava.token_path)
            except (OSError, json.JSONDecodeError) as err:
                logger.warning("Impossible de lire %s: %s",self.strava.token_path,err)

    def validate_config(self):
        """Valide la configuration (env ou fichier de tokens)"""
        missing = []
        if not self.strava.client_id:
            missing.append('STRAVA_CLIENT_ID')
        if not self.strava.client_secret:
            missing.append('STRAVA_CLIENT_SECRET')
        # Les tokens peuvent venir de l'env ou de data/.strava_token.json
        if not self.strava.access_token:
            missing.append(f'STRAVA_ACCESS_TOKEN (env ou {self.strava.token_path})')
        if not self.strava.refresh_token:
            missing.append(f'STRAVA_REFRESH_TOKEN (env ou {self.strava.token_path})')
        if not self.garmin.email:
            missing.append('GARMIN_EMAIL')
        if not self.garmin.password:
            missing.append('GARMIN_PASSWORD')

        if missing:
            raise ValueError(f"Configuration manquante: {', '.join(missing)}")

        logger.info("Configuration validée")

    def init_strava_client(self) -> bool:
        """Initialise le client Strava"""
        try:
            # Vérifier si le token est valide
            if self.is_token_expired():
                logger.info("Token Strava expiré, rafraîchissement...")
                self.refresh_strava_token()

            now = time.time()
            if now - getattr(self.state, "last_strava_connection_check", 0) > 900:
                self.clients.strava = StravaClient(
                                                    access_token=self.strava.access_token,
                                                    refresh_token=self.strava.refresh_token,
                                                    token_expires=self.strava.token_expires_at
                                                )

                self.state.athlete = self.clients.strava.get_athlete()
                logger.info("Connecté à Strava: %s %s",
                            self.state.athlete.firstname,
                            self.state.athlete.lastname)
                self.state.last_strava_connection_check = now
            else:
                logger.info("Connecté à Strava: %s %s",
                            self.state.athlete.firstname,
                            self.state.athlete.lastname)

            return True

        except Exception as e:  # pylint: disable=broad-except
            logger.error("Erreur initialisation Strava: %s", e)
            return False

    def _process_sync_activity(
        self,
        strava_activity: ActivityData,
        garmin_activities: Dict[str, Dict],
        synced_cache: Dict[str, float],
    ) -> tuple[bool, bool, bool]:
        """Process a single Strava activity sync. Returns (updated, skipped, error)."""
        try:
            garmin_activity = self.find_matching_garmin_activity(strava_activity, garmin_activities)
            if not garmin_activity:
                logger.info("⏭️ Aucune activité Garmin trouvée pour: '%s'", strava_activity.name)
                synced_cache[strava_activity.id] = time.time()
                return False, True, False

            needs_update, new_name, new_description = self.should_update_activity(
                strava_activity, garmin_activity
            )

            if not needs_update:
                logger.info("✅ '%s' déjà à jour", strava_activity.name)
                synced_cache[strava_activity.id] = time.time()
                return False, True, False

            # Wait until Strava has fully processed the activity before updating.
            # Indoor/manual activities never get a map polyline, so waiting for one
            # would block max_retries * delay for nothing.
            if strava_activity.has_polyline:
                logger.info("Activité %s déjà traitée par Strava (polyline présente)",
                            strava_activity.id)
            elif strava_activity.trainer or strava_activity.manual:
                logger.info("Activité %s indoor/manuelle, pas d'attente de traitement",
                            strava_activity.id)
            else:
                wait_until_processed(self.clients.strava, int(strava_activity.id))

            if self.update_strava_activity(strava_activity.id, new_name, new_description):
                logger.info("🔄 '%s' → '%s'", strava_activity.name, new_name)
                synced_cache[strava_activity.id] = time.time()
                return True, False, False

            return False, False, True

        except Exception as e:  # pylint: disable=broad-except
            logger.error("❌ Erreur sync activité '%s': %s", strava_activity.name, e)
            return False, False, True

    def _load_synced_cache(self) -> Dict[str, float]:
        """Load synced cache mapping activity id -> sync timestamp.

        Supports the legacy format (plain list of ids) by assigning the current time.
        """
        try:
            if os.path.exists(self.strava.cache_file):
                with open(self.strava.cache_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, list):
                    now = time.time()
                    return {str(activity_id): now for activity_id in raw}
                return {str(k): float(v) for k, v in raw.items()}
        except Exception as e:  # pylint: disable=broad-except
            logger.warning("Impossible de lire le cache de synchronisation: %s", e)
        return {}

    def _save_synced_cache(self, synced_cache: Dict[str, float], sync_days: int) -> None:
        """Persist synced cache, dropping entries older than twice the sync window."""
        try:
            cutoff = time.time() - 2 * sync_days * 86400
            pruned = {k: v for k, v in synced_cache.items() if v >= cutoff}
            with open(self.strava.cache_file, "w", encoding="utf-8") as f:
                json.dump(pruned, f, indent=2)
            logger.info("🗂️ Cache de synchronisation mis à jour (%s entrées)", len(pruned))
        except Exception as e:  # pylint: disable=broad-except
            logger.warning("Impossible de sauvegarder le cache de synchronisation: %s", e)

    def init_garmin_client(self) -> bool:
        """Initialise le client Garmin"""
        try:
            # Using Oauth1 and OAuth2 token files from directory
            logger.info(
                "Trying to login to Garmin Connect using token data from directory '%s'...", 
                self.garmin.tokenstore
            )

            self.clients.garmin = Garmin()
            self.clients.garmin.login(self.garmin.tokenstore)

            # Test de connexion
            display_name = self.clients.garmin.get_full_name()
            logger.info("Connecté à Garmin: %s", display_name)
            return True

        except (FileNotFoundError,
                GarthHTTPError,
                GarminConnectAuthenticationError,
                json.decoder.JSONDecodeError):
            logger.warning(
                "Token Garmin non trouvé ou invalide, connexion avec email/mot de passe..."
            )
            try:
                self.clients.garmin = Garmin(self.garmin.email, self.garmin.password)
                self.clients.garmin.login()

                display_name = self.clients.garmin.get_full_name()
                logger.info("Connecté à Garmin: %s", display_name)

                # Save Oauth1 and Oauth2 token files to directory for next login
                self.clients.garmin.garth.dump(self.garmin.tokenstore)
                logger.info(
                    "Oauth tokens stored in '%s' directory for future use", self.garmin.tokenstore
                )

                # Re-login Garmin API with tokens
                self.clients.garmin.login(self.garmin.tokenstore)
                return True
            except Exception as e: # pylint: disable=broad-except
                logger.exception("Erreur initialisation Garmin: %s", e)
                logger.error("Vérifiez vos identifiants Garmin Connect")
                return False

        except Exception as e: # pylint: disable=broad-except
            logger.exception("Erreur initialisation Garmin: %s", e)
            logger.error("Vérifiez vos identifiants Garmin Connect")
            return False

    def is_token_expired(self) -> bool:
        """Vérifie si le token Strava est expiré (basé sur l'expiration locale)"""
        return time.time() >= self.strava.token_expires_at

    def refresh_strava_token(self):
        """Rafraîchit le token Strava"""
        try:
            # Utiliser la méthode de la librairie stravalib pour rafraîchir le token
            client = StravaClient()
            token_response = client.refresh_access_token(
                client_id=self.strava.client_id,
                client_secret=self.strava.client_secret,
                refresh_token=self.strava.refresh_token,
            )
            self.strava.access_token = token_response["access_token"]
            self.strava.refresh_token = token_response["refresh_token"]
            self.strava.token_expires_at = token_response["expires_at"]

            if self.clients.strava:
                self.clients.strava.access_token = self.strava.access_token
                self.clients.strava.refresh_token = self.strava.refresh_token
                self.clients.strava.token_expires = self.strava.token_expires_at

            # Sauvegarder le refresh token dans un fichier pour persistance
            try:
                with open(self.strava.token_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "access_token": self.strava.access_token,
                        "refresh_token": self.strava.refresh_token,
                        "expires_at": self.strava.token_expires_at
                    }, f, indent=2)
                logger.info("Token Strava sauvegardé dans %s", self.strava.token_path)
            except Exception as file_err: # pylint: disable=broad-except
                logger.warning("Impossible de sauvegarder le token Strava: %s",file_err)

            logger.info("Token Strava rafraîchi avec succès")

        except Exception as e: # pylint: disable=broad-except
            logger.error("Erreur rafraîchissement token: %s", e)
            raise

    def get_recent_strava_activities(self, days: int = 7) -> List[ActivityData]:
        """Récupère les activités Strava récentes"""
        try:
            return fetch_recent_strava(self.clients.strava, days)
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Erreur récupération activités Strava: %s", e, exc_info=True)
            return []

    def get_garmin_activities_for_period(self, days: int = 7) -> Dict[str, Dict]:
        """Récupère toutes les activités Garmin pour une période donnée"""
        try:
            return get_garmin_activities_for_period(self.clients.garmin, self.cache, days)
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Erreur récupération activités Garmin: %s", e)
            return {}

    # Mapping Garmin typeKey to Strava type now imported from constants

    def find_matching_garmin_activity(
        self,
        strava_activity: ActivityData,
        garmin_activities: Dict
    ) -> Optional[Dict]:
        """Trouve l'activité Garmin correspondant à une activité Strava"""
        best_match = None
        min_time_diff = float('inf')

        # Use .value if available, else fallback to str()
        strava_type = getattr(strava_activity.type, 'value', None)
        if not strava_type:
            strava_type = str(strava_activity.type)
        strava_type = strava_type.lower()

        for garmin_activity in garmin_activities.values():
            if 'parsed_start_time' not in garmin_activity:
                continue

            # --- Activity type check with mapping (before time comparison, so a
            # closer activity of the wrong type cannot shadow the right one) ---
            garmin_type = garmin_activity.get('activityType', {}).get('typeKey', '').lower()
            mapped_strava_type = GARMIN_TO_STRAVA_TYPE.get(garmin_type, garmin_type)
            if mapped_strava_type and strava_type and mapped_strava_type != strava_type:
                logger.debug(
                    "Type non correspondant: Garmin '%s' (Strava attend '%s', "
                    "mapping Garmin→Strava: '%s')",
                    garmin_type,
                    strava_type,
                    mapped_strava_type
                )
                continue
            # ---------------------------

            garmin_start = garmin_activity['parsed_start_time']

            # Calculer la différence de temps
            time_diff = abs((garmin_start - strava_activity.start_date).total_seconds())

            # Tolérance de 1 minute (60 secondes)
            if time_diff < 60 and time_diff < min_time_diff:
                min_time_diff = time_diff
                best_match = garmin_activity

        if best_match:
            logger.info(
                "Match trouvé pour '%s' avec différence de %.1f min",
                strava_activity.name,
                min_time_diff/60
            )

        return best_match

    def should_update_activity(
        self,
        strava_activity: ActivityData,
        garmin_activity: Dict
    ) -> tuple[bool, str, str]:
        """Détermine si une activité doit être mise à jour"""
        garmin_name = garmin_activity.get('activityName', '').strip()
        garmin_description = garmin_activity.get('description', '').strip()

        # Prioriser le nom et la description du workout si disponible
        workout = garmin_activity.get('workout')
        if workout:
            garmin_name = workout.get('workoutName', garmin_name).strip() or garmin_name
            garmin_description = (
                workout.get('description', garmin_description).strip() or
                garmin_description
            )

        # Nettoyer les noms Strava par défaut génériques
        strava_name = strava_activity.name.strip()

        needs_update = False
        new_name = strava_name
        new_description = None

        # Mettre à jour le nom si Garmin a un nom différent et non vide
        if garmin_name and garmin_name != strava_name:
            # Éviter les noms génériques comme "Running", "Cycling"
            generic_names = ['Running', 'Cycling', 'Walking', 'Swimming', 'Workout']
            if garmin_name not in generic_names or strava_name in generic_names:
                new_name = garmin_name
                needs_update = True

        # La description Garmin n'est appliquée que lorsqu'une mise à jour du nom est
        # nécessaire. Une différence de description seule ne déclenche jamais de sync :
        # l'utilisateur doit pouvoir modifier sa description sur Strava après coup
        # sans qu'elle soit écrasée.
        if garmin_description:
            new_description = garmin_description

        return needs_update, new_name, new_description

    def update_strava_activity(self, activity_id: str, name: str, description: str) -> bool:
        """Met à jour une activité Strava"""
        return apply_strava_update(
            self.clients.strava,
            self.general.dry_run,
            activity_id,
            name,
            description,
        )
    def sync_activities(self):
        """Synchronise les activités entre Strava et Garmin"""
        # --- Fenêtre horaire (heures creuses configurables) ---
        local_hour = datetime.now(ZoneInfo(self.general.sync_tz)).hour
        if self.general.quiet_hours_start <= local_hour < self.general.quiet_hours_end:
            logger.info(
                "⏳ Fenêtre de synchronisation fermée (%sh-%sh %s). Sync ignorée.",
                self.general.quiet_hours_start,
                self.general.quiet_hours_end,
                self.general.sync_tz,
            )
            return True
        # --- Fin fenêtre horaire ---

        logger.info("🔄 Début de la synchronisation...")
        if self.general.dry_run:
            logger.info("⚠️ Mode DRY RUN activé : aucune modification ne sera faite sur Strava.")

        start_time = time.time()

        if not self.init_strava_client():
            logger.error("❌ Impossible d'initialiser le client Strava")
            return False

        # Récupérer les activités des deux plateformes
        sync_days = int(os.getenv('SYNC_DAYS', '7'))
        logger.info("📅 Synchronisation des activités des %s derniers jours", sync_days)

        strava_activities = self.get_recent_strava_activities(days=sync_days)
        if not strava_activities:
            logger.warning("⚠️ Aucune activité Strava trouvée")
            strava_activities = []

        synced_cache = self._load_synced_cache()

        # Filtrer les activités déjà synchronisées
        strava_activities_to_update = [a for a in strava_activities if a.id not in synced_cache]
        cached_ignored = max(0, len(strava_activities) - len(strava_activities_to_update))
        if not strava_activities_to_update:
            logger.info(
                "✅ Toutes les activités Strava récentes sont déjà synchronisées (cache)"
            )
        # ---------------------------------------------------------------

        if strava_activities_to_update and not self.init_garmin_client():
            logger.error("❌ Impossible d'initialiser le client Garmin")
            return False

        garmin_activities = {}
        if strava_activities_to_update:
            garmin_activities = self.get_garmin_activities_for_period(days=sync_days)
            if not garmin_activities:
                logger.warning("⚠️ Aucune activité Garmin trouvée")
                garmin_activities = {}

        # Synchroniser les activités
        counts = {"updates": 0, "skipped": 0, "errors": 0}

        for strava_activity in strava_activities_to_update:
            updated, skipped, error = self._process_sync_activity(
                strava_activity,
                garmin_activities,
                synced_cache,
            )
            counts["updates"] += int(updated)
            counts["skipped"] += int(skipped)
            counts["errors"] += int(error)

        # Sauvegarder le cache mis à jour après la boucle
        self._save_synced_cache(synced_cache, sync_days)

        # Résumé
        logger.info("=" * 50)
        logger.info("✅ Synchronisation terminée en %.1fs", time.time() - start_time)
        logger.info("📊 Résultats:")
        logger.info("   • Mises à jour: %s", counts["updates"])
        logger.info("   • Ignorées: %s", counts["skipped"])
        logger.info("   • Ignorées (cache): %s", cached_ignored)
        logger.info("   • Erreurs: %s", counts["errors"])
        logger.info("   • Total traité: %s", len(strava_activities_to_update))
        logger.info("=" * 50)

        return True

    def run_scheduler(self, interval_minutes: int = 60):
        """Lance le planificateur de synchronisation"""
        logger.info("⏰ Démarrage du planificateur (intervalle: %s min)", interval_minutes)

        # Planifier la synchronisation
        schedule.every(interval_minutes).minutes.do(self.sync_activities)

        # Synchronisation initiale
        logger.info("🚀 Synchronisation initiale...")
        self.sync_activities()

        # Boucle principale
        logger.info("🔄 Planificateur actif - Ctrl+C pour arrêter")
        while True:
            try:
                schedule.run_pending()
                time.sleep(60)  # Vérifier chaque minute

            except KeyboardInterrupt:
                logger.info("🛑 Arrêt du planificateur demandé")
                break
            except Exception as e: # pylint: disable=broad-except
                logger.error("❌ Erreur planificateur: %s", e, exc_info=True)
                logger.info("⏳ Attente de 5 minutes avant de reprendre...")
                time.sleep(300)

def main():
    """Point d'entrée principal"""
    setup_logging()
    try:
        # Banner
        logger.info("=" * 60)
        logger.info("🏃 SYNCHRONISATEUR STRAVA-GARMIN")
        logger.info("=" * 60)

        sync_app = StravaGarminSync()

        # Configuration
        interval = int(os.getenv('SYNC_INTERVAL_MINUTES', '60'))
        mode = os.getenv('RUN_MODE', 'scheduler')

        if mode == 'once':
            logger.info("🔄 Mode: exécution unique")
            sync_app.sync_activities()
        else:
            logger.info("🔄 Mode: planificateur continu")
            sync_app.run_scheduler(interval)

    except KeyboardInterrupt:
        logger.info("👋 Arrêt demandé par l'utilisateur")
    except Exception as e:
        logger.error("💥 Erreur fatale: %s", e)
        raise

if __name__ == "__main__":
    main()
