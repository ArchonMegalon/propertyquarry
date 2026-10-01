#!/usr/bin/env python3
"""Reference contract for externally sealed deploy state caches.

The authoritative compare-and-swap store lives behind the independently
installed release controller.  Local journal and receipt-ledger files are
only caches.  This module defines the strict generation/hash-chain checks the
controller and cache readers share; it intentionally cannot mint an external
authority signature or lower the authority's generation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping


SCHEMA = "propertyquarry.deploy-monotonic-seal.v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
ZERO_HASH = "0" * 64


class MonotonicStateError(ValueError):
    pass


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class MonotonicSeal:
    authority_id: str
    kind: str
    target_identity_sha256: str
    generation: int
    previous_seal_sha256: str
    state_sha256: str
    seal_sha256: str
    authority_signature: str

    def unsigned_payload(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "authority_id": self.authority_id,
            "kind": self.kind,
            "target_identity_sha256": self.target_identity_sha256,
            "generation": self.generation,
            "previous_seal_sha256": self.previous_seal_sha256,
            "state_sha256": self.state_sha256,
        }


def parse_seal(payload: Mapping[str, Any]) -> MonotonicSeal:
    expected = {
        "schema",
        "authority_id",
        "kind",
        "target_identity_sha256",
        "generation",
        "previous_seal_sha256",
        "state_sha256",
        "seal_sha256",
        "authority_signature",
    }
    if set(payload) != expected or payload.get("schema") != SCHEMA:
        raise MonotonicStateError("external monotonic seal fields are invalid")
    authority_id = str(payload["authority_id"] or "")
    kind = str(payload["kind"] or "")
    target = str(payload["target_identity_sha256"] or "")
    generation = payload["generation"]
    previous = str(payload["previous_seal_sha256"] or "")
    state_hash = str(payload["state_sha256"] or "")
    seal_hash = str(payload["seal_sha256"] or "")
    signature = str(payload["authority_signature"] or "")
    if (
        not IDENTITY_RE.fullmatch(authority_id)
        or not KIND_RE.fullmatch(kind)
        or not SHA256_RE.fullmatch(target)
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
        or not SHA256_RE.fullmatch(previous)
        or not SHA256_RE.fullmatch(state_hash)
        or not SHA256_RE.fullmatch(seal_hash)
        or not signature
    ):
        raise MonotonicStateError("external monotonic seal identity is invalid")
    result = MonotonicSeal(
        authority_id=authority_id,
        kind=kind,
        target_identity_sha256=target,
        generation=generation,
        previous_seal_sha256=previous,
        state_sha256=state_hash,
        seal_sha256=seal_hash,
        authority_signature=signature,
    )
    if sha256(canonical_bytes(result.unsigned_payload())) != seal_hash:
        raise MonotonicStateError("external monotonic seal hash is invalid")
    if generation == 0 and previous != ZERO_HASH:
        raise MonotonicStateError("genesis seal must have a zero previous hash")
    if generation > 0 and previous == ZERO_HASH:
        raise MonotonicStateError("non-genesis seal must chain to a previous seal")
    return result


def build_reference_seal(
    *,
    authority_id: str,
    kind: str,
    target_identity_sha256: str,
    generation: int,
    previous_seal_sha256: str,
    state_sha256: str,
    authority_signature: str = "EXTERNAL_AUTHORITY_SIGNATURE_REQUIRED",
) -> MonotonicSeal:
    unsigned = {
        "schema": SCHEMA,
        "authority_id": authority_id,
        "kind": kind,
        "target_identity_sha256": target_identity_sha256,
        "generation": generation,
        "previous_seal_sha256": previous_seal_sha256,
        "state_sha256": state_sha256,
    }
    return parse_seal(
        {
            **unsigned,
            "seal_sha256": sha256(canonical_bytes(unsigned)),
            "authority_signature": authority_signature,
        }
    )


def validate_cache_against_authority(
    *,
    cache_payload: bytes | None,
    cache_generation: int | None,
    authoritative_seal: MonotonicSeal,
    minimum_generation: int,
    expected_kind: str,
    expected_target_identity_sha256: str,
) -> None:
    if authoritative_seal.kind != expected_kind:
        raise MonotonicStateError("external seal belongs to a different state kind")
    if authoritative_seal.target_identity_sha256 != expected_target_identity_sha256:
        raise MonotonicStateError("external seal belongs to a different database target")
    if authoritative_seal.generation < minimum_generation:
        raise MonotonicStateError("external monotonic generation is below the compiled floor")
    if cache_payload is None or cache_generation is None:
        raise MonotonicStateError("local state cache is deleted; external recovery is required")
    if cache_generation != authoritative_seal.generation:
        raise MonotonicStateError("local state cache generation was rolled back or advanced without CAS")
    if sha256(cache_payload) != authoritative_seal.state_sha256:
        raise MonotonicStateError("local state cache hash does not match external authority")


def validate_successor(current: MonotonicSeal, successor: MonotonicSeal) -> None:
    if (
        successor.authority_id != current.authority_id
        or successor.kind != current.kind
        or successor.target_identity_sha256 != current.target_identity_sha256
        or successor.generation != current.generation + 1
        or successor.previous_seal_sha256 != current.seal_sha256
    ):
        raise MonotonicStateError("external monotonic CAS successor does not extend current seal")
