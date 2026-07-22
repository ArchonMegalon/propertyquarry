//go:build linux && amd64

package releasecontrol

import (
	"bytes"
	"crypto"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math/big"
	"net/netip"
	"os"
	"path/filepath"
	"strconv"
	"syscall"
	"testing"
	"time"
)

type phaseBTestFixture struct {
	now         time.Time
	rsaKey      *rsa.PrivateKey
	keyID       string
	keySet      *phaseBGitHubKeySet
	policy      *phaseBRootPolicy
	policyBytes []byte
	claims      map[string]any
	bearer      []byte
	signingKey  ed25519.PrivateKey
	journalPath string
	journalFD   int
}

func newPhaseBTestFixture(t *testing.T) *phaseBTestFixture {
	t.Helper()
	now := time.Unix(1_800_000_000, 0).UTC()
	rsaKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	keyID := "github-test-key-1"
	keySetBody := phaseBTestJWKS(t, keyID, &rsaKey.PublicKey, false)
	keySet := &phaseBGitHubKeySet{
		Issuer:       phaseBGitHubIssuer,
		DiscoveryURL: phaseBGitHubDiscoveryURL,
		JWKSURL:      "https://token.actions.githubusercontent.com/.well-known/jwks",
		CanonicalURL: "https://token.actions.githubusercontent.com/.well-known/jwks",
		Body:         keySetBody,
		BodyDigest:   sha256Digest(keySetBody),
	}
	identity := map[string]any{
		"audience":            "propertyquarry-release-control-v2",
		"repository":          "ArchonMegalon/propertyquarry",
		"ref":                 "refs/heads/main",
		"candidate_sha":       "1111111111111111111111111111111111111111",
		"workflow_ref":        "ArchonMegalon/propertyquarry/.github/workflows/smoke-runtime.yml@refs/heads/main",
		"workflow_sha":        "2222222222222222222222222222222222222222",
		"run_id":              "123456789",
		"run_attempt":         json.Number("1"),
		"job":                 "propertyquarry-release-v2",
		"environment":         "propertyquarry-production",
		"repository_id":       "200",
		"repository_owner_id": "100",
		"check_run_id":        "987654321",
		"subject":             "repo:ArchonMegalon@100/propertyquarry@200:environment:propertyquarry-production",
	}
	policyValue := map[string]any{
		"schema":                 "propertyquarry.release-root-policy.v2",
		"identity":               identity,
		"required_checks":        []any{"immutable-candidate", "ordinary-ci", "security-ci"},
		"max_request_ttl":        json.Number("600"),
		"max_preflight_validity": json.Number("300"),
		"decision_policy_digest": "sha256:" + string(bytes.Repeat([]byte{'3'}, 64)),
	}
	policyBytes, err := canonicalJSON(policyValue)
	if err != nil {
		t.Fatal(err)
	}
	role := installedRole{
		Contract: installedRoleContract{
			Role:    "root-policy",
			Path:    "/etc/propertyquarry-release-control/policy-v2.json",
			Mode:    0o640,
			Private: true,
		},
		Digest: sha256Digest(policyBytes),
		Size:   int64(len(policyBytes)),
	}
	policy, err := parseAuthenticatedPhaseBRootPolicy(policyBytes, role)
	if err != nil {
		t.Fatal(err)
	}
	claims := map[string]any{
		"iss":                 phaseBGitHubIssuer,
		"aud":                 "propertyquarry-release-control-v2",
		"sub":                 "repo:ArchonMegalon@100/propertyquarry@200:environment:propertyquarry-production",
		"repository":          "ArchonMegalon/propertyquarry",
		"repository_id":       "200",
		"repository_owner_id": "100",
		"ref":                 "refs/heads/main",
		"sha":                 "1111111111111111111111111111111111111111",
		"workflow_ref":        "ArchonMegalon/propertyquarry/.github/workflows/smoke-runtime.yml@refs/heads/main",
		"workflow_sha":        "2222222222222222222222222222222222222222",
		"run_id":              "123456789",
		"run_attempt":         "1",
		"environment":         "propertyquarry-production",
		"check_run_id":        "987654321",
		"jti":                 "oidc-token-id-1",
		"iat":                 json.Number(strconv.FormatInt(now.Unix()-10, 10)),
		"nbf":                 json.Number(strconv.FormatInt(now.Unix()-10, 10)),
		"exp":                 json.Number(strconv.FormatInt(now.Unix()+300, 10)),
		"actor":               "release-operator",
	}
	bearer := phaseBTestJWT(t, rsaKey, keyID, claims, nil)
	seed := make([]byte, ed25519.SeedSize)
	for index := range seed {
		seed[index] = byte(0x80 + index)
	}
	signingKey := ed25519.NewKeyFromSeed(seed)
	zero(seed)
	journalPath := t.TempDir()
	if err := os.Chmod(journalPath, 0o700); err != nil {
		t.Fatal(err)
	}
	journalFD, err := syscall.Open(journalPath, syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil {
		t.Fatal(err)
	}
	fixture := &phaseBTestFixture{
		now:         now,
		rsaKey:      rsaKey,
		keyID:       keyID,
		keySet:      keySet,
		policy:      policy,
		policyBytes: append([]byte(nil), policyBytes...),
		claims:      claims,
		bearer:      bearer,
		signingKey:  signingKey,
		journalPath: journalPath,
		journalFD:   journalFD,
	}
	t.Cleanup(func() {
		_ = syscall.Close(fixture.journalFD)
		fixture.keySet.release()
		fixture.policy.release()
		zero(fixture.policyBytes)
		zero(fixture.bearer)
		zero(fixture.signingKey)
	})
	return fixture
}

func (fixture *phaseBTestFixture) input() phaseBPreflightInput {
	return phaseBPreflightInput{
		Operation:           "release-preflight",
		RequestID:           "preflight-request-1",
		Nonce:               "oidc-token-id-1",
		ExpectedJournalHead: phaseBPreflightGenesisDigest(),
		Bearer:              fixture.bearer,
		KeySet:              fixture.keySet,
		Policy:              fixture.policy,
		EvaluatedAt:         fixture.now,
	}
}

func TestPhaseBPreflightProducesSignedNonAuthorizingEvidenceAndExactReplay(t *testing.T) {
	fixture := newPhaseBTestFixture(t)
	input := fixture.input()
	evidence, err := issuePhaseBNonAuthorizingPreflight(fixture.journalFD, input, fixture.signingKey)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(evidence)
	if bytes.Contains(evidence, fixture.bearer) {
		t.Fatal("OIDC bearer leaked into persisted evidence")
	}
	verified, err := verifyPhaseBPreflightEvidence(evidence, fixture.signingKey.Public().(ed25519.PublicKey))
	if err != nil {
		t.Fatal(err)
	}
	defer verified.release()
	if verified.Sequence != 1 || verified.PredecessorDigest != phaseBPreflightGenesisDigest() ||
		verified.RequestID != input.RequestID || verified.Nonce != input.Nonce {
		t.Fatalf("unexpected persisted binding: %#v", verified)
	}
	payload := phaseBTestEvidencePayload(t, evidence)
	if !exactBoolEquals(payload["github_oidc_signature_verified"], true) ||
		!exactBoolEquals(payload["github_oidc_transport_binding_verified"], false) ||
		!exactBoolEquals(payload["candidate_binding_verified"], true) ||
		!exactBoolEquals(payload["workflow_binding_verified"], true) ||
		!exactBoolEquals(payload["environment_binding_verified"], true) ||
		!exactBoolEquals(payload["root_policy_binding_verified"], true) ||
		!exactBoolEquals(payload["job_name_binding_verified"], false) ||
		!exactBoolEquals(payload["authoritative"], false) ||
		!exactBoolEquals(payload["ready"], false) ||
		!exactBoolEquals(payload["production_ready"], false) ||
		!exactBoolEquals(payload["performs_release_effects"], false) ||
		!exactBoolEquals(payload["release_effects_authorized"], false) {
		t.Fatal("evidence overstated phase-B authority")
	}
	identity := payload["github_identity"].(map[string]any)
	if identity["candidate_sha"] != fixture.policy.Identity.CandidateSHA ||
		identity["workflow_sha"] != fixture.policy.Identity.WorkflowSHA ||
		identity["check_run_id"] != "987654321" || payload["policy_job"] != "propertyquarry-release-v2" {
		t.Fatal("evidence omitted candidate, workflow, or job-correlation binding")
	}

	// Exact authenticated replay returns the original signed bytes even when
	// the caller's formerly-current journal predecessor is now stale.
	input.EvaluatedAt = fixture.now.Add(time.Second)
	replayed, err := issuePhaseBNonAuthorizingPreflight(fixture.journalFD, input, fixture.signingKey)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(replayed)
	if !bytes.Equal(replayed, evidence) {
		t.Fatal("exact replay did not return byte-identical evidence")
	}

	// The same result survives an authority-process restart and index rebuild.
	if err := syscall.Close(fixture.journalFD); err != nil {
		t.Fatal(err)
	}
	fixture.journalFD = -1
	reopened, err := syscall.Open(fixture.journalPath, syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil {
		t.Fatal(err)
	}
	fixture.journalFD = reopened
	restartedReplay, err := issuePhaseBNonAuthorizingPreflight(reopened, input, fixture.signingKey)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(restartedReplay)
	if !bytes.Equal(restartedReplay, evidence) {
		t.Fatal("restart replay changed signed evidence")
	}
}

func TestPhaseBPreflightJournalCASNonceAndTamperAreFailClosed(t *testing.T) {
	fixture := newPhaseBTestFixture(t)
	input := fixture.input()
	wrong := input
	wrong.ExpectedJournalHead = "sha256:" + string(bytes.Repeat([]byte{'9'}, 64))
	if _, err := issuePhaseBNonAuthorizingPreflight(fixture.journalFD, wrong, fixture.signingKey); err == nil {
		t.Fatal("wrong journal CAS predecessor was accepted")
	}
	evidence, err := issuePhaseBNonAuthorizingPreflight(fixture.journalFD, input, fixture.signingKey)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(evidence)
	first, err := verifyPhaseBPreflightEvidence(evidence, fixture.signingKey.Public().(ed25519.PublicKey))
	if err != nil {
		t.Fatal(err)
	}
	defer first.release()

	secondClaims := clonePhaseBClaims(fixture.claims)
	secondClaims["jti"] = "oidc-token-id-2"
	secondBearer := phaseBTestJWT(t, fixture.rsaKey, fixture.keyID, secondClaims, nil)
	defer zero(secondBearer)
	second := input
	second.RequestID = "preflight-request-2"
	second.Nonce = "oidc-token-id-2"
	second.Bearer = secondBearer
	second.ExpectedJournalHead = first.EvidenceDigest
	secondEvidence, err := issuePhaseBNonAuthorizingPreflight(fixture.journalFD, second, fixture.signingKey)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(secondEvidence)

	nonceConflict := second
	nonceConflict.RequestID = "preflight-request-3"
	if _, err := issuePhaseBNonAuthorizingPreflight(fixture.journalFD, nonceConflict, fixture.signingKey); err == nil {
		t.Fatal("nonce reuse with a different request ID was accepted")
	}

	path := filepath.Join(fixture.journalPath, phaseBPreflightEventName(1))
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	raw[len(raw)/2] ^= 1
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	zero(raw)
	if _, err := issuePhaseBNonAuthorizingPreflight(fixture.journalFD, second, fixture.signingKey); err == nil {
		t.Fatal("tampered durable journal was accepted")
	}
}

func TestPhaseBPreflightJournalBoundsCrashLeftovers(t *testing.T) {
	fixture := newPhaseBTestFixture(t)
	for index := 0; index <= phaseBPreflightMaximumPending; index++ {
		name := fmt.Sprintf(".phase-b-preflight-pending-%064x.tmp", index)
		if err := os.WriteFile(filepath.Join(fixture.journalPath, name), nil, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := issuePhaseBNonAuthorizingPreflight(
		fixture.journalFD,
		fixture.input(),
		fixture.signingKey,
	); err == nil {
		t.Fatal("unbounded pending journal crash leftovers were accepted")
	}
}

func TestPhaseBPreflightRecoversDurablePendingEvent(t *testing.T) {
	fixture := newPhaseBTestFixture(t)
	sourcePath := t.TempDir()
	if err := os.Chmod(sourcePath, 0o700); err != nil {
		t.Fatal(err)
	}
	sourceFD, err := syscall.Open(
		sourcePath,
		syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW,
		0,
	)
	if err != nil {
		t.Fatal(err)
	}
	expected, err := issuePhaseBNonAuthorizingPreflight(sourceFD, fixture.input(), fixture.signingKey)
	if closeErr := syscall.Close(sourceFD); err == nil && closeErr != nil {
		err = closeErr
	}
	if err != nil {
		t.Fatal(err)
	}
	defer zero(expected)
	pendingName := ".phase-b-preflight-pending-" + string(bytes.Repeat([]byte{'a'}, 64)) + ".tmp"
	if err := os.WriteFile(filepath.Join(fixture.journalPath, pendingName), expected, 0o600); err != nil {
		t.Fatal(err)
	}
	recovered, err := issuePhaseBNonAuthorizingPreflight(
		fixture.journalFD,
		fixture.input(),
		fixture.signingKey,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(recovered)
	if !bytes.Equal(recovered, expected) {
		t.Fatal("durable pending event was not rolled forward byte-identically")
	}
	if _, err := os.Stat(filepath.Join(fixture.journalPath, pendingName)); !os.IsNotExist(err) {
		t.Fatal("recovered pending event name remains")
	}
	if _, err := os.Stat(filepath.Join(fixture.journalPath, phaseBPreflightEventName(1))); err != nil {
		t.Fatal("recovered pending event was not published")
	}
}

func TestPhaseBPreflightRemovesPartialPendingBeforeAdmission(t *testing.T) {
	fixture := newPhaseBTestFixture(t)
	pendingName := ".phase-b-preflight-pending-" + string(bytes.Repeat([]byte{'b'}, 64)) + ".tmp"
	if err := os.WriteFile(filepath.Join(fixture.journalPath, pendingName), []byte{'{'}, 0o600); err != nil {
		t.Fatal(err)
	}
	evidence, err := issuePhaseBNonAuthorizingPreflight(
		fixture.journalFD,
		fixture.input(),
		fixture.signingKey,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(evidence)
	if _, err := os.Stat(filepath.Join(fixture.journalPath, pendingName)); !os.IsNotExist(err) {
		t.Fatal("partial pending event was not durably removed")
	}
}

func TestPhaseBPreflightRejectsSignedDuplicateReplayDuringRebuild(t *testing.T) {
	for _, location := range []string{"final", "pending"} {
		t.Run(location, func(t *testing.T) {
			fixture := newPhaseBTestFixture(t)
			firstWire, err := issuePhaseBNonAuthorizingPreflight(
				fixture.journalFD,
				fixture.input(),
				fixture.signingKey,
			)
			if err != nil {
				t.Fatal(err)
			}
			defer zero(firstWire)
			first, err := verifyPhaseBPreflightEvidence(
				firstWire,
				fixture.signingKey.Public().(ed25519.PublicKey),
			)
			if err != nil {
				t.Fatal(err)
			}
			defer first.release()
			payload := phaseBTestEvidencePayload(t, firstWire)
			payload["journal_sequence"] = json.Number("2")
			payload["journal_predecessor_digest"] = first.EvidenceDigest
			payloadBytes, err := canonicalJSON(payload)
			if err != nil {
				t.Fatal(err)
			}
			duplicate, duplicateCanonical, err := signPhaseBPreflightPayload(payloadBytes, fixture.signingKey)
			zero(payloadBytes)
			zero(duplicateCanonical)
			if err != nil {
				t.Fatal(err)
			}
			defer zero(duplicate)
			name := phaseBPreflightEventName(2)
			if location == "pending" {
				name = ".phase-b-preflight-pending-" + string(bytes.Repeat([]byte{'c'}, 64)) + ".tmp"
			}
			if err := os.WriteFile(filepath.Join(fixture.journalPath, name), duplicate, 0o600); err != nil {
				t.Fatal(err)
			}
			if _, err := issuePhaseBNonAuthorizingPreflight(
				fixture.journalFD,
				fixture.input(),
				fixture.signingKey,
			); err == nil {
				t.Fatal("signed duplicate request/nonce replay was accepted")
			}
		})
	}
}

func TestPhaseBGitHubOIDCRejectsSignatureIdentityTimeAndKeySubstitution(t *testing.T) {
	fixture := newPhaseBTestFixture(t)
	tests := []struct {
		name   string
		mutate func(map[string]any)
		now    time.Time
	}{
		{"candidate", func(value map[string]any) { value["sha"] = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }, fixture.now},
		{"workflow", func(value map[string]any) { value["workflow_sha"] = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" }, fixture.now},
		{"environment", func(value map[string]any) { value["environment"] = "staging" }, fixture.now},
		{"audience", func(value map[string]any) { value["aud"] = "another-audience" }, fixture.now},
		{"repository-id", func(value map[string]any) { value["repository_id"] = "201" }, fixture.now},
		{"repository-owner-id", func(value map[string]any) { value["repository_owner_id"] = "101" }, fixture.now},
		{"check-run-id", func(value map[string]any) { value["check_run_id"] = "987654322" }, fixture.now},
		{"subject-repository", func(value map[string]any) {
			value["sub"] = "repo:Attacker/property:environment:propertyquarry-production"
		}, fixture.now},
		{"subject-extra-context", func(value map[string]any) {
			value["sub"] = "repo:ArchonMegalon@100/propertyquarry@200:attacker:value:environment:propertyquarry-production"
		}, fixture.now},
		{"future", func(value map[string]any) {}, fixture.now.Add(-2 * time.Minute)},
		{"expired", func(value map[string]any) {}, fixture.now.Add(10 * time.Minute)},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			claims := clonePhaseBClaims(fixture.claims)
			test.mutate(claims)
			token := phaseBTestJWT(t, fixture.rsaKey, fixture.keyID, claims, nil)
			defer zero(token)
			if _, err := verifyPhaseBGitHubOIDC(token, fixture.keySet, fixture.policy, test.now); err == nil {
				t.Fatal("substituted OIDC token was accepted")
			}
		})
	}

	tampered := append([]byte(nil), fixture.bearer...)
	tampered[len(tampered)-1] ^= 1
	if _, err := verifyPhaseBGitHubOIDC(tampered, fixture.keySet, fixture.policy, fixture.now); err == nil {
		t.Fatal("tampered JWS signature was accepted")
	}
	zero(tampered)

	badHeader := map[string]any{"alg": "HS256", "kid": fixture.keyID, "typ": "JWT"}
	wrongAlgorithm := phaseBTestJWT(t, fixture.rsaKey, fixture.keyID, fixture.claims, badHeader)
	if _, err := verifyPhaseBGitHubOIDC(wrongAlgorithm, fixture.keySet, fixture.policy, fixture.now); err == nil {
		t.Fatal("non-RS256 header was accepted")
	}
	zero(wrongAlgorithm)

	duplicateBody := phaseBTestJWKS(t, fixture.keyID, &fixture.rsaKey.PublicKey, true)
	duplicateSet := *fixture.keySet
	duplicateSet.Body = duplicateBody
	duplicateSet.BodyDigest = sha256Digest(duplicateBody)
	if _, err := verifyPhaseBGitHubOIDC(fixture.bearer, &duplicateSet, fixture.policy, fixture.now); err == nil {
		t.Fatal("duplicate JWKS key ID was accepted")
	}
	zero(duplicateBody)
}

func TestPhaseBRootPolicyMustBeCanonicalAndPackageAuthenticated(t *testing.T) {
	fixture := newPhaseBTestFixture(t)
	role := installedRole{
		Contract: installedRoleContract{
			Role:    "root-policy",
			Path:    "/etc/propertyquarry-release-control/policy-v2.json",
			Mode:    0o640,
			Private: true,
		},
		Digest: sha256Digest(fixture.policyBytes),
		Size:   int64(len(fixture.policyBytes)),
	}
	withLF := append(append([]byte(nil), fixture.policyBytes...), '\n')
	role.Digest = sha256Digest(withLF)
	role.Size = int64(len(withLF))
	if _, err := parseAuthenticatedPhaseBRootPolicy(withLF, role); err == nil {
		t.Fatal("noncanonical package-authenticated root policy was accepted")
	}
	zero(withLF)
	role.Digest = "sha256:" + string(bytes.Repeat([]byte{'f'}, 64))
	role.Size = int64(len(fixture.policyBytes))
	if _, err := parseAuthenticatedPhaseBRootPolicy(fixture.policyBytes, role); err == nil {
		t.Fatal("root policy outside the authenticated role digest was accepted")
	}
	role.Digest = sha256Digest(fixture.policyBytes)
	role.Contract.Private = false
	if _, err := parseAuthenticatedPhaseBRootPolicy(fixture.policyBytes, role); err == nil {
		t.Fatal("root policy with a fabricated non-private role contract was accepted")
	}
}

func TestPhaseBNetworkPolicyPinsGitHubAndRejectsNonPublicAddresses(t *testing.T) {
	for _, raw := range []string{
		"http://token.actions.githubusercontent.com/.well-known/jwks",
		"https://example.com/.well-known/jwks",
		"https://token.actions.githubusercontent.com/.well-known/jwks?next=x",
		"https://user@token.actions.githubusercontent.com/.well-known/jwks",
		"https://token.actions.githubusercontent.com/",
	} {
		if err := validatePhaseBGitHubHTTPSURL(raw); err == nil {
			t.Fatalf("unsafe GitHub authority URL accepted: %s", raw)
		}
	}
	for _, raw := range []string{"127.0.0.1", "10.0.0.1", "169.254.1.1", "192.0.2.1", "192.88.99.1", "198.18.0.1", "2001:db8::1", "::1", "fc00::1"} {
		if phaseBPublicAddress(netip.MustParseAddr(raw)) {
			t.Fatalf("non-public address accepted: %s", raw)
		}
	}
	for _, raw := range []string{"1.1.1.1", "2606:4700:4700::1111"} {
		if !phaseBPublicAddress(netip.MustParseAddr(raw)) {
			t.Fatalf("public address rejected: %s", raw)
		}
	}
}

func TestPhaseBPreflightEvidenceTamperAndKeySubstitutionFail(t *testing.T) {
	fixture := newPhaseBTestFixture(t)
	evidence, err := issuePhaseBNonAuthorizingPreflight(fixture.journalFD, fixture.input(), fixture.signingKey)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(evidence)
	tampered := append([]byte(nil), evidence...)
	tampered[len(tampered)/2] ^= 1
	if _, err := verifyPhaseBPreflightEvidence(tampered, fixture.signingKey.Public().(ed25519.PublicKey)); err == nil {
		t.Fatal("tampered evidence was accepted")
	}
	zero(tampered)
	otherSeed := bytes.Repeat([]byte{0x42}, ed25519.SeedSize)
	other := ed25519.NewKeyFromSeed(otherSeed)
	zero(otherSeed)
	defer zero(other)
	if _, err := verifyPhaseBPreflightEvidence(evidence, other.Public().(ed25519.PublicKey)); err == nil {
		t.Fatal("evidence key substitution was accepted")
	}

	payload := phaseBTestEvidencePayload(t, evidence)
	payload["release_authority"] = true
	smuggledPayload, err := canonicalJSON(payload)
	if err != nil {
		t.Fatal(err)
	}
	smuggled, smuggledCanonical, err := signPhaseBPreflightPayload(smuggledPayload, fixture.signingKey)
	zero(smuggledPayload)
	zero(smuggledCanonical)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(smuggled)
	if _, err := verifyPhaseBPreflightEvidence(smuggled, fixture.signingKey.Public().(ed25519.PublicKey)); err == nil {
		t.Fatal("signed evidence with an authority-smuggling field was accepted")
	}

	temporalPayload := phaseBTestEvidencePayload(t, evidence)
	identity := temporalPayload["github_identity"].(map[string]any)
	expiresAt, err := strconv.ParseInt(string(identity["expires_at"].(json.Number)), 10, 64)
	if err != nil {
		t.Fatal(err)
	}
	temporalPayload["valid_until"] = json.Number(strconv.FormatInt(expiresAt+1, 10))
	temporalBytes, err := canonicalJSON(temporalPayload)
	if err != nil {
		t.Fatal(err)
	}
	temporal, temporalCanonical, err := signPhaseBPreflightPayload(temporalBytes, fixture.signingKey)
	zero(temporalBytes)
	zero(temporalCanonical)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(temporal)
	if _, err := verifyPhaseBPreflightEvidence(temporal, fixture.signingKey.Public().(ed25519.PublicKey)); err == nil {
		t.Fatal("signed evidence extending beyond the OIDC expiry was accepted")
	}

	inconsistent := append(ed25519.PrivateKey(nil), fixture.signingKey...)
	inconsistent[len(inconsistent)-1] ^= 1
	defer zero(inconsistent)
	if _, err := issuePhaseBNonAuthorizingPreflight(
		fixture.journalFD,
		fixture.input(),
		inconsistent,
	); err == nil {
		t.Fatal("inconsistent Ed25519 private key was accepted")
	}
}

func phaseBTestJWT(
	t *testing.T,
	key *rsa.PrivateKey,
	keyID string,
	claims map[string]any,
	headerOverride map[string]any,
) []byte {
	t.Helper()
	header := headerOverride
	if header == nil {
		header = map[string]any{"alg": "RS256", "kid": keyID, "typ": "JWT"}
	}
	headerBytes, err := canonicalJSON(header)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(headerBytes)
	claimsBytes, err := canonicalJSON(claims)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(claimsBytes)
	signed := []byte(base64.RawURLEncoding.EncodeToString(headerBytes) + "." + base64.RawURLEncoding.EncodeToString(claimsBytes))
	digest := sha256.Sum256(signed)
	signature, err := rsa.SignPKCS1v15(rand.Reader, key, crypto.SHA256, digest[:])
	if err != nil {
		t.Fatal(err)
	}
	token := append(append(signed, '.'), []byte(base64.RawURLEncoding.EncodeToString(signature))...)
	zero(signature)
	return token
}

func phaseBTestJWKS(t *testing.T, keyID string, key *rsa.PublicKey, duplicate bool) []byte {
	t.Helper()
	exponent := big.NewInt(int64(key.E)).Bytes()
	jwk := map[string]any{
		"kty": "RSA",
		"use": "sig",
		"alg": "RS256",
		"kid": keyID,
		"n":   base64.RawURLEncoding.EncodeToString(key.N.Bytes()),
		"e":   base64.RawURLEncoding.EncodeToString(exponent),
	}
	keys := []any{jwk}
	if duplicate {
		keys = append(keys, jwk)
	}
	body, err := canonicalJSON(map[string]any{"keys": keys})
	if err != nil {
		t.Fatal(err)
	}
	return body
}

func phaseBTestEvidencePayload(t *testing.T, wire []byte) map[string]any {
	t.Helper()
	value, err := decodeStrictJSON(wire[:len(wire)-1])
	if err != nil {
		t.Fatal(err)
	}
	outer := value.(map[string]any)
	return outer["payload"].(map[string]any)
}

func clonePhaseBClaims(source map[string]any) map[string]any {
	result := make(map[string]any, len(source))
	for key, value := range source {
		result[key] = value
	}
	return result
}
