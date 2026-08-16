from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from app.api.routes import public_tours
from scripts import property_magicfit_public_eligibility as public_eligibility

from scripts.accept_magicfit_delivery import (
    ACCEPTED_DELIVERY_CONTRACT,
    BROWSER_RECEIPT_CONTRACT,
    EVIDENCE_CONTRACT,
    REVIEW_CONTRACT,
    REVIEW_CHECKS,
    VISUAL_REVIEW_CONTRACT,
    _activation_lock,
    _acceptance_reviewed_at,
    _confirm_named_bundle_identity,
    _video_probe,
    _write_bundle_bytes_atomic,
)
from scripts.property_magicfit_delivery_contract import (
    AUDIT_ARTIFACT_NAMES,
    AUDIT_CONTRACT,
    MANIFEST_TRANSFORM_CONTRACT,
)
from scripts.property_magicfit_secure_io import (
    publish_magicfit_review_receipt_bundle,
)
from scripts.property_magicfit_reviewer_authority import (
    REVIEWER_TEST_OWNER_UID_ENV,
    REVIEWER_TRUST_STORE_ENV,
)
from scripts.property_tour_publication_lock import property_tour_publication_lock
from scripts.verify_property_tour_controls import build_property_tour_control_receipt
from tests.magicfit_test_support import (
    magicfit_reviewer_subject,
    provision_magicfit_reviewer_test_authority,
)


ROOT = Path(__file__).resolve().parents[1]
SLUG = "locally-reviewed-magicfit"


@pytest.fixture(autouse=True)
def _isolate_reviewer_trust_store_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REVIEWER_TRUST_STORE_ENV, raising=False)
    monkeypatch.setenv(REVIEWER_TEST_OWNER_UID_ENV, str(os.geteuid()))


@pytest.mark.parametrize(
    "later_subject",
    ("generated_at", "browser", "visual", "evidence"),
)
def test_acceptance_review_timestamp_cannot_precede_any_review_subject(
    later_subject: str,
) -> None:
    reviewed_at = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    generated_at = reviewed_at
    receipts = {
        "browser": {"observed_at": "2026-07-20T12:00:00Z"},
        "visual": {"observed_at": "2026-07-20T12:00:00Z"},
        "evidence": {"observed_at": "2026-07-20T12:00:00Z"},
    }
    later = reviewed_at + timedelta(minutes=4)
    if later_subject == "generated_at":
        generated_at = later
    else:
        receipts[later_subject]["observed_at"] = "2026-07-20T12:04:00Z"

    with pytest.raises(
        SystemExit,
        match="magicfit_acceptance_review_timestamp_invalid",
    ):
        _acceptance_reviewed_at(
            generated_at=generated_at,
            browser_receipt=receipts["browser"],
            visual_review=receipts["visual"],
            evidence=receipts["evidence"],
            now=reviewed_at,
        )


def test_acceptance_review_timestamp_may_equal_latest_review_subject() -> None:
    reviewed_at = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    receipt = {"observed_at": "2026-07-20T12:00:00Z"}

    assert _acceptance_reviewed_at(
        generated_at=reviewed_at,
        browser_receipt=receipt,
        visual_review=receipt,
        evidence=receipt,
        now=reviewed_at,
    ) == "2026-07-20T12:00:00Z"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(
    script: str,
    tour_root: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("PROPERTYQUARRY_MAGICFIT_ACTIVATION_FAILPOINT", None)
    env["EA_PUBLIC_TOUR_DIR"] = str(tour_root)
    # Fixture materialization is independent of the host's production disk
    # floor; fail-closed low-disk behavior has dedicated importer coverage.
    env["PROPERTYQUARRY_TOUR_MIN_FREE_BYTES"] = "0"
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        # The acceptor performs bounded media probes in a child process. Give
        # those probes enough headroom when the full browser/media suite is
        # concurrently releasing host resources, while keeping a hard bound.
        timeout=60,
        check=False,
    )


def _write_playable_mp4(path: Path, *, color: str = "black") -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "ffmpeg is required for MagicFit acceptance fixtures"
    completed = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=32x32:d=1",
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


def _video_probe_without_ambient_event_loop(path: Path) -> dict[str, object]:
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(_video_probe, path).result()


def _materialize_pending(
    root: Path, *, color: str = "black"
) -> dict[str, Path]:
    tour_root = root / "public_tours"
    bundle = tour_root / SLUG
    bundle.mkdir(parents=True, exist_ok=True)
    manifest = bundle / "tour.json"
    if not manifest.exists():
        manifest.write_text(
            json.dumps({"slug": SLUG, "display_title": "Local review target"}),
            encoding="utf-8",
        )
    video = root / f"walkthrough-{color}.mp4"
    _write_playable_mp4(video, color=color)
    source_receipt = root / f"provider-receipt-{color}.json"
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
                    f"https://media.powlcdn.com/magicfit/local-review-{color}.mp4"
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

    pending_path = bundle / "tour.magicfit.pending.json"
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    staged_video = bundle / pending["staged_video_relpath"]
    staged_manifest = bundle / pending["staged_manifest_relpath"]
    final_video = bundle / pending["video_relpath"]
    accepted_sidecar = bundle / pending["accepted_sidecar_relpath"]
    delivery_digest = Path(pending["accepted_sidecar_relpath"]).stem
    probe = _video_probe_without_ambient_event_loop(staged_video)
    contact_sheet = root / f"contact-sheet-{color}.png"
    Image.new("RGB", (96, 54), color=(28, 36, 42)).save(contact_sheet, format="PNG")
    browser_receipt = root / f"browser-receipt-{color}.json"
    browser_receipt.write_text(
        json.dumps(
            {
                "schema": BROWSER_RECEIPT_CONTRACT,
                "status": "pass",
                "provider": "magicfit",
                "target_slug": SLUG,
                "observed_at": pending["generated_at"],
                "route": (
                    "operator-review://propertyquarry/magicfit/"
                    f"{SLUG}/{pending['video_sha256']}"
                ),
                "http_status": 200,
                "video_sha256": pending["video_sha256"],
                "base_manifest_sha256": pending["base_manifest_sha256"],
                "staged_manifest_sha256": pending["staged_manifest_sha256"],
                "delivery_digest": delivery_digest,
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
                        "route": (
                            "operator-review://propertyquarry/magicfit/"
                            f"{SLUG}/{pending['video_sha256']}"
                        ),
                    }
                ],
                "bad_responses": [],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    visual_review = root / f"visual-review-{color}.json"
    visual_review.write_text(
        json.dumps(
            {
                "schema": VISUAL_REVIEW_CONTRACT,
                "status": "pass",
                "provider": "magicfit",
                "target_slug": SLUG,
                "observed_at": pending["generated_at"],
                "video_sha256": pending["video_sha256"],
                "base_manifest_sha256": pending["base_manifest_sha256"],
                "staged_manifest_sha256": pending["staged_manifest_sha256"],
                "delivery_digest": delivery_digest,
                "checklist": {key: True for key in sorted(REVIEW_CHECKS)},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    evidence = root / f"evidence-{color}.json"
    evidence.write_text(
        json.dumps(
            {
                "schema": EVIDENCE_CONTRACT,
                "status": "pass",
                "provider": "magicfit",
                "target_slug": SLUG,
                "observed_at": pending["generated_at"],
                "source_receipt_sha256": pending["source_receipt_sha256"],
                "base_manifest_sha256": pending["base_manifest_sha256"],
                "staged_manifest_sha256": pending["staged_manifest_sha256"],
                "delivery_digest": delivery_digest,
                "video": {
                    "sha256": pending["video_sha256"],
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
                    "visual_review_sha256": _sha256(visual_review),
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    issued_at_value = datetime.now(timezone.utc).replace(microsecond=0)
    reviewed_at = pending["generated_at"]
    reviewer_test_authority = provision_magicfit_reviewer_test_authority(
        root / "reviewer-trust",
        public_tour_root=tour_root,
    )
    authority = root / f"reviewer-authority-{color}.json"
    signed_authorization = reviewer_test_authority.sign_authorization(
        subject=magicfit_reviewer_subject(
            delivery_digest=delivery_digest,
            video_sha256=pending["video_sha256"],
            staged_manifest_sha256=pending["staged_manifest_sha256"],
            browser_receipt_sha256=_sha256(browser_receipt),
            evidence_receipt_sha256=_sha256(evidence),
            visual_review_sha256=_sha256(visual_review),
            contact_sheet_sha256=_sha256(contact_sheet),
            reviewed_at=reviewed_at,
        ),
        issued_at=issued_at_value.isoformat().replace("+00:00", "Z"),
        expires_at=(issued_at_value + timedelta(hours=1))
        .isoformat()
        .replace("+00:00", "Z"),
        authorization_path=authority,
    )
    assert signed_authorization.path == authority
    os.environ[REVIEWER_TRUST_STORE_ENV] = str(
        reviewer_test_authority.trust_store_path
    )
    review_bundle_root = root / "private-review-receipts"
    review_bundle_root.mkdir(mode=0o700, exist_ok=True)
    review_bundle_root.chmod(0o700)
    review_bundle = review_bundle_root / delivery_digest
    return {
        "tour_root": tour_root,
        "bundle": bundle,
        "manifest": manifest,
        "staged_video": staged_video,
        "staged_manifest": staged_manifest,
        "final_video": final_video,
        "pending": pending_path,
        "accepted_sidecar": accepted_sidecar,
        "source_receipt": source_receipt,
        "contact_sheet": contact_sheet,
        "browser_receipt": browser_receipt,
        "visual_review": visual_review,
        "evidence": evidence,
        "authority": authority,
        "reviewer_trust_store": reviewer_test_authority.trust_store_path,
        "review_bundle_root": review_bundle_root,
        "review_bundle": review_bundle,
    }


def _publish_fixture_review_bundle(paths: dict[str, Path]) -> None:
    if os.path.lexists(paths["review_bundle"]):
        return
    publish_magicfit_review_receipt_bundle(
        paths["review_bundle_root"],
        delivery_digest=paths["review_bundle"].name,
        browser_receipt_bytes=paths["browser_receipt"].read_bytes(),
        evidence_receipt_bytes=paths["evidence"].read_bytes(),
        reason="fixture_review_receipt_bundle_invalid",
    )


def _accept(
    paths: dict[str, Path],
    *,
    failpoint: str = "",
    publish_review_bundle: bool = True,
) -> subprocess.CompletedProcess[str]:
    if publish_review_bundle:
        _publish_fixture_review_bundle(paths)
    acceptance_env = {
        REVIEWER_TRUST_STORE_ENV: str(paths["reviewer_trust_store"]),
    }
    if failpoint:
        acceptance_env["PROPERTYQUARRY_MAGICFIT_ACTIVATION_FAILPOINT"] = failpoint
    return _run(
        "accept_magicfit_delivery.py",
        paths["tour_root"],
        "--slug",
        SLUG,
        "--source-receipt",
        str(paths["source_receipt"]),
        "--contact-sheet",
        str(paths["contact_sheet"]),
        "--review-bundle",
        str(paths["review_bundle"]),
        "--visual-review",
        str(paths["visual_review"]),
        "--reviewer-authority",
        str(paths["authority"]),
        extra_env=acceptance_env,
    )


@pytest.mark.parametrize("later_receipt", ("browser", "visual", "evidence"))
def test_magicfit_acceptance_rejects_review_subject_inside_future_skew(
    tmp_path: Path,
    later_receipt: str,
) -> None:
    paths = _materialize_pending(tmp_path)
    observed_at = (
        (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=4))
        .isoformat()
        .replace("+00:00", "Z")
    )
    receipt_path = paths[
        {
            "browser": "browser_receipt",
            "visual": "visual_review",
            "evidence": "evidence",
        }[later_receipt]
    ]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["observed_at"] = observed_at
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True),
        encoding="utf-8",
    )
    if later_receipt in {"browser", "visual"}:
        evidence = json.loads(paths["evidence"].read_text(encoding="utf-8"))
        artifact_key = (
            "browser_receipt_sha256"
            if later_receipt == "browser"
            else "visual_review_sha256"
        )
        evidence["artifacts"][artifact_key] = _sha256(receipt_path)
        paths["evidence"].write_text(
            json.dumps(evidence, sort_keys=True),
            encoding="utf-8",
        )

    rejected = _accept(paths)

    assert rejected.returncode != 0
    assert "magicfit_acceptance_review_timestamp_invalid" in rejected.stderr
    assert not paths["accepted_sidecar"].exists()


def test_magicfit_acceptance_binds_exact_pending_delivery_and_evidence(
    tmp_path: Path,
) -> None:
    paths = _materialize_pending(tmp_path)
    manifest_before = paths["manifest"].read_bytes()
    video_before = paths["staged_video"].read_bytes()
    pending_before = json.loads(paths["pending"].read_text(encoding="utf-8"))

    # Import is private staging only: neither the active manifest nor a public
    # media path changes before the evidence-backed acceptance commit.
    assert json.loads(manifest_before) == {
        "slug": SLUG,
        "display_title": "Local review target",
    }
    assert not paths["final_video"].exists()
    assert not paths["accepted_sidecar"].exists()

    accepted = _accept(paths)

    assert accepted.returncode == 0, accepted.stderr
    result = json.loads(accepted.stdout)
    assert result["status"] == "delivery_accepted"
    assert result["reviewer_authority_sha256"] == _sha256(paths["authority"])
    assert result["evidence_sha256"] == _sha256(paths["evidence"])
    assert paths["manifest"].read_bytes() != manifest_before
    active_manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert active_manifest["video_relpath"] == result["video_relpath"]
    assert active_manifest["magicfit_import"]["proof_status"] == "delivery_accepted"
    assert paths["final_video"].read_bytes() == video_before
    assert paths["final_video"].stat().st_mode & 0o777 == 0o444
    assert not paths["staged_video"].exists()
    assert not paths["staged_manifest"].parent.exists()
    assert not paths["pending"].exists()
    sidecar = json.loads(
        paths["accepted_sidecar"].read_text(encoding="utf-8")
    )
    assert sidecar["status"] == "delivery_accepted"
    assert sidecar["contract_name"] == ACCEPTED_DELIVERY_CONTRACT
    assert sidecar["acceptance_status"] == "accepted"
    assert sidecar["launch_eligible"] is True
    assert sidecar["review"]["subject"]["tour_slug"] == SLUG
    assert sidecar["review"]["contract_name"] == REVIEW_CONTRACT
    assert sidecar["review"]["reviewer_authorization"]["delivery_digest"] == sidecar[
        "delivery_digest"
    ]
    assert sidecar["review"]["visual_review_sha256"] == _sha256(
        paths["visual_review"]
    )
    assert sidecar["base_manifest_sha256"] == pending_before["base_manifest_sha256"]
    assert sidecar["staged_manifest_sha256"] == pending_before[
        "staged_manifest_sha256"
    ]
    assert sidecar["delivery_digest"] == Path(
        pending_before["accepted_sidecar_relpath"]
    ).stem
    assert sidecar["review"]["subject"] == {
        "tour_slug": SLUG,
        "provider": "magicfit",
        "delivery_contract_name": ACCEPTED_DELIVERY_CONTRACT,
        "manifest_transform_contract": MANIFEST_TRANSFORM_CONTRACT,
        "requested_target_relpath": sidecar["requested_target_relpath"],
        "source_receipt_sha256": sidecar["source_receipt_sha256"],
        "video_relpath": sidecar["video_relpath"],
        "video_sha256": sidecar["video_sha256"],
        "video_size_bytes": sidecar["video_size_bytes"],
        "coverage_proof": sidecar["coverage_proof"],
        "base_manifest_sha256": sidecar["base_manifest_sha256"],
        "staged_manifest_sha256": sidecar["staged_manifest_sha256"],
        "delivery_digest": sidecar["delivery_digest"],
    }
    assert all(sidecar["review"]["checklist"].values())
    assert paths["accepted_sidecar"].stat().st_mode & 0o777 == 0o600
    assert sidecar["audit"]["contract_name"] == AUDIT_CONTRACT
    assert set(sidecar["audit"]["artifacts"]) == set(AUDIT_ARTIFACT_NAMES)
    for artifact in sidecar["audit"]["artifacts"].values():
        audit_path = paths["bundle"] / artifact["relpath"]
        assert audit_path.is_file()
        assert audit_path.stat().st_mode & 0o777 == 0o600
        assert audit_path.stat().st_size == artifact["size_bytes"]
        assert _sha256(audit_path) == artifact["sha256"]

    verifier = build_property_tour_control_receipt(tour_root=paths["tour_root"])
    assert verifier["provider_counts"]["magicfit"] == 1
    assert verifier["ready_provider_modes"] == ["magicfit"]


@pytest.mark.parametrize(
    "authority_failure",
    ("bad_signature", "unknown_key", "public_root_authorization"),
)
def test_magicfit_acceptance_rejects_unauthorized_review_without_public_mutation(
    tmp_path: Path,
    authority_failure: str,
) -> None:
    paths = _materialize_pending(tmp_path / authority_failure)
    manifest_before = paths["manifest"].read_bytes()
    pending_before = paths["pending"].read_bytes()
    staged_video_before = paths["staged_video"].read_bytes()
    authority_payload = json.loads(paths["authority"].read_text(encoding="utf-8"))
    if authority_failure == "bad_signature":
        authority_payload["signature_base64"] = base64.b64encode(
            b"\0" * 64
        ).decode("ascii")
        paths["authority"].write_text(
            json.dumps(authority_payload, sort_keys=True), encoding="utf-8"
        )
        paths["authority"].chmod(0o600)
    elif authority_failure == "unknown_key":
        authority_payload["key_id"] = "unknown-reviewer-key"
        paths["authority"].write_text(
            json.dumps(authority_payload, sort_keys=True), encoding="utf-8"
        )
        paths["authority"].chmod(0o600)
    else:
        public_authorization = paths["bundle"] / "untrusted-authorization.json"
        public_authorization.write_bytes(paths["authority"].read_bytes())
        public_authorization.chmod(0o600)
        paths = {**paths, "authority": public_authorization}

    rejected = _accept(paths)

    assert rejected.returncode != 0
    assert "magicfit_acceptance_reviewer_authorization_invalid" in rejected.stderr
    assert paths["manifest"].read_bytes() == manifest_before
    assert paths["pending"].read_bytes() == pending_before
    assert paths["staged_video"].read_bytes() == staged_video_before
    assert not paths["final_video"].exists()
    assert not paths["accepted_sidecar"].exists()


def test_magicfit_acceptance_fails_closed_on_legacy_loose_receipt_pair(
    tmp_path: Path,
) -> None:
    paths = _materialize_pending(tmp_path)

    rejected = _run(
        "accept_magicfit_delivery.py",
        paths["tour_root"],
        "--slug",
        SLUG,
        "--source-receipt",
        str(paths["source_receipt"]),
        "--browser-receipt",
        str(paths["browser_receipt"]),
        "--evidence-receipt",
        str(paths["evidence"]),
        "--contact-sheet",
        str(paths["contact_sheet"]),
        "--visual-review",
        str(paths["visual_review"]),
        "--reviewer-authority",
        str(paths["authority"]),
    )

    assert rejected.returncode != 0
    assert "magicfit_acceptance_legacy_loose_receipts_forbidden" in rejected.stderr
    assert paths["pending"].is_file()
    assert not paths["accepted_sidecar"].exists()


def test_magicfit_acceptance_consumes_only_committed_review_bundle_receipts(
    tmp_path: Path,
) -> None:
    paths = _materialize_pending(tmp_path)
    _publish_fixture_review_bundle(paths)
    committed_browser = (
        paths["review_bundle"] / "browser-receipt.json"
    ).read_bytes()
    committed_evidence = (
        paths["review_bundle"] / "evidence-receipt.json"
    ).read_bytes()
    paths["browser_receipt"].write_bytes(b"untrusted loose replacement\n")
    paths["evidence"].write_bytes(b"untrusted loose replacement\n")

    accepted = _accept(paths, publish_review_bundle=False)

    assert accepted.returncode == 0, accepted.stderr
    sidecar = json.loads(paths["accepted_sidecar"].read_text(encoding="utf-8"))
    browser_entry = sidecar["audit"]["artifacts"]["browser_receipt"]
    evidence_entry = sidecar["audit"]["artifacts"]["evidence_receipt"]
    assert (
        paths["bundle"] / str(browser_entry["relpath"])
    ).read_bytes() == committed_browser
    assert (
        paths["bundle"] / str(evidence_entry["relpath"])
    ).read_bytes() == committed_evidence


@pytest.mark.parametrize(
    "case",
    (
        "missing_bundle",
        "missing_receipt",
        "corrupt_manifest",
        "symlink_receipt",
        "symlink_bundle",
        "wrong_digest",
    ),
)
def test_magicfit_acceptance_rejects_uncommitted_or_invalid_review_bundle(
    tmp_path: Path, case: str
) -> None:
    paths = _materialize_pending(tmp_path / case)
    if case != "missing_bundle":
        _publish_fixture_review_bundle(paths)
    if case == "missing_receipt":
        (paths["review_bundle"] / "evidence-receipt.json").unlink()
    elif case == "corrupt_manifest":
        manifest_path = paths["review_bundle"] / "bundle-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["contract_name"] = "propertyquarry.magicfit_loose_pair.v0"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_path.chmod(0o600)
    elif case == "symlink_receipt":
        receipt_path = paths["review_bundle"] / "browser-receipt.json"
        real_path = receipt_path.with_name("browser-receipt.real.json")
        receipt_path.replace(real_path)
        receipt_path.symlink_to(real_path.name)
    elif case == "symlink_bundle":
        real_bundle = paths["review_bundle"].with_name(
            f"{paths['review_bundle'].name}.real"
        )
        paths["review_bundle"].replace(real_bundle)
        paths["review_bundle"].symlink_to(real_bundle.name, target_is_directory=True)
    elif case == "wrong_digest":
        wrong_digest = "f" * 64
        if wrong_digest == paths["review_bundle"].name:
            wrong_digest = "e" * 64
        wrong_bundle = publish_magicfit_review_receipt_bundle(
            paths["review_bundle_root"],
            delivery_digest=wrong_digest,
            browser_receipt_bytes=b'{"status":"pass"}\n',
            evidence_receipt_bytes=b'{"status":"pass"}\n',
        )
        paths = {**paths, "review_bundle": wrong_bundle.path}

    rejected = _accept(paths, publish_review_bundle=False)

    assert rejected.returncode != 0
    assert "magicfit_acceptance_review_receipt_bundle_invalid" in rejected.stderr
    assert paths["pending"].is_file()
    assert not paths["accepted_sidecar"].exists()


def test_magicfit_verifier_rejects_active_manifest_content_or_byte_changes(
    tmp_path: Path,
) -> None:
    for mutation in ("content", "whitespace"):
        paths = _materialize_pending(tmp_path / mutation)
        assert _accept(paths).returncode == 0
        assert build_property_tour_control_receipt(
            tour_root=paths["tour_root"]
        )["provider_counts"]["magicfit"] == 1

        if mutation == "content":
            manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
            manifest["display_title"] = "Changed after exact-byte review"
            paths["manifest"].write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
        else:
            paths["manifest"].write_bytes(paths["manifest"].read_bytes() + b"\n")

        receipt = build_property_tour_control_receipt(tour_root=paths["tour_root"])
        assert receipt["provider_counts"]["magicfit"] == 0
        missing = {
            row["provider"]: row
            for row in receipt["tours"][0]["missing_evidence"]
        }
        assert missing["magicfit"]["reason"] == "magicfit_walkthrough_disqualified"


def test_magicfit_verifier_requires_every_exact_audit_snapshot(
    tmp_path: Path,
) -> None:
    paths = _materialize_pending(tmp_path)
    assert _accept(paths).returncode == 0
    sidecar = json.loads(paths["accepted_sidecar"].read_text(encoding="utf-8"))

    for name in AUDIT_ARTIFACT_NAMES:
        artifact = sidecar["audit"]["artifacts"][name]
        artifact_path = paths["bundle"] / artifact["relpath"]
        original = artifact_path.read_bytes()

        artifact_path.unlink()
        assert build_property_tour_control_receipt(
            tour_root=paths["tour_root"]
        )["provider_counts"]["magicfit"] == 0
        artifact_path.write_bytes(original)
        artifact_path.chmod(0o600)

        artifact_path.write_bytes(original + b"\n")
        assert build_property_tour_control_receipt(
            tour_root=paths["tour_root"]
        )["provider_counts"]["magicfit"] == 0
        artifact_path.write_bytes(original)
        artifact_path.chmod(0o600)

    assert build_property_tour_control_receipt(
        tour_root=paths["tour_root"]
    )["provider_counts"]["magicfit"] == 1


def test_magicfit_verifier_rejects_symlinked_accepted_integrity_subjects(
    tmp_path: Path,
) -> None:
    for subject in ("accepted_sidecar", "final_video"):
        paths = _materialize_pending(tmp_path / subject)
        assert _accept(paths).returncode == 0
        subject_path = paths[subject]
        target_path = subject_path.with_name(f"verified-copy-{subject_path.name}")
        subject_path.replace(target_path)
        subject_path.symlink_to(target_path.name)
        assert subject_path.is_symlink()

        receipt = build_property_tour_control_receipt(tour_root=paths["tour_root"])
        assert receipt["provider_counts"]["magicfit"] == 0
        missing = {
            row["provider"]: row
            for row in receipt["tours"][0]["missing_evidence"]
        }
        assert missing["magicfit"]["reason"] == "magicfit_walkthrough_disqualified"


def test_magicfit_acceptance_rejects_mismatched_or_incomplete_evidence(
    tmp_path: Path,
) -> None:
    cases = (
        "wrong_video_digest",
        "failed_checklist",
        "browser_not_ended",
        "browser_arbitrary_abort",
        "changed_source_receipt",
        "visual_review_wrong_subject",
        "legacy_v1_visual_review",
        "legacy_v1_receipts",
    )
    for case in cases:
        paths = _materialize_pending(tmp_path / case)
        pending_before = paths["pending"].read_bytes()
        manifest_before = paths["manifest"].read_bytes()
        if case == "wrong_video_digest":
            evidence = json.loads(paths["evidence"].read_text(encoding="utf-8"))
            evidence["video"]["sha256"] = "0" * 64
            paths["evidence"].write_text(json.dumps(evidence), encoding="utf-8")
        elif case == "failed_checklist":
            evidence = json.loads(paths["evidence"].read_text(encoding="utf-8"))
            evidence["checklist"]["continuous_walkthrough"] = False
            paths["evidence"].write_text(json.dumps(evidence), encoding="utf-8")
        elif case == "browser_not_ended":
            browser = json.loads(
                paths["browser_receipt"].read_text(encoding="utf-8")
            )
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
        elif case == "changed_source_receipt":
            paths["source_receipt"].write_text(
                paths["source_receipt"].read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
        elif case in {"visual_review_wrong_subject", "legacy_v1_visual_review"}:
            visual_review = json.loads(
                paths["visual_review"].read_text(encoding="utf-8")
            )
            if case == "visual_review_wrong_subject":
                visual_review["staged_manifest_sha256"] = "0" * 64
            else:
                visual_review["schema"] = (
                    "propertyquarry.magicfit_private_visual_review.v1"
                )
                for key in (
                    "base_manifest_sha256",
                    "staged_manifest_sha256",
                    "delivery_digest",
                ):
                    visual_review.pop(key)
            paths["visual_review"].write_text(
                json.dumps(visual_review), encoding="utf-8"
            )
            evidence = json.loads(paths["evidence"].read_text(encoding="utf-8"))
            evidence["artifacts"]["visual_review_sha256"] = _sha256(
                paths["visual_review"]
            )
            paths["evidence"].write_text(json.dumps(evidence), encoding="utf-8")
        else:
            browser = json.loads(paths["browser_receipt"].read_text(encoding="utf-8"))
            browser["schema"] = "propertyquarry.magicfit_browser_playback.v1"
            for key in (
                "base_manifest_sha256",
                "staged_manifest_sha256",
                "delivery_digest",
            ):
                browser.pop(key)
            paths["browser_receipt"].write_text(json.dumps(browser), encoding="utf-8")
            evidence = json.loads(paths["evidence"].read_text(encoding="utf-8"))
            evidence["schema"] = "propertyquarry.magicfit_e2e_evidence.v1"
            for key in (
                "base_manifest_sha256",
                "staged_manifest_sha256",
                "delivery_digest",
            ):
                evidence.pop(key)
            evidence["artifacts"]["browser_receipt_sha256"] = _sha256(
                paths["browser_receipt"]
            )
            paths["evidence"].write_text(json.dumps(evidence), encoding="utf-8")

        rejected = _accept(paths)

        assert rejected.returncode != 0, case
        assert paths["pending"].read_bytes() == pending_before
        assert paths["manifest"].read_bytes() == manifest_before
        assert not paths["final_video"].exists()
        assert not paths["accepted_sidecar"].exists()
        verifier = build_property_tour_control_receipt(tour_root=paths["tour_root"])
        assert verifier["provider_counts"]["magicfit"] == 0


def test_magicfit_acceptance_rejects_mistyped_numeric_receipt_fields(
    tmp_path: Path,
) -> None:
    for case in (
        "boolean_browser_timing",
        "string_browser_timing",
        "boolean_evidence_duration",
        "string_evidence_size",
    ):
        paths = _materialize_pending(tmp_path / case)
        manifest_before = paths["manifest"].read_bytes()
        if case in {"boolean_browser_timing", "string_browser_timing"}:
            browser = json.loads(
                paths["browser_receipt"].read_text(encoding="utf-8")
            )
            replacement: object = (
                True
                if case == "boolean_browser_timing"
                else str(browser["duration_seconds"])
            )
            browser["duration_seconds"] = replacement
            browser["final_current_time"] = replacement
            paths["browser_receipt"].write_text(
                json.dumps(browser), encoding="utf-8"
            )
            evidence = json.loads(paths["evidence"].read_text(encoding="utf-8"))
            evidence["artifacts"]["browser_receipt_sha256"] = _sha256(
                paths["browser_receipt"]
            )
            paths["evidence"].write_text(json.dumps(evidence), encoding="utf-8")
        else:
            evidence = json.loads(paths["evidence"].read_text(encoding="utf-8"))
            if case == "boolean_evidence_duration":
                evidence["video"]["duration_seconds"] = True
            else:
                evidence["video"]["size_bytes"] = str(
                    evidence["video"]["size_bytes"]
                )
            paths["evidence"].write_text(json.dumps(evidence), encoding="utf-8")

        rejected = _accept(paths)

        assert rejected.returncode != 0, case
        assert paths["manifest"].read_bytes() == manifest_before
        assert paths["pending"].is_file()
        assert not paths["final_video"].exists()
        assert not paths["accepted_sidecar"].exists()
        receipt = build_property_tour_control_receipt(tour_root=paths["tour_root"])
        assert receipt["provider_counts"]["magicfit"] == 0


def test_magicfit_acceptance_rejects_prior_receipts_after_staged_manifest_changes(
    tmp_path: Path,
) -> None:
    for mutation in ("unrelated_field", "whitespace_only"):
        paths = _materialize_pending(tmp_path / mutation)
        pending_before = json.loads(paths["pending"].read_text(encoding="utf-8"))
        manifest_before = paths["manifest"].read_bytes()
        if mutation == "unrelated_field":
            staged_manifest = json.loads(
                paths["staged_manifest"].read_text(encoding="utf-8")
            )
            staged_manifest["display_title"] = "Changed after the prior review"
            paths["staged_manifest"].write_text(
                json.dumps(staged_manifest), encoding="utf-8"
            )
        else:
            paths["staged_manifest"].write_bytes(
                paths["staged_manifest"].read_bytes() + b"\n"
            )
        changed_staged_sha256 = _sha256(paths["staged_manifest"])
        assert changed_staged_sha256 != pending_before["staged_manifest_sha256"]

        # Even a completely refreshed receipt chain cannot authorize bytes
        # outside the importer-owned transform or alternate JSON whitespace.
        changed_pending = dict(pending_before)
        changed_pending["staged_manifest_sha256"] = changed_staged_sha256
        paths["pending"].write_text(json.dumps(changed_pending), encoding="utf-8")
        browser = json.loads(paths["browser_receipt"].read_text(encoding="utf-8"))
        browser["staged_manifest_sha256"] = changed_staged_sha256
        paths["browser_receipt"].write_text(json.dumps(browser), encoding="utf-8")
        visual = json.loads(paths["visual_review"].read_text(encoding="utf-8"))
        visual["staged_manifest_sha256"] = changed_staged_sha256
        paths["visual_review"].write_text(json.dumps(visual), encoding="utf-8")
        evidence = json.loads(paths["evidence"].read_text(encoding="utf-8"))
        evidence["staged_manifest_sha256"] = changed_staged_sha256
        evidence["artifacts"]["browser_receipt_sha256"] = _sha256(
            paths["browser_receipt"]
        )
        evidence["artifacts"]["visual_review_sha256"] = _sha256(
            paths["visual_review"]
        )
        paths["evidence"].write_text(json.dumps(evidence), encoding="utf-8")

        rejected = _accept(paths)

        assert rejected.returncode != 0
        assert "magicfit_acceptance_manifest_transform_invalid" in rejected.stderr
        assert paths["manifest"].read_bytes() == manifest_before
        assert not paths["final_video"].exists()
        assert not paths["accepted_sidecar"].exists()
        assert json.loads(paths["pending"].read_text(encoding="utf-8")) == changed_pending


def test_magicfit_activation_crash_boundaries_preserve_old_accepted_bundle(
    tmp_path: Path,
) -> None:
    for failpoint in ("after_final_video", "after_sidecar", "after_manifest"):
        case_root = tmp_path / failpoint
        first = _materialize_pending(case_root, color="black")
        assert _accept(first).returncode == 0

        old_manifest_bytes = first["manifest"].read_bytes()
        old_manifest = json.loads(old_manifest_bytes)
        old_video = first["bundle"] / old_manifest["video_relpath"]
        old_sidecar = first["bundle"] / old_manifest["video_sidecar_relpath"]
        old_video_bytes = old_video.read_bytes()
        old_sidecar_bytes = old_sidecar.read_bytes()

        replacement = _materialize_pending(case_root, color="blue")
        assert replacement["manifest"].read_bytes() == old_manifest_bytes
        assert old_video.read_bytes() == old_video_bytes
        assert old_sidecar.read_bytes() == old_sidecar_bytes
        before = build_property_tour_control_receipt(
            tour_root=replacement["tour_root"]
        )
        assert before["provider_counts"]["magicfit"] == 1

        interrupted = _accept(replacement, failpoint=failpoint)
        assert interrupted.returncode != 0
        assert f"magicfit_activation_test_failpoint:{failpoint}" in interrupted.stderr
        assert old_video.read_bytes() == old_video_bytes
        assert old_sidecar.read_bytes() == old_sidecar_bytes
        assert replacement["pending"].is_file()

        interrupted_manifest = json.loads(
            replacement["manifest"].read_text(encoding="utf-8")
        )
        if failpoint == "after_manifest":
            assert interrupted_manifest["video_relpath"] == str(
                replacement["final_video"].relative_to(replacement["bundle"])
            )
        else:
            assert replacement["manifest"].read_bytes() == old_manifest_bytes
            assert interrupted_manifest["video_relpath"] == old_manifest["video_relpath"]

        during = build_property_tour_control_receipt(
            tour_root=replacement["tour_root"]
        )
        assert during["provider_counts"]["magicfit"] == 1

        recovered = _accept(replacement)
        assert recovered.returncode == 0, recovered.stderr
        assert not replacement["pending"].exists()
        assert not replacement["staged_manifest"].parent.exists()
        active = json.loads(replacement["manifest"].read_text(encoding="utf-8"))
        assert active["video_relpath"] == json.loads(recovered.stdout)["video_relpath"]
        assert active["video_relpath"] != old_manifest["video_relpath"]
        assert old_video.read_bytes() == old_video_bytes
        assert old_sidecar.read_bytes() == old_sidecar_bytes
        after = build_property_tour_control_receipt(
            tour_root=replacement["tour_root"]
        )
        assert after["provider_counts"]["magicfit"] == 1


@pytest.mark.parametrize(
    "failpoint",
    ("after_pending_unlink", "after_stage_cleanup"),
)
def test_magicfit_activation_postcommit_crash_retry_is_idempotent(
    tmp_path: Path,
    failpoint: str,
) -> None:
    paths = _materialize_pending(tmp_path / failpoint)

    interrupted = _accept(paths, failpoint=failpoint)

    assert interrupted.returncode != 0
    assert f"magicfit_activation_test_failpoint:{failpoint}" in interrupted.stderr
    assert not paths["pending"].exists()
    assert paths["manifest"].is_file()
    assert paths["final_video"].is_file()
    assert paths["accepted_sidecar"].is_file()
    during = build_property_tour_control_receipt(tour_root=paths["tour_root"])
    assert during["provider_counts"]["magicfit"] == 1

    recovered = _accept(paths)

    assert recovered.returncode == 0, recovered.stderr
    recovery_receipt = json.loads(recovered.stdout)
    assert recovery_receipt["status"] == "delivery_accepted"
    assert recovery_receipt["idempotent_recovery"] is True
    assert recovery_receipt["video_relpath"] == json.loads(
        paths["manifest"].read_text(encoding="utf-8")
    )["video_relpath"]
    assert not paths["staged_manifest"].parent.exists()


def test_magicfit_acceptance_second_invocation_is_idempotent(tmp_path: Path) -> None:
    paths = _materialize_pending(tmp_path)
    first = _accept(paths)
    assert first.returncode == 0, first.stderr
    accepted_before = paths["accepted_sidecar"].read_bytes()
    manifest_before = paths["manifest"].read_bytes()
    video_before = paths["final_video"].read_bytes()

    second = _accept(paths)

    assert second.returncode == 0, second.stderr
    receipt = json.loads(second.stdout)
    assert receipt["status"] == "delivery_accepted"
    assert receipt["idempotent_recovery"] is True
    assert paths["accepted_sidecar"].read_bytes() == accepted_before
    assert paths["manifest"].read_bytes() == manifest_before
    assert paths["final_video"].read_bytes() == video_before


def test_magicfit_activation_dirfd_cannot_be_redirected_by_bundle_swap(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    moved_bundle = tmp_path / "bundle-held"
    outside = tmp_path / "outside"
    outside.mkdir()

    with _activation_lock(bundle) as bundle_fd:
        bundle.rename(moved_bundle)
        bundle.symlink_to(outside.name, target_is_directory=True)
        _write_bundle_bytes_atomic(
            bundle_fd,
            "descriptor-bound.json",
            b"held-directory\n",
            mode=0o600,
        )

    assert (moved_bundle / "descriptor-bound.json").read_bytes() == b"held-directory\n"
    assert not (outside / "descriptor-bound.json").exists()


def test_magicfit_acceptance_rejects_symlink_component_in_staged_subject(
    tmp_path: Path,
) -> None:
    paths = _materialize_pending(tmp_path)
    manifest_before = paths["manifest"].read_bytes()
    staged_directory = paths["staged_manifest"].parent
    real_directory = staged_directory.with_name(f"{staged_directory.name}-real")
    staged_directory.replace(real_directory)
    staged_directory.symlink_to(real_directory.name, target_is_directory=True)

    rejected = _accept(paths)

    assert rejected.returncode != 0
    assert "magicfit_acceptance_staged_manifest_invalid" in rejected.stderr
    assert paths["manifest"].read_bytes() == manifest_before
    assert paths["pending"].is_file()


def test_magicfit_acceptance_rejects_symlinked_external_source_receipt(
    tmp_path: Path,
) -> None:
    paths = _materialize_pending(tmp_path)
    manifest_before = paths["manifest"].read_bytes()
    real_source = paths["source_receipt"].with_name("source-receipt-real.json")
    paths["source_receipt"].replace(real_source)
    paths["source_receipt"].symlink_to(real_source.name)

    rejected = _accept(paths)

    assert rejected.returncode != 0
    assert "magicfit_acceptance_source_receipt_invalid" in rejected.stderr
    assert paths["manifest"].read_bytes() == manifest_before
    assert paths["pending"].is_file()


def test_magicfit_acceptance_rejects_signature_only_contact_sheet(
    tmp_path: Path,
) -> None:
    paths = _materialize_pending(tmp_path)
    paths["contact_sheet"].write_bytes(b"\x89PNG\r\n\x1a\nheader-only")

    rejected = _accept(paths)

    assert rejected.returncode != 0
    assert "magicfit_acceptance_contact_sheet_invalid" in rejected.stderr
    assert paths["manifest"].is_file()
    assert paths["pending"].is_file()
    assert not paths["final_video"].exists()


def test_magicfit_public_runtime_requires_exact_v4_for_walkthrough_and_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _materialize_pending(tmp_path)
    assert _accept(paths).returncode == 0
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(paths["tour_root"]))
    payload = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    video_relpath = str(payload["video_relpath"])
    public_eligibility.clear_magicfit_public_eligibility_cache()

    acceptance = public_tours._public_tour_walkthrough_acceptance(payload)
    assert acceptance["allowed"] is True
    assert acceptance["status"] == "accepted_v4"
    assert acceptance["verified_video_relpath"] == video_relpath
    assert public_tours.public_tour_walkthrough(SLUG).status_code == 200
    assert public_tours.public_tour_file(  # type: ignore[arg-type]
        SLUG, video_relpath, None
    ).status_code == 200

    original_sidecar = paths["accepted_sidecar"].read_bytes()
    legacy = json.loads(original_sidecar)
    legacy["contract_name"] = "propertyquarry.magicfit_delivery_acceptance.v2"
    for invalid_body in (
        b"{}",
        b'"accepted"',
        json.dumps(legacy).encode("utf-8"),
    ):
        paths["accepted_sidecar"].write_bytes(invalid_body)
        rejected = public_tours._public_tour_walkthrough_acceptance(payload)
        assert rejected["allowed"] is False
        with pytest.raises(public_tours.HTTPException) as error:
            public_tours.public_tour_walkthrough(SLUG)
        assert error.value.status_code == 404
        response = public_tours.public_tour_file(  # type: ignore[arg-type]
            SLUG, video_relpath, None
        )
        assert response.status_code == 410
    paths["accepted_sidecar"].write_bytes(original_sidecar)
    paths["accepted_sidecar"].chmod(0o600)


def test_magicfit_public_eligibility_cache_restats_and_identity_binds_negative_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _materialize_pending(tmp_path)
    assert _accept(paths).returncode == 0
    payload = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    sidecar = json.loads(paths["accepted_sidecar"].read_text(encoding="utf-8"))
    visual_entry = sidecar["audit"]["artifacts"]["visual_review"]
    visual_path = paths["bundle"] / str(visual_entry["relpath"])
    original_visual = visual_path.read_bytes()

    validation_calls = 0
    stat_calls = 0
    original_validate = public_eligibility._validate_magicfit_uncached
    original_stat = public_eligibility.stat_regular_file_identity

    def counted_validate(*args: object, **kwargs: object):
        nonlocal validation_calls
        validation_calls += 1
        return original_validate(*args, **kwargs)

    def counted_stat(*args: object, **kwargs: object):
        nonlocal stat_calls
        stat_calls += 1
        return original_stat(*args, **kwargs)

    monkeypatch.setattr(
        public_eligibility, "_validate_magicfit_uncached", counted_validate
    )
    monkeypatch.setattr(
        public_eligibility, "stat_regular_file_identity", counted_stat
    )
    public_eligibility.clear_magicfit_public_eligibility_cache()

    first = public_eligibility.evaluate_magicfit_public_eligibility(
        paths["bundle"], payload
    )
    first_stat_calls = stat_calls
    second = public_eligibility.evaluate_magicfit_public_eligibility(
        paths["bundle"], payload
    )
    assert first.eligible is True and first.cache_hit is False
    assert second.eligible is True and second.cache_hit is True
    assert validation_calls == 1
    assert stat_calls - first_stat_calls == 10

    visual_path.write_bytes(original_visual + b"\n")
    assert public_eligibility.evaluate_magicfit_public_eligibility(
        paths["bundle"], payload
    ).eligible is False
    assert public_eligibility.evaluate_magicfit_public_eligibility(
        paths["bundle"], payload
    ).eligible is False
    assert validation_calls == 2

    visual_path.write_bytes(original_visual)
    visual_path.chmod(0o600)
    assert public_eligibility.evaluate_magicfit_public_eligibility(
        paths["bundle"], payload
    ).eligible is True
    assert validation_calls == 3


def test_magicfit_public_eligibility_single_flights_positive_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _materialize_pending(tmp_path)
    assert _accept(paths).returncode == 0
    payload = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    original_validate = public_eligibility._validate_magicfit_uncached
    calls = 0
    calls_lock = threading.Lock()
    start = threading.Barrier(8)

    def slow_validate(*args: object, **kwargs: object):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.1)
        return original_validate(*args, **kwargs)

    def evaluate() -> bool:
        start.wait(timeout=3)
        return public_eligibility.evaluate_magicfit_public_eligibility(
            paths["bundle"], payload
        ).eligible

    monkeypatch.setattr(
        public_eligibility, "_validate_magicfit_uncached", slow_validate
    )
    public_eligibility.clear_magicfit_public_eligibility_cache()
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _index: evaluate(), range(8)))

    assert results == [True] * 8
    assert calls == 1


def test_magicfit_public_eligibility_single_flights_and_caches_negative_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _materialize_pending(tmp_path)
    assert _accept(paths).returncode == 0
    payload = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    sidecar = json.loads(paths["accepted_sidecar"].read_text(encoding="utf-8"))
    visual_entry = sidecar["audit"]["artifacts"]["visual_review"]
    visual_path = paths["bundle"] / str(visual_entry["relpath"])
    visual_path.write_bytes(visual_path.read_bytes() + b"\n")
    original_validate = public_eligibility._validate_magicfit_uncached
    calls = 0
    calls_lock = threading.Lock()
    start = threading.Barrier(8)

    def slow_validate(*args: object, **kwargs: object):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.1)
        return original_validate(*args, **kwargs)

    def evaluate() -> public_eligibility.MagicFitPublicEligibility:
        start.wait(timeout=3)
        return public_eligibility.evaluate_magicfit_public_eligibility(
            paths["bundle"], payload
        )

    monkeypatch.setattr(
        public_eligibility, "_validate_magicfit_uncached", slow_validate
    )
    public_eligibility.clear_magicfit_public_eligibility_cache()
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _index: evaluate(), range(8)))

    assert all(result.declared and not result.eligible for result in results)
    assert sum(result.cache_hit for result in results) == 7
    assert calls == 1


def test_magicfit_public_eligibility_rejects_symlink_and_audit_corruption(
    tmp_path: Path,
) -> None:
    for subject in ("accepted_sidecar", "audit"):
        paths = _materialize_pending(tmp_path / subject)
        assert _accept(paths).returncode == 0
        payload = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        if subject == "accepted_sidecar":
            subject_path = paths["accepted_sidecar"]
        else:
            sidecar = json.loads(
                paths["accepted_sidecar"].read_text(encoding="utf-8")
            )
            audit_entry = sidecar["audit"]["artifacts"]["browser_receipt"]
            subject_path = paths["bundle"] / str(audit_entry["relpath"])
        real_path = subject_path.with_name(f"{subject_path.name}.real")
        subject_path.replace(real_path)
        subject_path.symlink_to(real_path.name)
        public_eligibility.clear_magicfit_public_eligibility_cache()

        result = public_eligibility.evaluate_magicfit_public_eligibility(
            paths["bundle"], payload
        )

        assert result.declared is True
        assert result.eligible is False


def test_magicfit_public_eligibility_decodes_contact_sheet_after_rebinding(
    tmp_path: Path,
) -> None:
    paths = _materialize_pending(tmp_path)
    assert _accept(paths).returncode == 0
    payload = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    sidecar = json.loads(paths["accepted_sidecar"].read_text(encoding="utf-8"))
    artifacts = sidecar["audit"]["artifacts"]

    malformed_contact = b"\x89PNG\r\n\x1a\nheader-only"
    contact_entry = artifacts["contact_sheet"]
    contact_path = paths["bundle"] / str(contact_entry["relpath"])
    contact_path.write_bytes(malformed_contact)
    contact_path.chmod(0o600)
    contact_entry["sha256"] = hashlib.sha256(malformed_contact).hexdigest()
    contact_entry["size_bytes"] = len(malformed_contact)

    evidence_entry = artifacts["evidence_receipt"]
    evidence_path = paths["bundle"] / str(evidence_entry["relpath"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["artifacts"]["contact_sheet_sha256"] = contact_entry["sha256"]
    evidence_body = (
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    evidence_path.write_bytes(evidence_body)
    evidence_path.chmod(0o600)
    evidence_entry["sha256"] = hashlib.sha256(evidence_body).hexdigest()
    evidence_entry["size_bytes"] = len(evidence_body)
    sidecar["review"]["evidence_sha256"] = evidence_entry["sha256"]
    paths["accepted_sidecar"].write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    paths["accepted_sidecar"].chmod(0o600)
    public_eligibility.clear_magicfit_public_eligibility_cache()

    result = public_eligibility.evaluate_magicfit_public_eligibility(
        paths["bundle"], payload
    )

    assert result.declared is True
    assert result.eligible is False


def test_magicfit_acceptance_serializes_on_canonical_publication_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _materialize_pending(tmp_path)
    manifest_before = paths["manifest"].read_bytes()
    pending_before = paths["pending"].read_bytes()
    monkeypatch.setenv(
        "PROPERTYQUARRY_RECONSTRUCTION_PUBLICATION_LOCK_TIMEOUT_SECONDS",
        "0.05",
    )

    with property_tour_publication_lock(
        public_dir=paths["tour_root"],
        slug=SLUG,
        timeout_seconds=1.0,
    ):
        blocked = _accept(paths)

    assert blocked.returncode != 0
    assert "property_reconstruction_publication_lock_timeout" in blocked.stderr
    assert paths["manifest"].read_bytes() == manifest_before
    assert paths["pending"].read_bytes() == pending_before
    assert not paths["final_video"].exists()

    accepted = _accept(paths)
    assert accepted.returncode == 0, accepted.stderr


def test_magicfit_acceptance_rejects_named_bundle_swap_before_commit(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "public_tours" / SLUG
    bundle_dir.mkdir(parents=True)
    moved_bundle = bundle_dir.with_name(f"{SLUG}.moved")

    with _activation_lock(bundle_dir) as bundle_fd:
        bundle_dir.rename(moved_bundle)
        bundle_dir.symlink_to(moved_bundle.name, target_is_directory=True)

        with pytest.raises(
            SystemExit,
            match="magicfit_acceptance_bundle_changed",
        ):
            _confirm_named_bundle_identity(bundle_dir, bundle_fd)
