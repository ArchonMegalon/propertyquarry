//go:build linux && amd64

package authority

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"syscall"
)

const (
	aiPanoramaDiscoveryRequestSchema = "propertyquarry.prater-ai-panorama-record-discovery-request.v1"
	aiPanoramaDiscoveryResultSchema  = "propertyquarry.prater-ai-panorama-record-discovery-result.v1"
	aiPanoramaDiscoveryRequestPath   = aiPanoramaControlRoot + "/prater-record-discovery-request.v1.json"
	aiPanoramaMaximumDiscoveryBytes  = 64 * 1024
)

type aiPanoramaDiscoveryRequest struct {
	RequestID string
	Path      string
	SHA256    string
}

type aiPanoramaDiscoveryResult struct {
	RequestID                       string
	OwnerPrincipalID                string
	ExpectedPublicationRecordSHA256 string
	RawSHA256                       string
}

func (result *aiPanoramaDiscoveryResult) release() {
	if result == nil {
		return
	}
	result.OwnerPrincipalID = ""
	*result = aiPanoramaDiscoveryResult{}
}

func newAiPanoramaDiscoveryRequest(root string) (*aiPanoramaDiscoveryRequest, error) {
	if root == "" {
		root = "/"
	}
	identifier := make([]byte, 16)
	if _, err := rand.Read(identifier); err != nil {
		zero(identifier)
		return nil, fmt.Errorf("ai-panorama-discovery-request-random-failed")
	}
	requestID := hex.EncodeToString(identifier)
	zero(identifier)
	value := map[string]any{
		"schema": aiPanoramaDiscoveryRequestSchema, "version": json.Number("1"),
		"authority": "propertyquarry-release-control", "status": "requested",
		"request_id": requestID,
	}
	raw, err := canonicalJSON(value)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-discovery-request-invalid")
	}
	raw = append(raw, '\n')
	defer zero(raw)
	return &aiPanoramaDiscoveryRequest{
		RequestID: requestID, Path: aiPanoramaDiscoveryRequestPath,
		SHA256: aiPanoramaRawSHA256(raw),
	}, nil
}

func aiPanoramaDiscoveryRequestWire(requestID string) ([]byte, error) {
	if !aiPanoramaNoncePattern.MatchString(requestID) {
		return nil, fmt.Errorf("ai-panorama-discovery-request-invalid")
	}
	raw, err := canonicalJSON(map[string]any{
		"schema": aiPanoramaDiscoveryRequestSchema, "version": json.Number("1"),
		"authority": "propertyquarry-release-control", "status": "requested",
		"request_id": requestID,
	})
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-discovery-request-invalid")
	}
	return append(raw, '\n'), nil
}

func readAiPanoramaDiscoveryRequest(
	root string,
	expectedSHA256 string,
) (*aiPanoramaDiscoveryRequest, []byte, error) {
	if !aiPanoramaRawSHA256Pattern.MatchString(expectedSHA256) {
		return nil, nil, fmt.Errorf("ai-panorama-discovery-request-binding-invalid")
	}
	ownerUID, ownerGID := secureOwner(root)
	raw, err := secureRead(
		root, aiPanoramaDiscoveryRequestPath, 0o600,
		ownerUID, ownerGID, aiPanoramaMaximumDiscoveryBytes,
	)
	if err != nil || aiPanoramaRawSHA256(raw) != expectedSHA256 ||
		len(raw) < 3 || raw[len(raw)-1] != '\n' || raw[len(raw)-2] == '\n' ||
		bytes.IndexByte(raw[:len(raw)-1], '\n') >= 0 {
		zero(raw)
		return nil, nil, fmt.Errorf("ai-panorama-discovery-request-binding-invalid")
	}
	value, err := strictJSON(raw[:len(raw)-1], aiPanoramaMaximumDiscoveryBytes)
	requestID, requestOK := exactString(value["request_id"])
	expected, wireErr := aiPanoramaDiscoveryRequestWire(requestID)
	valid := err == nil && requestOK && wireErr == nil && bytes.Equal(expected, raw)
	zero(expected)
	if !valid {
		zero(raw)
		return nil, nil, fmt.Errorf("ai-panorama-discovery-request-binding-invalid")
	}
	return &aiPanoramaDiscoveryRequest{
		RequestID: requestID, Path: aiPanoramaDiscoveryRequestPath,
		SHA256: expectedSHA256,
	}, raw, nil
}

func removeAiPanoramaDiscoveryRequest(root string, expectedSHA256 string) error {
	target := rooted(root, aiPanoramaDiscoveryRequestPath)
	if _, err := os.Lstat(target); os.IsNotExist(err) {
		return nil
	} else if err != nil {
		return fmt.Errorf("ai-panorama-discovery-request-cleanup-observation-failed")
	}
	request, raw, err := readAiPanoramaDiscoveryRequest(root, expectedSHA256)
	if request != nil {
		request.RequestID = ""
	}
	zero(raw)
	if err != nil {
		return err
	}
	if err := os.Remove(target); err != nil {
		return fmt.Errorf("ai-panorama-discovery-request-cleanup-failed")
	}
	if err := fsyncAiPanoramaDirectory(rooted(root, aiPanoramaControlRoot)); err != nil {
		return fmt.Errorf("ai-panorama-discovery-request-cleanup-durability-unknown")
	}
	return nil
}

func persistAiPanoramaDiscoveryRequest(root string, raw []byte) error {
	if len(raw) < 3 || len(raw) > aiPanoramaMaximumDiscoveryBytes ||
		raw[len(raw)-1] != '\n' || raw[len(raw)-2] == '\n' {
		return fmt.Errorf("ai-panorama-discovery-request-persist-input-invalid")
	}
	value, err := strictJSON(raw[:len(raw)-1], aiPanoramaMaximumDiscoveryBytes)
	if err != nil || !hasKeys(value, "schema", "version", "authority", "status", "request_id") ||
		value["schema"] != aiPanoramaDiscoveryRequestSchema ||
		value["version"] != json.Number("1") ||
		value["authority"] != "propertyquarry-release-control" ||
		value["status"] != "requested" {
		return fmt.Errorf("ai-panorama-discovery-request-persist-input-invalid")
	}
	requestID, requestOK := exactString(value["request_id"])
	if !requestOK || !aiPanoramaNoncePattern.MatchString(requestID) {
		return fmt.Errorf("ai-panorama-discovery-request-persist-input-invalid")
	}
	ownerUID, ownerGID := secureOwner(root)
	controlRoot := rooted(root, aiPanoramaControlRoot)
	info, err := os.Lstat(controlRoot)
	if err != nil {
		return fmt.Errorf("ai-panorama-control-root-invalid")
	}
	metadata, metadataOK := infoSys(info)
	if !metadataOK || !info.IsDir() || info.Mode().Perm() != 0o700 ||
		info.Mode()&os.ModeSymlink != 0 || metadata.Uid != ownerUID ||
		metadata.Gid != ownerGID || metadata.Nlink < 2 {
		return fmt.Errorf("ai-panorama-control-root-invalid")
	}
	target := rooted(root, aiPanoramaDiscoveryRequestPath)
	if existing, err := readSecureFile(
		target, 0o600, ownerUID, ownerGID, aiPanoramaMaximumDiscoveryBytes,
	); err == nil {
		defer zero(existing)
		if !bytes.Equal(existing, raw) {
			return fmt.Errorf("ai-panorama-discovery-request-conflict")
		}
		return nil
	} else if _, statErr := os.Lstat(target); !os.IsNotExist(statErr) {
		return fmt.Errorf("ai-panorama-discovery-request-conflict")
	}
	file, err := os.OpenFile(
		target, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0o600,
	)
	if err != nil {
		return fmt.Errorf("ai-panorama-discovery-request-create-failed")
	}
	if err := writeAll(file, raw); err != nil || file.Sync() != nil || file.Close() != nil {
		_ = file.Close()
		return fmt.Errorf("ai-panorama-discovery-request-write-failed")
	}
	if err := fsyncAiPanoramaDirectory(controlRoot); err != nil {
		return fmt.Errorf("ai-panorama-discovery-request-durability-unknown")
	}
	return nil
}

func parseAiPanoramaDiscoveryResult(raw []byte, expectedRequestID string) (*aiPanoramaDiscoveryResult, error) {
	if len(raw) < 3 || len(raw) > aiPanoramaMaximumDiscoveryBytes ||
		raw[len(raw)-1] != '\n' || raw[len(raw)-2] == '\n' ||
		bytes.IndexByte(raw[:len(raw)-1], '\n') >= 0 ||
		!aiPanoramaNoncePattern.MatchString(expectedRequestID) {
		return nil, fmt.Errorf("ai-panorama-discovery-result-framing-invalid")
	}
	value, err := strictJSON(raw[:len(raw)-1], aiPanoramaMaximumDiscoveryBytes)
	if err != nil || !hasKeys(value,
		"schema", "version", "authority", "status", "owner_principal_id",
		"search_run_id", "candidate_ref", "expected_publication_record_sha256",
		"request_id", "database_mutation_performed", "release_authorized",
		"private_projection",
	) || value["schema"] != aiPanoramaDiscoveryResultSchema ||
		value["version"] != json.Number("1") ||
		value["authority"] != "propertyquarry-release-control" ||
		value["status"] != "discovered" ||
		value["search_run_id"] != "98bed75e984549c6bd4371d602662ab8" ||
		value["candidate_ref"] != "053ad185e1c44b2e" ||
		value["request_id"] != expectedRequestID ||
		value["database_mutation_performed"] != false ||
		value["release_authorized"] != false ||
		value["private_projection"] != true {
		return nil, fmt.Errorf("ai-panorama-discovery-result-binding-invalid")
	}
	owner, ownerOK := exactString(value["owner_principal_id"])
	publication, publicationOK := exactString(value["expected_publication_record_sha256"])
	if !ownerOK || !aiPanoramaSafeIDPattern.MatchString(owner) ||
		!publicationOK || !aiPanoramaRawSHA256Pattern.MatchString(publication) {
		return nil, fmt.Errorf("ai-panorama-discovery-result-private-projection-invalid")
	}
	return &aiPanoramaDiscoveryResult{
		RequestID: expectedRequestID, OwnerPrincipalID: owner,
		ExpectedPublicationRecordSHA256: publication,
		RawSHA256:                       aiPanoramaRawSHA256(raw),
	}, nil
}

func aiPanoramaRequestIDHash(requestID string) (string, error) {
	if !aiPanoramaNoncePattern.MatchString(requestID) {
		return "", fmt.Errorf("ai-panorama-request-id-invalid")
	}
	return aiPanoramaRawSHA256([]byte(requestID)), nil
}
