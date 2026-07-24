from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.product import property_tour_ai_panorama_operation_journal as journal


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _empty_journal() -> dict[str, object]:
    return {
        "schema": journal.OPERATION_JOURNAL_SCHEMA,
        "authority": journal.OPERATION_JOURNAL_AUTHORITY,
        "instance_id": "a" * 32,
        "sequence": 0,
        "tip_sha256": "0" * 64,
        "entries": [],
    }


def _admission() -> SimpleNamespace:
    return SimpleNamespace(
        permit_sha256="b" * 64,
        request_id="request-prater-release-001",
        nonce="c" * 32,
        _context_sha256="d" * 64,
        _ledger_instance_id="e" * 32,
        _ledger_sequence=7,
        _ledger_entry_sha256="f" * 64,
        nonce_consumed=True,
    )


@pytest.fixture
def journal_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    control_root = tmp_path / "controller"
    control_root.mkdir(mode=0o700)
    journal_path = control_root / journal.OPERATION_JOURNAL_NAME
    journal_path.write_bytes(_canonical(_empty_journal()))
    journal_path.chmod(0o600)
    lock_path = control_root / journal.OPERATION_JOURNAL_LOCK_NAME
    lock_path.write_bytes(b"lock\n")
    lock_path.chmod(0o600)
    monkeypatch.setattr(
        journal.admission_authority,
        "_CONTROLLER_PATHS",
        SimpleNamespace(
            control_root=control_root,
            required_uid=os.geteuid(),
        ),
    )

    def _open_control_root() -> int:
        return os.open(
            control_root,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )

    monkeypatch.setattr(
        journal.admission_authority,
        "_open_control_root",
        _open_control_root,
    )
    monkeypatch.setattr(
        journal.admission_authority,
        "revalidate_ai_panorama_install_admission",
        lambda admission, *, require_consumed: (
            admission
            if require_consumed and admission.nonce_consumed is True
            else (_ for _ in ()).throw(ValueError("not consumed"))
        ),
    )
    monkeypatch.setattr(
        journal.admission_authority,
        "revalidate_ai_panorama_install_recovery",
        lambda admission: SimpleNamespace(
            permit_sha256=admission.permit_sha256,
            request_id_sha256=journal._sha256(
                admission.request_id.encode("utf-8")
            ),
            nonce_sha256=journal._sha256(admission.nonce.encode("ascii")),
            context_sha256=admission._context_sha256,
            ledger_instance_id=admission._ledger_instance_id,
            ledger_sequence=admission._ledger_sequence,
            ledger_entry_sha256=admission._ledger_entry_sha256,
        ),
        raising=False,
    )
    monkeypatch.setattr(
        journal.admission_authority,
        "recover_ai_panorama_install_consumption",
        lambda _permit_relpath, expected: expected.recovery,
        raising=False,
    )
    return control_root


def test_operation_journal_records_hash_chained_terminal_evidence(
    journal_root: Path,
) -> None:
    admission = _admission()
    handle = journal.begin_ai_panorama_install_operation(
        admission,
        evidence={
            "phase": "prepared",
            "target_manifest": {"state": "absent"},
        },
    )
    terminal_sha256 = journal.finish_ai_panorama_install_operation(
        handle,
        event="committed",
        evidence={
            "phase": "committed",
            "target_manifest": {
                "state": "present",
                "tree_sha256": "e" * 64,
            },
        },
    )

    payload = json.loads(
        (journal_root / journal.OPERATION_JOURNAL_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert payload["sequence"] == 2
    assert payload["tip_sha256"] == terminal_sha256
    assert [entry["event"] for entry in payload["entries"]] == [
        "prepared",
        "committed",
    ]
    assert (
        payload["entries"][1]["previous_entry_sha256"]
        == payload["entries"][0]["entry_sha256"]
    )
    assert payload["entries"][0]["permit_sha256"] == admission.permit_sha256


def test_operation_journal_rejects_tamper_and_duplicate_operation(
    journal_root: Path,
) -> None:
    admission = _admission()
    handle = journal.begin_ai_panorama_install_operation(
        admission,
        evidence={"phase": "prepared"},
    )
    journal.finish_ai_panorama_install_operation(
        handle,
        event="failed-clean",
        evidence={"phase": "failed-clean"},
    )
    with pytest.raises(
        journal.AiPanoramaOperationJournalError,
        match="ai_panorama_operation_journal_transition_invalid",
    ):
        journal.begin_ai_panorama_install_operation(
            admission,
            evidence={"phase": "prepared-again"},
        )

    journal_path = journal_root / journal.OPERATION_JOURNAL_NAME
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    payload["entries"][0]["evidence"]["phase"] = "tampered"
    journal_path.write_bytes(_canonical(payload))
    journal_path.chmod(0o600)
    with pytest.raises(
        journal.AiPanoramaOperationJournalError,
        match="ai_panorama_operation_journal_invalid",
    ):
        journal._open_locked_journal()


def test_operation_journal_rejects_redigested_terminal_binding_mismatch(
    journal_root: Path,
) -> None:
    admission = _admission()
    handle = journal.begin_ai_panorama_install_operation(
        admission,
        evidence={"phase": "prepared"},
    )
    journal.finish_ai_panorama_install_operation(
        handle,
        event="committed",
        evidence={"phase": "committed"},
    )
    journal_path = journal_root / journal.OPERATION_JOURNAL_NAME
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    terminal = payload["entries"][-1]
    terminal["nonce_sha256"] = "1" * 64
    terminal["operation_id"] = journal._sha256(
        b"propertyquarry.ai-panorama-install-operation.v1\0"
        + bytes.fromhex(terminal["permit_sha256"])
        + bytes.fromhex(terminal["request_id_sha256"])
        + bytes.fromhex(terminal["nonce_sha256"])
        + bytes.fromhex(terminal["context_sha256"])
    )
    unsigned = dict(terminal)
    unsigned.pop("entry_sha256")
    terminal["entry_sha256"] = journal._entry_digest(unsigned)
    payload["tip_sha256"] = terminal["entry_sha256"]
    journal_path.write_bytes(_canonical(payload))
    journal_path.chmod(0o600)

    with pytest.raises(
        journal.AiPanoramaOperationJournalError,
        match="ai_panorama_operation_journal_transition_invalid",
    ):
        journal._open_locked_journal()


def test_operation_journal_requires_consumed_admission(
    journal_root: Path,
) -> None:
    admission = _admission()
    admission.nonce_consumed = False
    with pytest.raises(ValueError, match="not consumed"):
        journal.begin_ai_panorama_install_operation(
            admission,
            evidence={"phase": "prepared"},
        )

    payload = json.loads(
        (journal_root / journal.OPERATION_JOURNAL_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert payload["sequence"] == 0


def test_terminalization_uses_recovery_evidence_not_live_install_lease(
    journal_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = _admission()
    handle = journal.begin_ai_panorama_install_operation(
        admission,
        evidence={"phase": "prepared"},
    )
    monkeypatch.setattr(
        journal.admission_authority,
        "revalidate_ai_panorama_install_admission",
        lambda *_args, **_kwargs: pytest.fail(
            "terminalization must not require a live execution lease"
        ),
    )

    terminal_sha256 = journal.finish_ai_panorama_install_operation(
        handle,
        event="recovery-required",
        evidence={"phase": "recovery-required"},
    )

    payload = json.loads(
        (journal_root / journal.OPERATION_JOURNAL_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert payload["tip_sha256"] == terminal_sha256
    assert payload["entries"][-1]["event"] == "recovery-required"


def test_process_restart_terminalizes_matching_prepared_operation_only(
    journal_root: Path,
) -> None:
    admission = _admission()
    journal.begin_ai_panorama_install_operation(
        admission,
        evidence={"phase": "prepared"},
    )
    recovery = (
        journal.admission_authority.VerifiedAiPanoramaInstallRecoveryEvidence(
            permit_sha256=admission.permit_sha256,
            request_id_sha256=journal._sha256(
                admission.request_id.encode("utf-8")
            ),
            nonce_sha256=journal._sha256(admission.nonce.encode("ascii")),
            context_sha256=admission._context_sha256,
            ledger_instance_id=admission._ledger_instance_id,
            ledger_sequence=admission._ledger_sequence,
            ledger_entry_sha256=admission._ledger_entry_sha256,
            consumed_at="2026-07-24T08:00:00Z",
            recovery_expires_at="2026-07-25T08:00:00Z",
        )
    )

    expected = SimpleNamespace(recovery=recovery)
    terminal_sha256 = journal.finish_recovered_ai_panorama_install_operation(
        "prater-permit.v2.json",
        expected,
        event="recovery-required",
        evidence={"phase": "recovery-required", "restart": True},
    )

    payload = json.loads(
        (journal_root / journal.OPERATION_JOURNAL_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert payload["tip_sha256"] == terminal_sha256
    assert [entry["event"] for entry in payload["entries"]] == [
        "prepared",
        "recovery-required",
    ]
    with pytest.raises(
        journal.AiPanoramaOperationJournalError,
        match="ai_panorama_operation_recovery_invalid",
    ):
        journal.finish_recovered_ai_panorama_install_operation(
            "prater-permit.v2.json",
            expected,
            event="failed-clean",
            evidence={"phase": "failed-clean"},
        )


def test_process_restart_rejects_constructed_recovery_evidence(
    journal_root: Path,
) -> None:
    admission = _admission()
    journal.begin_ai_panorama_install_operation(
        admission,
        evidence={"phase": "prepared"},
    )
    forged = (
        journal.admission_authority.VerifiedAiPanoramaInstallRecoveryEvidence(
            permit_sha256=admission.permit_sha256,
            request_id_sha256=journal._sha256(
                admission.request_id.encode("utf-8")
            ),
            nonce_sha256=journal._sha256(admission.nonce.encode("ascii")),
            context_sha256=admission._context_sha256,
            ledger_instance_id=admission._ledger_instance_id,
            ledger_sequence=admission._ledger_sequence,
            ledger_entry_sha256=admission._ledger_entry_sha256,
            consumed_at="2026-07-24T08:00:00Z",
            recovery_expires_at="2026-07-25T08:00:00Z",
        )
    )

    with pytest.raises(
        journal.AiPanoramaOperationJournalError,
        match="ai_panorama_operation_recovery_invalid",
    ):
        journal.finish_recovered_ai_panorama_install_operation(
            forged,  # type: ignore[arg-type]
            SimpleNamespace(recovery=forged),
            event="recovery-required",
            evidence={"phase": "recovery-required", "restart": True},
        )


@pytest.mark.parametrize(
    ("nested_name", "expected_code"),
    (
        (
            journal.OPERATION_JOURNAL_LOCK_NAME,
            "ai_panorama_operation_journal_lock_invalid",
        ),
        (
            journal.OPERATION_JOURNAL_NAME,
            "ai_panorama_operation_journal_unavailable",
        ),
    ),
)
def test_operation_journal_rejects_same_device_nested_mount(
    journal_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    nested_name: str,
    expected_code: str,
) -> None:
    real_mount_id = journal.admission_authority._descriptor_mount_id
    root_descriptor = os.open(
        journal_root,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        root_mount_id = real_mount_id(
            root_descriptor,
            code="test_mount_identity_unavailable",
        )
    finally:
        os.close(root_descriptor)

    def _same_device_nested_mount(
        descriptor: int,
        *,
        code: str,
    ) -> int:
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        if Path(target).name == nested_name:
            return root_mount_id + 1
        return root_mount_id

    monkeypatch.setattr(
        journal.admission_authority,
        "_descriptor_mount_id",
        _same_device_nested_mount,
    )
    with pytest.raises(
        journal.admission_authority.AiPanoramaInstallPermitError,
        match=expected_code,
    ):
        journal._open_locked_journal()
