#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "ea" if (ROOT / "ea" / "app").is_dir() else ROOT
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.product.property_search_tour_binding import (  # noqa: E402
    PropertySearchTourBindingError,
)
from app.product.property_tour_ai_panorama_intake import (  # noqa: E402
    AiPanoramaIntakeError,
    load_private_ai_panorama_install_request,
)
from app.product.property_tour_hosting import (  # noqa: E402
    _property_public_tour_base_url,
)
from app.product.service import (  # noqa: E402
    bind_property_search_candidate_generated_reconstruction,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run or apply one exact, listing-anchored generated-tour binding through "
            "PropertyQuarry application storage."
        )
    )
    parser.add_argument(
        "--request-file",
        default="",
        help=(
            "Optional private 0600 AI-panorama install request. Identity fields "
            "are read without following symlinks and must agree with any CLI/env values."
        ),
    )
    parser.add_argument(
        "--principal-id",
        default=os.getenv("PROPERTYQUARRY_TOUR_BINDING_PRINCIPAL_ID") or "",
        help=(
            "Exact tenant principal. Prefer PROPERTYQUARRY_TOUR_BINDING_PRINCIPAL_ID "
            "to keep it out of shell history."
        ),
    )
    parser.add_argument("--run-id", default="", help="Required unless supplied by --request-file.")
    parser.add_argument(
        "--candidate-ref",
        default="",
        help="Required unless supplied by --request-file.",
    )
    parser.add_argument(
        "--listing-id",
        default="",
        help="Required unless external_id is supplied by --request-file.",
    )
    parser.add_argument(
        "--tour-url",
        default="",
        help=(
            "Required without --request-file. With --request-file it may only confirm "
            "the first-party URL derived from expected_slug."
        ),
    )
    parser.add_argument(
        "--reconstruction-kind",
        choices=("ai_panorama_360", "layout_preview"),
        default="ai_panorama_360",
    )
    parser.add_argument("--disclosure", default="")
    parser.add_argument(
        "--expected-record-sha256",
        default="",
        help="Required with --apply; copy before_sha256 from a fresh dry-run receipt.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the binding. Without this flag the command is read-only.",
    )
    return parser


def _request_string(request: dict[str, object], key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PropertySearchTourBindingError(
            f"property_search_tour_request_{key}_required"
        )
    return value.strip()


def _agreed_request_identity(
    *,
    explicit_value: object,
    request: dict[str, object],
    request_key: str,
) -> str:
    explicit = str(explicit_value or "").strip()
    requested = _request_string(request, request_key)
    if explicit and explicit != requested:
        raise PropertySearchTourBindingError(
            "property_search_tour_request_identity_mismatch"
        )
    return requested


def _request_generated_reconstruction_url(
    *,
    request: dict[str, object],
    explicit_tour_url: object,
) -> str:
    slug = _request_string(request, "expected_slug")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,159}", slug):
        raise PropertySearchTourBindingError(
            "property_search_tour_request_expected_slug_invalid"
        )
    base = urllib.parse.urlsplit(_property_public_tour_base_url())
    try:
        port = base.port
    except ValueError as exc:
        raise PropertySearchTourBindingError(
            "property_search_tour_request_public_base_invalid"
        ) from exc
    if (
        base.scheme.lower() != "https"
        or not base.hostname
        or base.username
        or base.password
        or base.query
        or base.fragment
        or base.path.rstrip("/") != "/tours"
        or port is not None and not 1 <= port <= 65535
    ):
        raise PropertySearchTourBindingError(
            "property_search_tour_request_public_base_invalid"
        )
    canonical_url = urllib.parse.urlunsplit(
        (
            base.scheme.lower(),
            base.netloc,
            f"/tours/{slug}/control",
            "",
            "",
        )
    )
    explicit = str(explicit_tour_url or "").strip()
    if explicit and explicit not in {
        canonical_url,
        canonical_url.removesuffix("/control"),
    }:
        raise PropertySearchTourBindingError(
            "property_search_tour_request_tour_url_mismatch"
        )
    return canonical_url


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        principal_id = str(args.principal_id or "").strip()
        run_id = str(args.run_id or "").strip()
        candidate_ref = str(args.candidate_ref or "").strip()
        listing_id = str(args.listing_id or "").strip()
        tour_url = str(args.tour_url or "").strip()
        if str(args.request_file or "").strip():
            request = load_private_ai_panorama_install_request(
                Path(str(args.request_file).strip())
            )
            if args.reconstruction_kind != "ai_panorama_360":
                raise PropertySearchTourBindingError(
                    "property_search_tour_request_reconstruction_kind_mismatch"
                )
            principal_id = _agreed_request_identity(
                explicit_value=principal_id,
                request=request,
                request_key="principal_id",
            )
            run_id = _agreed_request_identity(
                explicit_value=run_id,
                request=request,
                request_key="search_run_id",
            )
            candidate_ref = _agreed_request_identity(
                explicit_value=candidate_ref,
                request=request,
                request_key="candidate_ref",
            )
            listing_id = _agreed_request_identity(
                explicit_value=listing_id,
                request=request,
                request_key="external_id",
            )
            tour_url = _request_generated_reconstruction_url(
                request=request,
                explicit_tour_url=tour_url,
            )
        for value, code in (
            (principal_id, "property_search_tour_principal_required"),
            (run_id, "property_search_tour_run_id_required"),
            (candidate_ref, "property_search_tour_candidate_ref_required"),
            (listing_id, "property_search_tour_listing_id_required"),
            (tour_url, "property_search_tour_url_required"),
        ):
            if not value:
                raise PropertySearchTourBindingError(code)
        receipt = bind_property_search_candidate_generated_reconstruction(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
            expected_listing_id=listing_id,
            generated_reconstruction_url=tour_url,
            expected_record_sha256=args.expected_record_sha256,
            reconstruction_kind=args.reconstruction_kind,
            disclosure=args.disclosure,
            apply=bool(args.apply),
        )
    except (AiPanoramaIntakeError, PropertySearchTourBindingError) as exc:
        print(f"error:{exc.code}", file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(receipt, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
