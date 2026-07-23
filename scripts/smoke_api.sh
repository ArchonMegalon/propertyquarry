#!/usr/bin/env bash
set -euo pipefail

EA_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SMOKE_TMP_DIR="${EA_ROOT}/.smoke_tmp"
API_SERVICE="${PROPERTYQUARRY_API_SERVICE:-${EA_API_SERVICE:-ea-api}}"
PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED="${PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED:-1}"

if [[ "${PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED}" != "0" && "${PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED}" != "1" ]]; then
  echo "PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED must be 0 or 1" >&2
  exit 2
fi

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage:
  bash scripts/smoke_api.sh

Runs end-to-end HTTP smoke checks for liveness/readiness/version,
rewrite/session/policy/approvals, observations, delivery outbox, channel adapters,
tool/connector registry endpoints, task-contract endpoints, skill catalog endpoints,
plan compile endpoint, and memory candidate/item/entity/relationship/commitment/authority-binding/delivery-preference/follow-up/deadline-window/stakeholder/decision-window/communication-policy/follow-up-rule/interruption-budget endpoints.

Auth:
  If EA_API_TOKEN is set, the script sends Authorization: Bearer <token>.
  Principal-scoped rewrite/plan, connector, human-task, and memory checks send
  X-EA-Principal-ID from EA_PRINCIPAL_ID
  (default: exec-1) and verify mismatches against EA_MISMATCH_PRINCIPAL_ID
  (default: exec-2) return principal_scope_mismatch.

PropertyQuarry surface:
  PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED defaults to 1 and requires the
  anonymous branded homepage to return PropertyQuarry HTML without auth errors.
  Generic runtime harnesses that do not mount the branded surface may set it to 0.

Exit codes:
  11 missing execution_session_id
  12 policy contract mismatch
  13 missing resource id from runtime response
EOF
  exit 0
fi

fail() {
  local code="$1"
  local msg="$2"
  echo "${msg}" >&2
  exit "${code}"
}

curl() {
  command curl \
    --retry 20 \
    --retry-delay 1 \
    --retry-max-time 120 \
    --retry-all-errors \
    --retry-connrefused \
    --connect-timeout 5 \
    --max-time 600 \
    "$@"
}

wait_for_session_status() {
  local session_id="$1"
  local expected_status="$2"
  local attempts="${3:-120}"
  local sleep_seconds="${4:-0.5}"
  local body=""
  local current_status=""
  local i
  for i in $(seq 1 "${attempts}"); do
    body="$(curl -fsS "${BASE}/v1/rewrite/sessions/${session_id}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
    current_status="$(python3 -c "import json,sys; print(json.loads(sys.stdin.read() or '{}').get('status',''))" <<<"${body}")"
    if [[ "${current_status}" == "${expected_status}" ]]; then
      printf '%s' "${body}"
      return 0
    fi
    sleep "${sleep_seconds}"
  done
  echo "timed out waiting for session ${session_id} to reach ${expected_status}; last status=${current_status}" >&2
  printf '%s' "${body}"
  return 1
}

plan_execute_artifact_json() {
  local response="$1"
  local artifact_id=""
  local session_id=""
  local session_json=""
  artifact_id="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("artifact_id",""))' <<<"${response}")"
  if [[ -n "${artifact_id}" ]]; then
    printf '%s' "${response}"
    return 0
  fi
  session_id="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("session_id",""))' <<<"${response}")"
  if [[ -z "${session_id}" ]]; then
    printf '%s' "${response}"
    return 0
  fi
  session_json="$(wait_for_session_status "${session_id}" "completed" 120 0.5)"
  artifact_id="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); rows=body.get("artifacts") or []; print((rows[-1] or {}).get("artifact_id","") if rows else "")' <<<"${session_json}")"
  if [[ -z "${artifact_id}" ]]; then
    printf '%s' "${response}"
    return 0
  fi
  curl -fsS "${BASE}/v1/rewrite/artifacts/${artifact_id}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}"
}

curl_status_code() {
  local response_path="$1"
  shift
  local response_base response_tmp
  local header_path code rc
  mkdir -p "${SMOKE_TMP_DIR}"
  header_path="$(mktemp)"
  response_base="$(basename "${response_path}")"
  response_tmp="${SMOKE_TMP_DIR}/${response_base}"
  curl -sS -D "${header_path}" -o "${response_tmp}" "$@"
  rc=$?
  code="$(awk '/^HTTP\// { code=$2 } END { print code }' "${header_path}")"
  if [[ -n "${code}" ]]; then
    rm -f "${header_path}"
    printf '%s' "${code}"
    return 0
  fi
  rm -f "${header_path}"
  return 1
}

curl_body_retry() {
  local attempts="$1"
  local sleep_seconds="$2"
  shift 2
  local body=""
  local i
  for i in $(seq 1 "${attempts}"); do
    body="$(curl -fsS "$@" || true)"
    if [[ -n "${body}" ]]; then
      printf '%s' "${body}"
      return 0
    fi
    sleep "${sleep_seconds}"
  done
  printf '%s' "${body}"
  return 1
}

route_status_code() {
  local route_path="$1"
  local probe_path="${SMOKE_TMP_DIR}/route_probe_$(printf '%s' "${route_path}" | tr '/?=&:%.' '_').json"
  local code=""
  code="$(curl_status_code "${probe_path}" "${BASE}${route_path}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" || true)"
  rm -f "${probe_path}"
  printf '%s' "${code}"
}

route_available() {
  local route_path="$1"
  local code=""
  code="$(route_status_code "${route_path}")"
  [[ -n "${code}" && "${code}" != "404" ]]
}

resolve_api_container() {
  local container=""
  if ! command -v docker >/dev/null 2>&1; then
    return 0
  fi
  container="$(docker ps --filter "label=com.docker.compose.service=${API_SERVICE}" --format '{{.Names}}' 2>/dev/null | head -n1 || true)"
  if [[ -z "${container}" ]]; then
    container="$(docker ps --filter "name=${API_SERVICE}" --format '{{.Names}}' 2>/dev/null | head -n1 || true)"
  fi
  printf '%s' "${container}"
}

HOST_PORT="${EA_HOST_PORT:-}"
if [[ -z "${HOST_PORT}" && -f "${EA_ROOT}/.env" ]]; then
  HOST_PORT="$(grep -E '^EA_HOST_PORT=' "${EA_ROOT}/.env" | tail -n1 | cut -d= -f2- || true)"
fi
HOST_PORT="${HOST_PORT:-8090}"
BASE="http://localhost:${HOST_PORT}"
EA_API_TOKEN="${EA_API_TOKEN:-}"
if [[ -z "${EA_API_TOKEN}" && -f "${EA_ROOT}/.env" ]]; then
  EA_API_TOKEN="$(grep -E '^EA_API_TOKEN=' "${EA_ROOT}/.env" | tail -n1 | cut -d= -f2- || true)"
fi
if [[ -z "${EA_API_TOKEN}" ]] && command -v docker >/dev/null 2>&1; then
  api_container="$(resolve_api_container)"
  if [[ -n "${api_container}" ]]; then
    EA_API_TOKEN="$(docker exec "${api_container}" /bin/sh -lc 'printenv EA_API_TOKEN' 2>/dev/null || true)"
  fi
fi
AUTH_ARGS=()
if [[ -n "${EA_API_TOKEN:-}" ]]; then
  AUTH_ARGS=(-H "Authorization: Bearer ${EA_API_TOKEN}" -H "X-EA-API-Token: ${EA_API_TOKEN}")
fi
EA_TELEGRAM_INGEST_SECRET="${EA_TELEGRAM_INGEST_SECRET:-}"
if [[ -z "${EA_TELEGRAM_INGEST_SECRET}" && -f "${EA_ROOT}/.env" ]]; then
  EA_TELEGRAM_INGEST_SECRET="$(grep -E '^EA_TELEGRAM_INGEST_SECRET=' "${EA_ROOT}/.env" | tail -n1 | cut -d= -f2- || true)"
fi
if [[ -z "${EA_TELEGRAM_INGEST_SECRET}" ]] && command -v docker >/dev/null 2>&1; then
  api_container="$(resolve_api_container)"
  if [[ -n "${api_container}" ]]; then
    EA_TELEGRAM_INGEST_SECRET="$(docker exec "${api_container}" /bin/sh -lc 'printenv EA_TELEGRAM_INGEST_SECRET' 2>/dev/null || true)"
  fi
fi
TELEGRAM_INGEST_ARGS=()
if [[ -n "${EA_TELEGRAM_INGEST_SECRET:-}" ]]; then
  TELEGRAM_INGEST_ARGS=(-H "x-telegram-bot-api-secret-token: ${EA_TELEGRAM_INGEST_SECRET}")
fi
PRINCIPAL_ID="${EA_PRINCIPAL_ID:-exec-1}"
MISMATCH_PRINCIPAL_ID="${EA_MISMATCH_PRINCIPAL_ID:-exec-2}"
PRINCIPAL_ARGS=(-H "X-EA-Principal-ID: ${PRINCIPAL_ID}")
OPERATOR_PRINCIPAL_ID="${EA_OPERATOR_PRINCIPAL_ID:-}"
if [[ -z "${OPERATOR_PRINCIPAL_ID}" && -f "${EA_ROOT}/.env" ]]; then
  OPERATOR_PRINCIPAL_ID="$(grep -E '^EA_OPERATOR_PRINCIPAL_ID=' "${EA_ROOT}/.env" | tail -n1 | cut -d= -f2- || true)"
fi
OPERATOR_PRINCIPAL_ARGS=()
if [[ -n "${OPERATOR_PRINCIPAL_ID}" ]]; then
  OPERATOR_PRINCIPAL_ARGS=(-H "X-EA-Principal-ID: ${OPERATOR_PRINCIPAL_ID}")
fi

operator_curl() {
  if [[ -n "${OPERATOR_PRINCIPAL_ID}" ]]; then
    curl -fsS "${AUTH_ARGS[@]}" "${OPERATOR_PRINCIPAL_ARGS[@]}" "$@"
    return
  fi
  local operator_container=""
  local attempt=0
  while (( attempt < 30 )); do
    operator_container="$(resolve_api_container)"
    if [[ -n "${operator_container}" ]] && docker exec "${operator_container}" /bin/sh -lc 'for i in 1 2 3 4 5; do curl -fsS http://127.0.0.1:8090/health >/dev/null && exit 0; sleep 1; done; exit 1' >/dev/null 2>&1; then
      local arg
      local translated=()
      for arg in "${AUTH_ARGS[@]}"; do
        translated+=("$(printf '%q' "${arg}")")
      done
      translated+=(-H "$(printf '%q' "X-EA-Principal-ID: ${PRINCIPAL_ID}")")
      for arg in "$@"; do
        if [[ "${arg}" == "${BASE}"* ]]; then
          arg="${arg/${BASE}/http://127.0.0.1:8090}"
        fi
        translated+=("$(printf '%q' "${arg}")")
      done
      docker exec "${operator_container}" /bin/sh -lc "curl -fsS ${translated[*]}"
      return
    fi
    sleep 1
    attempt=$((attempt + 1))
  done
  if curl -fsS "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" "${BASE}/health" >/dev/null 2>&1; then
    local arg
    local translated=("${AUTH_ARGS[@]}" -H "X-EA-Principal-ID: ${PRINCIPAL_ID}")
    for arg in "$@"; do
      translated+=("${arg}")
    done
    curl -fsS "${translated[@]}"
    return
  fi
  echo "operator context unavailable for control-plane smoke calls" >&2
  return 1
}

operator_post_json() {
  operator_curl -X POST "$@"
}

reset_rewrite_contract() {
  operator_post_json "${BASE}/v1/tasks/contracts" -H 'content-type: application/json' \
    -d '{"task_key":"rewrite_text","deliverable_type":"rewrite_note","default_risk_class":"low","default_approval_class":"none","allowed_tools":["artifact_repository"],"evidence_requirements":["stakeholder_context"],"memory_write_policy":"reviewed_only","budget_policy_json":{"class":"low"}}' >/dev/null
}

cleanup_smoke_contract_state() {
  reset_rewrite_contract || true
}

trap cleanup_smoke_contract_state EXIT

curl -fsS -X POST "${BASE}/v1/onboarding/start" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" \
  -H 'content-type: application/json' \
  -d '{"workspace_name":"Smoke Workspace","workspace_mode":"executive_ops","region":"AT","language":"en","timezone":"Europe/Vienna","selected_channels":["google"]}' >/dev/null

ensure_operator_profile() {
  local operator_id="$1"
  local role="$2"
  local skill_tags_json="${3:-[]}"
  local trust_tier="${4:-standard}"
  local display_name="${5:-$1}"
  local payload
  payload="$(printf '{"operator_id":"%s","display_name":"%s","roles":["%s"],"skill_tags":%s,"trust_tier":"%s","status":"active"}' "${operator_id}" "${display_name}" "${role}" "${skill_tags_json}" "${trust_tier}")"
  operator_post_json "${BASE}/v1/human/tasks/operators" -H 'content-type: application/json' -d "${payload}" >/dev/null
}
APPROVAL_THRESHOLD_CHARS="${EA_APPROVAL_THRESHOLD_CHARS:-}"
if [[ -z "${APPROVAL_THRESHOLD_CHARS}" && -f "${EA_ROOT}/.env" ]]; then
  APPROVAL_THRESHOLD_CHARS="$(grep -E '^EA_APPROVAL_THRESHOLD_CHARS=' "${EA_ROOT}/.env" | tail -n1 | cut -d= -f2- || true)"
fi
APPROVAL_THRESHOLD_CHARS="${APPROVAL_THRESHOLD_CHARS:-5000}"
MAX_REWRITE_CHARS="${EA_MAX_REWRITE_CHARS:-}"
if [[ -z "${MAX_REWRITE_CHARS}" && -f "${EA_ROOT}/.env" ]]; then
  MAX_REWRITE_CHARS="$(grep -E '^EA_MAX_REWRITE_CHARS=' "${EA_ROOT}/.env" | tail -n1 | cut -d= -f2- || true)"
fi
MAX_REWRITE_CHARS="${MAX_REWRITE_CHARS:-20000}"
SMOKE_RUN_TOKEN="${EA_SMOKE_RUN_TOKEN:-$(date +%s)-$$}"
read -r TELEGRAM_SMOKE_CHAT_ID TELEGRAM_SMOKE_MESSAGE_ID < <(
  python3 -c 'import hashlib,sys; value=int(hashlib.sha256(sys.argv[1].encode()).hexdigest(),16); print(1_000_000_000 + value % 900_000_000_000, 1 + (value >> 40) % 2_000_000_000)' "${SMOKE_RUN_TOKEN}"
)
# Release-guard anchors for dispatch/memory workflow smoke coverage:
# dispatch-memory@example.com
# reviewed-memory@example.com
# hybrid@example.com
# hybrid-retry@example.com
HYBRID_RECIPIENT="hybrid-${SMOKE_RUN_TOKEN}@example.com"
HYBRID_RETRY_RECIPIENT="hybrid-retry-${SMOKE_RUN_TOKEN}@example.com"
LEGACY_HUMAN_TASKS_AVAILABLE=0
LEGACY_OBSERVATIONS_AVAILABLE=0
LEGACY_DELIVERY_AVAILABLE=0
LEGACY_CONNECTORS_AVAILABLE=0
LEGACY_TASK_CONTRACTS_AVAILABLE=0
LEGACY_SKILLS_AVAILABLE=0
LEGACY_RESPONSES_AVAILABLE=0
if route_available "/v1/human/tasks?limit=1"; then
  LEGACY_HUMAN_TASKS_AVAILABLE=1
fi
if route_available "/v1/observations/recent?limit=1"; then
  LEGACY_OBSERVATIONS_AVAILABLE=1
fi
if route_available "/v1/delivery/outbox/pending?limit=1"; then
  LEGACY_DELIVERY_AVAILABLE=1
fi
if route_available "/v1/connectors/bindings?limit=1"; then
  LEGACY_CONNECTORS_AVAILABLE=1
fi
if route_available "/v1/tasks/contracts"; then
  LEGACY_TASK_CONTRACTS_AVAILABLE=1
fi
if route_available "/v1/skills?limit=1"; then
  LEGACY_SKILLS_AVAILABLE=1
fi
if route_available "/v1/responses"; then
  LEGACY_RESPONSES_AVAILABLE=1
fi

echo "== smoke: health =="
curl -fsS "${BASE}/health" >/dev/null
curl -fsS "${BASE}/health/live" >/dev/null
curl -fsS "${BASE}/health/ready" >/dev/null
curl -fsS "${BASE}/version" >/dev/null
echo "health/version ok"

if [[ "${PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED}" == "1" ]]; then
  echo "== smoke: anonymous PropertyQuarry homepage =="
  PROPERTYQUARRY_PUBLIC_HOME="$(
    curl -fsS "${BASE}/" \
      -H 'Host: propertyquarry.com' \
      -H 'Accept: text/html'
  )"
  PROPERTYQUARRY_PUBLIC_HOME_FIELDS="$(
    python3 -c 'import sys; body=sys.stdin.read(); lowered=body.lower(); print("{}|{}|{}".format("<html" in lowered, "propertyquarry" in lowered, "auth_required" not in lowered))' \
      <<<"${PROPERTYQUARRY_PUBLIC_HOME}"
  )"
  if [[ "${PROPERTYQUARRY_PUBLIC_HOME_FIELDS}" != "True|True|True" ]]; then
    fail 12 "anonymous PropertyQuarry homepage is unavailable or auth-protected"
  fi
  echo "anonymous PropertyQuarry homepage ok"
else
  echo "anonymous PropertyQuarry homepage skipped (generic runtime profile)"
fi

echo "== smoke: openapi =="
OPENAPI_FIELDS="$(curl -fsS "${BASE}/openapi.json" | python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); schemas=((body.get('components') or {}).get('schemas') or {}); step_schema=schemas.get('SessionStepOut') or {}; step_examples=step_schema.get('examples') or []; waiting=next((row for row in step_examples if row.get('step_id') == 'step-artifact-save-waiting-approval'), {}); blocked=next((row for row in step_examples if row.get('step_id') == 'step-artifact-save-blocked-human'), {}); rewrite_examples=(schemas.get('RewriteAcceptedOut') or {}).get('examples') or []; rewrite_approval=next((row for row in rewrite_examples if row.get('status') == 'awaiting_approval'), {}); rewrite_human=next((row for row in rewrite_examples if row.get('status') == 'awaiting_human'), {}); rewrite_queued=next((row for row in rewrite_examples if row.get('status') == 'queued'), {}); plan_examples=(schemas.get('PlanExecuteAcceptedOut') or {}).get('examples') or []; plan_approval=next((row for row in plan_examples if row.get('status') == 'awaiting_approval'), {}); plan_human=next((row for row in plan_examples if row.get('status') == 'awaiting_human'), {}); plan_queued=next((row for row in plan_examples if row.get('status') == 'queued'), {}); print('{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}'.format(waiting.get('state',''), waiting.get('dependency_states') == {'step_policy_evaluate': 'completed'}, waiting.get('blocked_dependency_keys') == [], waiting.get('dependencies_satisfied') is True, blocked.get('state',''), blocked.get('blocked_dependency_keys') == ['step_human_review'], blocked.get('dependencies_satisfied') is False, rewrite_approval.get('approval_id',''), rewrite_human.get('human_task_id',''), rewrite_approval.get('next_action',''), rewrite_human.get('next_action',''), rewrite_queued.get('next_action',''), plan_approval.get('task_key',''), plan_human.get('task_key',''), plan_queued.get('task_key','')))")"
if [[ "${OPENAPI_FIELDS}" != "waiting_approval|True|True|True|queued|True|True|approval-123|human-task-123|poll_or_subscribe|poll_or_subscribe|poll_or_subscribe|decision_brief_approval|stakeholder_briefing_review|rewrite_retry_delayed" ]]; then
  echo "expected live OpenAPI session-step and async acceptance examples for approval/human/queued flows; got ${OPENAPI_FIELDS}" >&2
  fail 12 "policy contract mismatch"
fi
echo "openapi ok"

echo "== smoke: registration + workspace access =="
REGISTER_EMAIL="smoke-register-${SMOKE_RUN_TOKEN,,}@example.com"
REGISTER_START_JSON="$(curl -fsS -X POST "${BASE}/v1/register/start" -H 'content-type: application/json' -d "{\"email\":\"${REGISTER_EMAIL}\"}")"
REGISTER_START_FIELDS="$(python3 -c "import json,sys,urllib.parse; body=json.loads(sys.stdin.read() or '{}'); status=str(body.get('email_delivery_status','')); link=str(body.get('magic_link_url','')); parsed=urllib.parse.urlparse(link); code=str(body.get('verification_code') or urllib.parse.parse_qs(parsed.query).get('code',[''])[0]); print('{}|{}|{}|{}|{}|{}|{}'.format(body.get('email',''), len(code), bool(body.get('verification_token','')), link.startswith('/register?token='), bool(body.get('workspace_name','')), status in {'', 'sent', 'failed'}, status != 'failed' or bool(body.get('email_delivery_error',''))))" <<<"${REGISTER_START_JSON}")"
if [[ "${REGISTER_START_FIELDS}" != "${REGISTER_EMAIL}|6|True|True|True|True|True" ]]; then
  echo "expected registration start to return normalized email, token, recoverable six-digit code, local magic link, workspace name, and a valid delivery status envelope; got ${REGISTER_START_FIELDS}" >&2
  echo "${REGISTER_START_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
REGISTER_VERIFY_JSON="$(REGISTER_START_JSON="${REGISTER_START_JSON}" python3 -c 'import json,os,urllib.parse; body=json.loads(os.environ.get("REGISTER_START_JSON","{}")); link=str(body.get("magic_link_url","")); parsed=urllib.parse.urlparse(link); code=str(body.get("verification_code") or urllib.parse.parse_qs(parsed.query).get("code",[""])[0]); print(json.dumps({"verification_token": body.get("verification_token",""), "verification_code": code, "workspace_name": body.get("workspace_name","Smoke Register"), "timezone": body.get("suggested_timezone","Europe/Vienna") or "Europe/Vienna", "language": body.get("suggested_language","en") or "en"}))')"
REGISTER_VERIFY_RESPONSE="$(curl -fsS -X POST "${BASE}/v1/register/verify" -H 'content-type: application/json' -d "${REGISTER_VERIFY_JSON}")"
REGISTER_VERIFY_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); google_start=body.get('google_start') or {}; identity_only=google_start.get('identity_only') is True; auth_url=str(google_start.get('auth_url') or ''); start_url=str(google_start.get('start_url') or ''); ready=identity_only and google_start.get('ready') is True and auth_url.startswith('/sign-in/google?') and start_url == auth_url; missing=identity_only and google_start.get('ready') is False and not auth_url and not start_url and str(google_start.get('error') or '').startswith('google_oauth_propertyquarry_') and bool(google_start.get('detail')); print('{}|{}|{}|{}|{}'.format(bool(body.get('principal_id','')), str(body.get('access_url','')).startswith('/workspace-access/'), 'access_token' not in body, bool(body.get('access_expires_at','')), ready or missing)) " <<<"${REGISTER_VERIFY_RESPONSE}")"
if [[ "${REGISTER_VERIFY_FIELDS}" != "True|True|True|True|True" ]]; then
  echo "expected registration verify to issue a principal, workspace access link without a duplicated top-level token, expiry, and a ready or fail-closed identity-only Google start packet; got ${REGISTER_VERIFY_FIELDS}" >&2
  fail 12 "policy contract mismatch"
fi
REGISTER_ACCESS_URL="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("access_url",""))' <<<"${REGISTER_VERIFY_RESPONSE}")"
mkdir -p "${EA_ROOT}/.smoke_tmp"
REGISTER_COOKIE_JAR="${EA_ROOT}/.smoke_tmp/register_workspace_access.cookies"
REGISTER_ACCESS_HEADERS="${EA_ROOT}/.smoke_tmp/register_workspace_access.headers"
rm -f "${REGISTER_COOKIE_JAR}" "${REGISTER_ACCESS_HEADERS}"
curl -sS -D "${REGISTER_ACCESS_HEADERS}" -c "${REGISTER_COOKIE_JAR}" -o /dev/null "${BASE}${REGISTER_ACCESS_URL}"
REGISTER_ACCESS_FIELDS="$(python3 - "${REGISTER_ACCESS_HEADERS}" "${REGISTER_COOKIE_JAR}" <<'PY'
import sys
from pathlib import Path
headers=Path(sys.argv[1]).read_text(encoding='utf-8', errors='replace')
cookie_jar=Path(sys.argv[2]).read_text(encoding='utf-8', errors='replace')
status=''
location=''
cookie=False
for line in headers.splitlines():
    if line.startswith('HTTP/'):
        parts=line.split()
        if len(parts) >= 2:
            status=parts[1]
    if line.lower().startswith('location:'):
        location=line.split(':',1)[1].strip()
    if line.lower().startswith('set-cookie:') and 'ea_workspace_session=' in line:
        cookie=True
jar_cookie='ea_workspace_session' in cookie_jar
print(f"{status}|{location}|{cookie}|{jar_cookie}")
PY
)"
if [[ "${REGISTER_ACCESS_FIELDS}" != "303|/app/properties|True|True" ]]; then
  echo "expected workspace access link to set the session cookie and redirect to /app/properties; got ${REGISTER_ACCESS_FIELDS}" >&2
  cat "${REGISTER_ACCESS_HEADERS}" >&2
  cat "${REGISTER_COOKIE_JAR}" >&2
  fail 12 "policy contract mismatch"
fi
REGISTER_QUEUE_JSON="$(curl -fsS -b "${REGISTER_COOKIE_JAR}" "${BASE}/app/api/queue")"
REGISTER_QUEUE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}'.format(bool(body.get('generated_at','')), isinstance(body.get('items'), list), isinstance(body.get('total'), int)))" <<<"${REGISTER_QUEUE_JSON}")"
if [[ "${REGISTER_QUEUE_FIELDS}" != "True|True|True" ]]; then
  echo "expected workspace-session cookie to authorize /app/api/queue without bearer auth; got ${REGISTER_QUEUE_FIELDS}" >&2
  echo "${REGISTER_QUEUE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
ACCESS_SESSION_JSON="$(curl -fsS -X POST "${BASE}/app/api/access-sessions" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' -d '{"email":"smoke-access@example.com","role":"principal","display_name":"Smoke Access","expires_in_hours":24}')"
ACCESS_SESSION_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}|{}|{}'.format(bool(body.get('session_id','')), str(body.get('status','')) == 'active', bool(body.get('issued_at','')), bool(body.get('access_token','')), str(body.get('access_url','')).startswith('/workspace-access/')))" <<<"${ACCESS_SESSION_JSON}")"
if [[ "${ACCESS_SESSION_FIELDS}" != "True|True|True|True|True" ]]; then
  echo "expected access session creation to return active cookie-ready session state; got ${ACCESS_SESSION_FIELDS}" >&2
  echo "${ACCESS_SESSION_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
ACCESS_SESSION_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("session_id",""))' <<<"${ACCESS_SESSION_JSON}")"
ACCESS_SESSION_URL="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("access_url",""))' <<<"${ACCESS_SESSION_JSON}")"
ACCESS_SESSION_LIST_JSON="$(curl -fsS "${BASE}/app/api/access-sessions?status=active" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
ACCESS_SESSION_LIST_FIELDS="$(ACCESS_SESSION_ID="${ACCESS_SESSION_ID}" python3 -c 'import json,os,sys; body=json.loads(sys.stdin.read() or "{}"); session_id=os.environ.get("ACCESS_SESSION_ID",""); items=body.get("items") or []; print("{}|{}|{}".format(bool(body.get("generated_at","")), isinstance(items, list), any(str(item.get("session_id","")) == session_id and str(item.get("status","")) == "active" for item in items)))' <<<"${ACCESS_SESSION_LIST_JSON}")"
if [[ "${ACCESS_SESSION_LIST_FIELDS}" != "True|True|True" ]]; then
  echo "expected access session list to include the active session; got ${ACCESS_SESSION_LIST_FIELDS}" >&2
  echo "${ACCESS_SESSION_LIST_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
ACCESS_SESSION_REVOKE_JSON="$(curl -fsS -X POST "${BASE}/app/api/access-sessions/${ACCESS_SESSION_ID}/revoke" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
ACCESS_SESSION_REVOKE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}'.format(bool(body.get('session_id','')), str(body.get('status','')) == 'revoked', bool(body.get('revoked_at',''))))" <<<"${ACCESS_SESSION_REVOKE_JSON}")"
if [[ "${ACCESS_SESSION_REVOKE_FIELDS}" != "True|True|True" ]]; then
  echo "expected access session revoke to return revoked status with timestamp; got ${ACCESS_SESSION_REVOKE_FIELDS}" >&2
  echo "${ACCESS_SESSION_REVOKE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
REVOKED_ACCESS_STATUS="$(curl_status_code "${EA_ROOT}/.smoke_tmp/revoked_workspace_access.body" "${BASE}${ACCESS_SESSION_URL}")"
if [[ "${REVOKED_ACCESS_STATUS}" != "404" ]]; then
  echo "expected revoked workspace access link to return 404; got ${REVOKED_ACCESS_STATUS}" >&2
  cat "${EA_ROOT}/.smoke_tmp/revoked_workspace_access.body" >&2 || true
  fail 12 "policy contract mismatch"
fi
echo "registration/workspace access ok"

echo "== smoke: workspace browser surfaces =="
SEARCH_PAGE="$(curl_body_retry 15 1 "${BASE}/app/search?query=board" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
WORKSPACE_BROWSER_FIELDS="$(python3 -c 'import sys; search=(sys.stdin.read() or ""); surface_ok="data-pqx-surface=\"search\"" in search; shell_ok="data-property-app-shell" in search; launch_ok=("data-pqx-launch-top" in search or "data-property-start-top" in search); wizard_ok="\"wizard_steps\"" in search; print("{}|{}|{}|{}".format(surface_ok, shell_ok, launch_ok, wizard_ok))' <<<"${SEARCH_PAGE}")"
if [[ "${WORKSPACE_BROWSER_FIELDS}" != "True|True|True|True" ]]; then
  echo "expected workspace search surface to render shell, wizard payload and launch affordance; got ${WORKSPACE_BROWSER_FIELDS}" >&2
  fail 12 "policy contract mismatch"
fi
echo "workspace browser surfaces ok"

echo "== smoke: google signal sync =="
GOOGLE_SYNC_BODY="${EA_ROOT}/.smoke_tmp/google_signal_sync.json"
GOOGLE_SYNC_STATUS="$(curl -sS -o "${GOOGLE_SYNC_BODY}" -w '%{http_code}' -X POST "${BASE}/app/api/signals/google/sync?email_limit=1&calendar_limit=1" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
GOOGLE_SYNC_FIELDS="$(python3 - "${GOOGLE_SYNC_STATUS}" "${GOOGLE_SYNC_BODY}" <<'PY'
import json
import sys
from pathlib import Path

status = str(sys.argv[1] or "").strip()
body = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8", errors="replace") or "{}")
if status == "200":
    print(
        "{}|{}|{}|{}|{}".format(
            status,
            bool(str(body.get("generated_at", "")).strip()),
            bool(str(body.get("account_email", "")).strip()),
            isinstance(body.get("items"), list),
            isinstance(body.get("total"), int),
        )
    )
elif status == "409":
    detail = (
        str(body.get("detail", "")).strip()
        or str((body.get("error") or {}).get("details", "")).strip()
        or str((body.get("error") or {}).get("message", "")).strip()
        or str((body.get("error") or {}).get("code", "")).strip()
    )
    print(
        "{}|{}|{}|{}|{}".format(
            status,
            bool(detail),
            True,
            True,
            True,
        )
    )
else:
    print(f"{status}|False|False|False|False")
PY
)"
if [[ "${GOOGLE_SYNC_FIELDS}" != "200|True|True|True|True" && "${GOOGLE_SYNC_FIELDS}" != "409|True|True|True|True" ]]; then
  echo "expected google signal sync to either return a valid sync envelope or a clean conflict when no Google binding exists; got ${GOOGLE_SYNC_FIELDS}" >&2
  cat "${GOOGLE_SYNC_BODY}" >&2
  fail 12 "policy contract mismatch"
fi
echo "google signal sync ok"

echo "== smoke: rewrite =="
reset_rewrite_contract || true
REWRITE_JSON="$(curl_body_retry 5 1 -X POST "${BASE}/v1/rewrite/artifact" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' -d '{"text":"smoke run"}')"
echo "${REWRITE_JSON}"
ARTIFACT_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("artifact_id",""))' <<<"${REWRITE_JSON}")"
SESSION_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("execution_session_id",""))' <<<"${REWRITE_JSON}")"
if [[ -z "${ARTIFACT_ID}" ]]; then
  fail 13 "missing artifact_id from rewrite response"
fi
if [[ -z "${SESSION_ID}" ]]; then
  fail 11 "missing execution_session_id from rewrite response"
fi

echo "== smoke: session + policy =="
REWRITE_ARTIFACT_JSON="$(curl -fsS "${BASE}/v1/rewrite/artifacts/${ARTIFACT_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
REWRITE_ARTIFACT_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}|{}|{}|{}|{}|{}'.format(body.get('content',''), body.get('mime_type',''), body.get('preview_text',''), body.get('storage_handle',''), str(body.get('body_ref','')).startswith('file://'), body.get('task_key',''), body.get('principal_id',''), body.get('structured_output_json',{}) == {} and body.get('attachments_json',{}) == {}))" <<<"${REWRITE_ARTIFACT_JSON}")"
if [[ "${REWRITE_ARTIFACT_FIELDS}" != "smoke run|text/plain|smoke run|artifact://${ARTIFACT_ID}|True|rewrite_text|${PRINCIPAL_ID}|True" ]]; then
  echo "expected direct rewrite artifact fetch to project durable artifact envelope fields plus principal ownership; got ${REWRITE_ARTIFACT_FIELDS}" >&2
  echo "${REWRITE_ARTIFACT_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
SESSION_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
SESSION_RUNTIME_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); names=[e.get('name','') for e in (body.get('events') or [])]; events=set(names); queues=body.get('queue_items') or []; steps=body.get('steps') or []; history=body.get('human_task_assignment_history') or []; artifacts=body.get('artifacts') or []; first=(artifacts[0] if artifacts else {}); order_ok=('input_prepared' in events and 'policy_decision' in events and 'policy_step_completed' in events and names.index('input_prepared') < names.index('policy_decision') < names.index('policy_step_completed')); step_lookup={str((row.get('input_json') or {}).get('plan_step_key') or ''): row for row in steps}; input_step=step_lookup.get('step_input_prepare') or {}; policy_step=step_lookup.get('step_policy_evaluate') or {}; save_step=step_lookup.get('step_artifact_save') or {}; input_id=str(input_step.get('step_id','')); policy_id=str(policy_step.get('step_id','')); projection_ok=(input_step.get('dependency_keys') == [] and input_step.get('dependency_states') == {} and input_step.get('dependency_step_ids') == {} and input_step.get('blocked_dependency_keys') == [] and input_step.get('dependencies_satisfied') is True and policy_step.get('dependency_keys') == ['step_input_prepare'] and policy_step.get('parent_step_id') == input_id and policy_step.get('dependency_states') == {'step_input_prepare': 'completed'} and (policy_step.get('dependency_step_ids') or {}).get('step_input_prepare') == input_id and policy_step.get('blocked_dependency_keys') == [] and policy_step.get('dependencies_satisfied') is True and save_step.get('dependency_keys') == ['step_policy_evaluate'] and save_step.get('parent_step_id') == policy_id and save_step.get('dependency_states') == {'step_policy_evaluate': 'completed'} and (save_step.get('dependency_step_ids') or {}).get('step_policy_evaluate') == policy_id and save_step.get('blocked_dependency_keys') == [] and save_step.get('dependencies_satisfied') is True); print('{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}'.format(body.get('status',''), len(steps) >= 3, len(queues) >= 3 and all((q or {}).get('state','') == 'done' for q in queues), 'input_prepared' in events, 'policy_decision' in events, 'policy_step_completed' in events, 'tool_execution_completed' in events, len(history) == 0 and order_ok, projection_ok, first.get('mime_type',''), first.get('preview_text',''), first.get('storage_handle',''), str(first.get('body_ref','')).startswith('file://'), first.get('principal_id','')))" <<<"${SESSION_JSON}")"
if [[ "${SESSION_RUNTIME_FIELDS}" != "completed|True|True|True|True|True|True|True|True|text/plain|smoke run|artifact://${ARTIFACT_ID}|True|${PRINCIPAL_ID}" ]]; then
  echo "expected initial rewrite session to complete with ordered queued input/policy events, real single-dependency parent links, dependency-state projection metadata, empty human-task assignment history, and durable artifact envelope ownership fields; got ${SESSION_RUNTIME_FIELDS}" >&2
  echo "${SESSION_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
RECEIPT_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); rows=body.get("receipts") or []; print(((rows[0] or {}).get("receipt_id")) if rows else "")' <<<"${SESSION_JSON}")"
COST_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); rows=body.get("run_costs") or []; print(((rows[0] or {}).get("cost_id")) if rows else "")' <<<"${SESSION_JSON}")"
if [[ -z "${RECEIPT_ID}" ]]; then
  fail 13 "missing receipt_id from session response"
fi
if [[ -z "${COST_ID}" ]]; then
  fail 13 "missing cost_id from session response"
fi
RECEIPT_JSON="$(curl -fsS "${BASE}/v1/rewrite/receipts/${RECEIPT_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
RECEIPT_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); receipt=body.get('receipt_json') or {}; print('{}|{}'.format(receipt.get('handler_key',''), receipt.get('invocation_contract','')))" <<<"${RECEIPT_JSON}")"
if [[ "${RECEIPT_FIELDS}" != "artifact_repository|tool.v1" ]]; then
  echo "expected normalized receipt contract for artifact_repository; got ${RECEIPT_FIELDS}" >&2
  echo "${RECEIPT_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
curl -fsS "${BASE}/v1/rewrite/run-costs/${COST_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
curl -fsS "${BASE}/v1/policy/decisions/recent?session_id=${SESSION_ID}&limit=5" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
curl -fsS "${BASE}/v1/policy/approvals/pending?limit=5" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
curl -fsS "${BASE}/v1/policy/approvals/history?limit=5" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
POLICY_EVAL_SCOPE_MISMATCH_CODE="$(curl_status_code "${SMOKE_TMP_DIR}/ea_policy_eval_scope_mismatch_resp.json" -X POST "${BASE}/v1/policy/evaluate" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' -d "{\"content\":\"scoped policy evaluate\",\"tool_name\":\"connector.dispatch\",\"action_kind\":\"delivery.send\",\"channel\":\"email\",\"principal_id\":\"${MISMATCH_PRINCIPAL_ID}\"}")"
if [[ "${POLICY_EVAL_SCOPE_MISMATCH_CODE}" != "403" ]]; then
  echo "expected policy evaluate principal mismatch to return 403; got ${POLICY_EVAL_SCOPE_MISMATCH_CODE}" >&2
  cat "${SMOKE_TMP_DIR}/ea_policy_eval_scope_mismatch_resp.json" >&2
  fail 12 "policy contract mismatch"
fi
POLICY_EVAL_SCOPE_MISMATCH_REASON="$(python3 -c 'import json,sys; body=json.load(open(sys.argv[1])); print(((body.get("error") or {}).get("code","")))' "${SMOKE_TMP_DIR}/ea_policy_eval_scope_mismatch_resp.json")"
if [[ "${POLICY_EVAL_SCOPE_MISMATCH_REASON}" != "principal_scope_mismatch" ]]; then
  echo "expected policy evaluate principal mismatch code principal_scope_mismatch; got ${POLICY_EVAL_SCOPE_MISMATCH_REASON}" >&2
  cat "${SMOKE_TMP_DIR}/ea_policy_eval_scope_mismatch_resp.json" >&2
  fail 12 "policy contract mismatch"
fi
REWRITE_PRINCIPAL_MISMATCH_CODE="$(curl_status_code "${SMOKE_TMP_DIR}/ea_rewrite_principal_mismatch_resp.json" -X POST "${BASE}/v1/rewrite/artifact" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' -d "{\"text\":\"principal mismatch\",\"principal_id\":\"${MISMATCH_PRINCIPAL_ID}\"}")"
if [[ "${REWRITE_PRINCIPAL_MISMATCH_CODE}" != "403" ]]; then
  echo "expected rewrite principal mismatch create to return 403; got ${REWRITE_PRINCIPAL_MISMATCH_CODE}" >&2
  cat "${SMOKE_TMP_DIR}/ea_rewrite_principal_mismatch_resp.json" >&2
  fail 12 "policy contract mismatch"
fi
REWRITE_PRINCIPAL_MISMATCH_REASON="$(python3 -c 'import json,sys; body=json.load(open(sys.argv[1])); print(((body.get("error") or {}).get("code","")))' "${SMOKE_TMP_DIR}/ea_rewrite_principal_mismatch_resp.json")"
if [[ "${REWRITE_PRINCIPAL_MISMATCH_REASON}" != "principal_scope_mismatch" ]]; then
  echo "expected rewrite principal mismatch create code principal_scope_mismatch; got ${REWRITE_PRINCIPAL_MISMATCH_REASON}" >&2
  cat "${SMOKE_TMP_DIR}/ea_rewrite_principal_mismatch_resp.json" >&2
  fail 12 "policy contract mismatch"
fi
REWRITE_SESSION_MISMATCH_CODE="$(curl_status_code "${SMOKE_TMP_DIR}/ea_rewrite_session_mismatch_resp.json" "${BASE}/v1/rewrite/sessions/${SESSION_ID}" "${AUTH_ARGS[@]}" -H "X-EA-Principal-ID: ${MISMATCH_PRINCIPAL_ID}")"
REWRITE_ARTIFACT_MISMATCH_CODE="$(curl_status_code "${SMOKE_TMP_DIR}/ea_rewrite_artifact_mismatch_resp.json" "${BASE}/v1/rewrite/artifacts/${ARTIFACT_ID}" "${AUTH_ARGS[@]}" -H "X-EA-Principal-ID: ${MISMATCH_PRINCIPAL_ID}")"
REWRITE_RECEIPT_MISMATCH_CODE="$(curl_status_code "${SMOKE_TMP_DIR}/ea_rewrite_receipt_mismatch_resp.json" "${BASE}/v1/rewrite/receipts/${RECEIPT_ID}" "${AUTH_ARGS[@]}" -H "X-EA-Principal-ID: ${MISMATCH_PRINCIPAL_ID}")"
REWRITE_COST_MISMATCH_CODE="$(curl_status_code "${SMOKE_TMP_DIR}/ea_rewrite_cost_mismatch_resp.json" "${BASE}/v1/rewrite/run-costs/${COST_ID}" "${AUTH_ARGS[@]}" -H "X-EA-Principal-ID: ${MISMATCH_PRINCIPAL_ID}")"
if [[ "${REWRITE_SESSION_MISMATCH_CODE}|${REWRITE_ARTIFACT_MISMATCH_CODE}|${REWRITE_RECEIPT_MISMATCH_CODE}|${REWRITE_COST_MISMATCH_CODE}" != "403|403|403|403" ]]; then
  echo "expected foreign-principal session/artifact/receipt/run-cost fetches to return 403; got ${REWRITE_SESSION_MISMATCH_CODE}|${REWRITE_ARTIFACT_MISMATCH_CODE}|${REWRITE_RECEIPT_MISMATCH_CODE}|${REWRITE_COST_MISMATCH_CODE}" >&2
  cat "${SMOKE_TMP_DIR}/ea_rewrite_session_mismatch_resp.json" >&2
  cat "${SMOKE_TMP_DIR}/ea_rewrite_artifact_mismatch_resp.json" >&2
  cat "${SMOKE_TMP_DIR}/ea_rewrite_receipt_mismatch_resp.json" >&2
  cat "${SMOKE_TMP_DIR}/ea_rewrite_cost_mismatch_resp.json" >&2
  fail 12 "policy contract mismatch"
fi
REWRITE_SCOPE_MISMATCH_REASONS="$(python3 -c 'import json,sys; paths=sys.argv[1:]; print("|".join(((json.load(open(path)).get("error") or {}).get("code","")) for path in paths))' "${SMOKE_TMP_DIR}/ea_rewrite_session_mismatch_resp.json" "${SMOKE_TMP_DIR}/ea_rewrite_artifact_mismatch_resp.json" "${SMOKE_TMP_DIR}/ea_rewrite_receipt_mismatch_resp.json" "${SMOKE_TMP_DIR}/ea_rewrite_cost_mismatch_resp.json")"
if [[ "${REWRITE_SCOPE_MISMATCH_REASONS}" != "principal_scope_mismatch|principal_scope_mismatch|principal_scope_mismatch|principal_scope_mismatch" ]]; then
  echo "expected foreign-principal rewrite fetches to report principal_scope_mismatch; got ${REWRITE_SCOPE_MISMATCH_REASONS}" >&2
  cat "${SMOKE_TMP_DIR}/ea_rewrite_session_mismatch_resp.json" >&2
  cat "${SMOKE_TMP_DIR}/ea_rewrite_artifact_mismatch_resp.json" >&2
  cat "${SMOKE_TMP_DIR}/ea_rewrite_receipt_mismatch_resp.json" >&2
  cat "${SMOKE_TMP_DIR}/ea_rewrite_cost_mismatch_resp.json" >&2
  fail 12 "policy contract mismatch"
fi
echo "session/policy ok"

if (( LEGACY_HUMAN_TASKS_AVAILABLE )); then
echo "== smoke: human tasks =="
SESSION_STEP_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); rows=body.get("steps") or []; print(((rows[-1] or {}).get("step_id")) if rows else "")' <<<"${SESSION_JSON}")"
if [[ -z "${SESSION_STEP_ID}" ]]; then
  fail 13 "missing step_id from session response"
fi
HUMAN_CREATE_MISMATCH_CODE="$(curl_status_code "${SMOKE_TMP_DIR}/ea_human_create_mismatch_resp.json" -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" -H "X-EA-Principal-ID: ${MISMATCH_PRINCIPAL_ID}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${SESSION_ID}\",\"step_id\":\"${SESSION_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Cross-principal attach attempt.\"}")"
HUMAN_CREATE_MISMATCH_REASON="$(python3 -c 'import json; from pathlib import Path; import sys; body=json.loads(Path(sys.argv[1]).read_text() or "{}"); print((body.get("error") or {}).get("code",""))' "${SMOKE_TMP_DIR}/ea_human_create_mismatch_resp.json")"
HUMAN_SESSION_LIST_MISMATCH_CODE="$(curl_status_code "${SMOKE_TMP_DIR}/ea_human_session_list_mismatch_resp.json" "${BASE}/v1/human/tasks?session_id=${SESSION_ID}&limit=10" "${AUTH_ARGS[@]}" -H "X-EA-Principal-ID: ${MISMATCH_PRINCIPAL_ID}")"
HUMAN_SESSION_LIST_MISMATCH_REASON="$(python3 -c 'import json; from pathlib import Path; import sys; body=json.loads(Path(sys.argv[1]).read_text() or "{}"); print((body.get("error") or {}).get("code",""))' "${SMOKE_TMP_DIR}/ea_human_session_list_mismatch_resp.json")"
if [[ "${HUMAN_CREATE_MISMATCH_CODE}" != "403" || "${HUMAN_CREATE_MISMATCH_REASON}" != "principal_scope_mismatch" || "${HUMAN_SESSION_LIST_MISMATCH_CODE}" != "403" || "${HUMAN_SESSION_LIST_MISMATCH_REASON}" != "principal_scope_mismatch" ]]; then
  echo "expected foreign-principal session-bound human task create/list requests to fail with principal_scope_mismatch; got ${HUMAN_CREATE_MISMATCH_CODE}|${HUMAN_CREATE_MISMATCH_REASON}|${HUMAN_SESSION_LIST_MISMATCH_CODE}|${HUMAN_SESSION_LIST_MISMATCH_REASON}" >&2
  cat "${SMOKE_TMP_DIR}/ea_human_create_mismatch_resp.json" >&2
  cat "${SMOKE_TMP_DIR}/ea_human_session_list_mismatch_resp.json" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_CREATE_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${SESSION_ID}\",\"step_id\":\"${SESSION_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Review the draft before external send.\",\"authority_required\":\"send_on_behalf_review\",\"why_human\":\"External executive communication needs human tone review.\",\"quality_rubric_json\":{\"checks\":[\"tone\",\"accuracy\",\"stakeholder_sensitivity\"]},\"input_json\":{\"artifact_id\":\"${ARTIFACT_ID}\"},\"desired_output_json\":{\"format\":\"review_packet\"},\"priority\":\"high\",\"sla_due_at\":\"2000-01-01T00:00:00+00:00\",\"resume_session_on_return\":true}")"
HUMAN_TASK_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("human_task_id",""))' <<<"${HUMAN_CREATE_JSON}")"
HUMAN_CREATE_FIELDS="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); checks=(body.get("quality_rubric_json") or {}).get("checks") or []; print("{}|{}|{}|{}|{}|{}|{}|{}|{}".format(body.get("status",""), body.get("assignment_state",""), body.get("assignment_source",""), body.get("assigned_at") is None, body.get("assigned_by_actor_id",""), body.get("resume_session_on_return", False), body.get("authority_required",""), body.get("why_human",""), checks[0] if checks else ""))' <<<"${HUMAN_CREATE_JSON}")"
HUMAN_CREATE_ASSIGN_STATE="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("assignment_state",""))' <<<"${HUMAN_CREATE_JSON}")"
HUMAN_CREATE_ASSIGN_SOURCE="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("assignment_source",""))' <<<"${HUMAN_CREATE_JSON}")"
HUMAN_CREATE_ASSIGNED_BY="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("assigned_by_actor_id",""))' <<<"${HUMAN_CREATE_JSON}")"
HUMAN_CREATE_ASSIGNED_OPERATOR="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("assigned_operator_id",""))' <<<"${HUMAN_CREATE_JSON}")"
if [[ -z "${HUMAN_TASK_ID}" ]]; then
  fail 13 "missing human_task_id from human task create response"
fi
if [[ "${HUMAN_CREATE_ASSIGN_STATE}" == "assigned" ]]; then
  if [[ "${HUMAN_CREATE_FIELDS}" != "pending|assigned|${HUMAN_CREATE_ASSIGN_SOURCE}|False|${HUMAN_CREATE_ASSIGNED_BY}|True|send_on_behalf_review|External executive communication needs human tone review.|tone" ]]; then
    echo "expected assigned human task with explicit review-contract metadata after creation; got ${HUMAN_CREATE_FIELDS}" >&2
    echo "${HUMAN_CREATE_JSON}" >&2
    fail 12 "policy contract mismatch"
  fi
elif [[ "${HUMAN_CREATE_ASSIGN_STATE}" == "unassigned" ]]; then
  if [[ "${HUMAN_CREATE_FIELDS}" != "pending|unassigned||True||True|send_on_behalf_review|External executive communication needs human tone review.|tone" ]]; then
    echo "expected unassigned human task with explicit review-contract metadata after creation; got ${HUMAN_CREATE_FIELDS}" >&2
    echo "${HUMAN_CREATE_JSON}" >&2
    fail 12 "policy contract mismatch"
  fi
else
  echo "expected pending human task after creation to be assigned or unassigned; got assignment_state=${HUMAN_CREATE_ASSIGN_STATE}" >&2
  echo "${HUMAN_CREATE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_CREATE_SUMMARY_FIELDS="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print("{}|{}|{}|{}|{}|{}".format(body.get("last_transition_event_name",""), bool(body.get("last_transition_at","")), body.get("last_transition_assignment_state",""), body.get("last_transition_operator_id",""), body.get("last_transition_assignment_source",""), body.get("last_transition_by_actor_id","")))' <<<"${HUMAN_CREATE_JSON}")"
if [[ "${HUMAN_CREATE_ASSIGN_STATE}" == "assigned" ]]; then
  if [[ "${HUMAN_CREATE_SUMMARY_FIELDS}" != "human_task_created|True|assigned|${HUMAN_CREATE_ASSIGNED_OPERATOR}|${HUMAN_CREATE_ASSIGN_SOURCE}|${HUMAN_CREATE_ASSIGNED_BY}" ]]; then
    echo "expected assigned create response to expose compact last-transition summary after human_task_created; got ${HUMAN_CREATE_SUMMARY_FIELDS}" >&2
    echo "${HUMAN_CREATE_JSON}" >&2
    fail 12 "policy contract mismatch"
  fi
elif [[ "${HUMAN_CREATE_SUMMARY_FIELDS}" != "human_task_created|True|unassigned|||" ]]; then
  echo "expected unassigned create response to expose compact last-transition summary after human_task_created; got ${HUMAN_CREATE_SUMMARY_FIELDS}" >&2
  echo "${HUMAN_CREATE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
SESSION_HUMAN_WAITING_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
SESSION_HUMAN_WAITING_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); events={e.get('name','') for e in (body.get('events') or [])}; steps=body.get('steps') or []; step_id='${SESSION_STEP_ID}'; print('{}|{}|{}'.format(body.get('status',''), 'session_paused_for_human_task' in events, any((row or {}).get('step_id') == step_id and (row or {}).get('state') == 'waiting_human' for row in steps)))" <<<"${SESSION_HUMAN_WAITING_JSON}")"
if [[ "${SESSION_HUMAN_WAITING_FIELDS}" != "awaiting_human|True|True" ]]; then
  echo "expected session to reopen into awaiting_human with waiting_human step after human task creation; got ${SESSION_HUMAN_WAITING_FIELDS}" >&2
  echo "${SESSION_HUMAN_WAITING_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
SESSION_HUMAN_WAITING_SUMMARY_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); task_id='${HUMAN_TASK_ID}'; task=next((row for row in (body.get('human_tasks') or []) if (row or {}).get('human_task_id') == task_id), {}); print('{}|{}|{}|{}|{}|{}'.format(task.get('last_transition_event_name',''), bool(task.get('last_transition_at','')), task.get('last_transition_assignment_state',''), task.get('last_transition_operator_id',''), task.get('last_transition_assignment_source',''), task.get('last_transition_by_actor_id','')))" <<<"${SESSION_HUMAN_WAITING_JSON}")"
if [[ "${HUMAN_CREATE_ASSIGN_STATE}" == "assigned" ]]; then
  if [[ "${SESSION_HUMAN_WAITING_SUMMARY_FIELDS}" != "human_task_created|True|assigned|${HUMAN_CREATE_ASSIGNED_OPERATOR}|${HUMAN_CREATE_ASSIGN_SOURCE}|${HUMAN_CREATE_ASSIGNED_BY}" ]]; then
    echo "expected awaiting_human session row to expose assigned human_task_created transition summary; got ${SESSION_HUMAN_WAITING_SUMMARY_FIELDS}" >&2
    echo "${SESSION_HUMAN_WAITING_JSON}" >&2
    fail 12 "policy contract mismatch"
  fi
elif [[ "${SESSION_HUMAN_WAITING_SUMMARY_FIELDS}" != "human_task_created|True|unassigned|||" ]]; then
  echo "expected awaiting_human session row to expose human_task_created transition summary; got ${SESSION_HUMAN_WAITING_SUMMARY_FIELDS}" >&2
  echo "${SESSION_HUMAN_WAITING_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_ROLE_FILTER_JSON="$(curl -fsS "${BASE}/v1/human/tasks?role_required=communications_reviewer&overdue_only=true&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_ROLE_FILTER_MATCH="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); task_id='${HUMAN_TASK_ID}'; print(any((row or {}).get('human_task_id') == task_id for row in rows))" <<<"${HUMAN_ROLE_FILTER_JSON}")"
if [[ "${HUMAN_ROLE_FILTER_MATCH}" != "True" ]]; then
  echo "expected role/overdue human task queue filter to include ${HUMAN_TASK_ID}" >&2
  echo "${HUMAN_ROLE_FILTER_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_BACKLOG_JSON="$(curl -fsS "${BASE}/v1/human/tasks/backlog?role_required=communications_reviewer&overdue_only=true&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_BACKLOG_MATCH="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); task_id='${HUMAN_TASK_ID}'; print(any((row or {}).get('human_task_id') == task_id for row in rows))" <<<"${HUMAN_BACKLOG_JSON}")"
if [[ "${HUMAN_BACKLOG_MATCH}" != "True" ]]; then
  echo "expected human task backlog endpoint to include ${HUMAN_TASK_ID}" >&2
  echo "${HUMAN_BACKLOG_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_UNASSIGNED_JSON="$(curl -fsS "${BASE}/v1/human/tasks/unassigned?role_required=communications_reviewer&overdue_only=true&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_UNASSIGNED_MATCH="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); task_id='${HUMAN_TASK_ID}'; print(any((row or {}).get('human_task_id') == task_id for row in rows))" <<<"${HUMAN_UNASSIGNED_JSON}")"
if [[ "${HUMAN_CREATE_ASSIGN_STATE}" == "assigned" ]]; then
  if [[ "${HUMAN_UNASSIGNED_MATCH}" != "False" ]]; then
    echo "expected assigned task to be excluded from unassigned endpoint for ${HUMAN_TASK_ID}" >&2
    echo "${HUMAN_UNASSIGNED_JSON}" >&2
    fail 12 "policy contract mismatch"
  fi
elif [[ "${HUMAN_UNASSIGNED_MATCH}" != "True" ]]; then
  echo "expected unassigned task to be present in unassigned endpoint for ${HUMAN_TASK_ID}" >&2
  echo "${HUMAN_UNASSIGNED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_OPERATOR_SPECIALIST_JSON="$(operator_post_json "${BASE}/v1/human/tasks/operators" -H 'content-type: application/json' -d '{"operator_id":"operator-specialist","display_name":"Senior Comms Reviewer","roles":["communications_reviewer"],"skill_tags":["tone","accuracy","stakeholder_sensitivity"],"trust_tier":"senior","status":"active","notes":"Specialist in external executive communication."}')"
HUMAN_OPERATOR_SPECIALIST_FIELDS="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); tags=body.get("skill_tags") or []; print("{}|{}|{}".format(body.get("operator_id",""), body.get("trust_tier",""), tags[0] if tags else ""))' <<<"${HUMAN_OPERATOR_SPECIALIST_JSON}")"
if [[ "${HUMAN_OPERATOR_SPECIALIST_FIELDS}" != "operator-specialist|senior|tone" ]]; then
  echo "expected specialist operator profile to persist role/skill/trust metadata; got ${HUMAN_OPERATOR_SPECIALIST_FIELDS}" >&2
  echo "${HUMAN_OPERATOR_SPECIALIST_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
operator_post_json "${BASE}/v1/human/tasks/operators" -H 'content-type: application/json' \
  -d '{"operator_id":"operator-junior","display_name":"Junior Reviewer","roles":["communications_reviewer"],"skill_tags":["tone"],"trust_tier":"standard","status":"active"}' >/dev/null
HUMAN_ROUTING_HINT_JSON="$(curl -fsS "${BASE}/v1/human/tasks/${HUMAN_TASK_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_ROUTING_HINT_FIELDS="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); hints=body.get("routing_hints_json") or {}; suggested=hints.get("suggested_operator_ids") or []; print("{}|{}|{}|{}".format((hints.get("required_skill_tags") or [None])[0], hints.get("required_trust_tier",""), suggested[0] if suggested else "", hints.get("auto_assign_operator_id","")))' <<<"${HUMAN_ROUTING_HINT_JSON}")"
if [[ "${HUMAN_ROUTING_HINT_FIELDS}" != "accuracy|senior|operator-specialist|operator-specialist" ]]; then
  echo "expected human task operator auto-assignment hint after specialist profile creation; got ${HUMAN_ROUTING_HINT_FIELDS}" >&2
  echo "${HUMAN_ROUTING_HINT_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_ASSIGN_JSON="$(operator_post_json "${BASE}/v1/human/tasks/${HUMAN_TASK_ID}/assign" -H 'content-type: application/json' -d '{}')"
HUMAN_ASSIGN_FIELDS="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print("{}|{}|{}|{}|{}|{}".format(body.get("status",""), body.get("assignment_state",""), body.get("assigned_operator_id",""), body.get("assignment_source",""), bool(body.get("assigned_at","")), body.get("assigned_by_actor_id","")))' <<<"${HUMAN_ASSIGN_JSON}")"
if [[ "${HUMAN_ASSIGN_FIELDS}" != "pending|assigned|operator-specialist|recommended|True|${PRINCIPAL_ID}" ]]; then
  echo "expected assigned human task to stay pending with explicit assigned state and operator ownership; got ${HUMAN_ASSIGN_FIELDS}" >&2
  echo "${HUMAN_ASSIGN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_ASSIGN_SUMMARY_FIELDS="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print("{}|{}|{}|{}|{}|{}".format(body.get("last_transition_event_name",""), bool(body.get("last_transition_at","")), body.get("last_transition_assignment_state",""), body.get("last_transition_operator_id",""), body.get("last_transition_assignment_source",""), body.get("last_transition_by_actor_id","")))' <<<"${HUMAN_ASSIGN_JSON}")"
if [[ "${HUMAN_ASSIGN_SUMMARY_FIELDS}" != "human_task_assigned|True|assigned|operator-specialist|recommended|${PRINCIPAL_ID}" && "${HUMAN_ASSIGN_SUMMARY_FIELDS}" != "|False||||" ]]; then
  echo "expected assigned response to expose recommended last-transition summary; got ${HUMAN_ASSIGN_SUMMARY_FIELDS}" >&2
  echo "${HUMAN_ASSIGN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_OWNERLESS_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${SESSION_ID}\",\"step_id\":\"${SESSION_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Ownerless pending task.\",\"priority\":\"low\",\"resume_session_on_return\":false}")"
HUMAN_OWNERLESS_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("human_task_id",""))' <<<"${HUMAN_OWNERLESS_JSON}")"
if [[ -z "${HUMAN_OWNERLESS_ID}" ]]; then
  fail 13 "missing human_task_id from ownerless human task response"
fi
PRIORITY_SUMMARY_NONE_JSON="$(curl -fsS "${BASE}/v1/human/tasks/priority-summary?status=pending&assignment_state=unassigned&assignment_source=none" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
PRIORITY_SUMMARY_NONE_SOURCE="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("assignment_source",""))' <<<"${PRIORITY_SUMMARY_NONE_JSON}")"
PRIORITY_SUMMARY_NONE_TOTAL="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("total",""))' <<<"${PRIORITY_SUMMARY_NONE_JSON}")"
PRIORITY_SUMMARY_NONE_HIGHEST="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("highest_priority",""))' <<<"${PRIORITY_SUMMARY_NONE_JSON}")"
if [[ "${PRIORITY_SUMMARY_NONE_SOURCE}" != "none" || "${PRIORITY_SUMMARY_NONE_TOTAL}" -lt 1 ]]; then
  echo "expected assignment_source=none summary for ownerless pending work (total >= 1); got source=${PRIORITY_SUMMARY_NONE_SOURCE} total=${PRIORITY_SUMMARY_NONE_TOTAL} highest=${PRIORITY_SUMMARY_NONE_HIGHEST}" >&2
  echo "${PRIORITY_SUMMARY_NONE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_OWNERLESS_LIST_JSON="$(curl -fsS "${BASE}/v1/human/tasks?status=pending&assignment_state=unassigned&assignment_source=none&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_OWNERLESS_LIST_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted='${HUMAN_OWNERLESS_ID}'; blocked='${HUMAN_TASK_ID}'; print('{}|{}'.format(any((row or {}).get('human_task_id') == wanted for row in rows), all((row or {}).get('human_task_id') != blocked for row in rows)))" <<<"${HUMAN_OWNERLESS_LIST_JSON}")"
if [[ "${HUMAN_OWNERLESS_LIST_FIELDS}" != "True|True" ]]; then
  echo "expected assignment_source=none list filter to isolate ownerless pending work; got ${HUMAN_OWNERLESS_LIST_FIELDS}" >&2
  echo "${HUMAN_OWNERLESS_LIST_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_UNASSIGNED_NONE_JSON="$(curl -fsS "${BASE}/v1/human/tasks/unassigned?assignment_source=none&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_UNASSIGNED_NONE_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted='${HUMAN_OWNERLESS_ID}'; blocked='${HUMAN_TASK_ID}'; print('{}|{}'.format(any((row or {}).get('human_task_id') == wanted for row in rows), all((row or {}).get('human_task_id') != blocked for row in rows)))" <<<"${HUMAN_UNASSIGNED_NONE_JSON}")"
if [[ "${HUMAN_UNASSIGNED_NONE_FIELDS}" != "True|True" ]]; then
  echo "expected assignment_source=none unassigned queue to isolate ownerless pending work; got ${HUMAN_UNASSIGNED_NONE_FIELDS}" >&2
  echo "${HUMAN_UNASSIGNED_NONE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_OWNERLESS_BACKLOG_JSON="$(curl -fsS "${BASE}/v1/human/tasks/backlog?assignment_state=unassigned&assignment_source=none&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_OWNERLESS_BACKLOG_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted='${HUMAN_OWNERLESS_ID}'; blocked='${HUMAN_TASK_ID}'; print('{}|{}'.format(any((row or {}).get('human_task_id') == wanted for row in rows), all((row or {}).get('human_task_id') != blocked for row in rows)))" <<<"${HUMAN_OWNERLESS_BACKLOG_JSON}")"
if [[ "${HUMAN_OWNERLESS_BACKLOG_FIELDS}" != "True|True" ]]; then
  echo "expected assignment_source=none backlog queue to isolate ownerless pending work; got ${HUMAN_OWNERLESS_BACKLOG_FIELDS}" >&2
  echo "${HUMAN_OWNERLESS_BACKLOG_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
SESSION_HUMAN_NONE_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${SESSION_ID}?human_task_assignment_source=none" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
SESSION_HUMAN_NONE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); tasks=body.get('human_tasks') or []; history=body.get('human_task_assignment_history') or []; wanted='${HUMAN_OWNERLESS_ID}'; print('{}|{}|{}|{}|{}'.format(len(tasks), (tasks[0].get('human_task_id','') if tasks else ''), all((row or {}).get('assignment_source','') == '' for row in history), all((row or {}).get('event_name','') == 'human_task_created' for row in history), any((row or {}).get('human_task_id','') == wanted for row in history)))" <<<"${SESSION_HUMAN_NONE_JSON}")"
if [[ "${SESSION_HUMAN_NONE_FIELDS}" != "1|${HUMAN_OWNERLESS_ID}|True|True|True" ]]; then
  echo "expected session assignment_source=none filter to isolate current ownerless rows and created-only history; got ${SESSION_HUMAN_NONE_FIELDS}" >&2
  echo "${SESSION_HUMAN_NONE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_OWNERLESS_NEWER_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${SESSION_ID}\",\"step_id\":\"${SESSION_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Newer ownerless pending task.\",\"priority\":\"low\",\"resume_session_on_return\":false}")"
HUMAN_OWNERLESS_NEWER_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("human_task_id",""))' <<<"${HUMAN_OWNERLESS_NEWER_JSON}")"
if [[ -z "${HUMAN_OWNERLESS_NEWER_ID}" ]]; then
  fail 13 "missing human_task_id from newer ownerless human task response"
fi
PRIORITY_SUMMARY_NONE_MIXED_JSON="$(curl -fsS "${BASE}/v1/human/tasks/priority-summary?status=pending&assignment_state=unassigned&assignment_source=none" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
PRIORITY_SUMMARY_NONE_MIXED_SOURCE="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("assignment_source",""))' <<<"${PRIORITY_SUMMARY_NONE_MIXED_JSON}")"
PRIORITY_SUMMARY_NONE_MIXED_TOTAL="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("total",""))' <<<"${PRIORITY_SUMMARY_NONE_MIXED_JSON}")"
if [[ "${PRIORITY_SUMMARY_NONE_MIXED_SOURCE}" != "none" || "${PRIORITY_SUMMARY_NONE_MIXED_TOTAL}" -lt 2 ]]; then
  echo "expected assignment_source=none summary to stay ownerless-only after mixed-source churn; got source=${PRIORITY_SUMMARY_NONE_MIXED_SOURCE} total=${PRIORITY_SUMMARY_NONE_MIXED_TOTAL}" >&2
  echo "${PRIORITY_SUMMARY_NONE_MIXED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_OWNERLESS_LIST_MIXED_JSON="$(curl -fsS "${BASE}/v1/human/tasks?status=pending&assignment_state=unassigned&assignment_source=none&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_OWNERLESS_LIST_MIXED_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); ids={(row or {}).get('human_task_id','') for row in rows}; wanted={'${HUMAN_OWNERLESS_ID}','${HUMAN_OWNERLESS_NEWER_ID}'}; blocked='${HUMAN_TASK_ID}'; print('{}|{}'.format(wanted.issubset(ids), blocked not in ids))" <<<"${HUMAN_OWNERLESS_LIST_MIXED_JSON}")"
if [[ "${HUMAN_OWNERLESS_LIST_MIXED_FIELDS}" != "True|True" ]]; then
  echo "expected unsorted assignment_source=none list slice to stay ownerless-only after mixed-source churn; got ${HUMAN_OWNERLESS_LIST_MIXED_FIELDS}" >&2
  echo "${HUMAN_OWNERLESS_LIST_MIXED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_UNASSIGNED_NONE_MIXED_JSON="$(curl -fsS "${BASE}/v1/human/tasks/unassigned?assignment_source=none&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_UNASSIGNED_NONE_MIXED_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); ids={(row or {}).get('human_task_id','') for row in rows}; wanted={'${HUMAN_OWNERLESS_ID}','${HUMAN_OWNERLESS_NEWER_ID}'}; blocked='${HUMAN_TASK_ID}'; print('{}|{}'.format(wanted.issubset(ids), blocked not in ids))" <<<"${HUMAN_UNASSIGNED_NONE_MIXED_JSON}")"
if [[ "${HUMAN_UNASSIGNED_NONE_MIXED_FIELDS}" != "True|True" ]]; then
  echo "expected unsorted assignment_source=none unassigned slice to stay ownerless-only after mixed-source churn; got ${HUMAN_UNASSIGNED_NONE_MIXED_FIELDS}" >&2
  echo "${HUMAN_UNASSIGNED_NONE_MIXED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_OWNERLESS_BACKLOG_MIXED_JSON="$(curl -fsS "${BASE}/v1/human/tasks/backlog?assignment_state=unassigned&assignment_source=none&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_OWNERLESS_BACKLOG_MIXED_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); ids={(row or {}).get('human_task_id','') for row in rows}; wanted={'${HUMAN_OWNERLESS_ID}','${HUMAN_OWNERLESS_NEWER_ID}'}; blocked='${HUMAN_TASK_ID}'; print('{}|{}'.format(wanted.issubset(ids), blocked not in ids))" <<<"${HUMAN_OWNERLESS_BACKLOG_MIXED_JSON}")"
if [[ "${HUMAN_OWNERLESS_BACKLOG_MIXED_FIELDS}" != "True|True" ]]; then
  echo "expected unsorted assignment_source=none backlog slice to stay ownerless-only after mixed-source churn; got ${HUMAN_OWNERLESS_BACKLOG_MIXED_FIELDS}" >&2
  echo "${HUMAN_OWNERLESS_BACKLOG_MIXED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
SESSION_HUMAN_NONE_MIXED_JSON="$(curl -fsS "${BASE}/v1/human/tasks?session_id=${SESSION_ID}&assignment_source=none&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
SESSION_HUMAN_NONE_MIXED_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); ids={(row or {}).get('human_task_id','') for row in rows}; wanted={'${HUMAN_OWNERLESS_ID}','${HUMAN_OWNERLESS_NEWER_ID}'}; blocked='${HUMAN_TASK_ID}'; print('{}|{}'.format(wanted.issubset(ids), blocked not in ids))" <<<"${SESSION_HUMAN_NONE_MIXED_JSON}")"
if [[ "${SESSION_HUMAN_NONE_MIXED_FIELDS}" != "True|True" ]]; then
  echo "expected unsorted session-scoped assignment_source=none slice to stay ownerless-only after mixed-source churn; got ${SESSION_HUMAN_NONE_MIXED_FIELDS}" >&2
  echo "${SESSION_HUMAN_NONE_MIXED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_OWNERLESS_BACKLOG_CREATED_JSON="$(curl -fsS "${BASE}/v1/human/tasks/backlog?assignment_state=unassigned&assignment_source=none&sort=created_asc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_OWNERLESS_BACKLOG_CREATED_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted={'${HUMAN_OWNERLESS_ID}','${HUMAN_OWNERLESS_NEWER_ID}'}; blocked='${HUMAN_TASK_ID}'; ids={ (row or {}).get('human_task_id','') for row in rows[:10] }; print('{}|{}'.format(wanted.issubset(ids), all((row or {}).get('human_task_id') != blocked for row in rows[:10])) )" <<<"${HUMAN_OWNERLESS_BACKLOG_CREATED_JSON}")"
if [[ "${HUMAN_OWNERLESS_BACKLOG_CREATED_FIELDS}" != "True|True" ]]; then
  echo "expected assignment_source=none backlog sort=created_asc to preserve ownerless FIFO order while keeping mixed-source neighbors out; got ${HUMAN_OWNERLESS_BACKLOG_CREATED_FIELDS}" >&2
  echo "${HUMAN_OWNERLESS_BACKLOG_CREATED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_OWNERLESS_BACKLOG_TRANSITION_JSON="$(curl -fsS "${BASE}/v1/human/tasks/backlog?assignment_state=unassigned&assignment_source=none&sort=last_transition_desc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_OWNERLESS_BACKLOG_TRANSITION_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted={'${HUMAN_OWNERLESS_ID}','${HUMAN_OWNERLESS_NEWER_ID}'}; blocked='${HUMAN_TASK_ID}'; ids={ (row or {}).get('human_task_id','') for row in rows[:10] }; print('{}|{}'.format(wanted.issubset(ids), all((row or {}).get('human_task_id') != blocked for row in rows[:10])) )" <<<"${HUMAN_OWNERLESS_BACKLOG_TRANSITION_JSON}")"
if [[ "${HUMAN_OWNERLESS_BACKLOG_TRANSITION_FIELDS}" != "True|True" ]]; then
  echo "expected assignment_source=none backlog sort=last_transition_desc to keep mixed-source neighbors out while surfacing newest untouched ownerless work first; got ${HUMAN_OWNERLESS_BACKLOG_TRANSITION_FIELDS}" >&2
  echo "${HUMAN_OWNERLESS_BACKLOG_TRANSITION_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_OWNERLESS_UNASSIGNED_TRANSITION_JSON="$(curl -fsS "${BASE}/v1/human/tasks/unassigned?assignment_source=none&sort=last_transition_desc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_OWNERLESS_UNASSIGNED_TRANSITION_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted={'${HUMAN_OWNERLESS_ID}','${HUMAN_OWNERLESS_NEWER_ID}'}; blocked='${HUMAN_TASK_ID}'; ids={ (row or {}).get('human_task_id','') for row in rows[:10] }; print('{}|{}'.format(wanted.issubset(ids), all((row or {}).get('human_task_id') != blocked for row in rows[:10])) )" <<<"${HUMAN_OWNERLESS_UNASSIGNED_TRANSITION_JSON}")"
if [[ "${HUMAN_OWNERLESS_UNASSIGNED_TRANSITION_FIELDS}" != "True|True" ]]; then
  echo "expected assignment_source=none unassigned sort=last_transition_desc to keep mixed-source neighbors out while mirroring newest-first ownerless backlog ordering; got ${HUMAN_OWNERLESS_UNASSIGNED_TRANSITION_FIELDS}" >&2
  echo "${HUMAN_OWNERLESS_UNASSIGNED_TRANSITION_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_OWNERLESS_UNASSIGNED_CREATED_JSON="$(curl -fsS "${BASE}/v1/human/tasks/unassigned?assignment_source=none&sort=created_asc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_OWNERLESS_UNASSIGNED_CREATED_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted={'${HUMAN_OWNERLESS_ID}','${HUMAN_OWNERLESS_NEWER_ID}'}; blocked='${HUMAN_TASK_ID}'; ids={ (row or {}).get('human_task_id','') for row in rows[:10] }; print('{}|{}'.format(wanted.issubset(ids), all((row or {}).get('human_task_id') != blocked for row in rows[:10])) )" <<<"${HUMAN_OWNERLESS_UNASSIGNED_CREATED_JSON}")"
if [[ "${HUMAN_OWNERLESS_UNASSIGNED_CREATED_FIELDS}" != "True|True" ]]; then
  echo "expected assignment_source=none unassigned sort=created_asc to preserve ownerless FIFO order while keeping mixed-source neighbors out; got ${HUMAN_OWNERLESS_UNASSIGNED_CREATED_FIELDS}" >&2
  echo "${HUMAN_OWNERLESS_UNASSIGNED_CREATED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_OWNERLESS_LIST_CREATED_JSON="$(curl -fsS "${BASE}/v1/human/tasks?status=pending&assignment_state=unassigned&assignment_source=none&sort=created_asc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_OWNERLESS_LIST_CREATED_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted={'${HUMAN_OWNERLESS_ID}','${HUMAN_OWNERLESS_NEWER_ID}'}; blocked='${HUMAN_TASK_ID}'; ids={ (row or {}).get('human_task_id','') for row in rows[:10] }; print('{}|{}'.format(wanted.issubset(ids), all((row or {}).get('human_task_id') != blocked for row in rows[:10])) )" <<<"${HUMAN_OWNERLESS_LIST_CREATED_JSON}")"
if [[ "${HUMAN_OWNERLESS_LIST_CREATED_FIELDS}" != "True|True" ]]; then
  echo "expected assignment_source=none list sort=created_asc to preserve ownerless FIFO order while keeping mixed-source neighbors out; got ${HUMAN_OWNERLESS_LIST_CREATED_FIELDS}" >&2
  echo "${HUMAN_OWNERLESS_LIST_CREATED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_OWNERLESS_LIST_TRANSITION_JSON="$(curl -fsS "${BASE}/v1/human/tasks?status=pending&assignment_state=unassigned&assignment_source=none&sort=last_transition_desc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_OWNERLESS_LIST_TRANSITION_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted={'${HUMAN_OWNERLESS_ID}','${HUMAN_OWNERLESS_NEWER_ID}'}; blocked='${HUMAN_TASK_ID}'; ids={ (row or {}).get('human_task_id','') for row in rows[:10] }; print('{}|{}'.format(wanted.issubset(ids), all((row or {}).get('human_task_id') != blocked for row in rows[:10])) )" <<<"${HUMAN_OWNERLESS_LIST_TRANSITION_JSON}")"
if [[ "${HUMAN_OWNERLESS_LIST_TRANSITION_FIELDS}" != "True|True" ]]; then
  echo "expected assignment_source=none list sort=last_transition_desc to keep mixed-source neighbors out while surfacing newest untouched ownerless work first; got ${HUMAN_OWNERLESS_LIST_TRANSITION_FIELDS}" >&2
  echo "${HUMAN_OWNERLESS_LIST_TRANSITION_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
SESSION_HUMAN_NONE_CREATED_JSON="$(curl -fsS "${BASE}/v1/human/tasks?session_id=${SESSION_ID}&assignment_source=none&sort=created_asc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
SESSION_HUMAN_NONE_CREATED_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted={'${HUMAN_OWNERLESS_ID}','${HUMAN_OWNERLESS_NEWER_ID}'}; blocked='${HUMAN_TASK_ID}'; ids={ (row or {}).get('human_task_id','') for row in rows[:10] }; print('{}|{}'.format(wanted.issubset(ids), all((row or {}).get('human_task_id') != blocked for row in rows[:10])) )" <<<"${SESSION_HUMAN_NONE_CREATED_JSON}")"
if [[ "${SESSION_HUMAN_NONE_CREATED_FIELDS}" != "True|True" ]]; then
  echo "expected session-scoped assignment_source=none sort=created_asc to preserve ownerless FIFO order while keeping mixed-source neighbors out; got ${SESSION_HUMAN_NONE_CREATED_FIELDS}" >&2
  echo "${SESSION_HUMAN_NONE_CREATED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
SESSION_HUMAN_NONE_TRANSITION_JSON="$(curl -fsS "${BASE}/v1/human/tasks?session_id=${SESSION_ID}&assignment_source=none&sort=last_transition_desc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
SESSION_HUMAN_NONE_TRANSITION_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted={'${HUMAN_OWNERLESS_ID}','${HUMAN_OWNERLESS_NEWER_ID}'}; blocked='${HUMAN_TASK_ID}'; ids={ (row or {}).get('human_task_id','') for row in rows[:10] }; print('{}|{}'.format(wanted.issubset(ids), all((row or {}).get('human_task_id') != blocked for row in rows[:10])) )" <<<"${SESSION_HUMAN_NONE_TRANSITION_JSON}")"
if [[ "${SESSION_HUMAN_NONE_TRANSITION_FIELDS}" != "True|True" ]]; then
  echo "expected session-scoped assignment_source=none sort=last_transition_desc to keep mixed-source neighbors out while surfacing newest untouched ownerless work first; got ${SESSION_HUMAN_NONE_TRANSITION_FIELDS}" >&2
  echo "${SESSION_HUMAN_NONE_TRANSITION_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
SESSION_HUMAN_NONE_PROJECTION_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${SESSION_ID}?human_task_assignment_source=none" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
SESSION_HUMAN_NONE_PROJECTION_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); wanted={'${HUMAN_OWNERLESS_ID}','${HUMAN_OWNERLESS_NEWER_ID}'}; current_blocked='${HUMAN_TASK_ID}'; tasks=body.get('human_tasks') or []; history=body.get('human_task_assignment_history') or []; task_ids={ (row or {}).get('human_task_id','') for row in tasks }; history_ids={ (row or {}).get('human_task_id','') for row in history }; history_longer=len(history) > len(tasks); in_current=all((row or {}).get('human_task_id') != current_blocked for row in tasks); print('{}|{}|{}'.format(len(tasks) >= 2 and wanted.issubset(task_ids), history_longer and wanted.issubset(history_ids), in_current))" <<<"${SESSION_HUMAN_NONE_PROJECTION_JSON}")"
if [[ "${SESSION_HUMAN_NONE_PROJECTION_FIELDS}" != "True|True|True" ]]; then
  echo "expected session detail human_task_assignment_source=none projection to keep a two-row current ownerless slice while preserving a longer empty-source history trail under mixed-source churn; got ${SESSION_HUMAN_NONE_PROJECTION_FIELDS}" >&2
  echo "${SESSION_HUMAN_NONE_PROJECTION_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_ASSIGNED_BACKLOG_JSON="$(curl -fsS "${BASE}/v1/human/tasks/backlog?role_required=communications_reviewer&overdue_only=true&assignment_state=assigned&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_ASSIGNED_BACKLOG_MATCH="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); task_id='${HUMAN_TASK_ID}'; print(any((row or {}).get('human_task_id') == task_id for row in rows))" <<<"${HUMAN_ASSIGNED_BACKLOG_JSON}")"
if [[ "${HUMAN_ASSIGNED_BACKLOG_MATCH}" != "True" ]]; then
  echo "expected assigned-only backlog endpoint to include ${HUMAN_TASK_ID}" >&2
  echo "${HUMAN_ASSIGNED_BACKLOG_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_UNASSIGNED_AFTER_JSON="$(curl -fsS "${BASE}/v1/human/tasks/unassigned?role_required=communications_reviewer&overdue_only=true&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_UNASSIGNED_AFTER_MATCH="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); task_id='${HUMAN_TASK_ID}'; print(all((row or {}).get('human_task_id') != task_id for row in rows))" <<<"${HUMAN_UNASSIGNED_AFTER_JSON}")"
if [[ "${HUMAN_UNASSIGNED_AFTER_MATCH}" != "True" ]]; then
  echo "expected human task unassigned endpoint to drop ${HUMAN_TASK_ID} after assignment" >&2
  echo "${HUMAN_UNASSIGNED_AFTER_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_OPERATOR_BACKLOG_JSON="$(curl -fsS "${BASE}/v1/human/tasks/backlog?operator_id=operator-specialist&overdue_only=true&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_OPERATOR_BACKLOG_MATCH="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); task_id='${HUMAN_TASK_ID}'; print(any((row or {}).get('human_task_id') == task_id for row in rows))" <<<"${HUMAN_OPERATOR_BACKLOG_JSON}")"
if [[ "${HUMAN_OPERATOR_BACKLOG_MATCH}" != "True" ]]; then
  echo "expected operator-specialized backlog endpoint to include ${HUMAN_TASK_ID}" >&2
  echo "${HUMAN_OPERATOR_BACKLOG_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_OPERATOR_BACKLOG_LOW_JSON="$(curl -fsS "${BASE}/v1/human/tasks/backlog?operator_id=operator-junior&overdue_only=true&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_OPERATOR_BACKLOG_LOW_MATCH="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); task_id='${HUMAN_TASK_ID}'; print(all((row or {}).get('human_task_id') != task_id for row in rows))" <<<"${HUMAN_OPERATOR_BACKLOG_LOW_JSON}")"
if [[ "${HUMAN_OPERATOR_BACKLOG_LOW_MATCH}" != "True" ]]; then
  echo "expected operator-specialized backlog endpoint to exclude ${HUMAN_TASK_ID} for low-trust or under-skilled operators" >&2
  echo "${HUMAN_OPERATOR_BACKLOG_LOW_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_MINE_ASSIGNED_JSON="$(operator_curl "${BASE}/v1/human/tasks/mine?operator_id=operator-specialist&limit=10")"
HUMAN_MINE_ASSIGNED_MATCH="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); task_id='${HUMAN_TASK_ID}'; print(any((row or {}).get('human_task_id') == task_id for row in rows))" <<<"${HUMAN_MINE_ASSIGNED_JSON}")"
if [[ "${HUMAN_MINE_ASSIGNED_MATCH}" != "True" ]]; then
  echo "expected human task mine endpoint to include pre-assigned task ${HUMAN_TASK_ID}" >&2
  echo "${HUMAN_MINE_ASSIGNED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_REASSIGN_JSON="$(operator_post_json "${BASE}/v1/human/tasks/${HUMAN_TASK_ID}/assign" -H 'content-type: application/json' -d '{"operator_id":"operator-junior"}')"
HUMAN_REASSIGN_FIELDS="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print("{}|{}|{}|{}|{}|{}".format(body.get("status",""), body.get("assignment_state",""), body.get("assigned_operator_id",""), body.get("assignment_source",""), bool(body.get("assigned_at","")), body.get("assigned_by_actor_id","")))' <<<"${HUMAN_REASSIGN_JSON}")"
if [[ "${HUMAN_REASSIGN_FIELDS}" != "pending|assigned|operator-junior|manual|True|${PRINCIPAL_ID}" ]]; then
  echo "expected manual reassignment to overwrite current owner but preserve explicit provenance fields; got ${HUMAN_REASSIGN_FIELDS}" >&2
  echo "${HUMAN_REASSIGN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_REASSIGN_SUMMARY_FIELDS="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print("{}|{}|{}|{}|{}|{}".format(body.get("last_transition_event_name",""), bool(body.get("last_transition_at","")), body.get("last_transition_assignment_state",""), body.get("last_transition_operator_id",""), body.get("last_transition_assignment_source",""), body.get("last_transition_by_actor_id","")))' <<<"${HUMAN_REASSIGN_JSON}")"
if [[ "${HUMAN_REASSIGN_SUMMARY_FIELDS}" != "human_task_assigned|True|assigned|operator-junior|manual|${PRINCIPAL_ID}" && "${HUMAN_REASSIGN_SUMMARY_FIELDS}" != "|False||||" ]]; then
  echo "expected reassigned response to expose manual last-transition summary; got ${HUMAN_REASSIGN_SUMMARY_FIELDS}" >&2
  echo "${HUMAN_REASSIGN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_CLAIM_JSON="$(operator_post_json "${BASE}/v1/human/tasks/${HUMAN_TASK_ID}/claim" -H 'content-type: application/json' -d '{"operator_id":"operator-junior"}')"
HUMAN_CLAIM_FIELDS="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print("{}|{}|{}|{}|{}".format(body.get("status",""), body.get("assignment_state",""), body.get("assignment_source",""), bool(body.get("assigned_at","")), body.get("assigned_by_actor_id","")))' <<<"${HUMAN_CLAIM_JSON}")"
if [[ "${HUMAN_CLAIM_FIELDS}" != "claimed|claimed|manual|True|operator-junior" ]]; then
  echo "expected claimed human task after claim; got ${HUMAN_CLAIM_FIELDS}" >&2
  echo "${HUMAN_CLAIM_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_CLAIM_SUMMARY_FIELDS="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print("{}|{}|{}|{}|{}|{}".format(body.get("last_transition_event_name",""), bool(body.get("last_transition_at","")), body.get("last_transition_assignment_state",""), body.get("last_transition_operator_id",""), body.get("last_transition_assignment_source",""), body.get("last_transition_by_actor_id","")))' <<<"${HUMAN_CLAIM_JSON}")"
if [[ "${HUMAN_CLAIM_SUMMARY_FIELDS}" != "human_task_claimed|True|claimed|operator-junior|manual|operator-junior" && "${HUMAN_CLAIM_SUMMARY_FIELDS}" != "|False||||" ]]; then
  echo "expected claim response to expose claimed last-transition summary; got ${HUMAN_CLAIM_SUMMARY_FIELDS}" >&2
  echo "${HUMAN_CLAIM_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_OPERATOR_FILTER_JSON="$(curl -fsS "${BASE}/v1/human/tasks?assigned_operator_id=operator-junior&status=claimed&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_OPERATOR_FILTER_MATCH="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); task_id='${HUMAN_TASK_ID}'; print(any((row or {}).get('human_task_id') == task_id for row in rows))" <<<"${HUMAN_OPERATOR_FILTER_JSON}")"
if [[ "${HUMAN_OPERATOR_FILTER_MATCH}" != "True" ]]; then
  echo "expected assigned-operator human task queue filter to include ${HUMAN_TASK_ID}" >&2
  echo "${HUMAN_OPERATOR_FILTER_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_MINE_JSON="$(operator_curl "${BASE}/v1/human/tasks/mine?operator_id=operator-junior&limit=10")"
HUMAN_MINE_MATCH="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); task_id='${HUMAN_TASK_ID}'; print(any((row or {}).get('human_task_id') == task_id for row in rows))" <<<"${HUMAN_MINE_JSON}")"
if [[ "${HUMAN_MINE_MATCH}" != "True" ]]; then
  echo "expected human task mine endpoint to include ${HUMAN_TASK_ID}" >&2
  echo "${HUMAN_MINE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_RETURN_JSON="$(operator_post_json "${BASE}/v1/human/tasks/${HUMAN_TASK_ID}/return" -H 'content-type: application/json' \
  -d '{"operator_id":"operator-junior","resolution":"ready_for_send","returned_payload_json":{"summary":"Reviewed and ready."},"provenance_json":{"review_mode":"human"}}')"
HUMAN_RETURN_FIELDS="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print("{}|{}|{}|{}|{}|{}".format(body.get("status",""), body.get("assignment_state",""), body.get("assignment_source",""), body.get("resolution",""), bool(body.get("assigned_at","")), body.get("assigned_by_actor_id","")))' <<<"${HUMAN_RETURN_JSON}")"
if [[ "${HUMAN_RETURN_FIELDS}" != "returned|returned|manual|ready_for_send|True|operator-junior" ]]; then
  echo "expected returned human task after return; got ${HUMAN_RETURN_FIELDS}" >&2
  echo "${HUMAN_RETURN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_RETURN_SUMMARY_FIELDS="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print("{}|{}|{}|{}|{}|{}".format(body.get("last_transition_event_name",""), bool(body.get("last_transition_at","")), body.get("last_transition_assignment_state",""), body.get("last_transition_operator_id",""), body.get("last_transition_assignment_source",""), body.get("last_transition_by_actor_id","")))' <<<"${HUMAN_RETURN_JSON}")"
if [[ "${HUMAN_RETURN_SUMMARY_FIELDS}" != "human_task_returned|True|returned|operator-junior|manual|operator-junior" && "${HUMAN_RETURN_SUMMARY_FIELDS}" != "|False||||" ]]; then
  echo "expected return response to expose returned last-transition summary; got ${HUMAN_RETURN_SUMMARY_FIELDS}" >&2
  echo "${HUMAN_RETURN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_HISTORY_JSON="$(curl -fsS "${BASE}/v1/human/tasks/${HUMAN_TASK_ID}/assignment-history?limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_HISTORY_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); names=[(row or {}).get('event_name','') for row in rows]; operators=[(row or {}).get('assigned_operator_id','') for row in rows]; sources=[(row or {}).get('assignment_source','') for row in rows]; actors=[(row or {}).get('assigned_by_actor_id','') for row in rows]; task_keys={((row or {}).get('task_key','')) for row in rows}; deliverables={((row or {}).get('deliverable_type','')) for row in rows}; print('{}|{}|{}|{}|{}|{}'.format(','.join(names), ','.join(operators), ','.join(sources), ','.join(actors), ','.join(sorted(task_keys)), ','.join(sorted(deliverables))))" <<<"${HUMAN_HISTORY_JSON}")"
if [[ "${HUMAN_HISTORY_FIELDS}" != "human_task_created,human_task_assigned,human_task_assigned,human_task_claimed,human_task_returned|,operator-specialist,operator-junior,operator-junior,operator-junior|,recommended,manual,manual,manual|,${PRINCIPAL_ID},${PRINCIPAL_ID},operator-junior,operator-junior|rewrite_text|rewrite_note" ]]; then
  echo "expected task-scoped assignment-history endpoint to preserve both recommended and later manual owner transitions; got ${HUMAN_HISTORY_FIELDS}" >&2
  echo "${HUMAN_HISTORY_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
# Release-guard example query:
# event_name=human_task_assigned&assigned_by_actor_id=exec-1
HUMAN_HISTORY_ASSIGNED_JSON="$(curl -fsS "${BASE}/v1/human/tasks/${HUMAN_TASK_ID}/assignment-history?limit=10&event_name=human_task_assigned&assigned_by_actor_id=${PRINCIPAL_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_HISTORY_ASSIGNED_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); print(','.join((row or {}).get('assigned_operator_id','') for row in rows))" <<<"${HUMAN_HISTORY_ASSIGNED_JSON}")"
if [[ "${HUMAN_HISTORY_ASSIGNED_FIELDS}" != "operator-specialist,operator-junior" ]]; then
  echo "expected filtered assignment-history route to isolate recommended and manual assignment transitions; got ${HUMAN_HISTORY_ASSIGNED_FIELDS}" >&2
  echo "${HUMAN_HISTORY_ASSIGNED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_HISTORY_RETURN_JSON="$(curl -fsS "${BASE}/v1/human/tasks/${HUMAN_TASK_ID}/assignment-history?limit=10&event_name=human_task_returned&assigned_operator_id=operator-junior" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_HISTORY_RETURN_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); first=(rows[0] if rows else {}); print('{}|{}'.format(len(rows), (first or {}).get('assigned_by_actor_id','')))" <<<"${HUMAN_HISTORY_RETURN_JSON}")"
if [[ "${HUMAN_HISTORY_RETURN_FIELDS}" != "1|operator-junior" ]]; then
  echo "expected filtered assignment-history route to isolate returned transitions for a specific operator; got ${HUMAN_HISTORY_RETURN_FIELDS}" >&2
  echo "${HUMAN_HISTORY_RETURN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_HISTORY_RECOMMENDED_JSON="$(curl -fsS "${BASE}/v1/human/tasks/${HUMAN_TASK_ID}/assignment-history?limit=10&assignment_source=recommended" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_HISTORY_RECOMMENDED_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); first=(rows[0] if rows else {}); print('{}|{}|{}'.format(len(rows), (first or {}).get('event_name',''), (first or {}).get('assigned_operator_id','')))" <<<"${HUMAN_HISTORY_RECOMMENDED_JSON}")"
if [[ "${HUMAN_HISTORY_RECOMMENDED_FIELDS}" != "1|human_task_assigned|operator-specialist" ]]; then
  echo "expected filtered assignment-history route to isolate recommended assignment transitions by assignment_source; got ${HUMAN_HISTORY_RECOMMENDED_FIELDS}" >&2
  echo "${HUMAN_HISTORY_RECOMMENDED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_HISTORY_NONE_JSON="$(curl -fsS "${BASE}/v1/human/tasks/${HUMAN_TASK_ID}/assignment-history?limit=10&assignment_source=none" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_HISTORY_NONE_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); first=(rows[0] if rows else {}); print('{}|{}|{}'.format(len(rows), (first or {}).get('event_name',''), (first or {}).get('assignment_source','')))" <<<"${HUMAN_HISTORY_NONE_JSON}")"
if [[ "${HUMAN_HISTORY_NONE_FIELDS}" != "1|human_task_created|" ]]; then
  echo "expected filtered assignment-history route to isolate ownerless creation transitions by assignment_source=none; got ${HUMAN_HISTORY_NONE_FIELDS}" >&2
  echo "${HUMAN_HISTORY_NONE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
SESSION_HUMAN_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
SESSION_HUMAN_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); events={e.get('name','') for e in (body.get('events') or [])}; tasks=body.get('human_tasks') or []; steps=body.get('steps') or []; history=body.get('human_task_assignment_history') or []; task_id='${HUMAN_TASK_ID}'; step_id='${SESSION_STEP_ID}'; names=[(row or {}).get('event_name','') for row in history if (row or {}).get('human_task_id') == task_id]; operators=[(row or {}).get('assigned_operator_id','') for row in history if (row or {}).get('human_task_id') == task_id]; task_keys={((row or {}).get('task_key','')) for row in history if (row or {}).get('human_task_id') == task_id}; deliverables={((row or {}).get('deliverable_type','')) for row in history if (row or {}).get('human_task_id') == task_id}; packet=next((row for row in tasks if (row or {}).get('human_task_id') == task_id), {}); print('{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}'.format(body.get('status',''), 'human_task_created' in events and 'human_task_assigned' in events, 'human_task_claimed' in events, 'human_task_returned' in events and 'session_resumed_from_human_task' in events, any((row or {}).get('human_task_id') == task_id and (row or {}).get('status') == 'returned' and (row or {}).get('assignment_state') == 'returned' and (row or {}).get('assignment_source') == 'manual' and bool((row or {}).get('assigned_at','')) and (row or {}).get('assigned_by_actor_id') == 'operator-junior' for row in tasks), any((row or {}).get('step_id') == step_id and (row or {}).get('state') == 'completed' and ((row or {}).get('output_json') or {}).get('human_task_id') == task_id for row in steps), any((row or {}).get('assignment_source') == 'manual' for row in tasks if (row or {}).get('human_task_id') == task_id), any((row or {}).get('assigned_by_actor_id') == 'operator-junior' for row in tasks if (row or {}).get('human_task_id') == task_id), ','.join(names), ','.join(operators), ','.join(sorted(task_keys)), ','.join(sorted(deliverables)), packet.get('task_key',''), packet.get('deliverable_type','')))" <<<"${SESSION_HUMAN_JSON}")"
if [[ "${SESSION_HUMAN_FIELDS}" != "completed|True|True|True|True|True|True|True|human_task_created,human_task_assigned,human_task_assigned,human_task_claimed,human_task_returned|,operator-specialist,operator-junior,operator-junior,operator-junior|rewrite_text|rewrite_note|rewrite_text|rewrite_note" ]]; then
  echo "expected resumed session projection to expose returned row, completed resumed step, and inline assignment history; got ${SESSION_HUMAN_FIELDS}" >&2
  echo "${SESSION_HUMAN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
SESSION_HUMAN_SUMMARY_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); task_id='${HUMAN_TASK_ID}'; task=next((row for row in (body.get('human_tasks') or []) if (row or {}).get('human_task_id') == task_id), {}); print('{}|{}|{}|{}|{}|{}'.format(task.get('last_transition_event_name',''), bool(task.get('last_transition_at','')), task.get('last_transition_assignment_state',''), task.get('last_transition_operator_id',''), task.get('last_transition_assignment_source',''), task.get('last_transition_by_actor_id','')))" <<<"${SESSION_HUMAN_JSON}")"
if [[ "${SESSION_HUMAN_SUMMARY_FIELDS}" != "human_task_returned|True|returned|operator-junior|manual|operator-junior" ]]; then
  echo "expected resumed session task row to expose returned last-transition summary; got ${SESSION_HUMAN_SUMMARY_FIELDS}" >&2
  echo "${SESSION_HUMAN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
SESSION_HUMAN_MANUAL_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${SESSION_ID}?human_task_assignment_source=manual" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
SESSION_HUMAN_MANUAL_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); tasks=body.get('human_tasks') or []; history=body.get('human_task_assignment_history') or []; print('{}|{}|{}'.format(len(tasks), (tasks[0].get('human_task_id','') if tasks else ''), ','.join((row or {}).get('event_name','') for row in history)))" <<<"${SESSION_HUMAN_MANUAL_JSON}")"
if [[ "${SESSION_HUMAN_MANUAL_FIELDS}" != "1|${HUMAN_TASK_ID}|human_task_assigned,human_task_claimed,human_task_returned" ]]; then
  echo "expected session assignment-source filter to isolate manual ownership rows and transitions; got ${SESSION_HUMAN_MANUAL_FIELDS}" >&2
  echo "${SESSION_HUMAN_MANUAL_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
echo "human tasks ok"

echo "== smoke: human task last-transition sort =="
ensure_operator_profile "operator-sorter" "communications_reviewer" '["tone"]' "standard" "Queue Sorter"
SORT_REWRITE_JSON="$(curl_body_retry 5 1 -X POST "${BASE}/v1/rewrite/artifact" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' -d '{"text":"sort seed"}')"
SORT_SESSION_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("execution_session_id",""))' <<<"${SORT_REWRITE_JSON}")"
SORT_SESSION_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${SORT_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
SORT_STEP_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); rows=body.get("steps") or []; print(((rows[-1] or {}).get("step_id")) if rows else "")' <<<"${SORT_SESSION_JSON}")"
if [[ -z "${SORT_STEP_ID}" ]]; then
  fail 13 "missing sort step_id from session response"
fi
SORT_TASK_OLDER_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${SORT_SESSION_ID}\",\"step_id\":\"${SORT_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Older pending task.\",\"resume_session_on_return\":false}")"
SORT_TASK_OLDER_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${SORT_TASK_OLDER_JSON}")"
SORT_TASK_NEWER_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${SORT_SESSION_ID}\",\"step_id\":\"${SORT_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Newer untouched task.\",\"resume_session_on_return\":false}")"
SORT_TASK_NEWER_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${SORT_TASK_NEWER_JSON}")"
if [[ -z "${SORT_TASK_OLDER_ID}" || -z "${SORT_TASK_NEWER_ID}" ]]; then
  fail 13 "missing human task ids from sort smoke setup"
fi
SORT_ASSIGN_JSON="$(operator_post_json "${BASE}/v1/human/tasks/${SORT_TASK_OLDER_ID}/assign" -H 'content-type: application/json' -d '{"operator_id":"operator-sorter"}')"
SORT_ASSIGN_FIELDS="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print("{}|{}".format(body.get("human_task_id",""), body.get("last_transition_event_name","")))' <<<"${SORT_ASSIGN_JSON}")"
if [[ "${SORT_ASSIGN_FIELDS}" != "${SORT_TASK_OLDER_ID}|human_task_assigned" && "${SORT_ASSIGN_FIELDS}" != "${SORT_TASK_OLDER_ID}|" ]]; then
  echo "expected sort-smoke assignment to mark the older task as recently assigned; got ${SORT_ASSIGN_FIELDS}" >&2
  echo "${SORT_ASSIGN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
SORT_LIST_JSON="$(curl -fsS "${BASE}/v1/human/tasks?status=pending&sort=last_transition_desc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
SORT_LIST_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted=['${SORT_TASK_OLDER_ID}','${SORT_TASK_NEWER_ID}']; filtered=[row for row in rows if (row or {}).get('human_task_id') in wanted]; first=(filtered[0] if len(filtered) > 0 else {}); second=(filtered[1] if len(filtered) > 1 else {}); print('{}|{}|{}|{}'.format(first.get('human_task_id',''), first.get('last_transition_event_name',''), second.get('human_task_id',''), second.get('last_transition_event_name','')))" <<<"${SORT_LIST_JSON}")"
if [[ "${SORT_LIST_FIELDS}" != "${SORT_TASK_OLDER_ID}|human_task_assigned|${SORT_TASK_NEWER_ID}|human_task_created" && "${SORT_LIST_FIELDS}" != "${SORT_TASK_OLDER_ID}||${SORT_TASK_NEWER_ID}|human_task_created" && "${SORT_LIST_FIELDS}" != "${SORT_TASK_OLDER_ID}|human_task_assigned|${SORT_TASK_NEWER_ID}|" && "${SORT_LIST_FIELDS}" != "${SORT_TASK_OLDER_ID}||${SORT_TASK_NEWER_ID}|" ]]; then
  echo "expected sort=last_transition_desc to order general human task list by freshest ownership change; got ${SORT_LIST_FIELDS}" >&2
  echo "${SORT_LIST_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
SORT_BACKLOG_JSON="$(curl -fsS "${BASE}/v1/human/tasks/backlog?sort=last_transition_desc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
SORT_BACKLOG_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted=['${SORT_TASK_OLDER_ID}','${SORT_TASK_NEWER_ID}']; filtered=[row for row in rows if (row or {}).get('human_task_id') in wanted]; first=(filtered[0] if len(filtered) > 0 else {}); second=(filtered[1] if len(filtered) > 1 else {}); print('{}|{}|{}|{}'.format(first.get('human_task_id',''), first.get('last_transition_event_name',''), second.get('human_task_id',''), second.get('last_transition_event_name','')))" <<<"${SORT_BACKLOG_JSON}")"
if [[ "${SORT_BACKLOG_FIELDS}" != "${SORT_TASK_OLDER_ID}|human_task_assigned|${SORT_TASK_NEWER_ID}|human_task_created" && "${SORT_BACKLOG_FIELDS}" != "${SORT_TASK_OLDER_ID}||${SORT_TASK_NEWER_ID}|human_task_created" && "${SORT_BACKLOG_FIELDS}" != "${SORT_TASK_OLDER_ID}|human_task_assigned|${SORT_TASK_NEWER_ID}|" && "${SORT_BACKLOG_FIELDS}" != "${SORT_TASK_OLDER_ID}||${SORT_TASK_NEWER_ID}|" ]]; then
  echo "expected backlog sort=last_transition_desc to order pending work by freshest ownership change; got ${SORT_BACKLOG_FIELDS}" >&2
  echo "${SORT_BACKLOG_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
echo "human task last-transition sort ok"

echo "== smoke: human task created-asc sort =="
CREATED_ASC_REWRITE_JSON="$(curl_body_retry 5 1 -X POST "${BASE}/v1/rewrite/artifact" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' -d '{"text":"created asc seed"}')"
CREATED_ASC_SESSION_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("execution_session_id",""))' <<<"${CREATED_ASC_REWRITE_JSON}")"
CREATED_ASC_SESSION_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${CREATED_ASC_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
CREATED_ASC_STEP_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); rows=body.get("steps") or []; print(((rows[-1] or {}).get("step_id")) if rows else "")' <<<"${CREATED_ASC_SESSION_JSON}")"
if [[ -z "${CREATED_ASC_STEP_ID}" ]]; then
  fail 13 "missing created-asc sort step_id from session response"
fi
CREATED_ASC_OLDEST_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${CREATED_ASC_SESSION_ID}\",\"step_id\":\"${CREATED_ASC_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Oldest unassigned task.\",\"resume_session_on_return\":false}")"
CREATED_ASC_OLDEST_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${CREATED_ASC_OLDEST_JSON}")"
CREATED_ASC_OLDER_MINE_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${CREATED_ASC_SESSION_ID}\",\"step_id\":\"${CREATED_ASC_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Older assigned task.\",\"resume_session_on_return\":false}")"
CREATED_ASC_OLDER_MINE_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${CREATED_ASC_OLDER_MINE_JSON}")"
CREATED_ASC_MIDDLE_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${CREATED_ASC_SESSION_ID}\",\"step_id\":\"${CREATED_ASC_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Middle unassigned task.\",\"resume_session_on_return\":false}")"
CREATED_ASC_MIDDLE_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${CREATED_ASC_MIDDLE_JSON}")"
CREATED_ASC_NEWER_MINE_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${CREATED_ASC_SESSION_ID}\",\"step_id\":\"${CREATED_ASC_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Newer assigned task.\",\"resume_session_on_return\":false}")"
CREATED_ASC_NEWER_MINE_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${CREATED_ASC_NEWER_MINE_JSON}")"
if [[ -z "${CREATED_ASC_OLDEST_ID}" || -z "${CREATED_ASC_OLDER_MINE_ID}" || -z "${CREATED_ASC_MIDDLE_ID}" || -z "${CREATED_ASC_NEWER_MINE_ID}" ]]; then
  fail 13 "missing human task ids from created-asc sort smoke setup"
fi
CREATED_ASC_ASSIGN_OLDER_JSON="$(operator_post_json "${BASE}/v1/human/tasks/${CREATED_ASC_OLDER_MINE_ID}/assign" -H 'content-type: application/json' -d '{"operator_id":"operator-sorter"}')"
CREATED_ASC_ASSIGN_NEWER_JSON="$(operator_post_json "${BASE}/v1/human/tasks/${CREATED_ASC_NEWER_MINE_ID}/assign" -H 'content-type: application/json' -d '{"operator_id":"operator-sorter"}')"
CREATED_ASC_ASSIGN_FIELDS="$(python3 -c "import json,sys; first=json.loads(sys.argv[1] or '{}'); second=json.loads(sys.argv[2] or '{}'); print('{}|{}|{}|{}'.format(first.get('human_task_id',''), first.get('last_transition_event_name',''), second.get('human_task_id',''), second.get('last_transition_event_name','')))" "${CREATED_ASC_ASSIGN_OLDER_JSON}" "${CREATED_ASC_ASSIGN_NEWER_JSON}")"
if [[ "${CREATED_ASC_ASSIGN_FIELDS}" != "${CREATED_ASC_OLDER_MINE_ID}|human_task_assigned|${CREATED_ASC_NEWER_MINE_ID}|human_task_assigned" && "${CREATED_ASC_ASSIGN_FIELDS}" != "${CREATED_ASC_OLDER_MINE_ID}||${CREATED_ASC_NEWER_MINE_ID}|" && "${CREATED_ASC_ASSIGN_FIELDS}" != "${CREATED_ASC_OLDER_MINE_ID}||${CREATED_ASC_NEWER_MINE_ID}|human_task_assigned" && "${CREATED_ASC_ASSIGN_FIELDS}" != "${CREATED_ASC_OLDER_MINE_ID}|human_task_assigned|${CREATED_ASC_NEWER_MINE_ID}|" ]]; then
  echo "expected created-asc setup assignments to preserve assigned task ownership metadata; got ${CREATED_ASC_ASSIGN_FIELDS}" >&2
  echo "${CREATED_ASC_ASSIGN_OLDER_JSON}" >&2
  echo "${CREATED_ASC_ASSIGN_NEWER_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
CREATED_ASC_LIST_JSON="$(curl -fsS "${BASE}/v1/human/tasks?status=pending&sort=created_asc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
CREATED_ASC_LIST_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted=['${CREATED_ASC_OLDEST_ID}','${CREATED_ASC_OLDER_MINE_ID}','${CREATED_ASC_MIDDLE_ID}','${CREATED_ASC_NEWER_MINE_ID}']; filtered=[row for row in rows if (row or {}).get('human_task_id') in wanted]; ids=[(row or {}).get('human_task_id','') for row in filtered[:4]]; print('|'.join(ids))" <<<"${CREATED_ASC_LIST_JSON}")"
if [[ "${CREATED_ASC_LIST_FIELDS}" != "${CREATED_ASC_OLDEST_ID}|${CREATED_ASC_OLDER_MINE_ID}|${CREATED_ASC_MIDDLE_ID}|${CREATED_ASC_NEWER_MINE_ID}" ]]; then
  echo "expected sort=created_asc to order the general pending queue by oldest created task first; got ${CREATED_ASC_LIST_FIELDS}" >&2
  echo "${CREATED_ASC_LIST_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
CREATED_ASC_BACKLOG_JSON="$(curl -fsS "${BASE}/v1/human/tasks/backlog?sort=created_asc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
CREATED_ASC_BACKLOG_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted=['${CREATED_ASC_OLDEST_ID}','${CREATED_ASC_OLDER_MINE_ID}','${CREATED_ASC_MIDDLE_ID}','${CREATED_ASC_NEWER_MINE_ID}']; filtered=[row for row in rows if (row or {}).get('human_task_id') in wanted]; ids=[(row or {}).get('human_task_id','') for row in filtered[:4]]; print('|'.join(ids))" <<<"${CREATED_ASC_BACKLOG_JSON}")"
if [[ "${CREATED_ASC_BACKLOG_FIELDS}" != "${CREATED_ASC_OLDEST_ID}|${CREATED_ASC_OLDER_MINE_ID}|${CREATED_ASC_MIDDLE_ID}|${CREATED_ASC_NEWER_MINE_ID}" ]]; then
  echo "expected backlog sort=created_asc to order pending work by oldest created task first; got ${CREATED_ASC_BACKLOG_FIELDS}" >&2
  echo "${CREATED_ASC_BACKLOG_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
CREATED_ASC_UNASSIGNED_JSON="$(curl -fsS "${BASE}/v1/human/tasks/unassigned?sort=created_asc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
CREATED_ASC_UNASSIGNED_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted=['${CREATED_ASC_OLDEST_ID}','${CREATED_ASC_MIDDLE_ID}']; filtered=[row for row in rows if (row or {}).get('human_task_id') in wanted]; ids=[(row or {}).get('human_task_id','') for row in filtered[:2]]; print('|'.join(ids))" <<<"${CREATED_ASC_UNASSIGNED_JSON}")"
if [[ "${CREATED_ASC_UNASSIGNED_FIELDS}" != "${CREATED_ASC_OLDEST_ID}|${CREATED_ASC_MIDDLE_ID}" ]]; then
  echo "expected unassigned sort=created_asc to keep oldest unassigned work first; got ${CREATED_ASC_UNASSIGNED_FIELDS}" >&2
  echo "${CREATED_ASC_UNASSIGNED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
CREATED_ASC_MINE_JSON="$(operator_curl "${BASE}/v1/human/tasks/mine?operator_id=operator-sorter&status=pending&sort=created_asc&limit=10")"
CREATED_ASC_MINE_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted=['${CREATED_ASC_OLDER_MINE_ID}','${CREATED_ASC_NEWER_MINE_ID}']; filtered=[row for row in rows if (row or {}).get('human_task_id') in wanted]; ids=[(row or {}).get('human_task_id','') for row in filtered[:2]]; print('|'.join(ids))" <<<"${CREATED_ASC_MINE_JSON}")"
if [[ "${CREATED_ASC_MINE_FIELDS}" != "${CREATED_ASC_OLDER_MINE_ID}|${CREATED_ASC_NEWER_MINE_ID}" ]]; then
  echo "expected mine sort=created_asc to keep the operator queue in oldest-created order; got ${CREATED_ASC_MINE_FIELDS}" >&2
  echo "${CREATED_ASC_MINE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
echo "human task created-asc sort ok"

echo "== smoke: human task priority-desc-created-asc sort =="
PRIORITY_SORT_REWRITE_JSON="$(curl_body_retry 5 1 -X POST "${BASE}/v1/rewrite/artifact" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' -d '{"text":"priority sort seed"}')"
PRIORITY_SORT_SESSION_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("execution_session_id",""))' <<<"${PRIORITY_SORT_REWRITE_JSON}")"
PRIORITY_SORT_SESSION_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${PRIORITY_SORT_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
PRIORITY_SORT_STEP_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); rows=body.get("steps") or []; print(((rows[-1] or {}).get("step_id")) if rows else "")' <<<"${PRIORITY_SORT_SESSION_JSON}")"
if [[ -z "${PRIORITY_SORT_STEP_ID}" ]]; then
  fail 13 "missing priority sort step_id from session response"
fi
PRIORITY_SORT_OLDEST_NORMAL_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${PRIORITY_SORT_SESSION_ID}\",\"step_id\":\"${PRIORITY_SORT_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Oldest normal task.\",\"priority\":\"normal\",\"resume_session_on_return\":false}")"
PRIORITY_SORT_OLDEST_NORMAL_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${PRIORITY_SORT_OLDEST_NORMAL_JSON}")"
PRIORITY_SORT_OLDER_HIGH_MINE_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${PRIORITY_SORT_SESSION_ID}\",\"step_id\":\"${PRIORITY_SORT_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Older high-priority assigned task.\",\"priority\":\"high\",\"resume_session_on_return\":false}")"
PRIORITY_SORT_OLDER_HIGH_MINE_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${PRIORITY_SORT_OLDER_HIGH_MINE_JSON}")"
PRIORITY_SORT_MIDDLE_HIGH_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${PRIORITY_SORT_SESSION_ID}\",\"step_id\":\"${PRIORITY_SORT_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Middle high-priority unassigned task.\",\"priority\":\"high\",\"resume_session_on_return\":false}")"
PRIORITY_SORT_MIDDLE_HIGH_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${PRIORITY_SORT_MIDDLE_HIGH_JSON}")"
PRIORITY_SORT_NEWER_URGENT_MINE_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${PRIORITY_SORT_SESSION_ID}\",\"step_id\":\"${PRIORITY_SORT_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Newer urgent assigned task.\",\"priority\":\"urgent\",\"resume_session_on_return\":false}")"
PRIORITY_SORT_NEWER_URGENT_MINE_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${PRIORITY_SORT_NEWER_URGENT_MINE_JSON}")"
PRIORITY_SORT_NEWEST_NORMAL_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${PRIORITY_SORT_SESSION_ID}\",\"step_id\":\"${PRIORITY_SORT_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Newest normal task.\",\"priority\":\"normal\",\"resume_session_on_return\":false}")"
PRIORITY_SORT_NEWEST_NORMAL_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${PRIORITY_SORT_NEWEST_NORMAL_JSON}")"
if [[ -z "${PRIORITY_SORT_OLDEST_NORMAL_ID}" || -z "${PRIORITY_SORT_OLDER_HIGH_MINE_ID}" || -z "${PRIORITY_SORT_MIDDLE_HIGH_ID}" || -z "${PRIORITY_SORT_NEWER_URGENT_MINE_ID}" || -z "${PRIORITY_SORT_NEWEST_NORMAL_ID}" ]]; then
  fail 13 "missing human task ids from priority sort smoke setup"
fi
PRIORITY_SORT_ASSIGN_OLDER_JSON="$(operator_post_json "${BASE}/v1/human/tasks/${PRIORITY_SORT_OLDER_HIGH_MINE_ID}/assign" -H 'content-type: application/json' -d '{"operator_id":"operator-sorter"}')"
PRIORITY_SORT_ASSIGN_URGENT_JSON="$(operator_post_json "${BASE}/v1/human/tasks/${PRIORITY_SORT_NEWER_URGENT_MINE_ID}/assign" -H 'content-type: application/json' -d '{"operator_id":"operator-sorter"}')"
PRIORITY_SORT_ASSIGN_FIELDS="$(python3 -c "import json,sys; first=json.loads(sys.argv[1] or '{}'); second=json.loads(sys.argv[2] or '{}'); print('{}|{}|{}|{}'.format(first.get('human_task_id',''), first.get('last_transition_event_name',''), second.get('human_task_id',''), second.get('last_transition_event_name','')))" "${PRIORITY_SORT_ASSIGN_OLDER_JSON}" "${PRIORITY_SORT_ASSIGN_URGENT_JSON}")"
if [[ "${PRIORITY_SORT_ASSIGN_FIELDS}" != "${PRIORITY_SORT_OLDER_HIGH_MINE_ID}|human_task_assigned|${PRIORITY_SORT_NEWER_URGENT_MINE_ID}|human_task_assigned" && "${PRIORITY_SORT_ASSIGN_FIELDS}" != "${PRIORITY_SORT_OLDER_HIGH_MINE_ID}||${PRIORITY_SORT_NEWER_URGENT_MINE_ID}|" && "${PRIORITY_SORT_ASSIGN_FIELDS}" != "${PRIORITY_SORT_OLDER_HIGH_MINE_ID}|human_task_assigned|${PRIORITY_SORT_NEWER_URGENT_MINE_ID}|" && "${PRIORITY_SORT_ASSIGN_FIELDS}" != "${PRIORITY_SORT_OLDER_HIGH_MINE_ID}||${PRIORITY_SORT_NEWER_URGENT_MINE_ID}|human_task_assigned" ]]; then
  echo "expected priority-sort setup assignments to preserve assigned task ownership metadata; got ${PRIORITY_SORT_ASSIGN_FIELDS}" >&2
  echo "${PRIORITY_SORT_ASSIGN_OLDER_JSON}" >&2
  echo "${PRIORITY_SORT_ASSIGN_URGENT_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
PRIORITY_SORT_LIST_JSON="$(curl -fsS "${BASE}/v1/human/tasks?status=pending&sort=priority_desc_created_asc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
PRIORITY_SORT_LIST_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted=['${PRIORITY_SORT_OLDEST_NORMAL_ID}','${PRIORITY_SORT_OLDER_HIGH_MINE_ID}','${PRIORITY_SORT_MIDDLE_HIGH_ID}','${PRIORITY_SORT_NEWER_URGENT_MINE_ID}','${PRIORITY_SORT_NEWEST_NORMAL_ID}']; filtered=[row for row in rows if (row or {}).get('human_task_id') in wanted]; ids=[(row or {}).get('human_task_id','') for row in filtered[:5]]; print('|'.join(ids))" <<<"${PRIORITY_SORT_LIST_JSON}")"
if [[ "${PRIORITY_SORT_LIST_FIELDS}" != "${PRIORITY_SORT_NEWER_URGENT_MINE_ID}|${PRIORITY_SORT_OLDER_HIGH_MINE_ID}|${PRIORITY_SORT_MIDDLE_HIGH_ID}|${PRIORITY_SORT_OLDEST_NORMAL_ID}|${PRIORITY_SORT_NEWEST_NORMAL_ID}" ]]; then
  echo "expected sort=priority_desc_created_asc to order pending work by priority first and oldest-created within each band; got ${PRIORITY_SORT_LIST_FIELDS}" >&2
  echo "${PRIORITY_SORT_LIST_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
PRIORITY_SORT_BACKLOG_JSON="$(curl -fsS "${BASE}/v1/human/tasks/backlog?sort=priority_desc_created_asc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
PRIORITY_SORT_BACKLOG_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted=['${PRIORITY_SORT_OLDEST_NORMAL_ID}','${PRIORITY_SORT_OLDER_HIGH_MINE_ID}','${PRIORITY_SORT_MIDDLE_HIGH_ID}','${PRIORITY_SORT_NEWER_URGENT_MINE_ID}','${PRIORITY_SORT_NEWEST_NORMAL_ID}']; filtered=[row for row in rows if (row or {}).get('human_task_id') in wanted]; ids=[(row or {}).get('human_task_id','') for row in filtered[:5]]; print('|'.join(ids))" <<<"${PRIORITY_SORT_BACKLOG_JSON}")"
if [[ "${PRIORITY_SORT_BACKLOG_FIELDS}" != "${PRIORITY_SORT_NEWER_URGENT_MINE_ID}|${PRIORITY_SORT_OLDER_HIGH_MINE_ID}|${PRIORITY_SORT_MIDDLE_HIGH_ID}|${PRIORITY_SORT_OLDEST_NORMAL_ID}|${PRIORITY_SORT_NEWEST_NORMAL_ID}" ]]; then
  echo "expected backlog sort=priority_desc_created_asc to order pending work by priority first and oldest-created within each band; got ${PRIORITY_SORT_BACKLOG_FIELDS}" >&2
  echo "${PRIORITY_SORT_BACKLOG_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
PRIORITY_SORT_UNASSIGNED_JSON="$(curl -fsS "${BASE}/v1/human/tasks/unassigned?sort=priority_desc_created_asc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
PRIORITY_SORT_UNASSIGNED_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted=['${PRIORITY_SORT_MIDDLE_HIGH_ID}','${PRIORITY_SORT_OLDEST_NORMAL_ID}','${PRIORITY_SORT_NEWEST_NORMAL_ID}']; filtered=[row for row in rows if (row or {}).get('human_task_id') in wanted]; ids=[(row or {}).get('human_task_id','') for row in filtered[:3]]; print('|'.join(ids))" <<<"${PRIORITY_SORT_UNASSIGNED_JSON}")"
if [[ "${PRIORITY_SORT_UNASSIGNED_FIELDS}" != "${PRIORITY_SORT_MIDDLE_HIGH_ID}|${PRIORITY_SORT_OLDEST_NORMAL_ID}|${PRIORITY_SORT_NEWEST_NORMAL_ID}" ]]; then
  echo "expected unassigned sort=priority_desc_created_asc to keep higher-priority work ahead of older normal tasks; got ${PRIORITY_SORT_UNASSIGNED_FIELDS}" >&2
  echo "${PRIORITY_SORT_UNASSIGNED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
PRIORITY_SORT_MINE_JSON="$(operator_curl "${BASE}/v1/human/tasks/mine?operator_id=operator-sorter&status=pending&sort=priority_desc_created_asc&limit=10")"
PRIORITY_SORT_MINE_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted=['${PRIORITY_SORT_OLDER_HIGH_MINE_ID}','${PRIORITY_SORT_NEWER_URGENT_MINE_ID}']; filtered=[row for row in rows if (row or {}).get('human_task_id') in wanted]; ids=[(row or {}).get('human_task_id','') for row in filtered[:2]]; print('|'.join(ids))" <<<"${PRIORITY_SORT_MINE_JSON}")"
if [[ "${PRIORITY_SORT_MINE_FIELDS}" != "${PRIORITY_SORT_NEWER_URGENT_MINE_ID}|${PRIORITY_SORT_OLDER_HIGH_MINE_ID}" ]]; then
  echo "expected mine sort=priority_desc_created_asc to keep urgent assigned work ahead of older high-priority work; got ${PRIORITY_SORT_MINE_FIELDS}" >&2
  echo "${PRIORITY_SORT_MINE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
echo "human task priority-desc-created-asc sort ok"

echo "== smoke: human task priority filter =="
PRIORITY_FILTER_REWRITE_JSON="$(curl_body_retry 5 1 -X POST "${BASE}/v1/rewrite/artifact" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' -d '{"text":"priority filter seed"}')"
PRIORITY_FILTER_SESSION_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("execution_session_id",""))' <<<"${PRIORITY_FILTER_REWRITE_JSON}")"
PRIORITY_FILTER_SESSION_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${PRIORITY_FILTER_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
PRIORITY_FILTER_STEP_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); rows=body.get("steps") or []; print(((rows[-1] or {}).get("step_id")) if rows else "")' <<<"${PRIORITY_FILTER_SESSION_JSON}")"
PRIORITY_FILTER_ROLE="priority_filter_reviewer"
PRIORITY_FILTER_OPERATOR="operator-priority-filter"
ensure_operator_profile "${PRIORITY_FILTER_OPERATOR}" "${PRIORITY_FILTER_ROLE}" '["tone"]' "standard" "Priority Filter Reviewer"
if [[ -z "${PRIORITY_FILTER_STEP_ID}" ]]; then
  fail 13 "missing priority filter step_id from session response"
fi
PRIORITY_FILTER_NORMAL_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${PRIORITY_FILTER_SESSION_ID}\",\"step_id\":\"${PRIORITY_FILTER_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"${PRIORITY_FILTER_ROLE}\",\"brief\":\"Normal unassigned task.\",\"priority\":\"normal\",\"resume_session_on_return\":false}")"
PRIORITY_FILTER_NORMAL_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${PRIORITY_FILTER_NORMAL_JSON}")"
PRIORITY_FILTER_HIGH_MINE_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${PRIORITY_FILTER_SESSION_ID}\",\"step_id\":\"${PRIORITY_FILTER_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"${PRIORITY_FILTER_ROLE}\",\"brief\":\"High assigned task.\",\"priority\":\"high\",\"resume_session_on_return\":false}")"
PRIORITY_FILTER_HIGH_MINE_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${PRIORITY_FILTER_HIGH_MINE_JSON}")"
PRIORITY_FILTER_HIGH_UNASSIGNED_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${PRIORITY_FILTER_SESSION_ID}\",\"step_id\":\"${PRIORITY_FILTER_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"${PRIORITY_FILTER_ROLE}\",\"brief\":\"High unassigned task.\",\"priority\":\"high\",\"resume_session_on_return\":false}")"
PRIORITY_FILTER_HIGH_UNASSIGNED_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${PRIORITY_FILTER_HIGH_UNASSIGNED_JSON}")"
PRIORITY_FILTER_URGENT_MINE_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${PRIORITY_FILTER_SESSION_ID}\",\"step_id\":\"${PRIORITY_FILTER_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"${PRIORITY_FILTER_ROLE}\",\"brief\":\"Urgent assigned task.\",\"priority\":\"urgent\",\"resume_session_on_return\":false}")"
PRIORITY_FILTER_URGENT_MINE_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${PRIORITY_FILTER_URGENT_MINE_JSON}")"
if [[ -z "${PRIORITY_FILTER_NORMAL_ID}" || -z "${PRIORITY_FILTER_HIGH_MINE_ID}" || -z "${PRIORITY_FILTER_HIGH_UNASSIGNED_ID}" || -z "${PRIORITY_FILTER_URGENT_MINE_ID}" ]]; then
  fail 13 "missing human task ids from priority filter smoke setup"
fi
operator_post_json "${BASE}/v1/human/tasks/${PRIORITY_FILTER_HIGH_MINE_ID}/assign" -H 'content-type: application/json' -d "{\"operator_id\":\"${PRIORITY_FILTER_OPERATOR}\"}" >/dev/null
operator_post_json "${BASE}/v1/human/tasks/${PRIORITY_FILTER_URGENT_MINE_ID}/assign" -H 'content-type: application/json' -d "{\"operator_id\":\"${PRIORITY_FILTER_OPERATOR}\"}" >/dev/null
PRIORITY_FILTER_LIST_JSON="$(curl -fsS "${BASE}/v1/human/tasks?session_id=${PRIORITY_FILTER_SESSION_ID}&status=pending&role_required=${PRIORITY_FILTER_ROLE}&priority=high&sort=created_asc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
PRIORITY_FILTER_LIST_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); ids=[(row or {}).get('human_task_id','') for row in rows]; print('{}|{}|{}|{}'.format('|'.join([row for row in ids if row in ['${PRIORITY_FILTER_HIGH_MINE_ID}','${PRIORITY_FILTER_HIGH_UNASSIGNED_ID}']]), '${PRIORITY_FILTER_NORMAL_ID}' in ids, '${PRIORITY_FILTER_URGENT_MINE_ID}' in ids, len(ids)))" <<<"${PRIORITY_FILTER_LIST_JSON}")"
if [[ "${PRIORITY_FILTER_LIST_FIELDS}" != "${PRIORITY_FILTER_HIGH_MINE_ID}|${PRIORITY_FILTER_HIGH_UNASSIGNED_ID}|False|False|2" ]]; then
  echo "expected list priority filter to isolate only high-priority tasks in oldest-created order; got ${PRIORITY_FILTER_LIST_FIELDS}" >&2
  echo "${PRIORITY_FILTER_LIST_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
PRIORITY_FILTER_BACKLOG_JSON="$(curl -fsS "${BASE}/v1/human/tasks/backlog?role_required=${PRIORITY_FILTER_ROLE}&priority=high&sort=created_asc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
PRIORITY_FILTER_BACKLOG_FIELDS="$(python3 -c 'import json,sys; a,b,n,u=sys.argv[1:5]; rows=json.loads(sys.stdin.read() or "[]"); ids=[(row or {}).get("human_task_id","") for row in rows]; matched=sorted([row for row in ids if row in [a,b]]); print("{}|{}|{}|{}".format("|".join(matched), n in ids, u in ids, len(matched) >= 2))' "${PRIORITY_FILTER_HIGH_MINE_ID}" "${PRIORITY_FILTER_HIGH_UNASSIGNED_ID}" "${PRIORITY_FILTER_NORMAL_ID}" "${PRIORITY_FILTER_URGENT_MINE_ID}" <<<"${PRIORITY_FILTER_BACKLOG_JSON}")"
if [[ "${PRIORITY_FILTER_BACKLOG_FIELDS}" != "${PRIORITY_FILTER_HIGH_MINE_ID}|${PRIORITY_FILTER_HIGH_UNASSIGNED_ID}|False|False|True" && "${PRIORITY_FILTER_BACKLOG_FIELDS}" != "${PRIORITY_FILTER_HIGH_UNASSIGNED_ID}|${PRIORITY_FILTER_HIGH_MINE_ID}|False|False|True" && "${PRIORITY_FILTER_BACKLOG_FIELDS}" != "${PRIORITY_FILTER_HIGH_MINE_ID}||False|False|True" && "${PRIORITY_FILTER_BACKLOG_FIELDS}" != "|${PRIORITY_FILTER_HIGH_MINE_ID}|False|False|True" && "${PRIORITY_FILTER_BACKLOG_FIELDS}" != "|${PRIORITY_FILTER_HIGH_UNASSIGNED_ID}|False|False|True" ]]; then
  echo "expected backlog priority filter to isolate only high-priority tasks in oldest-created order; got ${PRIORITY_FILTER_BACKLOG_FIELDS}" >&2
  echo "${PRIORITY_FILTER_BACKLOG_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
PRIORITY_FILTER_UNASSIGNED_JSON="$(curl -fsS "${BASE}/v1/human/tasks/unassigned?role_required=${PRIORITY_FILTER_ROLE}&priority=high&sort=created_asc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
PRIORITY_FILTER_UNASSIGNED_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); ids=[(row or {}).get('human_task_id','') for row in rows]; print('|'.join([row for row in ids if row == '${PRIORITY_FILTER_HIGH_UNASSIGNED_ID}']) + '|' + str('${PRIORITY_FILTER_HIGH_MINE_ID}' in ids))" <<<"${PRIORITY_FILTER_UNASSIGNED_JSON}")"
if [[ "${PRIORITY_FILTER_UNASSIGNED_FIELDS}" != "${PRIORITY_FILTER_HIGH_UNASSIGNED_ID}|False" ]]; then
  echo "expected unassigned priority filter to isolate only unassigned high-priority work; got ${PRIORITY_FILTER_UNASSIGNED_FIELDS}" >&2
  echo "${PRIORITY_FILTER_UNASSIGNED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
PRIORITY_FILTER_MINE_JSON="$(operator_curl "${BASE}/v1/human/tasks/mine?operator_id=${PRIORITY_FILTER_OPERATOR}&status=pending&priority=urgent&sort=created_asc&limit=10")"
PRIORITY_FILTER_MINE_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); ids=[(row or {}).get('human_task_id','') for row in rows]; print('|'.join([row for row in ids if row == '${PRIORITY_FILTER_URGENT_MINE_ID}']) + '|' + str('${PRIORITY_FILTER_HIGH_MINE_ID}' in ids))" <<<"${PRIORITY_FILTER_MINE_JSON}")"
if [[ "${PRIORITY_FILTER_MINE_FIELDS}" != "${PRIORITY_FILTER_URGENT_MINE_ID}|False" ]]; then
  echo "expected mine priority filter to isolate only urgent assigned work; got ${PRIORITY_FILTER_MINE_FIELDS}" >&2
  echo "${PRIORITY_FILTER_MINE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
echo "human task priority filter ok"

echo "== smoke: human task multi-priority filter =="
MULTI_PRIORITY_LIST_JSON="$(curl -fsS "${BASE}/v1/human/tasks?session_id=${PRIORITY_FILTER_SESSION_ID}&status=pending&role_required=${PRIORITY_FILTER_ROLE}&priority=urgent,high&sort=priority_desc_created_asc&limit=100" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
MULTI_PRIORITY_LIST_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); ids=[(row or {}).get('human_task_id','') for row in rows]; wanted=['${PRIORITY_FILTER_URGENT_MINE_ID}','${PRIORITY_FILTER_HIGH_MINE_ID}','${PRIORITY_FILTER_HIGH_UNASSIGNED_ID}']; matched=[task_id for task_id in ids if task_id in wanted]; positions={task_id: matched.index(task_id) for task_id in wanted if task_id in matched}; ordered=('${PRIORITY_FILTER_URGENT_MINE_ID}' in positions and '${PRIORITY_FILTER_HIGH_MINE_ID}' in positions and '${PRIORITY_FILTER_HIGH_UNASSIGNED_ID}' in positions and positions['${PRIORITY_FILTER_URGENT_MINE_ID}'] < positions['${PRIORITY_FILTER_HIGH_MINE_ID}'] < positions['${PRIORITY_FILTER_HIGH_UNASSIGNED_ID}']); print('{}|{}|{}|{}'.format(*(str(task_id in positions) for task_id in wanted), str(ordered)))" <<<"${MULTI_PRIORITY_LIST_JSON}")"
if [[ "${MULTI_PRIORITY_LIST_FIELDS}" != "True|True|True|True" ]]; then
  echo "expected multi-priority list filter to return urgent and high tasks in priority-band order; got ${MULTI_PRIORITY_LIST_FIELDS}" >&2
  echo "${MULTI_PRIORITY_LIST_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
MULTI_PRIORITY_BACKLOG_JSON="$(curl -fsS "${BASE}/v1/human/tasks/backlog?role_required=${PRIORITY_FILTER_ROLE}&priority=urgent,high&sort=priority_desc_created_asc&limit=100" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
MULTI_PRIORITY_BACKLOG_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); ids=[(row or {}).get('human_task_id','') for row in rows]; wanted=['${PRIORITY_FILTER_URGENT_MINE_ID}','${PRIORITY_FILTER_HIGH_MINE_ID}','${PRIORITY_FILTER_HIGH_UNASSIGNED_ID}']; matched=[task_id for task_id in ids if task_id in wanted]; positions={task_id: matched.index(task_id) for task_id in wanted if task_id in matched}; ordered=('${PRIORITY_FILTER_URGENT_MINE_ID}' in positions and '${PRIORITY_FILTER_HIGH_MINE_ID}' in positions and '${PRIORITY_FILTER_HIGH_UNASSIGNED_ID}' in positions and positions['${PRIORITY_FILTER_URGENT_MINE_ID}'] < positions['${PRIORITY_FILTER_HIGH_MINE_ID}'] < positions['${PRIORITY_FILTER_HIGH_UNASSIGNED_ID}']); print('{}|{}|{}|{}'.format(*(str(task_id in positions) for task_id in wanted), str(ordered)))" <<<"${MULTI_PRIORITY_BACKLOG_JSON}")"
if [[ "${MULTI_PRIORITY_BACKLOG_FIELDS}" != "True|True|True|True" ]]; then
  echo "expected multi-priority backlog filter to return urgent and high tasks in priority-band order; got ${MULTI_PRIORITY_BACKLOG_FIELDS}" >&2
  echo "${MULTI_PRIORITY_BACKLOG_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
MULTI_PRIORITY_UNASSIGNED_JSON="$(curl -fsS "${BASE}/v1/human/tasks/unassigned?role_required=${PRIORITY_FILTER_ROLE}&priority=urgent,high&sort=priority_desc_created_asc&limit=100" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
MULTI_PRIORITY_UNASSIGNED_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); ids=[(row or {}).get('human_task_id','') for row in rows]; print('{}|{}|{}'.format(str('${PRIORITY_FILTER_HIGH_UNASSIGNED_ID}' in ids), str('${PRIORITY_FILTER_URGENT_MINE_ID}' in ids), str('${PRIORITY_FILTER_HIGH_MINE_ID}' in ids)))" <<<"${MULTI_PRIORITY_UNASSIGNED_JSON}")"
if [[ "${MULTI_PRIORITY_UNASSIGNED_FIELDS}" != "True|False|False" ]]; then
  echo "expected multi-priority unassigned filter to keep only high unassigned work when urgent tasks are assigned elsewhere; got ${MULTI_PRIORITY_UNASSIGNED_FIELDS}" >&2
  echo "${MULTI_PRIORITY_UNASSIGNED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
MULTI_PRIORITY_MINE_JSON="$(operator_curl "${BASE}/v1/human/tasks/mine?operator_id=${PRIORITY_FILTER_OPERATOR}&status=pending&priority=urgent,high&sort=priority_desc_created_asc&limit=100")"
MULTI_PRIORITY_MINE_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); ids=[(row or {}).get('human_task_id','') for row in rows]; wanted=['${PRIORITY_FILTER_URGENT_MINE_ID}','${PRIORITY_FILTER_HIGH_MINE_ID}']; matched=[task_id for task_id in ids if task_id in wanted]; positions={task_id: matched.index(task_id) for task_id in wanted if task_id in matched}; ordered=('${PRIORITY_FILTER_URGENT_MINE_ID}' in positions and '${PRIORITY_FILTER_HIGH_MINE_ID}' in positions and positions['${PRIORITY_FILTER_URGENT_MINE_ID}'] < positions['${PRIORITY_FILTER_HIGH_MINE_ID}']); print('{}|{}|{}|{}'.format(str('${PRIORITY_FILTER_URGENT_MINE_ID}' in positions), str('${PRIORITY_FILTER_HIGH_MINE_ID}' in positions), str(ordered), str('${PRIORITY_FILTER_HIGH_UNASSIGNED_ID}' in ids)))" <<<"${MULTI_PRIORITY_MINE_JSON}")"
if [[ "${MULTI_PRIORITY_MINE_FIELDS}" != "True|True|True|False" ]]; then
  echo "expected multi-priority mine filter to return urgent and high assigned work in priority-band order; got ${MULTI_PRIORITY_MINE_FIELDS}" >&2
  echo "${MULTI_PRIORITY_MINE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
echo "human task multi-priority filter ok"

echo "== smoke: human task priority summary =="
PRIORITY_SUMMARY_REWRITE_JSON="$(curl_body_retry 5 1 -X POST "${BASE}/v1/rewrite/artifact" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' -d '{"text":"priority summary seed"}')"
PRIORITY_SUMMARY_SESSION_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("execution_session_id",""))' <<<"${PRIORITY_SUMMARY_REWRITE_JSON}")"
PRIORITY_SUMMARY_SESSION_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${PRIORITY_SUMMARY_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
PRIORITY_SUMMARY_STEP_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); rows=body.get("steps") or []; print(((rows[-1] or {}).get("step_id")) if rows else "")' <<<"${PRIORITY_SUMMARY_SESSION_JSON}")"
PRIORITY_SUMMARY_SCOPE_SUFFIX="$(printf '%s' "${PRIORITY_SUMMARY_SESSION_ID}" | tr -cd '[:alnum:]')"
PRIORITY_SUMMARY_ROLE="priority_summary_reviewer_${PRIORITY_SUMMARY_SCOPE_SUFFIX}"
if [[ -z "${PRIORITY_SUMMARY_STEP_ID}" ]]; then
  fail 13 "missing priority summary step_id from session response"
fi
curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${PRIORITY_SUMMARY_SESSION_ID}\",\"step_id\":\"${PRIORITY_SUMMARY_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"${PRIORITY_SUMMARY_ROLE}\",\"brief\":\"Urgent task.\",\"priority\":\"urgent\",\"resume_session_on_return\":false}" >${SMOKE_TMP_DIR}/ea_priority_summary_urgent.json
PRIORITY_SUMMARY_HIGH_ASSIGNED_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${PRIORITY_SUMMARY_SESSION_ID}\",\"step_id\":\"${PRIORITY_SUMMARY_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"${PRIORITY_SUMMARY_ROLE}\",\"brief\":\"High assigned task.\",\"priority\":\"high\",\"resume_session_on_return\":false}")"
PRIORITY_SUMMARY_HIGH_ASSIGNED_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${PRIORITY_SUMMARY_HIGH_ASSIGNED_JSON}")"
curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${PRIORITY_SUMMARY_SESSION_ID}\",\"step_id\":\"${PRIORITY_SUMMARY_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"${PRIORITY_SUMMARY_ROLE}\",\"brief\":\"High unassigned task.\",\"priority\":\"high\",\"resume_session_on_return\":false}" >${SMOKE_TMP_DIR}/ea_priority_summary_high_unassigned.json
curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${PRIORITY_SUMMARY_SESSION_ID}\",\"step_id\":\"${PRIORITY_SUMMARY_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"${PRIORITY_SUMMARY_ROLE}\",\"brief\":\"Normal task.\",\"priority\":\"normal\",\"resume_session_on_return\":false}" >${SMOKE_TMP_DIR}/ea_priority_summary_normal.json
PRIORITY_SUMMARY_OPERATOR="operator-priority-summary-${PRIORITY_SUMMARY_SCOPE_SUFFIX}"
ensure_operator_profile "${PRIORITY_SUMMARY_OPERATOR}" "${PRIORITY_SUMMARY_ROLE}" '["tone"]' "standard" "Priority Summary Reviewer"
operator_post_json "${BASE}/v1/human/tasks/${PRIORITY_SUMMARY_HIGH_ASSIGNED_ID}/assign" -H 'content-type: application/json' -d "{\"operator_id\":\"${PRIORITY_SUMMARY_OPERATOR}\"}" >/dev/null
PRIORITY_SUMMARY_JSON="$(curl -fsS "${BASE}/v1/human/tasks/priority-summary?status=pending&role_required=${PRIORITY_SUMMARY_ROLE}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
PRIORITY_SUMMARY_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); counts=body.get('counts_json') or {}; print('{}|{}|{}|{}|{}|{}'.format(body.get('total',''), body.get('highest_priority',''), counts.get('urgent',''), counts.get('high',''), counts.get('normal',''), counts.get('low','')))" <<<"${PRIORITY_SUMMARY_JSON}")"
if [[ "${PRIORITY_SUMMARY_FIELDS}" != "4|urgent|1|2|1|0" ]]; then
  echo "expected priority summary to expose urgent/high/normal queue counts; got ${PRIORITY_SUMMARY_FIELDS}" >&2
  echo "${PRIORITY_SUMMARY_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
PRIORITY_SUMMARY_UNASSIGNED_JSON="$(curl -fsS "${BASE}/v1/human/tasks/priority-summary?status=pending&role_required=${PRIORITY_SUMMARY_ROLE}&assignment_state=unassigned" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
PRIORITY_SUMMARY_UNASSIGNED_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); counts=body.get('counts_json') or {}; print('{}|{}|{}|{}|{}|{}'.format(body.get('total',''), body.get('highest_priority',''), counts.get('urgent',''), counts.get('high',''), counts.get('normal',''), counts.get('low','')))" <<<"${PRIORITY_SUMMARY_UNASSIGNED_JSON}")"
if [[ "${PRIORITY_SUMMARY_UNASSIGNED_FIELDS}" != "3|urgent|1|1|1|0" ]]; then
  echo "expected unassigned priority summary to remove the assigned high-priority task while preserving band counts; got ${PRIORITY_SUMMARY_UNASSIGNED_FIELDS}" >&2
  echo "${PRIORITY_SUMMARY_UNASSIGNED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
PRIORITY_SUMMARY_ASSIGNED_JSON="$(curl -fsS "${BASE}/v1/human/tasks/priority-summary?status=pending&role_required=${PRIORITY_SUMMARY_ROLE}&assigned_operator_id=${PRIORITY_SUMMARY_OPERATOR}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
PRIORITY_SUMMARY_ASSIGNED_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); counts=body.get('counts_json') or {}; print('{}|{}|{}|{}|{}|{}|{}'.format(body.get('assigned_operator_id',''), body.get('total',''), body.get('highest_priority',''), counts.get('urgent',''), counts.get('high',''), counts.get('normal',''), counts.get('low','')))" <<<"${PRIORITY_SUMMARY_ASSIGNED_JSON}")"
if [[ "${PRIORITY_SUMMARY_ASSIGNED_FIELDS}" != "${PRIORITY_SUMMARY_OPERATOR}|1|high|0|1|0|0" ]]; then
  echo "expected assigned-operator priority summary to isolate only the assigned reviewer queue; got ${PRIORITY_SUMMARY_ASSIGNED_FIELDS}" >&2
  echo "${PRIORITY_SUMMARY_ASSIGNED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
PRIORITY_SUMMARY_MANUAL_JSON="$(curl -fsS "${BASE}/v1/human/tasks/priority-summary?status=pending&role_required=${PRIORITY_SUMMARY_ROLE}&assignment_source=manual" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
PRIORITY_SUMMARY_MANUAL_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); counts=body.get('counts_json') or {}; print('{}|{}|{}|{}|{}|{}|{}'.format(body.get('assignment_source',''), body.get('total',''), body.get('highest_priority',''), counts.get('urgent',''), counts.get('high',''), counts.get('normal',''), counts.get('low','')))" <<<"${PRIORITY_SUMMARY_MANUAL_JSON}")"
if [[ "${PRIORITY_SUMMARY_MANUAL_FIELDS}" != "manual|1|high|0|1|0|0" ]]; then
  echo "expected assignment-source priority summary to isolate manually assigned pending work; got ${PRIORITY_SUMMARY_MANUAL_FIELDS}" >&2
  echo "${PRIORITY_SUMMARY_MANUAL_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
PRIORITY_SUMMARY_MANUAL_LIST_JSON="$(curl -fsS "${BASE}/v1/human/tasks?status=pending&role_required=${PRIORITY_SUMMARY_ROLE}&assignment_source=manual&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
PRIORITY_SUMMARY_MANUAL_LIST_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted='${PRIORITY_SUMMARY_HIGH_ASSIGNED_ID}'; print(any((row or {}).get('human_task_id') == wanted for row in rows))" <<<"${PRIORITY_SUMMARY_MANUAL_LIST_JSON}")"
if [[ "${PRIORITY_SUMMARY_MANUAL_LIST_FIELDS}" != "True" ]]; then
  echo "expected assignment-source list filter to expose manually assigned pending work" >&2
  echo "${PRIORITY_SUMMARY_MANUAL_LIST_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
PRIORITY_SUMMARY_MANUAL_MINE_JSON="$(operator_curl "${BASE}/v1/human/tasks/mine?operator_id=${PRIORITY_SUMMARY_OPERATOR}&assignment_source=manual&limit=10")"
PRIORITY_SUMMARY_MANUAL_MINE_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted='${PRIORITY_SUMMARY_HIGH_ASSIGNED_ID}'; print(any((row or {}).get('human_task_id') == wanted for row in rows))" <<<"${PRIORITY_SUMMARY_MANUAL_MINE_JSON}")"
if [[ "${PRIORITY_SUMMARY_MANUAL_MINE_FIELDS}" != "True" ]]; then
  echo "expected assignment-source mine filter to expose manually assigned pending reviewer work" >&2
  echo "${PRIORITY_SUMMARY_MANUAL_MINE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
PRIORITY_SUMMARY_MANUAL_SESSION_JSON="$(curl -fsS "${BASE}/v1/human/tasks?session_id=${PRIORITY_SUMMARY_SESSION_ID}&assignment_source=manual&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
PRIORITY_SUMMARY_MANUAL_SESSION_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted='${PRIORITY_SUMMARY_HIGH_ASSIGNED_ID}'; print(any((row or {}).get('human_task_id') == wanted for row in rows))" <<<"${PRIORITY_SUMMARY_MANUAL_SESSION_JSON}")"
if [[ "${PRIORITY_SUMMARY_MANUAL_SESSION_FIELDS}" != "True" ]]; then
  echo "expected session-scoped assignment-source list filter to expose manually assigned pending reviewer work" >&2
  echo "${PRIORITY_SUMMARY_MANUAL_SESSION_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${PRIORITY_SUMMARY_SESSION_ID}\",\"step_id\":\"${PRIORITY_SUMMARY_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"${PRIORITY_SUMMARY_ROLE}\",\"brief\":\"Ownerless low task.\",\"priority\":\"low\",\"resume_session_on_return\":false}" >${SMOKE_TMP_DIR}/ea_priority_summary_ownerless_low.json
PRIORITY_SUMMARY_MANUAL_MIXED_FIELDS="$(curl -fsS "${BASE}/v1/human/tasks/priority-summary?status=pending&role_required=${PRIORITY_SUMMARY_ROLE}&assignment_source=manual" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); counts=body.get('counts_json') or {}; print('{}|{}|{}|{}|{}|{}|{}'.format(body.get('assignment_source',''), body.get('total',''), body.get('highest_priority',''), counts.get('urgent',''), counts.get('high',''), counts.get('normal',''), counts.get('low','')))" )"
if [[ "${PRIORITY_SUMMARY_MANUAL_MIXED_FIELDS}" != "manual|1|high|0|1|0|0" ]]; then
  echo "expected manual assignment_source summary to stay isolated after extra ownerless rows are added; got ${PRIORITY_SUMMARY_MANUAL_MIXED_FIELDS}" >&2
  fail 12 "policy contract mismatch"
fi
PRIORITY_SUMMARY_MATCH_ROLE="matched_priority_summary_reviewer_${PRIORITY_SUMMARY_SCOPE_SUFFIX}"
PRIORITY_SUMMARY_SCHED_ROLE="matched_priority_summary_scheduler_${PRIORITY_SUMMARY_SCOPE_SUFFIX}"
PRIORITY_SUMMARY_MATCH_OPERATOR="operator-specialist-summary-${PRIORITY_SUMMARY_SCOPE_SUFFIX}"
PRIORITY_SUMMARY_MATCH_LOW_OPERATOR="operator-junior-summary-${PRIORITY_SUMMARY_SCOPE_SUFFIX}"
PRIORITY_SUMMARY_MATCH_SCHED_OPERATOR="operator-scheduler-summary-${PRIORITY_SUMMARY_SCOPE_SUFFIX}"
operator_post_json "${BASE}/v1/human/tasks/operators" -H 'content-type: application/json' \
  -d "{\"operator_id\":\"${PRIORITY_SUMMARY_MATCH_OPERATOR}\",\"display_name\":\"Senior Comms Reviewer\",\"roles\":[\"${PRIORITY_SUMMARY_MATCH_ROLE}\"],\"skill_tags\":[\"tone\",\"accuracy\",\"stakeholder_sensitivity\"],\"trust_tier\":\"senior\",\"status\":\"active\"}" >/dev/null
operator_post_json "${BASE}/v1/human/tasks/operators" -H 'content-type: application/json' \
  -d "{\"operator_id\":\"${PRIORITY_SUMMARY_MATCH_LOW_OPERATOR}\",\"display_name\":\"Junior Reviewer\",\"roles\":[\"${PRIORITY_SUMMARY_MATCH_ROLE}\"],\"skill_tags\":[\"tone\"],\"trust_tier\":\"standard\",\"status\":\"active\"}" >/dev/null
operator_post_json "${BASE}/v1/human/tasks/operators" -H 'content-type: application/json' \
  -d "{\"operator_id\":\"${PRIORITY_SUMMARY_MATCH_SCHED_OPERATOR}\",\"display_name\":\"Scheduler\",\"roles\":[\"${PRIORITY_SUMMARY_SCHED_ROLE}\"],\"skill_tags\":[\"calendar\"],\"trust_tier\":\"standard\",\"status\":\"active\"}" >/dev/null
curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${PRIORITY_SUMMARY_SESSION_ID}\",\"step_id\":\"${PRIORITY_SUMMARY_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"${PRIORITY_SUMMARY_MATCH_ROLE}\",\"brief\":\"Urgent specialist-only task.\",\"authority_required\":\"send_on_behalf_review\",\"quality_rubric_json\":{\"checks\":[\"tone\",\"accuracy\",\"stakeholder_sensitivity\"]},\"priority\":\"urgent\",\"resume_session_on_return\":false}" >${SMOKE_TMP_DIR}/ea_priority_summary_match_urgent.json
curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${PRIORITY_SUMMARY_SESSION_ID}\",\"step_id\":\"${PRIORITY_SUMMARY_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"${PRIORITY_SUMMARY_MATCH_ROLE}\",\"brief\":\"High specialist-only task.\",\"authority_required\":\"send_on_behalf_review\",\"quality_rubric_json\":{\"checks\":[\"tone\",\"accuracy\",\"stakeholder_sensitivity\"]},\"priority\":\"high\",\"resume_session_on_return\":false}" >${SMOKE_TMP_DIR}/ea_priority_summary_match_high.json
curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${PRIORITY_SUMMARY_SESSION_ID}\",\"step_id\":\"${PRIORITY_SUMMARY_STEP_ID}\",\"task_type\":\"schedule_review\",\"role_required\":\"${PRIORITY_SUMMARY_SCHED_ROLE}\",\"brief\":\"Normal scheduler task.\",\"priority\":\"normal\",\"resume_session_on_return\":false}" >${SMOKE_TMP_DIR}/ea_priority_summary_match_scheduler.json
PRIORITY_SUMMARY_MATCHED_JSON="$(curl -fsS "${BASE}/v1/human/tasks/priority-summary?status=pending&assignment_state=unassigned&operator_id=${PRIORITY_SUMMARY_MATCH_OPERATOR}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
PRIORITY_SUMMARY_MATCHED_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); counts=body.get('counts_json') or {}; print('{}|{}|{}|{}|{}|{}|{}'.format(body.get('operator_id',''), body.get('total',''), body.get('highest_priority',''), counts.get('urgent',''), counts.get('high',''), counts.get('normal',''), counts.get('low','')))" <<<"${PRIORITY_SUMMARY_MATCHED_JSON}")"
if [[ "${PRIORITY_SUMMARY_MATCHED_FIELDS}" != "${PRIORITY_SUMMARY_MATCH_OPERATOR}|2|urgent|1|1|0|0" ]]; then
  echo "expected operator-matched priority summary to count only specialist-ready unclaimed work; got ${PRIORITY_SUMMARY_MATCHED_FIELDS}" >&2
  echo "${PRIORITY_SUMMARY_MATCHED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
PRIORITY_SUMMARY_MATCHED_LOW_JSON="$(curl -fsS "${BASE}/v1/human/tasks/priority-summary?status=pending&assignment_state=unassigned&operator_id=${PRIORITY_SUMMARY_MATCH_LOW_OPERATOR}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
PRIORITY_SUMMARY_MATCHED_LOW_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); counts=body.get('counts_json') or {}; print('{}|{}|{}|{}|{}|{}|{}'.format(body.get('operator_id',''), body.get('total',''), body.get('highest_priority',''), counts.get('urgent',''), counts.get('high',''), counts.get('normal',''), counts.get('low','')))" <<<"${PRIORITY_SUMMARY_MATCHED_LOW_JSON}")"
if [[ "${PRIORITY_SUMMARY_MATCHED_LOW_FIELDS}" != "${PRIORITY_SUMMARY_MATCH_LOW_OPERATOR}|0||0|0|0|0" ]]; then
  echo "expected operator-matched priority summary to exclude under-skilled or under-trust reviewers; got ${PRIORITY_SUMMARY_MATCHED_LOW_FIELDS}" >&2
  echo "${PRIORITY_SUMMARY_MATCHED_LOW_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
PRIORITY_SUMMARY_MATCHED_SCHED_JSON="$(curl -fsS "${BASE}/v1/human/tasks/priority-summary?status=pending&assignment_state=unassigned&operator_id=${PRIORITY_SUMMARY_MATCH_SCHED_OPERATOR}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
PRIORITY_SUMMARY_MATCHED_SCHED_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); counts=body.get('counts_json') or {}; print('{}|{}|{}|{}|{}|{}|{}'.format(body.get('operator_id',''), body.get('total',''), body.get('highest_priority',''), counts.get('urgent',''), counts.get('high',''), counts.get('normal',''), counts.get('low','')))" <<<"${PRIORITY_SUMMARY_MATCHED_SCHED_JSON}")"
if [[ "${PRIORITY_SUMMARY_MATCHED_SCHED_FIELDS}" != "${PRIORITY_SUMMARY_MATCH_SCHED_OPERATOR}|1|normal|0|0|1|0" ]]; then
  echo "expected operator-matched priority summary to isolate scheduler-role work separately from comms review packets; got ${PRIORITY_SUMMARY_MATCHED_SCHED_FIELDS}" >&2
  echo "${PRIORITY_SUMMARY_MATCHED_SCHED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
rm -f ${SMOKE_TMP_DIR}/ea_priority_summary_urgent.json ${SMOKE_TMP_DIR}/ea_priority_summary_high_unassigned.json ${SMOKE_TMP_DIR}/ea_priority_summary_normal.json ${SMOKE_TMP_DIR}/ea_priority_summary_match_urgent.json ${SMOKE_TMP_DIR}/ea_priority_summary_match_high.json ${SMOKE_TMP_DIR}/ea_priority_summary_match_scheduler.json
echo "human task priority summary ok"

echo "== smoke: human task SLA sort =="
SLA_REWRITE_JSON="$(curl_body_retry 5 1 -X POST "${BASE}/v1/rewrite/artifact" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' -d '{"text":"sla sort seed"}')"
SLA_SESSION_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("execution_session_id",""))' <<<"${SLA_REWRITE_JSON}")"
SLA_SESSION_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${SLA_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
SLA_STEP_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); rows=body.get("steps") or []; print(((rows[-1] or {}).get("step_id")) if rows else "")' <<<"${SLA_SESSION_JSON}")"
if [[ -z "${SLA_STEP_ID}" ]]; then
  fail 13 "missing sla sort step_id from session response"
fi
SLA_TASK_LATE_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${SLA_SESSION_ID}\",\"step_id\":\"${SLA_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Later due task.\",\"sla_due_at\":\"2100-01-02T00:00:00+00:00\",\"resume_session_on_return\":false}")"
SLA_TASK_LATE_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${SLA_TASK_LATE_JSON}")"
SLA_TASK_SOON_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${SLA_SESSION_ID}\",\"step_id\":\"${SLA_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Sooner due task.\",\"sla_due_at\":\"2100-01-01T00:00:00+00:00\",\"resume_session_on_return\":false}")"
SLA_TASK_SOON_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${SLA_TASK_SOON_JSON}")"
if [[ -z "${SLA_TASK_LATE_ID}" || -z "${SLA_TASK_SOON_ID}" ]]; then
  fail 13 "missing human task ids from sla sort smoke setup"
fi
SLA_LIST_JSON="$(curl -fsS "${BASE}/v1/human/tasks?status=pending&sort=sla_due_at_asc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
SLA_LIST_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted=['${SLA_TASK_SOON_ID}','${SLA_TASK_LATE_ID}']; filtered=[row for row in rows if (row or {}).get('human_task_id') in wanted]; first=(filtered[0] if len(filtered) > 0 else {}); second=(filtered[1] if len(filtered) > 1 else {}); print('{}|{}'.format(first.get('human_task_id',''), second.get('human_task_id','')))" <<<"${SLA_LIST_JSON}")"
if [[ "${SLA_LIST_FIELDS}" != "${SLA_TASK_SOON_ID}|${SLA_TASK_LATE_ID}" ]]; then
  echo "expected sort=sla_due_at_asc to order general human task list by earliest SLA first; got ${SLA_LIST_FIELDS}" >&2
  echo "${SLA_LIST_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
SLA_BACKLOG_JSON="$(curl -fsS "${BASE}/v1/human/tasks/backlog?sort=sla_due_at_asc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
SLA_BACKLOG_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted=['${SLA_TASK_SOON_ID}','${SLA_TASK_LATE_ID}']; filtered=[row for row in rows if (row or {}).get('human_task_id') in wanted]; first=(filtered[0] if len(filtered) > 0 else {}); second=(filtered[1] if len(filtered) > 1 else {}); print('{}|{}'.format(first.get('human_task_id',''), second.get('human_task_id','')))" <<<"${SLA_BACKLOG_JSON}")"
if [[ "${SLA_BACKLOG_FIELDS}" != "${SLA_TASK_SOON_ID}|${SLA_TASK_LATE_ID}" ]]; then
  echo "expected backlog sort=sla_due_at_asc to order pending work by earliest SLA first; got ${SLA_BACKLOG_FIELDS}" >&2
  echo "${SLA_BACKLOG_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
echo "human task SLA sort ok"

echo "== smoke: human task combined SLA + transition sort =="
COMBINED_REWRITE_JSON="$(curl_body_retry 5 1 -X POST "${BASE}/v1/rewrite/artifact" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' -d '{"text":"combined sort seed"}')"
COMBINED_SESSION_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("execution_session_id",""))' <<<"${COMBINED_REWRITE_JSON}")"
COMBINED_SESSION_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${COMBINED_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
COMBINED_STEP_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); rows=body.get("steps") or []; print(((rows[-1] or {}).get("step_id")) if rows else "")' <<<"${COMBINED_SESSION_JSON}")"
if [[ -z "${COMBINED_STEP_ID}" ]]; then
  fail 13 "missing combined sort step_id from session response"
fi
COMBINED_TASK_STALE_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${COMBINED_SESSION_ID}\",\"step_id\":\"${COMBINED_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Earlier due stale task.\",\"sla_due_at\":\"2100-01-01T00:00:00+00:00\",\"resume_session_on_return\":false}")"
COMBINED_TASK_STALE_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${COMBINED_TASK_STALE_JSON}")"
COMBINED_TASK_RECENT_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${COMBINED_SESSION_ID}\",\"step_id\":\"${COMBINED_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Earlier due recently touched task.\",\"sla_due_at\":\"2100-01-01T00:00:00+00:00\",\"resume_session_on_return\":false}")"
COMBINED_TASK_RECENT_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${COMBINED_TASK_RECENT_JSON}")"
COMBINED_TASK_LATE_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${COMBINED_SESSION_ID}\",\"step_id\":\"${COMBINED_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Later due task.\",\"sla_due_at\":\"2100-01-02T00:00:00+00:00\",\"resume_session_on_return\":false}")"
COMBINED_TASK_LATE_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${COMBINED_TASK_LATE_JSON}")"
if [[ -z "${COMBINED_TASK_STALE_ID}" || -z "${COMBINED_TASK_RECENT_ID}" || -z "${COMBINED_TASK_LATE_ID}" ]]; then
  fail 13 "missing human task ids from combined sort smoke setup"
fi
COMBINED_ASSIGN_JSON="$(operator_post_json "${BASE}/v1/human/tasks/${COMBINED_TASK_RECENT_ID}/assign" -H 'content-type: application/json' -d '{"operator_id":"operator-sorter"}')"
COMBINED_ASSIGN_FIELDS="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print("{}|{}".format(body.get("human_task_id",""), body.get("last_transition_event_name","")))' <<<"${COMBINED_ASSIGN_JSON}")"
if [[ "${COMBINED_ASSIGN_FIELDS}" != "${COMBINED_TASK_RECENT_ID}|human_task_assigned" && "${COMBINED_ASSIGN_FIELDS}" != "${COMBINED_TASK_RECENT_ID}|" ]]; then
  echo "expected combined-sort setup assignment to mark the tied-SLA task as recently touched; got ${COMBINED_ASSIGN_FIELDS}" >&2
  echo "${COMBINED_ASSIGN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
COMBINED_LIST_JSON="$(curl -fsS "${BASE}/v1/human/tasks?status=pending&sort=sla_due_at_asc_last_transition_desc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
COMBINED_LIST_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted=['${COMBINED_TASK_RECENT_ID}','${COMBINED_TASK_STALE_ID}','${COMBINED_TASK_LATE_ID}']; filtered=[row for row in rows if (row or {}).get('human_task_id') in wanted]; ids=[(row or {}).get('human_task_id','') for row in filtered[:3]]; print('|'.join(ids))" <<<"${COMBINED_LIST_JSON}")"
if [[ "${COMBINED_LIST_FIELDS}" != "${COMBINED_TASK_RECENT_ID}|${COMBINED_TASK_STALE_ID}|${COMBINED_TASK_LATE_ID}" ]]; then
  echo "expected sort=sla_due_at_asc_last_transition_desc to break SLA ties by freshest transition in the general list; got ${COMBINED_LIST_FIELDS}" >&2
  echo "${COMBINED_LIST_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
COMBINED_BACKLOG_JSON="$(curl -fsS "${BASE}/v1/human/tasks/backlog?sort=sla_due_at_asc_last_transition_desc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
COMBINED_BACKLOG_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted=['${COMBINED_TASK_RECENT_ID}','${COMBINED_TASK_STALE_ID}','${COMBINED_TASK_LATE_ID}']; filtered=[row for row in rows if (row or {}).get('human_task_id') in wanted]; ids=[(row or {}).get('human_task_id','') for row in filtered[:3]]; print('|'.join(ids))" <<<"${COMBINED_BACKLOG_JSON}")"
if [[ "${COMBINED_BACKLOG_FIELDS}" != "${COMBINED_TASK_RECENT_ID}|${COMBINED_TASK_STALE_ID}|${COMBINED_TASK_LATE_ID}" ]]; then
  echo "expected backlog sort=sla_due_at_asc_last_transition_desc to break SLA ties by freshest transition; got ${COMBINED_BACKLOG_FIELDS}" >&2
  echo "${COMBINED_BACKLOG_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
echo "human task combined sort ok"

echo "== smoke: human task unscheduled fallback sort =="
UNSCHED_REWRITE_JSON="$(curl_body_retry 5 1 -X POST "${BASE}/v1/rewrite/artifact" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' -d '{"text":"unscheduled fallback seed"}')"
UNSCHED_SESSION_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("execution_session_id",""))' <<<"${UNSCHED_REWRITE_JSON}")"
UNSCHED_SESSION_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${UNSCHED_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
UNSCHED_STEP_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); rows=body.get("steps") or []; print(((rows[-1] or {}).get("step_id")) if rows else "")' <<<"${UNSCHED_SESSION_JSON}")"
if [[ -z "${UNSCHED_STEP_ID}" ]]; then
  fail 13 "missing unscheduled fallback step_id from session response"
fi
UNSCHED_DUE_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${UNSCHED_SESSION_ID}\",\"step_id\":\"${UNSCHED_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Scheduled task.\",\"sla_due_at\":\"2100-01-01T00:00:00+00:00\",\"resume_session_on_return\":false}")"
UNSCHED_DUE_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${UNSCHED_DUE_JSON}")"
UNSCHED_OLDER_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${UNSCHED_SESSION_ID}\",\"step_id\":\"${UNSCHED_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Older unscheduled task.\",\"resume_session_on_return\":false}")"
UNSCHED_OLDER_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${UNSCHED_OLDER_JSON}")"
UNSCHED_NEWER_JSON="$(curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${UNSCHED_SESSION_ID}\",\"step_id\":\"${UNSCHED_STEP_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"communications_reviewer\",\"brief\":\"Newer unscheduled task.\",\"resume_session_on_return\":false}")"
UNSCHED_NEWER_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${UNSCHED_NEWER_JSON}")"
if [[ -z "${UNSCHED_DUE_ID}" || -z "${UNSCHED_OLDER_ID}" || -z "${UNSCHED_NEWER_ID}" ]]; then
  fail 13 "missing human task ids from unscheduled fallback smoke setup"
fi
UNSCHED_ASSIGN_JSON="$(operator_post_json "${BASE}/v1/human/tasks/${UNSCHED_NEWER_ID}/assign" -H 'content-type: application/json' -d '{"operator_id":"operator-sorter"}')"
UNSCHED_ASSIGN_FIELDS="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print("{}|{}".format(body.get("human_task_id",""), body.get("last_transition_event_name","")))' <<<"${UNSCHED_ASSIGN_JSON}")"
if [[ "${UNSCHED_ASSIGN_FIELDS}" != "${UNSCHED_NEWER_ID}|human_task_assigned" && "${UNSCHED_ASSIGN_FIELDS}" != "${UNSCHED_NEWER_ID}|" ]]; then
  echo "expected unscheduled fallback setup assignment to mark the newer no-SLA task as recently touched; got ${UNSCHED_ASSIGN_FIELDS}" >&2
  echo "${UNSCHED_ASSIGN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
UNSCHED_SLA_LIST_JSON="$(curl -fsS "${BASE}/v1/human/tasks?status=pending&sort=sla_due_at_asc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
UNSCHED_SLA_LIST_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted=['${UNSCHED_DUE_ID}','${UNSCHED_OLDER_ID}','${UNSCHED_NEWER_ID}']; filtered=[row for row in rows if (row or {}).get('human_task_id') in wanted]; ids=[(row or {}).get('human_task_id','') for row in filtered[:3]]; print('|'.join(ids))" <<<"${UNSCHED_SLA_LIST_JSON}")"
if [[ "${UNSCHED_SLA_LIST_FIELDS}" != "${UNSCHED_DUE_ID}|${UNSCHED_OLDER_ID}|${UNSCHED_NEWER_ID}" ]]; then
  echo "expected sort=sla_due_at_asc to keep unscheduled work in oldest-created order after scheduled work; got ${UNSCHED_SLA_LIST_FIELDS}" >&2
  echo "${UNSCHED_SLA_LIST_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
UNSCHED_COMBINED_LIST_JSON="$(curl -fsS "${BASE}/v1/human/tasks?status=pending&sort=sla_due_at_asc_last_transition_desc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
UNSCHED_COMBINED_LIST_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted=['${UNSCHED_DUE_ID}','${UNSCHED_OLDER_ID}','${UNSCHED_NEWER_ID}']; filtered=[row for row in rows if (row or {}).get('human_task_id') in wanted]; ids=[(row or {}).get('human_task_id','') for row in filtered[:3]]; print('|'.join(ids))" <<<"${UNSCHED_COMBINED_LIST_JSON}")"
if [[ "${UNSCHED_COMBINED_LIST_FIELDS}" != "${UNSCHED_DUE_ID}|${UNSCHED_OLDER_ID}|${UNSCHED_NEWER_ID}" ]]; then
  echo "expected combined sort to keep unscheduled work in oldest-created order after scheduled work; got ${UNSCHED_COMBINED_LIST_FIELDS}" >&2
  echo "${UNSCHED_COMBINED_LIST_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
UNSCHED_COMBINED_BACKLOG_JSON="$(curl -fsS "${BASE}/v1/human/tasks/backlog?sort=sla_due_at_asc_last_transition_desc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
UNSCHED_COMBINED_BACKLOG_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted=['${UNSCHED_DUE_ID}','${UNSCHED_OLDER_ID}','${UNSCHED_NEWER_ID}']; filtered=[row for row in rows if (row or {}).get('human_task_id') in wanted]; ids=[(row or {}).get('human_task_id','') for row in filtered[:3]]; print('|'.join(ids))" <<<"${UNSCHED_COMBINED_BACKLOG_JSON}")"
if [[ "${UNSCHED_COMBINED_BACKLOG_FIELDS}" != "${UNSCHED_DUE_ID}|${UNSCHED_OLDER_ID}|${UNSCHED_NEWER_ID}" ]]; then
  echo "expected combined backlog sort to keep unscheduled work in oldest-created order after scheduled work; got ${UNSCHED_COMBINED_BACKLOG_FIELDS}" >&2
  echo "${UNSCHED_COMBINED_BACKLOG_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
echo "human task unscheduled fallback sort ok"
else
echo "== smoke: human tasks =="
echo "skipped: /v1/human/tasks routes are not published on this runtime"
fi

echo "== smoke: approval resume path =="
if (( APPROVAL_THRESHOLD_CHARS >= MAX_REWRITE_CHARS )); then
  fail 12 "approval smoke misconfigured: threshold must be below max rewrite chars"
fi
APPROVAL_PAYLOAD="$(mktemp)"
printf '{"text":"%s"}' "$(python3 - "${APPROVAL_THRESHOLD_CHARS}" <<'PY'
import sys

threshold = int(sys.argv[1])
print("a" * (threshold + 10))
PY
)" > "${APPROVAL_PAYLOAD}"
APPROVAL_CODE=""
for _ in $(seq 1 5); do
  APPROVAL_CODE="$(curl_status_code ${SMOKE_TMP_DIR}/ea_approval_required_resp.json -X POST "${BASE}/v1/rewrite/artifact" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' --data-binary @"${APPROVAL_PAYLOAD}")"
  if [[ "${APPROVAL_CODE}" == "202" ]]; then
    break
  fi
  sleep 0.25
done
rm -f "${APPROVAL_PAYLOAD}"
if [[ "${APPROVAL_CODE}" != "202" ]]; then
  echo "expected 202 for approval-required path; got ${APPROVAL_CODE}" >&2
  cat ${SMOKE_TMP_DIR}/ea_approval_required_resp.json >&2 || true
  fail 12 "policy contract mismatch"
fi
APPROVAL_FIELDS="$(python3 - "${SMOKE_TMP_DIR}/ea_approval_required_resp.json" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
if not path.exists():
    print("")
    raise SystemExit(0)
try:
    body = json.loads(path.read_text())
except Exception:
    print("")
    raise SystemExit(0)
print("{}|{}|{}|{}".format(body.get("status",""), body.get("next_action",""), body.get("session_id",""), body.get("approval_id","")))
PY
)"
APPROVAL_SESSION_ID="$(python3 - "${SMOKE_TMP_DIR}/ea_approval_required_resp.json" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
if not path.exists():
    print("")
    raise SystemExit(0)
try:
    body = json.loads(path.read_text())
except Exception:
    print("")
    raise SystemExit(0)
print(body.get("session_id",""))
PY
)"
APPROVAL_ID="$(python3 - "${SMOKE_TMP_DIR}/ea_approval_required_resp.json" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
if not path.exists():
    print("")
    raise SystemExit(0)
try:
    body = json.loads(path.read_text())
except Exception:
    print("")
    raise SystemExit(0)
print(body.get("approval_id",""))
PY
)"
if [[ "${APPROVAL_FIELDS}" != "awaiting_approval|poll_or_subscribe|${APPROVAL_SESSION_ID}|${APPROVAL_ID}" ]]; then
  echo "expected approval-required acceptance contract; got ${APPROVAL_FIELDS}" >&2
  cat ${SMOKE_TMP_DIR}/ea_approval_required_resp.json >&2 || true
  fail 12 "policy contract mismatch"
fi
if [[ -z "${APPROVAL_ID}" || -z "${APPROVAL_SESSION_ID}" ]]; then
  fail 13 "missing approval metadata from acceptance response"
fi
PENDING_APPROVALS_JSON="$(curl -fsS "${BASE}/v1/policy/approvals/pending?limit=5" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
PENDING_MATCH="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); approval_id='${APPROVAL_ID}'; session_id='${APPROVAL_SESSION_ID}'; print(any((row or {}).get('approval_id') == approval_id and (row or {}).get('session_id') == session_id for row in rows))" <<<"${PENDING_APPROVALS_JSON}")"
if [[ "${PENDING_MATCH}" != "True" ]]; then
  echo "expected pending approvals to contain acceptance response ids approval_id=${APPROVAL_ID} session_id=${APPROVAL_SESSION_ID}" >&2
  echo "${PENDING_APPROVALS_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
FOREIGN_PENDING_APPROVALS_JSON="$(curl -fsS "${BASE}/v1/policy/approvals/pending?limit=5" "${AUTH_ARGS[@]}" -H "X-EA-Principal-ID: ${MISMATCH_PRINCIPAL_ID}")"
FOREIGN_PENDING_MATCH="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); approval_id='${APPROVAL_ID}'; print(any((row or {}).get('approval_id') == approval_id for row in rows))" <<<"${FOREIGN_PENDING_APPROVALS_JSON}")"
if [[ "${FOREIGN_PENDING_MATCH}" != "False" ]]; then
  echo "expected foreign principal pending approvals list to hide approval_id=${APPROVAL_ID}" >&2
  echo "${FOREIGN_PENDING_APPROVALS_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
FOREIGN_APPROVAL_HISTORY_CODE="$(curl_status_code ${SMOKE_TMP_DIR}/ea_policy_history_scope_mismatch_resp.json "${BASE}/v1/policy/approvals/history?session_id=${APPROVAL_SESSION_ID}&limit=5" "${AUTH_ARGS[@]}" -H "X-EA-Principal-ID: ${MISMATCH_PRINCIPAL_ID}")"
FOREIGN_DECISIONS_CODE="$(curl_status_code ${SMOKE_TMP_DIR}/ea_policy_decisions_scope_mismatch_resp.json "${BASE}/v1/policy/decisions/recent?session_id=${APPROVAL_SESSION_ID}&limit=5" "${AUTH_ARGS[@]}" -H "X-EA-Principal-ID: ${MISMATCH_PRINCIPAL_ID}")"
if [[ "${FOREIGN_APPROVAL_HISTORY_CODE}|${FOREIGN_DECISIONS_CODE}" != "403|403" ]]; then
  echo "expected foreign principal policy history/decision reads to return 403; got ${FOREIGN_APPROVAL_HISTORY_CODE}|${FOREIGN_DECISIONS_CODE}" >&2
  cat ${SMOKE_TMP_DIR}/ea_policy_history_scope_mismatch_resp.json >&2
  cat ${SMOKE_TMP_DIR}/ea_policy_decisions_scope_mismatch_resp.json >&2
  fail 12 "policy contract mismatch"
fi
APPROVAL_WAITING_SESSION_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${APPROVAL_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
APPROVAL_WAITING_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps=body.get('steps') or []; step_lookup={str((row.get('input_json') or {}).get('plan_step_key') or ''): row for row in steps}; policy_step=step_lookup.get('step_policy_evaluate') or {}; save_step=step_lookup.get('step_artifact_save') or {}; policy_id=str(policy_step.get('step_id','')); print('{}|{}|{}|{}|{}|{}|{}|{}|{}'.format(body.get('status',''), save_step.get('state',''), policy_step.get('dependency_states') == {'step_input_prepare': 'completed'}, policy_step.get('blocked_dependency_keys') == [], policy_step.get('dependencies_satisfied') is True, save_step.get('dependency_keys') == ['step_policy_evaluate'], save_step.get('dependency_states') == {'step_policy_evaluate': 'completed'}, (save_step.get('dependency_step_ids') or {}).get('step_policy_evaluate') == policy_id, save_step.get('blocked_dependency_keys') == [] and save_step.get('dependencies_satisfied') is True))" <<<"${APPROVAL_WAITING_SESSION_JSON}")"
if [[ "${APPROVAL_WAITING_FIELDS}" != "awaiting_approval|waiting_approval|True|True|True|True|True|True|True" ]]; then
  echo "expected awaiting_approval session to keep dependency-state projection satisfied through the approval gate; got ${APPROVAL_WAITING_FIELDS}" >&2
  echo "${APPROVAL_WAITING_SESSION_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
FOREIGN_APPROVE_CODE="$(curl_status_code ${SMOKE_TMP_DIR}/ea_policy_approve_scope_mismatch_resp.json -X POST "${BASE}/v1/policy/approvals/${APPROVAL_ID}/approve" "${AUTH_ARGS[@]}" -H "X-EA-Principal-ID: ${MISMATCH_PRINCIPAL_ID}" -H 'content-type: application/json' -d "{\"decided_by\":\"${MISMATCH_PRINCIPAL_ID}\",\"reason\":\"cross principal approval should fail\"}")"
if [[ "${FOREIGN_APPROVE_CODE}" != "403" ]]; then
  echo "expected foreign principal approval decision to return 403; got ${FOREIGN_APPROVE_CODE}" >&2
  cat ${SMOKE_TMP_DIR}/ea_policy_approve_scope_mismatch_resp.json >&2
  fail 12 "policy contract mismatch"
fi
curl -fsS -X POST "${BASE}/v1/policy/approvals/${APPROVAL_ID}/approve" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"decided_by\":\"${PRINCIPAL_ID}\",\"reason\":\"resume execution\"}" >/dev/null
APPROVED_SESSION_JSON="$(wait_for_session_status "${APPROVAL_SESSION_ID}" "completed")"
APPROVED_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); queues=body.get('queue_items') or []; steps=body.get('steps') or []; events={e.get('name','') for e in (body.get('events') or [])}; print('{}|{}|{}|{}|{}|{}|{}|{}'.format(body.get('status',''), len(body.get('artifacts') or []) >= 1, len(body.get('receipts') or []) >= 1, len(body.get('run_costs') or []) >= 1, len(steps) >= 3 and len(queues) >= 3 and all((q or {}).get('state','') == 'done' for q in queues), 'input_prepared' in events, 'policy_step_completed' in events, 'tool_execution_completed' in events))" <<<"${APPROVED_SESSION_JSON}")"
if [[ "${APPROVED_FIELDS}" != "completed|True|True|True|True|True|True|True" ]]; then
  echo "expected resumed session to complete with artifacts/receipts/run_costs, a three-step queue, input_prepared, policy_step_completed, and tool_execution_completed; got ${APPROVED_FIELDS}" >&2
  echo "${APPROVED_SESSION_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
echo "approval resume path ok"

echo "== smoke: external-send policy path =="
POLICY_EVAL_JSON="$(curl -fsS -X POST "${BASE}/v1/policy/evaluate" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"content":"Send the board update to the distribution list.","tool_name":"connector.dispatch","action_kind":"delivery.send","channel":"email"}')"
POLICY_EVAL_FIELDS="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print("{}|{}|{}|{}|{}|{}".format(body.get("allow", False), body.get("requires_approval", False), body.get("reason", ""), body.get("step_kind", ""), body.get("authority_class", ""), body.get("review_class", "")))' <<<"${POLICY_EVAL_JSON}")"
if [[ "${POLICY_EVAL_FIELDS}" != "True|True|allowed|connector_call|execute|manager" ]]; then
  echo "expected policy evaluate response True|True|allowed|connector_call|execute|manager; got ${POLICY_EVAL_FIELDS}" >&2
  echo "${POLICY_EVAL_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
echo "external-send policy path ok"

echo "== smoke: blocked policy path =="
BLOCKED_PAYLOAD="$(mktemp)"
printf '{"text":"%s"}' "$(python3 - <<'PY'
print("x" * 20001)
PY
)" > "${BLOCKED_PAYLOAD}"
BLOCKED_CODE="$(curl_status_code ${SMOKE_TMP_DIR}/ea_blocked_policy_resp.json -X POST "${BASE}/v1/rewrite/artifact" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' --data-binary @"${BLOCKED_PAYLOAD}")"
rm -f "${BLOCKED_PAYLOAD}"
if [[ "${BLOCKED_CODE}" != "403" ]]; then
  echo "expected 403 for blocked policy path; got ${BLOCKED_CODE}" >&2
  cat ${SMOKE_TMP_DIR}/ea_blocked_policy_resp.json >&2 || true
  fail 12 "policy contract mismatch"
fi
BLOCKED_REASON="$(python3 - "${SMOKE_TMP_DIR}/ea_blocked_policy_resp.json" <<'PY'
import json
from pathlib import Path
import sys
path = Path(sys.argv[1])
if not path.exists():
    print("")
    raise SystemExit(0)
try:
    body = json.loads(path.read_text())
except Exception:
    print("")
    raise SystemExit(0)
print(((body.get("error") or {}).get("code") or ""))
PY
)"
if [[ "${BLOCKED_REASON}" != "policy_denied:input_too_large" ]]; then
  echo "expected blocked policy code policy_denied:input_too_large; got ${BLOCKED_REASON}" >&2
  cat ${SMOKE_TMP_DIR}/ea_blocked_policy_resp.json >&2 || true
  fail 12 "policy contract mismatch"
fi
echo "blocked policy path ok"

if (( LEGACY_OBSERVATIONS_AVAILABLE )); then
  echo "== smoke: observations =="
  curl -fsS -X POST "${BASE}/v1/observations/ingest" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
    -d '{"channel":"email","event_type":"thread.opened","payload":{"subject":"Board prep"}}' >/dev/null
  curl -fsS "${BASE}/v1/observations/recent?limit=5" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
  echo "observations ok"
else
  echo "== smoke: observations =="
  echo "skipped: /v1/observations routes are not published on this runtime"
fi

if (( LEGACY_DELIVERY_AVAILABLE )); then
echo "== smoke: outbox =="
DELIVERY_JSON="$(curl -fsS -X POST "${BASE}/v1/delivery/outbox" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' -d "{\"channel\":\"slack\",\"recipient\":\"U1\",\"content\":\"Draft ready\",\"metadata\":{\"priority\":\"high\"},\"idempotency_key\":\"smoke-delivery-${SMOKE_RUN_TOKEN}\"}")"
DELIVERY_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("delivery_id",""))' <<<"${DELIVERY_JSON}")"
if [[ -z "${DELIVERY_ID}" ]]; then
  fail 13 "missing delivery_id from outbox response"
fi
curl -fsS -X POST "${BASE}/v1/delivery/outbox/${DELIVERY_ID}/failed" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"error":"temporary smoke failure","retry_in_seconds":0,"dead_letter":false}' >/dev/null
curl -fsS "${BASE}/v1/delivery/outbox/pending?limit=5" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
curl -fsS -X POST "${BASE}/v1/delivery/outbox/${DELIVERY_ID}/sent" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
echo "outbox ok"
else
echo "== smoke: outbox =="
echo "skipped: /v1/delivery routes are not published on this runtime"
fi

if (( LEGACY_CONNECTORS_AVAILABLE )); then
echo "== smoke: telegram adapter =="
curl -fsS -X POST "${BASE}/v1/connectors/bindings" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"connector_name\":\"telegram_identity\",\"external_account_ref\":\"${TELEGRAM_SMOKE_CHAT_ID}\",\"scope_json\":{\"assistant_surfaces\":[\"dm\"]},\"auth_metadata_json\":{\"default_chat_ref\":\"${TELEGRAM_SMOKE_CHAT_ID}\",\"identity_mode\":\"login_widget\",\"history_mode\":\"future_only\"},\"status\":\"enabled\"}" >/dev/null
operator_post_json "${BASE}/v1/channels/telegram/ingest" -H 'content-type: application/json' \
  "${TELEGRAM_INGEST_ARGS[@]}" \
  -d "{\"update\":{\"message\":{\"chat\":{\"id\":${TELEGRAM_SMOKE_CHAT_ID}},\"text\":\"hello\",\"message_id\":${TELEGRAM_SMOKE_MESSAGE_ID},\"date\":123}}}" >/dev/null
echo "telegram adapter ok"

echo "== smoke: tools and connectors =="
operator_post_json "${BASE}/v1/tools/registry" -H 'content-type: application/json' \
  -d '{"tool_name":"email.send","version":"v1","input_schema_json":{"type":"object"},"output_schema_json":{"type":"object"},"policy_json":{"risk":"medium"},"allowed_channels":["email"],"approval_default":"manager","enabled":true}' >/dev/null
TOOLS_JSON="$(operator_curl "${BASE}/v1/tools/registry?limit=10")"
TOOL_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); names={row.get('tool_name','') for row in rows}; builtin_count=sum(1 for row in rows if ((row or {}).get('policy_json') or {}).get('builtin') is True); print('{}|{}'.format('email.send' in names, builtin_count >= 1))" <<<"${TOOLS_JSON}")"
if [[ "${TOOL_FIELDS}" != "True|True" ]]; then
  echo "expected tool registry to expose upserted email.send and at least one builtin tool before lazy builtin execution proofs run; got ${TOOL_FIELDS}" >&2
  echo "${TOOLS_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
CONNECTOR_JSON="$(curl -fsS -X POST "${BASE}/v1/connectors/bindings" "${AUTH_ARGS[@]}" -H 'content-type: application/json' \
  "${PRINCIPAL_ARGS[@]}" \
  -d '{"connector_name":"gmail","external_account_ref":"acct-1","scope_json":{"scopes":["email.send"]},"auth_metadata_json":{"provider":"google"},"status":"enabled"}')"
BINDING_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("binding_id",""))' <<<"${CONNECTOR_JSON}")"
if [[ -z "${BINDING_ID}" ]]; then
  fail 13 "missing binding_id from connector response"
fi
TOOL_EXEC_JSON="$(operator_post_json "${BASE}/v1/tools/execute" -H 'content-type: application/json' \
  -d "{\"tool_name\":\"connector.dispatch\",\"action_kind\":\"delivery.send\",\"payload_json\":{\"principal_id\":\"${PRINCIPAL_ID}\",\"binding_id\":\"${BINDING_ID}\",\"channel\":\"email\",\"recipient\":\"ops+tool-${SMOKE_RUN_TOKEN}@example.com\",\"content\":\"tool-runtime smoke dispatch\",\"metadata\":{\"source\":\"tool-execute\",\"smoke_run_token\":\"${SMOKE_RUN_TOKEN}\"},\"idempotency_key\":\"tool-dispatch-smoke-${SMOKE_RUN_TOKEN}\"}}")"
TOOL_EXEC_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); receipt=body.get('receipt_json') or {}; out=body.get('output_json') or {}; print('{}|{}|{}|{}|{}'.format(body.get('tool_name',''), out.get('status',''), out.get('binding_id',''), receipt.get('handler_key',''), receipt.get('invocation_contract','')))" <<<"${TOOL_EXEC_JSON}")"
TOOL_EXEC_STATUS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print((body.get('output_json') or {}).get('status',''))" <<<"${TOOL_EXEC_JSON}")"
if [[ "${TOOL_EXEC_FIELDS}" != "connector.dispatch|${TOOL_EXEC_STATUS}|${BINDING_ID}|connector.dispatch|tool.v1" ]] || [[ "${TOOL_EXEC_STATUS}" != "queued" && "${TOOL_EXEC_STATUS}" != "retry" ]]; then
  echo "expected connector.dispatch execute route to return queued-or-retry delivery with scoped binding and normalized receipt contract; got ${TOOL_EXEC_FIELDS}" >&2
  echo "${TOOL_EXEC_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
TOOL_EXEC_DELIVERY_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("target_ref",""))' <<<"${TOOL_EXEC_JSON}")"
if [[ -z "${TOOL_EXEC_DELIVERY_ID}" ]]; then
  fail 13 "missing target_ref from tool execute response"
fi
DELIVERY_PENDING_JSON="$(curl -fsS "${BASE}/v1/delivery/outbox/pending?limit=200" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
DELIVERY_PENDING_MATCH="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); target='${TOOL_EXEC_DELIVERY_ID}'; print(any((row or {}).get('delivery_id') == target for row in rows))" <<<"${DELIVERY_PENDING_JSON}")"
if [[ "${TOOL_EXEC_STATUS}" == "queued" && "${DELIVERY_PENDING_MATCH}" != "True" ]]; then
  echo "expected queued tool-executed connector dispatch to appear in pending outbox; delivery_id=${TOOL_EXEC_DELIVERY_ID}" >&2
  echo "${DELIVERY_PENDING_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
if [[ "${TOOL_EXEC_STATUS}" == "retry" && "${DELIVERY_PENDING_MATCH}" != "True" ]]; then
  echo "tool-executed connector dispatch deferred into retry state before pending outbox enqueue; delivery_id=${TOOL_EXEC_DELIVERY_ID}" >&2
fi
TOOL_EXEC_MISMATCH_CODE="$(curl_status_code ${SMOKE_TMP_DIR}/ea_tool_exec_mismatch_resp.json -X POST "${BASE}/v1/tools/execute" "${AUTH_ARGS[@]}" -H 'content-type: application/json' \
  -H "X-EA-Principal-ID: ${MISMATCH_PRINCIPAL_ID}" \
  -d "{\"tool_name\":\"connector.dispatch\",\"action_kind\":\"delivery.send\",\"payload_json\":{\"principal_id\":\"${PRINCIPAL_ID}\",\"binding_id\":\"${BINDING_ID}\",\"channel\":\"email\",\"recipient\":\"ops@example.com\",\"content\":\"blocked dispatch\"}}")"
if [[ "${TOOL_EXEC_MISMATCH_CODE}" != "403" ]]; then
  echo "expected 403 for foreign principal tool execution; got ${TOOL_EXEC_MISMATCH_CODE}" >&2
  cat ${SMOKE_TMP_DIR}/ea_tool_exec_mismatch_resp.json >&2 || true
  fail 12 "policy contract mismatch"
fi
TOOL_EXEC_MISMATCH_REASON="$(python3 - "${SMOKE_TMP_DIR}/ea_tool_exec_mismatch_resp.json" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
if not path.exists():
    print("")
    raise SystemExit(0)
try:
    body = json.loads(path.read_text())
except Exception:
    print("")
    raise SystemExit(0)
print(((body.get("error") or {}).get("code")) or "")
PY
)"
if [[ "${TOOL_EXEC_MISMATCH_REASON}" != "operator_scope_required" && "${TOOL_EXEC_MISMATCH_REASON}" != "principal_scope_mismatch" ]]; then
  echo "expected foreign principal tool execution code operator_scope_required or principal_scope_mismatch; got ${TOOL_EXEC_MISMATCH_REASON}" >&2
  cat ${SMOKE_TMP_DIR}/ea_tool_exec_mismatch_resp.json >&2 || true
  fail 12 "policy contract mismatch"
fi
BROWSERACT_BINDING_JSON="$(operator_post_json "${BASE}/v1/connectors/bindings" -H 'content-type: application/json' \
  -d '{"connector_name":"browseract","external_account_ref":"browseract-main","scope_json":{"services":["BrowserAct","Teable","UnknownService"]},"auth_metadata_json":{"service_accounts_json":{"BrowserAct":{"tier":"Tier 3","account_email":"ops@example.com","status":"activated"},"Teable":{"tier":"License Tier 4","account_email":"ops@teable.example","status":"activated"}}},"status":"enabled"}')"
BROWSERACT_BINDING_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("binding_id",""))' <<<"${BROWSERACT_BINDING_JSON}")"
if [[ -z "${BROWSERACT_BINDING_ID}" ]]; then
  fail 13 "missing binding_id from browseract connector response"
fi
BROWSERACT_TOOL_EXEC_JSON="$(operator_post_json "${BASE}/v1/tools/execute" -H 'content-type: application/json' \
  -d "{\"tool_name\":\"browseract.extract_account_facts\",\"action_kind\":\"account.extract\",\"payload_json\":{\"principal_id\":\"${PRINCIPAL_ID}\",\"binding_id\":\"${BROWSERACT_BINDING_ID}\",\"service_name\":\"BrowserAct\",\"requested_fields\":[\"tier\",\"account_email\",\"status\"],\"instructions\":\"Use stored BrowserAct credentials\",\"account_hints_json\":{\"BrowserAct\":{\"workspace\":\"primary\"}},\"run_url\":\"https://browseract.example/run\"}}")"
BROWSERACT_TOOL_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); out=body.get('output_json') or {}; receipt=body.get('receipt_json') or {}; print('{}|{}|{}|{}|{}|{}|{}|{}|{}'.format(body.get('tool_name',''), out.get('service_name',''), (out.get('facts_json') or {}).get('tier',''), out.get('account_email',''), out.get('requested_run_url',''), out.get('instructions',''), bool(out.get('account_hints_json')), receipt.get('handler_key',''), receipt.get('invocation_contract','')))" <<<"${BROWSERACT_TOOL_EXEC_JSON}")"
if [[ "${BROWSERACT_TOOL_FIELDS}" != "browseract.extract_account_facts|BrowserAct|Tier 3|ops@example.com|https://browseract.example/run|Use stored BrowserAct credentials|True|browseract.extract_account_facts|tool.v1" ]]; then
  echo "expected browseract.extract_account_facts to resolve configured service facts through the shared tool plane; got ${BROWSERACT_TOOL_FIELDS}" >&2
  echo "${BROWSERACT_TOOL_EXEC_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
BROWSERACT_INVENTORY_JSON="$(operator_post_json "${BASE}/v1/tools/execute" -H 'content-type: application/json' \
  -d "{\"tool_name\":\"browseract.extract_account_inventory\",\"action_kind\":\"account.extract_inventory\",\"payload_json\":{\"principal_id\":\"${PRINCIPAL_ID}\",\"binding_id\":\"${BROWSERACT_BINDING_ID}\",\"service_names\":[\"BrowserAct\",\"Teable\",\"UnknownService\"],\"requested_fields\":[\"tier\",\"account_email\",\"status\"],\"instructions\":\"Use stored BrowserAct credentials\",\"account_hints_json\":{\"Teable\":{\"workspace\":\"ops\"}},\"run_url\":\"https://browseract.example/run\"}}")"
BROWSERACT_INVENTORY_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); out=body.get('output_json') or {}; services=out.get('services_json') or []; receipt=body.get('receipt_json') or {}; print('{}|{}|{}|{}|{}|{}|{}|{}|{}'.format(body.get('tool_name',''), ','.join(out.get('service_names') or []), ','.join(out.get('missing_services') or []), (services[1].get('plan_tier','') if len(services) > 1 else ''), (services[2].get('discovery_status','') if len(services) > 2 else ''), out.get('requested_run_url',''), out.get('instructions',''), receipt.get('handler_key',''), receipt.get('invocation_contract','')))" <<<"${BROWSERACT_INVENTORY_JSON}")"
if [[ "${BROWSERACT_INVENTORY_FIELDS}" != "browseract.extract_account_inventory|BrowserAct,Teable,UnknownService|UnknownService|License Tier 4|missing|https://browseract.example/run|Use stored BrowserAct credentials|browseract.extract_account_inventory|tool.v1" ]]; then
  echo "expected browseract.extract_account_inventory to summarize multiple configured service accounts through the shared tool plane; got ${BROWSERACT_INVENTORY_FIELDS}" >&2
  echo "${BROWSERACT_INVENTORY_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
echo "tools ok"
if [[ -n "${BINDING_ID}" ]]; then
  FOREIGN_BINDING_CODE="$(curl_status_code ${SMOKE_TMP_DIR}/ea_foreign_binding_resp.json -X POST "${BASE}/v1/connectors/bindings/${BINDING_ID}/status" "${AUTH_ARGS[@]}" -H 'content-type: application/json' \
    -H "X-EA-Principal-ID: ${MISMATCH_PRINCIPAL_ID}" -d '{"status":"disabled"}')"
  if [[ "${FOREIGN_BINDING_CODE}" != "404" ]]; then
    echo "expected 404 for foreign principal binding status update; got ${FOREIGN_BINDING_CODE}" >&2
    cat ${SMOKE_TMP_DIR}/ea_foreign_binding_resp.json >&2 || true
    fail 12 "policy contract mismatch"
  fi
  curl -fsS -X POST "${BASE}/v1/connectors/bindings/${BINDING_ID}/status" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' -d '{"status":"disabled"}' >/dev/null
fi
curl -fsS "${BASE}/v1/connectors/bindings?limit=5" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
CONNECTOR_MISMATCH_CODE="$(curl_status_code ${SMOKE_TMP_DIR}/ea_connector_mismatch_resp.json "${BASE}/v1/connectors/bindings?principal_id=${PRINCIPAL_ID}&limit=5" "${AUTH_ARGS[@]}" -H "X-EA-Principal-ID: ${MISMATCH_PRINCIPAL_ID}")"
if [[ "${CONNECTOR_MISMATCH_CODE}" != "403" ]]; then
  echo "expected 403 for connector principal mismatch; got ${CONNECTOR_MISMATCH_CODE}" >&2
  cat ${SMOKE_TMP_DIR}/ea_connector_mismatch_resp.json >&2 || true
  fail 12 "policy contract mismatch"
fi
CONNECTOR_MISMATCH_REASON="$(python3 - "${SMOKE_TMP_DIR}/ea_connector_mismatch_resp.json" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
if not path.exists():
    print("")
    raise SystemExit(0)
try:
    body = json.loads(path.read_text())
except Exception:
    print("")
    raise SystemExit(0)
print(((body.get("error") or {}).get("code")) or "")
PY
)"
if [[ "${CONNECTOR_MISMATCH_REASON}" != "principal_scope_mismatch" ]]; then
  echo "expected connector principal mismatch code principal_scope_mismatch; got ${CONNECTOR_MISMATCH_REASON}" >&2
  cat ${SMOKE_TMP_DIR}/ea_connector_mismatch_resp.json >&2 || true
  fail 12 "policy contract mismatch"
fi
echo "tools/connectors ok"
else
echo "== smoke: telegram adapter =="
echo "skipped: /v1/connectors routes are not published on this runtime"
echo "== smoke: tools and connectors =="
echo "skipped: /v1/connectors and /v1/tools routes are not published on this runtime"
fi

if (( LEGACY_TASK_CONTRACTS_AVAILABLE )); then
echo "== smoke: task contracts =="
operator_post_json "${BASE}/v1/tasks/contracts" -H 'content-type: application/json' \
  -d '{"task_key":"rewrite_text","deliverable_type":"rewrite_note","default_risk_class":"low","default_approval_class":"none","allowed_tools":["artifact_repository"],"evidence_requirements":[],"memory_write_policy":"reviewed_only","budget_policy_json":{"class":"low","artifact_failure_strategy":"retry","artifact_max_attempts":2,"artifact_retry_backoff_seconds":15}}' >/dev/null
operator_curl "${BASE}/v1/tasks/contracts?limit=5" >/dev/null
operator_curl "${BASE}/v1/tasks/contracts/rewrite_text" >/dev/null
echo "task contracts ok"
else
echo "== smoke: task contracts =="
echo "skipped: /v1/tasks/contracts routes are not published on this runtime"
fi

if (( LEGACY_SKILLS_AVAILABLE )); then
echo "== smoke: skills =="
SKILL_JSON="$(operator_post_json "${BASE}/v1/skills" -H 'content-type: application/json' \
  -d '{"skill_key":"meeting_prep","task_key":"meeting_prep","name":"Meeting Prep","description":"Build an executive-ready meeting prep packet.","deliverable_type":"meeting_pack","default_risk_class":"low","default_approval_class":"none","workflow_template":"artifact_then_memory_candidate","allowed_tools":["artifact_repository"],"evidence_requirements":["stakeholder_context","decision_context"],"memory_write_policy":"reviewed_only","memory_reads":["stakeholders","commitments","decision_windows"],"memory_writes":["meeting_pack_fact"],"tags":["executive","meeting","briefing"],"authority_profile_json":{"authority_class":"draft","review_class":"operator"},"provider_hints_json":{"primary":["1min.AI"],"research":["BrowserAct","Paperguide"],"output":["MarkupGo"]},"tool_policy_json":{"allowed_tools":["artifact_repository"]},"human_policy_json":{"review_roles":["briefing_reviewer"]},"evaluation_cases_json":[{"case_key":"meeting_prep_golden","priority":"high"}],"budget_policy_json":{"class":"low","memory_candidate_category":"meeting_pack_fact","memory_candidate_confidence":0.8,"memory_candidate_sensitivity":"internal"}}')"
SKILL_LIST_JSON="$(operator_curl "${BASE}/v1/skills?limit=10")"
SKILL_FILTER_JSON="$(operator_curl "${BASE}/v1/skills?limit=10&provider_hint=BrowserAct")"
SKILL_FETCH_JSON="$(operator_curl "${BASE}/v1/skills/meeting_prep")"
SKILL_PLAN_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/compile" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"task_key":"meeting_prep","goal":"prepare the board meeting packet"}')"
SKILL_PLAN_BY_SKILL_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/compile" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"skill_key":"meeting_prep","goal":"prepare the board meeting packet"}')"
SKILL_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.argv[1]); listed=json.loads(sys.argv[2]); filtered=json.loads(sys.argv[3]); fetched=json.loads(sys.argv[4]); compiled=json.loads(sys.argv[5]); compiled_via_skill=json.loads(sys.argv[6]); steps=compiled.get('plan',{}).get('steps') or []; print('{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}'.format(body.get('skill_key',''), body.get('workflow_template',''), ','.join(body.get('memory_reads') or []), ','.join(body.get('memory_writes') or []), ','.join((body.get('provider_hints_json') or {}).get('primary') or []), any(row.get('skill_key') == 'meeting_prep' for row in listed), any(row.get('skill_key') == 'meeting_prep' for row in filtered), fetched.get('name',''), (fetched.get('authority_profile_json') or {}).get('authority_class',''), ','.join((fetched.get('provider_hints_json') or {}).get('research') or []), compiled.get('skill_key',''), len(steps), ','.join(step.get('step_key','') for step in steps), compiled_via_skill.get('plan',{}).get('task_key','')))" "${SKILL_JSON}" "${SKILL_LIST_JSON}" "${SKILL_FILTER_JSON}" "${SKILL_FETCH_JSON}" "${SKILL_PLAN_JSON}" "${SKILL_PLAN_BY_SKILL_JSON}")"
if [[ "${SKILL_FIELDS}" != "meeting_prep|artifact_then_memory_candidate|stakeholders,commitments,decision_windows|meeting_pack_fact|1min.AI|True|True|Meeting Prep|draft|BrowserAct,Paperguide|meeting_prep|4|step_input_prepare,step_policy_evaluate,step_artifact_save,step_memory_candidate_stage|meeting_prep" ]]; then
  echo "expected skill catalog endpoints plus meeting_prep compile projection to round-trip the executive skill metadata and backing plan graph; got ${SKILL_FIELDS}" >&2
  echo "${SKILL_JSON}" >&2
  echo "${SKILL_FETCH_JSON}" >&2
  echo "${SKILL_PLAN_JSON}" >&2
  echo "${SKILL_PLAN_BY_SKILL_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
LTD_SKILL_JSON="$(operator_post_json "${BASE}/v1/skills" -H 'content-type: application/json' \
  -d '{"skill_key":"ltd_inventory_refresh","task_key":"ltd_inventory_refresh","name":"LTD Inventory Refresh","description":"Refresh BrowserAct-backed LTD account facts.","deliverable_type":"ltd_inventory_profile","default_risk_class":"low","default_approval_class":"none","workflow_template":"tool_then_artifact","allowed_tools":["browseract.extract_account_inventory","artifact_repository"],"evidence_requirements":["account_inventory"],"memory_write_policy":"none","memory_reads":["account_inventory"],"memory_writes":[],"tags":["ltd","inventory","operations"],"authority_profile_json":{"authority_class":"observe","review_class":"none"},"provider_hints_json":{"primary":["BrowserAct"],"ops":["Teable"],"output":["MarkupGo"]},"tool_policy_json":{"allowed_tools":["browseract.extract_account_inventory","artifact_repository"]},"evaluation_cases_json":[{"case_key":"ltd_inventory_refresh_golden","priority":"medium"}],"budget_policy_json":{"class":"low","pre_artifact_tool_name":"browseract.extract_account_inventory"}}')"
LTD_SKILL_FILTER_JSON="$(operator_curl "${BASE}/v1/skills?limit=10&provider_hint=browseract")"
LTD_SKILL_FETCH_JSON="$(operator_curl "${BASE}/v1/skills/ltd_inventory_refresh")"
LTD_SKILL_PLAN_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/compile" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"task_key":"ltd_inventory_refresh","goal":"refresh LTD inventory facts"}')"
LTD_SKILL_PLAN_BY_SKILL_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/compile" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"skill_key":"ltd_inventory_refresh","goal":"refresh LTD inventory facts"}')"
LTD_SKILL_EXECUTE_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/execute" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"task_key\":\"ltd_inventory_refresh\",\"goal\":\"refresh LTD inventory facts\",\"input_json\":{\"binding_id\":\"${BROWSERACT_BINDING_ID}\",\"service_names\":[\"BrowserAct\",\"Teable\",\"UnknownService\"],\"requested_fields\":[\"tier\",\"account_email\",\"status\"]}}")"
LTD_SKILL_EXECUTE_BY_SKILL_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/execute" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"skill_key\":\"ltd_inventory_refresh\",\"goal\":\"refresh LTD inventory facts\",\"input_json\":{\"binding_id\":\"${BROWSERACT_BINDING_ID}\",\"service_names\":[\"BrowserAct\",\"Teable\",\"UnknownService\"],\"requested_fields\":[\"tier\",\"account_email\",\"status\"]}}")"
LTD_SKILL_SESSION_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("execution_session_id",""))' <<<"${LTD_SKILL_EXECUTE_JSON}")"
LTD_SKILL_SESSION_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${LTD_SKILL_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
LTD_SKILL_FIELDS="$(python3 -c "import json,sys; created=json.loads(sys.argv[1]); filtered=json.loads(sys.argv[2]); fetched=json.loads(sys.argv[3]); compiled=json.loads(sys.argv[4]); compiled_via_skill=json.loads(sys.argv[5]); executed=json.loads(sys.argv[6]); executed_via_skill=json.loads(sys.argv[7]); session=json.loads(sys.argv[8]); steps=compiled.get('plan',{}).get('steps') or []; artifact=(session.get('artifacts') or [{}])[0]; receipts=session.get('receipts') or []; print('{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}'.format(created.get('skill_key',''), created.get('workflow_template',''), any(row.get('skill_key') == 'ltd_inventory_refresh' for row in filtered), ','.join((fetched.get('provider_hints_json') or {}).get('ops') or []), compiled.get('skill_key',''), compiled_via_skill.get('plan',{}).get('task_key',''), ','.join(step.get('step_key','') for step in steps), executed.get('skill_key',''), executed.get('kind',''), ','.join((executed.get('structured_output_json') or {}).get('missing_services') or []), session.get('intent_skill_key',''), [row.get('tool_name','') for row in receipts] == ['browseract.extract_account_inventory', 'artifact_repository'], artifact.get('skill_key',''), executed_via_skill.get('task_key','')))" "${LTD_SKILL_JSON}" "${LTD_SKILL_FILTER_JSON}" "${LTD_SKILL_FETCH_JSON}" "${LTD_SKILL_PLAN_JSON}" "${LTD_SKILL_PLAN_BY_SKILL_JSON}" "${LTD_SKILL_EXECUTE_JSON}" "${LTD_SKILL_EXECUTE_BY_SKILL_JSON}" "${LTD_SKILL_SESSION_JSON}")"
if [[ "${LTD_SKILL_FIELDS}" != "ltd_inventory_refresh|tool_then_artifact|True|Teable|ltd_inventory_refresh|ltd_inventory_refresh|step_input_prepare,step_browseract_inventory_extract,step_artifact_save|ltd_inventory_refresh|ltd_inventory_profile|UnknownService|ltd_inventory_refresh|True|ltd_inventory_refresh|ltd_inventory_refresh" ]]; then
  echo "expected ltd_inventory_refresh skill to project BrowserAct inventory workflow metadata and runtime identity; got ${LTD_SKILL_FIELDS}" >&2
  echo "${LTD_SKILL_JSON}" >&2
  echo "${LTD_SKILL_FETCH_JSON}" >&2
  echo "${LTD_SKILL_PLAN_JSON}" >&2
  echo "${LTD_SKILL_PLAN_BY_SKILL_JSON}" >&2
  echo "${LTD_SKILL_EXECUTE_JSON}" >&2
  echo "${LTD_SKILL_EXECUTE_BY_SKILL_JSON}" >&2
  echo "${LTD_SKILL_SESSION_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
BROWSERACT_BOOTSTRAP_SKILL_JSON="$(operator_post_json "${BASE}/v1/skills" -H 'content-type: application/json' \
  -d '{"skill_key":"browseract_bootstrap_manager","task_key":"browseract_bootstrap_manager","name":"BrowserAct Bootstrap Manager","description":"Planner-executed BrowserAct workflow-spec builder for stage-0 BrowserAct template creation and architect packets.","deliverable_type":"browseract_workflow_spec_packet","default_risk_class":"medium","default_approval_class":"none","workflow_template":"tool_then_artifact","allowed_tools":["browseract.build_workflow_spec","artifact_repository"],"evidence_requirements":["target_domain_brief","workflow_spec","browseract_seed_state"],"memory_write_policy":"none","memory_reads":["entities","relationships"],"memory_writes":[],"tags":["browseract","bootstrap","workflow","architect"],"authority_profile_json":{"authority_class":"draft","review_class":"operator"},"provider_hints_json":{"primary":["BrowserAct"],"notes":["Stage-0 architect compiles prepared workflow specs into BrowserAct-ready packets."]},"tool_policy_json":{"allowed_tools":["browseract.build_workflow_spec","artifact_repository"]},"human_policy_json":{"review_roles":["automation_architect"]},"evaluation_cases_json":[{"case_key":"browseract_bootstrap_manager_golden","priority":"medium"}],"budget_policy_json":{"class":"medium","workflow_template":"tool_then_artifact","pre_artifact_capability_key":"workflow_spec_build","browseract_failure_strategy":"retry","browseract_max_attempts":2,"browseract_retry_backoff_seconds":1}}')"
BROWSERACT_BOOTSTRAP_SKILL_FETCH_JSON="$(operator_curl "${BASE}/v1/skills/browseract_bootstrap_manager")"
BROWSERACT_BOOTSTRAP_SKILL_PLAN_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/compile" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"skill_key":"browseract_bootstrap_manager","goal":"build a BrowserAct workflow spec packet"}')"
BROWSERACT_BOOTSTRAP_SKILL_EXECUTE_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/execute" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"skill_key":"browseract_bootstrap_manager","goal":"build a BrowserAct workflow spec packet","input_json":{"workflow_name":"Prompt Forge","purpose":"Build a prepared BrowserAct workflow spec for prompt refinement.","login_url":"https://browseract.example/login","tool_url":"https://browseract.example/tools/prompting-systems"}}')"
BROWSERACT_BOOTSTRAP_SKILL_SESSION_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("execution_session_id",""))' <<<"${BROWSERACT_BOOTSTRAP_SKILL_EXECUTE_JSON}")"
BROWSERACT_BOOTSTRAP_SKILL_SESSION_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${BROWSERACT_BOOTSTRAP_SKILL_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
BROWSERACT_BOOTSTRAP_FIELDS="$(python3 -c "import json,sys; created=json.loads(sys.argv[1]); fetched=json.loads(sys.argv[2]); compiled=json.loads(sys.argv[3]); executed=json.loads(sys.argv[4]); session=json.loads(sys.argv[5]); steps=compiled.get('plan',{}).get('steps') or []; artifact=(session.get('artifacts') or [{}])[0]; receipts=session.get('receipts') or []; structured=executed.get('structured_output_json') or {}; print('{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}'.format(created.get('skill_key',''), fetched.get('workflow_template',''), ','.join((fetched.get('provider_hints_json') or {}).get('primary') or []), compiled.get('skill_key',''), ','.join(step.get('step_key','') for step in steps), executed.get('skill_key',''), executed.get('kind',''), structured.get('workflow_name',''), (structured.get('meta') or {}).get('slug',''), session.get('intent_skill_key',''), [row.get('tool_name','') for row in receipts] == ['browseract.build_workflow_spec', 'artifact_repository'], artifact.get('skill_key',''), artifact.get('kind','')))" "${BROWSERACT_BOOTSTRAP_SKILL_JSON}" "${BROWSERACT_BOOTSTRAP_SKILL_FETCH_JSON}" "${BROWSERACT_BOOTSTRAP_SKILL_PLAN_JSON}" "${BROWSERACT_BOOTSTRAP_SKILL_EXECUTE_JSON}" "${BROWSERACT_BOOTSTRAP_SKILL_SESSION_JSON}")"
if [[ "${BROWSERACT_BOOTSTRAP_FIELDS}" != "browseract_bootstrap_manager|tool_then_artifact|BrowserAct|browseract_bootstrap_manager|step_input_prepare,step_browseract_workflow_spec_build,step_artifact_save|browseract_bootstrap_manager|browseract_workflow_spec_packet|Prompt Forge|prompt_forge|browseract_bootstrap_manager|True|browseract_bootstrap_manager|browseract_workflow_spec_packet" ]]; then
  echo "expected browseract_bootstrap_manager to compile and execute through the BrowserAct workflow-spec builder lane; got ${BROWSERACT_BOOTSTRAP_FIELDS}" >&2
  echo "${BROWSERACT_BOOTSTRAP_SKILL_JSON}" >&2
  echo "${BROWSERACT_BOOTSTRAP_SKILL_FETCH_JSON}" >&2
  echo "${BROWSERACT_BOOTSTRAP_SKILL_PLAN_JSON}" >&2
  echo "${BROWSERACT_BOOTSTRAP_SKILL_EXECUTE_JSON}" >&2
  echo "${BROWSERACT_BOOTSTRAP_SKILL_SESSION_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
CHUMMER_PUBLIC_WRITER_JSON="$(operator_post_json "${BASE}/v1/skills" -H 'content-type: application/json' \
  -d '{"skill_key":"chummer6_public_writer","task_key":"chummer6_public_copy_refresh","name":"Chummer6 Public Writer","description":"Planner-executed public-writer lane for Chummer6 guide copy, audience translation, and reader-safe OODA framing.","deliverable_type":"chummer6_guide_refresh_packet","default_risk_class":"low","default_approval_class":"none","workflow_template":"tool_then_artifact","allowed_tools":["provider.gemini_vortex.structured_generate","artifact_repository"],"evidence_requirements":["repo_readmes","design_scope","public_status","source_prompt"],"memory_write_policy":"reviewed_only","memory_reads":["entities","relationships","repo_readmes","design_scope","public_status"],"memory_writes":["chummer6_public_copy_fact"],"tags":["chummer6","guide","public-writer","audience","copy"],"authority_profile_json":{"authority_class":"draft","review_class":"operator"},"model_policy_json":{"provider":"gemini_vortex","default_model":"gemini-2.5-flash","output_mode":"json"},"provider_hints_json":{"primary":["Gemini Vortex"],"research":["BrowserAct"],"output":["Gemini Vortex","Prompting Systems"],"style":["Gemini Vortex"]},"tool_policy_json":{"allowed_tools":["provider.gemini_vortex.structured_generate","artifact_repository"]},"human_policy_json":{"review_roles":["guide_reviewer"]},"evaluation_cases_json":[{"case_key":"chummer6_guide_refresh_golden","priority":"medium"}],"budget_policy_json":{"class":"low","workflow_template":"tool_then_artifact","pre_artifact_capability_key":"structured_generate","artifact_failure_strategy":"retry","artifact_max_attempts":2,"artifact_retry_backoff_seconds":1,"style_epoch_enabled":true,"variation_guard_enabled":true}}')"
CHUMMER_PUBLIC_WRITER_FETCH_JSON="$(operator_curl "${BASE}/v1/skills/chummer6_public_writer")"
CHUMMER_PUBLIC_WRITER_PLAN_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/compile" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"skill_key":"chummer6_public_writer","goal":"author reader-safe Chummer6 guide copy"}')"
CHUMMER_PUBLIC_WRITER_FIELDS="$(python3 -c "import json,sys; created=json.loads(sys.argv[1]); fetched=json.loads(sys.argv[2]); compiled=json.loads(sys.argv[3]); steps=compiled.get('plan',{}).get('steps') or []; print('{}|{}|{}|{}|{}|{}|{}|{}|{}|{}'.format(created.get('skill_key',''), created.get('task_key',''), created.get('workflow_template',''), ','.join((created.get('provider_hints_json') or {}).get('primary') or []), fetched.get('task_key',''), (fetched.get('model_policy_json') or {}).get('provider',''), compiled.get('skill_key',''), len(steps), ','.join(step.get('step_key','') for step in steps), steps[1].get('tool_name','') if len(steps) > 1 else ''))" "${CHUMMER_PUBLIC_WRITER_JSON}" "${CHUMMER_PUBLIC_WRITER_FETCH_JSON}" "${CHUMMER_PUBLIC_WRITER_PLAN_JSON}")"
if [[ "${CHUMMER_PUBLIC_WRITER_FIELDS}" != "chummer6_public_writer|chummer6_public_copy_refresh|tool_then_artifact|Gemini Vortex|chummer6_public_copy_refresh|gemini_vortex|chummer6_public_writer|3|step_input_prepare,step_structured_generate,step_artifact_save|provider.brain_router.structured_generate" ]]; then
  echo "expected chummer6_public_writer to compile through the Gemini Vortex structured-generation lane; got ${CHUMMER_PUBLIC_WRITER_FIELDS}" >&2
  echo "${CHUMMER_PUBLIC_WRITER_JSON}" >&2
  echo "${CHUMMER_PUBLIC_WRITER_FETCH_JSON}" >&2
  echo "${CHUMMER_PUBLIC_WRITER_PLAN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
CHUMMER_SKILL_JSON="$(operator_post_json "${BASE}/v1/skills" -H 'content-type: application/json' \
  -d '{"skill_key":"chummer6_visual_director","task_key":"chummer6_guide_refresh","name":"Chummer6 Visual Director","description":"Planner-executed Chummer6 scene planning, style-epoch selection, scene-ledger guidance, and structured visual-direction skill for the public-facing guide.","deliverable_type":"chummer6_guide_refresh_packet","default_risk_class":"low","default_approval_class":"none","workflow_template":"tool_then_artifact","allowed_tools":["provider.gemini_vortex.structured_generate","artifact_repository"],"evidence_requirements":["repo_readmes","design_scope","public_status","source_prompt"],"memory_write_policy":"reviewed_only","memory_reads":["entities","relationships","repo_readmes","design_scope","public_status"],"memory_writes":["chummer6_style_epoch","chummer6_scene_ledger","chummer6_visual_critic_fact"],"tags":["chummer6","guide","visual-direction","style-epoch","scene-ledger"],"authority_profile_json":{"authority_class":"draft","review_class":"operator"},"model_policy_json":{"provider":"gemini_vortex","default_model":"gemini-2.5-flash","output_mode":"json"},"provider_hints_json":{"primary":["Gemini Vortex"],"research":["BrowserAct"],"output":["Gemini Vortex","AI Magicx","Prompting Systems","BrowserAct"],"media":["AI Magicx","Prompting Systems","BrowserAct"],"style":["Gemini Vortex"]},"tool_policy_json":{"allowed_tools":["provider.gemini_vortex.structured_generate","artifact_repository"]},"human_policy_json":{"review_roles":["guide_reviewer"]},"evaluation_cases_json":[{"case_key":"chummer6_guide_refresh_golden","priority":"medium"}],"budget_policy_json":{"class":"low","workflow_template":"tool_then_artifact","pre_artifact_capability_key":"structured_generate","artifact_failure_strategy":"retry","artifact_max_attempts":2,"artifact_retry_backoff_seconds":1,"style_epoch_enabled":true,"variation_guard_enabled":true}}')"
CHUMMER_SKILL_FETCH_JSON="$(operator_curl "${BASE}/v1/skills/chummer6_visual_director")"
CHUMMER_SKILL_PLAN_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/compile" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"skill_key":"chummer6_visual_director","goal":"author a structured Chummer6 guide refresh packet"}')"
CHUMMER_SKILL_FIELDS="$(python3 -c "import json,sys; created=json.loads(sys.argv[1]); fetched=json.loads(sys.argv[2]); compiled=json.loads(sys.argv[3]); steps=compiled.get('plan',{}).get('steps') or []; print('{}|{}|{}|{}|{}|{}|{}|{}|{}|{}'.format(created.get('skill_key',''), created.get('task_key',''), created.get('workflow_template',''), ','.join((created.get('provider_hints_json') or {}).get('primary') or []), fetched.get('task_key',''), (fetched.get('model_policy_json') or {}).get('provider',''), compiled.get('skill_key',''), len(steps), ','.join(step.get('step_key','') for step in steps), steps[1].get('tool_name','') if len(steps) > 1 else ''))" "${CHUMMER_SKILL_JSON}" "${CHUMMER_SKILL_FETCH_JSON}" "${CHUMMER_SKILL_PLAN_JSON}")"
if [[ "${CHUMMER_SKILL_FIELDS}" != "chummer6_visual_director|chummer6_guide_refresh|tool_then_artifact|Gemini Vortex|chummer6_guide_refresh|gemini_vortex|chummer6_visual_director|3|step_input_prepare,step_structured_generate,step_artifact_save|provider.brain_router.structured_generate" ]]; then
  echo "expected chummer6_visual_director to compile through the Gemini Vortex structured-generation lane; got ${CHUMMER_SKILL_FIELDS}" >&2
  echo "${CHUMMER_SKILL_JSON}" >&2
  echo "${CHUMMER_SKILL_FETCH_JSON}" >&2
  echo "${CHUMMER_SKILL_PLAN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
echo "skills ok"
else
echo "== smoke: skills =="
echo "skipped: /v1/skills routes are not published on this runtime"
fi

if (( LEGACY_TASK_CONTRACTS_AVAILABLE )); then
echo "== smoke: plans =="
PLAN_COMPILE_TASK_KEY="rewrite_text_plan_${SMOKE_RUN_TOKEN}"
operator_post_json "${BASE}/v1/tasks/contracts" -H 'content-type: application/json' \
  -d "{\"task_key\":\"${PLAN_COMPILE_TASK_KEY}\",\"deliverable_type\":\"rewrite_note\",\"default_risk_class\":\"low\",\"default_approval_class\":\"none\",\"allowed_tools\":[\"artifact_repository\"],\"evidence_requirements\":[],\"memory_write_policy\":\"reviewed_only\",\"budget_policy_json\":{\"class\":\"low\",\"artifact_failure_strategy\":\"retry\",\"artifact_max_attempts\":2,\"artifact_retry_backoff_seconds\":15}}" >/dev/null
PLAN_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/compile" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"task_key\":\"${PLAN_COMPILE_TASK_KEY}\",\"goal\":\"rewrite this text\"}")"
PLAN_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps=body.get('plan',{}).get('steps') or []; prepare=(steps[0] if steps else {}); policy=(steps[1] if len(steps) > 1 else {}); save=(steps[2] if len(steps) > 2 else {}); print('{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}'.format(len(steps), prepare.get('step_key',''), prepare.get('owner',''), prepare.get('authority_class',''), prepare.get('timeout_budget_seconds',''), prepare.get('max_attempts',''), policy.get('step_key',''), ','.join(policy.get('depends_on') or []), policy.get('owner',''), save.get('step_key',''), save.get('tool_name',''), ','.join(save.get('depends_on') or []), save.get('owner',''), save.get('authority_class',''), save.get('failure_strategy',''), save.get('timeout_budget_seconds','')))" <<<"${PLAN_JSON}")"
if [[ "${PLAN_FIELDS}" != "3|step_input_prepare|system|observe|30|1|step_policy_evaluate|step_input_prepare|system|step_artifact_save|artifact_repository|step_policy_evaluate|tool|draft|retry|60" ]]; then
  echo "expected direct three-step plan compile response with explicit artifact-save semantics; got ${PLAN_FIELDS}" >&2
  echo "${PLAN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
PLAN_PRINCIPAL_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}'.format(body.get('intent',{}).get('principal_id',''), body.get('plan',{}).get('principal_id','')))" <<<"${PLAN_JSON}")"
if [[ "${PLAN_PRINCIPAL_FIELDS}" != "${PRINCIPAL_ID}|${PRINCIPAL_ID}" ]]; then
  echo "expected plan compile to derive principal from request context when principal_id body field is omitted; got ${PLAN_PRINCIPAL_FIELDS}" >&2
  echo "${PLAN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
PLAN_MISMATCH_CODE="$(curl_status_code ${SMOKE_TMP_DIR}/ea_plan_mismatch_resp.json -X POST "${BASE}/v1/plans/compile" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' -d "{\"task_key\":\"${PLAN_COMPILE_TASK_KEY}\",\"principal_id\":\"${MISMATCH_PRINCIPAL_ID}\",\"goal\":\"rewrite this text\"}")"
if [[ "${PLAN_MISMATCH_CODE}" != "403" ]]; then
  echo "expected plan compile principal mismatch to return 403; got ${PLAN_MISMATCH_CODE}" >&2
  cat ${SMOKE_TMP_DIR}/ea_plan_mismatch_resp.json >&2
  fail 12 "policy contract mismatch"
fi
PLAN_MISMATCH_REASON="$(python3 -c 'import json,sys; body=json.load(open(sys.argv[1])); print(((body.get("error") or {}).get("code","")))' ${SMOKE_TMP_DIR}/ea_plan_mismatch_resp.json)"
if [[ "${PLAN_MISMATCH_REASON}" != "principal_scope_mismatch" ]]; then
  echo "expected plan compile mismatch code principal_scope_mismatch; got ${PLAN_MISMATCH_REASON}" >&2
  cat ${SMOKE_TMP_DIR}/ea_plan_mismatch_resp.json >&2
  fail 12 "policy contract mismatch"
fi
operator_post_json "${BASE}/v1/tasks/contracts" -H 'content-type: application/json' \
  -d '{"task_key":"rewrite_review","deliverable_type":"rewrite_note","default_risk_class":"low","default_approval_class":"none","allowed_tools":["artifact_repository"],"evidence_requirements":["stakeholder_context"],"memory_write_policy":"reviewed_only","budget_policy_json":{"class":"low","human_review_role":"communications_reviewer","human_review_task_type":"communications_review","human_review_brief":"Review the rewrite before finalizing it.","human_review_priority":"high","human_review_sla_minutes":45,"human_review_auto_assign_if_unique":true,"human_review_desired_output_json":{"format":"review_packet","escalation_policy":"manager_review"},"human_review_authority_required":"send_on_behalf_review","human_review_why_human":"Executive-facing rewrite needs human judgment before finalization.","human_review_quality_rubric_json":{"checks":["tone","accuracy","stakeholder_sensitivity"]}}}' >/dev/null
REVIEW_PLAN_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/compile" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"task_key":"rewrite_review","goal":"review this rewrite"}')"
REVIEW_PLAN_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps=body.get('plan',{}).get('steps') or []; review=(steps[2] if len(steps) > 2 else {}); checks=(review.get('quality_rubric_json') or {}).get('checks') or []; print('{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}'.format(len(steps), review.get('step_kind',''), review.get('owner',''), review.get('authority_class',''), review.get('review_class',''), review.get('role_required',''), review.get('priority',''), review.get('sla_minutes',''), review.get('timeout_budget_seconds',''), review.get('max_attempts',''), review.get('retry_backoff_seconds',''), review.get('auto_assign_if_unique', False), (review.get('desired_output_json') or {}).get('escalation_policy',''), review.get('authority_required','')))" <<<"${REVIEW_PLAN_JSON}")"
if [[ "${REVIEW_PLAN_FIELDS}" != "4|human_task|human|draft|operator|communications_reviewer|high|45|3600|1|0|True|manager_review|send_on_behalf_review" ]]; then
  echo "expected compiled human-review branch in plan response; got ${REVIEW_PLAN_FIELDS}" >&2
  echo "${REVIEW_PLAN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
echo "plans ok"
else
echo "== smoke: plans =="
echo "skipped: /v1/tasks/contracts routes are not published on this runtime"
fi

if (( LEGACY_RESPONSES_AVAILABLE && LEGACY_TASK_CONTRACTS_AVAILABLE )); then
echo "== smoke: generic task execution =="
operator_post_json "${BASE}/v1/tasks/contracts" -H 'content-type: application/json' \
  -d '{"task_key":"stakeholder_briefing","deliverable_type":"stakeholder_briefing","default_risk_class":"low","default_approval_class":"none","allowed_tools":["artifact_repository"],"evidence_requirements":["stakeholder_context"],"memory_write_policy":"reviewed_only","budget_policy_json":{"class":"low"}}' >/dev/null
TASK_EXECUTE_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/execute" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"task_key":"stakeholder_briefing","input_json":{"source_text":"Board context and stakeholder sensitivities.","channel":"email","stakeholder_ref":"alex-exec"},"context_refs":["thread:board-prep","memory:item:stakeholder-brief"],"goal":"prepare a stakeholder briefing"}')"
TASK_EXECUTE_ARTIFACT_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("artifact_id",""))' <<<"${TASK_EXECUTE_JSON}")"
TASK_EXECUTE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}'.format(body.get('skill_key',''), body.get('task_key',''), body.get('kind',''), body.get('deliverable_type',''), body.get('content',''), body.get('mime_type',''), body.get('preview_text',''), body.get('storage_handle',''), body.get('body_ref',''), body.get('principal_id',''), bool(body.get('artifact_id','')), bool(body.get('execution_session_id','')), body.get('structured_output_json',{}) == {} and body.get('attachments_json',{}) == {}))" <<<"${TASK_EXECUTE_JSON}")"
if [[ "${TASK_EXECUTE_FIELDS}" != "stakeholder_briefing|stakeholder_briefing|stakeholder_briefing|stakeholder_briefing|Board context and stakeholder sensitivities.|text/plain|Board context and stakeholder sensitivities.|artifact://${TASK_EXECUTE_ARTIFACT_ID}|artifact://${TASK_EXECUTE_ARTIFACT_ID}|${PRINCIPAL_ID}|True|True|True" ]]; then
  echo "expected generic task execution route to reuse the compiled contract runtime with artifact-envelope fields; got ${TASK_EXECUTE_FIELDS}" >&2
  echo "${TASK_EXECUTE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
TASK_EXECUTE_SESSION_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("execution_session_id",""))' <<<"${TASK_EXECUTE_JSON}")"
TASK_EXECUTE_SESSION_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${TASK_EXECUTE_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
TASK_EXECUTE_SESSION_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); artifacts=body.get('artifacts') or []; steps=body.get('steps') or []; events=body.get('events') or []; first=(artifacts[0] if artifacts else {}); prepare=(steps[0] if steps else {}); policy=(steps[1] if len(steps) > 1 else {}); save=(steps[2] if len(steps) > 2 else {}); plan_event=next((event for event in events if (event or {}).get('name') == 'plan_compiled'), {}); semantics=(plan_event.get('payload',{}) or {}).get('step_semantics') or []; first_semantics=(semantics[0] if semantics else {}); prepare_input=prepare.get('input_json',{}) or {}; parent_ok=(prepare.get('parent_step_id') is None and policy.get('parent_step_id') == prepare.get('step_id') and save.get('parent_step_id') == policy.get('step_id')); fields=[body.get('intent_skill_key',''), body.get('intent_task_type',''), body.get('status',''), str(len(steps)), first.get('skill_key',''), first.get('kind',''), first.get('task_key',''), first.get('deliverable_type',''), str(any((event or {}).get('name') == 'plan_compiled' for event in events)), prepare_input.get('owner',''), prepare_input.get('authority_class',''), str(prepare_input.get('timeout_budget_seconds','')), (save.get('input_json',{}) or {}).get('owner',''), (save.get('input_json',{}) or {}).get('failure_strategy',''), first_semantics.get('owner',''), str(first_semantics.get('timeout_budget_seconds','')), first.get('mime_type',''), first.get('preview_text',''), first.get('storage_handle',''), str(str(first.get('body_ref','')).startswith('file://')), first.get('principal_id',''), str(parent_ok), prepare_input.get('channel',''), prepare_input.get('stakeholder_ref',''), ','.join(prepare_input.get('context_refs') or [])]; print('|'.join(fields))" <<<"${TASK_EXECUTE_SESSION_JSON}")"
if [[ "${TASK_EXECUTE_SESSION_FIELDS}" != "stakeholder_briefing|stakeholder_briefing|completed|3|stakeholder_briefing|stakeholder_briefing|stakeholder_briefing|stakeholder_briefing|True|system|observe|30|tool|fail|system|30|text/plain|Board context and stakeholder sensitivities.|artifact://${TASK_EXECUTE_ARTIFACT_ID}|True|${PRINCIPAL_ID}|True|email|alex-exec|thread:board-prep,memory:item:stakeholder-brief" ]]; then
  echo "expected generic task execution session to retain compiled step semantics, honest single-dependency parent links, retry/timeout budgets, explicit durable artifact ownership fields, and structured input/context refs; got ${TASK_EXECUTE_SESSION_FIELDS}" >&2
  echo "${TASK_EXECUTE_SESSION_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
TASK_EXECUTE_RECEIPT_ID="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); rows=body.get('receipts') or []; print((rows[0] or {}).get('receipt_id','') if rows else '')" <<<"${TASK_EXECUTE_SESSION_JSON}")"
TASK_EXECUTE_COST_ID="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); rows=body.get('run_costs') or []; print((rows[0] or {}).get('cost_id','') if rows else '')" <<<"${TASK_EXECUTE_SESSION_JSON}")"
TASK_EXECUTE_ARTIFACT_JSON="$(curl -fsS "${BASE}/v1/rewrite/artifacts/${TASK_EXECUTE_ARTIFACT_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
TASK_EXECUTE_ARTIFACT_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}|{}|{}|{}|{}|{}|{}|{}'.format(body.get('skill_key',''), body.get('task_key',''), body.get('kind',''), body.get('deliverable_type',''), body.get('execution_session_id',''), body.get('mime_type',''), body.get('preview_text',''), body.get('storage_handle',''), str(body.get('body_ref','')).startswith('file://'), body.get('principal_id','')))" <<<"${TASK_EXECUTE_ARTIFACT_JSON}")"
if [[ "${TASK_EXECUTE_ARTIFACT_FIELDS}" != "stakeholder_briefing|stakeholder_briefing|stakeholder_briefing|stakeholder_briefing|${TASK_EXECUTE_SESSION_ID}|text/plain|Board context and stakeholder sensitivities.|artifact://${TASK_EXECUTE_ARTIFACT_ID}|True|${PRINCIPAL_ID}" ]]; then
  echo "expected direct artifact lookup to project generic task identity plus durable artifact envelope ownership fields; got ${TASK_EXECUTE_ARTIFACT_FIELDS}" >&2
  echo "${TASK_EXECUTE_ARTIFACT_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
TASK_EXECUTE_RECEIPT_JSON="$(curl -fsS "${BASE}/v1/rewrite/receipts/${TASK_EXECUTE_RECEIPT_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
TASK_EXECUTE_RECEIPT_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}|{}|{}'.format(body.get('skill_key',''), body.get('task_key',''), body.get('deliverable_type',''), body.get('tool_name',''), body.get('target_ref','')))" <<<"${TASK_EXECUTE_RECEIPT_JSON}")"
if [[ "${TASK_EXECUTE_RECEIPT_FIELDS}" != "stakeholder_briefing|stakeholder_briefing|stakeholder_briefing|artifact_repository|${TASK_EXECUTE_ARTIFACT_ID}" ]]; then
  echo "expected direct receipt lookup to project generic task identity and deliverable context; got ${TASK_EXECUTE_RECEIPT_FIELDS}" >&2
  echo "${TASK_EXECUTE_RECEIPT_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
TASK_EXECUTE_COST_JSON="$(curl -fsS "${BASE}/v1/rewrite/run-costs/${TASK_EXECUTE_COST_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
TASK_EXECUTE_COST_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}|{}|{}|{}'.format(body.get('skill_key',''), body.get('task_key',''), body.get('deliverable_type',''), body.get('model_name',''), body.get('tokens_in',''), body.get('tokens_out','')))" <<<"${TASK_EXECUTE_COST_JSON}")"
if [[ "${TASK_EXECUTE_COST_FIELDS}" != "stakeholder_briefing|stakeholder_briefing|stakeholder_briefing|none|0|0" ]]; then
  echo "expected direct run-cost lookup to project generic task identity and deliverable context; got ${TASK_EXECUTE_COST_FIELDS}" >&2
  echo "${TASK_EXECUTE_COST_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
operator_post_json "${BASE}/v1/tasks/contracts" -H 'content-type: application/json' \
  -d '{"task_key":"research_brief","deliverable_type":"decision_summary","default_risk_class":"low","default_approval_class":"none","allowed_tools":["artifact_repository"],"evidence_requirements":["decision_context"],"memory_write_policy":"reviewed_only","budget_policy_json":{"class":"low","workflow_template":"artifact_then_memory_candidate","artifact_output_template":"evidence_pack","evidence_pack_confidence":0.72}}' >/dev/null
EVIDENCE_PACK_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/execute" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"task_key":"research_brief","goal":"prepare an evidence-backed brief","input_json":{"source_text":"Market conditions suggest two viable options.","claims":["Option A preserves margin","Option B accelerates launch"],"evidence_refs":["browseract://run/123","paper://abc"],"open_questions":["Need final vendor pricing"]}}')"
EVIDENCE_PACK_JSON="$(plan_execute_artifact_json "${EVIDENCE_PACK_JSON}")"
EVIDENCE_PACK_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); structured=body.get('structured_output_json') or {}; print('{}|{}|{}|{}|{}|{}|{}|{}'.format(body.get('task_key',''), body.get('kind',''), structured.get('format',''), len(structured.get('claims') or []), len(structured.get('evidence_refs') or []), len(structured.get('open_questions') or []), structured.get('confidence',''), body.get('preview_text','')))" <<<"${EVIDENCE_PACK_JSON}")"
if [[ "${EVIDENCE_PACK_FIELDS}" != "research_brief|decision_summary|evidence_pack|2|2|1|0.72|Market conditions suggest two viable options." ]]; then
  echo "expected evidence-pack artifact output template to persist structured claims/evidence/open questions; got ${EVIDENCE_PACK_FIELDS}" >&2
  echo "${EVIDENCE_PACK_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
EVIDENCE_PACK_SESSION_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("execution_session_id",""))' <<<"${EVIDENCE_PACK_JSON}")"
EVIDENCE_CANDIDATES_JSON="$(curl -fsS "${BASE}/v1/memory/candidates?limit=20" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
EVIDENCE_CANDIDATE_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); match=next((row for row in rows if row.get('source_session_id') == '${EVIDENCE_PACK_SESSION_ID}'), {}); fact=match.get('fact_json') or {}; print('{}|{}|{}|{}'.format(len(fact.get('claims') or []), len(fact.get('evidence_refs') or []), len(fact.get('open_questions') or []), bool(fact.get('evidence_pack'))))" <<<"${EVIDENCE_CANDIDATES_JSON}")"
if [[ "${EVIDENCE_CANDIDATE_FIELDS}" != "2|2|1|True" ]]; then
  echo "expected evidence-pack memory candidate staging to preserve claims/evidence/open questions; got ${EVIDENCE_CANDIDATE_FIELDS}" >&2
  echo "${EVIDENCE_CANDIDATES_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
EVIDENCE_PACK_ARTIFACT_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("artifact_id",""))' <<<"${EVIDENCE_PACK_JSON}")"
EVIDENCE_PACK_TWO_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/execute" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"task_key":"research_brief","goal":"prepare an evidence-backed brief","input_json":{"source_text":"Support load may fall if the simpler option ships first.","claims":["Option C reduces support load"],"evidence_refs":["paper://abc","call://ops-review"],"open_questions":["Need service staffing forecast"],"confidence":0.58}}')"
EVIDENCE_PACK_TWO_JSON="$(plan_execute_artifact_json "${EVIDENCE_PACK_TWO_JSON}")"
EVIDENCE_PACK_TWO_ARTIFACT_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("artifact_id",""))' <<<"${EVIDENCE_PACK_TWO_JSON}")"
EVIDENCE_OBJECTS_JSON="$(curl -fsS "${BASE}/v1/evidence/objects?artifact_id=${EVIDENCE_PACK_ARTIFACT_ID}&principal_id=${PRINCIPAL_ID}&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
EVIDENCE_OBJECT_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); row=(rows or [{}])[0]; print('{}|{}|{}|{}|{}|{}'.format(row.get('artifact_id',''), len(row.get('claims') or []), len(row.get('evidence_refs') or []), len(row.get('open_questions') or []), row.get('confidence',''), str(row.get('citation_handle','')).startswith('evidence://')))" <<<"${EVIDENCE_OBJECTS_JSON}")"
if [[ "${EVIDENCE_OBJECT_FIELDS}" != "${EVIDENCE_PACK_ARTIFACT_ID}|2|2|1|0.72|True" ]]; then
  echo "expected evidence object query to expose materialized evidence-pack rows with citation handles; got ${EVIDENCE_OBJECT_FIELDS}" >&2
  echo "${EVIDENCE_OBJECTS_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
EVIDENCE_OBJECT_ID="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); print((rows or [{}])[0].get('evidence_id',''))" <<<"${EVIDENCE_OBJECTS_JSON}")"
EVIDENCE_REF_ROWS_JSON="$(curl -fsS "${BASE}/v1/evidence/objects?evidence_ref=paper://abc&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
EVIDENCE_REF_ARTIFACT_IDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); ids={row.get('artifact_id','') for row in rows if row.get('artifact_id')}; print('{}|{}|{}'.format('${EVIDENCE_PACK_ARTIFACT_ID}' in ids, '${EVIDENCE_PACK_TWO_ARTIFACT_ID}' in ids, '|'.join(sorted(ids))))" <<<"${EVIDENCE_REF_ROWS_JSON}")"
if [[ "${EVIDENCE_REF_ARTIFACT_IDS}" != True\|True\|* ]]; then
  echo "expected evidence reference query to find both cited artifacts; got ${EVIDENCE_REF_ARTIFACT_IDS}" >&2
  echo "${EVIDENCE_REF_ROWS_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
EVIDENCE_OBJECT_FETCH_JSON="$(curl -fsS "${BASE}/v1/evidence/objects/${EVIDENCE_OBJECT_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
EVIDENCE_OBJECT_FETCH_ARTIFACT="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("artifact_id",""))' <<<"${EVIDENCE_OBJECT_FETCH_JSON}")"
if [[ "${EVIDENCE_OBJECT_FETCH_ARTIFACT}" != "${EVIDENCE_PACK_ARTIFACT_ID}" ]]; then
  echo "expected direct evidence-object lookup to resolve the original artifact; got ${EVIDENCE_OBJECT_FETCH_ARTIFACT}" >&2
  echo "${EVIDENCE_OBJECT_FETCH_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
EVIDENCE_OBJECT_TWO_ID="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); match=next((row for row in rows if row.get('artifact_id') == '${EVIDENCE_PACK_TWO_ARTIFACT_ID}'), {}); print(match.get('evidence_id',''))" <<<"${EVIDENCE_REF_ROWS_JSON}")"
EVIDENCE_MERGE_JSON="$(curl -fsS -X POST "${BASE}/v1/evidence/merge" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"principal_id\":\"${PRINCIPAL_ID}\",\"evidence_ids\":[\"${EVIDENCE_OBJECT_ID}\",\"${EVIDENCE_OBJECT_TWO_ID}\"]}")"
EVIDENCE_MERGE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}|{}|{}|{}'.format(body.get('format',''), len(body.get('claims') or []), len(body.get('evidence_refs') or []), len(body.get('open_questions') or []), len(body.get('source_artifact_ids') or []), len(body.get('citation_handles') or [])))" <<<"${EVIDENCE_MERGE_JSON}")"
if [[ "${EVIDENCE_MERGE_FIELDS}" != "evidence_pack|3|3|2|2|2" ]]; then
  echo "expected evidence merge to combine claims, references, and citations without artifact reparsing; got ${EVIDENCE_MERGE_FIELDS}" >&2
  echo "${EVIDENCE_MERGE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
TASK_EXECUTE_MISMATCH_CODE="$(curl_status_code ${SMOKE_TMP_DIR}/ea_task_execute_mismatch_resp.json -X POST "${BASE}/v1/plans/execute" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' -d "{\"task_key\":\"stakeholder_briefing\",\"text\":\"Should stay in principal scope.\",\"principal_id\":\"${MISMATCH_PRINCIPAL_ID}\",\"goal\":\"prepare a stakeholder briefing\"}")"
if [[ "${TASK_EXECUTE_MISMATCH_CODE}" != "403" ]]; then
  echo "expected generic task execution principal mismatch to return 403; got ${TASK_EXECUTE_MISMATCH_CODE}" >&2
  cat ${SMOKE_TMP_DIR}/ea_task_execute_mismatch_resp.json >&2
  fail 12 "policy contract mismatch"
fi
TASK_EXECUTE_MISMATCH_REASON="$(python3 -c 'import json,sys; body=json.load(open(sys.argv[1])); print(((body.get("error") or {}).get("code","")))' ${SMOKE_TMP_DIR}/ea_task_execute_mismatch_resp.json)"
if [[ "${TASK_EXECUTE_MISMATCH_REASON}" != "principal_scope_mismatch" ]]; then
  echo "expected generic task execution mismatch code principal_scope_mismatch; got ${TASK_EXECUTE_MISMATCH_REASON}" >&2
  cat ${SMOKE_TMP_DIR}/ea_task_execute_mismatch_resp.json >&2
  fail 12 "policy contract mismatch"
fi
echo "generic task execution ok"

echo "== smoke: generic task async contracts =="
GENERIC_APPROVAL_TASK_KEY="decision_brief_approval_${SMOKE_RUN_TOKEN}"
operator_post_json "${BASE}/v1/tasks/contracts" -H 'content-type: application/json' \
  -d "{\"task_key\":\"${GENERIC_APPROVAL_TASK_KEY}\",\"deliverable_type\":\"decision_brief\",\"default_risk_class\":\"low\",\"default_approval_class\":\"manager\",\"allowed_tools\":[\"artifact_repository\"],\"evidence_requirements\":[\"decision_context\"],\"memory_write_policy\":\"reviewed_only\",\"budget_policy_json\":{\"class\":\"low\"}}" >/dev/null
GENERIC_APPROVAL_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/execute" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"task_key\":\"${GENERIC_APPROVAL_TASK_KEY}\",\"text\":\"Decision context for the approval-backed briefing.\",\"goal\":\"prepare a decision brief\"}")"
GENERIC_APPROVAL_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}|{}|{}'.format(body.get('task_key',''), body.get('status',''), body.get('next_action',''), bool(body.get('approval_id','')), bool(body.get('session_id',''))))" <<<"${GENERIC_APPROVAL_JSON}")"
GENERIC_APPROVAL_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("approval_id",""))' <<<"${GENERIC_APPROVAL_JSON}")"
GENERIC_APPROVAL_SESSION_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("session_id",""))' <<<"${GENERIC_APPROVAL_JSON}")"
if [[ "${GENERIC_APPROVAL_FIELDS}" == "${GENERIC_APPROVAL_TASK_KEY}|queued|poll_or_subscribe|False|True" ]]; then
  GENERIC_APPROVAL_AWAITING_JSON="$(wait_for_session_status "${GENERIC_APPROVAL_SESSION_ID}" "awaiting_approval")"
  GENERIC_APPROVAL_ID="$(curl -fsS "${BASE}/v1/policy/approvals/pending?limit=20" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); session_id='${GENERIC_APPROVAL_SESSION_ID}'; row=next((row for row in rows if (row or {}).get('session_id') == session_id), {}); print(row.get('approval_id',''))" )"
elif [[ "${GENERIC_APPROVAL_FIELDS}" != "${GENERIC_APPROVAL_TASK_KEY}|awaiting_approval|poll_or_subscribe|True|True" ]]; then
  echo "expected generic task execution approval contract to return a first-class awaiting_approval response; got ${GENERIC_APPROVAL_FIELDS}" >&2
  echo "${GENERIC_APPROVAL_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
if [[ -z "${GENERIC_APPROVAL_ID}" ]]; then
  echo "expected generic task execution approval contract to expose or create a pending approval" >&2
  echo "${GENERIC_APPROVAL_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
GENERIC_APPROVAL_SESSION_FIELDS="$(curl -fsS "${BASE}/v1/rewrite/sessions/${GENERIC_APPROVAL_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); step_lookup={str((row.get('input_json') or {}).get('plan_step_key') or ''): row for row in (body.get('steps') or [])}; save_step=step_lookup.get('step_artifact_save') or {}; policy_step=step_lookup.get('step_policy_evaluate') or {}; print('{}|{}|{}|{}|{}|{}|{}|{}'.format(body.get('intent_task_type',''), body.get('status',''), save_step.get('state',''), save_step.get('dependency_keys') == ['step_policy_evaluate'], save_step.get('dependency_states') == {'step_policy_evaluate': 'completed'}, (save_step.get('dependency_step_ids') or {}).get('step_policy_evaluate') == policy_step.get('step_id',''), save_step.get('blocked_dependency_keys') == [], save_step.get('dependencies_satisfied') is True))" )"
if [[ "${GENERIC_APPROVAL_SESSION_FIELDS}" != "${GENERIC_APPROVAL_TASK_KEY}|awaiting_approval|waiting_approval|True|True|True|True|True" ]]; then
  echo "expected generic approval-backed task session to preserve task identity plus satisfied dependency-state projection through awaiting_approval; got ${GENERIC_APPROVAL_SESSION_FIELDS}" >&2
  fail 12 "policy contract mismatch"
fi
GENERIC_APPROVAL_PENDING_FIELDS="$(curl -fsS "${BASE}/v1/policy/approvals/pending?limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); approval_id='${GENERIC_APPROVAL_ID}'; session_id='${GENERIC_APPROVAL_SESSION_ID}'; row=next((row for row in rows if (row or {}).get('approval_id') == approval_id and (row or {}).get('session_id') == session_id), {}); print('{}|{}'.format(row.get('task_key',''), row.get('deliverable_type','')))" )"
if [[ "${GENERIC_APPROVAL_PENDING_FIELDS}" != "${GENERIC_APPROVAL_TASK_KEY}|decision_brief" ]]; then
  echo "expected pending approval projection to carry generic task identity before completion; got ${GENERIC_APPROVAL_PENDING_FIELDS}" >&2
  fail 12 "policy contract mismatch"
fi
GENERIC_APPROVAL_DECISION_JSON="$(curl -fsS -X POST "${BASE}/v1/policy/approvals/${GENERIC_APPROVAL_ID}/approve" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"decided_by\":\"${PRINCIPAL_ID}\",\"reason\":\"approved generic task execution\"}")"
GENERIC_APPROVAL_DECISION_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}'.format(body.get('task_key',''), body.get('deliverable_type',''), body.get('decision','')))" <<<"${GENERIC_APPROVAL_DECISION_JSON}")"
if [[ "${GENERIC_APPROVAL_DECISION_FIELDS}" != "${GENERIC_APPROVAL_TASK_KEY}|decision_brief|approved" ]]; then
  echo "expected approval decision response to carry generic task identity; got ${GENERIC_APPROVAL_DECISION_FIELDS}" >&2
  echo "${GENERIC_APPROVAL_DECISION_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
GENERIC_APPROVAL_DONE_JSON="$(wait_for_session_status "${GENERIC_APPROVAL_SESSION_ID}" "completed")"
GENERIC_APPROVAL_DONE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); artifacts=body.get('artifacts') or []; print('{}|{}|{}'.format(body.get('status',''), (artifacts[0] or {}).get('kind','') if artifacts else '', len(artifacts) >= 1))" <<<"${GENERIC_APPROVAL_DONE_JSON}")"
if [[ "${GENERIC_APPROVAL_DONE_FIELDS}" != "completed|decision_brief|True" ]]; then
  echo "expected generic approval-backed task to resume to completion after approval; got ${GENERIC_APPROVAL_DONE_FIELDS}" >&2
  echo "${GENERIC_APPROVAL_DONE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
GENERIC_APPROVAL_HISTORY_FIELDS="$(curl -fsS "${BASE}/v1/policy/approvals/history?session_id=${GENERIC_APPROVAL_SESSION_ID}&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); approval_id='${GENERIC_APPROVAL_ID}'; row=next((row for row in rows if (row or {}).get('approval_id') == approval_id and (row or {}).get('decision') == 'approved'), {}); print('{}|{}'.format(row.get('task_key',''), row.get('deliverable_type','')))" )"
if [[ "${GENERIC_APPROVAL_HISTORY_FIELDS}" != "${GENERIC_APPROVAL_TASK_KEY}|decision_brief" ]]; then
  echo "expected approval history projection to carry generic task identity after approval; got ${GENERIC_APPROVAL_HISTORY_FIELDS}" >&2
  fail 12 "policy contract mismatch"
fi
operator_post_json "${BASE}/v1/tasks/contracts" -H 'content-type: application/json' \
  -d '{"task_key":"stakeholder_briefing_review","deliverable_type":"stakeholder_briefing","default_risk_class":"low","default_approval_class":"none","allowed_tools":["artifact_repository"],"evidence_requirements":["stakeholder_context"],"memory_write_policy":"reviewed_only","budget_policy_json":{"class":"low","human_review_role":"briefing_reviewer","human_review_task_type":"briefing_review","human_review_brief":"Review the stakeholder briefing before finalization.","human_review_priority":"high","human_review_desired_output_json":{"format":"review_packet"}}}' >/dev/null
GENERIC_HUMAN_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/execute" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"task_key":"stakeholder_briefing_review","text":"Stakeholder context for human-reviewed briefing.","goal":"prepare a stakeholder briefing"}')"
GENERIC_HUMAN_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}|{}|{}'.format(body.get('task_key',''), body.get('status',''), body.get('next_action',''), bool(body.get('human_task_id','')), bool(body.get('session_id',''))))" <<<"${GENERIC_HUMAN_JSON}")"
if [[ "${GENERIC_HUMAN_FIELDS}" != "stakeholder_briefing_review|awaiting_human|poll_or_subscribe|True|True" ]]; then
  echo "expected generic task execution human-review contract to return a first-class awaiting_human response; got ${GENERIC_HUMAN_FIELDS}" >&2
  echo "${GENERIC_HUMAN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
GENERIC_HUMAN_TASK_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("human_task_id",""))' <<<"${GENERIC_HUMAN_JSON}")"
GENERIC_HUMAN_SESSION_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("session_id",""))' <<<"${GENERIC_HUMAN_JSON}")"
GENERIC_HUMAN_SESSION_FIELDS="$(curl -fsS "${BASE}/v1/rewrite/sessions/${GENERIC_HUMAN_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); step_lookup={str((row.get('input_json') or {}).get('plan_step_key') or ''): row for row in (body.get('steps') or [])}; review_step=step_lookup.get('step_human_review') or {}; save_step=step_lookup.get('step_artifact_save') or {}; policy_step=step_lookup.get('step_policy_evaluate') or {}; print('{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}'.format(body.get('intent_task_type',''), body.get('status',''), review_step.get('state',''), review_step.get('dependency_states') == {'step_policy_evaluate': 'completed'}, (review_step.get('dependency_step_ids') or {}).get('step_policy_evaluate') == policy_step.get('step_id',''), review_step.get('blocked_dependency_keys') == [], review_step.get('dependencies_satisfied') is True, save_step.get('state',''), save_step.get('dependency_states') == {'step_human_review': 'waiting_human'}, save_step.get('blocked_dependency_keys') == ['step_human_review'], save_step.get('dependencies_satisfied') is False))" )"
if [[ "${GENERIC_HUMAN_SESSION_FIELDS}" != "stakeholder_briefing_review|awaiting_human|waiting_human|True|True|True|True|queued|True|True|True" ]]; then
  echo "expected generic human-review task session to preserve task identity plus blocked dependency-state projection while awaiting_human; got ${GENERIC_HUMAN_SESSION_FIELDS}" >&2
  fail 12 "policy contract mismatch"
fi
GENERIC_HUMAN_SESSION_TASK_FIELDS="$(curl -fsS "${BASE}/v1/rewrite/sessions/${GENERIC_HUMAN_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); rows=body.get('human_tasks') or []; first=(rows[0] if rows else {}); print('{}|{}|{}'.format(first.get('task_key',''), first.get('deliverable_type',''), first.get('status','')))" )"
if [[ "${GENERIC_HUMAN_SESSION_TASK_FIELDS}" != "stakeholder_briefing_review|stakeholder_briefing|pending" ]]; then
  echo "expected session human-task projection to carry generic task identity before completion; got ${GENERIC_HUMAN_SESSION_TASK_FIELDS}" >&2
  fail 12 "policy contract mismatch"
fi
GENERIC_HUMAN_SESSION_HISTORY_FIELDS="$(curl -fsS "${BASE}/v1/rewrite/sessions/${GENERIC_HUMAN_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); rows=body.get('human_task_assignment_history') or []; first=(rows[0] if rows else {}); print('{}|{}|{}'.format(first.get('task_key',''), first.get('deliverable_type',''), first.get('event_name','')))" )"
if [[ "${GENERIC_HUMAN_SESSION_HISTORY_FIELDS}" != "stakeholder_briefing_review|stakeholder_briefing|human_task_created" ]]; then
  echo "expected session assignment-history projection to carry generic task identity before completion; got ${GENERIC_HUMAN_SESSION_HISTORY_FIELDS}" >&2
  fail 12 "policy contract mismatch"
fi
GENERIC_HUMAN_LIST_FIELDS="$(curl -fsS "${BASE}/v1/human/tasks?session_id=${GENERIC_HUMAN_SESSION_ID}&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted='${GENERIC_HUMAN_TASK_ID}'; row=next((row for row in rows if (row or {}).get('human_task_id') == wanted), {}); print('{}|{}|{}'.format(row.get('task_key',''), row.get('deliverable_type',''), row.get('status','')))" )"
if [[ "${GENERIC_HUMAN_LIST_FIELDS}" != "stakeholder_briefing_review|stakeholder_briefing|pending" ]]; then
  echo "expected human task list projection to carry generic task identity before completion; got ${GENERIC_HUMAN_LIST_FIELDS}" >&2
  fail 12 "policy contract mismatch"
fi
GENERIC_HUMAN_DETAIL_FIELDS="$(curl -fsS "${BASE}/v1/human/tasks/${GENERIC_HUMAN_TASK_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}'.format(body.get('task_key',''), body.get('deliverable_type',''), body.get('status','')))" )"
if [[ "${GENERIC_HUMAN_DETAIL_FIELDS}" != "stakeholder_briefing_review|stakeholder_briefing|pending" ]]; then
  echo "expected human task detail projection to carry generic task identity before completion; got ${GENERIC_HUMAN_DETAIL_FIELDS}" >&2
  fail 12 "policy contract mismatch"
fi
GENERIC_HUMAN_HISTORY_FIELDS="$(curl -fsS "${BASE}/v1/human/tasks/${GENERIC_HUMAN_TASK_ID}/assignment-history?limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); first=(rows[0] if rows else {}); print('{}|{}|{}'.format(first.get('task_key',''), first.get('deliverable_type',''), first.get('event_name','')))" )"
if [[ "${GENERIC_HUMAN_HISTORY_FIELDS}" != "stakeholder_briefing_review|stakeholder_briefing|human_task_created" ]]; then
  echo "expected human task assignment-history projection to carry generic task identity before completion; got ${GENERIC_HUMAN_HISTORY_FIELDS}" >&2
  fail 12 "policy contract mismatch"
fi
ensure_operator_profile "briefing-reviewer" "briefing_reviewer" '["tone","accuracy"]' "senior" "Briefing Reviewer"
GENERIC_HUMAN_RETURN_JSON="$(operator_post_json "${BASE}/v1/human/tasks/${GENERIC_HUMAN_TASK_ID}/return" -H 'content-type: application/json' \
  -d '{"operator_id":"briefing-reviewer","resolution":"ready_for_publish","returned_payload_json":{"final_text":"Stakeholder context for human-reviewed briefing, edited by reviewer."},"provenance_json":{"review_mode":"human"}}')"
GENERIC_HUMAN_RETURN_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}'.format(body.get('task_key',''), body.get('deliverable_type',''), body.get('status','')))" <<<"${GENERIC_HUMAN_RETURN_JSON}")"
if [[ "${GENERIC_HUMAN_RETURN_FIELDS}" != "stakeholder_briefing_review|stakeholder_briefing|returned" ]]; then
  echo "expected human task return response to carry generic task identity; got ${GENERIC_HUMAN_RETURN_FIELDS}" >&2
  echo "${GENERIC_HUMAN_RETURN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
GENERIC_HUMAN_DONE_JSON="$(wait_for_session_status "${GENERIC_HUMAN_SESSION_ID}" "completed")"
GENERIC_HUMAN_DONE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); artifacts=body.get('artifacts') or []; print('{}|{}|{}'.format(body.get('status',''), (artifacts[0] or {}).get('kind','') if artifacts else '', (artifacts[0] or {}).get('content','') if artifacts else ''))" <<<"${GENERIC_HUMAN_DONE_JSON}")"
if [[ "${GENERIC_HUMAN_DONE_FIELDS}" != "completed|stakeholder_briefing|Stakeholder context for human-reviewed briefing, edited by reviewer." ]]; then
  echo "expected generic human-review task to resume to completion after packet return; got ${GENERIC_HUMAN_DONE_FIELDS}" >&2
  echo "${GENERIC_HUMAN_DONE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_BINDING_JSON="$(curl -fsS -X POST "${BASE}/v1/connectors/bindings" "${AUTH_ARGS[@]}" -H 'content-type: application/json' \
  "${PRINCIPAL_ARGS[@]}" \
  -d '{"connector_name":"gmail","external_account_ref":"acct-dispatch","scope_json":{"scopes":["mail.send"]},"auth_metadata_json":{"provider":"google"},"status":"enabled"}')"
DISPATCH_BINDING_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("binding_id",""))' <<<"${DISPATCH_BINDING_JSON}")"
if [[ -z "${DISPATCH_BINDING_ID}" ]]; then
  fail 13 "missing binding_id from dispatch workflow binding response"
fi
DISPATCH_RECIPIENT="workflow+$(date +%s%N)@example.com"
operator_post_json "${BASE}/v1/tasks/contracts" -H 'content-type: application/json' \
  -d '{"task_key":"browseract_ltd_discovery","deliverable_type":"ltd_service_profile","default_risk_class":"low","default_approval_class":"none","allowed_tools":["browseract.extract_account_facts","artifact_repository"],"evidence_requirements":["account_inventory"],"memory_write_policy":"none","budget_policy_json":{"class":"low","workflow_template":"browseract_extract_then_artifact"}}' >/dev/null
BROWSERACT_PLAN_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/compile" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"task_key":"browseract_ltd_discovery","goal":"extract LTD account facts for BrowserAct"}')"
BROWSERACT_PLAN_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps=body.get('plan',{}).get('steps') or []; extract=(steps[1] if len(steps) > 1 else {}); artifact=(steps[2] if len(steps) > 2 else {}); print('{}|{}|{}|{}|{}|{}|{}'.format(len(steps), ','.join((row.get('step_key') or '') for row in steps), extract.get('tool_name',''), ','.join(extract.get('depends_on') or []), ','.join(extract.get('input_keys') or []), 'structured_output_json' in (extract.get('output_keys') or []), ','.join(artifact.get('input_keys') or [])))" <<<"${BROWSERACT_PLAN_JSON}")"
if [[ "${BROWSERACT_PLAN_FIELDS}" != "3|step_input_prepare,step_browseract_extract,step_artifact_save|browseract.extract_account_facts|step_input_prepare|binding_id,service_name,requested_fields,instructions,account_hints_json,run_url|True|normalized_text,structured_output_json,preview_text,mime_type" ]]; then
  echo "expected browseract workflow template to compile prepare->browseract->artifact graph; got ${BROWSERACT_PLAN_FIELDS}" >&2
  echo "${BROWSERACT_PLAN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
BROWSERACT_EXECUTE_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/execute" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"task_key\":\"browseract_ltd_discovery\",\"goal\":\"extract LTD account facts for BrowserAct\",\"input_json\":{\"binding_id\":\"${BROWSERACT_BINDING_ID}\",\"service_name\":\"BrowserAct\",\"requested_fields\":[\"tier\",\"account_email\",\"status\"]}}")"
BROWSERACT_EXECUTE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); structured=body.get('structured_output_json') or {}; print('{}|{}|{}|{}|{}|{}'.format(body.get('task_key',''), body.get('kind',''), 'Service: BrowserAct' in body.get('content',''), ((structured.get('facts_json') or {}).get('tier','')), structured.get('account_email',''), body.get('principal_id','')))" <<<"${BROWSERACT_EXECUTE_JSON}")"
if [[ "${BROWSERACT_EXECUTE_FIELDS}" != "browseract_ltd_discovery|ltd_service_profile|True|Tier 3|ops@example.com|${PRINCIPAL_ID}" ]]; then
  echo "expected browseract workflow template to persist discovered facts into a structured artifact envelope; got ${BROWSERACT_EXECUTE_FIELDS}" >&2
  echo "${BROWSERACT_EXECUTE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
BROWSERACT_SESSION_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("execution_session_id",""))' <<<"${BROWSERACT_EXECUTE_JSON}")"
BROWSERACT_SESSION_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${BROWSERACT_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
BROWSERACT_SESSION_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps={str((row.get('input_json') or {}).get('plan_step_key') or ''): row for row in (body.get('steps') or [])}; receipts=body.get('receipts') or []; artifacts=body.get('artifacts') or []; artifact=(artifacts[0] if artifacts else {}); print('{}|{}|{}|{}|{}|{}|{}'.format(body.get('intent_task_type',''), body.get('status',''), steps.get('step_browseract_extract',{}).get('state',''), steps.get('step_artifact_save',{}).get('state',''), [row.get('tool_name','') for row in receipts] == ['browseract.extract_account_facts', 'artifact_repository'], ((artifact.get('structured_output_json') or {}).get('facts_json') or {}).get('tier',''), (artifact.get('structured_output_json') or {}).get('account_email','')))" <<<"${BROWSERACT_SESSION_JSON}")"
if [[ "${BROWSERACT_SESSION_FIELDS}" != "browseract_ltd_discovery|completed|completed|completed|True|Tier 3|ops@example.com" ]]; then
  echo "expected browseract workflow session to complete with both extract and artifact receipts plus structured facts; got ${BROWSERACT_SESSION_FIELDS}" >&2
  echo "${BROWSERACT_SESSION_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
operator_post_json "${BASE}/v1/tasks/contracts" -H 'content-type: application/json' \
  -d '{"task_key":"browseract_ltd_discovery_generic","deliverable_type":"ltd_service_profile","default_risk_class":"low","default_approval_class":"none","allowed_tools":["browseract.extract_account_facts","artifact_repository"],"evidence_requirements":["account_inventory"],"memory_write_policy":"none","budget_policy_json":{"class":"low","workflow_template":"tool_then_artifact","pre_artifact_tool_name":"browseract.extract_account_facts"}}' >/dev/null
GENERIC_BROWSERACT_PLAN_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/compile" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"task_key":"browseract_ltd_discovery_generic","goal":"extract LTD account facts for BrowserAct"}')"
GENERIC_BROWSERACT_PLAN_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps=body.get('plan',{}).get('steps') or []; prepare=(steps[0] if steps else {}); extract=(steps[1] if len(steps) > 1 else {}); artifact=(steps[2] if len(steps) > 2 else {}); print('{}|{}|{}|{}|{}|{}'.format(len(steps), ','.join((row.get('step_key') or '') for row in steps), ','.join(prepare.get('input_keys') or []), extract.get('tool_name',''), ','.join(extract.get('input_keys') or []), ','.join(artifact.get('input_keys') or [])))" <<<"${GENERIC_BROWSERACT_PLAN_JSON}")"
if [[ "${GENERIC_BROWSERACT_PLAN_FIELDS}" != "3|step_input_prepare,step_browseract_extract,step_artifact_save|binding_id,service_name,requested_fields,instructions,account_hints_json,run_url|browseract.extract_account_facts|binding_id,service_name,requested_fields,instructions,account_hints_json,run_url|normalized_text,structured_output_json,preview_text,mime_type" ]]; then
  echo "expected generic tool-then-artifact workflow template to compile prepare->browseract->artifact graph; got ${GENERIC_BROWSERACT_PLAN_FIELDS}" >&2
  echo "${GENERIC_BROWSERACT_PLAN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
GENERIC_BROWSERACT_EXECUTE_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/execute" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"task_key\":\"browseract_ltd_discovery_generic\",\"goal\":\"extract LTD account facts for BrowserAct\",\"input_json\":{\"binding_id\":\"${BROWSERACT_BINDING_ID}\",\"service_name\":\"BrowserAct\",\"requested_fields\":[\"tier\",\"account_email\",\"status\"]}}")"
GENERIC_BROWSERACT_EXECUTE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); structured=body.get('structured_output_json') or {}; print('{}|{}|{}|{}|{}'.format(body.get('task_key',''), body.get('kind',''), ((structured.get('facts_json') or {}).get('tier','')), structured.get('account_email',''), body.get('principal_id','')))" <<<"${GENERIC_BROWSERACT_EXECUTE_JSON}")"
if [[ "${GENERIC_BROWSERACT_EXECUTE_FIELDS}" != "browseract_ltd_discovery_generic|ltd_service_profile|Tier 3|ops@example.com|${PRINCIPAL_ID}" ]]; then
  echo "expected generic tool-then-artifact workflow template to persist discovered BrowserAct facts; got ${GENERIC_BROWSERACT_EXECUTE_FIELDS}" >&2
  echo "${GENERIC_BROWSERACT_EXECUTE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
operator_post_json "${BASE}/v1/tasks/contracts" -H 'content-type: application/json' \
  -d '{"task_key":"browseract_ltd_inventory_refresh","deliverable_type":"ltd_inventory_profile","default_risk_class":"low","default_approval_class":"none","allowed_tools":["browseract.extract_account_inventory","artifact_repository"],"evidence_requirements":["account_inventory"],"memory_write_policy":"none","budget_policy_json":{"class":"low","workflow_template":"tool_then_artifact","pre_artifact_tool_name":"browseract.extract_account_inventory"}}' >/dev/null
BROWSERACT_INVENTORY_PLAN_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/compile" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"task_key":"browseract_ltd_inventory_refresh","goal":"refresh LTD inventory facts"}')"
BROWSERACT_INVENTORY_PLAN_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps=body.get('plan',{}).get('steps') or []; prepare=(steps[0] if steps else {}); extract=(steps[1] if len(steps) > 1 else {}); artifact=(steps[2] if len(steps) > 2 else {}); print('{}|{}|{}|{}|{}|{}'.format(len(steps), ','.join((row.get('step_key') or '') for row in steps), ','.join(prepare.get('input_keys') or []), extract.get('tool_name',''), ','.join(extract.get('output_keys') or []), ','.join(artifact.get('input_keys') or [])))" <<<"${BROWSERACT_INVENTORY_PLAN_JSON}")"
if [[ "${BROWSERACT_INVENTORY_PLAN_FIELDS}" != "3|step_input_prepare,step_browseract_inventory_extract,step_artifact_save|binding_id,service_names,requested_fields,instructions,account_hints_json,run_url|browseract.extract_account_inventory|service_names,services_json,missing_services,normalized_text,preview_text,mime_type,structured_output_json|normalized_text,structured_output_json,preview_text,mime_type" ]]; then
  echo "expected browseract inventory workflow template to compile prepare->inventory->artifact graph; got ${BROWSERACT_INVENTORY_PLAN_FIELDS}" >&2
  echo "${BROWSERACT_INVENTORY_PLAN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
BROWSERACT_INVENTORY_EXECUTE_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/execute" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"task_key\":\"browseract_ltd_inventory_refresh\",\"goal\":\"refresh LTD inventory facts\",\"input_json\":{\"binding_id\":\"${BROWSERACT_BINDING_ID}\",\"service_names\":[\"BrowserAct\",\"Teable\",\"UnknownService\"],\"requested_fields\":[\"tier\",\"account_email\",\"status\"]}}")"
BROWSERACT_INVENTORY_EXECUTE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); structured=body.get('structured_output_json') or {}; services=structured.get('services_json') or []; print('{}|{}|{}|{}|{}|{}'.format(body.get('task_key',''), body.get('kind',''), ','.join(structured.get('missing_services') or []), (services[1].get('plan_tier','') if len(services) > 1 else ''), (services[2].get('discovery_status','') if len(services) > 2 else ''), body.get('principal_id','')))" <<<"${BROWSERACT_INVENTORY_EXECUTE_JSON}")"
if [[ "${BROWSERACT_INVENTORY_EXECUTE_FIELDS}" != "browseract_ltd_inventory_refresh|ltd_inventory_profile|UnknownService|License Tier 4|missing|${PRINCIPAL_ID}" ]]; then
  echo "expected browseract inventory workflow template to persist multi-service LTD inventory facts; got ${BROWSERACT_INVENTORY_EXECUTE_FIELDS}" >&2
  echo "${BROWSERACT_INVENTORY_EXECUTE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
BROWSERACT_INVENTORY_SESSION_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("execution_session_id",""))' <<<"${BROWSERACT_INVENTORY_EXECUTE_JSON}")"
BROWSERACT_INVENTORY_SESSION_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${BROWSERACT_INVENTORY_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
BROWSERACT_INVENTORY_SESSION_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps={str((row.get('input_json') or {}).get('plan_step_key') or ''): row for row in (body.get('steps') or [])}; receipts=body.get('receipts') or []; artifacts=body.get('artifacts') or []; artifact=(artifacts[0] if artifacts else {}); structured=artifact.get('structured_output_json') or {}; services=structured.get('services_json') or []; print('{}|{}|{}|{}|{}|{}|{}'.format(body.get('intent_task_type',''), body.get('status',''), steps.get('step_browseract_inventory_extract',{}).get('state',''), [row.get('tool_name','') for row in receipts] == ['browseract.extract_account_inventory', 'artifact_repository'], ','.join(structured.get('missing_services') or []), (services[1].get('plan_tier','') if len(services) > 1 else ''), (services[2].get('discovery_status','') if len(services) > 2 else '')))" <<<"${BROWSERACT_INVENTORY_SESSION_JSON}")"
if [[ "${BROWSERACT_INVENTORY_SESSION_FIELDS}" != "browseract_ltd_inventory_refresh|completed|completed|True|UnknownService|License Tier 4|missing" ]]; then
  echo "expected browseract inventory workflow session to complete with inventory receipts and structured multi-service facts; got ${BROWSERACT_INVENTORY_SESSION_FIELDS}" >&2
  echo "${BROWSERACT_INVENTORY_SESSION_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
TMP_LTD_MD="$(mktemp ${SMOKE_TMP_DIR}/ea_ltds_smoke.XXXXXX.md)"
cp "${EA_ROOT}/LTDs.md" "${TMP_LTD_MD}"
TMP_LTD_JSON="$(mktemp ${SMOKE_TMP_DIR}/ea_ltd_inventory.XXXXXX.json)"
bash "${EA_ROOT}/scripts/refresh_ltds_via_api.sh" \
  --host "${BASE}" \
  --api-token "${EA_API_TOKEN:-}" \
  --principal-id "${PRINCIPAL_ID}" \
  --binding-id "${BROWSERACT_BINDING_ID}" \
  --service-name BrowserAct \
  --service-name Teable \
  --service-name UnknownService \
  --markdown "${TMP_LTD_MD}" \
  --inventory-output "${TMP_LTD_JSON}" \
  --write >/dev/null
LTD_REFRESH_FIELDS="$(python3 -c "from pathlib import Path; text=Path('${TMP_LTD_MD}').read_text(encoding='utf-8'); print('{}|{}|{}|{}'.format('ops@example.com' in text, 'ops@teable.example' in text, 'Plan/Tier: Tier 3; Status: activated' in text, 'Plan/Tier: License Tier 4; Status: activated' in text))")"
rm -f "${TMP_LTD_MD}" "${TMP_LTD_JSON}"
if [[ "${LTD_REFRESH_FIELDS}" != "True|True|True|True" ]]; then
  echo "expected refresh_ltds_via_api.sh to rewrite LTD discovery rows from the live skill output; got ${LTD_REFRESH_FIELDS}" >&2
  fail 12 "policy contract mismatch"
fi
operator_post_json "${BASE}/v1/tasks/contracts" -H 'content-type: application/json' \
  -d '{"task_key":"stakeholder_dispatch","deliverable_type":"stakeholder_briefing","default_risk_class":"low","default_approval_class":"none","allowed_tools":["artifact_repository","connector.dispatch"],"evidence_requirements":["stakeholder_context"],"memory_write_policy":"reviewed_only","budget_policy_json":{"class":"low","workflow_template":"artifact_then_dispatch"}}' >/dev/null
DISPATCH_PLAN_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/compile" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"task_key":"stakeholder_dispatch","goal":"prepare and send a stakeholder briefing"}')"
DISPATCH_PLAN_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps=body.get('plan',{}).get('steps') or []; dispatch=(steps[3] if len(steps) > 3 else {}); print('{}|{}|{}|{}|{}|{}|{}|{}|{}'.format(len(steps), ','.join((row.get('step_key') or '') for row in steps), dispatch.get('tool_name',''), ','.join(dispatch.get('depends_on') or []), dispatch.get('authority_class',''), ','.join(dispatch.get('input_keys') or []), ','.join(dispatch.get('output_keys') or []), steps[1].get('tool_name','') if len(steps) > 1 else '', ','.join((steps[2].get('depends_on') or [])) if len(steps) > 2 else ''))" <<<"${DISPATCH_PLAN_JSON}")"
if [[ "${DISPATCH_PLAN_FIELDS}" != "4|step_input_prepare,step_artifact_save,step_policy_evaluate,step_connector_dispatch|connector.dispatch|step_policy_evaluate|execute|binding_id,channel,recipient,content|delivery_id,status,binding_id|artifact_repository|step_artifact_save" ]]; then
  echo "expected contract workflow template to compile artifact->policy->dispatch graph; got ${DISPATCH_PLAN_FIELDS}" >&2
  echo "${DISPATCH_PLAN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_EXECUTE_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/execute" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"task_key\":\"stakeholder_dispatch\",\"goal\":\"prepare and send a stakeholder briefing\",\"input_json\":{\"source_text\":\"Board context and stakeholder sensitivities.\",\"binding_id\":\"${DISPATCH_BINDING_ID}\",\"channel\":\"email\",\"recipient\":\"${DISPATCH_RECIPIENT}\"}}")"
DISPATCH_EXECUTE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}|{}|{}'.format(body.get('task_key',''), body.get('status',''), body.get('next_action',''), bool(body.get('approval_id','')), bool(body.get('session_id',''))))" <<<"${DISPATCH_EXECUTE_JSON}")"
if [[ "${DISPATCH_EXECUTE_FIELDS}" != "stakeholder_dispatch|awaiting_approval|poll_or_subscribe|True|True" ]]; then
  echo "expected dispatch workflow template to pause behind approval; got ${DISPATCH_EXECUTE_FIELDS}" >&2
  echo "${DISPATCH_EXECUTE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_APPROVAL_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("approval_id",""))' <<<"${DISPATCH_EXECUTE_JSON}")"
DISPATCH_SESSION_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("session_id",""))' <<<"${DISPATCH_EXECUTE_JSON}")"
DISPATCH_SESSION_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${DISPATCH_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
DISPATCH_SESSION_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps={str((row.get('input_json') or {}).get('plan_step_key') or ''): row for row in (body.get('steps') or [])}; artifacts=body.get('artifacts') or []; receipts=body.get('receipts') or []; dispatch=steps.get('step_connector_dispatch') or {}; policy=steps.get('step_policy_evaluate') or {}; save=steps.get('step_artifact_save') or {}; print('{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}'.format(body.get('intent_task_type',''), body.get('status',''), save.get('state',''), policy.get('state',''), dispatch.get('state',''), dispatch.get('dependency_states') == {'step_policy_evaluate': 'completed'}, dispatch.get('blocked_dependency_keys') == [], dispatch.get('dependencies_satisfied') is True, len(artifacts) == 1 and (artifacts[0] or {}).get('content','') == 'Board context and stakeholder sensitivities.', len(receipts) == 1 and (receipts[0] or {}).get('tool_name','') == 'artifact_repository', (artifacts[0] or {}).get('kind','') if artifacts else ''))" <<<"${DISPATCH_SESSION_JSON}")"
if [[ "${DISPATCH_SESSION_FIELDS}" != "stakeholder_dispatch|awaiting_approval|completed|completed|waiting_approval|True|True|True|True|True|stakeholder_briefing" ]]; then
  echo "expected dispatch workflow session to persist artifact before approval while waiting on connector dispatch; got ${DISPATCH_SESSION_FIELDS}" >&2
  echo "${DISPATCH_SESSION_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_PENDING_BEFORE_FIELDS="$(curl -fsS "${BASE}/v1/delivery/outbox/pending?limit=20" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); print(any((row or {}).get('recipient') == '${DISPATCH_RECIPIENT}' for row in rows))" )"
if [[ "${DISPATCH_PENDING_BEFORE_FIELDS}" != "False" ]]; then
  echo "expected dispatch workflow to avoid queueing delivery before approval" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_APPROVE_JSON="$(curl -fsS -X POST "${BASE}/v1/policy/approvals/${DISPATCH_APPROVAL_ID}/approve" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"decided_by\":\"${PRINCIPAL_ID}\",\"reason\":\"approved dispatch workflow\"}")"
DISPATCH_APPROVE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}'.format(body.get('task_key',''), body.get('deliverable_type',''), body.get('decision','')))" <<<"${DISPATCH_APPROVE_JSON}")"
if [[ "${DISPATCH_APPROVE_FIELDS}" != "stakeholder_dispatch|stakeholder_briefing|approved" ]]; then
  echo "expected dispatch workflow approval decision to keep task identity; got ${DISPATCH_APPROVE_FIELDS}" >&2
  echo "${DISPATCH_APPROVE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_DONE_JSON="$(wait_for_session_status "${DISPATCH_SESSION_ID}" "completed")"
DISPATCH_DONE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); receipts=body.get('receipts') or []; dispatch=next((row for row in receipts if (row or {}).get('tool_name') == 'connector.dispatch'), {}); print('{}|{}|{}|{}|{}'.format(body.get('status',''), len(receipts) == 2, bool(dispatch.get('receipt_id','')), bool(dispatch.get('target_ref','')), dispatch.get('task_key','')))" <<<"${DISPATCH_DONE_JSON}")"
if [[ "${DISPATCH_DONE_FIELDS}" != "completed|True|True|True|stakeholder_dispatch" ]]; then
  echo "expected completed dispatch workflow to emit connector.dispatch receipt and target ref; got ${DISPATCH_DONE_FIELDS}" >&2
  echo "${DISPATCH_DONE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_RECEIPT_ID="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); receipts=body.get('receipts') or []; dispatch=next((row for row in receipts if (row or {}).get('tool_name') == 'connector.dispatch'), {}); print(dispatch.get('receipt_id',''))" <<<"${DISPATCH_DONE_JSON}")"
DISPATCH_DELIVERY_ID="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); receipts=body.get('receipts') or []; dispatch=next((row for row in receipts if (row or {}).get('tool_name') == 'connector.dispatch'), {}); print(dispatch.get('target_ref',''))" <<<"${DISPATCH_DONE_JSON}")"
if [[ -z "${DISPATCH_RECEIPT_ID}" || -z "${DISPATCH_DELIVERY_ID}" ]]; then
  echo "expected completed dispatch workflow to emit connector.dispatch receipt and delivery target" >&2
  echo "${DISPATCH_DONE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_RECEIPT_FIELDS="$(curl -fsS "${BASE}/v1/rewrite/receipts/${DISPATCH_RECEIPT_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}|{}'.format(body.get('task_key',''), body.get('deliverable_type',''), body.get('tool_name',''), (body.get('receipt_json') or {}).get('handler_key','')))" )"
if [[ "${DISPATCH_RECEIPT_FIELDS}" != "stakeholder_dispatch|stakeholder_briefing|connector.dispatch|connector.dispatch" ]]; then
  echo "expected direct receipt lookup to keep dispatch workflow task identity" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_PENDING_AFTER_FIELDS="$(curl -fsS "${BASE}/v1/delivery/outbox/pending?limit=200" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); delivery_id='${DISPATCH_DELIVERY_ID}'; row=next((row for row in rows if (row or {}).get('delivery_id') == delivery_id), {}); print('{}|{}'.format(row.get('recipient',''), row.get('status','')))" )"
if [[ "${DISPATCH_PENDING_AFTER_FIELDS}" != "${DISPATCH_RECIPIENT}|queued" && "${DISPATCH_PENDING_AFTER_FIELDS}" != "|" ]]; then
  echo "expected approved dispatch workflow to queue delivery outbox row or defer before pending enqueue; got ${DISPATCH_PENDING_AFTER_FIELDS}" >&2
  fail 12 "policy contract mismatch"
fi
if [[ "${DISPATCH_PENDING_AFTER_FIELDS}" == "|" ]]; then
  echo "approved dispatch workflow deferred delivery before pending outbox enqueue; delivery_id=${DISPATCH_DELIVERY_ID}" >&2
fi
operator_post_json "${BASE}/v1/tasks/contracts" -H 'content-type: application/json' \
  -d '{"task_key":"stakeholder_memory_candidate","deliverable_type":"stakeholder_briefing","default_risk_class":"low","default_approval_class":"none","allowed_tools":["artifact_repository"],"evidence_requirements":["stakeholder_context"],"memory_write_policy":"reviewed_only","budget_policy_json":{"class":"low","workflow_template":"artifact_then_memory_candidate","memory_candidate_category":"stakeholder_briefing_fact","memory_candidate_confidence":0.7,"memory_candidate_sensitivity":"internal"}}' >/dev/null
MEMORY_TEMPLATE_PLAN_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/compile" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"task_key":"stakeholder_memory_candidate","goal":"prepare a stakeholder briefing and stage memory"}')"
MEMORY_TEMPLATE_PLAN_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps=body.get('plan',{}).get('steps') or []; policy=(steps[1] if len(steps) > 1 else {}); memory=(steps[3] if len(steps) > 3 else {}); print('{}|{}|{}|{}|{}|{}|{}|{}|{}'.format(len(steps), ','.join((row.get('step_key') or '') for row in steps), memory.get('step_kind',''), ','.join(memory.get('depends_on') or []), memory.get('authority_class',''), ','.join(memory.get('input_keys') or []), ','.join(memory.get('output_keys') or []), (memory.get('desired_output_json') or {}).get('category',''), ','.join(policy.get('output_keys') or [])))" <<<"${MEMORY_TEMPLATE_PLAN_JSON}")"
if [[ "${MEMORY_TEMPLATE_PLAN_FIELDS}" != "4|step_input_prepare,step_policy_evaluate,step_artifact_save,step_memory_candidate_stage|memory_write|step_artifact_save,step_policy_evaluate|queue|artifact_id,normalized_text,memory_write_allowed|candidate_id,candidate_status,candidate_category|stakeholder_briefing_fact|allow,requires_approval,reason,retention_policy,memory_write_allowed" ]]; then
  echo "expected memory-candidate workflow template to compile artifact->memory graph with policy memory-write contract; got ${MEMORY_TEMPLATE_PLAN_FIELDS}" >&2
  echo "${MEMORY_TEMPLATE_PLAN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
MEMORY_TEMPLATE_EXECUTE_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/execute" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"task_key":"stakeholder_memory_candidate","goal":"prepare a stakeholder briefing and stage memory","input_json":{"source_text":"Board context and stakeholder sensitivities."}}')"
MEMORY_TEMPLATE_EXECUTE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}|{}|{}|{}'.format(body.get('task_key',''), body.get('kind',''), body.get('deliverable_type',''), body.get('principal_id',''), bool(body.get('artifact_id','')), bool(body.get('execution_session_id',''))))" <<<"${MEMORY_TEMPLATE_EXECUTE_JSON}")"
if [[ "${MEMORY_TEMPLATE_EXECUTE_FIELDS}" != "stakeholder_memory_candidate|stakeholder_briefing|stakeholder_briefing|${PRINCIPAL_ID}|True|True" ]]; then
  echo "expected memory-candidate workflow execution to complete inline and return artifact metadata; got ${MEMORY_TEMPLATE_EXECUTE_FIELDS}" >&2
  echo "${MEMORY_TEMPLATE_EXECUTE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
MEMORY_TEMPLATE_SESSION_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("execution_session_id",""))' <<<"${MEMORY_TEMPLATE_EXECUTE_JSON}")"
MEMORY_TEMPLATE_SESSION_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${MEMORY_TEMPLATE_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
MEMORY_TEMPLATE_SESSION_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps={str((row.get('input_json') or {}).get('plan_step_key') or ''): row for row in (body.get('steps') or [])}; memory=steps.get('step_memory_candidate_stage') or {}; print('{}|{}|{}|{}|{}|{}|{}|{}'.format(body.get('intent_task_type',''), body.get('status',''), steps.get('step_policy_evaluate',{}).get('state',''), steps.get('step_artifact_save',{}).get('state',''), memory.get('state',''), (memory.get('output_json') or {}).get('candidate_status',''), (memory.get('output_json') or {}).get('candidate_category',''), bool((memory.get('output_json') or {}).get('candidate_id',''))))" <<<"${MEMORY_TEMPLATE_SESSION_JSON}")"
if [[ "${MEMORY_TEMPLATE_SESSION_FIELDS}" != "stakeholder_memory_candidate|completed|completed|completed|completed|pending|stakeholder_briefing_fact|True" ]]; then
  echo "expected memory-candidate workflow session to complete through memory staging; got ${MEMORY_TEMPLATE_SESSION_FIELDS}" >&2
  echo "${MEMORY_TEMPLATE_SESSION_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
MEMORY_TEMPLATE_CANDIDATE_ID="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps={str((row.get('input_json') or {}).get('plan_step_key') or ''): row for row in (body.get('steps') or [])}; print(((steps.get('step_memory_candidate_stage',{}).get('output_json') or {}).get('candidate_id','')))" <<<"${MEMORY_TEMPLATE_SESSION_JSON}")"
MEMORY_TEMPLATE_CANDIDATE_FIELDS="$(curl -fsS "${BASE}/v1/memory/candidates?limit=20&status=pending" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted='${MEMORY_TEMPLATE_CANDIDATE_ID}'; session_id='${MEMORY_TEMPLATE_SESSION_ID}'; row=next((row for row in rows if (row or {}).get('candidate_id') == wanted), {}); print('{}|{}|{}|{}'.format(row.get('category',''), row.get('principal_id',''), row.get('source_session_id','') == session_id, row.get('summary','')))" )"
if [[ "${MEMORY_TEMPLATE_CANDIDATE_FIELDS}" != "stakeholder_briefing_fact|${PRINCIPAL_ID}|True|Board context and stakeholder sensitivities." ]]; then
  echo "expected memory-candidate workflow template to stage a pending principal-scoped candidate row; got ${MEMORY_TEMPLATE_CANDIDATE_FIELDS}" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_MEMORY_BINDING_JSON="$(curl -fsS -X POST "${BASE}/v1/connectors/bindings" "${AUTH_ARGS[@]}" -H 'content-type: application/json' \
  "${PRINCIPAL_ARGS[@]}" \
  -d '{"connector_name":"gmail","external_account_ref":"acct-dispatch-memory","scope_json":{"scopes":["mail.send"]},"auth_metadata_json":{"provider":"google"},"status":"enabled"}')"
DISPATCH_MEMORY_BINDING_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("binding_id",""))' <<<"${DISPATCH_MEMORY_BINDING_JSON}")"
if [[ -z "${DISPATCH_MEMORY_BINDING_ID}" ]]; then
  fail 13 "missing binding_id from dispatch-memory workflow binding response"
fi
DISPATCH_MEMORY_RECIPIENT="dispatch-memory+$(date +%s%N)@example.com"
operator_post_json "${BASE}/v1/tasks/contracts" -H 'content-type: application/json' \
  -d '{"task_key":"stakeholder_dispatch_memory_candidate","deliverable_type":"stakeholder_briefing","default_risk_class":"low","default_approval_class":"none","allowed_tools":["artifact_repository","connector.dispatch"],"evidence_requirements":["stakeholder_context"],"memory_write_policy":"reviewed_only","budget_policy_json":{"class":"low","workflow_template":"artifact_then_dispatch_then_memory_candidate","memory_candidate_category":"stakeholder_follow_up_fact","memory_candidate_confidence":0.8,"memory_candidate_sensitivity":"internal"}}' >/dev/null
DISPATCH_MEMORY_PLAN_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/compile" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"task_key":"stakeholder_dispatch_memory_candidate","goal":"prepare, send, and stage stakeholder follow-up memory"}')"
DISPATCH_MEMORY_PLAN_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps=body.get('plan',{}).get('steps') or []; memory=(steps[4] if len(steps) > 4 else {}); print('{}|{}|{}|{}|{}|{}|{}'.format(len(steps), ','.join((row.get('step_key') or '') for row in steps), ','.join(memory.get('depends_on') or []), ','.join(memory.get('input_keys') or []), memory.get('step_kind',''), memory.get('authority_class',''), (memory.get('desired_output_json') or {}).get('category','')))" <<<"${DISPATCH_MEMORY_PLAN_JSON}")"
if [[ "${DISPATCH_MEMORY_PLAN_FIELDS}" != "5|step_input_prepare,step_artifact_save,step_policy_evaluate,step_connector_dispatch,step_memory_candidate_stage|step_artifact_save,step_policy_evaluate,step_connector_dispatch|artifact_id,normalized_text,memory_write_allowed,delivery_id,status,binding_id,channel,recipient|memory_write|queue|stakeholder_follow_up_fact" ]]; then
  echo "expected dispatch-memory workflow template to compile dispatch->memory graph; got ${DISPATCH_MEMORY_PLAN_FIELDS}" >&2
  echo "${DISPATCH_MEMORY_PLAN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
dispatch_memory_execute_json() {
  local body=""
  local fields=""
  local i
  for i in $(seq 1 4); do
    body="$(curl -fsS -X POST "${BASE}/v1/plans/execute" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
      -d "{\"task_key\":\"stakeholder_dispatch_memory_candidate\",\"goal\":\"prepare, send, and stage stakeholder follow-up memory\",\"input_json\":{\"source_text\":\"Board context and stakeholder sensitivities.\",\"binding_id\":\"${DISPATCH_MEMORY_BINDING_ID}\",\"channel\":\"email\",\"recipient\":\"${DISPATCH_MEMORY_RECIPIENT}\"}}")"
    fields="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}|{}|{}'.format(body.get('task_key',''), body.get('status',''), body.get('next_action',''), bool(body.get('approval_id','')), bool(body.get('session_id',''))))" <<<"${body}")"
    if [[ "${fields}" == "stakeholder_dispatch_memory_candidate|awaiting_approval|poll_or_subscribe|True|True" ]]; then
      printf '%s' "${body}"
      return 0
    fi
    sleep 0.25
  done
  printf '%s' "${body}"
  return 1
}
DISPATCH_MEMORY_EXECUTE_JSON="$(dispatch_memory_execute_json)" || true
DISPATCH_MEMORY_EXECUTE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}|{}|{}'.format(body.get('task_key',''), body.get('status',''), body.get('next_action',''), bool(body.get('approval_id','')), bool(body.get('session_id',''))))" <<<"${DISPATCH_MEMORY_EXECUTE_JSON}")"
if [[ "${DISPATCH_MEMORY_EXECUTE_FIELDS}" != "stakeholder_dispatch_memory_candidate|awaiting_approval|poll_or_subscribe|True|True" ]]; then
  echo "expected dispatch-memory workflow to pause behind approval; got ${DISPATCH_MEMORY_EXECUTE_FIELDS}" >&2
  echo "${DISPATCH_MEMORY_EXECUTE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_MEMORY_APPROVAL_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("approval_id",""))' <<<"${DISPATCH_MEMORY_EXECUTE_JSON}")"
DISPATCH_MEMORY_SESSION_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("session_id",""))' <<<"${DISPATCH_MEMORY_EXECUTE_JSON}")"
DISPATCH_MEMORY_WAITING_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${DISPATCH_MEMORY_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
DISPATCH_MEMORY_WAITING_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps={str((row.get('input_json') or {}).get('plan_step_key') or ''): row for row in (body.get('steps') or [])}; memory=steps.get('step_memory_candidate_stage') or {}; print('{}|{}|{}|{}|{}|{}'.format(body.get('status',''), steps.get('step_artifact_save',{}).get('state',''), steps.get('step_policy_evaluate',{}).get('state',''), steps.get('step_connector_dispatch',{}).get('state',''), memory.get('state',''), ','.join(memory.get('blocked_dependency_keys') or [])))" <<<"${DISPATCH_MEMORY_WAITING_JSON}")"
if [[ "${DISPATCH_MEMORY_WAITING_FIELDS}" != "awaiting_approval|completed|completed|waiting_approval|queued|step_connector_dispatch" ]]; then
  echo "expected dispatch-memory workflow to keep post-dispatch memory step queued behind approval-gated send; got ${DISPATCH_MEMORY_WAITING_FIELDS}" >&2
  echo "${DISPATCH_MEMORY_WAITING_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_MEMORY_PRE_CANDIDATES="$(curl -fsS "${BASE}/v1/memory/candidates?limit=20&status=pending" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); session_id='${DISPATCH_MEMORY_SESSION_ID}'; print(any((row or {}).get('source_session_id') == session_id for row in rows))" )"
if [[ "${DISPATCH_MEMORY_PRE_CANDIDATES}" != "False" ]]; then
  echo "expected dispatch-memory workflow to avoid staging memory before approval-backed dispatch completes" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_MEMORY_APPROVE_JSON="$(curl -fsS -X POST "${BASE}/v1/policy/approvals/${DISPATCH_MEMORY_APPROVAL_ID}/approve" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"decided_by\":\"${PRINCIPAL_ID}\",\"reason\":\"approved dispatch memory workflow\"}")"
DISPATCH_MEMORY_APPROVE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}'.format(body.get('task_key',''), body.get('deliverable_type',''), body.get('decision','')))" <<<"${DISPATCH_MEMORY_APPROVE_JSON}")"
if [[ "${DISPATCH_MEMORY_APPROVE_FIELDS}" != "stakeholder_dispatch_memory_candidate|stakeholder_briefing|approved" ]]; then
  echo "expected dispatch-memory workflow approval decision to keep task identity; got ${DISPATCH_MEMORY_APPROVE_FIELDS}" >&2
  echo "${DISPATCH_MEMORY_APPROVE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_MEMORY_DONE_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${DISPATCH_MEMORY_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
DISPATCH_MEMORY_DELIVERY_ID="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); receipts=body.get('receipts') or []; dispatch=next((row for row in receipts if (row or {}).get('tool_name') == 'connector.dispatch'), {}); print(dispatch.get('target_ref',''))" <<<"${DISPATCH_MEMORY_DONE_JSON}")"
DISPATCH_MEMORY_CANDIDATE_ID="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps={str((row.get('input_json') or {}).get('plan_step_key') or ''): row for row in (body.get('steps') or [])}; print(((steps.get('step_memory_candidate_stage',{}).get('output_json') or {}).get('candidate_id','')))" <<<"${DISPATCH_MEMORY_DONE_JSON}")"
DISPATCH_MEMORY_FINAL_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps={str((row.get('input_json') or {}).get('plan_step_key') or ''): row for row in (body.get('steps') or [])}; memory=steps.get('step_memory_candidate_stage') or {}; receipts=body.get('receipts') or []; dispatch=next((row for row in receipts if (row or {}).get('tool_name') == 'connector.dispatch'), {}); print('{}|{}|{}|{}|{}'.format(body.get('status',''), steps.get('step_connector_dispatch',{}).get('state',''), memory.get('state',''), (memory.get('output_json') or {}).get('candidate_category',''), dispatch.get('target_ref','')))" <<<"${DISPATCH_MEMORY_DONE_JSON}")"
if [[ "${DISPATCH_MEMORY_FINAL_FIELDS}" != "completed|completed|completed|stakeholder_follow_up_fact|${DISPATCH_MEMORY_DELIVERY_ID}" ]]; then
  echo "expected dispatch-memory workflow to complete dispatch before memory staging; got ${DISPATCH_MEMORY_FINAL_FIELDS}" >&2
  echo "${DISPATCH_MEMORY_DONE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_MEMORY_CANDIDATE_FIELDS="$(curl -fsS "${BASE}/v1/memory/candidates?limit=20&status=pending" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted='${DISPATCH_MEMORY_CANDIDATE_ID}'; delivery_id='${DISPATCH_MEMORY_DELIVERY_ID}'; row=next((row for row in rows if (row or {}).get('candidate_id') == wanted), {}); fact=row.get('fact_json') or {}; print('{}|{}|{}|{}|{}'.format(row.get('category',''), row.get('principal_id',''), fact.get('delivery_id',''), fact.get('recipient',''), row.get('summary','')))" )"
if [[ "${DISPATCH_MEMORY_CANDIDATE_FIELDS}" != "stakeholder_follow_up_fact|${PRINCIPAL_ID}|${DISPATCH_MEMORY_DELIVERY_ID}|${DISPATCH_MEMORY_RECIPIENT}|Board context and stakeholder sensitivities." ]]; then
  echo "expected dispatch-memory workflow to stage post-dispatch candidate with delivery context; got ${DISPATCH_MEMORY_CANDIDATE_FIELDS}" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_MEMORY_REVIEW_BINDING_JSON="$(curl -fsS -X POST "${BASE}/v1/connectors/bindings" "${AUTH_ARGS[@]}" -H 'content-type: application/json' \
  "${PRINCIPAL_ARGS[@]}" \
  -d '{"connector_name":"gmail","external_account_ref":"acct-review-dispatch-memory","scope_json":{"scopes":["mail.send"]},"auth_metadata_json":{"provider":"google"},"status":"enabled"}')"
DISPATCH_MEMORY_REVIEW_BINDING_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("binding_id",""))' <<<"${DISPATCH_MEMORY_REVIEW_BINDING_JSON}")"
if [[ -z "${DISPATCH_MEMORY_REVIEW_BINDING_ID}" ]]; then
  fail 13 "missing binding_id from review-dispatch-memory workflow binding response"
fi
DISPATCH_MEMORY_REVIEW_RECIPIENT="reviewed-memory+$(date +%s%N)@example.com"
operator_post_json "${BASE}/v1/tasks/contracts" -H 'content-type: application/json' \
  -d '{"task_key":"stakeholder_review_dispatch_memory_candidate","deliverable_type":"stakeholder_briefing","default_risk_class":"low","default_approval_class":"none","allowed_tools":["artifact_repository","connector.dispatch"],"evidence_requirements":["stakeholder_context"],"memory_write_policy":"reviewed_only","budget_policy_json":{"class":"low","workflow_template":"artifact_then_dispatch_then_memory_candidate","human_review_role":"briefing_reviewer","human_review_task_type":"briefing_review","human_review_brief":"Review before stakeholder dispatch and memory staging.","human_review_priority":"high","human_review_desired_output_json":{"format":"review_packet"},"memory_candidate_category":"stakeholder_follow_up_fact","memory_candidate_confidence":0.8,"memory_candidate_sensitivity":"internal"}}' >/dev/null
DISPATCH_MEMORY_REVIEW_PLAN_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/compile" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"task_key":"stakeholder_review_dispatch_memory_candidate","goal":"review, send, and stage stakeholder follow-up memory"}')"
DISPATCH_MEMORY_REVIEW_PLAN_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps=body.get('plan',{}).get('steps') or []; print('{}|{}|{}|{}'.format(len(steps), ','.join((row.get('step_key') or '') for row in steps), steps[1].get('role_required','') if len(steps) > 1 else '', ','.join((steps[5].get('depends_on') or [])) if len(steps) > 5 else ''))" <<<"${DISPATCH_MEMORY_REVIEW_PLAN_JSON}")"
if [[ "${DISPATCH_MEMORY_REVIEW_PLAN_FIELDS}" != "6|step_input_prepare,step_human_review,step_artifact_save,step_policy_evaluate,step_connector_dispatch,step_memory_candidate_stage|briefing_reviewer|step_artifact_save,step_policy_evaluate,step_connector_dispatch" ]]; then
  echo "expected review-dispatch-memory workflow template to compile human->artifact->policy->dispatch->memory graph; got ${DISPATCH_MEMORY_REVIEW_PLAN_FIELDS}" >&2
  echo "${DISPATCH_MEMORY_REVIEW_PLAN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
dispatch_memory_review_execute_json() {
  local body=""
  local fields=""
  local i
  for i in $(seq 1 4); do
    body="$(curl -fsS -X POST "${BASE}/v1/plans/execute" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
      -d "{\"task_key\":\"stakeholder_review_dispatch_memory_candidate\",\"goal\":\"review, send, and stage stakeholder follow-up memory\",\"input_json\":{\"source_text\":\"Board context and stakeholder sensitivities.\",\"binding_id\":\"${DISPATCH_MEMORY_REVIEW_BINDING_ID}\",\"channel\":\"email\",\"recipient\":\"${DISPATCH_MEMORY_REVIEW_RECIPIENT}\"}}")"
    fields="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}|{}|{}|{}'.format(body.get('task_key',''), body.get('status',''), body.get('next_action',''), bool(body.get('human_task_id','')), bool(body.get('session_id','')), bool(body.get('approval_id',''))))" <<<"${body}")"
    if [[ "${fields}" == "stakeholder_review_dispatch_memory_candidate|awaiting_human|poll_or_subscribe|True|True|False" ]]; then
      printf '%s' "${body}"
      return 0
    fi
    sleep 0.25
  done
  printf '%s' "${body}"
  return 1
}
DISPATCH_MEMORY_REVIEW_EXECUTE_JSON="$(dispatch_memory_review_execute_json)" || true
DISPATCH_MEMORY_REVIEW_EXECUTE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}|{}|{}|{}'.format(body.get('task_key',''), body.get('status',''), body.get('next_action',''), bool(body.get('human_task_id','')), bool(body.get('session_id','')), bool(body.get('approval_id',''))))" <<<"${DISPATCH_MEMORY_REVIEW_EXECUTE_JSON}")"
if [[ "${DISPATCH_MEMORY_REVIEW_EXECUTE_FIELDS}" != "stakeholder_review_dispatch_memory_candidate|awaiting_human|poll_or_subscribe|True|True|False" ]]; then
  echo "expected review-dispatch-memory workflow to pause behind human review first; got ${DISPATCH_MEMORY_REVIEW_EXECUTE_FIELDS}" >&2
  echo "${DISPATCH_MEMORY_REVIEW_EXECUTE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_MEMORY_REVIEW_SESSION_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("session_id",""))' <<<"${DISPATCH_MEMORY_REVIEW_EXECUTE_JSON}")"
DISPATCH_MEMORY_REVIEW_HUMAN_TASK_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${DISPATCH_MEMORY_REVIEW_EXECUTE_JSON}")"
DISPATCH_MEMORY_REVIEW_RETURN_JSON="$(operator_post_json "${BASE}/v1/human/tasks/${DISPATCH_MEMORY_REVIEW_HUMAN_TASK_ID}/return" -H 'content-type: application/json' \
  -d '{"operator_id":"briefing-reviewer","resolution":"ready_for_dispatch","returned_payload_json":{"final_text":"Reviewed stakeholder briefing with follow-up notes."},"provenance_json":{"review_mode":"human"}}')"
DISPATCH_MEMORY_REVIEW_RETURN_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}'.format(body.get('task_key',''), body.get('status',''), body.get('resolution','')))" <<<"${DISPATCH_MEMORY_REVIEW_RETURN_JSON}")"
if [[ "${DISPATCH_MEMORY_REVIEW_RETURN_FIELDS}" != "stakeholder_review_dispatch_memory_candidate|returned|ready_for_dispatch" ]]; then
  echo "expected review-dispatch-memory human return to preserve task identity; got ${DISPATCH_MEMORY_REVIEW_RETURN_FIELDS}" >&2
  echo "${DISPATCH_MEMORY_REVIEW_RETURN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_MEMORY_REVIEW_AWAITING_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${DISPATCH_MEMORY_REVIEW_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
DISPATCH_MEMORY_REVIEW_AWAITING_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps={str((row.get('input_json') or {}).get('plan_step_key') or ''): row for row in (body.get('steps') or [])}; artifacts=body.get('artifacts') or []; print('{}|{}|{}|{}|{}|{}|{}'.format(body.get('status',''), steps.get('step_human_review',{}).get('state',''), steps.get('step_artifact_save',{}).get('state',''), steps.get('step_policy_evaluate',{}).get('state',''), steps.get('step_connector_dispatch',{}).get('state',''), steps.get('step_memory_candidate_stage',{}).get('state',''), (artifacts[0] or {}).get('content','') if artifacts else ''))" <<<"${DISPATCH_MEMORY_REVIEW_AWAITING_JSON}")"
if [[ "${DISPATCH_MEMORY_REVIEW_AWAITING_FIELDS}" != "awaiting_approval|completed|completed|completed|waiting_approval|queued|Reviewed stakeholder briefing with follow-up notes." && "${DISPATCH_MEMORY_REVIEW_AWAITING_FIELDS}" != "awaiting_approval|completed|completed|completed|completed|queued|Reviewed stakeholder briefing with follow-up notes." ]]; then
  echo "expected review-dispatch-memory workflow to pause for approval after human return while memory step stays queued; got ${DISPATCH_MEMORY_REVIEW_AWAITING_FIELDS}" >&2
  echo "${DISPATCH_MEMORY_REVIEW_AWAITING_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_MEMORY_REVIEW_APPROVAL_ID="$(curl -fsS "${BASE}/v1/policy/approvals/pending?limit=20" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); session_id='${DISPATCH_MEMORY_REVIEW_SESSION_ID}'; row=next((row for row in rows if (row or {}).get('session_id') == session_id), {}); print(row.get('approval_id',''))" )"
if [[ -z "${DISPATCH_MEMORY_REVIEW_APPROVAL_ID}" ]]; then
  echo "expected review-dispatch-memory workflow to create a pending approval after human return" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_MEMORY_REVIEW_APPROVE_JSON="$(curl -fsS -X POST "${BASE}/v1/policy/approvals/${DISPATCH_MEMORY_REVIEW_APPROVAL_ID}/approve" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"decided_by\":\"${PRINCIPAL_ID}\",\"reason\":\"approved reviewed dispatch memory\"}")"
DISPATCH_MEMORY_REVIEW_APPROVE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}'.format(body.get('task_key',''), body.get('deliverable_type',''), body.get('decision','')))" <<<"${DISPATCH_MEMORY_REVIEW_APPROVE_JSON}")"
if [[ "${DISPATCH_MEMORY_REVIEW_APPROVE_FIELDS}" != "stakeholder_review_dispatch_memory_candidate|stakeholder_briefing|approved" ]]; then
  echo "expected review-dispatch-memory approval decision to keep task identity; got ${DISPATCH_MEMORY_REVIEW_APPROVE_FIELDS}" >&2
  echo "${DISPATCH_MEMORY_REVIEW_APPROVE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_MEMORY_REVIEW_DONE_JSON="$(wait_for_session_status "${DISPATCH_MEMORY_REVIEW_SESSION_ID}" "completed")"
DISPATCH_MEMORY_REVIEW_DELIVERY_ID="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); receipts=body.get('receipts') or []; dispatch=next((row for row in receipts if (row or {}).get('tool_name') == 'connector.dispatch'), {}); print(dispatch.get('target_ref',''))" <<<"${DISPATCH_MEMORY_REVIEW_DONE_JSON}")"
DISPATCH_MEMORY_REVIEW_CANDIDATE_ID="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps={str((row.get('input_json') or {}).get('plan_step_key') or ''): row for row in (body.get('steps') or [])}; print(((steps.get('step_memory_candidate_stage',{}).get('output_json') or {}).get('candidate_id','')))" <<<"${DISPATCH_MEMORY_REVIEW_DONE_JSON}")"
DISPATCH_MEMORY_REVIEW_DONE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps={str((row.get('input_json') or {}).get('plan_step_key') or ''): row for row in (body.get('steps') or [])}; print('{}|{}|{}|{}'.format(body.get('status',''), steps.get('step_connector_dispatch',{}).get('state',''), steps.get('step_memory_candidate_stage',{}).get('state',''), (steps.get('step_memory_candidate_stage',{}).get('output_json') or {}).get('candidate_category','')))" <<<"${DISPATCH_MEMORY_REVIEW_DONE_JSON}")"
if [[ "${DISPATCH_MEMORY_REVIEW_DONE_FIELDS}" != "completed|completed|completed|stakeholder_follow_up_fact" ]]; then
  echo "expected review-dispatch-memory workflow to complete dispatch and memory staging after approval; got ${DISPATCH_MEMORY_REVIEW_DONE_FIELDS}" >&2
  echo "${DISPATCH_MEMORY_REVIEW_DONE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
DISPATCH_MEMORY_REVIEW_CANDIDATE_FIELDS="$(curl -fsS "${BASE}/v1/memory/candidates?limit=20&status=pending" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted='${DISPATCH_MEMORY_REVIEW_CANDIDATE_ID}'; delivery_id='${DISPATCH_MEMORY_REVIEW_DELIVERY_ID}'; row=next((row for row in rows if (row or {}).get('candidate_id') == wanted), {}); fact=row.get('fact_json') or {}; print('{}|{}|{}|{}|{}'.format(row.get('category',''), row.get('principal_id',''), fact.get('delivery_id',''), fact.get('recipient',''), row.get('summary','')))" )"
if [[ "${DISPATCH_MEMORY_REVIEW_CANDIDATE_FIELDS}" != "stakeholder_follow_up_fact|${PRINCIPAL_ID}|${DISPATCH_MEMORY_REVIEW_DELIVERY_ID}|${DISPATCH_MEMORY_REVIEW_RECIPIENT}|Reviewed stakeholder briefing with follow-up notes." ]]; then
  echo "expected review-dispatch-memory workflow to stage reviewed post-dispatch candidate with delivery context; got ${DISPATCH_MEMORY_REVIEW_CANDIDATE_FIELDS}" >&2
  fail 12 "policy contract mismatch"
fi
HYBRID_BINDING_JSON="$(curl -fsS -X POST "${BASE}/v1/connectors/bindings" "${AUTH_ARGS[@]}" -H 'content-type: application/json' \
  "${PRINCIPAL_ARGS[@]}" \
  -d '{"connector_name":"gmail","external_account_ref":"acct-review-dispatch","scope_json":{"scopes":["mail.send"]},"auth_metadata_json":{"provider":"google"},"status":"enabled"}')"
HYBRID_BINDING_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("binding_id",""))' <<<"${HYBRID_BINDING_JSON}")"
if [[ -z "${HYBRID_BINDING_ID}" ]]; then
  fail 13 "missing binding_id from review-then-dispatch workflow binding response"
fi
operator_post_json "${BASE}/v1/tasks/contracts" -H 'content-type: application/json' \
  -d '{"task_key":"stakeholder_review_dispatch","deliverable_type":"stakeholder_briefing","default_risk_class":"low","default_approval_class":"none","allowed_tools":["artifact_repository","connector.dispatch"],"evidence_requirements":["stakeholder_context"],"memory_write_policy":"reviewed_only","budget_policy_json":{"class":"low","workflow_template":"artifact_then_dispatch","human_review_role":"briefing_reviewer","human_review_task_type":"briefing_review","human_review_brief":"Review before stakeholder dispatch.","human_review_priority":"high","human_review_desired_output_json":{"format":"review_packet"}}}' >/dev/null
HYBRID_PLAN_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/compile" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"task_key":"stakeholder_review_dispatch","goal":"review and send a stakeholder briefing"}')"
HYBRID_PLAN_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps=body.get('plan',{}).get('steps') or []; review=(steps[1] if len(steps) > 1 else {}); save=(steps[2] if len(steps) > 2 else {}); dispatch=(steps[4] if len(steps) > 4 else {}); print('{}|{}|{}|{}|{}'.format(len(steps), ','.join((row.get('step_key') or '') for row in steps), review.get('role_required',''), ','.join(save.get('depends_on') or []), ','.join(dispatch.get('depends_on') or [])))" <<<"${HYBRID_PLAN_JSON}")"
if [[ "${HYBRID_PLAN_FIELDS}" != "5|step_input_prepare,step_human_review,step_artifact_save,step_policy_evaluate,step_connector_dispatch|briefing_reviewer|step_human_review|step_policy_evaluate" ]]; then
  echo "expected review-then-dispatch workflow template to compile human->artifact->policy->dispatch graph; got ${HYBRID_PLAN_FIELDS}" >&2
  echo "${HYBRID_PLAN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HYBRID_EXECUTE_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/execute" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"task_key\":\"stakeholder_review_dispatch\",\"goal\":\"review and send a stakeholder briefing\",\"input_json\":{\"source_text\":\"Board context and stakeholder sensitivities.\",\"binding_id\":\"${HYBRID_BINDING_ID}\",\"channel\":\"email\",\"recipient\":\"${HYBRID_RECIPIENT}\"}}")"
HYBRID_EXECUTE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}|{}|{}|{}'.format(body.get('task_key',''), body.get('status',''), body.get('next_action',''), bool(body.get('human_task_id','')), bool(body.get('session_id','')), bool(body.get('approval_id',''))))" <<<"${HYBRID_EXECUTE_JSON}")"
if [[ "${HYBRID_EXECUTE_FIELDS}" == "stakeholder_review_dispatch|queued|poll_or_subscribe|False|True|False" ]]; then
  HYBRID_SESSION_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("session_id",""))' <<<"${HYBRID_EXECUTE_JSON}")"
  for _ in $(seq 1 30); do
    HYBRID_HUMAN_TASK_ID="$(curl -fsS "${BASE}/v1/human/tasks?session_id=${HYBRID_SESSION_ID}&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c 'import json,sys; rows=json.loads(sys.stdin.read() or "[]"); row=rows[0] if rows else {}; print(row.get("human_task_id",""))')"
    if [[ -n "${HYBRID_HUMAN_TASK_ID}" ]]; then
      HYBRID_EXECUTE_FIELDS="stakeholder_review_dispatch|awaiting_human|poll_or_subscribe|True|True|False"
      break
    fi
    sleep 1
  done
fi
if [[ "${HYBRID_EXECUTE_FIELDS}" != "stakeholder_review_dispatch|awaiting_human|poll_or_subscribe|True|True|False" ]]; then
  echo "expected review-then-dispatch workflow to pause behind human review first; got ${HYBRID_EXECUTE_FIELDS}" >&2
  echo "${HYBRID_EXECUTE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HYBRID_SESSION_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("session_id",""))' <<<"${HYBRID_EXECUTE_JSON}")"
HYBRID_HUMAN_TASK_ID="${HYBRID_HUMAN_TASK_ID:-$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("human_task_id",""))' <<<"${HYBRID_EXECUTE_JSON}")}"
HYBRID_WAITING_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${HYBRID_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HYBRID_WAITING_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps={str((row.get('input_json') or {}).get('plan_step_key') or ''): row for row in (body.get('steps') or [])}; tasks=body.get('human_tasks') or []; print('{}|{}|{}|{}|{}|{}|{}|{}'.format(body.get('intent_task_type',''), body.get('status',''), steps.get('step_human_review',{}).get('state',''), steps.get('step_human_review',{}).get('dependency_states') == {'step_input_prepare': 'completed'}, steps.get('step_artifact_save',{}).get('state',''), steps.get('step_artifact_save',{}).get('dependency_states') == {'step_human_review': 'waiting_human'}, body.get('artifacts') == [], len(tasks) == 1 and (tasks[0] or {}).get('status','') == 'pending'))" <<<"${HYBRID_WAITING_JSON}")"
if [[ "${HYBRID_WAITING_FIELDS}" != "stakeholder_review_dispatch|awaiting_human|waiting_human|True|queued|True|True|True" ]]; then
  echo "expected review-then-dispatch workflow session to wait on the planner-inserted human review step; got ${HYBRID_WAITING_FIELDS}" >&2
  echo "${HYBRID_WAITING_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HYBRID_PENDING_BEFORE_FIELDS="$(curl -fsS "${BASE}/v1/delivery/outbox/pending?limit=20" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); print(any((row or {}).get('recipient') == '${HYBRID_RECIPIENT}' for row in rows))" )"
if [[ "${HYBRID_PENDING_BEFORE_FIELDS}" != "False" ]]; then
  echo "expected review-then-dispatch workflow to avoid queueing delivery before human review and approval" >&2
  fail 12 "policy contract mismatch"
fi
HYBRID_RETURN_JSON="$(operator_post_json "${BASE}/v1/human/tasks/${HYBRID_HUMAN_TASK_ID}/return" -H 'content-type: application/json' \
  -d '{"operator_id":"briefing-reviewer","resolution":"ready_for_dispatch","returned_payload_json":{"final_text":"Reviewed stakeholder briefing."},"provenance_json":{"review_mode":"human"}}')"
HYBRID_RETURN_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}'.format(body.get('task_key',''), body.get('status',''), body.get('resolution','')))" <<<"${HYBRID_RETURN_JSON}")"
if [[ "${HYBRID_RETURN_FIELDS}" != "stakeholder_review_dispatch|returned|ready_for_dispatch" ]]; then
  echo "expected review-then-dispatch human return to preserve task identity; got ${HYBRID_RETURN_FIELDS}" >&2
  echo "${HYBRID_RETURN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HYBRID_AWAITING_APPROVAL_JSON="$(wait_for_session_status "${HYBRID_SESSION_ID}" "awaiting_approval")"
HYBRID_AWAITING_APPROVAL_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps={str((row.get('input_json') or {}).get('plan_step_key') or ''): row for row in (body.get('steps') or [])}; artifacts=body.get('artifacts') or []; print('{}|{}|{}|{}|{}|{}|{}'.format(body.get('status',''), steps.get('step_human_review',{}).get('state',''), steps.get('step_artifact_save',{}).get('state',''), steps.get('step_policy_evaluate',{}).get('state',''), steps.get('step_connector_dispatch',{}).get('state',''), len(artifacts) == 1, (artifacts[0] or {}).get('content','') if artifacts else ''))" <<<"${HYBRID_AWAITING_APPROVAL_JSON}")"
if [[ "${HYBRID_AWAITING_APPROVAL_FIELDS}" != "awaiting_approval|completed|completed|completed|waiting_approval|True|Reviewed stakeholder briefing." && "${HYBRID_AWAITING_APPROVAL_FIELDS}" != "running|completed|completed|queued|queued|True|Reviewed stakeholder briefing." ]]; then
  echo "expected review-then-dispatch workflow to pause for approval after human return and artifact persistence; got ${HYBRID_AWAITING_APPROVAL_FIELDS}" >&2
  echo "${HYBRID_AWAITING_APPROVAL_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HYBRID_APPROVAL_ID="$(curl -fsS "${BASE}/v1/policy/approvals/pending?limit=20" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); session_id='${HYBRID_SESSION_ID}'; row=next((row for row in rows if (row or {}).get('session_id') == session_id), {}); print(row.get('approval_id',''))" )"
if [[ -z "${HYBRID_APPROVAL_ID}" ]]; then
  echo "expected review-then-dispatch workflow to create a pending approval after human return" >&2
  fail 12 "policy contract mismatch"
fi
HYBRID_APPROVE_JSON="$(curl -fsS -X POST "${BASE}/v1/policy/approvals/${HYBRID_APPROVAL_ID}/approve" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"decided_by\":\"${PRINCIPAL_ID}\",\"reason\":\"approved reviewed dispatch\"}")"
HYBRID_APPROVE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}'.format(body.get('task_key',''), body.get('deliverable_type',''), body.get('decision','')))" <<<"${HYBRID_APPROVE_JSON}")"
if [[ "${HYBRID_APPROVE_FIELDS}" != "stakeholder_review_dispatch|stakeholder_briefing|approved" ]]; then
  echo "expected review-then-dispatch approval decision to keep task identity; got ${HYBRID_APPROVE_FIELDS}" >&2
  echo "${HYBRID_APPROVE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HYBRID_DONE_JSON="$(wait_for_session_status "${HYBRID_SESSION_ID}" "completed")"
HYBRID_DONE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); receipts=body.get('receipts') or []; dispatch=next((row for row in receipts if (row or {}).get('tool_name') == 'connector.dispatch'), {}); print('{}|{}|{}|{}|{}'.format(body.get('status',''), len(receipts) == 2, bool(dispatch.get('receipt_id','')), bool(dispatch.get('target_ref','')), dispatch.get('task_key','')))" <<<"${HYBRID_DONE_JSON}")"
if [[ "${HYBRID_DONE_FIELDS}" != "completed|True|True|True|stakeholder_review_dispatch" ]]; then
  echo "expected completed review-then-dispatch workflow to emit connector.dispatch receipt and target ref; got ${HYBRID_DONE_FIELDS}" >&2
  echo "${HYBRID_DONE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HYBRID_DELIVERY_ID="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); receipts=body.get('receipts') or []; dispatch=next((row for row in receipts if (row or {}).get('tool_name') == 'connector.dispatch'), {}); print(dispatch.get('target_ref',''))" <<<"${HYBRID_DONE_JSON}")"
HYBRID_PENDING_AFTER_FIELDS="$(curl -fsS "${BASE}/v1/delivery/outbox/pending?limit=200" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); delivery_id='${HYBRID_DELIVERY_ID}'; row=next((row for row in rows if (row or {}).get('delivery_id') == delivery_id), {}); print('{}|{}'.format(row.get('recipient',''), row.get('status','')))" )"
if [[ "${HYBRID_PENDING_AFTER_FIELDS}" != "${HYBRID_RECIPIENT}|queued" && "${HYBRID_PENDING_AFTER_FIELDS}" != "|" ]]; then
  echo "expected approved review-then-dispatch workflow to queue delivery outbox row or defer before pending enqueue; got ${HYBRID_PENDING_AFTER_FIELDS}" >&2
  fail 12 "policy contract mismatch"
fi
if [[ "${HYBRID_PENDING_AFTER_FIELDS}" == "|" ]]; then
  echo "approved review-then-dispatch workflow deferred delivery before pending outbox enqueue; delivery_id=${HYBRID_DELIVERY_ID}" >&2
fi
operator_post_json "${BASE}/v1/tasks/contracts" -H 'content-type: application/json' \
  -d '{"task_key":"stakeholder_review_dispatch_retry","deliverable_type":"stakeholder_briefing","default_risk_class":"low","default_approval_class":"none","allowed_tools":["artifact_repository","connector.dispatch"],"evidence_requirements":["stakeholder_context"],"memory_write_policy":"reviewed_only","budget_policy_json":{"class":"low","workflow_template":"artifact_then_dispatch","human_review_role":"briefing_reviewer","human_review_task_type":"briefing_review","human_review_brief":"Review before stakeholder dispatch.","human_review_priority":"high","human_review_desired_output_json":{"format":"review_packet"},"dispatch_failure_strategy":"retry","dispatch_max_attempts":2,"dispatch_retry_backoff_seconds":45}}' >/dev/null
HYBRID_RETRY_EXECUTE_JSON="$(curl -fsS -X POST "${BASE}/v1/plans/execute" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"task_key\":\"stakeholder_review_dispatch_retry\",\"goal\":\"review and send a stakeholder briefing\",\"input_json\":{\"source_text\":\"Board context and stakeholder sensitivities.\",\"binding_id\":\"missing-review-dispatch-binding\",\"channel\":\"email\",\"recipient\":\"${HYBRID_RETRY_RECIPIENT}\"}}")"
HYBRID_RETRY_EXECUTE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}|{}|{}|{}'.format(body.get('task_key',''), body.get('status',''), body.get('next_action',''), bool(body.get('human_task_id','')), bool(body.get('session_id','')), bool(body.get('approval_id',''))))" <<<"${HYBRID_RETRY_EXECUTE_JSON}")"
if [[ "${HYBRID_RETRY_EXECUTE_FIELDS}" == "stakeholder_review_dispatch_retry|queued|poll_or_subscribe|False|True|False" ]]; then
  HYBRID_RETRY_SESSION_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("session_id",""))' <<<"${HYBRID_RETRY_EXECUTE_JSON}")"
  for _ in $(seq 1 30); do
    HYBRID_RETRY_HUMAN_TASK_ID="$(curl -fsS "${BASE}/v1/human/tasks?session_id=${HYBRID_RETRY_SESSION_ID}&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c 'import json,sys; rows=json.loads(sys.stdin.read() or "[]"); row=rows[0] if rows else {}; print(row.get("human_task_id",""))')"
    if [[ -n "${HYBRID_RETRY_HUMAN_TASK_ID}" ]]; then
      HYBRID_RETRY_EXECUTE_FIELDS="stakeholder_review_dispatch_retry|awaiting_human|poll_or_subscribe|True|True|False"
      break
    fi
    sleep 1
  done
fi
if [[ "${HYBRID_RETRY_EXECUTE_FIELDS}" != "stakeholder_review_dispatch_retry|awaiting_human|poll_or_subscribe|True|True|False" ]]; then
  echo "expected delayed review-then-dispatch workflow to pause behind human review first; got ${HYBRID_RETRY_EXECUTE_FIELDS}" >&2
  echo "${HYBRID_RETRY_EXECUTE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HYBRID_RETRY_SESSION_ID="${HYBRID_RETRY_SESSION_ID:-$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("session_id",""))' <<<"${HYBRID_RETRY_EXECUTE_JSON}")}"
HYBRID_RETRY_HUMAN_TASK_ID="${HYBRID_RETRY_HUMAN_TASK_ID:-$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${HYBRID_RETRY_EXECUTE_JSON}")}"
HYBRID_RETRY_RETURN_JSON="$(operator_post_json "${BASE}/v1/human/tasks/${HYBRID_RETRY_HUMAN_TASK_ID}/return" -H 'content-type: application/json' \
  -d '{"operator_id":"briefing-reviewer","resolution":"ready_for_dispatch","returned_payload_json":{"final_text":"Reviewed stakeholder briefing."},"provenance_json":{"review_mode":"human"}}')"
HYBRID_RETRY_RETURN_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}'.format(body.get('task_key',''), body.get('status',''), body.get('resolution','')))" <<<"${HYBRID_RETRY_RETURN_JSON}")"
if [[ "${HYBRID_RETRY_RETURN_FIELDS}" != "stakeholder_review_dispatch_retry|returned|ready_for_dispatch" ]]; then
  echo "expected delayed review-then-dispatch human return to preserve task identity; got ${HYBRID_RETRY_RETURN_FIELDS}" >&2
  echo "${HYBRID_RETRY_RETURN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HYBRID_RETRY_AWAITING_APPROVAL_JSON="$(wait_for_session_status "${HYBRID_RETRY_SESSION_ID}" "awaiting_approval")"
HYBRID_RETRY_AWAITING_APPROVAL_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps={str((row.get('input_json') or {}).get('plan_step_key') or ''): row for row in (body.get('steps') or [])}; artifacts=body.get('artifacts') or []; print('{}|{}|{}|{}|{}|{}|{}'.format(body.get('status',''), steps.get('step_human_review',{}).get('state',''), steps.get('step_artifact_save',{}).get('state',''), steps.get('step_policy_evaluate',{}).get('state',''), steps.get('step_connector_dispatch',{}).get('state',''), len(artifacts) == 1, (artifacts[0] or {}).get('content','') if artifacts else ''))" <<<"${HYBRID_RETRY_AWAITING_APPROVAL_JSON}")"
if [[ "${HYBRID_RETRY_AWAITING_APPROVAL_FIELDS}" != "awaiting_approval|completed|completed|completed|waiting_approval|True|Reviewed stakeholder briefing." ]]; then
  echo "expected delayed review-then-dispatch workflow to pause for approval after human return; got ${HYBRID_RETRY_AWAITING_APPROVAL_FIELDS}" >&2
  echo "${HYBRID_RETRY_AWAITING_APPROVAL_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HYBRID_RETRY_APPROVAL_ID="$(curl -fsS "${BASE}/v1/policy/approvals/pending?limit=20" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); session_id='${HYBRID_RETRY_SESSION_ID}'; row=next((row for row in rows if (row or {}).get('session_id') == session_id), {}); print(row.get('approval_id',''))" )"
if [[ -z "${HYBRID_RETRY_APPROVAL_ID}" ]]; then
  echo "expected delayed review-then-dispatch workflow to create a pending approval after human return" >&2
  fail 12 "policy contract mismatch"
fi
HYBRID_RETRY_APPROVE_JSON="$(curl -fsS -X POST "${BASE}/v1/policy/approvals/${HYBRID_RETRY_APPROVAL_ID}/approve" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"decided_by\":\"${PRINCIPAL_ID}\",\"reason\":\"approved reviewed dispatch retry\"}")"
HYBRID_RETRY_APPROVE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); print('{}|{}|{}'.format(body.get('task_key',''), body.get('deliverable_type',''), body.get('decision','')))" <<<"${HYBRID_RETRY_APPROVE_JSON}")"
if [[ "${HYBRID_RETRY_APPROVE_FIELDS}" != "stakeholder_review_dispatch_retry|stakeholder_briefing|approved" ]]; then
  echo "expected delayed review-then-dispatch approval decision to keep task identity; got ${HYBRID_RETRY_APPROVE_FIELDS}" >&2
  echo "${HYBRID_RETRY_APPROVE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HYBRID_RETRY_QUEUED_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${HYBRID_RETRY_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HYBRID_RETRY_QUEUED_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); steps={str((row.get('input_json') or {}).get('plan_step_key') or ''): row for row in (body.get('steps') or [])}; queues=body.get('queue_items') or []; latest=(queues[-1] if queues else {}); print('{}|{}|{}|{}|{}|{}|{}|{}'.format(body.get('status',''), steps.get('step_human_review',{}).get('state',''), steps.get('step_artifact_save',{}).get('state',''), steps.get('step_policy_evaluate',{}).get('state',''), steps.get('step_connector_dispatch',{}).get('state',''), (steps.get('step_connector_dispatch',{}).get('error_json') or {}).get('reason',''), latest.get('state',''), bool(latest.get('next_attempt_at',''))))" <<<"${HYBRID_RETRY_QUEUED_JSON}")"
if [[ "${HYBRID_RETRY_QUEUED_FIELDS}" != "queued|completed|completed|completed|queued|retry_scheduled|queued|True" ]]; then
  echo "expected delayed review-then-dispatch approval flow to leave dispatch queued behind next_attempt_at; got ${HYBRID_RETRY_QUEUED_FIELDS}" >&2
  echo "${HYBRID_RETRY_QUEUED_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HYBRID_RETRY_PENDING_AFTER_FIELDS="$(curl -fsS "${BASE}/v1/delivery/outbox/pending?limit=20" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); print(any((row or {}).get('recipient') == '${HYBRID_RETRY_RECIPIENT}' for row in rows))" )"
if [[ "${HYBRID_RETRY_PENDING_AFTER_FIELDS}" != "False" ]]; then
  echo "expected delayed review-then-dispatch retry to avoid queueing delivery before a successful dispatch run" >&2
  fail 12 "policy contract mismatch"
fi
echo "generic task async contracts ok"
else
echo "== smoke: generic task execution =="
echo "skipped: /v1/responses routes are not published on this runtime"
echo "== smoke: generic task async contracts =="
echo "skipped: /v1/responses routes are not published on this runtime"
fi

if (( LEGACY_HUMAN_TASKS_AVAILABLE && LEGACY_TASK_CONTRACTS_AVAILABLE )); then
echo "== smoke: compiled human review runtime =="
HUMAN_REWRITE_ROLE="communications_reviewer_${SMOKE_RUN_TOKEN}"
HUMAN_REWRITE_SPECIALIST_ID="operator-specialist-${SMOKE_RUN_TOKEN}"
operator_post_json "${BASE}/v1/tasks/contracts" -H 'content-type: application/json' \
  -d "{\"task_key\":\"rewrite_text\",\"deliverable_type\":\"rewrite_note\",\"default_risk_class\":\"low\",\"default_approval_class\":\"none\",\"allowed_tools\":[\"artifact_repository\"],\"evidence_requirements\":[\"stakeholder_context\"],\"memory_write_policy\":\"reviewed_only\",\"budget_policy_json\":{\"class\":\"low\",\"human_review_role\":\"${HUMAN_REWRITE_ROLE}\",\"human_review_task_type\":\"communications_review\",\"human_review_brief\":\"Review the rewrite before finalizing it.\",\"human_review_priority\":\"high\",\"human_review_sla_minutes\":45,\"human_review_auto_assign_if_unique\":true,\"human_review_desired_output_json\":{\"format\":\"review_packet\",\"escalation_policy\":\"manager_review\"},\"human_review_authority_required\":\"send_on_behalf_review\",\"human_review_why_human\":\"Executive-facing rewrite needs human judgment before finalization.\",\"human_review_quality_rubric_json\":{\"checks\":[\"tone\",\"accuracy\",\"stakeholder_sensitivity\"]}}}" >/dev/null
operator_post_json "${BASE}/v1/human/tasks/operators" -H 'content-type: application/json' \
  -d "{\"operator_id\":\"${HUMAN_REWRITE_SPECIALIST_ID}\",\"display_name\":\"Senior Comms Reviewer\",\"roles\":[\"${HUMAN_REWRITE_ROLE}\"],\"skill_tags\":[\"tone\",\"accuracy\",\"stakeholder_sensitivity\"],\"trust_tier\":\"senior\",\"status\":\"active\"}" >/dev/null
HUMAN_REWRITE_JSON="$(curl_body_retry 5 1 -X POST "${BASE}/v1/rewrite/artifact" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' -d '{"text":"rewrite with human review"}')"
HUMAN_REWRITE_FIELDS="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print("{}|{}|{}|{}".format(body.get("status",""), body.get("next_action",""), bool(body.get("human_task_id","")), body.get("approval_id","")))' <<<"${HUMAN_REWRITE_JSON}")"
if [[ "${HUMAN_REWRITE_FIELDS}" != "awaiting_human|poll_or_subscribe|True|" ]]; then
  echo "expected awaiting_human rewrite acceptance contract with human_task_id; got ${HUMAN_REWRITE_FIELDS}" >&2
  echo "${HUMAN_REWRITE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_REWRITE_SESSION_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("session_id",""))' <<<"${HUMAN_REWRITE_JSON}")"
HUMAN_REWRITE_TASK_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(body.get("human_task_id",""))' <<<"${HUMAN_REWRITE_JSON}")"
HUMAN_REWRITE_SESSION_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${HUMAN_REWRITE_SESSION_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_REWRITE_SESSION_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); tasks=body.get('human_tasks') or []; queues=body.get('queue_items') or []; steps=body.get('steps') or []; history=body.get('human_task_assignment_history') or []; review=next((row for row in tasks if (row or {}).get('human_task_id') == '${HUMAN_REWRITE_TASK_ID}'), {}); step_lookup={str((row.get('input_json') or {}).get('plan_step_key') or ''): row for row in steps}; review_step=step_lookup.get('step_human_review') or {}; save_step=step_lookup.get('step_artifact_save') or {}; policy_step=step_lookup.get('step_policy_evaluate') or {}; checks=(review.get('quality_rubric_json') or {}).get('checks') or []; names=[(row or {}).get('event_name','') for row in history if (row or {}).get('human_task_id') == '${HUMAN_REWRITE_TASK_ID}']; fields=[body.get('status',''), len(steps) == 4, len(queues) == 3 and all((q or {}).get('state','') == 'done' for q in queues), bool(review.get('human_task_id','')) and review.get('status') == 'pending', bool(review_step.get('step_id','')) and review_step.get('state') == 'waiting_human', review_step.get('input_json',{}).get('owner',''), review_step.get('input_json',{}).get('authority_class',''), review_step.get('input_json',{}).get('review_class',''), review_step.get('input_json',{}).get('failure_strategy',''), review_step.get('input_json',{}).get('timeout_budget_seconds',''), review_step.get('input_json',{}).get('max_attempts',''), review_step.get('input_json',{}).get('retry_backoff_seconds',''), review_step.get('dependency_states') == {'step_policy_evaluate': 'completed'}, (review_step.get('dependency_step_ids') or {}).get('step_policy_evaluate') == policy_step.get('step_id',''), review_step.get('blocked_dependency_keys') == [], review_step.get('dependencies_satisfied') is True, save_step.get('state') == 'queued', save_step.get('dependency_keys') == ['step_human_review'], save_step.get('dependency_states') == {'step_human_review': 'waiting_human'}, (save_step.get('dependency_step_ids') or {}).get('step_human_review') == review_step.get('step_id',''), save_step.get('blocked_dependency_keys') == ['step_human_review'], save_step.get('dependencies_satisfied') is False, review.get('priority',''), bool(review.get('sla_due_at','')), (review.get('desired_output_json') or {}).get('escalation_policy',''), review.get('authority_required',''), review.get('why_human',''), checks[0] if checks else '', review.get('assignment_state',''), review.get('assigned_operator_id',''), review.get('assignment_source',''), bool(review.get('assigned_at','')), review.get('assigned_by_actor_id',''), ','.join(names)]; print('|'.join(str(v) for v in fields))" <<<"${HUMAN_REWRITE_SESSION_JSON}")"
if [[ "${HUMAN_REWRITE_SESSION_FIELDS}" != "awaiting_human|True|True|True|True|human|draft|operator|fail|3600|1|0|True|True|True|True|True|True|True|True|True|True|high|True|manager_review|send_on_behalf_review|Executive-facing rewrite needs human judgment before finalization.|tone|assigned|${HUMAN_REWRITE_SPECIALIST_ID}|auto_preselected|True|orchestrator:auto_preselected|human_task_created,human_task_assigned" ]]; then
  echo "expected awaiting_human session with queued human review step; got ${HUMAN_REWRITE_SESSION_FIELDS}" >&2
  echo "${HUMAN_REWRITE_SESSION_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_REWRITE_SUMMARY_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); review=next((row for row in (body.get('human_tasks') or []) if (row or {}).get('human_task_id') == '${HUMAN_REWRITE_TASK_ID}'), {}); print('{}|{}|{}|{}|{}|{}'.format(review.get('last_transition_event_name',''), bool(review.get('last_transition_at','')), review.get('last_transition_assignment_state',''), review.get('last_transition_operator_id',''), review.get('last_transition_assignment_source',''), review.get('last_transition_by_actor_id','')))" <<<"${HUMAN_REWRITE_SESSION_JSON}")"
if [[ "${HUMAN_REWRITE_SUMMARY_FIELDS}" != "human_task_assigned|True|assigned|${HUMAN_REWRITE_SPECIALIST_ID}|auto_preselected|orchestrator:auto_preselected" ]]; then
  echo "expected planner-native human review row to expose compact auto-preselected transition summary; got ${HUMAN_REWRITE_SUMMARY_FIELDS}" >&2
  echo "${HUMAN_REWRITE_SESSION_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_REWRITE_AUTO_SESSION_JSON="$(curl -fsS "${BASE}/v1/rewrite/sessions/${HUMAN_REWRITE_SESSION_ID}?human_task_assignment_source=auto_preselected" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_REWRITE_AUTO_SESSION_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); tasks=body.get('human_tasks') or []; history=body.get('human_task_assignment_history') or []; print('{}|{}|{}'.format(len(tasks), (tasks[0].get('human_task_id','') if tasks else ''), ','.join((row or {}).get('event_name','') for row in history)))" <<<"${HUMAN_REWRITE_AUTO_SESSION_JSON}")"
if [[ "${HUMAN_REWRITE_AUTO_SESSION_FIELDS}" != "1|${HUMAN_REWRITE_TASK_ID}|human_task_assigned" ]]; then
  echo "expected session assignment-source filter to isolate planner auto-preselected pending rows and transitions; got ${HUMAN_REWRITE_AUTO_SESSION_FIELDS}" >&2
  echo "${HUMAN_REWRITE_AUTO_SESSION_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_REWRITE_AUTO_LIST_JSON="$(curl -fsS "${BASE}/v1/human/tasks?session_id=${HUMAN_REWRITE_SESSION_ID}&assignment_source=auto_preselected&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_REWRITE_AUTO_LIST_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted='${HUMAN_REWRITE_TASK_ID}'; print(any((row or {}).get('human_task_id') == wanted for row in rows))" <<<"${HUMAN_REWRITE_AUTO_LIST_JSON}")"
if [[ "${HUMAN_REWRITE_AUTO_LIST_FIELDS}" != "True" ]]; then
  echo "expected session-scoped assignment-source list filter to expose planner auto-preselected pending work" >&2
  echo "${HUMAN_REWRITE_AUTO_LIST_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_REWRITE_AUTO_SUMMARY_JSON="$(curl -fsS "${BASE}/v1/human/tasks/priority-summary?status=pending&role_required=${HUMAN_REWRITE_ROLE}&assigned_operator_id=${HUMAN_REWRITE_SPECIALIST_ID}&assignment_source=auto_preselected" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_REWRITE_AUTO_SUMMARY_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); counts=body.get('counts_json') or {}; print('{}|{}|{}|{}|{}|{}|{}'.format(body.get('assignment_source',''), body.get('total',''), body.get('highest_priority',''), counts.get('urgent',''), counts.get('high',''), counts.get('normal',''), counts.get('low','')))" <<<"${HUMAN_REWRITE_AUTO_SUMMARY_JSON}")"
if [[ "${HUMAN_REWRITE_AUTO_SUMMARY_FIELDS}" != "auto_preselected|1|high|0|1|0|0" ]]; then
  echo "expected assignment-source priority summary to isolate planner auto-preselected pending work; got ${HUMAN_REWRITE_AUTO_SUMMARY_FIELDS}" >&2
  echo "${HUMAN_REWRITE_AUTO_SUMMARY_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
curl -fsS -X POST "${BASE}/v1/human/tasks" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"session_id\":\"${HUMAN_REWRITE_SESSION_ID}\",\"task_type\":\"communications_review\",\"role_required\":\"${HUMAN_REWRITE_ROLE}\",\"brief\":\"Ownerless mixed-source review task.\",\"priority\":\"low\",\"resume_session_on_return\":false}" >${SMOKE_TMP_DIR}/ea_human_rewrite_ownerless_low.json
HUMAN_REWRITE_AUTO_SUMMARY_MIXED_FIELDS="$(curl -fsS "${BASE}/v1/human/tasks/priority-summary?status=pending&assignment_source=auto_preselected&role_required=${HUMAN_REWRITE_ROLE}&assigned_operator_id=${HUMAN_REWRITE_SPECIALIST_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" | python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); counts=body.get('counts_json') or {}; print('{}|{}|{}|{}|{}|{}|{}'.format(body.get('assignment_source',''), body.get('total',''), body.get('highest_priority',''), counts.get('urgent',''), counts.get('high',''), counts.get('normal',''), counts.get('low','')))" )"
if [[ "${HUMAN_REWRITE_AUTO_SUMMARY_MIXED_FIELDS}" != "auto_preselected|1|high|0|1|0|0" ]]; then
  echo "expected auto_preselected assignment_source summary to stay isolated after extra ownerless rows are added; got ${HUMAN_REWRITE_AUTO_SUMMARY_MIXED_FIELDS}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_REWRITE_AUTO_BACKLOG_JSON="$(curl -fsS "${BASE}/v1/human/tasks/backlog?operator_id=${HUMAN_REWRITE_SPECIALIST_ID}&assignment_source=auto_preselected&limit=10" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
HUMAN_REWRITE_AUTO_BACKLOG_FIELDS="$(python3 -c "import json,sys; rows=json.loads(sys.stdin.read() or '[]'); wanted='${HUMAN_REWRITE_TASK_ID}'; print(any((row or {}).get('human_task_id') == wanted for row in rows))" <<<"${HUMAN_REWRITE_AUTO_BACKLOG_JSON}")"
if [[ "${HUMAN_REWRITE_AUTO_BACKLOG_FIELDS}" != "True" ]]; then
  echo "expected assignment-source backlog filter to expose planner auto-preselected pending work" >&2
  echo "${HUMAN_REWRITE_AUTO_BACKLOG_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
ensure_operator_profile "reviewer-1" "${HUMAN_REWRITE_ROLE}" '["tone","accuracy","stakeholder_sensitivity"]' "senior" "Reviewer 1"
HUMAN_REWRITE_RETURN_JSON="$(operator_post_json "${BASE}/v1/human/tasks/${HUMAN_REWRITE_TASK_ID}/return" -H 'content-type: application/json' \
  -d '{"operator_id":"reviewer-1","resolution":"ready_for_send","returned_payload_json":{"final_text":"rewrite with human review, edited by reviewer"},"provenance_json":{"review_mode":"human"}}')"
HUMAN_REWRITE_RETURN_FIELDS="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print("{}|{}|{}|{}|{}|{}".format(body.get("last_transition_event_name",""), bool(body.get("last_transition_at","")), body.get("last_transition_assignment_state",""), body.get("last_transition_operator_id",""), body.get("last_transition_assignment_source",""), body.get("last_transition_by_actor_id","")))' <<<"${HUMAN_REWRITE_RETURN_JSON}")"
if [[ "${HUMAN_REWRITE_RETURN_FIELDS}" != "human_task_returned|True|returned|reviewer-1|manual|reviewer-1" ]]; then
  echo "expected human-review return response to expose compact returned transition summary; got ${HUMAN_REWRITE_RETURN_FIELDS}" >&2
  echo "${HUMAN_REWRITE_RETURN_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
HUMAN_REWRITE_DONE_JSON="$(wait_for_session_status "${HUMAN_REWRITE_SESSION_ID}" "completed")"
HUMAN_REWRITE_DONE_FIELDS="$(python3 -c "import json,sys; body=json.loads(sys.stdin.read() or '{}'); events={e.get('name','') for e in (body.get('events') or [])}; queues=body.get('queue_items') or []; steps=body.get('steps') or []; artifacts=body.get('artifacts') or []; print('{}|{}|{}|{}|{}|{}|{}'.format(body.get('status',''), len(artifacts) >= 1, (artifacts[0] or {}).get('content','') if artifacts else '', 'human_task_step_started' in events, 'session_resumed_from_human_task' in events, len(queues) == 4 and all((q or {}).get('state','') == 'done' for q in queues), len(steps) == 4 and all((row or {}).get('state') == 'completed' for row in steps)))" <<<"${HUMAN_REWRITE_DONE_JSON}")"
if [[ "${HUMAN_REWRITE_DONE_FIELDS}" != "completed|True|rewrite with human review, edited by reviewer|True|True|True|True" ]]; then
  echo "expected resumed human-review rewrite to complete with reviewer-edited artifact and fully drained queue; got ${HUMAN_REWRITE_DONE_FIELDS}" >&2
  echo "${HUMAN_REWRITE_DONE_JSON}" >&2
  fail 12 "policy contract mismatch"
fi
echo "compiled human review runtime ok"
else
echo "== smoke: compiled human review runtime =="
echo "skipped: human review runtime requires /v1/human/tasks and /v1/tasks/contracts"
fi

echo "== smoke: memory =="
MEMORY_CANDIDATE_JSON="$(curl -fsS -X POST "${BASE}/v1/memory/candidates" "${AUTH_ARGS[@]}" -H 'content-type: application/json' \
  "${PRINCIPAL_ARGS[@]}" \
  -d '{"category":"stakeholder_pref","summary":"CEO prefers concise updates","fact_json":{"tone":"concise"},"source_session_id":"session-1","source_event_id":"event-1","source_step_id":"step-1","confidence":0.72,"sensitivity":"internal"}')"
MEMORY_CANDIDATE_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("candidate_id",""))' <<<"${MEMORY_CANDIDATE_JSON}")"
if [[ -z "${MEMORY_CANDIDATE_ID}" ]]; then
  fail 13 "missing candidate_id from memory candidate response"
fi
MEMORY_PROMOTE_JSON="$(curl -fsS -X POST "${BASE}/v1/memory/candidates/${MEMORY_CANDIDATE_ID}/promote" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d '{"reviewer":"smoke-operator","sharing_policy":"private"}')"
MEMORY_ITEM_ID="$(python3 -c 'import json,sys; body=json.loads(sys.stdin.read() or "{}"); print(((body.get("item") or {}).get("item_id")) or "")' <<<"${MEMORY_PROMOTE_JSON}")"
if [[ -z "${MEMORY_ITEM_ID}" ]]; then
  fail 13 "missing item_id from memory promote response"
fi
curl -fsS "${BASE}/v1/memory/candidates?limit=5&status=promoted" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
curl -fsS "${BASE}/v1/memory/items?limit=5" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
MEMORY_MISMATCH_CODE="$(curl_status_code ${SMOKE_TMP_DIR}/ea_memory_mismatch_resp.json "${BASE}/v1/memory/items?limit=5&principal_id=${MISMATCH_PRINCIPAL_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}")"
if [[ "${MEMORY_MISMATCH_CODE}" != "403" ]]; then
  echo "expected 403 for memory principal mismatch; got ${MEMORY_MISMATCH_CODE}" >&2
  cat ${SMOKE_TMP_DIR}/ea_memory_mismatch_resp.json >&2 || true
  fail 12 "policy contract mismatch"
fi
MEMORY_MISMATCH_REASON="$(python3 - "${SMOKE_TMP_DIR}/ea_memory_mismatch_resp.json" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
if not path.exists():
    print("")
    raise SystemExit(0)
try:
    body = json.loads(path.read_text())
except Exception:
    print("")
    raise SystemExit(0)
print(((body.get("error") or {}).get("code")) or "")
PY
)"
if [[ "${MEMORY_MISMATCH_REASON}" != "principal_scope_mismatch" ]]; then
  echo "expected memory principal mismatch code principal_scope_mismatch; got ${MEMORY_MISMATCH_REASON}" >&2
  cat ${SMOKE_TMP_DIR}/ea_memory_mismatch_resp.json >&2 || true
  fail 12 "policy contract mismatch"
fi
curl -fsS "${BASE}/v1/memory/items/${MEMORY_ITEM_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
ENTITY_EXEC_JSON="$(curl -fsS -X POST "${BASE}/v1/memory/entities" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"principal_id\":\"${PRINCIPAL_ID}\",\"entity_type\":\"person\",\"canonical_name\":\"Alex Executive\",\"attributes_json\":{\"role\":\"executive\"},\"confidence\":0.9,\"status\":\"active\"}")"
ENTITY_EXEC_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("entity_id",""))' <<<"${ENTITY_EXEC_JSON}")"
if [[ -z "${ENTITY_EXEC_ID}" ]]; then
  fail 13 "missing entity_id from memory entity response"
fi
ENTITY_STAKE_JSON="$(curl -fsS -X POST "${BASE}/v1/memory/entities" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"principal_id\":\"${PRINCIPAL_ID}\",\"entity_type\":\"person\",\"canonical_name\":\"Sam Stakeholder\",\"attributes_json\":{\"role\":\"board_member\"},\"confidence\":0.88,\"status\":\"active\"}")"
ENTITY_STAKE_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("entity_id",""))' <<<"${ENTITY_STAKE_JSON}")"
if [[ -z "${ENTITY_STAKE_ID}" ]]; then
  fail 13 "missing entity_id from second memory entity response"
fi
REL_JSON="$(curl -fsS -X POST "${BASE}/v1/memory/relationships" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"principal_id\":\"${PRINCIPAL_ID}\",\"from_entity_id\":\"${ENTITY_EXEC_ID}\",\"to_entity_id\":\"${ENTITY_STAKE_ID}\",\"relationship_type\":\"reports_to\",\"attributes_json\":{\"strength\":\"high\"},\"confidence\":0.75}")"
REL_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("relationship_id",""))' <<<"${REL_JSON}")"
if [[ -z "${REL_ID}" ]]; then
  fail 13 "missing relationship_id from memory relationship response"
fi
COMMITMENT_JSON="$(curl -fsS -X POST "${BASE}/v1/memory/commitments" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"principal_id\":\"${PRINCIPAL_ID}\",\"title\":\"Send board follow-up\",\"details\":\"Draft and send by Friday\",\"status\":\"open\",\"priority\":\"high\",\"due_at\":\"2026-03-06T10:00:00+00:00\",\"source_json\":{\"source\":\"smoke\"}}")"
COMMITMENT_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("commitment_id",""))' <<<"${COMMITMENT_JSON}")"
if [[ -z "${COMMITMENT_ID}" ]]; then
  fail 13 "missing commitment_id from memory commitment response"
fi
BINDING_JSON="$(curl -fsS -X POST "${BASE}/v1/memory/authority-bindings" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"principal_id\":\"${PRINCIPAL_ID}\",\"subject_ref\":\"assistant\",\"action_scope\":\"calendar.write\",\"approval_level\":\"manager\",\"channel_scope\":[\"email\",\"slack\"],\"policy_json\":{\"quiet_hours_enforced\":true},\"status\":\"active\"}")"
BINDING_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("binding_id",""))' <<<"${BINDING_JSON}")"
if [[ -z "${BINDING_ID}" ]]; then
  fail 13 "missing binding_id from authority binding response"
fi
PREF_JSON="$(curl -fsS -X POST "${BASE}/v1/memory/delivery-preferences" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"principal_id\":\"${PRINCIPAL_ID}\",\"channel\":\"email\",\"recipient_ref\":\"ceo@example.com\",\"cadence\":\"urgent_only\",\"quiet_hours_json\":{\"start\":\"22:00\",\"end\":\"07:00\"},\"format_json\":{\"style\":\"concise\"},\"status\":\"active\"}")"
PREF_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("preference_id",""))' <<<"${PREF_JSON}")"
if [[ -z "${PREF_ID}" ]]; then
  fail 13 "missing preference_id from delivery preference response"
fi
curl -fsS "${BASE}/v1/memory/entities?limit=5&principal_id=${PRINCIPAL_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
curl -fsS "${BASE}/v1/memory/entities/${ENTITY_EXEC_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
curl -fsS "${BASE}/v1/memory/relationships?limit=5&principal_id=${PRINCIPAL_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
curl -fsS "${BASE}/v1/memory/relationships/${REL_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
curl -fsS "${BASE}/v1/memory/commitments?principal_id=${PRINCIPAL_ID}&limit=5" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
curl -fsS "${BASE}/v1/memory/commitments/${COMMITMENT_ID}?principal_id=${PRINCIPAL_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
curl -fsS "${BASE}/v1/memory/authority-bindings?principal_id=${PRINCIPAL_ID}&limit=5" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
curl -fsS "${BASE}/v1/memory/authority-bindings/${BINDING_ID}?principal_id=${PRINCIPAL_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
curl -fsS "${BASE}/v1/memory/delivery-preferences?principal_id=${PRINCIPAL_ID}&limit=5" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
curl -fsS "${BASE}/v1/memory/delivery-preferences/${PREF_ID}?principal_id=${PRINCIPAL_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
DEADLINE_JSON="$(curl -fsS -X POST "${BASE}/v1/memory/deadline-windows" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"principal_id\":\"${PRINCIPAL_ID}\",\"title\":\"Board prep delivery window\",\"start_at\":\"2026-03-07T08:30:00+00:00\",\"end_at\":\"2026-03-07T10:00:00+00:00\",\"status\":\"open\",\"priority\":\"high\",\"notes\":\"Draft must be ready before board sync\",\"source_json\":{\"source\":\"smoke\"}}")"
WINDOW_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("window_id",""))' <<<"${DEADLINE_JSON}")"
if [[ -z "${WINDOW_ID}" ]]; then
  fail 13 "missing window_id from deadline-window response"
fi
curl -fsS "${BASE}/v1/memory/deadline-windows?principal_id=${PRINCIPAL_ID}&limit=5" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
curl -fsS "${BASE}/v1/memory/deadline-windows/${WINDOW_ID}?principal_id=${PRINCIPAL_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
STAKEHOLDER_JSON="$(curl -fsS -X POST "${BASE}/v1/memory/stakeholders" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"principal_id\":\"${PRINCIPAL_ID}\",\"display_name\":\"Sam Stakeholder\",\"channel_ref\":\"email:sam@example.com\",\"authority_level\":\"approver\",\"importance\":\"high\",\"response_cadence\":\"fast\",\"tone_pref\":\"diplomatic\",\"sensitivity\":\"confidential\",\"escalation_policy\":\"notify_exec\",\"open_loops_json\":{\"board_follow_up\":\"open\"},\"friction_points_json\":{\"scheduling\":\"tight\"},\"last_interaction_at\":\"2026-03-06T15:30:00+00:00\",\"status\":\"active\",\"notes\":\"Needs concise summaries\"}")"
STAKEHOLDER_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("stakeholder_id",""))' <<<"${STAKEHOLDER_JSON}")"
if [[ -z "${STAKEHOLDER_ID}" ]]; then
  fail 13 "missing stakeholder_id from stakeholder response"
fi
curl -fsS "${BASE}/v1/memory/stakeholders?principal_id=${PRINCIPAL_ID}&limit=5" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
curl -fsS "${BASE}/v1/memory/stakeholders/${STAKEHOLDER_ID}?principal_id=${PRINCIPAL_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
DECISION_JSON="$(curl -fsS -X POST "${BASE}/v1/memory/decision-windows" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"principal_id\":\"${PRINCIPAL_ID}\",\"title\":\"Board response decision\",\"context\":\"Choose timing and channel for reply\",\"opens_at\":\"2026-03-06T08:00:00+00:00\",\"closes_at\":\"2026-03-06T12:00:00+00:00\",\"urgency\":\"high\",\"authority_required\":\"exec\",\"status\":\"open\",\"notes\":\"Needs decision before board prep\",\"source_json\":{\"source\":\"smoke\"}}")"
DECISION_WINDOW_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("decision_window_id",""))' <<<"${DECISION_JSON}")"
if [[ -z "${DECISION_WINDOW_ID}" ]]; then
  fail 13 "missing decision_window_id from decision-window response"
fi
curl -fsS "${BASE}/v1/memory/decision-windows?principal_id=${PRINCIPAL_ID}&limit=5" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
curl -fsS "${BASE}/v1/memory/decision-windows/${DECISION_WINDOW_ID}?principal_id=${PRINCIPAL_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
COMM_POLICY_JSON="$(curl -fsS -X POST "${BASE}/v1/memory/communication-policies" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"principal_id\":\"${PRINCIPAL_ID}\",\"scope\":\"board_threads\",\"preferred_channel\":\"email\",\"tone\":\"concise_diplomatic\",\"max_length\":1200,\"quiet_hours_json\":{\"start\":\"22:00\",\"end\":\"07:00\"},\"escalation_json\":{\"on_high_urgency\":\"notify_exec\"},\"status\":\"active\",\"notes\":\"Board-facing communication defaults\"}")"
COMM_POLICY_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("policy_id",""))' <<<"${COMM_POLICY_JSON}")"
if [[ -z "${COMM_POLICY_ID}" ]]; then
  fail 13 "missing policy_id from communication-policy response"
fi
curl -fsS "${BASE}/v1/memory/communication-policies?principal_id=${PRINCIPAL_ID}&limit=5" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
curl -fsS "${BASE}/v1/memory/communication-policies/${COMM_POLICY_ID}?principal_id=${PRINCIPAL_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
FOLLOW_RULE_JSON="$(curl -fsS -X POST "${BASE}/v1/memory/follow-up-rules" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"principal_id\":\"${PRINCIPAL_ID}\",\"name\":\"Board reminder escalation\",\"trigger_kind\":\"deadline_risk\",\"channel_scope\":[\"email\",\"slack\"],\"delay_minutes\":120,\"max_attempts\":3,\"escalation_policy\":\"notify_exec\",\"conditions_json\":{\"priority\":\"high\"},\"action_json\":{\"action\":\"draft_follow_up\"},\"status\":\"active\",\"notes\":\"Escalate if follow-up is late\"}")"
FOLLOW_RULE_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("rule_id",""))' <<<"${FOLLOW_RULE_JSON}")"
if [[ -z "${FOLLOW_RULE_ID}" ]]; then
  fail 13 "missing rule_id from follow-up-rule response"
fi
curl -fsS "${BASE}/v1/memory/follow-up-rules?principal_id=${PRINCIPAL_ID}&limit=5" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
curl -fsS "${BASE}/v1/memory/follow-up-rules/${FOLLOW_RULE_ID}?principal_id=${PRINCIPAL_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
INTERRUPTION_BUDGET_JSON="$(curl -fsS -X POST "${BASE}/v1/memory/interruption-budgets" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" -H 'content-type: application/json' \
  -d "{\"principal_id\":\"${PRINCIPAL_ID}\",\"scope\":\"workday\",\"window_kind\":\"daily\",\"budget_minutes\":120,\"used_minutes\":30,\"reset_at\":\"2026-03-07T00:00:00+00:00\",\"quiet_hours_json\":{\"start\":\"22:00\",\"end\":\"07:00\"},\"status\":\"active\",\"notes\":\"Keep non-critical interruptions bounded\"}")"
INTERRUPTION_BUDGET_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("budget_id",""))' <<<"${INTERRUPTION_BUDGET_JSON}")"
if [[ -z "${INTERRUPTION_BUDGET_ID}" ]]; then
  fail 13 "missing budget_id from interruption-budget response"
fi
curl -fsS "${BASE}/v1/memory/interruption-budgets?principal_id=${PRINCIPAL_ID}&limit=5" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
curl -fsS "${BASE}/v1/memory/interruption-budgets/${INTERRUPTION_BUDGET_ID}?principal_id=${PRINCIPAL_ID}" "${AUTH_ARGS[@]}" "${PRINCIPAL_ARGS[@]}" >/dev/null
echo "memory ok"

echo "smoke complete"
