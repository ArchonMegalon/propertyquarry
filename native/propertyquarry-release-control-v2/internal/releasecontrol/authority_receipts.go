package releasecontrol

import (
	"bytes"
	"crypto/ed25519"
	"crypto/x509"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"regexp"
	"strconv"
	"time"
)

const (
	launchAuthorityDecisionSchema  = "propertyquarry.release-control.launch-authority-decision.v2"
	launchAuthorityProducer        = "propertyquarry-resource-mediator"
	launchAuthoritySignatureDomain = "propertyquarry.release-control.launch-authority-decision.ed25519.v2\x00"

	evidenceStoreAcknowledgementSchema = "propertyquarry.release-control.evidence-store-acknowledgement.v2"
	evidenceStoreProducer              = "propertyquarry-evidence-authority"
	evidenceStoreSignatureDomain       = "propertyquarry.release-control.evidence-store-acknowledgement.ed25519.v2\x00"

	flagshipOperationsVerificationSchema = "propertyquarry.flagship-operations-evidence-verification.v1"
	flagshipOperationsVerifiedResult     = "verified"

	maxAuthorityReceiptBytes  = 128 * 1024
	maxAuthorityReceiptTTL    = 24 * 60 * 60
	maxAuthorityOperationsAge = 24 * 60 * 60
	maxAuthorityReplicaCount  = 256
	authorityOperationsWindow = 6 * 60 * 60
)

var (
	authorityCommitPattern     = regexp.MustCompile(`^[0-9a-f]{40}$`)
	authorityIdentifierPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$`)
	authorityReplicaPattern    = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)
)

type authorityOperationsBindings struct {
	PayloadDigest              string
	DeploymentID               string
	ChallengeNonce             string
	ChallengeDigest            string
	PolicyDigest               string
	ReplicaIDs                 []string
	WindowStart                int64
	WindowEnd                  int64
	DashboardReceiptDigest     string
	StructuredLogReceiptDigest string
	DistributedTraceDigest     string
	CrossLinkDigest            string
}

type authorityLifecycleBindings struct {
	LifecycleID      string
	LifecycleDigest  string
	FenceTokenDigest string
	FenceEpoch       int64
}

type authorityReceiptBindings struct {
	Identity           quarantinedIdentity
	Operation          string
	RequestID          string
	RequestDigest      string
	EnvelopeDigest     string
	RootPolicyDigest   string
	ReleaseCommitSHA   string
	ReleaseImageDigest string
	Operations         authorityOperationsBindings
	Lifecycle          authorityLifecycleBindings
	MaxReceiptTTL      int64
	MaxOperationsAge   int64
}

type evidenceStoreReceiptBindings struct {
	CASGeneration      int64
	PreviousDigest     string
	PersistedAckDigest string
	FsyncedAckDigest   string
}

type authorityReceiptTrustRoots struct {
	ResourceMediatorPublicKey  ed25519.PublicKey
	ResourceMediatorKeyID      string
	EvidenceAuthorityPublicKey ed25519.PublicKey
	EvidenceAuthorityKeyID     string
}

type verifiedAuthorityReceipt struct {
	CanonicalReceipt []byte
	ReceiptDigest    string
	PayloadDigest    string
	BindingDigest    string
	IssuedAt         int64
	ExpiresAt        int64
}

func (receipt *verifiedAuthorityReceipt) release() {
	if receipt == nil {
		return
	}
	zero(receipt.CanonicalReceipt)
	receipt.CanonicalReceipt = nil
	receipt.ReceiptDigest = ""
	receipt.PayloadDigest = ""
	receipt.BindingDigest = ""
	receipt.IssuedAt = 0
	receipt.ExpiresAt = 0
}

type verifiedAuthorityReceiptPair struct {
	LaunchDecision *verifiedAuthorityReceipt
	EvidenceStore  *verifiedAuthorityReceipt
}

func (pair *verifiedAuthorityReceiptPair) release() {
	if pair == nil {
		return
	}
	pair.LaunchDecision.release()
	pair.EvidenceStore.release()
	pair.LaunchDecision = nil
	pair.EvidenceStore = nil
}

type parsedSignedAuthorityReceipt struct {
	canonicalReceipt []byte
	receiptDigest    string
	payload          map[string]any
	payloadDigest    string
}

func (receipt *parsedSignedAuthorityReceipt) release() {
	if receipt == nil {
		return
	}
	zero(receipt.canonicalReceipt)
	receipt.canonicalReceipt = nil
	receipt.receiptDigest = ""
	receipt.payload = nil
	receipt.payloadDigest = ""
}

func verifyAuthorityReceiptPair(
	launchRaw []byte,
	evidenceStoreRaw []byte,
	roots authorityReceiptTrustRoots,
	expected authorityReceiptBindings,
	expectedStorage evidenceStoreReceiptBindings,
	now time.Time,
) (*verifiedAuthorityReceiptPair, error) {
	if err := validateAuthorityReceiptRoots(roots); err != nil {
		return nil, err
	}
	if err := validateAuthorityReceiptBindings(expected); err != nil {
		return nil, err
	}
	if err := validateEvidenceStoreBindings(expectedStorage); err != nil {
		return nil, err
	}
	if now.Unix() < 0 {
		return nil, fmt.Errorf("authority receipt clock invalid")
	}

	launch, err := verifyLaunchAuthorityDecision(
		launchRaw,
		roots.ResourceMediatorPublicKey,
		roots.ResourceMediatorKeyID,
		expected,
		now,
	)
	if err != nil {
		return nil, err
	}
	evidence, err := verifyEvidenceStoreAcknowledgement(
		evidenceStoreRaw,
		roots.EvidenceAuthorityPublicKey,
		roots.EvidenceAuthorityKeyID,
		expected,
		expectedStorage,
		launch.PayloadDigest,
		launch.ReceiptDigest,
		now,
	)
	if err != nil {
		launch.release()
		return nil, err
	}
	if launch.BindingDigest != evidence.BindingDigest ||
		evidence.IssuedAt < launch.IssuedAt ||
		evidence.IssuedAt >= launch.ExpiresAt ||
		evidence.ExpiresAt > launch.ExpiresAt {
		launch.release()
		evidence.release()
		return nil, fmt.Errorf("authority receipt pair binding invalid")
	}
	return &verifiedAuthorityReceiptPair{
		LaunchDecision: launch,
		EvidenceStore:  evidence,
	}, nil
}

func verifyLaunchAuthorityDecision(
	raw []byte,
	publicKey ed25519.PublicKey,
	keyID string,
	expected authorityReceiptBindings,
	now time.Time,
) (*verifiedAuthorityReceipt, error) {
	if err := validateAuthorityReceiptBindings(expected); err != nil {
		return nil, err
	}
	if now.Unix() < 0 {
		return nil, fmt.Errorf("authority receipt clock invalid")
	}
	parsed, err := parseSignedAuthorityReceipt(
		raw,
		launchAuthorityDecisionSchema,
		launchAuthorityProducer,
		launchAuthoritySignatureDomain,
		publicKey,
		keyID,
	)
	if err != nil {
		return nil, err
	}
	defer parsed.release()

	payload := parsed.payload
	if !hasExactKeys(
		payload,
		"decision",
		"binding_sha256",
		"issued_at",
		"expires_at",
		"request",
		"release",
		"gold_operations_verification",
		"lifecycle",
	) || !exactStringEquals(payload["decision"], "allow") {
		return nil, fmt.Errorf("launch authority payload invalid")
	}
	issuedAt, expiresAt, err := validateAuthorityReceiptFreshness(payload, expected, now)
	if err != nil {
		return nil, err
	}
	bindingDigest, err := validateCommonAuthorityBindings(
		payload,
		expected,
		issuedAt,
		now.Unix(),
	)
	if err != nil {
		return nil, err
	}
	if !exactStringEquals(payload["binding_sha256"], bindingDigest) {
		return nil, fmt.Errorf("launch authority binding digest invalid")
	}
	return verifiedReceiptFromParsed(parsed, bindingDigest, issuedAt, expiresAt), nil
}

func verifyEvidenceStoreAcknowledgement(
	raw []byte,
	publicKey ed25519.PublicKey,
	keyID string,
	expected authorityReceiptBindings,
	expectedStorage evidenceStoreReceiptBindings,
	launchPayloadDigest string,
	launchReceiptDigest string,
	now time.Time,
) (*verifiedAuthorityReceipt, error) {
	if err := validateAuthorityReceiptBindings(expected); err != nil {
		return nil, err
	}
	if err := validateEvidenceStoreBindings(expectedStorage); err != nil {
		return nil, err
	}
	if now.Unix() < 0 {
		return nil, fmt.Errorf("authority receipt clock invalid")
	}
	if !requestDigestPattern.MatchString(launchPayloadDigest) ||
		!requestDigestPattern.MatchString(launchReceiptDigest) {
		return nil, fmt.Errorf("launch decision digest invalid")
	}
	parsed, err := parseSignedAuthorityReceipt(
		raw,
		evidenceStoreAcknowledgementSchema,
		evidenceStoreProducer,
		evidenceStoreSignatureDomain,
		publicKey,
		keyID,
	)
	if err != nil {
		return nil, err
	}
	defer parsed.release()

	payload := parsed.payload
	if !hasExactKeys(
		payload,
		"acknowledgement",
		"binding_sha256",
		"issued_at",
		"expires_at",
		"request",
		"release",
		"gold_operations_verification",
		"lifecycle",
		"launch_decision_payload_sha256",
		"launch_decision_receipt_sha256",
		"storage",
	) ||
		!exactStringEquals(payload["acknowledgement"], "persisted-and-fsynced") ||
		!exactStringEquals(payload["launch_decision_payload_sha256"], launchPayloadDigest) ||
		!exactStringEquals(payload["launch_decision_receipt_sha256"], launchReceiptDigest) {
		return nil, fmt.Errorf("evidence store payload invalid")
	}
	issuedAt, expiresAt, err := validateAuthorityReceiptFreshness(payload, expected, now)
	if err != nil {
		return nil, err
	}
	bindingDigest, err := validateCommonAuthorityBindings(
		payload,
		expected,
		issuedAt,
		now.Unix(),
	)
	if err != nil {
		return nil, err
	}
	if !exactStringEquals(payload["binding_sha256"], bindingDigest) {
		return nil, fmt.Errorf("evidence store binding digest invalid")
	}
	if err := validateEvidenceStorePayload(payload["storage"], expectedStorage); err != nil {
		return nil, err
	}
	return verifiedReceiptFromParsed(parsed, bindingDigest, issuedAt, expiresAt), nil
}

func verifiedReceiptFromParsed(
	parsed *parsedSignedAuthorityReceipt,
	bindingDigest string,
	issuedAt int64,
	expiresAt int64,
) *verifiedAuthorityReceipt {
	return &verifiedAuthorityReceipt{
		CanonicalReceipt: append([]byte(nil), parsed.canonicalReceipt...),
		ReceiptDigest:    parsed.receiptDigest,
		PayloadDigest:    parsed.payloadDigest,
		BindingDigest:    bindingDigest,
		IssuedAt:         issuedAt,
		ExpiresAt:        expiresAt,
	}
}

func parseSignedAuthorityReceipt(
	raw []byte,
	expectedSchema string,
	expectedProducer string,
	signatureDomain string,
	publicKey ed25519.PublicKey,
	expectedKeyID string,
) (*parsedSignedAuthorityReceipt, error) {
	if len(raw) < 1 || len(raw) > maxAuthorityReceiptBytes {
		return nil, fmt.Errorf("signed authority receipt size invalid")
	}
	if err := validateRequestUnicode(raw); err != nil {
		return nil, fmt.Errorf("signed authority receipt unicode invalid")
	}
	if err := validateAuthorityReceiptRoot(publicKey, expectedKeyID); err != nil {
		return nil, err
	}
	value, err := decodeStrictJSON(raw)
	if err != nil {
		return nil, fmt.Errorf("signed authority receipt JSON invalid")
	}
	outer, ok := value.(map[string]any)
	if !ok || !hasExactKeys(
		outer,
		"schema",
		"producer",
		"key_id",
		"payload",
		"payload_sha256",
		"signature",
	) ||
		!exactStringEquals(outer["schema"], expectedSchema) ||
		!exactStringEquals(outer["producer"], expectedProducer) ||
		!exactStringEquals(outer["key_id"], expectedKeyID) {
		return nil, fmt.Errorf("signed authority receipt envelope invalid")
	}
	canonicalReceipt, err := canonicalJSON(value)
	if err != nil || !bytes.Equal(canonicalReceipt, raw) {
		zero(canonicalReceipt)
		return nil, fmt.Errorf("signed authority receipt is not canonical")
	}

	payload, ok := outer["payload"].(map[string]any)
	if !ok {
		zero(canonicalReceipt)
		return nil, fmt.Errorf("signed authority receipt payload invalid")
	}
	canonicalPayload, err := canonicalJSON(payload)
	if err != nil {
		zero(canonicalReceipt)
		return nil, fmt.Errorf("signed authority receipt payload invalid")
	}
	defer zero(canonicalPayload)
	payloadDigest := sha256Digest(canonicalPayload)
	if !exactStringEquals(outer["payload_sha256"], payloadDigest) {
		zero(canonicalReceipt)
		return nil, fmt.Errorf("signed authority receipt payload digest invalid")
	}

	unsigned := map[string]any{
		"schema":         outer["schema"],
		"producer":       outer["producer"],
		"key_id":         outer["key_id"],
		"payload":        payload,
		"payload_sha256": outer["payload_sha256"],
	}
	canonicalUnsigned, err := canonicalJSON(unsigned)
	if err != nil {
		zero(canonicalReceipt)
		return nil, fmt.Errorf("signed authority receipt envelope invalid")
	}
	defer zero(canonicalUnsigned)
	signatureText, ok := exactString(outer["signature"])
	if !ok {
		zero(canonicalReceipt)
		return nil, fmt.Errorf("signed authority receipt signature invalid")
	}
	signature, err := base64.RawURLEncoding.DecodeString(signatureText)
	if err != nil ||
		len(signature) != ed25519.SignatureSize ||
		base64.RawURLEncoding.EncodeToString(signature) != signatureText {
		zero(signature)
		zero(canonicalReceipt)
		return nil, fmt.Errorf("signed authority receipt signature encoding invalid")
	}
	defer zero(signature)
	message, err := authorityReceiptSignatureMessage(signatureDomain, canonicalUnsigned)
	if err != nil {
		zero(canonicalReceipt)
		return nil, err
	}
	defer zero(message)
	if !ed25519.Verify(publicKey, message, signature) {
		zero(canonicalReceipt)
		return nil, fmt.Errorf("signed authority receipt signature invalid")
	}
	return &parsedSignedAuthorityReceipt{
		canonicalReceipt: canonicalReceipt,
		receiptDigest:    sha256Digest(canonicalReceipt),
		payload:          payload,
		payloadDigest:    payloadDigest,
	}, nil
}

func authorityReceiptSignatureMessage(domain string, canonicalUnsigned []byte) ([]byte, error) {
	if domain == "" || len(canonicalUnsigned) == 0 {
		return nil, fmt.Errorf("authority receipt signature message invalid")
	}
	domainBytes := []byte(domain)
	message := make([]byte, 0, len(domainBytes)+8+len(canonicalUnsigned))
	message = append(message, domainBytes...)
	var length [8]byte
	binary.BigEndian.PutUint64(length[:], uint64(len(canonicalUnsigned)))
	message = append(message, length[:]...)
	message = append(message, canonicalUnsigned...)
	return message, nil
}

func validateAuthorityReceiptRoots(roots authorityReceiptTrustRoots) error {
	if err := validateAuthorityReceiptRoot(
		roots.ResourceMediatorPublicKey,
		roots.ResourceMediatorKeyID,
	); err != nil {
		return err
	}
	if err := validateAuthorityReceiptRoot(
		roots.EvidenceAuthorityPublicKey,
		roots.EvidenceAuthorityKeyID,
	); err != nil {
		return err
	}
	if roots.ResourceMediatorKeyID == roots.EvidenceAuthorityKeyID ||
		bytes.Equal(roots.ResourceMediatorPublicKey, roots.EvidenceAuthorityPublicKey) {
		return fmt.Errorf("authority receipt trust roots are not separated")
	}
	return nil
}

func validateAuthorityReceiptRoot(publicKey ed25519.PublicKey, keyID string) error {
	if len(publicKey) != ed25519.PublicKeySize || !requestDigestPattern.MatchString(keyID) {
		return fmt.Errorf("authority receipt trust root invalid")
	}
	der, err := x509.MarshalPKIXPublicKey(publicKey)
	if err != nil {
		return fmt.Errorf("authority receipt trust root invalid")
	}
	defer zero(der)
	if sha256Digest(der) != keyID {
		return fmt.Errorf("authority receipt key id does not identify public key")
	}
	return nil
}

func validateAuthorityReceiptBindings(expected authorityReceiptBindings) error {
	if (expected.Operation != "release-preflight" && expected.Operation != "release-run") ||
		!authorityIdentifierPattern.MatchString(expected.RequestID) ||
		!requestDigestPattern.MatchString(expected.RequestDigest) ||
		!requestDigestPattern.MatchString(expected.EnvelopeDigest) ||
		!requestDigestPattern.MatchString(expected.RootPolicyDigest) ||
		!authorityCommitPattern.MatchString(expected.ReleaseCommitSHA) ||
		!requestDigestPattern.MatchString(expected.ReleaseImageDigest) ||
		expected.Identity.CandidateSHA != expected.ReleaseCommitSHA ||
		expected.MaxReceiptTTL < 1 ||
		expected.MaxReceiptTTL > maxAuthorityReceiptTTL ||
		expected.MaxOperationsAge < 1 ||
		expected.MaxOperationsAge > maxAuthorityOperationsAge {
		return fmt.Errorf("authority receipt expected binding invalid")
	}
	identityValue := authorityIdentityValue(expected.Identity)
	parsedIdentity, ok := parseAuthorityIdentity(identityValue)
	if !ok || parsedIdentity != expected.Identity {
		return fmt.Errorf("authority receipt expected identity invalid")
	}
	operations := expected.Operations
	if !requestDigestPattern.MatchString(operations.PayloadDigest) ||
		!authorityIdentifierPattern.MatchString(operations.DeploymentID) ||
		!authorityIdentifierPattern.MatchString(operations.ChallengeNonce) ||
		!requestDigestPattern.MatchString(operations.ChallengeDigest) ||
		!requestDigestPattern.MatchString(operations.PolicyDigest) ||
		!requestDigestPattern.MatchString(operations.DashboardReceiptDigest) ||
		!requestDigestPattern.MatchString(operations.StructuredLogReceiptDigest) ||
		!requestDigestPattern.MatchString(operations.DistributedTraceDigest) ||
		!requestDigestPattern.MatchString(operations.CrossLinkDigest) ||
		operations.WindowStart < 0 ||
		operations.WindowEnd <= operations.WindowStart ||
		operations.WindowEnd-operations.WindowStart != authorityOperationsWindow ||
		!validAuthorityReplicaIDs(operations.ReplicaIDs) {
		return fmt.Errorf("authority receipt expected operations binding invalid")
	}
	lifecycle := expected.Lifecycle
	if !authorityIdentifierPattern.MatchString(lifecycle.LifecycleID) ||
		!requestDigestPattern.MatchString(lifecycle.LifecycleDigest) ||
		!requestDigestPattern.MatchString(lifecycle.FenceTokenDigest) ||
		lifecycle.FenceEpoch < 1 {
		return fmt.Errorf("authority receipt expected lifecycle binding invalid")
	}
	return nil
}

func validateEvidenceStoreBindings(expected evidenceStoreReceiptBindings) error {
	if expected.CASGeneration < 1 ||
		!requestDigestPattern.MatchString(expected.PreviousDigest) ||
		!requestDigestPattern.MatchString(expected.PersistedAckDigest) ||
		!requestDigestPattern.MatchString(expected.FsyncedAckDigest) ||
		expected.PreviousDigest == expected.PersistedAckDigest ||
		expected.PreviousDigest == expected.FsyncedAckDigest ||
		expected.PersistedAckDigest == expected.FsyncedAckDigest {
		return fmt.Errorf("evidence store expected binding invalid")
	}
	return nil
}

func validateAuthorityReceiptFreshness(
	payload map[string]any,
	expected authorityReceiptBindings,
	now time.Time,
) (int64, int64, error) {
	issuedAt, ok := exactBoundedInt(payload["issued_at"], 1)
	if !ok {
		return 0, 0, fmt.Errorf("authority receipt issued time invalid")
	}
	expiresAt, ok := exactBoundedInt(payload["expires_at"], 1)
	if !ok {
		return 0, 0, fmt.Errorf("authority receipt expiry invalid")
	}
	nowUnix := now.Unix()
	if expiresAt <= issuedAt ||
		expiresAt-issuedAt > expected.MaxReceiptTTL ||
		issuedAt > nowUnix ||
		nowUnix >= expiresAt {
		return 0, 0, fmt.Errorf("authority receipt freshness invalid")
	}
	return issuedAt, expiresAt, nil
}

func validateCommonAuthorityBindings(
	payload map[string]any,
	expected authorityReceiptBindings,
	issuedAt int64,
	verifiedAt int64,
) (string, error) {
	request, ok := payload["request"].(map[string]any)
	if !ok || !hasExactKeys(
		request,
		"identity",
		"operation",
		"request_id",
		"request_sha256",
		"envelope_sha256",
		"root_policy_sha256",
	) {
		return "", fmt.Errorf("authority receipt request binding invalid")
	}
	identityValue, ok := request["identity"].(map[string]any)
	if !ok {
		return "", fmt.Errorf("authority receipt identity binding invalid")
	}
	identity, ok := parseAuthorityIdentity(identityValue)
	if !ok ||
		identity != expected.Identity ||
		!exactStringEquals(request["operation"], expected.Operation) ||
		!exactStringEquals(request["request_id"], expected.RequestID) ||
		!exactStringEquals(request["request_sha256"], expected.RequestDigest) ||
		!exactStringEquals(request["envelope_sha256"], expected.EnvelopeDigest) ||
		!exactStringEquals(request["root_policy_sha256"], expected.RootPolicyDigest) {
		return "", fmt.Errorf("authority receipt request binding mismatch")
	}

	release, ok := payload["release"].(map[string]any)
	if !ok ||
		!hasExactKeys(release, "commit_sha", "image_digest") ||
		!exactStringEquals(release["commit_sha"], expected.ReleaseCommitSHA) ||
		!exactStringEquals(release["image_digest"], expected.ReleaseImageDigest) {
		return "", fmt.Errorf("authority receipt release binding mismatch")
	}
	if err := validateAuthorityOperationsPayload(
		payload["gold_operations_verification"],
		expected.Operations,
		issuedAt,
		verifiedAt,
		expected.MaxOperationsAge,
	); err != nil {
		return "", err
	}
	if err := validateAuthorityLifecyclePayload(payload["lifecycle"], expected.Lifecycle); err != nil {
		return "", err
	}
	common := map[string]any{
		"request":                      request,
		"release":                      release,
		"gold_operations_verification": payload["gold_operations_verification"],
		"lifecycle":                    payload["lifecycle"],
	}
	canonical, err := canonicalJSON(common)
	if err != nil {
		return "", fmt.Errorf("authority receipt common binding invalid")
	}
	defer zero(canonical)
	return sha256Digest(canonical), nil
}

func validateAuthorityOperationsPayload(
	value any,
	expected authorityOperationsBindings,
	issuedAt int64,
	verifiedAt int64,
	maximumAge int64,
) error {
	operations, ok := value.(map[string]any)
	if !ok || !hasExactKeys(
		operations,
		"schema",
		"payload_sha256",
		"deployment_id",
		"challenge_nonce",
		"challenge_sha256",
		"policy_sha256",
		"replica_ids",
		"window",
		"raw_receipt_hashes",
		"result",
		"cross_link_sha256",
	) ||
		!exactStringEquals(operations["schema"], flagshipOperationsVerificationSchema) ||
		!exactStringEquals(operations["payload_sha256"], expected.PayloadDigest) ||
		!exactStringEquals(operations["deployment_id"], expected.DeploymentID) ||
		!exactStringEquals(operations["challenge_nonce"], expected.ChallengeNonce) ||
		!exactStringEquals(operations["challenge_sha256"], expected.ChallengeDigest) ||
		!exactStringEquals(operations["policy_sha256"], expected.PolicyDigest) ||
		!exactStringEquals(operations["result"], flagshipOperationsVerifiedResult) ||
		!exactStringEquals(operations["cross_link_sha256"], expected.CrossLinkDigest) {
		return fmt.Errorf("authority receipt operations binding mismatch")
	}
	replicas, ok := operations["replica_ids"].([]any)
	if !ok || len(replicas) != len(expected.ReplicaIDs) {
		return fmt.Errorf("authority receipt replica binding invalid")
	}
	for index, value := range replicas {
		if !exactStringEquals(value, expected.ReplicaIDs[index]) {
			return fmt.Errorf("authority receipt replica binding mismatch")
		}
	}
	window, ok := operations["window"].(map[string]any)
	if !ok || !hasExactKeys(window, "start_unix", "end_unix") {
		return fmt.Errorf("authority receipt operations window invalid")
	}
	start, ok := exactBoundedInt(window["start_unix"], 0)
	if !ok {
		return fmt.Errorf("authority receipt operations window invalid")
	}
	end, ok := exactBoundedInt(window["end_unix"], 1)
	if !ok ||
		start != expected.WindowStart ||
		end != expected.WindowEnd ||
		end-start != authorityOperationsWindow ||
		end > issuedAt ||
		verifiedAt < issuedAt ||
		verifiedAt-end > maximumAge {
		return fmt.Errorf("authority receipt operations window binding invalid")
	}
	hashes, ok := operations["raw_receipt_hashes"].(map[string]any)
	if !ok ||
		!hasExactKeys(hashes, "dashboard_render", "structured_log_query", "distributed_trace_query") ||
		!exactStringEquals(hashes["dashboard_render"], expected.DashboardReceiptDigest) ||
		!exactStringEquals(hashes["structured_log_query"], expected.StructuredLogReceiptDigest) ||
		!exactStringEquals(hashes["distributed_trace_query"], expected.DistributedTraceDigest) {
		return fmt.Errorf("authority receipt raw evidence binding mismatch")
	}
	return nil
}

func validateAuthorityLifecyclePayload(value any, expected authorityLifecycleBindings) error {
	lifecycle, ok := value.(map[string]any)
	if !ok ||
		!hasExactKeys(
			lifecycle,
			"lifecycle_id",
			"lifecycle_sha256",
			"fence_token_sha256",
			"fence_epoch",
		) ||
		!exactStringEquals(lifecycle["lifecycle_id"], expected.LifecycleID) ||
		!exactStringEquals(lifecycle["lifecycle_sha256"], expected.LifecycleDigest) ||
		!exactStringEquals(lifecycle["fence_token_sha256"], expected.FenceTokenDigest) {
		return fmt.Errorf("authority receipt lifecycle binding mismatch")
	}
	epoch, ok := exactBoundedInt(lifecycle["fence_epoch"], 1)
	if !ok || epoch != expected.FenceEpoch {
		return fmt.Errorf("authority receipt fence binding mismatch")
	}
	return nil
}

func validateEvidenceStorePayload(value any, expected evidenceStoreReceiptBindings) error {
	storage, ok := value.(map[string]any)
	if !ok ||
		!hasExactKeys(
			storage,
			"cas_generation",
			"previous_sha256",
			"persisted_ack_sha256",
			"fsynced_ack_sha256",
		) ||
		!exactStringEquals(storage["previous_sha256"], expected.PreviousDigest) ||
		!exactStringEquals(storage["persisted_ack_sha256"], expected.PersistedAckDigest) ||
		!exactStringEquals(storage["fsynced_ack_sha256"], expected.FsyncedAckDigest) {
		return fmt.Errorf("evidence store durable acknowledgement binding mismatch")
	}
	generation, ok := exactBoundedInt(storage["cas_generation"], 1)
	if !ok || generation != expected.CASGeneration {
		return fmt.Errorf("evidence store CAS generation mismatch")
	}
	return nil
}

func parseAuthorityIdentity(value map[string]any) (quarantinedIdentity, bool) {
	if !hasExactKeys(
		value,
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
		return quarantinedIdentity{}, false
	}
	return parseQuarantinedIdentity(value)
}

func authorityIdentityValue(identity quarantinedIdentity) map[string]any {
	return map[string]any{
		"audience":      identity.Audience,
		"repository":    identity.Repository,
		"ref":           identity.Ref,
		"candidate_sha": identity.CandidateSHA,
		"workflow_ref":  identity.WorkflowRef,
		"workflow_sha":  identity.WorkflowSHA,
		"run_id":        identity.RunID,
		"run_attempt":   json.Number(strconv.FormatInt(identity.RunAttempt, 10)),
		"job":           identity.Job,
		"environment":   identity.Environment,
	}
}

func validAuthorityReplicaIDs(values []string) bool {
	if len(values) < 1 || len(values) > maxAuthorityReplicaCount {
		return false
	}
	for index, value := range values {
		if value == "UNCONFIGURED" ||
			!authorityReplicaPattern.MatchString(value) ||
			(index > 0 && values[index-1] >= value) {
			return false
		}
	}
	return true
}
