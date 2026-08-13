#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage:
  bash scripts/storage_size.sh

Read-only storage inventory for PropertyQuarry local state, generated media,
backups, project Docker volumes, container writable layers, and build cache.

Environment:
  EA_STORAGE_STATE_WARN_GB           Local state warning threshold (default: 15)
  EA_STORAGE_STATE_CRITICAL_GB       Local state critical threshold (default: 25)
  EA_STORAGE_VOLUME_WARN_GB          Project-volume warning threshold (default: 25)
  EA_STORAGE_BUILD_CACHE_WARN_GB     Host build-cache warning threshold (default: 20)
  EA_STORAGE_FAIL_ON_HIGH_WATER      1 exits 3 on warning/critical (default: 0)
EOF
  exit 0
fi
if [[ -n "${1:-}" ]]; then
  echo "unknown argument: ${1}" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
STATE_WARN_GB="${EA_STORAGE_STATE_WARN_GB:-15}"
STATE_CRITICAL_GB="${EA_STORAGE_STATE_CRITICAL_GB:-25}"
VOLUME_WARN_GB="${EA_STORAGE_VOLUME_WARN_GB:-25}"
BUILD_CACHE_WARN_GB="${EA_STORAGE_BUILD_CACHE_WARN_GB:-20}"
FAIL_ON_HIGH_WATER="${EA_STORAGE_FAIL_ON_HIGH_WATER:-0}"

for pair in \
  "EA_STORAGE_STATE_WARN_GB:${STATE_WARN_GB}" \
  "EA_STORAGE_STATE_CRITICAL_GB:${STATE_CRITICAL_GB}" \
  "EA_STORAGE_VOLUME_WARN_GB:${VOLUME_WARN_GB}" \
  "EA_STORAGE_BUILD_CACHE_WARN_GB:${BUILD_CACHE_WARN_GB}"
do
  if ! [[ "${pair#*:}" =~ ^[0-9]+$ ]]; then
    echo "${pair%%:*} must be an integer" >&2
    exit 2
  fi
done
if (( STATE_CRITICAL_GB <= STATE_WARN_GB )); then
  echo "EA_STORAGE_STATE_CRITICAL_GB must be greater than EA_STORAGE_STATE_WARN_GB" >&2
  exit 2
fi
if [[ "${FAIL_ON_HIGH_WATER}" != "0" && "${FAIL_ON_HIGH_WATER}" != "1" ]]; then
  echo "EA_STORAGE_FAIL_ON_HIGH_WATER must be 0 or 1" >&2
  exit 2
fi

bytes_for_path() {
  if [[ -e "$1" ]]; then
    du -sx -B1 "$1" 2>/dev/null | awk '{print $1}'
  else
    printf '0\n'
  fi
}

GIB=$((1024 * 1024 * 1024))
state_bytes="$(bytes_for_path "${ROOT}/state")"
state_status="ok"
high_water=0
if (( state_bytes >= STATE_CRITICAL_GB * GIB )); then
  state_status="critical"
  high_water=1
elif (( state_bytes >= STATE_WARN_GB * GIB )); then
  state_status="warning"
  high_water=1
fi

echo "== PropertyQuarry storage size =="
echo "workspace_root=${ROOT}"
echo "high_water_state=${state_status} bytes=${state_bytes} warn_gb=${STATE_WARN_GB} critical_gb=${STATE_CRITICAL_GB}"
echo "-- local state classes --"
for relative in \
  state/runtime \
  state/artifacts \
  state/backups \
  state/propertyquarry-dr \
  state/private_backups \
  state/incoming_property_tours \
  state/public_property_tours \
  state/vendor_apps \
  state/vendor_installers \
  state/wine-3dvista \
  state/wine-pano2vr \
  artifacts
do
  printf 'path_bytes=%s path=%s\n' "$(bytes_for_path "${ROOT}/${relative}")" "${relative}"
done

echo "-- PropertyQuarry Docker volumes --"
volume_total=0
while IFS= read -r volume_name; do
  [[ -z "${volume_name}" ]] && continue
  mountpoint="$(docker volume inspect -f '{{.Mountpoint}}' "${volume_name}" 2>/dev/null || true)"
  volume_bytes="$(bytes_for_path "${mountpoint}")"
  if [[ -z "${volume_bytes}" || "${volume_bytes}" == "0" ]]; then
    volume_bytes="$(docker run --rm --network none --read-only \
      -v "${volume_name}:/volume:ro" alpine:3.22 sh -c \
      'kb=$(du -sk /volume 2>/dev/null | cut -f1); echo $(( ${kb:-0} * 1024 ))' \
      2>/dev/null || printf '0')"
  fi
  volume_total=$((volume_total + volume_bytes))
  printf 'volume_bytes=%s volume=%s\n' "${volume_bytes}" "${volume_name}"
done < <(docker volume ls --filter label=com.docker.compose.project=property --format '{{.Name}}' | sort)
volume_status="ok"
if (( volume_total >= VOLUME_WARN_GB * GIB )); then
  volume_status="warning"
  high_water=1
fi
echo "high_water_project_volumes=${volume_status} bytes=${volume_total} warn_gb=${VOLUME_WARN_GB}"

echo "-- Docker host summary (shared host; diagnostic only) --"
docker system df
build_cache_bytes="$(docker system df --format '{{json .}}' 2>/dev/null | python3 -c 'import json,sys
total=0
for line in sys.stdin:
    try: row=json.loads(line)
    except Exception: continue
    if str(row.get("Type") or "").lower()=="build cache":
        raw=str(row.get("Size") or "0B").strip().upper()
        units={"B":1,"KB":1000,"MB":1000**2,"GB":1000**3,"TB":1000**4}
        for unit in ("TB","GB","MB","KB","B"):
            if raw.endswith(unit):
                try: total=int(float(raw[:-len(unit)])*units[unit])
                except Exception: total=0
                break
print(total)' || printf '0')"
build_cache_status="ok"
if (( build_cache_bytes >= BUILD_CACHE_WARN_GB * GIB )); then
  build_cache_status="warning"
  high_water=1
fi
echo "high_water_build_cache=${build_cache_status} bytes=${build_cache_bytes} warn_gb=${BUILD_CACHE_WARN_GB} shared_host=true"

if [[ "${FAIL_ON_HIGH_WATER}" == "1" && "${high_water}" == "1" ]]; then
  echo "storage high-water threshold exceeded" >&2
  exit 3
fi
