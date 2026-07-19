FROM python:3.13-slim

# Informations de l'image
LABEL maintainer="greg070"
LABEL description="Application de synchronisation Strava-Garmin"

# Variables d'environnement
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Créer un utilisateur non-root
RUN useradd --create-home --shell /bin/bash app

# Répertoire de travail
WORKDIR /app

# Copier les fichiers de dépendances
COPY requirements.txt .

# Installer les dépendances Python
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copier le code de l'application
COPY src/ /app/src/

# Résolution des imports du package (src layout)
ENV PYTHONPATH=/app/src

# Créer les répertoires pour les logs et données
RUN mkdir -p /app/logs /app/data && chown -R app:app /app

# Basculer vers l'utilisateur non-root
USER app

# Volumes pour les logs et l'état persistant (tokens, cache)
VOLUME ["/app/logs", "/app/data"]

ENTRYPOINT ["python", "-m", "strava_garmin_sync_app.strava_garmin_sync"]
