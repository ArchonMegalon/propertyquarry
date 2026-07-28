from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "recover_host_after_reboot.sh"
LOG_PREFIX = "[propertyquarry-postreboot] "


def test_compose_recovery_uses_each_stack_as_its_project_directory(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$FAKE_DOCKER_LOG"\n',
        encoding="utf-8",
    )
    fake_docker.chmod(0o700)

    environment = dict(os.environ)
    environment.update(
        {
            "FAKE_DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "PROPERTYQUARRY_HOST_RECOVERY_ALLOW": "1",
            "PROPERTYQUARRY_HOST_RECOVERY_DRY_RUN": "1",
        }
    )
    completed = subprocess.run(
        ("bash", str(SCRIPT)),
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )

    commands = [
        line.removeprefix(LOG_PREFIX)
        for line in completed.stdout.splitlines()
        if line.startswith(LOG_PREFIX + "running: docker compose --project-directory")
    ]
    assert commands == [
        "running: docker compose --project-directory /docker/dozzle "
        "-f /docker/dozzle/docker-compose.yml "
        "up -d --force-recreate --remove-orphans",
        "running: docker compose --project-directory /docker/filebrowser "
        "-f /docker/filebrowser/docker-compose.yml "
        "up -d --force-recreate --remove-orphans",
        "running: docker compose --project-directory /docker/plex "
        "-f /docker/plex/docker-compose.yml "
        "-f /docker/plex/docker-compose.override.yml "
        "up -d --force-recreate --remove-orphans",
        "running: docker compose --project-directory /docker/arr-v2 "
        "-f /docker/arr-v2/docker-compose.yml "
        "up -d --force-recreate --remove-orphans",
        "running: docker compose --project-directory /docker/fleet "
        "-f /docker/fleet/docker-compose.yml "
        "up -d --force-recreate --remove-orphans",
        "running: docker compose --project-directory "
        "/docker/chummercomplete/chummer.run-services "
        "-f /docker/chummercomplete/chummer.run-services/docker-compose.public-edge.yml "
        "up -d --force-recreate --remove-orphans",
        "running: docker compose --project-directory /docker/immich "
        "-f /docker/immich/docker-compose.yml "
        "-f /docker/immich/docker-compose.override.yml "
        "up -d --force-recreate --remove-orphans",
    ]
    assert docker_log.read_text(encoding="utf-8").splitlines() == [
        "info",
        "compose version",
    ]
