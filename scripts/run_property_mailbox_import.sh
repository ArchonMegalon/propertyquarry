#!/usr/bin/env bash
set -euo pipefail

EA_BASE_URL="${EA_BASE_URL:-http://127.0.0.1:8090}"
EA_API_TOKEN="${EA_API_TOKEN:-}"
PERSON_ID="${PERSON_ID:-}"
PRINCIPAL_ID="${PRINCIPAL_ID:-$PERSON_ID}"
ACCOUNT_EMAIL="${ACCOUNT_EMAIL:-}"
CONSENT_NOTE="${CONSENT_NOTE:-Explicitly approved import of housing-related Gmail threads.}"
EMAIL_LIMIT="${EMAIL_LIMIT:-80}"
LOOKBACK_DAYS="${LOOKBACK_DAYS:-540}"
FALLBACK_EMAIL_LIMIT="$EMAIL_LIMIT"
if (( FALLBACK_EMAIL_LIMIT > 50 )); then
  FALLBACK_EMAIL_LIMIT=50
fi

usage() {
  cat <<'EOF'
Usage:
  ACCOUNT_EMAIL=user@example.com \
  PERSON_ID=self \
  EA_API_TOKEN=... \
  /docker/property/scripts/run_property_mailbox_import.sh

Optional env vars:
  EA_BASE_URL     Default: http://127.0.0.1:8090
  PERSON_ID       Required target person id, for example self or household_member
  PRINCIPAL_ID    Default: PERSON_ID
  CONSENT_NOTE    Default: Explicitly approved import of housing-related Gmail threads.
  EMAIL_LIMIT     Default: 80
  LOOKBACK_DAYS   Default: 540

This script only triggers the existing EA mailbox-import endpoint.
It does not handle Gmail credentials itself.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ -z "$PERSON_ID" ]]; then
  echo "PERSON_ID is required." >&2
  usage >&2
  exit 1
fi

if [[ -z "$ACCOUNT_EMAIL" ]]; then
  echo "ACCOUNT_EMAIL is required." >&2
  usage >&2
  exit 1
fi

TMP_JSON="$(mktemp)"
TMP_BODY="$(mktemp)"
cleanup() {
  rm -f "$TMP_JSON"
  rm -f "$TMP_BODY"
}
trap cleanup EXIT

python3 - "$TMP_JSON" "$ACCOUNT_EMAIL" "$CONSENT_NOTE" "$EMAIL_LIMIT" "$LOOKBACK_DAYS" <<'PY'
import json
import sys

payload = {
    "account_email": sys.argv[2],
    "consent_confirmed": True,
    "consent_note": sys.argv[3],
    "email_limit": int(sys.argv[4]),
    "lookback_days": int(sys.argv[5]),
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
PY

URL="${EA_BASE_URL%/}/app/api/people/${PERSON_ID}/preference-profile/mailbox-import"
FALLBACK_QUERY="$(
  python3 - "$ACCOUNT_EMAIL" "$FALLBACK_EMAIL_LIMIT" <<'PY'
import sys
import urllib.parse

print(urllib.parse.urlencode({"account_email": sys.argv[1], "email_limit": int(sys.argv[2])}))
PY
)"
FALLBACK_URL="${EA_BASE_URL%/}/app/api/signals/google/property-sync?${FALLBACK_QUERY}"
AUTH_ARGS=()
if [[ -n "$EA_API_TOKEN" ]]; then
  AUTH_ARGS+=(-H "x-api-token: $EA_API_TOKEN")
fi
AUTH_ARGS+=(-H "x-ea-principal-id: $PRINCIPAL_ID")

HTTP_CODE="$(
  curl -sS -o "$TMP_BODY" -w '%{http_code}' -X POST "$URL" \
    -H 'Content-Type: application/json' \
    "${AUTH_ARGS[@]}" \
    --data-binary "@$TMP_JSON"
)"

if [[ "$HTTP_CODE" =~ ^2 ]]; then
  cat "$TMP_BODY"
  echo
  exit 0
fi

if [[ "$HTTP_CODE" == "404" ]]; then
  HTTP_CODE="$(
    curl -sS -o "$TMP_BODY" -w '%{http_code}' -X POST "$FALLBACK_URL" \
      "${AUTH_ARGS[@]}"
  )"
  if [[ "$HTTP_CODE" =~ ^2 ]]; then
    echo "Primary mailbox-import endpoint unavailable; fallback google property sync succeeded." >&2
    cat "$TMP_BODY"
    echo
    exit 0
  fi
fi

echo "Mailbox import failed with HTTP ${HTTP_CODE}." >&2
cat "$TMP_BODY" >&2
echo >&2

if [[ "$HTTP_CODE" == "404" ]]; then
  echo "The running API does not expose /app/api/people/${PERSON_ID}/preference-profile/mailbox-import." >&2
  echo "Check whether the stack expects /app/api/signals/google/property-sync instead, or whether the API container is out of sync with this script." >&2
elif [[ "$HTTP_CODE" == "401" ]]; then
  echo "Authentication failed. Ensure EA_API_TOKEN matches the live ea-api container and that PRINCIPAL_ID is valid for x-ea-principal-id." >&2
elif [[ "$HTTP_CODE" == "409" ]]; then
  echo "The API rejected the import request at the application level. The response body above should explain the missing dependency, such as a disconnected Google OAuth account." >&2
fi

exit 1
