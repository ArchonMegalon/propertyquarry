#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(EA_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_ROOT))

from scripts.propertyquarry_live_mobile_surface_smoke import DEFAULT_ROUTES, route_is_research_detail
from scripts.propertyquarry_live_http_security import normalized_origin, redact_secret_values
from scripts.propertyquarry_live_probe_auth import live_probe_request_headers
from scripts.propertyquarry_live_probe_secret_scope import (
    read_release_probe_secret_from_stdin,
    scrub_release_probe_secret_environment,
)
from scripts.propertyquarry_live_public_smoke import PUBLIC_INFORMATION_ROUTES
from scripts.propertyquarry_playwright_runtime import (
    SUPPORTED_PLAYWRIGHT_ENGINES,
    normalize_playwright_engine,
    playwright_browser_type,
    playwright_engine_launch_kwargs,
)


AXE_CORE_VERSION = "4.10.2"
DEFAULT_AXE_CORE_PATH = Path("node_modules/axe-core/axe.min.js")
DEFAULT_BROWSER_ENGINE = "chromium"
_GOLD_ACCESSIBILITY_ROUTE_TAIL = (
    "/app/settings/outcomes",
    "/app/settings/plan",
    "/app/research",
    "/app/properties/packets",
    "/app/properties/notifications/preview",
    "/app/support",
)
DEFAULT_ACCESSIBILITY_ROUTES = tuple(
    dict.fromkeys(
        (
            *PUBLIC_INFORMATION_ROUTES,
            *(
                route
                for route in DEFAULT_ROUTES
                if route not in {"/app/research", "/app/properties/packets"}
            ),
            *_GOLD_ACCESSIBILITY_ROUTE_TAIL,
        )
    )
)
REQUIRED_ACCESSIBILITY_CHECKS = (
    "route_document_loaded",
    "axe_core_version_pinned",
    "axe_no_moderate_or_higher_wcag_violations",
    "keyboard_only_navigation",
    "visible_keyboard_focus",
    "focus_not_obscured",
    "target_size_24_css_px_or_spacing",
    "dialog_focus_contract",
    "semantic_error_states",
    "semantic_live_progress_states",
    "zoom_200_reflow",
    "zoom_400_reflow",
    "contrast_signals_clear",
    "reduced_motion_honored",
)

_REFLOW_STABLE_SAMPLE_COUNT = 3
_REFLOW_SAMPLE_INTERVAL_MS = 50
_REFLOW_SETTLE_TIMEOUT_MS = 500
_REFLOW_MEASUREMENT_SCRIPT = """
() => {
  const visible = (node) => {
    if (node.closest('[hidden], [aria-hidden="true"], details:not([open])')) return false;
    const style = getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && rect.width > 0 && rect.height > 0;
  };
  const insideHorizontalScrollRegion = (node) => {
    let current = node.parentElement;
    while (current && current !== document.body) {
      const style = getComputedStyle(current);
      if (['auto', 'scroll'].includes(style.overflowX)
          && current.scrollWidth > current.clientWidth + 2) return true;
      current = current.parentElement;
    }
    return false;
  };
  const rounded = (value) => Math.round(value * 100) / 100;
  const viewportWidth = document.documentElement.clientWidth;
  const scrollWidth = document.documentElement.scrollWidth;
  const interactive = Array.from(document.querySelectorAll(
    'a[href], button, input, select, textarea, summary, [role="button"]'
  )).filter(visible);
  const clippedInteractive = interactive.filter((node) => {
    const rect = node.getBoundingClientRect();
    return (rect.left < -2 || rect.right > viewportWidth + 2)
      && !insideHorizontalScrollRegion(node);
  });
  return {
    reflow_viewport_width: viewportWidth,
    reflow_scroll_width: scrollWidth,
    reflow_without_horizontal_scroll: scrollWidth <= viewportWidth + 2,
    reflow_clipped_interactive_count: clippedInteractive.length,
    reflow_clipped_interactives: clippedInteractive.slice(0, 25).map((node) => {
      const rect = node.getBoundingClientRect();
      const className = typeof node.className === 'string' ? node.className : '';
      const parentClassName = node.parentElement
        && typeof node.parentElement.className === 'string'
        ? node.parentElement.className
        : '';
      return {
        tag: String(node.tagName || '').toLowerCase(),
        role: String(node.getAttribute('role') || ''),
        type: String(node.getAttribute('type') || ''),
        class_name: className.slice(0, 120),
        parent_class_name: parentClassName.slice(0, 120),
        left: rounded(rect.left),
        right: rounded(rect.right),
        width: rounded(rect.width),
      };
    }),
  };
}
"""


def normalize_browser_engines(engines: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    normalized: list[str] = []
    selected = (DEFAULT_BROWSER_ENGINE,) if engines is None else engines
    if not selected:
        raise ValueError("accessibility_browser_engines_required")
    for raw_engine in selected:
        engine = normalize_playwright_engine(raw_engine)
        if engine not in normalized:
            normalized.append(engine)
    return tuple(normalized)


def resolve_axe_core_source(path: Path) -> str:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(
            "axe_core_unavailable:"
            f"expected pinned axe-core {AXE_CORE_VERSION} input at {resolved}; "
            f"install axe-core@{AXE_CORE_VERSION} locally and pass --axe-core-path"
        )
    source = resolved.read_text(encoding="utf-8")
    if "axe" not in source or len(source) < 100:
        raise ValueError(f"axe_core_invalid_input:{resolved}")
    return source


def _origin(value: str) -> str:
    return normalized_origin(value)


def _route_has_literal_placeholder(value: object) -> bool:
    normalized = urllib.parse.unquote(str(value or "").strip())
    return any(marker in normalized for marker in ("{", "}", "[", "]", "<", ">"))


def _relative_route_parts(value: object) -> tuple[str, str] | None:
    normalized = str(value or "").strip()
    if not normalized or _route_has_literal_placeholder(normalized):
        return None
    parsed = urllib.parse.urlsplit(normalized)
    if parsed.scheme or parsed.netloc or parsed.fragment or not str(parsed.path or "").startswith("/"):
        return None
    path = str(parsed.path or "/").rstrip("/") or "/"
    return path, str(parsed.query or "")


def _concrete_research_detail_route(value: object) -> bool:
    parts = _relative_route_parts(value)
    if parts is None:
        return False
    path, _query = parts
    return route_is_research_detail(path) and path.count("/") == 3


def _concrete_shortlist_run_route(value: object) -> bool:
    parts = _relative_route_parts(value)
    if parts is None:
        return False
    path, query = parts
    return (
        not query
        and path.startswith("/app/shortlist/run/")
        and path.count("/") == 4
        and bool(path.rsplit("/", 1)[-1])
    )


def _concrete_public_tour_route(value: object) -> bool:
    parts = _relative_route_parts(value)
    if parts is None:
        return False
    path, query = parts
    slug = path.rsplit("/", 1)[-1]
    return (
        not query
        and path.startswith("/tours/")
        and path.count("/") == 2
        and bool(slug)
        and not slug.endswith(".json")
    )


def _navigation_contract(
    *,
    requested_url: str,
    final_url: str,
    status_code: int,
) -> tuple[bool, str]:
    if not 200 <= int(status_code) < 300:
        return False, "non_success_status"
    requested = urllib.parse.urlsplit(str(requested_url or ""))
    final = urllib.parse.urlsplit(str(final_url or ""))
    if _origin(requested_url) != _origin(final_url):
        return False, "cross_origin_redirect"
    requested_path = str(requested.path or "/").rstrip("/") or "/"
    final_path = str(final.path or "/").rstrip("/") or "/"
    if requested_path == final_path:
        return True, "exact_path"
    if (
        requested_path in {"/", "/app/properties"}
        and not requested.query
        and not requested.fragment
        and final_path == "/app/search"
        and not final.query
        and not final.fragment
    ):
        return True, "canonical_search_redirect"
    if (
        requested_path in {"/app/settings/google", "/app/settings/access"}
        and not requested.query
        and not requested.fragment
        and final_path == "/app/account"
    ):
        settings_view = requested_path.rsplit("/", 1)[-1]
        final_query = urllib.parse.parse_qs(final.query, keep_blank_values=True)
        if (
            final_query == {"settings_view": [settings_view]}
            and final.fragment == "connected-services"
        ):
            return True, "canonical_connected_services_redirect"
    if requested_path == "/app/support" and final_path == "/app/settings/support":
        return True, "canonical_app_support_redirect"
    if requested_path == "/app/billing" and final_path == "/app/account":
        final_query = urllib.parse.parse_qs(final.query, keep_blank_values=True)
        if final_query.get("billing") == ["1"] and final.fragment == "delivery":
            return True, "canonical_billing_handoff_redirect"
    if _concrete_public_tour_route(requested_path) and final_path.startswith(
        f"{requested_path}/control"
    ):
        suffix = final_path[len(requested_path) :]
        if suffix == "/control" or (
            suffix.startswith("/control/") and suffix.count("/") == 2
        ):
            return True, "canonical_public_tour_control_redirect"
    return False, "unexpected_final_path"


def _release_probe_configured_routes(routes: tuple[str, ...]) -> tuple[str, ...]:
    configured: list[str] = []
    for raw_route in routes:
        parsed = urllib.parse.urlsplit(str(raw_route or "").strip())
        if parsed.scheme or parsed.netloc or parsed.fragment:
            continue
        path = str(parsed.path or "/")
        if not (
            route_is_research_detail(raw_route)
            or path.startswith("/app/shortlist/run/")
        ):
            continue
        route = urllib.parse.urlunsplit(("", "", path, str(parsed.query or ""), ""))
        if route not in configured:
            configured.append(route)
    return tuple(configured)


def _routes_require_authenticated_app(routes: tuple[str, ...]) -> bool:
    for raw_route in routes:
        path = str(urllib.parse.urlsplit(str(raw_route or "").strip()).path or "/")
        if path == "/app" or path.startswith("/app/"):
            return True
    return False


def _redact_receipt_value(value: Any, *, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, bytes):
        return redact_secret_values(value.decode("utf-8", errors="replace"), secrets=secrets)
    if isinstance(value, dict):
        return {
            str(key): _redact_receipt_value(item, secrets=secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_receipt_value(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        return redact_secret_values(value, secrets=secrets)
    return value


def _continue_with_origin_scoped_headers(
    route: Any,
    *,
    authorized_origin: str,
    headers: dict[str, str],
    release_probe_secret: str = "",
    configured_routes: tuple[str, ...] = (),
) -> None:
    request_url = str(route.request.url or "")
    merged = {
        str(name): str(value)
        for name, value in dict(route.request.headers).items()
        if not str(name).strip().lower().startswith("x-propertyquarry-release-probe-")
    }
    merged.update(headers)
    route.continue_(
        headers=live_probe_request_headers(
            url=request_url,
            authorized_origin=authorized_origin,
            headers=merged,
            release_probe_secret=release_probe_secret,
            method=str(getattr(route.request, "method", "GET") or "GET"),
            configured_routes=configured_routes,
        )
    )


def _focus_metrics(page: Any) -> dict[str, Any]:
    focused = False
    for _ in range(16):
        page.keyboard.press("Tab")
        page.wait_for_timeout(50)
        metrics = dict(
            page.evaluate(
                """
                () => {
                  const node = document.activeElement;
                  if (!node || node === document.body || node === document.documentElement) {
                    return { focused: false, visible_focus: false, tag: '' };
                  }
                  const style = getComputedStyle(node);
                  const rect = node.getBoundingClientRect();
                  const outlineWidth = Number.parseFloat(style.outlineWidth || '0') || 0;
                  const focusVisible = (style.outlineStyle !== 'none' && outlineWidth > 0)
                    || (style.boxShadow && style.boxShadow !== 'none');
                  const left = Math.max(0, rect.left);
                  const right = Math.min(window.innerWidth, rect.right);
                  const top = Math.max(0, rect.top);
                  const bottom = Math.min(window.innerHeight, rect.bottom);
                  const samplePoints = [];
                  if (right > left && bottom > top) {
                    const xs = [left + 1, (left + right) / 2, right - 1];
                    const ys = [top + 1, (top + bottom) / 2, bottom - 1];
                    for (const x of xs) {
                      for (const y of ys) samplePoints.push([x, y]);
                    }
                  }
                  const unobscured = samplePoints.some(([x, y]) => {
                    const topmost = document.elementFromPoint(x, y);
                    return Boolean(topmost && (topmost === node || node.contains(topmost)));
                  });
                  return {
                    focused: rect.width > 0 && rect.height > 0,
                    visible_focus: Boolean(focusVisible),
                    focus_unobscured: unobscured,
                    focus_rect_left: Math.round(rect.left * 100) / 100,
                    focus_rect_top: Math.round(rect.top * 100) / 100,
                    focus_rect_right: Math.round(rect.right * 100) / 100,
                    focus_rect_bottom: Math.round(rect.bottom * 100) / 100,
                    tag: String(node.tagName || '').toLowerCase(),
                    role: String(node.getAttribute('role') || ''),
                  };
                }
                """
            )
            or {}
        )
        if metrics.get("focused"):
            focused = True
            return metrics
    return {
        "focused": focused,
        "visible_focus": False,
        "focus_unobscured": False,
        "focus_rect_left": None,
        "focus_rect_top": None,
        "focus_rect_right": None,
        "focus_rect_bottom": None,
        "tag": "",
        "role": "",
    }


def _target_size_metrics(page: Any) -> dict[str, Any]:
    return dict(
        page.evaluate(
            """
            () => {
              const minimum = 24;
              const visible = (node) => {
                if (node.closest('[hidden], [aria-hidden="true"], details:not([open])')) return false;
                const style = getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden'
                  && rect.width > 0 && rect.height > 0 && !node.disabled;
              };
              const inlineTextLink = (node) => {
                if (!node.matches('a[href]') || getComputedStyle(node).display !== 'inline') return false;
                const parent = node.parentElement;
                return Boolean(parent && parent.textContent.trim().length > node.textContent.trim().length);
              };
              const normalizedRect = (rect) => ({
                left: rect.left,
                right: rect.right,
                top: rect.top,
                bottom: rect.bottom,
                width: rect.width,
                height: rect.height,
              });
              const bestRect = (rects) => normalizedRect(
                [...rects].sort((left, right) => {
                  const leftFloor = Math.min(left.width, left.height);
                  const rightFloor = Math.min(right.width, right.height);
                  return rightFloor - leftFloor
                    || (right.width * right.height) - (left.width * left.height);
                })[0]
              );
              const effectiveRect = (node) => {
                const rects = [node.getBoundingClientRect()];
                if ('labels' in node && node.labels) {
                  for (const label of Array.from(node.labels)) {
                    if (visible(label)) rects.push(label.getBoundingClientRect());
                  }
                }
                const wrappingLabel = node.closest('label');
                if (wrappingLabel && visible(wrappingLabel)) {
                  rects.push(wrappingLabel.getBoundingClientRect());
                }
                return bestRect(rects);
              };
              const targets = Array.from(document.querySelectorAll(
                'a[href], button, input:not([type="hidden"]), select, textarea, summary, '
                + '[role="button"], [role="link"], [role="checkbox"], [role="radio"], '
                + '[role="switch"], [tabindex]:not([tabindex="-1"])'
              )).filter(visible);
              const descriptors = targets.map((node) => {
                const rect = effectiveRect(node);
                return {
                  node,
                  rect,
                  centerX: rect.left + rect.width / 2,
                  centerY: rect.top + rect.height / 2,
                  undersized: rect.width + 0.01 < minimum || rect.height + 0.01 < minimum,
                };
              });
              const circleIntersectsRect = (target, other) => {
                const nearestX = Math.max(
                  other.rect.left,
                  Math.min(target.centerX, other.rect.right)
                );
                const nearestY = Math.max(
                  other.rect.top,
                  Math.min(target.centerY, other.rect.bottom)
                );
                return Math.hypot(target.centerX - nearestX, target.centerY - nearestY) < 12 - 0.01;
              };
              const spacingExceptionApplies = (target) => descriptors.every((other) => {
                if (other.node === target.node) return true;
                if (other.undersized) {
                  return Math.hypot(
                    target.centerX - other.centerX,
                    target.centerY - other.centerY
                  ) >= minimum - 0.01;
                }
                return !circleIntersectsRect(target, other);
              });
              const failures = [];
              let spacingExceptionCount = 0;
              for (const target of descriptors) {
                const node = target.node;
                if (inlineTextLink(node)) continue;
                const rect = target.rect;
                if (!target.undersized) continue;
                if (spacingExceptionApplies(target)) {
                  spacingExceptionCount += 1;
                  continue;
                }
                failures.push({
                  tag: String(node.tagName || '').toLowerCase(),
                  role: String(node.getAttribute('role') || ''),
                  type: String(node.getAttribute('type') || ''),
                  width: Math.round(rect.width * 100) / 100,
                  height: Math.round(rect.height * 100) / 100,
                });
              }
              return {
                target_size_minimum_css_px: minimum,
                target_count: targets.length,
                target_size_spacing_exception_count: spacingExceptionCount,
                undersized_target_count: failures.length,
                undersized_targets: failures.slice(0, 25),
              };
            }
            """
        )
        or {}
    )


def _dialog_focus_metrics(page: Any) -> dict[str, Any]:
    opener_found = bool(
        page.evaluate(
            """
            () => {
              const visible = (node) => {
                const style = getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden'
                  && rect.width > 0 && rect.height > 0 && !node.disabled;
              };
              const candidates = Array.from(document.querySelectorAll(
                '[data-location-map-open], [aria-haspopup="dialog"], button[aria-controls], [data-dialog-trigger]'
              )).filter(visible);
              const opener = candidates.find((node) => {
                if (node.matches('[data-location-map-open], [aria-haspopup="dialog"], [data-dialog-trigger]')) return true;
                const controlled = document.getElementById(node.getAttribute('aria-controls') || '');
                return Boolean(controlled && controlled.matches('dialog, [role="dialog"], [aria-modal="true"]'));
              });
              if (!opener) return false;
              opener.setAttribute('data-pq-a11y-dialog-opener', '1');
              return true;
            }
            """
        )
    )
    if not opener_found:
        return {
            "dialog_applicable": False,
            "dialog_focus_contained": True,
            "dialog_escape_closes": True,
            "dialog_focus_restored": True,
        }
    opener = page.locator('[data-pq-a11y-dialog-opener="1"]').first
    opener.focus()
    page.keyboard.press("Enter")
    page.wait_for_timeout(150)
    dialog = page.locator('[role="dialog"]:visible, dialog[open], [aria-modal="true"]:visible').first
    if dialog.count() == 0:
        return {
            "dialog_applicable": True,
            "dialog_focus_contained": False,
            "dialog_escape_closes": False,
            "dialog_focus_restored": False,
        }
    initial_focus_inside = bool(
        dialog.evaluate("node => node === document.activeElement || node.contains(document.activeElement)")
    )
    focus_contained = initial_focus_inside
    for _ in range(8):
        page.keyboard.press("Tab")
        if not bool(dialog.evaluate("node => node === document.activeElement || node.contains(document.activeElement)")):
            focus_contained = False
            break
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    dialog_closed = dialog.count() == 0 or not dialog.is_visible()
    focus_restored = bool(
        page.evaluate(
            """
            () => {
              const opener = document.querySelector('[data-pq-a11y-dialog-opener="1"]');
              return Boolean(opener && (opener === document.activeElement || opener.contains(document.activeElement)));
            }
            """
        )
    )
    return {
        "dialog_applicable": True,
        "dialog_focus_contained": focus_contained,
        "dialog_escape_closes": dialog_closed,
        "dialog_focus_restored": focus_restored,
    }


def _page_semantic_metrics(page: Any) -> dict[str, Any]:
    return dict(
        page.evaluate(
            """
            () => {
              const visible = (node) => {
                const style = getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
              };
              const errors = Array.from(document.querySelectorAll(
                '[role="alert"], [aria-invalid="true"], [aria-errormessage], .error, .field-error, [data-error]'
              )).filter(visible);
              const errorSemantics = errors.every((node) => {
                if (node.getAttribute('role') === 'alert') return true;
                if (node.getAttribute('aria-invalid') === 'true') {
                  return Boolean(node.getAttribute('aria-describedby') || node.getAttribute('aria-errormessage'));
                }
                return Boolean(node.closest('[role="alert"]') || node.getAttribute('aria-live'));
              });
              const progress = Array.from(document.querySelectorAll(
                '[role="status"], [role="progressbar"], [aria-live], [aria-busy="true"], progress'
              )).filter(visible);
              const progressSemantics = progress.every((node) => Boolean(
                node.matches('progress, [role="status"], [role="progressbar"]')
                || node.getAttribute('aria-live')
                || node.getAttribute('aria-busy') === 'true'
              ));
              const animations = document.getAnimations ? document.getAnimations() : [];
              const activeMotion = animations.filter((animation) => {
                if (animation.playState !== 'running') return false;
                const timing = animation.effect && animation.effect.getComputedTiming
                  ? animation.effect.getComputedTiming()
                  : {};
                return Number(timing.duration || 0) > 100;
              });
              return {
                error_state_count: errors.length,
                error_semantics_valid: errorSemantics,
                live_progress_count: progress.length,
                live_progress_semantics_valid: progressSemantics,
                reduced_motion_media_matches: matchMedia('(prefers-reduced-motion: reduce)').matches,
                active_motion_count: activeMotion.length,
              };
            }
            """
        )
        or {}
    )


def _valid_reflow_measurement(
    measurement: dict[str, Any],
    *,
    expected_viewport_width: int,
) -> bool:
    viewport_width = measurement.get("reflow_viewport_width")
    scroll_width = measurement.get("reflow_scroll_width")
    clipped_count = measurement.get("reflow_clipped_interactive_count")
    clipped = measurement.get("reflow_clipped_interactives")
    without_horizontal_scroll = measurement.get(
        "reflow_without_horizontal_scroll"
    )
    if (
        type(viewport_width) is not int
        or viewport_width != expected_viewport_width
        or type(scroll_width) is not int
        or scroll_width <= 0
        or type(clipped_count) is not int
        or clipped_count < 0
        or type(without_horizontal_scroll) is not bool
        or not isinstance(clipped, list)
        or len(clipped) != min(clipped_count, 25)
        or any(not isinstance(row, dict) for row in clipped)
    ):
        return False
    return without_horizontal_scroll is (
        scroll_width <= viewport_width + 2
    )


def _reflow_measurement_signature(measurement: dict[str, Any]) -> str:
    return json.dumps(
        {
            "reflow_viewport_width": measurement.get("reflow_viewport_width"),
            "reflow_scroll_width": measurement.get("reflow_scroll_width"),
            "reflow_without_horizontal_scroll": measurement.get(
                "reflow_without_horizontal_scroll"
            ),
            "reflow_clipped_interactive_count": measurement.get(
                "reflow_clipped_interactive_count"
            ),
            "reflow_clipped_interactives": measurement.get(
                "reflow_clipped_interactives"
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _settled_reflow_measurement(
    page: Any,
    *,
    expected_viewport_width: int,
    required_stable_samples: int = _REFLOW_STABLE_SAMPLE_COUNT,
    sample_interval_ms: int = _REFLOW_SAMPLE_INTERVAL_MS,
    settle_timeout_ms: int = _REFLOW_SETTLE_TIMEOUT_MS,
) -> dict[str, Any]:
    if expected_viewport_width <= 0:
        raise ValueError("reflow_expected_viewport_width_invalid")
    if required_stable_samples < 2:
        raise ValueError("reflow_required_stable_samples_invalid")
    if sample_interval_ms <= 0:
        raise ValueError("reflow_sample_interval_ms_invalid")
    if settle_timeout_ms < sample_interval_ms:
        raise ValueError("reflow_settle_timeout_ms_invalid")

    previous_signature = ""
    stable_sample_count = 0
    sample_count = 0
    waited_ms = 0
    settled = False
    measurement: dict[str, Any] = {}
    while True:
        raw_measurement = page.evaluate(_REFLOW_MEASUREMENT_SCRIPT)
        measurement = (
            dict(raw_measurement) if isinstance(raw_measurement, dict) else {}
        )
        sample_count += 1
        if _valid_reflow_measurement(
            measurement,
            expected_viewport_width=expected_viewport_width,
        ):
            signature = _reflow_measurement_signature(measurement)
            if signature == previous_signature:
                stable_sample_count += 1
            else:
                previous_signature = signature
                stable_sample_count = 1
        else:
            previous_signature = ""
            stable_sample_count = 0

        if waited_ms >= settle_timeout_ms:
            # Settlement is symmetric: only the final consecutive signature
            # at the end of the full observation window counts.  Its geometry
            # decides pass/fail; invalid or oscillating measurements fail.
            settled = stable_sample_count >= required_stable_samples
            break
        wait_ms = min(sample_interval_ms, settle_timeout_ms - waited_ms)
        page.wait_for_timeout(wait_ms)
        waited_ms += wait_ms

    observed_without_horizontal_scroll = (
        measurement.get("reflow_without_horizontal_scroll") is True
    )
    return {
        "reflow_viewport_width": measurement.get("reflow_viewport_width", 0),
        "reflow_scroll_width": measurement.get("reflow_scroll_width", 0),
        "reflow_without_horizontal_scroll": (
            settled and observed_without_horizontal_scroll
        ),
        "reflow_observed_without_horizontal_scroll": (
            observed_without_horizontal_scroll
        ),
        "reflow_clipped_interactive_count": measurement.get(
            "reflow_clipped_interactive_count", -1
        ),
        "reflow_clipped_interactives": list(
            measurement.get("reflow_clipped_interactives") or []
        ),
        "reflow_measurement_settled": settled,
        "reflow_measurement_sample_count": sample_count,
        "reflow_measurement_stable_sample_count": stable_sample_count,
        "reflow_measurement_required_stable_sample_count": (
            required_stable_samples
        ),
        "reflow_measurement_waited_ms": waited_ms,
        "reflow_measurement_timeout_ms": settle_timeout_ms,
    }


def _zoom_reflow_metrics(
    measurement: dict[str, Any],
    *,
    zoom_percent: int,
) -> dict[str, Any]:
    if zoom_percent == 200:
        return {
            "zoom_percent": zoom_percent,
            **measurement,
        }
    if zoom_percent != 400:
        raise ValueError("reflow_zoom_percent_unsupported")
    return {
        "zoom_400_percent": zoom_percent,
        "zoom_400_viewport_width": measurement.get(
            "reflow_viewport_width", 0
        ),
        "zoom_400_scroll_width": measurement.get("reflow_scroll_width", 0),
        "zoom_400_reflow_without_horizontal_scroll": measurement.get(
            "reflow_without_horizontal_scroll"
        )
        is True,
        "zoom_400_reflow_observed_without_horizontal_scroll": measurement.get(
            "reflow_observed_without_horizontal_scroll"
        )
        is True,
        "zoom_400_clipped_interactive_count": measurement.get(
            "reflow_clipped_interactive_count", -1
        ),
        "zoom_400_clipped_interactives": list(
            measurement.get("reflow_clipped_interactives") or []
        ),
        "zoom_400_measurement_settled": measurement.get(
            "reflow_measurement_settled"
        )
        is True,
        "zoom_400_measurement_sample_count": measurement.get(
            "reflow_measurement_sample_count", 0
        ),
        "zoom_400_measurement_stable_sample_count": measurement.get(
            "reflow_measurement_stable_sample_count", 0
        ),
        "zoom_400_measurement_required_stable_sample_count": measurement.get(
            "reflow_measurement_required_stable_sample_count", 0
        ),
        "zoom_400_measurement_waited_ms": measurement.get(
            "reflow_measurement_waited_ms", 0
        ),
        "zoom_400_measurement_timeout_ms": measurement.get(
            "reflow_measurement_timeout_ms", 0
        ),
    }


def _axe_row_is_wcag_tagged(row: dict[str, Any]) -> bool:
    return any(
        str(tag or "").strip().lower().startswith("wcag")
        for tag in list(row.get("tags") or [])
    )


def _axe_moderate_or_higher_wcag_violations(
    violations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        row
        for row in violations
        if str(row.get("impact") or "").strip().lower()
        in {"moderate", "serious", "critical"}
        and _axe_row_is_wcag_tagged(row)
    ]


def _install_axe_core(context: Any, *, axe_source: str) -> None:
    # Playwright init scripts are evaluated before document scripts and are not
    # inline script elements.  That keeps the pinned audit engine available on
    # nonce-only CSP pages without weakening the application's CSP.
    context.add_init_script(script=axe_source)


def _axe_metrics(page: Any) -> dict[str, Any]:
    axe_version = str(page.evaluate("() => window.axe && window.axe.version || ''") or "")
    if axe_version != AXE_CORE_VERSION:
        raise RuntimeError(f"axe_core_version_mismatch:expected={AXE_CORE_VERSION}:observed={axe_version or 'missing'}")
    result = dict(
        page.evaluate(
            """
            async () => await window.axe.run(document, {
              runOnly: {
                type: 'tag',
                values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']
              },
              resultTypes: ['violations', 'incomplete']
            })
            """
        )
        or {}
    )
    violations = [dict(row) for row in list(result.get("violations") or []) if isinstance(row, dict)]
    serious = [row for row in violations if str(row.get("impact") or "") in {"serious", "critical"}]
    moderate_or_higher_wcag = _axe_moderate_or_higher_wcag_violations(violations)
    contrast_violations = [row for row in violations if str(row.get("id") or "") == "color-contrast"]
    contrast_incomplete = [
        dict(row)
        for row in list(result.get("incomplete") or [])
        if isinstance(row, dict) and str(row.get("id") or "") == "color-contrast"
    ]
    return {
        "axe_core_version": axe_version,
        "axe_violation_count": len(violations),
        "axe_serious_critical_count": len(serious),
        "axe_moderate_or_higher_wcag_count": len(moderate_or_higher_wcag),
        "axe_moderate_or_higher_wcag_violations": [
            {
                "id": str(row.get("id") or ""),
                "impact": str(row.get("impact") or ""),
                "help": str(row.get("help") or ""),
                "node_count": len(list(row.get("nodes") or [])),
                "wcag_tags": sorted(
                    str(tag)
                    for tag in list(row.get("tags") or [])
                    if str(tag or "").strip().lower().startswith("wcag")
                ),
            }
            for row in moderate_or_higher_wcag
        ],
        "axe_serious_critical_violations": [
            {
                "id": str(row.get("id") or ""),
                "impact": str(row.get("impact") or ""),
                "help": str(row.get("help") or ""),
                "node_count": len(list(row.get("nodes") or [])),
            }
            for row in serious
        ],
        "contrast_violation_count": len(contrast_violations),
        "contrast_incomplete_count": len(contrast_incomplete),
    }


def collect_accessibility_engine_rows(
    *,
    base_url: str,
    routes: tuple[str, ...],
    browser_engine: str,
    headers: dict[str, str],
    axe_source: str,
    timeout_ms: int,
    release_probe_secret: str = "",
    configured_routes: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    # Playwright starts a driver process before the named browser.  Remove both
    # probe credential environment names first; request signing continues from
    # the explicit in-memory value passed to this function.
    scrub_release_probe_secret_environment()
    from playwright.sync_api import sync_playwright

    engine = normalize_playwright_engine(browser_engine)
    authorized_origin = _origin(base_url)
    effective_configured_routes = tuple(
        dict.fromkeys((*configured_routes, *_release_probe_configured_routes(routes)))
    )
    rows: list[dict[str, Any]] = []
    with sync_playwright() as playwright:
        browser_type = playwright_browser_type(playwright, engine=engine)
        try:
            browser = browser_type.launch(
                **playwright_engine_launch_kwargs(
                    playwright,
                    engine=engine,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
            )
        except Exception as exc:
            raise RuntimeError(f"playwright_browser_engine_unavailable:{engine}:{type(exc).__name__}: {exc}") from exc
        try:
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                reduced_motion="reduce",
                service_workers="block",
            )
            _install_axe_core(context, axe_source=axe_source)
            context.route(
                "**/*",
                lambda route: _continue_with_origin_scoped_headers(
                    route,
                    authorized_origin=authorized_origin,
                    headers=headers,
                    release_probe_secret=release_probe_secret,
                    configured_routes=effective_configured_routes,
                ),
            )
            try:
                for route in routes:
                    url = base_url.rstrip("/") + "/" + route.lstrip("/")
                    page = context.new_page()
                    page.set_default_timeout(timeout_ms)
                    page.set_default_navigation_timeout(timeout_ms)
                    try:
                        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                        page.wait_for_timeout(250)
                        final_url = str(page.url or "")
                        status_code = int(response.status) if response is not None else 0
                        navigation_ok, navigation_contract = _navigation_contract(
                            requested_url=url,
                            final_url=final_url,
                            status_code=status_code,
                        )
                        metrics: dict[str, Any] = {
                            "browser_engine": engine,
                            "status_code": status_code,
                            "navigation_committed": response is not None,
                            "requested_url": url,
                            "final_url": final_url,
                            "route_document_loaded": navigation_ok,
                            "navigation_contract": navigation_contract,
                        }
                        metrics.update(_axe_metrics(page))
                        metrics.update(_focus_metrics(page))
                        metrics.update(_target_size_metrics(page))
                        metrics.update(_dialog_focus_metrics(page))
                        metrics.update(_page_semantic_metrics(page))
                        page.set_viewport_size({"width": 640, "height": 900})
                        metrics.update(
                            _zoom_reflow_metrics(
                                _settled_reflow_measurement(
                                    page,
                                    expected_viewport_width=640,
                                ),
                                zoom_percent=200,
                            )
                        )
                        page.set_viewport_size({"width": 320, "height": 900})
                        metrics.update(
                            _zoom_reflow_metrics(
                                _settled_reflow_measurement(
                                    page,
                                    expected_viewport_width=320,
                                ),
                                zoom_percent=400,
                            )
                        )
                    except Exception as exc:
                        metrics = {
                            "browser_engine": engine,
                            "status_code": 0,
                            "route_document_loaded": False,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    checks = evaluate_accessibility_metrics(metrics)
                    rows.append(
                        {
                            "route": route,
                            "url": url,
                            "browser_engine": engine,
                            "ok": all(check.get("ok") is True for check in checks),
                            "checks": checks,
                            "metrics": metrics,
                        }
                    )
                    page.close()
            finally:
                context.close()
        finally:
            browser.close()
    return rows


def evaluate_accessibility_metrics(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    dialog_ok = (
        metrics.get("dialog_focus_contained") is True
        and metrics.get("dialog_escape_closes") is True
        and metrics.get("dialog_focus_restored") is True
    )
    try:
        zoom_400_percent = int(metrics["zoom_400_percent"])
        zoom_400_viewport_width = int(metrics["zoom_400_viewport_width"])
        zoom_400_scroll_width = int(metrics["zoom_400_scroll_width"])
        zoom_400_clipped_interactive_count = int(
            metrics["zoom_400_clipped_interactive_count"]
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        zoom_400_percent = 0
        zoom_400_viewport_width = 0
        zoom_400_scroll_width = 0
        zoom_400_clipped_interactive_count = -1
    moderate_or_higher_wcag_count = metrics.get("axe_moderate_or_higher_wcag_count")
    target_size_minimum = metrics.get("target_size_minimum_css_px")
    undersized_target_count = metrics.get("undersized_target_count")
    return [
        {"name": "route_document_loaded", "ok": metrics.get("route_document_loaded") is True},
        {"name": "axe_core_version_pinned", "ok": metrics.get("axe_core_version") == AXE_CORE_VERSION},
        {
            "name": "axe_no_moderate_or_higher_wcag_violations",
            "ok": type(moderate_or_higher_wcag_count) is int
            and moderate_or_higher_wcag_count == 0
            and metrics.get("axe_core_version") == AXE_CORE_VERSION,
        },
        {"name": "keyboard_only_navigation", "ok": metrics.get("focused") is True},
        {"name": "visible_keyboard_focus", "ok": metrics.get("visible_focus") is True},
        {"name": "focus_not_obscured", "ok": metrics.get("focus_unobscured") is True},
        {
            "name": "target_size_24_css_px_or_spacing",
            "ok": type(target_size_minimum) in {int, float}
            and not isinstance(target_size_minimum, bool)
            and float(target_size_minimum) >= 24.0
            and type(undersized_target_count) is int
            and undersized_target_count == 0,
        },
        {"name": "dialog_focus_contract", "ok": dialog_ok, "applicable": bool(metrics.get("dialog_applicable"))},
        {"name": "semantic_error_states", "ok": metrics.get("error_semantics_valid") is True},
        {"name": "semantic_live_progress_states", "ok": metrics.get("live_progress_semantics_valid") is True},
        {
            "name": "zoom_200_reflow",
            "ok": int(metrics.get("zoom_percent") or 0) == 200
            and metrics.get("reflow_without_horizontal_scroll") is True
            and type(metrics.get("reflow_clipped_interactive_count")) is int
            and metrics.get("reflow_clipped_interactive_count") == 0,
        },
        {
            "name": "zoom_400_reflow",
            "ok": zoom_400_percent == 400
            and zoom_400_viewport_width == 320
            and 0 < zoom_400_scroll_width <= zoom_400_viewport_width + 2
            and metrics.get("zoom_400_reflow_without_horizontal_scroll") is True
            and zoom_400_clipped_interactive_count == 0,
        },
        {
            "name": "contrast_signals_clear",
            "ok": int(metrics.get("contrast_violation_count") or 0) == 0
            and int(metrics.get("contrast_incomplete_count") or 0) == 0
            and metrics.get("axe_core_version") == AXE_CORE_VERSION,
        },
        {
            "name": "reduced_motion_honored",
            "ok": metrics.get("reduced_motion_media_matches") is True
            and int(metrics.get("active_motion_count") or 0) == 0,
        },
    ]


def build_accessibility_receipt(
    *,
    base_url: str,
    routes: tuple[str, ...] = DEFAULT_ACCESSIBILITY_ROUTES,
    browser_engines: tuple[str, ...] = SUPPORTED_PLAYWRIGHT_ENGINES,
    api_token: str = "",
    principal_id: str = "pq-accessibility-gate",
    release_probe_secret: str = "",
    axe_core_path: Path = DEFAULT_AXE_CORE_PATH,
    timeout_ms: int = 30_000,
    collect_engine_rows: Callable[..., list[dict[str, Any]]] = collect_accessibility_engine_rows,
) -> dict[str, Any]:
    engines = normalize_browser_engines(browser_engines)
    normalized_probe_secret = str(release_probe_secret or "").strip()
    normalized_api_token = str(api_token or "").strip()
    normalized_principal_id = str(principal_id or "").strip()
    receipt_secrets = (normalized_api_token, normalized_probe_secret)
    if (
        _routes_require_authenticated_app(routes)
        and not normalized_probe_secret
        and not (normalized_api_token or normalized_principal_id)
    ):
        return _redact_receipt_value(
            {
                "status": "blocked",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "base_url": base_url,
                "axe_core_version": AXE_CORE_VERSION,
                "axe_core_path": str(axe_core_path.expanduser().resolve()),
                "required_browser_engines": list(engines),
                "configured_routes": list(routes),
                "route_count": 0,
                "failed_count": 1,
                "checks": [
                    {
                        "name": "authenticated_app_probe_auth_configured",
                        "ok": False,
                        "reason": "Authenticated accessibility app routes require --release-probe-secret-stdin or legacy API/principal auth.",
                    }
                ],
                "routes": [],
            },
            secrets=receipt_secrets,
        )
    try:
        axe_source = resolve_axe_core_source(axe_core_path)
    except Exception as exc:
        return _redact_receipt_value(
            {
                "status": "blocked",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "base_url": base_url,
                "axe_core_version": AXE_CORE_VERSION,
                "axe_core_path": str(axe_core_path.expanduser().resolve()),
                "required_browser_engines": list(engines),
                "configured_routes": list(routes),
                "route_count": 0,
                "failed_count": 1,
                "checks": [{"name": "axe_core_pinned_input", "ok": False, "reason": str(exc)}],
                "routes": [],
            },
            secrets=receipt_secrets,
        )
    headers = {"Accept": "text/html,application/xhtml+xml"}
    if not normalized_probe_secret and normalized_principal_id:
        headers["X-EA-Principal-ID"] = normalized_principal_id
    if not normalized_probe_secret and normalized_api_token:
        headers.update(
            {
                "Authorization": f"Bearer {normalized_api_token}",
                "X-EA-API-Token": normalized_api_token,
                "X-API-Token": normalized_api_token,
            }
        )
    configured_probe_routes = _release_probe_configured_routes(routes)
    rows: list[dict[str, Any]] = []
    engine_failures: list[dict[str, Any]] = []
    for engine in engines:
        try:
            rows.extend(
                collect_engine_rows(
                    base_url=base_url,
                    routes=routes,
                    browser_engine=engine,
                    headers=headers,
                    axe_source=axe_source,
                    timeout_ms=timeout_ms,
                    release_probe_secret=normalized_probe_secret,
                    configured_routes=configured_probe_routes,
                )
            )
        except Exception as exc:
            engine_failures.append(
                {"browser_engine": engine, "error": f"{type(exc).__name__}: {exc}"}
            )
    expected_samples = {(engine, route) for engine in engines for route in routes}
    observed_samples = {
        (str(row.get("browser_engine") or ""), str(row.get("route") or ""))
        for row in rows
    }
    missing_samples = sorted(expected_samples - observed_samples)
    dialog_sample_count = sum(
        1
        for row in rows
        if isinstance(row.get("metrics"), dict) and row["metrics"].get("dialog_applicable") is True
    )
    literal_placeholder_routes = [
        str(route or "") for route in routes if _route_has_literal_placeholder(route)
    ]
    research_detail_routes = [
        str(route or "") for route in routes if _concrete_research_detail_route(route)
    ]
    shortlist_run_routes = [
        str(route or "") for route in routes if _concrete_shortlist_run_route(route)
    ]
    public_tour_routes = [
        str(route or "") for route in routes if _concrete_public_tour_route(route)
    ]
    configured_route_paths = {
        str(route or "").split("?", 1)[0].rstrip("/") or "/"
        for route in routes
    }
    missing_public_information_routes = [
        route for route in PUBLIC_INFORMATION_ROUTES if route not in configured_route_paths
    ]
    missing_flagship_static_routes = [
        route for route in DEFAULT_ACCESSIBILITY_ROUTES if route not in configured_route_paths
    ]
    checks = [
        {"name": "axe_core_pinned_input", "ok": True, "version": AXE_CORE_VERSION},
        {
            "name": "accessibility_route_engine_matrix_complete",
            "ok": not missing_samples and not engine_failures,
            "missing_samples": [
                {"browser_engine": engine, "route": route}
                for engine, route in missing_samples
            ],
            "engine_failures": engine_failures,
        },
        {
            "name": "public_information_route_matrix_configured",
            "ok": not missing_public_information_routes,
            "required_routes": list(PUBLIC_INFORMATION_ROUTES),
            "missing_routes": missing_public_information_routes,
        },
        {
            "name": "flagship_static_route_matrix_configured",
            "ok": not missing_flagship_static_routes,
            "required_routes": list(DEFAULT_ACCESSIBILITY_ROUTES),
            "missing_routes": missing_flagship_static_routes,
        },
        {
            "name": "literal_route_placeholders_absent",
            "ok": not literal_placeholder_routes,
            "placeholder_routes": literal_placeholder_routes,
        },
        {
            "name": "research_detail_route_configured",
            "ok": bool(research_detail_routes),
            "required_route_prefix": "/app/research/",
            "matched_routes": research_detail_routes,
        },
        {
            "name": "shortlist_run_route_configured",
            "ok": bool(shortlist_run_routes),
            "required_route_prefix": "/app/shortlist/run/",
            "matched_routes": shortlist_run_routes,
        },
        {
            "name": "public_tour_route_configured",
            "ok": bool(public_tour_routes),
            "required_route_prefix": "/tours/",
            "matched_routes": public_tour_routes,
        },
        {
            "name": "dialog_focus_interaction_sampled",
            "ok": dialog_sample_count > 0,
            "sample_count": dialog_sample_count,
        },
    ]
    failed_rows = [row for row in rows if row.get("ok") is not True]
    failed_checks = [check for check in checks if check.get("ok") is not True]
    receipt = {
        "status": "pass" if not failed_rows and not failed_checks else "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "axe_core_version": AXE_CORE_VERSION,
        "axe_core_path": str(axe_core_path.expanduser().resolve()),
        "required_browser_engines": list(engines),
        "observed_browser_engines": sorted(
            {
                str(row.get("browser_engine") or "").strip()
                for row in rows
                if str(row.get("browser_engine") or "").strip()
            }
        ),
        "configured_routes": list(routes),
        "expected_sample_count": len(expected_samples),
        "observed_sample_count": len(observed_samples),
        "route_count": len(rows),
        "failed_count": len(failed_rows) + len(failed_checks),
        "dialog_interaction_sample_count": dialog_sample_count,
        "checks": checks,
        "routes": rows,
        "engine_failures": engine_failures,
        "manual_assistive_technology_evidence": {
            "status": "external_evidence_required",
            "required_for_launch": True,
            "satisfied_by_this_receipt": False,
        },
        "notes": [
            "Axe is injected only from the pinned local input; this gate never downloads scripts or uses a CDN.",
            "The 200% reflow check uses a 640 CSS-pixel viewport as the cross-engine equivalent of zooming a 1280-pixel layout to 200%.",
            "The 400% reflow check uses a 320 CSS-pixel viewport and rejects horizontal document overflow or unreachable clipped controls while allowing controls inside an explicit horizontal scroll rail.",
            "Navigation must remain on the configured origin and exact path; only the named billing, app-support, and same-slug public-tour control redirects are accepted and receipted.",
            "The route matrix preserves every Gold static customer route and requires concrete research-detail, shortlist-run, and first-party public-tour samples.",
            "This automated structural receipt does not replace manual screen-reader, voice-control, keyboard, cognitive, or physical-device review; independently attested manual assistive-technology evidence remains a launch blocker.",
        ],
    }
    receipt = _redact_receipt_value(receipt, secrets=receipt_secrets)
    serialized = json.dumps(receipt, sort_keys=True)
    if any(secret and secret in serialized for secret in receipt_secrets):
        raise RuntimeError("accessibility_receipt_secret_leak")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the strict PropertyQuarry accessibility route/engine gate.")
    parser.add_argument("--base-url", default=os.environ.get("PROPERTYQUARRY_ACCESSIBILITY_BASE_URL", "http://127.0.0.1:8097"))
    configured_dynamic_routes = tuple(
        str(os.environ.get(name) or "").strip()
        for name in (
            "PROPERTYQUARRY_ACCESSIBILITY_RESEARCH_DETAIL_ROUTE",
            "PROPERTYQUARRY_ACCESSIBILITY_SHORTLIST_RUN_ROUTE",
            "PROPERTYQUARRY_ACCESSIBILITY_PUBLIC_TOUR_ROUTE",
        )
        if str(os.environ.get(name) or "").strip()
    )
    default_routes = tuple(
        dict.fromkeys((*DEFAULT_ACCESSIBILITY_ROUTES, *configured_dynamic_routes))
    )
    parser.add_argument("--routes", default=",".join(default_routes))
    parser.add_argument(
        "--browser-engines",
        default=os.environ.get("PROPERTYQUARRY_ACCESSIBILITY_BROWSER_ENGINES", ",".join(SUPPORTED_PLAYWRIGHT_ENGINES)),
    )
    parser.add_argument("--axe-core-path", default=os.environ.get("PROPERTYQUARRY_AXE_CORE_PATH", str(DEFAULT_AXE_CORE_PATH)))
    parser.add_argument("--api-token", default=os.environ.get("PROPERTYQUARRY_ACCESSIBILITY_API_TOKEN") or os.environ.get("EA_API_TOKEN", ""))
    parser.add_argument("--principal-id", default=os.environ.get("PROPERTYQUARRY_ACCESSIBILITY_PRINCIPAL_ID", "pq-accessibility-gate"))
    parser.add_argument(
        "--release-probe-secret-stdin",
        action="store_true",
        help="Read the protected release-probe credential once from bounded stdin.",
    )
    parser.add_argument("--timeout-ms", type=int, default=30_000)
    parser.add_argument("--write", default="_completion/smoke/property-live-accessibility-latest.json")
    args = parser.parse_args()
    release_probe_secret = read_release_probe_secret_from_stdin(
        parser,
        enabled=bool(args.release_probe_secret_stdin),
    )
    scrub_release_probe_secret_environment()
    try:
        engines = normalize_browser_engines(
            tuple(engine.strip() for engine in str(args.browser_engines or "").split(",") if engine.strip())
        )
    except ValueError as exc:
        parser.error(str(exc))
    routes = tuple(route.strip() for route in str(args.routes or "").split(",") if route.strip())
    receipt = build_accessibility_receipt(
        base_url=str(args.base_url).strip(),
        routes=routes or DEFAULT_ACCESSIBILITY_ROUTES,
        browser_engines=engines,
        api_token=str(args.api_token or "").strip(),
        principal_id=str(args.principal_id or "").strip() or "pq-accessibility-gate",
        release_probe_secret=release_probe_secret,
        axe_core_path=Path(args.axe_core_path),
        timeout_ms=max(1_000, int(args.timeout_ms)),
    )
    output = json.dumps(receipt, indent=2, sort_keys=True)
    if args.write:
        output_path = Path(args.write)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if receipt.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
