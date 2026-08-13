from __future__ import annotations

from pathlib import Path


WORKBENCH_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "ea/app/templates/app/_property_workbench_script.html"
)


def test_local_packet_actions_do_not_claim_external_sharing() -> None:
    source = WORKBENCH_SCRIPT.read_text(encoding="utf-8")

    assert "Save review packet" in source
    assert "Save shortlist packet" in source
    assert "Local review shortlist from PropertyQuarry" in source
    assert 'data-rybbit-event="pq.packet.saved"' in source
    assert "Share this home" not in source
    assert ">Share results</button>" not in source
    assert "pq.packet.shared" not in source
    assert "Shareable shortlist from PropertyQuarry" not in source
