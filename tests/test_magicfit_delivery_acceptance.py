from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

from scripts.accept_magicfit_delivery import _video_probe
from scripts.verify_property_tour_controls import build_property_tour_control_receipt


ROOT = Path(__file__).resolve().parents[1]
SLUG = "locally-reviewed-magicfit"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(script: str, tour_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["EA_PUBLIC_TOUR_DIR"] = str(tour_root)
    # Fixture materialization is independent of the host's production disk floor;
    # fail-closed low-disk behavior has a dedicated importer integration test.
    env["PROPERTYQUARRY_TOUR_MIN_FREE_BYTES"] = "0"
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _write_playable_mp4(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "ffmpeg is required for MagicFit acceptance fixtures"
    completed = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=32x32:d=1",
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


def _materialize_pending(root: Path) -> dict[str, Path]:
    tour_root = root / "public_tours"
    bundle = tour_root / SLUG
    bundle.mkdir(parents=True)
    manifest = bundle / "tour.json"
    manifest.write_text(
        json.dumps({"slug": SLUG, "display_title": "Local review target"}),
        encoding="utf-8",
    )
    video = root / "walkthrough.mp4"
    _write_playable_mp4(video)
    source_receipt = root / "provider-receipt.json"
    source_receipt.write_text(
        json.dumps(
            {
                "provider": "magicfit",
                "provider_key": "magicfit",
                "provider_backend_key": "magicfit",
                "render_status": "completed",
                "target_slug": SLUG,
                "output_file": str(video.resolve()),
                "hosted_walkthrough_video_url": (
                    "https://media.powlcdn.com/magicfit/local-review.mp4"
                ),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    imported = _run(
        "import_magicfit_walkthrough.py",
        tour_root,
        "--slug",
        SLUG,
        "--video-path",
        str(video),
        "--source-receipt",
        str(source_receipt),
    )
    assert imported.returncode == 0, imported.stderr

    sidecar = json.loads((bundle / "tour.magicfit.json").read_text(encoding="utf-8"))
    imported_video = bundle / sidecar["video_relpath"]
    probe = _video_probe(imported_video)
    contact_sheet = root / "contact-sheet.png"
    Image.new("RGB", (96, 54), color=(28, 36, 42)).save(contact_sheet, format="PNG")
    authority = root / "reviewer-authority.pem"
    authority.write_text(
        "-----BEGIN PUBLIC KEY-----\nfixture-local-authority\n-----END PUBLIC KEY-----\n",
        encoding="utf-8",
    )
    browser_receipt = root / "browser-receipt.json"
    browser_receipt.write_text(
        json.dumps(
            {
                "schema": "propertyquarry.magicfit_browser_playback.v1",
                "status": "pass",
                "provider": "magicfit",
                "target_slug": SLUG,
                "observed_at": sidecar["generated_at"],
                "route": f"/tours/{SLUG}/walkthrough",
                "http_status": 200,
                "video_sha256": sidecar["video_sha256"],
                "duration_seconds": probe["duration_seconds"],
                "final_current_time": probe["duration_seconds"],
                "playback_to_end": True,
                "video_error": None,
                "console_errors": [],
                "request_failures": [],
                "benign_request_aborts": [
                    {
                        "failure": "net::ERR_ABORTED",
                        "method": "GET",
                        "resource_type": "media",
                        "route": f"/tours/{SLUG}/walkthrough",
                    }
                ],
                "bad_responses": [],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    evidence = root / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "schema": "propertyquarry.magicfit_e2e_evidence.v1",
                "status": "pass",
                "provider": "magicfit",
                "target_slug": SLUG,
                "observed_at": sidecar["generated_at"],
                "source_receipt_sha256": sidecar["source_receipt_sha256"],
                "video": {
                    "sha256": sidecar["video_sha256"],
                    "size_bytes": probe["size_bytes"],
                    "duration_seconds": probe["duration_seconds"],
                },
                "checklist": {
                    "playback_to_end": True,
                    "continuous_walkthrough": True,
                    "no_visible_rotation_jump": True,
                    "intended_property_and_scope": True,
                    "no_sensitive_or_trial_branding": True,
                },
                "artifacts": {
                    "contact_sheet_sha256": _sha256(contact_sheet),
                    "browser_receipt_sha256": _sha256(browser_receipt),
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return {
        "tour_root": tour_root,
        "bundle": bundle,
        "manifest": manifest,
        "video": imported_video,
        "sidecar": bundle / "tour.magicfit.json",
        "source_receipt": source_receipt,
        "contact_sheet": contact_sheet,
        "browser_receipt": browser_receipt,
        "evidence": evidence,
        "authority": authority,
    }


def _accept(paths: dict[str, Path]) -> subprocess.CompletedProcess[str]:
    return _run(
        "accept_magicfit_delivery.py",
        paths["tour_root"],
        "--slug",
        SLUG,
        "--source-receipt",
        str(paths["source_receipt"]),
        "--evidence-receipt",
        str(paths["evidence"]),
        "--contact-sheet",
        str(paths["contact_sheet"]),
        "--browser-receipt",
        str(paths["browser_receipt"]),
        "--reviewer-authority",
        str(paths["authority"]),
    )


def test_magicfit_acceptance_binds_exact_pending_delivery_and_evidence(
    tmp_path: Path,
) -> None:
    paths = _materialize_pending(tmp_path)
    manifest_before = paths["manifest"].read_bytes()
    video_before = paths["video"].read_bytes()

    accepted = _accept(paths)

    assert accepted.returncode == 0, accepted.stderr
    result = json.loads(accepted.stdout)
    assert result["status"] == "delivery_accepted"
    assert result["reviewer_authority_sha256"] == _sha256(paths["authority"])
    assert result["evidence_sha256"] == _sha256(paths["evidence"])
    assert paths["manifest"].read_bytes() == manifest_before
    assert paths["video"].read_bytes() == video_before
    sidecar = json.loads(paths["sidecar"].read_text(encoding="utf-8"))
    assert sidecar["status"] == "delivery_accepted"
    assert sidecar["acceptance_status"] == "accepted"
    assert sidecar["launch_eligible"] is True
    assert sidecar["review"]["subject"]["tour_slug"] == SLUG
    assert all(sidecar["review"]["checklist"].values())
    assert paths["sidecar"].stat().st_mode & 0o777 == 0o600

    verifier = build_property_tour_control_receipt(tour_root=paths["tour_root"])
    assert verifier["provider_counts"]["magicfit"] == 1
    assert verifier["ready_provider_modes"] == ["magicfit"]


def test_magicfit_acceptance_rejects_mismatched_or_incomplete_evidence(
    tmp_path: Path,
) -> None:
    cases = (
        "wrong_video_digest",
        "failed_checklist",
        "browser_not_ended",
        "browser_arbitrary_abort",
        "changed_source_receipt",
    )
    for case in cases:
        paths = _materialize_pending(tmp_path / case)
        pending_before = paths["sidecar"].read_bytes()
        if case == "wrong_video_digest":
            evidence = json.loads(paths["evidence"].read_text(encoding="utf-8"))
            evidence["video"]["sha256"] = "0" * 64
            paths["evidence"].write_text(json.dumps(evidence), encoding="utf-8")
        elif case == "failed_checklist":
            evidence = json.loads(paths["evidence"].read_text(encoding="utf-8"))
            evidence["checklist"]["continuous_walkthrough"] = False
            paths["evidence"].write_text(json.dumps(evidence), encoding="utf-8")
        elif case == "browser_not_ended":
            browser = json.loads(paths["browser_receipt"].read_text(encoding="utf-8"))
            browser["playback_to_end"] = False
            paths["browser_receipt"].write_text(json.dumps(browser), encoding="utf-8")
            evidence = json.loads(paths["evidence"].read_text(encoding="utf-8"))
            evidence["artifacts"]["browser_receipt_sha256"] = _sha256(
                paths["browser_receipt"]
            )
            paths["evidence"].write_text(json.dumps(evidence), encoding="utf-8")
        elif case == "browser_arbitrary_abort":
            browser = json.loads(paths["browser_receipt"].read_text(encoding="utf-8"))
            browser["benign_request_aborts"][0]["route"] = "/unrelated"
            paths["browser_receipt"].write_text(json.dumps(browser), encoding="utf-8")
            evidence = json.loads(paths["evidence"].read_text(encoding="utf-8"))
            evidence["artifacts"]["browser_receipt_sha256"] = _sha256(
                paths["browser_receipt"]
            )
            paths["evidence"].write_text(json.dumps(evidence), encoding="utf-8")
        else:
            paths["source_receipt"].write_text(
                paths["source_receipt"].read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )

        rejected = _accept(paths)

        assert rejected.returncode != 0, case
        assert paths["sidecar"].read_bytes() == pending_before
        verifier = build_property_tour_control_receipt(tour_root=paths["tour_root"])
        assert verifier["provider_counts"]["magicfit"] == 0
