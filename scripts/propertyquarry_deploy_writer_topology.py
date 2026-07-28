#!/usr/bin/env python3
"""Validate the pinned deploy writer topology and discover same-database containers."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA = "propertyquarry.deploy-writer-topology.v1"
TOPOLOGY_PATH = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "release"
    / "propertyquarry_deploy_writer_topology.v1.json"
)
TOPOLOGY_SHA256 = "b48aa603748f3f1f615da2a28964dca3abda35932cdb0e5bdd29bb9683b3ed8e"
ROLE_NAMES = ("api", "worker", "scheduler", "render", "migration", "ingress")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")


class WriterTopologyError(ValueError):
    pass


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WriterTopologyError(f"writer topology contains duplicate JSON key {key}")
        result[key] = value
    return result


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise WriterTopologyError(f"{label} fields do not match the canonical schema")


def _identifier(value: object, label: str) -> str:
    text = str(value or "")
    if not IDENTIFIER_RE.fullmatch(text):
        raise WriterTopologyError(f"{label} is missing or unsafe")
    return text


def load_topology() -> dict[str, Any]:
    try:
        raw = TOPOLOGY_PATH.read_bytes()
        payload = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                WriterTopologyError(f"writer topology contains non-finite constant {value}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WriterTopologyError(f"could not load pinned writer topology: {exc}") from exc
    if not isinstance(payload, dict):
        raise WriterTopologyError("writer topology must be a JSON object")
    if hashlib.sha256(_canonical_bytes(payload)).hexdigest() != TOPOLOGY_SHA256:
        raise WriterTopologyError("writer topology does not match the compiled release pin")
    _exact_keys(
        payload,
        {"schema", "topology_id", "status", "compose_project", "target", "external_database_writers"},
        "writer topology",
    )
    if payload["schema"] != SCHEMA or payload["status"] != "active":
        raise WriterTopologyError("writer topology identity is not active")
    _identifier(payload["topology_id"], "writer topology id")
    _identifier(payload["compose_project"], "writer topology Compose project")
    target = payload["target"]
    if not isinstance(target, dict):
        raise WriterTopologyError("writer topology target must be an object")
    _exact_keys(target, set(ROLE_NAMES), "writer topology target")
    expected_database_roles = {
        "api": True,
        "worker": True,
        "scheduler": True,
        "render": False,
        "migration": True,
        "ingress": False,
    }
    for role in ROLE_NAMES:
        row = target[role]
        if not isinstance(row, dict):
            raise WriterTopologyError(f"writer topology {role} row must be an object")
        _exact_keys(row, {"service", "container", "database_writer"}, f"writer topology {role}")
        _identifier(row["service"], f"writer topology {role} service")
        _identifier(row["container"], f"writer topology {role} container")
        if row["database_writer"] is not expected_database_roles[role]:
            raise WriterTopologyError(f"writer topology {role} database_writer classification is invalid")
    external = payload["external_database_writers"]
    if not isinstance(external, list):
        raise WriterTopologyError("external database writers must be an array")
    seen = {str(target[role]["container"]) for role in ROLE_NAMES}
    for index, row in enumerate(external):
        if not isinstance(row, dict):
            raise WriterTopologyError(f"external database writer {index} must be an object")
        _exact_keys(row, {"container", "restore_precommit"}, f"external database writer {index}")
        name = _identifier(row["container"], f"external database writer {index} container")
        if name in seen or not isinstance(row["restore_precommit"], bool):
            raise WriterTopologyError("external database writer is duplicate or has invalid restore policy")
        seen.add(name)
    return payload


def topology_digest() -> str:
    load_topology()
    return TOPOLOGY_SHA256


def allowed_database_writer_names(payload: Mapping[str, Any]) -> list[str]:
    target = payload["target"]
    assert isinstance(target, Mapping)
    # Derive the allowlist from the closed role classifications so adding a
    # durable consumer cannot silently omit it from containment. The migrator
    # remains included because a controller crash can leave it active.
    names = [
        str(target[role]["container"])
        for role in ROLE_NAMES
        if target[role]["database_writer"] is True
    ]
    names.extend(str(row["container"]) for row in payload["external_database_writers"])
    return names


def validate_production_target(payload: Mapping[str, Any], values: Mapping[str, str]) -> None:
    target = payload["target"]
    assert isinstance(target, Mapping)
    expected = {"compose_project": str(payload["compose_project"])}
    for role in ROLE_NAMES:
        row = target[role]
        expected[f"{role}_service"] = str(row["service"])
        expected[f"{role}_container"] = str(row["container"])
    mismatches = sorted(key for key, value in expected.items() if values.get(key) != value)
    if mismatches:
        raise WriterTopologyError(
            "production target diverges from the pinned writer topology: " + ", ".join(mismatches)
        )


def _inventory_from_inspect(
    payload: object,
    allowed_container_names: Sequence[str],
) -> list[tuple[str, str]]:
    allowed = set(allowed_container_names)
    if not allowed or any(not IDENTIFIER_RE.fullmatch(name) for name in allowed):
        raise WriterTopologyError("pinned writer container allowlist is invalid")
    if not isinstance(payload, list):
        raise WriterTopologyError("Docker inspection inventory must be an array")
    matches: list[tuple[str, str]] = []
    for row in payload:
        if not isinstance(row, Mapping):
            raise WriterTopologyError("Docker inspection inventory row is invalid")
        container_id = str(row.get("Id") or "").lower()
        name = str(row.get("Name") or "").removeprefix("/")
        state = row.get("State")
        if (
            not CONTAINER_ID_RE.fullmatch(container_id)
            or not IDENTIFIER_RE.fullmatch(name)
            or not isinstance(state, Mapping)
        ):
            raise WriterTopologyError("Docker writer identity is missing or unsafe")
        if state.get("Running") is not True:
            continue
        # Container inspection is containment inventory only.  Never infer
        # database identity from a literal DSN: aliases, mounted secrets and
        # non-Docker writers make that unsafe.  The installed controller uses
        # server-derived identity, role closure and pg_stat_activity as the
        # authorization boundary.
        if name in allowed:
            matches.append((container_id, name))
    names = [name for _, name in matches]
    if len(names) != len(set(names)):
        raise WriterTopologyError("Docker writer inventory contains duplicate container names")
    return sorted(matches, key=lambda item: item[1])


def discover_database_writers() -> list[tuple[str, str]]:
    topology = load_topology()
    allowed = allowed_database_writer_names(topology)
    try:
        ps = subprocess.run(
            ["docker", "ps", "--no-trunc", "--quiet"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WriterTopologyError(f"could not enumerate Docker writers: {exc}") from exc
    if ps.returncode != 0:
        raise WriterTopologyError("Docker writer enumeration failed")
    ids = [value.strip().lower() for value in ps.stdout.splitlines() if value.strip()]
    if any(not CONTAINER_ID_RE.fullmatch(value) for value in ids) or len(ids) != len(set(ids)):
        raise WriterTopologyError("Docker returned an invalid or duplicate container inventory")
    if not ids:
        return []
    try:
        inspect = subprocess.run(
            ["docker", "inspect", *ids],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WriterTopologyError(f"could not inspect Docker writers: {exc}") from exc
    if inspect.returncode != 0:
        raise WriterTopologyError("Docker writer inspection failed")
    try:
        payload = json.loads(inspect.stdout)
    except json.JSONDecodeError as exc:
        raise WriterTopologyError("Docker writer inspection was not valid JSON") from exc
    return _inventory_from_inspect(payload, allowed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("digest")
    subparsers.add_parser("allowlist")
    validate = subparsers.add_parser("validate-target")
    validate.add_argument("--compose-project", required=True)
    for role in ROLE_NAMES:
        validate.add_argument(f"--{role}-service", required=True)
        validate.add_argument(f"--{role}-container", required=True)
    subparsers.add_parser("inventory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = load_topology()
        if args.command == "digest":
            print(TOPOLOGY_SHA256)
        elif args.command == "allowlist":
            for name in allowed_database_writer_names(payload):
                print(name)
        elif args.command == "validate-target":
            validate_production_target(payload, vars(args))
        else:
            for container_id, name in discover_database_writers():
                print(f"{container_id}|{name}")
    except WriterTopologyError as exc:
        print(f"writer topology rejected: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
