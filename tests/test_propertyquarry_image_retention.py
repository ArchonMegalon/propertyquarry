from __future__ import annotations

import subprocess
from pathlib import Path

from scripts import propertyquarry_image_retention as retention


def _image(value: str) -> str:
    return "sha256:" + (value * 64)


def _row(
    repository: str,
    tag: str,
    image_id: str,
    created: str,
) -> dict[str, object]:
    return {
        "repository": repository,
        "tag": tag,
        "image_id": image_id,
        "created": created,
        "size_bytes": 100,
    }


def test_plan_is_repo_scoped_and_keeps_live_active_and_rollback() -> None:
    web, render = retention.TARGET_REPOSITORIES
    web_current = _image("a")
    web_rollback = _image("b")
    web_old = _image("c")
    web_container = _image("d")
    render_current = _image("e")
    render_rollback = _image("f")
    render_old = _image("1")
    rows = [
        _row(web, "local-aaaaaaaaaaaa", web_current, "2026-08-04T12:00:00Z"),
        _row(web, "local-bbbbbbbbbbbb", web_rollback, "2026-08-03T12:00:00Z"),
        _row(web, "local-cccccccccccc", web_old, "2026-08-02T12:00:00Z"),
        _row(web, "local-dddddddddddd", web_container, "2026-08-01T12:00:00Z"),
        _row(render, "local-eeeeeeeeeeee", render_current, "2026-08-04T12:00:00Z"),
        _row(render, "local-ffffffffffff", render_rollback, "2026-08-03T12:00:00Z"),
        _row(render, "local-111111111111", render_old, "2026-08-02T12:00:00Z"),
        _row("another-project", "local-222222222222", _image("2"), "2026-01-01T00:00:00Z"),
        _row(web, "flagship-333333333333", _image("3"), "2026-01-01T00:00:00Z"),
    ]

    plan = retention.build_plan(
        rows,
        active_image_ids={web_container},
        expected_images={web: web_current, render: render_current},
        keep_previous=1,
    )

    removable = {entry["reference"] for entry in plan["removable"]}
    assert removable == {
        f"{web}:local-cccccccccccc",
        f"{render}:local-111111111111",
    }
    reasons = {
        entry["reference"]: entry["reasons"] for entry in plan["protected"]
    }
    assert reasons[f"{web}:local-aaaaaaaaaaaa"] == ["expected_live"]
    assert reasons[f"{web}:local-bbbbbbbbbbbb"] == ["rollback"]
    assert reasons[f"{web}:local-dddddddddddd"] == ["container_reference"]
    assert reasons[f"{render}:local-eeeeeeeeeeee"] == ["expected_live"]
    assert reasons[f"{render}:local-ffffffffffff"] == ["rollback"]


def test_apply_rechecks_tag_identity_and_container_references(monkeypatch) -> None:
    web = retention.TARGET_REPOSITORIES[0]
    removable = [
        {"reference": f"{web}:local-aaaaaaaaaaaa", "image_id": _image("a")},
        {"reference": f"{web}:local-bbbbbbbbbbbb", "image_id": _image("b")},
        {"reference": f"{web}:local-cccccccccccc", "image_id": _image("c")},
    ]
    monkeypatch.setattr(retention, "_active_image_ids", lambda: {_image("b")})
    monkeypatch.setattr(
        retention,
        "_image_id_for_reference",
        lambda reference: {
            removable[0]["reference"]: _image("a"),
            removable[1]["reference"]: _image("b"),
            removable[2]["reference"]: _image("d"),
        }[reference],
    )
    calls: list[tuple[str, ...]] = []

    def run(command, *, check=True):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(retention, "_run", run)

    result = retention.apply_plan({"removable": removable, "status": "pass"})

    assert result["status"] == "pass"
    assert result["application"]["removed"] == [
        {"reference": removable[0]["reference"], "image_id": _image("a")}
    ]
    assert result["application"]["skipped"] == [
        {"reference": removable[1]["reference"], "reason": "container_reference"},
        {"reference": removable[2]["reference"], "reason": "tag_changed"},
    ]
    assert calls == [
        ("/usr/bin/docker", "image", "rm", removable[0]["reference"])
    ]


def test_deploy_runs_retention_after_verified_receipt() -> None:
    deploy = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "deploy_propertyquarry.sh"
    ).read_text()

    receipt_position = deploy.index("propertyquarry_local_deployment_receipt.py")
    retention_position = deploy.index("propertyquarry_image_retention.py")
    deployed_position = deploy.index("DEPLOYED local Docker deployment")

    assert receipt_position < retention_position < deployed_position
