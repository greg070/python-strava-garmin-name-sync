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
import schedule
from stravalib import Client as StravaClient
from garminconnect import Garmin
import pytz
from dataclasses import dataclass

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
        if os.path.exists(self.strava_token_path):
            try:
                with open(self.strava_token_path, "r") as f:
                    token_data = json.load(f)
                    self.strava_access_token = token_data.get("access_token", self.strava_access_token)
                    self.strava_refresh_token = token_data.get("refresh_token", self.strava_refresh_token)
                    self.strava_token_expires_at = token_data.get("expires_at", self.strava_token_expires_at)
                logger.info(f"Token Strava chargé depuis {self.strava_token_path}")
            except Exception as e:
                logger.warning(f"Impossible de lire {self.strava_token_path}: {e}")
        
        self.garmin_email = os.getenv('GARMIN_EMAIL')
        self.garmin_password = os.getenv('GARMIN_PASSWORD')

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
                logger.info(f"Connecté à Strava: {self.strava_athlete.firstname} {self.strava_athlete.lastname}")
                self.last_strava_connection_check = now
            else:
                logger.info(f"Connecté à Strava: {self.strava_athlete.firstname} {self.strava_athlete.lastname}") 

            return True
            
        except Exception as e:
            logger.error(f"Erreur initialisation Strava: {e}")
            return False
    
    def init_garmin_client(self) -> bool:
        """Initialise le client Garmin"""
        try:
            self.garmin_client = Garmin(self.garmin_email, self.garmin_password)
            self.garmin_client.login()
            
            # Test de connexion
            display_name = self.garmin_client.get_full_name()
            logger.info(f"Connecté à Garmin: {display_name}")
            return True
            
        except Exception as e:
            logger.error(f"Erreur initialisation Garmin: {e}")
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

            print("Access Token:", token_response['access_token'])
            print("Refresh Token:", token_response['refresh_token'])
            print("Expires At:", token_response['expires_at'])

            # Sauvegarder le refresh token dans un fichier pour persistance
            try:
                with open(self.strava_token_path, "w") as f:
                    json.dump({
                        "access_token": self.strava_access_token,
                        "refresh_token": self.strava_refresh_token,
                        "expires_at": self.strava_token_expires_at
                    }, f, indent=2)
                logger.info("Token Strava sauvegardé dans strava_token.json")
            except Exception as file_err:
                logger.warning(f"Impossible de sauvegarder le token Strava: {file_err}")
            
            logger.info("Token Strava rafraîchi avec succès")
            
        except Exception as e:
            logger.error(f"Erreur rafraîchissement token: {e}")
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

                logger.info(f"Strava Activity: ID={activity.id}, Name='{activity.name}', Start={start_date}, Type={activity.type.root}, sport_type={activity.sport_type.root}")
                logger.debug(f"Strava Activity: {activity}")

                activity_data = ActivityData(
                    id=str(activity.id),
                    name=activity.name or "",
                    start_date=start_date,
                    type=activity.type.root or "Unknown"
                )
                strava_activities.append(activity_data)
            
            logger.info(f"Récupéré {len(strava_activities)} activités Strava")
            return strava_activities
            
        except Exception as e:
            logger.error(f"Erreur récupération activités Strava: {e}")
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
                            
                            logger.info(f"Garmin Activity: ID={activity.get('activityId')}, Name='{activity.get('activityName')}', Start={activity.get('startTimeLocal')}, Type={activity.get('activityType', {}).get('typeKey')}")

                            logger.debug(f"Garmin Activity: {json.dumps(activity, indent=2, ensure_ascii=False)}")
                       
                            activity_id = str(activity.get('activityId', ''))
                            if activity_id:
                                # Normaliser la date de début
                                start_time_str = activity.get('startTimeLocal', '')
                                if start_time_str:
                                    # Garmin utilise parfois des formats différents
                                    try:
                                        if 'T' in start_time_str:
                                            start_time = datetime.fromisoformat(start_time_str.replace('Z', ''))
                                        else:
                                            start_time = datetime.strptime(start_time_str, '%Y-%m-%d %H:%M:%S')
                                        activity['parsed_start_time'] = start_time
                                    except:
                                        logger.warning(f"Format de date non reconnu: {start_time_str}")
                                        continue

                                # Récupérer le workout associé si present
                                associated_workout_id = activity.get('workoutId')
                                
                                logger.info(f"Workout ID associé à l'activité Garmin: {associated_workout_id}")

                                if associated_workout_id:
                                    try:
                                        workout = self.garmin_client.get_workout_by_id(str(associated_workout_id))
                                        logger.debug(f"workout garmin : {json.dumps(workout, indent=2, ensure_ascii=False)}")
                                        activity['workout'] = workout
                                    except Exception as e:
                                        logger.warning(f"Impossible de récupérer le workout {associated_workout_id}: {e}")
                                
                                activities[activity_id] = activity
                    
                    # Petit délai pour éviter de surcharger Garmin
                    time.sleep(1)
                    
                except Exception as e:
                    logger.warning(f"Erreur récupération activités Garmin pour {date_str}: {e}")
                
                current_date += timedelta(days=1)
            
            # Mettre en cache
            self.activity_cache[cache_key] = {
                'data': activities,
                'timestamp': current_time
            }
            
            logger.info(f"Récupéré {len(activities)} activités Garmin sur {days} jours")
            return activities
            
        except Exception as e:
            logger.error(f"Erreur récupération activités Garmin: {e}")
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
    
    def find_matching_garmin_activity(self, strava_activity: ActivityData, garmin_activities: Dict) -> Optional[Dict]:
        """Trouve l'activité Garmin correspondant à une activité Strava"""
        best_match = None
        min_time_diff = float('inf')
        
        for garmin_id, garmin_activity in garmin_activities.items():
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
                    logger.info(f"Type non correspondant: Garmin '{garmin_type}' (Strava attend '{strava_type}', mapping Garmin→Strava: '{mapped_strava_type}')")
                    continue
                # ---------------------------

                best_match = garmin_activity
        
        if best_match:
            logger.info(f"Match trouvé pour '{strava_activity.name}' avec différence de {min_time_diff/60:.1f} min")
        
        return best_match
    
    def should_update_activity(self, strava_activity: ActivityData, garmin_activity: Dict) -> tuple[bool, str, str]:
        """Détermine si une activité doit être mise à jour"""
        garmin_name = garmin_activity.get('activityName', '').strip()
        garmin_description = garmin_activity.get('description', '').strip()

        # Prioriser le nom et la description du workout si disponible
        workout = garmin_activity.get('workout')
        if workout:
            garmin_name = workout.get('workoutName', garmin_name).strip() or garmin_name
            garmin_description = workout.get('description', garmin_description).strip() or garmin_description

        
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
            logger.info(f"[DRY RUN] 🚫 Simuler mise à jour activité Strava {activity_id}: '{name}' '{description}'")
            return True
        try:
            self.strava_client.update_activity(
                activity_id=int(activity_id),
                name=name,
                description=description
            )
            logger.info(f"✅ Activité Strava {activity_id} mise à jour: '{name}'")
            return True
        except Exception as e:
            logger.error(f"❌ Erreur mise à jour activité Strava {activity_id}: {e}")
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
        
        if not self.init_garmin_client():
            logger.error("❌ Impossible d'initialiser le client Garmin")
            return False
        
        # Récupérer les activités des deux plateformes
        sync_days = int(os.getenv('SYNC_DAYS', '7'))
        logger.info(f"📅 Synchronisation des activités des {sync_days} derniers jours")
        
        strava_activities = self.get_recent_strava_activities(days=sync_days)
        if not strava_activities:
            logger.warning("⚠️ Aucune activité Strava trouvée")
            return False
        
        synced_cache = set()
        try:
            if os.path.exists(self.strava_cache_file):
                with open(self.strava_cache_file, "r") as f:
                    synced_cache = set(json.load(f))
        except Exception as e:
            logger.warning(f"Impossible de lire le cache de synchronisation: {e}")
            synced_cache = set()

        # Filtrer les activités déjà synchronisées
        strava_activities_to_update = [a for a in strava_activities if a.id not in synced_cache]
        if not strava_activities_to_update:
            logger.info("✅ Toutes les activités Strava récentes sont déjà synchronisées (cache)")
            return True
        # ---------------------------------------------------------------

        
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
                garmin_activity = self.find_matching_garmin_activity(strava_activity, garmin_activities)
                
                if not garmin_activity:
                    logger.info(f"⏭️ Aucune activité Garmin trouvée pour: '{strava_activity.name}'")
                    skipped_count += 1
                    continue
                
                # Vérifier s'il faut mettre à jour
                needs_update, new_name, new_description = self.should_update_activity(
                    strava_activity, garmin_activity
                )
                
                if needs_update:
                    if self.update_strava_activity(strava_activity.id, new_name, new_description):
                        updates_count += 1
                        logger.info(f"🔄 '{strava_activity.name}' → '{new_name}'")
                        synced_cache.add(strava_activity.id)
                    else:
                        errors_count += 1
                else:
                    logger.info(f"✅ '{strava_activity.name}' déjà à jour")
                    skipped_count += 1
                    synced_cache.add(strava_activity.id)
                
            except Exception as e:
                logger.error(f"❌ Erreur sync activité '{strava_activity.name}': {e}")
                errors_count += 1
                continue

        # Sauvegarder le cache mis à jour après la boucle
        try:
            with open(self.strava_cache_file, "w") as f:
                json.dump(list(synced_cache), f, indent=2)
            logger.info("🗂️ Cache de synchronisation mise à jour")
        except Exception as e:
            logger.warning(f"Impossible de sauvegarder le cache de synchronisation: {e}")
        
        # Résumé
        duration = time.time() - start_time
        logger.info("=" * 50)
        logger.info(f"✅ Synchronisation terminée en {duration:.1f}s")
        logger.info(f"📊 Résultats:")
        logger.info(f"   • Mises à jour: {updates_count}")
        logger.info(f"   • Ignorées: {skipped_count}")
        logger.info(f"   • Erreurs: {errors_count}")
        logger.info(f"   • Total traité: {len(strava_activities)}")
        logger.info("=" * 50)
        
        return True
    
    def run_scheduler(self, interval_minutes: int = 60):
        """Lance le planificateur de synchronisation"""
        logger.info(f"⏰ Démarrage du planificateur (intervalle: {interval_minutes} min)")
        
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
            except Exception as e:
                logger.error(f"❌ Erreur planificateur: {e}")
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
        logger.error(f"💥 Erreur fatale: {e}")
        raise

if __name__ == "__main__":
    main()