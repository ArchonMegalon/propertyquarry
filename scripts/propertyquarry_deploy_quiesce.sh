#!/usr/bin/env bash
# shellcheck shell=bash

# Fail-safe schema migration quiesce helpers for deploy_propertyquarry.sh.
#
# The caller supplies container_state_line() and the Docker Compose command
# array DC. Keeping the resolver and protocol together lets operator-contract
# tests exercise real project scoping, command ordering, and failure handling
# without touching Docker or a database.

declare -ag PROPERTYQUARRY_SCHEMA_QUIESCE_TARGET_SERVICES=()
declare -ag PROPERTYQUARRY_SCHEMA_QUIESCE_TARGET_CONTAINER_NAMES=()
declare -ag PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_SERVICES=()
declare -ag PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_CONTAINER_NAMES=()
declare -ag PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_CONTAINER_IDS=()
declare -ag PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_STATUSES=()
declare -ag PROPERTYQUARRY_POSTCOMMIT_HOLD_SERVICES=()
declare -ag PROPERTYQUARRY_POSTCOMMIT_HOLD_CONTAINER_NAMES=()
declare -ag PROPERTYQUARRY_ALLOWED_DATABASE_WRITER_CONTAINER_NAMES=()
declare -ag PROPERTYQUARRY_EXTERNAL_DATABASE_WRITER_IDS=()
declare -ag PROPERTYQUARRY_EXTERNAL_DATABASE_WRITER_NAMES=()

PROPERTYQUARRY_SCHEMA_QUIESCE_ARMED=0
PROPERTYQUARRY_SCHEMA_MIGRATION_COMMITTED=0
PROPERTYQUARRY_SCHEMA_QUIESCE_TIMEOUT_SECONDS=120
PROPERTYQUARRY_SCHEMA_RESTORE_TIMEOUT_SECONDS=180
PROPERTYQUARRY_SCHEMA_MIGRATION_SERVICE=""
PROPERTYQUARRY_SCHEMA_MIGRATION_CONTAINER_NAME=""
PROPERTYQUARRY_PUBLIC_INGRESS_HOLD_ARMED=0
PROPERTYQUARRY_PUBLIC_INGRESS_PROMOTED=0
PROPERTYQUARRY_PUBLIC_INGRESS_SERVICE=""
PROPERTYQUARRY_PUBLIC_INGRESS_CONTAINER_NAME=""

container_id_for_service() {
  local service="${1:-}"
  local output=""
  local candidate=""
  local ids=()
  if [[ $# -ne 2 || -z "${service}" ]]; then
    echo "container_id_for_service requires a Compose service and configured container name." >&2
    return 2
  fi

  # DC already contains the selected Compose project and file overlays. Never
  # search global Docker names here: an isolated candidate with no container
  # must not resolve the default live stack's identically named service.
  if ! output="$("${DC[@]}" ps --all --quiet "${service}" 2>/dev/null)"; then
    echo "Could not resolve project-scoped container for Compose service ${service}." >&2
    return 2
  fi
  while IFS= read -r candidate; do
    if [[ -n "${candidate}" ]]; then
      ids+=("${candidate}")
    fi
  done <<<"${output}"
  if (( ${#ids[@]} > 1 )); then
    echo "Compose service ${service} resolved to multiple containers; deploy requires an unambiguous target." >&2
    return 2
  fi
  if (( ${#ids[@]} == 1 )); then
    printf '%s' "${ids[0]}"
  fi
}

propertyquarry_schema_container_is_active() {
  local cid="$1"
  local state_line=""
  local status=""
  if [[ -z "${cid}" ]]; then
    return 1
  fi
  state_line="$(container_state_line "${cid}")"
  status="${state_line%%|*}"
  # Docker's terminal states are exited and dead. Paused, restarting,
  # removing, created, and unknown/unreadable states remain fail-closed as
  # active until Compose proves that they reached a terminal state.
  [[ "${status}" != "exited" && "${status}" != "dead" ]]
}

propertyquarry_schema_append_unique_target() {
  local service="$1"
  local container_name="$2"
  local existing=""
  for existing in "${PROPERTYQUARRY_SCHEMA_QUIESCE_TARGET_SERVICES[@]}"; do
    if [[ "${existing}" == "${service}" ]]; then
      return 0
    fi
  done
  PROPERTYQUARRY_SCHEMA_QUIESCE_TARGET_SERVICES+=("${service}")
  PROPERTYQUARRY_SCHEMA_QUIESCE_TARGET_CONTAINER_NAMES+=("${container_name}")
}

propertyquarry_append_unique_postcommit_hold_target() {
  local service="$1"
  local container_name="$2"
  local existing=""
  for existing in "${PROPERTYQUARRY_POSTCOMMIT_HOLD_SERVICES[@]}"; do
    if [[ "${existing}" == "${service}" ]]; then
      return 0
    fi
  done
  PROPERTYQUARRY_POSTCOMMIT_HOLD_SERVICES+=("${service}")
  PROPERTYQUARRY_POSTCOMMIT_HOLD_CONTAINER_NAMES+=("${container_name}")
}

propertyquarry_register_postcommit_hold_service() {
  local service="${1:-}"
  local container_name="${2:-}"
  if [[ -z "${service}" || -z "${container_name}" ]]; then
    echo "Post-commit hold registration requires a Compose service and container name." >&2
    return 2
  fi
  propertyquarry_append_unique_postcommit_hold_target "${service}" "${container_name}"
}

propertyquarry_register_public_ingress_hold() {
  local service="${1:-}"
  local container_name="${2:-}"
  if [[ -z "${service}" || -z "${container_name}" ]]; then
    echo "Public ingress hold registration requires a Compose service and container name." >&2
    return 2
  fi
  PROPERTYQUARRY_PUBLIC_INGRESS_SERVICE="${service}"
  PROPERTYQUARRY_PUBLIC_INGRESS_CONTAINER_NAME="${container_name}"
  PROPERTYQUARRY_PUBLIC_INGRESS_HOLD_ARMED=1
  PROPERTYQUARRY_PUBLIC_INGRESS_PROMOTED=0
}

propertyquarry_assert_public_ingress_inactive() {
  local original_cid="${1:-}"
  local current_cid=""
  if propertyquarry_schema_container_is_active "${original_cid}"; then
    echo "Public ingress hold failed: ${PROPERTYQUARRY_PUBLIC_INGRESS_SERVICE} container ${original_cid} is still active." >&2
    return 1
  fi
  if ! current_cid="$(container_id_for_service \
    "${PROPERTYQUARRY_PUBLIC_INGRESS_SERVICE}" \
    "${PROPERTYQUARRY_PUBLIC_INGRESS_CONTAINER_NAME}")"; then
    return 2
  fi
  if propertyquarry_schema_container_is_active "${current_cid}"; then
    echo "Public ingress hold failed: ${PROPERTYQUARRY_PUBLIC_INGRESS_SERVICE} has an active replacement container ${current_cid}." >&2
    return 1
  fi
}

propertyquarry_hold_public_ingress_stopped() {
  local original_cid=""
  if [[ "${PROPERTYQUARRY_PUBLIC_INGRESS_HOLD_ARMED}" != "1" ]]; then
    echo "Public ingress hold must be registered before it can be enforced." >&2
    return 2
  fi
  if ! original_cid="$(container_id_for_service \
    "${PROPERTYQUARRY_PUBLIC_INGRESS_SERVICE}" \
    "${PROPERTYQUARRY_PUBLIC_INGRESS_CONTAINER_NAME}")"; then
    return 2
  fi
  echo "Stopping and holding PropertyQuarry public ingress before writer quiesce." >&2
  if ! "${DC[@]}" stop --timeout "${PROPERTYQUARRY_SCHEMA_QUIESCE_TIMEOUT_SECONDS}" \
    "${PROPERTYQUARRY_PUBLIC_INGRESS_SERVICE}"; then
    echo "Could not stop PropertyQuarry public ingress; deploy will not touch schema writers." >&2
    return 1
  fi
  propertyquarry_assert_public_ingress_inactive "${original_cid}"
}

propertyquarry_mark_public_ingress_promoted() {
  local cid=""
  local state_line=""
  local status=""
  if [[ "${PROPERTYQUARRY_PUBLIC_INGRESS_HOLD_ARMED}" != "1" ]]; then
    echo "Cannot mark public ingress promoted before its hold is armed." >&2
    return 1
  fi
  if ! cid="$(container_id_for_service \
    "${PROPERTYQUARRY_PUBLIC_INGRESS_SERVICE}" \
    "${PROPERTYQUARRY_PUBLIC_INGRESS_CONTAINER_NAME}")"; then
    return 2
  fi
  if [[ -z "${cid}" ]]; then
    echo "Cannot mark public ingress promoted because its project-scoped container is absent." >&2
    return 1
  fi
  state_line="$(container_state_line "${cid}")"
  status="${state_line%%|*}"
  if [[ "${status}" != "running" ]]; then
    echo "Cannot mark public ingress promoted while ${PROPERTYQUARRY_PUBLIC_INGRESS_SERVICE} is ${status:-unknown}." >&2
    return 1
  fi
  PROPERTYQUARRY_PUBLIC_INGRESS_PROMOTED=1
}

propertyquarry_schema_capture_active_service() {
  local service="$1"
  local container_name="$2"
  local cid=""
  local state_line=""
  local status=""
  if ! cid="$(container_id_for_service "${service}" "${container_name}")"; then
    return 2
  fi
  if [[ -z "${cid}" ]]; then
    return 0
  fi
  state_line="$(container_state_line "${cid}")"
  status="${state_line%%|*}"
  if [[ "${status}" == "exited" || "${status}" == "dead" ]]; then
    return 0
  fi
  PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_SERVICES+=("${service}")
  PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_CONTAINER_NAMES+=("${container_name}")
  PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_CONTAINER_IDS+=("${cid}")
  PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_STATUSES+=("${status:-unknown}")
}

propertyquarry_database_writer_name_is_allowed() {
  local candidate="$1"
  local allowed=""
  for allowed in "${PROPERTYQUARRY_ALLOWED_DATABASE_WRITER_CONTAINER_NAMES[@]}"; do
    if [[ "${candidate}" == "${allowed}" ]]; then
      return 0
    fi
  done
  return 1
}

propertyquarry_capture_database_writer_inventory() {
  if [[ $# -ne 3 ]]; then
    echo "Database writer inventory requires the pinned API, worker, and scheduler container names." >&2
    return 2
  fi
  local api_container_name="$1"
  local worker_container_name="$2"
  local scheduler_container_name="$3"
  local inventory=""
  local line=""
  local cid=""
  local container_name=""
  local extra=""
  local seen_names=":"
  if ! declare -F database_writer_inventory_lines >/dev/null 2>&1; then
    echo "Authoritative database writer inventory callback is unavailable." >&2
    return 2
  fi
  if ! inventory="$(database_writer_inventory_lines)"; then
    echo "Authoritative database writer inventory could not be read; migration is blocked." >&2
    return 2
  fi
  PROPERTYQUARRY_EXTERNAL_DATABASE_WRITER_IDS=()
  PROPERTYQUARRY_EXTERNAL_DATABASE_WRITER_NAMES=()
  while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    IFS='|' read -r cid container_name extra <<<"${line}"
    if [[ -n "${extra}" || ! "${cid}" =~ ^[A-Za-z0-9_.:-]{1,128}$ || \
      ! "${container_name}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
      echo "Authoritative database writer inventory returned an invalid row." >&2
      return 2
    fi
    if [[ "${seen_names}" == *":${container_name}:"* ]]; then
      echo "Authoritative database writer inventory returned duplicate writer ${container_name}." >&2
      return 2
    fi
    seen_names="${seen_names}${container_name}:"
    if ! propertyquarry_database_writer_name_is_allowed "${container_name}"; then
      echo "Unknown database writer ${container_name} is active; migration is blocked before DDL." >&2
      return 1
    fi
    if [[ "${container_name}" != "${api_container_name}" && \
      "${container_name}" != "${worker_container_name}" && \
      "${container_name}" != "${scheduler_container_name}" ]]; then
      PROPERTYQUARRY_EXTERNAL_DATABASE_WRITER_IDS+=("${cid}")
      PROPERTYQUARRY_EXTERNAL_DATABASE_WRITER_NAMES+=("${container_name}")
    fi
  done <<<"${inventory}"
}

propertyquarry_stop_external_database_writers() {
  local index=0
  if (( ${#PROPERTYQUARRY_EXTERNAL_DATABASE_WRITER_IDS[@]} == 0 )); then
    return 0
  fi
  if ! declare -F stop_database_writer_container >/dev/null 2>&1 || \
    ! declare -F database_writer_container_is_active >/dev/null 2>&1; then
    echo "Cross-stack database writer stop/inspection callbacks are unavailable." >&2
    return 2
  fi
  for ((index = 0; index < ${#PROPERTYQUARRY_EXTERNAL_DATABASE_WRITER_IDS[@]}; index++)); do
    if ! stop_database_writer_container \
      "${PROPERTYQUARRY_EXTERNAL_DATABASE_WRITER_IDS[index]}" \
      "${PROPERTYQUARRY_SCHEMA_QUIESCE_TIMEOUT_SECONDS}"; then
      echo "Could not stop allowed cross-stack database writer ${PROPERTYQUARRY_EXTERNAL_DATABASE_WRITER_NAMES[index]}." >&2
      return 1
    fi
  done
  for ((index = 0; index < ${#PROPERTYQUARRY_EXTERNAL_DATABASE_WRITER_IDS[@]}; index++)); do
    if database_writer_container_is_active "${PROPERTYQUARRY_EXTERNAL_DATABASE_WRITER_IDS[index]}"; then
      echo "Cross-stack database writer ${PROPERTYQUARRY_EXTERNAL_DATABASE_WRITER_NAMES[index]} remained active." >&2
      return 1
    fi
  done
}

propertyquarry_assert_database_writer_inventory_empty() {
  local inventory=""
  local sessions=""
  if ! inventory="$(database_writer_inventory_lines)"; then
    echo "Immediate pre-DDL database writer revalidation failed closed." >&2
    return 2
  fi
  if [[ -n "${inventory}" ]]; then
    echo "A database writer appeared or remained active during immediate pre-DDL revalidation." >&2
    return 1
  fi
  if ! declare -F database_writer_session_inventory_lines >/dev/null 2>&1; then
    echo "Authoritative PostgreSQL session inventory callback is unavailable." >&2
    return 2
  fi
  if ! sessions="$(database_writer_session_inventory_lines)"; then
    echo "Immediate pre-DDL PostgreSQL session revalidation failed closed." >&2
    return 2
  fi
  if [[ -n "${sessions}" ]]; then
    echo "A non-migrator PostgreSQL client session remains active; migration is blocked." >&2
    return 1
  fi
}

propertyquarry_reconcile_incomplete_deploy_runtime() {
  if [[ $# -ne 11 ]]; then
    echo "Crash reconciliation requires the complete pinned API, worker, scheduler, render, and migration topology." >&2
    return 2
  fi
  local api_service="$1"
  local api_container_name="$2"
  local worker_service="$3"
  local worker_container_name="$4"
  local scheduler_service="$5"
  local scheduler_container_name="$6"
  local render_service="$7"
  local render_container_name="$8"
  local migration_service="$9"
  local migration_container_name="${10}"
  local timeout_seconds="${11}"
  local index=0
  if [[ "${PROPERTYQUARRY_PUBLIC_INGRESS_HOLD_ARMED}" != "1" ]]; then
    echo "Crash reconciliation requires the public ingress hold to be armed first." >&2
    return 2
  fi
  if ! [[ "${timeout_seconds}" =~ ^[1-9][0-9]*$ ]]; then
    echo "Crash reconciliation timeout must be a positive integer." >&2
    return 2
  fi
  PROPERTYQUARRY_SCHEMA_QUIESCE_TIMEOUT_SECONDS="${timeout_seconds}"
  PROPERTYQUARRY_SCHEMA_MIGRATION_SERVICE="${migration_service}"
  PROPERTYQUARRY_SCHEMA_MIGRATION_CONTAINER_NAME="${migration_container_name}"
  PROPERTYQUARRY_POSTCOMMIT_HOLD_SERVICES=()
  PROPERTYQUARRY_POSTCOMMIT_HOLD_CONTAINER_NAMES=()
  propertyquarry_append_unique_postcommit_hold_target "${api_service}" "${api_container_name}"
  propertyquarry_append_unique_postcommit_hold_target "${worker_service}" "${worker_container_name}"
  propertyquarry_append_unique_postcommit_hold_target "${scheduler_service}" "${scheduler_container_name}"
  propertyquarry_append_unique_postcommit_hold_target "${render_service}" "${render_container_name}"
  PROPERTYQUARRY_SCHEMA_QUIESCE_ARMED=1
  PROPERTYQUARRY_SCHEMA_MIGRATION_COMMITTED=1
  propertyquarry_hold_public_ingress_stopped || return $?
  # Stop the complete pinned runtime set before inventory classification.  A
  # controller crash can leave the pinned migrator active; inventory-first
  # would misclassify it or allow it to continue DDL while recovery aborted.
  if ! "${DC[@]}" stop --timeout "${timeout_seconds}" \
    "${api_service}" "${worker_service}" "${scheduler_service}" \
    "${render_service}" "${migration_service}"; then
    echo "Crash reconciliation could not stop every known runtime writer and migrator." >&2
    return 1
  fi
  propertyquarry_capture_database_writer_inventory \
    "${api_container_name}" "${worker_container_name}" \
    "${scheduler_container_name}" || return $?
  propertyquarry_stop_external_database_writers || return $?
  for ((index = 0; index < ${#PROPERTYQUARRY_POSTCOMMIT_HOLD_SERVICES[@]}; index++)); do
    propertyquarry_schema_assert_service_inactive \
      "${PROPERTYQUARRY_POSTCOMMIT_HOLD_SERVICES[index]}" \
      "${PROPERTYQUARRY_POSTCOMMIT_HOLD_CONTAINER_NAMES[index]}" || return 1
  done
  propertyquarry_schema_assert_service_inactive \
    "${migration_service}" "${migration_container_name}" || return 1
  local inventory=""
  if ! inventory="$(database_writer_inventory_lines)" || [[ -n "${inventory}" ]]; then
    echo "Crash reconciliation could not prove Docker database writers contained." >&2
    return 1
  fi
}

propertyquarry_complete_crash_reconciliation() {
  if [[ "${PROPERTYQUARRY_SCHEMA_QUIESCE_ARMED}" != "1" || \
    "${PROPERTYQUARRY_SCHEMA_MIGRATION_COMMITTED}" != "1" ]]; then
    echo "Crash reconciliation is not armed." >&2
    return 2
  fi
  propertyquarry_assert_database_writer_inventory_empty || return $?
  PROPERTYQUARRY_SCHEMA_QUIESCE_ARMED=0
  PROPERTYQUARRY_SCHEMA_MIGRATION_COMMITTED=0
}

propertyquarry_schema_assert_service_inactive() {
  local service="$1"
  local container_name="$2"
  local original_cid="${3:-}"
  local current_cid=""
  if propertyquarry_schema_container_is_active "${original_cid}"; then
    echo "Schema migration quiesce failed: ${service} container ${original_cid} is still active." >&2
    return 1
  fi
  if ! current_cid="$(container_id_for_service "${service}" "${container_name}")"; then
    return 2
  fi
  if propertyquarry_schema_container_is_active "${current_cid}"; then
    echo "Schema migration quiesce failed: ${service} has an active replacement container ${current_cid}." >&2
    return 1
  fi
  return 0
}

propertyquarry_quiesce_schema_writers() {
  if [[ $# -ne 12 ]]; then
    echo "Schema quiesce requires the complete pinned API, worker, scheduler, render, and migration topology." >&2
    return 2
  fi
  local api_service="$1"
  local api_container_name="$2"
  local worker_service="$3"
  local worker_container_name="$4"
  local scheduler_service="$5"
  local scheduler_container_name="$6"
  local render_service="$7"
  local render_container_name="$8"
  local migration_service="$9"
  local migration_container_name="${10}"
  local quiesce_timeout_seconds="${11}"
  local restore_timeout_seconds="${12}"
  local index=0

  if [[ "${PROPERTYQUARRY_SCHEMA_QUIESCE_ARMED}" == "1" ]]; then
    echo "PropertyQuarry schema migration quiesce is already armed." >&2
    return 1
  fi
  if ! [[ "${quiesce_timeout_seconds}" =~ ^[1-9][0-9]*$ ]]; then
    echo "PROPERTYQUARRY_MIGRATION_QUIESCE_TIMEOUT_SECONDS must be a positive integer." >&2
    return 2
  fi
  if ! [[ "${restore_timeout_seconds}" =~ ^[1-9][0-9]*$ ]]; then
    echo "PROPERTYQUARRY_MIGRATION_RESTORE_TIMEOUT_SECONDS must be a positive integer." >&2
    return 2
  fi

  PROPERTYQUARRY_SCHEMA_QUIESCE_TARGET_SERVICES=()
  PROPERTYQUARRY_SCHEMA_QUIESCE_TARGET_CONTAINER_NAMES=()
  PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_SERVICES=()
  PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_CONTAINER_NAMES=()
  PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_CONTAINER_IDS=()
  PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_STATUSES=()
  PROPERTYQUARRY_POSTCOMMIT_HOLD_SERVICES=()
  PROPERTYQUARRY_POSTCOMMIT_HOLD_CONTAINER_NAMES=()
  PROPERTYQUARRY_EXTERNAL_DATABASE_WRITER_IDS=()
  PROPERTYQUARRY_EXTERNAL_DATABASE_WRITER_NAMES=()
  PROPERTYQUARRY_SCHEMA_MIGRATION_COMMITTED=0
  PROPERTYQUARRY_SCHEMA_QUIESCE_TIMEOUT_SECONDS="${quiesce_timeout_seconds}"
  PROPERTYQUARRY_SCHEMA_RESTORE_TIMEOUT_SECONDS="${restore_timeout_seconds}"
  PROPERTYQUARRY_SCHEMA_MIGRATION_SERVICE="${migration_service}"
  PROPERTYQUARRY_SCHEMA_MIGRATION_CONTAINER_NAME="${migration_container_name}"

  propertyquarry_schema_append_unique_target "${api_service}" "${api_container_name}"
  propertyquarry_schema_append_unique_target "${worker_service}" "${worker_container_name}"
  propertyquarry_schema_append_unique_target "${scheduler_service}" "${scheduler_container_name}"
  propertyquarry_schema_append_unique_target "${render_service}" "${render_container_name}"
  propertyquarry_append_unique_postcommit_hold_target "${api_service}" "${api_container_name}"
  propertyquarry_append_unique_postcommit_hold_target "${worker_service}" "${worker_container_name}"
  propertyquarry_append_unique_postcommit_hold_target "${scheduler_service}" "${scheduler_container_name}"
  propertyquarry_append_unique_postcommit_hold_target "${render_service}" "${render_container_name}"
  propertyquarry_capture_database_writer_inventory \
    "${api_container_name}" "${worker_container_name}" \
    "${scheduler_container_name}" || return $?
  propertyquarry_schema_capture_active_service "${api_service}" "${api_container_name}" || return $?
  if [[ "${worker_service}" != "${api_service}" ]]; then
    propertyquarry_schema_capture_active_service "${worker_service}" "${worker_container_name}" || return $?
  fi
  if [[ "${scheduler_service}" != "${api_service}" && \
    "${scheduler_service}" != "${worker_service}" ]]; then
    propertyquarry_schema_capture_active_service "${scheduler_service}" "${scheduler_container_name}" || return $?
  fi
  if [[ "${render_service}" != "${api_service}" && \
    "${render_service}" != "${worker_service}" && \
    "${render_service}" != "${scheduler_service}" ]]; then
    propertyquarry_schema_capture_active_service "${render_service}" "${render_container_name}" || return $?
  fi

  # Arm before the first stop.  If Compose stops only part of the target set,
  # the EXIT handler restores the complete pre-migration state.
  PROPERTYQUARRY_SCHEMA_QUIESCE_ARMED=1
  if (( ${#PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_SERVICES[@]} == 0 )); then
    echo "No target API, worker, scheduler, or render containers were active before schema migration quiesce." >&2
  else
    echo "Quiescing target API, worker, scheduler, render, and allowed cross-stack writers before governed schema migration." >&2
  fi

  # Stop the complete target set, including a role that was absent during the
  # snapshot. This closes the capture-to-stop race without changing which
  # containers are eligible for pre-commit restoration.
  if ! "${DC[@]}" stop --timeout "${quiesce_timeout_seconds}" \
    "${PROPERTYQUARRY_SCHEMA_QUIESCE_TARGET_SERVICES[@]}"; then
    echo "Could not stop every pre-migration PropertyQuarry schema writer." >&2
    return 1
  fi
  propertyquarry_stop_external_database_writers || return $?

  for ((index = 0; index < ${#PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_SERVICES[@]}; index++)); do
    propertyquarry_schema_assert_service_inactive \
      "${PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_SERVICES[index]}" \
      "${PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_CONTAINER_NAMES[index]}" \
      "${PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_CONTAINER_IDS[index]}" || return 1
  done
  for ((index = 0; index < ${#PROPERTYQUARRY_SCHEMA_QUIESCE_TARGET_SERVICES[@]}; index++)); do
    propertyquarry_schema_assert_service_inactive \
      "${PROPERTYQUARRY_SCHEMA_QUIESCE_TARGET_SERVICES[index]}" \
      "${PROPERTYQUARRY_SCHEMA_QUIESCE_TARGET_CONTAINER_NAMES[index]}" || return 1
  done
  # This second authoritative inventory is the final operation before the
  # caller may start the migrator. It catches replacement and cross-stack
  # writers that appeared after the initial snapshot.
  propertyquarry_assert_database_writer_inventory_empty
}

propertyquarry_abort_active_schema_migration() {
  local cid=""
  if ! cid="$(container_id_for_service \
    "${PROPERTYQUARRY_SCHEMA_MIGRATION_SERVICE}" \
    "${PROPERTYQUARRY_SCHEMA_MIGRATION_CONTAINER_NAME}")"; then
    return 2
  fi
  if ! propertyquarry_schema_container_is_active "${cid}"; then
    return 0
  fi

  echo "Stopping the active schema migrator before any pre-migration runtime restoration." >&2
  if ! "${DC[@]}" stop --timeout "${PROPERTYQUARRY_SCHEMA_QUIESCE_TIMEOUT_SECONDS}" \
    "${PROPERTYQUARRY_SCHEMA_MIGRATION_SERVICE}"; then
    echo "Could not stop the active PropertyQuarry schema migrator; old writers will remain stopped." >&2
    return 1
  fi
  propertyquarry_schema_assert_service_inactive \
    "${PROPERTYQUARRY_SCHEMA_MIGRATION_SERVICE}" \
    "${PROPERTYQUARRY_SCHEMA_MIGRATION_CONTAINER_NAME}" \
    "${cid}"
}

propertyquarry_wait_for_restored_schema_writer() {
  local service="$1"
  local container_name="$2"
  local deadline=$((SECONDS + PROPERTYQUARRY_SCHEMA_RESTORE_TIMEOUT_SECONDS))
  local cid=""
  local state_line=""
  local status=""
  local health=""
  while (( SECONDS < deadline )); do
    if ! cid="$(container_id_for_service "${service}" "${container_name}")"; then
      return 2
    fi
    if [[ -n "${cid}" ]]; then
      state_line="$(container_state_line "${cid}")"
      status="${state_line%%|*}"
      health="${state_line##*|}"
      if [[ "${status}" == "running" ]] && \
        [[ "${health}" == "healthy" || "${health}" == "none" ]]; then
        return 0
      fi
      if [[ "${status}" == "exited" || "${status}" == "dead" ]]; then
        break
      fi
    fi
    sleep 1
  done
  echo "Failed to restore pre-migration service ${service} to a ready state." >&2
  return 1
}

propertyquarry_restore_pre_migration_schema_writers() {
  local index=0
  local failed=0
  if ! propertyquarry_abort_active_schema_migration; then
    return 1
  fi
  if (( ${#PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_SERVICES[@]} == 0 )); then
    echo "Migration failed before commit; this target had no prior API, worker, scheduler, or render containers to restore." >&2
    return 0
  fi

  echo "Migration did not commit; restoring only API, worker, scheduler, and render containers that were running before quiesce." >&2
  for ((index = 0; index < ${#PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_SERVICES[@]}; index++)); do
    if [[ "${PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_STATUSES[index]}" != "running" && \
      "${PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_STATUSES[index]}" != "restarting" ]]; then
      echo "Not starting pre-quiesce ${PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_STATUSES[index]} service ${PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_SERVICES[index]}; recovery will not activate a prior non-running writer." >&2
      continue
    fi
    if ! "${DC[@]}" start "${PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_SERVICES[index]}"; then
      echo "Could not restart pre-migration service ${PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_SERVICES[index]}." >&2
      failed=1
      continue
    fi
    if ! propertyquarry_wait_for_restored_schema_writer \
      "${PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_SERVICES[index]}" \
      "${PROPERTYQUARRY_SCHEMA_QUIESCE_PREVIOUS_CONTAINER_NAMES[index]}"; then
      failed=1
    fi
  done
  return "${failed}"
}

propertyquarry_hold_candidate_schema_writers_stopped() {
  local index=0
  local failed=0
  echo "Migration committed but candidate promotion did not finish; holding every candidate runtime writer stopped." >&2
  if ! "${DC[@]}" stop --timeout "${PROPERTYQUARRY_SCHEMA_QUIESCE_TIMEOUT_SECONDS}" \
    "${PROPERTYQUARRY_POSTCOMMIT_HOLD_SERVICES[@]}"; then
    failed=1
  fi
  for ((index = 0; index < ${#PROPERTYQUARRY_POSTCOMMIT_HOLD_SERVICES[@]}; index++)); do
    if ! propertyquarry_schema_assert_service_inactive \
      "${PROPERTYQUARRY_POSTCOMMIT_HOLD_SERVICES[index]}" \
      "${PROPERTYQUARRY_POSTCOMMIT_HOLD_CONTAINER_NAMES[index]}"; then
      failed=1
    fi
  done
  if ! propertyquarry_stop_external_database_writers; then
    failed=1
  fi
  echo "Do not restart the previous image after committed DDL without an explicit schema compatibility review." >&2
  return "${failed}"
}

propertyquarry_mark_schema_migration_committed() {
  if [[ "${PROPERTYQUARRY_SCHEMA_QUIESCE_ARMED}" != "1" ]]; then
    echo "Cannot mark schema migration committed before the quiesce protocol is armed." >&2
    return 1
  fi
  PROPERTYQUARRY_SCHEMA_MIGRATION_COMMITTED=1
}

propertyquarry_finish_schema_quiesce() {
  if [[ "${PROPERTYQUARRY_PUBLIC_INGRESS_HOLD_ARMED}" == "1" && \
    "${PROPERTYQUARRY_PUBLIC_INGRESS_PROMOTED}" != "1" ]]; then
    echo "Cannot finish deploy quiesce before verified public ingress promotion." >&2
    return 1
  fi
  PROPERTYQUARRY_SCHEMA_QUIESCE_ARMED=0
  PROPERTYQUARRY_PUBLIC_INGRESS_HOLD_ARMED=0
  trap - EXIT HUP INT TERM
}

propertyquarry_schema_quiesce_exit_handler() {
  trap '' HUP INT TERM
  local exit_code="${1:-1}"
  local recovery_failed=0
  trap - EXIT
  if [[ "${PROPERTYQUARRY_SCHEMA_QUIESCE_ARMED}" != "1" && \
    "${PROPERTYQUARRY_PUBLIC_INGRESS_HOLD_ARMED}" != "1" ]]; then
    exit "${exit_code}"
  fi
  if [[ "${exit_code}" == "0" ]]; then
    echo "Deploy exited while the schema migration quiesce was still armed." >&2
    exit_code=1
  fi
  if [[ "${PROPERTYQUARRY_PUBLIC_INGRESS_HOLD_ARMED}" == "1" ]]; then
    propertyquarry_hold_public_ingress_stopped || recovery_failed=1
  fi
  if [[ "${PROPERTYQUARRY_SCHEMA_QUIESCE_ARMED}" == "1" ]]; then
    if [[ "${PROPERTYQUARRY_SCHEMA_MIGRATION_COMMITTED}" == "1" ]]; then
      propertyquarry_hold_candidate_schema_writers_stopped || recovery_failed=1
      if [[ "${recovery_failed}" == "0" ]] && \
        declare -F record_deploy_containment_journal >/dev/null 2>&1; then
        record_deploy_containment_journal || recovery_failed=1
      fi
    else
      propertyquarry_restore_pre_migration_schema_writers || recovery_failed=1
    fi
  fi
  if [[ "${recovery_failed}" == "1" ]]; then
    echo "CRITICAL: PropertyQuarry schema migration recovery did not reach its fail-safe target state." >&2
    exit_code=1
  fi
  exit "${exit_code}"
}

propertyquarry_install_schema_quiesce_traps() {
  trap 'propertyquarry_schema_quiesce_exit_handler $?' EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
}
