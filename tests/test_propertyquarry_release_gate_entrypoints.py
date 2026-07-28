from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time

import pytest

from scripts import propertyquarry_compileall_clean as compileall_clean
from scripts import propertyquarry_native_release_control_gates as native_gate
from scripts import propertyquarry_gold_status as gold_status
from scripts import propertyquarry_release_make_dispatch as make_dispatch
from scripts import propertyquarry_release_python_verify as release_python_verifier


ROOT = Path(__file__).resolve().parents[1]
_STALE_RELEASE_REQUIREMENTS = (
    "release verifier requirements input/lock is stale: "
    "jsonschema[format-nongpl]==4.26.0 is missing from the compiled "
    "requirements lock"
)
_CURRENT_RELEASE_REQUIREMENTS_ISSUES = (
    release_python_verifier.requirements_lock_issues(
        (
            ROOT
            / "config"
            / "propertyquarry_release_verifier_requirements.in"
        ).read_bytes(),
        (ROOT / "ea" / "requirements.lock").read_bytes(),
        (
            ROOT
            / "config"
            / "propertyquarry_release_verifier_requirements.lock"
        ).read_bytes(),
    )
)
assert _CURRENT_RELEASE_REQUIREMENTS_ISSUES in (
    [],
    [
        "jsonschema[format-nongpl]==4.26.0 is missing from the compiled "
        "requirements lock"
    ],
)
_RELEASE_REQUIREMENTS_STALE = bool(_CURRENT_RELEASE_REQUIREMENTS_ISSUES)
_HARD_EXIT_PYTHON_RUNTIME_OVERRIDES = (
    "PYTHONHOME",
    "PYTHONOPTIMIZE",
    "PYTHONPATH",
    "PYTHONPLATLIBDIR",
    "PYTHONUSERBASE",
)
_HARD_EXIT_POSTGRES_TOPOLOGY_OVERRIDES = (
    "BUILDKIT_HOST",
    "BUILDX_BUILDER",
    "BUILDX_CONFIG",
    "COMPOSE_BAKE",
    "COMPOSE_CONVERT_WINDOWS_PATHS",
    "COMPOSE_DISABLE_ENV_FILE",
    "COMPOSE_DOCKER_CLI_BUILD",
    "COMPOSE_ENV_FILES",
    "COMPOSE_FILE",
    "COMPOSE_PATH_SEPARATOR",
    "COMPOSE_PROJECT_NAME",
    "COMPOSE_REMOVE_ORPHANS",
    "DATABASE_URL",
    "DOCKER_API_VERSION",
    "DOCKER_CERT_PATH",
    "DOCKER_CLI_PLUGIN_EXTRA_DIRS",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_DEFAULT_PLATFORM",
    "DOCKER_HOST",
    "DOCKER_TLS",
    "DOCKER_TLS_VERIFY",
    "EA_API_SERVICE",
    "EA_DB_CONTAINER",
    "EA_DB_SERVICE",
    "EA_RUNTIME_MODE",
    "EA_SCHEDULER_SERVICE",
    "EA_SMOKE_DB",
    "EA_WORKER_SERVICE",
    "POSTGRES_DB",
    "POSTGRES_PASSWORD",
    "POSTGRES_USER",
    "PROPERTYQUARRY_API_CONTAINER_NAME",
    "PROPERTYQUARRY_API_SERVICE",
    "PROPERTYQUARRY_DB_CONTAINER_NAME",
    "PROPERTYQUARRY_DB_SERVICE",
    "PROPERTYQUARRY_SCHEDULER_CONTAINER_NAME",
    "PROPERTYQUARRY_SCHEDULER_SERVICE",
    "PROPERTYQUARRY_WORKER_CONTAINER_NAME",
    "PROPERTYQUARRY_WORKER_SERVICE",
)
_HARD_EXIT_PROXY_OVERRIDES = (
    "ALL_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "http_proxy",
    "https_proxy",
)
_HARD_EXIT_FORBIDDEN_ENVIRONMENT = frozenset(
    (
        "EA_HOST_PORT",
        "EA_TEST_FILES",
        "EA_TEST_PYTHON",
        "PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED",
        "PYTEST_PYTHON_BIN",
        "PYTHON_BIN",
        *_HARD_EXIT_POSTGRES_TOPOLOGY_OVERRIDES,
        *_HARD_EXIT_PROXY_OVERRIDES,
        *_HARD_EXIT_PYTHON_RUNTIME_OVERRIDES,
    )
)


def _bash_function(source: str, name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n",
        source,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"missing bash function {name}"
    return match.group(0)


def _hard_exit_environment(**overrides: str) -> dict[str, str]:
    environment = dict(os.environ)
    for variable in _HARD_EXIT_FORBIDDEN_ENVIRONMENT:
        environment.pop(variable, None)
    environment.update(overrides)
    return environment


def _make_target(makefile: str, name: str) -> str:
    marker = f"\n{name}:"
    start = makefile.index(marker) + 1
    body_start = makefile.index("\n", start) + 1
    next_target = re.search(
        r"^[A-Za-z0-9_.-]+:",
        makefile[body_start:],
        re.MULTILINE,
    )
    end = body_start + next_target.start() if next_target else len(makefile)
    return makefile[start:end]


def _reachable_make_targets(
    makefile: str,
    roots: tuple[str, ...],
) -> dict[str, str]:
    pending = list(roots)
    reachable: dict[str, str] = {}
    target_pattern = re.compile(r"[A-Za-z0-9_.-]+")
    submake_pattern = re.compile(r"\t\$\(MAKE\) ([A-Za-z0-9_.-]+)")

    while pending:
        target = pending.pop()
        if target in reachable:
            continue
        section = _make_target(makefile, target)
        lines = section.rstrip().splitlines()
        declared_target, separator, prerequisites = lines[0].partition(":")
        assert separator == ":" and declared_target == target
        for prerequisite in prerequisites.split():
            assert target_pattern.fullmatch(prerequisite), (
                f"{target} has a dynamic Make prerequisite that this "
                f"release-safety audit cannot follow: {prerequisite}"
            )
            pending.append(prerequisite)
        for line in lines[1:]:
            if "$(MAKE)" not in line:
                continue
            match = submake_pattern.fullmatch(line)
            assert match is not None, (
                f"{target} has a dynamic submake that this release-safety "
                f"audit cannot follow: {line}"
            )
            pending.append(match.group(1))
        reachable[target] = section

    return reachable


def _native_junit(
    test_count: int,
    *,
    skipped: int = 0,
    failures: int = 0,
    errors: int = 0,
) -> str:
    disposition_counts = {
        "skipped": skipped,
        "failure": failures,
        "error": errors,
    }
    dispositions = [
        disposition
        for disposition, count in disposition_counts.items()
        for _ in range(count)
    ]
    cases = []
    for index in range(test_count):
        outcome = (
            f"<{dispositions[index]}/>"
            if index < len(dispositions)
            else ""
        )
        cases.append(
            f'<testcase classname="native.release" name="case_{index}">'
            f"{outcome}</testcase>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuites><testsuite name="pytest" errors="{errors}" '
        f'failures="{failures}" skipped="{skipped}" tests="{test_count}">'
        f'{"".join(cases)}</testsuite></testsuites>'
    )


def test_compileall_gate_keeps_bytecode_outside_source_tree(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    package = source_root / "package"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    (package / "module.py").write_text(
        "def value() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    previous_prefix = sys.pycache_prefix

    assert compileall_clean.compile_paths((source_root,))

    assert sys.pycache_prefix == previous_prefix
    assert not list(source_root.rglob("__pycache__"))
    assert not list(source_root.rglob("*.pyc"))


def test_native_release_gate_requires_both_explicit_pinned_inputs() -> None:
    with pytest.raises(
        native_gate.NativeGateError,
        match="PROPERTYQUARRY_NATIVE_GO is required",
    ):
        native_gate.validate_toolchain_inputs({})


def test_native_release_gate_rejects_any_skipped_test(tmp_path: Path) -> None:
    junit = tmp_path / "native.xml"
    junit.write_text(
        _native_junit(native_gate.EXPECTED_NATIVE_TESTS, skipped=1),
        encoding="utf-8",
    )

    with pytest.raises(native_gate.NativeGateError, match=r"skipped=1"):
        native_gate.validate_junit_result(junit)


def test_native_release_gate_accepts_only_a_complete_clean_junit_result(
    tmp_path: Path,
) -> None:
    junit = tmp_path / "native.xml"
    junit.write_text(
        _native_junit(native_gate.EXPECTED_NATIVE_TESTS),
        encoding="utf-8",
    )

    assert (
        native_gate.validate_junit_result(junit)
        == native_gate.EXPECTED_NATIVE_TESTS
    )


def test_native_release_gate_pinned_count_matches_actual_collection() -> None:
    result = subprocess.run(
        [
            str(
                ROOT
                / ".propertyquarry_release_tools"
                / "release-venv"
                / "bin"
                / "python"
            ),
            "-B",
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            str(native_gate.NATIVE_TEST.relative_to(ROOT)),
        ],
        cwd=ROOT,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "LANG": "C",
            "LC_ALL": "C",
            "TZ": "UTC",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    node_prefix = f"{native_gate.NATIVE_TEST.relative_to(ROOT).as_posix()}::"
    collected = [
        line for line in result.stdout.splitlines() if line.startswith(node_prefix)
    ]
    assert result.returncode == 0, result.stderr
    assert len(collected) == native_gate.EXPECTED_NATIVE_TESTS


@pytest.mark.parametrize(
    ("fixture_passes", "expected_status"),
    ((True, 0), (False, 1)),
)
def test_native_release_gate_keeps_success_and_failure_state_outside_repository(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture_passes: bool,
    expected_status: int,
) -> None:
    repository = tmp_path / "repository"
    test_directory = repository / "tests"
    test_directory.mkdir(parents=True)
    native_test = test_directory / "test_native_fixture.py"
    native_test.write_text(
        (
            "import os\n"
            "from pathlib import Path\n\n"
            "def test_native_fixture():\n"
            "    private_root = Path(os.environ['TMPDIR'])\n"
                "    assert private_root.is_dir()\n"
                "    assert private_root.parent == Path('/tmp')\n"
                "    assert private_root.name.startswith("
                f"{native_gate.NATIVE_TEMP_PREFIX!r})\n"
            "    assert 'PROPERTYQUARRY_GATE_RECEIPT_DIR' not in os.environ\n"
            f"    assert {fixture_passes!r}\n"
        ),
        encoding="utf-8",
    )
    cache = repository / ".pytest_cache" / "v" / "cache"
    cache.mkdir(parents=True)
    (cache / "nodeids").write_text('["sentinel-node"]\n', encoding="utf-8")
    (cache / "lastfailed").write_text(
        '{"sentinel-node": true}\n',
        encoding="utf-8",
    )
    receipt = repository / "_completion" / "native" / "receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"status":"sentinel"}\n', encoding="utf-8")

    def snapshot() -> dict[
        str,
        tuple[int, int, int, int, int, int, int, bytes],
    ]:
        result: dict[
            str,
            tuple[int, int, int, int, int, int, int, bytes],
        ] = {}
        paths = (repository, *sorted(repository.rglob("*")))
        for path in paths:
            metadata = path.lstat()
            if path.is_symlink():
                payload = os.readlink(path).encode("utf-8")
            elif path.is_file():
                payload = path.read_bytes()
            else:
                payload = b""
            result[str(path.relative_to(repository))] = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_nlink,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
                payload,
            )
        return result

    identity = (0, 0, 0, 0, 0, 0, 0, 0)
    toolchain = native_gate.ValidatedToolchain(
        go_binary=native_gate.ValidatedInput(Path("/trusted/go"), identity),
        archive=native_gate.ValidatedInput(
            Path("/trusted/go.tar.gz"),
            identity,
        ),
    )
    monkeypatch.setattr(native_gate, "ROOT", repository)
    monkeypatch.setattr(native_gate, "NATIVE_TEST", native_test)
    monkeypatch.setattr(native_gate, "EXPECTED_NATIVE_TESTS", 1)
    monkeypatch.setattr(
        native_gate,
        "validate_toolchain_inputs",
        lambda _environ: toolchain,
    )
    monkeypatch.setattr(
        native_gate,
        "_revalidate_input",
        lambda *_args, **_kwargs: None,
    )
    before = snapshot()

    status = native_gate.run_native_gate(
        {"PROPERTYQUARRY_GATE_RECEIPT_DIR": str(receipt.parent)}
    )

    assert status == expected_status
    assert snapshot() == before
    assert not list(repository.rglob("__pycache__"))
    assert not list(repository.rglob("*.pyc"))


def test_native_release_gate_child_environment_is_positive_allowlist(
    tmp_path: Path,
) -> None:
    identity = (0, 0, 0, 0, 0, 0, 0, 0)
    toolchain = native_gate.ValidatedToolchain(
        go_binary=native_gate.ValidatedInput(Path("/trusted/go"), identity),
        archive=native_gate.ValidatedInput(Path("/trusted/go.tar.gz"), identity),
    )

    environment = native_gate._native_test_environment(toolchain, tmp_path)

    assert environment["PROPERTYQUARRY_NATIVE_GO"] == "/trusted/go"
    assert environment["PROPERTYQUARRY_GO_ARCHIVE"] == "/trusted/go.tar.gz"
    assert environment["PATH"] == "/usr/bin:/bin"
    assert environment["GOPROXY"] == "off"
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert environment["TMPDIR"] == str(tmp_path)
    assert "MAKEFLAGS" not in environment
    assert "PROPERTYQUARRY_GATE_RECEIPT_DIR" not in environment
    assert "PYTEST_ADDOPTS" not in environment
    assert "LD_PRELOAD" not in environment


@pytest.mark.parametrize(
    "test_count",
    [
        native_gate.EXPECTED_NATIVE_TESTS - 1,
        native_gate.EXPECTED_NATIVE_TESTS + 1,
    ],
)
def test_native_release_gate_rejects_collection_count_drift(
    tmp_path: Path,
    test_count: int,
) -> None:
    junit = tmp_path / "native.xml"
    junit.write_text(
        _native_junit(test_count),
        encoding="utf-8",
    )

    with pytest.raises(
        native_gate.NativeGateError,
        match=rf"expected_tests={native_gate.EXPECTED_NATIVE_TESTS}, tests={test_count}",
    ):
        native_gate.validate_junit_result(junit)


def test_native_release_gate_rejects_header_only_junit_claim(
    tmp_path: Path,
) -> None:
    junit = tmp_path / "native.xml"
    junit.write_text(
        (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<testsuites><testsuite name="pytest" errors="0" failures="0" '
            f'skipped="0" tests="{native_gate.EXPECTED_NATIVE_TESTS}"/>'
            "</testsuites>"
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        native_gate.NativeGateError,
        match="testcase evidence does not match declared counts",
    ):
        native_gate.validate_junit_result(junit)


def test_native_release_gate_rejects_junit_outcome_summary_mismatch(
    tmp_path: Path,
) -> None:
    junit = tmp_path / "native.xml"
    payload = _native_junit(native_gate.EXPECTED_NATIVE_TESTS)
    junit.write_text(
        payload.replace('failures="0"', 'failures="1"', 1),
        encoding="utf-8",
    )

    with pytest.raises(
        native_gate.NativeGateError,
        match="testcase evidence does not match declared counts",
    ):
        native_gate.validate_junit_result(junit)


def test_every_release_entrypoint_runs_complete_protocol_and_native_gates() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    release_script = (ROOT / "scripts" / "property_release_gates.sh").read_text(
        encoding="utf-8"
    )
    release_preflight = _make_target(makefile, "propertyquarry-release-preflight")
    release_gate = _make_target(makefile, "property-release-gates")
    protocol = _make_target(makefile, "propertyquarry-release-protocol-contracts")
    native = _make_target(makefile, "propertyquarry-native-release-control-gates")
    protocol_contracts = (
        "tests/test_property_release_protocol_contracts.py",
        "tests/test_property_deploy_handoff_adversarial.py",
        "tests/test_property_deploy_operator_contracts.py",
        "tests/test_propertyquarry_release_gate_entrypoints.py",
    )
    focused_publication_contracts = (
        "tests/test_propertyquarry_release_authority_receipts.py",
        "tests/test_propertyquarry_release_request_signature.py",
        "tests/test_propertyquarry_flagship_operations_evidence.py",
        "tests/test_propertyquarry_release_evidence.py",
        "tests/test_propertyquarry_launch_gold_validation.py",
        "tests/test_propertyquarry_launch_authority.py",
    )

    assert release_script.startswith("#!/bin/bash -p\nset -euo pipefail\n")
    phony_line = next(
        line for line in makefile.splitlines() if line.startswith(".PHONY:")
    )
    assert "propertyquarry-native-release-control-gates" in phony_line
    protocol_call = "$(MAKE) propertyquarry-release-protocol-contracts"
    release_call = "$(MAKE) property-release-gates"
    assert protocol_call not in release_preflight
    assert release_preflight.count(release_call) == 1
    assert "$(MAKE) propertyquarry-native-release-control-gates" not in release_preflight
    assert release_gate.count("/bin/bash -p scripts/property_release_gates.sh") == 1
    for contract in protocol_contracts:
        assert contract in protocol
        assert release_script.count(contract) == 1
    for contract in focused_publication_contracts:
        assert contract not in protocol
        assert release_script.count(contract) == 1
    assert "$(MAKE) propertyquarry-native-release-control-gates" not in protocol
    assert "scripts/propertyquarry_native_release_control_gates.py" not in release_gate
    assert "scripts/propertyquarry_native_release_control_gates.py" not in release_preflight
    assert (
        "./scripts/propertyquarry_release_python.sh "
        "scripts/propertyquarry_native_release_control_gates.py"
        in native
    )
    assert (
        'PYTHON_BIN="${EA_ROOT}/scripts/propertyquarry_release_python.sh"'
        in release_script
    )
    assert "readonly PYTHON_BIN" in release_script
    native_script_call = (
        'PYTHONPATH=ea "${PYTHON_BIN}" '
        "scripts/propertyquarry_native_release_control_gates.py"
    )
    assert release_script.count(native_script_call) == 1
    pytest_calls = [
        match.start()
        for match in re.finditer(r'"\$\{PYTHON_BIN\}" -m pytest -q', release_script)
    ]
    native_call = release_script.index(native_script_call)
    assert pytest_calls
    assert max(
        release_script.index(contract)
        for contract in (*protocol_contracts, *focused_publication_contracts)
    ) < native_call
    assert max(pytest_calls) < native_call
    visual_block = release_script.index(
        'if [[ -n "${PROPERTYQUARRY_VISUAL_WATCH_URL:-}" ]]; then'
    )
    visual_end = release_script.index("\nfi\n", visual_block) + len("\nfi\n")
    assert release_script.rindex("scripts/propertyquarry_visual_watch.py") < visual_end
    assert visual_end < native_call
    assert native_call < release_script.index("scripts/propertyquarry_gold_status.py")
    assert "scripts/propertyquarry_native_release_control_gates.py" in _make_target(
        makefile, "operator-help"
    )
    assert "scripts/propertyquarry_native_release_control_gates.py" in (
        ROOT / "scripts" / "smoke_help.sh"
    ).read_text(encoding="utf-8")


def test_authenticated_protocol_gate_cannot_skip_jsonschema_runtime() -> None:
    contracts = (
        ROOT / "tests" / "test_property_release_protocol_contracts.py"
    ).read_text(encoding="utf-8")

    assert 'pytest.importorskip("jsonschema")' not in contracts
    assert (
        "from jsonschema import Draft202012Validator, FormatChecker, ValidationError"
        in contracts
    )
    assert "format_checker=FormatChecker()" in contracts
    assert "https://authority.invalid/%ZZ" in contracts
    assert "https://[:::]/v2" in contracts


def test_release_python_runner_rejects_interpreter_overrides() -> None:
    result = subprocess.run(
        [
            str(ROOT / "scripts" / "propertyquarry_release_python.sh"),
            "-c",
            'print("must-not-run")',
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHON_BIN": "/bin/true",
            "PYTEST_PYTHON_BIN": "/bin/true",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "error: release interpreter override forbidden\n"


def test_release_authority_uses_fixed_privileged_make_boundaries() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    smoke_help = (ROOT / "scripts" / "smoke_help.sh").read_text(encoding="utf-8")

    assert "override SHELL := /bin/bash" in makefile
    assert "override .SHELLFLAGS := -p -o pipefail -c" in makefile
    assert "override MAKE := /usr/bin/make" in makefile
    assert "override MAKE_COMMAND := /usr/bin/make" in makefile
    assert "/bin/bash -p scripts/property_release_gates.sh" in makefile
    assert "/bin/bash -p scripts/verify_release_assets.sh" in makefile
    assert "verify-release-assets-authenticated" in makefile
    assert "verify-flagship-release-readiness-authenticated" in makefile
    assert "verify-generated-release-artifacts-clean-authenticated" in makefile
    assert (
        "/bin/bash -p scripts/smoke_help.sh --authenticated"
        in _make_target(makefile, "smoke-help-authenticated")
    )
    assert (
        'PYTHON_COMMAND=("${EA_ROOT}/scripts/propertyquarry_release_python.sh")'
        in smoke_help
    )
    release_preflight = _make_target(makefile, "release-preflight")
    assert "$(MAKE) operator-help-authenticated" in release_preflight
    assert "$(MAKE) release-smoke-authenticated" in release_preflight
    authenticated_operator_help = _make_target(
        makefile, "operator-help-authenticated"
    )
    assert "./scripts/propertyquarry_release_python.sh $$s --help" in (
        authenticated_operator_help
    )
    assert "/bin/bash -p $$s --help" in authenticated_operator_help


def test_release_python_bootstrap_uses_private_canonical_no_follow_lane() -> None:
    bootstrap = (
        ROOT / "scripts" / "bootstrap_propertyquarry_release_python.sh"
    ).read_text(encoding="utf-8")

    assert '[[ "${ROOT}" == "/docker/property" ]]' in bootstrap
    assert (
        'VENV="${ROOT}/.propertyquarry_release_tools/release-venv"'
        in bootstrap
    )
    assert "state/propertyquarry_release_tools" not in bootstrap
    assert bootstrap.count("/usr/bin/env -i") == 4
    parity_call = (
        '"${VERIFY_SCRIPT}" --check-requirements-parity'
    )
    assert parity_call in bootstrap
    assert bootstrap.index(parity_call) < bootstrap.index(
        "acquire_bootstrap_lock\n"
    )
    assert bootstrap.index(parity_call) < bootstrap.index(
        '"${SYSTEM_PYTHON}" -I -m venv'
    )
    assert bootstrap.index(parity_call) < bootstrap.index(
        '"${VENV}/bin/python" -m pip install'
    )
    assert "PIP_INDEX_URL=https://pypi.org/simple" in bootstrap
    assert "--require-hashes" in bootstrap
    assert "--only-binary=:all:" in bootstrap
    assert '[[ -d "${parent}" && ! -L "${parent}" ]]' in bootstrap
    assert '"/docker/property/.propertyquarry_release_tools/release-venv")' in (
        bootstrap
    )
    assert 'exec {BOOTSTRAP_LOCK_FD}<"${ROOT}"' in bootstrap
    assert '/usr/bin/flock --exclusive "${BOOTSTRAP_LOCK_FD}"' in bootstrap
    assert bootstrap.index("acquire_bootstrap_lock\n") < bootstrap.index(
        'if [[ ! -e "${parent}" && ! -L "${parent}" ]]'
    )
    assert "propertyquarry_release_python_create_root.py" in bootstrap
    assert "created_venv_identity=" in bootstrap
    assert '"${current_venv_identity}" != "${created_venv_identity}"' in bootstrap
    assert '/usr/bin/rm -rf -- "${VENV}"' not in bootstrap
    assert "release-venv.incomplete.XXXXXXXX" in bootstrap
    assert (
        "existing release verifier is invalid; automatic replacement is forbidden"
        in bootstrap
    )


def test_release_python_bootstrap_directory_lock_serializes_creators(
    tmp_path: Path,
) -> None:
    source = (
        ROOT / "scripts" / "bootstrap_propertyquarry_release_python.sh"
    ).read_text(encoding="utf-8")
    fail_function = _bash_function(source, "fail")
    lock_function = _bash_function(source, "acquire_bootstrap_lock")
    holder = subprocess.Popen(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{fail_function}\n"
                f"{lock_function}\n"
                "ROOT=\"$1\"\n"
                "BOOTSTRAP_LOCK_FD=''\n"
                "acquire_bootstrap_lock\n"
                "printf 'locked\\n'\n"
                "read -r _\n"
            ),
            "bash",
            str(tmp_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline() == "locked\n"
        flock_command = [
            "/usr/bin/flock",
            "--nonblock",
            "--exclusive",
            str(tmp_path),
            "/bin/true",
        ]
        contender = subprocess.run(
            flock_command,
            check=False,
            capture_output=True,
            text=True,
        )
        assert contender.returncode == 1
        assert contender.stdout == ""
        assert contender.stderr == ""
    finally:
        assert holder.stdin is not None
        holder.stdin.write("release\n")
        holder.stdin.flush()
        stdout, stderr = holder.communicate(timeout=5)
        assert holder.returncode == 0, stderr
        assert stdout == ""

    after_release = subprocess.run(
        flock_command,
        check=False,
        capture_output=True,
        text=True,
    )
    assert after_release.returncode == 0
    assert after_release.stdout == ""
    assert after_release.stderr == ""


def test_release_python_bootstrap_lock_serializes_missing_parent_creation(
    tmp_path: Path,
) -> None:
    source = (
        ROOT / "scripts" / "bootstrap_propertyquarry_release_python.sh"
    ).read_text(encoding="utf-8")
    fail_function = _bash_function(source, "fail")
    lock_function = _bash_function(source, "acquire_bootstrap_lock")
    command = (
        f"{fail_function}\n"
        f"{lock_function}\n"
        "ROOT=\"$1\"\n"
        "parent=\"${ROOT}/tools\"\n"
        "BOOTSTRAP_LOCK_FD=''\n"
        "acquire_bootstrap_lock\n"
        "if [[ ! -e \"${parent}\" && ! -L \"${parent}\" ]]; then\n"
        "  /usr/bin/sleep 0.05\n"
        "  /usr/bin/mkdir -m 700 -- \"${parent}\"\n"
        "fi\n"
        "[[ -d \"${parent}\" && ! -L \"${parent}\" ]]\n"
    )
    processes = [
        subprocess.Popen(
            [
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-eu",
                "-c",
                command,
                "bash",
                str(tmp_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]

    for process in processes:
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        assert stdout == ""
        assert stderr == ""
    assert (tmp_path / "tools").is_dir()


@pytest.mark.parametrize("status", (129, 130, 143))
def test_release_python_bootstrap_signal_cleanup_cannot_resume(
    status: int,
) -> None:
    source = (
        ROOT / "scripts" / "bootstrap_propertyquarry_release_python.sh"
    ).read_text(encoding="utf-8")
    terminate_function = _bash_function(source, "terminate_from_signal")
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{terminate_function}\n"
                "cleanup() { printf 'cleanup\\n'; }\n"
                f"terminate_from_signal {status}\n"
                "printf 'resumed\\n'\n"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == status
    assert result.stdout == "cleanup\n"
    assert result.stderr == ""


@pytest.mark.parametrize("status", (129, 130, 143))
def test_release_python_bootstrap_signal_status_survives_cleanup_failure(
    status: int,
) -> None:
    source = (
        ROOT / "scripts" / "bootstrap_propertyquarry_release_python.sh"
    ).read_text(encoding="utf-8")
    terminate_function = _bash_function(source, "terminate_from_signal")
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{terminate_function}\n"
                "cleanup() { return 7; }\n"
                f"terminate_from_signal {status}\n"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == status
    assert result.stdout == ""
    assert result.stderr == ""


def test_release_python_bootstrap_repeated_signal_cannot_interrupt_cleanup(
    tmp_path: Path,
) -> None:
    source = (
        ROOT / "scripts" / "bootstrap_propertyquarry_release_python.sh"
    ).read_text(encoding="utf-8")
    terminate_function = _bash_function(source, "terminate_from_signal")
    marker = tmp_path / "cleanup-started"
    process = subprocess.Popen(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{terminate_function}\n"
                "MARKER=\"$1\"\n"
                "cleanup() {\n"
                "  printf 'entered\\n'\n"
                "  : > \"${MARKER}\"\n"
                "  /usr/bin/sleep 0.25\n"
                "  printf 'finished\\n'\n"
                "}\n"
                "terminate_from_signal 143\n"
            ),
            "bash",
            str(marker),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 3
    while not marker.exists() and process.poll() is None:
        if time.monotonic() >= deadline:
            process.kill()
            raise AssertionError("bootstrap cleanup did not start")
        time.sleep(0.01)
    os.killpg(process.pid, signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode == 143
    assert stdout == "entered\nfinished\n"
    assert stderr == ""


def test_release_python_bootstrap_exit_cleanup_ignores_nested_signal(
    tmp_path: Path,
) -> None:
    source = (
        ROOT / "scripts" / "bootstrap_propertyquarry_release_python.sh"
    ).read_text(encoding="utf-8")
    exit_function = _bash_function(source, "cleanup_on_exit")
    marker = tmp_path / "cleanup-started"
    process = subprocess.Popen(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{exit_function}\n"
                "MARKER=\"$1\"\n"
                "cleanup() {\n"
                "  printf 'entered\\n'\n"
                "  : > \"${MARKER}\"\n"
                "  /usr/bin/sleep 0.25\n"
                "  printf 'finished\\n'\n"
                "}\n"
                "trap 'cleanup_on_exit \"$?\"' EXIT\n"
                "exit 17\n"
            ),
            "bash",
            str(marker),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 3
    while not marker.exists() and process.poll() is None:
        if time.monotonic() >= deadline:
            process.kill()
            raise AssertionError("bootstrap EXIT cleanup did not start")
        time.sleep(0.01)
    os.killpg(process.pid, signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode == 17
    assert stdout == "entered\nfinished\n"
    assert stderr == ""


def test_release_python_bootstrap_cleanup_refuses_unidentified_partial_tree(
    tmp_path: Path,
) -> None:
    source = (
        ROOT / "scripts" / "bootstrap_propertyquarry_release_python.sh"
    ).read_text(encoding="utf-8")
    canonical = tmp_path / "release-venv"
    cleanup_function = _bash_function(source, "cleanup").replace(
        '"/docker/property/.propertyquarry_release_tools/release-venv")',
        f'"{canonical}")',
    )
    canonical.mkdir(mode=0o700)
    (canonical / "partial-marker").write_text("preserve\n", encoding="utf-8")
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{cleanup_function}\n"
                "parent=\"$1\"\n"
                "VENV=\"$2\"\n"
                "created=1\n"
                "created_venv_identity=''\n"
                "complete=0\n"
                "if cleanup; then exit 99; else exit 0; fi\n"
            ),
            "bash",
            str(tmp_path),
            str(canonical),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert (canonical / "partial-marker").read_text(encoding="utf-8") == (
        "preserve\n"
    )
    assert list(tmp_path.glob("release-venv.incomplete.*")) == []
    assert result.stderr == (
        "error: refusing cleanup without a created environment identity\n"
    )


def test_release_python_bootstrap_cleanup_quarantines_identified_partial_tree(
    tmp_path: Path,
) -> None:
    source = (
        ROOT / "scripts" / "bootstrap_propertyquarry_release_python.sh"
    ).read_text(encoding="utf-8")
    canonical = tmp_path / "release-venv"
    cleanup_function = _bash_function(source, "cleanup").replace(
        '"/docker/property/.propertyquarry_release_tools/release-venv")',
        f'"{canonical}")',
    )
    canonical.mkdir(mode=0o700)
    (canonical / "partial-marker").write_text("preserve\n", encoding="utf-8")
    identity = f"{canonical.stat().st_dev}:{canonical.stat().st_ino}"
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{cleanup_function}\n"
                "parent=\"$1\"\n"
                "VENV=\"$2\"\n"
                "created=1\n"
                "created_venv_identity=\"$3\"\n"
                "complete=0\n"
                "cleanup\n"
            ),
            "bash",
            str(tmp_path),
            str(canonical),
            identity,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert not canonical.exists()
    quarantines = list(tmp_path.glob("release-venv.incomplete.*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "partial-marker").read_text(
        encoding="utf-8"
    ) == "preserve\n"
    assert result.stderr == (
        f"warning: incomplete release verifier retained at {quarantines[0]}\n"
    )


def test_release_python_bootstrap_cleanup_refuses_replaced_created_tree(
    tmp_path: Path,
) -> None:
    source = (
        ROOT / "scripts" / "bootstrap_propertyquarry_release_python.sh"
    ).read_text(encoding="utf-8")
    canonical = tmp_path / "release-venv"
    original = tmp_path / "original"
    cleanup_function = _bash_function(source, "cleanup").replace(
        '"/docker/property/.propertyquarry_release_tools/release-venv")',
        f'"{canonical}")',
    )
    canonical.mkdir(mode=0o700)
    expected_identity = f"{canonical.stat().st_dev}:{canonical.stat().st_ino}"
    canonical.rename(original)
    canonical.mkdir(mode=0o700)
    (canonical / "replacement-marker").write_text(
        "preserve\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{cleanup_function}\n"
                "parent=\"$1\"\n"
                "VENV=\"$2\"\n"
                "created=1\n"
                "created_venv_identity=\"$3\"\n"
                "complete=0\n"
                "if cleanup; then exit 99; else exit 0; fi\n"
            ),
            "bash",
            str(tmp_path),
            str(canonical),
            expected_identity,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == (
        "error: refusing cleanup of replaced release verifier environment\n"
    )
    assert (canonical / "replacement-marker").read_text(
        encoding="utf-8"
    ) == "preserve\n"
    assert original.is_dir()
    assert list(tmp_path.glob("release-venv.incomplete.*")) == []


@pytest.mark.parametrize("fsync_call", (1, 2))
def test_release_python_bootstrap_quarantines_root_after_fsync_failure(
    tmp_path: Path,
    fsync_call: int,
) -> None:
    source = (
        ROOT / "scripts" / "bootstrap_propertyquarry_release_python.sh"
    ).read_text(encoding="utf-8")
    canonical = tmp_path / "release-venv"
    cleanup_function = _bash_function(source, "cleanup").replace(
        '"/docker/property/.propertyquarry_release_tools/release-venv")',
        f'"{canonical}")',
    )
    python_code = f"""
import errno
from pathlib import Path
import sys

sys.path.insert(0, {str(ROOT)!r})
from scripts import propertyquarry_release_python_create_root as creator

target = Path(sys.argv[1])
fail_call = int(sys.argv[2])
sys.argv = [sys.argv[0]]
creator.TARGET = target
original_fsync = creator.os.fsync
fsync_calls = 0

def failed_fsync(descriptor):
    global fsync_calls
    fsync_calls += 1
    if fsync_calls == fail_call:
        raise OSError(errno.EIO, "forced fsync failure")
    return original_fsync(descriptor)

creator.os.fsync = failed_fsync
raise SystemExit(creator._entrypoint())
"""
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{cleanup_function}\n"
                "parent=\"$1\"\n"
                "VENV=\"$2\"\n"
                "PYTHON_CODE=\"$3\"\n"
                "FAIL_CALL=\"$4\"\n"
                "created=0\n"
                "created_venv_identity=''\n"
                "complete=0\n"
                "set +e\n"
                "created_venv_identity=\"$(\n"
                "  /usr/bin/python3.12 -I -S -B -c \"${PYTHON_CODE}\" "
                "\"${VENV}\" \"${FAIL_CALL}\"\n"
                ")\"\n"
                "helper_status=\"$?\"\n"
                "set -e\n"
                "[[ \"${helper_status}\" -eq 2 ]]\n"
                "[[ \"${created_venv_identity}\" =~ ^[0-9]+:[0-9]+$ ]]\n"
                "cleanup\n"
            ),
            "bash",
            str(tmp_path),
            str(canonical),
            python_code,
            str(fsync_call),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "created release verifier finalization failed" in result.stderr
    assert "forced fsync failure" in result.stderr
    assert "Traceback" not in result.stderr
    assert not canonical.exists()
    quarantines = list(tmp_path.glob("release-venv.incomplete.*"))
    assert len(quarantines) == 1
    assert quarantines[0].is_dir()
    assert (
        f"warning: incomplete release verifier retained at {quarantines[0]}\n"
        in result.stderr
    )


@pytest.mark.parametrize(
    ("sent_signal", "expected_status"),
    (
        (signal.SIGHUP, 129),
        (signal.SIGINT, 130),
        (signal.SIGTERM, 143),
    ),
)
def test_release_python_bootstrap_full_helper_signal_path_quarantines_identity(
    tmp_path: Path,
    sent_signal: signal.Signals,
    expected_status: int,
) -> None:
    source = (
        ROOT / "scripts" / "bootstrap_propertyquarry_release_python.sh"
    ).read_text(encoding="utf-8")
    canonical = tmp_path / "release-venv"
    marker = tmp_path / "helper-opened"
    cleanup_function = _bash_function(source, "cleanup").replace(
        '"/docker/property/.propertyquarry_release_tools/release-venv")',
        f'"{canonical}")',
    )
    terminate_function = _bash_function(source, "terminate_from_signal")
    python_code = f"""
from pathlib import Path
import sys
import time

sys.path.insert(0, {str(ROOT)!r})
from scripts import propertyquarry_release_python_create_root as creator

target = Path(sys.argv[1])
marker = Path(sys.argv[2])
sys.argv = [sys.argv[0]]
creator.TARGET = target
original_open = creator.os.open
delayed = False

def delayed_open(path, flags, *args, **kwargs):
    global delayed
    if not delayed:
        delayed = True
        marker.write_text("ready\\n", encoding="utf-8")
        time.sleep(0.25)
    return original_open(path, flags, *args, **kwargs)

creator.os.open = delayed_open
raise SystemExit(creator._entrypoint())
"""
    process = subprocess.Popen(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{cleanup_function}\n"
                f"{terminate_function}\n"
                "parent=\"$1\"\n"
                "VENV=\"$2\"\n"
                "MARKER=\"$3\"\n"
                "PYTHON_CODE=\"$4\"\n"
                "created=0\n"
                "created_venv_identity=''\n"
                "complete=0\n"
                "trap 'terminate_from_signal 129' HUP\n"
                "trap 'terminate_from_signal 130' INT\n"
                "trap 'terminate_from_signal 143' TERM\n"
                "created_venv_identity=\"$(\n"
                "  /usr/bin/python3.12 -I -S -B -c \"${PYTHON_CODE}\" "
                "\"${VENV}\" \"${MARKER}\"\n"
                ")\"\n"
                "created=1\n"
                "printf 'resumed\\n'\n"
            ),
            "bash",
            str(tmp_path),
            str(canonical),
            str(marker),
            python_code,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 3
    while not marker.exists() and process.poll() is None:
        if time.monotonic() >= deadline:
            process.kill()
            raise AssertionError("root creator did not enter the identity window")
        time.sleep(0.01)
    os.killpg(process.pid, sent_signal)
    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode == expected_status
    assert stdout == ""
    assert not canonical.exists()
    quarantines = list(tmp_path.glob("release-venv.incomplete.*"))
    assert len(quarantines) == 1
    assert quarantines[0].is_dir()
    assert stderr == (
        f"warning: incomplete release verifier retained at {quarantines[0]}\n"
    )


@pytest.mark.parametrize(
    ("script", "arguments", "expected_returncode", "expected_output"),
    (
        (
            "propertyquarry_release_python.sh",
            ("-c", "raise SystemExit(23)"),
            2 if _RELEASE_REQUIREMENTS_STALE else 23,
            _STALE_RELEASE_REQUIREMENTS if _RELEASE_REQUIREMENTS_STALE else "",
        ),
        (
            "bootstrap_propertyquarry_release_python.sh",
            ("unexpected",),
            2,
            "accepts no arguments",
        ),
        (
            "property_release_gates.sh",
            ("--help",),
            0,
            "Runs the focused PropertyQuarry release bundle",
        ),
        (
            "propertyquarry_live_release_gates.sh",
            (),
            2,
            "set PROPERTYQUARRY_LIVE_MOBILE_BASE_URL",
        ),
        (
            "verify_release_assets.sh",
            ("--help",),
            0,
            "authenticated release interpreter",
        ),
    ),
)
def test_release_shell_entrypoints_ignore_bash_env(
    tmp_path: Path,
    script: str,
    arguments: tuple[str, ...],
    expected_returncode: int,
    expected_output: str,
) -> None:
    hostile_bash_env = tmp_path / "hostile-bash-env.sh"
    hostile_bash_env.write_text("exit 0\n", encoding="utf-8")
    result = subprocess.run(
        [str(ROOT / "scripts" / script), *arguments],
        cwd=ROOT,
        env={
            "BASH_ENV": str(hostile_bash_env),
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == expected_returncode
    assert expected_output in result.stdout + result.stderr


def test_release_python_runner_sanitizes_or_reports_known_stale_lock() -> None:
    environment = {
        **os.environ,
        "PROPERTYQUARRY_RELEASE_DISPATCH": "caller-must-not-propagate",
        "PYTHONOPTIMIZE": "2",
        "PYTEST_ADDOPTS": "--collect-only",
        "PYTEST_PLUGINS": "hostile_plugin",
    }
    environment.pop("PYTHON_BIN", None)
    environment.pop("PYTEST_PYTHON_BIN", None)
    result = subprocess.run(
        [
            str(ROOT / "scripts" / "propertyquarry_release_python.sh"),
            "-c",
            (
                "import os; "
                "assert 'PYTHONOPTIMIZE' not in os.environ; "
                "assert 'PROPERTYQUARRY_RELEASE_DISPATCH' not in os.environ; "
                "assert 'PYTEST_ADDOPTS' not in os.environ; "
                "assert 'PYTEST_PLUGINS' not in os.environ; "
                'print("trusted-release-python")'
            ),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    if _RELEASE_REQUIREMENTS_STALE:
        assert result.returncode == 2
        assert result.stdout == ""
        assert _STALE_RELEASE_REQUIREMENTS in result.stderr
    else:
        assert result.returncode == 0
        assert result.stdout == "trusted-release-python\n"
        assert result.stderr == ""


def _dispatch_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in make_dispatch.FORBIDDEN_ENVIRONMENT:
        environment.pop(name, None)
    environment.pop("PROPERTYQUARRY_NATIVE_GO", None)
    environment.pop("PROPERTYQUARRY_GO_ARCHIVE", None)
    return environment


def test_native_direct_gate_stops_at_the_earliest_release_blocker() -> None:
    environment = _dispatch_environment()
    environment.update(
        {
            "MAKEFLAGS": "-n",
            "GNUMAKEFLAGS": "--ignore-errors",
            "MAKEFILES": "/dev/null",
        }
    )
    environment.pop("PROPERTYQUARRY_NATIVE_GO", None)
    environment.pop("PROPERTYQUARRY_GO_ARCHIVE", None)

    result = subprocess.run(
        [
            str(ROOT / "scripts" / "propertyquarry_release_python.sh"),
            "scripts/propertyquarry_native_release_control_gates.py",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    if _RELEASE_REQUIREMENTS_STALE:
        assert _STALE_RELEASE_REQUIREMENTS in result.stderr
    else:
        assert "PROPERTYQUARRY_NATIVE_GO is required" in result.stderr


def test_direct_authenticated_make_target_is_only_a_non_authoritative_facade() -> None:
    environment = dict(os.environ)
    environment.pop("PROPERTYQUARRY_RELEASE_DISPATCH", None)
    result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "propertyquarry-native-release-control-gates",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "authenticated release targets are internal" in result.stderr


@pytest.mark.parametrize(
    "unsafe_arguments",
    (
        ("--just-print", "propertyquarry-native-release-control-gates"),
        ("--ignore-errors", "propertyquarry-native-release-control-gates"),
        ("--eval=override MAKECMDGOALS :=", "-n", "propertyquarry-native-release-control-gates"),
        ("-f", "/dev/null", "propertyquarry-native-release-control-gates"),
        ("propertyquarry-native-release-control-gates", "EXTRA=1"),
    ),
)
def test_release_dispatch_rejects_make_options_and_extra_arguments(
    unsafe_arguments: tuple[str, ...],
) -> None:
    result = subprocess.run(
        [
            str(ROOT / "scripts" / "propertyquarry_release_python.sh"),
            "scripts/propertyquarry_release_make_dispatch.py",
            *unsafe_arguments,
        ],
        cwd=ROOT,
        env=_dispatch_environment(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    if _RELEASE_REQUIREMENTS_STALE:
        assert _STALE_RELEASE_REQUIREMENTS in result.stderr
    else:
        assert "usage: Usage:" not in result.stderr


def test_release_dispatch_rejects_unverified_python_entrypoint() -> None:
    result = subprocess.run(
        [
            "/usr/bin/python3.12",
            "-I",
            "-S",
            "-B",
            str(ROOT / "scripts" / "propertyquarry_release_make_dispatch.py"),
            "propertyquarry-native-release-control-gates",
        ],
        cwd=ROOT,
        env=_dispatch_environment(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "requires the hash-locked release interpreter" in result.stderr


@pytest.mark.parametrize("variable", sorted(make_dispatch.FORBIDDEN_ENVIRONMENT))
def test_release_dispatch_rejects_caller_control_environment(variable: str) -> None:
    environment = _dispatch_environment()
    environment[variable] = ""
    result = subprocess.run(
        [
            str(ROOT / "scripts" / "propertyquarry_release_python.sh"),
            "scripts/propertyquarry_release_make_dispatch.py",
            "propertyquarry-native-release-control-gates",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    if variable in {"PYTHON_BIN", "PYTEST_PYTHON_BIN"}:
        assert "release interpreter override forbidden" in result.stderr
    elif _RELEASE_REQUIREMENTS_STALE:
        assert _STALE_RELEASE_REQUIREMENTS in result.stderr
    elif variable in {
        "PROPERTYQUARRY_RELEASE_DISPATCH",
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
    }:
        assert "PROPERTYQUARRY_NATIVE_GO is required" in result.stderr
    else:
        assert (
            "caller-controlled release build environment is forbidden"
            in result.stderr
        )


@pytest.mark.parametrize("variable", sorted(make_dispatch.FORBIDDEN_ENVIRONMENT))
def test_release_dispatch_child_environment_rejects_every_forbidden_key(
    variable: str,
) -> None:
    with pytest.raises(
        make_dispatch.DispatchError,
        match="caller-controlled release build environment is forbidden",
    ):
        make_dispatch.child_environment({variable: ""})


def test_release_dispatch_rejects_makefiles_before_parse_time_side_effect(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "make-startup-ran"
    startup = tmp_path / "startup.mk"
    startup.write_text(
        f"$(shell /usr/bin/touch {marker})\n",
        encoding="utf-8",
    )
    environment = _dispatch_environment()
    environment["MAKEFILES"] = str(startup)

    result = subprocess.run(
        [
            str(ROOT / "scripts" / "propertyquarry_release_python.sh"),
            "scripts/propertyquarry_release_make_dispatch.py",
            "propertyquarry-native-release-control-gates",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    if _RELEASE_REQUIREMENTS_STALE:
        assert _STALE_RELEASE_REQUIREMENTS in result.stderr
    else:
        assert (
            "caller-controlled release build environment is forbidden"
            in result.stderr
        )
    assert not marker.exists()


def test_release_dispatch_command_and_environment_are_closed() -> None:
    target = "propertyquarry-native-release-control-gates"
    assert make_dispatch.make_command(target) == (
        "/usr/bin/make",
        "--no-print-directory",
        "-rR",
        "--file=/docker/property/Makefile",
        target,
    )
    environment = make_dispatch.child_environment(
        {
            "PATH": "/tmp/hostile",
            "SHELL": "/bin/true",
            "BASH_ENV": "/tmp/hostile",
            "PROPERTYQUARRY_NATIVE_GO": "/trusted/go",
            "UNRELATED_SECRET": "must-not-pass",
            "XDG_CACHE_HOME": "/tmp/hostile-cache",
            "XDG_CONFIG_HOME": "/tmp/hostile-config",
            "XDG_DATA_HOME": "/tmp/hostile-data",
            "XDG_RUNTIME_DIR": "/tmp/hostile-runtime",
            "XDG_STATE_HOME": "/tmp/hostile-state",
        }
    )
    assert environment["HOME"] == "/nonexistent"
    assert environment["PATH"] == "/usr/bin:/bin"
    assert environment["PLAYWRIGHT_BROWSERS_PATH"] == (
        "/docker/property/.propertyquarry_release_tools/ms-playwright"
    )
    assert environment["PROPERTYQUARRY_NATIVE_GO"] == "/trusted/go"
    assert "SHELL" not in environment
    assert "BASH_ENV" not in environment
    assert "UNRELATED_SECRET" not in environment
    assert "PYTEST_DEBUG_TEMPROOT" not in environment
    assert not (
        make_dispatch.PRIVATE_RUNTIME_DIRECTORIES.keys() & environment.keys()
    )


def test_release_dispatch_private_runtime_environment_is_owned_and_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_runtime = tmp_path / "session-runtime"
    session_runtime.mkdir(mode=0o700)
    session_runtime.chmod(0o700)
    monkeypatch.setattr(
        make_dispatch,
        "TRUSTED_SESSION_RUNTIME_DIRECTORY",
        session_runtime,
    )
    runtime_root: Path | None = None
    with make_dispatch.private_runtime_environment() as environment:
        assert set(environment) == {
            *make_dispatch.PRIVATE_RUNTIME_DIRECTORIES,
            "PYTEST_DEBUG_TEMPROOT",
            "XDG_RUNTIME_DIR",
        }
        private_paths = (
            Path(environment["PYTEST_DEBUG_TEMPROOT"]),
            *(
                Path(environment[variable])
                for variable in make_dispatch.PRIVATE_RUNTIME_DIRECTORIES
            ),
        )
        runtime_roots = {path.parent for path in private_paths}
        assert len(runtime_roots) == 1
        runtime_root = runtime_roots.pop()
        assert runtime_root.parent == make_dispatch.PRIVATE_RUNTIME_PARENT
        assert runtime_root.name.startswith(
            make_dispatch.PRIVATE_RUNTIME_PREFIX
        )
        representative_socket = (
            Path(environment["PYTEST_DEBUG_TEMPROOT"])
            / "pytest-of-release"
            / "pytest-9999"
            / "test_local_docker_socket_must_0"
            / "docker.sock"
        )
        assert len(os.fsencode(representative_socket)) <= 107
        for path in (runtime_root, *private_paths):
            metadata = path.lstat()
            assert stat.S_ISDIR(metadata.st_mode)
            assert not stat.S_ISLNK(metadata.st_mode)
            assert metadata.st_uid == os.geteuid()
            assert stat.S_IMODE(metadata.st_mode) == 0o700
        assert Path(environment["XDG_RUNTIME_DIR"]) == session_runtime

    assert runtime_root is not None
    assert not runtime_root.exists()
    assert session_runtime.is_dir()


def test_release_dispatch_uses_private_runtime_when_session_bus_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        make_dispatch,
        "TRUSTED_SESSION_RUNTIME_DIRECTORY",
        tmp_path / "absent-session-runtime",
    )
    runtime_path: Path | None = None

    with make_dispatch.private_runtime_environment() as environment:
        runtime_path = Path(environment["XDG_RUNTIME_DIR"])
        assert runtime_path.name == "runtime"
        assert stat.S_IMODE(runtime_path.stat().st_mode) == 0o700

    assert runtime_path is not None
    assert not runtime_path.exists()


def test_release_dispatch_cleanup_refuses_replaced_runtime_root() -> None:
    manager = make_dispatch.private_runtime_environment()
    environment = manager.__enter__()
    runtime_root = Path(environment["XDG_CACHE_HOME"]).parent
    displaced_root = runtime_root.with_name(f"{runtime_root.name}.displaced")
    runtime_root.rename(displaced_root)
    runtime_root.mkdir(mode=0o700)
    runtime_root.chmod(0o700)
    try:
        with pytest.raises(
            make_dispatch.DispatchError,
            match="identity changed before cleanup",
        ):
            manager.__exit__(None, None, None)
        assert displaced_root.is_dir()
        assert runtime_root.is_dir()
    finally:
        shutil.rmtree(displaced_root)
        shutil.rmtree(runtime_root)


def test_release_dispatch_property_gate_environment_is_exactly_target_scoped() -> None:
    gate_sources = {
        name: (ROOT / "scripts" / name).read_text(encoding="utf-8")
        for name in (
            "property_release_gates.sh",
            "propertyquarry_live_release_gates.sh",
        )
    }
    referenced_property_environment = {
        variable
        for source in gate_sources.values()
        for variable in re.findall(
            r"\$\{(PROPERTYQUARRY_[A-Z0-9_]+)",
            source,
        )
    }
    referenced_shared_environment = {
        name
        for name in (
            "COMPOSE_PROJECT_NAME",
            "DATABASE_URL",
            "EA_API_TOKEN",
            "EA_PRINCIPAL_ID",
            "EA_PUBLIC_TOUR_DIR",
            "TEABLE_API_KEY",
            "TEABLE_BASE_URL",
        )
        if any(f"${{{name}" in source for source in gate_sources.values())
    }
    live_gate = gate_sources["propertyquarry_live_release_gates.sh"]
    live_required_environment = (
        make_dispatch.PROPERTY_RELEASE_GATE_LIVE_REQUIRED_ENVIRONMENT
    )
    supplied = {
        name: f"/trusted/{name.lower()}"
        for name in make_dispatch.PROPERTY_RELEASE_GATE_ENVIRONMENT
    }
    supplied["UNRELATED_SECRET"] = "must-not-pass"

    property_gate = make_dispatch.child_environment(
        supplied,
        target="property-release-gates",
    )
    unrelated_gate = make_dispatch.child_environment(
        supplied,
        target="verify-flagship-release-readiness-authenticated",
    )

    assert referenced_property_environment <= (
        make_dispatch.PROPERTY_RELEASE_GATE_ENVIRONMENT
    )
    assert referenced_shared_environment <= (
        make_dispatch.PROPERTY_RELEASE_GATE_ENVIRONMENT
    )
    assert make_dispatch.PROPERTY_RELEASE_GATE_REQUIRED_ENVIRONMENT
    assert live_required_environment
    for name in live_required_environment:
        assert (
            f"${{{name}" in live_gate
            or f"require_provenance_value {name} " in live_gate
        )
    for name in make_dispatch.PROPERTY_RELEASE_GATE_ENVIRONMENT:
        assert property_gate[name] == supplied[name]
        assert name not in unrelated_gate
    assert "UNRELATED_SECRET" not in property_gate


def test_release_dispatch_child_environment_rejects_unknown_target() -> None:
    with pytest.raises(
        make_dispatch.DispatchError,
        match="unsupported authenticated release target",
    ):
        make_dispatch.child_environment({}, target="not-a-release-target")


def test_release_guidance_uses_authenticated_dispatch_and_privileged_shebangs() -> None:
    authenticated_property_gate = (
        "./scripts/propertyquarry_release_python.sh "
        "scripts/propertyquarry_release_make_dispatch.py "
        "property-release-gates"
    )
    authenticated_preflight = (
        "./scripts/propertyquarry_release_python.sh "
        "scripts/propertyquarry_release_make_dispatch.py release-preflight"
    )
    authenticated_ltd = (
        "./scripts/propertyquarry_release_python.sh "
        "scripts/propertyquarry_release_make_dispatch.py ltd-release-gates"
    )
    documents = {
        path: (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "README.md",
            "RUNBOOK.md",
            "PRODUCT_RELEASE_CHECKLIST.md",
            "docs/REPO_ISOLATION.md",
            "docs/PROPERTYQUARRY_RELEASE_LIFECYCLE_V2.md",
            "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md",
            "docs/PROPERTYQUARRY_SLO_RELEASE_EVIDENCE.md",
            "scripts/operator_summary.sh",
        )
    }

    assert authenticated_property_gate in documents["README.md"]
    assert authenticated_property_gate in documents[
        "PRODUCT_RELEASE_CHECKLIST.md"
    ]
    assert authenticated_property_gate in documents["docs/REPO_ISOLATION.md"]
    assert authenticated_property_gate in documents[
        "docs/PROPERTYQUARRY_SLO_RELEASE_EVIDENCE.md"
    ]
    assert authenticated_preflight in documents["RUNBOOK.md"]
    assert authenticated_preflight in documents[
        "docs/PROPERTYQUARRY_RELEASE_LIFECYCLE_V2.md"
    ]
    assert authenticated_preflight in documents[
        "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md"
    ]
    assert authenticated_preflight in documents["scripts/operator_summary.sh"]
    assert authenticated_ltd in documents["RUNBOOK.md"]
    assert authenticated_ltd in documents["scripts/operator_summary.sh"]

    forbidden_guidance = {
        "README.md": ("bash scripts/property_release_gates.sh",),
        "RUNBOOK.md": (
            "bash scripts/property_release_gates.sh",
            "bash scripts/verify_release_assets.sh",
            "`make release-preflight`",
            "`make ltd-release-gates`",
        ),
        "PRODUCT_RELEASE_CHECKLIST.md": ("`make property-release-gates`",),
        "docs/REPO_ISOLATION.md": ("`make property-release-gates`",),
        "docs/PROPERTYQUARRY_RELEASE_LIFECYCLE_V2.md": (
            "`make release-preflight`",
        ),
        "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md": (
            "bash scripts/verify_release_assets.sh",
        ),
        "docs/PROPERTYQUARRY_SLO_RELEASE_EVIDENCE.md": (
            "bash scripts/property_release_gates.sh",
        ),
    }
    for path, forbidden_values in forbidden_guidance.items():
        for forbidden in forbidden_values:
            assert forbidden not in documents[path]

    workflow = (ROOT / ".github" / "workflows" / "smoke-runtime.yml").read_text(
        encoding="utf-8"
    )
    assert "run: ./scripts/propertyquarry_live_release_gates.sh" in workflow
    assert "run: bash scripts/propertyquarry_live_release_gates.sh" not in workflow
    assert f"run: {authenticated_property_gate}" in workflow
    assert "run: bash scripts/property_release_gates.sh" not in workflow


@pytest.mark.parametrize(
    ("script", "arguments"),
    (
        ("property_release_gates.sh", ("--help",)),
        ("verify_release_assets.sh", ("--help",)),
        ("propertyquarry_live_release_gates.sh", ()),
    ),
)
def test_privileged_release_shell_entrypoints_do_not_source_bash_env(
    tmp_path: Path,
    script: str,
    arguments: tuple[str, ...],
) -> None:
    marker = tmp_path / f"{script}.bash-env-ran"
    bash_environment = tmp_path / "hostile-bash-env.sh"
    bash_environment.write_text(
        f"/usr/bin/touch -- {marker}\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [str(ROOT / "scripts" / script), *arguments],
        cwd=ROOT,
        env={
            "BASH_ENV": str(bash_environment),
            "HOME": str(tmp_path),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
            "TZ": "UTC",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    if arguments == ("--help",):
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 2
        assert "PROPERTYQUARRY_LIVE_MOBILE_BASE_URL" in result.stderr
    assert not marker.exists()


def test_release_dispatch_allowlist_matches_makefile_public_targets() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    marker = "override _PROPERTYQUARRY_RELEASE_DISPATCH_TARGETS := "
    line = next(line for line in makefile.splitlines() if line.startswith(marker))
    assert frozenset(line.removeprefix(marker).split()) == make_dispatch.DISPATCH_TARGETS


def test_local_aggregate_targets_use_explicit_non_authoritative_python_lane() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    materialize = _make_target(makefile, "materialize-release-assets")
    verify_assets = _make_target(makefile, "verify-release-assets")
    readiness = _make_target(makefile, "verify-flagship-release-readiness")
    generated = _make_target(
        makefile,
        "verify-generated-release-artifacts-clean",
    )
    all_local = _make_target(makefile, "all-local")

    assert "propertyquarry_release_python.sh" not in materialize
    assert "$(PYTHON_BIN)" in materialize
    assert "--developer" in verify_assets
    assert "$(PYTHON_BIN)" in readiness
    assert "$(PYTHON_BIN)" in generated
    assert "verify-release-assets" in all_local
    assert "authenticated" not in all_local


def test_flagship_readiness_verification_is_read_only_and_materialization_is_explicit() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    materialize = _make_target(makefile, "materialize-release-assets")
    authenticated_materialize = _make_target(
        makefile,
        "materialize-release-assets-authenticated",
    )
    readiness = _make_target(
        makefile,
        "verify-flagship-release-readiness",
    )
    authenticated_readiness = _make_target(
        makefile,
        "verify-flagship-release-readiness-authenticated",
    )

    assert readiness.strip().splitlines() == [
        "verify-flagship-release-readiness:",
        "\t$(PYTHON_BIN) scripts/verify_flagship_release_readiness.py",
    ]
    assert authenticated_readiness.strip().splitlines() == [
        "verify-flagship-release-readiness-authenticated:",
        (
            "\t./scripts/propertyquarry_release_python.sh "
            "scripts/verify_flagship_release_readiness.py"
        ),
    ]
    materializer_scripts = [
        "scripts/materialize_ea_browser_workflow_proof.py",
        "scripts/materialize_ea_flagship_release_gate.py",
        "scripts/materialize_weekly_product_pulse.py",
    ]
    assert [
        line.split()[-1]
        for line in materialize.splitlines()[1:]
        if line.strip()
    ] == materializer_scripts
    assert [
        line.split()[-1]
        for line in authenticated_materialize.splitlines()[1:]
        if line.strip()
    ] == materializer_scripts
    for script in materializer_scripts:
        assert script not in readiness
        assert script not in authenticated_readiness
    assert (
        "materialize-release-assets-authenticated"
        in make_dispatch.DISPATCH_TARGETS
    )
    assert (
        "verify-flagship-release-readiness-authenticated"
        in make_dispatch.DISPATCH_TARGETS
    )

    generated_artifacts = (
        ROOT
        / ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json",
        ROOT
        / ".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json",
        ROOT
        / ".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json",
    )
    bytes_before = {
        artifact: artifact.read_bytes()
        for artifact in generated_artifacts
    }
    result = subprocess.run(
        [
            str(ROOT / "scripts/propertyquarry_release_python.sh"),
            "scripts/propertyquarry_release_make_dispatch.py",
            "verify-flagship-release-readiness-authenticated",
        ],
        cwd=ROOT,
        env=_dispatch_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    bytes_after = {
        artifact: artifact.read_bytes()
        for artifact in generated_artifacts
    }

    assert bytes_after == bytes_before
    if _RELEASE_REQUIREMENTS_STALE:
        assert result.returncode == 2
        assert result.stdout == ""
        assert _STALE_RELEASE_REQUIREMENTS in result.stderr
    else:
        json_start = result.stdout.find("{")
        assert json_start >= 0, result
        payload = json.loads(result.stdout[json_start:])
        assert payload["status"] in {"pass", "blocked"}
        assert result.returncode == (
            0 if payload["status"] == "pass" else 2
        )


def test_ci_aggregate_verification_never_reaches_receipt_materializers() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    isolated_test_prefix = [
        "\t@set -eu; \\",
        (
            '\tpropertyquarry_ci_temp="$$(/usr/bin/mktemp -d '
            '/tmp/propertyquarry-ci-test.XXXXXXXX)"; \\'
        ),
        "\tcleanup_propertyquarry_ci_temp() { \\",
        '\t  case "$$propertyquarry_ci_temp" in \\',
        (
            "\t    /tmp/propertyquarry-ci-test.*) /bin/rm -rf -- "
            '"$$propertyquarry_ci_temp" ;; \\'
        ),
        (
            '\t    *) echo "refusing unsafe CI temp cleanup: '
            '$$propertyquarry_ci_temp" >&2; return 70 ;; \\'
        ),
        "\t  esac; \\",
        "\t}; \\",
        "\ttrap cleanup_propertyquarry_ci_temp EXIT; \\",
    ]
    expected_test_recipes = {
        "test-api": [
            "test-api:",
            *isolated_test_prefix,
            (
                '\tPROPERTYQUARRY_GATE_RECEIPT_DIR="'
                '$$propertyquarry_ci_temp/exit-gate" PYTHONPATH=ea '
                "EA_STORAGE_BACKEND=memory $(PYTEST_PYTHON_BIN) -m pytest "
                "-q tests -p no:cacheprovider --durations=25 "
                "--durations-min=1.0 $(TEST_API_PYTEST_IGNORE) "
                "$(TEST_API_PYTEST_DESELECT)"
            ),
        ],
        "test-api-authenticated": [
            "test-api-authenticated:",
            *isolated_test_prefix,
            (
                '\tPROPERTYQUARRY_GATE_RECEIPT_DIR="'
                '$$propertyquarry_ci_temp/exit-gate" '
                "/usr/bin/env -u PROPERTYQUARRY_RELEASE_DISPATCH "
                "EA_STORAGE_BACKEND=memory "
                "./scripts/propertyquarry_release_python.sh -m pytest -q "
                "tests -p no:cacheprovider --durations=25 "
                "--durations-min=1.0 "
                "$(TEST_API_PYTEST_IGNORE) $(TEST_API_PYTEST_DESELECT)"
            ),
        ],
    }
    for target, expected_recipe in expected_test_recipes.items():
        assert _make_target(makefile, target).strip().splitlines() == expected_recipe

    roots = (
        "test-api",
        "test-api-authenticated",
        "ci-gates",
        "ci-gates-authenticated",
    )
    reachable = _reachable_make_targets(makefile, roots)
    forbidden_targets = {
        "materialize-release-assets",
        "materialize-release-assets-authenticated",
    }
    assert forbidden_targets.isdisjoint(reachable)
    forbidden_scripts = (
        "scripts/materialize_ea_browser_workflow_proof.py",
        "scripts/materialize_ea_flagship_release_gate.py",
        "scripts/materialize_weekly_product_pulse.py",
    )
    for target, section in reachable.items():
        for script in forbidden_scripts:
            assert script not in section, (
                f"{target} reaches canonical receipt materializer {script}"
            )


def test_local_api_gate_cleans_its_private_evidence_directory(
    tmp_path: Path,
) -> None:
    stub = tmp_path / "pytest-stub"
    stub.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'case "${PROPERTYQUARRY_GATE_RECEIPT_DIR:?}" in\n'
        "  /tmp/propertyquarry-ci-test.*/exit-gate) ;;\n"
        "  *) exit 71 ;;\n"
        "esac\n"
        'printf "%s\\n" "$PROPERTYQUARRY_GATE_RECEIPT_DIR" '
        '> "$PROPERTYQUARRY_TEST_OBSERVED_PATH"\n',
        encoding="utf-8",
    )
    stub.chmod(0o700)
    observed_path = tmp_path / "observed-path.txt"
    environment = dict(os.environ)
    environment["PROPERTYQUARRY_TEST_OBSERVED_PATH"] = str(observed_path)

    result = subprocess.run(
        [
            "/usr/bin/make",
            "--no-print-directory",
            "test-api",
            f"PYTEST_PYTHON_BIN={stub}",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    receipt_path = Path(observed_path.read_text(encoding="utf-8").strip())
    assert not receipt_path.exists()
    assert not receipt_path.parent.exists()


def test_local_api_gate_cleans_private_evidence_after_pytest_failure(
    tmp_path: Path,
) -> None:
    stub = tmp_path / "pytest-failure-stub"
    stub.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'case "${PROPERTYQUARRY_GATE_RECEIPT_DIR:?}" in\n'
        "  /tmp/propertyquarry-ci-test.*/exit-gate) ;;\n"
        "  *) exit 71 ;;\n"
        "esac\n"
        'printf "%s\\n" "$PROPERTYQUARRY_GATE_RECEIPT_DIR" '
        '> "$PROPERTYQUARRY_TEST_OBSERVED_PATH"\n'
        "exit 73\n",
        encoding="utf-8",
    )
    stub.chmod(0o700)
    observed_path = tmp_path / "observed-path.txt"
    environment = dict(os.environ)
    environment["PROPERTYQUARRY_TEST_OBSERVED_PATH"] = str(observed_path)

    result = subprocess.run(
        [
            "/usr/bin/make",
            "--no-print-directory",
            "test-api",
            f"PYTEST_PYTHON_BIN={stub}",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    receipt_path = Path(observed_path.read_text(encoding="utf-8").strip())
    assert not receipt_path.exists()
    assert not receipt_path.parent.exists()


def test_release_preflight_verification_leaves_preserve_generated_artifacts() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    release_preflight = _make_target(makefile, "release-preflight")
    local_verify_assets = _make_target(makefile, "verify-release-assets")
    local_verify_generated = _make_target(
        makefile,
        "verify-generated-release-artifacts-clean",
    )
    verify_assets = _make_target(
        makefile,
        "verify-release-assets-authenticated",
    )
    verify_generated = _make_target(
        makefile,
        "verify-generated-release-artifacts-clean-authenticated",
    )

    assert release_preflight.strip().splitlines() == [
        "release-preflight:",
        "\t$(MAKE) verify-release-assets-authenticated",
        "\t$(MAKE) verify-flagship-release-readiness-authenticated",
        "\t$(MAKE) verify-generated-release-artifacts-clean-authenticated",
        "\t$(MAKE) operator-help-authenticated",
        "\t$(MAKE) release-smoke-authenticated",
    ]
    assert local_verify_assets.strip().splitlines() == [
        "verify-release-assets:",
        (
            '\tPYTHON_BIN="$(PYTHON_BIN)" '
            "./scripts/verify_release_assets.sh --developer"
        ),
    ]
    assert local_verify_generated.strip().splitlines() == [
        "verify-generated-release-artifacts-clean:",
        "\t$(PYTHON_BIN) scripts/verify_generated_release_artifacts_clean.py",
    ]
    assert verify_assets.strip().splitlines() == [
        "verify-release-assets-authenticated:",
        "\t/bin/bash -p scripts/verify_release_assets.sh",
    ]
    assert verify_generated.strip().splitlines() == [
        "verify-generated-release-artifacts-clean-authenticated:",
        (
            "\t./scripts/propertyquarry_release_python.sh "
            "scripts/verify_generated_release_artifacts_clean.py"
        ),
    ]
    assert "materialize-release-assets" not in release_preflight
    assert "materialize-release-assets" not in local_verify_assets
    assert "materialize-release-assets" not in local_verify_generated
    assert "materialize-release-assets" not in verify_assets
    assert "materialize-release-assets" not in verify_generated

    generated_artifacts = (
        ROOT
        / ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json",
        ROOT
        / ".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json",
        ROOT
        / ".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json",
    )

    def snapshot() -> dict[Path, tuple[int, int, int, int, int, int, bytes]]:
        result: dict[Path, tuple[int, int, int, int, int, int, bytes]] = {}
        for artifact in generated_artifacts:
            metadata = artifact.lstat()
            result[artifact] = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
                artifact.read_bytes(),
            )
        return result

    expected_snapshot = snapshot()
    for target in (
        "verify-release-assets-authenticated",
        "verify-generated-release-artifacts-clean-authenticated",
    ):
        result = subprocess.run(
            [
                str(ROOT / "scripts/propertyquarry_release_python.sh"),
                "scripts/propertyquarry_release_make_dispatch.py",
                target,
            ],
            cwd=ROOT,
            env=_dispatch_environment(),
            check=False,
            capture_output=True,
            text=True,
        )
        combined_output = result.stdout + result.stderr
        if _RELEASE_REQUIREMENTS_STALE:
            assert result.returncode == 2, combined_output
            assert _STALE_RELEASE_REQUIREMENTS in combined_output
        else:
            assert result.returncode in {0, 2}, combined_output
            if result.returncode == 0:
                assert (
                    "generated release artifacts" in combined_output
                    or "release asset verification passed" in combined_output
                )
            else:
                assert (
                    "semantic drift" in combined_output
                    or "exact HEAD" in combined_output
                    or (
                        "release manifest worktree content differs from HEAD"
                        in combined_output
                    )
                )
        assert snapshot() == expected_snapshot


def test_release_asset_developer_mode_rejects_non_python_executable() -> None:
    result = subprocess.run(
        [
            str(ROOT / "scripts" / "verify_release_assets.sh"),
            "--developer",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHON_BIN": "/bin/true",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "developer Python 3.12 or newer is required" in result.stderr


def test_release_bundle_rejects_interpreter_override_before_help_or_mutation() -> None:
    result = subprocess.run(
        ["/bin/bash", "-p", "scripts/property_release_gates.sh", "--help"],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHON_BIN": "/bin/true",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "error: release interpreter override forbidden\n"


@pytest.mark.parametrize("variable", ("EA_TEST_PYTHON", "EA_TEST_FILES"))
def test_hard_exit_gate_rejects_postgres_test_overrides_before_help(
    variable: str,
) -> None:
    result = subprocess.run(
        [str(ROOT / "scripts" / "hard_exit_gates.sh"), "--help"],
        cwd=ROOT,
        env=_hard_exit_environment(**{variable: "/bin/true"}),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "error: postgres contract test override forbidden\n"


def test_hard_exit_gate_pins_postgres_contract_interpreter_and_default_set() -> None:
    script = (ROOT / "scripts" / "hard_exit_gates.sh").read_text(
        encoding="utf-8"
    )

    assert 'if [[ -v EA_TEST_PYTHON || -v EA_TEST_FILES ]]; then' in script
    assert "unset EA_TEST_PYTHON EA_TEST_FILES" in script
    assert (
        'EA_TEST_PYTHON="${RELEASE_PYTHON}" '
        "/bin/bash -p scripts/test_postgres_contracts.sh"
        in script
    )


@pytest.mark.parametrize(
    "variable",
    _HARD_EXIT_PYTHON_RUNTIME_OVERRIDES,
)
def test_hard_exit_gate_rejects_python_runtime_overrides_before_help(
    variable: str,
) -> None:
    result = subprocess.run(
        [str(ROOT / "scripts" / "hard_exit_gates.sh"), "--help"],
        cwd=ROOT,
        env=_hard_exit_environment(**{variable: "hostile"}),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert (
        result.stderr
        == f"error: Python runtime override forbidden: {variable}\n"
    )


def test_hard_exit_gate_pins_safe_python_runtime_flags() -> None:
    script = (ROOT / "scripts" / "hard_exit_gates.sh").read_text(
        encoding="utf-8"
    )

    assert "unset PYTHONHOME PYTHONOPTIMIZE PYTHONPATH" in script
    for assignment in (
        "export PYTHONDONTWRITEBYTECODE=1",
        "export PYTHONHASHSEED=0",
        "export PYTHONNOUSERSITE=1",
        "export PYTHONSAFEPATH=1",
    ):
        assert assignment in script
        assert script.index(assignment) < script.index(
            'if [[ "${1:-}" == "--help"'
        )


def test_hard_exit_gate_rejects_public_home_smoke_override_before_help() -> None:
    result = subprocess.run(
        [str(ROOT / "scripts" / "hard_exit_gates.sh"), "--help"],
        cwd=ROOT,
        env=_hard_exit_environment(
            PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED="0"
        ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert (
        result.stderr
        == "error: PropertyQuarry public-home smoke override forbidden\n"
    )


def test_hard_exit_gate_pins_public_home_check_for_both_postgres_smokes() -> None:
    script = (ROOT / "scripts" / "hard_exit_gates.sh").read_text(
        encoding="utf-8"
    )

    assert "if [[ -v PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED ]]; then" in script
    assert "unset PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED" in script
    smoke_calls = [
        line
        for line in script.splitlines()
        if line.startswith("run_flagship_postgres_smoke")
    ]
    assert smoke_calls == [
        "run_flagship_postgres_smoke() {",
        "run_flagship_postgres_smoke",
        "run_flagship_postgres_smoke --legacy-fixture",
    ]
    function_start = script.index("run_flagship_postgres_smoke() {")
    function_end = script.index("\n}\n", function_start)
    function_body = script[function_start:function_end]
    assert "PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED=1" in function_body


def test_hard_exit_gate_pins_exact_flagship_postgres_target() -> None:
    script = (ROOT / "scripts" / "hard_exit_gates.sh").read_text(
        encoding="utf-8"
    )
    function_start = script.index("run_flagship_postgres_smoke() {")
    function_end = script.index("\n}\n", function_start)
    function_body = script[function_start:function_end]
    compose = (ROOT / "docker-compose.property.yml").read_text(encoding="utf-8")

    for assignment in (
        'COMPOSE_FILE="${EA_ROOT}/docker-compose.property.yml"',
        "COMPOSE_PROJECT_NAME=propertyquarry",
        "EA_DB_CONTAINER=propertyquarry-db-live",
        "EA_SMOKE_DB=ea_smoke_runtime",
        "POSTGRES_DB=ea_smoke_runtime",
        "POSTGRES_USER=postgres",
        "PROPERTYQUARRY_API_CONTAINER_NAME=propertyquarry-api",
        "PROPERTYQUARRY_API_SERVICE=propertyquarry-api",
        "PROPERTYQUARRY_DB_CONTAINER_NAME=propertyquarry-db-live",
        "PROPERTYQUARRY_DB_SERVICE=propertyquarry-db",
        "PROPERTYQUARRY_SCHEDULER_CONTAINER_NAME=propertyquarry-scheduler",
        "PROPERTYQUARRY_SCHEDULER_SERVICE=propertyquarry-scheduler",
        "PROPERTYQUARRY_WORKER_CONTAINER_NAME=propertyquarry-worker",
        "PROPERTYQUARRY_WORKER_SERVICE=propertyquarry-worker",
    ):
        assert assignment in function_body
    for service in (
        "propertyquarry-api",
        "propertyquarry-worker",
        "propertyquarry-scheduler",
        "propertyquarry-db",
    ):
        assert f"  {service}:" in compose
    assert "/bin/bash -p scripts/smoke_postgres.sh" in function_body


@pytest.mark.parametrize(
    "variable",
    _HARD_EXIT_POSTGRES_TOPOLOGY_OVERRIDES,
)
def test_hard_exit_gate_rejects_postgres_topology_overrides_before_help(
    variable: str,
) -> None:
    result = subprocess.run(
        [str(ROOT / "scripts" / "hard_exit_gates.sh"), "--help"],
        cwd=ROOT,
        env=_hard_exit_environment(**{variable: "alternate-runtime"}),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert (
        result.stderr
        == f"error: postgres smoke topology override forbidden: {variable}\n"
    )


def test_hard_exit_gate_rejects_host_port_override_before_help() -> None:
    result = subprocess.run(
        [str(ROOT / "scripts" / "hard_exit_gates.sh"), "--help"],
        cwd=ROOT,
        env=_hard_exit_environment(EA_HOST_PORT="65535"),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == (
        "error: smoke target override forbidden: EA_HOST_PORT\n"
    )


@pytest.mark.parametrize("variable", _HARD_EXIT_PROXY_OVERRIDES)
def test_hard_exit_gate_rejects_smoke_proxy_overrides_before_help(
    variable: str,
) -> None:
    result = subprocess.run(
        [str(ROOT / "scripts" / "hard_exit_gates.sh"), "--help"],
        cwd=ROOT,
        env=_hard_exit_environment(**{variable: "http://127.0.0.1:1"}),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == f"error: smoke proxy override forbidden: {variable}\n"


def test_hard_exit_gate_pins_local_docker_and_loopback_proxy_bypass() -> None:
    script = (ROOT / "scripts" / "hard_exit_gates.sh").read_text(
        encoding="utf-8"
    )

    assert "export DOCKER_HOST=unix:///var/run/docker.sock" in script
    assert "export DOCKER_CONFIG=/nonexistent" in script
    assert "export NO_PROXY=localhost,127.0.0.1,::1" in script
    assert "export no_proxy=localhost,127.0.0.1,::1" in script


@pytest.mark.parametrize(
    "script_name",
    ("export_openapi.sh", "smoke_api_tibor.sh", "smoke_postgres.sh"),
)
def test_release_smoke_curl_ignores_ambient_startup_config(
    script_name: str,
) -> None:
    script = (ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    assert (
        'curl() {\n  # shellcheck disable=SC2317\n  command curl -q "$@"\n}'
        in script
    )
    assert script.index("curl() {") < script.index("EA_ROOT=")


@pytest.mark.parametrize(
    ("script_name", "prefix"),
    (
        ("smoke_api.sh", "propertyquarry-smoke-api"),
        ("smoke_api_tibor.sh", "propertyquarry-smoke-api-tibor"),
        ("smoke_postgres.sh", "propertyquarry-smoke-postgres"),
    ),
)
def test_release_smoke_private_temp_directory_is_mode_0700_and_removed(
    script_name: str,
    prefix: str,
) -> None:
    source = (ROOT / "scripts" / script_name).read_text(encoding="utf-8")
    create_function = _bash_function(source, "create_smoke_tmp_dir")
    cleanup_function = _bash_function(source, "cleanup_smoke_tmp")
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{create_function}\n"
                f"{cleanup_function}\n"
                "SMOKE_TMP_DIR=''\n"
                "create_smoke_tmp_dir\n"
                "created_path=\"${SMOKE_TMP_DIR}\"\n"
                "created_mode=\"$(stat -c '%a' -- \"${created_path}\")\"\n"
                "printf '%s\\n%s\\n' \"${created_path}\" \"${created_mode}\"\n"
                "cleanup_smoke_tmp\n"
                "[[ ! -e \"${created_path}\" && ! -L \"${created_path}\" ]]\n"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    created_path, created_mode = result.stdout.splitlines()
    assert created_path.startswith(f"/tmp/{prefix}.")
    assert created_mode == "700"
    assert not Path(created_path).exists()


@pytest.mark.parametrize(
    "script_name",
    ("smoke_api.sh", "smoke_api_tibor.sh"),
)
def test_release_smoke_response_writer_rejects_hostile_tmp_symlink_without_curl(
    script_name: str,
    tmp_path: Path,
) -> None:
    source = (ROOT / "scripts" / script_name).read_text(encoding="utf-8")
    curl_status_code = _bash_function(source, "curl_status_code")
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.write_text("do-not-touch\n", encoding="utf-8")
    hostile_path = private_dir / "predictable-response.json"
    hostile_path.symlink_to(victim)
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{curl_status_code}\n"
                "SMOKE_TMP_DIR=\"$1\"\n"
                "if curl_status_code \"$2\" https://should-not-run.invalid; then\n"
                "  exit 99\n"
                "else\n"
                "  rc=$?\n"
                "fi\n"
                "[[ \"${rc}\" == '2' ]]\n"
            ),
            "bash",
            str(private_dir),
            str(hostile_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": "/nonexistent"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert (
        result.stderr
        == "refusing smoke response path outside private temp directory\n"
    )
    assert hostile_path.is_symlink()
    assert victim.read_text(encoding="utf-8") == "do-not-touch\n"


@pytest.mark.parametrize(
    "script_name",
    ("smoke_api.sh", "smoke_api_tibor.sh"),
)
def test_release_smoke_response_writer_atomically_publishes_response(
    script_name: str,
    tmp_path: Path,
) -> None:
    source = (ROOT / "scripts" / script_name).read_text(encoding="utf-8")
    curl_status_code = _bash_function(source, "curl_status_code")
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    response_path = private_dir / "response.json"
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{curl_status_code}\n"
                "curl() {\n"
                "  local headers='' output=''\n"
                "  while (( $# )); do\n"
                "    case \"$1\" in\n"
                "      -D) headers=\"$2\"; shift 2 ;;\n"
                "      -o) output=\"$2\"; shift 2 ;;\n"
                "      *) shift ;;\n"
                "    esac\n"
                "  done\n"
                "  printf 'HTTP/1.1 403 Forbidden\\r\\n\\r\\n' >\"${headers}\"\n"
                "  printf 'expected response\\n' >\"${output}\"\n"
                "}\n"
                "SMOKE_TMP_DIR=\"$1\"\n"
                "curl_status_code \"$2\" https://should-not-run.invalid\n"
            ),
            "bash",
            str(private_dir),
            str(response_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "403"
    assert result.stderr == ""
    assert response_path.read_text(encoding="utf-8") == "expected response\n"
    assert sorted(private_dir.iterdir()) == [response_path]


def test_flagship_smoke_chain_has_no_predictable_response_output_paths() -> None:
    postgres = (ROOT / "scripts" / "smoke_postgres.sh").read_text(
        encoding="utf-8"
    )
    tibor = (ROOT / "scripts" / "smoke_api_tibor.sh").read_text(
        encoding="utf-8"
    )
    api = (ROOT / "scripts" / "smoke_api.sh").read_text(encoding="utf-8")

    assert "/tmp/ea_smoke_ready.json" not in postgres
    assert "/tmp/ea_smoke_token_probe.json" not in postgres
    assert "/tmp/tibor_ea_" not in tibor
    assert '${EA_ROOT}/.smoke_tmp' not in api
    for source in (postgres, tibor, api):
        assert "create_smoke_tmp_dir\ntrap cleanup_smoke_tmp EXIT" in source
        assert "cleanup_smoke_tmp\n}" in source


def test_release_asset_verifier_uses_private_temporary_workspace() -> None:
    source = (
        ROOT / "scripts" / "verify_release_assets.sh"
    ).read_text(encoding="utf-8")

    assert "/tmp/ea_design_mirror_verify.out" not in source
    assert "/tmp/ea_design_mirror_verify.err" not in source
    assert "/tmp/ea_design_mirror_full_verify.out" not in source
    assert "/tmp/ea_design_mirror_full_verify.err" not in source
    assert (
        '"/tmp/propertyquarry-release-assets.${BASHPID}.XXXXXXXX"'
        in source
    )
    assert 'SMOKE_RUNTIME_GUARD_TARGET="${RELEASE_ASSETS_TMP_DIR}/' in source
    assert 'DESIGN_MIRROR_STDOUT="${RELEASE_ASSETS_TMP_DIR}/' in source
    assert 'FULL_MIRROR_STDERR="${RELEASE_ASSETS_TMP_DIR}/' in source
    assert "candidate_metadata%:*:*" in source
    assert "/usr/bin/rm -rf --one-file-system --" in source
    assert "trap 'release_assets_cleanup_on_exit \"$?\"' EXIT" in source
    assert "trap 'release_assets_terminate_from_signal 143' TERM" in source
    assert (
        'if ! cleanup_release_assets_tmp && [[ "${status}" -eq 0 ]]; then'
        in source
    )


def test_release_asset_verifier_temporary_cleanup_is_identity_bound(
    tmp_path: Path,
) -> None:
    source = (
        ROOT / "scripts" / "verify_release_assets.sh"
    ).read_text(encoding="utf-8")
    temporary = tmp_path / "propertyquarry-release-assets.fixture"
    original = tmp_path / "original"
    temporary.mkdir(mode=0o700)
    expected_identity = f"{temporary.stat().st_dev}:{temporary.stat().st_ino}"
    temporary.rename(original)
    temporary.mkdir(mode=0o700)
    (temporary / "replacement-marker").write_text(
        "preserve\n",
        encoding="utf-8",
    )
    cleanup_function = _bash_function(
        source,
        "cleanup_release_assets_tmp",
    ).replace(
        "    /tmp/propertyquarry-release-assets.*)",
        f'    "{temporary}")',
    )
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{cleanup_function}\n"
                "RELEASE_ASSETS_TMP_DIR=\"$1\"\n"
                "RELEASE_ASSETS_TMP_IDENTITY=\"$2\"\n"
                "if cleanup_release_assets_tmp; then exit 99; else exit 0; fi\n"
            ),
            "bash",
            str(temporary),
            expected_identity,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == (
        "error: refusing cleanup of replaced release asset temporary directory\n"
    )
    assert (temporary / "replacement-marker").read_text(
        encoding="utf-8"
    ) == "preserve\n"
    assert original.is_dir()


def test_release_asset_verifier_temporary_cleanup_removes_exact_tree(
    tmp_path: Path,
) -> None:
    source = (
        ROOT / "scripts" / "verify_release_assets.sh"
    ).read_text(encoding="utf-8")
    temporary = tmp_path / "propertyquarry-release-assets.fixture"
    temporary.mkdir(mode=0o700)
    (temporary / "private-output").write_text("output\n", encoding="utf-8")
    expected_identity = f"{temporary.stat().st_dev}:{temporary.stat().st_ino}"
    cleanup_function = _bash_function(
        source,
        "cleanup_release_assets_tmp",
    ).replace(
        "    /tmp/propertyquarry-release-assets.*)",
        f'    "{temporary}")',
    )
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{cleanup_function}\n"
                "RELEASE_ASSETS_TMP_DIR=\"$1\"\n"
                "RELEASE_ASSETS_TMP_IDENTITY=\"$2\"\n"
                "cleanup_release_assets_tmp\n"
                "[[ -z \"${RELEASE_ASSETS_TMP_DIR}\" ]]\n"
                "[[ -z \"${RELEASE_ASSETS_TMP_IDENTITY}\" ]]\n"
            ),
            "bash",
            str(temporary),
            expected_identity,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert not temporary.exists()


@pytest.mark.parametrize(
    ("main_status", "expected_status"),
    (
        (0, 1),
        (17, 17),
    ),
)
def test_release_asset_verifier_exit_cleanup_failure_is_fail_closed(
    main_status: int,
    expected_status: int,
) -> None:
    source = (
        ROOT / "scripts" / "verify_release_assets.sh"
    ).read_text(encoding="utf-8")
    exit_function = _bash_function(source, "release_assets_cleanup_on_exit")
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{exit_function}\n"
                "cleanup_release_assets_tmp() {\n"
                "  printf 'cleanup failed\\n' >&2\n"
                "  return 7\n"
                "}\n"
                "release_assets_cleanup_on_exit \"$1\"\n"
            ),
            "bash",
            str(main_status),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == expected_status
    assert result.stdout == ""
    assert result.stderr == "cleanup failed\n"


@pytest.mark.parametrize("signal_status", (129, 130, 143))
def test_release_asset_verifier_cleanup_failure_preserves_signal_status(
    signal_status: int,
) -> None:
    source = (
        ROOT / "scripts" / "verify_release_assets.sh"
    ).read_text(encoding="utf-8")
    signal_function = _bash_function(
        source,
        "release_assets_terminate_from_signal",
    )
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{signal_function}\n"
                "cleanup_release_assets_tmp() {\n"
                "  printf 'cleanup failed\\n' >&2\n"
                "  return 7\n"
                "}\n"
                "release_assets_terminate_from_signal \"$1\"\n"
            ),
            "bash",
            str(signal_status),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == signal_status
    assert result.stdout == ""
    assert result.stderr == "cleanup failed\n"


def test_release_asset_generated_drift_probe_fails_without_traceback() -> None:
    source = (
        ROOT / "scripts" / "verify_release_assets.sh"
    ).read_text(encoding="utf-8")
    probe_start = source.index(
        "from scripts.verify_generated_release_artifacts_clean import _normalize"
    )
    probe_end = source.index(
        'echo "== verify release docs linkage =="',
        probe_start,
    )
    probe = source[probe_start:probe_end]

    assert "assert _normalize" not in probe
    assert 'print(f"{path}: semantic drift from HEAD", file=sys.stderr)' in probe
    assert "raise SystemExit(1)" in probe


def test_tibor_smoke_payloads_use_defaulted_principal_variable() -> None:
    script = (ROOT / "scripts" / "smoke_api_tibor.sh").read_text(
        encoding="utf-8"
    )

    assert 'PRINCIPAL_ID="${EA_PRINCIPAL_ID:-exec-1}"' in script
    assert "${EA_PRINCIPAL_ID}" not in script
    assert script.count('\\"principal_id\\":\\"${PRINCIPAL_ID}\\"') >= 4


def test_release_asset_verifier_has_no_bare_python_command() -> None:
    script = (ROOT / "scripts" / "verify_release_assets.sh").read_text(
        encoding="utf-8"
    )
    bare_python = re.compile(
        r"""
        (?:^|[;&|][ \t]*)
        (?:
          (?:(?:if|elif|while|until|!|command|exec|time|env)[ \t]+)
          |
          (?:[A-Za-z_][A-Za-z0-9_]*=[^;&| \t]+[ \t]+)
        )*
        python(?:3(?:\.[0-9]+)?)?
        (?=[ \t]|$)
        """,
        re.MULTILINE | re.VERBOSE,
    )

    assert bare_python.findall(script) == []


def test_smoke_runtime_bootstraps_and_runs_from_real_canonical_checkout() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "smoke-runtime.yml"
    ).read_text(encoding="utf-8")
    job_start = workflow.index("  smoke-runtime-api:")
    job_end = workflow.index("\n  propertyquarry-browser-contracts:", job_start)
    job = workflow[job_start:job_end]

    prepare = job.index("- name: Prepare canonical repository path")
    bootstrap = job.index("- name: Bootstrap hash-locked release verifier")
    chromium = job.index("- name: Install Chromium in canonical release cache")
    reproduce = job.index(
        "- name: Reproduce canonical release receipts in disposable checkout"
    )
    verify_reproduction = job.index(
        "- name: Verify reproduced release receipts match HEAD"
    )
    core = job.index("- name: Run read-only authenticated core CI gates")
    assert prepare < bootstrap < chromium < reproduce < verify_reproduction < core
    assert 'test ! -e /docker/property' in job
    assert 'install -d -m 0755 /docker/property' in job
    assert 'cp -a "$GITHUB_WORKSPACE/." /docker/property/' in job
    assert 'ln -sfn "$GITHUB_WORKSPACE" /docker/property' not in job
    assert "working-directory: /docker/property" in job[bootstrap:reproduce]
    assert (
        "./scripts/bootstrap_propertyquarry_release_python.sh"
        in job[bootstrap:reproduce]
    )
    assert "make " not in job[bootstrap:core]
    assert (
        "PLAYWRIGHT_BROWSERS_PATH: "
        "/docker/property/.propertyquarry_release_tools/ms-playwright"
        in job[chromium:reproduce]
    )
    assert (
        "python -m playwright install --with-deps chromium"
        in job[chromium:reproduce]
    )
    assert job.count(
        "- name: Reproduce canonical release receipts in disposable checkout"
    ) == 1
    assert (
        "working-directory: /docker/property" in job[reproduce:verify_reproduction]
    )
    assert (
        "run: ./scripts/propertyquarry_release_python.sh "
        "scripts/propertyquarry_release_make_dispatch.py "
        "materialize-release-assets-authenticated"
        in job[reproduce:verify_reproduction]
    )
    assert job.count("- name: Verify reproduced release receipts match HEAD") == 1
    assert "working-directory: /docker/property" in job[verify_reproduction:core]
    assert (
        "run: ./scripts/propertyquarry_release_python.sh "
        "scripts/propertyquarry_release_make_dispatch.py "
        "verify-generated-release-artifacts-clean-authenticated"
        in job[verify_reproduction:core]
    )
    assert "working-directory: /docker/property" in job[core:]
    assert (
        "run: ./scripts/propertyquarry_release_python.sh "
        "scripts/propertyquarry_release_make_dispatch.py ci-gates-authenticated"
        in job[core:]
    )


def test_gold_publication_occurs_only_after_every_release_gate() -> None:
    script = (ROOT / "scripts" / "property_release_gates.sh").read_text(
        encoding="utf-8"
    )
    clear_call = script.index("\nclear_gold_publication\n")
    first_release_gate = script.index(
        "scripts/propertyquarry_postgres_dr.py release-gate"
    )
    pytest_calls = [
        match.start()
        for match in re.finditer(r'"\$\{PYTHON_BIN\}" -m pytest -q', script)
    ]
    gold_call = script.index("scripts/propertyquarry_gold_status.py")
    notification_call = script.index("scripts/propertyquarry_notify_gold_status.py")

    assert script.count("scripts/propertyquarry_gold_status.py") == 1
    assert clear_call < first_release_gate
    assert pytest_calls
    assert max(pytest_calls) < gold_call
    assert script.rindex("scripts/propertyquarry_visual_watch.py") < gold_call
    assert gold_call < notification_call
    assert '--write "${gold_receipt}"' in script[gold_call:notification_call]


def test_failed_release_run_invalidates_every_canonical_gold_publication(
    tmp_path: Path,
) -> None:
    script = (ROOT / "scripts" / "property_release_gates.sh").read_text(
        encoding="utf-8"
    )
    cleanup = script[
        script.index("clear_gold_publication() {") : script.index(
            "\nif [[ -z \"${dr_backup_receipt}\""
        )
    ]

    for variable, path in (
        ("gold_receipt", "_completion/property_gold_status/release-gate.json"),
        ("gold_latest_receipt", "_completion/property_gold_status/latest.json"),
        (
            "gold_root_latest_receipt",
            "_completion/propertyquarry-gold-status-latest.json",
        ),
        (
            "gold_notification_report",
            "_completion/property_gold_status/telegram-notify-report.json",
        ),
    ):
        assert f'{variable}="{path}"' in script
        assert f'"${{{variable}}}"' in cleanup
    assert 'rm -f -- "${target}"' in cleanup
    assert (
        script.index("\nclear_gold_publication\n")
        < script.index("scripts/propertyquarry_postgres_dr.py release-gate")
    )

    canonical_gold_targets = (
        gold_status._CANONICAL_GOLD_STATUS_RELEASE_GATE_PATH,
        *gold_status._CANONICAL_GOLD_STATUS_LATEST_PATHS,
    )
    assert set(canonical_gold_targets) == {
        "_completion/property_gold_status/release-gate.json",
        "_completion/property_gold_status/latest.json",
        "_completion/propertyquarry-gold-status-latest.json",
    }
    targets = (
        *canonical_gold_targets,
        "_completion/property_gold_status/telegram-notify-report.json",
    )
    for relative in targets:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"status":"pass"}\n', encoding="utf-8")
    variable_start = script.index('gold_receipt="_completion/')
    cleanup_script = script[variable_start : script.index("\nif [[ -z", variable_start)]
    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            cleanup_script + "\nclear_gold_publication\nexit 17\n",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 17
    assert result.stdout == ""
    assert result.stderr == ""
    assert all(not (tmp_path / relative).exists() for relative in targets)
