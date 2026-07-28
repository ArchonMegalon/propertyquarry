from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_product_release_checklist_is_propertyquarry_scoped() -> None:
    checklist = (ROOT / "PRODUCT_RELEASE_CHECKLIST.md").read_text(encoding="utf-8")

    assert checklist.startswith("# PropertyQuarry Product Release Checklist\n")
    for required in (
        "search-to-decision loop",
        "Shortlist, compare, feedback, preference learning, and revisit",
        "Chromium, Firefox, and WebKit",
        "installed v2 controller's flagship security admission",
        "disabled legacy",
        "`propertyquarry-flagship-security` workflow job",
        "migration history, not",
        "release evidence; a skipped or missing controller security phase",
        "verified rollback path",
    ):
        assert required in checklist

    for stale_office_requirement in (
        "real executive-office work system",
        "`/app/today`",
        "`/app/briefing`",
        "`/app/inbox`",
        "`/app/follow-ups`",
        "`/app/people/{id}`",
    ):
        assert stale_office_requirement not in checklist


def test_release_checklist_requires_the_propertyquarry_product_loop() -> None:
    checklist = (ROOT / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8")
    normalized_checklist = " ".join(checklist.split())
    reconciliation = (
        ROOT / "docs" / "PROPERTYQUARRY_CACHED_UPSTREAM_RECONCILIATION.md"
    ).read_text(encoding="utf-8")
    normalized_reconciliation = " ".join(reconciliation.split())

    assert "`PRODUCT_RELEASE_CHECKLIST.md` is fully satisfied" in checklist
    assert "brief -> search dispatch -> ranked results -> property dossier" in checklist
    assert "memo -> queue -> draft/approval -> follow-up" not in checklist
    assert "`.codex-design/repo/IMPLEMENTATION_SCOPE.md`" in checklist
    assert (
        "Cached remote-tracking refs are planning evidence only"
        in normalized_checklist
    )
    assert (
        "uv pip compile --python-version 3.12 --generate-hashes"
        in normalized_checklist
    )
    assert "Do not hand-edit the hash lock" in normalized_checklist
    assert (
        "controlled release-environment recovery lane"
        in normalized_checklist
    )
    assert "produced no authoritative replacement lock" in normalized_checklist
    assert "performed no download" in normalized_checklist
    assert "explicit operator approval" in normalized_checklist
    assert (
        "./scripts/propertyquarry_release_python.sh "
        "scripts/propertyquarry_release_make_dispatch.py release-preflight"
        in normalized_checklist
    )
    assert (
        "authenticated `release-preflight` target leaves the "
        "already-materialized weekly pulse, browser proof, and flagship "
        "receipt byte-for-byte unchanged"
        in normalized_checklist
    )
    assert (
        "disposable canonical checkout" in normalized_checklist
        and "immediately verifies an exact `HEAD` match" in normalized_checklist
    )
    assert (
        "reproduction evidence only, not publication or release authority"
        in normalized_checklist
    )
    assert (
        "Source-tree read-only authenticated CI gate bundle"
        in normalized_checklist
        and "leaves all three canonical receipt bytes unchanged"
        in normalized_checklist
        and "routes nested exit-gate evidence through a cleaned private"
        in normalized_checklist
    )
    assert "`make release-preflight`" not in checklist
    assert "PROPERTYQUARRY_CACHED_UPSTREAM_RECONCILIATION.md" in checklist
    for required in (
        "not current-upstream or release authority",
        "Dependency-lock recovery boundary",
        "jsonschema[format-nongpl]==4.26.0",
        "--offline",
        "annotated-doc==0.0.4",
        "local registry cache is incomplete",
        "produced no authoritative replacement lock",
        "performed no download",
        "explicit operator approval",
        "Blocking migration-lineage decision",
        "propertyquarry_schema_migrations",
        "02fe41df",
        "2f20534f",
        "Inspect every live ledger first",
        "If no target has the local v15 checksum",
        "If any target has the local v15 checksum",
        "Never renumber or rewrite an already-applied migration",
        "performs_release_effects",
        "phase_b_oidc_linux.go",
        "Do not add cached-upstream",
        "imports `net/http`",
        "performs discovery/JWKS fetches",
        "offline native-authority contract",
        "outside the authoritative native source root",
        "tools/source-files.txt",
        "built_image_smoke",
        "v1 receipt",
        "runtime binding rejects that extra key",
        "Migrate producer and consumer atomically",
        "exactly validated v2 schema",
        "builder-output-to-runtime-binding compatibility test",
        "intended standalone repository identity",
        "Resolve repository, workflow, image, manifest, and provenance identity atomically",
        "Mixed `property` and `propertyquarry` identities must fail closed",
        "Never merge generated browser proof",
        "Direct `ci-gates`, authenticated `ci-gates`, and release preflight never refresh canonical evidence",
        "disposable canonical checkout",
        "current-runtime reproduction only",
        "not publication or release authority",
        "older commit is historical evidence",
        "Verified residual release blockers",
        "Pinned Go 1.26.5",
        "exact 29-file native source closure",
        "reports exactly eight blockers",
        "weekly pulse's flagship SHA-256 and size do not match",
        "do not silently repair it",
        "Local browser availability is not canonical browser proof",
        "next authorized critical-path sequence",
    ):
        assert required in normalized_reconciliation

    generated_section = normalized_reconciliation.split(
        "## Generated evidence", maxsplit=1
    )[1].split("## Verification order", maxsplit=1)[0]
    generated_order = (
        "Commit the clean source candidate",
        "Materialize the browser workflow proof",
        "Materialize the flagship release gate",
        "Materialize the weekly product pulse",
        "Recompute the release manifest and metadata envelope",
        "Prove detached exact reproduction and semantic cleanliness",
    )
    generated_positions = [generated_section.index(item) for item in generated_order]
    assert generated_positions == sorted(generated_positions)

    verification_section = normalized_reconciliation.split(
        "## Verification order", maxsplit=1
    )[1]
    verification_order = (
        "Check duplicate Python definitions",
        "Run schema, admission, controller",
        "Require candidate-builder-to-runtime-binding receipt compatibility",
        "Run the exact-manifest native no-network policy",
        "Run no-network candidate image build/smoke",
        "Regenerate evidence in the order above",
        "Run generated-artifact, release-asset",
    )
    verification_positions = [
        verification_section.index(item) for item in verification_order
    ]
    assert verification_positions == sorted(verification_positions)
