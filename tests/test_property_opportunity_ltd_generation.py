from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image

from app.domain.models import ToolInvocationResult
from app.product.property_opportunity_ltd_generation import (
    execute_property_opportunity_concept_cover,
    materialize_property_opportunity_concept_cover_asset,
    property_opportunity_concept_cover_asset_descriptor,
    property_opportunity_concept_cover_public_projection,
)


class _ToolExecution:
    def __init__(
        self,
        *,
        principal_id: str = "principal-a",
        asset_url: str = "https://assets.example.test/concept-cover.png",
    ) -> None:
        self.principal_id = principal_id
        self.asset_url = asset_url
        self.request = None

    def execute_invocation(self, request):  # type: ignore[no-untyped-def]
        self.request = request
        return ToolInvocationResult(
            tool_name="provider.onemin.image_generate",
            action_kind="image_generate",
            target_ref="onemin-generation-1",
            output_json={
                "provider_backend": "1min",
                "model": "gpt-image-1-mini",
                "asset_urls": [self.asset_url],
                "provider_private_response": "must-not-escape",
            },
            receipt_json={
                "handler_key": "provider.onemin.image_generate",
                "invocation_contract": "tool.v1",
                "principal_id": self.principal_id,
                "provider_key": "onemin",
                "provider_backend": "1min",
                "feature_type": "IMAGE_GENERATOR",
                "model": "gpt-image-1-mini",
                "account_id": "private-account",
                "slot_id": "private-slot",
            },
        )


def _opportunity() -> dict[str, object]:
    return {
        "opportunity_id": "property_opportunity:abc123",
        "fit_score": 72,
        "recommendation": "shortlist",
        "title": "Private listing title",
        "address": "Private Street 12, Vienna",
        "customer_email": "customer@example.test",
    }


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 8), color=(180, 108, 76)).save(output, format="PNG")
    return output.getvalue()


def _asset_fetcher(_url: str) -> tuple[bytes, str]:
    return _png_bytes(), "application/octet-stream"


def test_concept_cover_uses_principal_bound_provider_call_and_sanitizes_result(
    tmp_path: Path,
) -> None:
    execution = _ToolExecution()

    result = execute_property_opportunity_concept_cover(
        tool_execution=execution,
        principal_id="principal-a",
        job_id="job-a",
        opportunity=_opportunity(),
        artifact_root=tmp_path,
        asset_fetcher=_asset_fetcher,
    )

    assert execution.request is not None
    assert execution.request.tool_name == "provider.onemin.image_generate"
    assert execution.request.action_kind == "image_generate"
    assert execution.request.context_json == {
        "principal_id": "principal-a",
        "suppress_telegram_delivery": True,
    }
    prompt = str(execution.request.payload_json["prompt"])
    assert "strong fit" in prompt
    assert "recommendation: shortlist" in prompt
    assert "Private listing title" not in prompt
    assert "Private Street 12" not in prompt
    assert "customer@example.test" not in prompt
    assert "property_opportunity:abc123" not in prompt

    assert result["status"] == "ready"
    artifact = dict(result["artifact"])
    assert artifact["kind"] == "private_concept_cover"
    assert artifact["asset_url"] == (
        "/app/api/property/opportunities/generations/job-a/asset"
    )
    assert artifact["label"] == "AI concept cover"
    assert artifact["disclaimer"] == (
        "Synthetic mood illustration · not listing photography"
    )
    assert artifact["media_type"] == "image/png"
    assert artifact["byte_length"] == len(_png_bytes())
    assert artifact["width"] == 8
    assert artifact["height"] == 8
    assert result["receipt"]["status"] == "verified"  # type: ignore[index]
    assert result["receipt"]["principal_bound"] is True  # type: ignore[index]
    assert result["receipt"]["proof_scope"] == "provider_call"  # type: ignore[index]
    assert result["receipt"]["asset_materialized"] is True  # type: ignore[index]
    serialized = repr(result)
    assert "https://assets.example.test" not in serialized
    assert "private-account" not in serialized
    assert "private-slot" not in serialized
    assert "must-not-escape" not in serialized


def test_concept_cover_rejects_receipt_for_a_different_principal() -> None:
    execution = _ToolExecution(principal_id="principal-b")

    try:
        execute_property_opportunity_concept_cover(
            tool_execution=execution,
            principal_id="principal-a",
            job_id="job-a",
            opportunity=_opportunity(),
        )
    except RuntimeError as exc:
        assert str(exc) == "property_opportunity_ltd_generation_receipt_unverified"
    else:
        raise AssertionError("a cross-principal provider receipt must fail closed")


def test_concept_cover_rejects_private_provider_asset_url() -> None:
    execution = _ToolExecution(asset_url="https://127.0.0.1/private.png")

    try:
        execute_property_opportunity_concept_cover(
            tool_execution=execution,
            principal_id="principal-a",
            job_id="job-a",
            opportunity=_opportunity(),
        )
    except RuntimeError as exc:
        assert str(exc) == "property_opportunity_ltd_generation_receipt_unverified"
    else:
        raise AssertionError("a private provider asset URL must fail closed")


def test_persisted_concept_cover_is_revalidated_and_allowlisted_for_browser(
    tmp_path: Path,
) -> None:
    result = execute_property_opportunity_concept_cover(
        tool_execution=_ToolExecution(),
        principal_id="principal-a",
        job_id="job-a",
        opportunity=_opportunity(),
        artifact_root=tmp_path,
        asset_fetcher=_asset_fetcher,
    )
    result["raw_provider_response"] = "private-response"
    result["receipt"]["account_id"] = "private-account"  # type: ignore[index]
    result["artifact"]["label"] = "provider-controlled-copy"  # type: ignore[index]

    projection = property_opportunity_concept_cover_public_projection(
        result,
        generation_id="job-a",
        opportunity_id="property_opportunity:abc123",
    )

    assert projection["artifact"]["label"] == "AI concept cover"  # type: ignore[index]
    serialized = repr(projection)
    assert "private-response" not in serialized
    assert "private-account" not in serialized
    assert "provider-controlled-copy" not in serialized
    descriptor = property_opportunity_concept_cover_asset_descriptor(
        result,
        generation_id="job-a",
        opportunity_id="property_opportunity:abc123",
        artifact_root=tmp_path,
    )
    assert Path(str(descriptor["path"])).read_bytes() == _png_bytes()
    assert descriptor["sha256"] == projection["artifact"]["sha256"]  # type: ignore[index]
    assert property_opportunity_concept_cover_public_projection(
        result,
        generation_id="another-job",
        opportunity_id="property_opportunity:abc123",
    ) == {}


def test_legacy_provider_url_is_hidden_and_materialized_on_asset_read(
    tmp_path: Path,
) -> None:
    result = execute_property_opportunity_concept_cover(
        tool_execution=_ToolExecution(),
        principal_id="principal-a",
        job_id="job-a",
        opportunity=_opportunity(),
        artifact_root=tmp_path / "initial",
        asset_fetcher=_asset_fetcher,
    )
    result["artifact"] = {
        "kind": "private_concept_cover",
        "asset_url": "https://assets.example.test/legacy-presigned.png?secret=hidden",
    }
    receipt = dict(result["receipt"])
    for key in ("asset_materialized", "asset_sha256", "asset_byte_length"):
        receipt.pop(key, None)
    result["receipt"] = receipt

    projection = property_opportunity_concept_cover_public_projection(
        result,
        generation_id="job-a",
        opportunity_id="property_opportunity:abc123",
    )

    assert projection["artifact"]["asset_url"] == (  # type: ignore[index]
        "/app/api/property/opportunities/generations/job-a/asset"
    )
    assert "legacy-presigned" not in repr(projection)
    assert projection["receipt"]["asset_materialized"] is False  # type: ignore[index]
    descriptor = property_opportunity_concept_cover_asset_descriptor(
        result,
        generation_id="job-a",
        opportunity_id="property_opportunity:abc123",
        artifact_root=tmp_path / "legacy",
        asset_fetcher=_asset_fetcher,
    )
    assert descriptor["media_type"] == "image/png"


def test_materialization_rejects_non_image_and_oversized_provider_bytes(
    tmp_path: Path,
) -> None:
    for payload, expected_error in (
        (b"not-an-image", "property_opportunity_ltd_asset_image_invalid"),
        (
            b"x" * ((8 * 1024 * 1024) + 1),
            "property_opportunity_ltd_asset_size_invalid",
        ),
    ):
        try:
            materialize_property_opportunity_concept_cover_asset(
                provider_asset_url="https://assets.example.test/provider-output",
                generation_id=f"job-{expected_error}",
                artifact_root=tmp_path,
                asset_fetcher=lambda _url, value=payload: (
                    value,
                    "application/octet-stream",
                ),
            )
        except RuntimeError as exc:
            assert str(exc) == expected_error
        else:
            raise AssertionError("invalid provider image bytes must fail closed")
