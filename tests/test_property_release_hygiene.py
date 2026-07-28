from __future__ import annotations

import subprocess

from scripts import check_property_release_hygiene as release_hygiene


def test_release_hygiene_fails_when_tracked_worktree_is_dirty(monkeypatch) -> None:
    monkeypatch.setattr(release_hygiene, "release_manifest_runtime_sha", lambda: "ad4dd937")
    monkeypatch.setattr(release_hygiene, "git_head_sha", lambda: "ad4dd9372ae36543e1c36a8ed7a01092e2cc96c5")
    monkeypatch.setattr(release_hygiene, "git_head_parent_sha", lambda: "24ccb9c92331f446aa7f6f5e9f22213e6c42cd36")
    monkeypatch.setattr(release_hygiene, "_git_status_rows", lambda: [" M ea/app/product/service.py", "?? state/receipts/foo.json"])
    monkeypatch.setattr(release_hygiene, "tracked_paths", lambda: [])

    receipt = release_hygiene.build_release_hygiene_receipt()

    assert receipt["status"] == "fail"
    assert receipt["tracked_dirty_path_count"] == 1
    assert receipt["untracked_release_source_count"] == 0
    assert any("tracked worktree must be clean before release" in failure for failure in receipt["failures"])
    assert all("state/receipts/foo.json" not in failure for failure in receipt["failures"])


def test_release_hygiene_flags_untracked_release_sources_but_ignores_runtime_artifacts(monkeypatch) -> None:
    monkeypatch.setattr(release_hygiene, "release_manifest_runtime_sha", lambda: "ad4dd937")
    monkeypatch.setattr(release_hygiene, "git_head_sha", lambda: "ad4dd9372ae36543e1c36a8ed7a01092e2cc96c5")
    monkeypatch.setattr(release_hygiene, "git_head_parent_sha", lambda: "24ccb9c92331f446aa7f6f5e9f22213e6c42cd36")
    monkeypatch.setattr(
        release_hygiene,
        "_git_status_rows",
        lambda: [
            "?? scripts/property_provider_matrix_stage_runner.py",
            "?? state/receipts/propertyquarry_gold_status_current.json",
            "?? _completion/property_gold_status/latest.json",
            "?? _tmp_live_shots/research.png",
        ],
    )
    monkeypatch.setattr(release_hygiene, "tracked_paths", lambda: [])

    receipt = release_hygiene.build_release_hygiene_receipt()

    assert receipt["status"] == "fail"
    assert receipt["tracked_dirty_path_count"] == 0
    assert receipt["untracked_release_source_count"] == 1
    assert any(
        "untracked release source files forbidden before release: scripts/property_provider_matrix_stage_runner.py" in failure
        for failure in receipt["failures"]
    )


def test_release_hygiene_classifies_untracked_inputs_fail_closed() -> None:
    release_inputs = (
        "native/propertyquarry-release-control-v2/internal/releasecontrol/releasecontrol.go",
        "packaging/propertyquarry-release-control-v2/systemd/propertyquarry-release-control-v2@.service",
        "config/monitoring/propertyquarry_flagship_operations.v1.json",
        "config/propertyquarry_security_runner_requirements.lock",
        ".github/workflows/smoke-runtime.yml",
        "pytest.ini",
        "unclassified/release-input.bin",
    )
    generated_or_runtime_paths = (
        "_completion/property_gold_status/latest.json",
        "_tmp_live_shots/research.png",
        ".pytest_cache/v/cache/nodeids",
        "build/propertyquarry-release-control-v2/linux-amd64/propertyquarry-release-controller-v2",
        "state/receipts/propertyquarry_gold_status_current.json",
        "tmp_audit/probe.json",
    )

    assert all(release_hygiene._is_release_source_path(path) for path in release_inputs)
    assert not any(
        release_hygiene._is_release_source_path(path)
        for path in generated_or_runtime_paths
    )


def test_manifest_release_binding_accepts_only_named_metadata_descendants(monkeypatch) -> None:
    monkeypatch.setattr(release_hygiene, "git_commit_is_ancestor", lambda manifest, head: True)
    monkeypatch.setattr(
        release_hygiene,
        "committed_paths_since",
        lambda manifest, head: [
            "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md",
            ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json",
            ".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json",
            ".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json",
        ],
    )

    accepted, descendant_paths = release_hygiene.manifest_release_binding(
        "candidate-sha",
        "metadata-closeout-sha",
        "pulse-sha",
    )

    assert accepted is True
    assert descendant_paths == [
        "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md",
        ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json",
        ".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json",
        ".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json",
    ]


def test_release_manifest_runtime_sha_reads_canonical_json_authority(monkeypatch) -> None:
    expected = "a" * 40
    observed_paths = []

    def fake_load(path):
        observed_paths.append(path)
        return {"release_commit_sha": expected}

    monkeypatch.setattr(release_hygiene, "load_release_manifest", fake_load)

    assert release_hygiene.release_manifest_runtime_sha() == expected
    assert observed_paths == [
        release_hygiene.ROOT / "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md"
    ]


def test_manifest_release_binding_rejects_runtime_descendant(monkeypatch) -> None:
    monkeypatch.setattr(release_hygiene, "git_commit_is_ancestor", lambda manifest, head: True)
    monkeypatch.setattr(
        release_hygiene,
        "committed_paths_since",
        lambda manifest, head: [
            "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md",
            "ea/app/api/routes/landing.py",
        ],
    )

    accepted, descendant_paths = release_hygiene.manifest_release_binding(
        "candidate-sha",
        "changed-runtime-sha",
        "manifest-sha",
    )

    assert accepted is False
    assert "ea/app/api/routes/landing.py" in descendant_paths


def test_manifest_release_binding_rejects_non_ancestor(monkeypatch) -> None:
    monkeypatch.setattr(release_hygiene, "git_commit_is_ancestor", lambda manifest, head: False)

    accepted, descendant_paths = release_hygiene.manifest_release_binding(
        "unknown-sha",
        "current-sha",
        "parent-sha",
    )

    assert accepted is False
    assert descendant_paths == []


def test_committed_paths_since_keeps_reverted_runtime_change_visible(monkeypatch, tmp_path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "release-hygiene@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Release Hygiene"], cwd=tmp_path, check=True)
    runtime_path = tmp_path / "ea/app/api/routes/landing.py"
    runtime_path.parent.mkdir(parents=True)
    runtime_path.write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", runtime_path.relative_to(tmp_path).as_posix()], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=tmp_path, check=True)
    baseline_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    runtime_path.write_text("temporary runtime change\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "change runtime"], cwd=tmp_path, check=True)
    runtime_path.write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "revert runtime"], cwd=tmp_path, check=True)
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setattr(release_hygiene, "ROOT", tmp_path)

    descendant_paths = release_hygiene.committed_paths_since(baseline_sha, head_sha)

    assert descendant_paths == ["ea/app/api/routes/landing.py"]


def test_committed_paths_since_does_not_split_newline_filename(monkeypatch) -> None:
    class GitResult:
        returncode = 0
        stdout = (
            b"docs/PROPERTYQUARRY_RELEASE_MANIFEST.md\n"
            b".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json\0"
        )

    monkeypatch.setattr(release_hygiene.subprocess, "run", lambda *args, **kwargs: GitResult())

    descendant_paths = release_hygiene.committed_paths_since("candidate-sha", "head-sha")

    assert descendant_paths == [
        "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md\n"
        ".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json"
    ]
    assert descendant_paths[0] not in release_hygiene.RELEASE_METADATA_DESCENDANT_PATHS
