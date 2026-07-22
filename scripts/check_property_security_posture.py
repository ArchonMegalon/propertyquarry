#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _normalized_requirement_name(line: str) -> str:
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return ""
    name = re.split(r"[<>=!~\[]", raw, maxsplit=1)[0].strip().lower()
    return name


def _lock_package_names(lock_text: str) -> set[str]:
    names: set[str] = set()
    for line in lock_text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        if "==" not in raw:
            continue
        names.add(raw.split("==", 1)[0].strip().lower().replace("_", "-"))
    return names


def _logical_instructions(text: str) -> list[str]:
    instructions: list[str] = []
    current: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line and not current:
            continue
        if line.startswith("#") and not current:
            continue
        continued = line.endswith("\\")
        current.append(line[:-1].rstrip() if continued else line)
        if not continued:
            instructions.append(" ".join(part for part in current if part))
            current = []
    if current:
        instructions.append(" ".join(part for part in current if part))
    return instructions


def _dockerfile_base_images(dockerfile: str) -> list[str]:
    images: list[str] = []
    for instruction in _logical_instructions(dockerfile):
        tokens = instruction.split()
        if not tokens or tokens[0].upper() != "FROM":
            continue
        image_index = 1
        while image_index < len(tokens) and tokens[image_index].startswith("--"):
            image_index += 1
        if image_index < len(tokens):
            images.append(tokens[image_index])
    return images


def _unpinned_dockerfile_base_images(dockerfile: str) -> list[str]:
    return [
        image
        for image in _dockerfile_base_images(dockerfile)
        if image.lower() != "scratch"
        and re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", image) is None
    ]


def _dockerfile_final_user(dockerfile: str) -> str:
    instructions = _logical_instructions(dockerfile)
    stage_starts = [
        index
        for index, instruction in enumerate(instructions)
        if instruction.split(maxsplit=1)[0].upper() == "FROM"
    ]
    if not stage_starts:
        return ""
    users = [
        instruction.split(maxsplit=1)[1].strip()
        for instruction in instructions[stage_starts[-1] + 1 :]
        if instruction.split(maxsplit=1)[0].upper() == "USER"
        and len(instruction.split(maxsplit=1)) == 2
    ]
    return users[-1] if users else ""


def _hashed_requirement_contract_failures(requirements_text: str) -> list[str]:
    invalid: list[str] = []
    rows = _logical_instructions(requirements_text)
    for row in rows:
        tokens = row.split()
        requirement = tokens[0] if tokens else ""
        hashes = tokens[1:]
        pinned_requirement = re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9_,.-]+\])?==[^\s;]+",
            requirement,
        )
        valid_hashes = bool(hashes) and all(
            re.fullmatch(r"--hash=sha256:[0-9a-f]{64}", value) is not None
            for value in hashes
        )
        if pinned_requirement is None or not valid_hashes:
            invalid.append(requirement or "<empty>")
    return invalid or (["<missing>"] if not rows else [])


def _append_dockerfile_runtime_failures(
    failures: list[str],
    *,
    path: str,
    dockerfile: str,
) -> None:
    base_images = _dockerfile_base_images(dockerfile)
    unpinned_images = _unpinned_dockerfile_base_images(dockerfile)
    if not base_images:
        failures.append(f"{path} must contain at least one FROM instruction")
    elif unpinned_images:
        failures.append(
            f"{path} must pin every non-scratch FROM image by digest: "
            + ", ".join(unpinned_images)
        )
    if _dockerfile_final_user(dockerfile) != "10001:10001":
        failures.append(f"{path} must run its final stage as USER 10001:10001")


def _web_wheelhouse_install_contract_present(dockerfile: str) -> bool:
    instructions = _logical_instructions(dockerfile)
    required_copy_instructions = {
        "COPY ea/requirements.lock /app/requirements.lock",
        "COPY ea/requirements.wheelhouse.lock /app/requirements.wheelhouse.lock",
        "COPY vendor/propertyquarry-python-wheels /opt/propertyquarry-python-wheels",
        (
            "COPY --chmod=0555 scripts/verify_propertyquarry_python_wheelhouse.py "
            "/usr/local/libexec/verify_propertyquarry_python_wheelhouse.py"
        ),
    }
    if not required_copy_instructions.issubset(set(instructions)):
        return False
    expected_dependency_run = (
        "RUN python /usr/local/libexec/verify_propertyquarry_python_wheelhouse.py "
        "--requirements-lock /app/requirements.lock "
        "--hash-lock /app/requirements.wheelhouse.lock "
        "--wheelhouse /opt/propertyquarry-python-wheels && "
        "python -m pip install --no-cache-dir --no-index "
        "--find-links=/opt/propertyquarry-python-wheels --require-hashes "
        "--requirement /app/requirements.wheelhouse.lock && "
        "rm -rf /opt/propertyquarry-python-wheels && "
        "rm -f /usr/local/libexec/verify_propertyquarry_python_wheelhouse.py"
    )
    pip_install_command = re.compile(
        r"(?:^|&&|\|\||;)\s*"
        r"(?:(?:\S*/)?python(?:3(?:\.\d+)?)?\s+-m\s+pip|"
        r"(?:\S*/)?pip(?:3(?:\.\d+)?)?)\s+install\b"
    )
    pip_install_instructions = [
        instruction
        for instruction in instructions
        if instruction.startswith("RUN ")
        and pip_install_command.search(instruction.removeprefix("RUN "))
    ]
    return (
        expected_dependency_run in instructions
        and pip_install_instructions == [expected_dependency_run]
    )


def build_security_posture_receipt() -> dict[str, object]:
    failures: list[str] = []
    env_example = _read(".env.example")
    if "property@propertyquery.com" in env_example:
        failures.append(".env.example still references property@propertyquery.com")
    if re.search(r"^EA_REGISTRATION_EMAIL_FROM_FALLBACK=.+", env_example, flags=re.MULTILINE):
        failures.append(".env.example should not advertise a non-PropertyQuarry fallback sender")
    for env_name in ("EA_API_TOKEN", "EA_SIGNING_SECRET", "EA_CF_ACCESS_TEAM_DOMAIN", "EA_CF_ACCESS_AUD"):
        if not re.search(rf"^{re.escape(env_name)}=", env_example, flags=re.MULTILINE):
            failures.append(f".env.example must list prod auth/signing placeholder {env_name}")
    for env_name in (
        "PROPERTYQUARRY_API_DATABASE_URL",
        "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL",
        "PROPERTYQUARRY_WORKER_DATABASE_URL",
        "PROPERTYQUARRY_SCHEDULER_DATABASE_URL",
        "PROPERTYQUARRY_RENDER_DATABASE_URL",
        "PROPERTYQUARRY_MIGRATION_DATABASE_URL",
    ):
        if not re.search(
            rf"^{re.escape(env_name)}=$",
            env_example,
            flags=re.MULTILINE,
        ):
            failures.append(
                ".env.example must list the blank service-scoped database "
                f"placeholder {env_name}"
            )
    expected_service_aliases = {
        "PROPERTYQUARRY_API_SERVICE": "propertyquarry-api",
        "PROPERTYQUARRY_WORKER_SERVICE": "propertyquarry-worker",
        "PROPERTYQUARRY_SCHEDULER_SERVICE": "propertyquarry-scheduler",
        "PROPERTYQUARRY_DB_SERVICE": "propertyquarry-db",
        "PROPERTYQUARRY_API_CONTAINER_NAME": "propertyquarry-api-live",
        "PROPERTYQUARRY_WORKER_CONTAINER_NAME": "propertyquarry-worker-live",
        "PROPERTYQUARRY_SCHEDULER_CONTAINER_NAME": "propertyquarry-scheduler-live",
        "PROPERTYQUARRY_DB_CONTAINER_NAME": "propertyquarry-db-live",
        "PROPERTYQUARRY_RENDER_CONTAINER_NAME": "propertyquarry-render-live",
    }
    for env_name, expected_value in expected_service_aliases.items():
        if not re.search(rf"^{re.escape(env_name)}={re.escape(expected_value)}$", env_example, flags=re.MULTILINE):
            failures.append(f".env.example must default {env_name}={expected_value}")

    compose = _read("docker-compose.property.yml")
    forbidden_compose_tokens = (
        "ea-openvoice",
        "openvoice",
        "ea-responses-proxy",
        "ea-teable-relay",
        "memorial",
        "/var/run/docker.sock",
        "/mnt/onedrive",
        "/mnt/pcloud",
    )
    for token in forbidden_compose_tokens:
        if token in compose.lower():
            failures.append(f"docker-compose.property.yml contains inherited surface: {token}")
    for service_name in (
        "propertyquarry-api",
        "propertyquarry-worker",
        "propertyquarry-scheduler",
        "propertyquarry-db",
    ):
        if service_name not in compose:
            failures.append(f"docker-compose.property.yml missing {service_name}")
    expected_container_name_envs = (
        'container_name: "${PROPERTYQUARRY_API_CONTAINER_NAME:-propertyquarry-api-live}"',
        'container_name: "${PROPERTYQUARRY_WORKER_CONTAINER_NAME:-propertyquarry-worker-live}"',
        'container_name: "${PROPERTYQUARRY_SCHEDULER_CONTAINER_NAME:-propertyquarry-scheduler-live}"',
        'container_name: "${PROPERTYQUARRY_DB_CONTAINER_NAME:-propertyquarry-db-live}"',
        'container_name: "${PROPERTYQUARRY_RENDER_CONTAINER_NAME:-propertyquarry-render-live}"',
    )
    for expected in expected_container_name_envs:
        if expected not in compose:
            failures.append(f"docker-compose.property.yml must keep recoverable container alias {expected}")
    worker_marker = "  propertyquarry-worker:\n"
    scheduler_marker = "  propertyquarry-scheduler:\n"
    try:
        worker_section = compose.split(worker_marker, 1)[1].split(scheduler_marker, 1)[0]
    except IndexError:
        worker_section = ""
    required_worker_contracts = (
        'image: "${PROPERTYQUARRY_WEB_IMAGE:-propertyquarry-standalone-web-runtime:latest}"',
        'container_name: "${PROPERTYQUARRY_WORKER_CONTAINER_NAME:-propertyquarry-worker-live}"',
        "cap_drop:\n      - ALL",
        'security_opt:\n      - "no-new-privileges:true"',
        "read_only: true",
        "EA_ROLE: worker",
        'EA_STORAGE_BACKEND: "postgres"',
        'PROPERTYQUARRY_WORKER_PROFILE: "property_only"',
        'PROPERTYQUARRY_SEARCH_SCHEMA_READINESS_REQUIRED: "1"',
        "EA_WORKER_HEARTBEAT_PATH: /data/artifacts/propertyquarry-worker-heartbeat.json",
        'EA_WORKER_HEARTBEAT_MAX_AGE_SECONDS: "${EA_WORKER_HEARTBEAT_MAX_AGE_SECONDS:-120}"',
        'PROPERTYQUARRY_RELEASE_COMMIT_SHA: "${PROPERTYQUARRY_RELEASE_COMMIT_SHA:-}"',
        'PROPERTYQUARRY_RELEASE_IMAGE_DIGEST: "${PROPERTYQUARRY_RELEASE_IMAGE_DIGEST:-}"',
        'PROPERTYQUARRY_RELEASE_DEPLOYMENT_ID: "${PROPERTYQUARRY_RELEASE_DEPLOYMENT_ID:-}"',
        "./config:/config:ro",
        "./config:/app/config:ro",
        "propertyquarry_artifacts:/data/artifacts",
        "propertyquarry-db:",
        "propertyquarry-migrate:",
        "condition: service_completed_successfully",
        'test: ["CMD", "/usr/local/bin/python", "-m", "app.scheduler_healthcheck"]',
    )
    if not worker_section or any(
        required not in worker_section for required in required_worker_contracts
    ):
        failures.append(
            "docker-compose.property.yml must keep a hardened property-only durable worker"
        )
    if any(
        forbidden in worker_section
        for forbidden in (
            "property_scene_video_shared.env",
            "propertyquarry_render_internal",
            "PROPERTYQUARRY_MAGICFIT",
            "PROPERTYQUARRY_RECONSTRUCTION_RENDER",
        )
    ):
        failures.append(
            "docker-compose.property.yml worker must remain independent of advanced visuals"
        )
    try:
        api_section = compose.split("  propertyquarry-api:\n", 1)[1].split(
            "  propertyquarry-migrate:\n", 1
        )[0]
    except IndexError:
        api_section = ""
    for required_api_worker_gate in (
        'PROPERTYQUARRY_WORKER_HEARTBEAT_REQUIRED: "1"',
        "EA_WORKER_HEARTBEAT_PATH: /data/artifacts/propertyquarry-worker-heartbeat.json",
    ):
        if required_api_worker_gate not in api_section:
            failures.append(
                "docker-compose.property.yml API must fail closed on worker heartbeat"
            )
    service_section_markers = (
        ("propertyquarry-api", "propertyquarry-migrate"),
        ("propertyquarry-migrate", "propertyquarry-worker"),
        ("propertyquarry-worker", "propertyquarry-scheduler"),
        ("propertyquarry-scheduler", "propertyquarry-render-tools"),
        ("propertyquarry-render-tools", "propertyquarry-db"),
    )
    service_sections: dict[str, str] = {}
    for service_name, next_service_name in service_section_markers:
        try:
            service_sections[service_name] = compose.split(
                f"  {service_name}:\n",
                1,
            )[1].split(f"  {next_service_name}:\n", 1)[0]
        except IndexError:
            service_sections[service_name] = ""
    expected_database_mappings = {
        "propertyquarry-api": (
            'DATABASE_URL: "${PROPERTYQUARRY_API_DATABASE_URL:?'
        ),
        "propertyquarry-migrate": (
            'DATABASE_URL: "${PROPERTYQUARRY_MIGRATION_DATABASE_URL:?'
        ),
        "propertyquarry-worker": (
            'DATABASE_URL: "${PROPERTYQUARRY_WORKER_DATABASE_URL:?'
        ),
        "propertyquarry-scheduler": (
            'DATABASE_URL: "${PROPERTYQUARRY_SCHEDULER_DATABASE_URL:?'
        ),
        "propertyquarry-render-tools": (
            'DATABASE_URL: "${PROPERTYQUARRY_RENDER_DATABASE_URL:?'
        ),
    }
    for service_name, expected_mapping in expected_database_mappings.items():
        section = service_sections.get(service_name, "")
        if expected_mapping not in section:
            failures.append(
                "docker-compose.property.yml must map the service-scoped DSN "
                f"for {service_name}"
            )
    if (
        'PROPERTYQUARRY_API_ADMISSION_DATABASE_URL: '
        '"${PROPERTYQUARRY_API_ADMISSION_DATABASE_URL:?' not in api_section
        or 'PROPERTYQUARRY_ADMISSION_BACKEND: "postgres"' not in api_section
    ):
        failures.append(
            "docker-compose.property.yml API must require its dedicated "
            "PostgreSQL admission DSN"
        )
    for service_name in (
        "propertyquarry-api",
        "propertyquarry-worker",
        "propertyquarry-scheduler",
        "propertyquarry-render-tools",
    ):
        section = service_sections.get(service_name, "")
        if re.search(r"^\s+-\s+\.env\s*$", section, flags=re.MULTILINE):
            failures.append(
                "docker-compose.property.yml long-lived service must not load "
                f"the broad .env file: {service_name}"
            )
        for forbidden_database_authority in (
            "${DATABASE_URL",
            "postgresql://postgres:",
        ):
            if forbidden_database_authority in section:
                failures.append(
                    "docker-compose.property.yml long-lived service inherits "
                    "generic or migration database authority: "
                    f"{service_name} ({forbidden_database_authority})"
                )
        for protected_database_secret in (
            "PROPERTYQUARRY_MIGRATION_DATABASE_URL",
            "POSTGRES_PASSWORD",
        ):
            if (
                protected_database_secret in section
                and f'{protected_database_secret}: ""' not in section
            ):
                failures.append(
                    "docker-compose.property.yml long-lived service inherits "
                    "a migration or bootstrap secret instead of overriding it "
                    f"to blank: {service_name} ({protected_database_secret})"
                )
    migrate_section = service_sections.get("propertyquarry-migrate", "")
    if "${DATABASE_URL" in migrate_section or "postgresql://postgres:" in migrate_section:
        failures.append(
            "docker-compose.property.yml migration must use only its isolated "
            "service-scoped DSN"
        )
    if "POSTGRES_HOST_AUTH_METHOD" in compose or ":-trust" in compose:
        failures.append("docker-compose.property.yml must not default Postgres to trust auth")
    if 'POSTGRES_PASSWORD: "${POSTGRES_PASSWORD:?' not in compose:
        failures.append("docker-compose.property.yml must require POSTGRES_PASSWORD")
    if 'EA_RUNTIME_MODE: "${EA_RUNTIME_MODE:-prod}"' not in compose:
        failures.append("docker-compose.property.yml must default EA_RUNTIME_MODE to prod")
    if 'PROPERTYQUARRY_SCHEDULER_PROFILE: "${PROPERTYQUARRY_SCHEDULER_PROFILE:-property_only}"' not in compose:
        failures.append("docker-compose.property.yml must default the scheduler to property_only")
    if "dockerfile: ea/Dockerfile.property-web" not in compose:
        failures.append("docker-compose.property.yml must run API/worker/scheduler from the lightweight web runtime")
    if 'image: "${PROPERTYQUARRY_WEB_IMAGE:-propertyquarry-standalone-web-runtime:latest}"' not in compose:
        failures.append("docker-compose.property.yml must name the lightweight web runtime image")
    if "propertyquarry-render-tools:" not in compose or "render-tools" not in compose:
        failures.append("docker-compose.property.yml must expose an explicit render-tools profile")
    if 'image: "${PROPERTYQUARRY_RENDER_IMAGE:-propertyquarry-standalone-render-runtime:latest}"' not in compose:
        failures.append("docker-compose.property.yml must name the render tooling image separately")
    if re.search(r"^\s+user:\s*[\"']?0(?::0)?[\"']?\s*$", compose, flags=re.MULTILINE):
        failures.append("docker-compose.property.yml must not run property web services as root")
    if "SYS_NICE" in compose:
        failures.append("docker-compose.property.yml must not grant SYS_NICE to property web services")

    dockerfile = _read("ea/Dockerfile.property")
    _append_dockerfile_runtime_failures(
        failures,
        path="ea/Dockerfile.property",
        dockerfile=dockerfile,
    )
    if " docker.io" in dockerfile or "docker-compose" in dockerfile or "docker-29." in dockerfile:
        failures.append("ea/Dockerfile.property must not install Docker tooling")
    render_instructions = _logical_instructions(dockerfile)
    if (
        "COPY --chmod=0444 ea/requirements.property-render.txt "
        "/app/requirements.property-render.txt"
        not in render_instructions
    ):
        failures.append(
            "ea/Dockerfile.property must copy the dedicated hashed render requirements file"
        )
    render_pip_installs = [
        instruction
        for instruction in render_instructions
        if instruction.startswith("RUN ") and "python -m pip install" in instruction
    ]
    if not any(
        "--require-hashes" in instruction
        and "--only-binary=:all:" in instruction
        and "-r /app/requirements.property-render.txt" in instruction
        for instruction in render_pip_installs
    ):
        failures.append(
            "ea/Dockerfile.property must install /app/requirements.property-render.txt "
            "with --require-hashes and --only-binary=:all:"
        )
    render_requirement_failures = _hashed_requirement_contract_failures(
        _read("ea/requirements.property-render.txt")
    )
    if render_requirement_failures:
        failures.append(
            "ea/requirements.property-render.txt must pin every requirement with a "
            "sha256 hash: "
            + ", ".join(render_requirement_failures)
        )
    for required_render_runtime in (
        "psycopg==3.3.4",
        "psycopg-binary==3.3.4",
    ):
        if required_render_runtime not in _read(
            "ea/requirements.property-render.txt"
        ):
            failures.append(
                "ea/requirements.property-render.txt must include the pinned "
                "distributed admission runtime"
            )
            break
    for required_render_copy in (
        "COPY --chmod=0444 ea/app/observability.py /app/ea/app/observability.py",
        (
            "COPY --chmod=0444 ea/app/services/admission_control.py "
            "/app/ea/app/services/admission_control.py"
        ),
    ):
        if required_render_copy not in render_instructions:
            failures.append(
                "ea/Dockerfile.property must copy the bounded render admission runtime"
            )
            break
    if (
        'PROPERTYQUARRY_RENDER_DATABASE_URL:?Set a least-privilege '
        'PROPERTYQUARRY_RENDER_DATABASE_URL for admission state'
        not in compose
    ):
        failures.append(
            "docker-compose.property.yml render bridge must require its dedicated admission DSN"
        )
    if "for script in /tmp/src/scripts/*" in dockerfile or 'cp "$script" /app/scripts/' in dockerfile:
        failures.append("ea/Dockerfile.property must not bulk-copy scripts into the runtime image")
    web_dockerfile = _read("ea/Dockerfile.property-web")
    _append_dockerfile_runtime_failures(
        failures,
        path="ea/Dockerfile.property-web",
        dockerfile=web_dockerfile,
    )
    if " docker.io" in web_dockerfile or "docker-compose" in web_dockerfile or "docker-29." in web_dockerfile:
        failures.append("ea/Dockerfile.property-web must not install Docker tooling")
    if not _web_wheelhouse_install_contract_present(web_dockerfile):
        failures.append(
            "ea/Dockerfile.property-web must verify requirements.lock and install "
            "from the hash-locked offline wheelhouse"
        )
    if "COPY scripts/willhaben_property_packet.py /app/scripts/willhaben_property_packet.py" not in web_dockerfile:
        failures.append("ea/Dockerfile.property-web must explicitly copy the Willhaben packet helper")
    required_web_shared_copies = (
        (
            "COPY scripts/property_magicfit_contact_sheet.py "
            "/app/scripts/property_magicfit_contact_sheet.py"
        ),
        (
            "COPY scripts/property_magicfit_delivery_contract.py "
            "/app/scripts/property_magicfit_delivery_contract.py"
        ),
        (
            "COPY scripts/property_magicfit_public_eligibility.py "
            "/app/scripts/property_magicfit_public_eligibility.py"
        ),
        (
            "COPY scripts/property_magicfit_reviewer_authority.py "
            "/app/scripts/property_magicfit_reviewer_authority.py"
        ),
        (
            "COPY scripts/property_magicfit_secure_io.py "
            "/app/scripts/property_magicfit_secure_io.py"
        ),
        (
            "COPY scripts/property_tour_publication_lock.py "
            "/app/scripts/property_tour_publication_lock.py"
        ),
        (
            "COPY scripts/propertyquarry_playwright_runtime.py "
            "/app/scripts/propertyquarry_playwright_runtime.py"
        ),
        (
            "COPY scripts/browseract_ui_media.py "
            "/app/scripts/browseract_ui_media.py"
        ),
        (
            "COPY scripts/property_scene_video_shared_env.py "
            "/app/scripts/property_scene_video_shared_env.py"
        ),
    )
    if any(copy not in web_dockerfile for copy in required_web_shared_copies):
        failures.append(
            "ea/Dockerfile.property-web must explicitly copy the shared MagicFit "
            "contact-sheet, delivery-contract, eligibility, reviewer-authority, "
            "secure-I/O, publication-lock, browser runtime, media, and "
            "scene-video environment helpers"
        )
    reviewer_overlay = _read("docker-compose.property-magicfit-reviewer.yml")
    reviewer_trust_env = "PROPERTYQUARRY_MAGICFIT_REVIEWER_TRUST_STORE_FILE"
    reviewer_trust_target = "/run/propertyquarry/magicfit-reviewer-trust"
    reviewer_trust_source = "PROPERTYQUARRY_MAGICFIT_REVIEWER_TRUST_DIR"
    if reviewer_trust_env in compose:
        failures.append(
            "base PropertyQuarry compose must keep optional reviewer trust out of Core Gold"
        )
    if (
        reviewer_overlay.count("  propertyquarry-api:\n") != 1
        or reviewer_overlay.count("  propertyquarry-scheduler:\n") != 1
        or reviewer_overlay.count(reviewer_trust_env) != 2
        or reviewer_overlay.count(reviewer_trust_target) != 4
        or reviewer_overlay.count(reviewer_trust_source) != 2
        or reviewer_overlay.count("read_only: true") != 2
        or reviewer_overlay.count("create_host_path: false") != 2
    ):
        failures.append(
            "MagicFit reviewer overlay must mount one explicit external trust "
            "directory read-only without host-path creation in API and scheduler"
        )
    env_example = _read(".env.example")
    if f"{reviewer_trust_source}=\n" not in env_example:
        failures.append(
            ".env.example must declare the optional MagicFit reviewer trust directory"
        )
    if "for script in /tmp/src/scripts/*" in web_dockerfile or 'cp "$script" /app/scripts/' in web_dockerfile:
        failures.append("ea/Dockerfile.property-web must not bulk-copy scripts into the runtime image")
    for forbidden_native_tool in (
        "blender",
        "colmap",
        "espeak",
        "ffmpeg",
        "imagemagick",
        "libimage-exiftool-perl",
        "meshlab",
        "meshlabserver",
    ):
        if forbidden_native_tool in web_dockerfile.lower():
            failures.append(f"ea/Dockerfile.property-web must not install native media/render tool {forbidden_native_tool}")
    for forbidden_browser_payload in ("PLAYWRIGHT_BROWSERS_PATH=/ms-playwright", "python -m playwright install --with-deps chromium"):
        if forbidden_browser_payload in web_dockerfile:
            failures.append("ea/Dockerfile.property-web must not install browser payloads in the request-serving image")
    if not re.search(r"image:\s+\S+@sha256:[0-9a-f]{64}", compose):
        failures.append("docker-compose.property.yml must pin sidecar images by digest")

    public_tours = _read("ea/app/api/routes/public_tours.py")
    if "tour-action-tokens" in public_tours or "tourActionTokens" in public_tours:
        failures.append("public tours must not emit bearer-style action tokens into HTML")
    if "record_property_feedback(" in public_tours:
        failures.append("public tour feedback must not directly mutate owner learning profiles")
    if "request_property_tour_detail_refresh(" in public_tours:
        failures.append("public tour request-details must not queue owner work from public links")
    if 'request.headers.get("x-forwarded-for")' in public_tours and "PROPERTYQUARRY_TRUST_X_FORWARDED_FOR" not in public_tours:
        failures.append("public tour feedback must not trust x-forwarded-for without explicit opt-in")
    if 'except Exception:\n        pass' in public_tours:
        failures.append("public tour feedback must not silently swallow persistence failures")
    if '"status": "not_captured"' not in public_tours:
        failures.append("public tour feedback must report persistence failures honestly")
    public_tour_payload_match = re.search(
        r"def public_tour_payload\(slug: str\).*?(?=\n\n@router\.)",
        public_tours,
        flags=re.DOTALL,
    )
    public_tour_payload_body = public_tour_payload_match.group(0) if public_tour_payload_match else ""
    if (
        "_redacted_public_tour_payload(" not in public_tour_payload_body
        or "expose_asset_relpaths=False" not in public_tour_payload_body
        or "include_external_tour_urls=False" not in public_tour_payload_body
    ):
        failures.append("public tour JSON must use the redacted public payload builder")
    if "_PUBLIC_TOUR_DENIED_ASSET_EXTENSIONS" not in public_tours or "_public_tour_manifest(payload)" not in public_tours or "safe_relpath not in manifest" not in public_tours:
        failures.append("public tour file serving must use a manifest-backed asset allowlist with denied sidecar extensions")
    forbidden_public_render_fetchers = (
        "_fetch_listing_research",
        "_reverse_geocode",
        "_fetch_nearby_poi_research",
        "nominatim.openstreetmap.org",
        "overpass-api.de",
    )
    for token in forbidden_public_render_fetchers:
        if token in public_tours:
            failures.append("public tour render routes must use stored research snapshots, not live listing/geospatial fetches")
            break
    if "PROPERTYQUARRY_PUBLIC_MEDIA_ALLOWED_HOSTS" not in public_tours or "_public_tour_static_media_url_allowed" not in public_tours:
        failures.append("public tour scene media must use a static external-media host allowlist")
    if "_PUBLIC_TOUR_EXACT_LOCATION_FACT_KEYS" not in public_tours or "_redacted_public_tour_facts" not in public_tours:
        failures.append("public tour facts must use mode-aware exact-location redaction")
    if "_public_tour_external_media_url_allowed" not in public_tours:
        failures.append("public tour scene media must pass through the external-media URL guard")
    if "_PUBLIC_TOUR_PUBLIC_PDF_PRIVACY_CLASSES" not in public_tours or "floorplan_pdf_public" not in public_tours:
        failures.append("public tour PDFs must require an explicit public floorplan privacy class")
    if "PROPERTYQUARRY_PUBLIC_RATE_LIMIT_FAIL_CLOSED" not in public_tours or "_public_tour_prod_mode_enabled()" not in public_tours:
        failures.append("public tour durable rate-limit failures must fail closed in prod")
    if "_public_tour_security_headers" not in public_tours or "Content-Security-Policy" not in public_tours:
        failures.append("public tours must set public page/file security headers")

    requirements = _read("ea/requirements.txt")
    lock_text = _read("ea/requirements.lock")
    lock_names = _lock_package_names(lock_text)
    for line in requirements.splitlines():
        name = _normalized_requirement_name(line)
        if not name:
            continue
        if name.replace("_", "-") not in lock_names:
            failures.append(f"ea/requirements.lock missing direct requirement {name}")
    for line in lock_text.splitlines():
        raw = line.strip()
        if raw and not raw.startswith("#") and "==" not in raw:
            failures.append(f"ea/requirements.lock contains an unpinned row: {raw}")

    required_checks = [
        "property_env_placeholders",
        "property_service_aliases",
        "property_compose_isolation",
        "service_scoped_database_credentials",
        "non_root_pinned_runtime_image",
        "lightweight_web_runtime_split",
        "web_runtime_browser_payload_isolation",
        "render_tooling_profile",
        "render_hashed_requirements",
        "web_runtime_verified_hash_locked_offline_dependencies",
        "web_runtime_non_root_compose",
        "web_runtime_no_sys_nice",
        "web_runtime_willhaben_helper",
        "no_docker_tooling_in_property_runtime",
        "sidecar_images_pinned_by_digest",
        "public_tour_secret_and_mutation_guards",
        "public_tour_redacted_payloads",
        "public_tour_manifest_asset_allowlist",
        "public_tour_no_live_research_fetches",
        "public_tour_media_host_allowlist",
        "public_tour_exact_location_redaction",
        "public_tour_pdf_privacy_class",
        "public_tour_rate_limit_fail_closed",
        "public_tour_security_headers",
        "locked_direct_requirements",
    ]
    return {
        "schema": "propertyquarry.security_posture_receipt.v1",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "status": "pass" if not failures else "fail",
        "required_checks": required_checks,
        "failure_count": len(failures),
        "failures": failures,
        "note": "Static production-security posture gate for the isolated PropertyQuarry deployment plane.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check PropertyQuarry production security posture.")
    parser.add_argument("--write", default="", help="Optional path for a JSON receipt.")
    args = parser.parse_args()

    receipt = build_security_posture_receipt()
    failures = list(receipt.get("failures") or [])
    if args.write:
        out_path = Path(args.write)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if failures:
        print("property security posture check failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("ok: property security posture")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
