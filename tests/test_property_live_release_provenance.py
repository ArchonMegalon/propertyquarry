from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from scripts.propertyquarry_live_release_provenance import (
    build_live_release_provenance_receipt,
)
from scripts.verify_generated_release_artifacts_clean import (
    RELEASE_MANIFEST_FIELDS,
    RELEASE_MANIFEST_JSON_END,
    RELEASE_MANIFEST_JSON_START,
    release_manifest_sha256,
)


ROOT = Path(__file__).resolve().parents[1]
COMMIT_SHA = "a" * 40
WORKFLOW_HEAD_SHA = "d" * 40
WEB_DIGEST = f"sha256:{'b' * 64}"
RENDER_DIGEST = f"sha256:{'c' * 64}"
WEB_IMAGE = f"registry.example/propertyquarry-web@{WEB_DIGEST}"
RENDER_IMAGE = f"registry.example/propertyquarry-render@{RENDER_DIGEST}"
GENERATED_AT = "2026-07-16T14:30:00Z"
REPLICA_ID = "propertyquarry-api-1"


@contextmanager
def _version_server(payload: dict[str, object], *, redirect_to: str = "") -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if redirect_to:
                self.send_response(302)
                self.send_header("Location", redirect_to)
                self.end_headers()
                return
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _manifest_values(origin: str) -> dict[str, str]:
    values = {
        "release_manifest_schema": "propertyquarry.release_manifest.v1",
        "release_product": "PropertyQuarry",
        "release_candidate_status": (
            "source-browser-candidate-pending-protected-live-evidence"
        ),
        "release_repository": "ArchonMegalon/propertyquarry",
        "release_repository_origin": "https://github.com/ArchonMegalon/propertyquarry.git",
        "release_branch": "main",
        "release_commit_sha": COMMIT_SHA,
        "release_public_origin": origin,
        "release_artifact_set": (
            "propertyquarry-generated-release-artifacts-v1@sha256:" + "f" * 64
        ),
        "release_label": "propertyquarry-source-browser-candidate-aaaaaaaaaaaa",
        "release_generated_at": GENERATED_AT,
        "release_verification_commands": (
            "bash scripts/verify_release_assets.sh && "
            "python3 scripts/verify_flagship_release_readiness.py && "
            "python3 scripts/verify_generated_release_artifacts_clean.py"
        ),
        "release_deployment_id": "propertyquarry-governed-deploy-aaaaaaaaaaaa",
    }
    assert tuple(values) == RELEASE_MANIFEST_FIELDS
    return values


def _manifest_document(body: str) -> str:
    return (
        "# Test release manifest\n\n"
        f"{RELEASE_MANIFEST_JSON_START}\n"
        "```json\n"
        f"{body}\n"
        "```\n"
        f"{RELEASE_MANIFEST_JSON_END}\n"
    )


def _write_release_manifest(tmp_path: Path, origin: str) -> tuple[Path, dict[str, str]]:
    values = _manifest_values(origin)
    path = tmp_path / "release-manifest.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _manifest_document(json.dumps(values, indent=2, sort_keys=True)),
        encoding="utf-8",
    )
    return path, values


def _identity(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    return {
        "path": str(path),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _write_security_bundle(tmp_path: Path) -> tuple[Path, Path]:
    bundle = tmp_path / "security"
    artifacts = bundle / "artifacts"
    artifacts.mkdir(parents=True)
    for name, payload in (
        ("dependencies.pip-audit.json", b"[]\n"),
        ("web.sbom.cdx.json", b'{"bomFormat":"CycloneDX","components":[{}]}\n'),
        ("web.trivy.json", b'{"Results":[]}\n'),
        ("render.sbom.cdx.json", b'{"bomFormat":"CycloneDX","components":[{}]}\n'),
        ("render.trivy.json", b'{"Results":[]}\n'),
    ):
        (artifacts / name).write_bytes(payload)

    version_output = {"bytes": 8, "sha256": hashlib.sha256(b"tool 1.0").hexdigest()}
    receipt = {
        "schema": "propertyquarry.release_security_receipt.v1",
        "generated_at": GENERATED_AT,
        "completed_at": "2026-07-16T14:31:00Z",
        "mode": "flagship",
        "status": "pass",
        "gate_passed": True,
        "severity_threshold": "HIGH",
        "identities": {
            "release_commit_sha": COMMIT_SHA,
            "web_image": WEB_IMAGE,
            "render_image": RENDER_IMAGE,
            "dependency_lock": _identity(ROOT / "ea" / "requirements.lock"),
        },
        "network_contract": {
            "scanner_install_allowed": False,
            "registry_access_allowed": False,
            "image_source": "local_docker_digest_only",
            "trivy_database_updates_allowed": False,
        },
        "tools": {
            name: {
                "available": True,
                "version": "1.0.0",
                "version_output": version_output,
            }
            for name in ("pip-audit", "syft", "trivy")
        },
        "artifacts": {
            "dependencies": _identity(artifacts / "dependencies.pip-audit.json"),
            "web": {
                "image": WEB_IMAGE,
                "sbom": _identity(artifacts / "web.sbom.cdx.json"),
                "sbom_format": "CycloneDX",
                "component_count": 1,
                "vulnerability_scan": _identity(artifacts / "web.trivy.json"),
            },
            "render": {
                "image": RENDER_IMAGE,
                "sbom": _identity(artifacts / "render.sbom.cdx.json"),
                "sbom_format": "CycloneDX",
                "component_count": 1,
                "vulnerability_scan": _identity(artifacts / "render.trivy.json"),
            },
        },
        "findings": [],
        "summary": {"blocking": 0},
        "waivers": {"configured": 0, "applied": [], "unused": []},
    }
    receipt_path = bundle / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    binding_path = bundle / "workflow-binding.json"
    binding_path.write_text(
        json.dumps(
            {
                "contract_name": "propertyquarry.workflow_runtime_binding",
                "version": 1,
                "product": "PropertyQuarry",
                "runtime_commit_sha": COMMIT_SHA,
                "workflow_head_sha": WORKFLOW_HEAD_SHA,
                "run_id": "12345",
                "run_attempt": "2",
            }
        ),
        encoding="utf-8",
    )
    return receipt_path, binding_path


def _version_payload(
    manifest: dict[str, str],
    **overrides: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        **manifest,
        "release_manifest_sha256": release_manifest_sha256(manifest),
        "release_manifest_status": "complete",
        "release_image_digest": WEB_DIGEST,
        "replica_id": REPLICA_ID,
    }
    payload.update(overrides)
    return payload


def _probe(
    origin: str,
    receipt_path: Path,
    binding_path: Path,
    manifest_path: Path,
    manifest: dict[str, str],
    **overrides: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "base_url": origin,
        "expected_commit_sha": manifest["release_commit_sha"],
        "expected_repository": manifest["release_repository"],
        "expected_public_origin": manifest["release_public_origin"],
        "expected_branch": manifest["release_branch"],
        "expected_deployment_id": manifest["release_deployment_id"],
        "expected_artifact_set": manifest["release_artifact_set"],
        "expected_release_label": manifest["release_label"],
        "expected_release_generated_at": manifest["release_generated_at"],
        "expected_image_digest": WEB_DIGEST,
        "expected_replica_id": REPLICA_ID,
        "expected_web_image": WEB_IMAGE,
        "expected_render_image": RENDER_IMAGE,
        "security_receipt_path": receipt_path,
        "security_workflow_binding_path": binding_path,
        "expected_workflow_head_sha": WORKFLOW_HEAD_SHA,
        "expected_workflow_run_id": "12345",
        "expected_workflow_run_attempt": "2",
        "release_manifest_path": manifest_path,
    }
    values.update(overrides)
    return build_live_release_provenance_receipt(**values)  # type: ignore[arg-type]


def test_live_release_provenance_requires_complete_exact_manifest_and_security_bundle(
    tmp_path: Path,
) -> None:
    receipt_path, binding_path = _write_security_bundle(tmp_path)
    payload: dict[str, object] = {}
    with _version_server(payload) as origin:
        manifest_path, manifest = _write_release_manifest(tmp_path, origin)
        payload.update(_version_payload(manifest))
        receipt = _probe(
            origin,
            receipt_path,
            binding_path,
            manifest_path,
            manifest,
        )

    assert receipt["status"] == "pass"
    assert receipt["failed_count"] == 0
    assert receipt["security_receipt_binding"]["verified"] is True
    assert len(receipt["security_receipt_binding"]["receipt_sha256"]) == 64
    assert receipt["expected"]["release_manifest_sha256"] == release_manifest_sha256(
        manifest
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("release_manifest_schema", "propertyquarry.release_manifest.v2"),
        ("release_product", "OtherProduct"),
        ("release_candidate_status", "unreviewed"),
        ("release_repository", "another/repository"),
        ("release_repository_origin", "https://github.com/another/repository.git"),
        ("release_branch", "release"),
        ("release_commit_sha", "e" * 40),
        ("release_public_origin", "https://other.example"),
        (
            "release_artifact_set",
            "propertyquarry-generated-release-artifacts-v1@sha256:" + "e" * 64,
        ),
        ("release_label", "another-label"),
        ("release_generated_at", "2026-07-16T14:32:00Z"),
        ("release_verification_commands", "python3 unreviewed.py"),
        ("release_deployment_id", "another-deploy"),
        ("release_manifest_sha256", "e" * 64),
        ("release_image_digest", f"sha256:{'e' * 64}"),
        ("replica_id", "propertyquarry-api-2"),
        ("release_manifest_status", "incomplete"),
    ),
)
def test_live_release_provenance_rejects_every_manifest_or_runtime_mismatch(
    tmp_path: Path,
    field: str,
    replacement: str,
) -> None:
    receipt_path, binding_path = _write_security_bundle(tmp_path)
    payload: dict[str, object] = {}
    with _version_server(payload) as origin:
        manifest_path, manifest = _write_release_manifest(tmp_path, origin)
        payload.update(_version_payload(manifest, **{field: replacement}))
        receipt = _probe(
            origin,
            receipt_path,
            binding_path,
            manifest_path,
            manifest,
        )

    expected_check = (
        "release_manifest_complete"
        if field == "release_manifest_status"
        else f"{field}_matches"
    )
    assert receipt["status"] == "fail"
    assert any(
        check["name"] == expected_check and not check["ok"]
        for check in receipt["checks"]
    )


@pytest.mark.parametrize("malformation", ("missing", "duplicate", "unexpected"))
def test_live_release_provenance_rejects_malformed_local_manifest_before_network(
    tmp_path: Path,
    malformation: str,
) -> None:
    origin = "http://127.0.0.1:9"
    receipt_path, binding_path = _write_security_bundle(tmp_path)
    manifest = _manifest_values(origin)
    if malformation == "missing":
        malformed = dict(manifest)
        malformed.pop("release_product")
        body = json.dumps(malformed, indent=2, sort_keys=True)
    elif malformation == "unexpected":
        malformed = {**manifest, "unreviewed": "value"}
        body = json.dumps(malformed, indent=2, sort_keys=True)
    else:
        body = json.dumps(manifest, sort_keys=True)[:-1]
        body += ',"release_product":"Duplicate"}'
    manifest_path = tmp_path / "malformed-release-manifest.md"
    manifest_path.write_text(_manifest_document(body), encoding="utf-8")

    receipt = _probe(
        origin,
        receipt_path,
        binding_path,
        manifest_path,
        manifest,
    )

    assert receipt["status"] == "blocked"
    assert receipt["checks"][0]["name"] == "expected_release_identity_complete"
    assert receipt["checks"][0]["ok"] is False
    expected_fragment = {
        "duplicate": "duplicated",
        "missing": "missing",
        "unexpected": "unexpected",
    }[malformation]
    assert expected_fragment in receipt["checks"][0]["reason"]


def test_live_release_provenance_rejects_supplied_identity_that_differs_from_manifest(
    tmp_path: Path,
) -> None:
    origin = "http://127.0.0.1:9"
    receipt_path, binding_path = _write_security_bundle(tmp_path)
    manifest_path, manifest = _write_release_manifest(tmp_path, origin)

    receipt = _probe(
        origin,
        receipt_path,
        binding_path,
        manifest_path,
        manifest,
        expected_repository="another/repository",
    )

    assert receipt["status"] == "blocked"
    assert receipt["checks"][0]["reason"] == (
        "expected_release_manifest_mismatch:release_repository"
    )


def test_live_release_provenance_rejects_incomplete_expected_identity_before_network(
    tmp_path: Path,
) -> None:
    origin = "http://127.0.0.1:9"
    receipt_path, binding_path = _write_security_bundle(tmp_path)
    manifest_path, manifest = _write_release_manifest(tmp_path, origin)

    receipt = _probe(
        origin,
        receipt_path,
        binding_path,
        manifest_path,
        manifest,
        expected_deployment_id="",
    )

    assert receipt["status"] == "blocked"
    assert receipt["checks"][0]["name"] == "expected_release_identity_complete"


def test_live_release_provenance_rejects_unverified_or_tampered_security_evidence(
    tmp_path: Path,
) -> None:
    origin = "http://127.0.0.1:9"
    receipt_path, binding_path = _write_security_bundle(tmp_path)
    manifest_path, manifest = _write_release_manifest(tmp_path, origin)
    security_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    security_receipt["gate_passed"] = False
    receipt_path.write_text(json.dumps(security_receipt), encoding="utf-8")
    blocked_gate = _probe(
        origin,
        receipt_path,
        binding_path,
        manifest_path,
        manifest,
    )

    tampered_root = tmp_path / "tampered"
    receipt_path, binding_path = _write_security_bundle(tampered_root)
    manifest_path, manifest = _write_release_manifest(tampered_root, origin)
    (receipt_path.parent / "artifacts" / "web.sbom.cdx.json").write_text(
        '{"tampered":true}\n',
        encoding="utf-8",
    )
    blocked_artifact = _probe(
        origin,
        receipt_path,
        binding_path,
        manifest_path,
        manifest,
    )

    assert blocked_gate["status"] == "blocked"
    assert blocked_artifact["status"] == "blocked"
    assert any(
        check["name"] == "security_receipt_binding_verified" and not check["ok"]
        for check in blocked_artifact["checks"]
    )


def test_live_release_provenance_rejects_security_evidence_from_another_workflow_run(
    tmp_path: Path,
) -> None:
    origin = "http://127.0.0.1:9"
    receipt_path, binding_path = _write_security_bundle(tmp_path)
    manifest_path, manifest = _write_release_manifest(tmp_path, origin)
    receipt = _probe(
        origin,
        receipt_path,
        binding_path,
        manifest_path,
        manifest,
        expected_workflow_run_id="99999",
    )

    assert receipt["status"] == "blocked"
    assert receipt["checks"][-1]["reason"] == "security_workflow_binding_mismatch"


def test_live_release_provenance_does_not_follow_redirects(tmp_path: Path) -> None:
    receipt_path, binding_path = _write_security_bundle(tmp_path)
    with _version_server({}) as destination:
        with _version_server({}, redirect_to=f"{destination}/version") as source:
            manifest_path, manifest = _write_release_manifest(tmp_path, source)
            receipt = _probe(
                source,
                receipt_path,
                binding_path,
                manifest_path,
                manifest,
            )

    assert receipt["status"] == "fail"
    assert any(
        check["name"] == "version_status_ok" and not check["ok"]
        for check in receipt["checks"]
    )
