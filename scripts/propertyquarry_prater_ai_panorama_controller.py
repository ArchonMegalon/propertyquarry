#!/usr/bin/env python3
"""Fixed no-argument entrypoint for the governed Prater panorama operation.

This file is inert in a workspace.  The native controller may invoke the copy
inside an independently attested web image only with the fixed mounts described
by ``PROPERTYQUARRY_AI_PANORAMA_INSTALL_CONTROLLER_V1.md``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping


ENTRYPOINT_PATH = Path(
    "/usr/local/libexec/propertyquarry-prater-ai-panorama-controller-v1.py"
)
PREFLIGHT_ENTRYPOINT_PATH = Path(
    "/usr/local/libexec/propertyquarry-prater-ai-panorama-preflight-v1.py"
)
DISCOVERY_ENTRYPOINT_PATH = Path(
    "/usr/local/libexec/"
    "propertyquarry-prater-ai-panorama-record-discovery-v1.py"
)
CONTROL_ROOT = Path(
    "/var/lib/propertyquarry/release-control/ai-panorama-install"
)
REQUEST_RELPATH = "prater-release-request.v1.json"
DISCOVERY_REQUEST_RELPATH = "prater-record-discovery-request.v1.json"
PERMIT_RELPATH = "prater-ai-panorama-install.json"
REQUEST_SCHEMA = "propertyquarry.prater-ai-panorama-release-request.v1"
DISCOVERY_REQUEST_SCHEMA = (
    "propertyquarry.prater-ai-panorama-record-discovery-request.v1"
)
DISCOVERY_RESULT_SCHEMA = (
    "propertyquarry.prater-ai-panorama-record-discovery-result.v1"
)
DATABASE_SECRETS_SCHEMA = (
    "propertyquarry.prater-ai-panorama-db-secrets.v1"
)
TERMINAL_SCHEMA = "propertyquarry.prater-ai-panorama-terminal-receipt.v1"
AUTHORITY = "propertyquarry-release-control"
DATABASE_SECRETS_PATH = Path(
    "/run/propertyquarry-release-control/ai-panorama-install/"
    "prater-ai-panorama-db-secrets.v1.json"
)
MAX_REQUEST_BYTES = 64 * 1024
MAX_DATABASE_SECRETS_BYTES = 16 * 1024
MAX_TERMINAL_BYTES = 512 * 1024
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_REQUEST_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_ERROR_CODE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}\Z")
_OWNER_PRINCIPAL_ID_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.:@/+~-]{0,255}\Z"
)


class PraterControllerEntrypointError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


def _fail(code: str) -> None:
    raise PraterControllerEntrypointError(code)


def _canonical(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PraterControllerEntrypointError(
            "prater-controller-canonical-json-invalid"
        ) from exc


def _components() -> SimpleNamespace:
    candidates = (Path("/app"), Path(__file__).resolve().parents[1] / "ea")
    for candidate in candidates:
        if (candidate / "app").is_dir() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
            break
    try:
        from app.product import property_tour_ai_panorama_admission as admission
        from app.product import property_tour_ai_panorama_prater_release as release
    except Exception as exc:
        raise PraterControllerEntrypointError(
            "prater-controller-runtime-unavailable"
        ) from exc
    return SimpleNamespace(admission=admission, release=release)


def _require_attested_image_entrypoint(expected_path: Path) -> None:
    try:
        path = Path(__file__)
        details = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise PraterControllerEntrypointError(
            "prater-controller-entrypoint-invalid"
        ) from exc
    if (
        path != expected_path
        or stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != 0
        or stat.S_IMODE(details.st_mode) != 0o555
        or details.st_nlink != 1
    ):
        _fail("prater-controller-entrypoint-invalid")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("prater-controller-request-invalid")
        result[key] = value
    return result


def _strict_database_secrets_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("prater-controller-db-secrets-invalid")
        result[key] = value
    return result


def _load_database_secrets(admission_module: object) -> dict[str, str]:
    """Load the fixed private DB material without projecting its values."""

    try:
        stable = admission_module._read_absolute_regular(
            DATABASE_SECRETS_PATH,
            code="prater-controller-db-secrets-unavailable",
            maximum_bytes=MAX_DATABASE_SECRETS_BYTES,
            required_uid=0,
            exact_mode=0o400,
        )
    except Exception as exc:
        raise PraterControllerEntrypointError(
            "prater-controller-db-secrets-unavailable"
        ) from exc
    try:
        value = json.loads(
            stable.data.decode("ascii"),
            object_pairs_hook=_strict_database_secrets_object,
            parse_constant=lambda _value: _fail(
                "prater-controller-db-secrets-invalid"
            ),
        )
    except PraterControllerEntrypointError:
        raise
    except (UnicodeError, ValueError) as exc:
        raise PraterControllerEntrypointError(
            "prater-controller-db-secrets-invalid"
        ) from exc
    if (
        type(value) is not dict
        or stable.data != _canonical(value)
        or set(value)
        != {
            "schema",
            "version",
            "DATABASE_URL",
            "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET",
        }
        or value["schema"] != DATABASE_SECRETS_SCHEMA
        or type(value["version"]) is not int
        or value["version"] != 1
        or type(value["DATABASE_URL"]) is not str
        or type(value["PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"])
        is not str
    ):
        _fail("prater-controller-db-secrets-invalid")
    database_url = value["DATABASE_URL"]
    erasure_secret = value[
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"
    ]
    if (
        not database_url
        or len(database_url) > 4096
        or any(ord(character) < 33 or ord(character) > 126 for character in database_url)
        or len(erasure_secret) < 32
        or len(erasure_secret) > 4096
        or any(ord(character) < 33 or ord(character) > 126 for character in erasure_secret)
    ):
        _fail("prater-controller-db-secrets-invalid")
    try:
        parsed = urllib.parse.urlsplit(database_url)
        port = parsed.port
    except ValueError as exc:
        raise PraterControllerEntrypointError(
            "prater-controller-db-secrets-invalid"
        ) from exc
    if (
        parsed.scheme != "postgresql"
        or parsed.hostname != "propertyquarry-db"
        or port != 5432
        or not parsed.username
        or parsed.path in {"", "/"}
        or parsed.fragment
        or urllib.parse.urlunsplit(parsed) != database_url
    ):
        _fail("prater-controller-db-secrets-invalid")
    return {
        "DATABASE_URL": database_url,
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET": erasure_secret,
    }


@contextlib.contextmanager
def _database_secret_environment(admission_module: object):  # type: ignore[no-untyped-def]
    """Expose fixed secrets only in this one-shot process during DB work."""

    loaded = _load_database_secrets(admission_module)
    keys = tuple(loaded)
    previous = {key: os.environ.get(key) for key in keys}
    try:
        for key, value in loaded.items():
            os.environ[key] = value
        yield
    finally:
        for key in keys:
            prior = previous[key]
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior


def _load_request(admission_module: object) -> dict[str, str]:
    try:
        control_descriptor = admission_module._open_control_root()
        stable = admission_module._read_relative_regular(
            control_descriptor,
            REQUEST_RELPATH,
            code="prater-controller-request-unavailable",
            maximum_bytes=MAX_REQUEST_BYTES,
            required_uid=admission_module._CONTROLLER_PATHS.required_uid,
            exact_mode=0o600,
        )
    except Exception as exc:
        raise PraterControllerEntrypointError(
            "prater-controller-request-unavailable"
        ) from exc
    finally:
        if "control_descriptor" in locals():
            os.close(control_descriptor)
    try:
        value = json.loads(
            stable.data.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda _value: _fail(
                "prater-controller-request-invalid"
            ),
        )
    except PraterControllerEntrypointError:
        raise
    except (UnicodeError, ValueError) as exc:
        raise PraterControllerEntrypointError(
            "prater-controller-request-invalid"
        ) from exc
    if (
        type(value) is not dict
        or stable.data != _canonical(value)
        or set(value)
        != {
            "schema",
            "version",
            "authority",
            "status",
            "owner_principal_id",
            "expected_publication_record_sha256",
            "request_id",
        }
        or value["schema"] != REQUEST_SCHEMA
        or type(value["version"]) is not int
        or value["version"] != 1
        or value["authority"] != AUTHORITY
        or value["status"] != "approved"
        or type(value["owner_principal_id"]) is not str
        or _OWNER_PRINCIPAL_ID_RE.fullmatch(
            value["owner_principal_id"]
        )
        is None
        or type(value["expected_publication_record_sha256"]) is not str
        or _DIGEST_RE.fullmatch(
            value["expected_publication_record_sha256"]
        )
        is None
        or type(value["request_id"]) is not str
        or _REQUEST_ID_RE.fullmatch(value["request_id"]) is None
    ):
        _fail("prater-controller-request-invalid")
    return {
        "owner_principal_id": value["owner_principal_id"],
        "expected_publication_record_sha256": value[
            "expected_publication_record_sha256"
        ],
        "request_id": value["request_id"],
    }


def _load_discovery_request(admission_module: object) -> dict[str, str]:
    try:
        control_descriptor = admission_module._open_control_root()
        stable = admission_module._read_relative_regular(
            control_descriptor,
            DISCOVERY_REQUEST_RELPATH,
            code="prater-controller-discovery-request-unavailable",
            maximum_bytes=MAX_REQUEST_BYTES,
            required_uid=admission_module._CONTROLLER_PATHS.required_uid,
            exact_mode=0o600,
        )
    except Exception as exc:
        raise PraterControllerEntrypointError(
            "prater-controller-discovery-request-unavailable"
        ) from exc
    finally:
        if "control_descriptor" in locals():
            os.close(control_descriptor)
    try:
        value = json.loads(
            stable.data.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda _value: _fail(
                "prater-controller-discovery-request-invalid"
            ),
        )
    except PraterControllerEntrypointError:
        raise
    except (UnicodeError, ValueError) as exc:
        raise PraterControllerEntrypointError(
            "prater-controller-discovery-request-invalid"
        ) from exc
    if (
        type(value) is not dict
        or stable.data != _canonical(value)
        or set(value)
        != {
            "schema",
            "version",
            "authority",
            "status",
            "request_id",
        }
        or value["schema"] != DISCOVERY_REQUEST_SCHEMA
        or type(value["version"]) is not int
        or value["version"] != 1
        or value["authority"] != AUTHORITY
        or value["status"] != "requested"
        or type(value["request_id"]) is not str
        or _REQUEST_ID_RE.fullmatch(value["request_id"]) is None
    ):
        _fail("prater-controller-discovery-request-invalid")
    return {
        "request_id": value["request_id"],
    }


def _expected_bindings(
    components: SimpleNamespace,
    request: Mapping[str, str],
    trusted: object,
) -> object:
    admission = components.admission
    release = components.release
    return admission.AiPanoramaInstallExpectedBindings(
        subject=trusted.subject,
        actor_principal_id=trusted.actor_principal_id,
        owner_principal_id=request["owner_principal_id"],
        search_run_id=release.PRATER_SEARCH_RUN_ID,
        candidate_ref=release.PRATER_CANDIDATE_REF,
        external_id=release.PRATER_EXTERNAL_ID,
        listing_url=release.PRATER_LISTING_URL,
        source_ref=release.PRATER_SOURCE_REF,
        provider_key=release.PRATER_PROVIDER_KEY,
        expected_slug=release.PRATER_SLUG,
        expected_source_tree_sha256=release.PRATER_SOURCE_TREE_SHA256,
        expected_tour_sha256=release.PRATER_TOUR_SHA256,
        expected_core_manifest_sha256=release.PRATER_CORE_MANIFEST_SHA256,
        expected_materialization_receipt_sha256=(
            release.PRATER_MATERIALIZATION_RECEIPT_SHA256
        ),
        expected_candidate_marker_sha256=(
            release.PRATER_CANDIDATE_MARKER_SHA256
        ),
        expected_publication_record_sha256=request[
            "expected_publication_record_sha256"
        ],
        artifact_relpath=release.PRATER_ARTIFACT_RELPATH,
        materialization_receipt_relpath=(
            release.PRATER_MATERIALIZATION_RECEIPT_RELPATH
        ),
        request_id=request["request_id"],
        repository=trusted.repository,
        git_ref=trusted.git_ref,
        git_head_sha=trusted.git_head_sha,
        workflow_ref=trusted.workflow_ref,
        job=trusted.job,
        environment=trusted.environment,
        review_receipt_sha256=trusted.review_receipt_sha256,
        web_image=trusted.web_image,
        web_image_id=trusted.web_image_id,
        key_usage=trusted.key_usage,
        key_id=trusted.key_id,
        key_epoch=trusted.key_epoch,
        key_sha256=trusted.key_sha256,
        keyring_sha256=trusted.keyring_sha256,
        volume_profile_sha256=trusted.volume_profile_sha256,
        compose_plan_sha256=trusted.compose_plan_sha256,
        volume_id=trusted.volume_id,
        artifact_root_device=trusted.artifact_root_device,
        artifact_root_inode=trusted.artifact_root_inode,
        public_tour_root_device=trusted.public_tour_root_device,
        public_tour_root_inode=trusted.public_tour_root_inode,
        execution_lease_seconds=trusted.execution_lease_seconds,
    )


def _terminal_relpath(request_id: str) -> str:
    if _REQUEST_ID_RE.fullmatch(request_id) is None:
        _fail("prater-controller-request-invalid")
    return f"terminal-{request_id}.v1.json"


def _require_terminal_absent(admission_module: object, relpath: str) -> None:
    descriptor = -1
    try:
        descriptor = admission_module._open_control_root()
        os.stat(relpath, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    except Exception as exc:
        raise PraterControllerEntrypointError(
            "prater-controller-terminal-state-invalid"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _fail("prater-controller-terminal-already-exists")


def _write_terminal_receipt(
    admission_module: object,
    *,
    relpath: str,
    payload: Mapping[str, object],
) -> str:
    raw = _canonical(payload)
    if len(raw) > MAX_TERMINAL_BYTES:
        _fail("prater-controller-terminal-receipt-invalid")
    control_descriptor = -1
    temporary_descriptor = -1
    temporary_name = ""
    try:
        control_descriptor = admission_module._open_control_root()
        for _ in range(32):
            temporary_name = f".terminal.tmp-{secrets.token_hex(8)}"
            try:
                temporary_descriptor = os.open(
                    temporary_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=control_descriptor,
                )
            except FileExistsError:
                temporary_name = ""
                continue
            break
        if temporary_descriptor < 0 or not temporary_name:
            _fail("prater-controller-terminal-write-failed")
        offset = 0
        while offset < len(raw):
            written = os.write(temporary_descriptor, raw[offset:])
            if written <= 0:
                _fail("prater-controller-terminal-write-failed")
            offset += written
        os.fchmod(temporary_descriptor, 0o600)
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = -1
        os.link(
            temporary_name,
            relpath,
            src_dir_fd=control_descriptor,
            dst_dir_fd=control_descriptor,
            follow_symlinks=False,
        )
        os.fsync(control_descriptor)
        os.unlink(temporary_name, dir_fd=control_descriptor)
        temporary_name = ""
        os.fsync(control_descriptor)
        return hashlib.sha256(raw).hexdigest()
    except PraterControllerEntrypointError:
        raise
    except FileExistsError as exc:
        raise PraterControllerEntrypointError(
            "prater-controller-terminal-already-exists"
        ) from exc
    except OSError as exc:
        raise PraterControllerEntrypointError(
            "prater-controller-terminal-write-failed"
        ) from exc
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if control_descriptor >= 0:
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=control_descriptor)
                except FileNotFoundError:
                    pass
            os.close(control_descriptor)


def run() -> dict[str, object]:
    if os.geteuid() != 0 or os.getegid() != 0:
        _fail("prater-controller-root-required")
    _require_attested_image_entrypoint(ENTRYPOINT_PATH)
    components = _components()
    request = _load_request(components.admission)
    terminal_relpath = _terminal_relpath(request["request_id"])
    _require_terminal_absent(components.admission, terminal_relpath)
    permit_consumed = False
    release_returned = False
    release_committed = False
    try:
        with _database_secret_environment(components.admission):
            trusted = (
                components.admission.load_ai_panorama_install_trusted_context()
            )
            expected = _expected_bindings(components, request, trusted)
            verified = components.admission.consume_ai_panorama_install_permit(
                PERMIT_RELPATH,
                expected,
            )
            permit_consumed = True
            result = components.release.run_prater_ai_panorama_release(
                verified,
                apply=True,
            )
        release_returned = True
        if (
            result.get("status") != "released"
            or result.get("release_eligible") is not True
        ):
            _fail("prater-controller-release-ineligible")
        release_committed = True
        terminal = {
            "schema": TERMINAL_SCHEMA,
            "version": 1,
            "authority": AUTHORITY,
            "status": "committed",
            "request_id_sha256": hashlib.sha256(
                request["request_id"].encode("ascii")
            ).hexdigest(),
            "permit_sha256": verified.permit_sha256,
            "result": result,
            "private_values_redacted": True,
        }
        terminal_sha256 = _write_terminal_receipt(
            components.admission,
            relpath=terminal_relpath,
            payload=terminal,
        )
    except Exception as exc:
        raw_code = str(getattr(exc, "code", "") or "").strip()
        code = (
            raw_code
            if _ERROR_CODE_RE.fullmatch(raw_code)
            else "prater-controller-release-failed"
        )
        uncertain = (
            release_returned
            or release_committed
            or bool(getattr(exc, "commit_outcome_ambiguous", False))
            or "recovery_required" in code
            or "recovery-required" in code
        )
        if release_returned or release_committed:
            # A returned inner operation may already be committed even when
            # its projection is malformed.  Never persist false clean-failure
            # evidence after that boundary.
            raise PraterControllerEntrypointError(
                "prater-controller-recovery-required"
            ) from exc
        terminal_status = (
            "recovery-required"
            if uncertain
            else "rolled-back"
            if bool(getattr(exc, "rollback_performed", False))
            else "failed-clean"
            if permit_consumed
            else "failed"
        )
        failure = {
            "schema": TERMINAL_SCHEMA,
            "version": 1,
            "authority": AUTHORITY,
            "status": terminal_status,
            "request_id_sha256": hashlib.sha256(
                request["request_id"].encode("ascii")
            ).hexdigest(),
            "error": code,
            "private_values_redacted": True,
        }
        try:
            _write_terminal_receipt(
                components.admission,
                relpath=terminal_relpath,
                payload=failure,
            )
        except Exception as terminal_exc:
            raise PraterControllerEntrypointError(
                "prater-controller-recovery-required"
            ) from terminal_exc
        raise PraterControllerEntrypointError(code) from exc
    return {
        "schema": TERMINAL_SCHEMA,
        "status": "committed",
        "slug": components.release.PRATER_SLUG,
        "control_path": (
            f"/tours/{components.release.PRATER_SLUG}/control"
        ),
        "terminal_receipt_sha256": terminal_sha256,
        "private_values_redacted": True,
    }


def run_preflight() -> dict[str, object]:
    if os.geteuid() != 0 or os.getegid() != 0:
        _fail("prater-controller-root-required")
    _require_attested_image_entrypoint(PREFLIGHT_ENTRYPOINT_PATH)
    components = _components()
    request = _load_request(components.admission)
    trusted = components.admission.load_ai_panorama_install_trusted_context()
    expected = _expected_bindings(components, request, trusted)
    verified = components.admission.verify_ai_panorama_install_permit(
        PERMIT_RELPATH,
        expected,
    )
    result = (
        components.release.run_prater_ai_panorama_artifact_preflight(
            verified
        )
    )
    if (
        result.get("status") != "preflight_passed"
        or result.get("nonce_consumed") is not False
        or result.get("database_access_performed") is not False
    ):
        _fail("prater-controller-preflight-failed")
    return {
        "schema": TERMINAL_SCHEMA,
        "status": "preflight-passed",
        "slug": components.release.PRATER_SLUG,
        "nonce_consumed": False,
        "database_access_performed": False,
        "private_values_redacted": True,
    }


def run_record_discovery() -> dict[str, object]:
    """Create a private, non-authorizing record-hash projection for signing."""

    if os.geteuid() != 0 or os.getegid() != 0:
        _fail("prater-controller-root-required")
    _require_attested_image_entrypoint(DISCOVERY_ENTRYPOINT_PATH)
    components = _components()
    request = _load_discovery_request(components.admission)
    with _database_secret_environment(components.admission):
        discovered = (
            components.release.discover_prater_ai_panorama_publication_record()
        )
    record_sha256 = str(
        discovered.get("expected_publication_record_sha256") or ""
    ).strip().lower()
    if (
        discovered.get("status") != "record-discovered"
        or _DIGEST_RE.fullmatch(record_sha256) is None
        or discovered.get("database_mutation_performed") is not False
        or discovered.get("release_authorized") is not False
        or type(discovered.get("owner_principal_id")) is not str
        or _OWNER_PRINCIPAL_ID_RE.fullmatch(
            discovered["owner_principal_id"]
        )
        is None
    ):
        _fail("prater-controller-record-discovery-failed")
    return {
        "schema": DISCOVERY_RESULT_SCHEMA,
        "version": 1,
        "authority": AUTHORITY,
        "status": "discovered",
        "owner_principal_id": discovered["owner_principal_id"],
        "search_run_id": components.release.PRATER_SEARCH_RUN_ID,
        "candidate_ref": components.release.PRATER_CANDIDATE_REF,
        "expected_publication_record_sha256": record_sha256,
        "request_id": request["request_id"],
        "database_mutation_performed": False,
        "release_authorized": False,
        "private_projection": True,
    }


def main() -> int:
    if len(sys.argv) != 1:
        print(
            _canonical(
                {
                    "schema": TERMINAL_SCHEMA,
                    "status": "failed",
                    "error": "prater-controller-arguments-forbidden",
                    "private_values_redacted": True,
                }
            ).decode("ascii"),
            end="",
        )
        return 2
    try:
        path = Path(__file__)
        result = (
            run_record_discovery()
            if path == DISCOVERY_ENTRYPOINT_PATH
            else run_preflight()
            if path == PREFLIGHT_ENTRYPOINT_PATH
            else run()
        )
    except Exception as exc:
        error = (
            exc.code
            if isinstance(exc, PraterControllerEntrypointError)
            else "prater-controller-failed"
        )
        print(
            _canonical(
                {
                    "schema": TERMINAL_SCHEMA,
                    "status": "failed",
                    "error": error,
                    "private_values_redacted": True,
                }
            ).decode("ascii"),
            end="",
        )
        return 3
    print(_canonical(result).decode("ascii"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
