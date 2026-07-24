//go:build linux && amd64

package authority

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"
)

const (
	RunnerReservationPath                                = "/var/lib/propertyquarry-release-single-host-v2/runner-reservation.v2.json"
	RunnerPrerequisiteIntentPath                         = "/var/lib/propertyquarry-release-single-host-v2/runner-prerequisite-intent.v2.json"
	RunnerPrerequisiteApprovalPath                       = "/var/lib/propertyquarry-release-single-host-v2/runner-prerequisite-approval.v2.json"
	RunnerPrerequisiteIntentPathV3                       = "/var/lib/propertyquarry-release-single-host-v2/runner-prerequisite-intent.v3.json"
	RunnerPrerequisitePostAttemptPathV3                  = "/var/lib/propertyquarry-release-single-host-v2/runner-prerequisite-post-attempt.v3.json"
	RunnerPrerequisiteApprovalPathV3                     = "/var/lib/propertyquarry-release-single-host-v2/runner-prerequisite-approval.v3.json"
	runnerReservationSchema                              = "propertyquarry.release-control.single-host-runner-reservation.v2"
	runnerReservationSignatureDomain                     = "propertyquarry.release-control.single-host-runner-reservation-signature.v2\x00"
	runnerPrerequisiteIntentSchema                       = "propertyquarry.release-control.single-host-runner-prerequisite-intent.v2"
	runnerPrerequisiteIntentSignatureDomain              = "propertyquarry.release-control.single-host-runner-prerequisite-intent-signature.v2\x00"
	runnerPrerequisiteApprovalSchema                     = "propertyquarry.release-control.single-host-runner-prerequisite-approval.v2"
	runnerPrerequisiteApprovalSignatureDomain            = "propertyquarry.release-control.single-host-runner-prerequisite-approval-signature.v2\x00"
	runnerPrerequisiteIntentSchemaV3                     = "propertyquarry.release-control.single-host-runner-prerequisite-intent.v3"
	runnerPrerequisiteIntentSignatureDomainV3            = "propertyquarry.release-control.single-host-runner-prerequisite-intent-signature.v3\x00"
	runnerPrerequisiteApprovalSchemaV3                   = "propertyquarry.release-control.single-host-runner-prerequisite-approval.v3"
	runnerPrerequisiteApprovalSignatureDomainV3          = "propertyquarry.release-control.single-host-runner-prerequisite-approval-signature.v3\x00"
	runnerPrerequisitePostAttemptSchemaV3                = "propertyquarry.release-control.single-host-runner-prerequisite-post-attempt.v3"
	runnerPrerequisitePostAttemptSignatureDomainV3       = "propertyquarry.release-control.single-host-runner-prerequisite-post-attempt-signature.v3\x00"
	runnerPrerequisiteJob                                = "propertyquarry-protected-dispatch-inputs"
	runnerPrerequisiteJobKey                             = "propertyquarry-protected-dispatch-inputs"
	runnerReservationTTLSeconds                    int64 = 6 * 60 * 60
)

type runnerPrerequisiteProof struct {
	IntentDigest          string
	ApprovalDigest        string
	ApprovalPayloadDigest string
	JobID                 string
}

func validateInstalledRunnerPrerequisiteRecordsV2(root string, config *Config, public ed25519.PublicKey) (*runnerPrerequisiteProof, error) {
	if config == nil || len(public) != ed25519.PublicKeySize {
		return nil, fmt.Errorf("runner-prerequisite-input-invalid")
	}
	reservation, reservationRaw, err := readRunnerWire(root, RunnerReservationPath, 0o400, public, runnerReservationSignatureDomain)
	if err != nil {
		return nil, fmt.Errorf("runner-prerequisite-reservation-unavailable")
	}
	defer zero(reservationRaw)
	if !hasKeys(reservation,
		"authority_profile", "created_at_epoch", "environment", "expires_at_epoch", "receipt_authority_key_id",
		"release_job", "repository", "repository_id", "repository_owner_id", "reservation_nonce", "runner_label",
		"runner_label_nonce", "schema", "source_checkout_identity_sha256", "source_checkout_path", "source_tree_sha256",
		"version", "workflow_path", "workflow_ref", "workflow_sha",
	) {
		return nil, fmt.Errorf("runner-prerequisite-reservation-shape-invalid")
	}
	created, createdOK := exactInt(reservation["created_at_epoch"], 1, 1<<62)
	expires, expiresOK := exactInt(reservation["expires_at_epoch"], 1, 1<<62)
	nonce, nonceOK := exactString(reservation["reservation_nonce"])
	label, labelOK := exactString(reservation["runner_label"])
	sourceIdentity, sourceIdentityOK := exactString(reservation["source_checkout_identity_sha256"])
	sourcePath, sourcePathOK := exactString(reservation["source_checkout_path"])
	sourceTree, sourceTreeOK := exactString(reservation["source_tree_sha256"])
	version, versionOK := exactInt(reservation["version"], 2, 2)
	if reservation["schema"] != runnerReservationSchema || !versionOK || version != 2 || reservation["authority_profile"] != "single-host-production-v2" ||
		reservation["environment"] != Environment || reservation["repository"] != Repository || reservation["repository_id"] != RepositoryID ||
		reservation["repository_owner_id"] != RepositoryOwnerID || reservation["workflow_path"] != ".github/workflows/smoke-runtime.yml" ||
		reservation["workflow_ref"] != WorkflowRef || reservation["workflow_sha"] != config.WorkflowSHA || reservation["release_job"] != ReleaseJob ||
		reservation["receipt_authority_key_id"] != config.ReceiptAuthorityKeyID || !createdOK || !expiresOK || expires-created != runnerReservationTTLSeconds ||
		config.TransactionStartedAtEpoch < created || config.TransactionStartedAtEpoch > expires || !nonceOK || !runnerNoncePattern.MatchString(nonce) ||
		!labelOK || !runnerLabelPattern.MatchString(label) || label != config.RunnerLabel || reservation["runner_label_nonce"] != strings.TrimPrefix(label, "pqrelease-") ||
		!sourceIdentityOK || !digestPattern.MatchString(sourceIdentity) || !sourcePathOK || sourcePath != "/docker/property/state/runtime/propertyquarry-release-authority-v2.private/single-host-v2-release-checkouts/"+config.WorkflowSHA ||
		!sourceTreeOK || !digestPattern.MatchString(sourceTree) || digest(reservationRaw) != config.RunnerReservationDigest {
		return nil, fmt.Errorf("runner-prerequisite-reservation-binding-invalid")
	}
	nonceRaw, decodeErr := hex.DecodeString(nonce)
	if decodeErr != nil {
		return nil, fmt.Errorf("runner-prerequisite-reservation-nonce-invalid")
	}
	derivedInput := append([]byte(runnerLabelDerivationDomain), nonceRaw...)
	derived := sha256.Sum256(derivedInput)
	zero(nonceRaw)
	zero(derivedInput)
	if label != "pqrelease-"+hex.EncodeToString(derived[:16]) {
		return nil, fmt.Errorf("runner-prerequisite-reservation-label-invalid")
	}

	intent, intentRaw, err := readRunnerWire(root, RunnerPrerequisiteIntentPath, 0o400, public, runnerPrerequisiteIntentSignatureDomain)
	if err != nil {
		return nil, fmt.Errorf("runner-prerequisite-intent-unavailable")
	}
	defer zero(intentRaw)
	if !hasKeys(intent,
		"authority_profile", "comment", "discovered_at_epoch", "environment_id", "environment_name", "initial_jobs_sha256",
		"initial_pending_deployments_sha256", "initial_runs_index_sha256", "prerequisite_job_id", "prerequisite_job_name",
		"receipt_authority_key_id", "release_job", "repository", "repository_id", "repository_owner_id",
		"reservation_expires_at_epoch", "reservation_sha256", "run_attempt", "run_id", "runner_label", "schema", "version",
		"workflow_path", "workflow_ref", "workflow_sha",
	) {
		return nil, fmt.Errorf("runner-prerequisite-intent-shape-invalid")
	}
	discovered, discoveredOK := exactInt(intent["discovered_at_epoch"], 1, 1<<62)
	environmentID, environmentOK := exactString(intent["environment_id"])
	jobID, jobOK := exactString(intent["prerequisite_job_id"])
	runID, runOK := exactString(intent["run_id"])
	attempt, attemptOK := exactInt(intent["run_attempt"], 1, 1<<31-1)
	intentVersion, intentVersionOK := exactInt(intent["version"], 2, 2)
	if intent["schema"] != runnerPrerequisiteIntentSchema || !intentVersionOK || intentVersion != 2 || intent["authority_profile"] != "single-host-production-v2" ||
		intent["repository"] != Repository || intent["repository_id"] != RepositoryID || intent["repository_owner_id"] != RepositoryOwnerID ||
		intent["workflow_path"] != ".github/workflows/smoke-runtime.yml" || intent["workflow_ref"] != WorkflowRef || intent["workflow_sha"] != config.WorkflowSHA ||
		intent["receipt_authority_key_id"] != config.ReceiptAuthorityKeyID || intent["reservation_sha256"] != digest(reservationRaw) ||
		intent["reservation_expires_at_epoch"] != json.Number(fmt.Sprintf("%d", expires)) || intent["runner_label"] != label ||
		intent["environment_name"] != Environment || !environmentOK || !decimal(environmentID) || intent["prerequisite_job_name"] != runnerPrerequisiteJob ||
		!jobOK || !decimal(jobID) || jobID != config.RunnerPrerequisiteJobID || intent["release_job"] != ReleaseJob || !runOK || !decimal(runID) ||
		runID != config.RunnerRunID || !attemptOK || attempt != config.RunnerRunAttempt || !discoveredOK || discovered < created || discovered > expires ||
		intent["comment"] != "PropertyQuarry governed prerequisite approval "+digest(reservationRaw) || digest(intentRaw) != config.RunnerPrerequisiteIntentDigest {
		return nil, fmt.Errorf("runner-prerequisite-intent-binding-invalid")
	}
	for _, key := range []string{"initial_jobs_sha256", "initial_pending_deployments_sha256", "initial_runs_index_sha256"} {
		text, ok := exactString(intent[key])
		if !ok || !digestPattern.MatchString(text) {
			return nil, fmt.Errorf("runner-prerequisite-intent-evidence-invalid")
		}
	}

	approval, approvalRaw, err := readRunnerWire(root, RunnerPrerequisiteApprovalPath, 0o400, public, runnerPrerequisiteApprovalSignatureDomain)
	if err != nil {
		return nil, fmt.Errorf("runner-prerequisite-approval-unavailable")
	}
	defer zero(approvalRaw)
	if !hasKeys(approval,
		"approval_api_disposition", "approval_response_sha256", "approved_at_epoch", "completed_jobs_sha256", "environment_id",
		"environment_name", "intent_sha256", "post_pending_deployments_sha256", "prerequisite_conclusion", "prerequisite_job_id",
		"prerequisite_job_name", "receipt_authority_key_id", "release_job", "repository", "repository_id", "repository_owner_id",
		"reservation_expires_at_epoch", "reservation_sha256", "review_history_sha256", "run_attempt", "run_id", "runner_label",
		"schema", "version", "workflow_path", "workflow_ref", "workflow_sha",
	) {
		return nil, fmt.Errorf("runner-prerequisite-approval-shape-invalid")
	}
	approved, approvedOK := exactInt(approval["approved_at_epoch"], 1, 1<<62)
	approvalVersion, approvalVersionOK := exactInt(approval["version"], 2, 2)
	disposition, dispositionOK := exactString(approval["approval_api_disposition"])
	approvalPayloadRaw, payloadErr := canonicalJSON(approval)
	defer zero(approvalPayloadRaw)
	if approval["schema"] != runnerPrerequisiteApprovalSchema || !approvalVersionOK || approvalVersion != 2 || approval["intent_sha256"] != digest(intentRaw) ||
		approval["reservation_sha256"] != intent["reservation_sha256"] || approval["runner_label"] != intent["runner_label"] || approval["run_id"] != intent["run_id"] ||
		approval["run_attempt"] != intent["run_attempt"] || approval["prerequisite_job_id"] != intent["prerequisite_job_id"] ||
		approval["prerequisite_job_name"] != runnerPrerequisiteJob || approval["prerequisite_conclusion"] != "success" || approval["environment_id"] != intent["environment_id"] ||
		approval["environment_name"] != Environment || approval["receipt_authority_key_id"] != config.ReceiptAuthorityKeyID || approval["repository"] != Repository ||
		approval["repository_id"] != RepositoryID || approval["repository_owner_id"] != RepositoryOwnerID || approval["workflow_path"] != ".github/workflows/smoke-runtime.yml" ||
		approval["workflow_ref"] != WorkflowRef || approval["workflow_sha"] != config.WorkflowSHA || approval["release_job"] != ReleaseJob ||
		approval["reservation_expires_at_epoch"] != intent["reservation_expires_at_epoch"] || !dispositionOK ||
		(disposition != "approved" && disposition != "post-approved-recovered") || !approvedOK || approved < discovered || approved > expires ||
		approved > config.TransactionStartedAtEpoch || payloadErr != nil || digest(approvalRaw) != config.RunnerPrerequisiteApprovalDigest ||
		digest(approvalPayloadRaw) != config.RunnerPrerequisiteApprovalPayloadDigest {
		return nil, fmt.Errorf("runner-prerequisite-approval-binding-invalid")
	}
	if disposition == "approved" {
		response, ok := exactString(approval["approval_response_sha256"])
		if !ok || !digestPattern.MatchString(response) {
			return nil, fmt.Errorf("runner-prerequisite-approval-response-invalid")
		}
	} else if approval["approval_response_sha256"] != nil {
		return nil, fmt.Errorf("runner-prerequisite-approval-response-invalid")
	}
	for _, key := range []string{"completed_jobs_sha256", "post_pending_deployments_sha256", "review_history_sha256"} {
		text, ok := exactString(approval[key])
		if !ok || !digestPattern.MatchString(text) {
			return nil, fmt.Errorf("runner-prerequisite-approval-evidence-invalid")
		}
	}
	return &runnerPrerequisiteProof{
		IntentDigest: digest(intentRaw), ApprovalDigest: digest(approvalRaw),
		ApprovalPayloadDigest: digest(approvalPayloadRaw), JobID: jobID,
	}, nil
}

func runnerPrerequisiteJobName(runnerLabel, reservationDigest string) string {
	return runnerPrerequisiteJobKey + " | " + runnerLabel + " | " + reservationDigest
}

func validateInstalledRunnerPrerequisiteRecordsV3(root string, config *Config, public ed25519.PublicKey) (*runnerPrerequisiteProof, error) {
	if config == nil || len(public) != ed25519.PublicKeySize {
		return nil, fmt.Errorf("runner-prerequisite-input-invalid")
	}
	reservation, reservationRaw, err := readRunnerWire(root, RunnerReservationPath, 0o400, public, runnerReservationSignatureDomain)
	if err != nil {
		return nil, fmt.Errorf("runner-prerequisite-reservation-unavailable")
	}
	defer zero(reservationRaw)
	if !hasKeys(reservation,
		"authority_profile", "created_at_epoch", "environment", "expires_at_epoch", "receipt_authority_key_id",
		"release_job", "repository", "repository_id", "repository_owner_id", "reservation_nonce", "runner_label",
		"runner_label_nonce", "schema", "source_checkout_identity_sha256", "source_checkout_path", "source_tree_sha256",
		"version", "workflow_path", "workflow_ref", "workflow_sha",
	) {
		return nil, fmt.Errorf("runner-prerequisite-reservation-shape-invalid")
	}
	created, createdOK := exactInt(reservation["created_at_epoch"], 1, 1<<62)
	expires, expiresOK := exactInt(reservation["expires_at_epoch"], 1, 1<<62)
	nonce, nonceOK := exactString(reservation["reservation_nonce"])
	label, labelOK := exactString(reservation["runner_label"])
	sourceIdentity, sourceIdentityOK := exactString(reservation["source_checkout_identity_sha256"])
	sourcePath, sourcePathOK := exactString(reservation["source_checkout_path"])
	sourceTree, sourceTreeOK := exactString(reservation["source_tree_sha256"])
	version, versionOK := exactInt(reservation["version"], 2, 2)
	if reservation["schema"] != runnerReservationSchema || !versionOK || version != 2 || reservation["authority_profile"] != "single-host-production-v2" ||
		reservation["environment"] != Environment || reservation["repository"] != Repository || reservation["repository_id"] != RepositoryID ||
		reservation["repository_owner_id"] != RepositoryOwnerID || reservation["workflow_path"] != ".github/workflows/smoke-runtime.yml" ||
		reservation["workflow_ref"] != WorkflowRef || reservation["workflow_sha"] != config.WorkflowSHA || reservation["release_job"] != ReleaseJob ||
		reservation["receipt_authority_key_id"] != config.ReceiptAuthorityKeyID || !createdOK || !expiresOK || expires-created != runnerReservationTTLSeconds ||
		config.TransactionStartedAtEpoch < created || config.TransactionStartedAtEpoch > expires || !nonceOK || !runnerNoncePattern.MatchString(nonce) ||
		!labelOK || !runnerLabelPattern.MatchString(label) || label != config.RunnerLabel || reservation["runner_label_nonce"] != strings.TrimPrefix(label, "pqrelease-") ||
		!sourceIdentityOK || !digestPattern.MatchString(sourceIdentity) || !sourcePathOK || sourcePath != "/docker/property/state/runtime/propertyquarry-release-authority-v2.private/single-host-v2-release-checkouts/"+config.WorkflowSHA ||
		!sourceTreeOK || !digestPattern.MatchString(sourceTree) || digest(reservationRaw) != config.RunnerReservationDigest {
		return nil, fmt.Errorf("runner-prerequisite-reservation-binding-invalid")
	}
	nonceRaw, decodeErr := hex.DecodeString(nonce)
	if decodeErr != nil {
		return nil, fmt.Errorf("runner-prerequisite-reservation-nonce-invalid")
	}
	derivedInput := append([]byte(runnerLabelDerivationDomain), nonceRaw...)
	derived := sha256.Sum256(derivedInput)
	zero(nonceRaw)
	zero(derivedInput)
	if label != "pqrelease-"+hex.EncodeToString(derived[:16]) {
		return nil, fmt.Errorf("runner-prerequisite-reservation-label-invalid")
	}

	intent, intentRaw, err := readRunnerWire(root, RunnerPrerequisiteIntentPathV3, 0o400, public, runnerPrerequisiteIntentSignatureDomainV3)
	if err != nil {
		return nil, fmt.Errorf("runner-prerequisite-intent-unavailable")
	}
	defer zero(intentRaw)
	if !hasKeys(intent,
		"authority_profile", "comment", "discovered_at_epoch", "environment_id", "environment_name", "initial_jobs_sha256",
		"initial_pending_deployments_sha256", "initial_runs_index_sha256", "prerequisite_job_id", "prerequisite_job_key", "prerequisite_job_name",
		"receipt_authority_key_id", "release_job", "repository", "repository_id", "repository_owner_id",
		"reservation_expires_at_epoch", "reservation_sha256", "run_attempt", "run_id", "runner_label", "schema", "version",
		"workflow_path", "workflow_ref", "workflow_sha",
	) {
		return nil, fmt.Errorf("runner-prerequisite-intent-shape-invalid")
	}
	discovered, discoveredOK := exactInt(intent["discovered_at_epoch"], 1, 1<<62)
	environmentID, environmentOK := exactString(intent["environment_id"])
	jobID, jobOK := exactString(intent["prerequisite_job_id"])
	runID, runOK := exactString(intent["run_id"])
	attempt, attemptOK := exactInt(intent["run_attempt"], 1, 1<<31-1)
	intentVersion, intentVersionOK := exactInt(intent["version"], 3, 3)
	reservationDigest := digest(reservationRaw)
	expectedJobName := runnerPrerequisiteJobName(label, reservationDigest)
	if intent["schema"] != runnerPrerequisiteIntentSchemaV3 || !intentVersionOK || intentVersion != 3 || intent["authority_profile"] != "single-host-production-v2" ||
		intent["repository"] != Repository || intent["repository_id"] != RepositoryID || intent["repository_owner_id"] != RepositoryOwnerID ||
		intent["workflow_path"] != ".github/workflows/smoke-runtime.yml" || intent["workflow_ref"] != WorkflowRef || intent["workflow_sha"] != config.WorkflowSHA ||
		intent["receipt_authority_key_id"] != config.ReceiptAuthorityKeyID || intent["reservation_sha256"] != reservationDigest ||
		intent["reservation_expires_at_epoch"] != json.Number(fmt.Sprintf("%d", expires)) || intent["runner_label"] != label ||
		intent["environment_name"] != Environment || !environmentOK || !decimal(environmentID) || intent["prerequisite_job_key"] != runnerPrerequisiteJobKey ||
		intent["prerequisite_job_name"] != expectedJobName || !jobOK || !decimal(jobID) || jobID != config.RunnerPrerequisiteJobID ||
		intent["release_job"] != ReleaseJob || !runOK || !decimal(runID) || runID != config.RunnerRunID ||
		!attemptOK || attempt != config.RunnerRunAttempt || !discoveredOK || discovered < created || discovered > expires ||
		intent["comment"] != "PropertyQuarry governed prerequisite approval "+reservationDigest || digest(intentRaw) != config.RunnerPrerequisiteIntentDigest {
		return nil, fmt.Errorf("runner-prerequisite-intent-binding-invalid")
	}
	for _, key := range []string{"initial_jobs_sha256", "initial_pending_deployments_sha256", "initial_runs_index_sha256"} {
		text, ok := exactString(intent[key])
		if !ok || !digestPattern.MatchString(text) {
			return nil, fmt.Errorf("runner-prerequisite-intent-evidence-invalid")
		}
	}

	postAttempt, postAttemptRaw, err := readRunnerWire(root, RunnerPrerequisitePostAttemptPathV3, 0o400, public, runnerPrerequisitePostAttemptSignatureDomainV3)
	if err != nil {
		return nil, fmt.Errorf("runner-prerequisite-post-attempt-unavailable")
	}
	defer zero(postAttemptRaw)
	if !hasKeys(postAttempt,
		"attempted_at_epoch", "authority_profile", "comment", "environment_id", "environment_name", "github_api_path",
		"http_method", "intent_sha256", "pre_post_jobs_sha256", "pre_post_pending_deployments_count",
		"pre_post_pending_deployments_sha256", "pre_post_release_job_present", "pre_post_review_history_sha256",
		"pre_post_review_match_count", "pre_post_review_scope", "pre_post_run_sha256", "prerequisite_job_id",
		"prerequisite_job_key", "prerequisite_job_name", "receipt_authority_key_id", "repository", "repository_id",
		"repository_owner_id", "request_sha256", "reservation_expires_at_epoch", "reservation_sha256", "run_attempt",
		"run_id", "runner_label", "schema", "version", "workflow_path", "workflow_ref", "workflow_sha",
	) {
		return nil, fmt.Errorf("runner-prerequisite-post-attempt-shape-invalid")
	}
	attempted, attemptedOK := exactInt(postAttempt["attempted_at_epoch"], 1, 1<<62)
	postVersion, postVersionOK := exactInt(postAttempt["version"], 3, 3)
	pendingCount, pendingCountOK := exactInt(postAttempt["pre_post_pending_deployments_count"], 1, 1)
	reviewCount, reviewCountOK := exactInt(postAttempt["pre_post_review_match_count"], 0, 0)
	requestRaw, requestErr := canonicalJSON(map[string]any{
		"comment":         intent["comment"],
		"environment_ids": []any{json.Number(environmentID)},
		"state":           "approved",
	})
	defer zero(requestRaw)
	if postAttempt["schema"] != runnerPrerequisitePostAttemptSchemaV3 || !postVersionOK || postVersion != 3 ||
		postAttempt["authority_profile"] != "single-host-production-v2" || postAttempt["intent_sha256"] != digest(intentRaw) ||
		postAttempt["reservation_sha256"] != reservationDigest || postAttempt["reservation_expires_at_epoch"] != intent["reservation_expires_at_epoch"] ||
		postAttempt["runner_label"] != intent["runner_label"] || postAttempt["run_id"] != intent["run_id"] ||
		postAttempt["run_attempt"] != intent["run_attempt"] || postAttempt["prerequisite_job_id"] != intent["prerequisite_job_id"] ||
		postAttempt["prerequisite_job_key"] != intent["prerequisite_job_key"] || postAttempt["prerequisite_job_name"] != intent["prerequisite_job_name"] ||
		postAttempt["environment_id"] != intent["environment_id"] || postAttempt["environment_name"] != Environment ||
		postAttempt["receipt_authority_key_id"] != config.ReceiptAuthorityKeyID || postAttempt["repository"] != Repository ||
		postAttempt["repository_id"] != RepositoryID || postAttempt["repository_owner_id"] != RepositoryOwnerID ||
		postAttempt["workflow_path"] != ".github/workflows/smoke-runtime.yml" || postAttempt["workflow_ref"] != WorkflowRef ||
		postAttempt["workflow_sha"] != config.WorkflowSHA || postAttempt["http_method"] != "POST" ||
		postAttempt["github_api_path"] != "/repos/ArchonMegalon/propertyquarry/actions/runs/"+runID+"/pending_deployments" ||
		postAttempt["comment"] != intent["comment"] || requestErr != nil || postAttempt["request_sha256"] != digest(requestRaw) ||
		postAttempt["pre_post_release_job_present"] != false || !pendingCountOK || pendingCount != 1 ||
		!reviewCountOK || reviewCount != 0 || postAttempt["pre_post_review_scope"] != "any-approved-target-environment" ||
		!attemptedOK || attempted < discovered || attempted > expires {
		return nil, fmt.Errorf("runner-prerequisite-post-attempt-binding-invalid")
	}
	for _, key := range []string{"pre_post_jobs_sha256", "pre_post_pending_deployments_sha256", "pre_post_review_history_sha256", "pre_post_run_sha256"} {
		text, ok := exactString(postAttempt[key])
		if !ok || !digestPattern.MatchString(text) {
			return nil, fmt.Errorf("runner-prerequisite-post-attempt-evidence-invalid")
		}
	}

	approval, approvalRaw, err := readRunnerWire(root, RunnerPrerequisiteApprovalPathV3, 0o400, public, runnerPrerequisiteApprovalSignatureDomainV3)
	if err != nil {
		return nil, fmt.Errorf("runner-prerequisite-approval-unavailable")
	}
	defer zero(approvalRaw)
	if !hasKeys(approval,
		"approval_api_disposition", "approval_response_sha256", "approved_at_epoch", "completed_jobs_sha256", "environment_id",
		"environment_name", "intent_sha256", "post_pending_deployments_sha256", "prerequisite_conclusion", "prerequisite_job_id",
		"prerequisite_job_key", "prerequisite_job_name", "receipt_authority_key_id", "release_job", "repository", "repository_id", "repository_owner_id",
		"reservation_expires_at_epoch", "reservation_sha256", "review_history_sha256", "run_attempt", "run_id", "runner_label",
		"schema", "version", "workflow_path", "workflow_ref", "workflow_sha",
	) {
		return nil, fmt.Errorf("runner-prerequisite-approval-shape-invalid")
	}
	approved, approvedOK := exactInt(approval["approved_at_epoch"], 1, 1<<62)
	approvalVersion, approvalVersionOK := exactInt(approval["version"], 3, 3)
	disposition, dispositionOK := exactString(approval["approval_api_disposition"])
	approvalPayloadRaw, payloadErr := canonicalJSON(approval)
	defer zero(approvalPayloadRaw)
	if approval["schema"] != runnerPrerequisiteApprovalSchemaV3 || !approvalVersionOK || approvalVersion != 3 || approval["intent_sha256"] != digest(intentRaw) ||
		approval["reservation_sha256"] != intent["reservation_sha256"] || approval["runner_label"] != intent["runner_label"] || approval["run_id"] != intent["run_id"] ||
		approval["run_attempt"] != intent["run_attempt"] || approval["prerequisite_job_id"] != intent["prerequisite_job_id"] ||
		approval["prerequisite_job_key"] != intent["prerequisite_job_key"] || approval["prerequisite_job_name"] != intent["prerequisite_job_name"] ||
		approval["prerequisite_job_name"] != expectedJobName || approval["prerequisite_conclusion"] != "success" ||
		approval["environment_id"] != intent["environment_id"] || approval["environment_name"] != Environment ||
		approval["receipt_authority_key_id"] != config.ReceiptAuthorityKeyID || approval["repository"] != Repository ||
		approval["repository_id"] != RepositoryID || approval["repository_owner_id"] != RepositoryOwnerID ||
		approval["workflow_path"] != ".github/workflows/smoke-runtime.yml" || approval["workflow_ref"] != WorkflowRef ||
		approval["workflow_sha"] != config.WorkflowSHA || approval["release_job"] != ReleaseJob ||
		approval["reservation_expires_at_epoch"] != intent["reservation_expires_at_epoch"] || !dispositionOK ||
		(disposition != "approved" && disposition != "post-approved-recovered") || !approvedOK || approved != attempted || approved < discovered || approved > expires ||
		approved > config.TransactionStartedAtEpoch || payloadErr != nil || digest(approvalRaw) != config.RunnerPrerequisiteApprovalDigest ||
		digest(approvalPayloadRaw) != config.RunnerPrerequisiteApprovalPayloadDigest {
		return nil, fmt.Errorf("runner-prerequisite-approval-binding-invalid")
	}
	if disposition == "approved" {
		response, ok := exactString(approval["approval_response_sha256"])
		if !ok || !digestPattern.MatchString(response) {
			return nil, fmt.Errorf("runner-prerequisite-approval-response-invalid")
		}
	} else if approval["approval_response_sha256"] != nil {
		return nil, fmt.Errorf("runner-prerequisite-approval-response-invalid")
	}
	for _, key := range []string{"completed_jobs_sha256", "post_pending_deployments_sha256", "review_history_sha256"} {
		text, ok := exactString(approval[key])
		if !ok || !digestPattern.MatchString(text) {
			return nil, fmt.Errorf("runner-prerequisite-approval-evidence-invalid")
		}
	}
	return &runnerPrerequisiteProof{
		IntentDigest: digest(intentRaw), ApprovalDigest: digest(approvalRaw),
		ApprovalPayloadDigest: digest(approvalPayloadRaw), JobID: jobID,
	}, nil
}

func runnerPrerequisitePathPresent(root, absolute string) (bool, error) {
	_, err := os.Lstat(rooted(root, absolute))
	if err == nil {
		return true, nil
	}
	if os.IsNotExist(err) {
		return false, nil
	}
	return false, err
}

func validateInstalledRunnerPrerequisiteRecords(root string, config *Config, public ed25519.PublicKey) (*runnerPrerequisiteProof, error) {
	intentV3, intentErr := runnerPrerequisitePathPresent(root, RunnerPrerequisiteIntentPathV3)
	postAttemptV3, postAttemptErr := runnerPrerequisitePathPresent(root, RunnerPrerequisitePostAttemptPathV3)
	approvalV3, approvalErr := runnerPrerequisitePathPresent(root, RunnerPrerequisiteApprovalPathV3)
	if intentErr != nil || postAttemptErr != nil || approvalErr != nil || intentV3 != postAttemptV3 || intentV3 != approvalV3 {
		return nil, fmt.Errorf("runner-prerequisite-version-invalid")
	}
	if intentV3 {
		return validateInstalledRunnerPrerequisiteRecordsV3(root, config, public)
	}
	return validateInstalledRunnerPrerequisiteRecordsV2(root, config, public)
}

func validateInstalledRunnerPrerequisiteGate(root string, config *Config, public ed25519.PublicKey, expectedLaunchDigest string, now time.Time) error {
	proof, err := validateInstalledRunnerPrerequisiteRecords(root, config, public)
	if err != nil || proof.IntentDigest != config.RunnerPrerequisiteIntentDigest || proof.ApprovalDigest != config.RunnerPrerequisiteApprovalDigest || proof.ApprovalPayloadDigest != config.RunnerPrerequisiteApprovalPayloadDigest || proof.JobID != config.RunnerPrerequisiteJobID {
		return fmt.Errorf("runner-prerequisite-proof-invalid")
	}
	ticketPayload, ticketRaw, err := readRunnerWire(root, RunnerLaunchTicketPath, 0o400, public, runnerTicketSignatureDomain)
	if err != nil {
		return fmt.Errorf("runner-prerequisite-ticket-unavailable")
	}
	defer zero(ticketRaw)
	ticket, err := validateLaunchTicket(root, ticketPayload, ticketRaw, config, now, false)
	if err != nil || expectedLaunchDigest != "" && ticket.LaunchTicketDigest != expectedLaunchDigest {
		return fmt.Errorf("runner-prerequisite-ticket-binding-invalid")
	}
	return nil
}
