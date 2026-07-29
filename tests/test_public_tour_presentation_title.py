from app.api.routes.public_tours import (
    _generated_reconstruction_public_launch_html,
    _public_tour_presentation_title,
)


def test_presentation_title_removes_generated_slug_tail() -> None:
    title = (
        "Runtime reconstruction smoke "
        "runtime-reconstruction-smoke-runtime-service-reconstruction-smoke-"
        "walkthrough-e2e-v7-2-layout-first-628e8f7bd940737b7c34"
    )

    assert _public_tour_presentation_title(title) == "Runtime reconstruction smoke"


def test_presentation_title_preserves_human_listing_title() -> None:
    title = "Luxury Residence with breathtaking Skyline Views: DANUBEFLATS Vienna"

    assert _public_tour_presentation_title(title) == title


def test_presentation_title_truncates_long_prose_on_a_word_boundary() -> None:
    title = (
        "Bright family apartment with a quiet terrace, generous storage, "
        "thoughtful circulation, and excellent access to parks and transit"
    )

    rendered = _public_tour_presentation_title(title, limit=72)

    assert rendered.endswith("…")
    assert len(rendered) <= 73
    assert not rendered.endswith(" …")


def test_generated_reconstruction_uses_balanced_walkthrough_columns() -> None:
    rendered = _generated_reconstruction_public_launch_html({})

    assert 'class="stage-column stage-primary"' in rendered
    assert 'class="sidebar stage-column"' in rendered
    assert (
        'class="card sidebar-block sidebar-route"'
        in rendered
    )
    assert (
        'class="card reference-focus sidebar-block sidebar-reference"'
        in rendered
    )
    assert 'grid-template-columns:repeat(2,minmax(0,1fr))' in rendered
    assert 'grid-template-columns:repeat(3,minmax(0,1fr))' in rendered
    assert 'aspect-ratio:16 / 9' in rendered
    assert '.reference-shell, .reference-shell-doc { min-height:0; }' in rendered
