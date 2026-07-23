//go:build linux && amd64

package authority

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

const (
	RunnerReservationPath                           = "/var/lib/propertyquarry-release-single-host-v2/runner-reservation.v2.json"
	RunnerPrerequisiteIntentPath                    = "/var/lib/propertyquarry-release-single-host-v2/runner-prerequisite-intent.v2.json"
	RunnerPrerequisiteApprovalPath                  = "/var/lib/propertyquarry-release-single-host-v2/runner-prerequisite-approval.v2.json"
	runnerReservationSchema                         = "propertyquarry.release-control.single-host-runner-reservation.v2"
	runnerReservationSignatureDomain                = "propertyquarry.release-control.single-host-runner-reservation-signature.v2\x00"
	runnerPrerequisiteIntentSchema                  = "propertyquarry.release-control.single-host-runner-prerequisite-intent.v2"
	runnerPrerequisiteIntentSignatureDomain         = "propertyquarry.release-control.single-host-runner-prerequisite-intent-signature.v2\x00"
	runnerPrerequisiteApprovalSchema                = "propertyquarry.release-control.single-host-runner-prerequisite-approval.v2"
	runnerPrerequisiteApprovalSignatureDomain       = "propertyquarry.release-control.single-host-runner-prerequisite-approval-signature.v2\x00"
	runnerPrerequisiteJob                           = "propertyquarry-protected-dispatch-inputs"
	runnerReservationTTLSeconds               int64 = 6 * 60 * 60
)

type runnerPrerequisiteProof struct {
	IntentDigest          string
	ApprovalDigest        string
	ApprovalPayloadDigest string
	JobID                 string
}

func validateInstalledRunnerPrerequisiteRecords(root string, config *Config, public ed25519.PublicKey) (*runnerPrerequisiteProof, error) {
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
