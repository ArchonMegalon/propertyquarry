from __future__ import annotations

import hashlib
import json
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from scripts import property_reconstruction_render_bridge as bridge


def _write_generated_bundle(
    public_root: Path,
    slug: str,
    *,
    transaction_id: str = "",
    runtime_publish_required: bool = False,
    runtime_publish_status: str = "skipped_not_requested",
    runtime_publish_ok: bool = True,
) -> Path:
    bundle_dir = public_root / slug
    reconstruction_dir = bundle_dir / "generated-reconstruction"
    nested_dir = reconstruction_dir / "assets"
    nested_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "tour.json").write_text(
        json.dumps({"slug": slug}),
        encoding="utf-8",
    )
    (reconstruction_dir / "viewer.html").write_text("viewer", encoding="utf-8")
    (reconstruction_dir / "reconstruction.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "runtime_publish_required": runtime_publish_required,
                "runtime_publish_ok": runtime_publish_ok,
                "runtime_publish": {"status": runtime_publish_status, "slug": slug},
            }
        ),
        encoding="utf-8",
    )
    (nested_dir / "scene.bin").write_bytes(b"scene")
    bundle_dir.chmod(0o755)
    reconstruction_dir.chmod(0o755)
    nested_dir.chmod(0o755)
    (bundle_dir / "tour.json").chmod(0o644)
    (reconstruction_dir / "viewer.html").chmod(0o644)
    (reconstruction_dir / "reconstruction.json").chmod(0o644)
    (nested_dir / "scene.bin").chmod(0o644)
    if transaction_id:
        manifest_bytes = (bundle_dir / "tour.json").read_bytes()
        marker = bundle_dir / bridge._GENERATED_BUNDLE_COMMIT_MARKER
        marker.write_text(
            json.dumps(
                {
                    "schema": "propertyquarry.render_bundle_commit.v1",
                    "slug": slug,
                    "tour_manifest_sha256": hashlib.sha256(
                        manifest_bytes
                    ).hexdigest(),
                    "transaction_id": transaction_id,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        marker.chmod(0o600)
    return bundle_dir


def test_build_generator_command_rejects_paths_outside_public_tour_dir(tmp_path: Path, monkeypatch) -> None:
    public_root = tmp_path / "public_tours"
    public_root.mkdir()
    script_path = tmp_path / "generate_property_reconstruction.py"
    script_path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_root))
    monkeypatch.setattr(bridge, "_script_path", lambda: script_path)

    with pytest.raises(ValueError, match="path_outside_public_tour_dir"):
        bridge._build_generator_command(
            {
                "slug": "unsafe",
                "floorplan_path": str(tmp_path / "outside.jpg"),
                "photo_paths": [],
            }
        )


def test_build_generator_command_passes_reviewed_floorplan_analysis_to_generator(tmp_path: Path, monkeypatch) -> None:
    public_root = tmp_path / "public_tours"
    source_root = public_root / "source-locked"
    source_root.mkdir(parents=True)
    script_path = tmp_path / "generate_property_reconstruction.py"
    script_path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    floorplan = source_root / "floorplan.jpg"
    analysis = source_root / "floorplan-analysis.json"
    floorplan.write_bytes(b"floorplan")
    analysis.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_root))
    monkeypatch.setattr(bridge, "_script_path", lambda: script_path)

    command = bridge._build_generator_command(
        {
            "slug": "source-locked",
            "floorplan_path": str(floorplan),
            "floorplan_analysis_path": str(analysis),
            "photo_paths": [],
        }
    )

    assert "--floorplan-analysis" in command
    assert command[command.index("--floorplan-analysis") + 1] == str(analysis.resolve())


def test_run_generation_request_invokes_generator_with_shared_paths(tmp_path: Path, monkeypatch) -> None:
    public_root = tmp_path / "public_tours"
    bundle_root = public_root / "safe-slug" / ".reconstruction-source"
    bundle_root.mkdir(parents=True)
    script_path = tmp_path / "generate_property_reconstruction.py"
    script_path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    floorplan = bundle_root / "floorplan.jpg"
    photo = bundle_root / "photo-01.jpg"
    floorplan.write_bytes(b"floorplan")
    photo.write_bytes(b"photo")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_root))
    monkeypatch.setattr(bridge, "_script_path", lambda: script_path)

    captured: dict[str, object] = {}

    def _fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:  # type: ignore[no-untyped-def]
        captured["command"] = command
        captured["env_public_tour_dir"] = dict(kwargs.get("env") or {}).get("EA_PUBLIC_TOUR_DIR")
        _write_generated_bundle(
            public_root,
            "safe-slug",
            transaction_id=dict(kwargs.get("env") or {})[
                "PROPERTYQUARRY_RECONSTRUCTION_TRANSACTION_ID"
            ],
        )
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"status": "generated"}) + "\n", stderr="")

    monkeypatch.setattr(bridge, "_run_generator_process", _fake_run)

    result = bridge.run_generation_request(
        {
            "slug": "safe-slug",
            "floorplan_path": str(floorplan),
            "photo_paths": [str(photo)],
            "style_label": "Ikea",
            "room_count": 3,
            "route_labels": ["entry/hall", "living area", "bedroom"],
            "skip_video": False,
        }
    )

    assert result["status"] == "generated"
    command = list(captured["command"])
    assert command[:3] == [bridge.sys.executable, str(script_path), "--slug"]
    assert "--floorplan" in command
    assert "--photo" in command
    assert "--style-label" in command
    assert "--room-count" in command
    assert command.count("--room-label") == 3
    assert captured["env_public_tour_dir"] == str(public_root.resolve())


def test_run_generation_request_forwards_walkthrough_seconds_per_stop_env(tmp_path: Path, monkeypatch) -> None:
    public_root = tmp_path / "public_tours"
    bundle_root = public_root / "safe-slug" / ".reconstruction-source"
    bundle_root.mkdir(parents=True)
    script_path = tmp_path / "generate_property_reconstruction.py"
    script_path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    floorplan = bundle_root / "floorplan.jpg"
    floorplan.write_bytes(b"floorplan")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_root))
    monkeypatch.setattr(bridge, "_script_path", lambda: script_path)

    captured: dict[str, object] = {}

    def _fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:  # type: ignore[no-untyped-def]
        captured["env"] = dict(kwargs.get("env") or {})
        _write_generated_bundle(
            public_root,
            "safe-slug",
            transaction_id=dict(kwargs.get("env") or {})[
                "PROPERTYQUARRY_RECONSTRUCTION_TRANSACTION_ID"
            ],
        )
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"status": "generated"}) + "\n", stderr="")

    monkeypatch.setattr(bridge, "_run_generator_process", _fake_run)

    result = bridge.run_generation_request(
        {
            "slug": "safe-slug",
            "floorplan_path": str(floorplan),
            "photo_paths": [],
            "walkthrough_seconds_per_stop": 8.0,
        }
    )

    assert result["status"] == "generated"
    assert captured["env"]["PROPERTYQUARRY_RECONSTRUCTION_WALKTHROUGH_SECONDS_PER_STOP"] == "8.0"


def test_run_generation_request_reports_generator_timeout(tmp_path: Path, monkeypatch) -> None:
    public_root = tmp_path / "public_tours"
    bundle_root = public_root / "safe-slug" / ".reconstruction-source"
    bundle_root.mkdir(parents=True)
    script_path = tmp_path / "generate_property_reconstruction.py"
    script_path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    floorplan = bundle_root / "floorplan.jpg"
    floorplan.write_bytes(b"floorplan")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_root))
    monkeypatch.setenv("PROPERTYQUARRY_RECONSTRUCTION_TIMEOUT_SECONDS", "480")
    monkeypatch.setattr(bridge, "_script_path", lambda: script_path)

    captured: dict[str, object] = {}

    def _fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:  # type: ignore[no-untyped-def]
        captured["timeout"] = kwargs.get("timeout_seconds")
        raise subprocess.TimeoutExpired(command, timeout=kwargs.get("timeout_seconds") or 0)

    monkeypatch.setattr(bridge, "_run_generator_process", _fake_run)

    result = bridge.run_generation_request(
        {
            "slug": "safe-slug",
            "floorplan_path": str(floorplan),
            "photo_paths": [],
        }
    )

    assert captured["timeout"] == 480
    assert result["status"] == "failed"
    assert result["reason"] == "generator_timeout"
    assert result["timeout_seconds"] == 480


def test_run_generation_request_rejects_generator_reported_failure(tmp_path: Path, monkeypatch) -> None:
    public_root = tmp_path / "public_tours"
    bundle_root = public_root / "safe-slug" / ".reconstruction-source"
    bundle_root.mkdir(parents=True)
    script_path = tmp_path / "generate_property_reconstruction.py"
    script_path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    floorplan = bundle_root / "floorplan.jpg"
    floorplan.write_bytes(b"floorplan")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_root))
    monkeypatch.setattr(bridge, "_script_path", lambda: script_path)

    def _fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"status": "failed", "reason": "runtime_publish_failed"}) + "\n",
            stderr="",
        )

    monkeypatch.setattr(bridge, "_run_generator_process", _fake_run)

    result = bridge.run_generation_request(
        {
            "slug": "safe-slug",
            "floorplan_path": str(floorplan),
            "photo_paths": [],
        }
    )

    assert result["status"] == "failed"
    assert result["reason"] == "generator_reported_failure"
    assert result["detail"] == "runtime_publish_failed"
    assert "result" not in result


def test_run_generation_request_publishes_only_generated_bundle_permissions(tmp_path: Path, monkeypatch) -> None:
    public_root = tmp_path / "public_tours"
    source_dir = public_root / "safe-slug" / ".reconstruction-source"
    source_dir.mkdir(parents=True)
    source_path = source_dir / "floorplan.jpg"
    source_path.write_bytes(b"floorplan")
    private_manifest = public_root / "safe-slug" / "tour.private.json"
    private_manifest.write_text('{"principal_id":"private"}', encoding="utf-8")
    source_dir.chmod(0o700)
    source_path.chmod(0o600)
    private_manifest.chmod(0o600)
    script_path = tmp_path / "generate_property_reconstruction.py"
    script_path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_root))
    monkeypatch.setattr(bridge, "_script_path", lambda: script_path)

    def _fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:  # type: ignore[no-untyped-def]
        _write_generated_bundle(
            public_root,
            "safe-slug",
            transaction_id=dict(kwargs.get("env") or {})[
                "PROPERTYQUARRY_RECONSTRUCTION_TRANSACTION_ID"
            ],
        )
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"status": "generated"}) + "\n", stderr="")

    monkeypatch.setattr(bridge, "_run_generator_process", _fake_run)

    result = bridge.run_generation_request(
        {
            "slug": "safe-slug",
            "floorplan_path": str(source_path),
            "photo_paths": [],
        }
    )

    bundle_dir = public_root / "safe-slug"
    reconstruction_dir = bundle_dir / "generated-reconstruction"
    assert result["status"] == "generated"
    assert bundle_dir.stat().st_mode & 0o777 == 0o755
    assert (bundle_dir / "tour.json").stat().st_mode & 0o777 == 0o644
    assert reconstruction_dir.stat().st_mode & 0o777 == 0o755
    assert (reconstruction_dir / "viewer.html").stat().st_mode & 0o777 == 0o644
    assert (reconstruction_dir / "assets").stat().st_mode & 0o777 == 0o755
    assert (reconstruction_dir / "assets" / "scene.bin").stat().st_mode & 0o777 == 0o644
    assert source_dir.stat().st_mode & 0o777 == 0o700
    assert source_path.stat().st_mode & 0o777 == 0o600
    assert private_manifest.stat().st_mode & 0o777 == 0o600


def test_run_generation_request_rejects_bundle_symlink_before_generator(tmp_path: Path, monkeypatch) -> None:
    public_root = tmp_path / "public_tours"
    public_root.mkdir()
    target_dir = public_root / "target"
    target_dir.mkdir()
    (public_root / "safe-slug").symlink_to(target_dir, target_is_directory=True)
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_root))
    monkeypatch.setattr(
        bridge,
        "_run_generator_process",
        lambda *args, **kwargs: pytest.fail("generator must not run for a symlink bundle"),
    )

    result = bridge.run_generation_request({"slug": "safe-slug", "photo_paths": []})

    assert result == {
        "status": "failed",
        "reason": "generated_bundle_publish_failed",
        "detail": "generated_bundle_symlink_forbidden",
    }


def test_run_generation_request_rejects_generated_asset_symlink(tmp_path: Path, monkeypatch) -> None:
    public_root = tmp_path / "public_tours"
    source_dir = public_root / "safe-slug" / ".reconstruction-source"
    source_dir.mkdir(parents=True)
    source_path = source_dir / "floorplan.jpg"
    source_path.write_bytes(b"floorplan")
    outside_asset = tmp_path / "outside.bin"
    outside_asset.write_bytes(b"private")
    outside_asset.chmod(0o600)
    script_path = tmp_path / "generate_property_reconstruction.py"
    script_path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_root))
    monkeypatch.setattr(bridge, "_script_path", lambda: script_path)

    def _fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:  # type: ignore[no-untyped-def]
        bundle_dir = _write_generated_bundle(
            public_root,
            "safe-slug",
            transaction_id=dict(kwargs.get("env") or {})[
                "PROPERTYQUARRY_RECONSTRUCTION_TRANSACTION_ID"
            ],
        )
        (bundle_dir / "generated-reconstruction" / "linked.bin").symlink_to(outside_asset)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"status": "generated"}) + "\n", stderr="")

    monkeypatch.setattr(bridge, "_run_generator_process", _fake_run)

    result = bridge.run_generation_request(
        {
            "slug": "safe-slug",
            "floorplan_path": str(source_path),
            "photo_paths": [],
        }
    )

    assert result == {
        "status": "failed",
        "reason": "generated_bundle_publish_failed",
        "detail": "generated_reconstruction_asset_symlink_forbidden",
    }
    assert outside_asset.stat().st_mode & 0o777 != 0o644


def test_run_generation_request_fails_closed_on_wrong_public_modes(tmp_path: Path, monkeypatch) -> None:
    public_root = tmp_path / "public_tours"
    source_dir = public_root / "safe-slug" / ".reconstruction-source"
    source_dir.mkdir(parents=True)
    source_path = source_dir / "floorplan.jpg"
    source_path.write_bytes(b"floorplan")
    script_path = tmp_path / "generate_property_reconstruction.py"
    script_path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_root))
    monkeypatch.setattr(bridge, "_script_path", lambda: script_path)

    def _fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:  # type: ignore[no-untyped-def]
        bundle_dir = _write_generated_bundle(
            public_root,
            "safe-slug",
            transaction_id=dict(kwargs.get("env") or {})[
                "PROPERTYQUARRY_RECONSTRUCTION_TRANSACTION_ID"
            ],
        )
        (bundle_dir / "generated-reconstruction" / "viewer.html").chmod(0o600)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"status": "generated"}) + "\n", stderr="")

    monkeypatch.setattr(bridge, "_run_generator_process", _fake_run)

    result = bridge.run_generation_request(
        {
            "slug": "safe-slug",
            "floorplan_path": str(source_path),
            "photo_paths": [],
        }
    )

    assert result == {
        "status": "failed",
        "reason": "generated_bundle_publish_failed",
        "detail": "generated_bundle_permissions_invalid",
    }


PRIVATE_MARKER = "/home/operator/private/render-source.jpg"


def _redaction_config() -> bridge.BridgeConfig:
    return bridge.BridgeConfig(auth_token="test-token")


def _proven_recovery(slug: str = "flat") -> dict[str, object]:
    return {
        "slug": slug,
        "transaction_id_bound": True,
        "publication_durability": "fsynced_by_bridge_recovery",
        "runtime_publish_required": False,
        "runtime_publish_ok": True,
        "runtime_publish_status": "skipped_not_requested",
        "runtime_publication_proven": True,
    }


def _patch_redaction_request_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "redaction-public-tours"
    root.mkdir()
    monkeypatch.setattr(bridge, "_public_tour_dir", lambda: root)
    monkeypatch.setattr(bridge, "_validate_generation_cost", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bridge, "_generated_bundle_target", lambda *_args, **_kwargs: root / "flat")
    monkeypatch.setattr(bridge, "_build_generator_command", lambda _payload: ["generator"])
    monkeypatch.setattr(bridge, "_validate_generated_bundle_publication", lambda _slug: None)


def test_bridge_timeout_does_not_reflect_command_or_private_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_redaction_request_boundary(monkeypatch, tmp_path)

    def timed_out(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            ["generator", PRIVATE_MARKER],
            120,
            output=PRIVATE_MARKER,
            stderr=PRIVATE_MARKER,
        )

    monkeypatch.setattr(bridge, "_run_generator_process", timed_out)

    result = bridge.run_generation_request({"slug": "flat"}, config=_redaction_config())

    assert result == {
        "status": "failed",
        "reason": "generator_timeout",
        "timeout_seconds": 420,
    }
    assert PRIVATE_MARKER not in json.dumps(result, sort_keys=True)


def test_bridge_nonzero_exit_hashes_but_never_reflects_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_redaction_request_boundary(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bridge,
        "_run_generator_process",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["generator"],
            23,
            stdout="",
            stderr=f"cannot open {PRIVATE_MARKER}",
        ),
    )

    result = bridge.run_generation_request({"slug": "flat"}, config=_redaction_config())

    assert result["reason"] == "generator_exit_nonzero"
    assert result["returncode"] == 23
    assert len(str(result["diagnostic_sha256"])) == 64
    assert PRIVATE_MARKER not in json.dumps(result, sort_keys=True)


def test_bridge_reported_failure_discards_arbitrary_generator_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_redaction_request_boundary(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bridge,
        "_run_generator_process",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["generator"],
            0,
            stdout=json.dumps(
                {
                    "status": "failed",
                    "reason": PRIVATE_MARKER,
                    "private_context": PRIVATE_MARKER,
                }
            ),
            stderr="",
        ),
    )

    result = bridge.run_generation_request({"slug": "flat"}, config=_redaction_config())

    assert result == {
        "status": "failed",
        "reason": "generator_reported_failure",
        "detail": "generator_reported_non_generated_status",
    }
    assert PRIVATE_MARKER not in json.dumps(result, sort_keys=True)


@pytest.mark.parametrize(
    "unsafe_relpath",
    (
        PRIVATE_MARKER,
        "home/tibor/private",
        "C:/Users/private/file.glb",
        "generated-reconstruction/private\nfile.glb",
        "generated-reconstruction/../private.glb",
    ),
)
def test_bridge_success_allowlist_rejects_unsafe_relative_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_relpath: str,
) -> None:
    _patch_redaction_request_boundary(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bridge,
        "_probe_committed_transaction",
        lambda **_kwargs: _proven_recovery(),
    )
    monkeypatch.setattr(
        bridge,
        "_run_generator_process",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["generator"],
            0,
            stdout=json.dumps(
                {
                    "status": "generated",
                    "slug": "flat",
                    "viewer_relpath": "generated-reconstruction/viewer.html",
                    "model_relpath": unsafe_relpath,
                    "private_context": PRIVATE_MARKER,
                }
            ),
            stderr="",
        ),
    )

    result = bridge.run_generation_request({"slug": "flat"}, config=_redaction_config())

    public_result = result["result"]
    assert isinstance(public_result, dict)
    assert public_result["viewer_relpath"] == "generated-reconstruction/viewer.html"
    assert "model_relpath" not in public_result
    assert "private_context" not in public_result


def test_generator_process_abnormal_exit_kills_the_isolated_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class FakeProcess:
        pid = 4242

        def __init__(self, command: list[str], **kwargs: object) -> None:
            observed["kwargs"] = kwargs
            self.returncode: int | None = None
            self.communicate_count = 0

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            self.communicate_count += 1
            if self.communicate_count == 1:
                raise RuntimeError(PRIVATE_MARKER)
            return "", ""

        def kill(self) -> None:
            self.returncode = -9

    process_holder: dict[str, FakeProcess] = {}

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        process = FakeProcess(command, **kwargs)
        process_holder["process"] = process
        return process

    def kill_group(pid: int, sig: int) -> None:
        observed["killpg"] = (pid, sig)
        process_holder["process"].returncode = -9

    monkeypatch.setattr(bridge.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bridge.os, "killpg", kill_group)

    with pytest.raises(RuntimeError) as observed_error:
        bridge._run_generator_process(
            ["generator"],
            timeout_seconds=120,
            env={"PATH": "/usr/bin"},
        )

    kwargs = observed["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["start_new_session"] is True
    assert observed["killpg"] == (4242, signal.SIGKILL)
    assert str(observed_error.value) == "generator_process_communication_failed"
    assert PRIVATE_MARKER not in str(observed_error.value)
    assert process_holder["process"].communicate_count == 2


def test_bridge_does_not_forward_bearer_token_to_generator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_redaction_request_boundary(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN",
        "operator-secret",
    )
    monkeypatch.setenv("LD_PRELOAD", "/attacker/preload.so")
    monkeypatch.setenv("LD_ARBITRARY_FUTURE_CONTROL", "enabled")
    monkeypatch.setenv("GLIBC_TUNABLES", "glibc.rtld.nns=99")
    monkeypatch.setenv("GCONV_PATH", "/attacker/gconv")
    monkeypatch.setenv("NODE_OPTIONS", "--require=/attacker/preload.js")
    monkeypatch.setenv("NODE_PATH", "/attacker/node-modules")
    monkeypatch.setenv("NODE_ARBITRARY_FUTURE_CONTROL", "enabled")
    monkeypatch.setenv("PLAYWRIGHT_NODEJS_PATH", "/attacker/node")
    monkeypatch.setenv("_PLAYWRIGHT_DRIVER_CLI_PATH", "/attacker/cli.js")
    monkeypatch.setenv(
        "_PLAYWRIGHT_DRIVER_EXECUTABLE_PATH", "/attacker/driver"
    )
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/attacker/browsers")
    observed: dict[str, object] = {}

    def generated(
        command: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        observed["env"] = dict(env)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"status": "generated", "slug": "flat"}),
            stderr="",
        )

    monkeypatch.setattr(bridge, "_run_generator_process", generated)
    monkeypatch.setattr(
        bridge,
        "_probe_committed_transaction",
        lambda **_kwargs: _proven_recovery(),
    )

    result = bridge.run_generation_request({"slug": "flat"}, config=_redaction_config())

    assert result["status"] == "generated"
    environment = observed["env"]
    assert isinstance(environment, dict)
    assert "PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN" not in environment
    assert "GLIBC_TUNABLES" not in environment
    assert "GCONV_PATH" not in environment
    assert not any(name.startswith("LD_") for name in environment)
    assert not any(name.startswith("NODE_") for name in environment)
    assert "PLAYWRIGHT_NODEJS_PATH" not in environment
    assert "_PLAYWRIGHT_DRIVER_CLI_PATH" not in environment
    assert "_PLAYWRIGHT_DRIVER_EXECUTABLE_PATH" not in environment
    assert environment["PLAYWRIGHT_BROWSERS_PATH"] == "/ms-playwright"
    assert len(environment["PROPERTYQUARRY_RECONSTRUCTION_TRANSACTION_ID"]) == 32


def test_probe_committed_transaction_requires_exact_marker_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_root = tmp_path / "public_tours"
    public_root.mkdir()
    transaction_id = "1" * 32
    _write_generated_bundle(
        public_root,
        "safe-slug",
        transaction_id=transaction_id,
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_root))

    recovered = bridge._probe_committed_transaction(
        slug="safe-slug",
        transaction_id=transaction_id,
    )

    assert recovered == {
        "slug": "safe-slug",
        "transaction_id_bound": True,
        "publication_durability": "fsynced_by_bridge_recovery",
        "runtime_publish_required": False,
        "runtime_publish_ok": True,
        "runtime_publish_status": "skipped_not_requested",
        "runtime_publication_proven": True,
    }
    assert (
        bridge._probe_committed_transaction(
            slug="safe-slug",
            transaction_id="2" * 32,
        )
        is None
    )


def test_bridge_recovers_a_bound_local_commit_after_generator_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_redaction_request_boundary(monkeypatch, tmp_path)
    observed: dict[str, str] = {}

    def timed_out(
        command: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        observed["transaction_id"] = env[
            "PROPERTYQUARRY_RECONSTRUCTION_TRANSACTION_ID"
        ]
        raise subprocess.TimeoutExpired(command, timeout_seconds)

    def probe(*, slug: str, transaction_id: str) -> dict[str, object] | None:
        assert slug == "flat"
        assert transaction_id == observed["transaction_id"]
        return {
            **_proven_recovery(slug),
        }

    monkeypatch.setattr(bridge, "_run_generator_process", timed_out)
    monkeypatch.setattr(bridge, "_probe_committed_transaction", probe)

    result = bridge.run_generation_request(
        {"slug": "flat"},
        config=_redaction_config(),
    )

    assert result == {
        "status": "generated",
        "result": {
            "slug": "flat",
            "local_commit_applied": True,
            "generator_completion_observed": False,
            "recovery_state": "local_commit_recovered_after_generator_timeout",
            "transaction_id_bound": True,
            "publication_durability": "fsynced_by_bridge_recovery",
            "replaced_bundle_cleanup": "unverified_after_generator_exit",
            "runtime_publish_required": False,
            "runtime_publish_ok": True,
            "runtime_publish_status": "skipped_not_requested",
            "runtime_publication_proven": True,
        },
    }


def test_bridge_reports_nonzero_exit_after_bound_local_commit_honestly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_redaction_request_boundary(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bridge,
        "_run_generator_process",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["generator"],
            17,
            stdout="",
            stderr=f"failure at {PRIVATE_MARKER}",
        ),
    )
    monkeypatch.setattr(
        bridge,
        "_probe_committed_transaction",
        lambda **_kwargs: _proven_recovery(),
    )

    result = bridge.run_generation_request(
        {"slug": "flat"},
        config=_redaction_config(),
    )

    assert result["status"] == "failed"
    assert result["reason"] == "generator_exit_nonzero_after_local_commit"
    assert result["local_commit_applied"] is True
    assert result["transaction_id_bound"] is True
    assert result["returncode"] == 17
    assert len(str(result["diagnostic_sha256"])) == 64
    assert PRIVATE_MARKER not in json.dumps(result, sort_keys=True)


def test_bridge_normal_success_requires_current_transaction_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_root = tmp_path / "public_tours"
    (public_root / "safe-slug").mkdir(parents=True)
    script_path = tmp_path / "generate_property_reconstruction.py"
    script_path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_root))
    monkeypatch.setattr(bridge, "_script_path", lambda: script_path)

    def generated_without_marker(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        _write_generated_bundle(public_root, "safe-slug")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"status": "generated", "slug": "safe-slug"}),
            stderr="",
        )

    monkeypatch.setattr(
        bridge,
        "_run_generator_process",
        generated_without_marker,
    )

    result = bridge.run_generation_request(
        {"slug": "safe-slug", "photo_paths": []},
        config=_redaction_config(),
    )

    assert result == {
        "status": "failed",
        "reason": "generated_bundle_publish_failed",
        "detail": "generated_bundle_transaction_binding_invalid",
    }


def test_bridge_timeout_reports_required_runtime_publication_as_unverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_redaction_request_boundary(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bridge,
        "_run_generator_process",
        lambda command, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(command, kwargs["timeout_seconds"])
        ),
    )
    monkeypatch.setattr(
        bridge,
        "_probe_committed_transaction",
        lambda **_kwargs: {
            "slug": "flat",
            "transaction_id_bound": True,
            "publication_durability": "fsynced_by_bridge_recovery",
            "runtime_publish_required": True,
            "runtime_publish_ok": False,
            "runtime_publish_status": "pending_local_commit",
            "runtime_publication_proven": False,
        },
    )

    result = bridge.run_generation_request(
        {"slug": "flat"},
        config=_redaction_config(),
    )

    assert result["status"] == "failed"
    assert result["reason"] == "runtime_publication_unverified_after_timeout"
    assert result["local_commit_applied"] is True
    assert result["runtime_publish_required"] is True
    assert result["runtime_publication_proven"] is False


def test_bridge_non_object_output_after_commit_is_not_labeled_timeout_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_redaction_request_boundary(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bridge,
        "_run_generator_process",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="[]",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        bridge,
        "_probe_committed_transaction",
        lambda **_kwargs: _proven_recovery(),
    )

    result = bridge.run_generation_request(
        {"slug": "flat"},
        config=_redaction_config(),
    )

    assert result["status"] == "failed"
    assert result["reason"] == "generator_unparseable_after_local_commit"
    assert "timeout" not in json.dumps(result, sort_keys=True)


def test_bridge_serializes_same_slug_until_acknowledgement_finishes() -> None:
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first() -> None:
        with bridge._serialized_generation_slug("same-slug"):
            first_entered.set()
            assert release_first.wait(2)

    def second() -> None:
        with bridge._serialized_generation_slug("same-slug"):
            second_entered.set()

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    assert first_entered.wait(1)
    second_thread.start()
    time.sleep(0.05)
    assert not second_entered.is_set()
    release_first.set()
    first_thread.join(2)
    second_thread.join(2)

    assert second_entered.is_set()
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()


@pytest.mark.parametrize(
    "slug",
    (
        ".propertyquarry-stage-" + ("a" * 32),
        "a..b",
        "space slug",
        "query?slug",
        "slash/slug",
        "back\\slug",
    ),
)
def test_bridge_rejects_noncanonical_or_reserved_slug(slug: str) -> None:
    with pytest.raises(ValueError, match="slug_invalid"):
        bridge._validate_generation_cost(
            {"slug": slug, "photo_paths": []},
            config=_redaction_config(),
        )


def test_bridge_accepts_canonical_slug_contract() -> None:
    bridge._validate_generation_cost(
        {"slug": "A.valid_slug-1", "photo_paths": []},
        config=_redaction_config(),
    )
