package authority

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"syscall"
	"time"
)

const (
	maximumPlanBytes                 = 1_048_576
	maximumStepOutput                = 65_536
	maximumSteps                     = 64
	maximumReleaseVerifyStepSeconds  = int64(17_100)
	maximumRollbackStepSeconds       = int64(600)
	PredeployBackupExecutablePath    = "/usr/libexec/propertyquarry-release-control/propertyquarry-predeploy-backup-v2"
	PredeployBackupEncryptionKeyPath = "/home/tibor/.local/share/propertyquarry-backup-keys/propertyquarry-predeploy-backup-v2.key"
	PredeployBackupReceiptDirectory  = "/var/lib/propertyquarry-release-single-host-v2/backup-receipts"
	DatabaseControlExecutablePath    = "/usr/libexec/propertyquarry-release-control/propertyquarry-database-control-v2"
	DatabaseControlExecutableDigest  = "sha256:9bdebcd2bae867ef9ac4e38374e964dc81752b2a572eb8a0568f3bb45d5bfe18"
	DatabaseReceiptDirectory         = "/var/lib/propertyquarry-release-single-host-v2/database-receipts"
	DatabaseRuntimeEnvironmentPath   = "/docker/property/state/runtime/propertyquarry_database_roles.env"
	VerifyIsolationInputsStepID      = "verify-propertyquarry-isolation-inputs"
	ProvisionDatabaseRolesStepID     = "provision-propertyquarry-database-roles"
	MigrateSchemaStepID              = "migrate-propertyquarry-schema"
	HardenRuntimeACLStepID           = "harden-propertyquarry-runtime-acl"
	VerifySchemaReadinessStepID      = "verify-propertyquarry-schema-readiness"
	DeployRuntimeStepID              = "deploy-propertyquarry-runtime"
	VerifyRuntimeIsolationStepID     = "verify-propertyquarry-runtime-isolation"
)

type Plan struct {
	Raw                                     []byte
	Digest                                  string
	Executables                             map[string]string
	RunnerReservationDigest                 string
	RunnerLabel                             string
	RunnerPrerequisiteIntentDigest          string
	RunnerPrerequisiteApprovalDigest        string
	RunnerPrerequisiteApprovalPayloadDigest string
	RunnerPrerequisiteJobID                 string
	RunnerRunID                             string
	RunnerRunAttempt                        int64
	RunnerJobID                             string
	PreflightSteps                          []Step
	ReleaseSteps                            []Step
	VerifySteps                             []Step
	RollbackSteps                           []Step
}

type Step struct {
	ID               string
	Effect           string
	Argv             []string
	TimeoutSeconds   int64
	ExpectedExitCode int64
	Idempotent       bool
}

type StepResult struct {
	ID              string
	Effect          string
	ExitCode        int
	StartedAt       int64
	FinishedAt      int64
	StdoutBytes     int64
	StderrBytes     int64
	StdoutTruncated bool
	StderrTruncated bool
	Succeeded       bool
}

func (plan *Plan) release() {
	if plan == nil {
		return
	}
	zero(plan.Raw)
	*plan = Plan{}
}

// Release clears a detached or installed transaction plan after validation.
func (plan *Plan) Release() {
	plan.release()
}

func LoadPlan(root string, config *Config) (*Plan, error) {
	if config == nil {
		return nil, fmt.Errorf("plan-config-missing")
	}
	ownerUID, ownerGID := secureOwner(root)
	raw, err := secureRead(root, PlanPath, 0o444, ownerUID, ownerGID, maximumPlanBytes)
	if err != nil {
		return nil, fmt.Errorf("plan-unavailable")
	}
	if digest(raw) != config.PlanDigest {
		zero(raw)
		return nil, fmt.Errorf("plan-digest-mismatch")
	}
	value, err := strictJSON(raw, maximumPlanBytes)
	if err != nil {
		zero(raw)
		return nil, err
	}
	plan, err := parsePlan(value, raw, config)
	if err != nil {
		zero(raw)
		return nil, err
	}
	return plan, nil
}

func parsePlan(value map[string]any, raw []byte, config *Config) (*Plan, error) {
	if !hasKeys(value,
		"api_container_port", "api_host_ip", "api_host_port", "authority_profile", "backup_max_age_seconds", "cloudflared_image", "database_image", "database_substrate", "database_substrate_digest", "deployment_id", "envelope_sha", "executables", "github_identity_env_digest", "github_identity_env_gid", "github_identity_env_mode", "github_identity_env_path", "github_identity_env_uid", "host_machine_id_digest",
		"post_purge_root_env_digest", "pre_purge_root_env_digest", "pre_purge_runtime_inputs", "predecessor_runtime_sha", "preflight_steps", "project_name", "public_origin", "registration_email_env_digest", "registration_email_env_gid", "registration_email_env_mode", "registration_email_env_path", "registration_email_env_uid", "release_generation", "release_steps", "render_image",
		"repository", "runner_job_id", "runner_label", "runner_prerequisite_approval_payload_sha256", "runner_prerequisite_approval_sha256", "runner_prerequisite_intent_sha256", "runner_prerequisite_job_id", "runner_reservation_sha256", "runner_run_attempt", "runner_run_id", "rollback_steps", "runtime_deploy", "runtime_deploy_digest", "runtime_inputs", "runtime_retirement", "runtime_retirement_digest", "runtime_sha", "scene_video_env_digest", "scene_video_env_gid", "scene_video_env_mode", "scene_video_env_path", "scene_video_env_uid", "schema", "transaction_started_at_epoch", "verify_steps", "version", "web_image", "workflow_sha",
	) {
		return nil, fmt.Errorf("plan-shape-invalid")
	}
	boundStrings := map[string]string{
		"schema": PlanSchema, "authority_profile": "single-host-production-v2",
		"runtime_sha": config.RuntimeSHA, "workflow_sha": config.WorkflowSHA, "envelope_sha": config.EnvelopeSHA,
		"deployment_id":          config.DeploymentID,
		"host_machine_id_digest": config.HostMachineIDDigest, "repository": Repository,
		"project_name": ProjectName, "public_origin": PublicOrigin,
		"api_host_ip": config.APIHostIP,
		"web_image":   config.WebImage, "render_image": config.RenderImage,
		"cloudflared_image":                           config.CloudflaredImage,
		"database_image":                              config.DatabaseImage,
		"pre_purge_root_env_digest":                   config.PrePurgeRootEnvDigest,
		"post_purge_root_env_digest":                  config.PostPurgeRootEnvDigest,
		"runtime_retirement_digest":                   config.RuntimeRetirementDigest,
		"runtime_deploy_digest":                       config.RuntimeDeployDigest,
		"database_substrate_digest":                   config.DatabaseSubstrateDigest,
		"predecessor_runtime_sha":                     config.PredecessorRuntimeSHA,
		"runner_reservation_sha256":                   config.RunnerReservationDigest,
		"runner_label":                                config.RunnerLabel,
		"runner_prerequisite_intent_sha256":           config.RunnerPrerequisiteIntentDigest,
		"runner_prerequisite_approval_sha256":         config.RunnerPrerequisiteApprovalDigest,
		"runner_prerequisite_approval_payload_sha256": config.RunnerPrerequisiteApprovalPayloadDigest,
		"runner_prerequisite_job_id":                  config.RunnerPrerequisiteJobID,
		"runner_run_id":                               config.RunnerRunID,
		"runner_job_id":                               config.RunnerJobID,
		"github_identity_env_path":                    GoogleIdentityEnvPath, "github_identity_env_mode": "0600",
		"github_identity_env_digest":  config.GoogleIdentityEnvDigest,
		"registration_email_env_path": RegistrationEmailEnvPath, "registration_email_env_mode": "0600",
		"registration_email_env_digest": config.RegistrationEmailEnvDigest,
		"scene_video_env_path":          SceneVideoEnvPath, "scene_video_env_digest": config.SceneVideoEnvDigest,
	}
	for key, expected := range boundStrings {
		actual, ok := exactString(value[key])
		if !ok || actual != expected {
			return nil, fmt.Errorf("plan-%s-binding-invalid", key)
		}
	}
	if version, ok := exactInt(value["version"], 2, 2); !ok || version != 2 {
		return nil, fmt.Errorf("plan-version-invalid")
	}
	if port, ok := exactInt(value["api_host_port"], config.APIHostPort, config.APIHostPort); !ok || port != config.APIHostPort || port != APIHostPort {
		return nil, fmt.Errorf("plan-api-host-port-invalid")
	}
	if port, ok := exactInt(value["api_container_port"], config.APIContainerPort, config.APIContainerPort); !ok || port != config.APIContainerPort || port != APIContainerPort {
		return nil, fmt.Errorf("plan-api-container-port-invalid")
	}
	if generation, ok := exactInt(value["release_generation"], config.ReleaseGeneration, config.ReleaseGeneration); !ok || generation != config.ReleaseGeneration {
		return nil, fmt.Errorf("plan-generation-invalid")
	}
	if attempt, ok := exactInt(value["runner_run_attempt"], config.RunnerRunAttempt, config.RunnerRunAttempt); !ok || attempt != config.RunnerRunAttempt {
		return nil, fmt.Errorf("plan-runner-attempt-invalid")
	}
	if started, ok := exactInt(value["transaction_started_at_epoch"], config.TransactionStartedAtEpoch, config.TransactionStartedAtEpoch); !ok || started != config.TransactionStartedAtEpoch {
		return nil, fmt.Errorf("plan-transaction-start-invalid")
	}
	if maximumAge, ok := exactInt(value["backup_max_age_seconds"], BackupMaxAgeSeconds, BackupMaxAgeSeconds); !ok || maximumAge != config.BackupMaxAgeSeconds {
		return nil, fmt.Errorf("plan-backup-max-age-invalid")
	}
	if uid, ok := exactInt(value["github_identity_env_uid"], config.GoogleIdentityEnvUID, config.GoogleIdentityEnvUID); !ok || uid != config.GoogleIdentityEnvUID {
		return nil, fmt.Errorf("plan-google-identity-env-uid-invalid")
	}
	if gid, ok := exactInt(value["github_identity_env_gid"], config.GoogleIdentityEnvGID, config.GoogleIdentityEnvGID); !ok || gid != config.GoogleIdentityEnvGID {
		return nil, fmt.Errorf("plan-google-identity-env-gid-invalid")
	}
	if uid, ok := exactInt(value["registration_email_env_uid"], config.RegistrationEmailEnvUID, config.RegistrationEmailEnvUID); !ok || uid != config.RegistrationEmailEnvUID {
		return nil, fmt.Errorf("plan-registration-email-env-uid-invalid")
	}
	if gid, ok := exactInt(value["registration_email_env_gid"], config.RegistrationEmailEnvGID, config.RegistrationEmailEnvGID); !ok || gid != config.RegistrationEmailEnvGID {
		return nil, fmt.Errorf("plan-registration-email-env-gid-invalid")
	}
	if mode, ok := exactInt(value["scene_video_env_mode"], 384, 384); !ok || mode != 384 {
		return nil, fmt.Errorf("plan-scene-video-env-mode-invalid")
	}
	if uid, ok := exactInt(value["scene_video_env_uid"], config.SceneVideoEnvUID, config.SceneVideoEnvUID); !ok || uid != config.SceneVideoEnvUID || uid != 1000 {
		return nil, fmt.Errorf("plan-scene-video-env-uid-invalid")
	}
	if gid, ok := exactInt(value["scene_video_env_gid"], config.SceneVideoEnvGID, config.SceneVideoEnvGID); !ok || gid != config.SceneVideoEnvGID || gid != 1000 {
		return nil, fmt.Errorf("plan-scene-video-env-gid-invalid")
	}
	if !runtimeInputObservationsEqual(value["pre_purge_runtime_inputs"], config.PrePurgeRuntimeInputs) || !runtimeInputObservationsEqual(value["runtime_inputs"], config.RuntimeInputs) {
		return nil, fmt.Errorf("plan-runtime-inputs-binding-invalid")
	}
	runtimeRetirement, retirementDigest, retirementErr := validateRuntimeRetirementContract(value["runtime_retirement"], config.RuntimeSHA, config.DeploymentID)
	if retirementErr != nil || retirementDigest != config.RuntimeRetirementDigest || !canonicalValuesEqual(runtimeRetirement, config.RuntimeRetirement) {
		return nil, fmt.Errorf("plan-runtime-retirement-binding-invalid")
	}
	runtimeDeploy, deployDigest, deployErr := validateRuntimeDeployContract(value["runtime_deploy"], config.RuntimeSHA, config.DeploymentID)
	if deployErr != nil || deployDigest != config.RuntimeDeployDigest || !canonicalValuesEqual(runtimeDeploy, config.RuntimeDeploy) {
		return nil, fmt.Errorf("plan-runtime-deploy-binding-invalid")
	}
	if !databaseSubstrateValueEqual(value["database_substrate"], config.DatabaseSubstrate) {
		return nil, fmt.Errorf("plan-database-substrate-binding-invalid")
	}
	executablesValue, ok := value["executables"].(map[string]any)
	if !ok || len(executablesValue) != 4 {
		return nil, fmt.Errorf("plan-executables-invalid")
	}
	executables := make(map[string]string, len(executablesValue))
	for path, rawDigest := range executablesValue {
		expectedDigest, ok := exactString(rawDigest)
		if !ok || !digestPattern.MatchString(expectedDigest) || !validExecutablePath(path) {
			return nil, fmt.Errorf("plan-executable-invalid")
		}
		executables[path] = expectedDigest
	}
	for _, path := range []string{PredeployBackupExecutablePath, runtimeIsolationExecutablePath, DatabaseControlExecutablePath, RuntimeDeployExecutablePath} {
		if !digestPattern.MatchString(executables[path]) {
			return nil, fmt.Errorf("plan-required-executable-invalid")
		}
	}
	parse := func(key, effect string) ([]Step, error) {
		items, ok := value[key].([]any)
		if !ok || len(items) < 1 || len(items) > maximumSteps {
			return nil, fmt.Errorf("plan-%s-invalid", key)
		}
		steps := make([]Step, 0, len(items))
		seen := make(map[string]struct{}, len(items))
		for _, item := range items {
			stepValue, ok := item.(map[string]any)
			if !ok || !hasKeys(stepValue, "argv", "effect", "expected_exit_code", "id", "idempotent", "timeout_seconds") {
				return nil, fmt.Errorf("plan-step-shape-invalid")
			}
			id, idOK := exactString(stepValue["id"])
			actualEffect, effectOK := exactString(stepValue["effect"])
			timeout, timeoutOK := exactInt(stepValue["timeout_seconds"], 1, 9600)
			exitCode, exitOK := exactInt(stepValue["expected_exit_code"], 0, 0)
			idempotent, idempotentOK := stepValue["idempotent"].(bool)
			argvValue, argvOK := stepValue["argv"].([]any)
			if !idOK || !idPattern.MatchString(id) || actualEffect != effect || !effectOK || !timeoutOK || !exitOK ||
				!idempotentOK || (effect == "rollback" && !idempotent) || !argvOK || len(argvValue) < 1 || len(argvValue) > 64 {
				return nil, fmt.Errorf("plan-step-invalid")
			}
			if _, duplicate := seen[id]; duplicate {
				return nil, fmt.Errorf("plan-step-duplicate")
			}
			seen[id] = struct{}{}
			argv := make([]string, 0, len(argvValue))
			for _, rawArgument := range argvValue {
				argument, ok := exactString(rawArgument)
				if !ok || len(argument) > 4096 || containsForbiddenArgumentByte(argument) {
					return nil, fmt.Errorf("plan-step-argument-invalid")
				}
				argv = append(argv, argument)
			}
			if expectedDigest, ok := executables[argv[0]]; !ok || !digestPattern.MatchString(expectedDigest) {
				return nil, fmt.Errorf("plan-step-executable-unbound")
			}
			steps = append(steps, Step{ID: id, Effect: effect, Argv: argv, TimeoutSeconds: timeout, ExpectedExitCode: exitCode, Idempotent: idempotent})
		}
		return steps, nil
	}
	preflight, err := parse("preflight_steps", "read-only")
	if err != nil {
		return nil, err
	}
	if len(preflight) != 1 || !exactStep(preflight[0], Step{
		ID: VerifyIsolationInputsStepID, Effect: "read-only", Argv: expectedIsolationArgv(config, "verify-isolation-inputs", false, true),
		TimeoutSeconds: 600, ExpectedExitCode: 0, Idempotent: true,
	}) {
		return nil, fmt.Errorf("plan-preflight-isolation-contract-invalid")
	}
	release, err := parse("release_steps", "mutation")
	if err != nil {
		return nil, err
	}
	requiredReleaseOrder := [...]string{
		"predeploy-encrypted-backup",
		PurgeRuntimeIsolationStepID,
		RuntimeRetirementStepID,
		ProvisionDatabaseRolesStepID,
		MigrateSchemaStepID,
		HardenRuntimeACLStepID,
		VerifySchemaReadinessStepID,
		DeployRuntimeStepID,
	}
	if len(release) != len(requiredReleaseOrder) {
		return nil, fmt.Errorf("plan-release-order-invalid")
	}
	for index, expectedID := range requiredReleaseOrder {
		if release[index].ID != expectedID {
			return nil, fmt.Errorf("plan-release-order-invalid")
		}
	}
	backupReceiptPath := filepath.Join(PredeployBackupReceiptDirectory, config.RuntimeSHA, config.DeploymentID, "create.json")
	expectedBackupArgv := []string{
		PredeployBackupExecutablePath, "create",
		"--runtime-sha", config.RuntimeSHA,
		"--deployment-id", config.DeploymentID,
		"--envelope-sha", config.EnvelopeSHA,
		"--web-image", config.WebImage,
		"--render-image", config.RenderImage,
		"--database-image", config.DatabaseImage,
		"--receipt", backupReceiptPath,
		"--encryption-key", PredeployBackupEncryptionKeyPath,
	}
	if !release[0].Idempotent || release[0].TimeoutSeconds != 9600 || release[0].ExpectedExitCode != 0 || !equalStrings(release[0].Argv, expectedBackupArgv) {
		return nil, fmt.Errorf("plan-predeploy-backup-contract-invalid")
	}
	if !exactStep(release[1], Step{ID: PurgeRuntimeIsolationStepID, Effect: "mutation", Argv: expectedIsolationArgv(config, operationPurgeRuntimeIsolation, true, true), TimeoutSeconds: 600, ExpectedExitCode: 0, Idempotent: true}) {
		return nil, fmt.Errorf("plan-runtime-purge-contract-invalid")
	}
	if !exactStep(release[2], Step{ID: RuntimeRetirementStepID, Effect: "mutation", Argv: expectedIsolationArgv(config, operationRetireStaleRuntime, true, false), TimeoutSeconds: 600, ExpectedExitCode: 0, Idempotent: true}) {
		return nil, fmt.Errorf("plan-runtime-retirement-contract-invalid")
	}
	databaseContracts := [...]struct {
		stepID          string
		operation       string
		timeoutSeconds  int64
		receiptBaseName string
	}{
		{ProvisionDatabaseRolesStepID, "provision-roles", 900, "provision-roles.json"},
		{MigrateSchemaStepID, "migrate-schema", 1500, "migrate-schema.json"},
		{HardenRuntimeACLStepID, "harden-runtime-acl", 900, "harden-runtime-acl.json"},
		{VerifySchemaReadinessStepID, "verify-schema-readiness", 600, "verify-schema-readiness.json"},
	}
	for index, contract := range databaseContracts {
		step := release[index+3]
		expectedArgv := []string{
			DatabaseControlExecutablePath, contract.operation,
			"--runtime-sha", config.RuntimeSHA,
			"--deployment-id", config.DeploymentID,
			"--web-image", config.WebImage,
			"--database-image", config.DatabaseImage,
			"--receipt", filepath.Join(DatabaseReceiptDirectory, config.RuntimeSHA, config.DeploymentID, contract.receiptBaseName),
		}
		if step.ID != contract.stepID || !step.Idempotent || step.TimeoutSeconds != contract.timeoutSeconds || step.ExpectedExitCode != 0 || !equalStrings(step.Argv, expectedArgv) {
			return nil, fmt.Errorf("plan-database-control-contract-invalid")
		}
	}
	deployStep := release[len(requiredReleaseOrder)-1]
	if !exactStep(deployStep, Step{ID: DeployRuntimeStepID, Effect: "mutation", Argv: expectedRuntimeDeployArgv(config), TimeoutSeconds: 1800, ExpectedExitCode: 0, Idempotent: true}) {
		return nil, fmt.Errorf("plan-runtime-deploy-contract-invalid")
	}
	verify, err := parse("verify_steps", "verification")
	if err != nil {
		return nil, err
	}
	if len(verify) != 1 || !exactStep(verify[0], Step{ID: VerifyRuntimeIsolationStepID, Effect: "verification", Argv: expectedIsolationArgv(config, operationVerifyRuntimeIsolation, true, false), TimeoutSeconds: 600, ExpectedExitCode: 0, Idempotent: true}) {
		return nil, fmt.Errorf("plan-terminal-isolation-verification-invalid")
	}
	rollback, err := parse("rollback_steps", "rollback")
	if err != nil {
		return nil, err
	}
	if len(rollback) != 1 || !exactStep(rollback[0], Step{ID: RestoreRuntimeIsolationStepID, Effect: "rollback", Argv: expectedIsolationArgv(config, operationRestoreRuntimeIsolation, true, true), TimeoutSeconds: 600, ExpectedExitCode: 0, Idempotent: true}) {
		return nil, fmt.Errorf("plan-runtime-rollback-contract-invalid")
	}
	if stepTimeoutTotal(release)+stepTimeoutTotal(verify) != maximumReleaseVerifyStepSeconds || stepTimeoutTotal(rollback) != maximumRollbackStepSeconds ||
		time.Duration(maximumReleaseVerifyStepSeconds)*time.Second >= releaseExecutionTimeout ||
		releaseExecutionTimeout+rollbackExecutionTimeout >=
			releaseServerProtocolTimeout ||
		releaseServerProtocolTimeout >= releaseClientProtocolTimeout ||
		preflightClientProtocolTimeout+releaseClientProtocolTimeout+
			aiInstallClientProtocolTimeout >
			releaseWorkflowJobTimeout-releaseWorkflowSafetyMargin {
		return nil, fmt.Errorf("plan-lifecycle-timeout-envelope-invalid")
	}
	return &Plan{Raw: append([]byte(nil), raw...), Digest: digest(raw), Executables: executables,
		RunnerReservationDigest: config.RunnerReservationDigest, RunnerLabel: config.RunnerLabel,
		RunnerPrerequisiteIntentDigest:          config.RunnerPrerequisiteIntentDigest,
		RunnerPrerequisiteApprovalDigest:        config.RunnerPrerequisiteApprovalDigest,
		RunnerPrerequisiteApprovalPayloadDigest: config.RunnerPrerequisiteApprovalPayloadDigest,
		RunnerPrerequisiteJobID:                 config.RunnerPrerequisiteJobID,
		RunnerRunID:                             config.RunnerRunID, RunnerRunAttempt: config.RunnerRunAttempt, RunnerJobID: config.RunnerJobID,
		PreflightSteps: preflight, ReleaseSteps: release, VerifySteps: verify, RollbackSteps: rollback}, nil
}

func stepTimeoutTotal(steps []Step) int64 {
	var total int64
	for _, step := range steps {
		total += step.TimeoutSeconds
	}
	return total
}

func canonicalValuesEqual(first, second any) bool {
	firstRaw, firstErr := canonicalJSON(first)
	secondRaw, secondErr := canonicalJSON(second)
	equal := firstErr == nil && secondErr == nil && bytes.Equal(firstRaw, secondRaw)
	zero(firstRaw)
	zero(secondRaw)
	return equal
}

func exactStep(actual, expected Step) bool {
	return actual.ID == expected.ID && actual.Effect == expected.Effect && actual.TimeoutSeconds == expected.TimeoutSeconds && actual.ExpectedExitCode == expected.ExpectedExitCode && actual.Idempotent == expected.Idempotent && equalStrings(actual.Argv, expected.Argv)
}

func expectedIsolationArgv(config *Config, operation string, receipt, prePurgeDigest bool) []string {
	argv := []string{
		runtimeIsolationExecutablePath, operation,
		"--runtime-sha", config.RuntimeSHA,
		"--deployment-id", config.DeploymentID,
		"--envelope-sha", config.EnvelopeSHA,
		"--web-image", config.WebImage,
		"--render-image", config.RenderImage,
		"--cloudflared-image", config.CloudflaredImage,
		"--database-image", config.DatabaseImage,
		"--api-host-ip", config.APIHostIP,
		"--api-host-port", strconv.FormatInt(config.APIHostPort, 10),
		"--api-container-port", strconv.FormatInt(config.APIContainerPort, 10),
	}
	if prePurgeDigest {
		argv = append(argv, "--pre-purge-root-env-digest", config.PrePurgeRootEnvDigest)
	}
	if receipt {
		argv = append(argv, "--receipt", filepath.Join(runtimeIsolationReceiptDirectory, config.RuntimeSHA, config.DeploymentID, operation+".json"))
	}
	return argv
}

func expectedRuntimeDeployArgv(config *Config) []string {
	return []string{
		RuntimeDeployExecutablePath, "deploy-runtime",
		"--runtime-sha", config.RuntimeSHA,
		"--deployment-id", config.DeploymentID,
		"--envelope-sha", config.EnvelopeSHA,
		"--web-image", config.WebImage,
		"--render-image", config.RenderImage,
		"--cloudflared-image", config.CloudflaredImage,
		"--database-image", config.DatabaseImage,
		"--api-host-ip", config.APIHostIP,
		"--api-host-port", strconv.FormatInt(config.APIHostPort, 10),
		"--api-container-port", strconv.FormatInt(config.APIContainerPort, 10),
		"--receipt", filepath.Join(RuntimeDeployReceiptDirectory, config.RuntimeSHA, config.DeploymentID, "deploy-runtime.json"),
	}
}

func equalStrings(actual, expected []string) bool {
	if len(actual) != len(expected) {
		return false
	}
	for index := range expected {
		if actual[index] != expected[index] {
			return false
		}
	}
	return true
}

func validExecutablePath(path string) bool {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || len(path) > 4096 {
		return false
	}
	return regexp.MustCompile(`^/(usr/(bin|sbin|libexec/propertyquarry-release-control)|bin|sbin)/[A-Za-z0-9._/+:-]+$`).MatchString(path)
}

func containsForbiddenArgumentByte(value string) bool {
	for _, character := range []byte(value) {
		if character == 0 || character == '\n' || character == '\r' {
			return true
		}
	}
	return false
}

func validateExecutable(path, expectedDigest string) error {
	if err := validateSecureParentChain("/", path, 0); err != nil {
		return fmt.Errorf("step-executable-parent-invalid")
	}
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return fmt.Errorf("step-executable-unavailable")
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || (info.Mode().Perm() != 0o555 && info.Mode().Perm() != 0o755) || info.Size() < 1 || info.Size() > 256*1024*1024 {
		return fmt.Errorf("step-executable-metadata-invalid")
	}
	metadata, ok := info.Sys().(*syscall.Stat_t)
	if !ok || metadata.Uid != 0 || metadata.Gid != 0 || metadata.Nlink != 1 {
		return fmt.Errorf("step-executable-ownership-invalid")
	}
	hasher := sha256.New()
	if _, err := io.Copy(hasher, file); err != nil || "sha256:"+hex.EncodeToString(hasher.Sum(nil)) != expectedDigest {
		return fmt.Errorf("step-executable-digest-invalid")
	}
	after, err := file.Stat()
	if err != nil || !os.SameFile(info, after) {
		return fmt.Errorf("step-executable-changed")
	}
	return nil
}

type boundedCounter struct {
	count     int64
	truncated bool
}

func (counter *boundedCounter) Write(raw []byte) (int, error) {
	remaining := int64(maximumStepOutput) - counter.count
	if remaining > 0 {
		accepted := int64(len(raw))
		if accepted > remaining {
			accepted = remaining
		}
		counter.count += accepted
	}
	if counter.count >= maximumStepOutput && int64(len(raw)) > remaining {
		counter.truncated = true
	}
	return len(raw), nil
}

func runStep(parent context.Context, step Step, executables map[string]string, now func() time.Time) StepResult {
	started := now().UTC()
	result := StepResult{ID: step.ID, Effect: step.Effect, ExitCode: 255, StartedAt: started.Unix()}
	expectedDigest, ok := executables[step.Argv[0]]
	if !ok || validateExecutable(step.Argv[0], expectedDigest) != nil {
		result.FinishedAt = now().UTC().Unix()
		return result
	}
	ctx, cancel := context.WithTimeout(parent, time.Duration(step.TimeoutSeconds)*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, step.Argv[0], step.Argv[1:]...)
	command.Env = sanitizedStepEnvironment()
	command.Dir = "/"
	command.Stdin = nil
	stdout := &boundedCounter{}
	stderr := &boundedCounter{}
	command.Stdout = stdout
	command.Stderr = stderr
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGKILL}
	command.WaitDelay = 5 * time.Second
	command.Cancel = func() error {
		if command.Process == nil {
			return nil
		}
		if err := syscall.Kill(-command.Process.Pid, syscall.SIGKILL); err != nil && err != syscall.ESRCH {
			return err
		}
		return nil
	}
	err := command.Start()
	pid := 0
	if err == nil {
		pid = command.Process.Pid
		err = command.Wait()
	}
	groupClean := pid == 0 || terminateProcessGroup(pid) == nil
	result.StdoutBytes, result.StderrBytes = stdout.count, stderr.count
	result.StdoutTruncated, result.StderrTruncated = stdout.truncated, stderr.truncated
	if command.ProcessState != nil {
		result.ExitCode = command.ProcessState.ExitCode()
		if result.ExitCode < 0 {
			result.ExitCode = 255
		}
	}
	result.FinishedAt = now().UTC().Unix()
	result.Succeeded = err == nil && result.ExitCode == int(step.ExpectedExitCode) && ctx.Err() == nil && groupClean && !stdout.truncated && !stderr.truncated
	return result
}

func sanitizedStepEnvironment() []string {
	return []string{"HOME=/nonexistent", "LANG=C.UTF-8", "LC_ALL=C.UTF-8", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "TZ=UTC"}
}

func terminateProcessGroup(pid int) error {
	if pid < 1 {
		return fmt.Errorf("process-group-invalid")
	}
	if err := syscall.Kill(-pid, syscall.SIGKILL); err != nil && err != syscall.ESRCH {
		return err
	}
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		err := syscall.Kill(-pid, 0)
		if err == syscall.ESRCH {
			return nil
		}
		if err != nil && err != syscall.EPERM {
			return err
		}
		time.Sleep(10 * time.Millisecond)
	}
	return fmt.Errorf("process-group-not-empty")
}

func resultValue(result StepResult) map[string]any {
	return map[string]any{
		"effect": result.Effect, "exit_code": json.Number(strconv.Itoa(result.ExitCode)),
		"finished_at": json.Number(strconv.FormatInt(result.FinishedAt, 10)), "id": result.ID,
		"started_at":       json.Number(strconv.FormatInt(result.StartedAt, 10)),
		"stderr_bytes":     json.Number(strconv.FormatInt(result.StderrBytes, 10)),
		"stderr_truncated": result.StderrTruncated, "stdout_bytes": json.Number(strconv.FormatInt(result.StdoutBytes, 10)),
		"stdout_truncated": result.StdoutTruncated, "succeeded": result.Succeeded,
	}
}
