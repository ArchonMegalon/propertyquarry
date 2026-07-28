from __future__ import annotations

import errno
import importlib.util
import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_generated_release_artifacts_clean.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("verify_generated_release_artifacts_clean", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "payload",
    (
        b'{"status":"blocked","status":"pass"}',
        b'{"status":NaN}',
        b'["pass"]',
        b'{"status":"pass"}\xff',
    ),
)
def test_generated_artifact_loader_rejects_ambiguous_or_noncanonical_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> None:
    module = _load_module()
    artifact = Path("artifact.json")
    (tmp_path / artifact).write_bytes(payload)
    monkeypatch.setattr(module, "ROOT", tmp_path)

    with pytest.raises(ValueError):
        module._load_worktree(artifact)


def test_generated_release_artifact_normalizer_ignores_host_runner_execution_fields() -> None:
    module = _load_module()
    head = {
        "status": "pass",
        "source_backed_journey_proof": {
            "as_of": "2026-05-31",
            "command": ".venv/bin/python -m pytest -q tests/test_product_browser_journeys.py",
            "cwd": "/docker/EA",
            "python_bin": ".venv/bin/python",
            "git_branch": "completion/absolute-product-finish",
            "output_excerpt": ["4 passed in 1.2s"],
            "exit_code": 0,
        },
    }
    hosted = {
        "status": "pass",
        "source_backed_journey_proof": {
            "as_of": "2026-06-01",
            "command": "/opt/hostedtoolcache/Python/3.12.*/bin/python -m pytest -q tests/test_product_browser_journeys.py",
            "cwd": "/home/runner/work/executive-assistant/executive-assistant",
            "python_bin": "/opt/hostedtoolcache/Python/3.12.*/bin/python",
            "git_branch": "main",
            "output_excerpt": ["4 passed in 1.0s"],
            "exit_code": 0,
        },
    }

    assert module._normalize(head) == module._normalize(hosted)


def test_generated_release_artifact_normalizer_preserves_semantic_status_drift() -> None:
    module = _load_module()

    assert module._normalize({"status": "pass"}) != module._normalize({"status": "blocked"})


def test_generated_release_artifact_comparison_preserves_json_scalar_types() -> None:
    module = _load_module()

    assert not module._json_values_equal(
        module._normalize({"version": 1}),
        module._normalize({"version": True}),
    )


def test_generated_release_artifact_normalizer_preserves_candidate_source_identity() -> None:
    module = _load_module()
    head = {
        "source_binding": {"code_commit": "a" * 40},
        "browser_receipt_binding": {"git_blob_oid": "b" * 40},
        "required_test_sources": [{"git_blob_oid": "c" * 40}],
    }
    materialized = {
        "source_binding": {"code_commit": "d" * 40},
        "browser_receipt_binding": {"git_blob_oid": "e" * 40},
        "required_test_sources": [{"git_blob_oid": "c" * 40}],
    }

    assert module._normalize(head) != module._normalize(materialized)


def test_generated_release_artifact_normalizer_ignores_only_browser_receipt_self_identity() -> None:
    module = _load_module()

    assert module._normalize(
        {
            "browser_receipt_binding": {
                "git_blob_oid": "a" * 40,
                "sha256": "c" * 64,
            }
        }
    ) == module._normalize(
        {
            "browser_receipt_binding": {
                "git_blob_oid": "b" * 40,
                "sha256": "d" * 64,
            }
        }
    )


def test_generated_release_artifact_normalizer_preserves_nested_source_binding() -> None:
    module = _load_module()
    head = {
        "provenance": {
            "source_binding": {
                "seed": {"sha256": "a" * 64},
            }
        }
    }
    materialized = {
        "provenance": {
            "source_binding": {
                "seed": {"sha256": "b" * 64},
            }
        }
    }

    assert module._normalize(head) != module._normalize(materialized)


def test_generated_release_artifact_normalizer_preserves_provenance_digest_drift() -> None:
    module = _load_module()
    head = {
        "release_truth_provenance": {
            "sha256": "a" * 64,
        }
    }
    materialized = {
        "release_truth_provenance": {
            "sha256": "b" * 64,
        }
    }

    assert module._normalize(head) != module._normalize(materialized)


def test_generated_release_artifact_normalizer_preserves_source_blob_drift() -> None:
    module = _load_module()
    head = {"required_test_sources": [{"git_blob_oid": "a" * 40}]}
    materialized = {"required_test_sources": [{"git_blob_oid": "b" * 40}]}

    assert module._normalize(head) != module._normalize(materialized)


def test_release_manifest_matches_complete_immutable_authority_envelope() -> None:
    module = _load_module()
    receipt = json.loads((ROOT / module.GENERATED_ARTIFACTS[0]).read_text(encoding="utf-8"))
    issues = module.verify_release_manifest(ROOT)

    if isinstance(receipt.get("source_binding"), dict):
        assert issues == []
    else:
        assert receipt["status"] == "blocked"
        assert receipt["source_binding"] is None
        assert issues == [
            "release authority receipt runtime commit SHA is missing or invalid",
            "release manifest authority field mismatches current evidence: release_commit_sha",
            "release manifest authority field mismatches current evidence: release_artifact_set",
            "release manifest authority field mismatches current evidence: release_label",
            "release manifest authority field mismatches current evidence: release_deployment_id",
            "release manifest authority field mismatches current evidence: release_generated_at",
        ]


def test_release_manifest_authority_fails_closed_on_missing_and_mismatched_fields() -> None:
    module = _load_module()
    expected = {
        "release_repository": "ArchonMegalon/property",
        "release_mirror_repository": "ArchonMegalon/propertyquarry",
        "release_commit_sha": "a" * 40,
    }
    observed = {
        "release_repository": "wrong/repository",
        "release_commit_sha": "a" * 40,
        "unreviewed_field": "unexpected",
    }

    assert module._validate_release_manifest_values(observed, expected) == [
        "release manifest authority field mismatches current evidence: release_repository",
        "release manifest authority field is missing: release_mirror_repository",
        "release manifest authority field is unexpected: unreviewed_field",
    ]


def _manifest_values(module: Any) -> dict[str, str]:
    values = dict(module.RELEASE_MANIFEST_STATIC_VALUES)
    values.update(
        {
            "release_commit_sha": "a" * 40,
            "release_artifact_set": (
                module.RELEASE_ARTIFACT_SET_PREFIX + "b" * 64
            ),
            "release_label": "propertyquarry-source-browser-candidate-aaaaaaaaaaaa",
            "release_generated_at": "2026-07-16T14:30:00Z",
            "release_deployment_id": "propertyquarry-governed-deploy-aaaaaaaaaaaa",
        }
    )
    return {field: values[field] for field in module.RELEASE_MANIFEST_FIELDS}


def _manifest_document(module: Any, body: str) -> str:
    return (
        "# Release manifest\n\n"
        f"{module.RELEASE_MANIFEST_JSON_START}\n"
        "```json\n"
        f"{body}\n"
        "```\n"
        f"{module.RELEASE_MANIFEST_JSON_END}\n"
    )


def test_release_manifest_parser_rejects_duplicate_authority_fields() -> None:
    module = _load_module()
    values, issues = module._parse_release_manifest(
        _manifest_document(
            module,
            '{"release_product":"PropertyQuarry",'
            '"release_product":"Duplicate"}',
        )
    )

    assert values == {}
    assert issues == ["release manifest authority field is duplicated: release_product"]


@pytest.mark.parametrize(
    ("mutation", "error_fragment"),
    (
        ("missing", "authority field is missing: release_product"),
        ("unexpected", "authority field is unexpected: unreviewed_field"),
        ("non_string", "authority field must be a string: release_product"),
        (
            "surrounding_whitespace",
            "authority field contains surrounding whitespace: release_product",
        ),
    ),
)
def test_release_manifest_loader_rejects_non_exact_authority_shape(
    tmp_path: Path,
    mutation: str,
    error_fragment: str,
) -> None:
    module = _load_module()
    values: dict[str, object] = _manifest_values(module)
    if mutation == "missing":
        values.pop("release_product")
    elif mutation == "unexpected":
        values["unreviewed_field"] = "unexpected"
    elif mutation == "surrounding_whitespace":
        values["release_product"] = " PropertyQuarry"
    else:
        values["release_product"] = 1
    path = tmp_path / "release-manifest.md"
    path.write_text(
        _manifest_document(module, json.dumps(values, sort_keys=True)),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=error_fragment):
        module.load_release_manifest(path)


def test_release_manifest_parser_rejects_reversed_authority_markers() -> None:
    module = _load_module()

    values, issues = module._parse_release_manifest(
        f"{module.RELEASE_MANIFEST_JSON_END}\n"
        f"{module.RELEASE_MANIFEST_JSON_START}\n"
    )

    assert values == {}
    assert issues == ["release manifest canonical JSON markers are out of order"]


def test_release_manifest_loader_and_digest_use_the_same_canonical_object(
    tmp_path: Path,
) -> None:
    module = _load_module()
    values = _manifest_values(module)
    path = tmp_path / "release-manifest.md"
    path.write_text(
        _manifest_document(module, json.dumps(values, indent=2, sort_keys=False)),
        encoding="utf-8",
    )

    loaded = module.load_release_manifest(path)
    reordered = dict(reversed(tuple(loaded.items())))
    changed = {**loaded, "release_label": loaded["release_label"] + "-changed"}

    assert loaded == values
    assert module.release_manifest_sha256(reordered) == module.release_manifest_sha256(
        loaded
    )
    assert module.release_manifest_sha256(changed) != module.release_manifest_sha256(
        loaded
    )


def test_release_manifest_loader_fails_closed_on_invalid_utf8(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "release-manifest.md"
    path.write_bytes(b"\xff")

    with pytest.raises(ValueError, match="missing or unreadable: UnicodeDecodeError"):
        module.load_release_manifest(path)


def test_release_artifact_set_identity_changes_when_any_member_changes(tmp_path: Path) -> None:
    module = _load_module()
    for index, relative_path in enumerate(module.GENERATED_ARTIFACTS):
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"artifact-{index}".encode("utf-8"))

    initial = module._release_artifact_set_identity(tmp_path)
    changed_path = tmp_path / module.GENERATED_ARTIFACTS[-1]
    changed_path.write_bytes(b"changed")

    assert initial.startswith(module.RELEASE_ARTIFACT_SET_PREFIX)
    assert module._release_artifact_set_identity(tmp_path) != initial


@pytest.mark.parametrize(
    ("code_commit", "generated_at", "expected_issue"),
    (
        (
            int("1" * 40),
            "2026-07-16T14:30:00Z",
            "release authority receipt runtime commit SHA is missing or invalid",
        ),
        (
            "a" * 40,
            "2026-99-99T99:99:99Z",
            "release authority receipt generated_at is missing or not UTC RFC3339 seconds",
        ),
    ),
)
def test_release_manifest_authority_rejects_typed_or_impossible_receipt_identity(
    tmp_path: Path,
    code_commit: object,
    generated_at: str,
    expected_issue: str,
) -> None:
    module = _load_module()
    for index, relative_path in enumerate(module.GENERATED_ARTIFACTS):
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {"status": "pass"}
        if index == 0:
            payload.update(
                {
                    "generated_at": generated_at,
                    "source_binding": {"code_commit": code_commit},
                }
            )
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    _expected, issues = module._release_manifest_expected_values(tmp_path)

    assert expected_issue in issues


def _initialize_generated_artifact_repository(module: Any, root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Generated Artifact Test"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "generated@example.test"],
        cwd=root,
        check=True,
    )
    for path in module.GENERATED_ARTIFACTS:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "status": "pass",
                    "generated_at": "2026-07-16T14:30:00Z",
                    "source_binding": {
                        "version": 1,
                        "code_commit": "a" * 40,
                        "seed": {"sha256": "b" * 64},
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        target.chmod(0o644)
    manifest = root / module.RELEASE_MANIFEST_PATH
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("release manifest baseline\n", encoding="utf-8")
    manifest.chmod(0o644)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "generated baseline"], cwd=root, check=True)


def _filesystem_snapshot(path: Path) -> tuple[int, int, int, int, int, int, bytes]:
    metadata = path.lstat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        path.read_bytes(),
    )


def test_generated_release_verifier_is_read_only_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    _initialize_generated_artifact_repository(module, tmp_path)
    changed_path = tmp_path / module.GENERATED_ARTIFACTS[0]
    payload = json.loads(changed_path.read_text(encoding="utf-8"))
    payload["generated_at"] = "2026-07-17T14:30:00Z"
    changed_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    changed_path.chmod(0o666)
    artifact_paths = tuple(tmp_path / path for path in module.GENERATED_ARTIFACTS)
    parent_paths = tuple(dict.fromkeys(path.parent for path in artifact_paths))
    artifacts_before = {
        path: _filesystem_snapshot(path)
        for path in artifact_paths
    }
    parents_before = {
        path: path.lstat()
        for path in parent_paths
    }
    monkeypatch.setattr(module, "ROOT", tmp_path)

    assert module.main() == 1

    captured = capsys.readouterr()
    assert "exact HEAD bytes and mode drift; verification is read-only" in (
        captured.err
    )
    assert {
        path: _filesystem_snapshot(path)
        for path in artifact_paths
    } == artifacts_before
    for path, before in parents_before.items():
        after = path.lstat()
        assert (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) == (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )


def test_generated_release_verifier_rejects_rebound_candidate_without_restoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    _initialize_generated_artifact_repository(module, tmp_path)
    for path in module.GENERATED_ARTIFACTS:
        target = tmp_path / path
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["source_binding"]["code_commit"] = "c" * 40
        target.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)

    assert module.main() == 1
    for path in module.GENERATED_ARTIFACTS:
        payload = json.loads((tmp_path / path).read_text(encoding="utf-8"))
        assert payload["source_binding"]["code_commit"] == "c" * 40


def test_generated_release_verifier_defers_restore_until_manifest_checks_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    _initialize_generated_artifact_repository(module, tmp_path)
    changed_path = tmp_path / module.GENERATED_ARTIFACTS[0]
    payload = json.loads(changed_path.read_text(encoding="utf-8"))
    payload["generated_at"] = "2026-07-17T14:30:00Z"
    changed_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(
        module,
        "verify_release_manifest",
        lambda *args, **kwargs: ["manifest binding failed"],
    )

    assert module.main(["--restore-exact-head"]) == 1
    observed = json.loads(changed_path.read_text(encoding="utf-8"))
    assert observed["generated_at"] == "2026-07-17T14:30:00Z"


@pytest.mark.parametrize(
    "drift_kind",
    ("staged", "worktree", "mode", "unstaged_mode"),
)
def test_generated_release_verifier_rejects_manifest_checkout_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift_kind: str,
) -> None:
    module = _load_module()
    _initialize_generated_artifact_repository(module, tmp_path)
    manifest = tmp_path / module.RELEASE_MANIFEST_PATH
    if drift_kind in {"mode", "unstaged_mode"}:
        manifest.chmod(manifest.stat().st_mode | 0o100)
        if drift_kind == "mode":
            subprocess.run(
                ["git", "add", module.RELEASE_MANIFEST_PATH.as_posix()],
                cwd=tmp_path,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "restore",
                    "--source=HEAD",
                    "--worktree",
                    "--",
                    module.RELEASE_MANIFEST_PATH.as_posix(),
                ],
                cwd=tmp_path,
                check=True,
            )
    else:
        manifest.write_text("misleading release manifest drift\n", encoding="utf-8")
    if drift_kind == "staged":
        subprocess.run(
            ["git", "add", module.RELEASE_MANIFEST_PATH.as_posix()],
            cwd=tmp_path,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "restore",
                "--source=HEAD",
                "--worktree",
                "--",
                module.RELEASE_MANIFEST_PATH.as_posix(),
            ],
            cwd=tmp_path,
            check=True,
        )
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "verify_release_manifest", lambda *args, **kwargs: [])

    assert module.main() == 1


def test_generated_release_verifier_restores_exact_nonexecutable_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    _initialize_generated_artifact_repository(module, tmp_path)
    changed_path = tmp_path / module.GENERATED_ARTIFACTS[0]
    changed_path.chmod(0o666)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "verify_release_manifest", lambda *args, **kwargs: [])

    assert module.main(["--restore-exact-head"]) == 0
    assert stat.S_IMODE(changed_path.stat().st_mode) == 0o644


def test_generated_release_verifier_rejects_staged_semantic_drift_without_restoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    _initialize_generated_artifact_repository(module, tmp_path)
    changed_path = tmp_path / module.GENERATED_ARTIFACTS[0]
    payload = json.loads(changed_path.read_text(encoding="utf-8"))
    payload["status"] = "blocked"
    changed_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "--", module.GENERATED_ARTIFACTS[0].as_posix()],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "restore",
            "--source=HEAD",
            "--worktree",
            "--",
            module.GENERATED_ARTIFACTS[0].as_posix(),
        ],
        cwd=tmp_path,
        check=True,
    )
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "verify_release_manifest", lambda *args, **kwargs: [])

    assert module.main() == 1
    observed = json.loads(changed_path.read_text(encoding="utf-8"))
    assert observed["status"] == "pass"


def test_generated_release_verifier_rejects_staged_mode_only_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    _initialize_generated_artifact_repository(module, tmp_path)
    changed_path = tmp_path / module.GENERATED_ARTIFACTS[0]
    changed_path.chmod(changed_path.stat().st_mode | 0o100)
    subprocess.run(
        ["git", "add", "--", module.GENERATED_ARTIFACTS[0].as_posix()],
        cwd=tmp_path,
        check=True,
    )
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "verify_release_manifest", lambda *args, **kwargs: [])

    assert module.main() == 1
    staged_summary = subprocess.run(
        ["git", "diff", "--cached", "--summary"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "mode change 100644 => 100755" in staged_summary


def test_generated_release_verifier_rejects_symlinked_artifact_without_following_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    _initialize_generated_artifact_repository(module, tmp_path)
    changed_path = tmp_path / module.GENERATED_ARTIFACTS[0]
    victim = tmp_path / "outside-generated-artifacts.json"
    victim.write_bytes(changed_path.read_bytes())
    changed_path.unlink()
    changed_path.symlink_to(victim)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "verify_release_manifest", lambda *args, **kwargs: [])

    assert module.main() == 1
    assert changed_path.is_symlink()
    assert victim.read_bytes() == subprocess.run(
        ["git", "show", f"HEAD:{module.GENERATED_ARTIFACTS[0].as_posix()}"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    ).stdout


@pytest.mark.parametrize("filter_name", ("sentinel", "unspecified", "unset"))
def test_generated_release_verifier_restores_without_executing_checkout_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filter_name: str,
) -> None:
    module = _load_module()
    _initialize_generated_artifact_repository(module, tmp_path)
    attributes = tmp_path / ".gitattributes"
    attributes.write_text(
        f".codex-design/product/*.json filter={filter_name}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", ".gitattributes"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add generated artifact filter"],
        cwd=tmp_path,
        check=True,
    )
    sentinel = tmp_path / "checkout-filter-executed"
    driver = tmp_path / "checkout_filter.py"
    driver.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        f"Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    filter_command = f"{shlex.quote(sys.executable)} {shlex.quote(str(driver))}"
    subprocess.run(
        ["git", "config", f"filter.{filter_name}.smudge", filter_command],
        cwd=tmp_path,
        check=True,
    )
    changed_path = tmp_path / module.GENERATED_ARTIFACTS[0]
    payload = json.loads(changed_path.read_text(encoding="utf-8"))
    payload["generated_at"] = "2026-07-17T14:30:00Z"
    changed_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "verify_release_manifest", lambda *args, **kwargs: [])

    assert module.main(["--restore-exact-head"]) == 0
    assert not sentinel.exists()
    observed = json.loads(changed_path.read_text(encoding="utf-8"))
    assert observed["generated_at"] == "2026-07-16T14:30:00Z"


def test_generated_release_verifier_ignores_ambient_git_repository_redirection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    target = tmp_path / "target"
    decoy = tmp_path / "decoy"
    target.mkdir()
    decoy.mkdir()
    _initialize_generated_artifact_repository(module, target)
    _initialize_generated_artifact_repository(module, decoy)

    changed_path = target / module.GENERATED_ARTIFACTS[0]
    payload = json.loads(changed_path.read_text(encoding="utf-8"))
    payload["status"] = "blocked"
    changed_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "--", module.GENERATED_ARTIFACTS[0].as_posix()],
        cwd=target,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "restore",
            "--source=HEAD",
            "--worktree",
            "--",
            module.GENERATED_ARTIFACTS[0].as_posix(),
        ],
        cwd=target,
        check=True,
    )

    monkeypatch.setattr(module, "ROOT", target)
    monkeypatch.setattr(module, "verify_release_manifest", lambda *args, **kwargs: [])
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy / ".git" / "index"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")

    assert module.main() == 1


def test_generated_release_verifier_pins_worktree_against_local_git_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    target = tmp_path / "target"
    decoy = tmp_path / "decoy"
    target.mkdir()
    decoy.mkdir()
    _initialize_generated_artifact_repository(module, target)
    subprocess.run(
        ["git", "config", "core.worktree", str(decoy)],
        cwd=target,
        check=True,
    )

    changed_path = target / module.GENERATED_ARTIFACTS[0]
    payload = json.loads(changed_path.read_text(encoding="utf-8"))
    payload["generated_at"] = "2026-07-17T14:30:00Z"
    changed_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", target)
    monkeypatch.setattr(module, "verify_release_manifest", lambda *args, **kwargs: [])

    assert module.main(["--restore-exact-head"]) == 0
    restored = json.loads(changed_path.read_text(encoding="utf-8"))
    assert restored["generated_at"] == "2026-07-16T14:30:00Z"
    assert not tuple(decoy.iterdir())


@pytest.mark.parametrize("mutation", ("in_place", "replacement"))
def test_generated_release_verifier_refuses_concurrent_edit_after_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    module = _load_module()
    _initialize_generated_artifact_repository(module, tmp_path)
    changed_path = tmp_path / module.GENERATED_ARTIFACTS[0]
    approved = json.loads(changed_path.read_text(encoding="utf-8"))
    approved["generated_at"] = "2026-07-17T14:30:00Z"
    changed_path.write_text(json.dumps(approved) + "\n", encoding="utf-8")

    concurrent = dict(approved)
    concurrent["status"] = "concurrent-operator-edit"
    concurrent_bytes = (json.dumps(concurrent) + "\n").encode("utf-8")

    def mutate_after_approval(*_args: object, **_kwargs: object) -> list[str]:
        if mutation == "in_place":
            changed_path.write_bytes(concurrent_bytes)
        else:
            replacement = changed_path.with_name(changed_path.name + ".replacement")
            replacement.write_bytes(concurrent_bytes)
            replacement.replace(changed_path)
        return []

    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(
        module,
        "verify_release_manifest",
        mutate_after_approval,
    )

    assert module.main(["--restore-exact-head"]) == 1
    assert changed_path.read_bytes() == concurrent_bytes


@pytest.mark.parametrize("mutation", ("in_place", "replacement"))
def test_generated_release_verifier_preserves_edit_at_restore_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    module = _load_module()
    _initialize_generated_artifact_repository(module, tmp_path)
    changed_path = tmp_path / module.GENERATED_ARTIFACTS[0]
    approved = json.loads(changed_path.read_text(encoding="utf-8"))
    approved["generated_at"] = "2026-07-17T14:30:00Z"
    changed_path.write_text(json.dumps(approved) + "\n", encoding="utf-8")
    concurrent_bytes = b'{"status":"concurrent-at-restore-exchange"}\n'
    original_exchange = module._rename_exchange
    exchange_calls = 0

    def exchange_with_destination_edit(
        parent_fd: int,
        source: str,
        destination_name: str,
    ) -> None:
        nonlocal exchange_calls
        exchange_calls += 1
        if exchange_calls == 1:
            if mutation == "in_place":
                changed_path.write_bytes(concurrent_bytes)
            else:
                replacement = changed_path.with_name(
                    changed_path.name + ".operator-replacement"
                )
                replacement.write_bytes(concurrent_bytes)
                replacement.replace(changed_path)
        original_exchange(parent_fd, source, destination_name)

    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "verify_release_manifest", lambda *args, **kwargs: [])
    monkeypatch.setattr(module, "_rename_exchange", exchange_with_destination_edit)

    assert module.main(["--restore-exact-head"]) == 1
    assert changed_path.read_bytes() == concurrent_bytes


def test_generated_release_verifier_detects_restore_staging_path_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    _initialize_generated_artifact_repository(module, tmp_path)
    changed_path = tmp_path / module.GENERATED_ARTIFACTS[0]
    approved = json.loads(changed_path.read_text(encoding="utf-8"))
    approved["generated_at"] = "2026-07-17T14:30:00Z"
    changed_path.write_text(json.dumps(approved) + "\n", encoding="utf-8")
    approved_bytes = changed_path.read_bytes()
    substituted_bytes = b'{"status":"substituted-restore-staging-path"}\n'
    original_exchange = module._rename_exchange
    exchange_calls = 0

    def exchange_with_staged_substitution(
        parent_fd: int,
        source: str,
        destination_name: str,
    ) -> None:
        nonlocal exchange_calls
        exchange_calls += 1
        if exchange_calls == 1:
            replacement = changed_path.parent / "restore-staging-substitute.json"
            replacement.write_bytes(substituted_bytes)
            replacement.replace(changed_path.parent / source)
        original_exchange(parent_fd, source, destination_name)

    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "verify_release_manifest", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        module,
        "_rename_exchange",
        exchange_with_staged_substitution,
    )

    assert module.main(["--restore-exact-head"]) == 1
    assert changed_path.read_bytes() == approved_bytes
    recovery = [
        path
        for path in changed_path.parent.iterdir()
        if path.name.startswith(f".{changed_path.name}.release-restore-")
    ]
    assert [path.read_bytes() for path in recovery] == [substituted_bytes]


def test_generated_release_verifier_rolls_back_restore_staging_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    _initialize_generated_artifact_repository(module, tmp_path)
    changed_path = tmp_path / module.GENERATED_ARTIFACTS[0]
    approved = json.loads(changed_path.read_text(encoding="utf-8"))
    approved["generated_at"] = "2026-07-17T14:30:00Z"
    changed_path.write_text(json.dumps(approved) + "\n", encoding="utf-8")
    approved_bytes = changed_path.read_bytes()
    victim = changed_path.parent / "restore-symlink-victim.json"
    victim_bytes = b'{"preserve":"restore-symlink-victim"}\n'
    victim.write_bytes(victim_bytes)
    original_exchange = module._rename_exchange
    exchange_calls = 0

    def exchange_with_staged_symlink(
        parent_fd: int,
        source: str,
        destination_name: str,
    ) -> None:
        nonlocal exchange_calls
        exchange_calls += 1
        if exchange_calls == 1:
            staged_path = changed_path.parent / source
            staged_path.unlink()
            staged_path.symlink_to(victim.name)
        original_exchange(parent_fd, source, destination_name)

    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "verify_release_manifest", lambda *args, **kwargs: [])
    monkeypatch.setattr(module, "_rename_exchange", exchange_with_staged_symlink)

    assert module.main(["--restore-exact-head"]) == 1
    assert not changed_path.is_symlink()
    assert changed_path.read_bytes() == approved_bytes
    assert victim.read_bytes() == victim_bytes
    recovery = [
        path
        for path in changed_path.parent.iterdir()
        if path.name.startswith(f".{changed_path.name}.release-restore-")
    ]
    assert len(recovery) == 1
    assert recovery[0].is_symlink()
    assert recovery[0].readlink() == Path(victim.name)


def test_generated_release_verifier_preserves_rollback_entry_after_identity_eio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    _initialize_generated_artifact_repository(module, tmp_path)
    changed_path = tmp_path / module.GENERATED_ARTIFACTS[0]
    approved = json.loads(changed_path.read_text(encoding="utf-8"))
    approved["generated_at"] = "2026-07-17T14:30:00Z"
    changed_path.write_text(json.dumps(approved) + "\n", encoding="utf-8")
    approved_bytes = changed_path.read_bytes()
    victim = changed_path.parent / "restore-symlink-victim.json"
    victim_bytes = b'{"preserve":"restore-symlink-victim"}\n'
    victim.write_bytes(victim_bytes)
    original_exchange = module._rename_exchange
    exchange_calls = 0
    opened_parent_fds: list[int] = []
    original_open_parent = module._open_checkout_parent

    def capture_parent_fd(root: Path, path: Path) -> int:
        parent_fd = original_open_parent(root, path)
        opened_parent_fds.append(parent_fd)
        return parent_fd

    def exchange_with_staged_symlink(
        parent_fd: int,
        source: str,
        destination_name: str,
    ) -> None:
        nonlocal exchange_calls
        exchange_calls += 1
        if exchange_calls == 1:
            staged_path = changed_path.parent / source
            staged_path.unlink()
            staged_path.symlink_to(victim.name)
        original_exchange(parent_fd, source, destination_name)

    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "verify_release_manifest", lambda *args, **kwargs: [])
    monkeypatch.setattr(module, "_open_checkout_parent", capture_parent_fd)
    monkeypatch.setattr(module, "_rename_exchange", exchange_with_staged_symlink)
    monkeypatch.setattr(
        module,
        "_entry_identity_no_follow",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(errno.EIO, "injected post-rollback identity failure")
        ),
    )

    assert module.main(["--restore-exact-head"]) == 1
    assert not changed_path.is_symlink()
    assert changed_path.read_bytes() == approved_bytes
    assert victim.read_bytes() == victim_bytes
    recovery = [
        path
        for path in changed_path.parent.iterdir()
        if path.name.startswith(f".{changed_path.name}.release-restore-")
    ]
    assert len(recovery) == 1
    assert recovery[0].is_symlink()
    assert recovery[0].readlink() == Path(victim.name)
    assert opened_parent_fds
    for parent_fd in opened_parent_fds:
        with pytest.raises(OSError):
            os.fstat(parent_fd)


def test_generated_release_verifier_preserves_displaced_bytes_when_symlink_rollback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    _initialize_generated_artifact_repository(module, tmp_path)
    changed_path = tmp_path / module.GENERATED_ARTIFACTS[0]
    approved = json.loads(changed_path.read_text(encoding="utf-8"))
    approved["generated_at"] = "2026-07-17T14:30:00Z"
    changed_path.write_text(json.dumps(approved) + "\n", encoding="utf-8")
    approved_bytes = changed_path.read_bytes()
    victim = changed_path.parent / "restore-symlink-victim.json"
    victim_bytes = b'{"preserve":"restore-symlink-victim"}\n'
    victim.write_bytes(victim_bytes)
    original_exchange = module._rename_exchange
    exchange_calls = 0

    def exchange_with_failed_symlink_rollback(
        parent_fd: int,
        source: str,
        destination_name: str,
    ) -> None:
        nonlocal exchange_calls
        exchange_calls += 1
        if exchange_calls == 1:
            staged_path = changed_path.parent / source
            staged_path.unlink()
            staged_path.symlink_to(victim.name)
            original_exchange(parent_fd, source, destination_name)
            return
        raise OSError(errno.EIO, "injected symlink rollback failure")

    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "verify_release_manifest", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        module,
        "_rename_exchange",
        exchange_with_failed_symlink_rollback,
    )

    assert module.main(["--restore-exact-head"]) == 1
    assert changed_path.is_symlink()
    assert changed_path.readlink() == Path(victim.name)
    assert victim.read_bytes() == victim_bytes
    recovery = [
        path
        for path in changed_path.parent.iterdir()
        if path.name.startswith(f".{changed_path.name}.release-restore-")
    ]
    assert [path.read_bytes() for path in recovery] == [approved_bytes]


def test_generated_release_verifier_preserves_displaced_data_if_rollback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    _initialize_generated_artifact_repository(module, tmp_path)
    artifact_path = module.GENERATED_ARTIFACTS[0]
    changed_path = tmp_path / artifact_path
    approved = json.loads(changed_path.read_text(encoding="utf-8"))
    approved["generated_at"] = "2026-07-17T14:30:00Z"
    changed_path.write_text(json.dumps(approved) + "\n", encoding="utf-8")
    head_bytes = subprocess.run(
        ["git", "show", f"HEAD:{artifact_path.as_posix()}"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    ).stdout
    concurrent_bytes = b'{"status":"concurrent-before-failed-rollback"}\n'
    original_exchange = module._rename_exchange
    exchange_calls = 0

    def exchange_with_failed_rollback(
        parent_fd: int,
        source: str,
        destination_name: str,
    ) -> None:
        nonlocal exchange_calls
        exchange_calls += 1
        if exchange_calls == 1:
            changed_path.write_bytes(concurrent_bytes)
            original_exchange(parent_fd, source, destination_name)
            return
        raise OSError(errno.EIO, "injected exchange rollback failure")

    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "verify_release_manifest", lambda *args, **kwargs: [])
    monkeypatch.setattr(module, "_rename_exchange", exchange_with_failed_rollback)

    assert module.main(["--restore-exact-head"]) == 1
    assert changed_path.read_bytes() == head_bytes
    recovery = [
        path
        for path in changed_path.parent.iterdir()
        if path.name.startswith(f".{changed_path.name}.release-restore-")
    ]
    assert [path.read_bytes() for path in recovery] == [concurrent_bytes]
