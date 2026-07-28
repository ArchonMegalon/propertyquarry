from __future__ import annotations

import errno
import importlib.util
import json
import subprocess
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


def test_generated_release_artifact_normalizer_preserves_release_identities() -> None:
    module = _load_module()
    head = {
        "source_binding": {"code_commit": "a" * 40},
        "browser_receipt_binding": {"git_blob_oid": "b" * 40},
        "artifact": {"sha256": "c" * 64, "size_bytes": 123},
        "required_test_sources": [{"git_blob_oid": "c" * 40}],
    }
    materialized = {
        "source_binding": {"code_commit": "d" * 40},
        "browser_receipt_binding": {"git_blob_oid": "e" * 40},
        "artifact": {"sha256": "f" * 64, "size_bytes": 456},
        "required_test_sources": [{"git_blob_oid": "c" * 40}],
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


def test_generated_release_artifact_exact_check_rejects_even_volatile_byte_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_load_head_bytes", lambda path, *, root: b'{"generated_at":"old"}\n')
    monkeypatch.setattr(
        module,
        "_load_worktree_bytes",
        lambda path, *, root: b'{"generated_at":"new"}\n',
    )

    failures = module._exact_artifact_failures(root=Path("/unused"))

    assert len(failures) == len(module.GENERATED_ARTIFACTS)
    assert all("exact byte drift after materialization" in failure for failure in failures)


def test_generated_release_artifact_main_never_invokes_git_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_exact_artifact_failures", lambda **kwargs: [])
    monkeypatch.setattr(module, "verify_release_manifest", lambda root: [])

    def forbidden_subprocess(*args: object, **kwargs: object) -> object:
        raise AssertionError("verification must not invoke a mutating git command")

    monkeypatch.setattr(module.subprocess, "run", forbidden_subprocess)

    assert module.main([]) == 0


def test_detached_materialization_does_not_touch_the_caller_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", repo.as_posix()], check=True)
    subprocess.run(
        ["git", "-C", repo.as_posix(), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", repo.as_posix(), "config", "user.name", "PropertyQuarry Test"],
        check=True,
    )
    for path in module.GENERATED_ARTIFACTS:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
    manifest = repo / module.RELEASE_MANIFEST_PATH
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("immutable manifest\n", encoding="utf-8")
    materializer = repo / "scripts" / "noop_materializer.py"
    materializer.parent.mkdir(parents=True, exist_ok=True)
    materializer.write_text("raise SystemExit(0)\n", encoding="utf-8")
    subprocess.run(["git", "-C", repo.as_posix(), "add", "."], check=True)
    subprocess.run(["git", "-C", repo.as_posix(), "commit", "-qm", "fixture"], check=True)
    before = {
        path: (repo / path).read_bytes()
        for path in (*module.GENERATED_ARTIFACTS, module.RELEASE_MANIFEST_PATH)
    }
    monkeypatch.setattr(module, "MATERIALIZER_SCRIPTS", (Path("scripts/noop_materializer.py"),))

    assert module._run_materializers_in_detached_worktree(root=repo) == []
    assert {
        path: (repo / path).read_bytes()
        for path in (*module.GENERATED_ARTIFACTS, module.RELEASE_MANIFEST_PATH)
    } == before
    assert subprocess.run(
        ["git", "-C", repo.as_posix(), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""
