from __future__ import annotations

import os
import re


PROPERTYQUARRY_ANDROID_RELEASE_APP_ID = "com.myexternalbrain.propertyquarry"
PROPERTYQUARRY_ANDROID_PREVIEW_APP_ID = "com.myexternalbrain.propertyquarry.preview"
_CERTIFICATE_SHA256 = re.compile(r"(?:[0-9A-F]{2}:){31}[0-9A-F]{2}")
_CERTIFICATE_ENV_BY_APP_ID = {
    PROPERTYQUARRY_ANDROID_RELEASE_APP_ID: "PROPERTYQUARRY_ANDROID_APP_LINK_SHA256_CERTS",
    PROPERTYQUARRY_ANDROID_PREVIEW_APP_ID: "PROPERTYQUARRY_ANDROID_PREVIEW_APP_LINK_SHA256_CERTS",
}


def _certificate_fingerprints(name: str) -> tuple[str, ...]:
    values: list[str] = []
    for raw in str(os.environ.get(name) or "").split(","):
        compact = str(raw or "").strip().upper().replace(":", "")
        if len(compact) != 64 or any(character not in "0123456789ABCDEF" for character in compact):
            continue
        normalized = ":".join(
            compact[index : index + 2] for index in range(0, len(compact), 2)
        )
        if _CERTIFICATE_SHA256.fullmatch(normalized) and normalized not in values:
            values.append(normalized)
    return tuple(values[:8])


def propertyquarry_android_app_link_statements() -> list[dict[str, object]]:
    statements: list[dict[str, object]] = []
    for package_name, env_name in _CERTIFICATE_ENV_BY_APP_ID.items():
        fingerprints = _certificate_fingerprints(env_name)
        if not fingerprints:
            continue
        statements.append(
            {
                "relation": ["delegate_permission/common.handle_all_urls"],
                "target": {
                    "namespace": "android_app",
                    "package_name": package_name,
                    "sha256_cert_fingerprints": list(fingerprints),
                },
            }
        )
    return statements


def propertyquarry_android_app_links_readiness() -> dict[str, bool]:
    return {
        app_id: bool(_certificate_fingerprints(env_name))
        for app_id, env_name in _CERTIFICATE_ENV_BY_APP_ID.items()
    }


def propertyquarry_android_app_links_ready(
    *,
    app_id: str = PROPERTYQUARRY_ANDROID_RELEASE_APP_ID,
) -> bool:
    return propertyquarry_android_app_links_readiness().get(str(app_id or ""), False)
