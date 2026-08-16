# Project Chummer

Project Chummer is a multi-repo modernization of the legacy Chummer 5 application into the explainable Shadowrun campaign OS: a deterministic rules engine, an authored workbench, a campaign and living-dossier spine, a play/mobile session shell, and the hosted/support/publication layers needed to keep long campaigns coherent.

The entry wedge is still character build and explain.
The retention engine is campaign continuity.
The product wins when Shadowrun math, session state, and recovery all stay understandable under pressure.
The next additive proof is not "bigger platform" but small living-campaign loops: the world remembers what the runners did, and talks back through receipts.

## Product entry

Start with `START_HERE.md` if you are new.
Use `GLOSSARY.md` when the repo-specific language gets dense.
Use `journeys/README.md` when the question is "what does the user actually do end to end?"
Use `GOLDEN_JOURNEY_RELEASE_GATES.yaml` when the question is "which journeys must every release wave prove?"
Use `PRODUCT_SPINE.yaml` when the question is "which core loop, surface, truth domain, Horizon lane, or provider-adapter rule owns this?"
Use `PRODUCT_SPINE_REDESIGN.md` when the question is "why is the product organized around build, run, remember, explain, and publish?"
Use `FINAL_GOLD_GRAPH.generated.json` when the question is "what is the current whole-product release posture?"
Use `JOURNEY_GATES.generated.json` when the question is "what lived journey truth is the fleet publishing right now?"
Use `CURRENT_HUMAN_APPROVALS.generated.md` when the question is "what still needs an actual person instead of more automation?"

Use `RULE_AUTHORITY_HUMAN_BOUNDARIES.generated.md` only for SR4/SR6 rule-authority review; it is not a whole-product approval ledger.

Use `history/` only for version-named, superseded closeout records. Historical prose never outranks a generated current decision.
Use `METRICS_AND_SLOS.yaml` when the question is "what counts as good enough to ship?"
Use `PRODUCT_HEALTH_SCORECARD.yaml` when the question is "how does whole-product reality steer the next decision?"
Use `WHOLE_PROJECT_MISSED_SCOPE_ACCEPTANCE.md` when the question is "what did we miss across the whole product, and what additional goal now blocks gold?"
Use `CAMPAIGN_OPERABILITY_SCORING_RUBRIC.yaml` when the question is "what score a supposedly clean release still has to earn before promotion claims widen?"

### Reading tracks

1. Public/product story:
   `VISION.md` -> `PUBLIC_LANDING_POLICY.md` -> `PUBLIC_NAVIGATION.yaml` -> `PUBLIC_LANDING_MANIFEST.yaml` -> `PUBLIC_FEATURE_REGISTRY.yaml` -> `PUBLIC_PROGRESS_PARTS.yaml` -> `PUBLIC_CAMPAIGN_IMAGE_MANIFEST.yaml` -> `PUBLIC_USER_MODEL.md` -> `PUBLIC_AUTH_FLOW.md` -> `PRODUCTLIFT_FEEDBACK_ROADMAP_BRIDGE.md` -> `KATTEB_PUBLIC_GUIDE_OPTIMIZATION_LANE.md` -> `PUBLIC_SITE_VISIBILITY_AND_SEARCH_OPTIMIZATION.md` -> `PUBLIC_SIGNAL_TO_CANON_PIPELINE.md` -> `PUBLIC_FEEDBACK_AND_CONTENT_REGISTRY.yaml` -> `PUBLIC_FEEDBACK_TAXONOMY.yaml` -> `COMPANION_PERSONA_AND_INTERACTION_MODEL.md` -> `COMPANION_PACKET.md` -> `COMPANION_TRIGGER_REGISTRY.yaml` -> `COMPANION_EVENT_SCHEMA.yaml` -> `PUBLIC_MEDIA_BRIEFS.yaml` -> `PUBLIC_VIDEO_BRIEFS.yaml` -> `MEDIA_ARTIFACT_RECIPE_REGISTRY.yaml`
2. Product middle and control loop:
   `PRODUCT_SPINE_REDESIGN.md` -> `PRODUCT_SPINE.yaml` -> `FINAL_GOLD_GRAPH.generated.json` -> `CONFIDENCE_READINESS_AND_CONTINUITY_GUIDE.md` -> `CONFIDENCE_READINESS_AND_CONTINUITY_REGISTRY.yaml` -> `LIVING_CAMPAIGN_LOOP_MATERIALIZATION_GUIDE.md` -> `LIVING_CAMPAIGN_LOOP_MATERIALIZATION_REGISTRY.yaml` -> `LOST_POTENTIAL_MATERIALIZATION_WAVE.md` -> `LOST_POTENTIAL_MATERIALIZATION_REGISTRY.yaml` -> `READY_FOR_TONIGHT_MODE.md` -> `READY_FOR_TONIGHT_GATES.yaml` -> `PUBLIC_ONBOARDING_PATHS_FOR_NO_DESKTOP_USERS.md` -> `ROLE_KITS_AND_STARTER_LOADOUTS.md` -> `ROLE_KIT_REGISTRY.yaml` -> `SOURCE_AWARE_EXPLAIN_PUBLIC_TRUST_HOOK.md` -> `EXPLAIN_EVERY_VALUE_AND_GROUNDED_FOLLOW_UP.md` -> `BUILD_GHOST_MVP_001.md` -> `CAMPAIGN_ADOPTION_START_FROM_TODAY_FLOW.md` -> `FOUNDRY_FIRST_VTT_HANDOFF_PROOF.md` -> `VTT_EXPORT_TARGET_ACCEPTANCE.yaml` -> `RUNNER_PASSPORT_AND_CROSS_COMMUNITY_TRUST.md` -> `RUNNER_PASSPORT_ACCEPTANCE.yaml` -> `LIVE_ACTION_ECONOMY_AND_TURN_ASSIST.md` -> `SOURCE_ANCHOR_AND_LOCAL_RULEBOOK_BINDING.md` -> `CAMPAIGN_ADOPTION_WIZARD.md` -> `GM_RUNBOARD_LIVE_OPERATIONS.md` -> `RUNNER_RESUME_AND_GOAL_PINS.md` -> `PREP_PACKET_FACTORY_AND_PROCEDURAL_TABLES.md` -> `BLACK_LEDGER_MVP_001.md` -> `WORLD_BROADCAST_AND_FACTION_PROPAGANDA_CADENCE.md` -> `WORLD_BROADCAST_RECIPE_REGISTRY.yaml` -> `WORLD_DISPATCH_AND_REACTIVATION_LOOP.md` -> `WORLD_DISPATCH_REACTIVATION_GATES.yaml` -> `CREW_AND_MISSION_FIT_MODEL.md` -> `SUPPORT_PACKET_AND_CALCULATION_REPORT_UX.md` -> `CAMPAIGN_SPINE_AND_CREW_MODEL.md` -> `CHARACTER_LIFECYCLE_AND_LIVING_DOSSIER.md` -> `ROAMING_WORKSPACE_AND_ENTITLEMENT_SYNC.md` -> `CAMPAIGN_WORKSPACE_AND_DEVICE_ROLES.md` -> `INTEROP_AND_PORTABILITY_MODEL.md` -> `RULE_ENVIRONMENT_AND_AMEND_SYSTEM.md` -> `USER_JOURNEYS.md` -> `GOLDEN_JOURNEY_RELEASE_GATES.yaml` -> `JOURNEY_GATES.generated.json` -> `PRODUCT_CONTROL_AND_GOVERNOR_LOOP.md` -> `SUPPORT_AND_SIGNAL_OODA_LOOP.md` -> `EXPERIENCE_SUCCESS_METRICS.md`
3. Repo and contract boundaries:
   `ARCHITECTURE.md` -> `PRODUCT_BACKBONE_WORKSPACE.md` -> `PRODUCT_BACKBONE_WORKSPACE.yaml` -> `OWNERSHIP_MATRIX.md` -> `LEAD_DESIGNER_OPERATING_MODEL.md` -> `PRODUCT_GOVERNOR_AND_AUTOPILOT_LOOP.md` -> `PROVIDER_AND_ROUTE_STEWARDSHIP.md` -> `CONTRACT_SETS.yaml` -> `projects/*.md`
4. Delivery and release control:
   Start with `RELEASE_READINESS_SCOPE_MODEL.md` to distinguish artifact delivery, whole-product preview, and stable authority.
   `RELEASE_PIPELINE.md` -> `REPO_HYGIENE_RELEASE_TRUST_AND_AUTOMATION_SAFETY.md` -> `REPO_HARDENING_CHECKLIST.yaml` -> `DESKTOP_CLIENT_PRODUCT_CUT.md` -> `DESKTOP_PLATFORM_POLICY.yaml` -> `DESKTOP_PLATFORM_ACCEPTANCE_MATRIX.yaml` -> `PRIMARY_ROUTE_POLICY.yaml` -> `PUBLIC_RELEASE_EXPERIENCE.yaml` -> `PUBLIC_DOWNLOADS_POLICY.md` -> `DESKTOP_AUTO_UPDATE_SYSTEM.md` -> `PUBLIC_AUTO_UPDATE_POLICY.md` -> `LOCALIZATION_AND_LANGUAGE_SYSTEM.md` -> `LOCALIZATION_PARITY_MATRIX.yaml` -> `ERROR_TAXONOMY_AND_ESCALATION_MATRIX.yaml` -> `KNOWN_ISSUE_AND_FIX_STATUS_LANGUAGE.md` -> `ACCESSIBILITY_AND_COPY_SAFETY_RELEASE_CHECKLIST.md` -> `ONBOARDING_AND_EMPTY_STATE_JOURNEY_CONTRACT.md` -> `LONG_RUNNING_ACTION_SAFETY_CONTRACT.md` -> `FLAGSHIP_RESPONSIVENESS_BUDGETS.yaml` -> `CAMPAIGN_OPERABILITY_SCORING_RUBRIC.yaml` -> `FLAGSHIP_RELEASE_POLICY.yaml` -> `RELEASE_SCOPE_DECISION.yaml` -> `PRODUCT_USAGE_TELEMETRY_MODEL.md` -> `PRODUCT_USAGE_TELEMETRY_EVENT_SCHEMA.md` -> `PRIVACY_AND_RETENTION_BOUNDARIES.md` -> `FEEDBACK_AND_CRASH_REPORTING_SYSTEM.md` -> `FEEDBACK_AND_SIGNAL_OODA_LOOP.md` -> `FEEDBACK_AND_CRASH_STATUS_MODEL.md` -> `ACCOUNT_AWARE_FRONT_DOOR_CLOSEOUT.md` -> `WHOLE_PROJECT_MISSED_SCOPE_ACCEPTANCE.md` -> `PROGRAM_MILESTONES.yaml` -> `FINAL_GOLD_GRAPH.generated.json` -> `PREVIEW_RELEASE_DECISION.generated.json` -> `CURRENT_RELEASE_DECISION.generated.json` -> `CURRENT_BLOCKERS.generated.md` -> `CURRENT_HUMAN_APPROVALS.generated.md`
5. Future lanes and public explainer posture:
   `HORIZONS.md` -> `HORIZON_REGISTRY.yaml` -> `HORIZON_PROMOTION_RULES.md` -> `HORIZON_SIGNAL_POLICY.md` -> `LTD_DISCOVERY_OUTREACH_AND_VALIDATION_INTEGRATION_GUIDE.md` -> `ICANPRENEUR_DISCOVERY_AND_VALIDATION_LANE.md` -> `PRODUCTLIFT_FEEDBACK_ROADMAP_BRIDGE.md` -> `KATTEB_PUBLIC_GUIDE_OPTIMIZATION_LANE.md` -> `PUBLIC_SITE_VISIBILITY_AND_SEARCH_OPTIMIZATION.md` -> `PUBLIC_SIGNAL_TO_CANON_PIPELINE.md` -> `KARMA_FORGE_DISCOVERY_AND_HOUSE_RULE_INTAKE.md` -> `HOUSE_RULE_DISCOVERY_REGISTRY.yaml` -> `BUILD_LAB_PRODUCT_MODEL.md` -> `EXPLAIN_EVERY_VALUE_AND_GROUNDED_FOLLOW_UP.md` -> `FLAGSHIP_PRODUCT_BAR.md` -> `SURFACE_DESIGN_SYSTEM_AND_AI_REVIEW_LOOP.md` -> `CHUMMER5A_FAMILIARITY_BRIDGE.md` -> `DENSE_WORKBENCH_BUDGET.yaml` -> `VETERAN_FIRST_MINUTE_GATE.yaml` -> `PRIMARY_ROUTE_REGISTRY.yaml` -> `DESKTOP_EXECUTABLE_EXIT_GATES.md` -> `FLAGSHIP_RELEASE_ACCEPTANCE.yaml` -> `COMMUNITY_SAFETY_MODERATION_AND_APPEALS.md` -> `COMMUNITY_SAFETY_EVENT_AND_APPEAL_STATES.yaml` -> `CREATOR_DASHBOARD_AND_ADOPTION_ANALYTICS.md` -> `CREATOR_PUBLICATION_ANALYTICS_SCHEMA.yaml` -> `CREATOR_OPERATING_SYSTEM.md` -> `ACCESSIBILITY_AND_COGNITIVE_LOAD_RELEASE_BAR.md` -> `ACCESSIBILITY_COGNITIVE_LOAD_GATES.yaml` -> `NEXT_12_BIGGEST_WINS_GUIDE.md` -> `NEXT_12_BIGGEST_WINS_REGISTRY.yaml` -> `CONFIDENCE_READINESS_AND_CONTINUITY_GUIDE.md` -> `CONFIDENCE_READINESS_AND_CONTINUITY_REGISTRY.yaml` -> `LEGACY_CLIENT_AND_ADJACENT_PARITY.md` -> `LEGACY_CLIENT_AND_ADJACENT_PARITY_REGISTRY.yaml` -> `FLAGSHIP_PARITY_REGISTRY.yaml` -> `CAMPAIGN_OS_GAP_AND_CHANGE_GUIDE.md` -> `PUBLIC_GUIDE_POLICY.md` -> `PUBLIC_GUIDE_PAGE_REGISTRY.yaml` -> `PUBLIC_PART_REGISTRY.yaml` -> `PUBLIC_FAQ_REGISTRY.yaml` -> `NEXT_WAVE_ACCOUNT_AWARE_FRONT_DOOR.md` -> `NEXT_20_BIG_WINS_EXECUTION_PLAN.md` -> `NEXT_20_BIG_WINS_REGISTRY.yaml` -> `POST_AUDIT_NEXT_20_BIG_WINS_GUIDE.md` -> `POST_AUDIT_NEXT_20_BIG_WINS_REGISTRY.yaml` -> `POST_AUDIT_NEXT_20_BIG_WINS_CLOSEOUT.md` -> `NEXT_20_BIG_WINS_AFTER_POST_AUDIT_CLOSEOUT_GUIDE.md` -> `NEXT_20_BIG_WINS_AFTER_POST_AUDIT_CLOSEOUT_REGISTRY.yaml`
6. Governed LTD operating systems:
   `EXTERNAL_TOOLS_PLANE.md` -> `LTD_CAPABILITY_MAP.md` -> `LTD_CAPABILITY_MESH_OPERATING_MODEL.md` -> `LTD_UTILIZATION_MATRIX.md` -> `executive-assistant/docs/LTD_CAPABILITY_ROUTER.md` -> `executive-assistant/config/ltd_capacity_scheduler.yaml` -> `executive-assistant/config/ltd_blast_radius.yaml` -> `executive-assistant/config/ltd_capability_router.yaml` -> `FEATURE_AND_OPPORTUNITY_GUIDE_FOR_DEVELOPERS.md` -> `WHAT_WE_MISSED_LTD_UTILIZATION_OPPORTUNITIES_FOR_CHUMMER6_EXECUTIVE_ASSISTANT.md` -> `LTD_RUNTIME_AND_PROJECTION_REGISTRY.yaml` -> `LTD_CADENCE_AND_FOLLOWTHROUGH_SYSTEM.md` -> `LTD_CADENCE_AND_FOLLOWTHROUGH_REGISTRY.yaml` -> `TEABLE_ADMIN_PROJECTION_AND_INTENT_LAYER.md` -> `ADMIN_INTENT_AND_PROJECTION_RECEIPTS.yaml` -> `EMAILIT_OUTBOUND_DELIVERY_PROVIDER.md` -> `OUTBOUND_NOTIFICATION_TEMPLATE_REGISTRY.yaml` -> `EMAIL_DELIVERY_RECEIPT_MODEL.md` -> `PRODUCT_ANALYTICS_AND_JOURNEY_PROOF_MODEL.md` -> `JOURNEY_PROOF_EVENTS.yaml` -> `ARTIFACT_FACTORY_PIPELINE_MODEL.md` -> `MEDIA_RECIPE_EXECUTION_AND_CLOSEOUT.yaml` -> `BLACK_LEDGER_ADMIN_WORKBENCH_MODEL.md` -> `BLACK_LEDGER_SEASON_OPERATOR_PLAYBOOK.md` -> `WORLD_TICK_AND_OPEN_RUN_CLOSEOUT_SOPS.yaml` -> `COMMUNITY_HUB_OPERATIONS_MODEL.md` -> `KARMA_FORGE_DISCOVERY_LAB_WORKFLOWS.yaml` -> `TABLE_PULSE_DEBRIEF_STUDIO_WORKFLOWS.yaml` -> `COMPANION_LINE_PACK_AND_TRIGGER_OPERATIONS.md` -> `COMPANION_LINE_PACK_REGISTRY.yaml` -> `USER_CONTRIBUTION_PRIVACY_AND_IP_POLICY.md` -> `USER_CONTRIBUTION_VISIBILITY_REGISTRY.yaml` -> `PREMIUM_AND_COMMUNITY_PACKAGING_MODEL.md` -> `PREMIUM_CAPABILITY_REGISTRY.yaml`

### Full canonical set

1. `START_HERE.md`
2. `GLOSSARY.md`
3. `VISION.md`
4. `PRODUCT_SPINE_REDESIGN.md`
5. `PRODUCT_SPINE.yaml`
6. `FINAL_GOLD_GRAPH.generated.json`
7. `HORIZONS.md`
8. `HORIZON_REGISTRY.yaml`
9. `ARCHITECTURE.md`
10. `PRODUCT_BACKBONE_WORKSPACE.md`
11. `PRODUCT_BACKBONE_WORKSPACE.yaml`
12. `LEAD_DESIGNER_OPERATING_MODEL.md`
13. `PRODUCT_GOVERNOR_AND_AUTOPILOT_LOOP.md`
14. `PRODUCT_HEALTH_SCORECARD.yaml`
15. `WHOLE_PROJECT_MISSED_SCOPE_ACCEPTANCE.md`
16. `RELEASE_PIPELINE.md`
17. `PUBLIC_DOWNLOADS_POLICY.md`
18. `DESKTOP_CLIENT_PRODUCT_CUT.md`
19. `DESKTOP_PLATFORM_ACCEPTANCE_MATRIX.yaml`
20. `DESKTOP_AUTO_UPDATE_SYSTEM.md`
21. `PUBLIC_AUTO_UPDATE_POLICY.md`
22. `LOCALIZATION_AND_LANGUAGE_SYSTEM.md`
23. `LOCALIZATION_PARITY_MATRIX.yaml`
24. `ACCOUNT_AWARE_INSTALL_AND_SUPPORT_LINKING.md`
25. `FEEDBACK_AND_CRASH_REPORTING_SYSTEM.md`
26. `FEEDBACK_AND_SIGNAL_OODA_LOOP.md`
27. `FEEDBACK_AND_CRASH_AUTOMATION.md`
28. `FEEDBACK_AND_CRASH_STATUS_MODEL.md`
29. `PUBLIC_LANDING_POLICY.md`
30. `PUBLIC_LANDING_MANIFEST.yaml`
31. `PUBLIC_FEATURE_REGISTRY.yaml`
32. `PUBLIC_LANDING_ASSET_REGISTRY.yaml`
33. `PUBLIC_USER_MODEL.md`
34. `PUBLIC_AUTH_FLOW.md`
35. `IDENTITY_AND_CHANNEL_LINKING_MODEL.md`
36. `PUBLIC_MEDIA_BRIEFS.yaml`
37. `PARTICIPATION_AND_BOOSTER_WORKFLOW.md`
38. `COMMUNITY_SPONSORSHIP_BACKLOG.md`
39. `EXTERNAL_TOOLS_PLANE.md`
40. `LTD_CAPABILITY_MAP.md`
41. `PUBLIC_GUIDE_POLICY.md`
42. `PUBLIC_GUIDE_PAGE_REGISTRY.yaml`
43. `PUBLIC_PART_REGISTRY.yaml`
44. `PUBLIC_FAQ_REGISTRY.yaml`
45. `PUBLIC_HELP_COPY.md`
46. `PUBLIC_GUIDE_EXPORT_MANIFEST.yaml`
47. `HORIZON_SIGNAL_POLICY.md`
48. `PUBLIC_MEDIA_AND_GUIDE_ASSET_POLICY.md`
49. `METRICS_AND_SLOS.yaml`
50. `PUBLIC_TRUST_CONTENT.yaml`
51. `journeys/README.md`
52. `OWNERSHIP_MATRIX.md`
53. `PROGRAM_MILESTONES.yaml`
54. `CONTRACT_SETS.yaml`
55. `GROUP_BLOCKERS.md`
56. `projects/*.md` for repo-specific scope
57. `CAMPAIGN_SPINE_AND_CREW_MODEL.md`
58. `CHARACTER_LIFECYCLE_AND_LIVING_DOSSIER.md`
59. `ROAMING_WORKSPACE_AND_ENTITLEMENT_SYNC.md`
60. `CAMPAIGN_WORKSPACE_AND_DEVICE_ROLES.md`
61. `PRODUCT_CONTROL_AND_GOVERNOR_LOOP.md`
62. `SUPPORT_AND_SIGNAL_OODA_LOOP.md`
63. `USER_JOURNEYS.md`
64. `EXPERIENCE_SUCCESS_METRICS.md`
65. `PUBLIC_NAVIGATION.yaml`
66. `PUBLIC_PROGRESS_PARTS.yaml`
67. `PUBLIC_CAMPAIGN_IMAGE_MANIFEST.yaml`
68. `PUBLIC_RELEASE_EXPERIENCE.yaml`
69. `BUILD_LAB_PRODUCT_MODEL.md`
70. `ACCOUNT_AWARE_FRONT_DOOR_CLOSEOUT.md`
71. `NEXT_WAVE_ACCOUNT_AWARE_FRONT_DOOR.md`
72. `NEXT_15_BIG_WINS_EXECUTION_PLAN.md`
73. `NEXT_20_BIG_WINS_EXECUTION_PLAN.md`
74. `NEXT_20_BIG_WINS_REGISTRY.yaml`
75. `POST_AUDIT_NEXT_20_BIG_WINS_GUIDE.md`
76. `POST_AUDIT_NEXT_20_BIG_WINS_REGISTRY.yaml`
77. `POST_AUDIT_NEXT_20_BIG_WINS_CLOSEOUT.md`
78. `NEXT_20_BIG_WINS_AFTER_POST_AUDIT_CLOSEOUT_GUIDE.md`
79. `NEXT_20_BIG_WINS_AFTER_POST_AUDIT_CLOSEOUT_REGISTRY.yaml`
80. `INTEROP_AND_PORTABILITY_MODEL.md`
81. `RULE_ENVIRONMENT_AND_AMEND_SYSTEM.md`
82. `PROVIDER_AND_ROUTE_STEWARDSHIP.md`
83. `CAMPAIGN_OS_GAP_AND_CHANGE_GUIDE.md`
84. `GOLDEN_JOURNEY_RELEASE_GATES.yaml`
85. `JOURNEY_GATES.generated.json`
86. `PRIVACY_AND_RETENTION_BOUNDARIES.md`
87. `FLAGSHIP_PRODUCT_BAR.md`
88. `SURFACE_DESIGN_SYSTEM_AND_AI_REVIEW_LOOP.md`
89. `CHUMMER5A_FAMILIARITY_BRIDGE.md`
90. `DENSE_WORKBENCH_BUDGET.yaml`
91. `VETERAN_FIRST_MINUTE_GATE.yaml`
92. `PRIMARY_ROUTE_REGISTRY.yaml`
93. `DESKTOP_EXECUTABLE_EXIT_GATES.md`
94. `FLAGSHIP_RELEASE_ACCEPTANCE.yaml`
95. `LEGACY_CLIENT_AND_ADJACENT_PARITY.md`
96. `LEGACY_CLIENT_AND_ADJACENT_PARITY_REGISTRY.yaml`
97. `FLAGSHIP_PARITY_REGISTRY.yaml`
98. `NEXT_12_BIGGEST_WINS_GUIDE.md`
99. `NEXT_12_BIGGEST_WINS_REGISTRY.yaml`
100. `PRODUCT_USAGE_TELEMETRY_MODEL.md`
101. `PRODUCT_USAGE_TELEMETRY_EVENT_SCHEMA.md`
102. `PUBLIC_VIDEO_BRIEFS.yaml`
103. `MEDIA_ARTIFACT_RECIPE_REGISTRY.yaml`
104. `STRUCTURED_VIDEO_AND_NARRATED_MEDIA_MODEL.md`
105. `VIDBOARD_AND_LTD_WOW_FACTOR_WORKFLOWS.md`
106. `adrs/ADR-0016-structured-presenter-video-lane.md`
107. `COMPANION_PERSONA_AND_INTERACTION_MODEL.md`
108. `COMPANION_PACKET.md`
109. `COMPANION_TRIGGER_REGISTRY.yaml`
110. `COMPANION_EVENT_SCHEMA.yaml`
111. `adrs/ADR-0017-first-party-companion-runtime-and-bounded-voice-mode.md`
112. `LTD_DISCOVERY_OUTREACH_AND_VALIDATION_INTEGRATION_GUIDE.md`
113. `ICANPRENEUR_DISCOVERY_AND_VALIDATION_LANE.md`
114. `KARMA_FORGE_DISCOVERY_AND_HOUSE_RULE_INTAKE.md`
115. `HOUSE_RULE_DISCOVERY_REGISTRY.yaml`
116. `PRODUCTLIFT_FEEDBACK_ROADMAP_BRIDGE.md`
117. `KATTEB_PUBLIC_GUIDE_OPTIMIZATION_LANE.md`
118. `PUBLIC_SIGNAL_TO_CANON_PIPELINE.md`
119. `PUBLIC_FEEDBACK_AND_CONTENT_REGISTRY.yaml`
120. `PUBLIC_FEEDBACK_TAXONOMY.yaml`
121. `PUBLIC_SITE_VISIBILITY_AND_SEARCH_OPTIMIZATION.md`
122. `SIGNITIC_FACTION_WAR_AND_WORLD_TICK_CAMPAIGNS.md`
123. `LTD_RUNTIME_AND_PROJECTION_REGISTRY.yaml`
124. `TEABLE_ADMIN_PROJECTION_AND_INTENT_LAYER.md`
125. `ADMIN_INTENT_AND_PROJECTION_RECEIPTS.yaml`
126. `EMAILIT_OUTBOUND_DELIVERY_PROVIDER.md`
127. `OUTBOUND_NOTIFICATION_TEMPLATE_REGISTRY.yaml`
128. `EMAIL_DELIVERY_RECEIPT_MODEL.md`
129. `PRODUCT_ANALYTICS_AND_JOURNEY_PROOF_MODEL.md`
130. `JOURNEY_PROOF_EVENTS.yaml`
131. `ARTIFACT_FACTORY_PIPELINE_MODEL.md`
132. `MEDIA_RECIPE_EXECUTION_AND_CLOSEOUT.yaml`
133. `BLACK_LEDGER_ADMIN_WORKBENCH_MODEL.md`
134. `BLACK_LEDGER_SEASON_OPERATOR_PLAYBOOK.md`
135. `WORLD_TICK_AND_OPEN_RUN_CLOSEOUT_SOPS.yaml`
136. `COMMUNITY_HUB_OPERATIONS_MODEL.md`
137. `KARMA_FORGE_DISCOVERY_LAB_WORKFLOWS.yaml`
138. `TABLE_PULSE_DEBRIEF_STUDIO_WORKFLOWS.yaml`
139. `PUBLIC_GROWTH_AND_VISIBILITY_STACK.md`
140. `USER_CONTRIBUTION_PRIVACY_AND_IP_POLICY.md`
141. `USER_CONTRIBUTION_VISIBILITY_REGISTRY.yaml`
142. `COMPANION_LINE_PACK_AND_TRIGGER_OPERATIONS.md`
143. `COMPANION_LINE_PACK_REGISTRY.yaml`
144. `PREMIUM_AND_COMMUNITY_PACKAGING_MODEL.md`
145. `PREMIUM_CAPABILITY_REGISTRY.yaml`
146. `FLAGSHIP_READINESS_PLANES.yaml`
147. `CHUMMER5A_HUMAN_PARITY_ACCEPTANCE_SPEC.md`
148. `CHUMMER5A_HUMAN_PARITY_ACCEPTANCE_MATRIX.yaml`
149. `READY_FOR_TONIGHT_MODE.md`
150. `READY_FOR_TONIGHT_GATES.yaml`
151. `PUBLIC_ONBOARDING_PATHS_FOR_NO_DESKTOP_USERS.md`
152. `ROLE_KITS_AND_STARTER_LOADOUTS.md`
153. `ROLE_KIT_REGISTRY.yaml`
154. `SOURCE_AWARE_EXPLAIN_PUBLIC_TRUST_HOOK.md`
155. `CAMPAIGN_ADOPTION_START_FROM_TODAY_FLOW.md`
156. `FOUNDRY_FIRST_VTT_HANDOFF_PROOF.md`
157. `VTT_EXPORT_TARGET_ACCEPTANCE.yaml`
158. `COMMUNITY_SAFETY_MODERATION_AND_APPEALS.md`
159. `COMMUNITY_SAFETY_EVENT_AND_APPEAL_STATES.yaml`
160. `WORLD_BROADCAST_AND_FACTION_PROPAGANDA_CADENCE.md`
161. `WORLD_BROADCAST_RECIPE_REGISTRY.yaml`
162. `CREATOR_DASHBOARD_AND_ADOPTION_ANALYTICS.md`
163. `CREATOR_PUBLICATION_ANALYTICS_SCHEMA.yaml`
164. `ACCESSIBILITY_AND_COGNITIVE_LOAD_RELEASE_BAR.md`
165. `ACCESSIBILITY_COGNITIVE_LOAD_GATES.yaml`
166. `RUNNER_PASSPORT_AND_CROSS_COMMUNITY_TRUST.md`
167. `RUNNER_PASSPORT_ACCEPTANCE.yaml`
168. `WORLD_DISPATCH_AND_REACTIVATION_LOOP.md`
169. `WORLD_DISPATCH_REACTIVATION_GATES.yaml`
170. `CREATOR_OPERATING_SYSTEM.md`
171. `LTD_CADENCE_AND_FOLLOWTHROUGH_SYSTEM.md`
172. `LTD_CADENCE_AND_FOLLOWTHROUGH_REGISTRY.yaml`
173. `EXPLAIN_EVERY_VALUE_AND_GROUNDED_FOLLOW_UP.md`
174. `CAMPAIGN_OPERABILITY_SCORING_RUBRIC.yaml`
175. `RELEASE_READINESS_SCOPE_MODEL.md`
176. `CAMPAIGN_OPERABILITY_SCORECARD.generated.json`

The current high-leverage hero-path additions are `READY_FOR_TONIGHT_MODE.md`, `READY_FOR_TONIGHT_GATES.yaml`, `PUBLIC_ONBOARDING_PATHS_FOR_NO_DESKTOP_USERS.md`, `ROLE_KITS_AND_STARTER_LOADOUTS.md`, `ROLE_KIT_REGISTRY.yaml`, `SOURCE_AWARE_EXPLAIN_PUBLIC_TRUST_HOOK.md`, `EXPLAIN_EVERY_VALUE_AND_GROUNDED_FOLLOW_UP.md`, `CAMPAIGN_ADOPTION_START_FROM_TODAY_FLOW.md`, `FOUNDRY_FIRST_VTT_HANDOFF_PROOF.md`, `VTT_EXPORT_TARGET_ACCEPTANCE.yaml`, `COMMUNITY_SAFETY_MODERATION_AND_APPEALS.md`, `COMMUNITY_SAFETY_EVENT_AND_APPEAL_STATES.yaml`, `WORLD_BROADCAST_AND_FACTION_PROPAGANDA_CADENCE.md`, `WORLD_BROADCAST_RECIPE_REGISTRY.yaml`, `CREATOR_DASHBOARD_AND_ADOPTION_ANALYTICS.md`, `CREATOR_PUBLICATION_ANALYTICS_SCHEMA.yaml`, `ACCESSIBILITY_AND_COGNITIVE_LOAD_RELEASE_BAR.md`, and `ACCESSIBILITY_COGNITIVE_LOAD_GATES.yaml`.
They exist so the product cannot mistake canonical depth for immediate user value: the design must still prove readiness tonight, no-desktop participation, role-kit clarity, start-from-today adoption, Foundry-first handoff, world cadence, community safety, creator feedback, and reduced cognitive load.

`HORIZON_REGISTRY.yaml` is the machine-readable source for horizon existence, order, public-guide eligibility, and eventual build path.
The current horizon set covers knowledge fabric, spatial/runsite artifacts, creator press, replay/forensics, and bounded table coaching in addition to the earlier continuity and simulation lanes.
`CAMPAIGN_SPINE_AND_CREW_MODEL.md` is the missing-middle canon for the campaign-scale product: runner dossier, crew, campaign, run, scene, objective, continuity, and replay-safe event memory.
`CHARACTER_LIFECYCLE_AND_LIVING_DOSSIER.md` is the canonical bridge from deterministic build truth into the long-lived dossier a player, GM, campaign, and artifact lane actually carry forward.
`ROAMING_WORKSPACE_AND_ENTITLEMENT_SYNC.md` defines how claimed installs restore person, campaign, and entitlement-shaped workspace truth across devices without mutating signed artifacts, syncing secrets, or hiding conflict semantics.
`CAMPAIGN_WORKSPACE_AND_DEVICE_ROLES.md` defines the next visible product layer on top of roaming workspace: the home cockpit, campaign workspace, what-changed-for-me packet, and install-local device roles such as workstation, play tablet, observer screen, travel cache, and preview scout.
`INTEROP_AND_PORTABILITY_MODEL.md` makes import/export, portable dossier and campaign packages, migration receipts, and round-trip provenance first-class product promises instead of leaving them as compatibility folklore.
`RULE_ENVIRONMENT_AND_AMEND_SYSTEM.md` makes rules presets, custom-data overlays, amend packages, and activation receipts first-class product truth instead of hidden custom-data cargo.
`PRODUCT_CONTROL_AND_GOVERNOR_LOOP.md` defines the product-control plane as a first-class middle layer instead of leaving whole-product steering implicit in support notes or operator habit.
`PRODUCT_BACKBONE_WORKSPACE.md` defines the convergence target for one primary product workspace so repo boundaries stay secondary to flagship product truth.
`PRODUCT_BACKBONE_WORKSPACE.yaml` is the machine-readable module map and first-wave convergence order for that workspace.
`DENSE_WORKBENCH_BUDGET.yaml` is the release-blocking density and noise-budget contract for the promoted flagship desktop head.
`VETERAN_FIRST_MINUTE_GATE.yaml` is the release-blocking orientation test pack for serious Chummer5a users on the promoted desktop route.
`PRIMARY_ROUTE_REGISTRY.yaml` is the machine-readable "one real route per major job" contract that keeps fallback from masquerading as the product.
`FLAGSHIP_PARITY_REGISTRY.yaml` is the release-facing parity ladder; it is intentionally stricter than the softer legacy parity `covered` registry.
`SUPPORT_AND_SIGNAL_OODA_LOOP.md` defines how support, crash, feedback, release, and public-promise signals become governed packets that can actually change design, docs, queue, or release posture.
`USER_JOURNEYS.md` is the top-level product map for Build, Explain, Run, Publish, and Improve, with the detailed happy-path/failure-mode canon still living under `journeys/*.md`.
`GOLDEN_JOURNEY_RELEASE_GATES.yaml` is the machine-readable proof contract for the six journeys every release wave must keep passable enough to promote honestly.
`CAMPAIGN_AUTHORITY_AND_PERMISSIONS.md` is the canonical campaign and community authority matrix for campaign roster, run, workspace, publication, and escalation actions across player, organizer, support, and operator roles.
`EXPERIENCE_SUCCESS_METRICS.md` translates repo and release gates back into user-facing promises so the product is measured as a lived system, not only as a clean repo graph.
`RELEASE_PIPELINE.md` is the canonical source for where release orchestration, desktop packaging, runtime-bundle production, registry publication truth, updater feeds, and public download/install rendering belong.
`DESKTOP_CLIENT_PRODUCT_CUT.md` names the shipped flagship desktop head, the fallback head, the current preview cut, and the explicit platform posture so delivery focus does not drift with repo shape.
`DESKTOP_PLATFORM_ACCEPTANCE_MATRIX.yaml` is the machine-readable release truth for Windows, Linux, and macOS package posture, smoke gating, signing/notarization expectations, updater mode, and supportability.
`PUBLIC_DOWNLOADS_POLICY.md` and `PUBLIC_AUTO_UPDATE_POLICY.md` are the public copy and CTA truth for `/downloads` and in-app update promises, so landing/help/guide surfaces cannot drift away from the install/update contract.
`DESKTOP_AUTO_UPDATE_SYSTEM.md` is the canonical source for the first desktop self-update wave, including the split between install media, machine update payloads, registry-owned release heads, rollout states, and UI-owned apply helpers.
`LOCALIZATION_AND_LANGUAGE_SYSTEM.md` defines the shipping locale set, translation domains, fallback rules, restart behavior, carried-corpus bridge strategy, and localization acceptance gates for desktop and hosted surfaces.
`LOCALIZATION_PARITY_MATRIX.yaml` is the machine-readable parity target for locale-by-domain coverage across app chrome, install/update/support, explain/receipts, data/rules names, and generated artifacts.
`FEEDBACK_AND_CRASH_REPORTING_SYSTEM.md` is the canonical source for the first support plane, including the split between crash reporting, structured bug reporting, lightweight feedback, Hub-owned case truth, and the rule that the grounded support assistant stays an optional phase-2 layer rather than the gate in front of real support intake.
`FEEDBACK_AND_SIGNAL_OODA_LOOP.md` is the canonical routing loop from raw support, survey, public-issue, and release signals into code, docs, queue, policy, or canon action.
`ACCOUNT_AWARE_INSTALL_AND_SUPPORT_LINKING.md` is the canonical source for Hub-first downloads, claimable installs, installation-level auth, and the rule that Chummer personalizes the relationship rather than the binary.
`FEEDBACK_AND_CRASH_STATUS_MODEL.md` is the canonical source for support-case status events, fix-available notices, and post-release follow-up rules.
`PRODUCT_GOVERNOR_AND_AUTOPILOT_LOOP.md` defines the whole-product operator seam between reality and canon, while `PRODUCT_HEALTH_SCORECARD.yaml` defines the weekly pulse that role uses to freeze, reroute, or escalate work.
`WEEKLY_PRODUCT_PULSE.generated.json` is the generated weekly snapshot that turns the scorecard and progress history into a bounded governor-ready decision artifact, and for EA it condenses the EA flagship receipt, fleet journey gates, and scorecard into one steering packet.
`PUBLIC_LANDING_MANIFEST.yaml`, `PUBLIC_FEATURE_REGISTRY.yaml`, and `PUBLIC_LANDING_ASSET_REGISTRY.yaml` are the machine-readable source for the `chummer.run` landing structure, CTA routing, public proof shelf, asset slots, and signed-in overlay posture.
`PUBLIC_NAVIGATION.yaml` and `PUBLIC_PROGRESS_PARTS.yaml` define the public front-door routes and the public pulse grouping, while `PUBLIC_CAMPAIGN_IMAGE_MANIFEST.yaml` is the canonical campaign-art direction for the front door rather than an orphan media sidecar.
`PUBLIC_PROGRESS_PARTS.yaml` is the canonical product-part mapping, public copy registry, and ETA/momentum policy input for the hosted `/progress` report, while `PROGRESS_REPORT.generated.json`, `PROGRESS_REPORT.generated.html`, and `PROGRESS_REPORT_POSTER.svg` are generated downstream projections that Hub may serve directly. The raster-only rule in the public media briefs applies to front-door campaign art rather than these generated progress exports.
`PUBLIC_RELEASE_EXPERIENCE.yaml` is the canonical guest and signed-in release shelf posture for `/downloads`, install help, known-issue routing, and the trust language around promoted versus preview desktop heads.
`PUBLIC_AUTH_FLOW.md` defines the first-wave login/signup/logout posture, guest fallbacks, and which provider surfaces may appear publicly in the hosted shell.
`IDENTITY_AND_CHANNEL_LINKING_MODEL.md` is the canonical source for email hygiene, social bootstrap, linked identities, official companion channels, and the rule that EA stays the orchestrator brain behind those channels.
`PUBLIC_GUIDE_PAGE_REGISTRY.yaml`, `PUBLIC_PART_REGISTRY.yaml`, `PUBLIC_FAQ_REGISTRY.yaml`, and `PUBLIC_HELP_COPY.md` are the machine-readable and public-safe source of truth for downstream guide generation outside the landing surface, including the generated download/build shelf.
`METRICS_AND_SLOS.yaml` is the release-scorecard canon for measurable user-trust, continuity, publication, and install/update gates.
`PRODUCT_USAGE_TELEMETRY_MODEL.md` defines the privacy-bounded, install-aware telemetry Chummer should keep so roadmap, localization, ruleset, houserule, startup, and workflow decisions can be based on real product use rather than guesswork.
`PRODUCT_USAGE_TELEMETRY_EVENT_SCHEMA.md` defines the exact event names, daily rollups, and opt-out settings that `chummer6-ui` and `chummer6-hub` should implement for that telemetry plane.
`PRIVACY_AND_RETENTION_BOUNDARIES.md` defines the default retention clocks, redaction posture, and ownership split for support, crash, claim/install, survey, provider-trace, and publication telemetry surfaces.
`PUBLIC_TRUST_CONTENT.yaml` is the canonical trust-content manifest for help, contact, and support statements surfaced at `/help`, `/contact`, and `/downloads`.
`PUBLIC_VIDEO_BRIEFS.yaml`, `MEDIA_ARTIFACT_RECIPE_REGISTRY.yaml`, `STRUCTURED_VIDEO_AND_NARRATED_MEDIA_MODEL.md`, and `VIDBOARD_AND_LTD_WOW_FACTOR_WORKFLOWS.md` are the media/publication canon that turns owned LTD posture into repeatable artifact-factory workflows rather than isolated vendor notes.
`BLACK_LEDGER_NEWSROOM_CANON.md`, `BLACK_LEDGER_ANCHOR_BIBLE.yaml`, `BLACK_LEDGER_BROADCAST_STYLE_GUIDE.md`, `BLACK_LEDGER_NEWSROOM_EDITORIAL_POLICY.md`, and `BLACK_LEDGER_NEWSROOM_QUALITY_GATES.yaml` define the photoreal newsroom bar for Black Ledger bulletins so the public news lane cannot regress back to SVG-only motion cards or ungrounded hype.
`ARTIFACT_FACTORY_PIPELINE_MODEL.md` and `MEDIA_RECIPE_EXECUTION_AND_CLOSEOUT.yaml` define the cross-tool artifact factory from approved source packet to rendered video, document, card, audio, social, signature, email, and closeout receipts.
`LTD_RUNTIME_AND_PROJECTION_REGISTRY.yaml` composes owned tools into governed operating systems: public growth, discovery, artifact factory, BLACK LEDGER ops, Table Pulse/companion lab, and trust/closure. It also keeps optional buys such as SendFox, Flonnect, CutMe Short, Backona AI, and Visby as watchlist candidates rather than product truth.
`RUNBOOK_AND_ORIGIN_PROVIDER_GOLD_PRODUCTION_GATE.md` is the shared promotion gate for the Subscribr and First Book ai split across `runbook-press` and `origin-dossier`: it defines the contracts, blockers, validation evidence, and rollout order required before either lane can be called gold-production ready.
`TEABLE_ADMIN_PROJECTION_AND_INTENT_LAYER.md`, `BLACK_LEDGER_ADMIN_WORKBENCH_MODEL.md`, and `ADMIN_INTENT_AND_PROJECTION_RECEIPTS.yaml` define Teable as an operator workbench and intent-entry layer, never as canonical world, run, support, rule, release, or entitlement truth.
`EMAILIT_OUTBOUND_DELIVERY_PROVIDER.md`, `OUTBOUND_NOTIFICATION_TEMPLATE_REGISTRY.yaml`, and `EMAIL_DELIVERY_RECEIPT_MODEL.md` define Emailit as an outbound delivery candidate downstream of Hub-owned notification truth, template refs, suppression, and receipts.
`PRODUCT_ANALYTICS_AND_JOURNEY_PROOF_MODEL.md` and `JOURNEY_PROOF_EVENTS.yaml` extend product telemetry into golden-journey proof so Chummer measures whether users succeed, not only whether artifacts were published.
`SIGNITIC_FACTION_WAR_AND_WORLD_TICK_CAMPAIGNS.md` defines projection-only managed-signature campaigns for BLACK LEDGER world ticks, faction propaganda, Community Hub recruitment, intel drives, and season/newsreel amplification, with first-party destinations and no notification, world, support, analytics, or authorization truth.
`COMMUNITY_HUB_OPERATIONS_MODEL.md`, `BLACK_LEDGER_SEASON_OPERATOR_PLAYBOOK.md`, `WORLD_TICK_AND_OPEN_RUN_CLOSEOUT_SOPS.yaml`, `KARMA_FORGE_DISCOVERY_LAB_WORKFLOWS.yaml`, and `TABLE_PULSE_DEBRIEF_STUDIO_WORKFLOWS.yaml` make the living-campaign and discovery loops operational instead of leaving them as feature ideas.
`USER_CONTRIBUTION_PRIVACY_AND_IP_POLICY.md` and `USER_CONTRIBUTION_VISIBILITY_REGISTRY.yaml` define how user lore, intel, house-rule, session, media, and public feedback contributions stay useful without leaking private table material or copyrighted source text.
`COMPANION_LINE_PACK_AND_TRIGGER_OPERATIONS.md`, `COMPANION_LINE_PACK_REGISTRY.yaml`, `PREMIUM_AND_COMMUNITY_PACKAGING_MODEL.md`, and `PREMIUM_CAPABILITY_REGISTRY.yaml` define reviewed companion content operations and monetization boundaries that sell capacity, convenience, publishing, media, or organizer tooling rather than rules advantage.
`COMPANION_PERSONA_AND_INTERACTION_MODEL.md`, `COMPANION_PACKET.md`, `COMPANION_TRIGGER_REGISTRY.yaml`, and `COMPANION_EVENT_SCHEMA.yaml` define the first-party companion identity, trigger truth, runtime packet contract, suppression discipline, and structured event lanes so desktop/mobile shells, public concierge surfaces, EA compile passes, and downstream media packs stay aligned.
`journeys/*.md` defines the top end-to-end user flows and failure-mode recoveries that multiple repos must preserve.
`BUILD_LAB_PRODUCT_MODEL.md` defines Build Lab as a flagship Build plus Explain surface rather than leaving it as a downstream milestone label without a canonical product promise.
`EXPLAIN_EVERY_VALUE_AND_GROUNDED_FOLLOW_UP.md` makes every visible mechanical value, warning, and bounded what-if answer part of the same packet-backed trust contract instead of leaving explain quality to tooltips, presenter demos, or repo-local folklore.
`FLAGSHIP_PRODUCT_BAR.md` defines the cross-repo craftsmanship bar for what counts as a premium, public-release-ready Chummer product rather than only a closed wave or green test run.
`SURFACE_DESIGN_SYSTEM_AND_AI_REVIEW_LOOP.md` defines the cross-surface design contract, platform overlays, AI generation instructions, and screenshot-review loop that every promoted UI head must follow.
`CHUMMER5A_FAMILIARITY_BRIDGE.md` is the veteran-orientation bridge for install, first-run, and workbench familiarity so modernization keeps Chummer5a muscle memory intact.
`DESKTOP_EXECUTABLE_EXIT_GATES.md` defines machine-checked desktop flagship gates for installer coherence, startup proof, live shell command surfaces, public-route truth, and the dedicated user-journey tester audit that proves focus-stable Master Index search plus visible new-character creation from the promoted Linux binary.
`FLAGSHIP_RELEASE_ACCEPTANCE.yaml` turns that craftsmanship bar into a machine-readable acceptance matrix so release control, ETA, and completion logic can prove flagship readiness instead of merely describing it.
`ACCOUNT_AWARE_FRONT_DOOR_CLOSEOUT.md` records the just-closed install, update, support, and operator-control wave so roadmap and milestone language does not lag the public-main implementation.
`POST_AUDIT_NEXT_20_BIG_WINS_CLOSEOUT.md` records the now-closed post-audit wave boundary and keeps `ROADMAP.md`, public proof evidence, and registry status aligned.
`NEXT_12_BIGGEST_WINS_GUIDE.md` and `NEXT_12_BIGGEST_WINS_REGISTRY.yaml` are the active flagship-closeout wave after the closed additive and post-audit sequences.
`LEGACY_CLIENT_AND_ADJACENT_PARITY.md` and `LEGACY_CLIENT_AND_ADJACENT_PARITY_REGISTRY.yaml` are the active no-step-back overlay for that closeout wave: they turn Chummer4, Chummer5a, Hero Lab, Genesis, and CommLink-class expectations into release-blocking feature families instead of vague nostalgia.
`CAMPAIGN_OS_GAP_AND_CHANGE_GUIDE.md` is the current audit-driven remediation overlay for that closeout wave: it states where journey proof, bounded-context discipline, flagship surface focus, localization, provider stewardship, and promotion proof still lag the now-strong architectural center.
`NEXT_WAVE_ACCOUNT_AWARE_FRONT_DOOR.md` remains the historical milestone spine for the front-door wave, while `NEXT_15_BIG_WINS_EXECUTION_PLAN.md` is preserved as the older prior plan, `NEXT_20_BIG_WINS_EXECUTION_PLAN.md` is the preserved additive-wave closeout plan, and `NEXT_20_BIG_WINS_REGISTRY.yaml` is the machine-readable closeout registry that validators and downstream mirrors can consume directly.

## Active Chummer repos

### `chummer6-design`

Lead-designer repo. Owns cross-repo canonical design truth.

### `chummer6-core`

Deterministic rules/runtime engine. Owns engine truth, explain canon, reducer truth, runtime bundles, and engine contracts.

### `chummer6-ui`

Workbench/browser/desktop product head. Owns builders, inspectors, compare tools, moderation/admin UX, large-screen operator flows, desktop installer recipes, desktop updater integration, desktop apply helpers, and in-app feedback/bug/crash entry points.

### `chummer6-mobile`

Player and GM play-mode shell. Owns mobile/PWA/session UX, offline ledger, sync client, and play-safe live-session surfaces.

### `chummer6-hub`

Hosted orchestration and community plane. Owns identity mapping, user/community accounts, generic groups and memberships, sponsorship/guided-contribution UX, fact/reward/entitlement ledgers, public landing/home projection for `chummer.run`, play API aggregation, relay, approvals, memory, Coach/Spider/Director orchestration, support-case and help surfaces, and hosted service policy. The next major product sequencing rule is Hub-first: account/group/ledger backbone before more guided-contribution-specific Fleet product behavior.

### `chummer6-ui-kit`

Shared design system package. Owns tokens, themes, shell primitives, accessibility primitives, and Chummer-specific reusable UI components.

### `chummer6-hub-registry`

Artifact catalog and publication system. Owns immutable artifacts, publication workflows, release channels, desktop release heads, install/update truth, reviews, compatibility, and runtime-bundle head metadata.

### `chummer6-media-factory`

Dedicated media execution plant. Owns render jobs, previews, manifests, asset lifecycle, and provider isolation for documents, portraits, and bounded video.

## Reference-only repo

### `chummer5a`

Legacy/oracle repo. Used for migration, regression fixtures, and compatibility reference. It is not the vNext product lane.

## Adjacent repos

These inform the program but are not part of the main release train:

* `fleet` — worker orchestration/control plane, mirrored from this repo for execution policy, parity automation, queue synthesis, release orchestration, and signing/notarization evidence
* `executive-assistant` — governed assistant runtime and synthesis/petition reference pattern, including proactive horizon scans, human-edit reflection, bounded replanning, interruption-budget throttling, and explicit design-governance skills such as `design_petition`, `design_synthesis`, and `mirror_status_brief`; repo scope lives in `projects/executive-assistant.md`
* `Chummer6` — downstream public guide and Horizons explainer repo; useful for public storytelling, but not canonical design truth

## Current program priorities

4. Keep canonical design files concise, machine-readable where useful, and clearly above operational evidence noise.
5. Keep Hub’s user/group/ledger/sponsorship model canonical so community participation, premium bursts, and later GM-group tooling all grow from one reusable platform.
6. Maintain Fleet’s cheap-first execution plane and premium-burst policy through mirrored design truth rather than repo-local invention.
7. Give workers a legal petition path when the blueprint is missing a seam, synthesize repeated findings before they become queue truth, and route whole-product signal clusters through the product-governor loop instead of ad hoc operator intuition.
8. Treat future repo work as additive product evolution, not split-wave cleanup or contract-canon repair.
9. Keep sponsored participation generic: Hub grows the reusable user/group/ledger platform first, and Fleet stays the worker execution plane underneath it.
10. Keep `chummer.run` as the product front door and proof shelf, while `Chummer6` remains the deeper downstream explainer.
11. Keep release/build/install/update truth split cleanly: Core emits runtime bundles, UI emits installer-ready desktop heads plus updater apply logic, Fleet orchestrates the release lane, Registry owns promoted channel truth and feed metadata, and Hub renders downloads from registry state.
12. Keep installs claimable rather than personalized: Hub may bind an install to an account, but shipped desktop artifacts remain canonical signed builds for their release target.
13. Keep no-step-back parity release-blocking: modernization is allowed, but the active registry must close every in-scope legacy or adjacent client feature family with a first-class successor or bounded receipt before flagship claims are allowed.
14. Treat `chummer-product` as the primary integration workspace: product-model modules, parity lab, classic dense workbench, and desktop-native registry/update flow now converge there before any future repo split gets to define the user experience.

The foundational closure wave is materially finished. The Account-Aware Front Door wave, the Next 20 additive wave, and the Post-Audit Next 20 wave are all materially closed on public `main`, with their closeout records preserved in `ACCOUNT_AWARE_FRONT_DOOR_CLOSEOUT.md`, `NEXT_20_BIG_WINS_EXECUTION_PLAN.md`, `NEXT_20_BIG_WINS_REGISTRY.yaml`, and `POST_AUDIT_NEXT_20_BIG_WINS_CLOSEOUT.md`. Campaign workspace / GM runboard, rule-environment posture, package-owned campaign contracts, roaming restore, Build Lab handoff UX, Rules Navigator, creator publication posture, and the first organizer/operator layer now count as shipped product surfaces instead of only design intent. Remaining growth tracks such as campaign indispensability, publication depth, install-aware trust posture, broader public promotion, and live operator cadence now sit on top of finished release-governance and boundary truth instead of reopening it. Flagship closeout is still not complete: `BLK-009` flagship localization proof and `BLK-010` campaign-OS lived-system proof remain active.

The current risk is no longer missing architecture. The current risk is that the campaign OS can be described better than it can be proven as a lived system across install, continuity, play, publication, closure, and no-step-back client parity. `CAMPAIGN_OS_GAP_AND_CHANGE_GUIDE.md` and `LEGACY_CLIENT_AND_ADJACENT_PARITY.md` are the active correction layers for that gap.

The queued successor wave after the current flagship closeout is `NEXT_90_DAY_PRODUCT_ADVANCE_GUIDE.md`, with machine-readable staging in `NEXT_90_DAY_PRODUCT_ADVANCE_REGISTRY.yaml` and `NEXT_90_DAY_QUEUE_STAGING.generated.yaml`. It keeps the next quarter focused on repeatable desktop release truth, parity-lab proof, boring continuity, first-party artifact proof, premium campaign orientation bundles, bounded public concierge and trust-surface guidance, install-aware release and support concierge flow, campaign operations, rule-environment studio, portable exchange, creator publication, artifact shelves, organizer/community operations, guided onboarding, public launch-health packets, and product-governor cadence rather than reopening architecture cleanup.

`PUBLIC_CONCIERGE_AND_TRUST_WIDGET_MODEL.md`, `PUBLIC_CONCIERGE_WORKFLOWS.yaml`, and `EXTERNAL_TOOLS_BLOCKING_POLICY_REWORK.md` are the current canon for bounded public trust widgets, structured intake and booking handoff, and the narrow policy exception that allows those flows on Hub-owned public surfaces without weakening first-party truth boundaries.

`PARTICIPATION_AND_BOOSTER_WORKFLOW.md` is the first-class canon for user language, ownership, state transitions, receipts, recognition, and package/bootstrap truth for the bounded participation lane.

`COMMUNITY_SPONSORSHIP_BACKLOG.md` is the implementation-ordered source for the Hub-first community/accounting wave. It distinguishes what already landed in Hub/Fleet/EA from the remaining durable-storage, convergence, and product-depth deltas.

`REPO_HYGIENE_RELEASE_TRUST_AND_AUTOMATION_SAFETY.md` and `REPO_HARDENING_CHECKLIST.yaml` are the active hardening canon for public repo hygiene, signed release-manifest truth, declarative boundary lint, workflow safety, Fleet blast-radius limits, boring user-loop proof, and explicit public support lanes.

`PRODUCT_GOVERNOR_AND_AUTOPILOT_LOOP.md`, `PROVIDER_AND_ROUTE_STEWARDSHIP.md`, `FEEDBACK_AND_SIGNAL_OODA_LOOP.md`, and `PRODUCT_HEALTH_SCORECARD.yaml` are the operating loop for turning product reality into governed course correction instead of leaving that work as scattered feedback notes.

`CAMPAIGN_SPINE_AND_CREW_MODEL.md`, `CHARACTER_LIFECYCLE_AND_LIVING_DOSSIER.md`, `PRODUCT_CONTROL_AND_GOVERNOR_LOOP.md`, `SUPPORT_AND_SIGNAL_OODA_LOOP.md`, and `BUILD_LAB_PRODUCT_MODEL.md` are the now-closed additive center-of-gravity wave record: the executable middle between build truth and campaign reality, plus the flagship Build and Explain surfaces that made that middle visible to real users. They are now the baseline for follow-on campaign breadth and promotion work rather than still-open canon debt.

## Non-goal

The immediate goal is not to add endless new features while the architecture is still blurry.

The immediate goal is:

* clean ownership
* package-based contracts
* real split completion
* durable design truth
* repeatable release governance
