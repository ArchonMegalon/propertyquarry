override SHELL := /bin/bash
override .SHELLFLAGS := -p -o pipefail -c
override MAKE := /usr/bin/make
override MAKE_COMMAND := /usr/bin/make

override _PROPERTYQUARRY_RELEASE_DISPATCH_TARGETS := release-preflight propertyquarry-release-preflight propertyquarry-release-protocol-contracts propertyquarry-native-release-control-gates materialize-release-assets-authenticated verify-release-assets-authenticated verify-flagship-release-readiness-authenticated verify-generated-release-artifacts-clean-authenticated property-release-gates property-security-posture ltd-release-gates verify-ltd-critical-entries-authenticated verify-ltd-flagship-subset-authenticated verify-pocket-audio-archive verify-design-mirror-bundle verify-design-full-mirror-parity ci-gates-authenticated
override _PROPERTYQUARRY_RELEASE_INTERNAL_TARGETS := ci-local-authenticated operator-help-authenticated release-smoke-authenticated smoke-api-authenticated smoke-help-authenticated test-api-authenticated
override _PROPERTYQUARRY_RELEASE_AUTHENTICATED_TARGETS := $(_PROPERTYQUARRY_RELEASE_DISPATCH_TARGETS) $(_PROPERTYQUARRY_RELEASE_INTERNAL_TARGETS)
ifneq ($(strip $(filter $(_PROPERTYQUARRY_RELEASE_AUTHENTICATED_TARGETS),$(MAKECMDGOALS))),)
ifneq ($(PROPERTYQUARRY_RELEASE_DISPATCH),1)
$(error authenticated release targets are internal; use scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py)
endif
ifneq ($(filter command line,$(origin MAKEFLAGS)),)
$(error authenticated release targets forbid command-line MAKEFLAGS overrides)
endif
override _PROPERTYQUARRY_MAKE_SHORT_FLAGS := $(filter-out -%,$(firstword $(MAKEFLAGS)))
override _PROPERTYQUARRY_UNSAFE_MAKE_FLAGS := $(strip $(foreach flag,n i q t,$(if $(findstring $(flag),$(_PROPERTYQUARRY_MAKE_SHORT_FLAGS)),$(flag))))
ifneq ($(_PROPERTYQUARRY_UNSAFE_MAKE_FLAGS),)
$(error authenticated release targets forbid dry-run, ignore-errors, question, and touch modes)
endif
ifneq ($(filter --dry-run --just-print --recon --ignore-errors --question --touch,$(MAKEFLAGS)),)
$(error authenticated release targets forbid dry-run, ignore-errors, question, and touch modes)
endif
endif

.PHONY: deploy deploy-legacy-ea-stack deploy-memory deploy-bootstrap bootstrap db-status db-size db-retention smoke-api smoke-api-authenticated smoke-api-tibor smoke-postgres smoke-postgres-legacy smoke-help smoke-help-authenticated release-smoke release-smoke-authenticated release-preflight propertyquarry-release-preflight propertyquarry-release-protocol-contracts propertyquarry-native-release-control-gates bootstrap-propertyquarry-release-python release-docs test-api test-api-real-chromium test-api-authenticated test-all propertyquarry-target-recovery-canary test-postgres-contracts test-telegram-bot openapi-export openapi-diff openapi-prune endpoints version-info operator-summary operator-help operator-help-authenticated provider-readiness overlay-vision-check overlay-vision-pull support-bundle tasks-archive tasks-archive-prune tasks-archive-dry-run materialize-release-assets materialize-release-assets-authenticated verify-generated-release-artifacts-clean verify-generated-release-artifacts-clean-authenticated ci-local ci-local-authenticated ci-gates ci-gates-authenticated ci-gates-postgres ci-gates-postgres-legacy hard-exit-gates runtime-hard-exit-gates property-release-gates property-security-posture ltd-release-gates verify-release-assets verify-release-assets-authenticated verify-flagship-release-readiness verify-flagship-release-readiness-authenticated verify-pocket-audio-archive verify-ltd-critical-entries verify-ltd-critical-entries-authenticated verify-ltd-flagship-subset verify-ltd-flagship-subset-authenticated verify-design-mirror-bundle verify-design-full-mirror-parity repair-design-mirror-bundle docs-verify all-local

PYTHON_BIN ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
override PYTHONDONTWRITEBYTECODE := 1
export PYTHONDONTWRITEBYTECODE
PYTEST_PYTHON_BIN ?= $(shell if [ -x .venv/bin/python ] && .venv/bin/python -c 'import pytest' >/dev/null 2>&1; then printf '%s' .venv/bin/python; elif command -v python3 >/dev/null 2>&1 && python3 -c 'import pytest' >/dev/null 2>&1; then command -v python3; elif [ -x scripts/propertyquarry_release_python.sh ] && scripts/propertyquarry_release_python.sh -c 'import pytest' >/dev/null 2>&1; then printf '%s' ./scripts/propertyquarry_release_python.sh; else printf '%s' python3; fi)
TEST_API_PYTEST_IGNORE ?= --ignore-glob=tests/test_chummer*.py --ignore-glob=tests/test_next90*.py --ignore=tests/test_design_mirror_bundle_contracts.py
TEST_API_PYTEST_DESELECT ?= \
	--deselect=tests/test_responses_api_contracts.py::test_tool_shim_direct_operator_unblock_hotspot_does_not_restart_from_new_shard_after_repo_diff \
	--deselect=tests/test_responses_api_contracts.py::test_tool_shim_decision_prefers_operator_repo_diff_followup_over_prompt_hotspot_after_shard_telemetry \
	--deselect=tests/test_responses_api_contracts.py::test_tool_shim_decision_prefers_operator_repo_hunks_after_repo_diff_followup \
	--deselect=tests/test_responses_api_contracts.py::test_tool_shim_decision_prefers_operator_verify_after_repo_hunks \
	--deselect=tests/test_responses_api_contracts.py::test_tool_shim_decision_prefers_operator_provider_health_after_verify \
	--deselect=tests/test_responses_api_contracts.py::test_tool_shim_decision_prefers_operator_live_routing_hotspots_after_provider_health \
	--deselect=tests/test_responses_api_contracts.py::test_tool_shim_direct_nested_post_staged_command_builds_repo_diff_after_allowed_worker_reads \
	--deselect=tests/test_responses_api_contracts.py::test_tool_shim_direct_nested_telemetry_first_command_uses_allowed_fleet_source_paths_from_runtime_json \
	--deselect=tests/test_responses_api_contracts.py::test_tool_shim_direct_nested_telemetry_first_command_survives_prompt_truncation_when_history_marks_operator_unblock \
	--deselect=tests/test_responses_api_contracts.py::test_tool_shim_direct_nested_telemetry_first_command_skips_equivalent_var_lib_telemetry_after_prompt_read \
	--deselect=tests/test_responses_api_contracts.py::test_tool_shim_direct_nested_telemetry_first_command_ignores_non_fleet_task_logs_and_repo_worklists \
	--deselect=tests/test_responses_api_contracts.py::test_tool_shim_build_staged_repo_diff_command_groups_existing_paths \
	--deselect=tests/test_responses_api_contracts.py::test_local_fleet_runtime_helpers_cover_output_token_and_command_selection \
	--deselect=tests/e2e/test_propertyquarry_target_recovery_canary.py::test_property_target_recovery_canary_under_tibor

deploy:
	./scripts/deploy_propertyquarry.sh

deploy-legacy-ea-stack:
	PROPERTYQUARRY_USE_LEGACY_STACK=1 bash scripts/deploy.sh

deploy-memory:
	PROPERTYQUARRY_USE_LEGACY_STACK=1 EA_MEMORY_ONLY=1 bash scripts/deploy.sh

deploy-bootstrap:
	PROPERTYQUARRY_USE_LEGACY_STACK=1 EA_BOOTSTRAP_DB=1 bash scripts/deploy.sh

bootstrap:
	bash scripts/db_bootstrap.sh

db-status:
	bash scripts/db_status.sh

db-size:
	bash scripts/db_size.sh

db-retention:
	bash scripts/db_retention.sh

smoke-api:
	bash scripts/smoke_api.sh

smoke-api-authenticated:
	/bin/bash -p scripts/smoke_api.sh

smoke-api-tibor:
	bash scripts/smoke_api_tibor.sh

smoke-postgres:
	bash scripts/smoke_postgres.sh

smoke-postgres-legacy:
	bash scripts/smoke_postgres.sh --legacy-fixture

smoke-help:
	bash scripts/smoke_help.sh

smoke-help-authenticated:
	/bin/bash -p scripts/smoke_help.sh --authenticated

release-smoke: smoke-help smoke-api

release-smoke-authenticated: smoke-help-authenticated smoke-api-authenticated

release-preflight:
	$(MAKE) verify-release-assets-authenticated
	$(MAKE) verify-flagship-release-readiness-authenticated
	$(MAKE) verify-generated-release-artifacts-clean-authenticated
	$(MAKE) operator-help-authenticated
	$(MAKE) release-smoke-authenticated

propertyquarry-release-preflight:
	$(MAKE) release-preflight
	$(MAKE) property-release-gates

propertyquarry-release-protocol-contracts:
	./scripts/propertyquarry_release_python.sh -m pytest -q tests/test_property_release_protocol_contracts.py tests/test_property_deploy_handoff_adversarial.py tests/test_property_deploy_operator_contracts.py tests/test_propertyquarry_release_gate_entrypoints.py

propertyquarry-native-release-control-gates:
	./scripts/propertyquarry_release_python.sh scripts/propertyquarry_native_release_control_gates.py

bootstrap-propertyquarry-release-python:
	./scripts/bootstrap_propertyquarry_release_python.sh

release-docs:
	$(MAKE) docs-verify
	$(MAKE) operator-help

test-api:
	@set -eu; \
	propertyquarry_ci_temp="$$(/usr/bin/mktemp -d /tmp/propertyquarry-ci-test.XXXXXXXX)"; \
	cleanup_propertyquarry_ci_temp() { \
	  case "$$propertyquarry_ci_temp" in \
	    /tmp/propertyquarry-ci-test.*) /bin/rm -rf -- "$$propertyquarry_ci_temp" ;; \
	    *) echo "refusing unsafe CI temp cleanup: $$propertyquarry_ci_temp" >&2; return 70 ;; \
	  esac; \
	}; \
	trap cleanup_propertyquarry_ci_temp EXIT; \
	PROPERTYQUARRY_GATE_RECEIPT_DIR="$$propertyquarry_ci_temp/exit-gate" PYTHONPATH=ea EA_STORAGE_BACKEND=memory $(PYTEST_PYTHON_BIN) -m pytest -q tests -p no:cacheprovider --durations=25 --durations-min=1.0 $(TEST_API_PYTEST_IGNORE) $(TEST_API_PYTEST_DESELECT)

test-api-real-chromium: test-api
test-api-real-chromium: export PROPERTYQUARRY_REQUIRE_REAL_CHROMIUM_INTEGRATION := 1

test-api-authenticated:
	@set -eu; \
	propertyquarry_ci_temp="$$(/usr/bin/mktemp -d /tmp/propertyquarry-ci-test.XXXXXXXX)"; \
	cleanup_propertyquarry_ci_temp() { \
	  case "$$propertyquarry_ci_temp" in \
	    /tmp/propertyquarry-ci-test.*) /bin/rm -rf -- "$$propertyquarry_ci_temp" ;; \
	    *) echo "refusing unsafe CI temp cleanup: $$propertyquarry_ci_temp" >&2; return 70 ;; \
	  esac; \
	}; \
	trap cleanup_propertyquarry_ci_temp EXIT; \
	PROPERTYQUARRY_GATE_RECEIPT_DIR="$$propertyquarry_ci_temp/exit-gate" /usr/bin/env -u PROPERTYQUARRY_RELEASE_DISPATCH EA_STORAGE_BACKEND=memory ./scripts/propertyquarry_release_python.sh -m pytest -q tests -p no:cacheprovider --durations=25 --durations-min=1.0 $(TEST_API_PYTEST_IGNORE) $(TEST_API_PYTEST_DESELECT)

test-all:
	PYTHONPATH=ea $(PYTEST_PYTHON_BIN) -m pytest -q

# This canary observes live provider and Fleet repair state. Keep it fail-visible,
# but outside deterministic ci-gates so external volatility cannot mask regressions.
propertyquarry-target-recovery-canary:
	PYTHONPATH=ea $(PYTEST_PYTHON_BIN) -m pytest -q tests/e2e/test_propertyquarry_target_recovery_canary.py::test_property_target_recovery_canary_under_tibor

test-postgres-contracts:
	bash scripts/test_postgres_contracts.sh

test-telegram-bot:
	PYTHONPATH=ea EA_STORAGE_BACKEND=memory $(PYTEST_PYTHON_BIN) -m pytest -q tests/e2e/test_telegram_bot_workflows.py tests/e2e/test_telegram_bot_outbound_workflows.py

openapi-export:
	bash scripts/export_openapi.sh

openapi-diff:
	bash scripts/diff_openapi.sh

openapi-prune:
	bash scripts/prune_openapi.sh

endpoints:
	bash scripts/list_endpoints.sh

version-info:
	bash scripts/version_info.sh

operator-summary:
	bash scripts/operator_summary.sh

provider-readiness:
	$(PYTHON_BIN) scripts/chummer6_provider_readiness.py

operator-help:
	@for s in scripts/deploy_propertyquarry.sh scripts/deploy.sh scripts/db_bootstrap.sh scripts/db_status.sh scripts/db_size.sh scripts/db_retention.sh scripts/smoke_api.sh scripts/smoke_help.sh scripts/smoke_postgres.sh scripts/test_postgres_contracts.sh scripts/hard_exit_gates.sh scripts/runtime_hard_exit_gates.sh scripts/property_release_gates.sh scripts/propertyquarry_native_release_control_gates.py scripts/propertyquarry_release_make_dispatch.py scripts/verify_ltd_critical_entries.py scripts/verify_ltd_flagship_subset.py scripts/list_endpoints.sh scripts/version_info.sh scripts/export_openapi.sh scripts/diff_openapi.sh scripts/prune_openapi.sh scripts/operator_summary.sh scripts/support_bundle.sh scripts/archive_tasks.sh scripts/bootstrap_payfunnels_propertyquarry.py scripts/bootstrap_emailit_propertyquarry.py scripts/validate_propertyquarry_release_protocol.py scripts/verify_release_assets.sh scripts/chummer6_overlay_vision_readiness.py; do \
	  echo "===== $$s --help ====="; \
	  case "$$s" in \
	    scripts/deploy_propertyquarry.sh) ./scripts/deploy_propertyquarry.sh --help ;; \
	    *.py) $(PYTHON_BIN) $$s --help ;; \
	    *) /bin/bash -p $$s --help ;; \
	  esac; \
	  echo; \
	done

operator-help-authenticated:
	@for s in scripts/deploy_propertyquarry.sh scripts/deploy.sh scripts/db_bootstrap.sh scripts/db_status.sh scripts/db_size.sh scripts/db_retention.sh scripts/smoke_api.sh scripts/smoke_help.sh scripts/smoke_postgres.sh scripts/test_postgres_contracts.sh scripts/hard_exit_gates.sh scripts/runtime_hard_exit_gates.sh scripts/property_release_gates.sh scripts/propertyquarry_native_release_control_gates.py scripts/propertyquarry_release_make_dispatch.py scripts/verify_ltd_critical_entries.py scripts/verify_ltd_flagship_subset.py scripts/list_endpoints.sh scripts/version_info.sh scripts/export_openapi.sh scripts/diff_openapi.sh scripts/prune_openapi.sh scripts/operator_summary.sh scripts/support_bundle.sh scripts/archive_tasks.sh scripts/bootstrap_payfunnels_propertyquarry.py scripts/bootstrap_emailit_propertyquarry.py scripts/validate_propertyquarry_release_protocol.py scripts/verify_release_assets.sh scripts/chummer6_overlay_vision_readiness.py; do \
	  echo "===== $$s --help ====="; \
	  case "$$s" in \
	    scripts/deploy_propertyquarry.sh) ./scripts/deploy_propertyquarry.sh --help ;; \
	    *.py) ./scripts/propertyquarry_release_python.sh $$s --help ;; \
	    *) /bin/bash -p $$s --help ;; \
	  esac; \
	  echo; \
	done

overlay-vision-check:
	$(PYTHON_BIN) scripts/chummer6_overlay_vision_readiness.py

overlay-vision-pull:
	$(PYTHON_BIN) scripts/chummer6_overlay_vision_readiness.py --pull

support-bundle:
	bash scripts/support_bundle.sh

tasks-archive:
	bash scripts/archive_tasks.sh

tasks-archive-prune:
	bash scripts/archive_tasks.sh --prune-done

tasks-archive-dry-run:
	bash scripts/archive_tasks.sh --dry-run

materialize-release-assets:
	$(PYTHON_BIN) scripts/materialize_ea_browser_workflow_proof.py
	$(PYTHON_BIN) scripts/materialize_ea_flagship_release_gate.py
	$(PYTHON_BIN) scripts/materialize_weekly_product_pulse.py

materialize-release-assets-authenticated:
	./scripts/propertyquarry_release_python.sh scripts/materialize_ea_browser_workflow_proof.py
	./scripts/propertyquarry_release_python.sh scripts/materialize_ea_flagship_release_gate.py
	./scripts/propertyquarry_release_python.sh scripts/materialize_weekly_product_pulse.py

verify-generated-release-artifacts-clean:
	$(PYTHON_BIN) scripts/verify_generated_release_artifacts_clean.py

verify-generated-release-artifacts-clean-authenticated:
	./scripts/propertyquarry_release_python.sh scripts/verify_generated_release_artifacts_clean.py

ci-local:
	$(PYTHON_BIN) scripts/propertyquarry_compileall_clean.py
	bash scripts/smoke_help.sh

ci-local-authenticated:
	./scripts/propertyquarry_release_python.sh scripts/propertyquarry_compileall_clean.py
	$(MAKE) smoke-help-authenticated

# Read-only local mirror of the smoke-runtime CI verification order.
# Generator reproduction remains an explicit, separate workflow step.
ci-gates:
	$(MAKE) smoke-help
	$(MAKE) ci-local
	$(MAKE) test-api-real-chromium
	$(MAKE) verify-release-assets
	$(MAKE) verify-flagship-release-readiness
	$(MAKE) verify-generated-release-artifacts-clean

# Authenticated repository verification. This is dispatched outside GNU Make
# parsing and remains distinct from installed production launch authority.
ci-gates-authenticated:
	$(MAKE) smoke-help-authenticated
	$(MAKE) ci-local-authenticated
	$(MAKE) test-api-authenticated
	$(MAKE) verify-release-assets-authenticated
	$(MAKE) verify-flagship-release-readiness-authenticated
	$(MAKE) verify-generated-release-artifacts-clean-authenticated

ci-gates-postgres:
	$(MAKE) ci-gates
	$(MAKE) smoke-postgres

ci-gates-postgres-legacy:
	$(MAKE) ci-gates
	$(MAKE) smoke-postgres-legacy

hard-exit-gates: bootstrap-propertyquarry-release-python
	/bin/bash -p scripts/hard_exit_gates.sh

runtime-hard-exit-gates:
	bash scripts/runtime_hard_exit_gates.sh

property-release-gates:
	/bin/bash -p scripts/property_release_gates.sh

property-security-posture:
	./scripts/propertyquarry_release_python.sh scripts/check_property_security_posture.py

ltd-release-gates:
	$(MAKE) verify-ltd-critical-entries-authenticated
	$(MAKE) verify-ltd-flagship-subset-authenticated

verify-release-assets:
	PYTHON_BIN="$(PYTHON_BIN)" ./scripts/verify_release_assets.sh --developer

verify-release-assets-authenticated:
	/bin/bash -p scripts/verify_release_assets.sh

verify-flagship-release-readiness:
	$(PYTHON_BIN) scripts/verify_flagship_release_readiness.py

verify-flagship-release-readiness-authenticated:
	./scripts/propertyquarry_release_python.sh scripts/verify_flagship_release_readiness.py

verify-pocket-audio-archive:
	./scripts/propertyquarry_release_python.sh scripts/verify_pocket_audio_archive.py

verify-ltd-critical-entries:
	$(PYTHON_BIN) scripts/verify_ltd_critical_entries.py

verify-ltd-critical-entries-authenticated:
	./scripts/propertyquarry_release_python.sh scripts/verify_ltd_critical_entries.py

verify-ltd-flagship-subset:
	$(PYTHON_BIN) scripts/verify_ltd_flagship_subset.py

verify-ltd-flagship-subset-authenticated:
	./scripts/propertyquarry_release_python.sh scripts/verify_ltd_flagship_subset.py

verify-design-mirror-bundle:
	./scripts/propertyquarry_release_python.sh scripts/verify_design_mirror_bundle.py

verify-design-full-mirror-parity:
	./scripts/propertyquarry_release_python.sh scripts/verify_full_design_mirror_parity.py

repair-design-mirror-bundle:
	bash scripts/repair_design_mirror_bundle.sh

docs-verify: verify-release-assets
	$(PYTHON_BIN) scripts/check_docs_links.py

all-local: ci-local verify-release-assets verify-flagship-release-readiness verify-generated-release-artifacts-clean
