from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "ea" / "app" / "templates"
PUBLIC_TOURS_ROUTE = (
    ROOT / "ea" / "app" / "api" / "routes" / "public_tours.py"
)


def _template(path: str) -> str:
    return (TEMPLATES / path).read_text(encoding="utf-8")


def test_propertyquarry_public_and_console_shells_have_keyboard_skip_navigation() -> None:
    public_shell = _template("base_public.html")
    console_shell = _template("base_console.html")
    workbench = _template("app/property_decision_workbench.html")

    assert 'class="skip-link" href="#main-content"' in public_shell
    assert '<main id="main-content" tabindex="-1">' in public_shell

    assert 'class="skip-link" href="{% if property_app_nav %}#pq-main-content{% else %}#console-main-content{% endif %}"' in console_shell
    assert 'id="pq-main-content" tabindex="-1" data-property-spa-shell' in console_shell
    assert 'id="console-main-content" tabindex="-1"' in console_shell

    assert 'class="pqx-skip-link" href="#pqx-main-content"' in workbench
    assert 'class="pqx-main" id="pqx-main-content" tabindex="-1"' in workbench


def test_propertyquarry_shared_surfaces_keep_one_flat_minimal_design_layer() -> None:
    public_shell = _template("base_public.html")
    console_shell = _template("base_console.html")
    workbench = _template("app/property_decision_workbench.html")
    object_detail = _template("app/object_detail.html")
    research_detail = _template("app/property_research_detail.html")

    assert "--radius: 8px;" in public_shell
    assert "--radius: 8px;" in console_shell
    assert "PQ_FLAGSHIP_MINIMAL_CONSOLE_V1" in console_shell
    assert "PQ_FLAGSHIP_MINIMAL_WORKBENCH_V1" in workbench
    assert "PQ_FLAGSHIP_MINIMAL_OBJECT_V1" in object_detail
    assert "PQ_FLAGSHIP_MINIMAL_RESEARCH_V1" in research_detail

    minimal_layer = workbench.index("PQ_FLAGSHIP_MINIMAL_WORKBENCH_V1")
    touch_floor = workbench.index('@media (max-width: 760px), (pointer: coarse)', minimal_layer)
    css_end = workbench.index("PQ_WORKBENCH_CSS_END", touch_floor)
    assert minimal_layer < touch_floor < css_end
    assert ".pqx-shell :is(a, button, summary, input, select, textarea):focus-visible" in workbench
    assert ".pqx-shell[data-pqx-surface=\"shortlist\"] .pqx-result-diorama.is-placeholder" in workbench


def test_propertyquarry_shortlist_keeps_direct_tour_actions_and_compact_empty_media() -> None:
    public_example = _template("propertyquarry_example_shortlist.html")
    results = _template("app/_property_results_list.html")

    assert "data-pq-example-media-actions" in public_example
    assert 'aria-label="Open 3D tour for {{ row.get(\'title\') or \'example home\' }}"' in public_example
    assert 'aria-label="Open walkthrough for {{ row.get(\'title\') or \'example home\' }}"' in public_example
    assert 'aria-label="Match score {{ row.get(\'score\') }}"' in public_example

    assert '>3D tour</a>' in results
    assert '>Camera walkthrough</a>' in results
    assert 'data-rybbit-event="pq.tour.opened"' in results
    assert 'data-rybbit-event="pq.flythrough.opened"' in results
    assert 'class="pqx-result-diorama-empty-label"' in results
    assert "Preview not available" in results


def test_propertyquarry_minimal_layer_preserves_route_and_runtime_hooks() -> None:
    workbench = _template("app/property_decision_workbench.html")
    results = _template("app/_property_results_list.html")
    surface = workbench + results

    for hook in (
        "data-property-app-shell",
        "data-property-spa-shell",
        "data-property-decision-workbench",
        "data-property-workbench-json",
        "data-property-console-topnav",
        "data-workbench-results-table",
        "data-property-start-top",
    ):
        assert hook in surface


def test_public_pano2vr_shell_hides_raw_provider_metadata() -> None:
    public_tours_route = PUBLIC_TOURS_ROUTE.read_text(encoding="utf-8")

    assert '<div class="live-head live-head-single">' in public_tours_route
    assert '<div class="kv"><b>Brand</b>{brand_html}</div>' not in public_tours_route
    assert '<div class="kv"><b>Link</b>{html.escape(hostname' not in public_tours_route
