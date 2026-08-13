from __future__ import annotations

from app.api.routes.public_tours import _tour_control_panorama_html


def _render(*, locale: str = "en") -> str:
    return _tour_control_panorama_html(
        {"display_title": "Karl & Czerny"},
        panorama_spec={
            "representation_disclosure": "AI reconstruction from listing media; not a measured survey.",
            "scenes": [
                {
                    "id": "vestibule",
                    "label": "Entrance vestibule",
                    "image_url": "/tours/files/karl/vestibule.jpg",
                    "floorplan_x_pct": 30,
                    "floorplan_y_pct": 70,
                }
            ],
            "initial_scene_id": "vestibule",
        },
        provider_label="PropertyQuarry AI 360",
        viewer_name="ai-panorama",
        nonce="premium-test-nonce",
        locale=locale,
    )


def test_ai_panorama_shell_uses_premium_compact_navigation_contract() -> None:
    rendered = _render()

    assert "Karl &amp; Czerny" in rendered
    assert '<div class="identity-kicker">PropertyQuarry AI 360</div>' in rendered
    assert '<span class="trust-chip">Source-plan checked</span>' in rendered
    assert '<details class="disclosure"><summary>About this view</summary>' in rendered
    assert "AI reconstruction from listing media; not a measured survey." in rendered
    assert "scene-number" in rendered
    assert "scene-label" in rendered
    assert "String(index + 1).padStart(2, '0')" in rendered
    assert "min-height: 44px" in rendered
    assert "prefers-reduced-motion: reduce" in rendered


def test_ai_panorama_dollhouse_names_semantic_portals_without_changing_routes() -> None:
    rendered = _render()

    assert "isLoggiaDoor" in rendered
    assert "? 'Loggia door'" in rendered
    assert "connectedLabels.join(' ↔ ')" in rendered
    assert "marker.dataset.portalId = String(portal.id || '')" in rendered
    assert "marker.dataset.targetRoomId = String(portal.target_room_id || 'outside')" in rendered
    assert "button.dataset.targetSceneId = String(hotspot.target)" in rendered


def test_ai_panorama_premium_chrome_is_localized_for_vienna() -> None:
    rendered = _render(locale="de-AT")

    assert '<span class="trust-chip">Mit Quellgrundriss geprüft</span>' in rendered
    assert "<summary>Zu dieser Ansicht</summary>" in rendered
    assert "? 'Loggia-Tür'" in rendered

