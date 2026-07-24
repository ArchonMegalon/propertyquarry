#!/usr/bin/env python3
"""Fixed no-argument classifier for one interrupted governed Prater release."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
from pathlib import Path

for _candidate in (Path("/app"), Path(__file__).resolve().parents[1]):
    if (_candidate / "scripts").is_dir():
        sys.path.insert(0, str(_candidate))
        break

from scripts import propertyquarry_prater_ai_panorama_controller as controller


ENTRYPOINT_PATH = Path(
    "/usr/local/libexec/propertyquarry-prater-ai-panorama-recovery-v1.py"
)
RESULT_SCHEMA = (
    "propertyquarry.prater-ai-panorama-recovery-classification.v1"
)
ENTRYPOINT_UID = 0
ENTRYPOINT_GID = 0
ENTRYPOINT_MODE = 0o555
EXECUTION_UID = 0
EXECUTION_GID = 0
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")


def _self_attest() -> None:
    descriptor = -1
    try:
        if Path(__file__) != ENTRYPOINT_PATH:
            raise RuntimeError("prater-recovery-entrypoint-invalid")
        before = ENTRYPOINT_PATH.stat(follow_symlinks=False)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or int(before.st_uid) != ENTRYPOINT_UID
            or int(before.st_gid) != ENTRYPOINT_GID
            or stat.S_IMODE(before.st_mode) != ENTRYPOINT_MODE
            or int(before.st_nlink) != 1
        ):
            raise RuntimeError("prater-recovery-entrypoint-invalid")
        descriptor = os.open(
            ENTRYPOINT_PATH,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        after = ENTRYPOINT_PATH.stat(follow_symlinks=False)
        identity = (
            int(before.st_dev),
            int(before.st_ino),
            int(before.st_mode),
            int(before.st_uid),
            int(before.st_gid),
            int(before.st_nlink),
            int(before.st_size),
            int(before.st_mtime_ns),
            int(before.st_ctime_ns),
        )
        for observed in (opened, after):
            if (
                int(observed.st_dev),
                int(observed.st_ino),
                int(observed.st_mode),
                int(observed.st_uid),
                int(observed.st_gid),
                int(observed.st_nlink),
                int(observed.st_size),
                int(observed.st_mtime_ns),
                int(observed.st_ctime_ns),
            ) != identity:
                raise RuntimeError("prater-recovery-entrypoint-changed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def run() -> dict[str, object]:
    if os.geteuid() != EXECUTION_UID or os.getegid() != EXECUTION_GID:
        raise RuntimeError("prater-recovery-root-required")
    _self_attest()
    components = controller._components()
    request = controller._load_request(components.admission)
    with controller._database_secret_environment(components.admission):
        trusted = (
            components.admission.load_ai_panorama_install_trusted_context()
        )
        expected = controller._expected_bindings(
            components,
            request,
            trusted,
        )
        recovered = (
            components.release.recover_prater_ai_panorama_historical_operation(
                request["permit_relpath"],
                expected,
            )
        )
    required = {
        "classification",
        "event",
        "operation_id",
        "operation_terminal_entry_sha256",
        "operation_terminal_evidence_sha256",
        "permit_sha256",
        "request_id_sha256",
        "database_mutation_performed",
        "public_target_mutation_performed",
        "private_values_redacted",
    }
    classification = recovered.get("classification")
    event = recovered.get("event")
    if (
        type(recovered) is not dict
        or set(recovered) != required
        or classification
        not in {
            "committed",
            "failed-clean",
            "rolled-back",
            "recovery-required",
        }
        or (
            event != (
                "consumed-failed-clean"
                if classification == "failed-clean"
                and event == "consumed-failed-clean"
                else classification
            )
        )
        or any(
            type(recovered.get(field)) is not str
            or _DIGEST_RE.fullmatch(recovered[field]) is None
            for field in (
                "operation_id",
                "operation_terminal_entry_sha256",
                "operation_terminal_evidence_sha256",
                "permit_sha256",
                "request_id_sha256",
            )
        )
        or recovered.get("database_mutation_performed") is not False
        or recovered.get("public_target_mutation_performed") is not False
        or recovered.get("private_values_redacted") is not True
    ):
        raise RuntimeError("prater-recovery-result-invalid")
    request_id_sha256 = hashlib.sha256(
        request["request_id"].encode("ascii")
    ).hexdigest()
    if recovered["request_id_sha256"] != request_id_sha256:
        raise RuntimeError("prater-recovery-result-invalid")
    terminal = {
        "schema": controller.TERMINAL_SCHEMA,
        "version": 1,
        "authority": controller.AUTHORITY,
        "status": classification,
        "request_id_sha256": request_id_sha256,
        "permit_sha256": recovered["permit_sha256"],
        "operation_id_sha256": recovered["operation_id"],
        "operation_terminal_entry_sha256": recovered[
            "operation_terminal_entry_sha256"
        ],
        "operation_terminal_evidence_sha256": recovered[
            "operation_terminal_evidence_sha256"
        ],
        "database_mutation_performed": False,
        "public_target_mutation_performed": False,
        "private_values_redacted": True,
    }
    terminal_receipt_sha256 = (
        controller._write_or_validate_terminal_receipt(
            components.admission,
            relpath=controller._terminal_relpath(request["request_id"]),
            payload=terminal,
        )
    )
    return {
        "schema": RESULT_SCHEMA,
        "version": 1,
        "authority": controller.AUTHORITY,
        "status": "classified",
        "classification": classification,
        "request_id_sha256": request_id_sha256,
        "permit_sha256": recovered["permit_sha256"],
        "operation_id_sha256": recovered["operation_id"],
        "operation_terminal_entry_sha256": recovered[
            "operation_terminal_entry_sha256"
        ],
        "terminal_receipt_sha256": terminal_receipt_sha256,
        "database_mutation_performed": False,
        "public_target_mutation_performed": False,
        "retry_authorized": False,
        "private_values_redacted": True,
    }


def main() -> int:
    if len(sys.argv) != 1 or Path(sys.argv[0]) != ENTRYPOINT_PATH:
        return 2
    try:
        result = run()
    except Exception:
        return 1
    sys.stdout.buffer.write(controller._canonical(result))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
