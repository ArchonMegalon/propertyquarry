//go:build linux && amd64

package authority

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
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
