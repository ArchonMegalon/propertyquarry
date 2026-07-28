from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import propertyquarry_release_receipt_binding as receipt_binding
from scripts.propertyquarry_release_receipt_binding import ReleaseBindingError
from scripts.propertyquarry_release_receipt_binding import build_source_binding


SEED = Path(".codex-design/repo/EA_FLAGSHIP_RELEASE_GATE.json")
SOURCE_CASES = (
    Path("tests/test_propertyquarry_workspace_redesign.py"),
    Path("tests/e2e/test_propertyquarry_greenfield_browser.py"),
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write(root: Path, path: Path | str, text: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _commit(root: Path, message: str) -> str:
    _git(root, "add", ".")
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _initialize_repository(root: Path) -> tuple[list[dict[str, object]], str]:
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Receipt Test")
    _git(root, "config", "user.email", "receipt@example.test")
    evidence_sources = [
        {"file": path.as_posix(), "cases": [f"case_{index}"]}
        for index, path in enumerate(SOURCE_CASES, start=1)
    ]
    _write(
        root,
        SEED,
        json.dumps({"browser_workflow_proof": {"evidence_sources": evidence_sources}}) + "\n",
    )
    for path in SOURCE_CASES:
        _write(root, path, f"# {path.name}\n")
    _write(root, "app.txt", "source-v1\n")
    return evidence_sources, _commit(root, "initial source")


def _binding(root: Path, evidence_sources: list[dict[str, object]]) -> dict[str, object]:
    return build_source_binding(
        root,
        seed_path=SEED,
        evidence_sources=evidence_sources,
    )


def test_file_snapshot_binding_derives_digests_from_one_stable_read(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "-b", "main")
    artifact = Path("evidence/receipt.json")
    _write(tmp_path, artifact, '{"generation":"one"}\n')

    snapshot, binding = receipt_binding.file_snapshot_binding(
        tmp_path,
        artifact,
    )

    assert snapshot.payload == b'{"generation":"one"}\n'
    assert binding == {
        "path": artifact.as_posix(),
        "sha256": receipt_binding.sha256_bytes(snapshot.payload),
        "git_blob_oid": receipt_binding.git_blob_oid_bytes(
            tmp_path,
            snapshot.payload,
        ),
    }
    snapshot.assert_unchanged()


def test_file_snapshot_binding_rejects_replacement_during_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _git(tmp_path, "init", "-b", "main")
    artifact = Path("evidence/receipt.json")
    target = tmp_path / artifact
    _write(tmp_path, artifact, '{"generation":"one"}\n')
    original_git_blob_oid_bytes = receipt_binding.git_blob_oid_bytes

    def replace_during_digest(root: Path, payload: bytes) -> str:
        replacement = target.with_name("replacement.json")
        replacement.write_text(
            '{"generation":"two"}\n',
            encoding="utf-8",
        )
        replacement.replace(target)
        return original_git_blob_oid_bytes(root, payload)

    monkeypatch.setattr(
        receipt_binding,
        "git_blob_oid_bytes",
        replace_during_digest,
    )

    with pytest.raises(
        ReleaseBindingError,
        match="changed after it was read",
    ):
        receipt_binding.file_snapshot_binding(tmp_path, artifact)


def test_source_binding_walks_consecutive_metadata_only_refresh_commits(tmp_path: Path) -> None:
    evidence_sources, initial = _initialize_repository(tmp_path)
    assert _binding(tmp_path, evidence_sources)["code_commit"] == initial

    _write(tmp_path, "app.txt", "source-v2\n")
    source_commit = _commit(tmp_path, "change source")
    assert _binding(tmp_path, evidence_sources)["code_commit"] == source_commit

    metadata_paths = (
        ".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json",
        ".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json",
        ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json",
        "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md",
    )
    for path in metadata_paths:
        _write(tmp_path, path, f"metadata for {source_commit}\n")
    metadata_commit = _commit(tmp_path, "refresh release metadata")
    assert _binding(tmp_path, evidence_sources)["code_commit"] == source_commit
    assert build_source_binding(
        tmp_path,
        seed_path=SEED,
        evidence_sources=evidence_sources,
        code_commit=metadata_commit,
    )["code_commit"] == source_commit

    _write(tmp_path, metadata_paths[1], "second metadata refresh\n")
    _commit(tmp_path, "refresh pulse metadata")
    assert _binding(tmp_path, evidence_sources)["code_commit"] == source_commit


def test_source_binding_does_not_hide_evidence_changes_in_metadata_commit(tmp_path: Path) -> None:
    evidence_sources, _initial = _initialize_repository(tmp_path)
    _write(tmp_path, SOURCE_CASES[0], "# changed evidence source\n")
    _write(
        tmp_path,
        ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json",
        "metadata refresh\n",
    )
    evidence_commit = _commit(tmp_path, "change evidence and metadata")

    binding = _binding(tmp_path, evidence_sources)
    assert binding["code_commit"] == evidence_commit
    assert binding["required_test_sources"][0]["git_blob_oid"] == _git(
        tmp_path,
        "rev-parse",
        f"{evidence_commit}:{SOURCE_CASES[0].as_posix()}",
    )


def test_source_binding_rejects_dirty_source_outside_generated_evidence(
    tmp_path: Path,
) -> None:
    evidence_sources, _initial = _initialize_repository(tmp_path)
    _write(tmp_path, "app.txt", "uncommitted-runtime-change\n")

    with pytest.raises(
        ReleaseBindingError,
        match="release source worktree differs from the immutable candidate",
    ):
        _binding(tmp_path, evidence_sources)


def test_source_binding_rejects_untracked_source_outside_generated_evidence(
    tmp_path: Path,
) -> None:
    evidence_sources, _initial = _initialize_repository(tmp_path)
    _write(tmp_path, "app/new_runtime.py", "NEW_RUNTIME = True\n")

    with pytest.raises(ReleaseBindingError, match="untracked=.*app/new_runtime.py"):
        _binding(tmp_path, evidence_sources)


@pytest.mark.parametrize("index_flag", ("--assume-unchanged", "--skip-worktree"))
def test_source_binding_rejects_hidden_dirty_index_entries(
    tmp_path: Path,
    index_flag: str,
) -> None:
    evidence_sources, _initial = _initialize_repository(tmp_path)
    _git(tmp_path, "update-index", index_flag, "app.txt")
    _write(tmp_path, "app.txt", "hidden-uncommitted-runtime-change\n")

    with pytest.raises(ReleaseBindingError, match="hidden tracked release source"):
        _binding(tmp_path, evidence_sources)


def test_source_binding_ignores_ambient_git_repository_redirection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    evidence_sources, _initial = _initialize_repository(candidate)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _initialize_repository(decoy)
    _write(candidate, "app.txt", "dirty candidate hidden by ambient Git redirect\n")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy / ".git" / "index"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")

    with pytest.raises(
        ReleaseBindingError,
        match="release source worktree differs from the immutable candidate",
    ):
        _binding(candidate, evidence_sources)


def test_source_binding_allows_only_explicit_generated_evidence_drift(
    tmp_path: Path,
) -> None:
    evidence_sources, initial = _initialize_repository(tmp_path)
    _write(
        tmp_path,
        ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json",
        '{"status":"pass"}\n',
    )

    assert _binding(tmp_path, evidence_sources)["code_commit"] == initial


def test_source_binding_rejects_explicit_commit_for_an_older_source_candidate(
    tmp_path: Path,
) -> None:
    evidence_sources, initial = _initialize_repository(tmp_path)
    _write(tmp_path, "app.txt", "source-v2\n")
    _commit(tmp_path, "change source")

    with pytest.raises(ReleaseBindingError, match="does not identify the current candidate"):
        build_source_binding(
            tmp_path,
            seed_path=SEED,
            evidence_sources=evidence_sources,
            code_commit=initial,
        )


def test_source_binding_rejects_ambiguous_committed_seed_json(tmp_path: Path) -> None:
    evidence_sources, _initial = _initialize_repository(tmp_path)
    encoded_sources = json.dumps(evidence_sources)
    _write(
        tmp_path,
        SEED,
        (
            '{"browser_workflow_proof":{"evidence_sources":[]},'
            f'"browser_workflow_proof":{{"evidence_sources":{encoded_sources}}}}}\n'
        ),
    )
    _commit(tmp_path, "ambiguous seed")

    with pytest.raises(ReleaseBindingError, match="committed flagship seed is invalid JSON"):
        _binding(tmp_path, evidence_sources)


def test_source_binding_rejects_shallow_metadata_history(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    evidence_sources, source_commit = _initialize_repository(source)
    _write(
        source,
        ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json",
        f"metadata for {source_commit}\n",
    )
    _commit(source, "refresh release metadata")

    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "clone", "--depth", "1", source.resolve().as_uri(), str(checkout)],
        check=True,
        capture_output=True,
        text=True,
    )

    with pytest.raises(ReleaseBindingError, match="ancestry is shallow"):
        _binding(checkout, evidence_sources)
