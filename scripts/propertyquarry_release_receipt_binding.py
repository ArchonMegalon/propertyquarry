#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__:
    from . import propertyquarry_release_proof_baseline as release_proof_baseline
    from .check_property_release_hygiene import (
        RELEASE_GIT_COMMAND,
        release_git_environment,
    )
else:
    import propertyquarry_release_proof_baseline as release_proof_baseline
    from check_property_release_hygiene import (
        RELEASE_GIT_COMMAND,
        release_git_environment,
    )


CANONICAL_SEED = Path(".codex-design/repo/EA_FLAGSHIP_RELEASE_GATE.json")
CANONICAL_BROWSER_RECEIPT = Path(".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json")
CANONICAL_FLAGSHIP_RECEIPT = Path(".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json")
CANONICAL_RELEASE_MANIFEST = Path("docs/PROPERTYQUARRY_RELEASE_MANIFEST.md")
CANONICAL_WEEKLY_PULSE = Path(".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json")
SOURCE_BINDING_VERSION = 1
MAX_METADATA_ONLY_ANCESTORS = 128
RELEASE_METADATA_ONLY_PATHS = frozenset(
    {
        CANONICAL_BROWSER_RECEIPT.as_posix(),
        CANONICAL_FLAGSHIP_RECEIPT.as_posix(),
        CANONICAL_RELEASE_MANIFEST.as_posix(),
        CANONICAL_WEEKLY_PULSE.as_posix(),
    }
)


class ReleaseBindingError(ValueError):
    pass


FileIdentity = tuple[int, int, int, int, int, int]


def _file_identity(metadata: os.stat_result) -> FileIdentity:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


@dataclass(frozen=True)
class StableFileSnapshot:
    path: Path
    identity: FileIdentity
    payload: bytes

    def assert_unchanged(self) -> None:
        try:
            resolved = self.path.resolve(strict=True)
            observed = self.path.lstat()
        except OSError as exc:
            raise ReleaseBindingError(
                f"release evidence changed after it was read: {self.path}"
            ) from exc
        if (
            resolved != self.path
            or not stat.S_ISREG(observed.st_mode)
            or _file_identity(observed) != self.identity
        ):
            raise ReleaseBindingError(
                f"release evidence changed after it was read: {self.path}"
            )


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def read_stable_regular_file(path: Path) -> StableFileSnapshot:
    lexical = Path(os.path.abspath(os.path.normpath(path)))
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise ReleaseBindingError(
            f"release evidence file is missing or unreadable: {path}"
        ) from exc
    if resolved != lexical:
        raise ReleaseBindingError(
            f"release evidence path contains a symlink: {path}"
        )

    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        file_descriptor = os.open(lexical, flags)
    except OSError as exc:
        raise ReleaseBindingError(
            f"release evidence file is missing or unreadable: {path}"
        ) from exc
    try:
        before = os.fstat(file_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ReleaseBindingError(
                f"release evidence path is not a regular file: {path}"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(file_descriptor)
    finally:
        os.close(file_descriptor)

    payload = b"".join(chunks)
    identity = _file_identity(before)
    if identity != _file_identity(after) or len(payload) != before.st_size:
        raise ReleaseBindingError(
            f"release evidence changed while it was being read: {path}"
        )
    snapshot = StableFileSnapshot(
        path=lexical,
        identity=identity,
        payload=payload,
    )
    snapshot.assert_unchanged()
    return snapshot


def _safe_relative_path(value: Path | str) -> Path:
    path = Path(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseBindingError(f"release evidence path is not a safe repository-relative path: {path}")
    return path


def canonical_regular_file(root: Path, relative_path: Path | str) -> Path:
    root = root.resolve(strict=True)
    relative_path = _safe_relative_path(relative_path)
    candidate = root
    for index, part in enumerate(relative_path.parts):
        candidate = candidate / part
        try:
            mode = os.lstat(candidate).st_mode
        except OSError as exc:
            raise ReleaseBindingError(f"release evidence file is missing or unreadable: {relative_path}: {exc}") from exc
        if stat.S_ISLNK(mode):
            raise ReleaseBindingError(f"release evidence path contains a symlink: {relative_path}")
        if index < len(relative_path.parts) - 1 and not stat.S_ISDIR(mode):
            raise ReleaseBindingError(f"release evidence parent is not a directory: {relative_path}")
    if not stat.S_ISREG(mode):
        raise ReleaseBindingError(f"release evidence path is not a regular file: {relative_path}")
    try:
        candidate.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise ReleaseBindingError(f"release evidence path escapes the repository: {relative_path}") from exc
    return candidate


def git_bytes(root: Path, *args: str) -> bytes:
    completed = subprocess.run(
        [*RELEASE_GIT_COMMAND, *args],
        cwd=root,
        env=release_git_environment(),
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseBindingError(f"git {' '.join(args)} failed: {detail or 'unknown git error'}")
    return bytes(completed.stdout)


def git_text(root: Path, *args: str) -> str:
    return git_bytes(root, *args).decode("utf-8", errors="strict").strip()


def working_tree_changed_paths(root: Path) -> list[str]:
    """Return every staged, unstaged, or untracked repository-relative path."""
    raw_status = git_bytes(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    records = raw_status.split(b"\0")
    changed: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise ReleaseBindingError("release proof candidate has an unreadable Git worktree status")
        status = record[:2]
        path_values = [record[3:]]
        if b"R" in status or b"C" in status:
            if index >= len(records) or not records[index]:
                raise ReleaseBindingError("release proof candidate has an incomplete Git rename status")
            path_values.append(records[index])
            index += 1
        for raw_path in path_values:
            try:
                path = _safe_relative_path(raw_path.decode("utf-8", errors="strict")).as_posix()
            except (UnicodeError, ReleaseBindingError) as exc:
                raise ReleaseBindingError(
                    "release proof candidate has a non-canonical Git worktree path"
                ) from exc
            changed.append(path)
    return list(dict.fromkeys(changed))


def resolve_commit(root: Path, revision: str = "HEAD") -> str:
    commit = git_text(root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    if not commit:
        raise ReleaseBindingError(f"Git revision did not resolve to a commit: {revision}")
    return commit.lower()


def commit_parents(root: Path, commit: str) -> list[str]:
    fields = git_text(root, "rev-list", "--parents", "-n", "1", commit).split()
    if not fields or fields[0].lower() != commit.lower():
        raise ReleaseBindingError(f"could not resolve commit parents for {commit}")
    return [item.lower() for item in fields[1:]]


def declared_commit_parents(root: Path, commit: str) -> list[str]:
    parents: list[str] = []
    for line in git_bytes(root, "cat-file", "-p", commit).splitlines():
        if not line:
            break
        if line.startswith(b"parent "):
            parents.append(line.removeprefix(b"parent ").decode("ascii").lower())
    return parents


def changed_paths(root: Path, parent: str, commit: str) -> list[str]:
    output = git_text(root, "diff-tree", "--no-commit-id", "--name-only", "-r", parent, commit)
    return [line.strip() for line in output.splitlines() if line.strip()]


def commit_tree(root: Path, commit: str) -> str:
    tree = git_text(root, "rev-parse", "--verify", f"{commit}^{{tree}}")
    if not tree:
        raise ReleaseBindingError(f"Git commit did not resolve to a tree: {commit}")
    return tree.lower()


def resolve_source_binding_commit(root: Path, revision: str = "HEAD") -> str:
    """Resolve the source commit behind transparent merges and receipt refreshes."""
    candidate = resolve_commit(root, revision)
    for _depth in range(MAX_METADATA_ONLY_ANCESTORS):
        parents = commit_parents(root, candidate)
        declared_parents = declared_commit_parents(root, candidate)
        if not parents and declared_parents:
            raise ReleaseBindingError(
                "source binding ancestry is shallow; fetch complete Git history before materialization"
            )
        if len(parents) == 2:
            candidate_tree = commit_tree(root, candidate)
            identical_tree_parents = [
                parent for parent in parents if commit_tree(root, parent) == candidate_tree
            ]
            if len(identical_tree_parents) != 1:
                return candidate
            candidate = identical_tree_parents[0]
            continue
        if len(parents) != 1:
            return candidate
        parent = parents[0]
        paths = set(changed_paths(root, parent, candidate))
        if not paths or not paths.issubset(RELEASE_METADATA_ONLY_PATHS):
            return candidate
        candidate = parent
    return candidate


def commit_timestamp(root: Path, commit: str) -> int:
    raw = git_text(root, "show", "-s", "--format=%ct", commit)
    try:
        return int(raw)
    except ValueError as exc:
        raise ReleaseBindingError(f"commit has an invalid timestamp: {commit}") from exc


def commit_file_bytes(root: Path, commit: str, relative_path: Path | str) -> bytes:
    relative_path = _safe_relative_path(relative_path)
    return git_bytes(root, "show", f"{commit}:{relative_path.as_posix()}")


def commit_file_oid(root: Path, commit: str, relative_path: Path | str) -> str:
    relative_path = _safe_relative_path(relative_path)
    return git_text(root, "rev-parse", f"{commit}:{relative_path.as_posix()}").lower()


def working_file_oid(root: Path, relative_path: Path | str) -> str:
    snapshot, binding = file_snapshot_binding(root, relative_path)
    snapshot.assert_unchanged()
    return binding["git_blob_oid"]


def git_blob_oid_bytes(root: Path, payload: bytes) -> str:
    completed = subprocess.run(
        [*RELEASE_GIT_COMMAND, "hash-object", "--no-filters", "--stdin"],
        cwd=root,
        env=release_git_environment(),
        input=payload,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseBindingError(
            "git hash-object --no-filters --stdin failed: "
            f"{detail or 'unknown git error'}"
        )
    return completed.stdout.decode("ascii", errors="strict").strip().lower()


def file_snapshot_binding(
    root: Path,
    relative_path: Path | str,
) -> tuple[StableFileSnapshot, dict[str, str]]:
    relative_path = _safe_relative_path(relative_path)
    path = canonical_regular_file(root, relative_path)
    snapshot = read_stable_regular_file(path)
    binding = {
        "path": relative_path.as_posix(),
        "sha256": sha256_bytes(snapshot.payload),
        "git_blob_oid": git_blob_oid_bytes(root, snapshot.payload),
    }
    snapshot.assert_unchanged()
    return snapshot, binding


def file_digest_binding(root: Path, relative_path: Path | str) -> dict[str, str]:
    _snapshot, binding = file_snapshot_binding(root, relative_path)
    return binding


def _commit_bound_file(root: Path, relative_path: Path, code_commit: str) -> dict[str, str]:
    path = canonical_regular_file(root, relative_path)
    working_bytes = path.read_bytes()
    committed_bytes = commit_file_bytes(root, code_commit, relative_path)
    if working_bytes != committed_bytes:
        raise ReleaseBindingError(
            f"release source differs from immutable code commit {code_commit}: {relative_path.as_posix()}"
        )
    return {
        "path": relative_path.as_posix(),
        "sha256": sha256_bytes(working_bytes),
        "git_blob_oid": commit_file_oid(root, code_commit, relative_path),
    }


def _commit_file_binding(
    root: Path,
    relative_path: Path,
    code_commit: str,
) -> dict[str, str]:
    committed_bytes = commit_file_bytes(root, code_commit, relative_path)
    return {
        "path": relative_path.as_posix(),
        "sha256": sha256_bytes(committed_bytes),
        "git_blob_oid": commit_file_oid(root, code_commit, relative_path),
    }


def committed_source_binding(
    root: Path,
    *,
    seed_path: Path | str,
    evidence_sources: object,
    code_commit: str | None = None,
) -> dict[str, Any]:
    """Build an immutable source binding using only bytes from a committed candidate."""
    root = root.resolve(strict=True)
    seed_path = _safe_relative_path(seed_path)
    code_commit = resolve_source_binding_commit(
        root,
        code_commit if code_commit is not None else "HEAD",
    )
    try:
        seed_payload = json.loads(
            commit_file_bytes(root, code_commit, seed_path).decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReleaseBindingError(
            f"committed flagship seed is invalid canonical JSON at {code_commit}: "
            f"{seed_path.as_posix()}"
        ) from exc
    if not isinstance(seed_payload, dict):
        raise ReleaseBindingError("committed flagship seed must contain a JSON object")
    seed_baseline_blockers = release_proof_baseline.approved_seed_baseline_blockers(
        seed_payload
    )
    if seed_baseline_blockers:
        raise ReleaseBindingError("; ".join(seed_baseline_blockers))
    seed_proof_contract = seed_payload.get("browser_workflow_proof")
    declared_evidence_sources = (
        seed_proof_contract.get("evidence_sources")
        if isinstance(seed_proof_contract, dict)
        else None
    )
    if evidence_sources != declared_evidence_sources:
        raise ReleaseBindingError(
            "supplied browser evidence sources do not match the immutable candidate seed "
            "or approved baseline"
        )
    if not isinstance(evidence_sources, list):
        raise ReleaseBindingError("flagship seed lacks browser evidence source nodes")
    baseline_blockers = release_proof_baseline.approved_evidence_source_blockers(
        evidence_sources
    )
    if baseline_blockers:
        raise ReleaseBindingError("; ".join(baseline_blockers))

    source_paths: list[Path] = []
    for entry in evidence_sources:
        if not isinstance(entry, dict):
            raise ReleaseBindingError(
                "flagship seed contains an invalid browser evidence source node"
            )
        source_path = _safe_relative_path(str(entry.get("file") or ""))
        raw_cases = entry.get("cases")
        cases = (
            [item.strip() for item in raw_cases if isinstance(item, str) and item.strip()]
            if isinstance(raw_cases, list)
            else []
        )
        if (
            not isinstance(raw_cases, list)
            or not cases
            or len(cases) != len(raw_cases)
            or len(cases) != len(set(cases))
        ):
            raise ReleaseBindingError(
                f"browser evidence source lacks required cases: "
                f"{source_path.as_posix()}"
            )
        source_paths.append(source_path)
    source_path_values = [path.as_posix() for path in source_paths]
    source_backed_paths = [
        path for path in source_path_values if "/e2e/" not in path
    ]
    real_browser_paths = [path for path in source_path_values if "/e2e/" in path]
    if (
        len(source_path_values) != len(set(source_path_values))
        or not source_backed_paths
        or len(real_browser_paths) != 1
    ):
        raise ReleaseBindingError(
            "flagship seed must define distinct evidence sources with at least one "
            "source-backed lane and exactly one real-browser lane"
        )
    return {
        "version": SOURCE_BINDING_VERSION,
        "approved_baseline": release_proof_baseline.approved_baseline_binding(),
        "code_commit": code_commit,
        "seed": _commit_file_binding(root, seed_path, code_commit),
        "required_test_sources": [
            _commit_file_binding(root, source_path, code_commit)
            for source_path in source_paths
        ],
    }


def build_source_binding(
    root: Path,
    *,
    seed_path: Path | str,
    evidence_sources: object,
    code_commit: str | None = None,
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    seed_path = _safe_relative_path(seed_path)
    changed_paths_in_worktree = working_tree_changed_paths(root)
    unbound_changes = [
        path for path in changed_paths_in_worktree if path not in RELEASE_METADATA_ONLY_PATHS
    ]
    if unbound_changes:
        raise ReleaseBindingError(
            "release proof candidate has uncommitted non-metadata changes: "
            + ", ".join(unbound_changes)
        )
    code_commit = resolve_source_binding_commit(
        root,
        code_commit if code_commit is not None else "HEAD",
    )
    try:
        seed_payload = json.loads(canonical_regular_file(root, seed_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseBindingError(f"flagship seed is not valid canonical JSON: {exc}") from exc
    if not isinstance(seed_payload, dict):
        raise ReleaseBindingError("flagship seed must contain a JSON object")
    seed_baseline_blockers = release_proof_baseline.approved_seed_baseline_blockers(seed_payload)
    if seed_baseline_blockers:
        raise ReleaseBindingError("; ".join(seed_baseline_blockers))
    seed_proof_contract = seed_payload.get("browser_workflow_proof")
    declared_evidence_sources = (
        seed_proof_contract.get("evidence_sources")
        if isinstance(seed_proof_contract, dict)
        else None
    )
    if evidence_sources != declared_evidence_sources:
        raise ReleaseBindingError(
            "supplied browser evidence sources do not match the canonical flagship seed or immutable approved baseline"
        )
    if not isinstance(evidence_sources, list):
        raise ReleaseBindingError("flagship seed lacks browser evidence source nodes")
    baseline_blockers = release_proof_baseline.approved_evidence_source_blockers(evidence_sources)
    if baseline_blockers:
        raise ReleaseBindingError("; ".join(baseline_blockers))
    source_paths: list[Path] = []
    for entry in evidence_sources:
        if not isinstance(entry, dict):
            raise ReleaseBindingError("flagship seed contains an invalid browser evidence source node")
        source_path = _safe_relative_path(str(entry.get("file") or ""))
        raw_cases = entry.get("cases")
        cases = (
            [item.strip() for item in raw_cases if isinstance(item, str) and item.strip()]
            if isinstance(raw_cases, list)
            else []
        )
        if (
            not isinstance(raw_cases, list)
            or not cases
            or len(cases) != len(raw_cases)
            or len(cases) != len(set(cases))
        ):
            raise ReleaseBindingError(f"browser evidence source lacks required cases: {source_path.as_posix()}")
        source_paths.append(source_path)
    source_path_values = [path.as_posix() for path in source_paths]
    source_backed_paths = [path for path in source_path_values if "/e2e/" not in path]
    real_browser_paths = [path for path in source_path_values if "/e2e/" in path]
    if (
        len(source_path_values) != len(set(source_path_values))
        or not source_backed_paths
        or len(real_browser_paths) != 1
    ):
        raise ReleaseBindingError(
            "flagship seed must define distinct evidence sources with at least one source-backed lane "
            "and exactly one real-browser lane"
        )
    return {
        "version": SOURCE_BINDING_VERSION,
        "approved_baseline": release_proof_baseline.approved_baseline_binding(),
        "code_commit": code_commit,
        "seed": _commit_bound_file(root, seed_path, code_commit),
        "required_test_sources": [
            _commit_bound_file(root, source_path, code_commit) for source_path in source_paths
        ],
    }
