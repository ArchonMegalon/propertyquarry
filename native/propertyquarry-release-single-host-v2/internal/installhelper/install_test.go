package installhelper

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"propertyquarry.local/release-single-host-v2/internal/authority"
)

type installFixture struct {
	root                 string
	packageKey           ed25519.PrivateKey
	receiptKey           ed25519.PrivateKey
	envelope             []byte
	registrationEnvelope []byte
	sceneEnvelope        []byte
	machineID            string
}

func (fixture *installFixture) activationAttempt(t *testing.T) (*activationAttempt, error) {
	t.Helper()
	challenge := make([]byte, 32)
	if _, err := rand.Read(challenge); err != nil {
		return nil, err
	}
	challengeDigest := digest(challenge)
	zero(challenge)
	now := time.Now().UTC().Unix()
	expected, public, err := installedActivationExpected(fixture.root, challengeDigest)
	if err != nil {
		t.Logf("fixture activation binding failed: %v", err)
		return nil, err
	}
	defer zero(public)
	expected.ChallengeCreatedAt = now
	expected.CanaryStartedAt = now
	payload := map[string]any{
		"authority_profile": "single-host-production-v2", "challenge_sha256": challengeDigest,
		"config_digest": expected.ConfigDigest, "controller_sha256": expected.ControllerDigest,
		"github_immutable_oidc_subject_verified": true, "github_repository_runner_admin_read_verified": true,
		"immutable_subject":        "repo:ArchonMegalon@11421547/propertyquarry@1257593732:environment:propertyquarry-production",
		"package_authority_key_id": expected.PackageAuthorityKeyID, "package_manifest_digest": expected.PackageManifestDigest,
		"plan_digest": expected.PlanDigest, "receipt_authority_key_id": expected.ReceiptAuthorityKeyID,
		"repository": "ArchonMegalon/propertyquarry", "repository_id": "1257593732", "repository_owner_id": "11421547",
		"runtime_sha": expected.RuntimeSHA, "schema": "propertyquarry.release-control.single-host-activation-canary-receipt.v2",
		"unit_sha256": expected.UnitDigest, "valid_until": json.Number(strconv.FormatInt(now+120, 10)),
		"verified_at": json.Number(strconv.FormatInt(now, 10)), "version": json.Number("2"), "workflow_sha": expected.WorkflowSHA,
	}
	payloadRaw, err := canonicalJSON(payload)
	if err != nil {
		return nil, err
	}
	signature := ed25519.Sign(fixture.receiptKey, framed("propertyquarry.release-control.single-host-activation-canary-receipt-signature.v2\x00", payloadRaw))
	zero(payloadRaw)
	wire, err := canonicalJSON(map[string]any{"payload": payload, "signature": base64.RawURLEncoding.EncodeToString(signature), "signature_key_id": expected.ReceiptAuthorityKeyID})
	zero(signature)
	if err != nil {
		return nil, err
	}
	proof, err := authority.VerifyActivationCanaryReceipt(wire, public, expected, time.Now().UTC())
	if err != nil {
		t.Logf("fixture activation proof failed: %v", err)
		zero(wire)
		return nil, err
	}
	return &activationAttempt{receipt: wire, challengeDigest: challengeDigest, challengeCreatedAt: now, canaryStartedAt: now, installedStateProof: proof}, nil
}

func (fixture *installFixture) activate(t *testing.T) func() (*activationAttempt, error) {
	t.Helper()
	return func() (*activationAttempt, error) { return fixture.activationAttempt(t) }
}

func TestInstallerDrainTimeoutExceedsServerAndClientLifecycle(t *testing.T) {
	if authorityDrainTimeout != 315*time.Minute {
		t.Fatalf("authority drain timeout = %s, want 315m", authorityDrainTimeout)
	}
}

func TestActivationProofMismatchAndFailureAbortActiveSocket(t *testing.T) {
	fixture := newInstallFixture(t)
	verified := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer verified.Release()
	installer := &Installer{HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid()), Activate: fixture.activate(t), Deactivate: func() error { return nil }}
	if _, err := installer.Install(verified); err != nil {
		t.Fatal(err)
	}
	active, aborts := true, 0
	installer.AbortActivation = func() error { aborts++; active = false; return nil }
	installer.Activate = func() (*activationAttempt, error) {
		attempt, err := fixture.activationAttempt(t)
		if attempt != nil {
			attempt.challengeDigest = "sha256:" + strings.Repeat("0", 64)
		}
		return attempt, err
	}
	if _, err := installer.activateCandidate(verified); err == nil || active || aborts != 1 {
		t.Fatalf("wrong-challenge proof did not fail closed: active=%t aborts=%d err=%v", active, aborts, err)
	}
	active = true
	installer.Activate = func() (*activationAttempt, error) { return nil, errors.New("synthetic canary failure") }
	receipt, err := installer.Install(verified)
	if err != nil {
		t.Fatal(err)
	}
	payload := signedInstallReceiptPayload(t, receipt, fixture.receiptKey)
	if payload["disposition"] != "activation-failed" || payload["activation_canary_verified"] != false || active || aborts != 2 {
		t.Fatalf("idempotent canary failure did not abort active socket: active=%t aborts=%d payload=%#v", active, aborts, payload)
	}
	active = true
	installer.AbortActivation = func() error { aborts++; return errors.New("synthetic abort failure") }
	if _, err := installer.activateCandidate(verified); err == nil || err.Error() != "activation-failed-and-deactivation-failed" || !active {
		t.Fatalf("abort failure was not surfaced distinctly: active=%t aborts=%d err=%v", active, aborts, err)
	}
}

func TestReactivationRejectsForgedInstalledProofAndAbortsActiveSocket(t *testing.T) {
	fixture := newInstallFixture(t)
	verified := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer verified.Release()
	installer := &Installer{
		HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid()),
		Activate: fixture.activate(t), Deactivate: func() error { return nil },
	}
	if _, err := installer.Install(verified); err != nil {
		t.Fatal(err)
	}
	active, aborts := true, 0
	installer.AbortActivation = func() error {
		aborts++
		active = false
		return nil
	}
	installer.Activate = func() (*activationAttempt, error) {
		attempt, err := fixture.activationAttempt(t)
		if attempt != nil && attempt.installedStateProof != nil {
			attempt.installedStateProof.ReceiptDigest = "sha256:" + strings.Repeat("0", 64)
		}
		return attempt, err
	}
	if err := installer.reactivateCurrent(); err == nil || err.Error() != "reactivation-proof-invalid" || active || aborts != 1 {
		t.Fatalf("forged reactivation proof did not fail closed: active=%t aborts=%d err=%v", active, aborts, err)
	}
}

func newInstallFixture(t *testing.T) *installFixture {
	t.Helper()
	root := t.TempDir()
	for _, relative := range []string{"etc", "etc/propertyquarry-release-control-v2", "etc/propertyquarry-release-single-host-v2", "usr", "usr/lib", "usr/libexec", "usr/lib/systemd", "usr/lib/systemd/system", "usr/lib/sysusers.d", "usr/lib/tmpfiles.d", "var", "var/lib", "home", "home/tibor", "home/tibor/.local", "home/tibor/.local/share", "docker", "docker/property", "docker/property/state", "docker/property/state/runtime"} {
		path := filepath.Join(root, relative)
		if err := os.Mkdir(path, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(path, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Chmod(filepath.Join(root, "etc/propertyquarry-release-single-host-v2"), 0o700); err != nil {
		t.Fatal(err)
	}
	writeTestFile(t, filepath.Join(root, "etc/passwd"), []byte("root:x:0:0:root:/root:/bin/sh\n"), 0o644)
	writeTestFile(t, filepath.Join(root, "etc/group"), []byte("root:x:0:\n"), 0o644)
	writeTestFile(t, filepath.Join(root, "etc/propertyquarry-release-single-host-v2/github-api-token.cred"), []byte("fixture-github-api-credential-00000001\n"), 0o400)
	machineID := "0123456789abcdef0123456789abcdef"
	writeTestFile(t, filepath.Join(root, "etc/machine-id"), []byte(machineID+"\n"), 0o444)
	envelope := []byte("PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID=client\nPROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET=secret\nPROPERTYQUARRY_GOOGLE_OAUTH_REDIRECT_URI=https://propertyquarry.com/app/auth/google/callback\nPROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET=state\nPROPERTYQUARRY_IDENTITY_SESSION_SECRET=session\n")
	writeTestFile(t, filepath.Join(root, "docker/property/state/runtime/propertyquarry_google_identity.env"), envelope, 0o600)
	registrationEnvelope := []byte("EMAILIT_API_KEY=key\nEA_REGISTRATION_EMAIL_FROM=register@propertyquarry.com\nEA_REGISTRATION_EMAIL_NAME=PropertyQuarry\nEA_REGISTRATION_EMAIL_FROM_FALLBACK=fallback@propertyquarry.com\nEA_REGISTRATION_EMAIL_NAME_FALLBACK=PropertyQuarry fallback\nEA_REGISTRATION_EMAIL_FORCE_FALLBACK=false\nEA_EMAIL_DEFAULT_FROM=hello@propertyquarry.com\nEA_EMAIL_DEFAULT_NAME=PropertyQuarry\n")
	writeTestFile(t, filepath.Join(root, "docker/property/state/runtime/propertyquarry_registration_email.env"), registrationEnvelope, 0o600)
	sceneEnvelope := []byte("PROPERTYQUARRY_RENDER_BRIDGE_TOKEN=fixture-render-bridge-token\n")
	writeTestFile(t, filepath.Join(root, "docker/property/state/runtime/property_scene_video_shared.env"), sceneEnvelope, 0o600)
	_, packageKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	_, receiptKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	encoded, _, err := EncodePublicKeyDER(packageKey.Public().(ed25519.PublicKey))
	if err != nil {
		t.Fatal(err)
	}
	packageAnchor, _ := testPublicPEM(t, packageKey.Public().(ed25519.PublicKey))
	writeTestFile(t, filepath.Join(root, "etc/propertyquarry-release-control-v2/package-authority-v2.pem"), packageAnchor, 0o444)
	previous := EmbeddedPackageAuthorityDERBase64
	EmbeddedPackageAuthorityDERBase64 = encoded
	t.Cleanup(func() {
		EmbeddedPackageAuthorityDERBase64 = previous
		zero(packageKey)
		zero(receiptKey)
	})
	return &installFixture{root: root, packageKey: packageKey, receiptKey: receiptKey, envelope: envelope, registrationEnvelope: registrationEnvelope, sceneEnvelope: sceneEnvelope, machineID: machineID}
}

func primeTestPreAdmission(installer *Installer) {
	secret := bytes.Repeat([]byte{0x5a}, 32)
	installer.preAdmissionRequired = true
	installer.preAdmission = &preAdmissionState{secret: secret, keyID: digest(secret)}
}

func (fixture *installFixture) verifiedPackage(t *testing.T, generation int64, runtimeSHA, predecessor string) *VerifiedPackage {
	t.Helper()
	workflowSHA := strings.Repeat("f", 40)
	prePurgeRootEnvDigest := digest([]byte("fixture-pre-purge-root-environment"))
	packageAnchor, packageKeyID := testPublicPEM(t, fixture.packageKey.Public().(ed25519.PublicKey))
	receiptAnchor, receiptKeyID := testPublicPEM(t, fixture.receiptKey.Public().(ed25519.PublicKey))
	receiptPrivate, err := EncodePrivateKeyPEM(fixture.receiptKey)
	if err != nil {
		t.Fatal(err)
	}
	plan := map[string]any{
		"authority_profile": "single-host-production-v2", "envelope_sha": strings.Repeat("e", 64),
		"api_host_ip": apiHostIP, "api_host_port": json.Number("8097"), "api_container_port": json.Number("8090"),
		"database_image":             databaseImage,
		"pre_purge_root_env_digest":  prePurgeRootEnvDigest,
		"github_identity_env_digest": digest(fixture.envelope), "github_identity_env_gid": json.Number(strconv.Itoa(os.Getegid())),
		"github_identity_env_mode": "0600", "github_identity_env_path": "/docker/property/state/runtime/propertyquarry_google_identity.env",
		"github_identity_env_uid": json.Number(strconv.Itoa(os.Geteuid())), "host_machine_id_digest": digest([]byte(fixture.machineID)),
		"registration_email_env_digest": digest(fixture.registrationEnvelope), "registration_email_env_gid": json.Number(strconv.Itoa(os.Getegid())),
		"registration_email_env_mode": "0600", "registration_email_env_path": "/docker/property/state/runtime/propertyquarry_registration_email.env",
		"registration_email_env_uid": json.Number(strconv.Itoa(os.Geteuid())),
		"scene_video_env_digest":     digest(fixture.sceneEnvelope), "scene_video_env_gid": json.Number("1000"),
		"scene_video_env_mode": json.Number("384"), "scene_video_env_path": "/docker/property/state/runtime/property_scene_video_shared.env",
		"scene_video_env_uid": json.Number("1000"), "cloudflared_image": "cloudflare/cloudflared@sha256:" + strings.Repeat("d", 64),
		"release_generation": json.Number(strconv.FormatInt(generation, 10)), "runtime_sha": runtimeSHA, "workflow_sha": workflowSHA,
		"runner_reservation_sha256": "sha256:" + strings.Repeat("6", 64), "runner_label": "pqrelease-" + strings.Repeat("a", 32),
		"runner_run_id": "123", "runner_run_attempt": json.Number("1"), "runner_job_id": "456",
		"schema": "propertyquarry.release-control.single-host-transaction-plan.v2", "version": json.Number("2"),
	}
	planRaw, err := canonicalJSON(plan)
	if err != nil {
		t.Fatal(err)
	}
	config := map[string]any{
		"authority_profile": "single-host-production-v2", "envelope_sha": strings.Repeat("e", 64),
		"api_host_ip": apiHostIP, "api_host_port": json.Number("8097"), "api_container_port": json.Number("8090"),
		"database_image":             databaseImage,
		"pre_purge_root_env_digest":  prePurgeRootEnvDigest,
		"github_identity_env_digest": digest(fixture.envelope), "github_identity_env_gid": json.Number(strconv.Itoa(os.Getegid())),
		"github_identity_env_mode": "0600", "github_identity_env_path": "/docker/property/state/runtime/propertyquarry_google_identity.env",
		"github_identity_env_uid": json.Number(strconv.Itoa(os.Geteuid())), "host_machine_id_digest": digest([]byte(fixture.machineID)),
		"package_authority_key_id": packageKeyID, "plan_digest": digest(planRaw), "predecessor_runtime_sha": predecessor,
		"registration_email_env_digest": digest(fixture.registrationEnvelope), "registration_email_env_gid": json.Number(strconv.Itoa(os.Getegid())),
		"registration_email_env_mode": "0600", "registration_email_env_path": "/docker/property/state/runtime/propertyquarry_registration_email.env",
		"registration_email_env_uid": json.Number(strconv.Itoa(os.Geteuid())),
		"scene_video_env_digest":     digest(fixture.sceneEnvelope), "scene_video_env_gid": json.Number("1000"),
		"scene_video_env_mode": json.Number("384"), "scene_video_env_path": "/docker/property/state/runtime/property_scene_video_shared.env",
		"scene_video_env_uid": json.Number("1000"), "cloudflared_image": "cloudflare/cloudflared@sha256:" + strings.Repeat("d", 64),
		"receipt_authority_key_id": receiptKeyID, "release_generation": json.Number(strconv.FormatInt(generation, 10)),
		"allowed_runner_uid": json.Number("1999"), "allowed_runner_gid": json.Number("1999"),
		"runner_reservation_sha256": plan["runner_reservation_sha256"], "runner_label": plan["runner_label"],
		"runner_run_id": plan["runner_run_id"], "runner_run_attempt": plan["runner_run_attempt"], "runner_job_id": plan["runner_job_id"],
		"runtime_sha": runtimeSHA, "workflow_sha": workflowSHA, "schema": "propertyquarry.release-control.single-host-profile.v2", "version": json.Number("2"),
	}
	configRaw, err := canonicalJSON(config)
	if err != nil {
		t.Fatal(err)
	}
	configSignature := ed25519.Sign(fixture.packageKey, framed(configSignatureDomain, configRaw))
	buildReceipt, _ := canonicalJSON(map[string]any{"authoritative": false, "schema": "fixture", "version": json.Number("2")})
	files := map[string]*FileRecord{}
	add := func(path string, mode os.FileMode, raw []byte) {
		files[path] = &FileRecord{InstallPath: path, PackagePath: "payload" + path, Purpose: "fixture", Mode: mode, Size: int64(len(raw)), Digest: digest(raw), Data: append([]byte(nil), raw...)}
	}
	add("/usr/libexec/propertyquarry-release-control/propertyquarry-release-single-host-v2", 0o755, []byte("fixture-controller"))
	add("/etc/propertyquarry-release-single-host-v2/authority.v2.json", 0o400, configRaw)
	add("/etc/propertyquarry-release-single-host-v2/authority.v2.sig", 0o444, configSignature)
	add("/etc/propertyquarry-release-single-host-v2/native-build-receipt.v2.json", 0o444, buildReceipt)
	add("/etc/propertyquarry-release-single-host-v2/transaction-plan.v2.json", 0o444, planRaw)
	add("/etc/propertyquarry-release-single-host-v2/package-authority-v2.pem", 0o444, packageAnchor)
	add("/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.key", 0o400, receiptPrivate)
	add("/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.pem", 0o444, receiptAnchor)
	add("/usr/lib/systemd/system/propertyquarry-release-single-host-v2.socket", 0o444, []byte("[Socket]\n"))
	add("/usr/lib/systemd/system/propertyquarry-release-single-host-v2@.service", 0o444, []byte("[Service]\n"))
	add("/usr/lib/systemd/system/propertyquarry-release-single-host-v2-activation-canary.service", 0o444, []byte("[Service]\nType=oneshot\n"))
	add("/usr/lib/sysusers.d/propertyquarry-release-single-host-v2.conf", 0o444, []byte("g propertyquarry-runner-v2\n"))
	add("/usr/lib/tmpfiles.d/propertyquarry-release-single-host-v2.conf", 0o444, []byte("d /run/propertyquarry-release-single-host-v2 0750 root root -\n"))
	add("/usr/lib/propertyquarry-release-runner-v2/runner.lock.json", 0o444, []byte("{}"))
	add("/usr/libexec/propertyquarry-release-control/run-propertyquarry-ephemeral-runner-v2", 0o555, []byte("#!/bin/false\n"))
	add("/var/lib/propertyquarry-release-single-host-v2/runner-launch-ticket.v2.json", 0o400, []byte("{\"fixture\":\"runner-launch-ticket\"}"))
	add("/var/lib/propertyquarry-release-single-host-v2/runner-reservation.v2.json", 0o400, []byte("{\"fixture\":\"runner-reservation\"}"))
	fileItems := make([]any, 0, len(files))
	for _, path := range SortedInstallPaths(files) {
		file := files[path]
		fileItems = append(fileItems, map[string]any{
			"install_path": path, "mode": "0" + strconv.FormatUint(uint64(file.Mode.Perm()), 8),
			"package_path": file.PackagePath, "purpose": file.Purpose, "sha256": file.Digest,
			"size": json.Number(strconv.FormatInt(file.Size, 10)),
		})
	}
	manifest := map[string]any{"api_host_ip": apiHostIP, "api_host_port": json.Number("8097"), "api_container_port": json.Number("8090"), "database_image": databaseImage, "pre_purge_root_env_digest": prePurgeRootEnvDigest, "config_digest": digest(configRaw), "files": fileItems, "receipt_authority_key_id": receiptKeyID, "release_generation": json.Number(strconv.FormatInt(generation, 10)), "runtime_sha": runtimeSHA, "workflow_sha": workflowSHA}
	manifestRaw, err := canonicalJSON(manifest)
	if err != nil {
		t.Fatal(err)
	}
	manifestSignature := ed25519.Sign(fixture.packageKey, framed(packageSignatureDomain, manifestRaw))
	started := time.Now().UTC().Unix() - 1
	return &VerifiedPackage{ManifestRaw: manifestRaw, ManifestSignature: manifestSignature, ArchiveDigest: digest([]byte("archive-" + runtimeSHA)), PackageAuthorityKeyID: packageKeyID, ReceiptAuthorityKeyID: receiptKeyID, ConfigDigest: digest(configRaw), PlanDigest: digest(planRaw), BuildReceiptDigest: digest(buildReceipt), RuntimeSHA: runtimeSHA, WorkflowSHA: workflowSHA, EnvelopeSHA: strings.Repeat("e", 64), HostMachineIDDigest: digest([]byte(fixture.machineID)), DatabaseImage: databaseImage, APIHostIP: apiHostIP, APIHostPort: apiHostPort, APIContainerPort: apiContainerPort, PrePurgeRootEnvDigest: prePurgeRootEnvDigest, ReleaseGeneration: generation, TransactionStartedAt: started, MaterializationValidUntil: started + authority.BackupMaxAgeSeconds, Files: files}
}

func TestInstallerRejectsDatabaseImageRebindingBeforeCreatingInstallState(t *testing.T) {
	fixture := newInstallFixture(t)
	verified := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer verified.Release()
	verified.DatabaseImage = "postgres:16-alpine@sha256:" + strings.Repeat("0", 64)
	installer := &Installer{HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid())}
	if _, err := installer.Install(verified); err == nil || err.Error() != "install-database-image-binding-invalid" {
		t.Fatalf("database image rebinding accepted: %v", err)
	}
	for _, path := range []string{
		filepath.Join(fixture.root, "etc/propertyquarry-release-single-host-v2/authority.v2.json"),
		filepath.Join(fixture.root, "var/lib/propertyquarry-release-single-host-v2"),
	} {
		if _, err := os.Lstat(path); !os.IsNotExist(err) {
			t.Fatalf("install state created before database image admission at %s: %v", path, err)
		}
	}
}

func TestInstallerCommitsIdempotentlyAndEmitsSignedReceipt(t *testing.T) {
	fixture := newInstallFixture(t)
	verified := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer verified.Release()
	activations := 0
	installer := &Installer{HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid()), Activate: func() (*activationAttempt, error) { activations++; return fixture.activationAttempt(t) }}
	receipt, err := installer.Install(verified)
	if err != nil {
		t.Fatal(err)
	}
	assertSignedInstallReceipt(t, receipt, fixture.receiptKey, "installed-and-active")
	firstPayload := signedInstallReceiptPayload(t, receipt, fixture.receiptKey)
	backupKeyID, keyIDOK := exactString(firstPayload["backup_encryption_key_id"])
	if firstPayload["backup_encryption_key_created"] != true || !keyIDOK || !digestPattern.MatchString(backupKeyID) {
		t.Fatalf("backup key provisioning not bound into install receipt: %#v", firstPayload)
	}
	if activations != 1 {
		t.Fatalf("unexpected activation count %d", activations)
	}
	receipt, err = installer.Install(verified)
	if err != nil {
		t.Fatal(err)
	}
	assertSignedInstallReceipt(t, receipt, fixture.receiptKey, "already-installed")
	secondPayload := signedInstallReceiptPayload(t, receipt, fixture.receiptKey)
	if secondPayload["backup_encryption_key_created"] != false || secondPayload["backup_encryption_key_id"] != backupKeyID {
		t.Fatalf("backup key was not stably reused: %#v", secondPayload)
	}
	if activations != 2 {
		t.Fatalf("idempotent activation count %d", activations)
	}
}

func TestInstallerCreatesAndReusesExactBackupEncryptionKey(t *testing.T) {
	fixture := newInstallFixture(t)
	installer := &Installer{HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid())}
	primeTestPreAdmission(installer)
	keyID, created, err := installer.ensureBackupEncryptionKey()
	if err != nil || !created || !digestPattern.MatchString(keyID) {
		t.Fatalf("backup key was not created: id=%q created=%v err=%v", keyID, created, err)
	}
	keyPath := filepath.Join(fixture.root, strings.TrimPrefix(backupEncryptionKeyPath, "/"))
	raw, err := readExactFile(keyPath, 0o600, backupEncryptionKeyUID, backupEncryptionKeyGID, 65)
	if err != nil || len(raw) != 65 || raw[64] != '\n' {
		t.Fatalf("backup key file is not exact: len=%d err=%v", len(raw), err)
	}
	defer zero(raw)
	decoded := make([]byte, 32)
	if count, err := hex.Decode(decoded, raw[:64]); err != nil || count != len(decoded) || digest(decoded) != keyID {
		zero(decoded)
		t.Fatalf("backup key id is not bound to decoded bytes: %v", err)
	}
	zero(decoded)
	secondID, secondCreated, err := installer.ensureBackupEncryptionKey()
	if err != nil || secondCreated || secondID != keyID {
		t.Fatalf("backup key was not reused exactly: id=%q created=%v err=%v", secondID, secondCreated, err)
	}
}

func TestInstallerRejectsMalformedOrWeaklyProtectedBackupEncryptionKey(t *testing.T) {
	for _, testCase := range []struct {
		name   string
		tamper func(string) error
	}{
		{name: "uppercase", tamper: func(path string) error { return os.WriteFile(path, []byte(strings.Repeat("A", 64)+"\n"), 0o600) }},
		{name: "mode", tamper: func(path string) error { return os.Chmod(path, 0o640) }},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			fixture := newInstallFixture(t)
			installer := &Installer{HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid())}
			primeTestPreAdmission(installer)
			if _, _, err := installer.ensureBackupEncryptionKey(); err != nil {
				t.Fatal(err)
			}
			keyPath := filepath.Join(fixture.root, strings.TrimPrefix(backupEncryptionKeyPath, "/"))
			if err := testCase.tamper(keyPath); err != nil {
				t.Fatal(err)
			}
			if _, _, err := installer.ensureBackupEncryptionKey(); err == nil || err.Error() != "install-backup-key-invalid" {
				t.Fatalf("tampered backup key accepted: %v", err)
			}
		})
	}
}

func TestInstallerRejectsReceiptKeyRotationBeforeJournalReplay(t *testing.T) {
	fixture := newInstallFixture(t)
	first := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer first.Release()
	installer := &Installer{HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid()), Activate: fixture.activate(t)}
	if _, err := installer.Install(first); err != nil {
		t.Fatal(err)
	}
	successor := fixture.verifiedPackage(t, 2, strings.Repeat("b", 40), strings.Repeat("a", 40))
	defer successor.Release()
	successor.ReceiptAuthorityKeyID = "sha256:" + strings.Repeat("f", 64)
	if err := installer.enforceReceiptKeyContinuity(successor); err == nil || err.Error() != "install-receipt-key-rotation-forbidden" {
		t.Fatalf("receipt key rotation was not rejected before replay: %v", err)
	}
}

func TestInstallerRejectsBackupEncryptionKeyRotationAgainstSignedJournal(t *testing.T) {
	fixture := newInstallFixture(t)
	verified := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer verified.Release()
	installer := &Installer{HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid()), Activate: fixture.activate(t)}
	if _, err := installer.Install(verified); err != nil {
		t.Fatal(err)
	}
	keyPath := filepath.Join(fixture.root, strings.TrimPrefix(backupEncryptionKeyPath, "/"))
	if err := os.WriteFile(keyPath, []byte(strings.Repeat("2", 64)+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := installer.Install(verified); err == nil || err.Error() != "install-backup-key-rotation-forbidden" {
		t.Fatalf("backup key rotation was not rejected by signed continuity evidence: %v", err)
	}
}

func TestInstallerRollsBackEveryTargetWhenActivationFails(t *testing.T) {
	fixture := newInstallFixture(t)
	first := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer first.Release()
	activation := 0
	installer := &Installer{HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid()), Activate: func() (*activationAttempt, error) {
		activation++
		if activation == 2 {
			return nil, errors.New("synthetic activation failure")
		}
		return fixture.activationAttempt(t)
	}, Deactivate: func() error { return nil }}
	if _, err := installer.Install(first); err != nil {
		t.Fatal(err)
	}
	firstManifest := append([]byte(nil), first.ManifestRaw...)
	successor := fixture.verifiedPackage(t, 2, strings.Repeat("b", 40), strings.Repeat("a", 40))
	defer successor.Release()
	receipt, err := installer.Install(successor)
	if err != nil {
		t.Fatal(err)
	}
	assertSignedInstallReceipt(t, receipt, fixture.receiptKey, "rolled-back")
	payload := signedInstallReceiptPayload(t, receipt, fixture.receiptKey)
	assertReceiptFlags(t, payload, map[string]bool{
		"candidate_authority_installed": false, "prior_authority_restored": true,
		"systemd_socket_active": true, "rollback_performed": true, "rollback_succeeded": true,
		"reactivation_performed": true, "reactivation_succeeded": true,
	})
	installed, err := os.ReadFile(filepath.Join(fixture.root, "etc/propertyquarry-release-single-host-v2/package-manifest.v2.json"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(installed, firstManifest) || activation != 3 {
		t.Fatal("previous authority was not restored and reactivated")
	}
}

func TestInstallerRecoversInterruptedUpgradeAndResumesSignedSequence(t *testing.T) {
	fixture := newInstallFixture(t)
	first := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer first.Release()
	active := false
	installer := &Installer{
		HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid()),
		Activate: func() (*activationAttempt, error) { active = true; return fixture.activationAttempt(t) }, Deactivate: func() error { active = false; return nil },
	}
	if _, err := installer.Install(first); err != nil {
		t.Fatal(err)
	}
	successor := fixture.verifiedPackage(t, 2, strings.Repeat("b", 40), strings.Repeat("a", 40))
	defer successor.Release()
	installer.Interrupt = func(point string) bool { return point == "after-install-5" }
	if receipt, err := installer.Install(successor); err != errInstallInterrupted || len(receipt) != 0 {
		t.Fatalf("expected abrupt interruption, receipt=%s err=%v", receipt, err)
	}
	if active {
		t.Fatal("upgrade was not quiesced before the interrupted file swap")
	}
	installer.Interrupt = nil
	receipt, err := installer.Install(successor)
	if err != nil {
		t.Fatal(err)
	}
	assertSignedInstallReceipt(t, receipt, fixture.receiptKey, "installed-and-active")
	payload := signedInstallReceiptPayload(t, receipt, fixture.receiptKey)
	assertReceiptFlags(t, payload, map[string]bool{
		"candidate_authority_installed": true, "prior_authority_restored": true,
		"systemd_socket_active": true, "recovery_performed": true, "recovery_succeeded": true,
		"rollback_performed": false, "rollback_succeeded": false,
		"deactivation_performed": true, "deactivation_succeeded": true,
		"reactivation_performed": true, "reactivation_succeeded": true,
	})
	if !active {
		t.Fatal("candidate socket was not verified active")
	}
	assertNoTransactionArtifacts(t, installer, successor)
	assertContiguousSignedJournal(t, installer, successor, fixture.receiptKey)
}

func TestInstallerSuccessfulGenesisReceiptPreservesRecoveryDeactivation(t *testing.T) {
	fixture := newInstallFixture(t)
	verified := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer verified.Release()
	deactivations := 0
	installer := &Installer{
		HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid()),
		Activate: fixture.activate(t),
		Deactivate: func() error {
			deactivations++
			return nil
		},
		Interrupt: func(point string) bool { return point == "after-install-2" },
	}
	if receipt, err := installer.Install(verified); err != errInstallInterrupted || len(receipt) != 0 {
		t.Fatalf("expected interrupted genesis install, receipt=%s err=%v", receipt, err)
	}
	installer.Interrupt = nil
	receipt, err := installer.Install(verified)
	if err != nil {
		t.Fatal(err)
	}
	assertSignedInstallReceipt(t, receipt, fixture.receiptKey, "installed-and-active")
	payload := signedInstallReceiptPayload(t, receipt, fixture.receiptKey)
	assertReceiptFlags(t, payload, map[string]bool{
		"candidate_authority_installed": true, "systemd_socket_active": true,
		"recovery_performed": true, "recovery_succeeded": true,
		"deactivation_performed": true, "deactivation_succeeded": true,
		"reactivation_performed": false, "reactivation_succeeded": false,
		"rollback_performed": false, "rollback_succeeded": false,
	})
	if deactivations != 1 {
		t.Fatalf("recovery deactivation count=%d, want 1", deactivations)
	}
	assertNoTransactionArtifacts(t, installer, verified)
	assertContiguousSignedJournal(t, installer, verified, fixture.receiptKey)
}

func TestMergeRecoveryStateDoesNotMaskCurrentActionFailure(t *testing.T) {
	current := installReceiptState{
		deactivationPerformed: true, deactivationSucceeded: false,
		activationPerformed: true, activationSucceeded: false,
		reactivationPerformed: true, reactivationSucceeded: false,
	}
	recovery := installReceiptState{
		recoveryPerformed: true, recoverySucceeded: true,
		deactivationPerformed: true, deactivationSucceeded: true,
		reactivationPerformed: true, reactivationSucceeded: true,
	}
	merged := mergeRecoveryState(current, recovery)
	if !merged.recoveryPerformed || !merged.recoverySucceeded || !merged.deactivationPerformed || merged.deactivationSucceeded || !merged.reactivationPerformed || merged.reactivationSucceeded {
		t.Fatalf("recovery merge masked the current attempt's action failure: %#v", merged)
	}
}

func TestInstallerRecoveryIsIdempotentAcrossSecondCrash(t *testing.T) {
	fixture := newInstallFixture(t)
	first := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer first.Release()
	installer := &Installer{HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid()), Activate: fixture.activate(t), Deactivate: func() error { return nil }}
	if _, err := installer.Install(first); err != nil {
		t.Fatal(err)
	}
	successor := fixture.verifiedPackage(t, 2, strings.Repeat("b", 40), strings.Repeat("a", 40))
	defer successor.Release()
	installer.Interrupt = func(point string) bool { return point == "after-install-2" }
	if _, err := installer.Install(successor); err != errInstallInterrupted {
		t.Fatalf("first interruption not reached: %v", err)
	}
	installer.Interrupt = func(point string) bool { return point == "during-restore-2" }
	if _, err := installer.Install(successor); err != errInstallInterrupted {
		t.Fatalf("recovery interruption not reached: %v", err)
	}
	installer.Interrupt = nil
	receipt, err := installer.Install(successor)
	if err != nil {
		t.Fatal(err)
	}
	assertSignedInstallReceipt(t, receipt, fixture.receiptKey, "installed-and-active")
	assertNoTransactionArtifacts(t, installer, successor)
}

func TestInstallerCleansBackupsAfterSignedSuccessCrashWithoutClaimingRollback(t *testing.T) {
	fixture := newInstallFixture(t)
	verified := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer verified.Release()
	activations := 0
	installer := &Installer{HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid()), Activate: func() (*activationAttempt, error) { activations++; return fixture.activationAttempt(t) }, Deactivate: func() error { return nil }}
	installer.Interrupt = func(point string) bool { return point == "after-succeeded" }
	if _, err := installer.Install(verified); err != errInstallInterrupted {
		t.Fatalf("success-boundary interruption not reached: %v", err)
	}
	installer.Interrupt = nil
	receipt, err := installer.Install(verified)
	if err != nil {
		t.Fatal(err)
	}
	assertSignedInstallReceipt(t, receipt, fixture.receiptKey, "already-installed")
	payload := signedInstallReceiptPayload(t, receipt, fixture.receiptKey)
	assertReceiptFlags(t, payload, map[string]bool{
		"candidate_authority_installed": true, "systemd_socket_active": true,
		"rollback_performed": false, "rollback_succeeded": false,
	})
	if activations != 2 {
		t.Fatalf("unexpected activation count: %d", activations)
	}
	assertNoTransactionArtifacts(t, installer, verified)
}

func TestInstallerRejectsTamperedSignedJournalBeforeMutation(t *testing.T) {
	fixture := newInstallFixture(t)
	verified := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer verified.Release()
	activations := 0
	installer := &Installer{HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid()), Activate: func() (*activationAttempt, error) { activations++; return fixture.activationAttempt(t) }, Deactivate: func() error { return nil }, Interrupt: func(point string) bool { return point == "after-staged" }}
	if _, err := installer.Install(verified); err != errInstallInterrupted {
		t.Fatalf("staging interruption not reached: %v", err)
	}
	txID := strings.TrimPrefix(verified.ArchiveDigest, "sha256:")[:32]
	eventPath := filepath.Join(fixture.root, "var/lib/propertyquarry-release-single-host-v2/install-journal", txID+"-00000002-staged.json")
	raw, err := os.ReadFile(eventPath)
	if err != nil {
		t.Fatal(err)
	}
	raw[len(raw)-1] ^= 1
	if err := os.WriteFile(eventPath, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	installer.Interrupt = nil
	if _, err := installer.Install(verified); err == nil || !strings.Contains(err.Error(), "install-journal") {
		t.Fatalf("tampered journal accepted: %v", err)
	}
	if activations != 0 {
		t.Fatal("host activation occurred after journal tampering")
	}
}

func TestInstallerPromotesSignedPendingJournalAndDiscardsTornCurrentWrite(t *testing.T) {
	fixture := newInstallFixture(t)
	verified := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer verified.Release()
	installer := &Installer{HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid()), Activate: fixture.activate(t), Deactivate: func() error { return nil }, Interrupt: func(point string) bool { return point == "after-staged" }}
	if _, err := installer.Install(verified); err != errInstallInterrupted {
		t.Fatalf("staging interruption not reached: %v", err)
	}
	txID := strings.TrimPrefix(verified.ArchiveDigest, "sha256:")[:32]
	directory := filepath.Join(fixture.root, "var/lib/propertyquarry-release-single-host-v2/install-journal")
	stagedName := txID + "-00000002-staged.json"
	if err := os.Rename(filepath.Join(directory, stagedName), filepath.Join(directory, ".pending-"+stagedName)); err != nil {
		t.Fatal(err)
	}
	tornName := ".pending-" + txID + "-00000003-deactivated.json"
	if err := os.WriteFile(filepath.Join(directory, tornName), []byte("{\"torn\":"), 0o600); err != nil {
		t.Fatal(err)
	}
	installer.Interrupt = nil
	receipt, err := installer.Install(verified)
	if err != nil {
		t.Fatal(err)
	}
	assertSignedInstallReceipt(t, receipt, fixture.receiptKey, "installed-and-active")
	if _, err := os.Lstat(filepath.Join(directory, tornName)); !os.IsNotExist(err) {
		t.Fatalf("torn pending journal remains: %v", err)
	}
	assertContiguousSignedJournal(t, installer, verified, fixture.receiptKey)
}

func TestInstallerRemovesTornDeterministicStageDuringRecovery(t *testing.T) {
	fixture := newInstallFixture(t)
	verified := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer verified.Release()
	installer := &Installer{HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid()), Activate: fixture.activate(t), Deactivate: func() error { return nil }, Interrupt: func(point string) bool { return point == "after-admitted" }}
	if _, err := installer.Install(verified); err != errInstallInterrupted {
		t.Fatalf("admission interruption not reached: %v", err)
	}
	txID := strings.TrimPrefix(verified.ArchiveDigest, "sha256:")[:32]
	targets, err := installer.candidateTargets(verified, txID)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(targets[0].stage, []byte("torn"), 0o600); err != nil {
		t.Fatal(err)
	}
	installer.Interrupt = nil
	receipt, err := installer.Install(verified)
	if err != nil {
		t.Fatal(err)
	}
	assertSignedInstallReceipt(t, receipt, fixture.receiptKey, "installed-and-active")
	assertNoTransactionArtifacts(t, installer, verified)
}

func TestInstallerRequiresQuiescenceBeforeUpgradeSwap(t *testing.T) {
	fixture := newInstallFixture(t)
	first := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer first.Release()
	installer := &Installer{HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid()), Activate: fixture.activate(t), Deactivate: func() error { return nil }}
	if _, err := installer.Install(first); err != nil {
		t.Fatal(err)
	}
	successor := fixture.verifiedPackage(t, 2, strings.Repeat("b", 40), strings.Repeat("a", 40))
	defer successor.Release()
	deactivatedBeforeSwap := false
	installer.Deactivate = func() error {
		raw, err := os.ReadFile(filepath.Join(fixture.root, "etc/propertyquarry-release-single-host-v2/package-manifest.v2.json"))
		if err != nil || !bytes.Equal(raw, first.ManifestRaw) {
			return errors.New("candidate files visible before quiescence")
		}
		deactivatedBeforeSwap = true
		return nil
	}
	installer.AbortActivation = func() error { return nil }
	receipt, err := installer.Install(successor)
	if err != nil {
		t.Fatal(err)
	}
	assertSignedInstallReceipt(t, receipt, fixture.receiptKey, "installed-and-active")
	if !deactivatedBeforeSwap {
		t.Fatal("upgrade did not quiesce the prior socket")
	}
}

func TestInstallerRefusesUpgradeFromIncompletePriorGeneration(t *testing.T) {
	fixture := newInstallFixture(t)
	first := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer first.Release()
	deactivations := 0
	installer := &Installer{HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid()), Activate: fixture.activate(t), Deactivate: func() error { deactivations++; return nil }}
	if _, err := installer.Install(first); err != nil {
		t.Fatal(err)
	}
	controllerPath := filepath.Join(fixture.root, "usr/libexec/propertyquarry-release-control/propertyquarry-release-single-host-v2")
	raw, err := os.ReadFile(controllerPath)
	if err != nil {
		t.Fatal(err)
	}
	raw[0] ^= 1
	if err := os.WriteFile(controllerPath, raw, 0o755); err != nil {
		t.Fatal(err)
	}
	successor := fixture.verifiedPackage(t, 2, strings.Repeat("b", 40), strings.Repeat("a", 40))
	defer successor.Release()
	if _, err := installer.Install(successor); err == nil || !strings.Contains(err.Error(), "installed-manifest-payload-invalid") {
		t.Fatalf("upgrade accepted incomplete prior generation: %v", err)
	}
	if deactivations != 0 {
		t.Fatal("incomplete prior generation was mutated or quiesced")
	}
}

func TestInstallerReportsDeactivationFailureWithoutClaimingRollback(t *testing.T) {
	fixture := newInstallFixture(t)
	first := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer first.Release()
	installer := &Installer{HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid()), Activate: fixture.activate(t), Deactivate: func() error { return nil }}
	if _, err := installer.Install(first); err != nil {
		t.Fatal(err)
	}
	successor := fixture.verifiedPackage(t, 2, strings.Repeat("b", 40), strings.Repeat("a", 40))
	defer successor.Release()
	installer.Deactivate = func() error { return errors.New("synthetic deactivation failure") }
	receipt, err := installer.Install(successor)
	if err != nil {
		t.Fatal(err)
	}
	assertSignedInstallReceipt(t, receipt, fixture.receiptKey, "deactivation-failed")
	payload := signedInstallReceiptPayload(t, receipt, fixture.receiptKey)
	assertReceiptFlags(t, payload, map[string]bool{
		"candidate_authority_installed": false, "prior_authority_restored": true,
		"systemd_socket_active": true, "deactivation_performed": true, "deactivation_succeeded": false,
		"rollback_performed": false, "rollback_succeeded": false,
	})
	installed, err := os.ReadFile(filepath.Join(fixture.root, "etc/propertyquarry-release-single-host-v2/package-manifest.v2.json"))
	if err != nil || !bytes.Equal(installed, first.ManifestRaw) {
		t.Fatal("candidate files were swapped despite failed deactivation")
	}
}

func TestInstallerReportsRollbackDeactivationFailureTruthfully(t *testing.T) {
	fixture := newInstallFixture(t)
	first := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer first.Release()
	activationCalls, deactivationCalls := 0, 0
	installer := &Installer{HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid())}
	installer.Activate = func() (*activationAttempt, error) {
		activationCalls++
		if activationCalls == 2 {
			return nil, errors.New("synthetic candidate activation failure")
		}
		return fixture.activationAttempt(t)
	}
	installer.Deactivate = func() error {
		deactivationCalls++
		if deactivationCalls == 2 {
			return errors.New("synthetic rollback deactivation failure")
		}
		return nil
	}
	installer.AbortActivation = func() error { return nil }
	if _, err := installer.Install(first); err != nil {
		t.Fatal(err)
	}
	successor := fixture.verifiedPackage(t, 2, strings.Repeat("b", 40), strings.Repeat("a", 40))
	defer successor.Release()
	receipt, err := installer.Install(successor)
	if err != nil {
		t.Fatal(err)
	}
	assertSignedInstallReceipt(t, receipt, fixture.receiptKey, "rollback-deactivation-failed")
	payload := signedInstallReceiptPayload(t, receipt, fixture.receiptKey)
	assertReceiptFlags(t, payload, map[string]bool{
		"candidate_authority_installed": true, "prior_authority_restored": false,
		"systemd_socket_active": false, "rollback_performed": false, "rollback_succeeded": false,
		"deactivation_performed": true, "deactivation_succeeded": false,
	})
}

func TestInstallerReportsPriorReactivationFailureAfterSuccessfulRollback(t *testing.T) {
	fixture := newInstallFixture(t)
	first := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer first.Release()
	activationCalls := 0
	installer := &Installer{
		HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid()),
		Activate: func() (*activationAttempt, error) {
			activationCalls++
			if activationCalls >= 2 {
				return nil, errors.New("synthetic activation failure")
			}
			return fixture.activationAttempt(t)
		},
		Deactivate: func() error { return nil },
	}
	if _, err := installer.Install(first); err != nil {
		t.Fatal(err)
	}
	successor := fixture.verifiedPackage(t, 2, strings.Repeat("b", 40), strings.Repeat("a", 40))
	defer successor.Release()
	receipt, err := installer.Install(successor)
	if err != nil {
		t.Fatal(err)
	}
	assertSignedInstallReceipt(t, receipt, fixture.receiptKey, "prior-restored-reactivation-failed")
	payload := signedInstallReceiptPayload(t, receipt, fixture.receiptKey)
	assertReceiptFlags(t, payload, map[string]bool{
		"candidate_authority_installed": false, "prior_authority_restored": true,
		"systemd_socket_active": false, "rollback_performed": true, "rollback_succeeded": true,
		"reactivation_performed": true, "reactivation_succeeded": false,
	})
}

func TestInstallerSelfBindingUsesSignedRuntimeDigestAndSize(t *testing.T) {
	digestValue, size, err := executableIdentity()
	if err != nil {
		t.Fatal(err)
	}
	verified := &VerifiedPackage{InstallerBinaryDigest: digestValue, InstallerBinarySize: size}
	if err := installerIdentityBindingMatches(verified, digestValue, size); err != nil {
		t.Fatal(err)
	}
	verified.InstallerBinaryDigest = "sha256:" + strings.Repeat("0", 64)
	if err := installerIdentityBindingMatches(verified, digestValue, size); err == nil {
		t.Fatal("installer digest substitution accepted")
	}
	verified.InstallerBinaryDigest = digestValue
	verified.InstallerBinarySize++
	if err := installerIdentityBindingMatches(verified, digestValue, size); err == nil {
		t.Fatal("installer size substitution accepted")
	}
}

func TestInstallerRejectsRegistrationEnvelopeBeforeCreatingInstallState(t *testing.T) {
	fixture := newInstallFixture(t)
	verified := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer verified.Release()
	registrationPath := filepath.Join(fixture.root, "docker/property/state/runtime/propertyquarry_registration_email.env")
	if err := os.WriteFile(registrationPath, []byte("EMAILIT_API_KEY=only-one-name\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	installer := &Installer{HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid())}
	if _, err := installer.Install(verified); err == nil || !strings.Contains(err.Error(), "install-registration-envelope-invalid") {
		t.Fatalf("invalid registration envelope accepted: %v", err)
	}
	for _, path := range []string{
		filepath.Join(fixture.root, "etc/propertyquarry-release-single-host-v2/authority.v2.json"),
		filepath.Join(fixture.root, "var/lib/propertyquarry-release-single-host-v2"),
	} {
		if _, err := os.Lstat(path); !os.IsNotExist(err) {
			t.Fatalf("install state created before envelope admission at %s: %v", path, err)
		}
	}
}

func TestInstallerRejectsSceneVideoEnvelopeBeforeCreatingInstallState(t *testing.T) {
	fixture := newInstallFixture(t)
	verified := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer verified.Release()
	scenePath := filepath.Join(fixture.root, "docker/property/state/runtime/property_scene_video_shared.env")
	if err := os.WriteFile(scenePath, []byte("PROPERTYQUARRY_RENDER_BRIDGE_TOKEN=substituted\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	installer := &Installer{HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid())}
	if _, err := installer.Install(verified); err == nil || !strings.Contains(err.Error(), "install-scene-video-envelope-invalid") {
		t.Fatalf("substituted scene-video envelope accepted: %v", err)
	}
	for _, path := range []string{
		filepath.Join(fixture.root, "etc/propertyquarry-release-single-host-v2/authority.v2.json"),
		filepath.Join(fixture.root, "var/lib/propertyquarry-release-single-host-v2"),
	} {
		if _, err := os.Lstat(path); !os.IsNotExist(err) {
			t.Fatalf("install state created before scene-video envelope admission at %s: %v", path, err)
		}
	}
}

func TestInstallerAcquiresValidatedEtcLockBeforeCreatingPackageDirectories(t *testing.T) {
	fixture := newInstallFixture(t)
	verified := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer verified.Release()
	installer := &Installer{HostRoot: fixture.root, OwnerUID: uint32(os.Geteuid()), OwnerGID: uint32(os.Getegid()), Interrupt: func(point string) bool { return point == "after-lock" }}
	if _, err := installer.Install(verified); err != errInstallInterrupted {
		t.Fatalf("lock boundary not reached: %v", err)
	}
	lockPath := filepath.Join(fixture.root, "etc/.propertyquarry-release-single-host-v2.install.lock")
	info, err := os.Lstat(lockPath)
	if err != nil || !validOwnedRegular(info, uint32(os.Geteuid()), uint32(os.Getegid())) || info.Mode().Perm() != 0o600 {
		t.Fatalf("validated install lock missing: info=%v err=%v", info, err)
	}
	for _, path := range []string{
		filepath.Join(fixture.root, "etc/propertyquarry-release-single-host-v2/authority.v2.json"),
		filepath.Join(fixture.root, "usr/libexec/propertyquarry-release-control"),
		filepath.Join(fixture.root, "var/lib/propertyquarry-release-single-host-v2"),
	} {
		if _, err := os.Lstat(path); !os.IsNotExist(err) {
			t.Fatalf("package directory created before lock boundary at %s: %v", path, err)
		}
	}
}

func TestPythonPackageCrossLanguageIntegration(t *testing.T) {
	archivePath := os.Getenv("PROPERTYQUARRY_PACKAGE_INTEGRATION_ARCHIVE")
	anchorPath := os.Getenv("PROPERTYQUARRY_PACKAGE_INTEGRATION_ANCHOR")
	if archivePath == "" && anchorPath == "" {
		t.Skip("set package integration archive and anchor paths")
	}
	if archivePath == "" || anchorPath == "" {
		t.Fatal("both package integration paths are required")
	}
	anchorRaw, err := os.ReadFile(anchorPath)
	if err != nil {
		t.Fatal(err)
	}
	key, der, keyID, err := parsePublicPEM(anchorRaw)
	zero(anchorRaw)
	zero(der)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(key)
	verified, err := VerifyPackageFile(archivePath, key, keyID)
	if err != nil {
		t.Fatal(err)
	}
	defer verified.Release()
	if verified.PackageAuthorityKeyID != keyID || len(verified.Files) != len(requiredPackageFiles) || verified.ReleaseGeneration < 1 || verified.DatabaseImage != databaseImage {
		t.Fatal("cross-language package binding incomplete")
	}
}

func assertSignedInstallReceipt(t *testing.T, raw []byte, key ed25519.PrivateKey, disposition string) {
	t.Helper()
	payload := signedInstallReceiptPayload(t, raw, key)
	if payload["disposition"] != disposition {
		t.Fatalf("unexpected receipt disposition: %#v", payload)
	}
}

func signedInstallReceiptPayload(t *testing.T, raw []byte, key ed25519.PrivateKey) map[string]any {
	t.Helper()
	wrapper, err := strictJSON(raw, maximumManifestBytes)
	if err != nil {
		t.Fatal(err)
	}
	payload, ok := wrapper["payload"].(map[string]any)
	if !ok {
		t.Fatalf("unexpected receipt: %#v", wrapper)
	}
	payloadRaw, err := canonicalJSON(payload)
	if err != nil {
		t.Fatal(err)
	}
	signatureText, _ := exactString(wrapper["signature"])
	signature, err := base64.RawURLEncoding.DecodeString(signatureText)
	if err != nil || !ed25519.Verify(key.Public().(ed25519.PublicKey), framed(installReceiptDomain, payloadRaw), signature) {
		t.Fatal("install receipt signature invalid")
	}
	return payload
}

func assertReceiptFlags(t *testing.T, payload map[string]any, expected map[string]bool) {
	t.Helper()
	for name, value := range expected {
		actual, ok := payload[name].(bool)
		if !ok || actual != value {
			t.Fatalf("receipt flag %s=%v, want %v: %#v", name, payload[name], value, payload)
		}
	}
}

func assertNoTransactionArtifacts(t *testing.T, installer *Installer, verified *VerifiedPackage) {
	t.Helper()
	txID := strings.TrimPrefix(verified.ArchiveDigest, "sha256:")[:32]
	targets, err := installer.candidateTargets(verified, txID)
	if err != nil {
		t.Fatal(err)
	}
	for _, target := range targets {
		for _, path := range []string{target.stage, target.backup} {
			if _, err := os.Lstat(path); !os.IsNotExist(err) {
				t.Fatalf("transaction artifact remains at %s: %v", path, err)
			}
		}
	}
}

func assertContiguousSignedJournal(t *testing.T, installer *Installer, verified *VerifiedPackage, receiptKey ed25519.PrivateKey) {
	t.Helper()
	txID := strings.TrimPrefix(verified.ArchiveDigest, "sha256:")[:32]
	directory := filepath.Join(installer.HostRoot, "var/lib/propertyquarry-release-single-host-v2/install-journal")
	entries, err := os.ReadDir(directory)
	if err != nil {
		t.Fatal(err)
	}
	sequence := int64(0)
	for _, entry := range entries {
		matches := installJournalNamePattern.FindStringSubmatch(entry.Name())
		if len(matches) != 4 || matches[1] != txID {
			continue
		}
		sequence++
		parsed, err := strconv.ParseInt(matches[2], 10, 64)
		if err != nil || parsed != sequence {
			t.Fatalf("journal sequence gap at %s", entry.Name())
		}
	}
	if sequence < 5 {
		t.Fatalf("journal unexpectedly short: %d", sequence)
	}
	replay, err := installer.replayJournal(directory, verified, receiptKey.Public().(ed25519.PublicKey), txID)
	if err != nil {
		t.Fatal(err)
	}
	if replay.lastSequence != sequence || replay.lastEvent != "succeeded" {
		t.Fatalf("unexpected replay terminal: %#v", replay)
	}
}

func testPublicPEM(t *testing.T, key ed25519.PublicKey) ([]byte, string) {
	t.Helper()
	der, err := x509.MarshalPKIXPublicKey(key)
	if err != nil {
		t.Fatal(err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der}), digest(der)
}

func writeTestFile(t *testing.T, path string, raw []byte, mode os.FileMode) {
	t.Helper()
	if err := os.WriteFile(path, raw, mode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, mode); err != nil {
		t.Fatal(err)
	}
}
