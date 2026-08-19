#!/usr/bin/env bash
# Build the Docker image for linux/amd64 from a Mac (Apple Silicon or Intel).
#
# Docker Desktop cross-builds via buildx (Rosetta 2 / QEMU emulation).
#
# Usage:
#   ./scripts/build-amd64.sh                 # build and load into local Docker
#   ./scripts/build-amd64.sh --push          # build and push to Docker Hub
#   ./scripts/build-amd64.sh --tag v1.2      # custom tag (default: latest)
#   ./scripts/build-amd64.sh --push --tag v1.2
set -euo pipefail

IMAGE="greg07/strava-garmin-name-sync"
TAG="latest"
OUTPUT="--load"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --push) OUTPUT="--push"; shift ;;
    --tag)  TAG="${2:?--tag requires a value}"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Option inconnue: $1 (voir --help)" >&2; exit 1 ;;
  esac
done

cd "$(dirname "$0")/.."

if ! docker buildx version >/dev/null 2>&1; then
  echo "❌ docker buildx introuvable — installez/démarrez Docker Desktop" >&2
  exit 1
fi

echo "🔨 Build de ${IMAGE}:${TAG} pour linux/amd64…"
docker buildx build \
  --platform linux/amd64 \
  --tag "${IMAGE}:${TAG}" \
  "${OUTPUT}" \
  .

if [[ "${OUTPUT}" == "--push" ]]; then
  echo "✅ ${IMAGE}:${TAG} (linux/amd64) poussée sur Docker Hub"
else
  echo "✅ ${IMAGE}:${TAG} (linux/amd64) chargée dans le Docker local"
  docker image inspect "${IMAGE}:${TAG}" --format '   Architecture: {{.Os}}/{{.Architecture}}'
fi
