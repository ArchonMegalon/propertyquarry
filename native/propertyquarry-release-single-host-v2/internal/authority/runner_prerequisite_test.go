//go:build linux && amd64

package authority

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"
)

func TestInstalledRunnerPrerequisiteProofRejectsTamperRebindCopyAndNoncanonicalSignature(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	public := fixture.receiptKey.Public().(ed25519.PublicKey)
	proof, err := validateInstalledRunnerPrerequisiteRecords(fixture.root, fixture.config, public)
	if err != nil || proof.IntentDigest != fixture.config.RunnerPrerequisiteIntentDigest || proof.ApprovalDigest != fixture.config.RunnerPrerequisiteApprovalDigest || proof.ApprovalPayloadDigest != fixture.config.RunnerPrerequisiteApprovalPayloadDigest || proof.JobID != fixture.config.RunnerPrerequisiteJobID {
		t.Fatalf("exact prerequisite proof rejected: proof=%#v err=%v", proof, err)
	}

	intentPath := rooted(fixture.root, RunnerPrerequisiteIntentPath)
	approvalPath := rooted(fixture.root, RunnerPrerequisiteApprovalPath)
	intentRaw, err := os.ReadFile(intentPath)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(intentRaw)
	approvalRaw, err := os.ReadFile(approvalPath)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(approvalRaw)

	tampered := append([]byte(nil), approvalRaw...)
	tampered[len(tampered)-2] ^= 1
	writeFixture(t, approvalPath, tampered, 0o400)
	if _, err := validateInstalledRunnerPrerequisiteRecords(fixture.root, fixture.config, public); err == nil {
		t.Fatal("post-install prerequisite approval tamper accepted")
	}
	zero(tampered)
	writeFixture(t, approvalPath, approvalRaw, 0o400)

	writeFixture(t, intentPath, approvalRaw, 0o400)
	if _, err := validateInstalledRunnerPrerequisiteRecords(fixture.root, fixture.config, public); err == nil {
		t.Fatal("approval wrapper copied into intent slot accepted")
	}
	writeFixture(t, intentPath, intentRaw, 0o400)

	wrapper, err := strictJSON(approvalRaw, maximumJournalBytes)
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
	if _, err := verifyRunnerWire(noncanonicalRaw, public, runnerPrerequisiteApprovalSignatureDomain); err == nil {
		t.Fatal("noncanonical base64url signature spelling accepted")
	}
	zero(signature)
	zero(noncanonicalDecoded)
	zero(noncanonicalRaw)

	wrapper, err = strictJSON(approvalRaw, maximumJournalBytes)
	if err != nil {
		t.Fatal(err)
	}
	payload := wrapper["payload"].(map[string]any)
	payload["prerequisite_conclusion"] = "failure"
	payloadRaw, err := canonicalJSON(payload)
	if err != nil {
		t.Fatal(err)
	}
	reboundRaw, err := signRunnerWire(payload, fixture.receiptKey, runnerPrerequisiteApprovalSignatureDomain)
	if err != nil {
		t.Fatal(err)
	}
	reboundConfig := *fixture.config
	reboundConfig.RunnerPrerequisiteApprovalDigest = digest(reboundRaw)
	reboundConfig.RunnerPrerequisiteApprovalPayloadDigest = digest(payloadRaw)
	writeFixture(t, approvalPath, reboundRaw, 0o400)
	if _, err := validateInstalledRunnerPrerequisiteRecords(fixture.root, &reboundConfig, public); err == nil {
		t.Fatal("correctly resigned semantic prerequisite rebind accepted")
	}
	zero(payloadRaw)
	zero(reboundRaw)
}

func installV3PrerequisiteFixture(t *testing.T, fixture *authorityFixture) ([]byte, []byte) {
	t.Helper()
	intentV2, err := os.ReadFile(rooted(fixture.root, RunnerPrerequisiteIntentPath))
	if err != nil {
		t.Fatal(err)
	}
	defer zero(intentV2)
	intentWire, err := strictJSON(intentV2, maximumJournalBytes)
	if err != nil {
		t.Fatal(err)
	}
	intent := intentWire["payload"].(map[string]any)
	intent["prerequisite_job_key"] = runnerPrerequisiteJobKey
	intent["prerequisite_job_name"] = runnerPrerequisiteJobName(
		fixture.config.RunnerLabel,
		fixture.config.RunnerReservationDigest,
	)
	intent["schema"] = runnerPrerequisiteIntentSchemaV3
	intent["version"] = json.Number("3")
	intentRaw, err := signRunnerWire(intent, fixture.receiptKey, runnerPrerequisiteIntentSignatureDomainV3)
	if err != nil {
		t.Fatal(err)
	}

	approvalV2, err := os.ReadFile(rooted(fixture.root, RunnerPrerequisiteApprovalPath))
	if err != nil {
		t.Fatal(err)
	}
	defer zero(approvalV2)
	approvalWire, err := strictJSON(approvalV2, maximumJournalBytes)
	if err != nil {
		t.Fatal(err)
	}
	approval := approvalWire["payload"].(map[string]any)
	approval["intent_sha256"] = digest(intentRaw)
	approval["prerequisite_job_key"] = runnerPrerequisiteJobKey
	approval["prerequisite_job_name"] = intent["prerequisite_job_name"]
	approval["schema"] = runnerPrerequisiteApprovalSchemaV3
	approval["version"] = json.Number("3")
	requestRaw, err := canonicalJSON(map[string]any{
		"comment":         intent["comment"],
		"environment_ids": []any{json.Number("42")},
		"state":           "approved",
	})
	if err != nil {
		t.Fatal(err)
	}
	postAttempt := map[string]any{
		"attempted_at_epoch":                  approval["approved_at_epoch"],
		"authority_profile":                   "single-host-production-v2",
		"comment":                             intent["comment"],
		"environment_id":                      intent["environment_id"],
		"environment_name":                    Environment,
		"github_api_path":                     "/repos/ArchonMegalon/propertyquarry/actions/runs/" + fixture.config.RunnerRunID + "/pending_deployments",
		"http_method":                         "POST",
		"intent_sha256":                       digest(intentRaw),
		"pre_post_jobs_sha256":                "sha256:" + strings.Repeat("8", 64),
		"pre_post_pending_deployments_count":  json.Number("1"),
		"pre_post_pending_deployments_sha256": "sha256:" + strings.Repeat("9", 64),
		"pre_post_release_job_present":        false,
		"pre_post_review_history_sha256":      "sha256:" + strings.Repeat("a", 64),
		"pre_post_review_match_count":         json.Number("0"),
		"pre_post_review_scope":               "any-approved-target-environment",
		"pre_post_run_sha256":                 "sha256:" + strings.Repeat("b", 64),
		"prerequisite_job_id":                 intent["prerequisite_job_id"],
		"prerequisite_job_key":                runnerPrerequisiteJobKey,
		"prerequisite_job_name":               intent["prerequisite_job_name"],
		"receipt_authority_key_id":            fixture.config.ReceiptAuthorityKeyID,
		"repository":                          Repository,
		"repository_id":                       RepositoryID,
		"repository_owner_id":                 RepositoryOwnerID,
		"request_sha256":                      digest(requestRaw),
		"reservation_expires_at_epoch":        intent["reservation_expires_at_epoch"],
		"reservation_sha256":                  intent["reservation_sha256"],
		"run_attempt":                         intent["run_attempt"],
		"run_id":                              intent["run_id"],
		"runner_label":                        intent["runner_label"],
		"schema":                              runnerPrerequisitePostAttemptSchemaV3,
		"version":                             json.Number("3"),
		"workflow_path":                       intent["workflow_path"],
		"workflow_ref":                        intent["workflow_ref"],
		"workflow_sha":                        intent["workflow_sha"],
	}
	zero(requestRaw)
	postAttemptRaw, err := signRunnerWire(postAttempt, fixture.receiptKey, runnerPrerequisitePostAttemptSignatureDomainV3)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(postAttemptRaw)
	approvalPayloadRaw, err := canonicalJSON(approval)
	if err != nil {
		t.Fatal(err)
	}
	approvalRaw, err := signRunnerWire(approval, fixture.receiptKey, runnerPrerequisiteApprovalSignatureDomainV3)
	if err != nil {
		t.Fatal(err)
	}
	fixture.config.RunnerPrerequisiteIntentDigest = digest(intentRaw)
	fixture.config.RunnerPrerequisiteApprovalDigest = digest(approvalRaw)
	fixture.config.RunnerPrerequisiteApprovalPayloadDigest = digest(approvalPayloadRaw)
	zero(approvalPayloadRaw)
	writeFixture(t, rooted(fixture.root, RunnerPrerequisiteIntentPathV3), intentRaw, 0o400)
	writeFixture(t, rooted(fixture.root, RunnerPrerequisitePostAttemptPathV3), postAttemptRaw, 0o400)
	writeFixture(t, rooted(fixture.root, RunnerPrerequisiteApprovalPathV3), approvalRaw, 0o400)
	return intentRaw, approvalRaw
}

func TestInstalledRunnerPrerequisiteV3BindsDynamicJobIdentityAndRejectsMixedVersions(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	public := fixture.receiptKey.Public().(ed25519.PublicKey)
	intentRaw, approvalRaw := installV3PrerequisiteFixture(t, fixture)
	defer zero(intentRaw)
	defer zero(approvalRaw)
	proof, err := validateInstalledRunnerPrerequisiteRecords(fixture.root, fixture.config, public)
	if err != nil || proof.IntentDigest != digest(intentRaw) || proof.ApprovalDigest != digest(approvalRaw) {
		t.Fatalf("v3 prerequisite proof rejected: proof=%#v err=%v", proof, err)
	}
	postAttemptPath := rooted(fixture.root, RunnerPrerequisitePostAttemptPathV3)
	postAttemptRaw, err := os.ReadFile(postAttemptPath)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(postAttemptRaw)
	if err := os.Remove(postAttemptPath); err != nil {
		t.Fatal(err)
	}
	if _, err := validateInstalledRunnerPrerequisiteRecords(fixture.root, fixture.config, public); err == nil {
		t.Fatal("v3 approval without signed post-attempt accepted")
	}
	writeFixture(t, postAttemptPath, postAttemptRaw, 0o400)

	approvalWire, err := strictJSON(approvalRaw, maximumJournalBytes)
	if err != nil {
		t.Fatal(err)
	}
	approval := approvalWire["payload"].(map[string]any)
	approval["prerequisite_job_name"] = runnerPrerequisiteJobKey
	reboundPayload, err := canonicalJSON(approval)
	if err != nil {
		t.Fatal(err)
	}
	reboundRaw, err := signRunnerWire(approval, fixture.receiptKey, runnerPrerequisiteApprovalSignatureDomainV3)
	if err != nil {
		t.Fatal(err)
	}
	reboundConfig := *fixture.config
	reboundConfig.RunnerPrerequisiteApprovalDigest = digest(reboundRaw)
	reboundConfig.RunnerPrerequisiteApprovalPayloadDigest = digest(reboundPayload)
	writeFixture(t, rooted(fixture.root, RunnerPrerequisiteApprovalPathV3), reboundRaw, 0o400)
	if _, err := validateInstalledRunnerPrerequisiteRecords(fixture.root, &reboundConfig, public); err == nil {
		t.Fatal("correctly resigned static v3 prerequisite display name accepted")
	}
	zero(reboundPayload)
	zero(reboundRaw)

	writeFixture(t, rooted(fixture.root, RunnerPrerequisiteApprovalPathV3), approvalRaw, 0o400)
	if err := os.Remove(rooted(fixture.root, RunnerPrerequisiteIntentPathV3)); err != nil {
		t.Fatal(err)
	}
	if _, err := validateInstalledRunnerPrerequisiteRecords(fixture.root, fixture.config, public); err == nil {
		t.Fatal("mixed v2/v3 prerequisite file set accepted")
	}
}

func TestRunnerRequestAuthorizationRereadsInstalledPrerequisiteProof(t *testing.T) {
	fixture := newRunnerLifecycleFixture(t)
	binding := fixture.admitAndStart(t)
	request := &workflowRequest{
		DiagnosticRunID: fixture.authority.config.RunnerRunID, DiagnosticRunAttempt: fixture.authority.config.RunnerRunAttempt,
		DiagnosticRunnerLabel: fixture.label, RunnerTicketDigest: fixture.authority.config.RunnerReservationDigest,
	}
	identity := &Identity{
		RunID: fixture.authority.config.RunnerRunID, RunAttempt: fixture.authority.config.RunnerRunAttempt,
		CheckRunID: fixture.authority.config.RunnerJobID, RunnerID: "789",
		RunnerName: "pq-release-" + fixture.label[len("pqrelease-"):], RunnerLabel: fixture.label,
	}
	if _, err := verifyRunnerTicketForRequest(fixture.authority.root, fixture.authority.config, request, identity, fixture.now.Add(3*time.Second)); err != nil {
		t.Fatalf("exact installed proof did not authorize request: %v", err)
	}
	approvalPath := rooted(fixture.authority.root, RunnerPrerequisiteApprovalPath)
	approvalRaw, err := os.ReadFile(approvalPath)
	if err != nil {
		t.Fatal(err)
	}
	approvalRaw[len(approvalRaw)-2] ^= 1
	writeFixture(t, approvalPath, approvalRaw, 0o400)
	if observed, err := verifyRunnerTicketForRequest(fixture.authority.root, fixture.authority.config, request, identity, fixture.now.Add(4*time.Second)); err == nil || observed != nil {
		t.Fatal("request authorization reused cached prerequisite proof after post-install tamper")
	}
	if binding.LaunchTicketDigest == "" {
		t.Fatal("runner fixture did not produce a launch binding")
	}
}

func TestConfigRejectsReleaseAndPrerequisiteJobIdentityCollision(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	raw, err := os.ReadFile(rooted(fixture.root, ConfigPath))
	if err != nil {
		t.Fatal(err)
	}
	value, err := strictJSON(raw, maximumConfigBytes)
	zero(raw)
	if err != nil {
		t.Fatal(err)
	}
	value["runner_prerequisite_job_id"] = value["runner_job_id"]
	rebound, err := canonicalJSON(value)
	if err != nil {
		t.Fatal(err)
	}
	if parsed, err := parseConfigWithExternalValidation(value, rebound, fixture.config.PackageAuthorityKeyID, fixture.root, false); err == nil || parsed != nil {
		if parsed != nil {
			parsed.release()
		}
		t.Fatal("release and prerequisite job identity collision accepted")
	}
	zero(rebound)
}
