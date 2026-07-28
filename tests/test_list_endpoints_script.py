from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "list_endpoints.sh"


def _environment(tmp_path: Path, *, body: str, exit_code: int = 0) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    curl = bin_dir / "curl"
    curl.write_text(
        """#!/usr/bin/env python3
import os
import sys

sys.stdout.write(os.environ.get("FAKE_CURL_BODY", ""))
raise SystemExit(int(os.environ.get("FAKE_CURL_EXIT_CODE", "0")))
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "EA_HOST_PORT": "18090",
        "FAKE_CURL_BODY": body,
        "FAKE_CURL_EXIT_CODE": str(exit_code),
    }


def test_list_endpoints_parses_piped_openapi_and_sorts_rows(tmp_path: Path) -> None:
    document = {
        "paths": {
            "/z-last": {"post": {}, "get": {}},
            "/a-first": {"delete": {}, "get": {}},
        }
    }

    completed = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=_environment(tmp_path, body=json.dumps(document)),
        capture_output=True,
        text=True,
        check=True,
    )

    assert completed.stdout.splitlines() == [
        "DELETE  /a-first",
        "GET     /a-first",
        "GET     /z-last",
        "POST    /z-last",
    ]


def test_list_endpoints_propagates_curl_and_parser_failures(tmp_path: Path) -> None:
    curl_failure = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=_environment(tmp_path / "curl", body="", exit_code=22),
        capture_output=True,
        text=True,
        check=False,
    )
    parser_failure = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=_environment(tmp_path / "parser", body="{not-json"),
        capture_output=True,
        text=True,
        check=False,
    )

    assert curl_failure.returncode != 0
    assert parser_failure.returncode != 0
