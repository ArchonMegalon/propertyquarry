#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from property_magicfit_env import load_magicfit_env

OUT_DIR = Path(os.environ.get("PROPERTYQUARRY_MAGICFIT_COMPLETION_DIR") or "/docker/property/_completion/magicfit_provider")
OUT_PATH = OUT_DIR / "MAGICFIT_PROVIDER_VERIFICATION.generated.json"
DEFAULT_MAGICFIT_EMAIL = "magicfit-account@example.test"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def account_hash(email: str) -> str:
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:16]


def main() -> int:
    load_magicfit_env()
    out_email = (
        os.environ.get("PROPERTYQUARRY_MAGICFIT_EMAIL")
        or os.environ.get("MAGICFIT_EMAIL")
        or DEFAULT_MAGICFIT_EMAIL
    ).strip() or DEFAULT_MAGICFIT_EMAIL
    out_tier = os.environ.get("PROPERTYQUARRY_MAGICFIT_TIER", "5").strip() or "5"
    password_present = bool((os.environ.get("PROPERTYQUARRY_MAGICFIT_PASSWORD") or os.environ.get("MAGICFIT_PASSWORD") or "").strip())
    payload = {
        "generated_at": utc_now(),
        "status": "pilot",
        "service": "MagicFit",
        "account": {
            "account_user": out_email,
            "account_user_hash": account_hash(out_email),
            "license_tier": f"License Tier {out_tier}",
            "user_reported": True,
            "local_credential_present": password_present,
            "login_verified": False,
            "billing_or_license_status": "owned_user_reported_not_browser_verified",
        },
        "verification_checklist": {
            "account_user": {"verified": True, "value": out_email, "source": "user_reported_local_config"},
            "license_tier": {"verified": True, "value": f"License Tier {out_tier}", "source": "user_reported_local_config"},
            "credits_per_month": {"verified": True, "value": 6000, "source": "appsumo_founder_qna"},
            "text_to_video": {"verified": True, "source": "appsumo_public"},
            "image_to_video": {"verified": True, "source": "appsumo_public"},
            "mp4_download": {"verified": True, "source": "appsumo_public"},
            "hd_video_audio": {"verified": True, "source": "appsumo_public"},
            "watermark_status": {"verified": False, "value": "not_proven", "source": "public_sources_incomplete"},
            "commercial_use_rights": {"verified": False, "value": "unclear", "source": "public_sources_incomplete"},
            "model_availability": {"verified": True, "value": ["kling_2_6_turbo_pro", "google_veo_3_1", "seedance_2"], "source": "appsumo_public"},
            "max_duration_resolution": {"verified": False, "value": {"max_duration": None, "max_resolution": "1080p"}, "source": "public_sources_incomplete"},
            "deletion_privacy_terms": {"verified": False, "value": "not_reviewed", "source": "policy_review_missing"},
        },
        "capabilities": {
            "text_to_video": {"verified": True, "source": "appsumo_public"},
            "image_to_video": {"verified": True, "source": "appsumo_public"},
            "download_mp4": {"verified": True, "source": "appsumo_public"},
            "hd_video_audio": {"verified": True, "source": "appsumo_public"},
            "prompt_mode": {"verified": True, "source": "appsumo_public"},
            "image_upload_mode": {"verified": True, "source": "appsumo_public"},
            "human_reference_faces": {"verified": True, "source": "appsumo_founder_update"},
            "commercial_use_rights": {"verified": False, "status": "unclear_from_public_sources"},
            "watermark_free_export": {"verified": False, "status": "not_proven"},
            "provider_branding_required": {"verified": False, "status": "not_proven"},
            "monthly_credit_refresh": {"verified": True, "source": "appsumo_founder_qna"},
            "credits_current_offer_bonus": {"verified": True, "value": 6000, "source": "appsumo_founder_qna"},
            "credit_costs_per_model": {"verified": True, "source": "appsumo_founder_qna"},
            "max_resolution": {"verified": True, "value": "1080p", "source": "appsumo_founder_update"},
            "max_duration": {"verified": False, "status": "not_proven_publicly"},
            "allowed_aspect_ratios": {"verified": False, "status": "not_proven_publicly"},
            "deletion_privacy_terms": {"verified": False, "status": "not_reviewed"},
            "api_access_in_ltd": {"verified": True, "value": False, "source": "appsumo_founder_qna"},
        },
        "models": {
            "kling_2_6_turbo_pro": {"verified_available": True},
            "google_veo_3_1": {"verified_available": True},
            "seedance_2": {"verified_available": True, "verified_1080p": True, "verified_human_reference_faces": True},
        },
        "privacy": {
            "no_private_campaign_data": "enforced_by_policy_only",
            "no_sourcebook_text": "enforced_by_policy_only",
            "no_real_user_likeness": "enforced_by_policy_only",
            "deletion_or_project_cleanup_checked": False,
        },
        "sources": [
            {
                "label": "AppSumo product page",
                "url": "https://appsumo.com/products/magicfit/",
                "facts": [
                    "License Tier 5 exists",
                    "text-to-video and image-to-video are advertised",
                    "HD video with audio download is advertised",
                    "Kling 2.6 Turbo Pro and Google Veo 3.1 are listed",
                ],
            },
            {
                "label": "AppSumo founder Q&A on credits and API",
                "url": "https://appsumo.com/products/magicfit/questions/what-is-a-credit-and-do-you-have-api-1493602/",
                "facts": [
                    "API access exists",
                    "API is not included in the AppSumo deal",
                    "image/video generations consume credits",
                ],
            },
            {
                "label": "AppSumo founder Q&A on LTD sustainability",
                "url": "https://appsumo.com/products/magicfit/questions/ltd-sustainability-are-credits-guarante-1493403/",
                "facts": [
                    "monthly credit refresh is claimed stable",
                ],
            },
            {
                "label": "AppSumo founder Q&A on Tier 5 offer",
                "url": "https://appsumo.com/products/magicfit/questions/can-i-upgrade-my-subscription-with-the-1-1505466/",
                "facts": [
                    "Tier 5 current offer mentions 6,000 free credits",
                ],
            },
            {
                "label": "PushOwl Help Center image-to-video docs",
                "url": "https://docs.pushowl.com/en/articles/11087393-how-to-create-image-to-video-using-magicfit",
                "facts": [
                    "image-to-video workflow is documented publicly",
                ],
            },
        ],
        "blocking_reasons": [
            "Commercial-use rights are not explicitly proven from public sources.",
            "Watermark-free export is not proven from public sources.",
            "No browser-authenticated account verification receipt exists for the user account.",
            "No repeatable render-job receipt exists yet for Chummer sample clips.",
        ],
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
