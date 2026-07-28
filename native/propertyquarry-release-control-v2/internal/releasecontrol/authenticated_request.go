//go:build linux

package releasecontrol

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/binary"
	"fmt"
	"os"
	"strings"
	"syscall"
	"time"
)

const (
	requestSignatureDomain            = "propertyquarry.release-request-signature.v2\x00"
	requestSignaturePrefix            = "ed25519-v2"
	authenticatedRootPolicySchema     = "propertyquarry.release-root-policy.v2"
	maxRootPolicyBytes                = 256 * 1024
	maxRequestSignatureBytes          = 169
	maxAuthenticatedRequestTTL        = 900
	maxAuthenticatedPreflightValidity = 3600
)

type authenticatedRootPolicy struct {
	Schema               string
	Identity             quarantinedIdentity
	RequiredChecks       []string
	DecisionPolicyDigest string
	MaxRequestTTL        int64
	MaxPreflightValidity int64
}

type authenticatedInstalledRequestBindings struct {
	policy           authenticatedRootPolicy
	rootPolicyDigest string
	requestKeyID     string
}

type authenticatedRootPolicySnapshot struct {
	schema               string
	identity             quarantinedIdentity
	requiredChecks       []string
	decisionPolicyDigest string
	maxRequestTTL        int64
	maxPreflightValidity int64
}

func (snapshot authenticatedRootPolicySnapshot) requiredCheckIDs() []string {
	return append([]string(nil), snapshot.requiredChecks...)
}

type authenticatedInstalledAuthoritySnapshot struct {
	stateGeneration      stableIdentity
	authenticationDigest string
	payloadTreeDigest    string
	authorityKeyID       string
	manifestDigest       string
	nativeBuildDigest    string
}

// authenticatedTransactionSnapshot is a detached, package-private value. It
// deliberately retains only authenticated scalar bindings and a private copy
// of the root-policy check list; raw request, policy, signature, and key bytes
// remain owned by their bounded parsing scopes. It does not retain the replay
// flock and must never be treated as a durable response/effect commit
// capability.
type authenticatedTransactionSnapshot struct {
	operation              string
	requestDigest          string
	canonicalRequestDigest string
	envelopeDigest         string
	signaturePayloadDigest string
	requestSignatureDigest string
	requestID              string
	nonce                  string
	issuedAt               int64
	expiresAt              int64
	requestKeyID           string
	rootPolicyDigest       string
	rootPolicy             authenticatedRootPolicySnapshot
	installedAuthority     authenticatedInstalledAuthoritySnapshot
}

// authenticateInstalledRequest establishes only request authentication. It
// does not evaluate release evidence, mutate lifecycle state, write a
// response, or authorize a release effect.
func authenticateInstalledRequest(
	paths installedRuntimePaths,
	verification *installedAuthorityVerification,
	request *quarantinedRequest,
	now time.Time,
) error {
	_, err := authenticateInstalledRequestBindings(
		paths,
		verification,
		request,
		now,
	)
	return err
}

func authenticateInstalledRequestBindings(
	paths installedRuntimePaths,
	verification *installedAuthorityVerification,
	request *quarantinedRequest,
	now time.Time,
) (authenticatedInstalledRequestBindings, error) {
	if verification == nil || request == nil || request.authenticationEstablished {
		return authenticatedInstalledRequestBindings{},
			fmt.Errorf("installed request authentication state invalid")
	}
	policyRaw, err := readAuthenticatedInstalledRole(
		paths,
		verification,
		"root-policy",
		maxRootPolicyBytes,
	)
	if err != nil {
		return authenticatedInstalledRequestBindings{}, err
	}
	defer zero(policyRaw)
	policy, err := parseAuthenticatedRootPolicy(policyRaw)
	if err != nil {
		return authenticatedInstalledRequestBindings{}, err
	}
	rootPolicyDigest := sha256Digest(policyRaw)

	anchorRaw, err := readAuthenticatedInstalledRole(
		paths,
		verification,
		"request-trust-root",
		4096,
	)
	if err != nil {
		return authenticatedInstalledRequestBindings{}, err
	}
	defer zero(anchorRaw)
	publicKey, keyID, err := parseEd25519PublicAnchor(anchorRaw)
	if err != nil {
		return authenticatedInstalledRequestBindings{},
			fmt.Errorf("installed request trust root invalid")
	}
	if err := authenticateQuarantinedRequest(request, policy, publicKey, keyID, now); err != nil {
		return authenticatedInstalledRequestBindings{}, err
	}
	return authenticatedInstalledRequestBindings{
		policy: authenticatedRootPolicy{
			Schema:               policy.Schema,
			Identity:             policy.Identity,
			RequiredChecks:       append([]string(nil), policy.RequiredChecks...),
			DecisionPolicyDigest: policy.DecisionPolicyDigest,
			MaxRequestTTL:        policy.MaxRequestTTL,
			MaxPreflightValidity: policy.MaxPreflightValidity,
		},
		rootPolicyDigest: rootPolicyDigest,
		requestKeyID:     keyID,
	}, nil
}

func readAuthenticatedInstalledRole(
	paths installedRuntimePaths,
	verification *installedAuthorityVerification,
	roleName string,
	maximum int64,
) ([]byte, error) {
	if verification == nil || maximum < 1 {
		return nil, fmt.Errorf("installed authenticated role request invalid")
	}
	role, ok := verification.Roles[roleName]
	if !ok || role.Size < 1 || role.Size > maximum {
		return nil, fmt.Errorf("installed authenticated role invalid")
	}
	raw, _, err := readStableRootedFile(
		paths.Root,
		role.Contract.Path,
		role.Size,
		expectedFileMetadata{
			Mode: role.Contract.Mode,
			UID:  role.UID,
			GID:  role.GID,
		},
	)
	if err != nil {
		return nil, err
	}
	if int64(len(raw)) != role.Size || sha256Digest(raw) != role.Digest {
		zero(raw)
		return nil, fmt.Errorf("installed authenticated role digest mismatch")
	}
	return raw, nil
}

func parseAuthenticatedRootPolicy(raw []byte) (authenticatedRootPolicy, error) {
	value, err := decodeStrictJSON(raw)
	if err != nil {
		return authenticatedRootPolicy{}, fmt.Errorf("root policy invalid")
	}
	outer, ok := value.(map[string]any)
	if !ok || !hasExactKeys(
		outer,
		"schema",
		"identity",
		"required_checks",
		"decision_policy_digest",
		"max_request_ttl",
		"max_preflight_validity",
	) {
		return authenticatedRootPolicy{}, fmt.Errorf("root policy invalid")
	}
	if !exactStringEquals(outer["schema"], authenticatedRootPolicySchema) {
		return authenticatedRootPolicy{}, fmt.Errorf("root policy schema invalid")
	}
	identityObject, ok := outer["identity"].(map[string]any)
	if !ok || !hasExactKeys(
		identityObject,
		"audience",
		"repository",
		"ref",
		"candidate_sha",
		"workflow_ref",
		"workflow_sha",
		"run_id",
		"run_attempt",
		"job",
		"environment",
	) {
		return authenticatedRootPolicy{}, fmt.Errorf("root policy identity invalid")
	}
	identity, ok := parseQuarantinedIdentity(identityObject)
	if !ok {
		return authenticatedRootPolicy{}, fmt.Errorf("root policy identity invalid")
	}
	checks, ok := outer["required_checks"].([]any)
	if !ok || len(checks) == 0 {
		return authenticatedRootPolicy{}, fmt.Errorf("root policy checks invalid")
	}
	seen := make(map[string]struct{}, len(checks))
	requiredChecks := make([]string, 0, len(checks))
	for _, item := range checks {
		check, ok := exactString(item)
		if !ok || !requestIdentifierPattern.MatchString(check) {
			return authenticatedRootPolicy{}, fmt.Errorf("root policy check invalid")
		}
		if _, duplicate := seen[check]; duplicate {
			return authenticatedRootPolicy{}, fmt.Errorf("root policy checks invalid")
		}
		seen[check] = struct{}{}
		requiredChecks = append(requiredChecks, check)
	}
	decisionDigest, ok := exactString(outer["decision_policy_digest"])
	if !ok || !requestDigestPattern.MatchString(decisionDigest) {
		return authenticatedRootPolicy{}, fmt.Errorf("root policy decision digest invalid")
	}
	maxRequestTTL, ok := exactBoundedInt(outer["max_request_ttl"], 1)
	if !ok || maxRequestTTL > maxAuthenticatedRequestTTL {
		return authenticatedRootPolicy{}, fmt.Errorf("root policy request ttl invalid")
	}
	maxPreflightValidity, ok := exactBoundedInt(
		outer["max_preflight_validity"],
		1,
	)
	if !ok || maxPreflightValidity > maxAuthenticatedPreflightValidity {
		return authenticatedRootPolicy{}, fmt.Errorf("root policy preflight validity invalid")
	}
	canonical, err := canonicalJSON(outer)
	if err != nil {
		return authenticatedRootPolicy{}, fmt.Errorf("root policy canonicalization failed")
	}
	defer zero(canonical)
	if !bytes.Equal(raw, canonical) {
		return authenticatedRootPolicy{}, fmt.Errorf("root policy is not canonical")
	}
	return authenticatedRootPolicy{
		Schema:               authenticatedRootPolicySchema,
		Identity:             identity,
		RequiredChecks:       requiredChecks,
		DecisionPolicyDigest: decisionDigest,
		MaxRequestTTL:        maxRequestTTL,
		MaxPreflightValidity: maxPreflightValidity,
	}, nil
}

func authenticateQuarantinedRequest(
	request *quarantinedRequest,
	policy authenticatedRootPolicy,
	publicKey ed25519.PublicKey,
	keyID string,
	now time.Time,
) error {
	if request == nil || request.authenticationEstablished ||
		len(publicKey) != ed25519.PublicKeySize ||
		!requestDigestPattern.MatchString(keyID) {
		return fmt.Errorf("request authentication input invalid")
	}
	if !request.envelopeDigestMatches {
		return fmt.Errorf("request envelope digest mismatch")
	}
	if request.envelope.Identity != policy.Identity {
		return fmt.Errorf("request identity policy mismatch")
	}
	nowUnix := now.Unix()
	if nowUnix < 0 ||
		request.envelope.ExpiresAt <= request.envelope.IssuedAt ||
		request.envelope.ExpiresAt-request.envelope.IssuedAt > policy.MaxRequestTTL ||
		request.envelope.IssuedAt > nowUnix ||
		nowUnix >= request.envelope.ExpiresAt {
		return fmt.Errorf("request validity window invalid")
	}
	signature, err := parseRequestSignature(request.requestSignature, keyID)
	if err != nil {
		return err
	}
	defer zero(signature)
	message, err := requestSignatureMessage(
		request.signaturePayload,
		request.canonicalEnvelope,
	)
	if err != nil {
		return err
	}
	defer zero(message)
	if !ed25519.Verify(publicKey, message, signature) {
		return fmt.Errorf("request signature invalid")
	}
	request.authenticationEstablished = true
	request.authenticatedKeyID = keyID
	return nil
}

func parseRequestSignature(value string, expectedKeyID string) ([]byte, error) {
	if len(value) != maxRequestSignatureBytes {
		return nil, fmt.Errorf("request signature profile invalid")
	}
	profile, remainder, ok := strings.Cut(value, "/")
	if !ok {
		return nil, fmt.Errorf("request signature profile invalid")
	}
	keyID, encoded, ok := strings.Cut(remainder, "/")
	if !ok ||
		strings.Contains(encoded, "/") ||
		profile != requestSignaturePrefix ||
		keyID != expectedKeyID ||
		!requestDigestPattern.MatchString(keyID) {
		return nil, fmt.Errorf("request signature profile invalid")
	}
	signature, err := base64.RawURLEncoding.DecodeString(encoded)
	if err != nil ||
		len(signature) != ed25519.SignatureSize ||
		base64.RawURLEncoding.EncodeToString(signature) != encoded {
		zero(signature)
		return nil, fmt.Errorf("request signature encoding invalid")
	}
	return signature, nil
}

func requestSignatureMessage(signaturePayload, canonicalEnvelope []byte) ([]byte, error) {
	if len(signaturePayload) == 0 || len(canonicalEnvelope) == 0 {
		return nil, fmt.Errorf("request signature message invalid")
	}
	domain := []byte(requestSignatureDomain)
	message := make(
		[]byte,
		0,
		len(domain)+8+len(signaturePayload)+8+len(canonicalEnvelope),
	)
	message = append(message, domain...)
	var length [8]byte
	binary.BigEndian.PutUint64(length[:], uint64(len(signaturePayload)))
	message = append(message, length[:]...)
	message = append(message, signaturePayload...)
	binary.BigEndian.PutUint64(length[:], uint64(len(canonicalEnvelope)))
	message = append(message, length[:]...)
	message = append(message, canonicalEnvelope...)
	return message, nil
}

func writeAuthenticatedRequestPipe(
	writer *os.File,
	raw []byte,
	timeout time.Duration,
) error {
	if writer == nil || len(raw) < 1 || len(raw) > maxRequestBytes || timeout <= 0 {
		if writer != nil {
			_ = writer.Close()
		}
		return fmt.Errorf("authenticated request transfer invalid")
	}
	fd := int(writer.Fd())
	if err := validateWritePipe(fd); err != nil {
		_ = writer.Close()
		return err
	}
	if err := syscall.SetNonblock(fd, true); err != nil {
		_ = writer.Close()
		return fmt.Errorf("authenticated request transfer setup failed")
	}
	deadline := time.Now().Add(timeout)
	for offset := 0; offset < len(raw); {
		if time.Until(deadline) <= 0 {
			_ = writer.Close()
			return fmt.Errorf("authenticated request transfer timed out")
		}
		count, err := syscall.Write(fd, raw[offset:])
		if count > 0 {
			offset += count
		}
		if err == nil {
			if count == 0 {
				_ = writer.Close()
				return fmt.Errorf("authenticated request transfer stalled")
			}
			continue
		}
		if err == syscall.EINTR {
			continue
		}
		if err != syscall.EAGAIN && err != syscall.EWOULDBLOCK {
			_ = writer.Close()
			return fmt.Errorf("authenticated request transfer failed")
		}
		remaining := time.Until(deadline)
		if remaining <= 0 {
			_ = writer.Close()
			return fmt.Errorf("authenticated request transfer timed out")
		}
		pause := time.Millisecond
		if remaining < pause {
			pause = remaining
		}
		time.Sleep(pause)
	}
	if err := writer.Close(); err != nil {
		return fmt.Errorf("authenticated request transfer close failed")
	}
	return nil
}

func readAuthenticatedRequestPipe(
	fd int,
	timeout time.Duration,
) ([]byte, error) {
	if fd < 3 || timeout <= 0 {
		if fd >= 0 {
			_ = syscall.Close(fd)
		}
		return nil, fmt.Errorf("authenticated request descriptor invalid")
	}
	if err := validateReadPipe(fd); err != nil {
		_ = syscall.Close(fd)
		return nil, err
	}
	defer syscall.Close(fd)
	if err := syscall.SetNonblock(fd, true); err != nil {
		return nil, fmt.Errorf("authenticated request read setup failed")
	}
	raw, err := readPipeUntilEOF(fd, maxRequestBytes, timeout)
	if err != nil || len(raw) < 1 {
		zero(raw)
		return nil, fmt.Errorf("authenticated request read failed")
	}
	return raw, nil
}

func authenticateControllerRequest(
	fd int,
	operation string,
	eventID string,
	transportDigest string,
	now time.Time,
) error {
	_, err := authenticateControllerTransaction(
		fd,
		operation,
		eventID,
		transportDigest,
		now,
	)
	return err
}

func authenticateControllerRequestWithPaths(
	fd int,
	operation string,
	eventID string,
	transportDigest string,
	now time.Time,
	paths installedRuntimePaths,
) error {
	_, err := authenticateControllerTransactionWithPaths(
		fd,
		operation,
		eventID,
		transportDigest,
		now,
		paths,
	)
	return err
}

func authenticateControllerTransaction(
	fd int,
	operation string,
	eventID string,
	transportDigest string,
	now time.Time,
) (*authenticatedTransactionSnapshot, error) {
	return authenticateControllerTransactionWithPaths(
		fd,
		operation,
		eventID,
		transportDigest,
		now,
		defaultInstalledRuntimePaths(),
	)
}

func authenticateControllerTransactionWithPaths(
	fd int,
	operation string,
	eventID string,
	transportDigest string,
	now time.Time,
	paths installedRuntimePaths,
) (*authenticatedTransactionSnapshot, error) {
	raw, err := readAuthenticatedRequestPipe(fd, installedChildDeadline)
	if err != nil {
		return nil, err
	}
	defer zero(raw)
	request, err := parseQuarantinedRequest(raw)
	if err != nil {
		return nil, err
	}
	defer request.release()
	if request.rawBodyDigest != transportDigest ||
		request.envelope.Operation != operation ||
		eventID != "local-"+request.rawBodyDigest[len("sha256:"):] {
		return nil, fmt.Errorf("controller request binding mismatch")
	}
	verification, err := validateInstalledLocalAuthority(Controller, paths)
	if err != nil {
		return nil, err
	}
	initialStateGeneration, err := validateInstalledAuthorityState(paths)
	if err != nil {
		return nil, err
	}
	authenticated, err := authenticateInstalledRequestBindings(
		paths,
		verification,
		request,
		now,
	)
	if err != nil {
		return nil, err
	}
	revalidated, err := validateInstalledLocalAuthority(Controller, paths)
	if err != nil || !sameInstalledAuthority(verification, revalidated) {
		return nil, fmt.Errorf("installed authority changed during controller authentication")
	}
	if err := adoptInstalledRequestReplay(
		paths,
		request,
		authenticated.rootPolicyDigest,
		revalidated,
		initialStateGeneration,
	); err != nil {
		return nil, fmt.Errorf("installed request replay claim invalid")
	}
	finalVerification, err := validateInstalledLocalAuthority(Controller, paths)
	if err != nil || !sameInstalledAuthority(revalidated, finalVerification) {
		return nil, fmt.Errorf("installed authority changed during replay adoption")
	}
	var snapshot *authenticatedTransactionSnapshot
	err = withLockedInstalledRequestReplay(
		paths,
		request,
		authenticated.rootPolicyDigest,
		finalVerification,
		initialStateGeneration,
		func(adoption *lockedInstalledReplayAdoption) error {
			lockedVerification, validationErr :=
				validateInstalledLocalAuthority(Controller, paths)
			if validationErr != nil ||
				!sameInstalledAuthority(finalVerification, lockedVerification) {
				return fmt.Errorf(
					"installed authority changed during locked replay adoption",
				)
			}
			snapshot, validationErr = newAuthenticatedTransactionSnapshot(
				request,
				authenticated,
				lockedVerification,
				adoption,
				paths,
			)
			return validationErr
		},
	)
	if err != nil {
		snapshot = nil
		return nil, fmt.Errorf("installed request replay claim invalid")
	}
	return snapshot, nil
}

// newAuthenticatedTransactionSnapshot requires a live replay-adoption
// capability. It takes the exact state generation as its final validation so
// callers cannot substitute an unlocked scalar for the held replay boundary.
func newAuthenticatedTransactionSnapshot(
	request *quarantinedRequest,
	authenticated authenticatedInstalledRequestBindings,
	verification *installedAuthorityVerification,
	adoption *lockedInstalledReplayAdoption,
	paths installedRuntimePaths,
) (*authenticatedTransactionSnapshot, error) {
	if request == nil ||
		verification == nil ||
		adoption == nil ||
		!request.authenticationEstablished ||
		request.authenticatedKeyID != authenticated.requestKeyID ||
		!requestDigestPattern.MatchString(authenticated.requestKeyID) ||
		!request.envelopeDigestMatches ||
		request.claimedEnvelopeDigest != request.canonicalEnvelopeDigest ||
		!installedReplayEnvelopeIntact(request) {
		return nil, fmt.Errorf("authenticated transaction input invalid")
	}
	if sha256Digest(request.rawBody) != request.rawBodyDigest ||
		sha256Digest(request.canonicalBody) != request.canonicalBodyDigest ||
		sha256Digest(request.canonicalEnvelope) != request.canonicalEnvelopeDigest ||
		sha256Digest(request.signaturePayload) != request.signaturePayloadDigest {
		return nil, fmt.Errorf("authenticated transaction digest changed")
	}
	signature, err := parseRequestSignature(
		request.requestSignature,
		authenticated.requestKeyID,
	)
	zero(signature)
	if err != nil {
		return nil, fmt.Errorf("authenticated transaction signature binding invalid")
	}

	policy := authenticated.policy
	if policy.Schema != authenticatedRootPolicySchema ||
		policy.Identity != request.envelope.Identity ||
		len(policy.RequiredChecks) == 0 ||
		!requestDigestPattern.MatchString(policy.DecisionPolicyDigest) ||
		policy.MaxRequestTTL < 1 ||
		policy.MaxRequestTTL > maxAuthenticatedRequestTTL ||
		policy.MaxPreflightValidity < 1 ||
		policy.MaxPreflightValidity > maxAuthenticatedPreflightValidity ||
		!requestDigestPattern.MatchString(authenticated.rootPolicyDigest) {
		return nil, fmt.Errorf("authenticated transaction policy invalid")
	}
	seenChecks := make(map[string]struct{}, len(policy.RequiredChecks))
	for _, check := range policy.RequiredChecks {
		if !requestIdentifierPattern.MatchString(check) {
			return nil, fmt.Errorf("authenticated transaction policy check invalid")
		}
		if _, duplicate := seenChecks[check]; duplicate {
			return nil, fmt.Errorf("authenticated transaction policy checks invalid")
		}
		seenChecks[check] = struct{}{}
	}
	rootPolicyRole, ok := verification.Roles["root-policy"]
	if !ok || rootPolicyRole.Digest != authenticated.rootPolicyDigest {
		return nil, fmt.Errorf("authenticated transaction policy generation changed")
	}
	if !requestDigestPattern.MatchString(verification.AuthenticationDigest) ||
		!requestDigestPattern.MatchString(verification.PayloadTreeDigest) ||
		!requestDigestPattern.MatchString(verification.AuthorityKeyID) ||
		!requestDigestPattern.MatchString(verification.ManifestDigest) ||
		!requestDigestPattern.MatchString(verification.NativeBuildDigest) {
		return nil, fmt.Errorf("authenticated transaction authority invalid")
	}
	stateGeneration, err := adoption.snapshotStateGeneration()
	if err != nil ||
		validateDirectoryIdentity(
			stateGeneration,
			expectedFileMetadata{
				Mode: 0o700,
				UID:  paths.AuthorityUID,
				GID:  paths.AuthorityGID,
			},
		) != nil {
		return nil, fmt.Errorf("authenticated transaction state generation invalid")
	}

	return &authenticatedTransactionSnapshot{
		operation:              request.envelope.Operation,
		requestDigest:          request.rawBodyDigest,
		canonicalRequestDigest: request.canonicalBodyDigest,
		envelopeDigest:         request.canonicalEnvelopeDigest,
		signaturePayloadDigest: request.signaturePayloadDigest,
		requestSignatureDigest: sha256Digest([]byte(request.requestSignature)),
		requestID:              request.envelope.RequestID,
		nonce:                  request.envelope.Nonce,
		issuedAt:               request.envelope.IssuedAt,
		expiresAt:              request.envelope.ExpiresAt,
		requestKeyID:           authenticated.requestKeyID,
		rootPolicyDigest:       authenticated.rootPolicyDigest,
		rootPolicy: authenticatedRootPolicySnapshot{
			schema:               policy.Schema,
			identity:             policy.Identity,
			requiredChecks:       append([]string(nil), policy.RequiredChecks...),
			decisionPolicyDigest: policy.DecisionPolicyDigest,
			maxRequestTTL:        policy.MaxRequestTTL,
			maxPreflightValidity: policy.MaxPreflightValidity,
		},
		installedAuthority: authenticatedInstalledAuthoritySnapshot{
			stateGeneration:      stateGeneration,
			authenticationDigest: verification.AuthenticationDigest,
			payloadTreeDigest:    verification.PayloadTreeDigest,
			authorityKeyID:       verification.AuthorityKeyID,
			manifestDigest:       verification.ManifestDigest,
			nativeBuildDigest:    verification.NativeBuildDigest,
		},
	}, nil
}
