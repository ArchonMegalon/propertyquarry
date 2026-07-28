#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

QUARANTINED_ARTIFACTS = (
    ".codex-design/",
    ".codex-studio/",
    "feedback/",
    "skills/",
    "docs/black_ledger_newsroom/",
    "docs/chummer5a_parity_lab/",
    "docs/chummer_explain_narration_packs/",
    "docs/chummer_governor_packets/",
    "docs/chummer_launch_followthrough/",
    "docs/chummer_operator_safe_packets/",
    "docs/chummer_organizer_packets/",
    "scripts/bootstrap_chummer6_guide_skill.py",
)

DOCKER_CONTEXT_EXCLUSIONS = (
    ".codex-design/",
    ".codex-studio/",
    "_completion/",
    "feedback/",
    "skills/",
    "docs/black_ledger_newsroom/",
    "docs/chummer5a_parity_lab/",
    "docs/chummer_explain_narration_packs/",
    "docs/chummer_governor_packets/",
    "docs/chummer_launch_followthrough/",
    "docs/chummer_operator_safe_packets/",
    "docs/chummer_organizer_packets/",
    "scripts/bootstrap_chummer6_guide_skill.py",
)

RUNTIME_RELEASE_FILES = (
    "docker-compose.property.yml",
    "docker-compose.cloudflared.yml",
    "docker-compose.property-legacy-edge.yml",
    "ea/Dockerfile.property",
    "ea/Dockerfile.property-web",
    "scripts/deploy_propertyquarry.sh",
    "scripts/property_release_gates.sh",
)

PROPERTYQUARRY_AUTHORITY_FILES = (
    ".github/NO_GITHUB_ACTIONS.md",
    "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md",
    "ea/app/api/routes/health.py",
    "ea/app/product/property_tour_hosting.py",
    "ea/app/services/public_branding.py",
    "ea/app/services/public_clickrank.py",
    "scripts/propertyquarry_local_deployment_receipt.py",
    "scripts/verify_generated_release_artifacts_clean.py",
)

FORBIDDEN_AUTHORITY_TOKENS = (
    "ArchonMegalon/property.git",
    "ArchonMegalon/property'",
    'ArchonMegalon/property"',
    "myexternalbrain.com",
)

QUARANTINED_HOST_OPS_SCRIPTS = (
    "scripts/harden_propertyquarry_docker.sh",
    "scripts/recover_host_after_reboot.sh",
)

FORBIDDEN_RUNTIME_TOKENS = (
    ".codex-design",
    ".codex-studio",
    "/feedback",
    " feedback/",
    "skills/",
    "bootstrap_chummer6_guide_skill",
    "/docker/chummercomplete",
    "chummer-playwright",
    "ea-openvoice",
    "openvoice",
    "ea-responses-proxy",
    "ea-teable-relay",
    "/mnt/onedrive",
    "/mnt/pcloud",
)


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _script_path(path: str) -> Path:
    return ROOT / path


def _makefile_deploy_uses_governed_handoff(makefile: str) -> bool:
    match = re.search(r"^deploy:\n(?P<body>(?:\t.*\n)+)", makefile, flags=re.MULTILINE)
    if not match:
        return False
    return match.group("body").strip() == "./scripts/deploy_propertyquarry.sh"


def main() -> int:
    failures: list[str] = []
    doc_path = ROOT / "docs" / "REPO_ISOLATION.md"
    if not doc_path.exists():
        failures.append("docs/REPO_ISOLATION.md must document inherited archive quarantine")
    else:
        doc_text = doc_path.read_text(encoding="utf-8")
        for artifact in QUARANTINED_ARTIFACTS:
            if artifact not in doc_text:
                failures.append(f"docs/REPO_ISOLATION.md must list quarantined artifact {artifact}")

    dockerignore_path = ROOT / ".dockerignore"
    if not dockerignore_path.exists():
        failures.append(".dockerignore must exist to keep inherited/generated artifacts out of Docker builds")
    else:
        ignored = {
            line.strip()
            for line in dockerignore_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        for artifact in DOCKER_CONTEXT_EXCLUSIONS:
            if artifact not in ignored:
                failures.append(f".dockerignore must exclude quarantined/generated Docker context artifact {artifact}")

    for path in RUNTIME_RELEASE_FILES:
        text = _read(path)
        lowered = text.lower()
        for token in FORBIDDEN_RUNTIME_TOKENS:
            if token.lower() in lowered:
                failures.append(f"{path} references inherited/non-property runtime token {token!r}")

    for path in PROPERTYQUARRY_AUTHORITY_FILES:
        text = _read(path)
        lowered = text.lower()
        for token in FORBIDDEN_AUTHORITY_TOKENS:
            if token.lower() in lowered:
                failures.append(
                    f"{path} grants PropertyQuarry authority to external token {token!r}"
                )

    for path in (
        ".github/NO_GITHUB_ACTIONS.md",
        "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md",
        "scripts/verify_generated_release_artifacts_clean.py",
    ):
        if "ArchonMegalon/propertyquarry" not in _read(path):
            failures.append(
                f"{path} must bind release authority to ArchonMegalon/propertyquarry"
            )

    for path in QUARANTINED_HOST_OPS_SCRIPTS:
        if not _script_path(path).exists():
            failures.append(f"{path} is listed as a quarantined host-ops script but does not exist")
            continue
        text = _read(path)
        lowered = text.lower()
        if "propertyquarry_host_recovery_allow" not in lowered:
            failures.append(f"{path} must require PROPERTYQUARRY_HOST_RECOVERY_ALLOW=1 before host-level actions")
        if "--dry-run" not in text and "PROPERTYQUARRY_HOST_RECOVERY_DRY_RUN" not in text:
            failures.append(f"{path} must support a dry-run mode for host recovery actions")

    makefile = _read("Makefile")
    if not _makefile_deploy_uses_governed_handoff(makefile):
        failures.append(
            "Makefile deploy target must delegate only to scripts/deploy_propertyquarry.sh"
        )
    if "PROPERTYQUARRY_USE_LEGACY_STACK=1 bash scripts/deploy.sh" not in makefile:
        failures.append("Makefile must keep legacy EA deploy behind PROPERTYQUARRY_USE_LEGACY_STACK=1")

    architecture = _read("docs/ARCHITECTURE.md")
    if "REPO_ISOLATION.md" not in architecture:
        failures.append("docs/ARCHITECTURE.md must point to docs/REPO_ISOLATION.md")

    if failures:
        print("property repo isolation check failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("ok: property repo isolation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
