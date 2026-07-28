# EA scene-video operator runbook

This runbook covers the shared `ea.scene_video_generate` skill used by Runsite and PropertyQuarry.

## Provider names and boundaries

Public provider choices:

- `mootion`: Mootion scene/movie lane.
- `magicfit`: MagicFit image-to-video lane.
- `omagic`: Public name for the OMagic scene-video lane.
- `magic`: Alias for `omagic`.
- `onemin_i2v`: 1min.AI image-to-video fallback/probe lane.

Provider boundary rules:

- `omagic` and `magic` must use an OMagic-specific model-upload adapter and must produce model-input consumption proof.
- `onemin_i2v`, `onemin`, and `1min` are 1min.AI. They must never satisfy an OMagic claim.
- `onemin` and `1min` are not public scene-video aliases for OMagic.
- Do not overwrite `ONEMIN_*` credentials while configuring OMagic scene-video.
- OMagic readiness is blocked until OMagic credentials and `render_omagic_property_model_walkthrough.py` exist.

## Runtime readiness

Use the deployed API container for runtime truth:

```bash
docker exec -i propertyquarry-api python - <<'PY'
import json
from app.services.scene_video_contract import scene_video_provider_runtime_readiness

for provider in ("mootion", "magicfit", "magic", "omagic", "onemin_i2v"):
    value = scene_video_provider_runtime_readiness(provider)
    print(provider, json.dumps({
        "status": value.get("status"),
        "ready": value.get("ready"),
        "provider_key": value.get("provider_key"),
        "provider_backend_key": value.get("provider_backend_key"),
        "blockers": value.get("blockers"),
        "credit_state": value.get("credit_state"),
        "runtime_account_count": value.get("runtime_account_count"),
        "execution_lane": value.get("execution_lane"),
    }, sort_keys=True))
PY
```

Expected interpretations:

- `magic` / `omagic` ready with `provider_backend_key=omagic` means the OMagic backend is dispatchable.
- `onemin_i2v` ready with `provider_backend_key=onemin_i2v` means only the 1min fallback lane is dispatchable.
- `magicfit_insufficient_credits` means MagicFit should not be rendered until credits or additional funded accounts are proven.
- `mootion_docker_socket_missing` / `mootion_docker_cli_missing` blocks the local Mootion worker lane only.
- `execution_lane=browseract_remote` means Mootion can use an explicit BrowserAct workflow/run lane without API-local Docker.

## Current PropertyQuarry provider-refresh packet

Latest refreshed receipts:

- `_completion/scene_video_readiness/release-gate.json`: `2026-07-06T19:21:49Z`
- `_completion/scene_video_readiness/release-gate-verifier.json`: `pass` at `2026-07-06T19:21:50Z`
- `_completion/scene_video_readiness/provider-refresh-packet.json`: `2026-07-06T19:21:50Z`
- `_completion/scene_video_readiness/provider-refresh-packet-verifier.json`: `pass` at `2026-07-06T19:21:50Z`
- `_completion/property_gold_status/latest.json`: `blocked` at `2026-07-06T19:21:51+00:00` only on `scene_video_provider_runtime`

Current runtime truth:

- `mootion`: ready through the remote BrowserAct lane.
- `onemin_i2v`: ready as the separate 1min fallback lane.
- `magicfit`: expected `3` accounts, runtime sees `1`, visible gap `2`, blocked by `magicfit_insufficient_credits`.
- `magic` / `omagic`: expected `8` accounts, runtime sees `0`, visible gap `8`, blocked by missing OMagic credentials, missing render endpoint or command, and disabled model-upload adapter.

Regenerate the current release-gate receipts from the deployed runtime:

```bash
docker exec propertyquarry-api python /app/scripts/property_scene_video_readiness_report.py \
  --output /data/artifacts/property-scene-video-readiness-current-container.json
docker exec propertyquarry-api python /app/scripts/verify_property_scene_video_readiness.py \
  --receipt /data/artifacts/property-scene-video-readiness-current-container.json \
  --output /data/artifacts/property-scene-video-readiness-verifier-current-container.json
docker exec propertyquarry-api python /app/scripts/materialize_scene_video_provider_refresh_packet.py \
  --receipt /data/artifacts/property-scene-video-readiness-current-container.json \
  --output /data/artifacts/property-scene-video-provider-refresh-packet-current-container.json
docker exec propertyquarry-api python /app/scripts/verify_scene_video_provider_refresh_packet.py \
  --packet /data/artifacts/property-scene-video-provider-refresh-packet-current-container.json \
  --output /data/artifacts/property-scene-video-provider-refresh-packet-verifier-current-container.json
docker cp propertyquarry-api:/data/artifacts/property-scene-video-readiness-current-container.json \
  _completion/scene_video_readiness/release-gate.json
docker cp propertyquarry-api:/data/artifacts/property-scene-video-readiness-verifier-current-container.json \
  _completion/scene_video_readiness/release-gate-verifier.json
docker cp propertyquarry-api:/data/artifacts/property-scene-video-provider-refresh-packet-current-container.json \
  _completion/scene_video_readiness/provider-refresh-packet.json
docker cp propertyquarry-api:/data/artifacts/property-scene-video-provider-refresh-packet-verifier-current-container.json \
  _completion/scene_video_readiness/provider-refresh-packet-verifier.json
PYTHONPATH=ea python3 scripts/propertyquarry_gold_status.py \
  --write _completion/property_gold_status/latest.json
```

## Telegram receipt proof

Use `delivery_probe_video_url` to prove principal binding and Telegram media delivery without spending provider credits.

```bash
docker exec propertyquarry-api ffmpeg -y \
  -f lavfi -i color=c=0x172033:s=640x360:d=1.5 \
  -f lavfi -i sine=frequency=880:duration=1.5 \
  -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest \
  /tmp/ea-scene-video-telegram-probe.mp4 >/dev/null 2>&1

docker exec -i propertyquarry-api python - <<'PY'
import json
from app.main import app
from app.domain.models import ToolInvocationRequest
import app.main as main

container = getattr(getattr(app, "state", object()), "container", None) or getattr(main, "container", None)
result = container.tool_execution.execute_invocation(
    ToolInvocationRequest(
        session_id="scene-video-telegram-probe",
        step_id="telegram-probe",
        tool_name="ea.scene_video_generate",
        action_kind="video.generate",
        payload_json={
            "provider_key": "magic",
            "context_kind": "scene_briefing",
            "title": "EA scene-video Telegram delivery probe",
            "delivery_probe_video_url": "/tmp/ea-scene-video-telegram-probe.mp4",
            "telegram_delivery_requested": True,
        },
        context_json={"principal_id": "<operator-principal-id>"},
    )
)
out = dict(result.output_json or {})
receipt = dict(result.receipt_json or {})
delivery = dict(out.get("telegram_delivery_json") or receipt.get("telegram_delivery_json") or {})
print(json.dumps({
    "provider_key": out.get("provider_key"),
    "provider_backend_key": out.get("provider_backend_key"),
    "render_status": out.get("render_status"),
    "telegram_status": delivery.get("status"),
    "telegram_kind": delivery.get("kind"),
    "telegram_message_count": len(delivery.get("message_ids") or []),
}, sort_keys=True))
PY
```

Pass criteria:

- `telegram_status=sent`
- `telegram_kind=video`
- `telegram_message_count` is at least `1`

## OMagic real render proof

The delivery probe proves Telegram. Completion still needs one real provider render-to-Telegram proof when the upstream provider is stable.

Use a short OMagic render through `provider_key=magic` and require:

- `provider_key=omagic`
- `provider_backend_key=omagic`
- `model_input_consumed=true`
- `render_status=completed`
- `telegram_delivery_json.status=sent`
- `telegram_delivery_json.media_ref` is a plain URL or file path, not a JSON blob

If 1min returns HTTP `524`, do not burn through accounts. Gateway timeout retry across 1min accounts is fail-closed by default. Only opt in deliberately with:

```bash
PROPERTYQUARRY_ONEMIN_VIDEO_RETRY_GATEWAY_TIMEOUTS=1
```

## MagicFit unblock

Current blocker shape:

- `magicfit_insufficient_credits`
- credit source: render failure marker
- expected `3` accounts and one runtime account detected

Do not clear the marker just to make readiness green.

Valid unblock options:

- Add credits to the currently configured MagicFit account, then run a real MagicFit render probe.
- Configure the full MagicFit account pool through the secure account JSON merge path, then verify `runtime_account_count >= 3`.

Required account JSON shape:

```json
[
  {"email": "<magicfit-account-email>", "password": "<magicfit-account-password>"},
  {"email": "<magicfit-account-email-2>", "password": "<magicfit-account-password-2>"}
]
```

The account JSON file must be provider-only, must not include 1min credentials, and must have mode `0o600` before merge:

```bash
chmod 600 <magicfit-accounts.json>
python3 scripts/merge_scene_video_provider_accounts_env.py \
  --env-file .env \
  --magicfit-accounts-json-file <magicfit-accounts.json> \
  --expected-magicfit-count 3 \
  --magicfit-account-index <funded-account-index> \
  --write
```

This writes only:

- `PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON`
- `PROPERTYQUARRY_MAGICFIT_ACCOUNT_INDEX`

If you want the runtime to read a stable secret file instead of inline JSON env, use file-env mode:

```bash
chmod 600 <magicfit-accounts.json>
python3 scripts/merge_scene_video_provider_accounts_env.py \
  --env-file .env \
  --magicfit-accounts-json-file <magicfit-accounts.json> \
  --expected-magicfit-count 3 \
  --magicfit-account-index <funded-account-index> \
  --write-file-env \
  --write
```

Default file-env install target:

- host file path: `state/incoming_property_tours/_operator-import-lane/scene_video_provider_accounts/magicfit-accounts.json`
- runtime env value: `/data/incoming_property_tours/_operator-import-lane/scene_video_provider_accounts/magicfit-accounts.json`

This writes only:

- `PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON_FILE`
- `PROPERTYQUARRY_MAGICFIT_ACCOUNT_INDEX`

When `*_ACCOUNTS_JSON_FILE` is present, PropertyQuarry runtime readiness and MagicFit helper selection prefer that file over inline `*_ACCOUNTS_JSON`.

MagicFit pass criteria:

- `provider_backend_key=magicfit`
- `render_status=completed`
- a playable hosted walkthrough video is returned, for example `hosted_walkthrough_video_url`
- `magicfit_insufficient_credits` is cleared only after that proof render succeeds

Provider render success is not delivery acceptance. `import_magicfit_walkthrough.py`
copies the playable artifact but writes `tour.magicfit.json` with
`acceptance_status=pending` and `launch_eligible=false`. A separate delivery
review must replace that state with the closed
`propertyquarry.magicfit_delivery_acceptance.v1` profile. It requires canonical
MagicFit/provider state, the nonempty source-receipt hash, the active canonical
`video_relpath` and exact `video_sha256`, and a nested
`propertyquarry.magicfit_delivery_review.v1` record. That record binds the tour
slug and all three artifact identities to a UTC review time, reviewer-authority
digest, evidence digest, and literal passing checks for end-to-end playback,
walkthrough continuity, visible rotation jumps, intended property/scope, and
sensitive or trial branding. Missing, extra, stale, unbound, mistyped, future,
pre-import, or hash-mismatched fields remain ineligible; status aliases and
truthy strings are not approval. Here, stale means an artifact binding that no
longer matches the active manifest or video bytes, not age alone. The local
profile deliberately has no expiry and cannot establish freshness or detect
replay. Allowlisted remote playback remains a separate live-probed control path.

This local JSON contract prevents accidental promotion; it is not reviewer or
provider authority. A launch authority must still verify canonical signed bytes,
a root-pinned reviewer key and role (including revocation and scope), the
content-addressed provider/evidence artifacts, trusted time, and replay state.
The digest fields do not prove those facts by themselves, and an unverified
`signature` field must never be accepted.

Readiness with multiple accounts may become `constrained` rather than blocked if only one account is known depleted.

## OMagic / Magic unblock

`magic` is a public alias for `omagic`; both must resolve to `provider_backend_key=omagic`.

Current blocker shape:

- expected `8` OMagic accounts and zero runtime accounts detected
- `omagic_credentials_missing`
- `omagic_model_upload_endpoint_missing`
- `omagic_model_upload_adapter_disabled`

Required account JSON shape:

```json
[
  {"email": "<omagic-account-email>", "password": "<omagic-account-password>"},
  {"email": "<omagic-account-email-2>", "password": "<omagic-account-password-2>"}
]
```

Merge the OMagic account pool without disabling the `magic` alias:

```bash
chmod 600 <omagic-accounts.json>
python3 scripts/merge_scene_video_provider_accounts_env.py \
  --env-file .env \
  --omagic-accounts-json-file <omagic-accounts.json> \
  --expected-omagic-count 8 \
  --write
```

This writes both OMagic and Magic alias account envs:

- `PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON`
- `PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON`

File-env mode installs one stable provider-only account file and points both OMagic and Magic alias envs at it:

```bash
chmod 600 <omagic-accounts.json>
python3 scripts/merge_scene_video_provider_accounts_env.py \
  --env-file .env \
  --omagic-accounts-json-file <omagic-accounts.json> \
  --expected-omagic-count 8 \
  --write-file-env \
  --write
```

Default file-env install target:

- host file path: `state/incoming_property_tours/_operator-import-lane/scene_video_provider_accounts/omagic-accounts.json`
- runtime env value: `/data/incoming_property_tours/_operator-import-lane/scene_video_provider_accounts/omagic-accounts.json`

This writes:

- `PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON_FILE`
- `PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON_FILE`

When `*_ACCOUNTS_JSON_FILE` is present, PropertyQuarry runtime readiness prefers that file over inline `*_ACCOUNTS_JSON`.

Configure one real model-upload adapter target before enabling the adapter:

```bash
PROPERTYQUARRY_OMAGIC_RENDER_ENDPOINT=<provider-render-endpoint>
# or
PROPERTYQUARRY_OMAGIC_RENDER_COMMAND=<provider-render-command>
```

Only set `PROPERTYQUARRY_OMAGIC_MODEL_UPLOAD_ENABLED=1` after a successful model-upload proof render.

OMagic pass criteria:

- real model input is supplied through `--model-path` or `--model-url`
- adapter state reports `model_input_consumed=true`
- adapter state reports `provider_backend_key=omagic`
- hosted tour bundle contains the returned walkthrough video
- regenerated readiness shows `magic` and `omagic` both dispatchable through OMagic

## Mootion unblock

Mootion has two lanes.

Local worker lane:

- Requires `docker` CLI in the API runtime.
- Requires Docker socket or valid `DOCKER_HOST`.
- Current API compose intentionally does not mount `/var/run/docker.sock`.

Remote BrowserAct lane:

- Requires explicit `workflow_id`, `run_url`, or a BrowserAct binding containing Mootion workflow/run metadata.
- Bootstrap/update the bridge with `python3 scripts/bootstrap_mootion_browseract_bridge.py --submit-architect --write-env`.
- The bootstrap writes only the Mootion bridge packet/binding and `CHUMMER6_RUNSITE_VIDEO_BINDING_ID`; it refuses `ONEMIN_*` writes.
- If a principal has an enabled BrowserAct binding scoped to Mootion with workflow/run metadata, `ea.scene_video_generate` auto-selects that bridge without a caller-provided `binding_id`.
- Scene-video passes `force_browseract`, `allow_browseract_remote_fallback`, and `remote_fallback_allowed` when remote Mootion is requested.
- Readiness should report `execution_lane=browseract_remote` while preserving local worker blockers under `mootion_local_worker_blockers`.

Do not mount Docker into the public API container casually. Prefer a dedicated host-tools/sidecar worker if local Docker execution is required.

## UI picker verification

Backend contracts expose:

- PropertyQuarry provider choice: `mootion`, `magicfit`, `omagic`, `magic`
- Runsite scene-video allowed providers: `mootion`, `magicfit`, `omagic`

Manual UI smoke still needed:

- PropertyQuarry walkthrough provider selector shows the expected choices.
- Runsite scene-video request flow accepts the same provider choices.
- `magic` displays or normalizes as OMagic where appropriate.

## Teable / 1min backup boundary

1min.AI account backup is separate from OMagic public routing.

Rules:

- Do not overwrite `ONEMIN_*` values when editing OMagic scene-video.
- Treat Teable backup proof as separate recovery evidence, not runtime readiness.
- If Teable API returns `403`, report the local backup paths and the remote proof gap rather than mutating credentials.

Known local backup locations:

- `/docker/EA/config/onemin_api_keys.local.json`
- `/docker/property/config/onemin_api_keys.local.json`
- `/docker/EA/config/onemin_slot_owners.local.json`

Do not print API keys or passwords in receipts, logs, or user-visible summaries.
