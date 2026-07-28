from __future__ import annotations

import html
import logging
import re
import time
import urllib.parse
import uuid
from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging_utils import exception_log_fields, log_event
from app.observability import (
    bind_runtime_trace_context,
    get_runtime_metrics,
    new_server_trace_context,
    route_template,
    runtime_build_identity,
)

try:
    from psycopg import InterfaceError as PsycopgInterfaceError
    from psycopg import OperationalError as PsycopgOperationalError
except Exception:  # pragma: no cover - psycopg is optional in some test modes
    PsycopgInterfaceError = None
    PsycopgOperationalError = None


_LOG = logging.getLogger(__name__)

_BROWSER_MUTATION_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_BROWSER_MUTATION_PATH_PREFIXES = ("/app/", "/admin/")
_PROPERTYQUARRY_RAW_API_DOC_PATHS = {"/openapi.json", "/api/docs", "/api/redoc"}
_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

_DEFAULT_BROWSER_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'self'; "
        "img-src 'self' data: blob: https:; "
        "font-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://app.rybbit.io https://js.clickrank.ai; "
        "connect-src 'self' https: wss:; "
        "frame-src 'self' https:; "
        "media-src 'self' blob: https:; "
        "form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()",
    "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
}


def _correlation_id(request: Request) -> str:
    return str(getattr(request.state, "correlation_id", "") or uuid.uuid4())


def _request_correlation_id(request: Request) -> str:
    candidate = str(request.headers.get("x-correlation-id") or "").strip()
    if _CORRELATION_ID_RE.fullmatch(candidate):
        return candidate
    return str(uuid.uuid4())


def _apply_request_observability_headers(
    request: Request,
    response: Response,
) -> None:
    response.headers["x-correlation-id"] = _correlation_id(request)
    telemetry_context = getattr(request.state, "telemetry_context", None)
    if telemetry_context is not None:
        response.headers["traceparent"] = format_traceparent(telemetry_context)


def _error_payload(
    *,
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
) -> JSONResponse:
    safe_details = jsonable_encoder(
        details,
        custom_encoder={
            Exception: lambda value: str(value),
            type(ValueError()): lambda value: str(value),
        },
    )
    response = JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": str(code or "error"),
                "message": str(message or "request_failed"),
                "details": safe_details,
                "correlation_id": _correlation_id(request),
            }
        },
    )
    response.headers["Cache-Control"] = "no-store"
    _apply_request_observability_headers(request, response)
    return response


def _code_from_http(status_code: int, detail: Any) -> str:
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    if status_code == 400:
        return "bad_request"
    if status_code == 401:
        return "unauthorized"
    if status_code == 403:
        return "forbidden"
    if status_code == 404:
        return "not_found"
    if status_code == 409:
        return "conflict"
    if status_code == 422:
        return "validation_error"
    return "request_failed"


def _browser_auth_redirect(request: Request, *, code: str) -> RedirectResponse | None:
    if str(code or "").strip() != "auth_required":
        return None
    method = str(request.method or "").upper()
    if method not in {"GET", "HEAD"}:
        return None
    path = str(request.url.path or "").strip()
    if not path.startswith("/app") and not path.startswith("/admin"):
        return None
    if path.startswith("/app/api") or path.startswith("/admin/api"):
        return None
    accept = str(request.headers.get("accept") or "").lower()
    sec_fetch_dest = str(request.headers.get("sec-fetch-dest") or "").lower()
    wants_html = "text/html" in accept or sec_fetch_dest == "document"
    if not wants_html:
        return None
    query = str(request.url.query or "").strip()
    return_to = f"{path}?{query}" if query else path
    params = {"return_to": return_to}
    referer = str(request.headers.get("referer") or "").strip()
    try:
        parsed_referer = urllib.parse.urlsplit(referer)
    except ValueError:
        parsed_referer = urllib.parse.SplitResult("", "", "", "", "")
    if (
        parsed_referer.path.startswith("/app")
        and _normalize_origin(referer, default_scheme=str(request.url.scheme or "https")) == _request_origin(request)
    ):
        params["session"] = "expired"
    target = "/sign-in?" + urllib.parse.urlencode(params)
    response = RedirectResponse(target, status_code=303)
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
    return response


def _propertyquarry_browser_document_request(request: Request) -> bool:
    if str(request.method or "").upper() not in {"GET", "HEAD"}:
        return False
    if not _request_is_propertyquarry_host(request):
        return False
    path = str(request.url.path or "").strip()
    if path.startswith(("/api/", "/v1/", "/app/api/", "/admin/api/")):
        return False
    accept = str(request.headers.get("accept") or "").lower()
    sec_fetch_dest = str(request.headers.get("sec-fetch-dest") or "").lower()
    return "text/html" in accept or sec_fetch_dest == "document"


def _propertyquarry_browser_failure_response(
    request: Request,
    *,
    status_code: int,
) -> HTMLResponse | None:
    if not _propertyquarry_browser_document_request(request):
        return None
    failure_states = {
        404: {
            "state": "not_found",
            "kicker": "Page not found",
            "title": "We couldn\u2019t find that page.",
            "detail": "The link may have moved. Your saved searches and shortlist are unchanged.",
            "action_href": "/",
            "action_label": "Go to PropertyQuarry",
        },
        500: {
            "state": "internal_error",
            "kicker": "Temporary interruption",
            "title": "Something went wrong on our side.",
            "detail": "Your saved work is still safe. Try this page again, or open support if it keeps happening.",
            "action_href": str(request.url.path or "/"),
            "action_label": "Try again",
        },
        503: {
            "state": "service_unavailable",
            "kicker": "Temporary interruption",
            "title": "PropertyQuarry is taking a short pause.",
            "detail": "Your saved work is still safe. Try again in a moment.",
            "action_href": str(request.url.path or "/"),
            "action_label": "Try again",
        },
    }
    state = failure_states.get(int(status_code))
    if state is None:
        return None
    safe = {key: html.escape(str(value or ""), quote=True) for key, value in state.items()}
    document = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <meta name=\"robots\" content=\"noindex,nofollow,noarchive,nosnippet\">
  <title>{safe['title']} | PropertyQuarry</title>
  <style>
    :root {{ color-scheme: light dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px; background: #f4f1eb; color: #211f1b; }}
    main {{ width: min(100%, 680px); padding: clamp(24px, 6vw, 52px); border: 1px solid #d7d0c5; border-radius: 20px; background: #fffdf9; box-shadow: 0 20px 60px rgba(42,36,28,.08); }}
    .mark {{ display: inline-grid; place-items: center; width: 42px; height: 42px; border-radius: 12px; background: #223f37; color: #fff; font-weight: 800; }}
    .kicker {{ margin-top: 24px; color: #52645d; font-size: .78rem; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }}
    h1 {{ margin: 8px 0 12px; max-width: 18ch; font-size: clamp(2rem, 7vw, 3.75rem); line-height: 1; letter-spacing: -.04em; }}
    p {{ max-width: 55ch; color: #5e5a52; line-height: 1.6; }}
    nav {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 28px; }}
    a {{ min-height: 44px; display: inline-flex; align-items: center; padding: 0 18px; border: 1px solid #b9b0a2; border-radius: 999px; color: inherit; font-weight: 750; text-decoration: none; }}
    a.primary {{ background: #223f37; border-color: #223f37; color: #fff; }}
    a:focus-visible {{ outline: 3px solid #d68f35; outline-offset: 3px; }}
    @media (prefers-color-scheme: dark) {{ body {{ background: #151714; color: #f3efe7; }} main {{ background: #1d211d; border-color: #3b413a; }} p, .kicker {{ color: #bdc6bd; }} a {{ border-color: #636a62; }} }}
  </style>
</head>
<body>
  <main data-pq-failure-state=\"{safe['state']}\" role=\"alert\" aria-labelledby=\"pq-failure-title\">
    <span class=\"mark\" aria-hidden=\"true\">PQ</span>
    <div class=\"kicker\">{safe['kicker']}</div>
    <h1 id=\"pq-failure-title\">{safe['title']}</h1>
    <p>{safe['detail']}</p>
    <nav aria-label=\"Recovery actions\">
      <a class=\"primary\" data-pq-next-action href=\"{safe['action_href']}\">{safe['action_label']}</a>
      <a href=\"/support\">Open support</a>
    </nav>
  </main>
</body>
</html>"""
    response = HTMLResponse(document, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
    return response


def _request_is_https(request: Request) -> bool:
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").strip().lower()
    tokens = {token.strip() for token in forwarded_proto.split(",") if token.strip()}
    if "https" in tokens or "wss" in tokens:
        return True
    if tokens:
        return False
    return str(getattr(request.url, "scheme", "") or "").strip().lower() in {"https", "wss"}


def _request_origin(request: Request) -> str:
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").strip().lower()
    scheme_tokens = [token.strip() for token in forwarded_proto.split(",") if token.strip()]
    scheme = "https" if "https" in scheme_tokens or "wss" in scheme_tokens else ""
    if not scheme:
        scheme = scheme_tokens[0] if scheme_tokens else str(getattr(request.url, "scheme", "") or "http").strip().lower()
    if scheme == "wss":
        scheme = "https"
    forwarded_host = str(request.headers.get("x-forwarded-host") or "").strip()
    host = forwarded_host.split(",", 1)[0].strip() if forwarded_host else str(request.headers.get("host") or request.url.netloc or "").strip()
    return _normalize_origin(f"{scheme}://{host}", default_scheme=scheme)


def _request_is_propertyquarry_host(request: Request) -> bool:
    forwarded_host = str(request.headers.get("x-forwarded-host") or "").strip()
    raw_host = forwarded_host.split(",", 1)[0].strip() if forwarded_host else str(request.headers.get("host") or request.url.netloc or "").strip()
    hostname = raw_host.rsplit("@", 1)[-1].split(":", 1)[0].strip().lower()
    return hostname in {"propertyquarry.com", "www.propertyquarry.com"}


def _propertyquarry_raw_api_docs_request(request: Request) -> bool:
    if not _request_is_propertyquarry_host(request):
        return False
    path = str(request.url.path or "").strip()
    return path in _PROPERTYQUARRY_RAW_API_DOC_PATHS


def _normalize_origin(raw_value: str, *, default_scheme: str = "") -> str:
    normalized = str(raw_value or "").strip()
    if not normalized:
        return ""
    try:
        parsed = urllib.parse.urlsplit(normalized)
    except ValueError:
        return ""
    scheme = str(parsed.scheme or "").strip().lower()
    host = parsed.hostname
    port = parsed.port
    if not scheme and not parsed.netloc:
        fallback = urllib.parse.urlsplit(f"//{parsed.path.split('?', 1)[0]}")
        if not fallback.hostname:
            return ""
        scheme = str(default_scheme or "https").strip().lower()
        host = fallback.hostname
        port = fallback.port
    if not scheme:
        scheme = str(default_scheme or "https").strip().lower()
    if not host:
        return ""
    if scheme == "wss":
        scheme = "https"
    host = str(host).strip().lower()
    if host.startswith("www."):
        host = host[4:]
    if (scheme == "https" and (port == 443)) or (scheme == "http" and (port == 80)) or port is None:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _origin_of_url(raw_value: str) -> str:
    return _normalize_origin(str(raw_value or ""), default_scheme="https")


def _browser_mutation_request_is_cross_site(request: Request) -> bool:
    method = str(request.method or "").upper()
    if method not in _BROWSER_MUTATION_METHODS:
        return False
    path = str(request.url.path or "").strip()
    if not any(path.startswith(prefix) for prefix in _BROWSER_MUTATION_PATH_PREFIXES):
        return False
    expected = _request_origin(request).lower()
    origin = _origin_of_url(str(request.headers.get("origin") or ""))
    if origin:
        return origin != expected
    referer = _origin_of_url(str(request.headers.get("referer") or ""))
    if referer:
        return referer != expected
    return False


def _apply_default_browser_security_headers(request: Request, response: Any) -> None:
    for name, value in _DEFAULT_BROWSER_SECURITY_HEADERS.items():
        if name not in response.headers:
            response.headers[name] = value
    if _request_is_https(request) and "Strict-Transport-Security" not in response.headers:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"


def install_error_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.correlation_id = _request_correlation_id(request)
        trace_context = new_server_trace_context(request.headers.get("traceparent"))
        request.state.trace_id = trace_context.trace_id
        request.state.span_id = trace_context.span_id
        request.state.parent_span_id = trace_context.parent_span_id
        request.state.trace_flags = trace_context.trace_flags
        request.state.traceparent = trace_context.traceparent
        build_identity = runtime_build_identity()
        started_at = time.perf_counter()
        response = None
        status_code = 500
        with bind_runtime_trace_context(
            trace_context,
            correlation_id=_correlation_id(request),
        ):
            try:
                if _propertyquarry_raw_api_docs_request(request):
                    response = _error_payload(
                        request=request,
                        status_code=404,
                        code="propertyquarry_api_schema_not_public",
                        message="raw runtime API schema is not public on the PropertyQuarry customer surface",
                        details="Use the public docs and support pages for customer-facing product information.",
                    )
                    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
                elif _browser_mutation_request_is_cross_site(request):
                    response = _error_payload(
                        request=request,
                        status_code=403,
                        code="cross_site_browser_mutation",
                        message="cross-site browser mutation blocked",
                        details="unsafe browser requests must originate from the same site",
                    )
                else:
                    response = await call_next(request)
                status_code = int(response.status_code)
                response.headers["x-correlation-id"] = _correlation_id(request)
                response.headers["traceparent"] = trace_context.traceparent
                _apply_default_browser_security_headers(request, response)
                return response
            finally:
                duration_seconds = max(0.0, time.perf_counter() - started_at)
                route = route_template(request)
                get_runtime_metrics(request.app).record_request(
                    method=request.method,
                    route=route,
                    status_code=status_code,
                    duration_seconds=duration_seconds,
                )
                log_event(
                    _LOG,
                    logging.INFO,
                    "http_request_completed",
                    correlation_id=_correlation_id(request),
                    trace_id=trace_context.trace_id,
                    span_id=trace_context.span_id,
                    parent_span_id=trace_context.parent_span_id,
                    trace_flags=trace_context.trace_flags,
                    trace_source=trace_context.source,
                    release_commit_sha=build_identity["release_commit_sha"],
                    release_image_digest=build_identity["release_image_digest"],
                    replica_id=build_identity["replica_id"],
                    method=request.method,
                    route=route,
                    status_code=status_code,
                    status_class=f"{status_code // 100}xx",
                    duration_seconds=round(duration_seconds, 6),
                )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):  # type: ignore[no-untyped-def]
        code = _code_from_http(exc.status_code, exc.detail)
        redirect = _browser_auth_redirect(request, code=code)
        if redirect is not None:
            return redirect
        browser_failure = _propertyquarry_browser_failure_response(
            request,
            status_code=exc.status_code,
        )
        if browser_failure is not None:
            return browser_failure
        message = str(exc.detail or code)
        response = _error_payload(
            request=request,
            status_code=exc.status_code,
            code=code,
            message=message,
            details=exc.detail,
        )
        for header_name, header_value in dict(exc.headers or {}).items():
            response.headers[str(header_name)] = str(header_value)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):  # type: ignore[no-untyped-def]
        return _error_payload(
            request=request,
            status_code=422,
            code="validation_error",
            message="request validation failed",
            details=exc.errors(),
        )

    @app.exception_handler(PermissionError)
    async def permission_exception_handler(request: Request, exc: PermissionError):  # type: ignore[no-untyped-def]
        log_event(
            _LOG,
            logging.ERROR,
            "internal_permission_error",
            correlation_id=_correlation_id(request),
            method=request.method,
            route=route_template(request),
            error_type=exc.__class__.__name__,
            **exception_log_fields(exc),
        )
        response = _propertyquarry_browser_failure_response(request, status_code=500)
        if response is None:
            response = _error_payload(
                request=request,
                status_code=500,
                code="internal_error",
                message="internal server error",
                details="permission_error",
            )
        _apply_request_observability_headers(request, response)
        _apply_default_browser_security_headers(request, response)
        return response

    async def _database_unavailable_handler(request: Request, exc: Exception):  # type: ignore[no-untyped-def]
        correlation_id = _correlation_id(request)
        log_event(
            _LOG,
            logging.WARNING,
            "database_unavailable",
            correlation_id=correlation_id,
            error_type=exc.__class__.__name__,
            error_detail=str(exc or "").strip(),
        )
        response = _propertyquarry_browser_failure_response(request, status_code=503)
        if response is None:
            response = _error_payload(
                request=request,
                status_code=503,
                code="database_unavailable",
                message="temporary service interruption",
                details="database_temporarily_unavailable",
            )
        response.headers["Retry-After"] = "5"
        _apply_request_observability_headers(request, response)
        return response

    if PsycopgOperationalError is not None:
        app.add_exception_handler(PsycopgOperationalError, _database_unavailable_handler)
    if PsycopgInterfaceError is not None:
        app.add_exception_handler(PsycopgInterfaceError, _database_unavailable_handler)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):  # type: ignore[no-untyped-def]
        log_event(
            _LOG,
            logging.ERROR,
            "unhandled_exception",
            correlation_id=_correlation_id(request),
            method=request.method,
            route=route_template(request),
            error_type=exc.__class__.__name__,
            **exception_log_fields(exc),
        )
        response = _propertyquarry_browser_failure_response(request, status_code=500)
        if response is None:
            response = _error_payload(
                request=request,
                status_code=500,
                code="internal_error",
                message="internal server error",
                details=exc.__class__.__name__,
            )
        _apply_request_observability_headers(request, response)
        _apply_default_browser_security_headers(request, response)
        return response
