from __future__ import annotations

from app.domain.models import ToolInvocationResult
from app.product.property_opportunity_ltd_generation import (
    execute_property_opportunity_concept_cover,
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


def test_concept_cover_uses_principal_bound_provider_call_and_sanitizes_result() -> None:
    execution = _ToolExecution()

    result = execute_property_opportunity_concept_cover(
        tool_execution=execution,
        principal_id="principal-a",
        job_id="job-a",
        opportunity=_opportunity(),
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
    assert result["artifact"] == {
        "kind": "private_concept_cover",
        "asset_url": "https://assets.example.test/concept-cover.png",
        "label": "AI concept cover",
        "disclaimer": "Synthetic mood illustration · not listing photography",
    }
    assert result["receipt"]["status"] == "verified"  # type: ignore[index]
    assert result["receipt"]["principal_bound"] is True  # type: ignore[index]
    assert result["receipt"]["proof_scope"] == "provider_call"  # type: ignore[index]
    serialized = repr(result)
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


def test_persisted_concept_cover_is_revalidated_and_allowlisted_for_browser() -> None:
    result = execute_property_opportunity_concept_cover(
        tool_execution=_ToolExecution(),
        principal_id="principal-a",
        job_id="job-a",
        opportunity=_opportunity(),
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
    assert property_opportunity_concept_cover_public_projection(
        result,
        generation_id="another-job",
        opportunity_id="property_opportunity:abc123",
    ) == {}
