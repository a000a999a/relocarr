#!/usr/bin/env bash
# Interactive install/reinstall for relocarr. Safe to re-run: with no flags it
# reuses the existing .env, rebuilds the image, and restarts the container
# without touching your configured apps/jobs.
set -euo pipefail
cd "$(dirname "$0")"

RECONFIGURE=false
RESET_DB=false
for arg in "$@"; do
  case "$arg" in
    --reconfigure) RECONFIGURE=true ;;
    --reset-db) RESET_DB=true ;;
    -h|--help)
      echo "Usage: ./install.sh [--reconfigure] [--reset-db]"
      echo
      echo "  (no flags)      first install, or rebuild+restart with existing settings"
      echo "  --reconfigure   re-ask every setup question, even if .env already exists"
      echo "  --reset-db      wipe the relocarr database (all apps/jobs) and start fresh"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg (see --help)"
      exit 1
      ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required but wasn't found. Install it first: https://docs.docker.com/engine/install/"
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  echo "Neither 'docker compose' nor 'docker-compose' was found. Install the Docker Compose plugin first."
  exit 1
fi

if [ -f .env ] && [ "$RECONFIGURE" = false ]; then
  echo "Reusing existing .env (run './install.sh --reconfigure' to change settings)."
else
  if [ -f .env ]; then
    backup=".env.bak.$(date +%Y%m%d%H%M%S)"
    cp .env "$backup"
    echo "Backed up existing .env to $backup"
  fi

  echo
  echo "relocarr setup"
  echo "--------------"
  echo "These two paths must match what Radarr/Sonarr/Transmission themselves use --"
  echo "relocarr has to see each file at the exact path the *arr reports it at."
  echo

  read -rp "Downloads/staging root (e.g. /home/user/Downloads): " DOWNLOADS_PATH
  while [ ! -d "$DOWNLOADS_PATH" ]; do
    echo "  '$DOWNLOADS_PATH' doesn't exist on this machine."
    read -rp "Downloads/staging root: " DOWNLOADS_PATH
  done

  read -rp "Media/target root (e.g. /mnt/nvidia): " MEDIA_PATH
  while [ ! -d "$MEDIA_PATH" ]; do
    echo "  '$MEDIA_PATH' doesn't exist on this machine."
    read -rp "Media/target root: " MEDIA_PATH
  done

  echo
  echo "Docker networks on this host (pick the one your *arr stack is on, so"
  echo "relocarr can reach it by container name, e.g. http://radarr:7878):"
  docker network ls --format '  {{.Name}}' | grep -vE '^  (bridge|host|none)$' || echo "  (none found)"
  read -rp "Network name to join [blank = skip, reach *arr apps by host IP instead]: " ARR_NETWORK_NAME

  read -rp "PUID [default: $(id -u)]: " PUID_IN
  PUID="${PUID_IN:-$(id -u)}"
  read -rp "PGID [default: $(id -g)]: " PGID_IN
  PGID="${PGID_IN:-$(id -g)}"

  read -rp "Port for the web UI [default: 8686]: " PORT_IN
  RELOCARR_PORT="${PORT_IN:-8686}"

  cat > .env <<EOF
DOWNLOADS_PATH=$DOWNLOADS_PATH
MEDIA_PATH=$MEDIA_PATH
ARR_NETWORK_NAME=$ARR_NETWORK_NAME
PUID=$PUID
PGID=$PGID
RELOCARR_PORT=$RELOCARR_PORT
EOF
  echo "Saved .env."
fi

# shellcheck disable=SC1091
source .env

if [ -n "${ARR_NETWORK_NAME:-}" ]; then
  if ! docker network inspect "$ARR_NETWORK_NAME" >/dev/null 2>&1; then
    echo "Warning: network '$ARR_NETWORK_NAME' doesn't exist yet -- 'docker compose up' will fail."
    echo "Start your *arr stack first, or re-run with --reconfigure to pick a different network."
    exit 1
  fi
  cat > docker-compose.override.yml <<EOF
services:
  relocarr:
    networks:
      - arr-net

networks:
  arr-net:
    external: true
    name: ${ARR_NETWORK_NAME}
EOF
else
  rm -f docker-compose.override.yml
fi

mkdir -p config

if [ "$RESET_DB" = true ] && [ -f config/relocarr.db ]; then
  backup="config/relocarr.db.bak.$(date +%Y%m%d%H%M%S)"
  mv config/relocarr.db "$backup"
  echo "Backed up database to $backup -- apps will need to be re-added through the UI."
fi

echo
echo "Building and starting relocarr..."
$COMPOSE up -d --build

echo
echo "Done. Web UI: http://localhost:${RELOCARR_PORT}/"
echo "(or http://<this host's LAN IP>:${RELOCARR_PORT}/ from another machine)"
