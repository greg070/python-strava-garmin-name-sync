"""Dataclasses for configuration, clients, and activity models used by the sync app."""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict

from garminconnect import Garmin
from stravalib import Client as StravaClient


@dataclass
class ActivityData:
    """Classe pour stocker les données d'activité"""
    id: str
    name: str
    start_date: datetime
    type: str
    garmin_id: Optional[str] = None


@dataclass
class GeneralConfig:
    """Configuration générale de l'application"""
    dry_run: bool


@dataclass
class StravaConfig:
    """Configuration et tokens Strava"""
    client_id: Optional[str]
    client_secret: Optional[str]
    access_token: Optional[str]
    refresh_token: Optional[str]
    token_expires_at: int
    token_path: str
    cache_file: str


@dataclass
class GarminConfig:
    """Configuration Garmin"""
    email: Optional[str]
    password: Optional[str]
    tokenstore: str


@dataclass
class ActivityCache:
    """Cache d'activités et durée de validité"""
    data: Dict
    duration: int


@dataclass
class SyncState:
    """Etat volatile de l'application"""
    last_strava_connection_check: float = 0
    athlete: Optional[object] = None


@dataclass
class Clients:
    """Clients API"""
    strava: Optional[StravaClient] = None
    garmin: Optional[Garmin] = None
