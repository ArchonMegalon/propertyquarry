from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import propertyquarry_accessibility_gate as accessibility_gate
from scripts import propertyquarry_live_probe_secret_scope as secret_scope
from scripts import propertyquarry_live_mobile_surface_smoke as mobile_smoke


ROOT = Path(__file__).resolve().parents[1]
LIVE_SECRET_ENV = "PROPERTYQUARRY_LIVE_PROBE_SECRET"
PERFORMANCE_SECRET_ENV = "PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET"


def _hostile_bash_env(tmp_path: Path) -> tuple[Path, Path]:
    leak_path = tmp_path / "hostile-startup-hook.leak"
    hook_path = tmp_path / "hostile-bash-env.sh"
    hook_path.write_text(
        "if [[ -n \"${PROPERTYQUARRY_LIVE_PROBE_SECRET:-}\" ]]; then\n"
        "  builtin printf '%s' \"${PROPERTYQUARRY_LIVE_PROBE_SECRET}\" "
        "> \"${PQ_STARTUP_HOOK_LEAK}\"\n"
        "fi\n",
        encoding="utf-8",
    )
    hook_path.chmod(0o600)
    return hook_path, leak_path


def _prove_hostile_bash_env_is_live(
    *,
    hook_path: Path,
    leak_path: Path,
    secret: str,
) -> dict[str, str]:
    environment = {
        "PATH": "/usr/bin:/bin",
        "BASH_ENV": str(hook_path),
        "ENV": str(hook_path),
        "PQ_STARTUP_HOOK_LEAK": str(leak_path),
        LIVE_SECRET_ENV: secret,
    }
    control = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", ":"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert control.returncode == 0, (control.stdout, control.stderr)
    assert leak_path.read_text(encoding="utf-8") == secret
    leak_path.unlink()
    return environment


def test_outer_release_gate_privileged_startup_blocks_hostile_bash_env(
    tmp_path: Path,
) -> None:
    secret = "synthetic-outer-startup-secret-" + "S" * 48
    hook_path, leak_path = _hostile_bash_env(tmp_path)
    environment = _prove_hostile_bash_env_is_live(
        hook_path=hook_path,
        leak_path=leak_path,
        secret=secret,
    )

    release_gate = ROOT / "scripts/property_release_gates.sh"
    result = subprocess.run(
        [str(release_gate)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert not leak_path.exists()
    assert secret not in result.stdout
    assert secret not in result.stderr
    source = release_gate.read_text(encoding="utf-8")
    assert source.startswith("#!/bin/bash -p\n")
    assert source.index("builtin unset \\\n") < source.index(
        'performance_release_probe_secret="${PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET'
    )
    assert os.access(release_gate, os.X_OK)


def test_nested_live_gate_launcher_removes_hostile_bash_env_before_secret_assignment(
    tmp_path: Path,
) -> None:
    secret = "synthetic-nested-startup-secret-" + "N" * 48
    hook_path, leak_path = _hostile_bash_env(tmp_path)
    environment = _prove_hostile_bash_env_is_live(
        hook_path=hook_path,
        leak_path=leak_path,
        secret=secret,
    )

    result = subprocess.run(
        [
            "/usr/bin/env",
            "-u",
            "BASH_ENV",
            "-u",
            "ENV",
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-p",
            "scripts/propertyquarry_live_release_gates.sh",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert not leak_path.exists()
    assert secret not in result.stdout
    assert secret not in result.stderr
    release_source = (ROOT / "scripts/property_release_gates.sh").read_text(
        encoding="utf-8"
    )
    secret_assignment = (
        f'{LIVE_SECRET_ENV}="${{performance_release_probe_secret}}" \\\n'
    )
    env_invocation = "/usr/bin/env \\\n  -u BASH_ENV \\\n  -u ENV \\\n"
    assert secret_assignment in release_source
    assert env_invocation in release_source
    assert release_source.index(secret_assignment) < release_source.index(
        env_invocation
    )


def test_nested_live_gate_secret_is_environment_only_not_env_argv(
    tmp_path: Path,
) -> None:
    secret = "synthetic-env-argv-secret-" + "A" * 48
    release_source = (ROOT / "scripts/property_release_gates.sh").read_text(
        encoding="utf-8"
    )
    launch_start = release_source.index(
        f'{LIVE_SECRET_ENV}="${{performance_release_probe_secret}}" \\\n'
    )
    launch_end = release_source.index(
        "\nunset performance_release_probe_secret",
        launch_start,
    )
    launch_block = release_source[launch_start:launch_end]
    assert launch_block.count("/usr/bin/env") == 1

    capture_script = tmp_path / "capture-env-argv.py"
    capture_output = tmp_path / "captured-env-argv.json"
    capture_script.write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['PQ_ARGV_CAPTURE_OUTPUT']).write_text(\n"
        "    json.dumps({\n"
        "        'argv': sys.argv,\n"
        "        'secret_environment': os.environ.get(\n"
        "            'PROPERTYQUARRY_LIVE_PROBE_SECRET'\n"
        "        ),\n"
        "    }),\n"
        "    encoding='utf-8',\n"
        ")\n",
        encoding="utf-8",
    )
    capture_launch_block = launch_block.replace(
        "/usr/bin/env",
        '"${PQ_TEST_PYTHON}" "${PQ_ARGV_CAPTURE_SCRIPT}"',
        1,
    )
    shell_program = (
        'performance_release_probe_secret="${PQ_TEST_SOURCE_SECRET}"\n'
        'PYTHON_BIN="${PQ_TEST_PYTHON}"\n'
        + capture_launch_block
    )
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-p", "-c", shell_program],
        cwd=ROOT,
        env={
            "PATH": "/usr/bin:/bin",
            "PQ_ARGV_CAPTURE_OUTPUT": str(capture_output),
            "PQ_ARGV_CAPTURE_SCRIPT": str(capture_script),
            "PQ_TEST_PYTHON": sys.executable,
            "PQ_TEST_SOURCE_SECRET": secret,
        },
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    captured = json.loads(capture_output.read_text(encoding="utf-8"))
    assert captured["secret_environment"] == secret
    assert captured["argv"][1:] == [
        "-u",
        "BASH_ENV",
        "-u",
        "ENV",
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-p",
        "scripts/propertyquarry_live_release_gates.sh",
    ]
    assert all(secret not in argument for argument in captured["argv"])
    assert all(
        not argument.startswith(f"{LIVE_SECRET_ENV}=")
        for argument in captured["argv"]
    )


def test_release_probe_stdin_is_exactly_bounded_and_malformed_input_is_not_echoed(
    monkeypatch,
    capsys,
) -> None:
    parser = argparse.ArgumentParser(prog="probe-secret-test")
    for name in (LIVE_SECRET_ENV, PERFORMANCE_SECRET_ENV):
        monkeypatch.delenv(name, raising=False)
    exact_secret = "s" * secret_scope.MAX_RELEASE_PROBE_SECRET_BYTES
    monkeypatch.setattr(
        secret_scope.sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO((exact_secret + "\n").encode("utf-8"))),
    )
    assert secret_scope.read_release_probe_secret_from_stdin(
        parser,
        enabled=True,
    ) == exact_secret

    reflected_marker = "oversized-secret-marker"
    oversized = (reflected_marker + "x" * 4_200).encode("utf-8")
    monkeypatch.setattr(
        secret_scope.sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(oversized)),
    )
    with pytest.raises(SystemExit) as exc_info:
        secret_scope.read_release_probe_secret_from_stdin(parser, enabled=True)
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert reflected_marker not in captured.out
    assert reflected_marker not in captured.err


def _live_release_environment(
    *,
    python_stub: Path,
    trace_path: Path,
    security_receipt: Path,
    live_secret: str,
    performance_secret: str,
) -> dict[str, str]:
    return {
        "PATH": f"{python_stub.parent}:/usr/bin:/bin",
        "PYTHON_BIN": str(python_stub),
        "PQ_SECRET_SCOPE_TRACE": str(trace_path),
        "PQ_EXPECTED_STDIN_LENGTH": str(len(live_secret)),
        LIVE_SECRET_ENV: live_secret,
        PERFORMANCE_SECRET_ENV: performance_secret,
        "PROPERTYQUARRY_LIVE_MOBILE_BASE_URL": "https://propertyquarry.invalid",
        "PROPERTYQUARRY_LIVE_RESEARCH_DETAIL_ROUTE": (
            "/app/research/perf-candidate-1020?run_id=run-gold-mobile"
        ),
        "PROPERTYQUARRY_LIVE_PRINCIPAL_ID": "pq-live-mobile-smoke",
        "PROPERTYQUARRY_ACCESSIBILITY_PUBLIC_TOUR_ROUTE": "/tours/flagship-proof",
        "PROPERTYQUARRY_EXPECTED_RELEASE_COMMIT_SHA": "a" * 40,
        "PROPERTYQUARRY_EXPECTED_RELEASE_REPOSITORY": "owner/property",
        "PROPERTYQUARRY_EXPECTED_RELEASE_PUBLIC_ORIGIN": (
            "https://propertyquarry.invalid"
        ),
        "PROPERTYQUARRY_EXPECTED_RELEASE_BRANCH": "main",
        "PROPERTYQUARRY_EXPECTED_RELEASE_DEPLOYMENT_ID": "propertyquarry-deploy-a",
        "PROPERTYQUARRY_EXPECTED_RELEASE_ARTIFACT_SET": "global-core",
        "PROPERTYQUARRY_EXPECTED_RELEASE_LABEL": "flagship-a",
        "PROPERTYQUARRY_EXPECTED_RELEASE_GENERATED_AT": "2026-07-19T12:00:00Z",
        "PROPERTYQUARRY_EXPECTED_RELEASE_IMAGE_DIGEST": "sha256:" + "b" * 64,
        "PROPERTYQUARRY_EXPECTED_REPLICA_ID": "propertyquarry-web-1",
        "PROPERTYQUARRY_EXPECTED_WEB_IMAGE": "propertyquarry-web@sha256:" + "c" * 64,
        "PROPERTYQUARRY_EXPECTED_RENDER_IMAGE": (
            "propertyquarry-render@sha256:" + "d" * 64
        ),
        "PROPERTYQUARRY_RELEASE_SECURITY_RECEIPT": str(security_receipt),
        "PROPERTYQUARRY_RELEASE_SECURITY_WORKFLOW_BINDING": str(security_receipt),
        "PROPERTYQUARRY_WORKFLOW_HEAD_SHA": "a" * 40,
        "PROPERTYQUARRY_WORKFLOW_RUN_ID": "12345",
        "PROPERTYQUARRY_WORKFLOW_RUN_ATTEMPT": "1",
        "DATABASE_URL": "postgresql://property:test@db.invalid/property",
        "TEABLE_BASE_URL": "https://teable.invalid",
        "TEABLE_API_KEY": "test-teable-key",
        "PROPERTYQUARRY_EVIDENCE_OVERLAY_TEABLE_BASE_ID": "base-a",
        "PROPERTYQUARRY_EXPECTED_TEABLE_ORIGIN": "https://teable.invalid",
        "PROPERTYQUARRY_EXPECTED_TEABLE_BASE_ID_SHA256": "e" * 64,
        "PROPERTYQUARRY_RYBBIT_ORIGIN": "https://analytics.invalid",
        "PROPERTYQUARRY_RYBBIT_SITE_ID": "site-a",
        "PROPERTYQUARRY_RYBBIT_SITE_ID_SHA256": "f" * 64,
        "PROPERTYQUARRY_RYBBIT_API_KEY": "test-rybbit-key",
        "PROPERTYQUARRY_RYBBIT_SITE_API_URL": "https://analytics.invalid/site",
        "PROPERTYQUARRY_RYBBIT_HAS_DATA_API_URL": (
            "https://analytics.invalid/has-data"
        ),
        "PROPERTYQUARRY_RYBBIT_EVENTS_API_URL": (
            "https://analytics.invalid/events"
        ),
        "PROPERTYQUARRY_LIVE_TELEGRAM_BOT_TOKEN": "test-telegram-token",
        "PROPERTYQUARRY_LIVE_TELEGRAM_CHAT_ID": "test-chat",
    }


def test_live_release_gate_captures_before_children_and_uses_stdin_only(
    tmp_path: Path,
) -> None:
    live_secret = "live-probe-secret-" + "L" * 57
    performance_secret = "performance-probe-secret-" + "P" * 71
    trace_path = tmp_path / "child-trace.txt"
    security_receipt = tmp_path / "security.json"
    security_receipt.write_text("{}\n", encoding="utf-8")
    runtime_root = tmp_path / "runtime-root"
    runtime_scripts = runtime_root / "scripts"
    runtime_scripts.mkdir(parents=True)
    live_gate = runtime_scripts / "propertyquarry_live_release_gates.sh"
    live_gate.write_text(
        (ROOT / "scripts/propertyquarry_live_release_gates.sh").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    live_gate.chmod(0o700)
    python_stub = runtime_scripts / "propertyquarry_release_python.sh"
    python_stub.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "if [[ -n \"${PROPERTYQUARRY_LIVE_PROBE_SECRET:-}\" || "
        "-n \"${PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET:-}\" ]]; then\n"
        "  printf 'probe-secret-environment-inherited\\n' >> \"${PQ_SECRET_SCOPE_TRACE}\"\n"
        "  exit 91\n"
        "fi\n"
        "printf 'python\\t%s\\n' \"$*\" >> \"${PQ_SECRET_SCOPE_TRACE}\"\n"
        "case \" $* \" in\n"
        "  *' --release-probe-secret-stdin '*)\n"
        "    IFS= read -r supplied_secret || true\n"
        "    if [[ ${#supplied_secret} -ne ${PQ_EXPECTED_STDIN_LENGTH} ]]; then\n"
        "      printf 'unexpected-stdin-length:%s\\n' \"${#supplied_secret}\" >> \"${PQ_SECRET_SCOPE_TRACE}\"\n"
        "      exit 92\n"
        "    fi\n"
        "    printf 'bounded-secret-stdin\\n' >> \"${PQ_SECRET_SCOPE_TRACE}\"\n"
        "    ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    python_stub.chmod(0o700)
    mkdir_stub = runtime_scripts / "mkdir"
    mkdir_stub.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "if [[ -n \"${PROPERTYQUARRY_LIVE_PROBE_SECRET:-}\" || "
        "-n \"${PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET:-}\" ]]; then\n"
        "  exit 93\n"
        "fi\n"
        "printf 'mkdir-clean-environment\\n' >> \"${PQ_SECRET_SCOPE_TRACE}\"\n"
        "exec /bin/mkdir \"$@\"\n",
        encoding="utf-8",
    )
    mkdir_stub.chmod(0o700)

    result = subprocess.run(
        ["/bin/bash", str(live_gate)],
        cwd=runtime_root,
        env=_live_release_environment(
            python_stub=python_stub,
            trace_path=trace_path,
            security_receipt=security_receipt,
            live_secret=live_secret,
            performance_secret=performance_secret,
        ),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    trace = trace_path.read_text(encoding="utf-8")
    assert result.returncode == 0, (result.stdout, result.stderr, trace)
    assert trace.count("bounded-secret-stdin") == 4
    assert "probe-secret-environment-inherited" not in trace
    assert "mkdir-clean-environment" in trace
    for secret in (live_secret, performance_secret):
        assert secret not in result.stdout
        assert secret not in result.stderr
        assert secret not in trace

    source = (ROOT / "scripts/propertyquarry_live_release_gates.sh").read_text(
        encoding="utf-8"
    )
    assert "$(" not in source
    assert source.index('release_probe_secret="${PROPERTYQUARRY_LIVE_PROBE_SECRET') < source.index(
        'EA_ROOT="${PWD}"'
    )
    assert 'PROPERTYQUARRY_LIVE_PROBE_SECRET="${release_probe_secret}"' not in source
    assert source.count("--release-probe-secret-stdin") == 4


def test_mobile_python_worker_start_scrubs_both_probe_secret_environments(
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}

    class Queue:
        def __init__(self) -> None:
            self.payload: dict[str, object] | None = None

        def put(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def empty(self) -> bool:
            return self.payload is None

        def get(self) -> dict[str, object] | None:
            return self.payload

    class Process:
        exitcode = 0

        def __init__(self, *, queue: Queue) -> None:
            self.queue = queue

        def start(self) -> None:
            observed["worker_environment"] = {
                LIVE_SECRET_ENV: os.environ.get(LIVE_SECRET_ENV),
                PERFORMANCE_SECRET_ENV: os.environ.get(PERFORMANCE_SECRET_ENV),
            }
            self.queue.put(
                {
                    "ok": True,
                    "status_code": 200,
                    "metrics": {"proof_mode": "playwright"},
                }
            )

        def join(self, _timeout: int) -> None:
            pass

        def is_alive(self) -> bool:
            return False

    class Context:
        def Queue(self, *, maxsize: int) -> Queue:  # noqa: N802
            assert maxsize == 1
            return Queue()

        def Process(self, *, target, kwargs):  # noqa: N802, ANN001
            del target
            return Process(queue=kwargs["queue"])

    monkeypatch.setenv(LIVE_SECRET_ENV, "live-worker-secret")
    monkeypatch.setenv(PERFORMANCE_SECRET_ENV, "performance-worker-secret")
    monkeypatch.setattr(mobile_smoke.multiprocessing, "get_all_start_methods", lambda: ["spawn"])
    monkeypatch.setattr(mobile_smoke.multiprocessing, "get_context", lambda _method: Context())

    status, metrics = mobile_smoke.collect_playwright_route_metrics(
        route="/app/search",
        url="https://propertyquarry.invalid/app/search",
        headers={},
        authorized_origin="https://propertyquarry.invalid",
        browser_args=[],
        viewport_width=390,
        viewport_height=844,
        route_timeout_ms=2_000,
        route_deadline_seconds=3,
        release_probe_secret="in-memory-only-secret",
    )

    assert status == 200
    assert metrics["status_code"] == 200
    assert observed["worker_environment"] == {
        LIVE_SECRET_ENV: None,
        PERFORMANCE_SECRET_ENV: None,
    }
    assert os.environ[LIVE_SECRET_ENV] == "live-worker-secret"
    assert os.environ[PERFORMANCE_SECRET_ENV] == "performance-worker-secret"


def test_mobile_browser_launch_scrubs_both_probe_secret_environments(
    monkeypatch,
) -> None:
    from playwright import sync_api

    observed: dict[str, object] = {}

    class Page:
        url = "https://propertyquarry.invalid/app/search"

        def set_default_timeout(self, _timeout: int) -> None:
            pass

        def set_default_navigation_timeout(self, _timeout: int) -> None:
            pass

        def goto(self, *_args, **_kwargs):
            return SimpleNamespace(status=200)

        def wait_for_load_state(self, *_args, **_kwargs) -> None:
            pass

        def wait_for_timeout(self, _timeout: int) -> None:
            pass

        def evaluate(self, _script: str) -> dict[str, object]:
            return {"viewport_width": 390}

    class Context:
        def route(self, _pattern: str, _handler) -> None:
            pass

        def new_page(self) -> Page:
            return Page()

        def close(self) -> None:
            pass

    class Browser:
        def new_context(self, **_kwargs) -> Context:
            return Context()

        def close(self) -> None:
            pass

    class BrowserType:
        def launch(self, **_kwargs) -> Browser:
            observed["browser_environment"] = {
                LIVE_SECRET_ENV: os.environ.get(LIVE_SECRET_ENV),
                PERFORMANCE_SECRET_ENV: os.environ.get(PERFORMANCE_SECRET_ENV),
            }
            return Browser()

    class PlaywrightContext:
        def __enter__(self):
            return SimpleNamespace(chromium=BrowserType())

        def __exit__(self, *_args) -> None:
            pass

    class Queue:
        def put(self, payload: dict[str, object]) -> None:
            observed["payload"] = payload

    monkeypatch.setenv(LIVE_SECRET_ENV, "live-browser-secret")
    monkeypatch.setenv(PERFORMANCE_SECRET_ENV, "performance-browser-secret")
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: PlaywrightContext())

    mobile_smoke._playwright_route_metrics_worker(
        Queue(),
        url="https://propertyquarry.invalid/app/search",
        headers={},
        authorized_origin="https://propertyquarry.invalid",
        browser_args=[],
        viewport_width=390,
        viewport_height=844,
        route_timeout_ms=2_000,
        release_probe_secret="in-memory-only-secret",
    )

    assert observed["browser_environment"] == {
        LIVE_SECRET_ENV: None,
        PERFORMANCE_SECRET_ENV: None,
    }
    assert observed["payload"]["ok"] is True


def test_accessibility_playwright_driver_and_browser_receive_scrubbed_environment(
    monkeypatch,
) -> None:
    from playwright import sync_api

    observed: dict[str, object] = {}

    class Context:
        def add_init_script(self, *, script: str) -> None:
            observed["axe_init_script"] = script
            observed["axe_init_environment"] = {
                LIVE_SECRET_ENV: os.environ.get(LIVE_SECRET_ENV),
                PERFORMANCE_SECRET_ENV: os.environ.get(PERFORMANCE_SECRET_ENV),
            }

        def route(self, _pattern: str, _handler) -> None:
            pass

        def close(self) -> None:
            pass

    class Browser:
        def new_context(self, **_kwargs) -> Context:
            return Context()

        def close(self) -> None:
            pass

    class BrowserType:
        def launch(self, **_kwargs) -> Browser:
            observed["browser_environment"] = {
                LIVE_SECRET_ENV: os.environ.get(LIVE_SECRET_ENV),
                PERFORMANCE_SECRET_ENV: os.environ.get(PERFORMANCE_SECRET_ENV),
            }
            return Browser()

    class PlaywrightContext:
        def __enter__(self):
            observed["driver_environment"] = {
                LIVE_SECRET_ENV: os.environ.get(LIVE_SECRET_ENV),
                PERFORMANCE_SECRET_ENV: os.environ.get(PERFORMANCE_SECRET_ENV),
            }
            return SimpleNamespace(chromium=BrowserType())

        def __exit__(self, *_args) -> None:
            pass

    monkeypatch.setenv(LIVE_SECRET_ENV, "live-accessibility-secret")
    monkeypatch.setenv(PERFORMANCE_SECRET_ENV, "performance-accessibility-secret")
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: PlaywrightContext())

    rows = accessibility_gate.collect_accessibility_engine_rows(
        base_url="https://propertyquarry.invalid",
        routes=(),
        browser_engine="chromium",
        headers={},
        axe_source="window.axe = {};",
        timeout_ms=2_000,
        release_probe_secret="in-memory-only-secret",
    )

    assert rows == []
    assert observed["driver_environment"] == {
        LIVE_SECRET_ENV: None,
        PERFORMANCE_SECRET_ENV: None,
    }
    assert observed["browser_environment"] == observed["driver_environment"]
    assert observed["axe_init_script"] == "window.axe = {};"
    assert observed["axe_init_environment"] == observed["driver_environment"]


def test_probe_secret_environment_rejection_and_receipts_never_echo_values(
    tmp_path: Path,
) -> None:
    live_secret = "live-secret-never-echo-" + "x" * 40
    performance_secret = "performance-secret-never-echo-" + "y" * 40
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/propertyquarry_live_authenticated_smoke.py"),
            "--release-probe-secret-stdin",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            LIVE_SECRET_ENV: live_secret,
            PERFORMANCE_SECRET_ENV: performance_secret,
        },
        input=live_secret + "\n",
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    for secret in (live_secret, performance_secret):
        assert secret not in result.stdout
        assert secret not in result.stderr

    mobile_receipt = mobile_smoke.build_seed_fixture_blocked_receipt(
        base_url="https://propertyquarry.invalid",
        host_header="propertyquarry.invalid",
        principal_id="pq-live-mobile-smoke",
        viewport_width=390,
        viewport_height=844,
        error=f"upstream reflected {live_secret}",
        release_probe_secret=live_secret,
    )
    axe_path = tmp_path / "axe.min.js"
    axe_path.write_text(
        "/* pinned axe input */\n" + "window.axe = {};\n" * 20,
        encoding="utf-8",
    )

    def reflected_failure(**_kwargs):
        raise RuntimeError(f"browser reflected {live_secret}")

    accessibility_receipt = accessibility_gate.build_accessibility_receipt(
        base_url="https://propertyquarry.invalid",
        routes=("/sign-in",),
        browser_engines=("chromium",),
        release_probe_secret=live_secret,
        axe_core_path=axe_path,
        collect_engine_rows=reflected_failure,
    )
    serialized = json.dumps(
        {"mobile": mobile_receipt, "accessibility": accessibility_receipt},
        sort_keys=True,
    )
    assert live_secret not in serialized
    assert "[redacted-secret]" in serialized
