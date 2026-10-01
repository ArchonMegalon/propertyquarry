from __future__ import annotations

import http.cookiejar
import http.cookies
import json
import re
import urllib.parse
import urllib.request
from email.message import Message
from typing import Callable


BILLING_DNS_OVER_HTTPS_ENDPOINTS = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
)
PROPERTYQUARRY_BILLING_HANDOFF_ALLOWED_HOSTS = (
    "propertyquarry.directoryup.com",
)


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def no_proxy_opener(*handlers: object) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), *handlers)


def header_value(headers: dict[str, object], name: str) -> str:
    normalized_name = str(name or "").strip().lower()
    for key, value in headers.items():
        if str(key or "").strip().lower() == normalized_name:
            return str(value or "").strip()
    return ""


def public_dns_host_resolves(host: str, expected_target: str) -> bool:
    normalized_host = str(host or "").strip().lower().rstrip(".")
    normalized_target = str(expected_target or "").strip().lower().rstrip(".")
    if not normalized_host:
        return False
    matched_cname_answer = False
    matched_address_answer = False
    for endpoint in BILLING_DNS_OVER_HTTPS_ENDPOINTS:
        for record_type in ("CNAME", "A", "AAAA"):
            query = urllib.parse.urlencode({"name": normalized_host, "type": record_type})
            request = urllib.request.Request(
                f"{endpoint}?{query}",
                headers={
                    "Accept": "application/dns-json",
                    "User-Agent": "PropertyQuarry-billing-handoff-probe/1.0",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=8) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except Exception:
                continue
            for row in payload.get("Answer") or []:
                if not isinstance(row, dict):
                    continue
                answer_name = str(row.get("name") or "").strip().lower().rstrip(".")
                answer_data = str(row.get("data") or "").strip().lower().rstrip(".")
                if answer_name != normalized_host or not answer_data:
                    continue
                row_type = int(row.get("type") or 0)
                if row_type == 5:
                    matched_cname_answer = True
                    if normalized_target and answer_data == normalized_target:
                        return True
                elif row_type in {1, 28}:
                    matched_address_answer = True
    # Cloudflare-proxied CNAMEs intentionally publish edge A/AAAA records. A
    # public address answer is enough to prove the HTTPS handoff host resolves.
    if matched_address_answer:
        return True
    return matched_cname_answer if not normalized_target else False


def https_redirect_host_resolves(
    location: str,
    resolver: Callable[[str, int], object],
    *,
    expected_cname_target: str = "",
) -> bool:
    parsed = urllib.parse.urlparse(str(location or "").strip())
    if parsed.scheme != "https":
        return False
    host = str(parsed.hostname or "").strip().lower()
    if not host:
        return False
    try:
        resolver(host, 443)
    except OSError:
        return public_dns_host_resolves(host, expected_cname_target)
    return True


def https_handoff_follow_redirect_url(
    current_url: str,
    redirect_location: str,
    *,
    allowed_hosts: tuple[str, ...] = (),
) -> str:
    next_url = urllib.parse.urljoin(str(current_url or "").strip(), str(redirect_location or "").strip())
    parsed = urllib.parse.urlparse(next_url)
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    normalized_allowed_hosts: set[str] = set()
    for allowed_host in allowed_hosts:
        raw_allowed_host = str(allowed_host or "").strip()
        if not raw_allowed_host:
            continue
        parsed_allowed_host = urllib.parse.urlparse(
            raw_allowed_host if "://" in raw_allowed_host else f"//{raw_allowed_host}"
        )
        normalized_allowed_host = str(parsed_allowed_host.hostname or "").strip().lower()
        if normalized_allowed_host:
            normalized_allowed_hosts.add(normalized_allowed_host)
    current_host = str(urllib.parse.urlparse(str(current_url or "").strip()).hostname or "").strip().lower()
    next_host = str(parsed.hostname or "").strip().lower()
    if next_host != current_host and next_host not in normalized_allowed_hosts:
        return ""
    return next_url


class _SetCookieResponse:
    """Minimal response adapter for CookieJar's browser-style policy checks."""

    def __init__(self, set_cookie_header: str) -> None:
        self._headers = Message()
        self._headers.add_header("Set-Cookie", set_cookie_header)

    def info(self) -> Message:
        return self._headers


def _set_cookie_header_rows(set_cookie_headers: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(set_cookie_headers, str):
        rows = (set_cookie_headers,)
    else:
        rows = tuple(str(row or "") for row in set_cookie_headers)
    return tuple(row.strip() for row in rows if row.strip())


def _extract_redirect_response_cookies(
    cookie_jar: http.cookiejar.CookieJar,
    *,
    request: urllib.request.Request,
    set_cookie_headers: str | list[str] | tuple[str, ...],
) -> None:
    # Extract one header at a time so a malformed vendor cookie cannot discard
    # otherwise valid cookies or crash the launch-readiness probe.
    for row in _set_cookie_header_rows(set_cookie_headers):
        try:
            cookie_jar.extract_cookies(_SetCookieResponse(row), request)
        except Exception:
            continue


def _redirect_cookie_jar(*, location: str, cookie_header: str = "") -> http.cookiejar.CookieJar:
    cookie_jar = http.cookiejar.CookieJar()
    if not str(cookie_header or "").strip():
        return cookie_jar
    try:
        supplied_cookies = http.cookies.SimpleCookie()
        supplied_cookies.load(str(cookie_header or ""))
    except Exception:
        return cookie_jar
    request = urllib.request.Request(str(location or "").strip())
    for morsel in supplied_cookies.values():
        if not morsel.key:
            continue
        # A caller-provided Cookie header has no Domain/Path metadata. Scope it
        # conservatively to the current HTTPS host instead of replaying it to a
        # later explicitly allowlisted host.
        _extract_redirect_response_cookies(
            cookie_jar,
            request=request,
            set_cookie_headers=f"{morsel.key}={morsel.coded_value}; Path=/; Secure",
        )
    return cookie_jar


def _redirect_cookie_header(cookie_jar: http.cookiejar.CookieJar, location: str) -> str:
    request = urllib.request.Request(str(location or "").strip())
    try:
        cookie_jar.add_cookie_header(request)
    except Exception:
        return ""
    return str(request.get_header("Cookie") or "").strip()


def _cookie_header_has_authenticated_member(cookie_header: str) -> bool:
    try:
        cookies = http.cookies.SimpleCookie()
        cookies.load(str(cookie_header or ""))
    except Exception:
        return False
    token_cookie = cookies.get("token")
    loggedin_cookie = cookies.get("loggedin")
    return bool(
        token_cookie is not None
        and token_cookie.value
        and loggedin_cookie is not None
        and loggedin_cookie.value == "1"
    )


def _cookie_header_from_set_cookie(set_cookie_headers: str | list[str] | tuple[str, ...]) -> str:
    header_rows = _set_cookie_header_rows(set_cookie_headers)
    if not header_rows:
        return ""
    cookie = http.cookies.SimpleCookie()
    for row in header_rows:
        if not str(row or "").strip():
            continue
        try:
            cookie.load(str(row or ""))
        except Exception:
            continue
    return "; ".join(f"{morsel.key}={morsel.value}" for morsel in cookie.values() if morsel.key)


def _merge_cookie_headers(existing_cookie_header: str, set_cookie_headers: str | list[str] | tuple[str, ...]) -> str:
    merged: dict[str, str] = {}
    for cookie_header in (existing_cookie_header, _cookie_header_from_set_cookie(set_cookie_headers)):
        for chunk in str(cookie_header or "").split(";"):
            if "=" not in chunk:
                continue
            key, value = chunk.split("=", 1)
            key = key.strip()
            if key:
                merged[key] = value.strip()
    return "; ".join(f"{key}={value}" for key, value in merged.items())


def _handoff_body_has_account_marker(body: str) -> bool:
    lowered = str(body or "").lower()
    return any(
        marker in lowered
        for marker in (
            "logout",
            "log out",
            "my account",
            "account dashboard",
            "member dashboard",
            "/account/logout",
            "/account/home",
        )
    )


def https_handoff_url_usable(
    location: str,
    *,
    timeout_seconds: float = 8.0,
    visited_urls: tuple[str, ...] = (),
    allowed_hosts: tuple[str, ...] = (),
    cookie_header: str = "",
    _cookie_jar: http.cookiejar.CookieJar | None = None,
) -> dict[str, object]:
    parsed = urllib.parse.urlparse(str(location or "").strip())
    if parsed.scheme != "https" or not parsed.hostname:
        return {"ok": False, "status_code": 0, "error": "handoff_url_not_https"}
    redirect_cookie_jar = (
        _cookie_jar
        if _cookie_jar is not None
        else _redirect_cookie_jar(
            location=str(location or "").strip(),
            cookie_header=cookie_header,
        )
    )
    request_headers = {
        "User-Agent": "PropertyQuarry-billing-handoff-probe/1.0",
        "Accept": "text/html,application/json,*/*",
    }
    request_cookie_header = _redirect_cookie_header(redirect_cookie_jar, str(location or "").strip())
    if request_cookie_header:
        request_headers["Cookie"] = request_cookie_header
    request = urllib.request.Request(
        str(location),
        headers=request_headers,
    )
    opener = no_proxy_opener(NoRedirectHandler)
    response_headers: dict[str, object] = {}
    set_cookie_headers: list[str] = []
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status_code = int(response.status)
            response_headers = dict(response.headers.items())
            try:
                set_cookie_headers = [str(row or "") for row in (response.headers.get_all("Set-Cookie") or [])]
            except Exception:
                set_cookie_headers = []
            redirect_location = ""
            body = response.read(16_384).decode("utf-8", errors="replace").lower()
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code)
        response_headers = dict(exc.headers.items())
        try:
            set_cookie_headers = [str(row or "") for row in (exc.headers.get_all("Set-Cookie") or [])]
        except Exception:
            set_cookie_headers = []
        redirect_location = header_value(dict(exc.headers or {}), "Location")
        body = exc.read(16_384).decode("utf-8", errors="replace").lower()
    except Exception as exc:
        return {"ok": False, "status_code": 0, "error": f"{type(exc).__name__}: {exc}"}
    response_set_cookie_headers: str | list[str] | tuple[str, ...] = (
        set_cookie_headers or header_value(response_headers, "Set-Cookie")
    )
    _extract_redirect_response_cookies(
        redirect_cookie_jar,
        request=request,
        set_cookie_headers=response_set_cookie_headers,
    )
    next_url = (
        https_handoff_follow_redirect_url(
            location,
            redirect_location,
            allowed_hosts=allowed_hosts,
        )
        if redirect_location
        else ""
    )
    authenticated_member_redirect = bool(
        next_url
        and _cookie_header_has_authenticated_member(
            _redirect_cookie_header(redirect_cookie_jar, next_url)
        )
    )
    login_target = str(redirect_location or urllib.parse.urlparse(str(location or "")).path or "").lower()
    body_is_login = "<title" in body and "login" in body and ("email" in body or "password" in body)
    body_has_account_marker = _handoff_body_has_account_marker(body)
    requires_login = body_is_login or (
        ("/login" in login_target or "login_direct_url" in login_target)
        and not body_has_account_marker
    )
    server_header = str(response_headers.get("server") or response_headers.get("Server") or "").strip().lower()
    cloudflare_error_code_match = (
        re.search(r"error code:\s*(\d{3,4})", body, flags=re.IGNORECASE)
        if status_code == 403 and "cloudflare" in server_header and not requires_login
        else None
    )
    cloudflare_error_code = str(cloudflare_error_code_match.group(1) or "").strip() if cloudflare_error_code_match else ""
    usable = 200 <= status_code < 300 and not requires_login and not cloudflare_error_code
    result = {
        "ok": usable,
        "status_code": status_code,
        "redirect_location": redirect_location,
        "error": (
            ""
            if usable
            else (
                "handoff_url_requires_separate_login"
                if requires_login
                else (f"handoff_url_cloudflare_error_{cloudflare_error_code}" if cloudflare_error_code else f"handoff_url_http_{status_code}")
            )
        ),
    }
    if redirect_location and not next_url:
        return {
            **result,
            "ok": False,
            "error": "handoff_url_redirect_not_allowed",
            "redirect_chain": [urllib.parse.urljoin(str(location or "").strip(), redirect_location)],
        }
    if requires_login and not authenticated_member_redirect:
        return {
            **result,
            "redirect_chain": (
                [urllib.parse.urljoin(str(location or "").strip(), redirect_location)]
                if redirect_location
                else []
            ),
        }
    if not redirect_location:
        return result
    if len(visited_urls) >= 2:
        return {
            **result,
            "ok": False,
            "error": "handoff_url_too_many_redirects",
            "redirect_chain": [urllib.parse.urljoin(str(location or "").strip(), redirect_location)],
        }
    if next_url in visited_urls:
        return {
            **result,
            "ok": False,
            "error": "handoff_url_redirect_loop",
            "redirect_chain": [next_url],
        }
    downstream = https_handoff_url_usable(
        next_url,
        timeout_seconds=timeout_seconds,
        visited_urls=(*visited_urls, str(location or "").strip()),
        allowed_hosts=allowed_hosts,
        _cookie_jar=redirect_cookie_jar,
    )
    redirect_chain = [next_url, *list(downstream.get("redirect_chain") or [])]
    if not downstream.get("ok"):
        return {
            "ok": False,
            "status_code": int(downstream.get("status_code") or status_code),
            "redirect_location": str(downstream.get("redirect_location") or redirect_location),
            "error": str(downstream.get("error") or "handoff_url_requires_separate_login"),
            "redirect_chain": redirect_chain,
        }
    return {
        **downstream,
        "redirect_chain": redirect_chain,
    }


def _admin_login_form_fields(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for name in ("action", "tlocation", "string", "website", "rftoken", "newsite"):
        patterns = (
            rf'NAME={name} VALUE="([^"]*)"',
            rf"NAME={name} VALUE='([^']*)'",
            rf'name="{name}" value="([^"]*)"',
            rf"name='{name}' value='([^']*)'",
        )
        for pattern in patterns:
            match = re.search(pattern, html, flags=re.IGNORECASE)
            if match:
                fields[name] = str(match.group(1) or "").strip()
                break
        else:
            fields[name] = ""
    return fields


def _admin_login_form_action(html: str, login_url: str) -> str:
    match = re.search(r"<form[^>]+action=['\"]([^'\"]+)['\"]", html, flags=re.IGNORECASE)
    if not match:
        return ""
    return urllib.parse.urljoin(login_url, str(match.group(1) or "").strip())


def _admin_recovery_href(html: str, login_url: str) -> str:
    for href, label in re.findall(r"<a[^>]+href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", html, flags=re.IGNORECASE | re.DOTALL):
        normalized_label = re.sub(r"<[^>]+>", " ", label)
        normalized_label = " ".join(normalized_label.split()).lower()
        if any(token in normalized_label for token in ("forgot", "reset", "password", "username")):
            return urllib.parse.urljoin(login_url, href.strip())
    return ""


def _default_admin_recovery_href(login_url: str, form_action: str = "") -> str:
    for candidate in (form_action, login_url):
        parsed = urllib.parse.urlparse(str(candidate or "").strip())
        host = str(parsed.hostname or "").strip().lower()
        if host.endswith("managemydirectory.com"):
            return urllib.parse.urlunparse(("https", "www.managemydirectory.com", "/admin/login.php", "", "action=retrieve", ""))
    return ""


def billing_admin_login_surface_probe(
    login_url: str = "https://propertyquarry.directoryup.com/admin/login",
    *,
    timeout_seconds: float = 8.0,
) -> dict[str, object]:
    request = urllib.request.Request(
        login_url,
        headers={
            "User-Agent": "PropertyQuarry-billing-admin-probe/1.0",
            "Accept": "text/html,*/*",
        },
    )
    opener = no_proxy_opener()
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            html = response.read(32_768).decode("utf-8", errors="replace")
            status_code = int(response.status)
            final_url = str(response.geturl() or login_url).strip()
    except urllib.error.HTTPError as exc:
        html = exc.read(32_768).decode("utf-8", errors="replace")
        status_code = int(exc.code)
        final_url = str(exc.geturl() or login_url).strip()
    except Exception as exc:
        return {
            "configured": False,
            "ok": False,
            "login_url": login_url,
            "status_code": 0,
            "final_url": login_url,
            "error": f"billing_admin_login_probe_failed:{type(exc).__name__}",
        }
    lowered = html.lower()
    form_action = _admin_login_form_action(html, final_url)
    hidden_fields = _admin_login_form_fields(html)
    recovery_href = _admin_recovery_href(html, final_url) or _default_admin_recovery_href(final_url, form_action)
    has_username_field = 'name=username' in lowered or 'name="username"' in lowered or "name='username'" in lowered
    has_password_field = 'name=password' in lowered or 'name="password"' in lowered or "name='password'" in lowered
    recaptcha_required = "recaptcha" in lowered or "g-recaptcha" in lowered or "captcha" in lowered
    ok = status_code == 200 and bool(form_action) and has_username_field and has_password_field
    return {
        "configured": True,
        "ok": ok,
        "login_url": login_url,
        "status_code": status_code,
        "final_url": final_url,
        "form_action": form_action,
        "website_id": hidden_fields.get("website", ""),
        "has_username_field": has_username_field,
        "has_password_field": has_password_field,
        "recaptcha_required": recaptcha_required,
        "recovery_href": recovery_href,
        "error": "" if ok else "billing_admin_login_form_unusable",
    }


def billing_admin_login_attempt(
    *,
    username: str,
    password: str,
    login_url: str = "https://propertyquarry.directoryup.com/admin/login",
    timeout_seconds: float = 8.0,
) -> dict[str, object]:
    normalized_username = str(username or "").strip()
    normalized_password = str(password or "").strip()
    if not normalized_username or not normalized_password:
        return {
            "attempted": False,
            "authenticated": False,
            "error": "billing_admin_credentials_missing",
        }
    surface = billing_admin_login_surface_probe(login_url, timeout_seconds=timeout_seconds)
    if not surface.get("ok"):
        return {
            "attempted": False,
            "authenticated": False,
            "error": str(surface.get("error") or "billing_admin_login_form_unusable"),
            "surface_probe": surface,
        }
    opener = no_proxy_opener(urllib.request.HTTPCookieProcessor())
    initial_request = urllib.request.Request(
        login_url,
        headers={
            "User-Agent": "PropertyQuarry-billing-admin-probe/1.0",
            "Accept": "text/html,*/*",
        },
    )
    with opener.open(initial_request, timeout=timeout_seconds) as response:
        html = response.read(32_768).decode("utf-8", errors="replace")
        effective_login_url = str(response.geturl() or login_url).strip()
    form_action = _admin_login_form_action(html, effective_login_url) or str(surface.get("form_action") or "").strip()
    payload = {
        **_admin_login_form_fields(html),
        "username": normalized_username,
        "password": normalized_password,
    }
    post = urllib.request.Request(
        form_action or effective_login_url,
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={
            "User-Agent": "PropertyQuarry-billing-admin-probe/1.0",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,*/*",
        },
    )
    try:
        with opener.open(post, timeout=timeout_seconds) as response:
            status_code = int(response.status)
            final_url = str(response.geturl() or form_action or effective_login_url).strip()
            raw_body = response.read(32_768).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code)
        final_url = str(exc.geturl() or form_action or effective_login_url).strip()
        raw_body = exc.read(32_768).decode("utf-8", errors="replace")
    body = raw_body.lower()
    invalid = "message=invalid" in final_url.lower() or "administration login" in body
    authenticated = (
        status_code == 200
        and not invalid
        and any(token in body for token in ("logout", "dashboard", "administration", "admin home"))
        and "name=password" not in body
    )
    recovery_href = (
        str(surface.get("recovery_href") or "").strip()
        or _admin_recovery_href(raw_body, final_url)
        or _default_admin_recovery_href(final_url, form_action)
    )
    return {
        "attempted": True,
        "authenticated": authenticated,
        "status_code": status_code,
        "final_url": final_url,
        "recovery_href": recovery_href,
        "error": "" if authenticated else ("billing_admin_invalid_credentials" if invalid else "billing_admin_authentication_failed"),
        "surface_probe": surface,
    }
