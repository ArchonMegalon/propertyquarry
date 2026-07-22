from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github/workflows/propertyquarry-publish-runtime-images.yml"


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _job(workflow: str, name: str) -> str:
    marker = f"  {name}:\n"
    start = workflow.index(marker)
    body_start = start + len(marker)
    next_job = re.search(r"^  [a-zA-Z0-9_-]+:\n", workflow[body_start:], flags=re.MULTILINE)
    end = body_start + next_job.start() if next_job else len(workflow)
    return workflow[start:end]


def test_image_publish_workflow_loads_as_yaml_with_the_expected_jobs() -> None:
    parsed = yaml.safe_load(_workflow())

    assert isinstance(parsed, dict)
    assert set(parsed["jobs"]) == {"preflight", "build-and-publish", "receipt"}
    assert parsed["jobs"]["build-and-publish"]["strategy"]["matrix"]["include"] == [
        {
            "component": "web",
            "service": "propertyquarry-api",
            "dockerfile": "ea/Dockerfile.property-web",
            "image": "ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime",
        },
        {
            "component": "render",
            "service": "propertyquarry-render-tools",
            "dockerfile": "ea/Dockerfile.property",
            "image": "ghcr.io/archonmegalon/propertyquarry-standalone-render-runtime",
        },
    ]


def test_image_publish_workflow_is_dispatch_only_protected_and_immutably_pinned() -> None:
    workflow = _workflow()
    trigger = workflow.split("jobs:", 1)[0]

    assert "workflow_dispatch:" in trigger
    assert "push:" not in trigger
    assert "pull_request:" not in trigger
    assert "permissions: {}" in trigger
    assert "cancel-in-progress: false" in trigger
    assert workflow.count("environment:\n      name: propertyquarry-production") == 3

    action_lines = [
        line.strip()
        for line in workflow.splitlines()
        if re.match(r"^\s*(?:-\s+)?uses:\s+", line)
    ]
    assert action_lines
    for line in action_lines:
        declaration, _, comment = line.partition("#")
        ref = declaration.split("uses:", 1)[1].strip().strip("'\"")
        assert re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+@[0-9a-f]{40}", ref)
        assert re.fullmatch(r"v[1-9][0-9]*", comment.strip())

    for current_pin in (
        "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7",
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7",
        "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8",
        "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c # v4",
        "docker/login-action@af1e73f918a031802d376d3c8bbc3fe56130a9b0 # v4",
        "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a # v7",
        "actions/attest@a1948c3f048ba23858d222213b7c278aabede763 # v4",
    ):
        assert current_pin in workflow

    preflight = _job(workflow, "preflight")
    build = _job(workflow, "build-and-publish")
    receipt = _job(workflow, "receipt")
    assert "id-token: write" not in preflight
    assert "attestations: write" not in preflight
    assert "id-token: write" not in receipt
    assert "attestations: write" not in receipt
    assert build.count("id-token: write") == 1
    assert build.count("attestations: write") == 1
    assert build.count("packages: write") == 1
    assert "artifact-metadata:" not in workflow
    assert "attestations:" not in workflow.split("jobs:", 1)[0]


def test_image_publish_preflight_binds_clean_main_envelope_to_manifest_runtime() -> None:
    workflow = _workflow()
    preflight = _job(workflow, "preflight")

    for required in (
        '"${GITHUB_EVENT_NAME}" != "workflow_dispatch"',
        '"${GITHUB_REF}" != "refs/heads/main"',
        '"${GITHUB_REPOSITORY}" != "${EXPECTED_REPOSITORY}"',
        '"${REPOSITORY_VISIBILITY}" != "public"',
        '"${envelope_sha}" != "${GITHUB_SHA}"',
        "git status --porcelain --untracked-files=all",
        "scripts/check_property_release_hygiene.py --write",
        "release_manifest_runtime_sha",
        'git merge-base --is-ancestor "${runtime_sha}" "${envelope_sha}"',
        '"release_branch": "main"',
        '"release_product": "PropertyQuarry"',
        '"release_repository": os.environ["EXPECTED_REPOSITORY"]',
        '"deployment_performed": False',
        '"environment_variables_mutated": False',
    ):
        assert required in preflight

    assert "EXPECTED_WEB_IMAGE: ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime" in preflight
    assert "EXPECTED_RENDER_IMAGE: ghcr.io/archonmegalon/propertyquarry-standalone-render-runtime" in preflight
    assert "fetch-depth: 0" in preflight
    assert "persist-credentials: false" in preflight


def test_image_publish_builds_both_services_from_the_exact_envelope_with_supply_chain_proof() -> None:
    workflow = _workflow()
    build = _job(workflow, "build-and-publish")

    for required in (
        "service: propertyquarry-api",
        "dockerfile: ea/Dockerfile.property-web",
        "image: ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime",
        "service: propertyquarry-render-tools",
        "dockerfile: ea/Dockerfile.property",
        "image: ghcr.io/archonmegalon/propertyquarry-standalone-render-runtime",
        "ref: ${{ needs.preflight.outputs.envelope_sha }}",
        "context: source",
        "file: source/${{ matrix.dockerfile }}",
        "platforms: linux/amd64",
        "push: true",
        "provenance: mode=max",
        "sbom: true",
        "uses: actions/attest@a1948c3f048ba23858d222213b7c278aabede763 # v4",
        "subject-name: ${{ matrix.image }}",
        "subject-digest: ${{ steps.build.outputs.digest }}",
        "push-to-registry: true",
        "create-storage-record: false",
        "SIGSTORE_ATTESTATION_ID: ${{ steps.sigstore.outputs.attestation-id }}",
        "SIGSTORE_ATTESTATION_URL: ${{ steps.sigstore.outputs.attestation-url }}",
        "SIGSTORE_BUNDLE_PATH: ${{ steps.sigstore.outputs.bundle-path }}",
        "GitHub Sigstore bundle subject does not match the exact image digest",
        "GitHub Sigstore bundle has no signing certificate",
        "GitHub Sigstore bundle has no transparency-log entry",
        '"bundle_sha256": os.environ["SIGSTORE_BUNDLE_SHA256"]',
        '"bundle_artifact_path": f\'sigstore/{os.environ["COMPONENT"]}-bundle.json\'',
        '"sigstore_instance": "public-good"',
        "org.opencontainers.image.revision=${{ needs.preflight.outputs.envelope_sha }}",
        "org.opencontainers.image.version=${{ needs.preflight.outputs.runtime_sha }}",
        "com.propertyquarry.release.runtime-sha=${{ needs.preflight.outputs.runtime_sha }}",
        "com.propertyquarry.release.workflow-envelope-sha=${{ needs.preflight.outputs.envelope_sha }}",
        "envelope_tracked_archive_sha256",
        "dockerfile_sha256",
        "dockerignore_sha256",
        "compose_sha256",
        "docker compose -f source/docker-compose.property.yml config --format json --no-interpolate",
        "Compose build context mismatch",
        "Compose Dockerfile mismatch",
        '"compose_build_mapping_verified": True',
        'docker buildx imagetools inspect --raw "${IMAGE_REPOSITORY}@${IMAGE_DIGEST}"',
        'docker buildx imagetools inspect --raw "${PUBLISHED_TAG}"',
        '"${remote_digest}" != "${IMAGE_DIGEST}"',
        '"${remote_tag_digest}" != "${IMAGE_DIGEST}"',
        "https://slsa.dev/provenance/",
        "https://spdx.dev/Document",
        "remote image label mismatch",
    ):
        assert required in build

    assert "packages: write" in build
    assert "runtime-${RUNTIME_SHA}-envelope-${ENVELOPE_SHA}-run-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" in build
    assert ":latest" not in build
    assert "ref: ${{ needs.preflight.outputs.runtime_sha }}" not in build


def test_image_publish_receipt_rejects_registry_drift_missing_or_equal_digests() -> None:
    workflow = _workflow()
    receipt = _job(workflow, "receipt")

    for required in (
        "expected exactly two image receipt fragments",
        'value.get("registry") != "ghcr.io"',
        "unexpected registry repository",
        r're.fullmatch(r"sha256:[0-9a-f]{64}", digest)',
        'fragments["web"]["digest"] == fragments["render"]["digest"]',
        "web and render image digests must be distinct",
        '"schema": "propertyquarry.image_publish_receipt.v1"',
        '"runtime_commit_sha": os.environ["RUNTIME_SHA"]',
        '"workflow_envelope_sha": os.environ["ENVELOPE_SHA"]',
        '"input_hashes": value["input_hashes"]',
        '"compose_build_mapping_verified": value["compose_build_mapping_verified"]',
        '"sigstore_provenance": value["sigstore_provenance"]',
        "GitHub Sigstore bundle hash mismatch",
        '"immutable_ref": value["immutable_ref"]',
        '"promotion_authority_granted": False',
        '"deployment_performed": False',
        '"environment_variables_mutated": False',
        "if-no-files-found: error",
    ):
        assert required in receipt

    for forbidden in (
        "docker compose up",
        "docker compose down",
        "docker compose build",
        "docker compose push",
        "deploy_propertyquarry.sh",
        "gh variable set",
        "gh secret set",
        "GITHUB_ENV",
        "propertyquarry-release-controller",
        "gh attestation verify",
    ):
        assert forbidden not in workflow
