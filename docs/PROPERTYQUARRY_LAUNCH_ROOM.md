# PropertyQuarry launch room

This is the one-page operator entrypoint for current release truth:

```text
python3 scripts/propertyquarry_launch_room.py
python3 scripts/propertyquarry_launch_room.py \
  --format json \
  --write _completion/propertyquarry_launch_room/current.json
```

The view reads the shared repository-role policy, canonical marked release
manifest, exact candidate-bound browser receipt, and local Git state. It reports
the canonical and legacy roles, envelope/runtime identities, separate source /
journey / browser counts, Core and Advanced Visual posture, current protected
Actions evidence, live deployment, public edge, and the next operator action.

It is deliberately fail-closed:

- `ArchonMegalon/propertyquarry` is the only canonical release authority.
- `ArchonMegalon/property` is a noncanonical verifier, not an exact mirror or a
  second candidate.
- checked-in source/browser receipts are candidate proof, not hosted CI or
  deployment proof;
- absence of exact current protected Actions, deployment, rollback, DR, or edge
  receipts is shown as missing, never inferred from historical observations;
- Core Gold can be candidate-eligible while production remains blocked; and
- Advanced Visual Gold remains
  `unavailable_unbound_producer_receipts` until its additive evidence is bound.

The report always sets `production_launch_ready` to false unless a future,
separately reviewed version consumes and validates the complete protected/live
receipt set. The current tool makes no network-freshness claim.
