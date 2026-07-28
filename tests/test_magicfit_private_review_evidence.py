from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.accept_magicfit_delivery import (
    REVIEW_CHECKS,
    _validate_browser_receipt,
    _validate_evidence,
)
from scripts.build_private_magicfit_review_evidence import (
    BROWSER_RECEIPT_CONTRACT,
    EVIDENCE_CONTRACT,
    REVIEW_PAGE_ROUTE,
    VISUAL_REVIEW_CONTRACT,
    WORKER_MEMORY_MAX_BYTES,
    WORKER_TASKS_MAX,
    _capped_worker_command,
    _private_review_server,
    _require_worker_cgroup_limits,
)
from scripts.propertyquarry_playwright_runtime import (
    playwright_chromium_capture_available,
)


ROOT = Path(__file__).resolve().parents[1]
SLUG = "private-magicfit-review"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _write_video(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg
    completed = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x36:d=1.2",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _fixture(root: Path) -> dict[str, Path]:
    private_root = root / "private"
    bundle = private_root / SLUG
    bundle.mkdir(parents=True)
    video = bundle / "magicfit-walkthrough.mp4"
    _write_video(video)
    source = private_root / "source-receipt.json"
    source.write_text(
        json.dumps(
            {
                "provider": "magicfit",
                "provider_backend_key": "magicfit",
                "render_status": "completed",
                "target_slug": SLUG,
                "output_file": str(video.resolve()),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    generated_at = _utc_now()
    manifest = {
        "slug": SLUG,
        "display_title": "Private local review",
        "video_provider": "magicfit",
        "video_provider_backend_key": "magicfit",
        "video_sidecar_relpath": "tour.magicfit.json",
        "video_relpath": video.name,
    }
    (bundle / "tour.json").write_text(json.dumps(manifest), encoding="utf-8")
    sidecar = {
        "contract_name": "propertyquarry.magicfit_delivery_acceptance.v1",
        "provider": "magicfit",
        "provider_key": "magicfit",
        "provider_backend_key": "magicfit",
        "render_status": "completed",
        "status": "rendered_pending_delivery_acceptance",
        "acceptance_status": "pending",
        "launch_eligible": False,
        "video_relpath": video.name,
        "video_sha256": _sha256(video),
        "source_receipt_sha256": _sha256(source),
        "generated_at": generated_at,
    }
    (bundle / "tour.magicfit.json").write_text(
        json.dumps(sidecar), encoding="utf-8"
    )
    contact = private_root / "contact-sheet.png"
    contact.write_bytes(PNG_1X1)
    visual = private_root / "visual-review.json"
    visual.write_text(
        json.dumps(
            {
                "schema": VISUAL_REVIEW_CONTRACT,
                "status": "pass",
                "provider": "magicfit",
                "target_slug": SLUG,
                "observed_at": generated_at,
                "video_sha256": _sha256(video),
                "checklist": {key: True for key in sorted(REVIEW_CHECKS)},
            }
        ),
        encoding="utf-8",
    )
    return {
        "private_root": private_root,
        "bundle": bundle,
        "video": video,
        "source": source,
        "contact": contact,
        "visual": visual,
        "browser": private_root / "browser.json",
        "evidence": private_root / "evidence.json",
        "public_root": root / "public-tours",
    }


def _command(
    paths: dict[str, Path],
    *,
    allow: bool = True,
    browser_only: bool = False,
) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "build_private_magicfit_review_evidence.py"),
    ]
    if allow:
        command.append("--allow-private-review")
    if browser_only:
        command.append("--browser-only")
    command.extend(
        [
            "--slug",
            SLUG,
            "--bundle-dir",
            str(paths["bundle"]),
            "--source-receipt",
            str(paths["source"]),
            "--browser-receipt-out",
            str(paths["browser"]),
            "--timeout-seconds",
            "20",
        ]
    )
    if not browser_only:
        command.extend(
            [
                "--contact-sheet",
                str(paths["contact"]),
                "--visual-review",
                str(paths["visual"]),
                "--evidence-receipt-out",
                str(paths["evidence"]),
            ]
        )
    return command


def _env(paths: dict[str, Path]) -> dict[str, str]:
    env = dict(os.environ)
    env["EA_PUBLIC_TOUR_DIR"] = str(paths["public_root"])
    env["PROPERTYQUARRY_TOUR_MIN_FREE_BYTES"] = "0"
    env["PROPERTYQUARRY_TOUR_LOCK_DIR"] = str(paths["private_root"] / "locks")
    return env


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not playwright_chromium_capture_available(),
    reason="ffmpeg and a real local Chromium are required",
)
def test_private_magicfit_review_runs_real_token_gated_playback_and_emits_exact_receipts(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    completed = subprocess.run(
        _command(paths),
        cwd=ROOT,
        env=_env(paths),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "private_review_evidence_ready"
    assert result["acceptance_status"] == "pending"
    assert result["launch_eligible"] is False
    assert result["reviewer_authority_generated"] is False
    assert result["published"] is False
    assert result["proof_transport"] == "ephemeral_token_gated_loopback_review_server"
    assert result["public_route_proof"] is False
    assert result["execution_boundary"] == {
        "kind": "transient_user_systemd_scope",
        "memory_max_bytes": 1024 * 1024 * 1024,
        "memory_swap_max_bytes": 0,
        "tasks_max": 128,
        "cpu_quota_percent": 100.0,
        "runtime_max_seconds": 40,
    }
    assert not paths["public_root"].exists()

    browser = json.loads(paths["browser"].read_text(encoding="utf-8"))
    assert browser == {
        "schema": BROWSER_RECEIPT_CONTRACT,
        "status": "pass",
        "provider": "magicfit",
        "target_slug": SLUG,
        "observed_at": browser["observed_at"],
        "route": f"/tours/{SLUG}/walkthrough",
        "http_status": 200,
        "video_sha256": _sha256(paths["video"]),
        "duration_seconds": browser["duration_seconds"],
        "final_current_time": browser["final_current_time"],
        "playback_to_end": True,
        "video_error": None,
        "console_errors": [],
        "request_failures": [],
        "benign_request_aborts": browser["benign_request_aborts"],
        "bad_responses": [],
    }
    assert browser["duration_seconds"] > 1.0
    assert browser["final_current_time"] >= browser["duration_seconds"] - 0.25
    assert len(browser["benign_request_aborts"]) <= 1

    evidence = json.loads(paths["evidence"].read_text(encoding="utf-8"))
    assert evidence["schema"] == EVIDENCE_CONTRACT
    assert evidence["status"] == "pass"
    assert evidence["source_receipt_sha256"] == _sha256(paths["source"])
    assert evidence["video"]["sha256"] == _sha256(paths["video"])
    assert evidence["artifacts"]["contact_sheet_sha256"] == _sha256(
        paths["contact"]
    )
    assert evidence["artifacts"]["browser_receipt_sha256"] == _sha256(
        paths["browser"]
    )
    assert all(evidence["checklist"].values())
    assert paths["browser"].stat().st_mode & 0o777 == 0o600
    assert paths["evidence"].stat().st_mode & 0o777 == 0o600
    assert not list(paths["private_root"].glob(".magicfit-private-review-*"))
    assert not list(paths["private_root"].glob("*authority*"))


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not playwright_chromium_capture_available(),
    reason="ffmpeg and a real local Chromium are required",
)
def test_private_magicfit_browser_only_writes_only_exact_technical_receipt(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    paths["contact"].unlink()
    paths["visual"].unlink()
    completed = subprocess.run(
        _command(paths, browser_only=True),
        cwd=ROOT,
        env=_env(paths),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "private_browser_playback_ready"
    assert result["proof_scope"] == "private_technical_playback_only"
    assert result["proof_transport"] == "ephemeral_token_gated_loopback_review_server"
    assert result["public_route_proof"] is False
    assert result["acceptance_status"] == "pending"
    assert paths["browser"].is_file()
    assert paths["browser"].stat().st_mode & 0o777 == 0o600
    assert not paths["evidence"].exists()
    assert not paths["public_root"].exists()
    browser = json.loads(paths["browser"].read_text(encoding="utf-8"))
    assert browser["schema"] == BROWSER_RECEIPT_CONTRACT
    assert browser["status"] == "pass"
    assert browser["route"] == f"/tours/{SLUG}/walkthrough"

    repeated = subprocess.run(
        _command(paths, browser_only=True),
        cwd=ROOT,
        env=_env(paths),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert repeated.returncode != 0
    assert "magicfit_private_review_output_exists" in repeated.stderr


def test_private_magicfit_review_defaults_denied_and_refuses_public_root(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    denied = subprocess.run(
        _command(paths, allow=False),
        cwd=ROOT,
        env=_env(paths),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert denied.returncode != 0
    assert "magicfit_private_review_not_authorized" in denied.stderr
    assert not paths["browser"].exists()
    assert not paths["evidence"].exists()

    denied_browser_only = subprocess.run(
        _command(paths, allow=False, browser_only=True),
        cwd=ROOT,
        env=_env(paths),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert denied_browser_only.returncode != 0
    assert "magicfit_private_review_not_authorized" in denied_browser_only.stderr
    assert not paths["browser"].exists()

    collision = _command(paths, browser_only=True)
    collision.extend(("--evidence-receipt-out", str(paths["browser"])))
    collided = subprocess.run(
        collision,
        cwd=ROOT,
        env=_env(paths),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert collided.returncode != 0
    assert "magicfit_private_review_output_collision" in collided.stderr
    assert not paths["browser"].exists()

    public_env = _env(paths)
    public_env["EA_PUBLIC_TOUR_DIR"] = str(paths["private_root"])
    public = subprocess.run(
        _command(paths),
        cwd=ROOT,
        env=public_env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert public.returncode != 0
    assert "magicfit_private_review_public_root_forbidden" in public.stderr
    assert not paths["browser"].exists()
    assert not paths["evidence"].exists()


def test_private_review_server_is_loopback_only_and_requires_ephemeral_token(
    tmp_path: Path,
) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"not-needed-for-page-probe")
    token = tmp_path / "token"
    token.write_text("ephemeral-test-token", encoding="ascii")
    token.chmod(0o600)
    route = f"/tours/{SLUG}/walkthrough"

    with _private_review_server(
        video_path=video,
        route=route,
        token_path=token,
    ) as review_url:
        assert review_url.startswith("http://127.0.0.1:")
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(review_url, timeout=3)
        assert denied.value.code == 404
        request = urllib.request.Request(
            review_url,
            headers={"Authorization": "Bearer ephemeral-test-token"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.status == 200
            assert REVIEW_PAGE_ROUTE in review_url
            assert b"tour-video" in response.read()


def test_worker_scope_wrapper_has_every_aggregate_limit_and_no_uncapped_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    systemd_run = tmp_path / "systemd-run"
    systemd_run.write_text("fixture", encoding="utf-8")
    command = _capped_worker_command(
        ["python3", "worker.py"],
        runtime_max_seconds=47,
        systemd_run_path=str(systemd_run),
        unit_suffix="0123456789abcdef",
    )
    assert command[:6] == [
        str(systemd_run),
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        "--unit=propertyquarry-magicfit-review-0123456789abcdef",
    ]
    assert f"--property=MemoryMax={WORKER_MEMORY_MAX_BYTES}" in command
    assert "--property=MemorySwapMax=0" in command
    assert f"--property=TasksMax={WORKER_TASKS_MAX}" in command
    assert "--property=CPUQuota=100%" in command
    assert "--property=RuntimeMaxSec=47s" in command
    assert command[-3:] == ["--", "python3", "worker.py"]

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit, match="cgroup_runner_missing"):
        _capped_worker_command(["python3", "worker.py"], runtime_max_seconds=47)


def test_worker_rejects_uncapped_cgroup_and_accepts_only_tighter_finite_limits(
    tmp_path: Path,
) -> None:
    cgroup_root = tmp_path / "cgroup"
    scope = cgroup_root / "review.scope"
    scope.mkdir(parents=True)
    proc_cgroup = tmp_path / "proc-self-cgroup"
    proc_cgroup.write_text("0::/review.scope\n", encoding="utf-8")
    (scope / "memory.max").write_text(str(512 * 1024 * 1024), encoding="ascii")
    (scope / "memory.swap.max").write_text("0", encoding="ascii")
    (scope / "pids.max").write_text("64", encoding="ascii")
    (scope / "cpu.max").write_text("50000 100000", encoding="ascii")

    assert _require_worker_cgroup_limits(
        proc_cgroup_path=proc_cgroup,
        cgroup_root=cgroup_root,
    ) == {
        "memory_max_bytes": 512 * 1024 * 1024,
        "memory_swap_max_bytes": 0,
        "tasks_max": 64,
        "cpu_quota_percent": 50.0,
    }

    (scope / "memory.max").write_text("max", encoding="ascii")
    with pytest.raises(SystemExit, match="cgroup_memory_uncapped"):
        _require_worker_cgroup_limits(
            proc_cgroup_path=proc_cgroup,
            cgroup_root=cgroup_root,
        )


def test_acceptance_validators_reject_finite_overflow_numbers() -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    observed_at = now.isoformat().replace("+00:00", "Z")
    sha = "a" * 64
    browser = {
        "schema": BROWSER_RECEIPT_CONTRACT,
        "status": "pass",
        "provider": "magicfit",
        "target_slug": SLUG,
        "observed_at": observed_at,
        "route": f"/tours/{SLUG}/walkthrough",
        "http_status": 200,
        "video_sha256": sha,
        "duration_seconds": float("inf"),
        "final_current_time": float("inf"),
        "playback_to_end": True,
        "video_error": None,
        "console_errors": [],
        "request_failures": [],
        "benign_request_aborts": [],
        "bad_responses": [],
    }
    with pytest.raises(SystemExit, match="browser_receipt_contract_invalid"):
        _validate_browser_receipt(
            browser,
            slug=SLUG,
            generated_at=now,
            video_sha256=sha,
            video_duration=1.0,
        )

    evidence = {
        "schema": EVIDENCE_CONTRACT,
        "status": "pass",
        "provider": "magicfit",
        "target_slug": SLUG,
        "observed_at": observed_at,
        "source_receipt_sha256": "b" * 64,
        "video": {
            "sha256": sha,
            "size_bytes": 1,
            "duration_seconds": float("inf"),
        },
        "checklist": {key: True for key in sorted(REVIEW_CHECKS)},
        "artifacts": {
            "contact_sheet_sha256": "c" * 64,
            "browser_receipt_sha256": "d" * 64,
        },
    }
    with pytest.raises(SystemExit, match="evidence_video_mismatch"):
        _validate_evidence(
            evidence,
            slug=SLUG,
            generated_at=now,
            video_sha256=sha,
            video_probe={"size_bytes": 1, "duration_seconds": 1.0},
            source_receipt_sha256="b" * 64,
            contact_sheet_sha256="c" * 64,
            browser_receipt_sha256="d" * 64,
        )
