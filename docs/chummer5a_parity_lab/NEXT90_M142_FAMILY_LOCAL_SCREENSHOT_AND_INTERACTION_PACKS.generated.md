# Next90 M142 EA Family-Local Screenshot And Interaction Packs

- status: `fail`
- ready: `False`
- canonical queue frontier: `5399660048`

## Desktop readiness
- `desktop_client`: `warning`
- summary: Desktop flagship proof is waiting on external host-proof capture.

## Family summary
- `dense_builder_and_career_workflows`: pass
  - compare artifacts: `oracle:tabs, oracle:workspace_actions, workflow:build_explain_publish`
  - workflow task ids: `reach_real_workbench, recover_section_rhythm`
  - required screenshots: `05-dense-section-light.png, 06-dense-section-dark.png, 07-loaded-runner-tabs-light.png`
  - parity audit: visual=`yes` behavioral=`yes`
  - screenshot receipts:
  - screenshot `screenshot:dense_workbench_light` -> `ok`
    receipt proof: `screenshot_gate` requires `05-dense-section-light.png, dense_builder, legacy_dense_builder_rhythm`
  - screenshot `screenshot:dense_workbench_dark` -> `ok`
    receipt proof: `screenshot_gate` requires `06-dense-section-dark.png`
  - screenshot `screenshot:loaded_runner_tabs` -> `ok`
    receipt proof: `visual_gate` requires `07-loaded-runner-tabs-light.png, Loaded_runner_preserves_visible_character_tab_posture`
  - interaction receipts:
  - interaction `oracle:tabs` -> `ok`
    receipt proof: `section_host_ruleset_parity` requires `expectedTabIds, tab-info, tab-skills, tab-qualities, tab-combat, tab-gear`
  - interaction `oracle:workspace_actions` -> `ok`
    receipt proof: `section_host_ruleset_parity` requires `expectedWorkspaceActionIds, tab-info.summary, tab-skills.skills, tab-gear.inventory`
  - interaction `workflow:build_explain_publish` -> `ok`
    receipt proof: `workflow_gate` requires `create-open-import-save-save-as-print-export, dense-workbench-affordances-search-add-edit-remove-preview-drill-in-compare, Loaded_runner_workbench_preserves_legacy_frmcareer_landmarks, Character_creation_preserves_familiar_dense_builder_rhythm, Advancement_and_karma_journal_workflows_preserve_familiar_progression_rhythm`
  - interaction `workbench:classic_dense_posture` -> `ok`
    receipt proof: `classic_dense_gate` requires `usesCompactFluentDensity, Character_creation_preserves_familiar_dense_builder_rhythm`
- `dice_initiative_and_table_utilities`: fail
  - compare artifacts: `menu:dice_roller, workflow:initiative`
  - workflow task ids: `locate_save_import_settings`
  - required screenshots: `02-menu-open-light.png, 04-loaded-runner-light.png`
  - parity audit: visual=`yes` behavioral=`yes`
  - screenshot receipts:
  - screenshot `screenshot:menu_open` -> `ok`
    receipt proof: `visual_gate` requires `02-menu-open-light.png, Runtime_backed_menu_bar_preserves_classic_labels_and_clickable_primary_menus`
  - screenshot `screenshot:loaded_runner_utility_lane` -> `ok`
    receipt proof: `screenshot_gate` requires `04-loaded-runner-light.png, menu:dice_roller_or_workflow:initiative_screenshot, initiative_screenshot`
  - interaction receipts:
  - interaction `menu:dice_roller` -> `ok`
    receipt proof: `generated_dialog_parity` requires `dialog.dice_roller, dice_roller`
  - interaction `workflow:initiative` -> `missing`
    receipt proof: `gm_runboard_route` requires `Initiative lane:, ResolveRunboardInitiativeSummary, gm_runboard`
  - interaction `workflow:initiative_budget_receipt` -> `ok`
    receipt proof: `core_receipts_doc` requires `workflow:initiative, SessionActionBudgetDeterministicReceipt`
  - interaction `workflow:initiative_runtime_marker` -> `ok`
    receipt proof: `workflow_gate` requires `initiative_utility, menu:dice_roller_or_workflow:initiative_screenshot, 11 + 2d6`
- `identity_contacts_lifestyles_history`: pass
  - compare artifacts: `workflow:contacts, workflow:lifestyles, workflow:notes`
  - workflow task ids: `recover_section_rhythm`
  - required screenshots: `10-contacts-section-light.png, 11-diary-dialog-light.png`
  - parity audit: visual=`yes` behavioral=`yes`
  - screenshot receipts:
  - screenshot `screenshot:contacts_section` -> `ok`
    receipt proof: `visual_gate` requires `10-contacts-section-light.png, legacyContactsWorkflowRhythm`
  - screenshot `screenshot:diary_dialog` -> `ok`
    receipt proof: `visual_gate` requires `11-diary-dialog-light.png, legacyDiaryWorkflowRhythm`
  - interaction receipts:
  - interaction `workflow:contacts` -> `ok`
    receipt proof: `section_host_ruleset_parity` requires `tab-contacts.contacts, tab-contacts`
  - interaction `workflow:lifestyles` -> `ok`
    receipt proof: `core_receipts_doc` requires `workflow:lifestyles, WorkspaceWorkflowDeterministicReceipt`
  - interaction `workflow:notes` -> `ok`
    receipt proof: `section_host_ruleset_parity` requires `tab-notes.metadata, tab-notes`
  - interaction `workflow:contacts_notes_runtime_marker` -> `ok`
    receipt proof: `workflow_gate` requires `Contacts_diary_and_support_routes_execute_with_public_path_visibility, tab-lifestyle.lifestyles, tab-notes.metadata`

## Queue guardrails
- canonical queue or registry rows still control closeout; this packet does not mark them complete locally
- the approved `.codex-design` local mirror must stay byte-for-byte aligned with canonical queue and registry metadata
- duplicate queue or registry rows fail closed

## Closeout blockers
- dice_initiative_and_table_utilities: missing interaction receipts: workflow:initiative
- published readiness still reports desktop_client as warning: Desktop flagship proof is waiting on external host-proof capture.
- canonical design/queue rows are not marked complete yet
