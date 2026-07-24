//go:build linux && amd64

package authority

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"time"
)

const (
	aiPanoramaComposePlanPath  = aiPanoramaRuntimeRoot + "/public-tour-compose-plan.v1.json"
	aiPanoramaPermitRoot       = aiPanoramaControlRoot + "/permits"
	aiPanoramaLegacyPermitPath = aiPanoramaPermitRoot + "/prater-ai-panorama-install.json"
	aiPanoramaMaximumPermit    = 128 * 1024
	aiPanoramaMaximumKeyring   = 256 * 1024
	aiPanoramaMaximumKeyCount  = 64
)

var (
	aiPanoramaRawSHA256Pattern = regexp.MustCompile(`^[0-9a-f]{64}$`)
	aiPanoramaNoncePattern     = regexp.MustCompile(`^[0-9a-f]{32}$`)
	aiPanoramaSafeIDPattern    = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:@/+~-]{0,255}$`)
	aiPanoramaTimestampPattern = regexp.MustCompile(`^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$`)
)

type aiPanoramaPurposeKey struct {
	KeyID          string
	Epoch          int64
	Public         ed25519.PublicKey
	Raw            []byte
	PublicSHA256   string
	KeyringSHA256  string
	ActivationTime time.Time
	AcceptUntil    *time.Time
	RevokedAt      *time.Time
}

func (key *aiPanoramaPurposeKey) release() {
	if key == nil {
		return
	}
	zero(key.Public)
	zero(key.Raw)
	*key = aiPanoramaPurposeKey{}
}

type aiPanoramaSignedPermit struct {
	RequestID       string
	Relpath         string
	Path            string
	SHA256          string
	PreimageSHA256  string
	KeyID           string
	KeyEpoch        int64
	KeySHA256       string
	KeyringSHA256   string
	IssuedAt        string
	ExpiresAt       string
	ExecutionLease  int64
	CanonicalLength int
}

func aiPanoramaPermitRelpath(requestID string) (string, error) {
	if !aiPanoramaNoncePattern.MatchString(requestID) {
		return "", fmt.Errorf("ai-panorama-permit-request-id-invalid")
	}
	return "prater-ai-panorama-install-" + requestID + ".v2.json", nil
}

func aiPanoramaPermitPath(requestID string) (string, error) {
	relpath, err := aiPanoramaPermitRelpath(requestID)
	if err != nil {
		return "", err
	}
	return aiPanoramaPermitRoot + "/" + relpath, nil
}

func aiPanoramaRawSHA256(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

func aiPanoramaCanonicalJSON(value any) ([]byte, error) {
	return canonicalJSON(value)
}

func parseAiPanoramaTimestamp(value any) (time.Time, bool) {
	text, ok := exactString(value)
	if !ok || !aiPanoramaTimestampPattern.MatchString(text) {
		return time.Time{}, false
	}
	parsed, err := time.Parse(time.RFC3339Nano, text)
	if err != nil || parsed.Location() != time.UTC {
		return time.Time{}, false
	}
	return parsed, true
}

func parseAiPanoramaOptionalTimestamp(value any) (*time.Time, bool) {
	if value == nil {
		return nil, true
	}
	parsed, ok := parseAiPanoramaTimestamp(value)
	if !ok {
		return nil, false
	}
	return &parsed, true
}

func aiPanoramaTimeWithinKey(key *aiPanoramaPurposeKey, instant time.Time) bool {
	if key == nil || instant.IsZero() || instant.Location() != time.UTC ||
		instant.Before(key.ActivationTime) {
		return false
	}
	if key.AcceptUntil != nil && !instant.Before(*key.AcceptUntil) {
		return false
	}
	if key.RevokedAt != nil && !instant.Before(*key.RevokedAt) {
		return false
	}
	return true
}

func loadAiPanoramaPurposeKey(root string, private ed25519.PrivateKey, issuedAt, observedAt time.Time) (*aiPanoramaPurposeKey, error) {
	if root == "" {
		root = "/"
	}
	if len(private) != ed25519.PrivateKeySize || issuedAt.IsZero() || observedAt.IsZero() ||
		issuedAt.Location() != time.UTC || observedAt.Location() != time.UTC {
		return nil, fmt.Errorf("ai-panorama-keyring-input-invalid")
	}
	ownerUID, ownerGID := secureOwner(root)
	raw, err := secureRead(root, aiPanoramaPurposeKeyringPath, 0o444, ownerUID, ownerGID, aiPanoramaMaximumKeyring)
	if err != nil || len(raw) < 3 || raw[len(raw)-1] != '\n' ||
		raw[len(raw)-2] == '\n' || bytes.IndexByte(raw[:len(raw)-1], '\n') >= 0 {
		zero(raw)
		return nil, fmt.Errorf("ai-panorama-keyring-unavailable")
	}
	defer zero(raw)
	value, err := strictJSON(raw[:len(raw)-1], aiPanoramaMaximumKeyring)
	if err != nil || !hasKeys(value,
		"schema", "version", "authority", "algorithm", "status", "usage",
		"rotation_epoch", "minimum_accepted_epoch", "keys",
	) || value["schema"] != aiPanoramaKeyringSchema ||
		value["version"] != json.Number("1") ||
		value["authority"] != "propertyquarry-release-control" ||
		value["algorithm"] != "Ed25519" || value["status"] != "active" ||
		value["usage"] != aiPanoramaPermitKeyUsage {
		return nil, fmt.Errorf("ai-panorama-keyring-invalid")
	}
	rotation, rotationOK := exactInt(value["rotation_epoch"], 1, 1<<62)
	minimum, minimumOK := exactInt(value["minimum_accepted_epoch"], 1, 1<<62)
	items, itemsOK := value["keys"].([]any)
	if !rotationOK || !minimumOK || minimum > rotation || !itemsOK ||
		len(items) < 1 || len(items) > aiPanoramaMaximumKeyCount {
		return nil, fmt.Errorf("ai-panorama-keyring-invalid")
	}
	expectedPublic := private.Public().(ed25519.PublicKey)
	expectedPublicSHA256 := aiPanoramaRawSHA256(expectedPublic)
	seenIDs := make(map[string]struct{}, len(items))
	var previousEpoch int64
	var selected *aiPanoramaPurposeKey
	for _, rawItem := range items {
		item, ok := rawItem.(map[string]any)
		if !ok || !hasKeys(item,
			"key_id", "epoch", "usage", "public_key", "public_key_sha256",
			"activates_at", "accept_until", "revoked_at",
		) || item["usage"] != aiPanoramaPermitKeyUsage {
			return nil, fmt.Errorf("ai-panorama-keyring-key-invalid")
		}
		keyID, keyIDOK := exactString(item["key_id"])
		epoch, epochOK := exactInt(item["epoch"], 1, 1<<62)
		publicText, publicOK := exactString(item["public_key"])
		publicSHA256, digestOK := exactString(item["public_key_sha256"])
		activation, activationOK := parseAiPanoramaTimestamp(item["activates_at"])
		acceptUntil, acceptOK := parseAiPanoramaOptionalTimestamp(item["accept_until"])
		revokedAt, revokedOK := parseAiPanoramaOptionalTimestamp(item["revoked_at"])
		decoded, decodeErr := base64.RawURLEncoding.DecodeString(publicText)
		if !keyIDOK || !aiPanoramaSafeIDPattern.MatchString(keyID) ||
			!epochOK || epoch <= previousEpoch || !publicOK ||
			decodeErr != nil || len(decoded) != ed25519.PublicKeySize ||
			base64.RawURLEncoding.EncodeToString(decoded) != publicText ||
			!digestOK || !aiPanoramaRawSHA256Pattern.MatchString(publicSHA256) ||
			aiPanoramaRawSHA256(decoded) != publicSHA256 ||
			!activationOK || !acceptOK || !revokedOK ||
			(acceptUntil != nil && !acceptUntil.After(activation)) ||
			(revokedAt != nil && revokedAt.Before(activation)) {
			zero(decoded)
			return nil, fmt.Errorf("ai-panorama-keyring-key-invalid")
		}
		if _, duplicate := seenIDs[keyID]; duplicate {
			zero(decoded)
			return nil, fmt.Errorf("ai-panorama-keyring-key-invalid")
		}
		seenIDs[keyID] = struct{}{}
		previousEpoch = epoch
		if bytes.Equal(decoded, expectedPublic) {
			if selected != nil || epoch < minimum || publicSHA256 != expectedPublicSHA256 {
				zero(decoded)
				return nil, fmt.Errorf("ai-panorama-keyring-private-key-binding-invalid")
			}
			selected = &aiPanoramaPurposeKey{
				KeyID: keyID, Epoch: epoch, Public: append(ed25519.PublicKey(nil), decoded...),
				Raw:          append([]byte(nil), raw...),
				PublicSHA256: publicSHA256, KeyringSHA256: aiPanoramaRawSHA256(raw),
				ActivationTime: activation, AcceptUntil: acceptUntil, RevokedAt: revokedAt,
			}
		}
		zero(decoded)
	}
	if previousEpoch != rotation || selected == nil ||
		!aiPanoramaTimeWithinKey(selected, issuedAt) ||
		!aiPanoramaTimeWithinKey(selected, observedAt) {
		if selected != nil {
			selected.release()
		}
		return nil, fmt.Errorf("ai-panorama-keyring-private-key-not-active")
	}
	return selected, nil
}

func aiPanoramaPermitFields() []string {
	return []string{
		"audience", "issuer", "operation", "subject", "actor_principal_id",
		"owner_principal_id", "search_run_id", "candidate_ref", "external_id",
		"listing_url", "source_ref", "provider_key", "expected_slug",
		"expected_source_tree_sha256", "expected_tour_sha256",
		"expected_core_manifest_sha256", "expected_materialization_receipt_sha256",
		"expected_candidate_marker_sha256", "expected_publication_record_sha256",
		"artifact_relpath", "materialization_receipt_relpath", "request_id",
		"repository", "git_ref", "git_head_sha", "workflow_ref", "job",
		"environment", "review_receipt_sha256", "web_image", "web_image_id",
		"key_usage", "key_epoch", "key_sha256", "keyring_sha256",
		"volume_profile_sha256", "compose_plan_sha256", "volume_id",
		"artifact_root_device", "artifact_root_inode", "public_tour_root_device",
		"public_tour_root_inode", "execution_lease_seconds", "issued_at",
		"expires_at", "nonce",
	}
}

func buildAiPanoramaPermit(
	config *Config,
	identity *Identity,
	discovery *aiPanoramaDiscoveryResult,
	runtime *aiPanoramaRuntimeObservation,
	sealed *aiPanoramaSealedArtifactObservation,
	signing *aiPanoramaSigningContext,
	key *aiPanoramaPurposeKey,
	issuedAt time.Time,
) (map[string]any, error) {
	if config == nil || identity == nil || discovery == nil || runtime == nil ||
		sealed == nil || signing == nil || key == nil ||
		!aiPanoramaNoncePattern.MatchString(discovery.RequestID) ||
		!aiPanoramaSafeIDPattern.MatchString(discovery.OwnerPrincipalID) ||
		!aiPanoramaRawSHA256Pattern.MatchString(discovery.ExpectedPublicationRecordSHA256) ||
		issuedAt.IsZero() || issuedAt.Location() != time.UTC {
		return nil, fmt.Errorf("ai-panorama-permit-build-input-invalid")
	}
	nonce, err := newAiPanoramaInstanceID()
	if err != nil || nonce == discovery.RequestID {
		return nil, fmt.Errorf("ai-panorama-permit-nonce-failed")
	}
	expiresAt := issuedAt.Add(3 * time.Minute)
	permit := map[string]any{
		"audience": "propertyquarry-ai-panorama-install-controller",
		"issuer":   "propertyquarry-release-control", "operation": aiPanoramaInstallOperation,
		"subject": identity.Subject, "actor_principal_id": signing.ActorPrincipalID,
		"owner_principal_id": discovery.OwnerPrincipalID,
		"search_run_id":      "98bed75e984549c6bd4371d602662ab8",
		"candidate_ref":      "053ad185e1c44b2e", "external_id": "1807240910",
		"listing_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/naehe-prater-und-messe-wien-i-u1-u2-i-ruhelage-i-garage-i-maisonette-i-voll-moebliert-i-in-der-vorgartenstrasse-1807240910/",
		"source_ref":  "property-scout:1807240910", "provider_key": "willhaben",
		"expected_slug":                           aiPanoramaPraterSlug,
		"expected_source_tree_sha256":             aiPanoramaExpectedSourceTree,
		"expected_tour_sha256":                    aiPanoramaExpectedTourDigest,
		"expected_core_manifest_sha256":           aiPanoramaExpectedCoreDigest,
		"expected_materialization_receipt_sha256": aiPanoramaExpectedReceiptDigest,
		"expected_candidate_marker_sha256":        aiPanoramaExpectedMarkerDigest,
		"expected_publication_record_sha256":      discovery.ExpectedPublicationRecordSHA256,
		"artifact_relpath":                        "bundle/" + aiPanoramaPraterSlug,
		"materialization_receipt_relpath":         "materialization.receipt.json",
		"request_id":                              discovery.RequestID,
		"repository":                              Repository, "git_ref": identity.Ref,
		"git_head_sha": identity.CandidateSHA, "workflow_ref": identity.WorkflowRef,
		"job": ReleaseJob, "environment": identity.Environment,
		"review_receipt_sha256": signing.ReviewReceiptSHA256,
		"web_image":             config.WebImage, "web_image_id": runtime.ImageID,
		"key_usage":  aiPanoramaPermitKeyUsage,
		"key_epoch":  json.Number(fmt.Sprintf("%d", key.Epoch)),
		"key_sha256": key.PublicSHA256, "keyring_sha256": key.KeyringSHA256,
		"volume_profile_sha256":   signing.VolumeProfileSHA256,
		"compose_plan_sha256":     signing.ComposePlanSHA256,
		"volume_id":               aiPanoramaVolumeID,
		"artifact_root_device":    json.Number(fmt.Sprintf("%d", sealed.RootDevice)),
		"artifact_root_inode":     json.Number(fmt.Sprintf("%d", sealed.RootInode)),
		"public_tour_root_device": json.Number(fmt.Sprintf("%d", runtime.PublicVolumeDevice)),
		"public_tour_root_inode":  json.Number(fmt.Sprintf("%d", runtime.PublicVolumeInode)),
		"execution_lease_seconds": json.Number(fmt.Sprintf("%d", signing.ExecutionLeaseSeconds)),
		"issued_at":               issuedAt.Format(time.RFC3339),
		"expires_at":              expiresAt.Format(time.RFC3339),
		"nonce":                   nonce,
	}
	if err := validateAiPanoramaPermitForSigning(permit, key, issuedAt); err != nil {
		return nil, err
	}
	return permit, nil
}

func validateAiPanoramaPermitForSigning(permit map[string]any, key *aiPanoramaPurposeKey, issuedAt time.Time) error {
	if permit == nil || key == nil || !hasKeys(permit, aiPanoramaPermitFields()...) ||
		permit["audience"] != "propertyquarry-ai-panorama-install-controller" ||
		permit["issuer"] != "propertyquarry-release-control" ||
		permit["operation"] != aiPanoramaInstallOperation ||
		permit["subject"] != ImmutableOIDCSubjectPrefix+":environment:"+Environment ||
		permit["actor_principal_id"] != "propertyquarry-release-controller" ||
		permit["search_run_id"] != "98bed75e984549c6bd4371d602662ab8" ||
		permit["candidate_ref"] != "053ad185e1c44b2e" ||
		permit["external_id"] != "1807240910" ||
		permit["listing_url"] != "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/naehe-prater-und-messe-wien-i-u1-u2-i-ruhelage-i-garage-i-maisonette-i-voll-moebliert-i-in-der-vorgartenstrasse-1807240910/" ||
		permit["source_ref"] != "property-scout:1807240910" ||
		permit["provider_key"] != "willhaben" ||
		permit["expected_slug"] != aiPanoramaPraterSlug ||
		permit["expected_source_tree_sha256"] != aiPanoramaExpectedSourceTree ||
		permit["expected_tour_sha256"] != aiPanoramaExpectedTourDigest ||
		permit["expected_core_manifest_sha256"] != aiPanoramaExpectedCoreDigest ||
		permit["expected_materialization_receipt_sha256"] != aiPanoramaExpectedReceiptDigest ||
		permit["expected_candidate_marker_sha256"] != aiPanoramaExpectedMarkerDigest ||
		permit["artifact_relpath"] != "bundle/"+aiPanoramaPraterSlug ||
		permit["materialization_receipt_relpath"] != "materialization.receipt.json" ||
		permit["repository"] != Repository || permit["git_ref"] != "refs/heads/main" ||
		permit["workflow_ref"] != WorkflowRef ||
		permit["job"] != ReleaseJob || permit["environment"] != Environment ||
		permit["key_usage"] != aiPanoramaPermitKeyUsage ||
		permit["key_sha256"] != key.PublicSHA256 ||
		permit["keyring_sha256"] != key.KeyringSHA256 ||
		permit["volume_id"] != aiPanoramaVolumeID {
		return fmt.Errorf("ai-panorama-permit-binding-invalid")
	}
	keyEpoch, keyEpochOK := exactInt(permit["key_epoch"], key.Epoch, key.Epoch)
	_, leaseOK := exactInt(permit["execution_lease_seconds"], 1, 900)
	artifactDevice, artifactDeviceOK := exactInt(permit["artifact_root_device"], 1, 1<<62)
	artifactInode, artifactInodeOK := exactInt(permit["artifact_root_inode"], 1, 1<<62)
	publicDevice, publicDeviceOK := exactInt(permit["public_tour_root_device"], 1, 1<<62)
	publicInode, publicInodeOK := exactInt(permit["public_tour_root_inode"], 1, 1<<62)
	issued, issuedOK := parseAiPanoramaTimestamp(permit["issued_at"])
	expires, expiresOK := parseAiPanoramaTimestamp(permit["expires_at"])
	requestID, requestOK := exactString(permit["request_id"])
	nonce, nonceOK := exactString(permit["nonce"])
	gitHead, gitHeadOK := exactString(permit["git_head_sha"])
	webImage, webImageOK := exactString(permit["web_image"])
	webImageID, webImageIDOK := exactString(permit["web_image_id"])
	if !keyEpochOK || keyEpoch != key.Epoch || !leaseOK ||
		!artifactDeviceOK || artifactDevice < 1 || !artifactInodeOK || artifactInode < 1 ||
		!publicDeviceOK || publicDevice < 1 || !publicInodeOK || publicInode < 1 ||
		!issuedOK || !issued.Equal(issuedAt) || !expiresOK ||
		expires.Sub(issued) <= 0 || expires.Sub(issued) > 5*time.Minute ||
		!requestOK || !aiPanoramaNoncePattern.MatchString(requestID) ||
		!nonceOK || !aiPanoramaNoncePattern.MatchString(nonce) ||
		!gitHeadOK || !shaPattern.MatchString(gitHead) ||
		!webImageOK || !imagePattern.MatchString(webImage) ||
		!webImageIDOK || !digestPattern.MatchString(webImageID) ||
		!aiPanoramaTimeWithinKey(key, issued) {
		return fmt.Errorf("ai-panorama-permit-proof-invalid")
	}
	for _, field := range []string{
		"expected_publication_record_sha256", "review_receipt_sha256",
		"volume_profile_sha256", "compose_plan_sha256",
	} {
		text, ok := exactString(permit[field])
		if !ok || !aiPanoramaRawSHA256Pattern.MatchString(text) {
			return fmt.Errorf("ai-panorama-permit-digest-invalid")
		}
	}
	for _, field := range []string{"subject", "owner_principal_id"} {
		text, ok := exactString(permit[field])
		if !ok || !aiPanoramaSafeIDPattern.MatchString(text) {
			return fmt.Errorf("ai-panorama-permit-principal-invalid")
		}
	}
	return nil
}

func signAiPanoramaPermit(root string, permit map[string]any, private ed25519.PrivateKey, issuedAt, observedAt time.Time) ([]byte, *aiPanoramaSignedPermit, error) {
	key, err := loadAiPanoramaPurposeKey(root, private, issuedAt, observedAt)
	if err != nil {
		return nil, nil, err
	}
	defer key.release()
	if err := validateAiPanoramaPermitForSigning(permit, key, issuedAt); err != nil {
		return nil, nil, err
	}
	signatureContext := map[string]any{
		"algorithm": "Ed25519", "key_id": key.KeyID, "encoding": "base64url",
	}
	body := map[string]any{
		"domain": aiPanoramaPermitSignatureDomain, "schema": aiPanoramaPermitSchema,
		"version": json.Number("2"), "permit": permit,
		"signature_context": signatureContext,
	}
	canonicalBody, err := aiPanoramaCanonicalJSON(body)
	if err != nil {
		return nil, nil, fmt.Errorf("ai-panorama-permit-preimage-invalid")
	}
	defer zero(canonicalBody)
	preimage := make([]byte, 0, len(aiPanoramaPermitSignatureDomain)+1+8+len(canonicalBody))
	preimage = append(preimage, aiPanoramaPermitSignatureDomain...)
	preimage = append(preimage, 0)
	length := make([]byte, 8)
	binary.BigEndian.PutUint64(length, uint64(len(canonicalBody)))
	preimage = append(preimage, length...)
	zero(length)
	preimage = append(preimage, canonicalBody...)
	signature := ed25519.Sign(private, preimage)
	if !ed25519.Verify(key.Public, preimage, signature) {
		zero(preimage)
		zero(signature)
		return nil, nil, fmt.Errorf("ai-panorama-permit-signature-self-check-failed")
	}
	preimageSHA256 := aiPanoramaRawSHA256(preimage)
	zero(preimage)
	envelope := map[string]any{
		"schema": aiPanoramaPermitSchema, "version": json.Number("2"),
		"permit": permit,
		"signature": map[string]any{
			"algorithm": "Ed25519", "key_id": key.KeyID, "encoding": "base64url",
			"value": base64.RawURLEncoding.EncodeToString(signature),
		},
	}
	zero(signature)
	raw, err := aiPanoramaCanonicalJSON(envelope)
	if err != nil || len(raw) < 2 || len(raw)+1 > aiPanoramaMaximumPermit {
		zero(raw)
		return nil, nil, fmt.Errorf("ai-panorama-permit-envelope-invalid")
	}
	raw = append(raw, '\n')
	issuedText, _ := exactString(permit["issued_at"])
	expiresText, _ := exactString(permit["expires_at"])
	lease, _ := exactInt(permit["execution_lease_seconds"], 1, 900)
	requestID, _ := exactString(permit["request_id"])
	relpath, pathErr := aiPanoramaPermitRelpath(requestID)
	path, pathErr2 := aiPanoramaPermitPath(requestID)
	if pathErr != nil || pathErr2 != nil {
		zero(raw)
		return nil, nil, fmt.Errorf("ai-panorama-permit-path-invalid")
	}
	return raw, &aiPanoramaSignedPermit{
		RequestID: requestID, Relpath: relpath, Path: path,
		SHA256: aiPanoramaRawSHA256(raw), PreimageSHA256: preimageSHA256,
		KeyID: key.KeyID, KeyEpoch: key.Epoch, KeySHA256: key.PublicSHA256,
		KeyringSHA256: key.KeyringSHA256, IssuedAt: issuedText,
		ExpiresAt: expiresText, ExecutionLease: lease, CanonicalLength: len(raw),
	}, nil
}

func persistAiPanoramaPermit(root string, raw []byte, observation *aiPanoramaSignedPermit) error {
	if root == "" {
		root = "/"
	}
	if observation == nil || len(raw) < 2 || len(raw) > aiPanoramaMaximumPermit ||
		raw[len(raw)-1] != '\n' || aiPanoramaRawSHA256(raw) != observation.SHA256 ||
		!aiPanoramaNoncePattern.MatchString(observation.RequestID) ||
		filepath.Dir(observation.Path) != aiPanoramaPermitRoot {
		return fmt.Errorf("ai-panorama-permit-persist-input-invalid")
	}
	expectedRelpath, pathErr := aiPanoramaPermitRelpath(observation.RequestID)
	expectedPath, pathErr2 := aiPanoramaPermitPath(observation.RequestID)
	if pathErr != nil || pathErr2 != nil ||
		observation.Relpath != expectedRelpath || observation.Path != expectedPath {
		return fmt.Errorf("ai-panorama-permit-persist-input-invalid")
	}
	ownerUID, ownerGID := secureOwner(root)
	permitRoot := rooted(root, aiPanoramaPermitRoot)
	if err := validateExternalParentChain(root, permitRoot, ownerUID, ownerGID); err != nil {
		return fmt.Errorf("ai-panorama-permit-root-parent-invalid")
	}
	rootInfo, err := os.Lstat(permitRoot)
	if err != nil || !rootInfo.IsDir() || rootInfo.Mode().Perm() != 0o700 ||
		rootInfo.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("ai-panorama-permit-root-invalid")
	}
	if metadata, ok := infoSys(rootInfo); !ok ||
		metadata.Uid != ownerUID || metadata.Gid != ownerGID || metadata.Nlink < 2 {
		return fmt.Errorf("ai-panorama-permit-root-invalid")
	}
	return persistAiPanoramaProjectionFile(root, &aiPanoramaProjection{
		Kind: "permit", Path: observation.Path, Mode: 0o600,
		SHA256: observation.SHA256, Raw: raw,
	})
}

func readAiPanoramaPersistedPermit(
	root string,
	requestID string,
	expectedSHA256 string,
) (map[string]any, []byte, error) {
	if root == "" {
		root = "/"
	}
	path, pathErr := aiPanoramaPermitPath(requestID)
	if pathErr != nil || !aiPanoramaRawSHA256Pattern.MatchString(expectedSHA256) {
		return nil, nil, fmt.Errorf("ai-panorama-permit-binding-invalid")
	}
	ownerUID, ownerGID := secureOwner(root)
	raw, err := secureRead(
		root, path, 0o600,
		ownerUID, ownerGID, aiPanoramaMaximumPermit,
	)
	if err != nil || len(raw) < 3 || raw[len(raw)-1] != '\n' ||
		raw[len(raw)-2] == '\n' || bytes.IndexByte(raw[:len(raw)-1], '\n') >= 0 ||
		aiPanoramaRawSHA256(raw) != expectedSHA256 {
		zero(raw)
		return nil, nil, fmt.Errorf("ai-panorama-permit-binding-invalid")
	}
	envelope, err := strictJSON(raw[:len(raw)-1], aiPanoramaMaximumPermit)
	canonical, canonicalErr := canonicalJSON(envelope)
	canonical = append(canonical, '\n')
	valid := err == nil && canonicalErr == nil && bytes.Equal(canonical, raw) &&
		hasKeys(envelope, "schema", "version", "permit", "signature") &&
		envelope["schema"] == aiPanoramaPermitSchema &&
		envelope["version"] == json.Number("2")
	zero(canonical)
	permit, permitOK := envelope["permit"].(map[string]any)
	signature, signatureOK := envelope["signature"].(map[string]any)
	if !valid || !permitOK || permit["request_id"] != requestID ||
		!signatureOK || !hasKeys(
		signature, "algorithm", "key_id", "encoding", "value",
	) || signature["algorithm"] != "Ed25519" ||
		signature["encoding"] != "base64url" {
		zero(raw)
		return nil, nil, fmt.Errorf("ai-panorama-permit-binding-invalid")
	}
	return permit, raw, nil
}
