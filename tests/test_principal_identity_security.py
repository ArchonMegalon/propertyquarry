from __future__ import annotations

import hashlib
import ipaddress
from types import SimpleNamespace

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.testclient import TestClient
import pytest

from app.propertyquarry_release_probe import (
    PROPERTYQUARRY_RELEASE_PROBE_NONCE_HEADER,
    PROPERTYQUARRY_RELEASE_PROBE_NONCE_SHA256_RESPONSE_HEADER,
    PROPERTYQUARRY_RELEASE_PROBE_SIGNATURE_HEADER,
    PROPERTYQUARRY_RELEASE_PROBE_TIMESTAMP_HEADER,
    PROPERTYQUARRY_RELEASE_PROBE_PROVIDER_CATALOG_PATH,
    propertyquarry_release_probe_signature,
)
from app.api.dependencies import RequestContext, get_request_context, require_runtime_metrics_auth
from app.api.errors import install_error_handlers
from app.api.principal_identity import (
    PRINCIPAL_ASSERTION_AUDIENCE_HEADER,
    PRINCIPAL_ASSERTION_NONCE_HEADER,
    PRINCIPAL_ASSERTION_SIGNATURE_HEADER,
    PRINCIPAL_ASSERTION_TIMESTAMP_HEADER,
    PRINCIPAL_ID_HEADER,
    VERIFIED_PRINCIPAL_ASSERTION_STATE_KEY,
    PrincipalIdentityMiddleware,
    PrincipalIdentityPolicy,
    VerifiedPrincipalAssertion,
    principal_assertion_signature,
)
from app.services.cloudflare_access import CloudflareAccessIdentity
from app.settings import RuntimeProfile


_NOW = 1_800_000_000
_ASSERTION_SECRET = "edge-assertion-secret-that-is-separate-0001"
_ASSERTION_AUDIENCE = "propertyquarry-api"
_RELEASE_PROBE_SECRET = "propertyquarry-release-probe-secret-0000000001"
_RELEASE_PROBE_PRINCIPAL_ID = "propertyquarry-release-probe"
_RELEASE_PROBE_ORIGIN = "http://testserver"
_RELEASE_PROBE_RESEARCH_PATH = "/app/research/perf-candidate-1020"
_RELEASE_PROBE_RESEARCH_QUERY = "run_id=run-gold-mobile"
_RELEASE_PROBE_RESEARCH_ROUTE = (
    f"{_RELEASE_PROBE_RESEARCH_PATH}?{_RELEASE_PROBE_RESEARCH_QUERY}"
)
_RELEASE_PROBE_SHORTLIST_RUN_PATH = "/app/shortlist/run/run-gold-mobile"
_LOOPBACK_NETWORKS = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))


def _container(*, mode: str, api_token: str = "shared-token") -> SimpleNamespace:
    auth_mode = "token" if api_token else "anonymous_dev"
    principal_source = "verified_identity" if mode == "prod" else "caller_header_or_default"
    settings = SimpleNamespace(
        auth=SimpleNamespace(
            api_token=api_token,
            default_principal_id="safe-default",
            signing_secret="workspace-signing-secret",
            allow_loopback_no_auth=False,
            cf_access_team_domain="",
            cf_access_audiences=(),
            cf_access_certs_url="",
        ),
        runtime=SimpleNamespace(mode=mode),
        storage=SimpleNamespace(backend="memory", database_url=""),
        database_url="",
    )
    profile = RuntimeProfile(
        mode=mode,
        storage_backend="postgres" if mode == "prod" else "memory",
        durability="durable" if mode == "prod" else "ephemeral",
        auth_mode=auth_mode,
        principal_source=principal_source,
        database_required=mode == "prod",
        database_configured=mode == "prod",
        source_backend="postgres" if mode == "prod" else "memory",
    )
    return SimpleNamespace(settings=settings, runtime_profile=profile)


def _policy(
    *,
    mode: str = "prod",
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = _LOOPBACK_NETWORKS,
    assertions_enabled: bool = True,
    release_probe_enabled: bool = False,
    release_probe_origin: str = _RELEASE_PROBE_ORIGIN,
    release_probe_research_detail_route: str = _RELEASE_PROBE_RESEARCH_ROUTE,
    release_probe_shortlist_run_path: str = _RELEASE_PROBE_SHORTLIST_RUN_PATH,
) -> PrincipalIdentityPolicy:
    return PrincipalIdentityPolicy(
        runtime_mode=mode,
        trusted_proxy_cidrs=networks,
        assertion_secret=_ASSERTION_SECRET if assertions_enabled else "",
        assertion_audience=_ASSERTION_AUDIENCE if assertions_enabled else "",
        assertion_max_age_seconds=120,
        assertion_future_skew_seconds=15,
        assertion_nonce_capacity=256,
        release_probe_secret=_RELEASE_PROBE_SECRET if release_probe_enabled else "",
        release_probe_principal_id=_RELEASE_PROBE_PRINCIPAL_ID if release_probe_enabled else "",
        release_probe_origin=release_probe_origin if release_probe_enabled else "",
        release_probe_research_detail_route=(
            release_probe_research_detail_route if release_probe_enabled else ""
        ),
        release_probe_shortlist_run_path=(
            release_probe_shortlist_run_path if release_probe_enabled else ""
        ),
    )


def _identity_app(
    *,
    mode: str = "prod",
    api_token: str = "shared-token",
    policy: PrincipalIdentityPolicy | None = None,
) -> FastAPI:
    app = FastAPI()
    app.state.container = _container(mode=mode, api_token=api_token)
    app.add_middleware(
        PrincipalIdentityMiddleware,
        policy=policy or _policy(mode=mode),
        clock=lambda: float(_NOW),
    )
    install_error_handlers(app)

    @app.get("/who")
    @app.get(_RELEASE_PROBE_RESEARCH_PATH)
    @app.get(_RELEASE_PROBE_SHORTLIST_RUN_PATH)
    @app.get(PROPERTYQUARRY_RELEASE_PROBE_PROVIDER_CATALOG_PATH)
    def who(
        request: Request,
        response: Response,
        context: RequestContext = Depends(get_request_context),
    ) -> dict[str, object]:
        response.headers["cache-control"] = "public, max-age=86400"
        response.headers[
            PROPERTYQUARRY_RELEASE_PROBE_NONCE_SHA256_RESPONSE_HEADER
        ] = "f" * 64
        return {
            "principal_id": context.principal_id,
            "auth_source": context.auth_source,
            "operator_id": context.operator_id,
            "stripped": list(getattr(request.state, "identity_headers_stripped", ())),
        }

    @app.get("/system", dependencies=[Depends(require_runtime_metrics_auth)])
    def system() -> dict[str, bool]:
        return {"ok": True}

    return app


def _assertion_headers(
    *,
    principal_id: str,
    nonce: str,
    timestamp: int = _NOW,
    audience: str = _ASSERTION_AUDIENCE,
    method: str = "GET",
    path: str = "/who",
    query_string: str = "",
) -> dict[str, str]:
    signature = principal_assertion_signature(
        secret=_ASSERTION_SECRET,
        method=method,
        path=path,
        query_string=query_string,
        principal_id=principal_id,
        timestamp=timestamp,
        nonce=nonce,
        audience=audience,
    )
    return {
        PRINCIPAL_ID_HEADER: principal_id,
        PRINCIPAL_ASSERTION_TIMESTAMP_HEADER: str(timestamp),
        PRINCIPAL_ASSERTION_NONCE_HEADER: nonce,
        PRINCIPAL_ASSERTION_AUDIENCE_HEADER: audience,
        PRINCIPAL_ASSERTION_SIGNATURE_HEADER: signature,
    }


def _release_probe_headers(
    *,
    nonce: str,
    timestamp: int = _NOW,
    method: str = "GET",
    path: str = _RELEASE_PROBE_RESEARCH_PATH,
    query_string: str = _RELEASE_PROBE_RESEARCH_QUERY,
    origin: str = _RELEASE_PROBE_ORIGIN,
) -> dict[str, str]:
    signature = propertyquarry_release_probe_signature(
        secret=_RELEASE_PROBE_SECRET,
        method=method,
        path=path,
        query_string=query_string,
        timestamp=timestamp,
        nonce=nonce,
        origin=origin,
    )
    return {
        PROPERTYQUARRY_RELEASE_PROBE_TIMESTAMP_HEADER: str(timestamp),
        PROPERTYQUARRY_RELEASE_PROBE_NONCE_HEADER: nonce,
        PROPERTYQUARRY_RELEASE_PROBE_SIGNATURE_HEADER: signature,
    }


def _request(*, headers: dict[str, str], state: dict[str, object] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/who",
            "query_string": b"",
            "headers": [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in headers.items()
            ],
            "client": ("127.0.0.1", 50000),
            "state": dict(state or {}),
        }
    )


def test_shared_production_bearer_cannot_select_or_cross_tenants_with_headers() -> None:
    app = _identity_app(policy=_policy(assertions_enabled=False))
    client = TestClient(app, client=("127.0.0.1", 50000))

    for attempted_principal in ("tenant-a", "tenant-b"):
        response = client.get(
            "/who",
            headers={
                "Authorization": "Bearer shared-token",
                "X-EA-Principal-ID": attempted_principal,
                "X-Principal-ID": attempted_principal,
                "X-EA-Tenant-ID": attempted_principal,
                "X-EA-Operator-ID": "spoofed-operator",
            },
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "principal_required"

    system_response = client.get(
        "/system",
        headers={
            "Authorization": "Bearer shared-token",
            "X-EA-Principal-ID": "tenant-b",
        },
    )
    assert system_response.status_code == 200
    assert system_response.json() == {"ok": True}


def test_valid_edge_assertion_wins_over_related_spoof_headers_and_is_stripped() -> None:
    app = _identity_app()
    client = TestClient(app, client=("127.0.0.1", 50000))
    headers = _assertion_headers(
        principal_id="tenant-a",
        nonce="nonce-edge-tenant-a-0001",
    )
    headers.update(
        {
            "Authorization": "Bearer shared-token",
            "X-Principal-ID": "tenant-b",
            "X-EA-Tenant-ID": "tenant-b",
            "X-EA-Operator-ID": "spoofed-operator",
        }
    )

    response = client.get("/who", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["principal_id"] == "tenant-a"
    assert body["auth_source"] == "edge_principal_assertion"
    assert body["operator_id"] == ""
    assert "x-ea-principal-id" in body["stripped"]
    assert "x-principal-id" in body["stripped"]
    assert "x-ea-operator-id" in body["stripped"]


def test_edge_assertion_nonce_replay_is_rejected_with_security_envelope() -> None:
    app = _identity_app()
    client = TestClient(app, client=("127.0.0.1", 50000))
    headers = _assertion_headers(
        principal_id="tenant-replay",
        nonce="nonce-replay-guard-0001",
    )

    assert client.get("/who", headers=headers).status_code == 200
    replay = client.get("/who", headers=headers)

    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "principal_assertion_invalid"
    assert replay.json()["error"]["details"]["reason"] == "nonce_replayed_or_capacity_exhausted"
    assert replay.headers["x-content-type-options"] == "nosniff"
    assert replay.headers["x-correlation-id"] == replay.json()["error"]["correlation_id"]


@pytest.mark.parametrize(
    ("header_updates", "expected_reason"),
    [
        ({PRINCIPAL_ASSERTION_AUDIENCE_HEADER: "wrong-audience"}, "audience_invalid"),
        ({PRINCIPAL_ASSERTION_TIMESTAMP_HEADER: str(_NOW - 121)}, "timestamp_expired"),
        ({PRINCIPAL_ASSERTION_SIGNATURE_HEADER: "0" * 64}, "signature_invalid"),
    ],
)
def test_edge_assertion_rejects_bad_audience_timestamp_and_signature(
    header_updates: dict[str, str],
    expected_reason: str,
) -> None:
    app = _identity_app()
    client = TestClient(app, client=("127.0.0.1", 50000))
    headers = _assertion_headers(
        principal_id="tenant-invalid",
        nonce=f"nonce-invalid-{expected_reason}-0001",
    )
    headers.update(header_updates)

    response = client.get("/who", headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["details"]["reason"] == expected_reason


def test_edge_assertion_signature_is_bound_to_query_string() -> None:
    app = _identity_app()
    client = TestClient(app, client=("127.0.0.1", 50000))
    headers = _assertion_headers(
        principal_id="tenant-query-bound",
        nonce="nonce-query-bound-0001",
        query_string="",
    )

    response = client.get("/who?view=other", headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["details"]["reason"] == "signature_invalid"


def test_valid_release_probe_uses_fixed_principal_and_strips_identity_and_probe_headers() -> None:
    app = _identity_app(policy=_policy(release_probe_enabled=True))
    client = TestClient(app, client=("127.0.0.1", 50000))
    headers = _release_probe_headers(nonce="release-probe-valid-fixed-principal-0001")

    response = client.get(_RELEASE_PROBE_RESEARCH_ROUTE, headers=headers)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers[
        PROPERTYQUARRY_RELEASE_PROBE_NONCE_SHA256_RESPONSE_HEADER
    ] == hashlib.sha256(
        b"propertyquarry-release-probe\0release-probe-valid-fixed-principal-0001"
    ).hexdigest()
    body = response.json()
    assert body["principal_id"] == _RELEASE_PROBE_PRINCIPAL_ID
    assert body["auth_source"] == "propertyquarry_release_probe"
    assert body["operator_id"] == ""
    stripped = set(body["stripped"])
    assert {
        PROPERTYQUARRY_RELEASE_PROBE_TIMESTAMP_HEADER.lower(),
        PROPERTYQUARRY_RELEASE_PROBE_NONCE_HEADER.lower(),
        PROPERTYQUARRY_RELEASE_PROBE_SIGNATURE_HEADER.lower(),
    }.issubset(stripped)


def test_release_probe_nonce_replay_is_rejected() -> None:
    app = _identity_app(policy=_policy(release_probe_enabled=True))
    client = TestClient(app, client=("127.0.0.1", 50000))
    headers = {
        **_release_probe_headers(nonce="release-probe-replay-guard-0001"),
    }

    assert client.get(_RELEASE_PROBE_RESEARCH_ROUTE, headers=headers).status_code == 200
    replay = client.get(_RELEASE_PROBE_RESEARCH_ROUTE, headers=headers)

    assert replay.status_code == 401
    assert replay.json()["error"]["details"]["reason"] == (
        "release_probe_nonce_replayed_or_capacity_exhausted"
    )


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("method_binding", "release_probe_signature_invalid"),
        ("path_binding", "release_probe_signature_invalid"),
        ("query_binding", "release_probe_signature_invalid"),
        ("origin_binding", "release_probe_origin_forbidden"),
        ("signature", "release_probe_signature_invalid"),
        ("timestamp_expired", "release_probe_timestamp_expired"),
        ("timestamp_future", "release_probe_timestamp_in_future"),
        ("timestamp_invalid", "release_probe_timestamp_invalid"),
        ("nonce_invalid", "release_probe_nonce_invalid"),
    ],
)
def test_release_probe_rejects_bad_binding_signature_and_time(
    case: str,
    expected_reason: str,
) -> None:
    route = _RELEASE_PROBE_RESEARCH_ROUTE
    request_path = route
    policy = _policy(release_probe_enabled=True)
    headers = _release_probe_headers(nonce=f"release-probe-{case}-0001")
    if case == "method_binding":
        headers = _release_probe_headers(
            nonce="release-probe-method-binding-0001",
            method="HEAD",
        )
    elif case == "path_binding":
        request_path = _RELEASE_PROBE_SHORTLIST_RUN_PATH
    elif case == "query_binding":
        headers = _release_probe_headers(
            nonce="release-probe-query-binding-0001",
            query_string="run_id=another-release-probe-run",
        )
    elif case == "origin_binding":
        headers["Host"] = "other.example.test"
    elif case == "signature":
        headers[PROPERTYQUARRY_RELEASE_PROBE_SIGNATURE_HEADER] = "0" * 64
    elif case == "timestamp_expired":
        headers = _release_probe_headers(
            nonce="release-probe-expired-time-0001",
            timestamp=_NOW - 121,
        )
    elif case == "timestamp_future":
        headers = _release_probe_headers(
            nonce="release-probe-future-time-0001",
            timestamp=_NOW + 16,
        )
    elif case == "timestamp_invalid":
        headers[PROPERTYQUARRY_RELEASE_PROBE_TIMESTAMP_HEADER] = "not-a-timestamp"
    elif case == "nonce_invalid":
        headers = _release_probe_headers(nonce="bad nonce")
    response = TestClient(
        _identity_app(policy=policy),
        client=("127.0.0.1", 50000),
    ).get(request_path, headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["details"]["reason"] == expected_reason


def test_release_probe_rejects_non_read_method_and_forbidden_path() -> None:
    client = TestClient(
        _identity_app(policy=_policy(release_probe_enabled=True)),
        client=("127.0.0.1", 50000),
    )
    post_headers = {
        **_release_probe_headers(
            nonce="release-probe-post-method-0001",
            method="POST",
        ),
    }
    forbidden_headers = {
        **_release_probe_headers(
            nonce="release-probe-forbidden-path-0001",
            path="/not-configured",
        ),
    }

    post = client.post(_RELEASE_PROBE_RESEARCH_ROUTE, headers=post_headers)
    forbidden = client.get("/not-configured", headers=forbidden_headers)

    assert post.status_code == 401
    assert post.json()["error"]["details"]["reason"] == "release_probe_read_only"
    assert forbidden.status_code == 401
    assert forbidden.json()["error"]["details"]["reason"] == "release_probe_path_forbidden"


def test_release_probe_allows_only_canonical_read_only_provider_catalog_queries() -> None:
    client = TestClient(
        _identity_app(policy=_policy(release_probe_enabled=True)),
        client=("127.0.0.1", 50000),
    )
    allowed_query = "country=AT"
    allowed = client.get(
        f"{PROPERTYQUARRY_RELEASE_PROBE_PROVIDER_CATALOG_PATH}?{allowed_query}",
        headers=_release_probe_headers(
            nonce="release-probe-provider-catalog-allowed-0001",
            path=PROPERTYQUARRY_RELEASE_PROBE_PROVIDER_CATALOG_PATH,
            query_string=allowed_query,
        ),
    )
    lower_query = "country=at"
    lower = client.get(
        f"{PROPERTYQUARRY_RELEASE_PROBE_PROVIDER_CATALOG_PATH}?{lower_query}",
        headers=_release_probe_headers(
            nonce="release-probe-provider-catalog-lower-0001",
            path=PROPERTYQUARRY_RELEASE_PROBE_PROVIDER_CATALOG_PATH,
            query_string=lower_query,
        ),
    )
    extra_query = "country=AT&include=secrets"
    extra = client.get(
        f"{PROPERTYQUARRY_RELEASE_PROBE_PROVIDER_CATALOG_PATH}?{extra_query}",
        headers=_release_probe_headers(
            nonce="release-probe-provider-catalog-extra-0001",
            path=PROPERTYQUARRY_RELEASE_PROBE_PROVIDER_CATALOG_PATH,
            query_string=extra_query,
        ),
    )

    assert allowed.status_code == 200
    assert allowed.json()["principal_id"] == _RELEASE_PROBE_PRINCIPAL_ID
    assert allowed.json()["auth_source"] == "propertyquarry_release_probe"
    assert allowed.headers["cache-control"] == "no-store"
    assert lower.status_code == 401
    assert lower.json()["error"]["details"]["reason"] == "release_probe_path_forbidden"
    assert extra.status_code == 401
    assert extra.json()["error"]["details"]["reason"] == "release_probe_path_forbidden"


def test_release_probe_rejects_incomplete_duplicated_conflicting_and_unconfigured_headers() -> None:
    configured = TestClient(
        _identity_app(policy=_policy(release_probe_enabled=True)),
        client=("127.0.0.1", 50000),
    )
    unconfigured = TestClient(
        _identity_app(policy=_policy(release_probe_enabled=False)),
        client=("127.0.0.1", 50000),
    )
    incomplete = configured.get(
        _RELEASE_PROBE_RESEARCH_ROUTE,
        headers={
            PROPERTYQUARRY_RELEASE_PROBE_TIMESTAMP_HEADER: str(_NOW),
        },
    )
    complete_headers = {
        **_release_probe_headers(nonce="release-probe-unconfigured-0001"),
    }
    disabled = unconfigured.get(_RELEASE_PROBE_RESEARCH_ROUTE, headers=complete_headers)
    conflict_headers = {
        **_release_probe_headers(nonce="release-probe-conflict-0001"),
        "Authorization": "Bearer shared-token",
    }
    conflict = configured.get(_RELEASE_PROBE_RESEARCH_ROUTE, headers=conflict_headers)
    session_conflict = configured.get(
        _RELEASE_PROBE_RESEARCH_ROUTE,
        headers={
            **_release_probe_headers(nonce="release-probe-session-conflict-0001"),
            "Cookie": "propertyquarry_session=must-not-mix",
        },
    )
    duplicate_headers = [
        (PROPERTYQUARRY_RELEASE_PROBE_TIMESTAMP_HEADER, str(_NOW)),
        (PROPERTYQUARRY_RELEASE_PROBE_TIMESTAMP_HEADER, str(_NOW)),
        (PROPERTYQUARRY_RELEASE_PROBE_NONCE_HEADER, "release-probe-duplicate-0001"),
        (PROPERTYQUARRY_RELEASE_PROBE_SIGNATURE_HEADER, "0" * 64),
    ]
    duplicated = configured.get(_RELEASE_PROBE_RESEARCH_ROUTE, headers=duplicate_headers)

    assert incomplete.status_code == 401
    assert incomplete.json()["error"]["details"]["reason"] == (
        "release_probe_headers_incomplete_or_duplicated"
    )
    assert disabled.status_code == 401
    assert disabled.json()["error"]["details"]["reason"] == "release_probe_not_configured"
    assert conflict.status_code == 401
    assert conflict.json()["error"]["details"]["reason"] == "release_probe_auth_conflict"
    assert session_conflict.status_code == 401
    assert session_conflict.json()["error"]["details"]["reason"] == "release_probe_auth_conflict"
    assert duplicated.status_code == 401
    assert duplicated.json()["error"]["details"]["reason"] == (
        "release_probe_headers_incomplete_or_duplicated"
    )


def test_cloudflare_access_and_workspace_session_precede_edge_assertion() -> None:
    edge = VerifiedPrincipalAssertion(
        principal_id="edge-tenant",
        audience=_ASSERTION_AUDIENCE,
        nonce_hash="nonce-hash",
        issued_at=_NOW,
    )
    cloudflare = CloudflareAccessIdentity(
        principal_id="cf-tenant",
        email="user@example.com",
        subject="subject-1",
        display_name="User",
        issuer="https://example.cloudflareaccess.com",
        idp_name="google",
        audiences=("cf-audience",),
        claims={},
    )
    container = _container(mode="prod")

    access_context = get_request_context(
        _request(
            headers={},
            state={
                VERIFIED_PRINCIPAL_ASSERTION_STATE_KEY: edge,
                "cloudflare_access_identity": cloudflare,
            },
        ),
        container=container,
    )
    session_context = get_request_context(
        _request(
            headers={},
            state={
                VERIFIED_PRINCIPAL_ASSERTION_STATE_KEY: edge,
                "workspace_access_session_payload": {
                    "principal_id": "session-tenant",
                    "role": "principal",
                    "email": "session@example.com",
                },
            },
        ),
        container=container,
    )

    assert access_context.principal_id == "cf-tenant"
    assert access_context.auth_source == "cloudflare_access"
    assert session_context.principal_id == "session-tenant"
    assert session_context.auth_source == "workspace_access_session"


def test_proxy_chain_cannot_make_untrusted_peer_authoritative() -> None:
    trusted_networks = (ipaddress.ip_network("10.0.0.0/8"),)
    policy = _policy(networks=trusted_networks)
    headers = _assertion_headers(
        principal_id="proxy-tenant",
        nonce="nonce-proxy-chain-0001",
    )
    headers["X-Forwarded-For"] = "198.51.100.4, 10.0.0.3"

    untrusted = TestClient(
        _identity_app(policy=policy),
        client=("203.0.113.9", 50000),
    ).get("/who", headers=headers)
    trusted_headers = _assertion_headers(
        principal_id="proxy-tenant",
        nonce="nonce-proxy-chain-0002",
    )
    trusted_headers["X-Forwarded-For"] = "198.51.100.4, 10.0.0.3"
    trusted = TestClient(
        _identity_app(policy=policy),
        client=("10.0.0.4", 50000),
    ).get("/who", headers=trusted_headers)

    assert untrusted.status_code == 401
    assert untrusted.json()["error"]["details"]["reason"] == "untrusted_proxy_peer"
    assert trusted.status_code == 200
    assert trusted.json()["principal_id"] == "proxy-tenant"


@pytest.mark.parametrize(
    "legacy_env_name",
    (
        "EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER",
        "EA_ALLOW_AUTHENTICATED_PRINCIPAL_HEADER",
        "EA_TRUST_API_TOKEN_PRINCIPAL_HEADER",
    ),
)
def test_legacy_trust_flags_cannot_restore_prod_override_but_dev_loopback_remains(
    monkeypatch: pytest.MonkeyPatch,
    legacy_env_name: str,
) -> None:
    monkeypatch.setenv(legacy_env_name, "1")
    container = _container(mode="prod")
    with pytest.raises(HTTPException, match="principal_required"):
        get_request_context(
            _request(
                headers={
                    "Authorization": "Bearer shared-token",
                    "X-EA-Principal-ID": "legacy-spoof",
                }
            ),
            container=container,
        )

    dev_app = _identity_app(
        mode="dev",
        api_token="",
        policy=_policy(mode="dev", assertions_enabled=False),
    )
    loopback = TestClient(dev_app, client=("127.0.0.1", 50000)).get(
        "/who",
        headers={"X-EA-Principal-ID": "dev-loopback"},
    )
    remote = TestClient(dev_app, client=("203.0.113.7", 50000)).get(
        "/who",
        headers={"X-EA-Principal-ID": "remote-spoof"},
    )

    assert loopback.status_code == 200
    assert loopback.json()["principal_id"] == "dev-loopback"
    assert remote.status_code == 200
    assert remote.json()["principal_id"] == "safe-default"


def test_main_app_orders_identity_sanitization_before_ingress_context_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_RUNTIME_MODE", "dev")
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.setenv("EA_API_TOKEN", "")
    monkeypatch.delenv("EA_EDGE_PRINCIPAL_ASSERTION_SECRET", raising=False)
    monkeypatch.delenv("EA_EDGE_PRINCIPAL_ASSERTION_AUDIENCE", raising=False)
    from app.api.app import create_app

    app = create_app()
    middleware_classes = [middleware.cls for middleware in app.user_middleware]

    assert middleware_classes.index(PrincipalIdentityMiddleware) < next(
        index
        for index, middleware_class in enumerate(middleware_classes)
        if middleware_class.__name__ == "IngressAbuseMiddleware"
    )


def test_edge_assertion_configuration_requires_separate_complete_secret() -> None:
    with pytest.raises(RuntimeError, match="configuration_incomplete"):
        PrincipalIdentityPolicy.from_environ(
            runtime_mode="prod",
            trusted_proxy_cidrs=_LOOPBACK_NETWORKS,
            environ={"EA_EDGE_PRINCIPAL_ASSERTION_SECRET": _ASSERTION_SECRET},
        )
    with pytest.raises(RuntimeError, match="must_be_separate"):
        PrincipalIdentityPolicy.from_environ(
            runtime_mode="prod",
            trusted_proxy_cidrs=_LOOPBACK_NETWORKS,
            environ={
                "EA_EDGE_PRINCIPAL_ASSERTION_SECRET": _ASSERTION_SECRET,
                "EA_EDGE_PRINCIPAL_ASSERTION_AUDIENCE": _ASSERTION_AUDIENCE,
                "EA_API_TOKEN": _ASSERTION_SECRET,
            },
        )


def test_release_probe_configuration_rejects_incomplete_short_and_invalid_identity() -> None:
    complete = {
        "PROPERTYQUARRY_RELEASE_PROBE_SECRET": _RELEASE_PROBE_SECRET,
        "PROPERTYQUARRY_RELEASE_PROBE_PRINCIPAL_ID": _RELEASE_PROBE_PRINCIPAL_ID,
        "PROPERTYQUARRY_RELEASE_PROBE_ORIGIN": "https://propertyquarry.com",
        "PROPERTYQUARRY_RELEASE_PROBE_RESEARCH_DETAIL_ROUTE": _RELEASE_PROBE_RESEARCH_ROUTE,
        "PROPERTYQUARRY_RELEASE_PROBE_SHORTLIST_RUN_PATH": _RELEASE_PROBE_SHORTLIST_RUN_PATH,
    }

    with pytest.raises(RuntimeError, match="propertyquarry_release_probe_configuration_incomplete"):
        PrincipalIdentityPolicy.from_environ(
            runtime_mode="prod",
            trusted_proxy_cidrs=_LOOPBACK_NETWORKS,
            environ={"PROPERTYQUARRY_RELEASE_PROBE_SECRET": _RELEASE_PROBE_SECRET},
        )
    with pytest.raises(RuntimeError, match="propertyquarry_release_probe_secret_too_short"):
        PrincipalIdentityPolicy.from_environ(
            runtime_mode="prod",
            trusted_proxy_cidrs=_LOOPBACK_NETWORKS,
            environ={**complete, "PROPERTYQUARRY_RELEASE_PROBE_SECRET": "too-short"},
        )
    with pytest.raises(RuntimeError, match="propertyquarry_release_probe_principal_invalid"):
        PrincipalIdentityPolicy.from_environ(
            runtime_mode="prod",
            trusted_proxy_cidrs=_LOOPBACK_NETWORKS,
            environ={
                **complete,
                "PROPERTYQUARRY_RELEASE_PROBE_PRINCIPAL_ID": "caller selected principal",
            },
        )
    with pytest.raises(RuntimeError, match="propertyquarry_release_probe_origin_requires_https"):
        PrincipalIdentityPolicy.from_environ(
            runtime_mode="prod",
            trusted_proxy_cidrs=_LOOPBACK_NETWORKS,
            environ={
                **complete,
                "PROPERTYQUARRY_RELEASE_PROBE_ORIGIN": "http://propertyquarry.com",
            },
        )


def test_release_probe_secret_must_be_separate_from_other_auth_secrets() -> None:
    complete = {
        "PROPERTYQUARRY_RELEASE_PROBE_SECRET": _RELEASE_PROBE_SECRET,
        "PROPERTYQUARRY_RELEASE_PROBE_PRINCIPAL_ID": _RELEASE_PROBE_PRINCIPAL_ID,
        "PROPERTYQUARRY_RELEASE_PROBE_ORIGIN": "https://propertyquarry.com",
        "PROPERTYQUARRY_RELEASE_PROBE_RESEARCH_DETAIL_ROUTE": _RELEASE_PROBE_RESEARCH_ROUTE,
        "PROPERTYQUARRY_RELEASE_PROBE_SHORTLIST_RUN_PATH": _RELEASE_PROBE_SHORTLIST_RUN_PATH,
        "EA_API_TOKEN": _RELEASE_PROBE_SECRET,
    }

    with pytest.raises(
        RuntimeError,
        match="propertyquarry_release_probe_secret_must_be_separate:ea_api_token",
    ):
        PrincipalIdentityPolicy.from_environ(
            runtime_mode="prod",
            trusted_proxy_cidrs=_LOOPBACK_NETWORKS,
            environ=complete,
        )
