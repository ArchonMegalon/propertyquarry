from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import propertyquarry_release_proof_baseline as release_proof_baseline
from scripts import propertyquarry_release_receipt_binding as receipt_binding
from scripts.propertyquarry_release_receipt_binding import ReleaseBindingError
from scripts.propertyquarry_release_receipt_binding import build_source_binding


SEED = Path(".codex-design/repo/EA_FLAGSHIP_RELEASE_GATE.json")
ROOT = Path(__file__).resolve().parents[1]
SOURCE_CASES = tuple(
    Path(str(entry["file"]))
    for entry in release_proof_baseline.approved_evidence_sources()
)


def _global_launch_contract() -> dict[str, object]:
    seed = json.loads((ROOT / SEED).read_text(encoding="utf-8"))
    contract = seed["global_launch_contract"]
    assert release_proof_baseline.approved_global_launch_contract_blockers(contract) == []
    return contract


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
    evidence_sources = release_proof_baseline.approved_evidence_sources()
    journey_evidence = release_proof_baseline.approved_journey_evidence()
    _write(
        root,
        SEED,
        json.dumps(
            {
                "product": "propertyquarry",
                "surface": "propertyquarry_flagship_release_control",
                "browser_workflow_proof": {
                    "proof_target": "propertyquarry",
                    "evidence_sources": evidence_sources,
                },
                "journey_evidence_matrix": {
                    "required_journey_ids": list(
                        release_proof_baseline.APPROVED_REQUIRED_JOURNEY_IDS
                    ),
                    "rows": [
                        {
                            "journey_id": journey_id,
                            "evidence_sources": journey_evidence[journey_id],
                        }
                        for journey_id in release_proof_baseline.APPROVED_REQUIRED_JOURNEY_IDS
                    ],
                },
                "global_launch_contract": _global_launch_contract(),
            }
        )
        + "\n",
    )
    for path in SOURCE_CASES:
        _write(root, path, f"# {path.name}\n")
    _write(root, "app.txt", "source-v1\n")
    _write(root, "ea/app/runtime.py", 'VALUE = "committed"\n')
    _write(root, "scripts/materialize_ea_browser_workflow_proof.py", "# committed materializer\n")
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
    initial_binding = _binding(tmp_path, evidence_sources)
    assert initial_binding["code_commit"] == initial
    assert initial_binding["approved_baseline"] == release_proof_baseline.approved_baseline_binding()
    assert [entry["path"] for entry in initial_binding["required_test_sources"]] == [
        path.as_posix() for path in SOURCE_CASES
    ]

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


def test_source_binding_walks_transparent_synthetic_merge_and_metadata_envelope(
    tmp_path: Path,
) -> None:
    evidence_sources, initial = _initialize_repository(tmp_path)
    _git(tmp_path, "switch", "-c", "feature")
    _write(tmp_path, "app.txt", "source-v2\n")
    source_commit = _commit(tmp_path, "change source")
    for path in (
        ".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json",
        ".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json",
        ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json",
        "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md",
    ):
        _write(tmp_path, path, f"metadata for {source_commit}\n")
    feature_head = _commit(tmp_path, "refresh release metadata")

    _git(tmp_path, "switch", "main")
    merge_commit = _git(
        tmp_path,
        "merge",
        "--no-ff",
        "-m",
        "synthetic pull request merge",
        feature_head,
    )
    merge_head = _git(tmp_path, "rev-parse", "HEAD")

    assert merge_commit
    assert merge_head not in {initial, feature_head}
    assert _git(tmp_path, "rev-parse", f"{merge_head}^{{tree}}") == _git(
        tmp_path,
        "rev-parse",
        f"{feature_head}^{{tree}}",
    )
    assert _binding(tmp_path, evidence_sources)["code_commit"] == source_commit


def test_source_binding_keeps_merge_commit_when_integration_changes_the_tree(
    tmp_path: Path,
) -> None:
    evidence_sources, _initial = _initialize_repository(tmp_path)
    _git(tmp_path, "switch", "-c", "feature")
    _write(tmp_path, "app.txt", "source-v2\n")
    feature_head = _commit(tmp_path, "change feature source")

    _git(tmp_path, "switch", "main")
    _write(tmp_path, "main-only.txt", "integrated source\n")
    _commit(tmp_path, "change main source")
    _git(
        tmp_path,
        "merge",
        "--no-ff",
        "-m",
        "merge feature with integration source",
        feature_head,
    )
    merge_head = _git(tmp_path, "rev-parse", "HEAD")

    assert _binding(tmp_path, evidence_sources)["code_commit"] == merge_head


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


def test_source_binding_rejects_self_consistent_weakened_evidence_baseline(tmp_path: Path) -> None:
    evidence_sources, _initial = _initialize_repository(tmp_path)
    weakened = json.loads(json.dumps(evidence_sources))
    weakened[0]["cases"][0] = "test_unapproved_weakened_public_entry"

    with pytest.raises(ReleaseBindingError, match="immutable approved baseline"):
        _binding(tmp_path, weakened)


def test_source_binding_reads_and_rejects_weakened_journey_seed_itself(tmp_path: Path) -> None:
    _evidence_sources, _initial = _initialize_repository(tmp_path)
    seed_path = tmp_path / SEED
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    journey_id = "feedback"
    row = next(
        item
        for item in seed["journey_evidence_matrix"]["rows"]
        if item["journey_id"] == journey_id
    )
    approved_case = row["evidence_sources"][0]["cases"][0]
    weakened_case = "test_unapproved_weakened_feedback"
    row["evidence_sources"][0]["cases"][0] = weakened_case
    browser_source = next(
        item
        for item in seed["browser_workflow_proof"]["evidence_sources"]
        if item["file"] == row["evidence_sources"][0]["file"]
    )
    browser_source["cases"][browser_source["cases"].index(approved_case)] = weakened_case
    seed_path.write_text(json.dumps(seed) + "\n", encoding="utf-8")
    _commit(tmp_path, "weaken release proof seed")

    with pytest.raises(
        ReleaseBindingError,
        match="journey feedback evidence sources do not match the immutable approved baseline",
    ):
        _binding(tmp_path, seed["browser_workflow_proof"]["evidence_sources"])


@pytest.mark.parametrize(
    "dirty_path",
    (Path("ea/app/runtime.py"), Path("scripts/materialize_ea_browser_workflow_proof.py")),
)
def test_source_binding_rejects_dirty_runtime_or_materializer_candidate(
    tmp_path: Path,
    dirty_path: Path,
) -> None:
    evidence_sources, _initial = _initialize_repository(tmp_path)
    _write(tmp_path, dirty_path, "# dirty code actually used by proof execution\n")

    with pytest.raises(
        ReleaseBindingError,
        match="release proof candidate has uncommitted non-metadata changes",
    ):
        _binding(tmp_path, evidence_sources)


def test_source_binding_allows_only_dirty_canonical_release_metadata(tmp_path: Path) -> None:
    evidence_sources, initial = _initialize_repository(tmp_path)
    _write(
        tmp_path,
        ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json",
        "generated browser receipt\n",
    )

    binding = _binding(tmp_path, evidence_sources)

    assert binding["code_commit"] == initial


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
