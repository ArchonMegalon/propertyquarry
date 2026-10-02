#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
if str(EA_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_ROOT))

from app.repositories.property_evidence_overlays_postgres import (  # noqa: E402
    PostgresPropertyEvidenceOverlayRepository,
)


EXPORT_SCHEMA = "propertyquarry.evidence_overlay_teable_export.v1"
RECEIPT_SCHEMA = "propertyquarry.evidence_overlay_read_model_receipt.v3"
ROLLBACK_TOKEN_SCHEMA = "propertyquarry.evidence_overlay_activation_rollback.v1"
ACTIVATION_AUTHORITY_SCHEMA = "propertyquarry.launch_authority_envelope.v1"
REGISTRY_PATH = ROOT / "docs" / "PROPERTYQUARRY_EVIDENCE_OVERLAY_REGISTRY.json"
REQUIRED_LAYER_KEYS = {
    "environmental_quality",
    "fiber_broadband",
    "media_attention",
    "official_safety_context",
    "public_mobility",
    "school_context",
    "summer_heat",
    "traffic_noise",
}
EXPECTED_SOURCE_CADENCE_BY_LAYER = {
    "environmental_quality": "live",
    "fiber_broadband": "reference_dataset",
    "media_attention": "current_feed",
    "official_safety_context": "annual_context",
    "public_mobility": "live_or_reference",
    "school_context": "reference_dataset",
    "summer_heat": "reference_dataset",
    "traffic_noise": "reference_dataset",
}
EXPECTED_SOURCE_TEMPORALITIES_BY_LAYER = {
    "environmental_quality": {"live"},
    "fiber_broadband": {"reference"},
    "media_attention": {"current_feed"},
    "official_safety_context": {"reference"},
    "public_mobility": {"live", "reference"},
    "school_context": {"reference"},
    "summer_heat": {"reference"},
    "traffic_noise": {"reference"},
}
SOURCE_TEMPORALITIES_WITH_MAX_AGE = {"current_feed", "live"}
ALLOWED_MEDIA_SOURCE_CLASSES = {
    "independent_press",
    "licensed_news_index",
    "municipal_rss",
    "public_broadcaster",
    "publisher_feed",
}
ALLOWED_SAFETY_GEOGRAPHIC_SCOPES = {
    "citywide",
    "district_aggregate",
    "neighborhood_aggregate",
}
MAX_ACCEPTED_QUERY_BUDGET_MS = 100.0
MAX_TEABLE_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_TEABLE_RECORDS_PER_TABLE = 10_000
MAX_PRIVATE_JSON_BYTES = 16 * 1024 * 1024
MAX_ACTIVATION_AUTHORITY_AGE_SECONDS = 15 * 60
ALLOWED_MATCH_KEYS = {
    "candidate_ref",
    "district",
    "district_polygon",
    "neighborhood",
    "postal_code",
    "property_coordinate",
    "school_catchment",
    "street",
}
ALLOWED_PAYLOAD_FIELDS = {
    "article_url",
    "cache_updated_at",
    "fixed_or_mobile",
    "geographic_scope",
    "headline",
    "independent_press",
    "media_source_class",
    "person_scoring",
    "property_scoring",
    "reference_period",
    "rights_caveat",
    "source_name",
    "source_checked_at",
    "source_temporality",
    "source_updated_at",
    "source_url",
    "speed_band",
    "summary",
    "technology",
    "time_window",
    "topic_label",
    "uncertainty_label",
    "ui_state",
    "value_label",
}

_REFERENCE_PERIOD_PART = r"[0-9]{4}(?:-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12][0-9]|3[01]))?)?"
REFERENCE_PERIOD_PATTERN = re.compile(
    rf"^(?:{_REFERENCE_PERIOD_PART})(?:/(?:{_REFERENCE_PERIOD_PART}))?$"
)


def _text(value: object) -> str:
    return str(value or "").strip()


def _integer(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _object(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _string_set(value: object) -> set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {_text(item) for item in value if _text(item)}


def _positive_count_map(
    value: object,
    *,
    allow_empty: bool = False,
) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    if not value:
        return {} if allow_empty else None
    normalized: dict[str, int] = {}
    for key, count in value.items():
        normalized_key = _text(key)
        if (
            not normalized_key
            or normalized_key in normalized
            or type(count) is not int
            or count < 1
        ):
            return None
        normalized[normalized_key] = count
    return normalized


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: object) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _reference_period_point(value: str) -> datetime | None:
    try:
        if len(value) == 4:
            return datetime(int(value), 1, 1, tzinfo=timezone.utc)
        if len(value) == 7:
            year, month = value.split("-", 1)
            return datetime(int(year), int(month), 1, tzinfo=timezone.utc)
        if len(value) == 10:
            parsed = datetime.fromisoformat(value)
            return parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return None


def _valid_reference_period(value: object) -> bool:
    text = _text(value)
    if not REFERENCE_PERIOD_PATTERN.fullmatch(text):
        return False
    start_text, separator, end_text = text.partition("/")
    start = _reference_period_point(start_text)
    end = _reference_period_point(end_text) if separator else start
    return start is not None and end is not None and start <= end


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: object) -> str:
    return _sha256_bytes(_text(value).encode("utf-8"))


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("private receipt JSON contains duplicate keys")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    raise ValueError(f"private receipt JSON contains non-finite number: {value}")


def _contains_raw_base_id_key(value: object) -> bool:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                normalized = str(key).replace("-", "_").casefold()
                if normalized in {"base_id", "baseid"}:
                    return True
                pending.append(child)
        elif isinstance(current, list):
            pending.extend(current)
    return False


def _safe_http_url(value: object) -> bool:
    text = _text(value)
    if not text:
        return True
    parsed = urllib.parse.urlparse(text)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and not parsed.username
        and not parsed.password
    )


def _safe_https_base_url(value: object) -> str:
    parsed = urllib.parse.urlparse(_text(value))
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        return ""
    if parsed.query or parsed.fragment:
        return ""
    return urllib.parse.urlunparse(
        ("https", parsed.netloc.lower(), parsed.path.rstrip("/"), "", "", "")
    )


def _safe_https_origin(value: object) -> str:
    normalized = _safe_https_base_url(value)
    parsed = urllib.parse.urlparse(normalized)
    if not normalized or parsed.path not in {"", "/"}:
        return ""
    return urllib.parse.urlunparse(("https", parsed.netloc, "", "", "", ""))


def _https_base_origin(value: object) -> str:
    normalized = _safe_https_base_url(value)
    if not normalized:
        return ""
    parsed = urllib.parse.urlparse(normalized)
    return urllib.parse.urlunparse(("https", parsed.netloc, "", "", "", ""))


def _positive_finite(
    value: object, *, name: str, maximum: float | None = None
) -> float:
    try:
        normalized = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive finite number") from exc
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    if maximum is not None and normalized > maximum:
        raise ValueError(f"{name} must not exceed {maximum:g}")
    return normalized


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _teable_get_json(
    *,
    base_url: str,
    api_key: str,
    path: str,
    timeout_seconds: float,
) -> tuple[object, dict[str, object]]:
    normalized_base = _safe_https_base_url(base_url)
    if not normalized_base or not _text(api_key) or not path.startswith("/api/"):
        raise ValueError("authenticated Teable request configuration is invalid")
    url = f"{normalized_base}{path}"
    if not url.startswith(f"{normalized_base}/api/"):
        raise ValueError(
            "authenticated Teable request path escaped its configured origin"
        )
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "PropertyQuarry-Evidence-Overlay-Ingest/1",
        },
    )
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=max(float(timeout_seconds), 1.0)) as response:
            status_code = int(getattr(response, "status", 0) or 0)
            content_type = _text(response.headers.get("Content-Type")).casefold()
            body = response.read(MAX_TEABLE_RESPONSE_BYTES + 1)
            final_url = _text(response.geturl())
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Teable API returned HTTP {int(exc.code or 0)}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ValueError("Teable API request failed") from exc
    if (
        status_code != 200
        or _safe_https_base_url(final_url.rsplit("/api/", 1)[0]) != normalized_base
    ):
        raise ValueError("Teable API response origin or status is invalid")
    if content_type and "json" not in content_type:
        raise ValueError("Teable API response is not JSON")
    if len(body) > MAX_TEABLE_RESPONSE_BYTES:
        raise ValueError("Teable API response exceeds the protected size limit")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Teable API response contains invalid JSON") from exc
    return payload, {
        "status_code": status_code,
        "response_sha256": _sha256_bytes(body),
        "size_bytes": len(body),
    }


def _teable_rows(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [dict(row) for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("records", "tables", "data", "items"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [dict(row) for row in rows if isinstance(row, dict)]
    return []


def fetch_teable_export(
    *,
    base_url: str,
    api_key: str,
    base_id: str,
    registry: dict[str, object],
    timeout_seconds: float = 30.0,
    now: datetime | None = None,
) -> dict[str, object]:
    observed_at = now or _utc_now()
    normalized_base = _safe_https_base_url(base_url)
    normalized_base_id = _text(base_id)
    if not normalized_base or not _text(api_key) or not normalized_base_id:
        raise ValueError("Teable base URL, API key, and base ID are required")
    layers = _registry_layers(registry)
    expected_tables = {_text(layer.get("teable_table")) for layer in layers}
    table_payload, discovery_evidence = _teable_get_json(
        base_url=normalized_base,
        api_key=api_key,
        path=f"/api/base/{urllib.parse.quote(normalized_base_id, safe='')}/table",
        timeout_seconds=timeout_seconds,
    )
    table_ids = {
        _text(row.get("name") or row.get("tableName")): _text(
            row.get("id") or row.get("tableId")
        )
        for row in _teable_rows(table_payload)
        if _text(row.get("name") or row.get("tableName"))
        and _text(row.get("id") or row.get("tableId"))
    }
    if not expected_tables.issubset(table_ids):
        missing = sorted(expected_tables - set(table_ids))
        raise ValueError(
            f"Teable base is missing evidence overlay tables: {', '.join(missing)}"
        )

    tables: dict[str, list[dict[str, object]]] = {}
    table_evidence: dict[str, dict[str, object]] = {}
    take = 1000
    for table_name in sorted(expected_tables):
        table_id = table_ids[table_name]
        rows: list[dict[str, object]] = []
        page_evidence: list[dict[str, object]] = []
        skip = 0
        while True:
            query = urllib.parse.urlencode(
                {
                    "fieldKeyType": "name",
                    "cellFormat": "json",
                    "take": take,
                    "skip": skip,
                }
            )
            payload, evidence = _teable_get_json(
                base_url=normalized_base,
                api_key=api_key,
                path=f"/api/table/{urllib.parse.quote(table_id, safe='')}/record?{query}",
                timeout_seconds=timeout_seconds,
            )
            page = _teable_rows(payload)
            rows.extend(page)
            page_evidence.append(evidence)
            if len(rows) > MAX_TEABLE_RECORDS_PER_TABLE:
                raise ValueError(
                    f"Teable table {table_name} exceeds the protected record limit"
                )
            if len(page) < take:
                break
            skip += take
        tables[table_name] = rows
        table_evidence[table_name] = {
            "table_id_sha256": _sha256_text(table_id),
            "record_count": len(rows),
            "page_count": len(page_evidence),
            "pages": page_evidence,
        }
    return {
        "schema": EXPORT_SCHEMA,
        "generated_at": _iso(observed_at),
        "tables": tables,
        "source_evidence": {
            "mode": "authenticated_teable_api",
            "auth_kind": "bearer_api_key",
            "secret_in_export": False,
            "base_origin": urllib.parse.urlparse(normalized_base).scheme
            + "://"
            + urllib.parse.urlparse(normalized_base).netloc,
            "base_id_sha256": _sha256_text(normalized_base_id),
            "redirects_followed": False,
            "table_discovery": discovery_evidence,
            "tables": table_evidence,
        },
    }


def _load_object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _stable_private_json_object(
    path: Path,
    *,
    name: str,
) -> tuple[dict[str, object], str]:
    source = path.expanduser()
    try:
        before = os.stat(source, follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"{name} is not readable") from exc
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{name} must be a regular file")
    if stat.S_IMODE(before.st_mode) != stat.S_IRUSR | stat.S_IWUSR:
        raise ValueError(f"{name} must have mode 0600")
    if before.st_size < 1 or before.st_size > MAX_PRIVATE_JSON_BYTES:
        raise ValueError(f"{name} size is invalid")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise ValueError(f"{name} cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        identity_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (
            not stat.S_ISREG(opened.st_mode)
            or any(
                getattr(opened, field) != getattr(before, field)
                for field in identity_fields
            )
        ):
            raise ValueError(f"{name} identity changed before read")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(1_048_576, MAX_PRIVATE_JSON_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_PRIVATE_JSON_BYTES:
                raise ValueError(f"{name} size is invalid")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if total != after.st_size or any(
        getattr(after, field) != getattr(opened, field) for field in identity_fields
    ):
        raise ValueError(f"{name} changed during read")
    try:
        path_after = os.stat(source, follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"{name} changed during read") from exc
    if not stat.S_ISREG(path_after.st_mode) or any(
        getattr(path_after, field) != getattr(after, field) for field in identity_fields
    ):
        raise ValueError(f"{name} changed during read")
    raw = b"".join(chunks)
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} must contain strict UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{name} must contain a JSON object")
    return payload, _sha256_bytes(raw)


def _registry_layers(registry: dict[str, object]) -> list[dict[str, object]]:
    if registry.get("contract_name") != "propertyquarry.evidence_overlay_registry.v2":
        raise ValueError("property evidence overlay registry contract is invalid")
    gold_policy = _object(registry.get("gold_policy"))
    if "cache_updated_at" not in _text(gold_policy.get("cache_time_policy")) or (
        "source freshness"
        not in _text(gold_policy.get("cache_time_policy")).casefold()
    ):
        raise ValueError(
            "property evidence overlay registry must distinguish cache time from source freshness"
        )
    if (
        "no property or person scoring"
        not in _text(gold_policy.get("privacy_policy")).casefold()
    ):
        raise ValueError(
            "property evidence overlay registry must forbid property and person scoring"
        )
    if (
        "municipal rss"
        not in _text(gold_policy.get("media_policy")).casefold()
        or "independent press"
        not in _text(gold_policy.get("media_policy")).casefold()
    ):
        raise ValueError(
            "property evidence overlay registry must classify municipal RSS separately from independent press"
        )
    layers = [
        dict(row) for row in list(registry.get("layers") or []) if isinstance(row, dict)
    ]
    if len(layers) != 8:
        raise ValueError(
            "property evidence overlay registry must contain exactly eight layers"
        )
    layer_keys = {_text(row.get("layer_key")) for row in layers}
    table_names = {_text(row.get("teable_table")) for row in layers}
    if layer_keys != REQUIRED_LAYER_KEYS:
        raise ValueError(
            "property evidence overlay registry layer set is not the approved eight-layer contract"
        )
    if len(table_names) != 8 or "" in table_names:
        raise ValueError(
            "property evidence overlay registry requires eight unique Teable tables"
        )
    for layer in layers:
        layer_key = _text(layer.get("layer_key"))
        if layer.get("ingestion_mode") != "async_teable_job":
            raise ValueError(
                "property evidence overlay registry ingestion mode must be async_teable_job"
            )
        if layer.get("read_model") != "cached_postgres_geo_rollup":
            raise ValueError(
                "property evidence overlay registry read model must be cached_postgres_geo_rollup"
            )
        if layer.get("search_policy") != "read_cached_rollup_only_no_inline_fetch":
            raise ValueError(
                "property evidence overlay registry must forbid request-time source fetches"
            )
        if _string_set(layer.get("ui_states")) != {
            "unavailable",
            "stale",
            "verified",
        }:
            raise ValueError(
                f"property evidence overlay registry layer {layer_key} has invalid UI states"
            )
        cadence_class = _text(layer.get("source_cadence_class"))
        expected_cadence = EXPECTED_SOURCE_CADENCE_BY_LAYER[layer_key]
        if cadence_class != expected_cadence:
            raise ValueError(
                f"property evidence overlay registry layer {layer_key} must use cadence class {expected_cadence}"
            )
        temporalities = _string_set(layer.get("allowed_source_temporalities"))
        expected_temporalities = EXPECTED_SOURCE_TEMPORALITIES_BY_LAYER[layer_key]
        if temporalities != expected_temporalities:
            raise ValueError(
                f"property evidence overlay registry layer {layer_key} has invalid source temporalities"
            )
        raw_source_age_policy = _object(
            layer.get("source_max_age_hours_by_temporality")
        )
        source_age_policy = {
            _text(key): value for key, value in raw_source_age_policy.items()
        }
        age_limited_temporalities = (
            temporalities & SOURCE_TEMPORALITIES_WITH_MAX_AGE
        )
        if set(source_age_policy) != age_limited_temporalities:
            raise ValueError(
                f"property evidence overlay registry layer {layer_key} must declare source max age only for live/current-feed temporalities"
            )
        for temporality, value in source_age_policy.items():
            _positive_finite(
                value,
                name=f"{layer_key}.{temporality}.source_max_age_hours",
                maximum=168.0,
            )
        source_sla_timestamp_fields = {
            _text(key): _text(value)
            for key, value in _object(
                layer.get("source_sla_timestamp_field_by_temporality")
            ).items()
        }
        expected_sla_timestamp_fields = {
            temporality: (
                "source_checked_at"
                if temporality == "current_feed"
                else "source_updated_at"
            )
            for temporality in age_limited_temporalities
        }
        if source_sla_timestamp_fields != expected_sla_timestamp_fields:
            raise ValueError(
                f"property evidence overlay registry layer {layer_key} has invalid source SLA timestamp fields"
            )
        expected_reference_period_modes = (
            {"reference"} if "reference" in temporalities else set()
        )
        if (
            _string_set(layer.get("reference_period_required_for"))
            != expected_reference_period_modes
        ):
            raise ValueError(
                f"property evidence overlay registry layer {layer_key} has invalid reference-period policy"
            )
        required_provenance = {
            "cache_updated_at",
            "source_name",
            "source_temporality",
            "source_updated_at",
            "source_url",
            "uncertainty_label",
        }
        if "reference" in temporalities:
            required_provenance.add("reference_period")
        if "current_feed" in temporalities:
            required_provenance.add("source_checked_at")
        provenance_fields = _string_set(layer.get("provenance_fields"))
        if not required_provenance.issubset(provenance_fields):
            raise ValueError(
                f"property evidence overlay registry layer {layer_key} lacks temporal provenance fields"
            )
        if layer_key == "official_safety_context":
            if (
                _string_set(layer.get("allowed_geographic_scopes"))
                != ALLOWED_SAFETY_GEOGRAPHIC_SCOPES
                or layer.get("rights_caveat_required") is not True
                or layer.get("property_scoring") is not False
                or layer.get("person_scoring") is not False
                or not {"geographic_scope", "rights_caveat"}.issubset(
                    provenance_fields
                )
            ):
                raise ValueError(
                    "official safety overlay registry policy must remain aggregate, rights-caveated, and score-free"
                )
        if layer_key == "media_attention":
            if (
                _string_set(layer.get("allowed_media_source_classes"))
                != ALLOWED_MEDIA_SOURCE_CLASSES
                or layer.get("municipal_rss_independent_press") is not False
                or layer.get("article_links_required") is not True
                or not {"media_source_class", "independent_press"}.issubset(
                    provenance_fields
                )
            ):
                raise ValueError(
                    "media overlay registry policy must distinguish municipal RSS from independent press"
                )
    return layers


def _record_fields(value: dict[str, object]) -> tuple[str, dict[str, object]]:
    record_id = _text(value.get("id") or value.get("record_id"))
    fields = value.get("fields")
    if fields is None:
        fields = {
            key: item for key, item in value.items() if key not in {"id", "record_id"}
        }
    if not record_id or not isinstance(fields, dict):
        raise ValueError("each Teable row requires a stable id and object fields")
    return record_id, dict(fields)


def _match_object(fields: dict[str, object]) -> dict[str, str]:
    raw = fields.get("match")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Teable match field must be valid JSON") from exc
    match = (
        dict(raw or {})
        if isinstance(raw, dict)
        else {
            key: fields.get(key) for key in ALLOWED_MATCH_KEYS if _text(fields.get(key))
        }
    )
    normalized = {
        _text(key): _text(value).casefold()
        for key, value in match.items()
        if _text(key) in ALLOWED_MATCH_KEYS and _text(value)
    }
    if not normalized:
        raise ValueError("Teable overlay row requires at least one approved match key")
    unexpected = sorted({_text(key) for key in match} - ALLOWED_MATCH_KEYS)
    if unexpected:
        raise ValueError(
            f"Teable overlay row contains forbidden match keys: {', '.join(unexpected)}"
        )
    return normalized


def _teable_source_evidence_failures(
    source_evidence: object,
    *,
    expected_tables: set[str],
    actual_table_counts: dict[str, int],
    expected_origin: str,
    expected_base_id_sha256: str,
) -> list[str]:
    failures: list[str] = []
    source = _object(source_evidence)
    if source.get("mode") != "authenticated_teable_api":
        failures.append("Teable export requires authenticated API source evidence")
    if (
        source.get("auth_kind") != "bearer_api_key"
        or source.get("secret_in_export") is not False
    ):
        failures.append("Teable export authentication evidence is invalid")
    if not _safe_https_base_url(source.get("base_origin")):
        failures.append("Teable export base origin must be HTTPS")
    if not re.fullmatch(r"[0-9a-f]{64}", _text(source.get("base_id_sha256"))):
        failures.append("Teable export base identity digest is invalid")
    if source.get("redirects_followed") is not False:
        failures.append("Teable export must prove redirects were not followed")
    normalized_expected_origin = _safe_https_origin(expected_origin)
    if not normalized_expected_origin:
        failures.append("independently configured expected Teable origin must be HTTPS")
    elif _safe_https_origin(source.get("base_origin")) != normalized_expected_origin:
        failures.append(
            "Teable export origin does not match independent launch authority"
        )
    normalized_expected_base_digest = _text(expected_base_id_sha256).casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized_expected_base_digest):
        failures.append(
            "independently configured expected Teable base digest is invalid"
        )
    elif (
        _text(source.get("base_id_sha256")).casefold()
        != normalized_expected_base_digest
    ):
        failures.append(
            "Teable export base identity does not match independent launch authority"
        )
    discovery = _object(source.get("table_discovery"))
    if (
        _integer(discovery.get("status_code")) != 200
        or not re.fullmatch(r"[0-9a-f]{64}", _text(discovery.get("response_sha256")))
        or (_integer(discovery.get("size_bytes")) or 0) < 2
    ):
        failures.append("Teable table-discovery response evidence is invalid")
    table_evidence = _object(source.get("tables"))
    if set(table_evidence) != expected_tables:
        failures.append("Teable source evidence table set mismatch")
    for table_name in sorted(expected_tables):
        row = _object(table_evidence.get(table_name))
        if not re.fullmatch(r"[0-9a-f]{64}", _text(row.get("table_id_sha256"))):
            failures.append(f"Teable table {table_name} identity digest is invalid")
        if _integer(row.get("record_count")) != actual_table_counts.get(table_name):
            failures.append(f"Teable table {table_name} source record count mismatch")
        raw_pages = row.get("pages")
        pages = raw_pages if isinstance(raw_pages, list) else []
        if _integer(row.get("page_count")) != len(pages) or not pages:
            failures.append(f"Teable table {table_name} page evidence is incomplete")
        for page in pages:
            evidence = _object(page)
            if (
                _integer(evidence.get("status_code")) != 200
                or not re.fullmatch(
                    r"[0-9a-f]{64}", _text(evidence.get("response_sha256"))
                )
                or (_integer(evidence.get("size_bytes")) or 0) < 2
            ):
                failures.append(f"Teable table {table_name} page evidence is invalid")
                break
    forbidden_secret_keys: set[str] = set()
    stack: list[object] = [source]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                normalized_key = _text(key).casefold()
                if normalized_key in {
                    "api_key",
                    "authorization",
                    "bearer_token",
                    "token",
                }:
                    forbidden_secret_keys.add(normalized_key)
                stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    if forbidden_secret_keys:
        failures.append("Teable source evidence contains forbidden secret fields")
    return failures


def build_ingestion_plan(
    *,
    export: dict[str, object],
    registry: dict[str, object],
    candidate_sha: str,
    max_age_hours: float,
    expected_teable_origin: str,
    expected_teable_base_id_sha256: str,
    launch_mode: bool = True,
    now: datetime | None = None,
) -> dict[str, object]:
    observed_at = now or _utc_now()
    failures: list[str] = []
    try:
        normalized_max_age_hours = _positive_finite(
            max_age_hours,
            name="max_age_hours",
        )
    except ValueError as exc:
        failures.append(str(exc))
        normalized_max_age_hours = 0.0
    if export.get("schema") != EXPORT_SCHEMA:
        failures.append(f"export schema must be {EXPORT_SCHEMA}")
    if not re.fullmatch(r"[0-9a-f]{40}", _text(candidate_sha)):
        failures.append("candidate_sha must be a full lowercase 40-character Git SHA")
    generated_at = _parse_datetime(export.get("generated_at"))
    if generated_at is None:
        failures.append("export generated_at must be an ISO-8601 timestamp")
        generated_at = observed_at
    elif generated_at > observed_at:
        failures.append("export generated_at cannot be in the future")
    raw_tables = export.get("tables")
    tables = dict(raw_tables or {}) if isinstance(raw_tables, dict) else {}
    layers = _registry_layers(registry)
    layer_by_table = {
        _text(layer.get("teable_table")): layer for layer in layers
    }
    expected_tables = {
        table_name: _text(layer.get("layer_key"))
        for table_name, layer in layer_by_table.items()
    }
    missing_tables = sorted(set(expected_tables) - set(tables))
    extra_tables = sorted(set(tables) - set(expected_tables))
    if missing_tables:
        failures.append(
            f"Teable export missing required tables: {', '.join(missing_tables)}"
        )
    if extra_tables:
        failures.append(
            f"Teable export contains unregistered tables: {', '.join(extra_tables)}"
        )

    normalized_records: list[dict[str, object]] = []
    table_counts: dict[str, int] = {}
    oldest_cache_by_layer: dict[str, str] = {}
    latest_cache_by_layer: dict[str, str] = {}
    source_temporal_evidence_by_layer: dict[str, dict[str, object]] = {}
    for layer in layers:
        layer_key = _text(layer.get("layer_key"))
        source_temporal_evidence_by_layer[layer_key] = {
            "cadence_class": _text(layer.get("source_cadence_class")),
            "allowed_temporalities": sorted(
                _string_set(layer.get("allowed_source_temporalities"))
            ),
            "source_max_age_hours_by_temporality": {
                _text(key): float(value)  # registry validation already fail-closes
                for key, value in _object(
                    layer.get("source_max_age_hours_by_temporality")
                ).items()
            },
            "source_sla_timestamp_field_by_temporality": {
                _text(key): _text(value)
                for key, value in _object(
                    layer.get("source_sla_timestamp_field_by_temporality")
                ).items()
            },
            "row_counts_by_temporality": {},
            "source_updated_at_row_counts_by_temporality": {},
            "oldest_source_updated_at_by_temporality": {},
            "latest_source_updated_at_by_temporality": {},
            "source_sla_at_row_counts_by_temporality": {},
            "oldest_source_sla_at_by_temporality": {},
            "latest_source_sla_at_by_temporality": {},
            "reference_periods": [],
            "reference_period_row_counts": {},
        }
    for table_name, layer_key in sorted(expected_tables.items()):
        layer = layer_by_table[table_name]
        allowed_temporalities = _string_set(
            layer.get("allowed_source_temporalities")
        )
        source_age_policy = {
            _text(key): float(value)
            for key, value in _object(
                layer.get("source_max_age_hours_by_temporality")
            ).items()
        }
        source_sla_timestamp_fields = {
            _text(key): _text(value)
            for key, value in _object(
                layer.get("source_sla_timestamp_field_by_temporality")
            ).items()
        }
        raw_rows = tables.get(table_name)
        rows = list(raw_rows or []) if isinstance(raw_rows, list) else []
        table_counts[table_name] = len(rows)
        if not rows:
            failures.append(f"Teable table {table_name} has no launch-proof row")
            continue
        for raw_row in rows:
            if not isinstance(raw_row, dict):
                failures.append(f"Teable table {table_name} contains a non-object row")
                continue
            try:
                record_id, fields = _record_fields(dict(raw_row))
                match = _match_object(fields)
            except ValueError as exc:
                failures.append(f"{table_name}: {exc}")
                continue
            payload = {
                key: fields.get(key) for key in ALLOWED_PAYLOAD_FIELDS if key in fields
            }
            payload["match"] = match
            payload["layer_key"] = layer_key
            source_temporality = _text(
                payload.get("source_temporality")
            ).casefold()
            if source_temporality not in allowed_temporalities:
                failures.append(
                    f"{table_name}/{record_id}: source_temporality must be one of "
                    f"{', '.join(sorted(allowed_temporalities))}"
                )
            source_updated_at = _parse_datetime(payload.get("source_updated_at"))
            source_checked_at = _parse_datetime(payload.get("source_checked_at"))
            cache_updated_at = _parse_datetime(payload.get("cache_updated_at"))
            if source_updated_at is None or cache_updated_at is None:
                failures.append(
                    f"{table_name}/{record_id}: source_updated_at and cache_updated_at are required"
                )
                continue
            if cache_updated_at > observed_at or source_updated_at > observed_at:
                failures.append(
                    f"{table_name}/{record_id}: evidence timestamps cannot be in the future"
                )
            if cache_updated_at < source_updated_at:
                failures.append(
                    f"{table_name}/{record_id}: cache_updated_at cannot predate source_updated_at"
                )
            if source_temporality == "current_feed":
                if source_checked_at is None:
                    failures.append(
                        f"{table_name}/{record_id}: current-feed rows require source_checked_at"
                    )
                elif (
                    source_checked_at > observed_at
                    or source_checked_at > cache_updated_at
                    or source_checked_at < source_updated_at
                ):
                    failures.append(
                        f"{table_name}/{record_id}: source_checked_at must be between source_updated_at and cache_updated_at"
                    )
            elif "source_checked_at" in payload:
                failures.append(
                    f"{table_name}/{record_id}: source_checked_at is only valid for current-feed rows"
                )
            cache_age_hours = max(
                (observed_at - cache_updated_at).total_seconds() / 3600.0, 0.0
            )
            if cache_age_hours > normalized_max_age_hours:
                failures.append(
                    f"{table_name}/{record_id}: cached row is older than "
                    f"{normalized_max_age_hours:g} hours"
                )
            source_max_age_hours = source_age_policy.get(source_temporality)
            source_sla_timestamp_field = source_sla_timestamp_fields.get(
                source_temporality, ""
            )
            source_sla_at = (
                source_checked_at
                if source_sla_timestamp_field == "source_checked_at"
                else (
                    source_updated_at
                    if source_sla_timestamp_field == "source_updated_at"
                    else None
                )
            )
            if source_max_age_hours is not None:
                if source_sla_at is not None and max(
                    (observed_at - source_sla_at).total_seconds() / 3600.0,
                    0.0,
                ) > source_max_age_hours:
                    failures.append(
                        f"{table_name}/{record_id}: {source_temporality} {source_sla_timestamp_field} is older than its "
                        f"{source_max_age_hours:g}-hour source-check SLA"
                    )
            reference_period = _text(payload.get("reference_period"))
            if source_temporality == "reference":
                if not _valid_reference_period(reference_period):
                    failures.append(
                        f"{table_name}/{record_id}: reference rows require an explicit ISO-8601 reference_period"
                    )
            elif reference_period:
                failures.append(
                    f"{table_name}/{record_id}: reference_period is only valid for reference rows"
                )
            if not _text(
                payload.get("summary")
                or payload.get("value_label")
                or payload.get("headline")
            ):
                failures.append(
                    f"{table_name}/{record_id}: customer summary is required"
                )
            if (
                not _text(payload.get("source_name"))
                or not _text(payload.get("source_url"))
                or not _safe_http_url(payload.get("source_url"))
            ):
                failures.append(
                    f"{table_name}/{record_id}: safe source_name/source_url provenance is required"
                )
            if not _text(payload.get("uncertainty_label")):
                failures.append(
                    f"{table_name}/{record_id}: uncertainty_label is required"
                )
            for score_field in ("property_scoring", "person_scoring"):
                if score_field in payload and payload.get(score_field) is not False:
                    failures.append(
                        f"{table_name}/{record_id}: {score_field} must never be enabled"
                    )
                if layer_key != "official_safety_context" and score_field in payload:
                    failures.append(
                        f"{table_name}/{record_id}: {score_field} is only valid as an explicit safety-layer denial"
                    )
            media_only_fields = {
                "article_url",
                "independent_press",
                "media_source_class",
            }
            if layer_key != "media_attention" and any(
                field in payload for field in media_only_fields
            ):
                failures.append(
                    f"{table_name}/{record_id}: media classification fields are only valid for media_attention"
                )
            safety_only_fields = {"geographic_scope", "rights_caveat"}
            if layer_key != "official_safety_context" and any(
                field in payload for field in safety_only_fields
            ):
                failures.append(
                    f"{table_name}/{record_id}: safety claim fields are only valid for official_safety_context"
                )
            ui_state = _text(payload.get("ui_state")).casefold()
            if ui_state not in {"stale", "verified"}:
                failures.append(
                    f"{table_name}/{record_id}: ui_state is required and must be stale or verified"
                )
            if ui_state == "stale" and source_max_age_hours is not None:
                failures.append(
                    f"{table_name}/{record_id}: stale {source_temporality} rows cannot enter a launch snapshot"
                )
            if layer_key == "media_attention" and not _safe_http_url(
                payload.get("article_url")
            ):
                failures.append(
                    f"{table_name}/{record_id}: media article_url must be a safe HTTP URL"
                )
            if layer_key == "media_attention" and not _text(payload.get("article_url")):
                failures.append(
                    f"{table_name}/{record_id}: media article_url is required"
                )
            if layer_key == "media_attention":
                media_source_class = _text(
                    payload.get("media_source_class")
                ).casefold()
                independent_press = payload.get("independent_press")
                if media_source_class not in ALLOWED_MEDIA_SOURCE_CLASSES:
                    failures.append(
                        f"{table_name}/{record_id}: media_source_class is required and invalid"
                    )
                if not isinstance(independent_press, bool):
                    failures.append(
                        f"{table_name}/{record_id}: independent_press must be an explicit boolean"
                    )
                elif media_source_class == "municipal_rss" and independent_press:
                    failures.append(
                        f"{table_name}/{record_id}: municipal RSS cannot be classified as independent press"
                    )
                elif media_source_class == "independent_press" and not independent_press:
                    failures.append(
                        f"{table_name}/{record_id}: independent_press classification must be explicit"
                    )
                media_claim_text = " ".join(
                    _text(payload.get(key))
                    for key in (
                        "headline",
                        "source_name",
                        "summary",
                        "topic_label",
                        "uncertainty_label",
                        "value_label",
                    )
                ).casefold()
                if media_source_class == "municipal_rss" and re.search(
                    r"\bindependent\s+(?:journalism|media|news|press|reporting)\b",
                    media_claim_text,
                ):
                    failures.append(
                        f"{table_name}/{record_id}: municipal RSS copy cannot claim independent reporting"
                    )
            if layer_key == "official_safety_context":
                geographic_scope = _text(
                    payload.get("geographic_scope")
                ).casefold()
                if geographic_scope not in ALLOWED_SAFETY_GEOGRAPHIC_SCOPES:
                    failures.append(
                        f"{table_name}/{record_id}: safety context must use an approved aggregate geographic_scope"
                    )
                if not _text(payload.get("rights_caveat")):
                    failures.append(
                        f"{table_name}/{record_id}: safety context requires a source rights_caveat"
                    )
                if payload.get("property_scoring") is not False:
                    failures.append(
                        f"{table_name}/{record_id}: safety context must explicitly disable property_scoring"
                    )
                if payload.get("person_scoring") is not False:
                    failures.append(
                        f"{table_name}/{record_id}: safety context must explicitly disable person_scoring"
                    )
            payload["source_temporality"] = source_temporality
            payload["source_updated_at"] = _iso(source_updated_at)
            if source_checked_at is not None:
                payload["source_checked_at"] = _iso(source_checked_at)
            payload["cache_updated_at"] = _iso(cache_updated_at)
            if reference_period:
                payload["reference_period"] = reference_period
            payload_sha256 = _sha256(payload)
            record_key = hashlib.sha256(
                f"{table_name}\0{record_id}".encode("utf-8")
            ).hexdigest()
            normalized_records.append(
                {
                    "layer_key": layer_key,
                    "record_key": record_key,
                    "match": match,
                    "payload": payload,
                    "teable_table": table_name,
                    "teable_record_id": record_id,
                    "source_updated_at": _iso(source_updated_at),
                    "source_checked_at": (
                        _iso(source_checked_at)
                        if source_checked_at is not None
                        else ""
                    ),
                    "cache_updated_at": _iso(cache_updated_at),
                    "payload_sha256": payload_sha256,
                }
            )
            oldest_cache = _parse_datetime(oldest_cache_by_layer.get(layer_key))
            if oldest_cache is None or cache_updated_at < oldest_cache:
                oldest_cache_by_layer[layer_key] = _iso(cache_updated_at)
            latest = _parse_datetime(latest_cache_by_layer.get(layer_key))
            if latest is None or cache_updated_at > latest:
                latest_cache_by_layer[layer_key] = _iso(cache_updated_at)
            temporal_evidence = source_temporal_evidence_by_layer[layer_key]
            row_counts = _object(
                temporal_evidence.get("row_counts_by_temporality")
            )
            row_counts[source_temporality] = int(
                row_counts.get(source_temporality) or 0
            ) + 1
            temporal_evidence["row_counts_by_temporality"] = row_counts
            source_updated_counts = _object(
                temporal_evidence.get(
                    "source_updated_at_row_counts_by_temporality"
                )
            )
            source_updated_counts[source_temporality] = int(
                source_updated_counts.get(source_temporality) or 0
            ) + 1
            temporal_evidence[
                "source_updated_at_row_counts_by_temporality"
            ] = source_updated_counts
            oldest_source_by_temporality = _object(
                temporal_evidence.get(
                    "oldest_source_updated_at_by_temporality"
                )
            )
            oldest_source = _parse_datetime(
                oldest_source_by_temporality.get(source_temporality)
            )
            if oldest_source is None or source_updated_at < oldest_source:
                oldest_source_by_temporality[source_temporality] = _iso(
                    source_updated_at
                )
            temporal_evidence[
                "oldest_source_updated_at_by_temporality"
            ] = oldest_source_by_temporality
            latest_source_by_temporality = _object(
                temporal_evidence.get(
                    "latest_source_updated_at_by_temporality"
                )
            )
            latest_source = _parse_datetime(
                latest_source_by_temporality.get(source_temporality)
            )
            if latest_source is None or source_updated_at > latest_source:
                latest_source_by_temporality[source_temporality] = _iso(
                    source_updated_at
                )
            temporal_evidence[
                "latest_source_updated_at_by_temporality"
            ] = latest_source_by_temporality
            if source_sla_at is not None:
                source_sla_counts = _object(
                    temporal_evidence.get(
                        "source_sla_at_row_counts_by_temporality"
                    )
                )
                source_sla_counts[source_temporality] = int(
                    source_sla_counts.get(source_temporality) or 0
                ) + 1
                temporal_evidence[
                    "source_sla_at_row_counts_by_temporality"
                ] = source_sla_counts
                oldest_source_sla_by_temporality = _object(
                    temporal_evidence.get(
                        "oldest_source_sla_at_by_temporality"
                    )
                )
                oldest_source_sla = _parse_datetime(
                    oldest_source_sla_by_temporality.get(source_temporality)
                )
                if oldest_source_sla is None or source_sla_at < oldest_source_sla:
                    oldest_source_sla_by_temporality[source_temporality] = _iso(
                        source_sla_at
                    )
                temporal_evidence[
                    "oldest_source_sla_at_by_temporality"
                ] = oldest_source_sla_by_temporality
                latest_source_sla_by_temporality = _object(
                    temporal_evidence.get(
                        "latest_source_sla_at_by_temporality"
                    )
                )
                latest_source_sla = _parse_datetime(
                    latest_source_sla_by_temporality.get(source_temporality)
                )
                if latest_source_sla is None or source_sla_at > latest_source_sla:
                    latest_source_sla_by_temporality[source_temporality] = _iso(
                        source_sla_at
                    )
                temporal_evidence[
                    "latest_source_sla_at_by_temporality"
                ] = latest_source_sla_by_temporality
            if reference_period:
                reference_periods = {
                    _text(value)
                    for value in list(
                        temporal_evidence.get("reference_periods") or []
                    )
                    if _text(value)
                }
                reference_periods.add(reference_period)
                temporal_evidence["reference_periods"] = sorted(
                    reference_periods
                )
                reference_period_counts = _object(
                    temporal_evidence.get("reference_period_row_counts")
                )
                reference_period_counts[reference_period] = int(
                    reference_period_counts.get(reference_period) or 0
                ) + 1
                temporal_evidence[
                    "reference_period_row_counts"
                ] = reference_period_counts
    source_evidence = _object(export.get("source_evidence"))
    failures.extend(
        _teable_source_evidence_failures(
            source_evidence,
            expected_tables=set(expected_tables),
            actual_table_counts=table_counts,
            expected_origin=expected_teable_origin,
            expected_base_id_sha256=expected_teable_base_id_sha256,
        )
    )
    return {
        "status": "pass" if not failures else "fail",
        "failures": failures,
        "records": normalized_records,
        "table_counts": table_counts,
        "layer_count": len(
            {str(row.get("layer_key") or "") for row in normalized_records}
        ),
        "oldest_cache_updated_at_by_layer": oldest_cache_by_layer,
        "latest_cache_updated_at_by_layer": latest_cache_by_layer,
        "max_cache_age_policy_hours": normalized_max_age_hours,
        "source_temporal_evidence_by_layer": source_temporal_evidence_by_layer,
        "source_generated_at": _iso(generated_at),
        "source_payload_sha256": _sha256(export),
        "registry_payload_sha256": _sha256(registry),
        "source_evidence": source_evidence,
        "source_authority": {
            "expected_origin": _safe_https_origin(expected_teable_origin),
            "expected_base_id_sha256": _text(expected_teable_base_id_sha256).casefold(),
            "bound_independently": True,
        },
        "ingestion_mode": (
            "launch_authenticated_fetch" if launch_mode else "fixture_prefetched_export"
        ),
        "layer_keys": sorted(expected_tables.values()),
        "table_names": sorted(expected_tables),
    }


def _p95(values: list[float]) -> float:
    if not values:
        return math.inf
    ordered = sorted(values)
    return ordered[max(math.ceil(len(ordered) * 0.95) - 1, 0)]


def execute_ingestion(
    *,
    plan: dict[str, object],
    repository: PostgresPropertyEvidenceOverlayRepository,
    candidate_sha: str,
    max_query_ms: float,
    stage_only: bool = False,
    observed_at: datetime | None = None,
) -> dict[str, object]:
    now = observed_at or _utc_now()
    failures = [str(item) for item in list(plan.get("failures") or [])]
    if not stage_only:
        failures.append(
            "read-model activation requires an explicit current-run preactivation authority"
        )
    try:
        normalized_max_query_ms = _positive_finite(
            max_query_ms,
            name="max_query_ms",
            maximum=MAX_ACCEPTED_QUERY_BUDGET_MS,
        )
    except ValueError as exc:
        failures.append(str(exc))
        normalized_max_query_ms = 0.0
    records = [
        dict(row) for row in list(plan.get("records") or []) if isinstance(row, dict)
    ]
    table_counts = {
        str(key): int(value)
        for key, value in dict(plan.get("table_counts") or {}).items()
    }
    snapshot_id = hashlib.sha256(
        (
            f"{candidate_sha}\0{plan.get('source_payload_sha256')}\0"
            f"{plan.get('registry_payload_sha256')}\0{_iso(now)}"
        ).encode("utf-8")
    ).hexdigest()
    coverage: list[dict[str, object]] = []
    samples: list[tuple[str, dict[str, str]]] = []
    seen_layers: set[str] = set()
    for row in records:
        layer_key = _text(row.get("layer_key"))
        if layer_key in seen_layers:
            continue
        match = dict(row.get("match") or {})
        if match:
            samples.append(
                (
                    layer_key,
                    {str(key): str(value) for key, value in match.items()},
                )
            )
            seen_layers.add(layer_key)
    durations_ms: list[float] = []
    previous_active_snapshot_id = ""
    activation_performed = False
    candidate_staged = False
    candidate_discarded = False
    if not failures:
        try:
            repository.ensure_schema()
            previous_active_snapshot_id = repository.active_snapshot_id()
            repository.stage_snapshot(
                snapshot_id=snapshot_id,
                source_schema=EXPORT_SCHEMA,
                source_generated_at=_text(plan.get("source_generated_at")),
                ingested_at=_iso(now),
                candidate_sha=candidate_sha,
                payload_sha256=_text(plan.get("source_payload_sha256")),
                records=records,
                table_counts=table_counts,
            )
            candidate_staged = True
            coverage = repository.coverage(snapshot_id=snapshot_id)
            coverage_by_layer = {
                str(row.get("layer_key") or ""): dict(row) for row in coverage
            }
            expected_layers = {str(row.get("layer_key") or "") for row in records}
            if (
                len(coverage) != 8
                or set(coverage_by_layer) != expected_layers
                or expected_layers != REQUIRED_LAYER_KEYS
            ):
                failures.append(
                    "Postgres staged read-model coverage does not exactly match "
                    "the eight-layer snapshot"
                )
            observed_table_counts = {
                _text(row.get("teable_table")): int(row.get("record_count") or 0)
                for row in coverage
            }
            if observed_table_counts != table_counts:
                failures.append(
                    "Postgres staged read-model counts do not match the Teable snapshot"
                )
            if len(samples) != 8:
                failures.append(
                    "Postgres staged read-model benchmark requires one sample per layer"
                )
            if not failures:
                for layer_key, sample in samples:
                    for _ in range(3):
                        started = time.perf_counter()
                        found = repository.lookup(sample, snapshot_id=snapshot_id)
                        duration_ms = (time.perf_counter() - started) * 1000.0
                        durations_ms.append(duration_ms)
                        if not any(
                            _text(row.get("layer_key")) == layer_key for row in found
                        ):
                            failures.append(
                                "Postgres staged read-model sample lookup did not "
                                f"return layer {layer_key}"
                            )
                            break
            if len(durations_ms) != 24 and not failures:
                failures.append(
                    "Postgres staged read-model benchmark requires three lookups per layer"
                )
        except Exception as exc:
            failures.append(
                "Postgres staged read-model validation failed: "
                f"{exc.__class__.__name__}"
            )
    query_p95_ms = _p95(durations_ms)
    if candidate_staged and (
        not math.isfinite(query_p95_ms) or query_p95_ms > normalized_max_query_ms
    ):
        failures.append(
            f"Postgres staged cached lookup p95 {query_p95_ms:.2f}ms exceeds "
            f"{normalized_max_query_ms:.2f}ms"
        )
    if candidate_staged and failures and not activation_performed:
        try:
            repository.discard_staged_snapshot(snapshot_id)
            candidate_discarded = True
        except Exception as exc:
            failures.append(
                f"Postgres staged read-model cleanup failed: {exc.__class__.__name__}"
            )
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "status": (
            "pass"
            if candidate_staged
            and not failures
            and (stage_only or activation_performed)
            else "fail"
        ),
        "generated_at": _iso(now),
        "candidate_sha": candidate_sha,
        "snapshot_id": snapshot_id,
        "source_schema": EXPORT_SCHEMA,
        "source_generated_at": _text(plan.get("source_generated_at")),
        "source_payload_sha256": _text(plan.get("source_payload_sha256")),
        "registry_payload_sha256": _text(plan.get("registry_payload_sha256")),
        "source_evidence": _object(plan.get("source_evidence")),
        "source_authority": _object(plan.get("source_authority")),
        "ingestion": {
            "source": "authenticated_teable_api_export",
            "target": "postgres_cached_geo_rollup",
            "mode": _text(plan.get("ingestion_mode")),
            "transaction": "staged_validate_benchmark_atomic_pointer_switch",
            "table_count": len(table_counts),
            "layer_count": int(plan.get("layer_count") or 0),
            "record_count": len(records),
            "table_counts": table_counts,
            "layer_keys": list(plan.get("layer_keys") or []),
            "table_names": list(plan.get("table_names") or []),
        },
        "temporal_evidence": {
            "cache_max_age_policy_hours": float(
                plan.get("max_cache_age_policy_hours") or 0.0
            ),
            "oldest_cache_updated_at_by_layer": dict(
                plan.get("oldest_cache_updated_at_by_layer") or {}
            ),
            "latest_cache_updated_at_by_layer": dict(
                plan.get("latest_cache_updated_at_by_layer") or {}
            ),
            "source_by_layer": dict(
                plan.get("source_temporal_evidence_by_layer") or {}
            ),
            "cache_updated_at_proves_source_freshness": False,
        },
        "activation": {
            "phase": "active" if activation_performed else "staged",
            "candidate_snapshot_id": snapshot_id,
            "previous_active_snapshot_id": previous_active_snapshot_id,
            "activated_snapshot_id": snapshot_id if activation_performed else "",
            "candidate_staged": candidate_staged,
            "candidate_discarded": candidate_discarded,
            "activation_performed": activation_performed,
            "active_pointer_switch": "atomic_final_transaction",
            "active_snapshot_unchanged": not activation_performed,
            "active_snapshot_preserved_on_failure": bool(failures)
            and not activation_performed,
            "active_revalidation_performed": False,
            "active_revalidation_query_sample_count": 0,
            "active_revalidation_query_p95_ms": None,
        },
        "read_model": {
            "source_fetch_during_search": False,
            "lookup_policy": "indexed_postgres_cached_rollup_only",
            "coverage": coverage,
            "sample_layer_count": len(samples),
            "query_sample_count": len(durations_ms),
            "query_p95_ms": (
                round(query_p95_ms, 3) if math.isfinite(query_p95_ms) else None
            ),
            "query_budget_ms": normalized_max_query_ms,
        },
        "privacy": {
            "area_context_only": True,
            "property_scoring": False,
            "person_scoring": False,
            "raw_article_bodies_stored": False,
            "match_key_allowlist": sorted(ALLOWED_MATCH_KEYS),
        },
        "claim_safety": {
            "aggregate_safety_context_only": True,
            "safety_source_rights_caveat_required": True,
            "municipal_rss_is_independent_press": False,
        },
        "failures": failures,
    }
    return receipt


def verify_receipt(
    receipt: dict[str, object],
    *,
    expected_candidate_sha: str,
    max_age_hours: float,
    expected_teable_origin: str,
    expected_teable_base_id_sha256: str,
    expected_phase: str = "active",
    now: datetime | None = None,
) -> list[str]:
    observed_at = now or _utc_now()
    failures: list[str] = []
    try:
        normalized_max_age_hours = _positive_finite(
            max_age_hours,
            name="max_age_hours",
        )
    except ValueError as exc:
        failures.append(str(exc))
        normalized_max_age_hours = 0.0
    if receipt.get("schema") != RECEIPT_SCHEMA or receipt.get("status") != "pass":
        failures.append(
            "evidence overlay read-model receipt must be a passing v3 receipt"
        )
    producer_failures = receipt.get("failures")
    if not isinstance(producer_failures, list) or producer_failures:
        failures.append(
            "evidence overlay read-model receipt contains producer failures"
        )
    if not re.fullmatch(r"[0-9a-f]{40}", _text(expected_candidate_sha)):
        failures.append("expected evidence overlay candidate SHA is invalid")
    if _text(receipt.get("candidate_sha")) != _text(expected_candidate_sha):
        failures.append("evidence overlay receipt candidate SHA mismatch")
    for field in ("source_payload_sha256", "registry_payload_sha256", "snapshot_id"):
        if not re.fullmatch(r"[0-9a-f]{64}", _text(receipt.get(field))):
            failures.append(f"evidence overlay receipt {field} is invalid")
    try:
        registry = _load_object(REGISTRY_PATH)
        registry_layers = _registry_layers(registry)
    except (OSError, json.JSONDecodeError, ValueError):
        failures.append("evidence overlay registry cannot be validated")
        expected_tables: set[str] = set()
        registry_layers: list[dict[str, object]] = []
    else:
        expected_tables = {_text(row.get("teable_table")) for row in registry_layers}
        if _text(receipt.get("registry_payload_sha256")) != _sha256(registry):
            failures.append("evidence overlay receipt registry digest mismatch")
    generated_at = _parse_datetime(receipt.get("generated_at"))
    if generated_at is None or generated_at > observed_at:
        failures.append("evidence overlay receipt generated_at is invalid")
    elif (
        observed_at - generated_at
    ).total_seconds() > normalized_max_age_hours * 3600.0:
        failures.append("evidence overlay receipt capture is operationally expired")
    if receipt.get("source_schema") != EXPORT_SCHEMA:
        failures.append("evidence overlay receipt source schema mismatch")
    source_generated_at = _parse_datetime(receipt.get("source_generated_at"))
    if source_generated_at is None or source_generated_at > observed_at:
        failures.append("evidence overlay receipt source timestamp is invalid")
    elif (
        observed_at - source_generated_at
    ).total_seconds() > normalized_max_age_hours * 3600.0:
        failures.append("evidence overlay Teable export capture is operationally expired")
    ingestion = _object(receipt.get("ingestion"))
    if (
        ingestion.get("source") != "authenticated_teable_api_export"
        or ingestion.get("target") != "postgres_cached_geo_rollup"
        or ingestion.get("mode") != "launch_authenticated_fetch"
    ):
        failures.append(
            "evidence overlay receipt must bind authenticated launch Teable-to-Postgres ingestion"
        )
    if (
        ingestion.get("transaction")
        != "staged_validate_benchmark_atomic_pointer_switch"
        or _integer(ingestion.get("layer_count")) != 8
    ):
        failures.append(
            "evidence overlay receipt must prove staged atomic coverage of all eight layers"
        )
    if _integer(ingestion.get("table_count")) != 8:
        failures.append("evidence overlay receipt must prove all eight Teable tables")
    raw_record_count = ingestion.get("record_count")
    record_count = raw_record_count if type(raw_record_count) is int else 0
    if record_count < 8:
        failures.append(
            "evidence overlay receipt must prove at least one row per layer"
        )
    if _string_set(ingestion.get("layer_keys")) != REQUIRED_LAYER_KEYS:
        failures.append("evidence overlay receipt layer set mismatch")
    if _string_set(ingestion.get("table_names")) != expected_tables:
        failures.append("evidence overlay receipt Teable table set mismatch")
    table_counts = _object(ingestion.get("table_counts"))
    table_counts_ok = set(table_counts) == expected_tables and all(
        type(value) is int and value >= 1 for value in table_counts.values()
    )
    if not table_counts_ok:
        failures.append(
            "evidence overlay receipt requires a positive row count for every Teable table"
        )
    normalized_table_counts = {
        str(key): (_integer(value) or 0) for key, value in table_counts.items()
    }
    if table_counts_ok and sum(normalized_table_counts.values()) != record_count:
        failures.append(
            "evidence overlay receipt record count must equal the sum of Teable table counts"
        )
    failures.extend(
        _teable_source_evidence_failures(
            receipt.get("source_evidence"),
            expected_tables=expected_tables,
            actual_table_counts=normalized_table_counts,
            expected_origin=expected_teable_origin,
            expected_base_id_sha256=expected_teable_base_id_sha256,
        )
    )
    source_authority = _object(receipt.get("source_authority"))
    normalized_expected_origin = _safe_https_origin(expected_teable_origin)
    normalized_expected_base_digest = _text(expected_teable_base_id_sha256).casefold()
    if (
        source_authority.get("bound_independently") is not True
        or _text(source_authority.get("expected_origin")) != normalized_expected_origin
        or _text(source_authority.get("expected_base_id_sha256")).casefold()
        != normalized_expected_base_digest
    ):
        failures.append(
            "evidence overlay receipt source authority does not match independent launch configuration"
        )
    temporal_evidence = _object(receipt.get("temporal_evidence"))
    try:
        cache_policy_hours = float(
            temporal_evidence.get("cache_max_age_policy_hours")
        )
    except (TypeError, ValueError):
        cache_policy_hours = 0.0
    if (
        not math.isfinite(cache_policy_hours)
        or cache_policy_hours <= 0
        or cache_policy_hours > normalized_max_age_hours
    ):
        failures.append("evidence overlay receipt cache-age policy is invalid")
    if temporal_evidence.get("cache_updated_at_proves_source_freshness") is not False:
        failures.append(
            "evidence overlay receipt must not represent cache recency as source freshness"
        )
    oldest_cache_by_layer = _object(
        temporal_evidence.get("oldest_cache_updated_at_by_layer")
    )
    latest_cache_by_layer = _object(
        temporal_evidence.get("latest_cache_updated_at_by_layer")
    )
    if (
        set(oldest_cache_by_layer) != REQUIRED_LAYER_KEYS
        or set(latest_cache_by_layer) != REQUIRED_LAYER_KEYS
    ):
        failures.append(
            "evidence overlay receipt must bind cache recency for all eight layers"
        )
    for layer_key in sorted(REQUIRED_LAYER_KEYS):
        oldest_cached_at = _parse_datetime(oldest_cache_by_layer.get(layer_key))
        latest_cached_at = _parse_datetime(latest_cache_by_layer.get(layer_key))
        if (
            oldest_cached_at is None
            or latest_cached_at is None
            or oldest_cached_at > latest_cached_at
            or latest_cached_at > observed_at
        ):
            failures.append(
                f"evidence overlay layer {layer_key} has invalid cache-recency evidence"
            )
        elif (
            observed_at - oldest_cached_at
        ).total_seconds() > normalized_max_age_hours * 3600.0:
            failures.append(
                f"evidence overlay layer {layer_key} has an expired cached row"
            )
    source_by_layer = _object(temporal_evidence.get("source_by_layer"))
    if set(source_by_layer) != REQUIRED_LAYER_KEYS:
        failures.append(
            "evidence overlay receipt must bind source timing for all eight layers"
        )
    registry_by_layer = {
        _text(layer.get("layer_key")): layer for layer in registry_layers
    }
    table_name_by_layer = {
        _text(layer.get("layer_key")): _text(layer.get("teable_table"))
        for layer in registry_layers
    }
    temporal_row_counts_by_layer: dict[str, int] = {}
    for layer_key in sorted(REQUIRED_LAYER_KEYS):
        layer = registry_by_layer.get(layer_key, {})
        source = _object(source_by_layer.get(layer_key))
        expected_temporalities = _string_set(
            layer.get("allowed_source_temporalities")
        )
        expected_source_age_policy = {
            _text(key): float(value)
            for key, value in _object(
                layer.get("source_max_age_hours_by_temporality")
            ).items()
        }
        expected_sla_timestamp_fields = {
            _text(key): _text(value)
            for key, value in _object(
                layer.get("source_sla_timestamp_field_by_temporality")
            ).items()
        }
        source_age_policy = _object(
            source.get("source_max_age_hours_by_temporality")
        )
        try:
            normalized_source_age_policy = {
                _text(key): float(value)
                for key, value in source_age_policy.items()
            }
        except (TypeError, ValueError):
            normalized_source_age_policy = {}
        source_sla_timestamp_fields = {
            _text(key): _text(value)
            for key, value in _object(
                source.get("source_sla_timestamp_field_by_temporality")
            ).items()
        }
        if (
            _text(source.get("cadence_class"))
            != _text(layer.get("source_cadence_class"))
            or _string_set(source.get("allowed_temporalities"))
            != expected_temporalities
            or normalized_source_age_policy != expected_source_age_policy
            or source_sla_timestamp_fields != expected_sla_timestamp_fields
        ):
            failures.append(
                f"evidence overlay layer {layer_key} source cadence receipt mismatch"
            )
        parsed_row_counts = _positive_count_map(
            source.get("row_counts_by_temporality")
        )
        row_counts = parsed_row_counts or {}
        temporal_row_count = sum(row_counts.values())
        temporal_row_counts_by_layer[layer_key] = temporal_row_count
        table_name = table_name_by_layer.get(layer_key, "")
        expected_table_count = normalized_table_counts.get(table_name, 0)
        if (
            parsed_row_counts is None
            or set(row_counts) - expected_temporalities
            or temporal_row_count != expected_table_count
        ):
            failures.append(
                f"evidence overlay layer {layer_key} source temporality counts are invalid"
            )
        source_updated_counts = _positive_count_map(
            source.get("source_updated_at_row_counts_by_temporality")
        )
        if source_updated_counts != row_counts:
            failures.append(
                f"evidence overlay layer {layer_key} source timestamp row coverage is incomplete"
            )
        oldest_source_by_temporality = _object(
            source.get("oldest_source_updated_at_by_temporality")
        )
        latest_source_by_temporality = _object(
            source.get("latest_source_updated_at_by_temporality")
        )
        if (
            set(oldest_source_by_temporality) != set(row_counts)
            or set(latest_source_by_temporality) != set(row_counts)
        ):
            failures.append(
                f"evidence overlay layer {layer_key} source timestamp coverage is invalid"
            )
        for temporality in sorted(row_counts):
            oldest_source_at = _parse_datetime(
                oldest_source_by_temporality.get(temporality)
            )
            latest_source_at = _parse_datetime(
                latest_source_by_temporality.get(temporality)
            )
            if (
                oldest_source_at is None
                or latest_source_at is None
                or oldest_source_at > latest_source_at
                or latest_source_at > observed_at
            ):
                failures.append(
                    f"evidence overlay layer {layer_key} has invalid {temporality} source timestamps"
                )
                continue
        oldest_source_sla_by_temporality = _object(
            source.get("oldest_source_sla_at_by_temporality")
        )
        latest_source_sla_by_temporality = _object(
            source.get("latest_source_sla_at_by_temporality")
        )
        expected_sla_temporalities = set(row_counts) & set(
            expected_source_age_policy
        )
        expected_source_sla_counts = {
            temporality: row_counts[temporality]
            for temporality in expected_sla_temporalities
        }
        source_sla_counts = _positive_count_map(
            source.get("source_sla_at_row_counts_by_temporality"),
            allow_empty=True,
        )
        if source_sla_counts != expected_source_sla_counts:
            failures.append(
                f"evidence overlay layer {layer_key} source SLA row coverage is incomplete"
            )
        if (
            set(oldest_source_sla_by_temporality)
            != expected_sla_temporalities
            or set(latest_source_sla_by_temporality)
            != expected_sla_temporalities
        ):
            failures.append(
                f"evidence overlay layer {layer_key} source SLA timestamp coverage is invalid"
            )
        for temporality in sorted(expected_sla_temporalities):
            oldest_source_sla_at = _parse_datetime(
                oldest_source_sla_by_temporality.get(temporality)
            )
            latest_source_sla_at = _parse_datetime(
                latest_source_sla_by_temporality.get(temporality)
            )
            source_max_age_hours = expected_source_age_policy[temporality]
            if (
                oldest_source_sla_at is None
                or latest_source_sla_at is None
                or oldest_source_sla_at > latest_source_sla_at
                or latest_source_sla_at > observed_at
            ):
                failures.append(
                    f"evidence overlay layer {layer_key} has invalid {temporality} source SLA timestamps"
                )
            elif (
                observed_at - oldest_source_sla_at
            ).total_seconds() > source_max_age_hours * 3600.0:
                failures.append(
                    f"evidence overlay layer {layer_key} has an expired {temporality} source-check SLA"
                )
        reference_periods = [
            _text(value)
            for value in list(source.get("reference_periods") or [])
            if _text(value)
        ]
        reference_period_counts = _positive_count_map(
            source.get("reference_period_row_counts"),
            allow_empty=True,
        )
        expected_reference_count = row_counts.get("reference", 0)
        if row_counts.get("reference", 0) > 0:
            if (
                not reference_periods
                or len(reference_periods) != len(set(reference_periods))
                or any(
                    not _valid_reference_period(value) for value in reference_periods
                )
                or reference_period_counts is None
                or set(reference_period_counts) != set(reference_periods)
                or sum(reference_period_counts.values())
                != expected_reference_count
            ):
                failures.append(
                    f"evidence overlay layer {layer_key} lacks valid reference-period evidence"
                )
        elif reference_periods or reference_period_counts != {}:
            failures.append(
                f"evidence overlay layer {layer_key} has reference periods without reference rows"
            )
    activation = _object(receipt.get("activation"))
    normalized_expected_phase = _text(expected_phase).casefold()
    if normalized_expected_phase not in {"staged", "active"}:
        failures.append("expected evidence overlay activation phase is invalid")
    elif activation.get("phase") != normalized_expected_phase:
        failures.append("evidence overlay receipt activation phase mismatch")
    if _text(activation.get("candidate_snapshot_id")) != _text(
        receipt.get("snapshot_id")
    ):
        failures.append(
            "evidence overlay receipt candidate snapshot binding is invalid"
        )
    if activation.get("candidate_staged") is not True:
        failures.append("evidence overlay receipt must prove the candidate was staged")
    if normalized_expected_phase == "staged":
        if (
            activation.get("activation_performed") is not False
            or _text(activation.get("activated_snapshot_id"))
            or activation.get("active_snapshot_unchanged") is not True
            or activation.get("active_revalidation_performed") is not False
            or (_integer(activation.get("active_revalidation_query_sample_count")) or 0)
            != 0
        ):
            failures.append(
                "staged evidence overlay receipt must leave the active snapshot unchanged"
            )
    elif (
        activation.get("activation_performed") is not True
        or _text(activation.get("activated_snapshot_id"))
        != _text(receipt.get("snapshot_id"))
        or activation.get("active_snapshot_unchanged") is not False
        or activation.get("active_revalidation_performed") is not True
        or (_integer(activation.get("active_revalidation_query_sample_count")) or 0)
        < 24
    ):
        failures.append(
            "active evidence overlay receipt must prove the final atomic pointer switch"
        )
    if normalized_expected_phase == "active":
        authorized_workflow = _object(activation.get("authorized_workflow"))
        if (
            not re.fullmatch(
                r"[0-9a-f]{64}",
                _text(activation.get("activation_authority_sha256")),
            )
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                _text(activation.get("staged_receipt_sha256")),
            )
            or set(authorized_workflow)
            != {"head_sha", "run_id", "run_attempt"}
            or not re.fullmatch(
                r"[0-9a-f]{40}", _text(authorized_workflow.get("head_sha"))
            )
            or not re.fullmatch(
                r"[1-9][0-9]{0,19}", _text(authorized_workflow.get("run_id"))
            )
            or not re.fullmatch(
                r"[1-9][0-9]{0,19}",
                _text(authorized_workflow.get("run_attempt")),
            )
        ):
            failures.append(
                "active evidence overlay receipt lacks current-run activation authority binding"
            )
    if activation.get("active_pointer_switch") != "atomic_final_transaction":
        failures.append("evidence overlay receipt active-pointer strategy is invalid")
    read_model = _object(receipt.get("read_model"))
    if read_model.get("source_fetch_during_search") is not False:
        failures.append(
            "evidence overlay receipt must forbid source fetches during search"
        )
    if read_model.get("lookup_policy") != "indexed_postgres_cached_rollup_only":
        failures.append("evidence overlay receipt lookup policy mismatch")
    if _integer(read_model.get("sample_layer_count")) != 8:
        failures.append(
            "evidence overlay receipt must exercise all eight read-model layers"
        )
    if (_integer(read_model.get("query_sample_count")) or 0) < 24:
        failures.append(
            "evidence overlay receipt requires three lookup samples per layer"
        )
    raw_coverage = read_model.get("coverage")
    coverage_rows = [
        dict(row)
        for row in (raw_coverage if isinstance(raw_coverage, list) else [])
        if isinstance(row, dict)
    ]
    coverage_by_layer = {_text(row.get("layer_key")): row for row in coverage_rows}
    if (
        len(coverage_rows) != len(REQUIRED_LAYER_KEYS)
        or set(coverage_by_layer) != REQUIRED_LAYER_KEYS
    ):
        failures.append("evidence overlay Postgres coverage layer set mismatch")
    elif any(
        type(row.get("record_count")) is not int
        or int(row["record_count"]) < 1
        for row in coverage_by_layer.values()
    ):
        failures.append("evidence overlay Postgres coverage contains an empty layer")
    else:
        for layer_key in sorted(REQUIRED_LAYER_KEYS):
            coverage = coverage_by_layer[layer_key]
            table_name = table_name_by_layer.get(layer_key, "")
            coverage_count = int(coverage["record_count"])
            if (
                _text(coverage.get("teable_table")) != table_name
                or coverage_count != normalized_table_counts.get(table_name, 0)
                or coverage_count
                != temporal_row_counts_by_layer.get(layer_key, 0)
            ):
                failures.append(
                    f"evidence overlay layer {layer_key} table, Postgres, and temporal row counts do not match"
                )
    try:
        query_p95_ms = float(read_model.get("query_p95_ms"))
        query_budget_ms = float(read_model.get("query_budget_ms"))
        if (
            not math.isfinite(query_budget_ms)
            or query_budget_ms <= 0
            or query_budget_ms > MAX_ACCEPTED_QUERY_BUDGET_MS
        ):
            failures.append(
                "evidence overlay receipt query budget exceeds the launch maximum"
            )
        if (
            not math.isfinite(query_p95_ms)
            or query_p95_ms < 0
            or query_p95_ms > query_budget_ms
        ):
            failures.append(
                "evidence overlay receipt exceeds its query performance budget"
            )
        if normalized_expected_phase == "active":
            active_query_p95_ms = float(
                activation.get("active_revalidation_query_p95_ms")
            )
            if (
                not math.isfinite(active_query_p95_ms)
                or active_query_p95_ms < 0
                or active_query_p95_ms > query_budget_ms
            ):
                failures.append(
                    "evidence overlay active revalidation exceeds its query budget"
                )
    except (TypeError, ValueError):
        failures.append(
            "evidence overlay receipt query performance evidence is invalid"
        )
    privacy = _object(receipt.get("privacy"))
    if (
        privacy.get("area_context_only") is not True
        or privacy.get("property_scoring") is not False
        or privacy.get("person_scoring") is not False
    ):
        failures.append("evidence overlay receipt privacy posture is invalid")
    if privacy.get("raw_article_bodies_stored") is not False:
        failures.append(
            "evidence overlay receipt must prove raw article bodies are not stored"
        )
    if _string_set(privacy.get("match_key_allowlist")) != ALLOWED_MATCH_KEYS:
        failures.append("evidence overlay receipt match-key allowlist mismatch")
    claim_safety = _object(receipt.get("claim_safety"))
    if claim_safety != {
        "aggregate_safety_context_only": True,
        "safety_source_rights_caveat_required": True,
        "municipal_rss_is_independent_press": False,
    }:
        failures.append("evidence overlay receipt claim-safety posture is invalid")
    return failures


def verify_activation_authority(
    authority: dict[str, object],
    *,
    expected_candidate_sha: str,
    expected_snapshot_id: str,
    expected_staged_receipt_sha256: str,
    workflow_head_sha: str,
    workflow_run_id: str,
    workflow_run_attempt: str,
    expected_teable_origin: str,
    expected_teable_base_id_sha256: str,
    now: datetime | None = None,
) -> list[str]:
    observed_at = now or _utc_now()
    failures: list[str] = []
    if (
        authority.get("schema") != ACTIVATION_AUTHORITY_SCHEMA
        or authority.get("status") != "pass"
        or authority.get("authority_phase") != "preactivation"
    ):
        failures.append("activation authority must be a passing preactivation envelope")
    if (
        not re.fullmatch(r"[0-9a-f]{40}", expected_candidate_sha)
        or _text(authority.get("candidate_sha")) != expected_candidate_sha
    ):
        failures.append("activation authority candidate SHA mismatch")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", expected_snapshot_id)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_staged_receipt_sha256)
    ):
        failures.append("activation authority staged snapshot identity is invalid")

    workflow = _object(authority.get("workflow"))
    expected_workflow = {
        "head_sha": workflow_head_sha,
        "run_id": workflow_run_id,
        "run_attempt": workflow_run_attempt,
    }
    if (
        not re.fullmatch(r"[0-9a-f]{40}", workflow_head_sha)
        or not re.fullmatch(r"[1-9][0-9]{0,19}", workflow_run_id)
        or not re.fullmatch(r"[1-9][0-9]{0,19}", workflow_run_attempt)
        or workflow != expected_workflow
    ):
        failures.append("activation authority workflow identity mismatch")

    normalized_teable_origin = _safe_https_origin(expected_teable_origin)
    normalized_teable_digest = _text(expected_teable_base_id_sha256).casefold()
    teable_authority = _object(authority.get("teable_authority"))
    if (
        not normalized_teable_origin
        or not re.fullmatch(r"[0-9a-f]{64}", normalized_teable_digest)
        or teable_authority
        != {
            "origin": normalized_teable_origin,
            "base_id_sha256": normalized_teable_digest,
            "supplied_independently": True,
        }
        or _contains_raw_base_id_key(authority)
    ):
        failures.append("activation authority Teable identity mismatch")

    activation_scope = _object(authority.get("activation_scope"))
    overlay_identity = _object(_object(authority.get("inputs")).get("overlay"))
    if (
        _text(activation_scope.get("snapshot_id")) != expected_snapshot_id
        or _text(activation_scope.get("staged_overlay_receipt_sha256")).casefold()
        != expected_staged_receipt_sha256
        or _text(activation_scope.get("activation_authority_sha256"))
        or _text(overlay_identity.get("sha256")).casefold()
        != expected_staged_receipt_sha256
    ):
        failures.append("activation authority staged receipt binding mismatch")

    checks = authority.get("checks")
    if (
        authority.get("activation_authorized") is not True
        or authority.get("launch_authorized") is not False
        or authority.get("notification_authorized") is not False
        or authority.get("failures") != []
        or not isinstance(checks, list)
        or not checks
        or any(not isinstance(row, dict) or row.get("ok") is not True for row in checks)
    ):
        failures.append("activation authority does not authorize activation exclusively")

    generated_at = _parse_datetime(authority.get("generated_at"))
    if generated_at is None or generated_at > observed_at:
        failures.append("activation authority generated_at is invalid")
    elif (observed_at - generated_at).total_seconds() > MAX_ACTIVATION_AUTHORITY_AGE_SECONDS:
        failures.append("activation authority is stale")
    return failures


def activate_staged_receipt(
    *,
    receipt: dict[str, object],
    repository: PostgresPropertyEvidenceOverlayRepository,
    snapshot_id: str,
    expected_candidate_sha: str,
    max_age_hours: float,
    expected_teable_origin: str,
    expected_teable_base_id_sha256: str,
    activation_authority_sha256: str,
    staged_receipt_sha256: str,
    authorized_workflow: dict[str, str],
    now: datetime | None = None,
) -> dict[str, object]:
    observed_at = now or _utc_now()
    updated = json.loads(json.dumps(receipt))
    failures = verify_receipt(
        updated,
        expected_candidate_sha=expected_candidate_sha,
        max_age_hours=max_age_hours,
        expected_teable_origin=expected_teable_origin,
        expected_teable_base_id_sha256=expected_teable_base_id_sha256,
        expected_phase="staged",
        now=observed_at,
    )
    if _text(snapshot_id) != _text(updated.get("snapshot_id")):
        failures.append("requested activation snapshot does not match staged receipt")
    workflow_binding = {
        str(key): _text(value) for key, value in authorized_workflow.items()
    }
    if (
        not re.fullmatch(r"[0-9a-f]{64}", activation_authority_sha256)
        or not re.fullmatch(r"[0-9a-f]{64}", staged_receipt_sha256)
        or set(workflow_binding) != {"head_sha", "run_id", "run_attempt"}
        or not re.fullmatch(
            r"[0-9a-f]{40}", _text(workflow_binding.get("head_sha"))
        )
        or not re.fullmatch(
            r"[1-9][0-9]{0,19}", _text(workflow_binding.get("run_id"))
        )
        or not re.fullmatch(
            r"[1-9][0-9]{0,19}", _text(workflow_binding.get("run_attempt"))
        )
    ):
        failures.append("activation authority binding is invalid")
    activation = _object(updated.get("activation"))
    previous_snapshot_id = _text(activation.get("previous_active_snapshot_id"))
    activated = False
    rollback_performed = False
    active_revalidation_durations_ms: list[float] = []
    if not failures:
        try:
            repository.ensure_schema()
            if repository.active_snapshot_id() != previous_snapshot_id:
                raise ValueError("active snapshot changed after staged proof")
            repository.activate_snapshot(
                snapshot_id=snapshot_id,
                activated_at=_iso(observed_at),
                expected_previous_snapshot_id=previous_snapshot_id,
            )
            activated = True
            if repository.active_snapshot_id() != snapshot_id:
                raise ValueError(
                    "active pointer does not reference the activated snapshot"
                )
            expected_coverage = {
                (
                    _text(row.get("layer_key")),
                    _text(row.get("teable_table")),
                    int(row.get("record_count") or 0),
                )
                for row in list(
                    _object(updated.get("read_model")).get("coverage") or []
                )
                if isinstance(row, dict)
            }
            active_coverage = {
                (
                    _text(row.get("layer_key")),
                    _text(row.get("teable_table")),
                    int(row.get("record_count") or 0),
                )
                for row in repository.coverage()
            }
            if active_coverage != expected_coverage:
                raise ValueError("active snapshot coverage differs from staged proof")
            active_samples = repository.benchmark_samples(snapshot_id=snapshot_id)
            if len(active_samples) != 8:
                raise ValueError(
                    "active snapshot revalidation requires one sample per layer"
                )
            for layer_key, sample in active_samples:
                for _ in range(3):
                    started = time.perf_counter()
                    found = repository.lookup(sample)
                    active_revalidation_durations_ms.append(
                        (time.perf_counter() - started) * 1000.0
                    )
                    if not any(
                        _text(row.get("layer_key")) == layer_key for row in found
                    ):
                        raise ValueError(
                            "active snapshot lookup revalidation missed its expected layer"
                        )
            active_query_p95_ms = _p95(active_revalidation_durations_ms)
            query_budget_ms = float(
                _object(updated.get("read_model")).get("query_budget_ms")
            )
            if (
                len(active_revalidation_durations_ms) != 24
                or not math.isfinite(active_query_p95_ms)
                or not math.isfinite(query_budget_ms)
                or active_query_p95_ms > query_budget_ms
            ):
                raise ValueError(
                    "active snapshot lookup revalidation exceeded its staged budget"
                )
            activation.update(
                {
                    "phase": "active",
                    "activated_snapshot_id": snapshot_id,
                    "activated_at": _iso(observed_at),
                    "activation_performed": True,
                    "active_snapshot_unchanged": False,
                    "active_snapshot_preserved_on_failure": False,
                    "active_revalidation_performed": True,
                    "active_revalidation_query_sample_count": len(
                        active_revalidation_durations_ms
                    ),
                    "active_revalidation_query_p95_ms": round(
                        active_query_p95_ms,
                        3,
                    ),
                    "activation_authority_sha256": activation_authority_sha256,
                    "staged_receipt_sha256": staged_receipt_sha256,
                    "authorized_workflow": workflow_binding,
                }
            )
            updated["activation"] = activation
            updated["status"] = "pass"
            updated["failures"] = []
            post_activation_failures = verify_receipt(
                updated,
                expected_candidate_sha=expected_candidate_sha,
                max_age_hours=max_age_hours,
                expected_teable_origin=expected_teable_origin,
                expected_teable_base_id_sha256=expected_teable_base_id_sha256,
                expected_phase="active",
                now=observed_at,
            )
            if post_activation_failures:
                raise ValueError("active receipt revalidation failed")
        except Exception as exc:
            failures.append(
                f"staged snapshot activation failed: {exc.__class__.__name__}"
            )
            if activated:
                try:
                    repository.restore_active_snapshot(
                        failed_snapshot_id=snapshot_id,
                        restore_snapshot_id=previous_snapshot_id,
                        restored_at=_iso(observed_at),
                    )
                    rollback_performed = True
                except Exception as rollback_exc:
                    failures.append(
                        "staged snapshot activation rollback failed: "
                        f"{rollback_exc.__class__.__name__}"
                    )
    if failures:
        activation.update(
            {
                "phase": "rolled_back" if rollback_performed else "staged",
                "activation_performed": activated and not rollback_performed,
                "rollback_performed": rollback_performed,
                "restored_snapshot_id": previous_snapshot_id
                if rollback_performed
                else "",
                "active_snapshot_unchanged": not activated or rollback_performed,
                "active_snapshot_preserved_on_failure": not activated
                or rollback_performed,
            }
        )
        updated["activation"] = activation
        updated["status"] = "fail"
        updated["failures"] = failures
    return updated


def build_activation_rollback_token(
    *,
    staged_receipt: dict[str, object],
    expected_candidate_sha: str,
    staged_receipt_sha256: str,
    activation_authority_sha256: str,
    authorized_workflow: dict[str, str],
    now: datetime | None = None,
) -> dict[str, object]:
    activation = _object(staged_receipt.get("activation"))
    return {
        "schema": ROLLBACK_TOKEN_SCHEMA,
        "status": "prepared",
        "generated_at": _iso(now or _utc_now()),
        "candidate_sha": expected_candidate_sha,
        "activated_snapshot_id": _text(staged_receipt.get("snapshot_id")),
        "restore_snapshot_id": _text(activation.get("previous_active_snapshot_id")),
        "staged_receipt_sha256": staged_receipt_sha256,
        "activation_authority_sha256": activation_authority_sha256,
        "authorized_workflow": dict(authorized_workflow),
        "active_receipt_sha256": "",
        "compare_policy": "restore_only_if_active_equals_activated_snapshot",
        "idempotent_restore": True,
        "restore_performed": False,
        "failures": [],
    }


def verify_activation_rollback_token(
    token: dict[str, object],
    *,
    expected_candidate_sha: str,
    expected_activated_snapshot_id: str,
) -> list[str]:
    failures: list[str] = []
    if token.get("schema") != ROLLBACK_TOKEN_SCHEMA:
        failures.append("overlay activation rollback token schema is invalid")
    if token.get("status") not in {"prepared", "armed", "restored"}:
        failures.append("overlay activation rollback token status is invalid")
    if _text(token.get("candidate_sha")) != expected_candidate_sha or not re.fullmatch(
        r"[0-9a-f]{40}",
        expected_candidate_sha,
    ):
        failures.append("overlay activation rollback token candidate SHA mismatch")
    activated_snapshot_id = _text(token.get("activated_snapshot_id"))
    if activated_snapshot_id != expected_activated_snapshot_id or not re.fullmatch(
        r"[0-9a-f]{64}", activated_snapshot_id
    ):
        failures.append("overlay activation rollback token snapshot mismatch")
    restore_snapshot_id = _text(token.get("restore_snapshot_id"))
    if restore_snapshot_id and not re.fullmatch(r"[0-9a-f]{64}", restore_snapshot_id):
        failures.append("overlay activation rollback token restore pointer is invalid")
    if restore_snapshot_id == activated_snapshot_id:
        failures.append("overlay activation rollback token pointers must be distinct")
    if not re.fullmatch(r"[0-9a-f]{64}", _text(token.get("staged_receipt_sha256"))):
        failures.append(
            "overlay activation rollback token staged receipt digest is invalid"
        )
    if not re.fullmatch(
        r"[0-9a-f]{64}",
        _text(token.get("activation_authority_sha256")),
    ):
        failures.append(
            "overlay activation rollback token authority digest is invalid"
        )
    authorized_workflow = _object(token.get("authorized_workflow"))
    if (
        set(authorized_workflow) != {"head_sha", "run_id", "run_attempt"}
        or not re.fullmatch(
            r"[0-9a-f]{40}", _text(authorized_workflow.get("head_sha"))
        )
        or not re.fullmatch(
            r"[1-9][0-9]{0,19}", _text(authorized_workflow.get("run_id"))
        )
        or not re.fullmatch(
            r"[1-9][0-9]{0,19}",
            _text(authorized_workflow.get("run_attempt")),
        )
    ):
        failures.append("overlay activation rollback token workflow binding is invalid")
    if token.get("status") == "armed" and not re.fullmatch(
        r"[0-9a-f]{64}",
        _text(token.get("active_receipt_sha256")),
    ):
        failures.append(
            "armed overlay activation rollback token active receipt digest is invalid"
        )
    if (
        token.get("compare_policy")
        != "restore_only_if_active_equals_activated_snapshot"
        or token.get("idempotent_restore") is not True
    ):
        failures.append("overlay activation rollback token compare policy is invalid")
    return failures


def restore_activation_from_token(
    *,
    token: dict[str, object],
    repository: PostgresPropertyEvidenceOverlayRepository,
    expected_candidate_sha: str,
    expected_activated_snapshot_id: str,
    now: datetime | None = None,
) -> dict[str, object]:
    observed_at = now or _utc_now()
    updated = json.loads(json.dumps(token))
    failures = verify_activation_rollback_token(
        updated,
        expected_candidate_sha=expected_candidate_sha,
        expected_activated_snapshot_id=expected_activated_snapshot_id,
    )
    restore_performed = False
    if not failures:
        try:
            repository.ensure_schema()
            restore_performed = repository.restore_active_snapshot(
                failed_snapshot_id=expected_activated_snapshot_id,
                restore_snapshot_id=_text(updated.get("restore_snapshot_id")),
                restored_at=_iso(observed_at),
            )
        except Exception as exc:
            failures.append(
                "overlay activation compare-and-restore failed: "
                f"{exc.__class__.__name__}"
            )
    updated.update(
        {
            "status": "restored" if not failures else "fail",
            "restored_at": _iso(observed_at) if not failures else "",
            "restore_performed": restore_performed,
            "restore_idempotent_noop": not failures and not restore_performed,
            "failures": failures,
        }
    )
    return updated


def _atomic_write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _require_mode_600(path: Path, *, name: str) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise ValueError(f"{name} is not readable") from exc
    if mode != stat.S_IRUSR | stat.S_IWUSR:
        raise ValueError(f"{name} must have mode 0600")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest and prove the eight-layer PropertyQuarry Teable-to-Postgres read model."
    )
    parser.add_argument(
        "--teable-export",
        default="",
        help="Optional pre-fetched authenticated Teable export; protected launch normally fetches directly.",
    )
    parser.add_argument("--teable-base-url-env", default="TEABLE_BASE_URL")
    parser.add_argument("--teable-api-key-env", default="TEABLE_API_KEY")
    parser.add_argument(
        "--teable-base-id-env", default="PROPERTYQUARRY_EVIDENCE_OVERLAY_TEABLE_BASE_ID"
    )
    parser.add_argument("--teable-timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--expected-teable-origin-env",
        default="PROPERTYQUARRY_EXPECTED_TEABLE_ORIGIN",
    )
    parser.add_argument(
        "--expected-teable-base-id-sha256-env",
        default="PROPERTYQUARRY_EXPECTED_TEABLE_BASE_ID_SHA256",
    )
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    parser.add_argument(
        "--max-cache-age-hours",
        "--max-age-hours",
        dest="max_age_hours",
        type=float,
        default=48.0,
        help=(
            "Maximum operational age for the cache/export/receipt. Source age is "
            "governed separately by each registry layer."
        ),
    )
    parser.add_argument("--max-query-ms", type=float, default=100.0)
    parser.add_argument(
        "--stage-only",
        action="store_true",
        help="Stage, validate, and benchmark without changing the active pointer.",
    )
    parser.add_argument(
        "--activate-snapshot",
        default="",
        help="Atomically activate a snapshot from an independently verified staged receipt.",
    )
    parser.add_argument(
        "--restore-activation",
        default="",
        help="Idempotently compare-and-restore from a private activation rollback token.",
    )
    parser.add_argument("--staged-receipt", default="")
    parser.add_argument("--activation-authority", default="")
    parser.add_argument(
        "--workflow-head-sha",
        default=os.getenv("PROPERTYQUARRY_WORKFLOW_HEAD_SHA") or "",
    )
    parser.add_argument(
        "--workflow-run-id",
        default=os.getenv("PROPERTYQUARRY_WORKFLOW_RUN_ID") or "",
    )
    parser.add_argument(
        "--workflow-run-attempt",
        default=os.getenv("PROPERTYQUARRY_WORKFLOW_RUN_ATTEMPT") or "",
    )
    parser.add_argument("--rollback-receipt", default="")
    parser.add_argument(
        "--fixture-mode",
        action="store_true",
        help="Allow a prefetched fixture export; fixture receipts are never launch-authoritative.",
    )
    parser.add_argument("--write", required=True)
    args = parser.parse_args()
    database_url = _text(os.getenv(str(args.database_url_env)))
    if not database_url:
        print(
            f"missing database URL environment variable: {args.database_url_env}",
            file=sys.stderr,
        )
        return 2
    try:
        repository = PostgresPropertyEvidenceOverlayRepository(database_url)
        requested_snapshot_id = _text(args.activate_snapshot)
        requested_restore_snapshot_id = _text(args.restore_activation)
        if requested_snapshot_id and requested_restore_snapshot_id:
            raise ValueError("activation and restore operations are mutually exclusive")
        max_age_hours = 0.0
        max_query_ms = 0.0
        timeout_seconds = 0.0
        expected_teable_origin = ""
        expected_teable_base_id_sha256 = ""
        if not requested_restore_snapshot_id:
            max_age_hours = _positive_finite(
                args.max_age_hours,
                name="max_age_hours",
            )
            max_query_ms = _positive_finite(
                args.max_query_ms,
                name="max_query_ms",
                maximum=MAX_ACCEPTED_QUERY_BUDGET_MS,
            )
            timeout_seconds = _positive_finite(
                args.teable_timeout_seconds,
                name="teable_timeout_seconds",
            )
            expected_teable_origin = _text(
                os.getenv(str(args.expected_teable_origin_env))
            )
            expected_teable_base_id_sha256 = _text(
                os.getenv(str(args.expected_teable_base_id_sha256_env))
            ).casefold()
            if not _safe_https_origin(expected_teable_origin):
                raise ValueError(
                    "expected Teable origin environment variable is invalid"
                )
            if not re.fullmatch(r"[0-9a-f]{64}", expected_teable_base_id_sha256):
                raise ValueError(
                    "expected Teable base digest environment variable is invalid"
                )
        if requested_restore_snapshot_id:
            if args.stage_only or args.fixture_mode or _text(args.teable_export):
                raise ValueError(
                    "activation restore cannot be combined with staging inputs"
                )
            rollback_receipt_path = Path(_text(args.rollback_receipt))
            if not _text(args.rollback_receipt):
                raise ValueError("rollback_receipt is required for activation restore")
            _require_mode_600(
                rollback_receipt_path,
                name="activation rollback receipt",
            )
            receipt = restore_activation_from_token(
                token=_load_object(rollback_receipt_path),
                repository=repository,
                expected_candidate_sha=str(args.candidate_sha),
                expected_activated_snapshot_id=requested_restore_snapshot_id,
            )
            if receipt.get("status") == "restored":
                _atomic_write(rollback_receipt_path, receipt)
        elif requested_snapshot_id:
            if args.stage_only or args.fixture_mode or _text(args.teable_export):
                raise ValueError(
                    "snapshot activation cannot be combined with staging or fixture inputs"
                )
            if not re.fullmatch(r"[0-9a-f]{64}", requested_snapshot_id):
                raise ValueError("activate_snapshot must be a lowercase SHA-256 digest")
            staged_receipt_path = Path(_text(args.staged_receipt))
            if not _text(args.staged_receipt):
                raise ValueError("staged_receipt is required for snapshot activation")
            rollback_receipt_path = Path(_text(args.rollback_receipt))
            if not _text(args.rollback_receipt):
                raise ValueError("rollback_receipt is required for snapshot activation")
            activation_authority_path = Path(_text(args.activation_authority))
            if not _text(args.activation_authority):
                raise ValueError(
                    "activation_authority is required for snapshot activation"
                )
            staged_receipt, staged_receipt_sha256 = _stable_private_json_object(
                staged_receipt_path,
                name="staged overlay receipt",
            )
            activation_authority, activation_authority_sha256 = (
                _stable_private_json_object(
                    activation_authority_path,
                    name="activation authority",
                )
            )
            workflow_binding = {
                "head_sha": _text(args.workflow_head_sha).casefold(),
                "run_id": _text(args.workflow_run_id),
                "run_attempt": _text(args.workflow_run_attempt),
            }
            activation_now = _utc_now()
            staged_failures = verify_receipt(
                staged_receipt,
                expected_candidate_sha=str(args.candidate_sha),
                max_age_hours=max_age_hours,
                expected_teable_origin=expected_teable_origin,
                expected_teable_base_id_sha256=expected_teable_base_id_sha256,
                expected_phase="staged",
                now=activation_now,
            )
            if staged_failures:
                raise ValueError("staged receipt failed activation preflight")
            authority_failures = verify_activation_authority(
                activation_authority,
                expected_candidate_sha=str(args.candidate_sha),
                expected_snapshot_id=requested_snapshot_id,
                expected_staged_receipt_sha256=staged_receipt_sha256,
                workflow_head_sha=workflow_binding["head_sha"],
                workflow_run_id=workflow_binding["run_id"],
                workflow_run_attempt=workflow_binding["run_attempt"],
                expected_teable_origin=expected_teable_origin,
                expected_teable_base_id_sha256=expected_teable_base_id_sha256,
                now=activation_now,
            )
            if authority_failures:
                raise ValueError("activation authority failed current-run preflight")
            rollback_token = build_activation_rollback_token(
                staged_receipt=staged_receipt,
                expected_candidate_sha=str(args.candidate_sha),
                staged_receipt_sha256=staged_receipt_sha256,
                activation_authority_sha256=activation_authority_sha256,
                authorized_workflow=workflow_binding,
                now=activation_now,
            )
            _atomic_write(rollback_receipt_path, rollback_token)
            receipt = activate_staged_receipt(
                receipt=staged_receipt,
                repository=repository,
                snapshot_id=requested_snapshot_id,
                expected_candidate_sha=str(args.candidate_sha),
                max_age_hours=max_age_hours,
                expected_teable_origin=expected_teable_origin,
                expected_teable_base_id_sha256=expected_teable_base_id_sha256,
                activation_authority_sha256=activation_authority_sha256,
                staged_receipt_sha256=staged_receipt_sha256,
                authorized_workflow=workflow_binding,
                now=activation_now,
            )
            receipt_activation = _object(receipt.get("activation"))
            if receipt.get("status") == "pass":
                rollback_token.update(
                    {
                        "status": "armed",
                        "armed_at": _iso(_utc_now()),
                        "active_receipt_sha256": _sha256(receipt),
                    }
                )
            elif receipt_activation.get("rollback_performed") is True:
                rollback_token.update(
                    {
                        "status": "restored",
                        "restored_at": _iso(_utc_now()),
                        "restore_performed": True,
                        "failures": list(receipt.get("failures") or []),
                    }
                )
            _atomic_write(rollback_receipt_path, rollback_token)
        else:
            if not args.stage_only:
                raise ValueError(
                    "launch ingestion requires --stage-only before explicit activation"
                )
            if _text(args.teable_export) and not args.fixture_mode:
                raise ValueError(
                    "prefetched Teable exports are forbidden in launch mode"
                )
            if args.fixture_mode and not _text(args.teable_export):
                raise ValueError("fixture mode requires --teable-export")
            registry = _load_object(REGISTRY_PATH)
            if args.fixture_mode:
                export = _load_object(Path(args.teable_export))
            else:
                teable_base_url = _text(os.getenv(str(args.teable_base_url_env)))
                if _https_base_origin(teable_base_url) != _safe_https_origin(
                    expected_teable_origin
                ):
                    raise ValueError(
                        "Teable request origin does not match independent launch authority"
                    )
                export = fetch_teable_export(
                    base_url=teable_base_url,
                    api_key=_text(os.getenv(str(args.teable_api_key_env))),
                    base_id=_text(os.getenv(str(args.teable_base_id_env))),
                    registry=registry,
                    timeout_seconds=timeout_seconds,
                )
            plan = build_ingestion_plan(
                export=export,
                registry=registry,
                candidate_sha=str(args.candidate_sha),
                max_age_hours=max_age_hours,
                expected_teable_origin=expected_teable_origin,
                expected_teable_base_id_sha256=expected_teable_base_id_sha256,
                launch_mode=not args.fixture_mode,
            )
            receipt = execute_ingestion(
                plan=plan,
                repository=repository,
                candidate_sha=str(args.candidate_sha),
                max_query_ms=max_query_ms,
                stage_only=True,
            )
            if not args.fixture_mode and receipt.get("status") == "pass":
                verification_failures = verify_receipt(
                    receipt,
                    expected_candidate_sha=str(args.candidate_sha),
                    max_age_hours=max_age_hours,
                    expected_teable_origin=expected_teable_origin,
                    expected_teable_base_id_sha256=expected_teable_base_id_sha256,
                    expected_phase="staged",
                )
                if verification_failures:
                    repository.discard_staged_snapshot(
                        _text(receipt.get("snapshot_id"))
                    )
                    receipt["status"] = "fail"
                    receipt["failures"] = verification_failures
                    receipt_activation = _object(receipt.get("activation"))
                    receipt_activation["candidate_discarded"] = True
                    receipt["activation"] = receipt_activation
    except Exception as exc:
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "status": "fail",
            "generated_at": _iso(_utc_now()),
            "candidate_sha": str(args.candidate_sha),
            "failures": [type(exc).__name__],
        }
    _atomic_write(Path(args.write), receipt)
    if receipt.get("status") not in {"pass", "restored"}:
        for failure in list(receipt.get("failures") or []):
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": receipt.get("status"),
                "receipt": str(args.write),
                "snapshot_id": receipt.get("snapshot_id"),
                "phase": _object(receipt.get("activation")).get("phase"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
