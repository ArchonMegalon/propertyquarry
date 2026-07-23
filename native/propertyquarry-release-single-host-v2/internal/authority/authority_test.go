package authority

import (
	"bytes"
	"context"
	"crypto"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

type httpDoerFunc func(*http.Request) (*http.Response, error)

func (function httpDoerFunc) Do(request *http.Request) (*http.Response, error) {
	return function(request)
}

func TestActivationAuthorityProbeRequiresExactRunnerScopeAndImmutableOIDC(t *testing.T) {
	token := "github_pat_fixture_token_never_printed"
	runnerStatus := http.StatusOK
	immutable := true
	subjectPrefix := ImmutableOIDCSubjectPrefix
	seen := []string{}
	client := httpDoerFunc(func(request *http.Request) (*http.Response, error) {
		if request.Method != http.MethodGet || request.Header.Get("Authorization") != "Bearer "+token || request.Header.Get("Accept") != "application/vnd.github+json" || request.Header.Get("X-GitHub-Api-Version") != "2022-11-28" {
			t.Fatalf("activation request not exact")
		}
		seen = append(seen, request.URL.String())
		status := http.StatusOK
		raw := []byte{}
		switch request.URL.String() {
		case activationRunnerURL:
			status = runnerStatus
			raw = []byte(`{"total_count":1,"runners":[{"id":789,"name":"pq-release-fixture"}]}`)
		case activationOIDCURL:
			raw = []byte(fmt.Sprintf(`{"sub_claim_prefix":%q,"use_default":true,"use_immutable_subject":%t}`, subjectPrefix, immutable))
		default:
			t.Fatalf("unexpected URL %s", request.URL)
		}
		return &http.Response{StatusCode: status, Body: io.NopCloser(bytes.NewReader(raw)), Request: request}, nil
	})
	if err := probeActivationAuthority(context.Background(), client, token); err != nil {
		t.Fatal(err)
	}
	if strings.Join(seen, "|") != activationRunnerURL+"|"+activationOIDCURL {
		t.Fatalf("unexpected activation request order: %#v", seen)
	}
	immutable = false
	if err := probeActivationAuthority(context.Background(), client, token); err == nil {
		t.Fatal("mutable OIDC subject accepted")
	}
	immutable = true
	subjectPrefix = "repo:ArchonMegalon/propertyquarry"
	if err := probeActivationAuthority(context.Background(), client, token); err == nil {
		t.Fatal("legacy name-only OIDC subject prefix accepted")
	}
	subjectPrefix = ImmutableOIDCSubjectPrefix
	runnerStatus = http.StatusForbidden
	if err := probeActivationAuthority(context.Background(), client, token); err == nil {
		t.Fatal("credential without repository Administration(read) accepted")
	}
	wrong, _ := http.NewRequest(http.MethodGet, "https://api.github.com/repos/ArchonMegalon/propertyquarry/actions/runners", nil)
	wrongFinal := httpDoerFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(`{"total_count":0,"runners":[]}`)), Request: wrong}, nil
	})
	if err := probeActivationAuthority(context.Background(), wrongFinal, token); err == nil {
		t.Fatal("wrong final GitHub URL accepted")
	}
	badShape := httpDoerFunc(func(request *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(`{"runners":[]}`)), Request: request}, nil
	})
	if err := probeActivationAuthority(context.Background(), badShape, token); err == nil {
		t.Fatal("malformed runner response accepted")
	}
}

func TestActivationCanaryReceiptIsFreshSignedAndChallengeBound(t *testing.T) {
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(private)
	keyID, err := publicKeyID(public)
	if err != nil {
		t.Fatal(err)
	}
	expected := ActivationCanaryExpected{
		ChallengeDigest: "sha256:" + strings.Repeat("1", 64), ConfigDigest: "sha256:" + strings.Repeat("2", 64),
		ControllerDigest: "sha256:" + strings.Repeat("3", 64), PackageManifestDigest: "sha256:" + strings.Repeat("4", 64),
		PlanDigest: "sha256:" + strings.Repeat("5", 64), UnitDigest: "sha256:" + strings.Repeat("6", 64),
		RuntimeSHA: strings.Repeat("7", 40), WorkflowSHA: strings.Repeat("8", 40),
		PackageAuthorityKeyID: "sha256:" + strings.Repeat("9", 64), ReceiptAuthorityKeyID: keyID,
	}
	now := time.Unix(1_900_000_000, 0).UTC()
	expected.ChallengeCreatedAt = now.Unix()
	expected.CanaryStartedAt = now.Unix()
	payload := activationCanaryPayload(expected, now.Unix())
	payloadRaw, err := canonicalJSON(payload)
	if err != nil {
		t.Fatal(err)
	}
	signature := ed25519.Sign(private, framed(activationCanaryDomain, payloadRaw))
	wire, err := canonicalJSON(map[string]any{"payload": payload, "signature": base64.RawURLEncoding.EncodeToString(signature), "signature_key_id": keyID})
	zero(payloadRaw)
	zero(signature)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(wire)
	if bytes.Contains(wire, []byte("github_pat_fixture")) || bytes.Contains(wire, bytes.Repeat([]byte{0xaa}, 32)) {
		t.Fatal("secret or plaintext challenge leaked into canary receipt")
	}
	proof, err := VerifyActivationCanaryReceipt(wire, public, expected, now)
	if err != nil || proof.ReceiptDigest != digest(wire) || proof.ValidUntil-proof.VerifiedAt != 120 {
		t.Fatalf("valid canary receipt rejected: proof=%#v err=%v", proof, err)
	}
	rebound := expected
	rebound.ChallengeDigest = "sha256:" + strings.Repeat("a", 64)
	if _, err := VerifyActivationCanaryReceipt(wire, public, rebound, now); err == nil {
		t.Fatal("canary receipt replayed across activation challenges")
	}
	if _, err := VerifyActivationCanaryReceipt(wire, public, expected, now.Add(121*time.Second)); err == nil {
		t.Fatal("expired canary receipt accepted")
	}
	wrongPublic, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := VerifyActivationCanaryReceipt(wire, wrongPublic, expected, now); err == nil {
		t.Fatal("canary receipt accepted with a key not matching the pinned key id")
	}
}

func TestHardenedGitHubClientDisablesProxyRedirectsAndOldTLS(t *testing.T) {
	client := hardenedHTTPClient()
	transport, ok := client.Transport.(*http.Transport)
	if !ok || transport.Proxy != nil || transport.TLSClientConfig == nil || transport.TLSClientConfig.MinVersion < tls.VersionTLS12 || !transport.DisableKeepAlives || transport.ForceAttemptHTTP2 {
		t.Fatal("GitHub client transport is not direct and hardened")
	}
	request, _ := http.NewRequest(http.MethodGet, "https://api.github.com/elsewhere", nil)
	if client.CheckRedirect == nil || client.CheckRedirect(request, nil) != http.ErrUseLastResponse {
		t.Fatal("GitHub client redirects are not rejected")
	}
}

const (
	fixtureRuntimeSHA   = "6d42d60052bc152b95469b2b882c7aa6ff147db0"
	fixtureWorkflowSHA  = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
	fixtureEnvelopeSHA  = "45e035d2cf3f67cd5a3d6aaa2da56e7765e5ad6545e035d2cf3f67cd5a3d6aaa"
	fixtureDeploymentID = "9e" + "11111111111111111111111111111111111111111111111111111111111111"
)

type authorityFixture struct {
	root       string
	config     *Config
	packageKey ed25519.PrivateKey
	receiptKey ed25519.PrivateKey
	plan       map[string]any
	planRaw    []byte
}

func newAuthorityFixture(t *testing.T, releaseFailure bool) *authorityFixture {
	t.Helper()
	root := t.TempDir()
	if err := os.Chmod(root, 0o700); err != nil {
		t.Fatal(err)
	}
	for _, relative := range []string{"etc", "etc/propertyquarry-release-single-host-v2", "var", "var/lib", "var/lib/propertyquarry-release-single-host-v2", "var/lib/propertyquarry-release-single-host-v2/journal", "docker", "docker/property", "docker/property/state", "docker/property/state/runtime"} {
		path := filepath.Join(root, relative)
		if err := os.Mkdir(path, 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(path, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	machineID := []byte("0123456789abcdef0123456789abcdef\n")
	writeFixture(t, rooted(root, "/etc/machine-id"), machineID, 0o444)
	registrationEmailEnv := []byte(strings.Join([]string{
		"EMAILIT_API_KEY=secret",
		"PROPERTYQUARRY_CLOUDFLARE_EMAIL_API_TOKEN=cloudflare-token",
		"PROPERTYQUARRY_CLOUDFLARE_EMAIL_ACCOUNT_ID=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		"EA_REGISTRATION_EMAIL_FROM=primary@example.test",
		"EA_REGISTRATION_EMAIL_NAME=PropertyQuarry",
		"EA_REGISTRATION_EMAIL_FROM_FALLBACK=fallback@example.test",
		"EA_REGISTRATION_EMAIL_NAME_FALLBACK=PropertyQuarry fallback",
		"EA_REGISTRATION_EMAIL_FORCE_FALLBACK=false",
		"EA_EMAIL_DEFAULT_FROM=legacy@example.test",
		"EA_EMAIL_DEFAULT_NAME=PropertyQuarry legacy",
	}, "\n") + "\n")
	legacyRegistrationEmailEnv := []byte(strings.Join([]string{
		"EMAILIT_API_KEY=secret",
		"EA_REGISTRATION_EMAIL_FROM=primary@example.test",
		"EA_REGISTRATION_EMAIL_NAME=PropertyQuarry",
		"EA_REGISTRATION_EMAIL_FROM_FALLBACK=fallback@example.test",
		"EA_REGISTRATION_EMAIL_NAME_FALLBACK=PropertyQuarry fallback",
		"EA_REGISTRATION_EMAIL_FORCE_FALLBACK=false",
		"EA_EMAIL_DEFAULT_FROM=legacy@example.test",
		"EA_EMAIL_DEFAULT_NAME=PropertyQuarry legacy",
	}, "\n") + "\n")
	prePurgeRootEnv := append(append([]byte{}, legacyRegistrationEmailEnv...), []byte("POST_PURGE_STATE=true\n")...)
	postPurgeRootEnv := []byte("POST_PURGE_STATE=true\n")
	writeFixture(t, rooted(root, BaseEnvironmentPath), prePurgeRootEnv, 0o600)
	googleIdentityEnv := []byte("PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID=client\nPROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET=secret\nPROPERTYQUARRY_GOOGLE_OAUTH_REDIRECT_URI=https://propertyquarry.com/app/auth/google/callback\nPROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET=state\nPROPERTYQUARRY_IDENTITY_SESSION_SECRET=session\n")
	writeFixture(t, rooted(root, GoogleIdentityEnvPath), googleIdentityEnv, 0o600)
	writeFixture(t, rooted(root, RegistrationEmailEnvPath), registrationEmailEnv, 0o600)
	sceneVideoEnv := []byte("PROPERTYQUARRY_RENDER_BRIDGE_TOKEN=fixture-render-bridge-token\n")
	writeFixture(t, rooted(root, SceneVideoEnvPath), sceneVideoEnv, 0o600)
	databaseRuntimeEnv := fixtureDatabaseRuntimeEnvironment()
	writeFixture(t, rooted(root, DatabaseRuntimeEnvironmentPath), databaseRuntimeEnv, 0o600)
	admissionEnv := []byte("PROPERTYQUARRY_API_ADMISSION_DATABASE_URL=fixture\n")
	writeFixture(t, rooted(root, AdmissionEnvPath), admissionEnv, 0o600)
	packageSeed := bytes.Repeat([]byte{0x11}, ed25519.SeedSize)
	receiptSeed := bytes.Repeat([]byte{0x22}, ed25519.SeedSize)
	packageKey := ed25519.NewKeyFromSeed(packageSeed)
	receiptKey := ed25519.NewKeyFromSeed(receiptSeed)
	zero(packageSeed)
	zero(receiptSeed)
	packageAnchor := publicPEM(t, packageKey.Public().(ed25519.PublicKey))
	receiptAnchor := publicPEM(t, receiptKey.Public().(ed25519.PublicKey))
	packageKeyID, err := publicKeyID(packageKey.Public().(ed25519.PublicKey))
	if err != nil {
		t.Fatal(err)
	}
	receiptKeyID, err := publicKeyID(receiptKey.Public().(ed25519.PublicKey))
	if err != nil {
		t.Fatal(err)
	}
	executables := map[string]any{
		PredeployBackupExecutablePath:  "sha256:" + strings.Repeat("a", 64),
		runtimeIsolationExecutablePath: "sha256:" + strings.Repeat("b", 64),
		DatabaseControlExecutablePath:  "sha256:" + strings.Repeat("c", 64),
		RuntimeDeployExecutablePath:    "sha256:" + strings.Repeat("d", 64),
	}
	_ = releaseFailure
	step := func(id, effect string, argv []string, timeout int64) map[string]any {
		arguments := make([]any, len(argv))
		for index, argument := range argv {
			arguments[index] = argument
		}
		return map[string]any{"argv": arguments, "effect": effect, "expected_exit_code": json.Number("0"), "id": id, "idempotent": true, "timeout_seconds": json.Number(strconv.FormatInt(timeout, 10))}
	}
	webImage := "ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime@sha256:" + strings.Repeat("a", 64)
	renderImage := "ghcr.io/archonmegalon/propertyquarry-standalone-render-runtime@sha256:" + strings.Repeat("b", 64)
	cloudflaredImage := "cloudflare/cloudflared@sha256:" + strings.Repeat("d", 64)
	transactionStartedAt := int64(1_800_000_000)
	runnerReservationNonce := strings.Repeat("01", 32)
	runnerReservationNonceRaw, err := hex.DecodeString(runnerReservationNonce)
	if err != nil {
		t.Fatal(err)
	}
	runnerLabelDigest := sha256.Sum256(append([]byte(runnerLabelDerivationDomain), runnerReservationNonceRaw...))
	zero(runnerReservationNonceRaw)
	runnerLabel := "pqrelease-" + hex.EncodeToString(runnerLabelDigest[:16])
	reservationCreated := transactionStartedAt - 60
	reservationExpires := reservationCreated + runnerReservationTTLSeconds
	reservationPayload := map[string]any{
		"authority_profile": "single-host-production-v2", "created_at_epoch": json.Number(strconv.FormatInt(reservationCreated, 10)),
		"environment": Environment, "expires_at_epoch": json.Number(strconv.FormatInt(reservationExpires, 10)),
		"receipt_authority_key_id": receiptKeyID, "release_job": ReleaseJob, "repository": Repository,
		"repository_id": RepositoryID, "repository_owner_id": RepositoryOwnerID, "reservation_nonce": runnerReservationNonce,
		"runner_label": runnerLabel, "runner_label_nonce": strings.TrimPrefix(runnerLabel, "pqrelease-"), "schema": runnerReservationSchema,
		"source_checkout_identity_sha256": "sha256:" + strings.Repeat("8", 64),
		"source_checkout_path":            "/docker/property/state/runtime/propertyquarry-release-authority-v2.private/single-host-v2-release-checkouts/" + fixtureWorkflowSHA,
		"source_tree_sha256":              "sha256:" + strings.Repeat("9", 64), "version": json.Number("2"),
		"workflow_path": ".github/workflows/smoke-runtime.yml", "workflow_ref": WorkflowRef, "workflow_sha": fixtureWorkflowSHA,
	}
	reservationRaw, err := signRunnerWire(reservationPayload, receiptKey, runnerReservationSignatureDomain)
	if err != nil {
		t.Fatal(err)
	}
	prerequisiteIntentPayload := map[string]any{
		"authority_profile": "single-host-production-v2", "comment": "PropertyQuarry governed prerequisite approval " + digest(reservationRaw),
		"discovered_at_epoch": json.Number(strconv.FormatInt(transactionStartedAt-30, 10)), "environment_id": "42", "environment_name": Environment,
		"initial_jobs_sha256": "sha256:" + strings.Repeat("1", 64), "initial_pending_deployments_sha256": "sha256:" + strings.Repeat("2", 64),
		"initial_runs_index_sha256": "sha256:" + strings.Repeat("3", 64), "prerequisite_job_id": "789", "prerequisite_job_name": runnerPrerequisiteJob,
		"receipt_authority_key_id": receiptKeyID, "release_job": ReleaseJob, "repository": Repository, "repository_id": RepositoryID,
		"repository_owner_id": RepositoryOwnerID, "reservation_expires_at_epoch": json.Number(strconv.FormatInt(reservationExpires, 10)),
		"reservation_sha256": digest(reservationRaw), "run_attempt": json.Number("1"), "run_id": "123", "runner_label": runnerLabel,
		"schema": runnerPrerequisiteIntentSchema, "version": json.Number("2"), "workflow_path": ".github/workflows/smoke-runtime.yml",
		"workflow_ref": WorkflowRef, "workflow_sha": fixtureWorkflowSHA,
	}
	prerequisiteIntentRaw, err := signRunnerWire(prerequisiteIntentPayload, receiptKey, runnerPrerequisiteIntentSignatureDomain)
	if err != nil {
		t.Fatal(err)
	}
	prerequisiteApprovalPayload := map[string]any{
		"approval_api_disposition": "approved", "approval_response_sha256": "sha256:" + strings.Repeat("4", 64),
		"approved_at_epoch": json.Number(strconv.FormatInt(transactionStartedAt-20, 10)), "completed_jobs_sha256": "sha256:" + strings.Repeat("5", 64),
		"environment_id": "42", "environment_name": Environment, "intent_sha256": digest(prerequisiteIntentRaw),
		"post_pending_deployments_sha256": "sha256:" + strings.Repeat("6", 64), "prerequisite_conclusion": "success",
		"prerequisite_job_id": "789", "prerequisite_job_name": runnerPrerequisiteJob, "receipt_authority_key_id": receiptKeyID,
		"release_job": ReleaseJob, "repository": Repository, "repository_id": RepositoryID, "repository_owner_id": RepositoryOwnerID,
		"reservation_expires_at_epoch": json.Number(strconv.FormatInt(reservationExpires, 10)), "reservation_sha256": digest(reservationRaw),
		"review_history_sha256": "sha256:" + strings.Repeat("7", 64), "run_attempt": json.Number("1"), "run_id": "123",
		"runner_label": runnerLabel, "schema": runnerPrerequisiteApprovalSchema, "version": json.Number("2"),
		"workflow_path": ".github/workflows/smoke-runtime.yml", "workflow_ref": WorkflowRef, "workflow_sha": fixtureWorkflowSHA,
	}
	prerequisiteApprovalPayloadRaw, err := canonicalJSON(prerequisiteApprovalPayload)
	if err != nil {
		t.Fatal(err)
	}
	prerequisiteApprovalRaw, err := signRunnerWire(prerequisiteApprovalPayload, receiptKey, runnerPrerequisiteApprovalSignatureDomain)
	if err != nil {
		t.Fatal(err)
	}
	writeFixture(t, rooted(root, RunnerReservationPath), reservationRaw, 0o400)
	writeFixture(t, rooted(root, RunnerPrerequisiteIntentPath), prerequisiteIntentRaw, 0o400)
	writeFixture(t, rooted(root, RunnerPrerequisiteApprovalPath), prerequisiteApprovalRaw, 0o400)
	input := func(path string, raw []byte) map[string]any {
		return map[string]any{"gid": json.Number("1000"), "mode": json.Number("384"), "path": path, "sha256": digest(raw), "size": json.Number(strconv.Itoa(len(raw))), "uid": json.Number("1000")}
	}
	prePurgeRuntimeInputs := []any{
		input(BaseEnvironmentPath, prePurgeRootEnv), input(SceneVideoEnvPath, sceneVideoEnv), input(DatabaseRuntimeEnvironmentPath, databaseRuntimeEnv),
		input(AdmissionEnvPath, admissionEnv), input(GoogleIdentityEnvPath, googleIdentityEnv), input(RegistrationEmailEnvPath, registrationEmailEnv),
	}
	runtimeInputs := []any{
		input(BaseEnvironmentPath, postPurgeRootEnv), input(SceneVideoEnvPath, sceneVideoEnv), input(DatabaseRuntimeEnvironmentPath, databaseRuntimeEnv),
		input(AdmissionEnvPath, admissionEnv), input(GoogleIdentityEnvPath, googleIdentityEnv), input(RegistrationEmailEnvPath, registrationEmailEnv),
	}
	runtimeRetirement := map[string]any{
		"containers": []any{}, "deployment_id": fixtureDeploymentID, "desired_live_allowlist": stringValues(desiredRuntimeContainerAllowlist),
		"operation": operationRetireStaleRuntime, "preserve_volumes": true,
		"receipt_path": filepath.Join(runtimeIsolationReceiptDirectory, fixtureRuntimeSHA, fixtureDeploymentID, operationRetireStaleRuntime+".json"),
	}
	fileObservation := func(path, mode, sha string, size int64) map[string]any {
		return map[string]any{"gid": json.Number("0"), "mode": mode, "path": path, "sha256": sha, "size": json.Number(strconv.FormatInt(size, 10)), "uid": json.Number("0")}
	}
	runtimeDeploy := map[string]any{
		"compose_argv": stringValues(expectedComposeArgv()),
		"compose_files": []any{
			fileObservation(PropertyComposePath, "0644", "sha256:"+strings.Repeat("1", 64), 1024),
			fileObservation(CloudflaredComposePath, "0644", "sha256:"+strings.Repeat("2", 64), 1024),
		},
		"compose_plugin": fileObservation(DockerComposePluginPath, "0755", "sha256:"+strings.Repeat("3", 64), 1024),
		"deployment_id":  fixtureDeploymentID, "docker_executable": fileObservation(DockerExecutablePath, "0755", "sha256:"+strings.Repeat("4", 64), 1024),
		"env_files": stringValues(runtimeIsolationInputPaths), "operation": "deploy-runtime",
		"receipt_path": filepath.Join(RuntimeDeployReceiptDirectory, fixtureRuntimeSHA, fixtureDeploymentID, "deploy-runtime.json"),
	}
	databaseSubstrate := map[string]any{
		"container_id": strings.Repeat("5", 64), "container_name": databaseControlContainer, "database": databaseControlDatabase,
		"database_oid": json.Number("83"), "image": DatabaseImage, "image_id": "sha256:" + strings.Repeat("6", 64),
		"pgdata_volume": map[string]any{
			"created_at": "2026-07-22T10:00:00Z", "driver": "local",
			"labels":     map[string]any{"com.docker.compose.project": ProjectName, "com.docker.compose.volume": "propertyquarry_pgdata"},
			"mountpoint": databasePGDataVolumeMountpoint, "name": databasePGDataVolumeName, "options": map[string]any{}, "scope": "local",
		},
		"repo_digest": canonicalRepoDigest(DatabaseImage),
	}
	backupArgv := []string{
		PredeployBackupExecutablePath, "create",
		"--runtime-sha", fixtureRuntimeSHA,
		"--deployment-id", fixtureDeploymentID,
		"--envelope-sha", fixtureEnvelopeSHA,
		"--web-image", webImage,
		"--render-image", renderImage,
		"--database-image", DatabaseImage,
		"--receipt", filepath.Join(PredeployBackupReceiptDirectory, fixtureRuntimeSHA, fixtureDeploymentID, "create.json"),
		"--encryption-key", PredeployBackupEncryptionKeyPath,
	}
	isolationArgv := func(operation string, receipt, prePurgeDigest bool) []string {
		argv := []string{
			runtimeIsolationExecutablePath, operation, "--runtime-sha", fixtureRuntimeSHA, "--deployment-id", fixtureDeploymentID,
			"--envelope-sha", fixtureEnvelopeSHA, "--web-image", webImage, "--render-image", renderImage,
			"--cloudflared-image", cloudflaredImage, "--database-image", DatabaseImage,
			"--api-host-ip", APIHostIP, "--api-host-port", "8097", "--api-container-port", "8090",
		}
		if prePurgeDigest {
			argv = append(argv, "--pre-purge-root-env-digest", digest(prePurgeRootEnv))
		}
		if receipt {
			argv = append(argv, "--receipt", filepath.Join(runtimeIsolationReceiptDirectory, fixtureRuntimeSHA, fixtureDeploymentID, operation+".json"))
		}
		return argv
	}
	databaseStep := func(id, operation, receiptBaseName string, timeout int64) map[string]any {
		return step(id, "mutation", []string{
			DatabaseControlExecutablePath, operation,
			"--runtime-sha", fixtureRuntimeSHA,
			"--deployment-id", fixtureDeploymentID,
			"--web-image", webImage,
			"--database-image", DatabaseImage,
			"--receipt", filepath.Join(DatabaseReceiptDirectory, fixtureRuntimeSHA, fixtureDeploymentID, receiptBaseName),
		}, timeout)
	}
	deployArgv := []string{
		RuntimeDeployExecutablePath, "deploy-runtime", "--runtime-sha", fixtureRuntimeSHA, "--deployment-id", fixtureDeploymentID,
		"--envelope-sha", fixtureEnvelopeSHA, "--web-image", webImage, "--render-image", renderImage,
		"--cloudflared-image", cloudflaredImage, "--database-image", DatabaseImage,
		"--api-host-ip", APIHostIP, "--api-host-port", "8097", "--api-container-port", "8090",
		"--receipt", filepath.Join(RuntimeDeployReceiptDirectory, fixtureRuntimeSHA, fixtureDeploymentID, "deploy-runtime.json"),
	}
	plan := map[string]any{
		"schema": PlanSchema, "version": json.Number("2"), "authority_profile": "single-host-production-v2",
		"api_host_ip": APIHostIP, "api_host_port": json.Number("8097"), "api_container_port": json.Number("8090"),
		"runtime_sha": fixtureRuntimeSHA, "workflow_sha": fixtureWorkflowSHA, "deployment_id": fixtureDeploymentID, "transaction_started_at_epoch": json.Number(strconv.FormatInt(transactionStartedAt, 10)), "backup_max_age_seconds": json.Number("3600"),
		"envelope_sha": fixtureEnvelopeSHA, "release_generation": json.Number("1"), "predecessor_runtime_sha": "genesis",
		"host_machine_id_digest": digest(bytes.TrimSpace(machineID)), "repository": Repository, "project_name": ProjectName, "public_origin": PublicOrigin,
		"runner_reservation_sha256": digest(reservationRaw), "runner_label": runnerLabel,
		"runner_run_id": "123", "runner_run_attempt": json.Number("1"), "runner_job_id": "456",
		"runner_prerequisite_intent_sha256":           digest(prerequisiteIntentRaw),
		"runner_prerequisite_approval_sha256":         digest(prerequisiteApprovalRaw),
		"runner_prerequisite_approval_payload_sha256": digest(prerequisiteApprovalPayloadRaw),
		"runner_prerequisite_job_id":                  "789",
		"github_identity_env_path":                    GoogleIdentityEnvPath, "github_identity_env_mode": "0600", "github_identity_env_digest": digest(googleIdentityEnv),
		"github_identity_env_uid": json.Number("1000"), "github_identity_env_gid": json.Number("1000"),
		"registration_email_env_path": RegistrationEmailEnvPath, "registration_email_env_mode": "0600", "registration_email_env_digest": digest(registrationEmailEnv),
		"registration_email_env_uid": json.Number("1000"), "registration_email_env_gid": json.Number("1000"),
		"scene_video_env_path": SceneVideoEnvPath, "scene_video_env_mode": json.Number("384"), "scene_video_env_digest": digest(sceneVideoEnv),
		"scene_video_env_uid": json.Number("1000"), "scene_video_env_gid": json.Number("1000"),
		"web_image":                 webImage,
		"render_image":              renderImage,
		"cloudflared_image":         cloudflaredImage,
		"database_image":            DatabaseImage,
		"pre_purge_root_env_digest": digest(prePurgeRootEnv), "post_purge_root_env_digest": digest(postPurgeRootEnv),
		"pre_purge_runtime_inputs": prePurgeRuntimeInputs, "runtime_inputs": runtimeInputs,
		"runtime_retirement": runtimeRetirement, "runtime_retirement_digest": digestCanonical(t, runtimeRetirement),
		"runtime_deploy": runtimeDeploy, "runtime_deploy_digest": digestCanonical(t, runtimeDeploy),
		"database_substrate": databaseSubstrate, "database_substrate_digest": digestCanonical(t, databaseSubstrate),
		"executables":     executables,
		"preflight_steps": []any{step(VerifyIsolationInputsStepID, "read-only", isolationArgv("verify-isolation-inputs", false, true), 600)},
		"release_steps": []any{
			step("predeploy-encrypted-backup", "mutation", backupArgv, 9600),
			step(PurgeRuntimeIsolationStepID, "mutation", isolationArgv(operationPurgeRuntimeIsolation, true, true), 600),
			step(RuntimeRetirementStepID, "mutation", isolationArgv(operationRetireStaleRuntime, true, false), 600),
			databaseStep(ProvisionDatabaseRolesStepID, "provision-roles", "provision-roles.json", 900),
			databaseStep(MigrateSchemaStepID, "migrate-schema", "migrate-schema.json", 1500),
			databaseStep(HardenRuntimeACLStepID, "harden-runtime-acl", "harden-runtime-acl.json", 900),
			databaseStep(VerifySchemaReadinessStepID, "verify-schema-readiness", "verify-schema-readiness.json", 600),
			step(DeployRuntimeStepID, "mutation", deployArgv, 1800),
		},
		"verify_steps":   []any{step(VerifyRuntimeIsolationStepID, "verification", isolationArgv(operationVerifyRuntimeIsolation, true, false), 600)},
		"rollback_steps": []any{step(RestoreRuntimeIsolationStepID, "rollback", isolationArgv(operationRestoreRuntimeIsolation, true, true), 600)},
	}
	planRaw, err := canonicalJSON(plan)
	if err != nil {
		t.Fatal(err)
	}
	writeFixture(t, rooted(root, PlanPath), planRaw, 0o444)
	configValue := map[string]any{
		"schema": ProfileSchema, "version": json.Number("2"), "authority_profile": "single-host-production-v2",
		"api_host_ip": APIHostIP, "api_host_port": json.Number("8097"), "api_container_port": json.Number("8090"),
		"runtime_sha": fixtureRuntimeSHA, "workflow_sha": fixtureWorkflowSHA, "deployment_id": fixtureDeploymentID, "transaction_started_at_epoch": json.Number(strconv.FormatInt(transactionStartedAt, 10)), "backup_max_age_seconds": json.Number("3600"),
		"envelope_sha": fixtureEnvelopeSHA, "release_generation": json.Number("1"), "predecessor_runtime_sha": "genesis",
		"repository": Repository, "repository_id": RepositoryID, "repository_owner_id": RepositoryOwnerID, "workflow_ref": WorkflowRef,
		"runner_reservation_sha256": plan["runner_reservation_sha256"], "runner_label": plan["runner_label"],
		"runner_run_id": plan["runner_run_id"], "runner_run_attempt": plan["runner_run_attempt"], "runner_job_id": plan["runner_job_id"],
		"runner_prerequisite_intent_sha256":           plan["runner_prerequisite_intent_sha256"],
		"runner_prerequisite_approval_sha256":         plan["runner_prerequisite_approval_sha256"],
		"runner_prerequisite_approval_payload_sha256": plan["runner_prerequisite_approval_payload_sha256"],
		"runner_prerequisite_job_id":                  plan["runner_prerequisite_job_id"],
		"release_job":                                 ReleaseJob, "environment": Environment, "project_name": ProjectName, "public_origin": PublicOrigin,
		"web_image": plan["web_image"], "render_image": plan["render_image"], "cloudflared_image": plan["cloudflared_image"], "database_image": plan["database_image"], "plan_digest": digest(planRaw),
		"pre_purge_root_env_digest": plan["pre_purge_root_env_digest"], "post_purge_root_env_digest": plan["post_purge_root_env_digest"],
		"pre_purge_runtime_inputs": prePurgeRuntimeInputs, "runtime_inputs": runtimeInputs,
		"runtime_retirement": runtimeRetirement, "runtime_retirement_digest": plan["runtime_retirement_digest"],
		"runtime_deploy": runtimeDeploy, "runtime_deploy_digest": plan["runtime_deploy_digest"],
		"database_substrate": databaseSubstrate, "database_substrate_digest": plan["database_substrate_digest"],
		"host_machine_id_digest": plan["host_machine_id_digest"], "package_authority_key_id": packageKeyID, "receipt_authority_key_id": receiptKeyID,
		"allowed_runner_uid": json.Number("1001"), "allowed_runner_gid": json.Number("1001"), "preflight_ttl_seconds": json.Number("300"),
		"github_api_credential_path": "/run/credentials/propertyquarry-release-single-host-v2.service/github-api-token",
		"github_oidc_request_origin": "https://vstoken.actions.githubusercontent.com", "ephemeral_runner_label_prefix": "pqrelease-",
		"github_identity_env_path": GoogleIdentityEnvPath, "github_identity_env_mode": "0600", "github_identity_env_digest": digest(googleIdentityEnv),
		"github_identity_env_uid": json.Number("1000"), "github_identity_env_gid": json.Number("1000"),
		"registration_email_env_path": RegistrationEmailEnvPath, "registration_email_env_mode": "0600", "registration_email_env_digest": digest(registrationEmailEnv),
		"registration_email_env_uid": json.Number("1000"), "registration_email_env_gid": json.Number("1000"),
		"scene_video_env_path": SceneVideoEnvPath, "scene_video_env_mode": json.Number("384"), "scene_video_env_digest": digest(sceneVideoEnv),
		"scene_video_env_uid": json.Number("1000"), "scene_video_env_gid": json.Number("1000"),
	}
	configRaw, err := canonicalJSON(configValue)
	if err != nil {
		t.Fatal(err)
	}
	configSignature := ed25519.Sign(packageKey, framed(configDomain, configRaw))
	writeFixture(t, rooted(root, ConfigPath), configRaw, 0o400)
	writeFixture(t, rooted(root, ConfigSignaturePath), configSignature, 0o444)
	writeFixture(t, rooted(root, PackageAnchorPath), packageAnchor, 0o444)
	writeFixture(t, rooted(root, ReceiptAnchorPath), receiptAnchor, 0o444)
	privateDER, err := x509.MarshalPKCS8PrivateKey(receiptKey)
	if err != nil {
		t.Fatal(err)
	}
	writeFixture(t, rooted(root, ReceiptKeyPath), pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: privateDER}), 0o400)
	config, loadedKey, err := LoadConfig(root)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(loadedKey, receiptKey) {
		t.Fatal("loaded receipt key changed")
	}
	zero(loadedKey)
	return &authorityFixture{root: root, config: config, packageKey: packageKey, receiptKey: receiptKey, plan: plan, planRaw: planRaw}
}

func fixtureDatabaseRuntimeEnvironment() []byte {
	password := strings.Repeat("A", 48)
	admission := "postgresql://propertyquarry_admission_runtime:" + password + "@propertyquarry-db:5432/propertyquarry_admission"
	return []byte(strings.Join([]string{
		"PROPERTYQUARRY_API_ADMISSION_DATABASE_URL=" + admission,
		"PROPERTYQUARRY_API_DATABASE_URL=postgresql://propertyquarry_api:" + password + "@propertyquarry-db:5432/propertyquarry",
		"PROPERTYQUARRY_MIGRATION_DATABASE_URL=postgresql://propertyquarry_migrator:" + password + "@propertyquarry-db:5432/propertyquarry?options=-c%20role%3Dpropertyquarry_owner%20-c%20search_path%3Dpublic%2Cpg_catalog",
		"PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET=" + password,
		"PROPERTYQUARRY_RENDER_DATABASE_URL=" + admission,
		"PROPERTYQUARRY_SCHEDULER_DATABASE_URL=postgresql://propertyquarry_scheduler:" + password + "@propertyquarry-db:5432/propertyquarry",
		"PROPERTYQUARRY_WORKER_DATABASE_URL=postgresql://propertyquarry_worker:" + password + "@propertyquarry-db:5432/propertyquarry",
	}, "\n") + "\n")
}

func stringValues(values []string) []any {
	result := make([]any, len(values))
	for index, value := range values {
		result[index] = value
	}
	return result
}

func digestCanonical(t *testing.T, value any) string {
	t.Helper()
	raw, err := canonicalJSON(value)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(raw)
	return digest(raw)
}

func (fixture *authorityFixture) close() {
	fixture.config.release()
	zero(fixture.packageKey)
	zero(fixture.receiptKey)
	zero(fixture.planRaw)
}

func writePackageManifestFixture(t *testing.T, fixture *authorityFixture) ([]byte, []byte) {
	t.Helper()
	preInputsRaw, err := canonicalJSON(runtimeInputObservationValues(fixture.config.PrePurgeRuntimeInputs))
	if err != nil {
		t.Fatal(err)
	}
	defer zero(preInputsRaw)
	inputsRaw, err := canonicalJSON(runtimeInputObservationValues(fixture.config.RuntimeInputs))
	if err != nil {
		t.Fatal(err)
	}
	defer zero(inputsRaw)
	value := map[string]any{
		"api_container_port": json.Number(strconv.FormatInt(fixture.config.APIContainerPort, 10)),
		"api_host_ip":        fixture.config.APIHostIP, "api_host_port": json.Number(strconv.FormatInt(fixture.config.APIHostPort, 10)),
		"backup_max_age_seconds": json.Number(strconv.FormatInt(fixture.config.BackupMaxAgeSeconds, 10)),
		"cloudflared_image":      fixture.config.CloudflaredImage, "config_digest": fixture.config.Digest,
		"database_image": fixture.config.DatabaseImage, "database_substrate_digest": fixture.config.DatabaseSubstrateDigest, "deployment_id": fixture.config.DeploymentID,
		"envelope_sha": fixture.config.EnvelopeSHA, "package_authority_key_id": fixture.config.PackageAuthorityKeyID,
		"plan_digest": fixture.config.PlanDigest, "post_purge_root_env_digest": fixture.config.PostPurgeRootEnvDigest,
		"pre_purge_root_env_digest": fixture.config.PrePurgeRootEnvDigest, "pre_purge_runtime_inputs_digest": digest(preInputsRaw),
		"receipt_authority_key_id": fixture.config.ReceiptAuthorityKeyID,
		"render_image":             fixture.config.RenderImage, "runtime_sha": fixture.config.RuntimeSHA, "workflow_sha": fixture.config.WorkflowSHA,
		"runtime_deploy_digest": fixture.config.RuntimeDeployDigest, "runtime_inputs_digest": digest(inputsRaw), "runtime_retirement_digest": fixture.config.RuntimeRetirementDigest,
		"schema": "propertyquarry.release-control.single-host-package.v2", "transaction_started_at_epoch": json.Number(strconv.FormatInt(fixture.config.TransactionStartedAtEpoch, 10)),
		"web_image": fixture.config.WebImage,
	}
	raw, err := canonicalJSON(value)
	if err != nil {
		t.Fatal(err)
	}
	signature := ed25519.Sign(fixture.packageKey, framed(packageManifestSignatureDomain, raw))
	writeFixture(t, rooted(fixture.root, "/etc/propertyquarry-release-single-host-v2/package-manifest.v2.json"), raw, 0o444)
	writeFixture(t, rooted(fixture.root, "/etc/propertyquarry-release-single-host-v2/package-manifest.v2.sig"), signature, 0o444)
	return raw, signature
}

func writeFixture(t *testing.T, path string, raw []byte, mode os.FileMode) {
	t.Helper()
	if _, err := os.Lstat(path); err == nil {
		if err := os.Chmod(path, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(path, raw, mode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, mode); err != nil {
		t.Fatal(err)
	}
}

func publicPEM(t *testing.T, key ed25519.PublicKey) []byte {
	t.Helper()
	raw, err := x509.MarshalPKIXPublicKey(key)
	if err != nil {
		t.Fatal(err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: raw})
}

func fileDigest(t *testing.T, path string) string {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return digest(raw)
}

func TestStrictCanonicalJSONRejectsDuplicatesAndNoncanonicalBytes(t *testing.T) {
	valid := []byte(`{"a":1,"b":[true,false]}`)
	if _, err := strictJSON(valid, 1024); err != nil {
		t.Fatal(err)
	}
	for _, raw := range [][]byte{[]byte(`{"a":1,"a":2}`), []byte(`{"b":2, "a":1}`), []byte(`{"a":1}\n`), []byte(`{"a":1}x`)} {
		if _, err := strictJSON(raw, 1024); err == nil {
			t.Fatalf("accepted hostile JSON %q", raw)
		}
	}
}

func TestLifecycleTimeoutEnvelopeAndPeerDisconnectCancellation(t *testing.T) {
	if maximumReleaseVerifyStepSeconds != 17_100 || maximumRollbackStepSeconds != 600 ||
		releaseExecutionTimeout != 290*time.Minute || rollbackExecutionTimeout != 10*time.Minute ||
		serverProtocolTimeout != 305*time.Minute || clientProtocolTimeout != 310*time.Minute {
		t.Fatal("release lifecycle timeout envelope drifted")
	}
	socketPath := filepath.Join(t.TempDir(), "peer.sock")
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: socketPath, Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	accepted := make(chan *net.UnixConn, 1)
	go func() {
		connection, acceptErr := listener.AcceptUnix()
		if acceptErr == nil {
			accepted <- connection
		}
	}()
	client, err := net.DialUnix("unix", nil, &net.UnixAddr{Name: socketPath, Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	server := <-accepted
	ctx, cancel, complete := peerBoundContext(context.Background(), server)
	defer cancel()
	defer complete()
	defer server.Close()
	if err := client.Close(); err != nil {
		t.Fatal(err)
	}
	select {
	case <-ctx.Done():
		if ctx.Err() != context.Canceled {
			t.Fatalf("unexpected peer lifecycle error: %v", ctx.Err())
		}
	case <-time.After(time.Second):
		t.Fatal("peer disconnect did not cancel authority context")
	}
}

func TestAuthenticatedConfigAndPlanBindExactReleaseWithoutCompileTimeCommitPin(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	if fixture.config.RuntimeSHA != fixtureRuntimeSHA || fixture.config.WorkflowSHA != fixtureWorkflowSHA || fixture.config.RuntimeSHA == fixture.config.WorkflowSHA || fixture.config.EnvelopeSHA != fixtureEnvelopeSHA || fixture.config.ReleaseGeneration != 1 || fixture.config.DatabaseImage != DatabaseImage || fixture.config.APIHostIP != APIHostIP || fixture.config.APIHostPort != APIHostPort || fixture.config.APIContainerPort != APIContainerPort {
		t.Fatalf("unexpected binding: %#v", fixture.config)
	}
	plan, err := LoadPlan(fixture.root, fixture.config)
	if err != nil {
		t.Fatal(err)
	}
	defer plan.release()
	if plan.Digest != fixture.config.PlanDigest || plan.ReleaseSteps[0].ID != "predeploy-encrypted-backup" || plan.ReleaseSteps[7].ID != DeployRuntimeStepID || plan.ReleaseSteps[7].Argv[0] != RuntimeDeployExecutablePath || plan.VerifySteps[len(plan.VerifySteps)-1].ID != VerifyRuntimeIsolationStepID || !plan.RollbackSteps[0].Idempotent {
		t.Fatal("plan binding lost")
	}
	tamperedPlan := cloneFields(fixture.plan)
	tamperedPlan["workflow_sha"] = fixtureRuntimeSHA
	if candidate, err := parsePlan(tamperedPlan, nil, fixture.config); err == nil {
		candidate.release()
		t.Fatal("plan workflow SHA collapsed onto runtime SHA")
	}
	configValue, err := strictJSON(fixture.config.Raw, maximumConfigBytes)
	if err != nil {
		t.Fatal(err)
	}
	configValue["workflow_sha"] = fixtureRuntimeSHA
	configRaw, _ := canonicalJSON(configValue)
	if candidate, err := parseConfigWithExternalValidation(configValue, configRaw, fixture.config.PackageAuthorityKeyID, fixture.root, false); err == nil {
		candidate.release()
		t.Fatal("profile workflow SHA collapsed onto runtime SHA")
	}
	zero(configRaw)
	envelopePath := rooted(fixture.root, GoogleIdentityEnvPath)
	envelopeRaw, err := os.ReadFile(envelopePath)
	if err != nil {
		t.Fatal(err)
	}
	envelopeRaw[len(envelopeRaw)-2] ^= 1
	writeFixture(t, envelopePath, envelopeRaw, 0o600)
	if config, key, err := LoadConfig(fixture.root); err == nil {
		config.release()
		zero(key)
		t.Fatal("tampered Google identity envelope authenticated")
	}
	envelopeRaw[len(envelopeRaw)-2] ^= 1
	writeFixture(t, envelopePath, envelopeRaw, 0o600)
	emailEnvelopePath := rooted(fixture.root, RegistrationEmailEnvPath)
	emailEnvelopeRaw, err := os.ReadFile(emailEnvelopePath)
	if err != nil {
		t.Fatal(err)
	}
	emailEnvelopeRaw[len(emailEnvelopeRaw)-2] ^= 1
	writeFixture(t, emailEnvelopePath, emailEnvelopeRaw, 0o600)
	if config, key, err := LoadConfig(fixture.root); err == nil {
		config.release()
		zero(key)
		t.Fatal("tampered registration email envelope authenticated")
	}
	emailEnvelopeRaw[len(emailEnvelopeRaw)-2] ^= 1
	writeFixture(t, emailEnvelopePath, emailEnvelopeRaw, 0o600)
	wrongNames := bytes.Replace(emailEnvelopeRaw, []byte("EA_EMAIL_DEFAULT_NAME="), []byte("EA_EMAIL_WRONG_NAME="), 1)
	writeFixture(t, emailEnvelopePath, wrongNames, 0o600)
	if err := validateRegistrationEmailEnvelope(fixture.root, uint32(os.Geteuid()), uint32(os.Getegid()), digest(wrongNames)); err == nil {
		t.Fatal("registration email envelope with an unexpected name accepted")
	}
	writeFixture(t, emailEnvelopePath, emailEnvelopeRaw, 0o600)
	interpolated := bytes.Replace(emailEnvelopeRaw, []byte("EMAILIT_API_KEY=secret"), []byte("EMAILIT_API_KEY=${SHARED_SECRET}"), 1)
	writeFixture(t, emailEnvelopePath, interpolated, 0o600)
	if err := validateRegistrationEmailEnvelope(fixture.root, uint32(os.Geteuid()), uint32(os.Getegid()), digest(interpolated)); err == nil {
		t.Fatal("registration email envelope interpolation syntax accepted")
	}
	writeFixture(t, emailEnvelopePath, emailEnvelopeRaw, 0o600)
	sceneEnvelopePath := rooted(fixture.root, SceneVideoEnvPath)
	sceneEnvelopeRaw, err := os.ReadFile(sceneEnvelopePath)
	if err != nil {
		t.Fatal(err)
	}
	sceneEnvelopeRaw[len(sceneEnvelopeRaw)-2] ^= 1
	writeFixture(t, sceneEnvelopePath, sceneEnvelopeRaw, 0o600)
	if config, key, err := LoadConfig(fixture.root); err == nil {
		config.release()
		zero(key)
		t.Fatal("tampered scene video environment authenticated")
	}
	sceneEnvelopeRaw[len(sceneEnvelopeRaw)-2] ^= 1
	writeFixture(t, sceneEnvelopePath, sceneEnvelopeRaw, 0o600)
	path := rooted(fixture.root, ConfigPath)
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	raw[len(raw)-1] ^= 1
	writeFixture(t, path, raw, 0o400)
	if config, key, err := LoadConfig(fixture.root); err == nil {
		config.release()
		zero(key)
		t.Fatal("tampered config authenticated")
	}
}

func TestPrePurgeRootEnvironmentDigestIsHistoricalNotStartupCurrentFile(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	if !digestPattern.MatchString(fixture.config.PrePurgeRootEnvDigest) {
		t.Fatal("historical pre-purge root environment digest is not bound")
	}
	writeFixture(t, rooted(fixture.root, BaseEnvironmentPath), []byte("POST_PURGE_STATE=true\n"), 0o600)
	config, key, err := LoadConfig(fixture.root)
	if err != nil {
		t.Fatalf("post-purge root environment was incorrectly treated as the historical startup digest: %v", err)
	}
	config.release()
	zero(key)
}

func TestRegistrationEmailEnvelopeRequiresExactOrderedCloudflareContract(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	expectedNames := []string{
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
	if RegistrationEmailKeyCount != int64(len(expectedNames)) || !equalStrings(RegistrationEmailEnvironmentNames(), expectedNames) {
		t.Fatal("registration email key contract drifted")
	}
	path := rooted(fixture.root, RegistrationEmailEnvPath)
	valid, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := validateRegistrationEmailEnvelope(fixture.root, uint32(os.Geteuid()), uint32(os.Getegid()), digest(valid)); err != nil {
		t.Fatalf("valid registration email envelope rejected: %v", err)
	}
	tokenLine := []byte("PROPERTYQUARRY_CLOUDFLARE_EMAIL_API_TOKEN=cloudflare-token\n")
	accountLine := []byte("PROPERTYQUARRY_CLOUDFLARE_EMAIL_ACCOUNT_ID=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
	orderedPair := append(append([]byte{}, tokenLine...), accountLine...)
	swappedPair := append(append([]byte{}, accountLine...), tokenLine...)
	cases := []struct {
		name string
		raw  []byte
	}{
		{name: "missing-token", raw: bytes.Replace(valid, tokenLine, nil, 1)},
		{name: "missing-account", raw: bytes.Replace(valid, accountLine, nil, 1)},
		{name: "swapped", raw: bytes.Replace(valid, orderedPair, swappedPair, 1)},
		{name: "extra", raw: append(append([]byte{}, valid...), []byte("PROPERTYQUARRY_CLOUDFLARE_EMAIL_EXTRA=unexpected\n")...)},
		{name: "empty-token", raw: bytes.Replace(valid, tokenLine, []byte("PROPERTYQUARRY_CLOUDFLARE_EMAIL_API_TOKEN=\n"), 1)},
		{name: "empty-account", raw: bytes.Replace(valid, accountLine, []byte("PROPERTYQUARRY_CLOUDFLARE_EMAIL_ACCOUNT_ID=\n"), 1)},
	}
	for _, candidate := range cases {
		t.Run(candidate.name, func(t *testing.T) {
			writeFixture(t, path, candidate.raw, 0o600)
			if err := validateRegistrationEmailEnvelope(fixture.root, uint32(os.Geteuid()), uint32(os.Getegid()), digest(candidate.raw)); err == nil {
				t.Fatal("invalid registration email envelope accepted")
			}
		})
	}
}

func TestPlanCannotOmitOrRebindPredeployBackup(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	releaseSteps := fixture.plan["release_steps"].([]any)
	backup := releaseSteps[0].(map[string]any)
	backup["timeout_seconds"] = json.Number("9599")
	raw, err := canonicalJSON(fixture.plan)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(raw)
	if plan, err := parsePlan(fixture.plan, raw, fixture.config); err == nil {
		plan.release()
		t.Fatal("rebound predeploy backup contract accepted")
	}
}

func TestStepExecutionUsesExactSanitizedDatabaseControlEnvironment(t *testing.T) {
	expected := []string{
		"HOME=/nonexistent",
		"LANG=C.UTF-8",
		"LC_ALL=C.UTF-8",
		"PATH=/usr/sbin:/usr/bin:/sbin:/bin",
		"TZ=UTC",
	}
	if actual := sanitizedStepEnvironment(); !equalStrings(actual, expected) {
		t.Fatalf("unexpected step environment: %#v", actual)
	}
}

func TestPlanRequiresOrderedDatabaseGatesAndTerminalIsolationVerification(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	releaseSteps := fixture.plan["release_steps"].([]any)
	releaseSteps[1], releaseSteps[2] = releaseSteps[2], releaseSteps[1]
	raw, err := canonicalJSON(fixture.plan)
	if err != nil {
		t.Fatal(err)
	}
	if plan, err := parsePlan(fixture.plan, raw, fixture.config); err == nil {
		plan.release()
		t.Fatal("reordered database gates accepted")
	}
	zero(raw)
	releaseSteps[1], releaseSteps[2] = releaseSteps[2], releaseSteps[1]
	verifySteps := fixture.plan["verify_steps"].([]any)
	verifySteps[len(verifySteps)-1].(map[string]any)["id"] = "generic-verification"
	raw, err = canonicalJSON(fixture.plan)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(raw)
	if plan, err := parsePlan(fixture.plan, raw, fixture.config); err == nil {
		plan.release()
		t.Fatal("missing terminal runtime isolation verification accepted")
	}
	zero(raw)
	verifySteps[len(verifySteps)-1].(map[string]any)["id"] = VerifyRuntimeIsolationStepID
	releaseSteps[2].(map[string]any)["timeout_seconds"] = json.Number("1499")
	raw, err = canonicalJSON(fixture.plan)
	if err != nil {
		t.Fatal(err)
	}
	if plan, err := parsePlan(fixture.plan, raw, fixture.config); err == nil {
		plan.release()
		t.Fatal("database gate timeout rebinding accepted")
	}
	zero(raw)
	releaseSteps[2].(map[string]any)["timeout_seconds"] = json.Number("1500")
	releaseSteps[4].(map[string]any)["argv"].([]any)[9] = "/tmp/wrong-receipt.json"
	raw, err = canonicalJSON(fixture.plan)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(raw)
	if plan, err := parsePlan(fixture.plan, raw, fixture.config); err == nil {
		plan.release()
		t.Fatal("database gate receipt rebinding accepted")
	}
	zero(raw)
	releaseSteps[4].(map[string]any)["argv"].([]any)[9] = filepath.Join(DatabaseReceiptDirectory, fixtureRuntimeSHA, "verify-schema-readiness.json")
	releaseSteps[3].(map[string]any)["argv"].([]any)[7] = "postgres:16-alpine@sha256:" + strings.Repeat("0", 64)
	raw, err = canonicalJSON(fixture.plan)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(raw)
	if plan, err := parsePlan(fixture.plan, raw, fixture.config); err == nil {
		plan.release()
		t.Fatal("database gate image rebinding accepted")
	}
}

func TestDatabaseControlReceiptsBindEveryOrderedGateAndRuntimeEnvironment(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	for _, relative := range []string{
		"var/lib/propertyquarry-release-single-host-v2/database-receipts",
		"var/lib/propertyquarry-release-single-host-v2/database-receipts/" + fixture.config.RuntimeSHA,
		"var/lib/propertyquarry-release-single-host-v2/database-receipts/" + fixture.config.RuntimeSHA + "/" + fixture.config.DeploymentID,
	} {
		path := filepath.Join(fixture.root, relative)
		if err := os.Mkdir(path, 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(path, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	for _, contract := range databaseOperationContracts() {
		raw := writeDatabaseReceiptFixture(t, fixture, contract.operation, validDatabaseResult(contract.operation))
		proof, err := verifyDatabaseControlReceipt(fixture.root, fixture.config, fixture.receiptKey.Public().(ed25519.PublicKey), contract.operation, 1_799_999_999, 1_800_000_101)
		if err != nil {
			t.Fatalf("%s receipt rejected: %v", contract.operation, err)
		}
		if proof.operation != contract.operation || proof.receiptDigest != digest(raw) || proof.databaseOID != 83 || proof.envFileDigest == "" || !digestPattern.MatchString(proof.databaseImageID) || proof.databaseRepoDigest != canonicalRepoDigest(fixture.config.DatabaseImage) || proof.receiptKeyID != fixture.config.ReceiptAuthorityKeyID {
			t.Fatalf("%s receipt proof incomplete: %#v", contract.operation, proof)
		}
	}
	if proof, err := verifyDatabaseControlReceipt(fixture.root, fixture.config, fixture.receiptKey.Public().(ed25519.PublicKey), "provision-roles", 1_800_000_001, 1_800_000_101); err == nil {
		zero([]byte(proof.receiptDigest))
		t.Fatal("stale same-runtime database receipt accepted outside step interval")
	}
	provisionReceiptPath := rooted(fixture.root, filepath.Join(DatabaseReceiptDirectory, fixture.config.RuntimeSHA, fixture.config.DeploymentID, "provision-roles.json"))
	provisionReceiptRaw, err := os.ReadFile(provisionReceiptPath)
	if err != nil {
		t.Fatal(err)
	}
	provisionWrapper, err := strictJSON(provisionReceiptRaw[:len(provisionReceiptRaw)-1], maximumDatabaseReceiptBytes)
	if err != nil {
		t.Fatal(err)
	}
	provisionPayload := provisionWrapper["payload"].(map[string]any)
	provisionPayload["database_image"] = "postgres:16-alpine@sha256:" + strings.Repeat("0", 64)
	provisionPayloadRaw, err := canonicalJSON(provisionPayload)
	if err != nil {
		t.Fatal(err)
	}
	provisionSignature := ed25519.Sign(fixture.receiptKey, framed(databaseControlReceiptSignatureDomain, provisionPayloadRaw))
	provisionWrapper["signature"] = base64.RawURLEncoding.EncodeToString(provisionSignature)
	reboundReceipt, err := canonicalJSON(provisionWrapper)
	zero(provisionPayloadRaw)
	zero(provisionSignature)
	if err != nil {
		t.Fatal(err)
	}
	reboundReceipt = append(reboundReceipt, '\n')
	writeFixture(t, provisionReceiptPath, reboundReceipt, 0o600)
	if proof, err := verifyDatabaseControlReceipt(fixture.root, fixture.config, fixture.receiptKey.Public().(ed25519.PublicKey), "provision-roles", 1_799_999_999, 1_800_000_101); err == nil {
		zero([]byte(proof.receiptDigest))
		t.Fatal("signed database image substitution accepted")
	}
	provisionPayload["database_image"] = fixture.config.DatabaseImage
	provisionPayload["database_image_id"] = "sha256:" + strings.Repeat("0", 63)
	provisionPayloadRaw, err = canonicalJSON(provisionPayload)
	if err != nil {
		t.Fatal(err)
	}
	provisionSignature = ed25519.Sign(fixture.receiptKey, framed(databaseControlReceiptSignatureDomain, provisionPayloadRaw))
	provisionWrapper["signature"] = base64.RawURLEncoding.EncodeToString(provisionSignature)
	reboundReceipt, err = canonicalJSON(provisionWrapper)
	zero(provisionPayloadRaw)
	zero(provisionSignature)
	if err != nil {
		t.Fatal(err)
	}
	reboundReceipt = append(reboundReceipt, '\n')
	writeFixture(t, provisionReceiptPath, reboundReceipt, 0o600)
	if proof, err := verifyDatabaseControlReceipt(fixture.root, fixture.config, fixture.receiptKey.Public().(ed25519.PublicKey), "provision-roles", 1_799_999_999, 1_800_000_101); err == nil {
		zero([]byte(proof.receiptDigest))
		t.Fatal("malformed signed database image ID accepted")
	}
	provisionPayload["database_image_id"] = "sha256:" + strings.Repeat("c", 64)
	provisionPayload["database_repo_digest"] = "postgres@sha256:" + strings.Repeat("0", 64)
	provisionPayloadRaw, err = canonicalJSON(provisionPayload)
	if err != nil {
		t.Fatal(err)
	}
	provisionSignature = ed25519.Sign(fixture.receiptKey, framed(databaseControlReceiptSignatureDomain, provisionPayloadRaw))
	provisionWrapper["signature"] = base64.RawURLEncoding.EncodeToString(provisionSignature)
	reboundReceipt, err = canonicalJSON(provisionWrapper)
	zero(provisionPayloadRaw)
	zero(provisionSignature)
	if err != nil {
		t.Fatal(err)
	}
	reboundReceipt = append(reboundReceipt, '\n')
	writeFixture(t, provisionReceiptPath, reboundReceipt, 0o600)
	if proof, err := verifyDatabaseControlReceipt(fixture.root, fixture.config, fixture.receiptKey.Public().(ed25519.PublicKey), "provision-roles", 1_799_999_999, 1_800_000_101); err == nil {
		zero([]byte(proof.receiptDigest))
		t.Fatal("signed database repository digest substitution accepted")
	}

	envPath := rooted(fixture.root, DatabaseRuntimeEnvironmentPath)
	envRaw, err := os.ReadFile(envPath)
	if err != nil {
		t.Fatal(err)
	}
	envRaw[len(envRaw)-2] ^= 1
	writeFixture(t, envPath, envRaw, 0o600)
	if proof, err := verifyDatabaseControlReceipt(fixture.root, fixture.config, fixture.receiptKey.Public().(ed25519.PublicKey), "verify-schema-readiness", 1_799_999_999, 1_800_000_101); err == nil {
		zero([]byte(proof.receiptDigest))
		t.Fatal("database runtime environment tamper accepted")
	}
	envRaw[len(envRaw)-2] ^= 1
	writeFixture(t, envPath, envRaw, 0o600)

	receiptPath := rooted(fixture.root, filepath.Join(DatabaseReceiptDirectory, fixture.config.RuntimeSHA, fixture.config.DeploymentID, "migrate-schema.json"))
	receiptRaw, err := os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	wrapper, err := strictJSON(receiptRaw[:len(receiptRaw)-1], maximumDatabaseReceiptBytes)
	if err != nil {
		t.Fatal(err)
	}
	signatureText := wrapper["signature"].(string)
	signature, err := base64.RawURLEncoding.DecodeString(signatureText)
	if err != nil {
		t.Fatal(err)
	}
	signature[0] ^= 1
	wrapper["signature"] = base64.RawURLEncoding.EncodeToString(signature)
	zero(signature)
	tampered, err := canonicalJSON(wrapper)
	if err != nil {
		t.Fatal(err)
	}
	tampered = append(tampered, '\n')
	writeFixture(t, receiptPath, tampered, 0o600)
	if proof, err := verifyDatabaseControlReceipt(fixture.root, fixture.config, fixture.receiptKey.Public().(ed25519.PublicKey), "migrate-schema", 1_799_999_999, 1_800_000_101); err == nil {
		zero([]byte(proof.receiptDigest))
		t.Fatal("database receipt signature tamper accepted")
	}
}

func TestDatabaseRuntimeEnvironmentRequiresExactCanonicalRoleBindings(t *testing.T) {
	valid := fixtureDatabaseRuntimeEnvironment()
	if !validDatabaseRuntimeEnvironment(valid) {
		t.Fatal("valid database runtime environment rejected")
	}
	lines := bytes.Split(valid[:len(valid)-1], []byte{'\n'})
	lines[0], lines[1] = lines[1], lines[0]
	reordered := append(bytes.Join(lines, []byte{'\n'}), '\n')
	if validDatabaseRuntimeEnvironment(reordered) {
		t.Fatal("reordered database runtime environment accepted")
	}
	renderRebound := bytes.Replace(valid, []byte("PROPERTYQUARRY_RENDER_DATABASE_URL=postgresql://propertyquarry_admission_runtime:"), []byte("PROPERTYQUARRY_RENDER_DATABASE_URL=postgresql://propertyquarry_admission_runtime:B"), 1)
	if validDatabaseRuntimeEnvironment(renderRebound) {
		t.Fatal("render admission credential rebinding accepted")
	}
}

func TestDatabaseReceiptProofsRequireStableDatabaseAndCredentialIdentity(t *testing.T) {
	backupDigest := "sha256:" + strings.Repeat("1", 64)
	purgeDigest := "sha256:" + strings.Repeat("2", 64)
	retirementDigest := "sha256:" + strings.Repeat("3", 64)
	substrate := &databaseSubstrate{digest: "sha256:" + strings.Repeat("4", 64), containerID: strings.Repeat("5", 64)}
	proof := &databaseReceiptProof{
		operation: "provision-roles", databaseOID: 83, envFileDigest: "sha256:" + strings.Repeat("a", 64),
		databaseImageID: "sha256:" + strings.Repeat("b", 64), databaseRepoDigest: canonicalRepoDigest(DatabaseImage),
		databaseSubstrate: substrate, backupReceiptDigest: backupDigest, purgeReceiptDigest: purgeDigest,
		retirementReceiptDigest: retirementDigest, predecessorReceiptDigest: retirementDigest,
		receiptDigest: "sha256:" + strings.Repeat("6", 64), schemaStatus: "provisioned", startedAt: 100, finishedAt: 101,
	}
	fields := map[string]any{
		"predeploy_backup_receipt_digest": backupDigest, "runtime_isolation_purge_receipt_digest": purgeDigest,
		"runtime_retirement_receipt_digest": retirementDigest, "runtime_retirement_finished_at_epoch": json.Number("99"),
		"database_substrate_digest": substrate.digest,
	}
	if !databaseProofContinues(fields, proof) {
		t.Fatal("first database proof rejected")
	}
	for field, value := range databaseReceiptProofFields(proof) {
		fields[field] = value
	}
	migrate := &databaseReceiptProof{
		operation: "migrate-schema", databaseOID: proof.databaseOID, envFileDigest: proof.envFileDigest,
		databaseImageID: proof.databaseImageID, databaseRepoDigest: proof.databaseRepoDigest,
		databaseSubstrate: substrate, backupReceiptDigest: backupDigest, purgeReceiptDigest: purgeDigest,
		retirementReceiptDigest: retirementDigest, predecessorReceiptDigest: proof.receiptDigest,
		receiptDigest: "sha256:" + strings.Repeat("7", 64), schemaStatus: "migrated", startedAt: 102, finishedAt: 103,
		schemaVersions: map[string]int64{"kernel": 3, "property_search": 5, "google_identity": 2},
	}
	if !databaseProofContinues(fields, migrate) {
		t.Fatal("schema migration proof rejected")
	}
	for field, value := range databaseReceiptProofFields(migrate) {
		fields[field] = value
	}
	harden := &databaseReceiptProof{
		operation: "harden-runtime-acl", databaseOID: proof.databaseOID, envFileDigest: proof.envFileDigest,
		databaseImageID: proof.databaseImageID, databaseRepoDigest: proof.databaseRepoDigest,
		databaseSubstrate: substrate, backupReceiptDigest: backupDigest, purgeReceiptDigest: purgeDigest,
		retirementReceiptDigest: retirementDigest, predecessorReceiptDigest: migrate.receiptDigest,
		receiptDigest: "sha256:" + strings.Repeat("8", 64), schemaStatus: "ready", startedAt: 104, finishedAt: 105,
		schemaVersions: map[string]int64{"kernel": 3, "property_search": 5, "google_identity": 2},
	}
	if !databaseProofContinues(fields, harden) {
		t.Fatal("stable database schema proof rejected")
	}
	changedOID := *harden
	changedOID.databaseOID = 84
	if databaseProofContinues(fields, &changedOID) {
		t.Fatal("database OID substitution accepted")
	}
	changedEnvironment := *harden
	changedEnvironment.envFileDigest = "sha256:" + strings.Repeat("b", 64)
	if databaseProofContinues(fields, &changedEnvironment) {
		t.Fatal("database credential substitution accepted")
	}
	changedImageID := *harden
	changedImageID.databaseImageID = "sha256:" + strings.Repeat("c", 64)
	if databaseProofContinues(fields, &changedImageID) {
		t.Fatal("database image ID substitution accepted")
	}
	changedRepoDigest := *harden
	changedRepoDigest.databaseRepoDigest = "postgres@sha256:" + strings.Repeat("c", 64)
	if databaseProofContinues(fields, &changedRepoDigest) {
		t.Fatal("database repository digest substitution accepted")
	}
	changedVersion := *harden
	changedVersion.schemaVersions = map[string]int64{"kernel": 4, "property_search": 5, "google_identity": 2}
	if databaseProofContinues(fields, &changedVersion) {
		t.Fatal("database schema component version substitution accepted")
	}
	missingVersion := *harden
	missingVersion.schemaVersions = map[string]int64{"kernel": 3, "property_search": 5}
	if databaseProofContinues(fields, &missingVersion) {
		t.Fatal("partial database schema version proof accepted")
	}
}

func TestRuntimeIsolationPurgeReceiptAuthenticatesExactPreimageAndRollbackArtifact(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	for _, relative := range []string{
		"home", "home/tibor", "home/tibor/.local", "home/tibor/.local/share", "home/tibor/.local/share/propertyquarry-backup-keys",
		"var/lib/propertyquarry-release-single-host-v2/isolation-receipts", "var/lib/propertyquarry-release-single-host-v2/isolation-receipts/" + fixture.config.RuntimeSHA,
		"var/lib/propertyquarry-release-single-host-v2/isolation-receipts/" + fixture.config.RuntimeSHA + "/" + fixture.config.DeploymentID,
		"var/lib/propertyquarry-release-single-host-v2/isolation-rollback", "var/lib/propertyquarry-release-single-host-v2/isolation-rollback/" + fixture.config.RuntimeSHA,
		"var/lib/propertyquarry-release-single-host-v2/isolation-rollback/" + fixture.config.RuntimeSHA + "/" + fixture.config.DeploymentID,
	} {
		path := filepath.Join(fixture.root, relative)
		if err := os.Mkdir(path, 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(path, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	keyBytes := bytes.Repeat([]byte{0x71}, 32)
	writeFixture(t, rooted(fixture.root, PredeployBackupEncryptionKeyPath), []byte(fmt.Sprintf("%x\n", keyBytes)), 0o600)
	keyID := digest(keyBytes)
	zero(keyBytes)
	baseRaw := []byte("POST_PURGE_STATE=true\n")
	admissionRaw := []byte("PROPERTYQUARRY_API_ADMISSION_DATABASE_URL=fixture\n")
	writeFixture(t, rooted(fixture.root, BaseEnvironmentPath), baseRaw, 0o600)
	writeFixture(t, rooted(fixture.root, AdmissionEnvPath), admissionRaw, 0o600)
	artifactRaw := []byte("sealed-rollback-artifact")
	artifactPath := filepath.Join(runtimeIsolationRollbackDirectory, fixture.config.RuntimeSHA, fixture.config.DeploymentID, "root-env.pre-purge.enc")
	writeFixture(t, rooted(fixture.root, artifactPath), artifactRaw, 0o600)
	manifestRaw, manifestSignature := writePackageManifestFixture(t, fixture)
	authoritySignature, err := os.ReadFile(rooted(fixture.root, ConfigSignaturePath))
	if err != nil {
		t.Fatal(err)
	}
	googleRaw, _ := os.ReadFile(rooted(fixture.root, GoogleIdentityEnvPath))
	registrationRaw, _ := os.ReadFile(rooted(fixture.root, RegistrationEmailEnvPath))
	sceneRaw, _ := os.ReadFile(rooted(fixture.root, SceneVideoEnvPath))
	databaseRaw, _ := os.ReadFile(rooted(fixture.root, DatabaseRuntimeEnvironmentPath))
	backupDigest := "sha256:" + strings.Repeat("1", 64)
	previousBackupVerifier := verifyIsolationBackupReceipt
	verifyIsolationBackupReceipt = func(_ string, actual *Config, _ ed25519.PublicKey) (*backupReceiptProof, error) {
		if actual != fixture.config {
			return nil, os.ErrInvalid
		}
		return &backupReceiptProof{receiptDigest: backupDigest, databaseImageID: actual.DatabaseSubstrate.imageID, databaseRepoDigest: canonicalRepoDigest(actual.DatabaseImage), databaseSubstrate: actual.DatabaseSubstrate, startedAt: 1_799_999_990, finishedAt: 1_799_999_999}, nil
	}
	t.Cleanup(func() { verifyIsolationBackupReceipt = previousBackupVerifier })
	inputs := map[string]any{
		"file_digests": map[string]any{
			BaseEnvironmentPath: digest(baseRaw), SceneVideoEnvPath: digest(sceneRaw), DatabaseRuntimeEnvironmentPath: digest(databaseRaw),
			AdmissionEnvPath: digest(admissionRaw), GoogleIdentityEnvPath: digest(googleRaw), RegistrationEmailEnvPath: digest(registrationRaw),
		},
		"google_key_count": json.Number("5"), "legacy_registration_email_present": false, "registration_email_key_count": json.Number("10"),
	}
	result := map[string]any{
		"backup_receipt_sha256": backupDigest, "inputs": inputs, "legacy_keys_removed": json.Number("8"),
		"post_purge_root_env_digest": digest(baseRaw), "pre_purge_root_env_digest": fixture.config.PrePurgeRootEnvDigest,
		"rollback_artifact": map[string]any{
			"ciphertext_bytes": json.Number(strconv.Itoa(len(artifactRaw))), "ciphertext_sha256": digest(artifactRaw), "encryption_key_id": keyID,
			"path": artifactPath, "plaintext_bytes": json.Number("1024"), "plaintext_sha256": fixture.config.PrePurgeRootEnvDigest,
		},
		"rollback_artifact_expected_removed_keys": json.Number("8"),
	}
	payload := map[string]any{
		"api_container_port": json.Number("8090"), "api_host_ip": APIHostIP, "api_host_port": json.Number("8097"),
		"authority_digest": fixture.config.Digest, "authority_signature_digest": digest(authoritySignature), "backup_max_age_seconds": json.Number("3600"),
		"cloudflared_image": fixture.config.CloudflaredImage, "config_digest": fixture.config.Digest,
		"database_image": fixture.config.DatabaseImage, "database_substrate_digest": fixture.config.DatabaseSubstrateDigest,
		"deployment_id": fixture.config.DeploymentID, "envelope_sha": fixture.config.EnvelopeSHA,
		"finished_at_epoch": json.Number("1800000001"), "host_machine_id_digest": fixture.config.HostMachineIDDigest,
		"operation": operationPurgeRuntimeIsolation, "package_authority_key_id": fixture.config.PackageAuthorityKeyID,
		"package_manifest_digest": digest(manifestRaw), "package_manifest_signature_digest": digest(manifestSignature), "plan_digest": fixture.config.PlanDigest,
		"pre_purge_runtime_inputs": runtimeInputObservationValues(fixture.config.PrePurgeRuntimeInputs),
		"production_ready":         false, "receipt_authority_key_id": fixture.config.ReceiptAuthorityKeyID, "render_image": fixture.config.RenderImage,
		"result": result, "runtime_deploy_digest": fixture.config.RuntimeDeployDigest, "runtime_inputs": runtimeInputObservationValues(fixture.config.RuntimeInputs),
		"runtime_retirement_digest": fixture.config.RuntimeRetirementDigest, "runtime_sha": fixture.config.RuntimeSHA, "scene_video_env_digest": fixture.config.SceneVideoEnvDigest,
		"scene_video_env_gid": json.Number(strconv.FormatInt(fixture.config.SceneVideoEnvGID, 10)), "scene_video_env_mode": json.Number("384"),
		"scene_video_env_path": SceneVideoEnvPath, "scene_video_env_uid": json.Number(strconv.FormatInt(fixture.config.SceneVideoEnvUID, 10)),
		"schema": runtimeIsolationReceiptSchema, "secret_values_emitted": false, "started_at_epoch": json.Number("1800000000"),
		"status": "verified", "transaction_started_at_epoch": json.Number("1800000000"), "web_image": fixture.config.WebImage,
	}
	writeIsolationReceiptFixture(t, fixture, operationPurgeRuntimeIsolation, payload)
	proof, err := verifyRuntimeIsolationReceipt(fixture.root, fixture.config, fixture.receiptKey.Public().(ed25519.PublicKey), operationPurgeRuntimeIsolation, 1_799_999_999, 1_800_000_002)
	if err != nil {
		t.Fatal(err)
	}
	if proof.backupReceiptDigest != backupDigest || proof.prePurgeRootEnvDigest != fixture.config.PrePurgeRootEnvDigest || proof.postPurgeRootEnvDigest != digest(baseRaw) || proof.rollbackArtifactDigest != digest(artifactRaw) {
		t.Fatalf("purge isolation proof incomplete: %#v", proof)
	}
	result["legacy_keys_removed"] = json.Number("10")
	result["rollback_artifact_expected_removed_keys"] = json.Number("10")
	writeIsolationReceiptFixture(t, fixture, operationPurgeRuntimeIsolation, payload)
	if proof, err := verifyRuntimeIsolationReceipt(fixture.root, fixture.config, fixture.receiptKey.Public().(ed25519.PublicKey), operationPurgeRuntimeIsolation, 1_799_999_999, 1_800_000_002); err != nil {
		t.Fatalf("full ten-key purge receipt rejected: %v", err)
	} else {
		zero([]byte(proof.receiptDigest))
	}
	result["legacy_keys_removed"] = json.Number("8")
	result["rollback_artifact_expected_removed_keys"] = json.Number("8")
	for _, invalid := range []struct {
		field map[string]any
		name  string
		value json.Number
	}{
		{field: inputs, name: "registration_email_key_count", value: json.Number("8")},
		{field: result, name: "legacy_keys_removed", value: json.Number("10")},
		{field: result, name: "rollback_artifact_expected_removed_keys", value: json.Number("9")},
		{field: result, name: "rollback_artifact_expected_removed_keys", value: json.Number("10")},
	} {
		original := invalid.field[invalid.name]
		invalid.field[invalid.name] = invalid.value
		writeIsolationReceiptFixture(t, fixture, operationPurgeRuntimeIsolation, payload)
		if proof, err := verifyRuntimeIsolationReceipt(fixture.root, fixture.config, fixture.receiptKey.Public().(ed25519.PublicKey), operationPurgeRuntimeIsolation, 1_799_999_999, 1_800_000_002); err == nil {
			zero([]byte(proof.receiptDigest))
			t.Fatalf("invalid mail-removal %s=%s accepted", invalid.name, invalid.value)
		}
		invalid.field[invalid.name] = original
	}
	result["pre_purge_root_env_digest"] = "sha256:" + strings.Repeat("0", 64)
	writeIsolationReceiptFixture(t, fixture, operationPurgeRuntimeIsolation, payload)
	if proof, err := verifyRuntimeIsolationReceipt(fixture.root, fixture.config, fixture.receiptKey.Public().(ed25519.PublicKey), operationPurgeRuntimeIsolation, 1_799_999_999, 1_800_000_002); err == nil {
		zero([]byte(proof.receiptDigest))
		t.Fatal("signed purge preimage substitution accepted")
	}
}

func TestRuntimeIsolationDatabaseSummariesRequireImageAndReceiptContinuity(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	envRaw, err := os.ReadFile(rooted(fixture.root, DatabaseRuntimeEnvironmentPath))
	if err != nil {
		t.Fatal(err)
	}
	envDigest := digest(envRaw)
	imageID := fixture.config.DatabaseSubstrate.imageID
	repoDigest := canonicalRepoDigest(fixture.config.DatabaseImage)
	items := map[string]any{}
	previousVerifier := verifyIsolationDatabaseReceipt
	verifyIsolationDatabaseReceipt = func(_ string, _ *Config, _ ed25519.PublicKey, operation string, started, finished int64) (*databaseReceiptProof, error) {
		predecessor := map[string]string{
			"migrate-schema": digest([]byte("provision-roles")), "harden-runtime-acl": digest([]byte("migrate-schema")),
			"verify-schema-readiness": digest([]byte("harden-runtime-acl")),
		}[operation]
		return &databaseReceiptProof{operation: operation, receiptDigest: digest([]byte(operation)), predecessorReceiptDigest: predecessor, envFileDigest: envDigest, databaseImageID: imageID, databaseRepoDigest: repoDigest, databaseSubstrate: fixture.config.DatabaseSubstrate, databaseOID: 83, schemaStatus: map[string]string{"provision-roles": "provisioned", "migrate-schema": "migrated", "harden-runtime-acl": "ready", "verify-schema-readiness": "ready"}[operation], startedAt: started, finishedAt: finished}, nil
	}
	t.Cleanup(func() { verifyIsolationDatabaseReceipt = previousVerifier })
	for index, contract := range databaseOperationContracts() {
		started := int64(100 + index*10)
		finished := started + 5
		items[contract.operation] = map[string]any{
			"database_image_id": imageID, "database_oid": json.Number("83"), "database_repo_digest": repoDigest,
			"env_file_sha256": envDigest, "finished_at_epoch": json.Number(strconv.FormatInt(finished, 10)),
			"receipt_sha256": digest([]byte(contract.operation)), "schema_status": map[string]string{"provision-roles": "provisioned", "migrate-schema": "migrated", "harden-runtime-acl": "ready", "verify-schema-readiness": "ready"}[contract.operation],
			"started_at_epoch": json.Number(strconv.FormatInt(started, 10)),
		}
	}
	if proofs, err := validateIsolationDatabaseReceipts(fixture.root, fixture.config, fixture.receiptKey.Public().(ed25519.PublicKey), items); err != nil || len(proofs) != 4 {
		t.Fatalf("valid database receipt summaries rejected: %v", err)
	}
	items["migrate-schema"].(map[string]any)["database_image_id"] = "sha256:" + strings.Repeat("d", 64)
	if _, err := validateIsolationDatabaseReceipts(fixture.root, fixture.config, fixture.receiptKey.Public().(ed25519.PublicKey), items); err == nil {
		t.Fatal("terminal isolation accepted changed database image identity")
	}
}

func writeIsolationReceiptFixture(t *testing.T, fixture *authorityFixture, operation string, payload map[string]any) {
	t.Helper()
	payloadRaw, err := canonicalJSON(payload)
	if err != nil {
		t.Fatal(err)
	}
	signature := ed25519.Sign(fixture.receiptKey, framed(runtimeIsolationReceiptSignatureDomain, payloadRaw))
	wrapper, err := canonicalJSON(map[string]any{"payload": payload, "signature": base64.RawURLEncoding.EncodeToString(signature), "signature_key_id": fixture.config.ReceiptAuthorityKeyID})
	zero(payloadRaw)
	zero(signature)
	if err != nil {
		t.Fatal(err)
	}
	wrapper = append(wrapper, '\n')
	writeFixture(t, rooted(fixture.root, filepath.Join(runtimeIsolationReceiptDirectory, fixture.config.RuntimeSHA, fixture.config.DeploymentID, operation+".json")), wrapper, 0o600)
}

func writeDatabaseReceiptFixture(t *testing.T, fixture *authorityFixture, operation string, result map[string]any) []byte {
	t.Helper()
	envRaw, err := os.ReadFile(rooted(fixture.root, DatabaseRuntimeEnvironmentPath))
	if err != nil {
		t.Fatal(err)
	}
	payload := map[string]any{
		"authority_digest": fixture.config.Digest, "backup_max_age_seconds": json.Number("3600"),
		"backup_receipt_sha256": "sha256:" + strings.Repeat("7", 64), "database": databaseControlDatabase,
		"database_container": databaseControlContainer, "docker_network": databaseControlNetwork,
		"database_image":    fixture.config.DatabaseImage,
		"database_image_id": fixture.config.DatabaseSubstrate.imageID, "database_repo_digest": canonicalRepoDigest(fixture.config.DatabaseImage),
		"database_substrate_before": fixture.config.DatabaseSubstrate.value, "database_substrate_after": fixture.config.DatabaseSubstrate.value,
		"deployment_id": fixture.config.DeploymentID,
		"env_file":      DatabaseRuntimeEnvironmentPath, "env_file_sha256": digest(envRaw),
		"finished_at_epoch": json.Number("1800000003"), "host_machine_id_digest": fixture.config.HostMachineIDDigest,
		"operation": operation, "production_ready": false, "receipt_authority_key_id": fixture.config.ReceiptAuthorityKeyID,
		"predecessor_receipt_sha256": "sha256:" + strings.Repeat("8", 64), "purge_receipt_sha256": "sha256:" + strings.Repeat("9", 64),
		"retirement_receipt_sha256": "sha256:" + strings.Repeat("a", 64), "result": result,
		"runtime_inputs": runtimeInputObservationValues(fixture.config.RuntimeInputs), "runtime_sha": fixture.config.RuntimeSHA, "schema": databaseControlReceiptSchema,
		"secret_values_emitted": false, "started_at_epoch": json.Number("1800000000"), "status": "verified",
		"transaction_started_at_epoch": json.Number("1800000000"),
		"web_image":                    fixture.config.WebImage,
	}
	payloadRaw, err := canonicalJSON(payload)
	if err != nil {
		t.Fatal(err)
	}
	signature := ed25519.Sign(fixture.receiptKey, framed(databaseControlReceiptSignatureDomain, payloadRaw))
	wrapperRaw, err := canonicalJSON(map[string]any{
		"payload": payload, "signature": base64.RawURLEncoding.EncodeToString(signature),
		"signature_key_id": fixture.config.ReceiptAuthorityKeyID,
	})
	zero(payloadRaw)
	zero(signature)
	if err != nil {
		t.Fatal(err)
	}
	wrapperRaw = append(wrapperRaw, '\n')
	receiptPath := rooted(fixture.root, filepath.Join(DatabaseReceiptDirectory, fixture.config.RuntimeSHA, fixture.config.DeploymentID, operation+".json"))
	writeFixture(t, receiptPath, wrapperRaw, 0o600)
	return wrapperRaw
}

func validDatabaseResult(operation string) map[string]any {
	if operation == "provision-roles" {
		return map[string]any{
			"credential_reused": false, "database_oid": json.Number("83"),
			"roles": []any{"propertyquarry_owner", "propertyquarry_migrator", "propertyquarry_api", "propertyquarry_worker", "propertyquarry_scheduler"},
		}
	}
	components := func(readiness bool) map[string]any {
		result := map[string]any{}
		for field, component := range map[string]string{"kernel": "ea_kernel", "property_search": "property_search", "google_identity": "propertyquarry_google_identity"} {
			if readiness {
				result[field] = map[string]any{
					"applied_versions": []any{json.Number("1")}, "component": component,
					"current_version": json.Number("1"), "ready": true, "reason": "ready", "required_version": json.Number("1"),
				}
			} else {
				result[field] = map[string]any{
					"applied_versions": []any{}, "component": component,
					"current_version": json.Number("1"), "previous_version": json.Number("1"),
				}
			}
		}
		return result
	}
	if operation == "migrate-schema" {
		schema := components(false)
		schema["status"] = "migrated"
		return map[string]any{"credential_reused": true, "database_oid": json.Number("83"), "schema": schema}
	}
	schema := components(true)
	schema["ready"] = true
	schema["status"] = "ready"
	return map[string]any{"credential_reused": true, "database_oid": json.Number("83"), "schema": schema}
}

func TestPredeployBackupReceiptBindsSignedRemoteCiphertextProof(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	for _, relative := range []string{
		"home", "home/tibor", "home/tibor/.local", "home/tibor/.local/share", "home/tibor/.local/share/propertyquarry-backup-keys",
		"mnt", "mnt/pcloud", "mnt/pcloud/propertyquarry", "mnt/pcloud/propertyquarry/releases", "mnt/pcloud/propertyquarry/releases/backups", "mnt/pcloud/propertyquarry/releases/backups/v2",
		"mnt/pcloud/propertyquarry/releases/backups/v2/" + fixture.config.RuntimeSHA,
		"var/lib/propertyquarry-release-single-host-v2/backup-receipts",
		"var/lib/propertyquarry-release-single-host-v2/backup-receipts/" + fixture.config.RuntimeSHA,
		"var/lib/propertyquarry-release-single-host-v2/backup-receipts/" + fixture.config.RuntimeSHA + "/" + fixture.config.DeploymentID,
	} {
		path := filepath.Join(fixture.root, relative)
		if err := os.Mkdir(path, 0o755); err != nil {
			t.Fatal(err)
		}
		mode := os.FileMode(0o755)
		if strings.Contains(relative, "propertyquarry-backup-keys") || strings.Contains(relative, "backup-receipts") {
			mode = 0o700
		}
		if err := os.Chmod(path, mode); err != nil {
			t.Fatal(err)
		}
	}
	keyBytes := bytes.Repeat([]byte{0x11}, 32)
	keyRaw := []byte(fmt.Sprintf("%x\n", keyBytes))
	writeFixture(t, rooted(fixture.root, PredeployBackupEncryptionKeyPath), keyRaw, 0o600)
	encryptionKeyID := digest(keyBytes)
	zero(keyBytes)
	zero(keyRaw)

	packageManifestRaw, packageManifestSignature := writePackageManifestFixture(t, fixture)

	remotePath := filepath.Join(predeployBackupRemoteParent, fixture.config.RuntimeSHA, fixture.config.DeploymentID)
	remoteRoot := rooted(fixture.root, remotePath)
	if err := os.Mkdir(remoteRoot, 0o775); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(remoteRoot, 0o775); err != nil {
		t.Fatal(err)
	}
	artifacts := make([]any, 0, len(backupArtifactContracts()))
	for _, contract := range backupArtifactContracts() {
		ciphertext := []byte("ciphertext-" + contract.name)
		writeFixture(t, filepath.Join(remoteRoot, contract.name+".pqenc"), ciphertext, 0o664)
		verification := map[string]any{"method": contract.method}
		switch contract.verification {
		case "database":
			verification["table_data_entries"] = json.Number("1")
			verification["toc_lines"] = json.Number("2")
		case "tar":
			verification["entries"] = json.Number("1")
		}
		coverage := make([]any, 0, len(contract.coverage))
		for _, item := range contract.coverage {
			coverage = append(coverage, item)
		}
		artifacts = append(artifacts, map[string]any{
			"chunk_count": json.Number("1"), "ciphertext_bytes": json.Number(strconv.Itoa(len(ciphertext))),
			"ciphertext_sha256": digest(ciphertext), "coverage": coverage, "filename": contract.name + ".pqenc",
			"kind": contract.kind, "name": contract.name, "plaintext_bytes": json.Number("1"),
			"plaintext_sha256": digest([]byte("p-" + contract.name)), "verification": verification,
		})
	}
	coverage := map[string]any{
		"config": []any{
			"/docker/property/.env",
			"/docker/property/config",
			"/docker/property/state/runtime/property_scene_video_shared.env",
			"/docker/property/state/runtime/propertyquarry_admission.env",
			"/docker/property/state/runtime/propertyquarry_database_roles.env",
			GoogleIdentityEnvPath,
			RegistrationEmailEnvPath,
		},
		"database": []any{"propertyquarry"}, "roles": []any{"postgres-cluster-roles"},
		"binds": []any{"/docker/property/state/incoming_property_tours"},
		"volumes": []any{
			"/var/lib/docker/volumes/property_propertyquarry_artifacts/_data",
			"/var/lib/docker/volumes/property_propertyquarry_governed_render_consents/_data",
			"/var/lib/docker/volumes/property_propertyquarry_provider_ledger/_data",
			"/var/lib/docker/volumes/property_propertyquarry_public_tours/_data",
		},
	}
	authoritySignature, err := os.ReadFile(rooted(fixture.root, ConfigSignaturePath))
	if err != nil {
		t.Fatal(err)
	}
	remoteManifestRaw, err := canonicalJSON(map[string]any{
		"artifacts": artifacts,
		"bindings": map[string]any{
			"authority_digest": fixture.config.Digest, "authority_signature_digest": digest(authoritySignature),
			"backup_max_age_seconds": json.Number("3600"), "config_digest": fixture.config.Digest,
			"database_image": fixture.config.DatabaseImage, "database_image_id": fixture.config.DatabaseSubstrate.imageID,
			"database_repo_digest":     canonicalRepoDigest(fixture.config.DatabaseImage),
			"database_substrate_after": fixture.config.DatabaseSubstrate.value, "database_substrate_before": fixture.config.DatabaseSubstrate.value,
			"deployment_id": fixture.config.DeploymentID, "envelope_sha": fixture.config.EnvelopeSHA,
			"package_authority_key_id": fixture.config.PackageAuthorityKeyID, "package_manifest_digest": digest(packageManifestRaw),
			"package_manifest_signature_digest": digest(packageManifestSignature), "plan_digest": fixture.config.PlanDigest,
			"pre_purge_runtime_inputs": runtimeInputObservationValues(fixture.config.PrePurgeRuntimeInputs),
			"render_image":             fixture.config.RenderImage, "runtime_sha": fixture.config.RuntimeSHA,
			"transaction_started_at_epoch": json.Number("1800000000"), "web_image": fixture.config.WebImage,
		},
		"encryption_key_id": encryptionKeyID, "plaintext_retained": false,
		"schema": "propertyquarry.predeploy-backup-remote-manifest.v2", "verification_complete": true,
	})
	if err != nil {
		t.Fatal(err)
	}
	remoteManifestRaw = append(remoteManifestRaw, '\n')
	writeFixture(t, filepath.Join(remoteRoot, predeployBackupManifestName), remoteManifestRaw, 0o664)
	payload := map[string]any{
		"artifacts": artifacts, "atomic_finalize": true, "authority_digest": fixture.config.Digest,
		"authority_signature_digest": digest(authoritySignature), "backup_max_age_seconds": json.Number("3600"), "config_digest": fixture.config.Digest, "coverage": coverage,
		"database_image": fixture.config.DatabaseImage, "database_image_id": fixture.config.DatabaseSubstrate.imageID,
		"database_repo_digest": canonicalRepoDigest(fixture.config.DatabaseImage), "database_substrate_after": fixture.config.DatabaseSubstrate.value,
		"database_substrate_before": fixture.config.DatabaseSubstrate.value, "deployment_id": fixture.config.DeploymentID,
		"disposition": "verified-and-published", "encryption_key_created": false, "encryption_key_id": encryptionKeyID,
		"envelope_sha": fixture.config.EnvelopeSHA, "finished_at_epoch": json.Number("1800000100"), "fsync_artifacts": true,
		"fsync_directories": true, "host_machine_id_digest": fixture.config.HostMachineIDDigest,
		"package_authority_key_id": fixture.config.PackageAuthorityKeyID, "package_manifest_digest": digest(packageManifestRaw),
		"package_manifest_signature_digest": digest(packageManifestSignature), "plaintext_retained": false,
		"plan_digest": fixture.config.PlanDigest, "pre_purge_runtime_inputs": runtimeInputObservationValues(fixture.config.PrePurgeRuntimeInputs),
		"production_ready": false, "receipt_authority_key_id": fixture.config.ReceiptAuthorityKeyID,
		"remote":       map[string]any{"manifest_sha256": digest(remoteManifestRaw), "path": remotePath, "provider": "pcloud-rclone", "version": "v2"},
		"render_image": fixture.config.RenderImage, "runtime_sha": fixture.config.RuntimeSHA, "schema": predeployBackupReceiptSchema,
		"started_at_epoch": json.Number("1800000000"), "transaction_started_at_epoch": json.Number("1800000000"), "web_image": fixture.config.WebImage,
	}
	payloadRaw, err := canonicalJSON(payload)
	if err != nil {
		t.Fatal(err)
	}
	signature := ed25519.Sign(fixture.receiptKey, framed(predeployBackupReceiptSignatureDomain, payloadRaw))
	receiptRaw, err := canonicalJSON(map[string]any{
		"payload": payload, "signature": base64.RawURLEncoding.EncodeToString(signature), "signature_key_id": fixture.config.ReceiptAuthorityKeyID,
	})
	if err != nil {
		t.Fatal(err)
	}
	receiptRaw = append(receiptRaw, '\n')
	receiptPath := rooted(fixture.root, filepath.Join(PredeployBackupReceiptDirectory, fixture.config.RuntimeSHA, fixture.config.DeploymentID, "create.json"))
	writeFixture(t, receiptPath, receiptRaw, 0o600)
	proof, err := verifyPredeployBackupReceipt(fixture.root, fixture.config, fixture.receiptKey.Public().(ed25519.PublicKey))
	if err != nil {
		t.Fatal(err)
	}
	if proof.receiptDigest != digest(receiptRaw) || proof.remotePath != remotePath || proof.manifestDigest != digest(remoteManifestRaw) || proof.encryptionKeyID != encryptionKeyID || proof.databaseImageID != fixture.config.DatabaseSubstrate.imageID || proof.databaseRepoDigest != canonicalRepoDigest(fixture.config.DatabaseImage) {
		t.Fatalf("backup proof binding changed: %#v", proof)
	}
	remoteManifestValue, err := strictJSON(remoteManifestRaw[:len(remoteManifestRaw)-1], len(remoteManifestRaw))
	if err != nil || validateRemoteBackupManifest(fixture.config, payload, remoteManifestValue) != nil {
		t.Fatalf("valid remote manifest binding rejected: %v", err)
	}
	remoteManifestValue["artifacts"].([]any)[0].(map[string]any)["ciphertext_sha256"] = "sha256:" + strings.Repeat("f", 64)
	if err := validateRemoteBackupManifest(fixture.config, payload, remoteManifestValue); err == nil {
		t.Fatal("remote manifest artifact rebinding accepted")
	}
	extraPath := filepath.Join(remoteRoot, "unexpected.pqenc")
	writeFixture(t, extraPath, []byte("unexpected"), 0o664)
	if proof, err := verifyPredeployBackupReceipt(fixture.root, fixture.config, fixture.receiptKey.Public().(ed25519.PublicKey)); err == nil {
		zero([]byte(proof.receiptDigest))
		t.Fatal("extra remote backup artifact accepted")
	}
	if err := os.Remove(extraPath); err != nil {
		t.Fatal(err)
	}
	firstArtifact := filepath.Join(remoteRoot, backupArtifactContracts()[0].name+".pqenc")
	tampered, err := os.ReadFile(firstArtifact)
	if err != nil {
		t.Fatal(err)
	}
	tampered[len(tampered)-1] ^= 1
	writeFixture(t, firstArtifact, tampered, 0o664)
	if proof, err := verifyPredeployBackupReceipt(fixture.root, fixture.config, fixture.receiptKey.Public().(ed25519.PublicKey)); err == nil {
		zero([]byte(proof.receiptDigest))
		t.Fatal("same-size remote ciphertext tamper accepted")
	}
}

func TestJournalIsSignedContiguousDurableAndAntiDowngrade(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	journal, err := OpenJournal(fixture.root, fixture.receiptKey)
	if err != nil {
		t.Fatal(err)
	}
	if err := enforceAntiDowngrade(journal, fixture.config); err != nil {
		t.Fatal(err)
	}
	fields := testAuthorityFields(fixture.config, "release-preflight", "request-1", "jti-1")
	wire, err := journal.Append("preflight-ready", fields)
	if err != nil {
		t.Fatal(err)
	}
	if len(wire) == 0 || journal.HeadDigest() != digest(wire) {
		t.Fatal("journal head not bound")
	}
	journal.Close()
	journal, err = OpenJournal(fixture.root, fixture.receiptKey)
	if err != nil {
		t.Fatal(err)
	}
	if len(journal.events) != 1 || journal.events[0].Sequence != 1 {
		t.Fatal("journal did not rebuild")
	}
	if err := enforceAntiDowngrade(journal, fixture.config); err != nil {
		t.Fatal(err)
	}
	successor := *fixture.config
	successor.ReleaseGeneration = 2
	successor.PredecessorRuntimeSHA = fixtureRuntimeSHA
	successor.RuntimeSHA = strings.Repeat("c", 40)
	successor.Digest = "sha256:" + strings.Repeat("d", 64)
	if err := enforceAntiDowngrade(journal, &successor); err != nil {
		t.Fatal(err)
	}
	successor.ReleaseGeneration = 3
	if err := enforceAntiDowngrade(journal, &successor); err == nil {
		t.Fatal("generation skip accepted")
	}
	journal.Close()
	path := filepath.Join(rooted(fixture.root, JournalPath), journalEventName(1))
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	raw[len(raw)/2] ^= 1
	writeFixture(t, path, raw, 0o600)
	if journal, err := OpenJournal(fixture.root, fixture.receiptKey); err == nil {
		journal.Close()
		t.Fatal("tampered journal authenticated")
	}
}

func TestJournalRecoversDurablePendingAppendAfterProcessKill(t *testing.T) {
	if root := os.Getenv("PROPERTYQUARRY_TEST_PENDING_JOURNAL_ROOT"); root != "" {
		runPendingJournalCrashChild(t, root)
		return
	}
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	readyReader, readyWriter, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer readyReader.Close()
	command := exec.Command(os.Args[0], "-test.run=^TestJournalRecoversDurablePendingAppendAfterProcessKill$")
	command.Env = append(os.Environ(), "PROPERTYQUARRY_TEST_PENDING_JOURNAL_ROOT="+fixture.root)
	command.ExtraFiles = []*os.File{readyWriter}
	var childOutput bytes.Buffer
	command.Stdout = &childOutput
	command.Stderr = &childOutput
	if err := command.Start(); err != nil {
		readyWriter.Close()
		t.Fatal(err)
	}
	if err := readyWriter.Close(); err != nil {
		_ = command.Process.Kill()
		_ = command.Wait()
		t.Fatal(err)
	}
	ready := make(chan error, 1)
	go func() {
		var signal [1]byte
		_, readErr := io.ReadFull(readyReader, signal[:])
		if readErr == nil && signal[0] != 1 {
			readErr = fmt.Errorf("unexpected-child-signal")
		}
		ready <- readErr
	}()
	select {
	case err := <-ready:
		if err != nil {
			_ = command.Process.Kill()
			_ = command.Wait()
			t.Fatalf("pending append child did not reach crash boundary: %v: %s", err, childOutput.String())
		}
	case <-time.After(10 * time.Second):
		_ = command.Process.Kill()
		_ = command.Wait()
		t.Fatalf("pending append child timed out: %s", childOutput.String())
	}
	if err := command.Process.Kill(); err != nil {
		_ = command.Wait()
		t.Fatal(err)
	}
	waitErr := command.Wait()
	exitErr, ok := waitErr.(*exec.ExitError)
	status, statusOK := command.ProcessState.Sys().(syscall.WaitStatus)
	if !ok || !statusOK || !status.Signaled() || status.Signal() != syscall.SIGKILL {
		t.Fatalf("pending append child was not killed at the durability boundary: %v %#v: %s", waitErr, exitErr, childOutput.String())
	}
	directory := rooted(fixture.root, JournalPath)
	entries, err := os.ReadDir(directory)
	if err != nil {
		t.Fatal(err)
	}
	pendingCount := 0
	for _, entry := range entries {
		if journalPendingPattern.MatchString(entry.Name()) {
			pendingCount++
		}
	}
	if pendingCount != 1 {
		t.Fatalf("killed append did not leave one durable pending event: %#v", entries)
	}
	journal, err := OpenJournal(fixture.root, fixture.receiptKey)
	if err != nil {
		t.Fatal(err)
	}
	if len(journal.events) != 1 || journal.events[0].Sequence != 1 || journal.events[0].RequestID != "request-crash" || journal.HeadDigest() != journal.events[0].ReceiptDigest {
		journal.Close()
		t.Fatalf("pending event was not authenticated and promoted: %#v", journal.events)
	}
	if _, err := os.Lstat(filepath.Join(directory, journalEventName(1))); err != nil {
		journal.Close()
		t.Fatal(err)
	}
	entries, err = os.ReadDir(directory)
	if err != nil {
		journal.Close()
		t.Fatal(err)
	}
	for _, entry := range entries {
		if journalPendingPattern.MatchString(entry.Name()) {
			journal.Close()
			t.Fatalf("promoted pending event remains at %s", entry.Name())
		}
	}
	if _, err := journal.Append("run-started", pendingJournalFields("release-run", "request-next", "jti-next")); err != nil {
		journal.Close()
		t.Fatal(err)
	}
	journal.Close()
	journal, err = OpenJournal(fixture.root, fixture.receiptKey)
	if err != nil {
		t.Fatal(err)
	}
	defer journal.Close()
	if len(journal.events) != 2 || journal.events[0].Sequence != 1 || journal.events[1].Sequence != 2 || journal.events[1].PredecessorDigest != journal.events[0].ReceiptDigest {
		t.Fatalf("recovered journal did not resume a contiguous chain: %#v", journal.events)
	}
}

func TestJournalRejectsTamperedPendingAppendWithoutMutation(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	keyID, err := publicKeyID(fixture.receiptKey.Public().(ed25519.PublicKey))
	if err != nil {
		t.Fatal(err)
	}
	payload := pendingJournalFields("release-preflight", "request-tampered", "jti-tampered")
	payload["schema"] = journalSchema
	payload["version"] = json.Number("2")
	payload["journal_sequence"] = json.Number("1")
	payload["journal_predecessor_digest"] = journalGenesisDigest
	payload["event_type"] = "preflight-ready"
	payload["receipt_key_id"] = keyID
	wire, err := signReceipt(payload, fixture.receiptKey)
	if err != nil {
		t.Fatal(err)
	}
	wire[len(wire)/2] ^= 1
	pendingPath := filepath.Join(rooted(fixture.root, JournalPath), ".pending-"+strings.Repeat("a", 64)+".tmp")
	writeFixture(t, pendingPath, wire, 0o600)
	before := append([]byte(nil), wire...)
	zero(wire)
	if journal, err := OpenJournal(fixture.root, fixture.receiptKey); err == nil {
		journal.Close()
		t.Fatal("tampered pending event was accepted")
	}
	after, err := os.ReadFile(pendingPath)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(after, before) {
		t.Fatal("failed pending authentication mutated forensic evidence")
	}
	if _, err := os.Lstat(filepath.Join(rooted(fixture.root, JournalPath), journalEventName(1))); !os.IsNotExist(err) {
		t.Fatalf("tampered pending event was published: %v", err)
	}
}

func runPendingJournalCrashChild(t *testing.T, root string) {
	t.Helper()
	config, key, err := LoadConfig(root)
	if err != nil {
		t.Fatal(err)
	}
	config.release()
	defer zero(key)
	journal, err := OpenJournal(root, key)
	if err != nil {
		t.Fatal(err)
	}
	defer journal.Close()
	ready := os.NewFile(uintptr(3), "pending-journal-ready")
	if ready == nil {
		t.Fatal("pending journal signal unavailable")
	}
	journal.afterPendingSync = func() {
		if _, err := ready.Write([]byte{1}); err != nil {
			os.Exit(91)
		}
		if err := ready.Close(); err != nil {
			os.Exit(92)
		}
		if err := syscall.Kill(os.Getpid(), syscall.SIGSTOP); err != nil {
			os.Exit(93)
		}
		os.Exit(94)
	}
	if _, err := journal.Append("preflight-ready", pendingJournalFields("release-preflight", "request-crash", "jti-crash")); err != nil {
		t.Fatal(err)
	}
	t.Fatal("pending append child passed the crash boundary")
}

func pendingJournalFields(operation, requestID, jti string) map[string]any {
	return map[string]any{
		"operation": operation, "run_id": "123", "run_attempt": json.Number("1"),
		"request_id": requestID, "oidc_jti": jti,
	}
}

func bindFixtureRunnerEvidence(request *workflowRequest) {
	request.DiagnosticRunnerLabel = "pqrelease-" + strings.Repeat("a", 32)
	request.RunnerTicketDigest = "sha256:" + strings.Repeat("7", 64)
}

func TestWorkflowPreflightThenAtomicRunProducesSignedTerminalReceipt(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	withFakeAuthentication(t, fixture)
	preflight := &workflowRequest{Operation: "release-preflight", RequestID: "request-preflight", OIDCRequestURL: "https://vstoken.actions.githubusercontent.com/x", ActionsToken: []byte("jti-preflight"), DiagnosticRunID: "123", DiagnosticRunAttempt: 1, DiagnosticSHA: fixtureWorkflowSHA, DiagnosticWorkflowSHA: fixtureWorkflowSHA,
		SecurityBootstrapAttestationSHA: strings.Repeat("a", 64), SecurityBootstrapRunID: "456", SecurityBootstrapArtifactDigest: "sha256:" + strings.Repeat("b", 64)}
	bindFixtureRunnerEvidence(preflight)
	readyWire, err := processWorkflowRequest(fixture.root, fixture.config, fixture.receiptKey, preflight)
	if err != nil {
		t.Fatal(err)
	}
	ready := receiptPayload(t, readyWire)
	if ready["disposition"] != "ready" || ready["ready"] != true || ready["release_effects_authorized"] != false {
		t.Fatalf("unexpected ready receipt: %#v", ready)
	}
	run := &workflowRequest{Operation: "release-run", RequestID: "request-run", OIDCRequestURL: preflight.OIDCRequestURL, ActionsToken: []byte("jti-run"), DiagnosticRunID: "123", DiagnosticRunAttempt: 1, DiagnosticSHA: fixtureWorkflowSHA, DiagnosticWorkflowSHA: fixtureWorkflowSHA,
		SecurityBootstrapAttestationSHA: preflight.SecurityBootstrapAttestationSHA, SecurityBootstrapRunID: preflight.SecurityBootstrapRunID, SecurityBootstrapArtifactDigest: preflight.SecurityBootstrapArtifactDigest}
	bindFixtureRunnerEvidence(run)
	stableAuthentication := authenticateRequest
	authenticateRequest = func(ctx context.Context, config *Config, requestURL string, token []byte, now time.Time) (*Identity, error) {
		identity, err := stableAuthentication(ctx, config, requestURL, token, now)
		if identity != nil {
			identity.RunnerID = "790"
			identity.RunnerName = "pq-release-" + strings.Repeat("c", 32)
			identity.RunnerLabel = "pqrelease-" + strings.Repeat("c", 32)
		}
		return identity, err
	}
	runnerMismatched := *run
	runnerMismatched.ActionsToken = []byte("jti-run-runner-mismatch")
	if _, err := processWorkflowRequest(fixture.root, fixture.config, fixture.receiptKey, &runnerMismatched); err == nil {
		t.Fatal("release run allocated to a different runner accepted")
	}
	authenticateRequest = stableAuthentication
	mismatched := *run
	mismatched.ActionsToken = []byte("jti-run-bootstrap-mismatch")
	mismatched.SecurityBootstrapArtifactDigest = "sha256:" + strings.Repeat("c", 64)
	if _, err := processWorkflowRequest(fixture.root, fixture.config, fixture.receiptKey, &mismatched); err == nil {
		t.Fatal("release run with different bootstrap evidence accepted")
	}
	terminalWire, err := processWorkflowRequest(fixture.root, fixture.config, fixture.receiptKey, run)
	if err != nil {
		t.Fatal(err)
	}
	terminal := receiptPayload(t, terminalWire)
	if terminal["disposition"] != "succeeded" || terminal["production_ready"] != true || terminal["release_effects_performed"] != true || terminal["rollback_performed"] != false || terminal["database_receipts_verified"] != true || terminal["database_runtime_env_revalidated"] != true {
		t.Fatalf("unexpected terminal receipt: %#v", terminal)
	}
	retriedWire, err := processWorkflowRequest(fixture.root, fixture.config, fixture.receiptKey, run)
	if err != nil || !bytes.Equal(retriedWire, terminalWire) {
		t.Fatalf("exact retry did not return terminal receipt: %v", err)
	}
	rebound := *run
	rebound.ActionsToken = []byte("jti-run-rebound-bootstrap")
	rebound.SecurityBootstrapArtifactDigest = "sha256:" + strings.Repeat("d", 64)
	if _, err := processWorkflowRequest(fixture.root, fixture.config, fixture.receiptKey, &rebound); err == nil {
		t.Fatal("cached terminal receipt replayed for rebound bootstrap evidence")
	}
	authenticateRequest = func(ctx context.Context, config *Config, requestURL string, token []byte, now time.Time) (*Identity, error) {
		identity, err := stableAuthentication(ctx, config, requestURL, token, now)
		if identity != nil {
			identity.RunnerID = "790"
		}
		return identity, err
	}
	runnerRebound := *run
	runnerRebound.ActionsToken = []byte("jti-run-rebound-runner")
	if _, err := processWorkflowRequest(fixture.root, fixture.config, fixture.receiptKey, &runnerRebound); err == nil {
		t.Fatal("cached terminal receipt replayed for rebound runner identity")
	}
	authenticateRequest = stableAuthentication
	retryWithFreshToken := *run
	retryWithFreshToken.ActionsToken = []byte("jti-run-retry")
	retriedWire, err = processWorkflowRequest(fixture.root, fixture.config, fixture.receiptKey, &retryWithFreshToken)
	if err != nil || !bytes.Equal(retriedWire, terminalWire) {
		t.Fatalf("authenticated reconnect did not return terminal receipt: %v", err)
	}
	journal, err := OpenJournal(fixture.root, fixture.receiptKey)
	if err != nil {
		t.Fatal(err)
	}
	defer journal.Close()
	if journal.events[len(journal.events)-1].EventType != "run-succeeded" || len(journal.events) < 7 {
		t.Fatal("transaction was not fully journaled")
	}
}

func TestPriorSuccessNeverClaimsCurrentLiveReadiness(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	withFakeAuthentication(t, fixture)
	preflight := &workflowRequest{Operation: "release-preflight", RequestID: "request-preflight", OIDCRequestURL: "https://vstoken.actions.githubusercontent.com/x", ActionsToken: []byte("jti-preflight"), DiagnosticRunID: "123", DiagnosticRunAttempt: 1, DiagnosticSHA: fixtureWorkflowSHA, DiagnosticWorkflowSHA: fixtureWorkflowSHA}
	bindFixtureRunnerEvidence(preflight)
	if _, err := processWorkflowRequest(fixture.root, fixture.config, fixture.receiptKey, preflight); err != nil {
		t.Fatal(err)
	}
	run := &workflowRequest{Operation: "release-run", RequestID: "request-run", OIDCRequestURL: preflight.OIDCRequestURL, ActionsToken: []byte("jti-run"), DiagnosticRunID: "123", DiagnosticRunAttempt: 1, DiagnosticSHA: fixtureWorkflowSHA, DiagnosticWorkflowSHA: fixtureWorkflowSHA}
	bindFixtureRunnerEvidence(run)
	if _, err := processWorkflowRequest(fixture.root, fixture.config, fixture.receiptKey, run); err != nil {
		t.Fatal(err)
	}
	historical := *preflight
	historical.RequestID = "request-preflight-history"
	historical.ActionsToken = []byte("jti-preflight-history")
	wire, err := processWorkflowRequest(fixture.root, fixture.config, fixture.receiptKey, &historical)
	if err != nil {
		t.Fatal(err)
	}
	payload := receiptPayload(t, wire)
	if payload["production_ready"] != false || payload["disposition"] != "already-terminal-history-not-live-readiness" {
		t.Fatalf("historical success overstated live readiness: %#v", payload)
	}
}

func TestRuntimeEnvelopeTamperAtDeployBoundaryForcesRollback(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	withFakeAuthentication(t, fixture)
	preflight := &workflowRequest{Operation: "release-preflight", RequestID: "request-preflight", OIDCRequestURL: "https://vstoken.actions.githubusercontent.com/x", ActionsToken: []byte("jti-preflight"), DiagnosticRunID: "123", DiagnosticRunAttempt: 1, DiagnosticSHA: fixtureWorkflowSHA, DiagnosticWorkflowSHA: fixtureWorkflowSHA}
	bindFixtureRunnerEvidence(preflight)
	if _, err := processWorkflowRequest(fixture.root, fixture.config, fixture.receiptKey, preflight); err != nil {
		t.Fatal(err)
	}
	path := rooted(fixture.root, RegistrationEmailEnvPath)
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	raw[len(raw)-2] ^= 1
	writeFixture(t, path, raw, 0o600)
	run := &workflowRequest{Operation: "release-run", RequestID: "request-run", OIDCRequestURL: preflight.OIDCRequestURL, ActionsToken: []byte("jti-run"), DiagnosticRunID: "123", DiagnosticRunAttempt: 1, DiagnosticSHA: fixtureWorkflowSHA, DiagnosticWorkflowSHA: fixtureWorkflowSHA}
	bindFixtureRunnerEvidence(run)
	wire, err := processWorkflowRequest(fixture.root, fixture.config, fixture.receiptKey, run)
	if err != nil {
		t.Fatal(err)
	}
	payload := receiptPayload(t, wire)
	if payload["production_ready"] != false || payload["runtime_envelopes_revalidated"] != false || payload["disposition"] != "rollback-failed" || payload["full_rollback_verified"] != false {
		t.Fatalf("tampered envelope crossed deploy boundary: %#v", payload)
	}
}

func TestBackupFailureDoesNotClaimProductionMutationOrRunRollback(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	withFakeAuthentication(t, fixture)
	preflight := &workflowRequest{Operation: "release-preflight", RequestID: "request-preflight", OIDCRequestURL: "https://vstoken.actions.githubusercontent.com/x", ActionsToken: []byte("jti-preflight"), DiagnosticRunID: "123", DiagnosticRunAttempt: 1, DiagnosticSHA: fixtureWorkflowSHA, DiagnosticWorkflowSHA: fixtureWorkflowSHA}
	bindFixtureRunnerEvidence(preflight)
	if _, err := processWorkflowRequest(fixture.root, fixture.config, fixture.receiptKey, preflight); err != nil {
		t.Fatal(err)
	}
	previousExecute := executePlanStep
	executePlanStep = func(ctx context.Context, step Step, executables map[string]string, now func() time.Time) StepResult {
		if step.ID == "predeploy-encrypted-backup" {
			stamp := now().UTC().Unix()
			return StepResult{ID: step.ID, Effect: step.Effect, ExitCode: 1, StartedAt: stamp, FinishedAt: stamp, Succeeded: false}
		}
		return previousExecute(ctx, step, executables, now)
	}
	t.Cleanup(func() { executePlanStep = previousExecute })
	run := &workflowRequest{Operation: "release-run", RequestID: "request-run", OIDCRequestURL: preflight.OIDCRequestURL, ActionsToken: []byte("jti-run"), DiagnosticRunID: "123", DiagnosticRunAttempt: 1, DiagnosticSHA: fixtureWorkflowSHA, DiagnosticWorkflowSHA: fixtureWorkflowSHA}
	bindFixtureRunnerEvidence(run)
	wire, err := processWorkflowRequest(fixture.root, fixture.config, fixture.receiptKey, run)
	if err != nil {
		t.Fatal(err)
	}
	payload := receiptPayload(t, wire)
	if payload["production_ready"] != false || payload["release_effects_performed"] != false || payload["rollback_performed"] != false || payload["disposition"] != "failed-before-production-mutation" {
		t.Fatalf("backup failure overstated production effects: %#v", payload)
	}
}

func TestInvalidDatabaseReceiptBlocksAdvancementAndForcesRollback(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	withFakeAuthentication(t, fixture)
	preflight := &workflowRequest{Operation: "release-preflight", RequestID: "request-preflight", OIDCRequestURL: "https://vstoken.actions.githubusercontent.com/x", ActionsToken: []byte("jti-preflight"), DiagnosticRunID: "123", DiagnosticRunAttempt: 1, DiagnosticSHA: fixtureWorkflowSHA, DiagnosticWorkflowSHA: fixtureWorkflowSHA}
	bindFixtureRunnerEvidence(preflight)
	if _, err := processWorkflowRequest(fixture.root, fixture.config, fixture.receiptKey, preflight); err != nil {
		t.Fatal(err)
	}
	previousVerify := verifyDatabaseReceipt
	verifyDatabaseReceipt = func(root string, config *Config, key ed25519.PublicKey, operation string, stepStartedAt int64, stepFinishedAt int64) (*databaseReceiptProof, error) {
		if operation == "migrate-schema" {
			return nil, fmt.Errorf("fixture-database-receipt-invalid")
		}
		return previousVerify(root, config, key, operation, stepStartedAt, stepFinishedAt)
	}
	t.Cleanup(func() { verifyDatabaseReceipt = previousVerify })
	run := &workflowRequest{Operation: "release-run", RequestID: "request-run", OIDCRequestURL: preflight.OIDCRequestURL, ActionsToken: []byte("jti-run"), DiagnosticRunID: "123", DiagnosticRunAttempt: 1, DiagnosticSHA: fixtureWorkflowSHA, DiagnosticWorkflowSHA: fixtureWorkflowSHA}
	bindFixtureRunnerEvidence(run)
	wire, err := processWorkflowRequest(fixture.root, fixture.config, fixture.receiptKey, run)
	if err != nil {
		t.Fatal(err)
	}
	payload := receiptPayload(t, wire)
	if payload["disposition"] != "rollback-failed" || payload["production_ready"] != false || payload["database_receipts_verified"] != false || payload["database_provision_roles_verified"] != true || payload["database_migrate_schema_verified"] != false || payload["runtime_envelopes_revalidated"] != false {
		t.Fatalf("invalid database receipt crossed the transactional boundary: %#v", payload)
	}
}

func TestWorkflowRequestIDIsStableAcrossReconnect(t *testing.T) {
	first, err := requestIDForRun("release-run", "123456789", 2)
	if err != nil {
		t.Fatal(err)
	}
	second, err := requestIDForRun("release-run", "123456789", 2)
	if err != nil || first != second || first != "release-run-123456789-2" {
		t.Fatalf("request ID is not stable: %q %q %v", first, second, err)
	}
}

func TestFailedMutationRunsIdempotentRollbackAndNeverClaimsReady(t *testing.T) {
	fixture := newAuthorityFixture(t, true)
	defer fixture.close()
	withFakeAuthentication(t, fixture)
	previousExecute := executePlanStep
	executePlanStep = func(ctx context.Context, step Step, executables map[string]string, now func() time.Time) StepResult {
		if step.ID == DeployRuntimeStepID {
			stamp := now().UTC().Unix()
			return StepResult{ID: step.ID, Effect: step.Effect, ExitCode: 1, StartedAt: stamp, FinishedAt: stamp, Succeeded: false}
		}
		return previousExecute(ctx, step, executables, now)
	}
	t.Cleanup(func() { executePlanStep = previousExecute })
	preflight := &workflowRequest{Operation: "release-preflight", RequestID: "request-preflight", OIDCRequestURL: "https://vstoken.actions.githubusercontent.com/x", ActionsToken: []byte("jti-preflight"), DiagnosticRunID: "123", DiagnosticRunAttempt: 1, DiagnosticSHA: fixtureWorkflowSHA, DiagnosticWorkflowSHA: fixtureWorkflowSHA}
	bindFixtureRunnerEvidence(preflight)
	if _, err := processWorkflowRequest(fixture.root, fixture.config, fixture.receiptKey, preflight); err != nil {
		t.Fatal(err)
	}
	run := &workflowRequest{Operation: "release-run", RequestID: "request-run", OIDCRequestURL: preflight.OIDCRequestURL, ActionsToken: []byte("jti-run"), DiagnosticRunID: "123", DiagnosticRunAttempt: 1, DiagnosticSHA: fixtureWorkflowSHA, DiagnosticWorkflowSHA: fixtureWorkflowSHA}
	bindFixtureRunnerEvidence(run)
	wire, err := processWorkflowRequest(fixture.root, fixture.config, fixture.receiptKey, run)
	if err != nil {
		t.Fatal(err)
	}
	payload := receiptPayload(t, wire)
	if payload["disposition"] != "rollback-failed" || payload["production_ready"] != false || payload["rollback_performed"] != true || payload["full_rollback_verified"] != false {
		t.Fatalf("unsafe rollback receipt: %#v", payload)
	}
}

func TestSyntheticGitHubOIDCAndJobCorrelationBindExactInstalledProfile(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	now := time.Unix(1_800_000_000, 0).UTC()
	private, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	keyID := "github-test-key"
	claims := map[string]any{
		"iss": githubOIDCIssuer, "aud": Audience, "sub": "repo:ArchonMegalon@" + RepositoryOwnerID + "/propertyquarry@" + RepositoryID + ":environment:" + Environment,
		"repository": Repository, "repository_id": RepositoryID, "repository_owner_id": RepositoryOwnerID, "ref": "refs/heads/main",
		"sha": fixtureWorkflowSHA, "workflow_ref": WorkflowRef, "workflow_sha": fixtureWorkflowSHA, "run_id": "123", "run_attempt": "1",
		"environment": Environment, "check_run_id": "456", "jti": "oidc-jti", "iat": json.Number(strconv.FormatInt(now.Unix()-10, 10)),
		"nbf": json.Number(strconv.FormatInt(now.Unix()-10, 10)), "exp": json.Number(strconv.FormatInt(now.Unix()+300, 10)),
	}
	token := signedJWT(t, private, keyID, claims)
	jwks := rsaJWKS(t, keyID, &private.PublicKey)
	identity, err := verifyGitHubJWT(token, &oidcKeySet{raw: jwks, digest: digest(jwks)}, fixture.config, now)
	if err != nil {
		t.Fatal(err)
	}
	runRaw, _ := canonicalJSON(map[string]any{"id": json.Number("123"), "run_attempt": json.Number("1"), "event": "workflow_dispatch", "status": "in_progress", "conclusion": nil, "head_sha": fixtureWorkflowSHA, "head_branch": "main", "path": ".github/workflows/smoke-runtime.yml", "repository": map[string]any{"full_name": Repository, "id": json.Number(RepositoryID)}})
	jobValue := map[string]any{"id": json.Number("456"), "name": ReleaseJob, "status": "in_progress", "conclusion": nil, "run_url": "https://api.github.com/repos/" + Repository + "/actions/runs/123", "head_sha": fixtureWorkflowSHA, "runner_id": json.Number("789"), "runner_name": "pq-release-" + strings.Repeat("a", 32), "labels": []any{"propertyquarry-release-controller-v2", "pqrelease-" + strings.Repeat("a", 32)}}
	jobRaw, _ := canonicalJSON(jobValue)
	if err := validateRunCorrelation(runRaw, fixture.config, identity); err != nil {
		t.Fatal(err)
	}
	runnerID, name, err := validateJobCorrelation(jobRaw, fixture.config, identity)
	if err != nil || runnerID != "789" || name != "pq-release-"+strings.Repeat("a", 32) {
		t.Fatalf("job correlation failed: %v", err)
	}
	runnerValue := map[string]any{
		"id": json.Number("789"), "name": name, "os": "linux", "status": "online", "busy": true, "ephemeral": true, "version": pinnedRunnerVersion,
		"labels": []any{
			map[string]any{"id": json.Number("1"), "name": "propertyquarry-release-controller-v2", "type": "custom"},
			map[string]any{"id": json.Number("2"), "name": "pqrelease-" + strings.Repeat("a", 32), "type": "custom"},
		},
	}
	runnerRaw, _ := canonicalJSON(runnerValue)
	label, err := validateRunnerCorrelation(runnerRaw, fixture.config, runnerID, name)
	if err != nil || label != "pqrelease-"+strings.Repeat("a", 32) {
		t.Fatalf("runner correlation failed: label=%q err=%v", label, err)
	}
	runnerValue["busy"] = false
	runnerRaw, _ = canonicalJSON(runnerValue)
	if _, err := validateRunnerCorrelation(runnerRaw, fixture.config, runnerID, name); err == nil {
		t.Fatal("idle runner accepted for in-progress release job")
	}
	runnerValue["busy"] = true
	runnerValue["ephemeral"] = false
	runnerRaw, _ = canonicalJSON(runnerValue)
	if _, err := validateRunnerCorrelation(runnerRaw, fixture.config, runnerID, name); err == nil {
		t.Fatal("persistent runner accepted for release job")
	}
	runnerValue["ephemeral"] = true
	runnerValue["version"] = "2.336.0"
	runnerRaw, _ = canonicalJSON(runnerValue)
	if _, err := validateRunnerCorrelation(runnerRaw, fixture.config, runnerID, name); err == nil {
		t.Fatal("repository-unadvertised runner version accepted")
	}
	jobValue["labels"] = []any{"self-hosted", "propertyquarry-release-controller-v2", "pqrelease-" + strings.Repeat("a", 32)}
	jobRaw, _ = canonicalJSON(jobValue)
	if _, _, err := validateJobCorrelation(jobRaw, fixture.config, identity); err == nil {
		t.Fatal("unrequested one-time job label accepted")
	}
	criticalHeader := map[string]any{"alg": "RS256", "crit": []any{"x5t"}, "kid": keyID, "typ": "JWT", "x5t": "github-example-thumbprint"}
	if _, err := verifyGitHubJWT(signedJWTWithHeader(t, private, criticalHeader, claims), &oidcKeySet{raw: jwks, digest: digest(jwks)}, fixture.config, now); err == nil {
		t.Fatal("unsupported critical JOSE extension accepted")
	}
	claims["sha"] = fixtureRuntimeSHA
	if _, err := verifyGitHubJWT(signedJWT(t, private, keyID, claims), &oidcKeySet{raw: jwks, digest: digest(jwks)}, fixture.config, now); err == nil {
		t.Fatal("runtime SHA substituted for workflow candidate SHA")
	}
	claims["sha"] = fixtureWorkflowSHA
	claims["workflow_sha"] = fixtureRuntimeSHA
	if _, err := verifyGitHubJWT(signedJWT(t, private, keyID, claims), &oidcKeySet{raw: jwks, digest: digest(jwks)}, fixture.config, now); err == nil {
		t.Fatal("runtime SHA substituted for workflow definition SHA")
	}
}

func TestWorkflowRequestBindsSecurityBootstrapEvidence(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	value := map[string]any{
		"actions_request_token": base64.RawURLEncoding.EncodeToString([]byte("fixture-token")),
		"diagnostic_identity": map[string]any{
			"candidate_sha": fixtureWorkflowSHA, "environment": Environment, "job": ReleaseJob,
			"ref": "refs/heads/main", "repository": Repository, "run_attempt": json.Number("1"), "run_id": "123",
			"runner_label": "pqrelease-" + strings.Repeat("a", 32), "runner_ticket_sha256": "sha256:" + strings.Repeat("7", 64),
			"workflow_ref": WorkflowRef, "workflow_sha": fixtureWorkflowSHA,
		},
		"oidc_request_url": "https://vstoken.actions.githubusercontent.com/fixture", "operation": "release-preflight",
		"request_id": "request-bootstrap", "schema": requestSchema,
		"security_bootstrap_attestation": map[string]any{
			"artifact_digest": "sha256:" + strings.Repeat("b", 64), "attestation_sha256": strings.Repeat("a", 64), "run_id": "456",
		},
		"version": json.Number("2"),
	}
	raw, _ := canonicalJSON(value)
	request, err := parseWorkflowRequest(raw, fixture.config)
	if err != nil {
		t.Fatal(err)
	}
	defer request.release()
	if request.SecurityBootstrapAttestationSHA != strings.Repeat("a", 64) || request.SecurityBootstrapRunID != "456" || request.SecurityBootstrapArtifactDigest != "sha256:"+strings.Repeat("b", 64) ||
		request.DiagnosticRunnerLabel != "pqrelease-"+strings.Repeat("a", 32) || request.RunnerTicketDigest != "sha256:"+strings.Repeat("7", 64) {
		t.Fatalf("bootstrap evidence not bound: %#v", request)
	}
	value["security_bootstrap_attestation"].(map[string]any)["attestation_sha256"] = strings.Repeat("a", 40)
	raw, _ = canonicalJSON(value)
	if _, err := parseWorkflowRequest(raw, fixture.config); err == nil {
		t.Fatal("short bootstrap attestation digest accepted")
	}
}

func TestClientEmitsSignedReceiptsButOnlyReturnsSuccessForExactOperationOutcome(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	request := &workflowRequest{
		Operation: "release-preflight", RequestID: "release-preflight-123-1", DiagnosticRunID: "123", DiagnosticRunAttempt: 1,
		SecurityBootstrapAttestationSHA: strings.Repeat("a", 64), SecurityBootstrapRunID: "456",
		SecurityBootstrapArtifactDigest: "sha256:" + strings.Repeat("b", 64),
		DiagnosticRunnerLabel:           "pqrelease-" + strings.Repeat("c", 32),
		RunnerTicketDigest:              "sha256:" + strings.Repeat("7", 64),
		RunnerLaunchTicketDigest:        "sha256:" + strings.Repeat("8", 64),
		RunnerNonce:                     strings.Repeat("c", 32),
	}
	identity := &Identity{RunID: "123", RunAttempt: 1, CheckRunID: "789", TokenID: "client-outcome-jti",
		RunnerID: "987", RunnerName: "pq-release-" + strings.Repeat("c", 32), RunnerLabel: "pqrelease-" + strings.Repeat("c", 32)}
	expected := clientResponseExpectation{
		Operation: request.Operation, RequestID: request.RequestID, RunID: identity.RunID, RunAttempt: identity.RunAttempt,
		RuntimeSHA: fixture.config.RuntimeSHA, WorkflowSHA: fixture.config.WorkflowSHA, SecurityBootstrapAttestationSHA: request.SecurityBootstrapAttestationSHA,
		SecurityBootstrapRunID: request.SecurityBootstrapRunID, SecurityBootstrapArtifactDigest: request.SecurityBootstrapArtifactDigest,
		RunnerTicketDigest: request.RunnerTicketDigest, RunnerLabel: request.DiagnosticRunnerLabel,
	}
	base := authorityFields(fixture.config, request, identity)
	base["schema"] = journalSchema
	base["version"] = json.Number("2")
	base["event_type"] = "preflight-ready"
	base["disposition"] = "ready"
	base["ready"] = true
	base["production_ready"] = false
	base["release_effects_authorized"] = false
	base["release_effects_performed"] = false
	wire, err := signReceipt(base, fixture.receiptKey)
	if err != nil {
		t.Fatal(err)
	}
	var emitted bytes.Buffer
	if err := emitClientResponse(wire, fixture.receiptKey.Public().(ed25519.PublicKey), expected, &emitted); err != nil {
		t.Fatalf("exact ready preflight rejected: %v", err)
	}
	if !bytes.Equal(emitted.Bytes(), append(append([]byte(nil), wire...), '\n')) {
		t.Fatal("ready preflight receipt was not emitted exactly")
	}

	failureCases := []struct {
		event       string
		disposition string
	}{
		{"preflight-not-ready", "not-ready"},
		{"run-failed-no-effects", "failed-before-production-mutation"},
		{"run-rolled-back", "rolled-back"},
		{"run-rollback-failed", "rollback-failed"},
	}
	for _, testCase := range failureCases {
		payload := cloneFields(base)
		payload["event_type"] = testCase.event
		payload["disposition"] = testCase.disposition
		payload["ready"] = false
		failureWire, signErr := signReceipt(payload, fixture.receiptKey)
		if signErr != nil {
			t.Fatal(signErr)
		}
		emitted.Reset()
		if err := emitClientResponse(failureWire, fixture.receiptKey.Public().(ed25519.PublicKey), expected, &emitted); err == nil {
			t.Fatalf("signed failure receipt %s returned client success", testCase.event)
		}
		if !bytes.Equal(emitted.Bytes(), append(append([]byte(nil), failureWire...), '\n')) {
			t.Fatalf("signed failure receipt %s was not emitted", testCase.event)
		}
		zero(failureWire)
	}

	releaseRequest := *request
	releaseRequest.Operation = "release-run"
	releaseRequest.RequestID = "release-run-123-1"
	releaseExpected := expected
	releaseExpected.Operation = releaseRequest.Operation
	releaseExpected.RequestID = releaseRequest.RequestID
	releasePayload := authorityFields(fixture.config, &releaseRequest, identity)
	releasePayload["schema"] = journalSchema
	releasePayload["version"] = json.Number("2")
	releasePayload["event_type"] = "run-succeeded"
	releasePayload["disposition"] = "succeeded"
	releasePayload["ready"] = false
	releasePayload["production_ready"] = true
	releasePayload["release_effects_authorized"] = true
	releasePayload["release_effects_performed"] = true
	releasePayload["rollback_performed"] = false
	releaseWire, err := signReceipt(releasePayload, fixture.receiptKey)
	if err != nil {
		t.Fatal(err)
	}
	emitted.Reset()
	if err := emitClientResponse(releaseWire, fixture.receiptKey.Public().(ed25519.PublicKey), releaseExpected, &emitted); err != nil {
		t.Fatalf("exact successful release rejected: %v", err)
	}

	rebound := cloneFields(releasePayload)
	rebound["security_bootstrap_run_id"] = "457"
	reboundWire, _ := signReceipt(rebound, fixture.receiptKey)
	emitted.Reset()
	if err := emitClientResponse(reboundWire, fixture.receiptKey.Public().(ed25519.PublicKey), releaseExpected, &emitted); err == nil {
		t.Fatal("signed response rebound to different bootstrap evidence returned client success")
	}
	if emitted.Len() == 0 {
		t.Fatal("signed rebound response was not emitted for diagnosis")
	}
	tamperedWire := append([]byte(nil), releaseWire...)
	tamperedWire[len(tamperedWire)-1] ^= 1
	emitted.Reset()
	if err := emitClientResponse(tamperedWire, fixture.receiptKey.Public().(ed25519.PublicKey), releaseExpected, &emitted); err == nil || emitted.Len() != 0 {
		t.Fatal("unauthenticated response was emitted or accepted")
	}
	zero(wire)
	zero(releaseWire)
	zero(reboundWire)
	zero(tamperedWire)
}

func TestGitHubCorrelationFetchesAndBindsExactBusyRunnerInventory(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	identity := &Identity{RunID: "123", RunAttempt: 1, CheckRunID: "456"}
	responses := map[string]map[string]any{
		"/repos/ArchonMegalon/propertyquarry/actions/runs/123": {
			"id": json.Number("123"), "run_attempt": json.Number("1"), "event": "workflow_dispatch", "status": "in_progress", "conclusion": nil,
			"head_sha": fixtureWorkflowSHA, "head_branch": "main", "path": ".github/workflows/smoke-runtime.yml",
			"repository": map[string]any{"full_name": Repository, "id": json.Number(RepositoryID)},
		},
		"/repos/ArchonMegalon/propertyquarry/actions/jobs/456": {
			"id": json.Number("456"), "name": ReleaseJob, "status": "in_progress", "conclusion": nil,
			"run_url": "https://api.github.com/repos/" + Repository + "/actions/runs/123", "head_sha": fixtureWorkflowSHA,
			"runner_id": json.Number("789"), "runner_name": "pq-release-" + strings.Repeat("a", 32),
			"labels": []any{"propertyquarry-release-controller-v2", "pqrelease-" + strings.Repeat("a", 32)},
		},
		"/repos/ArchonMegalon/propertyquarry/actions/runners/789": {
			"id": json.Number("789"), "name": "pq-release-" + strings.Repeat("a", 32), "os": "linux", "status": "online", "busy": true,
			"ephemeral": true, "version": pinnedRunnerVersion,
			"labels": []any{
				map[string]any{"id": json.Number("1"), "name": "propertyquarry-release-controller-v2", "type": "custom"},
				map[string]any{"id": json.Number("2"), "name": "pqrelease-" + strings.Repeat("a", 32), "type": "custom"},
			},
		},
	}
	seen := []string{}
	client := httpDoerFunc(func(request *http.Request) (*http.Response, error) {
		if request.Method != http.MethodGet || request.URL.Scheme != "https" || request.URL.Host != "api.github.com" || request.Header.Get("Authorization") != "Bearer fixture-api-token" {
			t.Fatalf("unexpected GitHub request: %#v", request)
		}
		value, ok := responses[request.URL.Path]
		if !ok {
			t.Fatalf("unexpected GitHub endpoint: %s", request.URL.Path)
		}
		seen = append(seen, request.URL.Path)
		raw, _ := canonicalJSON(value)
		return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(bytes.NewReader(raw)), Request: request}, nil
	})
	if err := correlateGitHubJobWithToken(context.Background(), client, fixture.config, identity, "fixture-api-token"); err != nil {
		t.Fatal(err)
	}
	if strings.Join(seen, "|") != "/repos/ArchonMegalon/propertyquarry/actions/runs/123|/repos/ArchonMegalon/propertyquarry/actions/jobs/456|/repos/ArchonMegalon/propertyquarry/actions/runners/789" {
		t.Fatalf("unexpected endpoint order: %#v", seen)
	}
	if identity.RunnerID != "789" || identity.RunnerName != "pq-release-"+strings.Repeat("a", 32) || identity.RunnerLabel != "pqrelease-"+strings.Repeat("a", 32) {
		t.Fatalf("runner identity not bound: %#v", identity)
	}
}

func TestBuildInfoNeverClaimsInstalledAuthority(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if code := Run([]string{"--self-test"}, os.Stdin, &stdout, &stderr); code != 0 || stderr.Len() != 0 {
		t.Fatalf("self-test failed: %d %q", code, stderr.String())
	}
	value, err := decodedJSONObject(bytes.TrimSuffix(stdout.Bytes(), []byte{'\n'}), 4096)
	if err != nil {
		t.Fatal(err)
	}
	if value["authoritative"] != false || value["production_ready"] != false || value["binding_source"] != "independently-signed-installed-profile" {
		t.Fatalf("build overstated authority: %#v", value)
	}
}

func testAuthorityFields(config *Config, operation, requestID, jti string) map[string]any {
	request := &workflowRequest{Operation: operation, RequestID: requestID}
	identity := &Identity{RunID: "123", RunAttempt: 1, CheckRunID: "456", TokenID: jti, RunnerName: "runner", RunnerLabel: "pqrelease-" + strings.Repeat("a", 32)}
	fields := authorityFields(config, request, identity)
	fields["valid_until"] = json.Number("1800000300")
	fields["disposition"] = "ready"
	fields["ready"] = true
	fields["production_ready"] = false
	fields["release_effects_authorized"] = false
	fields["release_effects_performed"] = false
	return fields
}

func withFakeAuthentication(t *testing.T, fixture *authorityFixture) {
	t.Helper()
	config := fixture.config
	previous := authenticateRequest
	previousNow := authorityNow
	previousExecute := executePlanStep
	previousVerifyBackup := verifyBackupReceipt
	previousVerifyDatabase := verifyDatabaseReceipt
	previousVerifyIsolation := verifyIsolationReceipt
	previousVerifyDeploy := verifyDeployReceipt
	previousVerifyRunner := verifyRunnerRequest
	previousConsumeRunner := consumeRunnerRequest
	authorityNow = func() time.Time { return time.Unix(1_800_000_000, 0).UTC() }
	executePlanStep = func(ctx context.Context, step Step, executables map[string]string, now func() time.Time) StepResult {
		if step.ID == PurgeRuntimeIsolationStepID {
			writeFixture(t, rooted(fixture.root, BaseEnvironmentPath), []byte("POST_PURGE_STATE=true\n"), 0o600)
		}
		started := now().UTC().Unix()
		return StepResult{ID: step.ID, Effect: step.Effect, ExitCode: 0, StartedAt: started, FinishedAt: started, Succeeded: true}
	}
	backupDigest := "sha256:" + strings.Repeat("1", 64)
	purgeDigest := "sha256:" + strings.Repeat("2", 64)
	retirementDigest := "sha256:" + strings.Repeat("3", 64)
	deployDigest := "sha256:" + strings.Repeat("4", 64)
	databaseDigests := map[string]string{
		"provision-roles": digest([]byte("receipt-provision-roles")), "migrate-schema": digest([]byte("receipt-migrate-schema")),
		"harden-runtime-acl": digest([]byte("receipt-harden-runtime-acl")), "verify-schema-readiness": digest([]byte("receipt-verify-schema-readiness")),
	}
	verifyBackupReceipt = func(_ string, actual *Config, _ ed25519.PublicKey) (*backupReceiptProof, error) {
		if actual != config {
			return nil, os.ErrInvalid
		}
		return &backupReceiptProof{
			receiptDigest: backupDigest, remotePath: predeployBackupRemoteParent + "/" + config.RuntimeSHA + "/" + config.DeploymentID,
			manifestDigest: "sha256:" + strings.Repeat("2", 64), encryptionKeyID: "sha256:" + strings.Repeat("3", 64),
			databaseImageID: config.DatabaseSubstrate.imageID, databaseRepoDigest: canonicalRepoDigest(config.DatabaseImage), databaseSubstrate: config.DatabaseSubstrate,
			startedAt: 1_800_000_000, finishedAt: 1_800_000_000,
		}, nil
	}
	verifyDatabaseReceipt = func(root string, actual *Config, _ ed25519.PublicKey, operation string, startedAt int64, finishedAt int64) (*databaseReceiptProof, error) {
		if actual != config {
			return nil, os.ErrInvalid
		}
		envRaw, err := os.ReadFile(rooted(root, DatabaseRuntimeEnvironmentPath))
		if err != nil {
			return nil, err
		}
		if _, ok := databaseOperationForStep(map[string]string{
			"provision-roles": ProvisionDatabaseRolesStepID, "migrate-schema": MigrateSchemaStepID,
			"harden-runtime-acl": HardenRuntimeACLStepID, "verify-schema-readiness": VerifySchemaReadinessStepID,
		}[operation]); !ok {
			return nil, os.ErrInvalid
		}
		var schemaVersions map[string]int64
		if operation != "provision-roles" {
			schemaVersions = map[string]int64{"kernel": 1, "property_search": 1, "google_identity": 1}
		}
		predecessor := retirementDigest
		if operation == "migrate-schema" {
			predecessor = databaseDigests["provision-roles"]
		} else if operation == "harden-runtime-acl" {
			predecessor = databaseDigests["migrate-schema"]
		} else if operation == "verify-schema-readiness" {
			predecessor = databaseDigests["harden-runtime-acl"]
		}
		return &databaseReceiptProof{
			operation: operation, receiptDigest: databaseDigests[operation], envFileDigest: digest(envRaw),
			databaseImageID: config.DatabaseSubstrate.imageID, databaseRepoDigest: canonicalRepoDigest(config.DatabaseImage), databaseSubstrate: config.DatabaseSubstrate,
			backupReceiptDigest: backupDigest, purgeReceiptDigest: purgeDigest, retirementReceiptDigest: retirementDigest, predecessorReceiptDigest: predecessor,
			databaseOID: 83, schemaStatus: map[string]string{"provision-roles": "provisioned", "migrate-schema": "migrated", "harden-runtime-acl": "ready", "verify-schema-readiness": "ready"}[operation],
			schemaVersions: schemaVersions, receiptKeyID: config.ReceiptAuthorityKeyID, startedAt: startedAt, finishedAt: finishedAt,
		}, nil
	}
	verifyIsolationReceipt = func(_ string, actual *Config, _ ed25519.PublicKey, operation string, startedAt, finishedAt int64) (*runtimeIsolationProof, error) {
		if actual != config {
			return nil, os.ErrInvalid
		}
		proof := &runtimeIsolationProof{operation: operation, startedAt: startedAt, finishedAt: finishedAt, backupReceiptDigest: backupDigest}
		switch operation {
		case operationPurgeRuntimeIsolation:
			proof.receiptDigest = purgeDigest
			proof.prePurgeRootEnvDigest = config.PrePurgeRootEnvDigest
			proof.postPurgeRootEnvDigest = config.PostPurgeRootEnvDigest
		case operationRetireStaleRuntime:
			proof.receiptDigest = retirementDigest
			proof.purgeReceiptDigest = purgeDigest
		case operationVerifyRuntimeIsolation:
			proof.receiptDigest = "sha256:" + strings.Repeat("5", 64)
			proof.deployReceiptDigest = deployDigest
			proof.postPurgeRootEnvDigest = config.PostPurgeRootEnvDigest
			proof.databaseImageID = config.DatabaseSubstrate.imageID
			proof.databaseRepoDigest = config.DatabaseSubstrate.repoDigest
			proof.databaseSubstrateDigest = config.DatabaseSubstrateDigest
			envRaw, err := os.ReadFile(rooted(fixture.root, DatabaseRuntimeEnvironmentPath))
			if err != nil {
				return nil, err
			}
			proof.databaseEnvDigest = digest(envRaw)
			proof.databaseReceipts = make(map[string]isolationDatabaseReceiptSummary, len(databaseDigests))
			for operation, receiptDigest := range databaseDigests {
				proof.databaseReceipts[operation] = isolationDatabaseReceiptSummary{operation: operation, receiptDigest: receiptDigest, envFileDigest: proof.databaseEnvDigest, databaseImageID: proof.databaseImageID, databaseRepoDigest: proof.databaseRepoDigest, databaseOID: config.DatabaseSubstrate.databaseOID, startedAt: startedAt, finishedAt: finishedAt}
			}
		case operationRestoreRuntimeIsolation:
			proof.receiptDigest = "sha256:" + strings.Repeat("6", 64)
			proof.prePurgeRootEnvDigest = config.PrePurgeRootEnvDigest
			proof.postPurgeRootEnvDigest = config.PostPurgeRootEnvDigest
		default:
			return nil, os.ErrInvalid
		}
		return proof, nil
	}
	verifyDeployReceipt = func(_ string, actual *Config, _ ed25519.PublicKey, startedAt, finishedAt int64) (*runtimeDeployProof, error) {
		if actual != config {
			return nil, os.ErrInvalid
		}
		return &runtimeDeployProof{
			receiptDigest: deployDigest, backupReceiptDigest: backupDigest, purgeReceiptDigest: purgeDigest,
			retirementReceiptDigest: retirementDigest, databaseReceiptDigests: databaseDigests,
			databaseSubstrateDigest: config.DatabaseSubstrateDigest, startedAt: startedAt, finishedAt: finishedAt,
		}, nil
	}
	authenticateRequest = func(_ context.Context, actual *Config, _ string, token []byte, _ time.Time) (*Identity, error) {
		if actual != config {
			return nil, os.ErrInvalid
		}
		return &Identity{Subject: "fixture", Repository: Repository, RepositoryID: RepositoryID, RepositoryOwnerID: RepositoryOwnerID, Ref: "refs/heads/main",
			CandidateSHA: config.WorkflowSHA, WorkflowRef: WorkflowRef, WorkflowSHA: config.WorkflowSHA, RunID: "123", RunAttempt: 1,
			Environment: Environment, CheckRunID: "456", TokenID: string(token), IssuedAt: 1_799_999_990, NotBefore: 1_799_999_990,
			ExpiresAt: 1_800_000_300, KeyID: "fixture", TokenDigest: digest(token), JWKSdigest: "sha256:" + strings.Repeat("e", 64),
			RunnerID: "789", RunnerName: "pq-release-" + strings.Repeat("a", 32), RunnerLabel: "pqrelease-" + strings.Repeat("a", 32)}, nil
	}
	verifyRunnerRequest = func(_ string, actual *Config, request *workflowRequest, identity *Identity, _ time.Time) (*runnerTicketBinding, error) {
		if actual != config || request == nil || identity == nil || request.RunnerTicketDigest != "sha256:"+strings.Repeat("7", 64) ||
			request.DiagnosticRunnerLabel != "pqrelease-"+strings.Repeat("a", 32) || identity.RunnerID != "789" ||
			identity.RunnerName != "pq-release-"+strings.Repeat("a", 32) || identity.RunnerLabel != request.DiagnosticRunnerLabel {
			return nil, fmt.Errorf("fixture-runner-ticket-binding-invalid")
		}
		return &runnerTicketBinding{
			DispatchTicketDigest: request.RunnerTicketDigest, LaunchTicketDigest: "sha256:" + strings.Repeat("8", 64),
			RunnerLabel: identity.RunnerLabel, RunnerNonce: strings.Repeat("a", 32), RunID: identity.RunID,
			RunAttempt: identity.RunAttempt, JobID: identity.CheckRunID, RunnerID: identity.RunnerID, RunnerName: identity.RunnerName,
		}, nil
	}
	consumeRunnerRequest = func(_ string, binding *runnerTicketBinding) error {
		if binding == nil || binding.DispatchTicketDigest != "sha256:"+strings.Repeat("7", 64) || binding.LaunchTicketDigest != "sha256:"+strings.Repeat("8", 64) ||
			binding.RunnerID != "789" || binding.RunnerName != "pq-release-"+strings.Repeat("a", 32) {
			return fmt.Errorf("fixture-runner-ticket-consume-invalid")
		}
		return nil
	}
	t.Cleanup(func() {
		authenticateRequest = previous
		authorityNow = previousNow
		executePlanStep = previousExecute
		verifyBackupReceipt = previousVerifyBackup
		verifyDatabaseReceipt = previousVerifyDatabase
		verifyIsolationReceipt = previousVerifyIsolation
		verifyDeployReceipt = previousVerifyDeploy
		verifyRunnerRequest = previousVerifyRunner
		consumeRunnerRequest = previousConsumeRunner
	})
}

func receiptPayload(t *testing.T, wire []byte) map[string]any {
	t.Helper()
	wrapper, err := strictJSON(wire, maximumJournalBytes)
	if err != nil {
		t.Fatal(err)
	}
	payload, ok := wrapper["payload"].(map[string]any)
	if !ok {
		t.Fatal("receipt payload missing")
	}
	return payload
}

func signedJWT(t *testing.T, key *rsa.PrivateKey, keyID string, claims map[string]any) []byte {
	t.Helper()
	return signedJWTWithHeader(t, key, map[string]any{"alg": "RS256", "kid": keyID, "typ": "JWT", "x5t": "github-example-thumbprint"}, claims)
}

func signedJWTWithHeader(t *testing.T, key *rsa.PrivateKey, protected map[string]any, claims map[string]any) []byte {
	t.Helper()
	header, err := canonicalJSON(protected)
	if err != nil {
		t.Fatal(err)
	}
	payload, _ := canonicalJSON(claims)
	encodedHeader := base64.RawURLEncoding.EncodeToString(header)
	encodedPayload := base64.RawURLEncoding.EncodeToString(payload)
	input := []byte(encodedHeader + "." + encodedPayload)
	hashed := sha256.Sum256(input)
	signature, err := rsa.SignPKCS1v15(rand.Reader, key, crypto.SHA256, hashed[:])
	if err != nil {
		t.Fatal(err)
	}
	return []byte(string(input) + "." + base64.RawURLEncoding.EncodeToString(signature))
}

func rsaJWKS(t *testing.T, keyID string, key *rsa.PublicKey) []byte {
	t.Helper()
	exponent := big.NewInt(int64(key.E)).Bytes()
	raw, err := canonicalJSON(map[string]any{"keys": []any{map[string]any{"alg": "RS256", "e": base64.RawURLEncoding.EncodeToString(exponent), "kid": keyID, "kty": "RSA", "n": base64.RawURLEncoding.EncodeToString(key.N.Bytes()), "use": "sig"}}})
	if err != nil {
		t.Fatal(err)
	}
	return raw
}
