#!/usr/bin/env bash
set -euo pipefail

EA_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_SERVICE="${PROPERTYQUARRY_DB_SERVICE:-${EA_DB_SERVICE:-propertyquarry-db}}"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage:
  bash scripts/db_status.sh

Checks kernel table presence and row counts for:
  execution_sessions, execution_events, observation_events,
  delivery_outbox, policy_decisions, artifacts,
  execution_steps, execution_queue, tool_receipts, run_costs,
  approval_requests, approval_decisions, human_tasks,
  memory_candidates, memory_items,
  entities, relationships, commitments, authority_bindings, delivery_preferences,
  follow_ups, deadline_windows, stakeholders, decision_windows, communication_policies, follow_up_rules,
  interruption_budgets
EOF
  exit 0
fi

if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
else
  DC=(docker-compose)
fi

if [[ -n "${EA_DB_CONTAINER:-}" ]]; then
  DB_CONTAINER="${EA_DB_CONTAINER}"
elif [[ -n "${PROPERTYQUARRY_DB_CONTAINER_NAME:-}" ]]; then
  DB_CONTAINER="${PROPERTYQUARRY_DB_CONTAINER_NAME}"
elif [[ "${DB_SERVICE}" == "propertyquarry-db" ]]; then
  DB_CONTAINER="propertyquarry-db-live"
else
  DB_CONTAINER="${DB_SERVICE}"
fi
DB_USER="${POSTGRES_USER:-postgres}"
if [[ -n "${POSTGRES_DB:-}" ]]; then
  DB_NAME="${POSTGRES_DB}"
elif [[ "${DB_SERVICE}" == "propertyquarry-db" || "${DB_CONTAINER}" == propertyquarry-db* ]]; then
  DB_NAME="propertyquarry"
else
  DB_NAME="ea"
fi

TABLES=(
  execution_sessions
  execution_events
  observation_events
  delivery_outbox
  policy_decisions
  artifacts
  execution_steps
  execution_queue
  tool_receipts
  run_costs
  approval_requests
  approval_decisions
  human_tasks
  memory_candidates
  memory_items
  entities
  relationships
  commitments
  authority_bindings
  delivery_preferences
  follow_ups
  deadline_windows
  stakeholders
  decision_windows
  communication_policies
  follow_up_rules
  interruption_budgets
)

echo "== PropertyQuarry DB status =="
if [[ "$(docker inspect --format '{{.State.Running}}' "${DB_CONTAINER}" 2>/dev/null || true)" != "true" ]]; then
  "${DC[@]}" up -d "${DB_SERVICE}" >/dev/null
fi

db_ready="false"
for _ in $(seq 1 30); do
  if docker exec "${DB_CONTAINER}" pg_isready -U "${DB_USER}" -d "${DB_NAME}" >/dev/null 2>&1; then
    db_ready="true"
    break
  fi
  sleep 1
done
if [[ "${db_ready}" != "true" ]]; then
  echo "database container ${DB_CONTAINER} did not become ready" >&2
  exit 1
fi

echo "-- table presence --"
for t in "${TABLES[@]}"; do
  exists="$(docker exec -i "${DB_CONTAINER}" psql -At -U "${DB_USER}" -d "${DB_NAME}" -c "SELECT to_regclass('public.${t}') IS NOT NULL;" | tr -d '[:space:]')"
  echo "${t}: ${exists}"
done

echo "-- row counts --"
for t in "${TABLES[@]}"; do
  exists="$(docker exec -i "${DB_CONTAINER}" psql -At -U "${DB_USER}" -d "${DB_NAME}" -c "SELECT to_regclass('public.${t}') IS NOT NULL;" | tr -d '[:space:]')"
  if [[ "${exists}" == "t" ]]; then
    count="$(docker exec -i "${DB_CONTAINER}" psql -At -U "${DB_USER}" -d "${DB_NAME}" -c "SELECT COUNT(*) FROM public.${t};" | tr -d '[:space:]')"
    echo "${t}: ${count}"
  else
    echo "${t}: missing"
  fi
done
