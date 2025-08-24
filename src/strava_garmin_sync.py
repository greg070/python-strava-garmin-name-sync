#!/usr/bin/env python3
"""
Application de synchronisation Strava-Garmin
Synchronise les noms et descriptions des activités Strava avec les données Garmin Connect
"""

import os
import time
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict
import json
from dataclasses import dataclass
import schedule
import pytz
from stravalib import Client as StravaClient
from garth.exc import GarthHTTPError

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError
)

os.makedirs('logs', exist_ok=True)
os.makedirs('data', exist_ok=True)

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/strava_garmin_sync.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

@dataclass
class ActivityData:
    """Classe pour stocker les données d'activité"""
    id: str
    name: str
    start_date: datetime
    type: str
    garmin_id: Optional[str] = None

class StravaGarminSync:
    """Classe principale pour la synchronisation Strava-Garmin"""
    def __init__(self):
        # Configuration depuis les variables d'environnement
        self.strava_client_id = os.getenv('STRAVA_CLIENT_ID')
        self.strava_client_secret = os.getenv('STRAVA_CLIENT_SECRET')
        self.strava_access_token = os.getenv('STRAVA_ACCESS_TOKEN')
        self.strava_refresh_token = os.getenv('STRAVA_REFRESH_TOKEN')
        self.strava_token_expires_at = int(os.getenv('STRAVA_TOKEN_EXPIRES_AT', '0'))
        self.last_strava_connection_check = 0  # Timestamp of last connection test
        self.strava_athlete = None
        self.strava_cache_file = "data/.strava_synced_cache.json"

        # Charger le token Strava depuis un fichier si disponible
        self.strava_token_path = "data/.strava_token.json"
        self.load_strava_token()

        self.garmin_email = os.getenv('GARMIN_EMAIL')
        self.garmin_password = os.getenv('GARMIN_PASSWORD')
        self.garmin_tokenstore = os.getenv("GARMIN_TOKENS_FILE_LOC") or "data/.garminconnect"

        # dry run mode
        self.dry_run = os.getenv('DRY_RUN', 'false').lower() in ['1', 'true', 'yes']

        # Clients
        self.strava_client = None
        self.garmin_client = None

        # Rate limiting
        self.last_strava_request = 0
        self.strava_requests_count = 0
        self.strava_daily_limit = 1000  # Limite quotidienne Strava
        self.strava_rate_limit = 100    # Limite par 15 minutes

        # Cache des activités pour éviter les requêtes répétées
        self.activity_cache = {}
        self.cache_duration = 3600  # 1 heure

        self.validate_config()

    def load_strava_token(self):
        """Charge le token Strava depuis un fichier"""
        if os.path.exists(self.strava_token_path):
            try:
                with open(self.strava_token_path, "r", encoding="utf-8") as f:
                    token_data = json.load(f)
                    self.strava_access_token = token_data.get("access_token",
                                                               self.strava_access_token)
                    self.strava_refresh_token = token_data.get("refresh_token",
                                                               self.strava_refresh_token)
                    self.strava_token_expires_at = token_data.get("expires_at",
                                                                  self.strava_token_expires_at)
                logger.info("Token Strava chargé depuis %s", self.strava_token_path)
            except (OSError, json.JSONDecodeError) as err:
                logger.warning("Impossible de lire %s: %s",self.strava_token_path,err)

    def validate_config(self):
        """Valide la configuration"""
        required_vars = [
            'STRAVA_CLIENT_ID', 'STRAVA_CLIENT_SECRET', 
            'STRAVA_ACCESS_TOKEN', 'STRAVA_REFRESH_TOKEN',
            'GARMIN_EMAIL', 'GARMIN_PASSWORD'
        ]

        missing = [var for var in required_vars if not os.getenv(var)]
        if missing:
            raise ValueError(f"Variables d'environnement manquantes: {', '.join(missing)}")

        logger.info("Configuration validée")

    def init_strava_client(self) -> bool:
        """Initialise le client Strava"""
        try:
            # Vérifier si le token est valide
            if self.is_token_expired():
                logger.info("Token Strava expiré, rafraîchissement...")
                self.refresh_strava_token()

            now = time.time()
            if now - getattr(self, "last_strava_connection_check", 0) > 900:
                self.strava_client = StravaClient(
                                                    access_token=self.strava_access_token,
                                                    refresh_token=self.strava_refresh_token,
                                                    token_expires=self.strava_token_expires_at
                                                )

                self.strava_athlete = self.strava_client.get_athlete()
                logger.info("Connecté à Strava: %s %s",
                            self.strava_athlete.firstname, 
                            self.strava_athlete.lastname)
                self.last_strava_connection_check = now
            else:
                logger.info("Connecté à Strava: %s %s",
                            self.strava_athlete.firstname,
                            self.strava_athlete.lastname)

            return True

        except Exception as e: # pylint: disable=broad-except
            logger.error("Erreur initialisation Strava: %s", e)
            return False

    def init_garmin_client(self) -> bool:
        """Initialise le client Garmin"""
        try:
            # Using Oauth1 and OAuth2 token files from directory
            logger.info(
                "Trying to login to Garmin Connect using token data from directory '%s'...", 
                self.garmin_tokenstore
            )

            self.garmin_client = Garmin()
            self.garmin_client.login(self.garmin_tokenstore)

            # Test de connexion
            display_name = self.garmin_client.get_full_name()
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
                self.garmin_client = Garmin(self.garmin_email, self.garmin_password)
                self.garmin_client.login()

                display_name = self.garmin_client.get_full_name()
                logger.info("Connecté à Garmin: %s", display_name)

                # Save Oauth1 and Oauth2 token files to directory for next login
                self.garmin_client.garth.dump(self.garmin_tokenstore)
                logger.info(
                    "Oauth tokens stored in '%s' directory for future use", self.garmin_tokenstore
                )

                # Re-login Garmin API with tokens
                self.garmin_client.login(self.garmin_tokenstore)
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
        return time.time() >= self.strava_token_expires_at

    def refresh_strava_token(self):
        """Rafraîchit le token Strava"""
        try:
            # Utiliser la méthode de la librairie stravalib pour rafraîchir le token
            client = StravaClient()
            token_response = client.refresh_access_token(
                client_id=self.strava_client_id,
                client_secret=self.strava_client_secret,
                refresh_token=self.strava_refresh_token,
            )
            self.strava_access_token = token_response["access_token"]
            self.strava_refresh_token = token_response["refresh_token"]
            self.strava_token_expires_at = token_response["expires_at"]

            self.strava_client.access_token = self.strava_access_token
            self.strava_client.refresh_token = self.strava_refresh_token
            self.strava_client.token_expires = self.strava_token_expires_at

            print("Access Token:", token_response['access_token'])
            print("Refresh Token:", token_response['refresh_token'])
            print("Expires At:", token_response['expires_at'])

            # Sauvegarder le refresh token dans un fichier pour persistance
            try:
                with open(self.strava_token_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "access_token": self.strava_access_token,
                        "refresh_token": self.strava_refresh_token,
                        "expires_at": self.strava_token_expires_at
                    }, f, indent=2)
                logger.info("Token Strava sauvegardé dans strava_token.json")
            except Exception as file_err: # pylint: disable=broad-except
                logger.warning("Impossible de sauvegarder le token Strava: %s",file_err)

            logger.info("Token Strava rafraîchi avec succès")

        except Exception as e: # pylint: disable=broad-except
            logger.error("Erreur rafraîchissement token: %s", e)
            raise

    def get_recent_strava_activities(self, days: int = 7) -> List[ActivityData]:
        """Récupère les activités Strava récentes"""
        try:

            after_date = datetime.now() - timedelta(days=days)
            activities = self.strava_client.get_activities(after=after_date, limit=200)

            strava_activities = []
            for activity in activities:
                # Convertir la date en datetime naive (sans timezone)
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

                activity_data = ActivityData(
                    id=str(activity.id),
                    name=activity.name or "",
                    start_date=start_date,
                    type=activity.type.root or "Unknown"
                )
                strava_activities.append(activity_data)

            logger.info("Récupéré %s activités Strava", len(strava_activities))
            return strava_activities

        except Exception as e: # pylint: disable=broad-except
            logger.error("Erreur récupération activités Strava: %s", e, exc_info=True)
            return []

    def get_garmin_activities_for_period(self, days: int = 7) -> Dict[str, Dict]:
        """Récupère toutes les activités Garmin pour une période donnée"""
        try:
            cache_key = f"garmin_activities_{days}"
            current_time = time.time()

            # Vérifier le cache
            if (cache_key in self.activity_cache and 
                current_time - self.activity_cache[cache_key]['timestamp'] < self.cache_duration):
                logger.debug("Utilisation du cache pour les activités Garmin")
                return self.activity_cache[cache_key]['data']

            activities = {}
            start_date = datetime.now() - timedelta(days=days)

            # Récupérer les activités par chunks de jours pour éviter la surcharge
            current_date = start_date
            while current_date <= datetime.now():
                try:
                    date_str = current_date.strftime('%Y-%m-%d')
                    daily_activities = self.garmin_client.get_activities_by_date(date_str, date_str)

                    if daily_activities:
                        for activity in daily_activities:

                            logger.info(
                                "Garmin Activity: ID=%s, Name='%s', Start=%s, Type=%s",
                                activity.get("activityId"),
                                activity.get("activityName"),
                                activity.get("startTimeLocal"),
                                activity.get("activityType", {}).get("typeKey"),
                            )

                            logger.debug(
                                "Garmin Activity: %s",
                                json.dumps(activity, indent=2, ensure_ascii=False),
                            )


                            activity_id = str(activity.get('activityId', ''))
                            if activity_id:
                                # Normaliser la date de début
                                start_time_str = activity.get('startTimeLocal', '')
                                if start_time_str:
                                    # Garmin utilise parfois des formats différents
                                    try:
                                        if 'T' in start_time_str:
                                            start_time = datetime.fromisoformat(
                                                start_time_str.replace('Z', ''))
                                        else:
                                            start_time = datetime.strptime(
                                                start_time_str, 
                                                '%Y-%m-%d %H:%M:%S')
                                        activity['parsed_start_time'] = start_time
                                    except Exception as err:  # pylint: disable=broad-except
                                        logger.warning("Format de date non reconnu: %s (%s)",
                                                        start_time_str,
                                                        err)
                                        continue

                                # Récupérer le workout associé si present
                                associated_workout_id = activity.get('workoutId')

                                logger.info(
                                    "Workout ID associé à l'activité Garmin: %s",
                                    associated_workout_id,
                                )


                                if associated_workout_id:
                                    try:
                                        workout = self.garmin_client.get_workout_by_id(
                                            str(associated_workout_id)
                                            )
                                        logger.debug(
                                            "workout garmin: %s",
                                            json.dumps(workout, indent=2, ensure_ascii=False),
)

                                        activity['workout'] = workout
                                    except Exception as e: # pylint: disable=broad-except
                                        logger.warning(
                                            "Impossible de récupérer le workout %s: %s",
                                            associated_workout_id,
                                            e,
                                        )

                                activities[activity_id] = activity

                    # Petit délai pour éviter de surcharger Garmin
                    time.sleep(1)
  
                except Exception as e: # pylint: disable=broad-except
                    logger.warning("Erreur récupération activités Garmin pour %s: %s", date_str, e)

                current_date += timedelta(days=1)

            # Mettre en cache
            self.activity_cache[cache_key] = {
                'data': activities,
                'timestamp': current_time
            }

            logger.info("Récupéré %s activités Garmin sur %s jours", len(activities), days)
            return activities
   
        except Exception as e: # pylint: disable=broad-except
            logger.error("Erreur récupération activités Garmin: %s", e)
            return {}

    # Mapping Garmin typeKey to Strava type
    GARMIN_TO_STRAVA_TYPE = {
        "running": "run",
        "cycling": "ride",
        "swimming": "swim",
        "walking": "walk",
        "hiking": "hike",
        "multi_sport": "workout",
        "fitness_equipment": "workout",
        "other": "workout"
    }

    def find_matching_garmin_activity(
        self,
        strava_activity: ActivityData,
        garmin_activities: Dict
    ) -> Optional[Dict]:
        """Trouve l'activité Garmin correspondant à une activité Strava"""
        best_match = None
        min_time_diff = float('inf')

        for _, garmin_activity in garmin_activities.items():
            if 'parsed_start_time' not in garmin_activity:
                continue

            garmin_start = garmin_activity['parsed_start_time']

            # Calculer la différence de temps
            time_diff = abs((garmin_start - strava_activity.start_date).total_seconds())

            # Tolérance de 1 minutes (60 secondes)
            if time_diff < 60 and time_diff < min_time_diff:
                min_time_diff = time_diff

                # --- Activity type check with mapping ---
                garmin_type = garmin_activity.get('activityType', {}).get('typeKey', '').lower()
                # Use .value if available, else fallback to str()
                strava_type = getattr(strava_activity.type, 'value', None)
                if not strava_type:
                    strava_type = str(strava_activity.type)
                strava_type = strava_type.lower()
                mapped_strava_type = self.GARMIN_TO_STRAVA_TYPE.get(garmin_type, garmin_type)
                if mapped_strava_type and strava_type and mapped_strava_type != strava_type:
                    logger.info(
                        "Type non correspondant: Garmin '%s' (Strava attend '%s', "
                        "mapping Garmin→Strava: '%s')", 
                        garmin_type,
                        strava_type,
                        mapped_strava_type
                    )
                    continue
                # ---------------------------

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

        # Mettre à jour la description si Garmin a une description et qu'elle diffère
        if garmin_description:
            new_description = garmin_description

        return needs_update, new_name, new_description

    def update_strava_activity(self, activity_id: str, name: str, description: str) -> bool:
        """Met à jour une activité Strava"""
        if self.dry_run:
            logger.info("[DRY RUN] 🚫 Simuler mise à jour activité Strava %s: '%s' '%s'", 
                        activity_id, name, description)
            return True
        try:
            self.strava_client.update_activity(
                activity_id=int(activity_id),
                name=name,
                description=description
            )
            logger.info("✅ Activité Strava %s mise à jour: '%s'", activity_id, name)
            return True
        except Exception as e: # pylint: disable=broad-except
            logger.error("❌ Erreur mise à jour activité Strava %s: %s", activity_id, e)
            return False
    def sync_activities(self):
        """Synchronise les activités entre Strava et Garmin"""
        # --- Fenêtre horaire BE ---
        be_tz = pytz.timezone("Europe/Brussels")
        now_be = datetime.now(be_tz)
        if 0 <= now_be.hour < 6:
            logger.info("⏳ Fenêtre de synchronisation fermée (minuit-6h BE). Sync ignorée.")
            return False
        # --- Fin fenêtre horaire ---

        logger.info("🔄 Début de la synchronisation...")
        if self.dry_run:
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
            return False

        synced_cache = set()
        try:
            if os.path.exists(self.strava_cache_file):
                with open(self.strava_cache_file, "r", encoding="utf-8") as f:
                    synced_cache = set(json.load(f))
        except Exception as e: # pylint: disable=broad-except
            logger.warning("Impossible de lire le cache de synchronisation: %s", e)
            synced_cache = set()

        # Filtrer les activités déjà synchronisées
        strava_activities_to_update = [a for a in strava_activities if a.id not in synced_cache]
        if not strava_activities_to_update:
            logger.info("✅ Toutes les activités Strava récentes sont déjà synchronisées (cache)")
            return True
        # ---------------------------------------------------------------

        if not self.init_garmin_client():
            logger.error("❌ Impossible d'initialiser le client Garmin")
            return False

        garmin_activities = self.get_garmin_activities_for_period(days=sync_days)
        if not garmin_activities:
            logger.warning("⚠️ Aucune activité Garmin trouvée")
            return False

        # Synchroniser les activités
        updates_count = 0
        skipped_count = 0
        errors_count = 0

        for strava_activity in strava_activities:
            try:
                # Trouver l'activité Garmin correspondante
                garmin_activity = self.find_matching_garmin_activity(strava_activity, 
                                                                    garmin_activities)

                if not garmin_activity:
                    logger.info(
                        "⏭️ Aucune activité Garmin trouvée pour: '%s'",
                        strava_activity.name
                    )
                    skipped_count += 1
                    continue

                # Vérifier s'il faut mettre à jour
                needs_update, new_name, new_description = self.should_update_activity(
                    strava_activity, garmin_activity
                )

                if needs_update:
                    if self.update_strava_activity(strava_activity.id, new_name, new_description):
                        updates_count += 1
                        logger.info("🔄 '%s' → '%s'", strava_activity.name, new_name)
                        synced_cache.add(strava_activity.id)
                    else:
                        errors_count += 1
                else:
                    logger.info("✅ '%s' déjà à jour", strava_activity.name)
                    skipped_count += 1
                    synced_cache.add(strava_activity.id)
    
            except Exception as e: # pylint: disable=broad-except
                logger.error("❌ Erreur sync activité '%s': %s", strava_activity.name, e)
                errors_count += 1
                continue

        # Sauvegarder le cache mis à jour après la boucle
        try:
            with open(self.strava_cache_file, "w", encoding="utf-8") as f:
                json.dump(list(synced_cache), f, indent=2)
            logger.info("🗂️ Cache de synchronisation mise à jour")
        except Exception as e: # pylint: disable=broad-except
            logger.warning("Impossible de sauvegarder le cache de synchronisation: %s", e)

        # Résumé
        duration = time.time() - start_time
        logger.info("=" * 50)
        logger.info("✅ Synchronisation terminée en %.1fs", duration)
        logger.info("📊 Résultats:")
        logger.info("   • Mises à jour: %s", updates_count)
        logger.info("   • Ignorées: %s", skipped_count)
        logger.info("   • Erreurs: %s", errors_count)
        logger.info("   • Total traité: %s", len(strava_activities))
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
    try:
        # Banner
        print("=" * 60)
        print("🏃 SYNCHRONISATEUR STRAVA-GARMIN")
        print("=" * 60)

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
