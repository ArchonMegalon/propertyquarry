//go:build linux

package releasecontrol

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"os"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

func authenticatedRequestFixture(
	t *testing.T,
) (*quarantinedRequest, authenticatedRootPolicy, ed25519.PublicKey, string) {
	t.Helper()
	unsigned, err := parseQuarantinedRequest([]byte(crossLanguageGoldenRequest))
	if err != nil {
		t.Fatal(err)
	}
	defer unsigned.release()

	seed := bytes.Repeat([]byte{0x41}, ed25519.SeedSize)
	privateKey := ed25519.NewKeyFromSeed(seed)
	zero(seed)
	publicKey := privateKey.Public().(ed25519.PublicKey)
	der, err := x509.MarshalPKIXPublicKey(publicKey)
	if err != nil {
		t.Fatal(err)
	}
	anchor := pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der})
	parsedKey, keyID, err := parseEd25519PublicAnchor(anchor)
	zero(anchor)
	zero(der)
	if err != nil {
		t.Fatal(err)
	}

	message, err := requestSignatureMessage(
		unsigned.signaturePayload,
		unsigned.canonicalEnvelope,
	)
	if err != nil {
		t.Fatal(err)
	}
	signature := ed25519.Sign(privateKey, message)
	zero(message)
	zero(privateKey)

	value, err := decodeStrictJSON([]byte(crossLanguageGoldenRequest))
	if err != nil {
		t.Fatal(err)
	}
	outer := value.(map[string]any)
	outer["request_signature"] = requestSignaturePrefix + "/" + keyID + "/" +
		base64.RawURLEncoding.EncodeToString(signature)
	zero(signature)
	raw, err := canonicalJSON(outer)
	if err != nil {
		t.Fatal(err)
	}
	request, err := parseQuarantinedRequest(raw)
	zero(raw)
	if err != nil {
		t.Fatal(err)
	}
	policy := authenticatedRootPolicy{
		Identity:      request.envelope.Identity,
		MaxRequestTTL: 900,
	}
	return request, policy, parsedKey, keyID
}

func authenticatedPolicyFixture(
	t *testing.T,
	identity quarantinedIdentity,
) []byte {
	t.Helper()
	identityValue, err := decodeStrictJSON([]byte(`{
		"audience":"` + identity.Audience + `",
		"repository":"` + identity.Repository + `",
		"ref":"` + identity.Ref + `",
		"candidate_sha":"` + identity.CandidateSHA + `",
		"workflow_ref":"` + identity.WorkflowRef + `",
		"workflow_sha":"` + identity.WorkflowSHA + `",
		"run_id":"` + identity.RunID + `",
		"run_attempt":` + strconv.FormatInt(identity.RunAttempt, 10) + `,
		"job":"` + identity.Job + `",
		"environment":"` + identity.Environment + `"
	}`))
	if err != nil {
		t.Fatal(err)
	}
	raw, err := canonicalJSON(map[string]any{
		"schema":                 "propertyquarry.release-root-policy.v2",
		"identity":               identityValue,
		"required_checks":        []any{"gold-launch-evidence"},
		"decision_policy_digest": "sha256:" + strings.Repeat("a", 64),
		"max_request_ttl":        json.Number("900"),
		"max_preflight_validity": json.Number("3600"),
	})
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestAuthenticateQuarantinedRequestEstablishesAuthenticationOnlyAfterAllBindings(t *testing.T) {
	request, policy, publicKey, keyID := authenticatedRequestFixture(t)
	defer request.release()
	if err := authenticateQuarantinedRequest(
		request,
		policy,
		publicKey,
		keyID,
		time.Unix(1050, 0),
	); err != nil {
		t.Fatal(err)
	}
	if !request.authenticationEstablished {
		t.Fatal("valid signed request did not establish authentication")
	}
	if err := authenticateQuarantinedRequest(
		request,
		policy,
		publicKey,
		keyID,
		time.Unix(1050, 0),
	); err == nil {
		t.Fatal("already-authenticated request was accepted again")
	}
}

func TestAuthenticateQuarantinedRequestRejectsEveryUnboundDimension(t *testing.T) {
	tests := map[string]func(
		*quarantinedRequest,
		*authenticatedRootPolicy,
		*ed25519.PublicKey,
		*string,
		*time.Time,
	){
		"envelope-digest": func(
			request *quarantinedRequest,
			_ *authenticatedRootPolicy,
			_ *ed25519.PublicKey,
			_ *string,
			_ *time.Time,
		) {
			request.envelopeDigestMatches = false
		},
		"identity": func(
			_ *quarantinedRequest,
			policy *authenticatedRootPolicy,
			_ *ed25519.PublicKey,
			_ *string,
			_ *time.Time,
		) {
			policy.Identity.Job = "different-job"
		},
		"ttl": func(
			_ *quarantinedRequest,
			policy *authenticatedRootPolicy,
			_ *ed25519.PublicKey,
			_ *string,
			_ *time.Time,
		) {
			policy.MaxRequestTTL = 99
		},
		"future": func(
			_ *quarantinedRequest,
			_ *authenticatedRootPolicy,
			_ *ed25519.PublicKey,
			_ *string,
			now *time.Time,
		) {
			*now = time.Unix(999, 0)
		},
		"expired": func(
			_ *quarantinedRequest,
			_ *authenticatedRootPolicy,
			_ *ed25519.PublicKey,
			_ *string,
			now *time.Time,
		) {
			*now = time.Unix(1100, 0)
		},
		"key-id": func(
			_ *quarantinedRequest,
			_ *authenticatedRootPolicy,
			_ *ed25519.PublicKey,
			keyID *string,
			_ *time.Time,
		) {
			*keyID = "sha256:" + strings.Repeat("0", 64)
		},
		"public-key": func(
			_ *quarantinedRequest,
			_ *authenticatedRootPolicy,
			publicKey *ed25519.PublicKey,
			_ *string,
			_ *time.Time,
		) {
			(*publicKey)[0] ^= 0xff
		},
		"signature": func(
			request *quarantinedRequest,
			_ *authenticatedRootPolicy,
			_ *ed25519.PublicKey,
			_ *string,
			_ *time.Time,
		) {
			parts := strings.Split(request.requestSignature, "/")
			signature, err := base64.RawURLEncoding.DecodeString(parts[2])
			if err != nil {
				t.Fatal(err)
			}
			signature[0] ^= 0xff
			request.requestSignature = parts[0] + "/" + parts[1] + "/" +
				base64.RawURLEncoding.EncodeToString(signature)
			zero(signature)
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			request, policy, publicKey, keyID := authenticatedRequestFixture(t)
			defer request.release()
			now := time.Unix(1050, 0)
			mutate(request, &policy, &publicKey, &keyID, &now)
			if err := authenticateQuarantinedRequest(
				request,
				policy,
				publicKey,
				keyID,
				now,
			); err == nil {
				t.Fatal("unbound request dimension was accepted")
			}
			if request.authenticationEstablished {
				t.Fatal("failed request retained authenticated state")
			}
		})
	}
}

func TestAuthenticatedRootPolicyIsClosedCanonicalAndExact(t *testing.T) {
	request, _, _, _ := authenticatedRequestFixture(t)
	defer request.release()
	raw := authenticatedPolicyFixture(t, request.envelope.Identity)
	defer zero(raw)
	policy, err := parseAuthenticatedRootPolicy(raw)
	if err != nil {
		t.Fatal(err)
	}
	if policy.Schema != authenticatedRootPolicySchema ||
		policy.Identity != request.envelope.Identity ||
		len(policy.RequiredChecks) != 1 ||
		policy.RequiredChecks[0] != "gold-launch-evidence" ||
		policy.DecisionPolicyDigest != "sha256:"+strings.Repeat("a", 64) ||
		policy.MaxRequestTTL != 900 ||
		policy.MaxPreflightValidity != 3600 {
		t.Fatalf("root policy changed: %#v", policy)
	}

	nonCanonical := append([]byte(" "), raw...)
	if _, err := parseAuthenticatedRootPolicy(nonCanonical); err == nil {
		t.Fatal("noncanonical root policy accepted")
	}
	zero(nonCanonical)

	duplicateChecks := bytes.Replace(
		raw,
		[]byte(`["gold-launch-evidence"]`),
		[]byte(`["gold-launch-evidence","gold-launch-evidence"]`),
		1,
	)
	if _, err := parseAuthenticatedRootPolicy(duplicateChecks); err == nil {
		t.Fatal("duplicate required checks accepted")
	}
	zero(duplicateChecks)

	unknown := bytes.Replace(
		raw,
		[]byte(`{"decision_policy_digest":`),
		[]byte(`{"unknown":false,"decision_policy_digest":`),
		1,
	)
	if _, err := parseAuthenticatedRootPolicy(unknown); err == nil {
		t.Fatal("unknown root policy key accepted")
	}
	zero(unknown)

	excessiveTTL := bytes.Replace(
		raw,
		[]byte(`"max_request_ttl":900`),
		[]byte(`"max_request_ttl":901`),
		1,
	)
	if _, err := parseAuthenticatedRootPolicy(excessiveTTL); err == nil {
		t.Fatal("root policy request ttl exceeded native ceiling")
	}
	zero(excessiveTTL)

	excessiveValidity := bytes.Replace(
		raw,
		[]byte(`"max_preflight_validity":3600`),
		[]byte(`"max_preflight_validity":3601`),
		1,
	)
	if _, err := parseAuthenticatedRootPolicy(excessiveValidity); err == nil {
		t.Fatal("root policy preflight validity exceeded native ceiling")
	}
	zero(excessiveValidity)
}

func TestControllerTransactionSnapshotRetainsDetachedAuthenticatedBindings(t *testing.T) {
	fixture := newInstalledAuthorityFixture(t)
	raw := signedInstalledFixtureRequest(t, fixture)
	defer zero(raw)
	supervisorRequest, err := parseQuarantinedRequest(raw)
	if err != nil {
		t.Fatal(err)
	}
	defer supervisorRequest.release()
	verification, err := validateInstalledLocalAuthority(Supervisor, fixture.paths)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	authenticated, err := authenticateInstalledRequestBindings(
		fixture.paths,
		verification,
		supervisorRequest,
		now,
	)
	if err != nil {
		t.Fatal(err)
	}
	stateGeneration, err := validateInstalledAuthorityState(fixture.paths)
	if err != nil {
		t.Fatal(err)
	}
	if err := claimInstalledRequestReplay(
		fixture.paths,
		supervisorRequest,
		authenticated.rootPolicyDigest,
		verification,
		stateGeneration,
	); err != nil {
		t.Fatal(err)
	}

	policyRaw, err := readAuthenticatedInstalledRole(
		fixture.paths,
		verification,
		"root-policy",
		maxRootPolicyBytes,
	)
	if err != nil {
		t.Fatal(err)
	}
	expectedPolicy, err := parseAuthenticatedRootPolicy(policyRaw)
	if err != nil {
		zero(policyRaw)
		t.Fatal(err)
	}
	expectedRootPolicyDigest := sha256Digest(policyRaw)
	zero(policyRaw)

	expectedOperation := supervisorRequest.envelope.Operation
	expectedRequestDigest := supervisorRequest.rawBodyDigest
	expectedCanonicalRequestDigest := supervisorRequest.canonicalBodyDigest
	expectedEnvelopeDigest := supervisorRequest.canonicalEnvelopeDigest
	expectedSignaturePayloadDigest := supervisorRequest.signaturePayloadDigest
	expectedRequestSignatureDigest := sha256Digest(
		[]byte(supervisorRequest.requestSignature),
	)
	expectedRequestID := supervisorRequest.envelope.RequestID
	expectedNonce := supervisorRequest.envelope.Nonce
	expectedIssuedAt := supervisorRequest.envelope.IssuedAt
	expectedExpiresAt := supervisorRequest.envelope.ExpiresAt
	expectedRequestKeyID := supervisorRequest.authenticatedKeyID

	var pipeFDs [2]int
	if err := syscall.Pipe2(pipeFDs[:], syscall.O_CLOEXEC); err != nil {
		t.Fatal(err)
	}
	writer := os.NewFile(uintptr(pipeFDs[1]), "snapshot-request-writer")
	if writer == nil {
		_ = syscall.Close(pipeFDs[0])
		_ = syscall.Close(pipeFDs[1])
		t.Fatal("snapshot request writer unavailable")
	}
	writeResult := make(chan error, 1)
	go func() {
		writeResult <- writeAuthenticatedRequestPipe(
			writer,
			raw,
			installedChildDeadline,
		)
	}()
	snapshot, err := authenticateControllerTransactionWithPaths(
		pipeFDs[0],
		expectedOperation,
		"local-"+expectedRequestDigest[len("sha256:"):],
		expectedRequestDigest,
		now,
		fixture.paths,
	)
	if err != nil {
		t.Fatal(err)
	}
	if err := <-writeResult; err != nil {
		t.Fatal(err)
	}
	if snapshot == nil {
		t.Fatal("authenticated controller transaction snapshot missing")
	}

	zero(raw)
	supervisorRequest.release()
	if snapshot.operation != expectedOperation ||
		snapshot.requestDigest != expectedRequestDigest ||
		snapshot.canonicalRequestDigest != expectedCanonicalRequestDigest ||
		snapshot.envelopeDigest != expectedEnvelopeDigest ||
		snapshot.signaturePayloadDigest != expectedSignaturePayloadDigest ||
		snapshot.requestSignatureDigest != expectedRequestSignatureDigest ||
		snapshot.requestID != expectedRequestID ||
		snapshot.nonce != expectedNonce ||
		snapshot.issuedAt != expectedIssuedAt ||
		snapshot.expiresAt != expectedExpiresAt ||
		snapshot.requestKeyID != expectedRequestKeyID ||
		snapshot.rootPolicyDigest != expectedRootPolicyDigest {
		t.Fatalf("authenticated transaction bindings changed: %#v", snapshot)
	}
	if snapshot.rootPolicy.schema != expectedPolicy.Schema ||
		snapshot.rootPolicy.identity != expectedPolicy.Identity ||
		snapshot.rootPolicy.decisionPolicyDigest != expectedPolicy.DecisionPolicyDigest ||
		snapshot.rootPolicy.maxRequestTTL != expectedPolicy.MaxRequestTTL ||
		snapshot.rootPolicy.maxPreflightValidity != expectedPolicy.MaxPreflightValidity {
		t.Fatalf("authenticated root policy snapshot changed: %#v", snapshot.rootPolicy)
	}
	checks := snapshot.rootPolicy.requiredCheckIDs()
	if len(checks) != 1 || checks[0] != "gold-launch-evidence" {
		t.Fatalf("authenticated root policy checks changed: %#v", checks)
	}
	checks[0] = "mutated-caller-copy"
	if snapshot.rootPolicy.requiredCheckIDs()[0] != "gold-launch-evidence" {
		t.Fatal("authenticated root policy checks were not defensively copied")
	}
	if snapshot.installedAuthority.authenticationDigest != verification.AuthenticationDigest ||
		snapshot.installedAuthority.payloadTreeDigest != verification.PayloadTreeDigest ||
		snapshot.installedAuthority.authorityKeyID != verification.AuthorityKeyID ||
		snapshot.installedAuthority.manifestDigest != verification.ManifestDigest ||
		snapshot.installedAuthority.nativeBuildDigest != verification.NativeBuildDigest {
		t.Fatalf(
			"installed authority snapshot changed: %#v",
			snapshot.installedAuthority,
		)
	}
	currentStateGeneration, err := validateInstalledAuthorityState(fixture.paths)
	if err != nil {
		t.Fatal(err)
	}
	if !sameInstalledDirectoryObject(
		snapshot.installedAuthority.stateGeneration,
		currentStateGeneration,
	) {
		t.Fatal("installed authority state generation was not retained")
	}
}

func TestRequestSignatureMessageIsDomainAndLengthSeparated(t *testing.T) {
	left, err := requestSignatureMessage([]byte("ab"), []byte("c"))
	if err != nil {
		t.Fatal(err)
	}
	defer zero(left)
	right, err := requestSignatureMessage([]byte("a"), []byte("bc"))
	if err != nil {
		t.Fatal(err)
	}
	defer zero(right)
	if bytes.Equal(left, right) ||
		!bytes.HasPrefix(left, []byte(requestSignatureDomain)) {
		t.Fatal("request signature framing is ambiguous")
	}
	digest := sha256.Sum256(left)
	if hex.EncodeToString(digest[:]) !=
		"05789b6993da7a0fa924a22059bd3b8814db996e16138224417f1b4e9746952c" {
		t.Fatal("request signature cross-language vector changed")
	}
	seed := bytes.Repeat([]byte{0x41}, ed25519.SeedSize)
	privateKey := ed25519.NewKeyFromSeed(seed)
	zero(seed)
	publicKey := privateKey.Public().(ed25519.PublicKey)
	der, err := x509.MarshalPKIXPublicKey(publicKey)
	if err != nil {
		t.Fatal(err)
	}
	anchor := pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der})
	_, keyID, err := parseEd25519PublicAnchor(anchor)
	zero(anchor)
	zero(der)
	if err != nil {
		t.Fatal(err)
	}
	signature := ed25519.Sign(privateKey, left)
	zero(privateKey)
	value := requestSignaturePrefix + "/" + keyID + "/" +
		base64.RawURLEncoding.EncodeToString(signature)
	zero(signature)
	if value != "ed25519-v2/"+
		"sha256:21981d07157626519dc4de7c1c09043877f8e66afd3554169bf9496df8419f50/"+
		"x1iXfjTGO6nxlwyEqTUfkekRJXaVnlc8DQ8bREEIybmxMmk73127nwzHi8U6MOv"+
		"HFdVcQhl_FhH664kHRK0GCA" {
		t.Fatal("request signature cross-language value changed")
	}
}

func TestRequestSignatureParserRejectsOversizedDelimiterInput(t *testing.T) {
	keyID := "sha256:" + strings.Repeat("a", 64)
	if _, err := parseRequestSignature(strings.Repeat("/", 1<<20), keyID); err == nil {
		t.Fatal("oversized signature profile accepted")
	}
}

func TestAuthenticatedRequestPipeTransfersExactBytesAndClosesBothEnds(t *testing.T) {
	var pipeFDs [2]int
	if err := syscall.Pipe2(pipeFDs[:], syscall.O_CLOEXEC); err != nil {
		t.Fatal(err)
	}
	writer := os.NewFile(uintptr(pipeFDs[1]), "authenticated-request-writer")
	if writer == nil {
		_ = syscall.Close(pipeFDs[0])
		_ = syscall.Close(pipeFDs[1])
		t.Fatal("authenticated request writer unavailable")
	}
	raw := []byte("exact\x00authenticated\nrequest")
	writeResult := make(chan error, 1)
	go func() {
		writeResult <- writeAuthenticatedRequestPipe(writer, raw, time.Second)
	}()

	received, err := readAuthenticatedRequestPipe(pipeFDs[0], time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(received)
	if err := <-writeResult; err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(received, raw) {
		t.Fatalf("authenticated request changed: got %q want %q", received, raw)
	}
	var stat syscall.Stat_t
	if err := syscall.Fstat(pipeFDs[0], &stat); err != syscall.EBADF {
		t.Fatalf("authenticated request reader retained: %v", err)
	}
	if err := syscall.Fstat(pipeFDs[1], &stat); err != syscall.EBADF {
		t.Fatalf("authenticated request writer retained: %v", err)
	}
}

func TestAuthenticatedRequestPipeRejectsOversizeAndClosesWriter(t *testing.T) {
	var pipeFDs [2]int
	if err := syscall.Pipe2(pipeFDs[:], syscall.O_CLOEXEC); err != nil {
		t.Fatal(err)
	}
	defer syscall.Close(pipeFDs[0])
	writer := os.NewFile(uintptr(pipeFDs[1]), "oversized-authenticated-request-writer")
	if writer == nil {
		_ = syscall.Close(pipeFDs[1])
		t.Fatal("authenticated request writer unavailable")
	}
	raw := make([]byte, maxRequestBytes+1)
	defer zero(raw)
	if err := writeAuthenticatedRequestPipe(writer, raw, time.Second); err == nil {
		t.Fatal("oversized authenticated request was transferred")
	}
	var stat syscall.Stat_t
	if err := syscall.Fstat(pipeFDs[1], &stat); err != syscall.EBADF {
		t.Fatalf("rejected authenticated request writer retained: %v", err)
	}
	buffer := make([]byte, 1)
	count, err := syscall.Read(pipeFDs[0], buffer)
	if err != nil || count != 0 {
		t.Fatalf("oversized authenticated request reached pipe: %q, %v", buffer[:count], err)
	}
}

func TestAuthenticatedRequestPipeReadTimeoutClosesReader(t *testing.T) {
	var pipeFDs [2]int
	if err := syscall.Pipe2(pipeFDs[:], syscall.O_CLOEXEC); err != nil {
		t.Fatal(err)
	}
	defer syscall.Close(pipeFDs[1])
	started := time.Now()
	if _, err := readAuthenticatedRequestPipe(pipeFDs[0], 20*time.Millisecond); err == nil {
		t.Fatal("idle authenticated request pipe did not time out")
	}
	if time.Since(started) > time.Second {
		t.Fatal("authenticated request deadline was not bounded")
	}
	var stat syscall.Stat_t
	if err := syscall.Fstat(pipeFDs[0], &stat); err != syscall.EBADF {
		t.Fatalf("timed-out authenticated request reader retained: %v", err)
	}
}

func TestControllerAuthenticatedRequestExtensionRefusesAndClosesOwnedDescriptors(t *testing.T) {
	if _, err := validateInstalledLocalAuthority(
		Controller,
		defaultInstalledRuntimePaths(),
	); err == nil {
		t.Skip("test requires the production installed authority to be absent")
	}

	raw := []byte(crossLanguageGoldenRequest)
	request, err := parseQuarantinedRequest(raw)
	if err != nil {
		t.Fatal(err)
	}
	operation := request.envelope.Operation
	transportDigest := request.rawBodyDigest
	eventID := "local-" + transportDigest[len("sha256:"):]
	request.release()

	var responseFDs [2]int
	if err := syscall.Pipe2(responseFDs[:], syscall.O_CLOEXEC); err != nil {
		t.Fatal(err)
	}
	defer syscall.Close(responseFDs[0])

	executablePath := t.TempDir() + "/controller"
	if err := os.WriteFile(executablePath, []byte("fixed-controller"), 0o755); err != nil {
		t.Fatal(err)
	}
	executableFD, err := syscall.Open(
		executablePath,
		syscall.O_RDONLY|syscall.O_CLOEXEC,
		0,
	)
	if err != nil {
		t.Fatal(err)
	}

	var requestFDs [2]int
	if err := syscall.Pipe2(requestFDs[:], syscall.O_CLOEXEC); err != nil {
		_ = syscall.Close(responseFDs[1])
		_ = syscall.Close(executableFD)
		t.Fatal(err)
	}
	count, err := syscall.Write(requestFDs[1], raw)
	if err != nil || count != len(raw) {
		_ = syscall.Close(responseFDs[1])
		_ = syscall.Close(executableFD)
		_ = syscall.Close(requestFDs[0])
		_ = syscall.Close(requestFDs[1])
		t.Fatalf("authenticated request setup failed: wrote %d of %d: %v", count, len(raw), err)
	}
	if err := syscall.Close(requestFDs[1]); err != nil {
		t.Fatal(err)
	}

	var stdout bytes.Buffer
	var stderr bytes.Buffer
	code := Run(Controller, []string{
		"--config", ControllerConfig,
		"--operation", operation,
		"--response-fd", strconv.Itoa(responseFDs[1]),
		"--event-id", eventID,
		"--request-transport-digest", transportDigest,
		"--installed-local-authority-executable-fd", strconv.Itoa(executableFD),
		"--authenticated-request-fd", strconv.Itoa(requestFDs[0]),
	}, &stdout, &stderr)
	if code != ExitProtocolFailure {
		t.Fatalf("installed controller returned %d", code)
	}
	if stdout.Len() != 0 || stderr.Len() != 0 {
		t.Fatalf(
			"installed controller emitted output: stdout=%q stderr=%q",
			stdout.String(),
			stderr.String(),
		)
	}
	var stat syscall.Stat_t
	for name, fd := range map[string]int{
		"response":              responseFDs[1],
		"installed executable":  executableFD,
		"authenticated request": requestFDs[0],
	} {
		if err := syscall.Fstat(fd, &stat); err != syscall.EBADF {
			t.Fatalf("%s descriptor retained: %v", name, err)
		}
	}
	buffer := make([]byte, 1)
	count, err = syscall.Read(responseFDs[0], buffer)
	if err != nil || count != 0 {
		t.Fatalf("controller response pipe was written: %q, %v", buffer[:count], err)
	}
}
