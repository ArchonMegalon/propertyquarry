#!/usr/bin/env python3
"""Authenticated private receiver for PropertyQuarry release-proof alerts."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping, Sequence

_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_IMPORT_ROOT))
from scripts import propertyquarry_observability_receipts as receipts
from scripts import propertyquarry_evidence_contract as evidence_contract


MAX_WEBHOOK_BYTES = 1024 * 1024
MAX_IDENTITY_BYTES = 4096


class ProofReceiverError(RuntimeError):
    """The proof webhook or receiver configuration is unsafe."""


def _mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ProofReceiverError(f"{field} must be an object")
    return value


def _text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProofReceiverError(f"{field} must be a non-empty trimmed string")
    return value


def _timestamp(value: object, *, field: str) -> datetime:
    text = _text(value, field=field)
    if not text.endswith("Z"):
        raise ProofReceiverError(f"{field} must be a UTC timestamp ending in Z")
    try:
        return datetime.fromisoformat(text[:-1] + "+00:00").astimezone(timezone.utc)
    except ValueError as exc:
        raise ProofReceiverError(f"{field} is not an ISO-8601 timestamp") from exc


def _strict_json(raw: bytes) -> Mapping[str, object]:
    def unique(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ProofReceiverError(f"duplicate webhook JSON key is forbidden: {key}")
            result[key] = value
        return result

    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ProofReceiverError(f"non-finite webhook JSON is forbidden: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProofReceiverError("webhook is not strict UTF-8 JSON") from exc
    return _mapping(parsed, field="webhook")


def build_alert_delivery_receipt(
    raw_acknowledgement: bytes,
    *,
    receiver_instance_sha256: str,
    received_at: datetime,
) -> dict[str, object]:
    """Validate and cache a gateway-signed ack; never mint one locally."""

    if not raw_acknowledgement or len(raw_acknowledgement) > MAX_WEBHOOK_BYTES:
        raise ProofReceiverError("operator acknowledgement size is invalid")
    if not receipts.SHA256_RE.fullmatch(receiver_instance_sha256):
        raise ProofReceiverError("receiver instance identity hash is invalid")
    acknowledgement = _strict_json(raw_acknowledgement)
    release = _mapping(
        acknowledgement.get("release"), field="operator acknowledgement release"
    )
    commit_sha = _text(release.get("commit_sha"), field="operator acknowledgement release SHA")
    image_digest = _text(
        release.get("image_digest"), field="operator acknowledgement image digest"
    )
    received = received_at.astimezone(timezone.utc)
    try:
        anchor, challenge = evidence_contract.load_evidence_challenge(
            expected_commit_sha=commit_sha,
            expected_image_digest=image_digest,
            now=received,
        )
        operator_gateway_trust = evidence_contract.load_operator_gateway_trust(
            evidence_anchor=anchor
        )
        receipts.validate_alert_delivery_receipt(
            acknowledgement,
            expected_commit_sha=commit_sha,
            expected_image_digest=image_digest,
            operator_gateway_trust=operator_gateway_trust,
            challenge=challenge,
            now=received,
        )
    except (evidence_contract.EvidenceContractError, receipts.ReceiptValidationError) as exc:
        raise ProofReceiverError(str(exc)) from exc
    return dict(acknowledgement)


def _secure_file(path: Path, *, field: str) -> bytes:
    if not path.is_absolute():
        raise ProofReceiverError(f"{field} path must be absolute")
    try:
        evidence_contract.assert_secure_external_parent(path, field=field)
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except (OSError, evidence_contract.EvidenceContractError) as exc:
        raise ProofReceiverError(f"{field} is unavailable or externally unsafe") from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or stat.S_IMODE(before.st_mode) & 0o077
            or not 0 < before.st_size <= MAX_IDENTITY_BYTES
        ):
            raise ProofReceiverError(f"{field} ownership, permissions, or size are invalid")
        raw = os.read(fd, MAX_IDENTITY_BYTES + 1)
        after = os.fstat(fd)
        if any(
            getattr(before, name) != getattr(after, name)
            for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        ) or len(raw) != before.st_size:
            raise ProofReceiverError(f"{field} changed while it was read")
        return raw
    finally:
        os.close(fd)


def _private_bind_address(raw: str) -> str:
    value = str(raw or "").strip()
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ProofReceiverError("bind address must be a private IP literal") from exc
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if (
        address.is_unspecified
        or address.is_multicast
        or not (address.is_loopback or address.is_private)
    ):
        raise ProofReceiverError("bind address must be loopback or private")
    return address.compressed


class ProofReceiverServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        token: str,
        receipt_directory: Path,
        receiver_instance_sha256: str,
    ) -> None:
        super().__init__(server_address, ProofReceiverHandler)
        self.token = token
        self.receipt_directory = receipt_directory
        self.receiver_instance_sha256 = receiver_instance_sha256


class ProofReceiverHandler(BaseHTTPRequestHandler):
    server: ProofReceiverServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _authorized(self) -> bool:
        expected = f"Bearer {self.server.token}"
        supplied = str(self.headers.get("Authorization") or "")
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def _json(self, status: HTTPStatus, payload: object) -> None:
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _refuse(self, status: HTTPStatus) -> None:
        self._json(status, {"error": "request refused"})

    def do_POST(self) -> None:
        if self.path != "/v1/alerts" or not self._authorized():
            self._refuse(HTTPStatus.NOT_FOUND if self.path != "/v1/alerts" else HTTPStatus.UNAUTHORIZED)
            return
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or not raw_length.isdigit() or not 0 < int(raw_length) <= MAX_WEBHOOK_BYTES:
            self._refuse(HTTPStatus.BAD_REQUEST)
            return
        raw = self.rfile.read(int(raw_length))
        try:
            receipt = build_alert_delivery_receipt(
                raw,
                receiver_instance_sha256=self.server.receiver_instance_sha256,
                received_at=datetime.now(timezone.utc),
            )
            output = self.server.receipt_directory / f"{receipt['nonce']}.json"
            receipts.atomic_write_json(output, receipt, overwrite=False)
        except (ProofReceiverError, receipts.ReceiptValidationError):
            self._refuse(HTTPStatus.BAD_REQUEST)
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:
        match = re.fullmatch(r"/receipts/([0-9a-f]{32})", self.path)
        if match is None or not self._authorized():
            self._refuse(HTTPStatus.NOT_FOUND if match is None else HTTPStatus.UNAUTHORIZED)
            return
        path = self.server.receipt_directory / f"{match.group(1)}.json"
        try:
            raw = path.read_bytes()
        except OSError:
            self._refuse(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9199)
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path("/run/secrets/propertyquarry_alert_proof_receiver_token"),
    )
    parser.add_argument(
        "--instance-id-file",
        type=Path,
        default=Path("/run/secrets/propertyquarry_alert_proof_receiver_instance"),
    )
    parser.add_argument("--receipt-directory", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bind = _private_bind_address(args.bind)
        if not 1 <= args.port <= 65535:
            raise ProofReceiverError("port must be between 1 and 65535")
        token_raw = _secure_file(args.token_file, field="proof receiver token file")
        instance_raw = _secure_file(args.instance_id_file, field="proof receiver instance file")
        token = token_raw.decode("utf-8").strip()
        if not token or "\n" in token or "\r" in token:
            raise ProofReceiverError("proof receiver token is invalid")
        if not args.receipt_directory.is_absolute():
            raise ProofReceiverError("receipt directory must be absolute")
        evidence_contract.assert_secure_external_parent(
            args.receipt_directory,
            field="proof receipt directory",
        )
        args.receipt_directory.mkdir(exist_ok=True, mode=0o700)
        receipt_metadata = args.receipt_directory.lstat()
        if (
            not stat.S_ISDIR(receipt_metadata.st_mode)
            or receipt_metadata.st_uid != 0
            or stat.S_IMODE(receipt_metadata.st_mode) & 0o077
        ):
            raise ProofReceiverError(
                "receipt directory must be a root-owned private directory"
            )
        server = ProofReceiverServer(
            (bind, args.port),
            token=token,
            receipt_directory=args.receipt_directory,
            receiver_instance_sha256=receipts.sha256_bytes(instance_raw),
        )
    except (
        OSError,
        UnicodeDecodeError,
        ProofReceiverError,
        evidence_contract.EvidenceContractError,
    ) as exc:
        print(f"proof receiver refused to start: {exc}", file=sys.stderr)
        return 2
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
