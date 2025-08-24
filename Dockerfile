FROM python:3.13-slim

# Informations de l'image
LABEL maintainer="your-email@example.com"
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

# Copier le code de l'application (arborescence complète)
COPY src/ /app/src/

# Assurer la résolution des imports de type `from src...`
ENV PYTHONPATH=/app

# Créer le répertoire pour les logs
RUN mkdir -p /app/logs && mkdir -p /app/data

# Changer la propriété des fichiers
RUN chown -R app:app /app

# Basculer vers l'utilisateur non-root
USER app

# Volume pour les logs
VOLUME ["/app/logs", "/app/data"]

# Port d'exposition (optionnel, pour monitoring futur)
EXPOSE 8080

# Point d'entrée (exécuter le module, évite les imports relatifs cassés)
ENTRYPOINT ["python", "-m", "src.strava_garmin_sync_app.strava_garmin_sync"]