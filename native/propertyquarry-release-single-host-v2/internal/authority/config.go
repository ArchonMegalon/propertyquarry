package authority

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
)

const (
	ProfileSchema                         = "propertyquarry.release-control.single-host-profile.v2"
	PlanSchema                            = "propertyquarry.release-control.single-host-transaction-plan.v2"
	Repository                            = "ArchonMegalon/propertyquarry"
	RepositoryID                          = "1257593732"
	RepositoryOwnerID                     = "11421547"
	ImmutableOIDCSubjectPrefix            = "repo:ArchonMegalon@" + RepositoryOwnerID + "/propertyquarry@" + RepositoryID
	WorkflowRef                           = "ArchonMegalon/propertyquarry/.github/workflows/smoke-runtime.yml@refs/heads/main"
	ReleaseJob                            = "propertyquarry-release-v2"
	Environment                           = "propertyquarry-production"
	Audience                              = "propertyquarry-release-single-host-v2"
	ProjectName                           = "property"
	PublicOrigin                          = "https://propertyquarry.com"
	APIHostIP                             = "127.0.0.1"
	APIHostPort                     int64 = 8097
	APIContainerPort                int64 = 8090
	DatabaseImage                         = "postgres:16-alpine@sha256:16bc17c64a573ef34162af9298258d1aec548232985b33ed7b1eac33ba35c229"
	ConfigPath                            = "/etc/propertyquarry-release-single-host-v2/authority.v2.json"
	ConfigSignaturePath                   = "/etc/propertyquarry-release-single-host-v2/authority.v2.sig"
	PackageAnchorPath                     = "/etc/propertyquarry-release-single-host-v2/package-authority-v2.pem"
	ReceiptKeyPath                        = "/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.key"
	ReceiptAnchorPath                     = "/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.pem"
	PlanPath                              = "/etc/propertyquarry-release-single-host-v2/transaction-plan.v2.json"
	BaseEnvironmentPath                   = "/docker/property/.env"
	SceneVideoEnvPath                     = "/docker/property/state/runtime/property_scene_video_shared.env"
	AdmissionEnvPath                      = "/docker/property/state/runtime/propertyquarry_admission.env"
	GoogleIdentityEnvPath                 = "/docker/property/state/runtime/propertyquarry_google_identity.env"
	RegistrationEmailEnvPath              = "/docker/property/state/runtime/propertyquarry_registration_email.env"
	JournalPath                           = "/var/lib/propertyquarry-release-single-host-v2/journal"
	SocketPath                            = "/run/propertyquarry-release-single-host-v2/request.sock"
	configDomain                          = "propertyquarry.release-control.single-host-profile-signature.v2\x00"
	maximumConfigBytes                    = 262144
	BackupMaxAgeSeconds             int64 = 3600
	LegacyRegistrationEmailKeyCount int64 = 8
	RegistrationEmailKeyCount       int64 = 10
)

var imagePattern = regexp.MustCompile(`^ghcr\.io/archonmegalon/propertyquarry-standalone-(web|render)-runtime@sha256:[0-9a-f]{64}$`)
var cloudflaredImagePattern = regexp.MustCompile(`^cloudflare/cloudflared@sha256:[0-9a-f]{64}$`)
var deploymentIDPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)
var envelopeSHAPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

func RegistrationEmailEnvironmentNames() []string {
	return []string{
		"EMAILIT_API_KEY",
		"PROPERTYQUARRY_CLOUDFLARE_EMAIL_API_TOKEN",
		"PROPERTYQUARRY_CLOUDFLARE_EMAIL_ACCOUNT_ID",
		"EA_REGISTRATION_EMAIL_FROM",
		"EA_REGISTRATION_EMAIL_NAME",
		"EA_REGISTRATION_EMAIL_FROM_FALLBACK",
		"EA_REGISTRATION_EMAIL_NAME_FALLBACK",
		"EA_REGISTRATION_EMAIL_FORCE_FALLBACK",
		"EA_EMAIL_DEFAULT_FROM",
		"EA_EMAIL_DEFAULT_NAME",
	}
}

type Config struct {
	Raw                                     []byte
	Digest                                  string
	HostMachineID                           string
	HostMachineIDDigest                     string
	RuntimeSHA                              string
	WorkflowSHA                             string
	DeploymentID                            string
	TransactionStartedAtEpoch               int64
	BackupMaxAgeSeconds                     int64
	EnvelopeSHA                             string
	ReleaseGeneration                       int64
	PredecessorRuntimeSHA                   string
	WebImage                                string
	RenderImage                             string
	CloudflaredImage                        string
	DatabaseImage                           string
	APIHostIP                               string
	APIHostPort                             int64
	APIContainerPort                        int64
	PrePurgeRootEnvDigest                   string
	PostPurgeRootEnvDigest                  string
	PrePurgeRuntimeInputs                   []runtimeInputObservation
	RuntimeInputs                           []runtimeInputObservation
	PrePurgeRuntimeInputsDigest             string
	RuntimeInputsDigest                     string
	RuntimeRetirement                       map[string]any
	RuntimeRetirementDigest                 string
	RuntimeDeploy                           map[string]any
	RuntimeDeployDigest                     string
	DatabaseSubstrate                       *databaseSubstrate
	DatabaseSubstrateDigest                 string
	PlanDigest                              string
	PackageAuthorityKeyID                   string
	ReceiptAuthorityKeyID                   string
	AllowedRunnerUID                        int64
	AllowedRunnerGID                        int64
	PreflightTTLSeconds                     int64
	GitHubAPICredentialPath                 string
	GitHubOIDCRequestOrigin                 string
	RepositoryID                            string
	RepositoryOwnerID                       string
	EphemeralRunnerLabelPrefix              string
	RunnerReservationDigest                 string
	RunnerLabel                             string
	RunnerPrerequisiteIntentDigest          string
	RunnerPrerequisiteApprovalDigest        string
	RunnerPrerequisiteApprovalPayloadDigest string
	RunnerPrerequisiteJobID                 string
	RunnerRunID                             string
	RunnerRunAttempt                        int64
	RunnerJobID                             string
	GoogleIdentityEnvDigest                 string
	GoogleIdentityEnvUID                    int64
	GoogleIdentityEnvGID                    int64
	RegistrationEmailEnvDigest              string
	RegistrationEmailEnvUID                 int64
	RegistrationEmailEnvGID                 int64
	SceneVideoEnvDigest                     string
	SceneVideoEnvUID                        int64
	SceneVideoEnvGID                        int64
}

func (config *Config) release() {
	if config == nil {
		return
	}
	zero(config.Raw)
	*config = Config{}
}

// Release clears a detached or installed authority profile after validation.
func (config *Config) Release() {
	config.release()
}

// ValidateDetachedConfigPlan applies the production profile and transaction
// validators to signed package payloads before they are installed. External
// host files are intentionally validated later by the installer's host-binding
// gate and again by LoadConfig after installation.
func ValidateDetachedConfigPlan(configRaw, planRaw []byte, packageKeyID string) (*Config, *Plan, error) {
	if len(configRaw) < 2 || len(configRaw) > maximumConfigBytes || len(planRaw) < 2 || len(planRaw) > maximumPlanBytes || !digestPattern.MatchString(packageKeyID) {
		return nil, nil, fmt.Errorf("detached-authority-input-invalid")
	}
	configValue, err := strictJSON(configRaw, maximumConfigBytes)
	if err != nil {
		return nil, nil, fmt.Errorf("detached-authority-config-invalid")
	}
	config, err := parseConfigWithExternalValidation(configValue, configRaw, packageKeyID, "/", false)
	if err != nil {
		return nil, nil, err
	}
	if digest(planRaw) != config.PlanDigest {
		config.release()
		return nil, nil, fmt.Errorf("detached-authority-plan-digest-invalid")
	}
	planValue, err := strictJSON(planRaw, maximumPlanBytes)
	if err != nil {
		config.release()
		return nil, nil, fmt.Errorf("detached-authority-plan-invalid")
	}
	plan, err := parsePlan(planValue, planRaw, config)
	if err != nil {
		config.release()
		return nil, nil, err
	}
	return config, plan, nil
}

func LoadConfig(root string) (*Config, ed25519.PrivateKey, error) {
	if root == "" {
		root = "/"
	}
	ownerUID, ownerGID := secureOwner(root)
	configRaw, err := secureRead(root, ConfigPath, 0o400, ownerUID, ownerGID, maximumConfigBytes)
	if err != nil {
		return nil, nil, fmt.Errorf("config-unavailable")
	}
	signature, err := secureRead(root, ConfigSignaturePath, 0o444, ownerUID, ownerGID, ed25519.SignatureSize)
	if err != nil || len(signature) != ed25519.SignatureSize {
		zero(configRaw)
		zero(signature)
		return nil, nil, fmt.Errorf("config-signature-unavailable")
	}
	packageAnchorRaw, err := secureRead(root, PackageAnchorPath, 0o444, ownerUID, ownerGID, 4096)
	if err != nil {
		zero(configRaw)
		zero(signature)
		return nil, nil, fmt.Errorf("package-anchor-unavailable")
	}
	packageAnchor, packageKeyID, err := parsePublicKey(packageAnchorRaw)
	zero(packageAnchorRaw)
	if err != nil || !ed25519.Verify(packageAnchor, framed(configDomain, configRaw), signature) {
		zero(configRaw)
		zero(signature)
		zero(packageAnchor)
		return nil, nil, fmt.Errorf("config-authentication-failed")
	}
	zero(signature)
	zero(packageAnchor)
	value, err := strictJSON(configRaw, maximumConfigBytes)
	if err != nil {
		zero(configRaw)
		return nil, nil, err
	}
	config, err := parseConfig(value, configRaw, packageKeyID, root)
	if err != nil {
		zero(configRaw)
		return nil, nil, err
	}
	receiptKeyRaw, err := secureRead(root, ReceiptKeyPath, 0o400, ownerUID, ownerGID, 4096)
	if err != nil {
		config.release()
		return nil, nil, fmt.Errorf("receipt-key-unavailable")
	}
	receiptKey, err := parsePrivateKey(receiptKeyRaw)
	zero(receiptKeyRaw)
	if err != nil {
		config.release()
		return nil, nil, fmt.Errorf("receipt-key-invalid")
	}
	receiptAnchorRaw, err := secureRead(root, ReceiptAnchorPath, 0o444, ownerUID, ownerGID, 4096)
	if err != nil {
		config.release()
		zero(receiptKey)
		return nil, nil, fmt.Errorf("receipt-anchor-unavailable")
	}
	receiptAnchor, receiptKeyID, err := parsePublicKey(receiptAnchorRaw)
	zero(receiptAnchorRaw)
	if err != nil || !bytes.Equal(receiptAnchor, receiptKey.Public().(ed25519.PublicKey)) || receiptKeyID != config.ReceiptAuthorityKeyID {
		config.release()
		zero(receiptKey)
		zero(receiptAnchor)
		return nil, nil, fmt.Errorf("receipt-key-binding-invalid")
	}
	zero(receiptAnchor)
	return config, receiptKey, nil
}

func parseConfig(value map[string]any, raw []byte, packageKeyID, root string) (*Config, error) {
	return parseConfigWithExternalValidation(value, raw, packageKeyID, root, true)
}

func parseConfigWithExternalValidation(value map[string]any, raw []byte, packageKeyID, root string, validateExternal bool) (*Config, error) {
	keys := []string{"allowed_runner_gid", "allowed_runner_uid", "api_container_port", "api_host_ip", "api_host_port", "authority_profile", "backup_max_age_seconds", "cloudflared_image", "database_image", "database_substrate", "database_substrate_digest", "deployment_id", "envelope_sha", "environment", "ephemeral_runner_label_prefix", "github_api_credential_path", "github_identity_env_digest", "github_identity_env_gid", "github_identity_env_mode", "github_identity_env_path", "github_identity_env_uid", "github_oidc_request_origin", "host_machine_id_digest", "package_authority_key_id", "plan_digest", "post_purge_root_env_digest", "pre_purge_root_env_digest", "pre_purge_runtime_inputs", "predecessor_runtime_sha", "preflight_ttl_seconds", "project_name", "public_origin", "receipt_authority_key_id", "registration_email_env_digest", "registration_email_env_gid", "registration_email_env_mode", "registration_email_env_path", "registration_email_env_uid", "release_generation", "release_job", "render_image", "repository", "repository_id", "repository_owner_id", "runner_job_id", "runner_label", "runner_prerequisite_approval_payload_sha256", "runner_prerequisite_approval_sha256", "runner_prerequisite_intent_sha256", "runner_prerequisite_job_id", "runner_reservation_sha256", "runner_run_attempt", "runner_run_id", "runtime_deploy", "runtime_deploy_digest", "runtime_inputs", "runtime_retirement", "runtime_retirement_digest", "runtime_sha", "scene_video_env_digest", "scene_video_env_gid", "scene_video_env_mode", "scene_video_env_path", "scene_video_env_uid", "schema", "transaction_started_at_epoch", "version", "web_image", "workflow_ref", "workflow_sha"}
	if !hasKeys(value, keys...) {
		return nil, fmt.Errorf("config-shape-invalid")
	}
	get := func(key string) (string, error) {
		text, ok := exactString(value[key])
		if !ok {
			return "", fmt.Errorf("config-%s-invalid", key)
		}
		return text, nil
	}
	schema, _ := get("schema")
	profile, _ := get("authority_profile")
	runtimeSHA, _ := get("runtime_sha")
	workflowSHA, _ := get("workflow_sha")
	deploymentID, _ := get("deployment_id")
	envelopeSHA, _ := get("envelope_sha")
	repository, _ := get("repository")
	workflowRef, _ := get("workflow_ref")
	releaseJob, _ := get("release_job")
	environment, _ := get("environment")
	projectName, _ := get("project_name")
	publicOrigin, _ := get("public_origin")
	apiHostIP, _ := get("api_host_ip")
	webImage, _ := get("web_image")
	renderImage, _ := get("render_image")
	cloudflaredImage, _ := get("cloudflared_image")
	databaseImage, _ := get("database_image")
	planDigest, _ := get("plan_digest")
	prePurgeRootEnvDigest, _ := get("pre_purge_root_env_digest")
	postPurgeRootEnvDigest, _ := get("post_purge_root_env_digest")
	runtimeRetirementDigest, _ := get("runtime_retirement_digest")
	runtimeDeployDigest, _ := get("runtime_deploy_digest")
	databaseSubstrateDigest, _ := get("database_substrate_digest")
	hostDigest, _ := get("host_machine_id_digest")
	configuredPackageKeyID, _ := get("package_authority_key_id")
	receiptKeyID, _ := get("receipt_authority_key_id")
	predecessorRuntimeSHA, _ := get("predecessor_runtime_sha")
	credentialPath, _ := get("github_api_credential_path")
	oidcOrigin, _ := get("github_oidc_request_origin")
	repositoryID, _ := get("repository_id")
	repositoryOwnerID, _ := get("repository_owner_id")
	labelPrefix, _ := get("ephemeral_runner_label_prefix")
	runnerReservationDigest, _ := get("runner_reservation_sha256")
	runnerLabel, _ := get("runner_label")
	runnerPrerequisiteIntentDigest, _ := get("runner_prerequisite_intent_sha256")
	runnerPrerequisiteApprovalDigest, _ := get("runner_prerequisite_approval_sha256")
	runnerPrerequisiteApprovalPayloadDigest, _ := get("runner_prerequisite_approval_payload_sha256")
	runnerPrerequisiteJobID, _ := get("runner_prerequisite_job_id")
	runnerRunID, _ := get("runner_run_id")
	runnerJobID, _ := get("runner_job_id")
	googleIdentityEnvPath, _ := get("github_identity_env_path")
	googleIdentityEnvMode, _ := get("github_identity_env_mode")
	googleIdentityEnvDigest, _ := get("github_identity_env_digest")
	registrationEmailEnvPath, _ := get("registration_email_env_path")
	registrationEmailEnvMode, _ := get("registration_email_env_mode")
	registrationEmailEnvDigest, _ := get("registration_email_env_digest")
	sceneVideoEnvPath, _ := get("scene_video_env_path")
	sceneVideoEnvDigest, _ := get("scene_video_env_digest")
	version, versionOK := exactInt(value["version"], 2, 2)
	runnerUID, uidOK := exactInt(value["allowed_runner_uid"], 1, 1<<31-1)
	runnerGID, gidOK := exactInt(value["allowed_runner_gid"], 1, 1<<31-1)
	ttl, ttlOK := exactInt(value["preflight_ttl_seconds"], 30, 900)
	releaseGeneration, generationOK := exactInt(value["release_generation"], 1, 1<<62)
	transactionStartedAt, transactionStartedOK := exactInt(value["transaction_started_at_epoch"], 1, 1<<62)
	backupMaxAge, backupMaxAgeOK := exactInt(value["backup_max_age_seconds"], BackupMaxAgeSeconds, BackupMaxAgeSeconds)
	googleIdentityEnvUID, googleUIDOK := exactInt(value["github_identity_env_uid"], 0, 1<<31-1)
	googleIdentityEnvGID, googleGIDOK := exactInt(value["github_identity_env_gid"], 0, 1<<31-1)
	registrationEmailEnvUID, emailUIDOK := exactInt(value["registration_email_env_uid"], 0, 1<<31-1)
	registrationEmailEnvGID, emailGIDOK := exactInt(value["registration_email_env_gid"], 0, 1<<31-1)
	sceneVideoEnvMode, sceneModeOK := exactInt(value["scene_video_env_mode"], 384, 384)
	sceneVideoEnvUID, sceneUIDOK := exactInt(value["scene_video_env_uid"], 1000, 1000)
	sceneVideoEnvGID, sceneGIDOK := exactInt(value["scene_video_env_gid"], 1000, 1000)
	runnerRunAttempt, runnerAttemptOK := exactInt(value["runner_run_attempt"], 1, 1<<31-1)
	apiHostPort, apiHostPortOK := exactInt(value["api_host_port"], APIHostPort, APIHostPort)
	apiContainerPort, apiContainerPortOK := exactInt(value["api_container_port"], APIContainerPort, APIContainerPort)
	prePurgeRuntimeInputs, runtimeInputs, runtimeInputsErr := validateSignedRuntimeInputs(value["pre_purge_runtime_inputs"], value["runtime_inputs"])
	prePurgeRuntimeInputsRaw, prePurgeRuntimeInputsCanonicalErr := canonicalJSON(value["pre_purge_runtime_inputs"])
	runtimeInputsRaw, runtimeInputsCanonicalErr := canonicalJSON(value["runtime_inputs"])
	defer zero(prePurgeRuntimeInputsRaw)
	defer zero(runtimeInputsRaw)
	runtimeRetirement, observedRetirementDigest, retirementErr := validateRuntimeRetirementContract(value["runtime_retirement"], runtimeSHA, deploymentID)
	runtimeDeploy, observedDeployDigest, deployErr := validateRuntimeDeployContract(value["runtime_deploy"], runtimeSHA, deploymentID)
	databaseSubstrate, substrateErr := validateDatabaseSubstrate(value["database_substrate"], databaseImage)
	if schema != ProfileSchema || profile != "single-host-production-v2" || version != 2 || !versionOK ||
		!shaPattern.MatchString(runtimeSHA) || !shaPattern.MatchString(workflowSHA) || workflowSHA == runtimeSHA || !deploymentIDPattern.MatchString(deploymentID) || !envelopeSHAPattern.MatchString(envelopeSHA) || repository != Repository ||
		workflowRef != WorkflowRef || releaseJob != ReleaseJob || environment != Environment ||
		projectName != ProjectName || publicOrigin != PublicOrigin || apiHostIP != APIHostIP || !apiHostPortOK || apiHostPort != APIHostPort || !apiContainerPortOK || apiContainerPort != APIContainerPort || !uidOK || !gidOK || !ttlOK || !generationOK || !transactionStartedOK || !backupMaxAgeOK || backupMaxAge != BackupMaxAgeSeconds ||
		!imagePattern.MatchString(webImage) || !strings.HasPrefix(webImage, "ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime@sha256:") ||
		!imagePattern.MatchString(renderImage) || !strings.HasPrefix(renderImage, "ghcr.io/archonmegalon/propertyquarry-standalone-render-runtime@sha256:") || webImage == renderImage || !cloudflaredImagePattern.MatchString(cloudflaredImage) || databaseImage != DatabaseImage ||
		(predecessorRuntimeSHA != "genesis" && !shaPattern.MatchString(predecessorRuntimeSHA)) ||
		!digestPattern.MatchString(planDigest) || !digestPattern.MatchString(hostDigest) || !digestPattern.MatchString(prePurgeRootEnvDigest) || !digestPattern.MatchString(postPurgeRootEnvDigest) ||
		runtimeInputsErr != nil || prePurgeRuntimeInputsCanonicalErr != nil || runtimeInputsCanonicalErr != nil || len(prePurgeRuntimeInputs) != len(runtimeIsolationInputPaths) || prePurgeRootEnvDigest != prePurgeRuntimeInputs[0].digest || postPurgeRootEnvDigest != runtimeInputs[0].digest ||
		retirementErr != nil || runtimeRetirementDigest != observedRetirementDigest || deployErr != nil || runtimeDeployDigest != observedDeployDigest || substrateErr != nil || databaseSubstrateDigest != databaseSubstrate.digest ||
		configuredPackageKeyID != packageKeyID || !digestPattern.MatchString(receiptKeyID) ||
		credentialPath != "/run/credentials/propertyquarry-release-single-host-v2.service/github-api-token" ||
		oidcOrigin != "https://vstoken.actions.githubusercontent.com" || !decimal(repositoryID) ||
		repositoryID != RepositoryID || repositoryOwnerID != RepositoryOwnerID || labelPrefix != "pqrelease-" || googleIdentityEnvPath != GoogleIdentityEnvPath ||
		!digestPattern.MatchString(runnerReservationDigest) || !runnerLabelPattern.MatchString(runnerLabel) ||
		!digestPattern.MatchString(runnerPrerequisiteIntentDigest) || !digestPattern.MatchString(runnerPrerequisiteApprovalDigest) || !digestPattern.MatchString(runnerPrerequisiteApprovalPayloadDigest) || !decimal(runnerPrerequisiteJobID) ||
		!decimal(runnerRunID) || !runnerAttemptOK || !decimal(runnerJobID) || runnerJobID == runnerPrerequisiteJobID ||
		googleIdentityEnvMode != "0600" || !digestPattern.MatchString(googleIdentityEnvDigest) || !googleUIDOK || !googleGIDOK ||
		registrationEmailEnvPath != RegistrationEmailEnvPath || registrationEmailEnvMode != "0600" ||
		!digestPattern.MatchString(registrationEmailEnvDigest) || !emailUIDOK || !emailGIDOK ||
		sceneVideoEnvPath != SceneVideoEnvPath || !digestPattern.MatchString(sceneVideoEnvDigest) || !sceneModeOK || sceneVideoEnvMode != 384 || !sceneUIDOK || !sceneGIDOK ||
		sceneVideoEnvDigest != prePurgeRuntimeInputs[1].digest || sceneVideoEnvUID != prePurgeRuntimeInputs[1].uid || sceneVideoEnvGID != prePurgeRuntimeInputs[1].gid ||
		googleIdentityEnvDigest != prePurgeRuntimeInputs[4].digest || googleIdentityEnvUID != prePurgeRuntimeInputs[4].uid || googleIdentityEnvGID != prePurgeRuntimeInputs[4].gid ||
		registrationEmailEnvDigest != prePurgeRuntimeInputs[5].digest || registrationEmailEnvUID != prePurgeRuntimeInputs[5].uid || registrationEmailEnvGID != prePurgeRuntimeInputs[5].gid {
		return nil, fmt.Errorf("config-binding-invalid")
	}
	machineID := ""
	if validateExternal {
		machineUID, machineGID := secureOwner(root)
		machineIDRaw, err := secureRead(root, "/etc/machine-id", 0o444, machineUID, machineGID, 64)
		if err != nil {
			return nil, fmt.Errorf("machine-id-unavailable")
		}
		machineID = strings.TrimSpace(string(machineIDRaw))
		zero(machineIDRaw)
		if !regexp.MustCompile(`^[0-9a-f]{32}$`).MatchString(machineID) || digest([]byte(machineID)) != hostDigest {
			return nil, fmt.Errorf("host-binding-invalid")
		}
		if err := validateGoogleIdentityEnvelope(root, uint32(googleIdentityEnvUID), uint32(googleIdentityEnvGID), googleIdentityEnvDigest); err != nil {
			return nil, err
		}
		if err := validateRegistrationEmailEnvelope(root, uint32(registrationEmailEnvUID), uint32(registrationEmailEnvGID), registrationEmailEnvDigest); err != nil {
			return nil, err
		}
		if err := validateExternalDigestFile(root, SceneVideoEnvPath, 0o600, uint32(sceneVideoEnvUID), uint32(sceneVideoEnvGID), sceneVideoEnvDigest, 256*1024); err != nil {
			return nil, fmt.Errorf("scene-video-envelope-invalid")
		}
		if err := validateCurrentRuntimeInputs(root, prePurgeRuntimeInputs, runtimeInputs); err != nil {
			return nil, err
		}
	}
	return &Config{
		Raw: append([]byte(nil), raw...), Digest: digest(raw), HostMachineID: machineID,
		HostMachineIDDigest: hostDigest, RuntimeSHA: runtimeSHA, WorkflowSHA: workflowSHA, DeploymentID: deploymentID, EnvelopeSHA: envelopeSHA,
		TransactionStartedAtEpoch: transactionStartedAt, BackupMaxAgeSeconds: backupMaxAge,
		ReleaseGeneration: releaseGeneration, PredecessorRuntimeSHA: predecessorRuntimeSHA,
		WebImage: webImage, RenderImage: renderImage, CloudflaredImage: cloudflaredImage, DatabaseImage: databaseImage,
		APIHostIP: apiHostIP, APIHostPort: apiHostPort, APIContainerPort: apiContainerPort,
		PrePurgeRootEnvDigest: prePurgeRootEnvDigest, PostPurgeRootEnvDigest: postPurgeRootEnvDigest,
		PrePurgeRuntimeInputs: prePurgeRuntimeInputs, RuntimeInputs: runtimeInputs,
		PrePurgeRuntimeInputsDigest: digest(prePurgeRuntimeInputsRaw), RuntimeInputsDigest: digest(runtimeInputsRaw),
		RuntimeRetirement: runtimeRetirement, RuntimeRetirementDigest: runtimeRetirementDigest,
		RuntimeDeploy: runtimeDeploy, RuntimeDeployDigest: runtimeDeployDigest,
		DatabaseSubstrate: databaseSubstrate, DatabaseSubstrateDigest: databaseSubstrateDigest,
		PlanDigest:            planDigest,
		PackageAuthorityKeyID: packageKeyID, ReceiptAuthorityKeyID: receiptKeyID,
		AllowedRunnerUID: runnerUID, AllowedRunnerGID: runnerGID, PreflightTTLSeconds: ttl,
		GitHubAPICredentialPath: credentialPath, GitHubOIDCRequestOrigin: oidcOrigin,
		RepositoryID: repositoryID, RepositoryOwnerID: repositoryOwnerID,
		EphemeralRunnerLabelPrefix: labelPrefix,
		RunnerReservationDigest:    runnerReservationDigest, RunnerLabel: runnerLabel,
		RunnerPrerequisiteIntentDigest:          runnerPrerequisiteIntentDigest,
		RunnerPrerequisiteApprovalDigest:        runnerPrerequisiteApprovalDigest,
		RunnerPrerequisiteApprovalPayloadDigest: runnerPrerequisiteApprovalPayloadDigest,
		RunnerPrerequisiteJobID:                 runnerPrerequisiteJobID,
		RunnerRunID:                             runnerRunID, RunnerRunAttempt: runnerRunAttempt, RunnerJobID: runnerJobID,
		GoogleIdentityEnvDigest: googleIdentityEnvDigest, GoogleIdentityEnvUID: googleIdentityEnvUID, GoogleIdentityEnvGID: googleIdentityEnvGID,
		RegistrationEmailEnvDigest: registrationEmailEnvDigest, RegistrationEmailEnvUID: registrationEmailEnvUID, RegistrationEmailEnvGID: registrationEmailEnvGID,
		SceneVideoEnvDigest: sceneVideoEnvDigest, SceneVideoEnvUID: sceneVideoEnvUID, SceneVideoEnvGID: sceneVideoEnvGID,
	}, nil
}

func validateExternalDigestFile(root, absolutePath string, mode uint32, uid, gid uint32, expectedDigest string, maximum int) error {
	path := rooted(root, absolutePath)
	if !digestPattern.MatchString(expectedDigest) {
		return fmt.Errorf("external-digest-file-digest-invalid")
	}
	if err := validateExternalParentChain(root, path, uid, gid); err != nil {
		return fmt.Errorf("external-digest-file-parent-invalid")
	}
	raw, err := readSecureFile(path, mode, uid, gid, maximum)
	if err != nil {
		return fmt.Errorf("external-digest-file-unavailable")
	}
	defer zero(raw)
	if digest(raw) != expectedDigest {
		return fmt.Errorf("external-digest-file-binding-invalid")
	}
	return nil
}

func validateGoogleIdentityEnvelope(root string, uid, gid uint32, expectedDigest string) error {
	return validateExactExternalEnvelope(root, GoogleIdentityEnvPath, uid, gid, expectedDigest, "google-identity", []string{
		"PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID",
		"PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET",
		"PROPERTYQUARRY_GOOGLE_OAUTH_REDIRECT_URI",
		"PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET",
		"PROPERTYQUARRY_IDENTITY_SESSION_SECRET",
	}, false)
}

func validateRegistrationEmailEnvelope(root string, uid, gid uint32, expectedDigest string) error {
	return validateExactExternalEnvelope(
		root, RegistrationEmailEnvPath, uid, gid, expectedDigest,
		"registration-email", RegistrationEmailEnvironmentNames(), true,
	)
}

func validateExactExternalEnvelope(root, absolutePath string, uid, gid uint32, expectedDigest, label string, expectedNames []string, ordered bool) error {
	path := rooted(root, absolutePath)
	if err := validateExternalParentChain(root, path, uid, gid); err != nil {
		return fmt.Errorf("%s-envelope-parent-invalid", label)
	}
	raw, err := readSecureFile(path, 0o600, uid, gid, 32*1024)
	if err != nil {
		return fmt.Errorf("%s-envelope-unavailable", label)
	}
	defer zero(raw)
	if digest(raw) != expectedDigest || len(raw) < 1 || raw[len(raw)-1] != '\n' || bytes.IndexAny(raw, "\x00\r") >= 0 {
		return fmt.Errorf("%s-envelope-digest-invalid", label)
	}
	lines := bytes.Split(raw[:len(raw)-1], []byte{'\n'})
	if len(lines) != len(expectedNames) {
		return fmt.Errorf("%s-envelope-shape-invalid", label)
	}
	expected := make(map[string]bool, len(expectedNames))
	for _, name := range expectedNames {
		expected[name] = false
	}
	for index, line := range lines {
		parts := bytes.SplitN(line, []byte{'='}, 2)
		if len(parts) != 2 || !validLiteralEnvironmentValue(parts[1]) {
			return fmt.Errorf("%s-envelope-entry-invalid", label)
		}
		name := string(parts[0])
		seen, allowed := expected[name]
		if !allowed || seen || (ordered && name != expectedNames[index]) {
			return fmt.Errorf("%s-envelope-name-invalid", label)
		}
		if name == "EA_REGISTRATION_EMAIL_FORCE_FALLBACK" && !bytes.Equal(parts[1], []byte("true")) && !bytes.Equal(parts[1], []byte("false")) {
			return fmt.Errorf("%s-envelope-entry-invalid", label)
		}
		expected[name] = true
	}
	return nil
}

func validLiteralEnvironmentValue(value []byte) bool {
	if len(value) == 0 || value[0] == ' ' || value[len(value)-1] == ' ' {
		return false
	}
	for _, character := range value {
		if character < 0x20 || character > 0x7e || character == '$' || character == '\'' || character == '"' || character == '\\' || character == '#' || character == '`' {
			return false
		}
	}
	return true
}

func validateExternalParentChain(root, path string, uid, gid uint32) error {
	boundary := filepath.Clean(root)
	if boundary == "." || !filepath.IsAbs(boundary) {
		return fmt.Errorf("external-root-invalid")
	}
	clean := filepath.Clean(path)
	relative, err := filepath.Rel(boundary, clean)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(os.PathSeparator)) {
		return fmt.Errorf("external-path-escape")
	}
	current := filepath.Dir(clean)
	for {
		info, err := os.Lstat(current)
		if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0o002 != 0 {
			return fmt.Errorf("external-parent-invalid")
		}
		metadata, ok := info.Sys().(*syscall.Stat_t)
		if !ok || (metadata.Uid != 0 && metadata.Uid != uid) || (info.Mode().Perm()&0o020 != 0 && (metadata.Uid != uid || metadata.Gid != gid)) {
			return fmt.Errorf("external-parent-owner-invalid")
		}
		if current == boundary {
			return nil
		}
		next := filepath.Dir(current)
		if next == current {
			return fmt.Errorf("external-parent-boundary-missing")
		}
		current = next
	}
}

func secureOwner(root string) (uint32, uint32) {
	if root == "" || root == "/" {
		return 0, 0
	}
	return uint32(os.Geteuid()), uint32(os.Getegid())
}

func rooted(root, absolute string) string {
	if root == "/" {
		return absolute
	}
	return filepath.Join(root, strings.TrimPrefix(absolute, "/"))
}

func secureRead(root, absolute string, mode uint32, uid, gid uint32, maximum int) ([]byte, error) {
	path := rooted(root, absolute)
	if err := validateSecureParentChain(root, path, uid); err != nil {
		return nil, err
	}
	return readSecureFile(path, mode, uid, gid, maximum)
}

func validateSecureParentChain(root, path string, uid uint32) error {
	boundary := filepath.Clean(root)
	if boundary == "." || !filepath.IsAbs(boundary) {
		return fmt.Errorf("secure-root-invalid")
	}
	clean := filepath.Clean(path)
	relative, err := filepath.Rel(boundary, clean)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(os.PathSeparator)) {
		return fmt.Errorf("secure-path-escape")
	}
	current := filepath.Dir(clean)
	for {
		info, err := os.Lstat(current)
		if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0o022 != 0 {
			return fmt.Errorf("secure-parent-invalid")
		}
		metadata, ok := info.Sys().(*syscall.Stat_t)
		if !ok || metadata.Uid != uid {
			return fmt.Errorf("secure-parent-owner-invalid")
		}
		if current == boundary {
			return nil
		}
		next := filepath.Dir(current)
		if next == current {
			return fmt.Errorf("secure-parent-boundary-missing")
		}
		current = next
	}
}

func readSecureFile(path string, mode uint32, uid, gid uint32, maximum int) ([]byte, error) {
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm() != os.FileMode(mode) || info.Size() < 1 || info.Size() > int64(maximum) {
		return nil, fmt.Errorf("secure-file-metadata-invalid")
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != uid || stat.Gid != gid || stat.Nlink != 1 {
		return nil, fmt.Errorf("secure-file-ownership-invalid")
	}
	raw := make([]byte, info.Size())
	if _, err := io.ReadFull(file, raw); err != nil {
		zero(raw)
		return nil, err
	}
	after, err := file.Stat()
	if err != nil || !os.SameFile(info, after) {
		zero(raw)
		return nil, fmt.Errorf("secure-file-changed")
	}
	return raw, nil
}

func parsePublicKey(raw []byte) (ed25519.PublicKey, string, error) {
	block, rest := pem.Decode(raw)
	if block == nil || block.Type != "PUBLIC KEY" || len(bytes.TrimSpace(rest)) != 0 {
		return nil, "", fmt.Errorf("public-key-invalid")
	}
	parsed, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		return nil, "", err
	}
	key, ok := parsed.(ed25519.PublicKey)
	if !ok || len(key) != ed25519.PublicKeySize {
		return nil, "", fmt.Errorf("public-key-type-invalid")
	}
	copyKey := append(ed25519.PublicKey(nil), key...)
	sum := sha256.Sum256(block.Bytes)
	return copyKey, "sha256:" + fmt.Sprintf("%x", sum[:]), nil
}

func parsePrivateKey(raw []byte) (ed25519.PrivateKey, error) {
	block, rest := pem.Decode(raw)
	if block == nil || block.Type != "PRIVATE KEY" || len(bytes.TrimSpace(rest)) != 0 {
		return nil, fmt.Errorf("private-key-invalid")
	}
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, err
	}
	key, ok := parsed.(ed25519.PrivateKey)
	if !ok || len(key) != ed25519.PrivateKeySize {
		return nil, fmt.Errorf("private-key-type-invalid")
	}
	return append(ed25519.PrivateKey(nil), key...), nil
}

func decimal(value string) bool {
	return regexp.MustCompile(`^[1-9][0-9]{0,19}$`).MatchString(value)
}

func signatureBytes(value string) ([]byte, error) {
	if !regexp.MustCompile(`^[A-Za-z0-9_-]{86}$`).MatchString(value) {
		return nil, fmt.Errorf("signature-invalid")
	}
	raw, err := base64.RawURLEncoding.Strict().DecodeString(value)
	if err != nil || len(raw) != ed25519.SignatureSize || base64.RawURLEncoding.EncodeToString(raw) != value {
		zero(raw)
		return nil, fmt.Errorf("signature-invalid")
	}
	return raw, nil
}
