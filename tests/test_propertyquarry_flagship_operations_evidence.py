from __future__ import annotations

import copy
import json
import os
import stat
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

import pytest

from propertyquarry_evidence_test_support import (
    EvidenceTestAuthority,
    install_test_authority,
)
from scripts import propertyquarry_evidence_contract as contract
from scripts import propertyquarry_flagship_operations_evidence as operations


RELEASE_SHA = "a" * 40
IMAGE_DIGEST = "sha256:" + "b" * 64
NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
TRACE_ID = "1" * 32
SPAN_IDS = ("1" * 16, "2" * 16, "3" * 16)
AUTHORITY: EvidenceTestAuthority


@pytest.fixture(autouse=True)
def _authenticated_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    global AUTHORITY
    AUTHORITY = install_test_authority(
        monkeypatch,
        release_commit_sha=RELEASE_SHA,
        release_image_digest=IMAGE_DIGEST,
        now=NOW + timedelta(minutes=1),
    )


def _policy() -> dict[str, object]:
    return json.loads(operations.OPERATIONS_POLICY_PATH.read_text(encoding="utf-8"))


def _policy_sha256() -> str:
    return operations.sha256_bytes(operations.OPERATIONS_POLICY_PATH.read_bytes())


def _write(path: Path, payload: object) -> bytes:
    raw = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    path.write_bytes(raw)
    return raw


def _common(
    *,
    schema: str,
    producer: str,
    evidence: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": schema,
        "producer": producer,
        "captured_at": operations.isoformat(NOW + timedelta(seconds=10)),
        "release": {
            "commit_sha": RELEASE_SHA,
            "image_digest": IMAGE_DIGEST,
        },
        "replica_ids": ["api-a", "worker-a"],
        "policy_sha256": _policy_sha256(),
        "window": {
            "start": operations.isoformat(NOW - timedelta(hours=6)),
            "end": operations.isoformat(NOW),
        },
        "evidence": dict(evidence),
    }


def _dashboard() -> dict[str, object]:
    policy = _policy()
    panel_evidence = []
    for panel in policy["panels"]:
        contract_value = (
            panel["queries"] if "queries" in panel else panel["query_contract"]
        )
        panel_evidence.append(
            {
                "panel_id": panel["id"],
                "query_contract_sha256": operations.sha256_bytes(
                    operations.canonical_json_bytes(contract_value)
                ),
                "status": "rendered",
                "sample_count": 1,
            }
        )
    return AUTHORITY.authenticate(
        _common(
            schema=operations.DASHBOARD_RENDER_SCHEMA,
            producer=operations.DASHBOARD_RENDER_PRODUCER,
            evidence={
                "dashboard_id": policy["dashboard_id"],
                "title": policy["title"],
                "editable": policy["editable"],
                "release_filters": policy["release_filters"],
                "panel_evidence": panel_evidence,
                "artifact": {
                    "media_type": "image/png",
                    "byte_length": 12345,
                    "sha256": "d" * 64,
                },
            },
        ),
        domain=contract.DASHBOARD_RENDER_DOMAIN,
    )


def _log_query() -> dict[str, object]:
    panel = next(panel for panel in _policy()["panels"] if panel["source"] == "logs")
    query = panel["query_contract"]
    return AUTHORITY.authenticate(
        _common(
            schema=operations.STRUCTURED_LOG_QUERY_SCHEMA,
            producer=operations.STRUCTURED_LOG_QUERY_PRODUCER,
            evidence={
                "panel_id": panel["id"],
                "query_contract_sha256": operations.sha256_bytes(
                    operations.canonical_json_bytes(query)
                ),
                "required_fields": query["required_fields"],
                "filter_fields": query["filter_fields"],
                "private_payload_allowed": query["private_payload_allowed"],
                "response_sha256": "e" * 64,
                "samples": [
                    {
                        "timestamp": operations.isoformat(NOW - timedelta(minutes=2)),
                        "service": "propertyquarry",
                        "event": "provider.completed",
                        "correlation_id": "request-correlation-1",
                        "trace_id": TRACE_ID,
                        "span_id": SPAN_IDS[1],
                        "release_commit_sha": RELEASE_SHA,
                        "release_image_digest": IMAGE_DIGEST,
                        "replica_id": "worker-a",
                    }
                ],
            },
        ),
        domain=contract.STRUCTURED_LOG_QUERY_DOMAIN,
    )


def _trace_query() -> dict[str, object]:
    panel = next(
        panel for panel in _policy()["panels"] if panel["source"] == "traces"
    )
    query = panel["query_contract"]
    spans = [
        {
            "boundary": "customer_api",
            "trace_id": TRACE_ID,
            "span_id": SPAN_IDS[0],
            "parent_span_id": None,
            "release_commit_sha": RELEASE_SHA,
            "release_image_digest": IMAGE_DIGEST,
            "replica_id": "api-a",
            "started_at": operations.isoformat(NOW - timedelta(minutes=5)),
            "ended_at": operations.isoformat(NOW - timedelta(minutes=4)),
        },
        {
            "boundary": "durable_search_worker",
            "trace_id": TRACE_ID,
            "span_id": SPAN_IDS[1],
            "parent_span_id": SPAN_IDS[0],
            "release_commit_sha": RELEASE_SHA,
            "release_image_digest": IMAGE_DIGEST,
            "replica_id": "worker-a",
            "started_at": operations.isoformat(NOW - timedelta(minutes=3)),
            "ended_at": operations.isoformat(NOW - timedelta(minutes=2)),
        },
        {
            "boundary": "provider_or_render_boundary",
            "trace_id": TRACE_ID,
            "span_id": SPAN_IDS[2],
            "parent_span_id": SPAN_IDS[1],
            "release_commit_sha": RELEASE_SHA,
            "release_image_digest": IMAGE_DIGEST,
            "replica_id": "worker-a",
            "started_at": operations.isoformat(NOW - timedelta(minutes=2)),
            "ended_at": operations.isoformat(NOW - timedelta(minutes=1)),
        },
    ]
    return AUTHORITY.authenticate(
        _common(
            schema=operations.DISTRIBUTED_TRACE_QUERY_SCHEMA,
            producer=operations.DISTRIBUTED_TRACE_QUERY_PRODUCER,
            evidence={
                "panel_id": panel["id"],
                "query_contract_sha256": operations.sha256_bytes(
                    operations.canonical_json_bytes(query)
                ),
                "propagation_format": query["propagation_format"],
                "same_trace_id": True,
                "release_attributes_present": True,
                "response_sha256": "f" * 64,
                "spans": spans,
            },
        ),
        domain=contract.DISTRIBUTED_TRACE_QUERY_DOMAIN,
    )


def _bundle(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "dashboard": tmp_path / "dashboard.json",
        "logs": tmp_path / "logs.json",
        "traces": tmp_path / "traces.json",
    }
    _write(paths["dashboard"], _dashboard())
    _write(paths["logs"], _log_query())
    _write(paths["traces"], _trace_query())
    return paths


def _verify(paths: dict[str, Path], **kwargs: object) -> dict[str, object]:
    return operations.verify_operations_evidence(
        release_commit_sha=RELEASE_SHA,
        release_image_digest=IMAGE_DIGEST,
        dashboard_render_receipt_path=paths["dashboard"],
        structured_log_query_receipt_path=paths["logs"],
        distributed_trace_query_receipt_path=paths["traces"],
        now=NOW + timedelta(minutes=1),
        **kwargs,
    )


def _resign(
    paths: dict[str, Path],
    target: str,
    payload: dict[str, object],
) -> None:
    domain = {
        "dashboard": contract.DASHBOARD_RENDER_DOMAIN,
        "logs": contract.STRUCTURED_LOG_QUERY_DOMAIN,
        "traces": contract.DISTRIBUTED_TRACE_QUERY_DOMAIN,
    }[target]
    _write(paths[target], AUTHORITY.resign(payload, domain=domain))


def test_verifies_strict_live_operations_receipts_and_cross_links(
    tmp_path: Path,
) -> None:
    paths = _bundle(tmp_path)
    expected_hashes = {
        "dashboard_render_receipt": operations.sha256_bytes(
            paths["dashboard"].read_bytes()
        ),
        "structured_log_query_receipt": operations.sha256_bytes(
            paths["logs"].read_bytes()
        ),
        "distributed_trace_query_receipt": operations.sha256_bytes(
            paths["traces"].read_bytes()
        ),
    }

    result = _verify(paths, expected_input_hashes=expected_hashes)

    assert result["status"] == "verified"
    assert result["source_contract_status"] == "defined_not_live_evidence"
    assert result["replica_ids"] == ["api-a", "worker-a"]
    assert result["shared_input_hashes"] == expected_hashes
    assert result["receipts"]["dashboard_render"]["panel_count"] == len(
        _policy()["panels"]
    )
    assert result["receipts"]["structured_log_query"]["trace_id"] == TRACE_ID
    assert result["receipts"]["distributed_trace_query"]["trace_id"] == TRACE_ID
    assert result["cross_receipt_links_verified"] is True
    assert result["payload_sha256"] == operations.compute_payload_sha256(result)


def test_rejects_unsigned_tampering(tmp_path: Path) -> None:
    paths = _bundle(tmp_path)
    payload = json.loads(paths["dashboard"].read_text(encoding="utf-8"))
    payload["evidence"]["artifact"]["byte_length"] += 1
    _write(paths["dashboard"], payload)

    with pytest.raises(
        operations.OperationsEvidenceValidationError,
        match="payload_sha256|signature",
    ):
        _verify(paths)


def test_rejects_rendered_panel_without_samples(tmp_path: Path) -> None:
    paths = _bundle(tmp_path)
    payload = json.loads(paths["dashboard"].read_text(encoding="utf-8"))
    payload["evidence"]["panel_evidence"][0]["sample_count"] = 0
    _resign(paths, "dashboard", payload)

    with pytest.raises(
        operations.OperationsEvidenceValidationError,
        match="positive bounded JSON integer",
    ):
        _verify(paths)


def test_rejects_differing_receipt_replica_sets(tmp_path: Path) -> None:
    paths = _bundle(tmp_path)
    payload = json.loads(paths["dashboard"].read_text(encoding="utf-8"))
    payload["replica_ids"] = ["api-a"]
    _resign(paths, "dashboard", payload)

    with pytest.raises(
        operations.OperationsEvidenceValidationError,
        match="exact replica list",
    ):
        _verify(paths)


@pytest.mark.parametrize("target", ["logs", "traces"])
def test_rejects_evidence_outside_declared_replicas(
    tmp_path: Path,
    target: str,
) -> None:
    paths = _bundle(tmp_path)
    payload = json.loads(paths[target].read_text(encoding="utf-8"))
    if target == "logs":
        payload["evidence"]["samples"][0]["replica_id"] = "worker-z"
    else:
        payload["evidence"]["spans"][1]["replica_id"] = "worker-z"
    _resign(paths, target, payload)

    with pytest.raises(
        operations.OperationsEvidenceValidationError,
        match="outside the declared replica set",
    ):
        _verify(paths)


@pytest.mark.parametrize(
    "raw, expected",
    [
        (
            b'{"schema_version":"one","schema_version":"two"}',
            "duplicate JSON key",
        ),
        (b'{"schema_version":NaN}', "non-finite JSON constant"),
    ],
)
def test_rejects_duplicate_keys_and_non_finite_json(
    tmp_path: Path,
    raw: bytes,
    expected: str,
) -> None:
    paths = _bundle(tmp_path)
    paths["dashboard"].write_bytes(raw)

    with pytest.raises(
        operations.OperationsEvidenceValidationError,
        match=expected,
    ):
        _verify(paths)


@pytest.mark.parametrize("mutation", ["policy", "panel_coverage"])
def test_rejects_wrong_policy_or_panel_coverage(
    tmp_path: Path,
    mutation: str,
) -> None:
    paths = _bundle(tmp_path)
    payload = json.loads(paths["dashboard"].read_text(encoding="utf-8"))
    if mutation == "policy":
        payload["policy_sha256"] = "0" * 64
        expected = "canonical operations policy"
    else:
        payload["evidence"]["panel_evidence"].pop()
        expected = "panel coverage"
    _resign(paths, "dashboard", payload)

    with pytest.raises(
        operations.OperationsEvidenceValidationError,
        match=expected,
    ):
        _verify(paths)


def test_rejects_stale_receipt(tmp_path: Path) -> None:
    paths = _bundle(tmp_path)
    payload = json.loads(paths["logs"].read_text(encoding="utf-8"))
    payload["captured_at"] = operations.isoformat(NOW - timedelta(minutes=20))
    payload["window"] = {
        "start": operations.isoformat(NOW - timedelta(hours=6, minutes=20)),
        "end": operations.isoformat(NOW - timedelta(minutes=20)),
    }
    _resign(paths, "logs", payload)

    with pytest.raises(operations.OperationsEvidenceValidationError):
        _verify(paths)


def test_rejects_release_mismatch_even_when_resigned(tmp_path: Path) -> None:
    paths = _bundle(tmp_path)
    payload = json.loads(paths["logs"].read_text(encoding="utf-8"))
    payload["release"]["commit_sha"] = "9" * 40
    _resign(paths, "logs", payload)

    with pytest.raises(
        operations.OperationsEvidenceValidationError,
        match="different release",
    ):
        _verify(paths)


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_parent",
        "cycle",
        "boundary_order",
        "wrong_root",
        "star_topology",
        "cross_link",
    ],
)
def test_rejects_invalid_trace_graph_or_log_trace_cross_link(
    tmp_path: Path,
    mutation: str,
) -> None:
    paths = _bundle(tmp_path)
    if mutation == "cross_link":
        payload = json.loads(paths["logs"].read_text(encoding="utf-8"))
        payload["evidence"]["samples"][0]["span_id"] = "9" * 16
        _resign(paths, "logs", payload)
        expected = "not linked"
    else:
        payload = json.loads(paths["traces"].read_text(encoding="utf-8"))
        spans = payload["evidence"]["spans"]
        if mutation == "unknown_parent":
            spans[1]["parent_span_id"] = "9" * 16
            expected = "unknown parent"
        elif mutation == "cycle":
            spans[0]["parent_span_id"] = SPAN_IDS[2]
            expected = "exactly one root|cycle"
        elif mutation == "boundary_order":
            spans.reverse()
            expected = "boundary order"
        elif mutation == "star_topology":
            spans[2]["parent_span_id"] = SPAN_IDS[0]
            expected = "canonical API-to-worker-to-provider parent chain"
        else:
            spans[0]["parent_span_id"] = SPAN_IDS[1]
            spans[1]["parent_span_id"] = None
            expected = "root must be"
        _resign(paths, "traces", payload)

    with pytest.raises(
        operations.OperationsEvidenceValidationError,
        match=expected,
    ):
        _verify(paths)


def test_rejects_shared_input_hash_substitution(tmp_path: Path) -> None:
    paths = _bundle(tmp_path)

    with pytest.raises(
        operations.OperationsEvidenceValidationError,
        match="shared launch input hash set",
    ):
        _verify(
            paths,
            expected_input_hashes={
                "dashboard_render_receipt": "0" * 64,
                "structured_log_query_receipt": "0" * 64,
                "distributed_trace_query_receipt": "0" * 64,
            },
        )


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "peer_writable"])
def test_strict_reader_rejects_untrusted_file_identity(
    tmp_path: Path,
    kind: str,
) -> None:
    source = tmp_path / "source.json"
    source.write_text('{"status":"verified"}\n', encoding="utf-8")
    candidate = tmp_path / "candidate.json"
    if kind == "symlink":
        candidate.symlink_to(source)
    elif kind == "hardlink":
        os.link(source, candidate)
    else:
        candidate.write_bytes(source.read_bytes())
        candidate.chmod(0o666)

    with pytest.raises(
        operations.OperationsEvidenceValidationError,
        match="unsafe|singly linked|immutable to peers",
    ):
        operations._load_strict_json(
            candidate,
            field="adversarial receipt",
            maximum_bytes=4096,
        )


def test_strict_reader_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "receipt.fifo"
    os.mkfifo(fifo, mode=0o600)
    outcomes: list[BaseException | object] = []

    def read_fifo() -> None:
        try:
            outcomes.append(
                operations._load_strict_json(
                    fifo,
                    field="FIFO receipt",
                    maximum_bytes=4096,
                )
            )
        except BaseException as exc:  # pragma: no branch - outcome is asserted below
            outcomes.append(exc)

    reader = threading.Thread(target=read_fifo, daemon=True)
    reader.start()
    reader.join(timeout=1.0)
    if reader.is_alive():
        # Release an implementation that incorrectly used a blocking open so
        # the test can fail cleanly instead of leaving a stuck worker.
        writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(writer)
        reader.join(timeout=1.0)
        pytest.fail("strict JSON reader blocked while opening a FIFO")

    assert len(outcomes) == 1
    assert isinstance(outcomes[0], operations.OperationsEvidenceValidationError)


def test_strict_reader_detects_path_replacement_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "receipt.json"
    candidate.write_text('{"status":"verified"}\n', encoding="utf-8")
    replacement = tmp_path / "replacement.json"
    replacement.write_text('{"status":"attacker"}\n', encoding="utf-8")
    real_read = operations.secure_file_io.os.read
    replaced = False

    def replacing_read(descriptor: int, count: int) -> bytes:
        nonlocal replaced
        chunk = real_read(descriptor, count)
        if not replaced:
            replaced = True
            os.replace(replacement, candidate)
        return chunk

    monkeypatch.setattr(operations.secure_file_io.os, "read", replacing_read)
    with pytest.raises(
        operations.OperationsEvidenceValidationError,
        match="changed while it was read",
    ):
        operations._load_strict_json(
            candidate,
            field="replaced receipt",
            maximum_bytes=4096,
        )


def test_atomic_output_replaces_symlink_without_following_target(
    tmp_path: Path,
) -> None:
    victim = tmp_path / "victim.json"
    victim.write_text("do not replace\n", encoding="utf-8")
    output = tmp_path / "verification.json"
    output.symlink_to(victim)
    payload = operations.add_payload_sha256({"status": "verified"})

    operations.atomic_write_json(output, payload, overwrite=True)

    assert not output.is_symlink()
    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert victim.read_text(encoding="utf-8") == "do not replace\n"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_atomic_no_overwrite_preserves_concurrent_creator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "verification.json"
    payload = operations.add_payload_sha256({"status": "verified"})
    concurrent_payload = b'{"owner":"concurrent"}\n'
    real_link = operations.secure_file_io.os.link

    def racing_link(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        assert dst_dir_fd is not None
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=dst_dir_fd,
        )
        try:
            os.write(descriptor, concurrent_payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(operations.secure_file_io.os, "link", racing_link)
    with pytest.raises(
        operations.OperationsEvidenceValidationError,
        match="already exists",
    ):
        operations.atomic_write_json(output, payload, overwrite=False)

    assert output.read_bytes() == concurrent_payload


def test_atomic_output_rejects_symlinked_parent(tmp_path: Path) -> None:
    redirected_parent = tmp_path / "redirected"
    redirected_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(redirected_parent, target_is_directory=True)

    with pytest.raises(
        operations.OperationsEvidenceValidationError,
        match="without following links",
    ):
        operations.atomic_write_json(
            linked_parent / "verification.json",
            {"status": "verified"},
            overwrite=True,
        )

    assert not (redirected_parent / "verification.json").exists()


def test_atomic_output_parent_swap_cannot_redirect_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "evidence"
    parent.mkdir()
    moved_parent = tmp_path / "original-evidence"
    redirected_parent = tmp_path / "redirected"
    redirected_parent.mkdir()
    output = parent / "verification.json"
    real_replace = operations.secure_file_io.os.replace
    swapped = False

    def swapping_replace(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            parent.rename(moved_parent)
            parent.symlink_to(redirected_parent, target_is_directory=True)
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(
        operations.secure_file_io.os,
        "replace",
        swapping_replace,
    )
    with pytest.raises(
        operations.OperationsEvidenceValidationError,
        match="directory chain changed",
    ):
        operations.atomic_write_json(
            output,
            {"status": "verified"},
            overwrite=True,
        )

    assert not (redirected_parent / output.name).exists()
    assert (moved_parent / output.name).is_file()
