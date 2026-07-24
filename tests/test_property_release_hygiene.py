from __future__ import annotations

import subprocess

import pytest

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


def test_manifest_release_binding_accepts_safe_synthetic_merge_parent(monkeypatch) -> None:
    manifest_sha = "runtime-sha"
    head_sha = "synthetic-merge-sha"
    base_parent_sha = "base-parent-sha"
    feature_parent_sha = "feature-parent-sha"

    monkeypatch.setattr(
        release_hygiene,
        "git_commit_is_ancestor",
        lambda ancestor, descendant: (ancestor, descendant)
        in {
            (manifest_sha, head_sha),
            (manifest_sha, feature_parent_sha),
        },
    )
    monkeypatch.setattr(
        release_hygiene,
        "committed_paths_since",
        lambda ancestor, descendant: ["docs/PROPERTYQUARRY_RELEASE_MANIFEST.md"],
    )
    monkeypatch.setattr(
        release_hygiene,
        "tree_paths_between",
        lambda parent, head: [] if parent == feature_parent_sha and head == head_sha else None,
    )

    accepted, descendant_paths = release_hygiene.manifest_release_binding(
        manifest_sha,
        head_sha,
        [base_parent_sha, feature_parent_sha],
    )
    detailed_accepted, detailed_paths, binding_parent = (
        release_hygiene._manifest_release_binding(
            manifest_sha,
            head_sha,
            [base_parent_sha, feature_parent_sha],
        )
    )

    assert accepted is True
    assert descendant_paths == ["docs/PROPERTYQUARRY_RELEASE_MANIFEST.md"]
    assert (detailed_accepted, detailed_paths) == (accepted, descendant_paths)
    assert binding_parent == feature_parent_sha


def test_release_hygiene_receipt_reports_safe_synthetic_merge_parent(monkeypatch) -> None:
    manifest_sha = "a" * 40
    head_sha = "b" * 40
    base_parent_sha = "c" * 40
    feature_parent_sha = "d" * 40
    monkeypatch.setattr(release_hygiene, "release_manifest_runtime_sha", lambda: manifest_sha)
    monkeypatch.setattr(release_hygiene, "git_head_sha", lambda: head_sha)
    monkeypatch.setattr(
        release_hygiene,
        "git_commit_parent_shas",
        lambda commit: [base_parent_sha, feature_parent_sha],
    )
    monkeypatch.setattr(
        release_hygiene,
        "git_commit_is_ancestor",
        lambda ancestor, descendant: (ancestor, descendant)
        in {
            (manifest_sha, head_sha),
            (manifest_sha, feature_parent_sha),
        },
    )
    monkeypatch.setattr(
        release_hygiene,
        "committed_paths_since",
        lambda ancestor, descendant: list(release_hygiene.RELEASE_METADATA_DESCENDANT_PATHS),
    )
    monkeypatch.setattr(release_hygiene, "tree_paths_between", lambda parent, head: [])
    monkeypatch.setattr(release_hygiene, "_git_status_rows", lambda: [])
    monkeypatch.setattr(release_hygiene, "tracked_paths", lambda: [])

    receipt = release_hygiene.build_release_hygiene_receipt()

    assert receipt["status"] == "pass"
    assert receipt["parent_commit"] == feature_parent_sha
    assert "parent_commits" not in receipt
    assert receipt["merge_commit_required"] is False
    assert receipt["merge_parent_commits"] == [
        base_parent_sha,
        feature_parent_sha,
    ]
    assert set(receipt) == {
        "schema",
        "generated_at",
        "status",
        "required_checks",
        "failure_count",
        "failures",
        "manifest_runtime_commit",
        "head_commit",
        "parent_commit",
        "merge_commit_required",
        "merge_base_parent_commit",
        "merge_parent_commits",
        "head_tree",
        "reviewed_envelope_commit",
        "reviewed_envelope_parent_commits",
        "reviewed_envelope_tree",
        "merge_tree_matches_reviewed_envelope",
        "manifest_descendant_paths",
        "manifest_metadata_only_ancestor",
        "tracked_dirty_path_count",
        "untracked_release_source_count",
        "note",
    }


def test_manifest_release_binding_rejects_merge_parent_with_source_delta_from_manifest(monkeypatch) -> None:
    manifest_sha = "runtime-sha"
    head_sha = "synthetic-merge-sha"
    base_parent_sha = "base-parent-sha"
    unrelated_parent_sha = "unrelated-parent-sha"

    monkeypatch.setattr(
        release_hygiene,
        "git_commit_is_ancestor",
        lambda ancestor, descendant: (ancestor, descendant)
        in {
            (manifest_sha, head_sha),
            (manifest_sha, base_parent_sha),
        },
    )
    monkeypatch.setattr(
        release_hygiene,
        "committed_paths_since",
        lambda ancestor, descendant: ["ea/app/api/routes/landing.py"],
    )
    monkeypatch.setattr(
        release_hygiene,
        "tree_paths_between",
        lambda parent, head: (_ for _ in ()).throw(AssertionError("unsafe parent tree must not be trusted")),
    )

    accepted, descendant_paths = release_hygiene.manifest_release_binding(
        manifest_sha,
        head_sha,
        [base_parent_sha, unrelated_parent_sha],
    )

    assert accepted is False
    assert descendant_paths == ["ea/app/api/routes/landing.py"]


def _patch_protected_merge(
    monkeypatch,
    *,
    parent_shas: list[str] | None = None,
    envelope_parent_shas: list[str] | None = None,
    head_tree: str | None = None,
    envelope_tree: str | None = None,
    envelope_paths: list[str] | None = None,
    merge_paths: list[str] | None = None,
    base_is_ancestor: bool = True,
    runtime_is_base_ancestor: bool = False,
) -> tuple[str, str, str, str]:
    runtime_sha = "a" * 40
    head_sha = "b" * 40
    base_sha = "c" * 40
    envelope_sha = "d" * 40
    tree_sha = "e" * 40
    actual_parent_shas = (
        [base_sha, envelope_sha] if parent_shas is None else parent_shas
    )
    actual_envelope_parents = (
        [runtime_sha]
        if envelope_parent_shas is None
        else envelope_parent_shas
    )
    actual_envelope_paths = (
        sorted(release_hygiene.RELEASE_METADATA_DESCENDANT_PATHS)
        if envelope_paths is None
        else envelope_paths
    )
    actual_merge_paths = [] if merge_paths is None else merge_paths
    monkeypatch.setattr(
        release_hygiene,
        "git_commit_parent_shas",
        lambda commit: (
            actual_parent_shas
            if commit == head_sha
            else actual_envelope_parents
            if commit == envelope_sha
            else []
        ),
    )
    monkeypatch.setattr(
        release_hygiene,
        "git_commit_tree_sha",
        lambda commit: (
            (tree_sha if head_tree is None else head_tree)
            if commit == head_sha
            else (tree_sha if envelope_tree is None else envelope_tree)
            if commit == envelope_sha
            else ""
        ),
    )
    monkeypatch.setattr(
        release_hygiene,
        "git_commit_is_ancestor",
        lambda ancestor, descendant: (
            base_is_ancestor
            if (ancestor, descendant) == (base_sha, envelope_sha)
            else runtime_is_base_ancestor
            if (ancestor, descendant) == (runtime_sha, base_sha)
            else False
        ),
    )
    monkeypatch.setattr(
        release_hygiene,
        "tree_paths_between",
        lambda base, head: (
            actual_envelope_paths
            if (base, head) == (runtime_sha, envelope_sha)
            else actual_merge_paths
            if (base, head) == (envelope_sha, head_sha)
            else []
        ),
    )
    return runtime_sha, head_sha, base_sha, envelope_sha


def test_protected_merge_binding_accepts_exact_reviewed_envelope(
    monkeypatch,
) -> None:
    runtime_sha, head_sha, base_sha, envelope_sha = _patch_protected_merge(
        monkeypatch
    )

    accepted, descendant_paths = release_hygiene.manifest_release_binding(
        runtime_sha,
        head_sha,
        [base_sha, envelope_sha],
        require_merge_commit=True,
    )

    assert accepted is True
    assert descendant_paths == sorted(
        release_hygiene.RELEASE_METADATA_DESCENDANT_PATHS
    )


@pytest.mark.parametrize(
    "tamper",
    [
        "linear",
        "parent-order",
        "extra-parent",
        "non-direct-runtime-parent",
        "tree-drift",
        "base-not-ancestor",
        "runtime-in-base",
        "extra-metadata-path",
        "missing-metadata-path",
        "merge-delta",
    ],
)
def test_protected_merge_binding_rejects_topology_tamper(
    monkeypatch,
    tamper: str,
) -> None:
    runtime_sha = "a" * 40
    head_sha = "b" * 40
    base_sha = "c" * 40
    envelope_sha = "d" * 40
    kwargs: dict[str, object] = {}
    parent_shas = [base_sha, envelope_sha]
    if tamper == "linear":
        parent_shas = [envelope_sha]
    elif tamper == "parent-order":
        parent_shas = [envelope_sha, base_sha]
    elif tamper == "extra-parent":
        parent_shas = [base_sha, envelope_sha, "f" * 40]
    elif tamper == "non-direct-runtime-parent":
        kwargs["envelope_parent_shas"] = ["f" * 40]
    elif tamper == "tree-drift":
        kwargs["head_tree"] = "f" * 40
    elif tamper == "base-not-ancestor":
        kwargs["base_is_ancestor"] = False
    elif tamper == "runtime-in-base":
        kwargs["runtime_is_base_ancestor"] = True
    elif tamper == "extra-metadata-path":
        kwargs["envelope_paths"] = sorted(
            [
                *release_hygiene.RELEASE_METADATA_DESCENDANT_PATHS,
                "ea/app/api/routes/landing.py",
            ]
        )
    elif tamper == "missing-metadata-path":
        kwargs["envelope_paths"] = sorted(
            release_hygiene.RELEASE_METADATA_DESCENDANT_PATHS
        )[1:]
    elif tamper == "merge-delta":
        kwargs["merge_paths"] = ["docs/PROPERTYQUARRY_RELEASE_MANIFEST.md"]
    _patch_protected_merge(
        monkeypatch,
        parent_shas=parent_shas,
        **kwargs,
    )

    accepted, _descendant_paths = release_hygiene.manifest_release_binding(
        runtime_sha,
        head_sha,
        parent_shas,
        require_merge_commit=True,
    )

    assert accepted is False


def test_protected_release_receipt_carries_exact_topology(monkeypatch) -> None:
    runtime_sha, head_sha, base_sha, envelope_sha = _patch_protected_merge(
        monkeypatch
    )
    monkeypatch.setattr(
        release_hygiene,
        "release_manifest_runtime_sha",
        lambda: runtime_sha,
    )
    monkeypatch.setattr(release_hygiene, "git_head_sha", lambda: head_sha)
    monkeypatch.setattr(release_hygiene, "_git_status_rows", lambda: [])
    monkeypatch.setattr(release_hygiene, "tracked_paths", lambda: [])

    receipt = release_hygiene.build_release_hygiene_receipt(
        require_merge_commit=True
    )

    assert receipt["status"] == "pass"
    assert receipt["merge_commit_required"] is True
    assert receipt["merge_base_parent_commit"] == base_sha
    assert receipt["merge_parent_commits"] == [base_sha, envelope_sha]
    assert receipt["parent_commit"] == envelope_sha
    assert receipt["reviewed_envelope_commit"] == envelope_sha
    assert receipt["reviewed_envelope_parent_commits"] == [runtime_sha]
    assert receipt["head_tree"] == "e" * 40
    assert receipt["reviewed_envelope_tree"] == "e" * 40
    assert receipt["merge_tree_matches_reviewed_envelope"] is True


def test_manifest_release_binding_rejects_merge_resolution_source_delta(monkeypatch) -> None:
    manifest_sha = "runtime-sha"
    head_sha = "synthetic-merge-sha"
    feature_parent_sha = "feature-parent-sha"
    unrelated_parent_sha = "unrelated-parent-sha"

    monkeypatch.setattr(
        release_hygiene,
        "git_commit_is_ancestor",
        lambda ancestor, descendant: (ancestor, descendant)
        in {
            (manifest_sha, head_sha),
            (manifest_sha, feature_parent_sha),
        },
    )
    monkeypatch.setattr(
        release_hygiene,
        "committed_paths_since",
        lambda ancestor, descendant: ["docs/PROPERTYQUARRY_RELEASE_MANIFEST.md"],
    )
    monkeypatch.setattr(
        release_hygiene,
        "tree_paths_between",
        lambda parent, head: ["ea/app/api/routes/landing.py"],
    )

    accepted, descendant_paths = release_hygiene.manifest_release_binding(
        manifest_sha,
        head_sha,
        [feature_parent_sha, unrelated_parent_sha],
    )

    assert accepted is False
    assert descendant_paths == [
        "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md",
        "ea/app/api/routes/landing.py",
    ]


def test_manifest_release_binding_preserves_ordinary_commit_history_audit(monkeypatch) -> None:
    monkeypatch.setattr(release_hygiene, "git_commit_is_ancestor", lambda manifest, head: True)
    monkeypatch.setattr(
        release_hygiene,
        "committed_paths_since",
        lambda manifest, head: [
            "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md",
            "ea/app/api/routes/landing.py",
        ],
    )
    monkeypatch.setattr(
        release_hygiene,
        "tree_paths_between",
        lambda parent, head: (_ for _ in ()).throw(AssertionError("ordinary history must use commit audit")),
    )

    accepted, descendant_paths = release_hygiene.manifest_release_binding(
        "runtime-sha",
        "ordinary-head-sha",
        ["ordinary-parent-sha"],
    )

    assert accepted is False
    assert descendant_paths == [
        "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md",
        "ea/app/api/routes/landing.py",
    ]


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


def test_manifest_release_binding_audits_direct_parent_commit_paths(monkeypatch) -> None:
    monkeypatch.setattr(
        release_hygiene,
        "git_commit_is_ancestor",
        lambda manifest, head: True,
    )
    monkeypatch.setattr(
        release_hygiene,
        "committed_paths_since",
        lambda manifest, head: ["ea/app/api/routes/landing.py"],
    )

    accepted, descendant_paths = release_hygiene.manifest_release_binding(
        "runtime-sha",
        "envelope-sha",
        "runtime-sha",
    )

    assert accepted is False
    assert descendant_paths == ["ea/app/api/routes/landing.py"]
