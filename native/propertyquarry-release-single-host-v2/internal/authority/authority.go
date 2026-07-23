package authority

import (
	"context"
	"crypto/ed25519"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"runtime"
	"strconv"
	"time"
)

const ExitFailure = 50

var (
	SourceManifestDigest     = "sha256:unbound"
	ScratchExecutionContract = "unbound"
)

var (
	authorityNow           = time.Now
	authenticateRequest    = authenticateGitHubRequest
	executePlanStep        = runStep
	verifyBackupReceipt    = verifyPredeployBackupReceipt
	verifyDatabaseReceipt  = verifyDatabaseControlReceipt
	verifyIsolationReceipt = verifyRuntimeIsolationReceipt
	verifyDeployReceipt    = verifyRuntimeDeployReceipt
	verifyRunnerRequest    = verifyRunnerTicketForRequest
	consumeRunnerRequest   = consumeRunnerTicket
)

func Run(args []string, stdin *os.File, stdout, stderr io.Writer) int {
	if len(args) == 1 && (args[0] == "--build-info-json" || args[0] == "--self-test") {
		value := map[string]any{
			"authoritative": false, "binding_source": "independently-signed-installed-profile",
			"component": "propertyquarry-release-single-host-v2", "performs_release_effects": false,
			"production_ready": false, "profile": "single-host-production-v2", "schema": "propertyquarry.release-control.single-host-build-info.v2",
			"scratch_execution_contract": ScratchExecutionContract, "self_test": args[0] == "--self-test",
			"source_manifest_digest": SourceManifestDigest, "toolchain": runtime.Version(), "version": json.Number("2"),
		}
		raw, err := canonicalJSON(value)
		if err != nil {
			return ExitFailure
		}
		raw = append(raw, '\n')
		written, err := stdout.Write(raw)
		zero(raw)
		if err != nil || written < 1 {
			return ExitFailure
		}
		return 0
	}
	var err error
	if len(args) == 2 && args[0] == "client" {
		err = clientRequest(args[1], stdout)
	} else if len(args) == 1 && args[0] == "serve" {
		err = serverConnection(stdin, stdout, "/")
	} else if len(args) == 1 && args[0] == "activation-probe" {
		err = runActivationCanary(context.Background(), "/", authorityNow().UTC(), productionHTTPClient())
	} else if len(args) >= 1 && (args[0] == "runner-ticket-admit" || args[0] == "runner-start-verify") {
		err = runnerTicketCommand(args[0], args[1:], stdout)
	} else if len(args) >= 1 && args[0] == "runner-session-verify" {
		err = verifyRunnerSessionCommand(args[1:], stdout)
	} else if len(args) >= 1 && args[0] == "runner-supervise" {
		err = runnerSupervisorCommand(args[1:], stdout)
	} else {
		err = fmt.Errorf("mode-invalid")
	}
	if err != nil {
		_, _ = io.WriteString(stderr, "propertyquarry-single-host-authority-rejected\n")
		return ExitFailure
	}
	return 0
}

func processWorkflowRequest(root string, config *Config, key ed25519.PrivateKey, request *workflowRequest) ([]byte, error) {
	return processWorkflowRequestContext(context.Background(), root, config, key, request)
}

func processWorkflowRequestContext(parent context.Context, root string, config *Config, key ed25519.PrivateKey, request *workflowRequest) ([]byte, error) {
	if parent == nil || config == nil || request == nil || len(key) != ed25519.PrivateKeySize {
		return nil, fmt.Errorf("workflow-input-invalid")
	}
	plan, err := LoadPlan(root, config)
	if err != nil {
		return nil, err
	}
	defer plan.release()
	journal, err := OpenJournal(root, key)
	if err != nil {
		return nil, err
	}
	defer journal.Close()
	if err := enforceAntiDowngrade(journal, config); err != nil {
		return nil, err
	}
	if err := recoverIncomplete(parent, root, journal, plan, config); err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(parent, 60*time.Second)
	defer cancel()
	identity, err := authenticateRequest(ctx, config, request.OIDCRequestURL, request.ActionsToken, authorityNow().UTC())
	if err != nil {
		return nil, err
	}
	if identity.RunID != request.DiagnosticRunID || identity.RunAttempt != request.DiagnosticRunAttempt || identity.CandidateSHA != request.DiagnosticSHA || identity.WorkflowSHA != request.DiagnosticWorkflowSHA || identity.RunnerLabel != request.DiagnosticRunnerLabel {
		return nil, fmt.Errorf("workflow-diagnostic-mismatch")
	}
	runnerBinding, err := verifyRunnerRequest(root, config, request, identity, authorityNow().UTC())
	if err != nil {
		return nil, err
	}
	request.RunnerLaunchTicketDigest = runnerBinding.LaunchTicketDigest
	request.RunnerNonce = runnerBinding.RunnerNonce
	var latestMatchingRequest *JournalEvent
	for index := len(journal.events) - 1; index >= 0; index-- {
		event := &journal.events[index]
		if latestMatchingRequest == nil && event.RequestID == request.RequestID && event.Operation == request.Operation && event.RunID == identity.RunID && event.RunAttempt == identity.RunAttempt {
			latestMatchingRequest = event
		}
		if event.OIDCJTI == identity.TokenID {
			if event.RequestID != request.RequestID || event.Operation != request.Operation || event.RunID != identity.RunID || event.RunAttempt != identity.RunAttempt {
				return nil, fmt.Errorf("oidc-replay-conflict")
			}
		}
	}
	if latestMatchingRequest != nil {
		if !cachedRequestBindingMatches(latestMatchingRequest.Payload, config, request, identity) {
			return nil, fmt.Errorf("request-reconnect-binding-mismatch")
		}
		if latestMatchingRequest.EventType == "preflight-ready" {
			if err := consumeRunnerRequest(root, runnerBinding); err != nil {
				return nil, err
			}
		}
		return append([]byte(nil), latestMatchingRequest.Wire...), nil
	}
	switch request.Operation {
	case "release-preflight":
		return executePreflight(parent, root, journal, plan, config, request, identity)
	case "release-run":
		return executeRelease(parent, root, journal, plan, config, request, identity)
	default:
		return nil, fmt.Errorf("workflow-operation-invalid")
	}
}

func enforceAntiDowngrade(journal *Journal, config *Config) error {
	if journal == nil || config == nil || config.ReleaseGeneration < 1 {
		return fmt.Errorf("authority-generation-invalid")
	}
	if len(journal.events) == 0 {
		if config.PredecessorRuntimeSHA != "genesis" {
			return fmt.Errorf("authority-genesis-binding-invalid")
		}
		return nil
	}
	var maximumGeneration int64
	var maximumRuntime, maximumConfig string
	for _, event := range journal.events {
		generation, ok := exactInt(event.Payload["release_generation"], 1, 1<<62)
		runtimeSHA, runtimeOK := exactString(event.Payload["runtime_sha"])
		configDigest, configOK := exactString(event.Payload["config_digest"])
		if !ok || !runtimeOK || !shaPattern.MatchString(runtimeSHA) || !configOK || !digestPattern.MatchString(configDigest) {
			return fmt.Errorf("authority-journal-generation-invalid")
		}
		if generation > maximumGeneration {
			maximumGeneration, maximumRuntime, maximumConfig = generation, runtimeSHA, configDigest
		} else if generation == maximumGeneration && (runtimeSHA != maximumRuntime || configDigest != maximumConfig) {
			return fmt.Errorf("authority-generation-fork")
		}
	}
	if config.ReleaseGeneration < maximumGeneration || config.ReleaseGeneration > maximumGeneration+1 {
		return fmt.Errorf("authority-generation-downgrade-or-skip")
	}
	if config.ReleaseGeneration == maximumGeneration {
		if config.RuntimeSHA != maximumRuntime || config.Digest != maximumConfig {
			return fmt.Errorf("authority-generation-rebinding")
		}
		return nil
	}
	if config.PredecessorRuntimeSHA != maximumRuntime {
		return fmt.Errorf("authority-predecessor-invalid")
	}
	return nil
}

func executePreflight(parent context.Context, root string, journal *Journal, plan *Plan, config *Config, request *workflowRequest, identity *Identity) ([]byte, error) {
	for index := len(journal.events) - 1; index >= 0; index-- {
		event := &journal.events[index]
		if event.EventType == "run-succeeded" && event.Payload["config_digest"] == config.Digest {
			fields := authorityFields(config, request, identity)
			fields["checks"] = []any{}
			fields["evaluated_at"] = json.Number(strconv.FormatInt(authorityNow().UTC().Unix(), 10))
			fields["valid_until"] = json.Number("0")
			fields["ready"] = false
			fields["production_ready"] = false
			fields["release_effects_authorized"] = false
			fields["release_effects_performed"] = false
			fields["disposition"] = "already-terminal-history-not-live-readiness"
			return journal.Append("preflight-not-ready", fields)
		}
	}
	results := make([]any, 0, len(plan.PreflightSteps))
	preflightStarted := authorityNow().UTC().Unix()
	ready := preflightStarted >= config.TransactionStartedAtEpoch && preflightStarted-config.TransactionStartedAtEpoch <= config.BackupMaxAgeSeconds
	for _, step := range plan.PreflightSteps {
		result := executePlanStep(parent, step, plan.Executables, authorityNow)
		results = append(results, resultValue(result))
		if !result.Succeeded {
			ready = false
			break
		}
	}
	now := authorityNow().UTC().Unix()
	validUntil := now + config.PreflightTTLSeconds
	if identity.ExpiresAt < validUntil {
		validUntil = identity.ExpiresAt
	}
	if validUntil <= now {
		ready = false
	}
	fields := authorityFields(config, request, identity)
	fields["checks"] = results
	fields["evaluated_at"] = json.Number(strconv.FormatInt(now, 10))
	fields["valid_until"] = json.Number(strconv.FormatInt(validUntil, 10))
	fields["ready"] = ready
	fields["production_ready"] = false
	fields["release_effects_authorized"] = false
	fields["release_effects_performed"] = false
	fields["disposition"] = "not-ready"
	eventType := "preflight-not-ready"
	if ready {
		fields["disposition"] = "ready"
		eventType = "preflight-ready"
	}
	wire, err := journal.Append(eventType, fields)
	if err != nil {
		return nil, err
	}
	if ready {
		binding := &runnerTicketBinding{
			DispatchTicketDigest: request.RunnerTicketDigest, LaunchTicketDigest: request.RunnerLaunchTicketDigest,
			RunnerLabel: identity.RunnerLabel, RunnerNonce: request.RunnerNonce, RunID: identity.RunID,
			RunAttempt: identity.RunAttempt, JobID: identity.CheckRunID, RunnerID: identity.RunnerID, RunnerName: identity.RunnerName,
		}
		if err := consumeRunnerRequest(root, binding); err != nil {
			zero(wire)
			return nil, err
		}
	}
	return wire, nil
}

func executeRelease(parent context.Context, root string, journal *Journal, plan *Plan, config *Config, request *workflowRequest, identity *Identity) ([]byte, error) {
	now := authorityNow().UTC().Unix()
	if now < config.TransactionStartedAtEpoch || now-config.TransactionStartedAtEpoch > config.BackupMaxAgeSeconds {
		return nil, fmt.Errorf("release-transaction-window-invalid")
	}
	var ready *JournalEvent
	for index := len(journal.events) - 1; index >= 0; index-- {
		event := &journal.events[index]
		if event.EventType == "preflight-ready" && event.RunID == identity.RunID && event.RunAttempt == identity.RunAttempt {
			ready = event
			break
		}
		if event.RunID == identity.RunID && terminalEvent(event.EventType) {
			return nil, fmt.Errorf("release-run-already-terminal")
		}
	}
	if ready == nil || ready.Payload["config_digest"] != config.Digest || ready.Payload["plan_digest"] != plan.Digest || ready.Payload["workflow_sha"] != config.WorkflowSHA || ready.Payload["web_image"] != config.WebImage || ready.Payload["render_image"] != config.RenderImage ||
		ready.Payload["check_run_id"] != identity.CheckRunID || ready.Payload["runner_id"] != identity.RunnerID || ready.Payload["runner_name"] != identity.RunnerName || ready.Payload["runner_label"] != identity.RunnerLabel ||
		ready.Payload["runner_label_nonce"] != request.RunnerNonce || ready.Payload["runner_dispatch_ticket_sha256"] != request.RunnerTicketDigest || ready.Payload["runner_launch_ticket_sha256"] != request.RunnerLaunchTicketDigest || ready.Payload["runner_ticket_authenticated"] != true ||
		ready.Payload["security_bootstrap_attestation_sha256"] != request.SecurityBootstrapAttestationSHA || ready.Payload["security_bootstrap_run_id"] != request.SecurityBootstrapRunID ||
		ready.Payload["security_bootstrap_artifact_digest"] != request.SecurityBootstrapArtifactDigest {
		return nil, fmt.Errorf("ready-preflight-unavailable")
	}
	validUntil, ok := exactInt(ready.Payload["valid_until"], 1, 1<<62)
	if !ok || now > validUntil || ready.OIDCJTI == identity.TokenID {
		return nil, fmt.Errorf("ready-preflight-expired")
	}
	fields := authorityFields(config, request, identity)
	fields["ready_preflight_receipt_digest"] = ready.ReceiptDigest
	fields["admitted_at"] = json.Number(strconv.FormatInt(now, 10))
	fields["fence_generation"] = json.Number(strconv.FormatInt(int64(len(journal.events)+1), 10))
	fields["ready"] = false
	fields["production_ready"] = false
	fields["release_effects_authorized"] = true
	fields["release_effects_performed"] = false
	fields["disposition"] = "admitted"
	if _, err := journal.Append("run-admitted", fields); err != nil {
		return nil, err
	}
	return executeTransaction(parent, root, journal, plan, config, request, identity, ready.ReceiptDigest, false)
}

func executeTransaction(parent context.Context, root string, journal *Journal, plan *Plan, config *Config, request *workflowRequest, identity *Identity, readyDigest string, recovery bool) ([]byte, error) {
	base := authorityFields(config, request, identity)
	base["ready_preflight_receipt_digest"] = readyDigest
	base["ready"] = false
	base["production_ready"] = false
	base["release_effects_authorized"] = true
	base["release_effects_performed"] = false
	base["runtime_envelopes_revalidated"] = false
	base["predeploy_backup_verified"] = false
	base["database_receipts_verified"] = false
	base["database_runtime_env_revalidated"] = false
	base["database_provision_roles_verified"] = false
	base["database_migrate_schema_verified"] = false
	base["database_harden_runtime_acl_verified"] = false
	base["database_verify_schema_readiness_verified"] = false
	base["runtime_isolation_purge_verified"] = false
	base["runtime_retirement_verified"] = false
	base["runtime_deploy_verified"] = false
	base["runtime_isolation_terminal_verified"] = false
	base["isolation_purge_may_have_started"] = false
	base["runtime_retirement_may_have_started"] = false
	base["database_mutation_may_have_started"] = false
	base["runtime_deploy_may_have_started"] = false
	base["lifecycle_context_cancelled"] = false
	base["recovery"] = recovery
	allSucceeded := true
	for _, phase := range []struct {
		name  string
		steps []Step
	}{{"release", plan.ReleaseSteps}, {"verify", plan.VerifySteps}} {
		for _, step := range phase.steps {
			boundaryValid := true
			if phase.name == "release" && step.ID == PurgeRuntimeIsolationStepID {
				base["isolation_purge_may_have_started"] = true
			}
			if phase.name == "release" && step.ID == RuntimeRetirementStepID {
				base["runtime_retirement_may_have_started"] = true
			}
			if phase.name == "release" {
				if _, databaseStep := databaseOperationForStep(step.ID); databaseStep {
					purgeVerified := base["runtime_isolation_purge_verified"] == true
					retirementVerified := base["runtime_retirement_verified"] == true
					runtimeInputsValid := validateExactCurrentRuntimeInputs(root, config.RuntimeInputs) == nil
					boundaryValid = purgeVerified && retirementVerified && runtimeInputsValid
					if boundaryValid {
						base["database_mutation_may_have_started"] = true
					}
				}
			}
			if phase.name == "release" && step.ID == DeployRuntimeStepID {
				envelopesValid := validateGoogleIdentityEnvelope(root, uint32(config.GoogleIdentityEnvUID), uint32(config.GoogleIdentityEnvGID), config.GoogleIdentityEnvDigest) == nil &&
					validateRegistrationEmailEnvelope(root, uint32(config.RegistrationEmailEnvUID), uint32(config.RegistrationEmailEnvGID), config.RegistrationEmailEnvDigest) == nil &&
					validateExternalDigestFile(root, SceneVideoEnvPath, 0o600, uint32(config.SceneVideoEnvUID), uint32(config.SceneVideoEnvGID), config.SceneVideoEnvDigest, 256*1024) == nil
				base["runtime_envelopes_revalidated"] = envelopesValid
				databaseEnvDigest, digestOK := base["database_runtime_env_digest"].(string)
				databaseEnvironmentValid := digestOK && allDatabaseReceiptsVerified(base) && validateDatabaseRuntimeEnvironment(root, databaseEnvDigest) == nil
				base["database_runtime_env_revalidated"] = databaseEnvironmentValid
				runtimeInputsValid := validateExactCurrentRuntimeInputs(root, config.RuntimeInputs) == nil
				boundaryValid = boundaryValid && envelopesValid && databaseEnvironmentValid && runtimeInputsValid && base["runtime_isolation_purge_verified"] == true && base["runtime_retirement_verified"] == true
				if boundaryValid {
					base["runtime_deploy_may_have_started"] = true
				}
			}
			if phase.name == "verify" && step.ID == VerifyRuntimeIsolationStepID {
				boundaryValid = base["runtime_deploy_verified"] == true && validateExactCurrentRuntimeInputs(root, config.RuntimeInputs) == nil
			}
			if boundaryValid && phase.name == "release" && productionMutationMayStart(step.ID) {
				base["release_effects_performed"] = true
			}
			started := cloneFields(base)
			started["phase"] = phase.name
			started["step_id"] = step.ID
			started["disposition"] = "step-started"
			if _, err := journal.Append("step-started", started); err != nil {
				return nil, err
			}
			var result StepResult
			if boundaryValid {
				result = executePlanStep(parent, step, plan.Executables, authorityNow)
			} else {
				now := authorityNow().UTC().Unix()
				result = StepResult{ID: step.ID, Effect: step.Effect, ExitCode: 255, StartedAt: now, FinishedAt: now, Succeeded: false}
			}
			if step.ID == "predeploy-encrypted-backup" && result.Succeeded {
				proof, proofErr := verifyBackupReceipt(root, config, journal.key.Public().(ed25519.PublicKey))
				if proofErr != nil || proof.startedAt < result.StartedAt || proof.finishedAt > result.FinishedAt {
					result.Succeeded = false
				} else {
					for field, value := range backupProofFields(proof) {
						base[field] = value
					}
				}
			}
			if step.ID == RuntimeRetirementStepID && result.Succeeded {
				proof, proofErr := verifyIsolationReceipt(root, config, journal.key.Public().(ed25519.PublicKey), operationRetireStaleRuntime, result.StartedAt, result.FinishedAt)
				if proofErr != nil || proof.backupReceiptDigest != stringValue(base["predeploy_backup_receipt_digest"]) || proof.purgeReceiptDigest != stringValue(base["runtime_isolation_purge_receipt_digest"]) {
					result.Succeeded = false
				} else {
					for field, value := range isolationReceiptProofFields(proof) {
						base[field] = value
					}
				}
			}
			if step.ID == PurgeRuntimeIsolationStepID && result.Succeeded {
				proof, proofErr := verifyIsolationReceipt(root, config, journal.key.Public().(ed25519.PublicKey), operationPurgeRuntimeIsolation, result.StartedAt, result.FinishedAt)
				if proofErr != nil || proof.backupReceiptDigest != stringValue(base["predeploy_backup_receipt_digest"]) {
					result.Succeeded = false
				} else {
					for field, value := range isolationReceiptProofFields(proof) {
						base[field] = value
					}
				}
			}
			if contract, ok := databaseOperationForStep(step.ID); ok && result.Succeeded {
				proof, proofErr := verifyDatabaseReceipt(root, config, journal.key.Public().(ed25519.PublicKey), contract.operation, result.StartedAt, result.FinishedAt)
				if proofErr != nil || !databaseProofContinues(base, proof) {
					result.Succeeded = false
				} else {
					for field, value := range databaseReceiptProofFields(proof) {
						base[field] = value
					}
					base["database_receipts_verified"] = allDatabaseReceiptsVerified(base)
				}
			}
			if step.ID == DeployRuntimeStepID && result.Succeeded {
				proof, proofErr := verifyDeployReceipt(root, config, journal.key.Public().(ed25519.PublicKey), result.StartedAt, result.FinishedAt)
				if proofErr != nil || !runtimeDeployProofContinues(base, proof) {
					result.Succeeded = false
				} else {
					for field, value := range runtimeDeployProofFields(proof) {
						base[field] = value
					}
				}
			}
			if step.ID == VerifyRuntimeIsolationStepID && result.Succeeded {
				proof, proofErr := verifyIsolationReceipt(root, config, journal.key.Public().(ed25519.PublicKey), operationVerifyRuntimeIsolation, result.StartedAt, result.FinishedAt)
				deployFinished, deployFinishedOK := exactInt(base["runtime_deploy_finished_at_epoch"], 1, 1<<62)
				if proofErr != nil || proof.backupReceiptDigest != stringValue(base["predeploy_backup_receipt_digest"]) || proof.postPurgeRootEnvDigest != config.PostPurgeRootEnvDigest || proof.deployReceiptDigest != stringValue(base["runtime_deploy_receipt_digest"]) || !deployFinishedOK || proof.startedAt < deployFinished || !isolationDatabaseProofMatches(base, proof) {
					result.Succeeded = false
				} else {
					for field, value := range isolationReceiptProofFields(proof) {
						base[field] = value
					}
				}
			}
			finished := cloneFields(base)
			finished["phase"] = phase.name
			finished["step"] = resultValue(result)
			finished["disposition"] = "step-finished"
			if _, err := journal.Append("step-finished", finished); err != nil {
				return nil, err
			}
			if !result.Succeeded {
				allSucceeded = false
				break
			}
		}
		if !allSucceeded {
			break
		}
	}
	if parent.Err() != nil {
		allSucceeded = false
		base["lifecycle_context_cancelled"] = true
	}
	if allSucceeded {
		if verified, ok := base["predeploy_backup_verified"].(bool); !ok || !verified {
			return nil, fmt.Errorf("predeploy-backup-not-verified")
		}
		if verified, ok := base["database_receipts_verified"].(bool); !ok || !verified {
			return nil, fmt.Errorf("database-receipts-not-verified")
		}
		if revalidated, ok := base["database_runtime_env_revalidated"].(bool); !ok || !revalidated {
			return nil, fmt.Errorf("database-runtime-environment-not-revalidated")
		}
		if revalidated, ok := base["runtime_envelopes_revalidated"].(bool); !ok || !revalidated {
			return nil, fmt.Errorf("runtime-envelopes-not-revalidated")
		}
		if verified, ok := base["runtime_isolation_terminal_verified"].(bool); !ok || !verified {
			return nil, fmt.Errorf("runtime-isolation-terminal-not-verified")
		}
		if base["runtime_isolation_purge_verified"] != true || base["runtime_retirement_verified"] != true || base["runtime_deploy_verified"] != true {
			return nil, fmt.Errorf("runtime-proof-chain-incomplete")
		}
		terminal := cloneFields(base)
		terminal["completed_at"] = json.Number(strconv.FormatInt(authorityNow().UTC().Unix(), 10))
		terminal["production_ready"] = true
		terminal["disposition"] = "succeeded"
		terminal["rollback_performed"] = false
		return journal.Append("run-succeeded", terminal)
	}
	if effects, ok := base["release_effects_performed"].(bool); !ok || !effects {
		terminal := cloneFields(base)
		terminal["completed_at"] = json.Number(strconv.FormatInt(authorityNow().UTC().Unix(), 10))
		terminal["production_ready"] = false
		terminal["disposition"] = "failed-before-production-mutation"
		terminal["rollback_performed"] = false
		return journal.Append("run-failed-no-effects", terminal)
	}
	return executeRollback(parent, root, journal, plan, config, base)
}

func runtimeDeployProofContinues(fields map[string]any, proof *runtimeDeployProof) bool {
	if fields == nil || proof == nil || proof.backupReceiptDigest != stringValue(fields["predeploy_backup_receipt_digest"]) || proof.purgeReceiptDigest != stringValue(fields["runtime_isolation_purge_receipt_digest"]) || proof.retirementReceiptDigest != stringValue(fields["runtime_retirement_receipt_digest"]) || proof.databaseSubstrateDigest != stringValue(fields["database_substrate_digest"]) {
		return false
	}
	previousFinished, ok := exactInt(fields["database_verify_schema_readiness_finished_at_epoch"], 1, 1<<62)
	if !ok || proof.startedAt < previousFinished || len(proof.databaseReceiptDigests) != len(databaseOperationContracts()) {
		return false
	}
	prefixes := map[string]string{
		"provision-roles":         "database_provision_roles",
		"migrate-schema":          "database_migrate_schema",
		"harden-runtime-acl":      "database_harden_runtime_acl",
		"verify-schema-readiness": "database_verify_schema_readiness",
	}
	for _, contract := range databaseOperationContracts() {
		if proof.databaseReceiptDigests[contract.operation] != stringValue(fields[prefixes[contract.operation]+"_receipt_digest"]) {
			return false
		}
	}
	return true
}

func databaseProofContinues(fields map[string]any, proof *databaseReceiptProof) bool {
	if fields == nil || proof == nil {
		return false
	}
	prerequisites := map[string]string{
		"migrate-schema":          "database_provision_roles_verified",
		"harden-runtime-acl":      "database_migrate_schema_verified",
		"verify-schema-readiness": "database_harden_runtime_acl_verified",
	}
	if prerequisite, required := prerequisites[proof.operation]; required && fields[prerequisite] != true {
		return false
	}
	if proof.operation != "provision-roles" && proof.operation != "migrate-schema" && proof.operation != "harden-runtime-acl" && proof.operation != "verify-schema-readiness" {
		return false
	}
	if proof.backupReceiptDigest != stringValue(fields["predeploy_backup_receipt_digest"]) || proof.purgeReceiptDigest != stringValue(fields["runtime_isolation_purge_receipt_digest"]) || proof.retirementReceiptDigest != stringValue(fields["runtime_retirement_receipt_digest"]) || proof.databaseSubstrate == nil || proof.databaseSubstrate.digest != stringValue(fields["database_substrate_digest"]) {
		return false
	}
	predecessorDigestField := map[string]string{
		"provision-roles":         "runtime_retirement_receipt_digest",
		"migrate-schema":          "database_provision_roles_receipt_digest",
		"harden-runtime-acl":      "database_migrate_schema_receipt_digest",
		"verify-schema-readiness": "database_harden_runtime_acl_receipt_digest",
	}[proof.operation]
	predecessorFinishedField := map[string]string{
		"provision-roles":         "runtime_retirement_finished_at_epoch",
		"migrate-schema":          "database_provision_roles_finished_at_epoch",
		"harden-runtime-acl":      "database_migrate_schema_finished_at_epoch",
		"verify-schema-readiness": "database_harden_runtime_acl_finished_at_epoch",
	}[proof.operation]
	predecessorFinished, predecessorFinishedOK := exactInt(fields[predecessorFinishedField], 1, 1<<62)
	if proof.predecessorReceiptDigest != stringValue(fields[predecessorDigestField]) || !predecessorFinishedOK || proof.startedAt < predecessorFinished {
		return false
	}
	if previousDigest, exists := fields["database_runtime_env_digest"]; exists {
		digestText, ok := previousDigest.(string)
		if !ok || digestText != proof.envFileDigest {
			return false
		}
	}
	if previousOID, exists := fields["database_oid"]; exists {
		oid, ok := exactInt(previousOID, 1, 1<<62)
		if !ok || oid != proof.databaseOID {
			return false
		}
	}
	if previousImageID, exists := fields["database_image_id"]; exists {
		imageID, ok := exactString(previousImageID)
		if !ok || imageID != proof.databaseImageID {
			return false
		}
	}
	if previousRepoDigest, exists := fields["database_repo_digest"]; exists {
		repoDigest, ok := exactString(previousRepoDigest)
		if !ok || repoDigest != proof.databaseRepoDigest {
			return false
		}
	}
	existingVersions := 0
	for _, component := range databaseSchemaComponents {
		field := "database_schema_" + component.field + "_version"
		previous, exists := fields[field]
		if !exists {
			continue
		}
		existingVersions++
		version, ok := exactInt(previous, 1, 1<<31-1)
		if !ok || proof.schemaVersions == nil || proof.schemaVersions[component.field] != version {
			return false
		}
	}
	if proof.operation == "provision-roles" {
		return len(proof.schemaVersions) == 0 && existingVersions == 0
	}
	if len(proof.schemaVersions) != len(databaseSchemaComponents) {
		return false
	}
	for _, component := range databaseSchemaComponents {
		if proof.schemaVersions[component.field] < 1 {
			return false
		}
	}
	if proof.operation == "migrate-schema" {
		return existingVersions == 0
	}
	if existingVersions != len(databaseSchemaComponents) {
		return false
	}
	return true
}

func productionMutationMayStart(stepID string) bool {
	switch stepID {
	case PurgeRuntimeIsolationStepID, RuntimeRetirementStepID, ProvisionDatabaseRolesStepID, MigrateSchemaStepID, HardenRuntimeACLStepID, DeployRuntimeStepID:
		return true
	default:
		return false
	}
}

func executeRollback(parent context.Context, root string, journal *Journal, plan *Plan, config *Config, base map[string]any) ([]byte, error) {
	rollbackContext, rollbackCancel := context.WithTimeout(context.WithoutCancel(parent), rollbackExecutionTimeout)
	defer rollbackCancel()
	rollbackOK := true
	for _, step := range plan.RollbackSteps {
		started := cloneFields(base)
		started["phase"] = "rollback"
		started["step_id"] = step.ID
		started["disposition"] = "rollback-step-started"
		if _, err := journal.Append("rollback-step-started", started); err != nil {
			return nil, err
		}
		result := executePlanStep(rollbackContext, step, plan.Executables, authorityNow)
		if step.ID == RestoreRuntimeIsolationStepID && result.Succeeded {
			proof, proofErr := verifyIsolationReceipt(root, config, journal.key.Public().(ed25519.PublicKey), operationRestoreRuntimeIsolation, result.StartedAt, result.FinishedAt)
			if proofErr != nil || proof.backupReceiptDigest != stringValue(base["predeploy_backup_receipt_digest"]) || proof.prePurgeRootEnvDigest != config.PrePurgeRootEnvDigest || proof.postPurgeRootEnvDigest != stringValue(base["post_purge_root_env_digest"]) {
				result.Succeeded = false
			} else {
				for field, value := range isolationReceiptProofFields(proof) {
					base[field] = value
				}
			}
		}
		finished := cloneFields(base)
		finished["phase"] = "rollback"
		finished["step"] = resultValue(result)
		finished["disposition"] = "rollback-step-finished"
		if _, err := journal.Append("rollback-step-finished", finished); err != nil {
			return nil, err
		}
		if !result.Succeeded {
			rollbackOK = false
			break
		}
	}
	terminal := cloneFields(base)
	terminal["completed_at"] = json.Number(strconv.FormatInt(authorityNow().UTC().Unix(), 10))
	terminal["rollback_performed"] = true
	terminal["production_ready"] = false
	fullRollbackPossible := base["runtime_retirement_may_have_started"] != true && base["database_mutation_may_have_started"] != true && base["runtime_deploy_may_have_started"] != true
	terminal["full_rollback_verified"] = rollbackOK && fullRollbackPossible
	if rollbackOK && fullRollbackPossible {
		terminal["disposition"] = "rolled-back"
		return journal.Append("run-rolled-back", terminal)
	}
	terminal["disposition"] = "rollback-failed"
	return journal.Append("run-rollback-failed", terminal)
}

func recoverIncomplete(parent context.Context, root string, journal *Journal, plan *Plan, config *Config) error {
	if config == nil {
		return fmt.Errorf("recovery-config-missing")
	}
	if len(journal.events) == 0 {
		return nil
	}
	last := &journal.events[len(journal.events)-1]
	if last.Operation != "release-run" || terminalEvent(last.EventType) || last.EventType == "preflight-ready" || last.EventType == "preflight-not-ready" {
		return nil
	}
	if last.Payload["config_digest"] != config.Digest || last.Payload["plan_digest"] != config.PlanDigest || last.Payload["runtime_sha"] != config.RuntimeSHA || last.Payload["workflow_sha"] != config.WorkflowSHA || last.Payload["deployment_id"] != config.DeploymentID {
		return fmt.Errorf("recovery-installed-authority-mismatch")
	}
	request := &workflowRequest{Operation: last.Operation, RequestID: last.RequestID, DiagnosticRunID: last.RunID, DiagnosticRunAttempt: last.RunAttempt,
		DiagnosticRunnerLabel: stringValue(last.Payload["runner_label"]), RunnerTicketDigest: stringValue(last.Payload["runner_dispatch_ticket_sha256"]),
		RunnerLaunchTicketDigest: stringValue(last.Payload["runner_launch_ticket_sha256"]), RunnerNonce: stringValue(last.Payload["runner_label_nonce"]),
		SecurityBootstrapAttestationSHA: stringValue(last.Payload["security_bootstrap_attestation_sha256"]),
		SecurityBootstrapRunID:          stringValue(last.Payload["security_bootstrap_run_id"]), SecurityBootstrapArtifactDigest: stringValue(last.Payload["security_bootstrap_artifact_digest"])}
	identity := &Identity{RunID: last.RunID, RunAttempt: last.RunAttempt, TokenID: last.OIDCJTI, CandidateSHA: stringValue(last.Payload["workflow_sha"]), WorkflowSHA: stringValue(last.Payload["workflow_sha"]),
		RunnerID: stringValue(last.Payload["runner_id"]), RunnerName: stringValue(last.Payload["runner_name"]), RunnerLabel: stringValue(last.Payload["runner_label"]), CheckRunID: stringValue(last.Payload["check_run_id"])}
	base := cloneFields(last.Payload)
	for field, expected := range authorityFields(config, request, identity) {
		if actual, exists := base[field]; exists && !canonicalValuesEqual(actual, expected) {
			return fmt.Errorf("recovery-journal-authority-mismatch")
		}
		base[field] = expected
	}
	base["ready"] = false
	base["production_ready"] = false
	base["release_effects_authorized"] = true
	releaseEffectsPerformed, ok := last.Payload["release_effects_performed"].(bool)
	if !ok {
		return fmt.Errorf("recovery-release-effect-state-invalid")
	}
	base["release_effects_performed"] = releaseEffectsPerformed
	base["recovery"] = true
	base["disposition"] = "recovery-started"
	if _, err := journal.Append("recovery-started", base); err != nil {
		return err
	}
	if !releaseEffectsPerformed {
		terminal := cloneFields(base)
		terminal["completed_at"] = json.Number(strconv.FormatInt(authorityNow().UTC().Unix(), 10))
		terminal["rollback_performed"] = false
		terminal["production_ready"] = false
		terminal["disposition"] = "recovered-before-production-mutation"
		wire, err := journal.Append("run-failed-no-effects", terminal)
		zero(wire)
		return err
	}
	wire, err := executeRollback(parent, root, journal, plan, config, base)
	zero(wire)
	if err != nil {
		return err
	}
	switch journal.events[len(journal.events)-1].EventType {
	case "run-rolled-back", "run-rollback-failed":
		return nil
	default:
		return fmt.Errorf("recovery-terminal-state-invalid")
	}
}

func authorityFields(config *Config, request *workflowRequest, identity *Identity) map[string]any {
	return map[string]any{
		"operation": request.Operation, "request_id": request.RequestID, "oidc_jti": identity.TokenID,
		"run_id": identity.RunID, "run_attempt": json.Number(strconv.FormatInt(identity.RunAttempt, 10)), "check_run_id": identity.CheckRunID,
		"runner_id": identity.RunnerID, "runner_name": identity.RunnerName, "runner_label": identity.RunnerLabel,
		"runner_label_nonce": request.RunnerNonce, "runner_dispatch_ticket_sha256": request.RunnerTicketDigest,
		"runner_launch_ticket_sha256": request.RunnerLaunchTicketDigest, "runner_ticket_authenticated": true,
		"security_bootstrap_attestation_sha256": request.SecurityBootstrapAttestationSHA,
		"security_bootstrap_run_id":             request.SecurityBootstrapRunID, "security_bootstrap_artifact_digest": request.SecurityBootstrapArtifactDigest,
		"security_bootstrap_evidence_bound": true, "security_bootstrap_evidence_source": "workflow-needs-output",
		"security_bootstrap_artifact_authenticated": false,
		"authority_profile":                         "single-host-production-v2", "authority_scope": "host:" + config.HostMachineIDDigest + "/project:" + ProjectName,
		"single_host_authority": true, "external_cas_profile": false, "authoritative": true,
		"runtime_sha": config.RuntimeSHA, "workflow_sha": config.WorkflowSHA, "envelope_sha": config.EnvelopeSHA, "release_generation": json.Number(strconv.FormatInt(config.ReleaseGeneration, 10)),
		"deployment_id": config.DeploymentID, "transaction_started_at_epoch": json.Number(strconv.FormatInt(config.TransactionStartedAtEpoch, 10)), "backup_max_age_seconds": json.Number(strconv.FormatInt(config.BackupMaxAgeSeconds, 10)),
		"predecessor_runtime_sha": config.PredecessorRuntimeSHA, "repository": Repository, "workflow_ref": WorkflowRef,
		"config_digest": config.Digest, "plan_digest": config.PlanDigest, "host_machine_id_digest": config.HostMachineIDDigest,
		"web_image": config.WebImage, "render_image": config.RenderImage, "cloudflared_image": config.CloudflaredImage, "configured_receipt_key_id": config.ReceiptAuthorityKeyID,
		"database_image":            config.DatabaseImage,
		"database_substrate_digest": config.DatabaseSubstrateDigest,
		"runtime_retirement_digest": config.RuntimeRetirementDigest,
		"runtime_deploy_digest":     config.RuntimeDeployDigest,
		"api_host_ip":               config.APIHostIP, "api_host_port": json.Number(strconv.FormatInt(config.APIHostPort, 10)), "api_container_port": json.Number(strconv.FormatInt(config.APIContainerPort, 10)),
		"pre_purge_root_env_digest":  config.PrePurgeRootEnvDigest,
		"post_purge_root_env_digest": config.PostPurgeRootEnvDigest,
		"pre_purge_runtime_inputs":   runtimeInputObservationValues(config.PrePurgeRuntimeInputs),
		"runtime_inputs":             runtimeInputObservationValues(config.RuntimeInputs),
		"scene_video_env_path":       SceneVideoEnvPath, "scene_video_env_mode": json.Number("384"), "scene_video_env_digest": config.SceneVideoEnvDigest,
		"scene_video_env_uid": json.Number(strconv.FormatInt(config.SceneVideoEnvUID, 10)), "scene_video_env_gid": json.Number(strconv.FormatInt(config.SceneVideoEnvGID, 10)),
		"github_identity_env_path": GoogleIdentityEnvPath, "github_identity_env_mode": "0600", "github_identity_env_digest": config.GoogleIdentityEnvDigest,
		"github_identity_env_uid": json.Number(strconv.FormatInt(config.GoogleIdentityEnvUID, 10)), "github_identity_env_gid": json.Number(strconv.FormatInt(config.GoogleIdentityEnvGID, 10)),
		"registration_email_env_path": RegistrationEmailEnvPath, "registration_email_env_mode": "0600", "registration_email_env_digest": config.RegistrationEmailEnvDigest,
		"registration_email_env_uid": json.Number(strconv.FormatInt(config.RegistrationEmailEnvUID, 10)), "registration_email_env_gid": json.Number(strconv.FormatInt(config.RegistrationEmailEnvGID, 10)),
		"github_oidc_signature_verified": true, "github_job_correlation_verified": true,
	}
}

func cachedRequestBindingMatches(payload map[string]any, config *Config, request *workflowRequest, identity *Identity) bool {
	if payload == nil || config == nil || request == nil || identity == nil {
		return false
	}
	expected := map[string]any{
		"operation": request.Operation, "request_id": request.RequestID, "run_id": identity.RunID,
		"run_attempt": json.Number(strconv.FormatInt(identity.RunAttempt, 10)), "check_run_id": identity.CheckRunID,
		"runner_id": identity.RunnerID, "runner_name": identity.RunnerName, "runner_label": identity.RunnerLabel,
		"runner_label_nonce": request.RunnerNonce, "runner_dispatch_ticket_sha256": request.RunnerTicketDigest,
		"runner_launch_ticket_sha256": request.RunnerLaunchTicketDigest, "runner_ticket_authenticated": true,
		"runtime_sha": config.RuntimeSHA, "workflow_sha": config.WorkflowSHA, "deployment_id": config.DeploymentID, "config_digest": config.Digest, "plan_digest": config.PlanDigest,
		"security_bootstrap_attestation_sha256": request.SecurityBootstrapAttestationSHA,
		"security_bootstrap_run_id":             request.SecurityBootstrapRunID, "security_bootstrap_artifact_digest": request.SecurityBootstrapArtifactDigest,
	}
	for key, expectedValue := range expected {
		if !canonicalValuesEqual(payload[key], expectedValue) {
			return false
		}
	}
	return true
}

func cloneFields(source map[string]any) map[string]any {
	result := make(map[string]any, len(source)+4)
	for key, value := range source {
		result[key] = value
	}
	return result
}

func terminalEvent(value string) bool {
	return value == "run-succeeded" || value == "run-rolled-back" || value == "run-rollback-failed" || value == "run-failed-no-effects"
}

func stringValue(value any) string { text, _ := exactString(value); return text }
