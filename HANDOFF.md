# PropertyQuarry handoff — 2026-08-04

## Active goal

Finish PropertyQuarry as a premium, production-ready property evaluation and presentation system. Keep evaluation evidence-bound and auditable; keep optional AI/media providers additive and governed; bound database and deployment-image growth; preserve licensed 3DVista/Matterport assets and receipts; keep Karl-Czerny-Gasse faithful to the original floorplan; validate topology, rendering, accessibility, security, and live behavior; approve Karl only after evidence-backed checks pass; then commit, push, deploy, refresh proof, and deliver verified links over Telegram. The long-running product goal is complete. Continue to preserve the evidence gates and release controls below during maintenance.

## Mandatory operating rules

- Working directory: `/docker/property`.
- Read `/docker/property/AGENTS.md`. For every new coding/debugging task, call vexp `run_pipeline` first. Do not use grep/glob/Bash/cat to explore the codebase; use vexp and `get_skeleton`.
- Relevant skills for the current lane: `ea-governed-render-lane` and `browser-act`. Read their `SKILL.md` files before continuing browser/provider work.
- Do not read `.env`, print credentials, or expose connector metadata. Crezlo credentials exist in the governed binding described below.
- Do not rerun Karl through Crezlo: a real provider tour already exists. Continue by inspecting/editing that exact tour.
- Crezlo output is optional provider evidence. It must not replace authoritative Karl scoring, topology, or the licensed 3DVista tour without the full acceptance receipt.
- Do not delete the Crezlo tour. If acceptance remains blocked, prefer making it private/draft through a reversible provider action after resolving the exact control.

## Repository and production baseline

- Branch: `integration/property-origin-main-20260728`.
- The pre-maintenance envelope and remote baseline were `03609f23c482f21a6a079ffd9ef36752c20fb711`; use `git rev-parse HEAD` and `git rev-parse '@{upstream}'` for the final post-maintenance envelope instead of copying an older handoff hash.
- Deployed source before the image-retention maintenance release: `b4ff5c084a14fa265962b3f9a7fb6b64bdc77d82`.
- Web image: `sha256:e854a2db5af7fdc37e5b37c4483c065a27c08bb9d613b8bfeea9e750d2b997ff`.
- Render image: `sha256:8daf78acd92c89bc49a703b9d685066b1330ee7521714010b6027089e10bf873`.
- The exact legacy shortlist alias, canonical Karl candidate, normal-camera walkthrough, and licensed 3DVista control were production-verified before this maintenance patch. Focused retention/deployment/diorama/media-link gate: 8/8 passed.
- Database: 437 MiB / 458,431,511 bytes. Retention is live and a direct production run found zero eligible rows. The largest relation is `observation_events` at 156,516,352 bytes; current evidence does not indicate runaway PropertyQuarry DB growth.

## Authoritative Karl state

- Public candidate alias: `cece2dad814fdf68`. The curated diorama manifest resolves it to scoped candidate `ad48357be22535c1`, source listing `1536069684` in 1020 Wien.
- Property URL: `https://propertyquarry.com/app/shortlist?candidate=cece2dad814fdf68`.
- Authoritative public walkthrough: `https://propertyquarry.com/tours/karl-czerny-gasse-2-urban-jungle/walkthrough`.
- Authoritative licensed 3DVista tour: `https://propertyquarry.com/tours/3dvista/karl-czerny-gasse-2-urban-jungle/3dvista/index.htm`.
- Runtime bundle in the API container: `/data/public_property_tours/karl-czerny-gasse-2-urban-jungle`.
- Original floorplan: `floorplan.webp`. It includes the `VERTRAGSGRUNDLAGE` stamp; the stamp is not a room. Preserve this scan as legal/source evidence.
- Customer-facing clean derivative: `state/release/assets/karl-czerny-gasse-floorplan-clean.imagegen.png`, 1470×1070 PNG, SHA-256 `a3dfae54b15108ae357c5bafa6dcc43fba24cf25cdd35cc26bee63734815280b`. It removes only the stamp and is used by both 3DVista and Crezlo.
- The apartment entrance is in `vorraum` / VR. The stairwell is outside the entrance.
- Correct adjacency: balcony/loggia ↔ living-kitchen; terrace ↔ primary bedroom.
- Forbidden edges: stairwell → balcony/loggia, vorraum → balcony/loggia, living-kitchen → terrace, balcony/loggia → terrace.
- Seven panorama sources, in intended order: `vorraum`, `living-kitchen`, `bedroom-primary`, `terrace`, `bedroom-guest`, `wc`, `bath`.
- The public raw-panorama route intentionally rejects these AI reconstructions. Do not weaken that privacy/acceptance guard.
- Telegram delivery of the authoritative walkthrough and 3DVista links happened in message 5037. Message 5060 incorrectly requested a new export after a host-side discovery run read stale paths; message 5061 corrected it and confirmed that no upload is needed. Correction receipt: `_completion/notifications/propertyquarry-karl-tour-correction-20260804.json`.
- Do not assign the coordinates of `Karl-Czerny Gasse 2, 1200 Wien` to this listing. That address is the saved commute destination, not proven listing identity. The source listing only establishes the 1020 postal area, so exact-distance enrichment must remain evidence-blocked until the listing provides an exact pin/address.

### 3DVista clean-floorplan correction — live and verified

- The five stamped floor-map pyramid files were backed up under `state/release/backups/karl-3dvista-stamped-map-20260804/` and replaced with clean derivatives from `state/release/assets/karl-3dvista-clean-map/`.
- New governed 3DVista export-tree SHA-256: `1542185d9e45b498a5586b05d7f88a44569f127d4e1bbd92551a349c6f7b188b`.
- `tour.private.json` was atomically updated with the new export provenance and a `customer_floorplan_derivative` receipt while retaining `original_source_preserved=true`.
- Public control and vendor routes return 200; the floorplan overlay opens, the stamp is absent, and existing scene pins remain positioned. Proof: `state/release/proof/karl-3dvista-clean-floorplan-open-20260804.png`, SHA-256 `22c984bd03d12ce83b9a349165d48c2c76b3dde5ed691be4f1ffd014def348cd`.

## Crezlo run completed in this session

This is a real provider-side tour. Do not create another one.

- Binding ID: `b4e88d66-a8b0-4a41-9b25-e387da17a4b6`.
- Principal hash: `0cc1bc07a959a085`.
- Connector: `browseract`; external account: `crezlo-auto`; enabled scopes include `browseract` and `crezlo`.
- Provider workspace: `EA Property Tours`.
- Workspace ID: `019d0cff-3282-70a9-9c5a-20dfdce7f3fe`.
- Workspace host: `ea-property-tours-20260320.crezlotours.com`.
- Crezlo tour ID: `019fcbba-4128-7028-9bca-dff49e63fefa`.
- Title: `Karl-Czerny-Gasse 2 - Urban Jungle - topology faithful`.
- Slug: `karl-czerny-gasse-2-urban-jungle-topology-faithful`.
- Editor URL: `https://ea-property-tours-20260320.crezlotours.com/admin/tours/019fcbba-4128-7028-9bca-dff49e63fefa`.
- Anonymous embed: `https://ea-property-tours-20260320.crezlotours.com/embed/019fcbba-4128-7028-9bca-dff49e63fefa`.
- Crezlo initially returned status `published` and created 8 panorama scenes: the stamped floorplan was incorrectly one of them. This was corrected in place on the existing tour; no duplicate was created.
- Creation timestamp: `2026-08-04T07:43:34.869580+00:00`.

Source hashes used for the run:

- `floorplan.webp`: `76d53b6c90e53262438ecbc58474b46a683af69d105d70349b8b9f176f74be4e`
- `panoramas/vorraum.jpg`: `cdc7ed7b90816dd795b3fe108fef3aa3ea9dd7eaab537606da8fdb8907dae618`
- `panoramas/living-kitchen.jpg`: `a02da826e07b8da81c076671f9d216a85cf5515f1524d9b2cfab30c0cc62a93c`
- `panoramas/bedroom-primary.jpg`: `0863427314d03d314b3bf389abf89cc5a31f7c0c2fd46e1fbd8dc016240c1166`
- `panoramas/terrace.jpg`: `03a02fda8dad59af830738ebaec24faee7a0566f1aa025de5bd1d8dacc164b6a`
- `panoramas/bedroom-guest.jpg`: `492a895a7562b2c49399ac522e4d0e6721fb29204fb0f2792afd06c06fdca0fc`
- `panoramas/wc.jpg`: `c663cdc7eadbe99e35a9b52c56c45ce4cdc94dc550fa0976bef73d9c4ae330bc`
- `panoramas/bath.jpg`: `14524b288542dd4f4ec736b10a84f978ebbaf1aea2bb3cb48a7b3811af36a6dd`

### Crezlo clean-floorplan correction — complete at provider, not promoted

- The exact governed binding in the PropertyQuarry production database was used. Do not substitute the generic `EA_CREZLO_LOGIN_*` account: it maps to Crezlo user `019e9728-…`, while Karl is owned by binding user `019cfc61-…` and workspace `019d0cff-…`. No credential value was printed or persisted.
- Added a real floor map named `Karl-Czerny-Gasse 2 · Grundriss` using the clean derivative. Floor-map ID: `floormap-1785833556485`; provider file ID: `019fcbf9-9cc9-7243-9a5c-87f185a0c182`. Upload and tour-update responses were HTTP 200.
- Deleted only stamped panorama scene `939acbdc-faf6-4a21-8493-f02142ae04b4` after confirming the clean floor map. Provider DELETE returned HTTP 200. The stamped original remains recoverable from the preserved local source.
- Final provider state: 7 panorama scenes + 1 clean floor map; stamped scene absent. Anonymous buyer API, clean media asset, and embed all return 200. The public Floor Plan control renders the clean image at 1470×1070 with no bad HTTP responses or console errors.
- Public proof: `state/release/proof/karl-crezlo-public-clean-floorplan-open-20260804.png`, SHA-256 `e3247601c453ecf6812479c9daab8a9140d4538157817ae1a05f90f0fab11036`.
- Redacted receipt: `state/release/propertyquarry-karl-crezlo-clean-floorplan.receipt.json`.

## Crezlo worker fix committed

`scripts/crezlo_property_tour_worker.py` had two live-provider defects:

1. Workspace discovery called the obsolete `https://tours.crezlo.com/api/seller/tours/workspaces`, which now returns 404. It now calls `https://api.caliqik.com/api/seller/tours/workspaces`.
2. Crezlo returns the workspace `internal_fqdn` as the bare label `ea-property-tours-20260320`. The worker now normalizes a bare label to `<label>.crezlotours.com` and strips schemes/paths safely.

Checks already passed:

- `python3 -m py_compile scripts/crezlo_property_tour_worker.py`
- Focused regression: `tests/test_tool_execution.py::test_crezlo_worker_uses_live_workspace_api_and_normalizes_bare_fqdn`.
- Crezlo worker test selection: 2 passed.
- `git diff --check`.

## Final local regression gate

- Fixed the mobile AI-panorama hotspot hover transform so a top-edge hotspot no longer moves 2 px outside the declared safe area.
- Updated stale browser selectors to target the normal-camera walkthrough, the explicit `3D tour` link, the disclosure copy, and the single current visible floorplan lightbox. The shortlist diorama thumbnails remain rendered and interactive through their existing atlas controls.
- Fixed the target-recovery timeout: exact-location searches now fetch full detail previews through the bounded parallel candidate-preview lane, use a three-worker floor when configured concurrency allows it, and use the bounded preview wrapper for follow-up source research. This keeps provider concurrency plan-aware while preventing one source with a moved deep target from serializing every candidate detail request.
- New regression file: `tests/test_property_search_preview_prefetch.py` (3 passed).
- Full flagship browser file: 6 passed.
- Affected greenfield browser tests: 2 passed.
- Live Tibor target-recovery canary against the current Willhaben target: 1 passed in 303.37 s; its individual search run retained the unchanged 180 s bounded timeout.
- Syntax compilation and `git diff --check` passed. The pre-existing `public_tours.py` invalid-escape SyntaxWarning remains non-fatal and unrelated to this patch.

## Durable Playwright worker fixed

- The repository now tracks `docker/propertyquarry-playwright/Dockerfile`, `package.json`, and its exact lockfile.
- Browser base and npm package are both pinned to current stable Playwright `1.62.1`; the base is additionally digest-pinned to `sha256:dcc5531e97840b9b5e794f2814476b21571c5124a3fca2267d73041f56e7580e`.
- `scripts/build_propertyquarry_playwright_image.sh` reproducibly builds `propertyquarry-playwright:local`, asserts the installed version, runs a high-severity npm audit, and launches real headless Chromium.
- Local rebuilt image ID: `sha256:2b4f2ac34a624fcf253efec7582b3a38c4835e4203b1dc7fe500c283d76e241c`.
- Audit result: zero vulnerabilities. Browser smoke: pass.

## Runtime-aware tour discovery fixed

- Host-side discovery now resolves both the incoming bind mount and the live public-tour named volume from the running PropertyQuarry container. Named public volumes are inspected through a bounded temporary snapshot that is always cleaned up.
- Runtime marker diagnostics now report verified 3DVista/Pano2VR entries instead of claiming markers are missing unconditionally.
- Exact live artifacts are treated as already imported. Older reviewed 3DVista drops are treated as superseded when a newer live correction exists, so the clean Karl runtime cannot be overwritten by the older stamped drop.
- Live dry run resolves Karl's older drop as `superseded_by_newer_live_bundle`; it does not emit a Karl import or repair request.

## 1minAI / Google Maps OODA runtime truth

- Production has 1minAI evaluation enabled, Google Maps fact OODA enabled, a live 1minAI key, and a principal-scoped BrowserAct binding/API key. A separate run URL is optional because the binding exists.
- The remaining Karl distance blocker is source evidence, not provider configuration: the listing has no exact property pin/address. Preserve `exact_listing_coordinates_required`; do not fabricate coordinates from the commute destination.

## Current Crezlo acceptance result

Do not promote/import this provider output yet.

What passed after the correction:

- Anonymous embed returns HTTP 200.
- Desktop viewer renders one canvas.
- Dragging changes the rendered view.
- Previous/next scene controls exist and the next-scene action changes the view.
- Mobile viewport returns HTTP 200, renders one canvas, and exposes the next-scene control.

What failed or remains unverified:

- Provider API now reports 7 panorama scenes and one real clean floor-map layer. The stamped scene is absent.
- No hotspot/navigation graph or provider spatial metadata/cubemaps are present. The floor map has zero markers.
- No evidence yet proves the required topology, exact-property provenance, floorplan alignment/geometry receipt, connected scene graph, or first-party provider-control route.
- A live call to `BrowserActToolAdapter._crezlo_immersive_acceptance` returned `accepted=false`, first reason `spatial_scenes_missing`; `spatial_scene_count=0`, `hotspot_count=0`, `scene_graph_connected=false`. Keep promotion fail-closed.
- The licensed 3DVista tour remains authoritative.

The exact live seller requests were captured without persisting authorization values. Tour detail is `GET /api/seller/tours/<tour-id>?product_type=tours&workspace_id=<workspace-id>`; the clean floor map used `POST /api/seller/tours/files` followed by `PUT /api/seller/tours/<tour-id>`, and the stamped scene removal used `DELETE /api/seller/tours/<tour-id>/scenes/<scene-id>`. All three mutations returned HTTP 200. Never print authorization values.

## Deployment-image retention closure

- `scripts/propertyquarry_image_retention.py` now plans safely by default and applies only with both expected live image IDs. It considers only exact `local-<12 hex>` tags in the standalone PropertyQuarry web/render repositories, protects every image referenced by any container, protects the expected live images, and retains one additional distinct rollback image per runtime.
- The governed deployment runs the retention tool only after the local deployment receipt passes, and writes `state/release/propertyquarry-image-retention.v1.json`. Tests cover repository scoping, live/rollback/container protection, race-time identity checks, and deployment ordering.
- Initial bounded cleanup removed 2.198 GB of unused build cache, two dangling image records, and 32 stale PropertyQuarry local tags. Docker image storage fell from 56.81 GB to 54.86 GB; filesystem free space rose from roughly 43 GB to 48 GB. These items are recoverable by rebuilding from source.
- No database, Docker volume, tour asset, provider artifact, container, or evidence-tagged image was deleted. Do not run host-wide `docker image prune -a` or delete unused-looking volumes from this repository; the remaining 94% host utilization includes other projects and recoverable data outside PropertyQuarry's safe cleanup authority.

## Remaining external constraints

1. There are no known in-repository Karl release blockers after the final maintenance deployment and live verification pass.
2. Do not recreate or re-edit the Crezlo tour unless a new evidence-backed correction is required. It is provider-corrected but intentionally not imported/promoted because acceptance remains fail-closed.
3. Keep exact-distance OODA blocked until the source listing yields an exact pin/address. Provider configuration is complete; the Karl-Czerny address is only a commute target and must not be reused as property identity.
4. MagicFit remains an optional Advanced Visual provider lane without a verified artifact. Core Gold remains valid through the licensed 3DVista tour; do not weaken provider acceptance to manufacture an Advanced Visual pass.
5. Host filesystem utilization still rounds to 94%. Further cleanup requires a separate, cross-project retention audit or explicit authority over old volumes/rollback images; do not infer that authority from PropertyQuarry maintenance.

## Historical artifacts warning

Three older artifacts matched the word `Karl`, but their actual outputs are unrelated AB 1.8 / Naschmarkt tours. Do not treat them as Karl evidence and do not delete them without an explicit request.
