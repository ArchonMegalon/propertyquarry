from __future__ import annotations

import pytest

from scripts.check_property_tour_delivery_contract import _check_ready_contract


@pytest.mark.parametrize(
    "ready_count",
    ("not-an-integer", True, 1.0, None, 0, -1),
)
def test_ready_contract_requires_real_positive_integer_count(
    ready_count: object,
) -> None:
    failures: list[str] = []

    _check_ready_contract(
        "3dvista",
        {
            "status": "ready",
            "blocked_reason": "",
            "required_to_send": [],
            "ready_payload": {
                "provider": "3dvista",
                "ready_count": ready_count,
                "sample_controls": [
                    {
                        "control_path": "/tours/example/control/3dvista",
                        "evidence": "verified_fixture",
                    }
                ],
            },
        },
        failures,
    )

    assert failures == [
        "3dvista ready_payload must prove at least one ready control"
    ]


def test_ready_contract_accepts_positive_integer_count() -> None:
    failures: list[str] = []

    _check_ready_contract(
        "3dvista",
        {
            "status": "ready",
            "blocked_reason": "",
            "required_to_send": [],
            "ready_payload": {
                "provider": "3dvista",
                "ready_count": 1,
                "sample_controls": [
                    {
                        "control_path": "/tours/example/control/3dvista",
                        "evidence": "verified_fixture",
                    }
                ],
            },
        },
        failures,
    )

    assert failures == []
