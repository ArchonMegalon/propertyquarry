#!/usr/bin/env bash
set -euo pipefail

PROPERTYQUARRY_HOST_RECOVERY_ALLOW="${PROPERTYQUARRY_HOST_RECOVERY_ALLOW:-0}"
PROPERTYQUARRY_HOST_RECOVERY_DRY_RUN="${PROPERTYQUARRY_HOST_RECOVERY_DRY_RUN:-0}"

if [[ "${PROPERTYQUARRY_HOST_RECOVERY_ALLOW}" != "1" ]]; then
  echo "Refusing host-level Docker recovery without PROPERTYQUARRY_HOST_RECOVERY_ALLOW=1" >&2
  exit 2
fi

log() {
  printf '[propertyquarry-postreboot] %s\n' "$1"
}

run() {
  log "running: $*"
  if [[ "${PROPERTYQUARRY_HOST_RECOVERY_DRY_RUN}" == "1" ]]; then
    return 0
  fi
  "$@"
}

wait_for_docker() {
  local deadline=$((SECONDS + 180))
  while (( SECONDS < deadline )); do
    if docker info >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  return 1
}

recreate_compose_stack() {
  local workdir="$1"
  shift
  run docker compose --project-directory "$workdir" "$@" up -d --force-recreate --remove-orphans
}

main() {
  cd /docker/property

  log "waiting for docker"
  if ! wait_for_docker; then
    log "docker did not become ready within 180s"
    exit 1
  fi

  log "recovering standalone PropertyQuarry first"
  run bash /docker/property/scripts/harden_propertyquarry_docker.sh

  log "recreating the externalbrain tunnel without reviving legacy ea-api"
  run docker compose \
    -f /docker/EA/docker-compose.yml \
    -f /docker/EA/docker-compose.prod.yml \
    -f /docker/EA/docker-compose.cloudflared.yml \
    up -d --force-recreate --no-deps ea-cloudflared

  log "refreshing lightweight utility stacks"
  run docker rm -f dozzle filebrowser 2>/dev/null || true
  recreate_compose_stack /docker/dozzle -f /docker/dozzle/docker-compose.yml
  recreate_compose_stack /docker/filebrowser -f /docker/filebrowser/docker-compose.yml

  log "recovering plex stack"
  run docker rm -f plex tautulli autoheal-plex 2>/dev/null || true
  recreate_compose_stack /docker/plex -f /docker/plex/docker-compose.yml -f /docker/plex/docker-compose.override.yml

  log "recovering media stack"
  recreate_compose_stack /docker/arr-v2 -f /docker/arr-v2/docker-compose.yml

  log "recovering fleet stack"
  recreate_compose_stack /docker/fleet -f /docker/fleet/docker-compose.yml

  log "recovering chummer public edge stack"
  recreate_compose_stack /docker/chummercomplete/chummer.run-services -f /docker/chummercomplete/chummer.run-services/docker-compose.public-edge.yml

  log "recovering immich stack"
  recreate_compose_stack /docker/immich -f /docker/immich/docker-compose.yml -f /docker/immich/docker-compose.override.yml

  log "skipping legacy EA app stack on purpose"
  log "propertyquarry.com currently relies on the externalbrain tunnel reaching PropertyQuarry via the legacy ea-api alias"
  log "do not start ea-api/ea-worker/ea-scheduler until that tunnel target is intentionally redesigned"

  log "final service overview"
  run docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Networks}}'
}

main "$@"
