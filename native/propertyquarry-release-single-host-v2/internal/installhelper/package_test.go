package installhelper

import (
	"archive/tar"
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"testing"

	"propertyquarry.local/release-single-host-v2/internal/authority"
)

func TestPythonPackageVerifierCrossLanguage(t *testing.T) {
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
	if verified.PackageAuthorityKeyID != keyID || len(verified.Files) != len(requiredPackageFiles) || verified.InstallerBinaryDigest == "" || verified.InstallerBinarySize < 1 || verified.DatabaseImage != databaseImage || verified.APIHostIP != apiHostIP || verified.APIHostPort != apiHostPort || verified.APIContainerPort != apiContainerPort {
		t.Fatal("cross-language package binding incomplete")
	}
}

func TestDeterministicUSTAREnvelopeRejectsTrailingAndPaddingData(t *testing.T) {
	raw := pythonTarfileUSTAR(t, "manifest.v2.json")
	if err := validateDeterministicUSTAREnvelope(raw); err != nil {
		t.Fatalf("Python canonical USTAR rejected: %v", err)
	}

	paddingTamper := append([]byte(nil), raw...)
	paddingTamper[513] = 1
	if err := validateDeterministicUSTAREnvelope(paddingTamper); err == nil {
		t.Fatal("non-zero member padding accepted")
	}

	trailingTamper := append(append([]byte(nil), raw...), make([]byte, 10240)...)
	trailingTamper[len(raw)] = 1
	if err := validateDeterministicUSTAREnvelope(trailingTamper); err == nil {
		t.Fatal("data after the canonical USTAR record accepted")
	}
}

func TestDeterministicUSTAREnvelopeRejectsParserAcceptedNoncanonicalHeaders(t *testing.T) {
	canonical := pythonTarfileUSTAR(t, "manifest.v2.json")
	if canonical[154] != 0 || canonical[155] != ' ' {
		t.Fatal("Python checksum terminator fixture is not NUL-space")
	}
	variants := map[string]func(*testing.T, []byte){
		"checksum-space-space": func(_ *testing.T, raw []byte) {
			raw[154] = ' '
		},
		"checksum-leading-space": func(_ *testing.T, raw []byte) {
			raw[148] = ' '
		},
		"mode-space-terminated": func(_ *testing.T, raw []byte) {
			raw[107] = ' '
			recalculateUSTARChecksum(raw[:512])
		},
		"uid-space-padded": func(_ *testing.T, raw []byte) {
			for index := 108; index < 116; index++ {
				raw[index] = ' '
			}
			recalculateUSTARChecksum(raw[:512])
		},
		"regular-device-field-as-octal-zero": func(t *testing.T, raw []byte) {
			if err := formatPythonUSTAROctal(raw[329:337], 0); err != nil {
				t.Fatal(err)
			}
			recalculateUSTARChecksum(raw[:512])
		},
		"hidden-name-suffix": func(t *testing.T, raw []byte) {
			terminator := bytes.IndexByte(raw[:100], 0)
			if terminator < 0 || terminator+1 >= 100 {
				t.Fatal("name fixture has no spare field byte")
			}
			raw[terminator+1] = 'x'
			recalculateUSTARChecksum(raw[:512])
		},
		"regular-type-as-NUL": func(_ *testing.T, raw []byte) {
			raw[156] = 0
			recalculateUSTARChecksum(raw[:512])
		},
	}
	for name, mutate := range variants {
		t.Run(name, func(t *testing.T) {
			raw := append([]byte(nil), canonical...)
			mutate(t, raw)
			assertArchiveTarAccepts(t, raw, "manifest.v2.json")
			if err := validateDeterministicUSTAREnvelope(raw); err == nil {
				t.Fatal("noncanonical header accepted")
			}
		})
	}
}

func TestDeterministicUSTAREnvelopeUsesPythonFirstValidNameSplit(t *testing.T) {
	first := strings.Repeat("a", 60)
	second := strings.Repeat("b", 40)
	third := strings.Repeat("c", 40)
	name := first + "/" + second + "/" + third
	canonical := pythonTarfileUSTAR(t, name)
	if err := validateDeterministicUSTAREnvelope(canonical); err != nil {
		t.Fatalf("Python long-name split rejected: %v", err)
	}
	alternate := append([]byte(nil), canonical...)
	for index := 0; index < 100; index++ {
		alternate[index] = 0
	}
	for index := 345; index < 500; index++ {
		alternate[index] = 0
	}
	copy(alternate[0:100], third)
	copy(alternate[345:500], first+"/"+second)
	recalculateUSTARChecksum(alternate[:512])
	assertArchiveTarAccepts(t, alternate, name)
	if err := validateDeterministicUSTAREnvelope(alternate); err == nil {
		t.Fatal("alternate USTAR prefix split accepted")
	}
}

func pythonTarfileUSTAR(t *testing.T, name string) []byte {
	t.Helper()
	const script = `import io,sys,tarfile
output=io.BytesIO()
with tarfile.open(fileobj=output,mode="w",format=tarfile.USTAR_FORMAT) as archive:
    info=tarfile.TarInfo(sys.argv[1])
    info.size=1
    info.mode=0o444
    info.uid=0
    info.gid=0
    info.uname=""
    info.gname=""
    info.mtime=0
    info.type=tarfile.REGTYPE
    archive.addfile(info,io.BytesIO(b"x"))
sys.stdout.buffer.write(output.getvalue())
`
	command := exec.Command("python3", "-c", script, name)
	command.Env = []string{
		"LANG=C.UTF-8",
		"LC_ALL=C.UTF-8",
		"PATH=/usr/bin:/bin",
		"PYTHONDONTWRITEBYTECODE=1",
	}
	raw, err := command.Output()
	if err != nil {
		t.Fatalf("Python tarfile fixture failed: %v", err)
	}
	if len(raw) < 10240 || len(raw)%10240 != 0 {
		t.Fatalf("Python tarfile fixture length invalid: %d", len(raw))
	}
	return raw
}

func assertArchiveTarAccepts(t *testing.T, raw []byte, expectedName string) {
	t.Helper()
	reader := tar.NewReader(bytes.NewReader(raw))
	header, err := reader.Next()
	if err != nil {
		t.Fatalf("archive/tar rejected adversarial fixture: %v", err)
	}
	if header.Name != expectedName {
		t.Fatalf("archive/tar changed fixture name: %q", header.Name)
	}
	data, err := io.ReadAll(reader)
	if err != nil || !bytes.Equal(data, []byte{'x'}) {
		t.Fatalf("archive/tar fixture body invalid: %q, %v", data, err)
	}
	if _, err := reader.Next(); err != io.EOF {
		t.Fatalf("archive/tar fixture terminator invalid: %v", err)
	}
}

func recalculateUSTARChecksum(header []byte) {
	for index := 148; index < 156; index++ {
		header[index] = ' '
	}
	checksum := int64(0)
	for _, value := range header {
		checksum += int64(value)
	}
	digits := strconv.FormatInt(checksum, 8)
	for index := 148; index < 154; index++ {
		header[index] = '0'
	}
	copy(header[154-len(digits):154], digits)
	header[154] = 0
	header[155] = ' '
}

func TestNativeBuildReceiptBindsControllerInstallerAndClaims(t *testing.T) {
	controller := syntheticStaticELF(t)
	packageKeyID := "sha256:" + strings.Repeat("a", 64)
	sourceDigest := "sha256:" + strings.Repeat("b", 64)
	previousSource := InstallerSourceManifestDigest
	InstallerSourceManifestDigest = sourceDigest
	t.Cleanup(func() { InstallerSourceManifestDigest = previousSource })
	receipt := validNativeBuildReceipt(controller, packageKeyID, sourceDigest)
	raw, err := canonicalJSON(receipt)
	if err != nil {
		t.Fatal(err)
	}
	raw = append(raw, '\n')
	binding, err := validateNativeBuildReceipt(raw, controller, packageKeyID)
	if err != nil {
		t.Fatal(err)
	}
	if binding.installerDigest != "sha256:"+strings.Repeat("c", 64) || binding.installerSize != 4096 || binding.sourceManifestDigest != sourceDigest {
		t.Fatalf("unexpected build binding: %#v", binding)
	}

	receipt["installer_package_authority_bound"] = false
	tampered, _ := canonicalJSON(receipt)
	tampered = append(tampered, '\n')
	if _, err := validateNativeBuildReceipt(tampered, controller, packageKeyID); err == nil {
		t.Fatal("unbound installer build receipt accepted")
	}

	receipt = validNativeBuildReceipt(controller, packageKeyID, sourceDigest)
	receipt["static_elf_verified_in_both_builds"] = false
	tampered, _ = canonicalJSON(receipt)
	tampered = append(tampered, '\n')
	if _, err := validateNativeBuildReceipt(tampered, controller, packageKeyID); err == nil {
		t.Fatal("false static ELF build claim accepted")
	}
}

func TestRequiredPayloadPurposeIsExact(t *testing.T) {
	files := make(map[string]*FileRecord, len(requiredPackageFiles))
	for path, required := range requiredPackageFiles {
		size := required.size
		if size == 0 {
			size = 1
		}
		files[path] = &FileRecord{InstallPath: path, Purpose: required.purpose, Mode: required.mode, Size: size, Digest: required.digest, Data: []byte{'x'}}
	}
	if err := validateRequiredFiles(files); err != nil {
		t.Fatal(err)
	}
	files["/usr/libexec/propertyquarry-release-control/propertyquarry-release-single-host-v2"].Purpose = "arbitrary-executable"
	if err := validateRequiredFiles(files); err == nil {
		t.Fatal("incorrect signed purpose accepted")
	}
	files["/usr/libexec/propertyquarry-release-control/propertyquarry-release-single-host-v2"].Purpose = "controller-binary"
	files[databaseControlHelperPath].Digest = "sha256:" + strings.Repeat("0", 64)
	if err := validateRequiredFiles(files); err == nil {
		t.Fatal("substituted database control helper accepted")
	}
}

func signedRunnerFixture(t *testing.T, private ed25519.PrivateKey, keyID, domain string, payload map[string]any) []byte {
	t.Helper()
	canonical, err := canonicalJSON(payload)
	if err != nil {
		t.Fatal(err)
	}
	signature := ed25519.Sign(private, framed(domain, canonical))
	wire, err := canonicalJSON(map[string]any{
		"payload": payload, "signature": base64.RawURLEncoding.EncodeToString(signature), "signature_key_id": keyID,
	})
	zero(canonical)
	zero(signature)
	if err != nil {
		t.Fatal(err)
	}
	return wire
}

func TestRunnerMaterialBindsReservationTicketCheckoutAndSocket(t *testing.T) {
	_, receiptPrivate, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(receiptPrivate)
	receiptPublic := receiptPrivate.Public().(ed25519.PublicKey)
	_, receiptKeyID := testPublicPEM(t, receiptPublic)
	workflowSHA := strings.Repeat("b", 40)
	runtimeSHA := strings.Repeat("a", 40)
	nonce := strings.Repeat("c", 64)
	nonceRaw, _ := hex.DecodeString(nonce)
	derived := sha256.Sum256(append([]byte(runnerLabelDerivationDomain), nonceRaw...))
	zero(nonceRaw)
	label := "pqrelease-" + hex.EncodeToString(derived[:16])
	created := int64(1_753_179_900)
	started := int64(1_753_180_000)
	reservationPayload := map[string]any{
		"authority_profile": "single-host-production-v2", "created_at_epoch": json.Number(strconv.FormatInt(created, 10)),
		"environment": authority.Environment, "expires_at_epoch": json.Number(strconv.FormatInt(created+21600, 10)),
		"receipt_authority_key_id": receiptKeyID, "release_job": authority.ReleaseJob, "repository": authority.Repository,
		"repository_id": authority.RepositoryID, "repository_owner_id": authority.RepositoryOwnerID, "reservation_nonce": nonce,
		"runner_label": label, "runner_label_nonce": strings.TrimPrefix(label, "pqrelease-"), "schema": runnerReservationSchema,
		"source_checkout_identity_sha256": "sha256:" + strings.Repeat("1", 64),
		"source_checkout_path":            "/docker/property/state/runtime/propertyquarry-release-authority-v2.private/single-host-v2-release-checkouts/" + workflowSHA,
		"source_tree_sha256":              "sha256:" + strings.Repeat("2", 64), "version": json.Number("2"),
		"workflow_path": ".github/workflows/smoke-runtime.yml", "workflow_ref": authority.WorkflowRef, "workflow_sha": workflowSHA,
	}
	reservationRaw := signedRunnerFixture(t, receiptPrivate, receiptKeyID, runnerReservationSignatureDomain, reservationPayload)
	config := &authority.Config{
		Digest: digest([]byte("config")), PlanDigest: digest([]byte("plan")), ReceiptAuthorityKeyID: receiptKeyID,
		RuntimeSHA: runtimeSHA, WorkflowSHA: workflowSHA, TransactionStartedAtEpoch: started,
		RunnerReservationDigest: digest(reservationRaw), RunnerLabel: label, RunnerRunID: "123", RunnerRunAttempt: 1, RunnerJobID: "456",
		RunnerPrerequisiteIntentDigest: "sha256:" + strings.Repeat("3", 64), RunnerPrerequisiteApprovalDigest: "sha256:" + strings.Repeat("4", 64),
		RunnerPrerequisiteApprovalPayloadDigest: "sha256:" + strings.Repeat("5", 64), RunnerPrerequisiteJobID: "789",
		WebImage: "ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime@sha256:" + strings.Repeat("8", 64),
	}
	ticketPayload := map[string]any{
		"authority_profile": "single-host-production-v2", "bound_at_epoch": json.Number(strconv.FormatInt(started+10, 10)),
		"config_digest": config.Digest, "dispatch_ticket_sha256": config.RunnerReservationDigest,
		"docker_socket": map[string]any{"device": json.Number("11"), "gid": json.Number("112"), "inode": json.Number("12"), "mode": "0660", "nlink": json.Number("1"), "path": "/var/run/docker.sock", "uid": json.Number("0")},
		"environment":   authority.Environment, "expires_at_epoch": json.Number(strconv.FormatInt(started+1810, 10)), "job_id": config.RunnerJobID,
		"plan_digest": config.PlanDigest, "receipt_authority_key_id": receiptKeyID, "release_job": authority.ReleaseJob,
		"repository": authority.Repository, "repository_id": authority.RepositoryID, "repository_owner_id": authority.RepositoryOwnerID,
		"reservation_nonce": nonce, "run_attempt": json.Number("1"), "run_id": config.RunnerRunID, "runner_image": config.WebImage,
		"runner_label": label, "runner_label_nonce": strings.TrimPrefix(label, "pqrelease-"), "runtime_sha": runtimeSHA,
		"runner_prerequisite_intent_sha256":           config.RunnerPrerequisiteIntentDigest,
		"runner_prerequisite_approval_sha256":         config.RunnerPrerequisiteApprovalDigest,
		"runner_prerequisite_approval_payload_sha256": config.RunnerPrerequisiteApprovalPayloadDigest,
		"runner_prerequisite_job_id":                  config.RunnerPrerequisiteJobID,
		"schema":                                      runnerLaunchTicketSchema, "version": json.Number("2"), "workflow_path": ".github/workflows/smoke-runtime.yml",
		"workflow_ref": authority.WorkflowRef, "workflow_sha": workflowSHA,
	}
	ticketRaw := signedRunnerFixture(t, receiptPrivate, receiptKeyID, runnerLaunchTicketSignatureDomain, ticketPayload)
	binding, err := validateRunnerMaterial(reservationRaw, ticketRaw, config, config.PlanDigest, receiptKeyID, receiptPublic)
	if err != nil {
		t.Fatal(err)
	}
	if binding.launchTicketDigest != digest(ticketRaw) || binding.sourceCheckoutIdentity != reservationPayload["source_checkout_identity_sha256"] || binding.sourceCheckoutPath != reservationPayload["source_checkout_path"] || binding.sourceTreeDigest != reservationPayload["source_tree_sha256"] || binding.boundAt != started+10 {
		t.Fatalf("unexpected runner binding: %#v", binding)
	}

	tampered := append([]byte(nil), ticketRaw...)
	tampered[len(tampered)-1] ^= 1
	if _, err := validateRunnerMaterial(reservationRaw, tampered, config, config.PlanDigest, receiptKeyID, receiptPublic); err == nil {
		t.Fatal("tampered runner launch ticket accepted")
	}
	ticketPayload["docker_socket"].(map[string]any)["inode"] = json.Number("0")
	invalidSocket := signedRunnerFixture(t, receiptPrivate, receiptKeyID, runnerLaunchTicketSignatureDomain, ticketPayload)
	if _, err := validateRunnerMaterial(reservationRaw, invalidSocket, config, config.PlanDigest, receiptKeyID, receiptPublic); err == nil {
		t.Fatal("invalid runner socket binding accepted")
	}
}

func TestRunnerPrerequisiteMaterialBindsExactRawWrappersAndSemantics(t *testing.T) {
	_, receiptPrivate, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(receiptPrivate)
	receiptPublic := receiptPrivate.Public().(ed25519.PublicKey)
	_, receiptKeyID := testPublicPEM(t, receiptPublic)
	workflowSHA := strings.Repeat("b", 40)
	nonce := strings.Repeat("01", 32)
	nonceRaw, _ := hex.DecodeString(nonce)
	derived := sha256.Sum256(append([]byte(runnerLabelDerivationDomain), nonceRaw...))
	zero(nonceRaw)
	label := "pqrelease-" + hex.EncodeToString(derived[:16])
	created := int64(1_753_179_900)
	started := int64(1_753_180_000)
	expires := created + 21600
	reservationPayload := map[string]any{
		"authority_profile": "single-host-production-v2", "created_at_epoch": json.Number(strconv.FormatInt(created, 10)),
		"environment": authority.Environment, "expires_at_epoch": json.Number(strconv.FormatInt(expires, 10)),
		"receipt_authority_key_id": receiptKeyID, "release_job": authority.ReleaseJob, "repository": authority.Repository,
		"repository_id": authority.RepositoryID, "repository_owner_id": authority.RepositoryOwnerID, "reservation_nonce": nonce,
		"runner_label": label, "runner_label_nonce": strings.TrimPrefix(label, "pqrelease-"), "schema": runnerReservationSchema,
		"source_checkout_identity_sha256": "sha256:" + strings.Repeat("1", 64),
		"source_checkout_path":            "/docker/property/state/runtime/propertyquarry-release-authority-v2.private/single-host-v2-release-checkouts/" + workflowSHA,
		"source_tree_sha256":              "sha256:" + strings.Repeat("2", 64), "version": json.Number("2"),
		"workflow_path": ".github/workflows/smoke-runtime.yml", "workflow_ref": authority.WorkflowRef, "workflow_sha": workflowSHA,
	}
	reservationRaw := signedRunnerFixture(t, receiptPrivate, receiptKeyID, runnerReservationSignatureDomain, reservationPayload)
	intentPayload := map[string]any{
		"authority_profile": "single-host-production-v2", "comment": "PropertyQuarry governed prerequisite approval " + digest(reservationRaw),
		"discovered_at_epoch": json.Number(strconv.FormatInt(started-30, 10)), "environment_id": "42", "environment_name": authority.Environment,
		"initial_jobs_sha256": "sha256:" + strings.Repeat("3", 64), "initial_pending_deployments_sha256": "sha256:" + strings.Repeat("4", 64),
		"initial_runs_index_sha256": "sha256:" + strings.Repeat("5", 64), "prerequisite_job_id": "789", "prerequisite_job_name": runnerPrerequisiteJob,
		"receipt_authority_key_id": receiptKeyID, "release_job": authority.ReleaseJob, "repository": authority.Repository,
		"repository_id": authority.RepositoryID, "repository_owner_id": authority.RepositoryOwnerID,
		"reservation_expires_at_epoch": json.Number(strconv.FormatInt(expires, 10)), "reservation_sha256": digest(reservationRaw),
		"run_attempt": json.Number("1"), "run_id": "123", "runner_label": label, "schema": runnerPrerequisiteIntentSchema,
		"version": json.Number("2"), "workflow_path": ".github/workflows/smoke-runtime.yml", "workflow_ref": authority.WorkflowRef, "workflow_sha": workflowSHA,
	}
	intentRaw := signedRunnerFixture(t, receiptPrivate, receiptKeyID, runnerPrerequisiteIntentSignatureDomain, intentPayload)
	approvalPayload := map[string]any{
		"approval_api_disposition": "approved", "approval_response_sha256": "sha256:" + strings.Repeat("6", 64),
		"approved_at_epoch": json.Number(strconv.FormatInt(started-20, 10)), "completed_jobs_sha256": "sha256:" + strings.Repeat("7", 64),
		"environment_id": "42", "environment_name": authority.Environment, "intent_sha256": digest(intentRaw),
		"post_pending_deployments_sha256": "sha256:" + strings.Repeat("8", 64), "prerequisite_conclusion": "success",
		"prerequisite_job_id": "789", "prerequisite_job_name": runnerPrerequisiteJob, "receipt_authority_key_id": receiptKeyID,
		"release_job": authority.ReleaseJob, "repository": authority.Repository, "repository_id": authority.RepositoryID,
		"repository_owner_id": authority.RepositoryOwnerID, "reservation_expires_at_epoch": json.Number(strconv.FormatInt(expires, 10)),
		"reservation_sha256": digest(reservationRaw), "review_history_sha256": "sha256:" + strings.Repeat("9", 64),
		"run_attempt": json.Number("1"), "run_id": "123", "runner_label": label, "schema": runnerPrerequisiteApprovalSchema,
		"version": json.Number("2"), "workflow_path": ".github/workflows/smoke-runtime.yml", "workflow_ref": authority.WorkflowRef, "workflow_sha": workflowSHA,
	}
	approvalPayloadRaw, err := canonicalJSON(approvalPayload)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(approvalPayloadRaw)
	approvalRaw := signedRunnerFixture(t, receiptPrivate, receiptKeyID, runnerPrerequisiteApprovalSignatureDomain, approvalPayload)
	config := &authority.Config{
		WorkflowSHA: workflowSHA, TransactionStartedAtEpoch: started, ReceiptAuthorityKeyID: receiptKeyID,
		RunnerReservationDigest: digest(reservationRaw), RunnerLabel: label, RunnerRunID: "123", RunnerRunAttempt: 1, RunnerJobID: "456",
		RunnerPrerequisiteIntentDigest: digest(intentRaw), RunnerPrerequisiteApprovalDigest: digest(approvalRaw),
		RunnerPrerequisiteApprovalPayloadDigest: digest(approvalPayloadRaw), RunnerPrerequisiteJobID: "789",
	}
	binding, err := validateRunnerPrerequisiteMaterial(intentRaw, approvalRaw, reservationRaw, config, receiptKeyID, receiptPublic)
	if err != nil || binding.intentDigest != config.RunnerPrerequisiteIntentDigest || binding.approvalDigest != config.RunnerPrerequisiteApprovalDigest || binding.approvalPayloadDigest != config.RunnerPrerequisiteApprovalPayloadDigest || binding.jobID != "789" {
		t.Fatalf("exact prerequisite material rejected: binding=%#v err=%v", binding, err)
	}

	tampered := append([]byte(nil), approvalRaw...)
	tampered[len(tampered)-2] ^= 1
	if _, err := validateRunnerPrerequisiteMaterial(intentRaw, tampered, reservationRaw, config, receiptKeyID, receiptPublic); err == nil {
		t.Fatal("tampered prerequisite approval accepted")
	}
	if _, err := validateRunnerPrerequisiteMaterial(approvalRaw, approvalRaw, reservationRaw, config, receiptKeyID, receiptPublic); err == nil {
		t.Fatal("approval copied into prerequisite intent slot accepted")
	}
	equalJobs := *config
	equalJobs.RunnerPrerequisiteJobID = equalJobs.RunnerJobID
	if _, err := validateRunnerPrerequisiteMaterial(intentRaw, approvalRaw, reservationRaw, &equalJobs, receiptKeyID, receiptPublic); err == nil {
		t.Fatal("release and prerequisite job identity collision accepted")
	}

	wrapper, err := strictJSON(approvalRaw, maximumManifestBytes)
	if err != nil {
		t.Fatal(err)
	}
	signatureText, _ := exactString(wrapper["signature"])
	signature, err := base64.RawURLEncoding.DecodeString(signatureText)
	if err != nil {
		t.Fatal(err)
	}
	alphabet := "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
	last := strings.IndexByte(alphabet, signatureText[len(signatureText)-1])
	if last < 0 {
		t.Fatal("fixture signature is not base64url")
	}
	replacement := last + 1
	if last%16 == 15 {
		replacement = last - 1
	}
	noncanonicalText := signatureText[:len(signatureText)-1] + alphabet[replacement:replacement+1]
	noncanonicalDecoded, err := base64.RawURLEncoding.DecodeString(noncanonicalText)
	if err != nil || !bytes.Equal(noncanonicalDecoded, signature) {
		t.Fatalf("noncanonical base64 fixture did not decode identically: %v", err)
	}
	wrapper["signature"] = noncanonicalText
	noncanonicalRaw, err := canonicalJSON(wrapper)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := validateSignedRunnerWire(noncanonicalRaw, receiptPublic, receiptKeyID, runnerPrerequisiteApprovalSignatureDomain); err == nil {
		t.Fatal("noncanonical base64url signature spelling accepted")
	}
	zero(signature)
	zero(noncanonicalDecoded)
	zero(noncanonicalRaw)

	reboundPayload := make(map[string]any, len(approvalPayload))
	for key, value := range approvalPayload {
		reboundPayload[key] = value
	}
	reboundPayload["prerequisite_conclusion"] = "failure"
	reboundPayloadRaw, err := canonicalJSON(reboundPayload)
	if err != nil {
		t.Fatal(err)
	}
	reboundRaw := signedRunnerFixture(t, receiptPrivate, receiptKeyID, runnerPrerequisiteApprovalSignatureDomain, reboundPayload)
	reboundConfig := *config
	reboundConfig.RunnerPrerequisiteApprovalDigest = digest(reboundRaw)
	reboundConfig.RunnerPrerequisiteApprovalPayloadDigest = digest(reboundPayloadRaw)
	if _, err := validateRunnerPrerequisiteMaterial(intentRaw, reboundRaw, reservationRaw, &reboundConfig, receiptKeyID, receiptPublic); err == nil {
		t.Fatal("correctly resigned prerequisite semantic rebind accepted")
	}
	zero(reboundPayloadRaw)
	zero(reboundRaw)
}

func TestFrozenV2ManifestAndFiveHelperSurface(t *testing.T) {
	if len(requiredPackageFiles) != 26 {
		t.Fatalf("required package file count = %d, want 26", len(requiredPackageFiles))
	}
	sealed := map[string]requiredPackageFile{
		predeployBackupHelperPath:  {mode: 0o755, purpose: "predeploy-backup-helper", size: 91482, digest: "sha256:a7a877b6aae97628892f9c603eddc8267625689676a0daf4685de65613be56d3"},
		databaseControlHelperPath:  {mode: 0o755, purpose: "database-control-helper", size: 60006, digest: "sha256:cb6ccfd9e043efa13a559dd1c9538fb557c76890f4c1fda69f0dd623dc72664b"},
		runtimeDatabaseHelperPath:  {mode: 0o755, purpose: "runtime-database-helper", size: 50478, digest: "sha256:bd7be57c75e22e645ed6ec2acf5fc521e05f1d76ff4afa5abe233a8faa92d5e2"},
		runtimeIsolationHelperPath: {mode: 0o755, purpose: "runtime-isolation-helper", size: 159586, digest: "sha256:0310d09ddb2df14b0e766e52d53270cc381e29c009c55995c19f6e6ceff2cca8"},
		runtimeDeployHelperPath:    {mode: 0o755, purpose: "runtime-deploy-helper", size: 82510, digest: "sha256:85518c562f05834407f758f44044195435a1f4c84c3df90d2c7219b28aa0a9a6"},
	}
	for path, expected := range sealed {
		if actual, ok := requiredPackageFiles[path]; !ok || actual != expected {
			t.Fatalf("sealed helper %q = %#v, want %#v", path, actual, expected)
		}
	}
	if !envelopeSHAPattern.MatchString(strings.Repeat("a", 64)) || envelopeSHAPattern.MatchString(strings.Repeat("a", 40)) {
		t.Fatal("envelope SHA contract is not exact raw64")
	}
}

func TestManifestRejectsPackageReceiptKeyRoleCollision(t *testing.T) {
	keyID := "sha256:" + strings.Repeat("d", 64)
	value := map[string]any{
		"api_container_port": json.Number("8090"), "api_host_ip": apiHostIP, "api_host_port": json.Number("8097"),
		"archive_format": "ustar-v1", "authority_profile": "single-host-production-v2", "backup_max_age_seconds": json.Number("3600"),
		"build_receipt_digest": "sha256:" + strings.Repeat("1", 64), "config_digest": "sha256:" + strings.Repeat("2", 64),
		"cloudflared_image": "cloudflare/cloudflared@sha256:" + strings.Repeat("8", 64),
		"database_image":    databaseImage, "database_substrate_digest": "sha256:" + strings.Repeat("d", 64),
		"deployment_id": strings.Repeat("d", 64), "envelope_sha": strings.Repeat("3", 64), "files": []any{}, "host_machine_id_digest": "sha256:" + strings.Repeat("4", 64),
		"installed_manifest_path":           "/etc/propertyquarry-release-single-host-v2/package-manifest.v2.json",
		"installed_manifest_signature_path": "/etc/propertyquarry-release-single-host-v2/package-manifest.v2.sig",
		"non_authoritative_until":           "independent-root-helper-reverification-and-atomic-install", "package_authority_key_id": keyID,
		"package_signing_private_key_included": false, "payload_root": "payload", "plan_digest": "sha256:" + strings.Repeat("5", 64),
		"post_purge_root_env_digest": "sha256:" + strings.Repeat("b", 64), "pre_purge_root_env_digest": "sha256:" + strings.Repeat("a", 64),
		"pre_purge_runtime_inputs_digest": "sha256:" + strings.Repeat("c", 64),
		"receipt_authority_key_id":        keyID, "release_generation": json.Number("1"), "root_helper_verification_required": true,
		"runner_prerequisite_intent_sha256":           "sha256:" + strings.Repeat("1", 64),
		"runner_prerequisite_approval_sha256":         "sha256:" + strings.Repeat("2", 64),
		"runner_prerequisite_approval_payload_sha256": "sha256:" + strings.Repeat("3", 64),
		"runner_prerequisite_job_id":                  "789",
		"render_image":                                "ghcr.io/archonmegalon/propertyquarry-standalone-render-runtime@sha256:" + strings.Repeat("2", 64),
		"runtime_deploy_digest":                       "sha256:" + strings.Repeat("e", 64), "runtime_inputs_digest": "sha256:" + strings.Repeat("f", 64),
		"runtime_retirement_digest": "sha256:" + strings.Repeat("0", 64), "runtime_sha": strings.Repeat("6", 40), "workflow_sha": strings.Repeat("7", 40), "schema": packageManifestSchema,
		"scene_video_env_digest": "sha256:" + strings.Repeat("9", 64), "scene_video_env_gid": json.Number("1000"),
		"scene_video_env_mode": json.Number("384"), "scene_video_env_path": "/docker/property/state/runtime/property_scene_video_shared.env",
		"scene_video_env_uid": json.Number("1000"), "transaction_started_at_epoch": json.Number("1753180000"), "version": json.Number("2"),
		"web_image": "ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime@sha256:" + strings.Repeat("1", 64),
	}
	if _, err := parseAndBindManifest(value, nil, nil, "sha256:"+strings.Repeat("7", 64), nil, make([]byte, 32), keyID); err == nil {
		t.Fatal("package and receipt authority key collision accepted")
	}
}

func validNativeBuildReceipt(controller []byte, packageKeyID, sourceDigest string) map[string]any {
	return map[string]any{
		"authoritative": false, "binary_mode": "0755", "binary_sha256": digest(controller), "binary_size": json.Number(strconv.Itoa(len(controller))),
		"build_flags":                    []any{"-mod=readonly", "-trimpath", "-buildvcs=false", "-buildmode=exe"},
		"go_tests_passed_in_both_builds": true, "host_network_namespace_isolated": false, "independent_toolchain_extractions": true,
		"installer_binary_mode": "0555", "installer_binary_sha256": "sha256:" + strings.Repeat("c", 64), "installer_binary_size": json.Number("4096"),
		"installer_package_authority_bound": true, "installer_package_authority_key_id": packageKeyID, "module_network_resolution_disabled": true,
		"package_signature_verified": false, "performs_release_effects": false, "production_ready": false, "receipt_published_last": true,
		"reproducible_double_build": true, "root_install_performed": false, "schema": buildReceiptSchema,
		"scratch_execution_contract": "linux-amd64-static-et-exec-v1", "source_manifest_digest": sourceDigest,
		"static_elf_verified_in_both_builds": true, "toolchain": "go1.26.5 linux/amd64", "toolchain_archive_bytes": json.Number("66879095"),
		"toolchain_archive_sha256": "5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053", "version": json.Number("2"),
	}
}

func syntheticStaticELF(t *testing.T) []byte {
	t.Helper()
	raw := make([]byte, 176)
	copy(raw[:16], []byte{0x7f, 'E', 'L', 'F', 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0})
	binary.LittleEndian.PutUint16(raw[16:18], 2)
	binary.LittleEndian.PutUint16(raw[18:20], 62)
	binary.LittleEndian.PutUint32(raw[20:24], 1)
	binary.LittleEndian.PutUint64(raw[24:32], 0x400000)
	binary.LittleEndian.PutUint64(raw[32:40], 64)
	binary.LittleEndian.PutUint16(raw[52:54], 64)
	binary.LittleEndian.PutUint16(raw[54:56], 56)
	binary.LittleEndian.PutUint16(raw[56:58], 2)
	load := raw[64:120]
	binary.LittleEndian.PutUint32(load[0:4], 1)
	binary.LittleEndian.PutUint32(load[4:8], 5)
	binary.LittleEndian.PutUint64(load[32:40], uint64(len(raw)))
	binary.LittleEndian.PutUint64(load[40:48], uint64(len(raw)))
	stack := raw[120:176]
	binary.LittleEndian.PutUint32(stack[0:4], 0x6474e551)
	binary.LittleEndian.PutUint32(stack[4:8], 6)
	return raw
}
