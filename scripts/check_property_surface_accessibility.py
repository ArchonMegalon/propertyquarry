#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

SURFACE_TEMPLATES = (
    "ea/app/templates/propertyquarry_home.html",
    "ea/app/templates/pricing_page.html",
    "ea/app/templates/sign_in.html",
    "ea/app/templates/register.html",
    "ea/app/templates/security_page.html",
    "ea/app/templates/docs_page.html",
    "ea/app/templates/integrations_page.html",
    "ea/app/templates/data_deletion.html",
    "ea/app/templates/base_public.html",
    "ea/app/templates/base_console.html",
    "ea/app/templates/app/property_decision_workbench.html",
    "ea/app/templates/app/property_research_detail.html",
    "ea/app/templates/app/property_packets.html",
    "ea/app/templates/app/_property_results_list.html",
    "ea/app/templates/app/_property_running_panel.html",
    "ea/app/templates/app/_property_search_agents_panel.html",
    "ea/app/templates/app/_property_account_panel.html",
    "ea/app/templates/app/_property_billing_panel.html",
    "ea/app/templates/app/_property_selected_review_panel.html",
)

FORBIDDEN_CUSTOMER_NOISE = (
    "billing truth",
    "plan and limits",
    "refresh delivery",
    "repair status checked",
    "what happened",
    "what still worked",
    "main blocker",
    "best next move",
    "release gate",
    "review gates",
    "visual checks",
    "proof",
    "search posture",
    "account posture",
    "latest run posture",
    "saved posture",
    "billing posture",
    "plan and billing posture",
    "energy posture",
    "running-cost posture",
    "authority posture",
    "governed review",
    "workspace diagnostics bundle",
    "support posture",
    "runtime posture",
    "runtime and provider posture",
    "provider risk",
    "provider posture",
    "health score",
    "fix verification",
    "channel receipt",
    "install receipt",
    "support bundle",
    "export bundle",
    "open bundle",
    "grounded packet",
    "outcome posture",
    "follow-up artifacts",
    "artifacts ready",
    "provider failures",
    "proof of value",
    "supporting proof",
    "operator center",
)

ALLOWED_HASH_TARGETS = {
    "#main-content",
    "#pqx-main-content",
    "#results-list",
    "#pqx-filtered-breakdown",
    "#sign-in-options",
}
FOCUSABLE_HASH_TARGETS = {"#main-content", "#pqx-main-content", "#sign-in-options"}

ACCESSIBILITY_PRIMITIVE_TEMPLATES = (
    "ea/app/templates/base_public.html",
    "ea/app/templates/base_console.html",
    "ea/app/templates/app/property_decision_workbench.html",
    "ea/app/templates/app/property_research_detail.html",
)

LIGHT_BACKGROUND_RE = re.compile(
    r"background(?:-color)?\s*:\s*"
    r"(?:"
    r"#fff(?:fff|[0-9a-f]{3})?\b|"
    r"white\b|"
    r"rgb\(\s*255\s*,\s*(?:255|253|252|251|250|249|248|244|241)\s*,\s*(?:255|253|252|249|248|245|242|241|237|232|228|221|214|208)\s*\)|"
    r"rgba\(\s*255\s*,\s*(?:255|253|252|251|250|249|248|244|241)\s*,\s*(?:255|253|252|249|248|245|242|241|237|232|228|221|214|208)\s*,\s*(?:0?\.(?:6[5-9]|[7-9][0-9]*)|1(?:\.0+)?)\s*\)|"
    r"color-mix\([^;{}]*(?:white|#ffffff)"
    r")",
    flags=re.IGNORECASE,
)
CSS_BLOCK_RE = re.compile(r"(?P<selectors>[^{}]+)\{(?P<body>[^{}]*)\}", flags=re.DOTALL)
CLASS_RE = re.compile(r"\.([a-zA-Z][a-zA-Z0-9_-]*)")
GENERIC_DARK_SUFFIXES = ("-card", "-panel", "-table", "-row", "-chip", "-pill", "-value")
GENERIC_DARK_ROOTS = ("base_public.html", "base_console.html")
MODIFIER_ONLY_CLASSES = {
    "active",
    "danger",
    "disabled",
    "good",
    "is-active",
    "is-blocked",
    "is-filter-recovered",
    "is-ready",
    "is-selected",
    "is-submitting",
    "primary",
    "subtle",
    "warn",
}


def _hex_to_rgb(value: str) -> tuple[float, float, float] | None:
    text = value.strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", text):
        return None
    return tuple(int(text[index : index + 2], 16) / 255.0 for index in (1, 3, 5))  # type: ignore[return-value]


def _relative_luminance(value: str) -> float | None:
    rgb = _hex_to_rgb(value)
    if rgb is None:
        return None

    def _channel(component: float) -> float:
        if component <= 0.03928:
            return component / 12.92
        return ((component + 0.055) / 1.055) ** 2.4

    red, green, blue = (_channel(component) for component in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast_ratio(foreground: str, background: str) -> float | None:
    foreground_luminance = _relative_luminance(foreground)
    background_luminance = _relative_luminance(background)
    if foreground_luminance is None or background_luminance is None:
        return None
    lighter = max(foreground_luminance, background_luminance)
    darker = min(foreground_luminance, background_luminance)
    return (lighter + 0.05) / (darker + 0.05)


def _css_vars(text: str, selector: str) -> dict[str, str]:
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\}}", text, flags=re.IGNORECASE | re.DOTALL)
    if match is None:
        return {}
    return {
        str(var_match.group("name")): str(var_match.group("value")).strip()
        for var_match in re.finditer(r"--(?P<name>[a-zA-Z0-9_-]+)\s*:\s*(?P<value>[^;]+);", match.group("body"))
    }


def _line_number(text: str, offset: int) -> int:
    return text[:offset].count("\n") + 1


def _attr(tag: str, name: str) -> str | None:
    match = re.search(rf"""\b{name}\s*=\s*(['"])(.*?)\1""", tag, flags=re.IGNORECASE | re.DOTALL)
    if match is None:
        return None
    return match.group(2)


def _visible_text(html_fragment: str) -> str:
    no_template = re.sub(r"{[#%{].*?[#}%]}", " ", html_fragment, flags=re.DOTALL)
    no_tags = re.sub(r"<[^>]+>", " ", no_template)
    return re.sub(r"\s+", " ", no_tags).strip()


def _check_customer_noise(path: Path, text: str, failures: list[str]) -> None:
    lowered = text.lower()
    for phrase in FORBIDDEN_CUSTOMER_NOISE:
        offset = lowered.find(phrase)
        if offset >= 0:
            failures.append(f"{path}:{_line_number(text, offset)} contains customer-facing noise phrase: {phrase}")


def _check_buttons(path: Path, text: str, failures: list[str]) -> None:
    for match in re.finditer(r"<button\b(?P<attrs>[^>]*)>(?P<body>.*?)</button>", text, flags=re.IGNORECASE | re.DOTALL):
        tag = match.group(0)
        attrs = match.group("attrs")
        body = match.group("body")
        line = _line_number(text, match.start())
        if re.search(r"\btype\s*=", attrs, flags=re.IGNORECASE) is None:
            failures.append(f"{path}:{line} button must declare type")
        label = _visible_text(body) or _attr(tag, "aria-label") or _attr(tag, "title")
        if not str(label or "").strip():
            failures.append(f"{path}:{line} button needs visible text, aria-label or title")


def _check_links(path: Path, text: str, failures: list[str]) -> None:
    for match in re.finditer(r"<a\b(?P<attrs>[^>]*)>", text, flags=re.IGNORECASE | re.DOTALL):
        tag = match.group(0)
        href = _attr(tag, "href")
        line = _line_number(text, match.start())
        if href is None:
            failures.append(f"{path}:{line} anchor must declare href")
            continue
        normalized = href.strip()
        if normalized in {"", "#"}:
            failures.append(f"{path}:{line} anchor href must not be empty or #")
        if normalized.lower().startswith("javascript:"):
            failures.append(f"{path}:{line} anchor must not use javascript: href")
        if normalized.startswith("#"):
            if normalized not in ALLOWED_HASH_TARGETS:
                failures.append(f"{path}:{line} hash link must target an approved in-page action")
                continue
            if normalized in FOCUSABLE_HASH_TARGETS:
                target_id = normalized[1:]
                target_match = re.search(
                    rf"<(?P<tag>[a-zA-Z][a-zA-Z0-9:-]*)\b(?P<attrs>[^>]*\bid\s*=\s*(['\"]){re.escape(target_id)}\3[^>]*)>",
                    text,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                if target_match is None:
                    failures.append(f"{path}:{line} approved hash link target must exist in the same surface")
                    continue
                target_tag = target_match.group(0)
                if (_attr(target_tag, "tabindex") or "").strip() not in {"-1", "0"}:
                    failures.append(f"{path}:{line} approved hash link target must accept keyboard focus")


def _check_images(path: Path, text: str, failures: list[str]) -> None:
    for match in re.finditer(r"<img\b(?P<attrs>[^>]*)>", text, flags=re.IGNORECASE | re.DOTALL):
        attrs = match.group("attrs")
        if re.search(r"\balt\s*=", attrs, flags=re.IGNORECASE) is None:
            failures.append(f"{path}:{_line_number(text, match.start())} image must declare alt text")


def _check_dialogs(path: Path, text: str, failures: list[str]) -> None:
    for match in re.finditer(r"<dialog\b(?P<attrs>[^>]*)>", text, flags=re.IGNORECASE | re.DOTALL):
        tag = match.group(0)
        line = _line_number(text, match.start())
        if not ((_attr(tag, "aria-label") or "").strip() or (_attr(tag, "aria-labelledby") or "").strip()):
            failures.append(f"{path}:{line} dialog needs aria-label or aria-labelledby")


def _check_accessibility_primitives(path: Path, text: str, failures: list[str]) -> None:
    relative = str(path.relative_to(ROOT))
    if relative not in ACCESSIBILITY_PRIMITIVE_TEMPLATES:
        return
    if "prefers-reduced-motion: reduce" not in text:
        failures.append(f"{path} must define a prefers-reduced-motion: reduce block")
    if ":focus-visible" not in text:
        failures.append(f"{path} must define visible focus styles")
    if relative in {"ea/app/templates/base_public.html", "ea/app/templates/base_console.html"}:
        for token in ("--touch-target:", "--touch-target-coarse:", "--focus-ring:"):
            if token not in text:
                failures.append(f"{path} must define {token.rstrip(':')}")
        if "@media (pointer: coarse)" not in text or "var(--touch-target-coarse)" not in text:
            failures.append(f"{path} must increase interactive targets for coarse pointers")
        if "min-height: var(--touch-target)" not in text:
            failures.append(f"{path} must use --touch-target for primary buttons")
    if relative == "ea/app/templates/base_console.html":
        if ".pq-mobile-nav" in text or "data-property-mobile-dock" in text:
            failures.append(f"{path} must not render legacy mobile bottom dock navigation")
        if ".pq-appbar-mobile-nav a" not in text or ".pq-appbar-mobile-nav span" not in text:
            failures.append(f"{path} must style the top mobile app navigation")
        if "min-height: var(--touch-target)" not in text:
            failures.append(f"{path} must give top mobile nav links a minimum touch target")
        if "var(--touch-target-coarse)" not in text:
            failures.append(f"{path} must enlarge top mobile nav links for coarse pointers")
    if relative == "ea/app/templates/app/property_decision_workbench.html":
        for token in ("--pq-touch-target:", "--pq-touch-target-coarse:", "--pq-focus-ring:"):
            if token not in text:
                failures.append(f"{path} must define {token.rstrip(':')}")
        if "@media (pointer: coarse)" not in text or "var(--pq-touch-target-coarse)" not in text:
            failures.append(f"{path} must increase workbench controls for coarse pointers")
        result_fact_index = text.find(".pqx-result-fact {")
        late_result_guard_index = text.find('html[data-pq-theme="dark"] .pqx-result-fact,', result_fact_index)
        if result_fact_index < 0 or late_result_guard_index < 0:
            failures.append(f"{path} must keep a late dark-mode result-control guard after result card styles")
    if relative == "ea/app/templates/app/property_research_detail.html":
        if "@media (pointer: coarse)" not in text or "var(--touch-target-coarse)" not in text:
            failures.append(f"{path} must increase research-detail controls for coarse pointers")
    if relative == "ea/app/templates/app/object_detail.html":
        if 'html[data-pq-theme="dark"] .object-media-toolbar' not in text:
            failures.append(f"{path} must darken the object media toolbar in dark mode")


def _check_contrast_tokens(path: Path, text: str, failures: list[str]) -> None:
    relative = str(path.relative_to(ROOT))
    if relative in {"ea/app/templates/base_public.html", "ea/app/templates/base_console.html"}:
        light_tokens = _css_vars(text, ":root")
        dark_tokens = _css_vars(text, 'html[data-pq-theme="dark"]')
        pairs = (
            ("text", "panel", 4.5),
            ("text-soft", "panel", 4.5),
            ("text-dim", "panel", 3.0),
            ("text", "bg", 4.5),
            ("text-soft", "bg", 4.5),
        )
        for label, tokens in (("light", light_tokens), ("dark", dark_tokens)):
            for foreground, background, minimum in pairs:
                ratio = _contrast_ratio(tokens.get(foreground, ""), tokens.get(background, ""))
                if ratio is None or ratio < minimum:
                    failures.append(
                        f"{path} {label} contrast {foreground} on {background} must be >= {minimum:g}:1"
                    )
        return
    elif relative == "ea/app/templates/app/property_decision_workbench.html":
        light_tokens = _css_vars(text, ":root")
        dark_tokens = _css_vars(text, 'html[data-pq-theme="dark"]')
        pairs = (
            ("pq-ink", "pq-paper", 4.5),
            ("pq-muted", "pq-paper", 4.5),
            ("pq-faint", "pq-paper", 3.0),
            ("pq-ink", "pq-panel", 4.5),
            ("pq-muted", "pq-panel", 4.5),
        )
        for label, tokens in (("light", light_tokens), ("dark", dark_tokens)):
            for foreground, background, minimum in pairs:
                ratio = _contrast_ratio(tokens.get(foreground, ""), tokens.get(background, ""))
                if ratio is None or ratio < minimum:
                    failures.append(
                        f"{path} {label} contrast {foreground} on {background} must be >= {minimum:g}:1"
                    )
        return
    else:
        return

    for foreground, background, minimum in pairs:
        ratio = _contrast_ratio(tokens.get(foreground, ""), tokens.get(background, ""))
        if ratio is None or ratio < minimum:
            failures.append(f"{path} contrast {foreground} on {background} must be >= {minimum:g}:1")


def _dark_selector_text(text: str) -> str:
    return "\n".join(
        match.group(0)
        for match in CSS_BLOCK_RE.finditer(text)
        if 'html[data-pq-theme="dark"]' in match.group("selectors")
    )


def _class_has_dark_guard(class_name: str, dark_text: str, *, template_name: str) -> bool:
    if class_name in MODIFIER_ONLY_CLASSES:
        return False
    if class_name.endswith(GENERIC_DARK_SUFFIXES):
        return True
    if template_name in GENERIC_DARK_ROOTS and (
        class_name.endswith(("-section",)) or class_name in {"btn", "panel", "summary"}
    ):
        return True
    return f".{class_name}" in dark_text


def _check_light_backgrounds_have_dark_guards(path: Path, text: str, failures: list[str]) -> None:
    relative = str(path.relative_to(ROOT))
    if relative not in SURFACE_TEMPLATES:
        return
    dark_text = _dark_selector_text(text)
    for match in CSS_BLOCK_RE.finditer(text):
        selectors = re.sub(r"\s+", " ", match.group("selectors")).strip()
        if 'html[data-pq-theme="dark"]' in selectors:
            continue
        body = match.group("body")
        if LIGHT_BACKGROUND_RE.search(body) is None:
            continue
        classes = [
            class_name
            for class_name in CLASS_RE.findall(selectors)
            if class_name not in MODIFIER_ONLY_CLASSES
        ]
        if not classes:
            continue
        if any(_class_has_dark_guard(class_name, dark_text, template_name=Path(relative).name) for class_name in classes):
            continue
        failures.append(
            f"{path}:{_line_number(text, match.start())} light background selector lacks a dark-mode guard: {selectors[:140]}"
        )


def main() -> int:
    failures: list[str] = []
    for relative_path in SURFACE_TEMPLATES:
        path = ROOT / relative_path
        if not path.exists():
            failures.append(f"{relative_path} is missing")
            continue
        text = path.read_text(encoding="utf-8")
        _check_customer_noise(path, text, failures)
        _check_buttons(path, text, failures)
        _check_links(path, text, failures)
        _check_images(path, text, failures)
        _check_dialogs(path, text, failures)
        _check_accessibility_primitives(path, text, failures)
        _check_contrast_tokens(path, text, failures)
        _check_light_backgrounds_have_dark_guards(path, text, failures)

    release_gate = (ROOT / "scripts" / "property_release_gates.sh").read_text(encoding="utf-8")
    if "scripts/check_property_surface_accessibility.py" not in release_gate:
        failures.append("property_release_gates.sh must run check_property_surface_accessibility.py")

    if failures:
        print("property surface accessibility check failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print("ok: property surface accessibility")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
