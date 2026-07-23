package authority

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	ActivationCanaryUnitPath          = "/usr/lib/systemd/system/propertyquarry-release-single-host-v2-activation-canary.service"
	ActivationCanaryChallengePath     = "/run/propertyquarry-release-single-host-v2/activation-canary/activation-challenge.v2"
	ActivationCanaryResultPath        = "/run/propertyquarry-release-single-host-v2/activation-canary/activation-canary-receipt.v2.json"
	ControllerBinaryPath              = "/usr/libexec/propertyquarry-release-control/propertyquarry-release-single-host-v2"
	PackageManifestPath               = "/etc/propertyquarry-release-single-host-v2/package-manifest.v2.json"
	activationTokenCredentialPath     = "/run/credentials/propertyquarry-release-single-host-v2-activation-canary.service/github-api-token"
	activationChallengeCredentialPath = "/run/credentials/propertyquarry-release-single-host-v2-activation-canary.service/activation-challenge"
	activationKeyCredentialPath       = "/run/credentials/propertyquarry-release-single-host-v2-activation-canary.service/receipt-authority-key"
	activationCanaryDomain            = "propertyquarry.release-control.single-host-activation-canary-receipt-signature.v2\x00"
	activationCanaryLifetime          = 120 * time.Second
	maximumActivationCanaryBytes      = 64 * 1024
)

var (
	activationRunnerURL = "https://api.github.com/repos/ArchonMegalon/propertyquarry/actions/runners?per_page=1"
	activationOIDCURL   = "https://api.github.com/repos/ArchonMegalon/propertyquarry/actions/oidc/customization/sub"
)

// ActivationCanaryExpected is the complete public binding that a fresh host
// canary receipt must carry. It intentionally contains no credential material.
type ActivationCanaryExpected struct {
	ChallengeDigest       string
	ChallengeCreatedAt    int64
	CanaryStartedAt       int64
	ConfigDigest          string
	ControllerDigest      string
	PackageManifestDigest string
	PlanDigest            string
	UnitDigest            string
	RuntimeSHA            string
	WorkflowSHA           string
	PackageAuthorityKeyID string
	ReceiptAuthorityKeyID string
}

// ActivationCanaryProof is safe to nest in the signed install receipt.
type ActivationCanaryProof struct {
	Receipt         map[string]any
	ReceiptDigest   string
	ChallengeDigest string
	UnitDigest      string
	VerifiedAt      int64
	ValidUntil      int64
}

func runActivationCanary(ctx context.Context, root string, now time.Time, client httpDoer) error {
	if os.Geteuid() != 0 || ctx == nil || client == nil || now.IsZero() || root != "/" {
		return fmt.Errorf("activation-canary-input-invalid")
	}
	config, receiptPublic, err := loadCanaryConfig(root)
	if err != nil {
		return err
	}
	defer config.release()
	defer zero(receiptPublic)
	binding, err := activationCanaryBinding(root, config, "")
	if err != nil {
		return err
	}
	challenge, err := secureRead(root, activationChallengeCredentialPath, 0o400, 0, 0, 32)
	if err != nil || len(challenge) != 32 {
		zero(challenge)
		return fmt.Errorf("activation-canary-challenge-invalid")
	}
	binding.ChallengeDigest = digest(challenge)
	zero(challenge)
	credential, err := secureRead(root, activationTokenCredentialPath, 0o400, 0, 0, 4096)
	if err != nil || len(credential) < 32 {
		zero(credential)
		return fmt.Errorf("activation-canary-token-invalid")
	}
	defer zero(credential)
	token := strings.TrimSuffix(string(credential), "\n")
	if token == "" || strings.ContainsAny(token, "\r\n\x00") {
		return fmt.Errorf("activation-canary-token-invalid")
	}
	keyRaw, err := secureRead(root, activationKeyCredentialPath, 0o400, 0, 0, 4096)
	if err != nil {
		zero(keyRaw)
		return fmt.Errorf("activation-canary-key-invalid")
	}
	key, err := parsePrivateKey(keyRaw)
	zero(keyRaw)
	if err != nil || !bytes.Equal(key.Public().(ed25519.PublicKey), receiptPublic) {
		zero(key)
		return fmt.Errorf("activation-canary-key-invalid")
	}
	defer zero(key)
	if err := probeActivationAuthority(ctx, client, token); err != nil {
		return err
	}
	verifiedAt := now.UTC().Unix()
	payload := activationCanaryPayload(binding, verifiedAt)
	payloadRaw, err := canonicalJSON(payload)
	if err != nil {
		return fmt.Errorf("activation-canary-receipt-invalid")
	}
	defer zero(payloadRaw)
	signature := ed25519.Sign(key, framed(activationCanaryDomain, payloadRaw))
	defer zero(signature)
	wrapper := map[string]any{
		"payload":          payload,
		"signature":        base64.RawURLEncoding.EncodeToString(signature),
		"signature_key_id": config.ReceiptAuthorityKeyID,
	}
	wire, err := canonicalJSON(wrapper)
	if err != nil {
		return fmt.Errorf("activation-canary-receipt-invalid")
	}
	defer zero(wire)
	return writeActivationCanaryResult(root, wire)
}

func probeActivationAuthority(ctx context.Context, client httpDoer, token string) error {
	runnerRaw, err := fetchGitHubAPI(ctx, client, activationRunnerURL, token)
	if err != nil {
		return fmt.Errorf("activation-canary-runner-scope-rejected")
	}
	defer zero(runnerRaw)
	runner, err := decodedJSONObject(runnerRaw, maximumGitHubAPIBytes)
	if err != nil || !hasKeys(runner, "runners", "total_count") {
		return fmt.Errorf("activation-canary-runner-response-invalid")
	}
	total, totalOK := claimInt(runner["total_count"])
	runners, runnersOK := runner["runners"].([]any)
	if !totalOK || total < 0 || !runnersOK || len(runners) > 1 || int64(len(runners)) > total {
		return fmt.Errorf("activation-canary-runner-response-invalid")
	}
	for _, item := range runners {
		value, ok := item.(map[string]any)
		id, idOK := claimInt(value["id"])
		name, nameOK := exactString(value["name"])
		if !ok || !idOK || id < 1 || !nameOK || len(name) < 1 || strings.TrimSpace(name) != name || len(name) > 256 {
			return fmt.Errorf("activation-canary-runner-response-invalid")
		}
	}
	oidcRaw, err := fetchGitHubAPI(ctx, client, activationOIDCURL, token)
	if err != nil {
		return fmt.Errorf("activation-canary-oidc-scope-rejected")
	}
	defer zero(oidcRaw)
	oidc, err := decodedJSONObject(oidcRaw, maximumGitHubAPIBytes)
	if err != nil || !hasKeys(oidc, "sub_claim_prefix", "use_default", "use_immutable_subject") ||
		oidc["use_default"] != true || oidc["use_immutable_subject"] != true || oidc["sub_claim_prefix"] != "repo:ArchonMegalon/propertyquarry" {
		return fmt.Errorf("activation-canary-oidc-response-invalid")
	}
	return nil
}

func activationCanaryPayload(binding ActivationCanaryExpected, verifiedAt int64) map[string]any {
	immutableSubject := "repo:ArchonMegalon@" + RepositoryOwnerID + "/propertyquarry@" + RepositoryID + ":environment:" + Environment
	return map[string]any{
		"authority_profile":                            "single-host-production-v2",
		"challenge_sha256":                             binding.ChallengeDigest,
		"config_digest":                                binding.ConfigDigest,
		"controller_sha256":                            binding.ControllerDigest,
		"github_immutable_oidc_subject_verified":       true,
		"github_repository_runner_admin_read_verified": true,
		"immutable_subject":                            immutableSubject,
		"package_authority_key_id":                     binding.PackageAuthorityKeyID,
		"package_manifest_digest":                      binding.PackageManifestDigest,
		"plan_digest":                                  binding.PlanDigest,
		"receipt_authority_key_id":                     binding.ReceiptAuthorityKeyID,
		"repository":                                   Repository,
		"repository_id":                                RepositoryID,
		"repository_owner_id":                          RepositoryOwnerID,
		"runtime_sha":                                  binding.RuntimeSHA,
		"schema":                                       "propertyquarry.release-control.single-host-activation-canary-receipt.v2",
		"unit_sha256":                                  binding.UnitDigest,
		"valid_until":                                  json.Number(strconv.FormatInt(verifiedAt+int64(activationCanaryLifetime/time.Second), 10)),
		"verified_at":                                  json.Number(strconv.FormatInt(verifiedAt, 10)),
		"version":                                      json.Number("2"),
		"workflow_sha":                                 binding.WorkflowSHA,
	}
}

// VerifyActivationCanaryReceipt verifies a canary receipt against the package-
// pinned receipt key, an exact activation challenge, and a <=2 minute window.
func VerifyActivationCanaryReceipt(raw []byte, public ed25519.PublicKey, expected ActivationCanaryExpected, now time.Time) (*ActivationCanaryProof, error) {
	if len(raw) < 2 || len(raw) > maximumActivationCanaryBytes || len(public) != ed25519.PublicKeySize || now.IsZero() {
		return nil, fmt.Errorf("activation-canary-receipt-invalid")
	}
	derivedKeyID, err := publicKeyID(public)
	if err != nil || derivedKeyID != expected.ReceiptAuthorityKeyID {
		return nil, fmt.Errorf("activation-canary-receipt-key-invalid")
	}
	wrapper, err := strictJSON(raw, maximumActivationCanaryBytes)
	if err != nil || !hasKeys(wrapper, "payload", "signature", "signature_key_id") || wrapper["signature_key_id"] != expected.ReceiptAuthorityKeyID {
		return nil, fmt.Errorf("activation-canary-receipt-invalid")
	}
	payload, ok := wrapper["payload"].(map[string]any)
	signatureText, signatureOK := exactString(wrapper["signature"])
	if !ok || !signatureOK {
		return nil, fmt.Errorf("activation-canary-receipt-invalid")
	}
	signature, err := signatureBytes(signatureText)
	if err != nil {
		return nil, fmt.Errorf("activation-canary-receipt-invalid")
	}
	defer zero(signature)
	payloadRaw, err := canonicalJSON(payload)
	if err != nil || !ed25519.Verify(public, framed(activationCanaryDomain, payloadRaw), signature) {
		zero(payloadRaw)
		return nil, fmt.Errorf("activation-canary-receipt-authentication-failed")
	}
	defer zero(payloadRaw)
	verifiedAt, verifiedOK := exactInt(payload["verified_at"], 1, 1<<62)
	validUntil, validOK := exactInt(payload["valid_until"], 1, 1<<62)
	nowUnix := now.UTC().Unix()
	if !hasKeys(payload, "authority_profile", "challenge_sha256", "config_digest", "controller_sha256", "github_immutable_oidc_subject_verified", "github_repository_runner_admin_read_verified", "immutable_subject", "package_authority_key_id", "package_manifest_digest", "plan_digest", "receipt_authority_key_id", "repository", "repository_id", "repository_owner_id", "runtime_sha", "schema", "unit_sha256", "valid_until", "verified_at", "version", "workflow_sha") ||
		payload["schema"] != "propertyquarry.release-control.single-host-activation-canary-receipt.v2" || payload["version"] != json.Number("2") || payload["authority_profile"] != "single-host-production-v2" ||
		payload["challenge_sha256"] != expected.ChallengeDigest || payload["config_digest"] != expected.ConfigDigest || payload["controller_sha256"] != expected.ControllerDigest ||
		payload["package_manifest_digest"] != expected.PackageManifestDigest || payload["plan_digest"] != expected.PlanDigest || payload["unit_sha256"] != expected.UnitDigest ||
		payload["runtime_sha"] != expected.RuntimeSHA || payload["workflow_sha"] != expected.WorkflowSHA || payload["package_authority_key_id"] != expected.PackageAuthorityKeyID || payload["receipt_authority_key_id"] != expected.ReceiptAuthorityKeyID ||
		payload["repository"] != Repository || payload["repository_id"] != RepositoryID || payload["repository_owner_id"] != RepositoryOwnerID ||
		payload["immutable_subject"] != "repo:ArchonMegalon@"+RepositoryOwnerID+"/propertyquarry@"+RepositoryID+":environment:"+Environment ||
		payload["github_repository_runner_admin_read_verified"] != true || payload["github_immutable_oidc_subject_verified"] != true ||
		!digestPattern.MatchString(expected.ChallengeDigest) || !verifiedOK || !validOK || validUntil-verifiedAt != int64(activationCanaryLifetime/time.Second) || verifiedAt > nowUnix+5 || validUntil < nowUnix ||
		expected.ChallengeCreatedAt < 1 || expected.CanaryStartedAt < expected.ChallengeCreatedAt || expected.CanaryStartedAt-expected.ChallengeCreatedAt > 30 || verifiedAt < expected.CanaryStartedAt || verifiedAt-expected.CanaryStartedAt > 60 {
		return nil, fmt.Errorf("activation-canary-receipt-binding-invalid")
	}
	return &ActivationCanaryProof{Receipt: wrapper, ReceiptDigest: digest(raw), ChallengeDigest: expected.ChallengeDigest, UnitDigest: expected.UnitDigest, VerifiedAt: verifiedAt, ValidUntil: validUntil}, nil
}

// InstalledActivationCanaryExpected derives verification data solely from the
// signed, installed public profile and installed package payload.
func InstalledActivationCanaryExpected(root, challengeDigest string) (ActivationCanaryExpected, ed25519.PublicKey, error) {
	config, receiptPublic, err := loadCanaryConfig(root)
	if err != nil {
		return ActivationCanaryExpected{}, nil, err
	}
	defer config.release()
	binding, err := activationCanaryBinding(root, config, challengeDigest)
	if err != nil {
		zero(receiptPublic)
		return ActivationCanaryExpected{}, nil, err
	}
	return binding, receiptPublic, nil
}

func activationCanaryBinding(root string, config *Config, challengeDigest string) (ActivationCanaryExpected, error) {
	ownerUID, ownerGID := secureOwner(root)
	unit, err := secureRead(root, ActivationCanaryUnitPath, 0o444, ownerUID, ownerGID, 65_536)
	if err != nil {
		return ActivationCanaryExpected{}, fmt.Errorf("activation-canary-unit-invalid")
	}
	defer zero(unit)
	controller, err := secureRead(root, ControllerBinaryPath, 0o755, ownerUID, ownerGID, 256*1024*1024)
	if err != nil {
		return ActivationCanaryExpected{}, fmt.Errorf("activation-canary-controller-invalid")
	}
	defer zero(controller)
	manifest, err := secureRead(root, PackageManifestPath, 0o444, ownerUID, ownerGID, maximumConfigBytes)
	if err != nil {
		return ActivationCanaryExpected{}, fmt.Errorf("activation-canary-manifest-invalid")
	}
	defer zero(manifest)
	return ActivationCanaryExpected{
		ChallengeDigest: challengeDigest, ConfigDigest: config.Digest, ControllerDigest: digest(controller),
		PackageManifestDigest: digest(manifest), PlanDigest: config.PlanDigest, UnitDigest: digest(unit),
		RuntimeSHA: config.RuntimeSHA, WorkflowSHA: config.WorkflowSHA, PackageAuthorityKeyID: config.PackageAuthorityKeyID,
		ReceiptAuthorityKeyID: config.ReceiptAuthorityKeyID,
	}, nil
}

func loadCanaryConfig(root string) (*Config, ed25519.PublicKey, error) {
	ownerUID, ownerGID := secureOwner(root)
	configRaw, err := secureRead(root, ConfigPath, 0o400, ownerUID, ownerGID, maximumConfigBytes)
	if err != nil {
		return nil, nil, fmt.Errorf("activation-canary-config-invalid")
	}
	defer zero(configRaw)
	signature, err := secureRead(root, ConfigSignaturePath, 0o444, ownerUID, ownerGID, ed25519.SignatureSize)
	if err != nil || len(signature) != ed25519.SignatureSize {
		zero(signature)
		return nil, nil, fmt.Errorf("activation-canary-config-invalid")
	}
	defer zero(signature)
	anchorRaw, err := secureRead(root, PackageAnchorPath, 0o444, ownerUID, ownerGID, 4096)
	if err != nil {
		return nil, nil, fmt.Errorf("activation-canary-config-invalid")
	}
	packagePublic, packageKeyID, err := parsePublicKey(anchorRaw)
	zero(anchorRaw)
	if err != nil || !ed25519.Verify(packagePublic, framed(configDomain, configRaw), signature) {
		zero(packagePublic)
		return nil, nil, fmt.Errorf("activation-canary-config-invalid")
	}
	zero(packagePublic)
	value, err := strictJSON(configRaw, maximumConfigBytes)
	if err != nil {
		return nil, nil, fmt.Errorf("activation-canary-config-invalid")
	}
	config, err := parseConfig(value, configRaw, packageKeyID, root)
	if err != nil {
		return nil, nil, fmt.Errorf("activation-canary-config-invalid")
	}
	receiptAnchorRaw, err := secureRead(root, ReceiptAnchorPath, 0o444, ownerUID, ownerGID, 4096)
	if err != nil {
		config.release()
		return nil, nil, fmt.Errorf("activation-canary-receipt-anchor-invalid")
	}
	receiptPublic, receiptKeyID, err := parsePublicKey(receiptAnchorRaw)
	zero(receiptAnchorRaw)
	if err != nil || receiptKeyID != config.ReceiptAuthorityKeyID {
		config.release()
		zero(receiptPublic)
		return nil, nil, fmt.Errorf("activation-canary-receipt-anchor-invalid")
	}
	return config, receiptPublic, nil
}

func writeActivationCanaryResult(root string, raw []byte) error {
	path := rooted(root, ActivationCanaryResultPath)
	parent := filepath.Dir(path)
	info, err := os.Lstat(parent)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm() != 0o750 {
		return fmt.Errorf("activation-canary-result-parent-invalid")
	}
	metadata, ok := info.Sys().(*syscall.Stat_t)
	if !ok || metadata.Uid != 0 || metadata.Gid != 0 || metadata.Nlink != 2 {
		return fmt.Errorf("activation-canary-result-parent-invalid")
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return fmt.Errorf("activation-canary-result-create-failed")
	}
	succeeded := false
	defer func() {
		_ = file.Close()
		if !succeeded {
			_ = os.Remove(path)
		}
	}()
	if err := file.Chown(0, 0); err != nil || file.Chmod(0o600) != nil || writeCanaryAll(file, raw) != nil || file.Sync() != nil || file.Close() != nil {
		return fmt.Errorf("activation-canary-result-write-failed")
	}
	succeeded = true
	directory, err := os.OpenFile(parent, os.O_RDONLY|syscall.O_CLOEXEC|syscall.O_DIRECTORY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return fmt.Errorf("activation-canary-result-sync-failed")
	}
	defer directory.Close()
	if err := directory.Sync(); err != nil {
		return fmt.Errorf("activation-canary-result-sync-failed")
	}
	return nil
}

func writeCanaryAll(writer io.Writer, raw []byte) error {
	for len(raw) > 0 {
		written, err := writer.Write(raw)
		if err != nil || written < 1 {
			return fmt.Errorf("activation-canary-short-write")
		}
		raw = raw[written:]
	}
	return nil
}

// Tighten the GitHub helper for all callers: even a client implementation
// that ignores CheckRedirect cannot substitute another final endpoint.
func githubResponseURLIsExact(response *http.Response, expected string) bool {
	return response != nil && response.Request != nil && response.Request.URL.String() == expected
}

func activationChallengeDigest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return "sha256:" + fmt.Sprintf("%x", sum[:])
}
