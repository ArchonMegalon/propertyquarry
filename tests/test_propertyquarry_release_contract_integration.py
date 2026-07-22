from __future__ import annotations

import copy

from scripts import propertyquarry_database_control_v2 as database
from scripts import propertyquarry_predeploy_backup_v2 as backup
from scripts import propertyquarry_runtime_deploy_v2 as deploy
from scripts import propertyquarry_runtime_isolation_v2 as isolation


def _runtime_inputs() -> list[dict[str, object]]:
    return [
        {
            "gid": 1000,
            "mode": 0o600,
            "path": str(path),
            "sha256": "sha256:" + f"{index + 1:x}" * 64,
            "size": index + 1,
            "uid": 1000,
        }
        for index, path in enumerate(backup.RUNTIME_INPUT_PATHS)
    ]


def test_six_runtime_inputs_have_one_cross_helper_descriptor_contract() -> None:
    expected_paths = tuple(backup.RUNTIME_INPUT_PATHS)
    assert tuple(database.RUNTIME_INPUT_PATHS) == expected_paths
    assert tuple(deploy.ENV_FILES) == expected_paths
    assert tuple(isolation.RUNTIME_INPUTS) == expected_paths

    descriptors = _runtime_inputs()
    authority = {
        "github_identity_env_gid": 1000,
        "github_identity_env_uid": 1000,
        "registration_email_env_gid": 1000,
        "registration_email_env_uid": 1000,
        "scene_video_env_gid": 1000,
        "scene_video_env_uid": 1000,
    }
    assert backup._validated_runtime_inputs(copy.deepcopy(descriptors)) == descriptors
    assert database._validate_runtime_inputs(copy.deepcopy(descriptors)) == descriptors
    assert (
        deploy._validate_runtime_input_array(
            copy.deepcopy(descriptors),
            authority=authority,
        )
        == descriptors
    )
    assert all(item["mode"] == 384 for item in descriptors)
    assert all(isinstance(item["mode"], int) for item in descriptors)


def test_database_receipt_shape_is_identical_at_every_verification_gate() -> None:
    expected = frozenset(database.DATABASE_RECEIPT_PAYLOAD_KEYS)
    assert deploy.DATABASE_PAYLOAD_KEYS == expected
    assert isolation.DATABASE_PAYLOAD_KEYS == expected


def test_envelope_is_one_raw_sha256_shape_across_release_helpers() -> None:
    envelope_sha = "a" * 64
    assert backup.SHA256_HEX_RE.fullmatch(envelope_sha)
    assert isolation.SHA256_RE.fullmatch(envelope_sha)
    assert deploy.ENVELOPE_SHA_RE.fullmatch(envelope_sha)
    assert deploy.RUNTIME_SHA_RE.fullmatch(envelope_sha) is None
