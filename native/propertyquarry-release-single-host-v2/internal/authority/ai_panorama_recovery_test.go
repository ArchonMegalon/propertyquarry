//go:build linux && amd64

package authority

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func aiPanoramaTestRecoveryResultWire(
	t *testing.T,
	overrides map[string]any,
) []byte {
	t.Helper()
	value := map[string]any{
		"schema":                           aiPanoramaRecoveryClassificationSchema,
		"version":                          json.Number("1"),
		"authority":                        "propertyquarry-release-control",
		"status":                           "classified",
		"classification":                   "failed-clean",
		"request_id_sha256":                strings.Repeat("1", 64),
		"permit_sha256":                    strings.Repeat("2", 64),
		"operation_id_sha256":              strings.Repeat("3", 64),
		"operation_terminal_entry_sha256":  strings.Repeat("4", 64),
		"terminal_receipt_sha256":          strings.Repeat("5", 64),
		"database_mutation_performed":      false,
		"public_target_mutation_performed": false,
		"retry_authorized":                 false,
		"private_values_redacted":          true,
	}
	for key, raw := range overrides {
		value[key] = raw
	}
	wire, err := canonicalJSON(value)
	if err != nil {
		t.Fatal(err)
	}
	return append(wire, '\n')
}

func TestAiPanoramaRecoveryClassificationResultIsExactAndRetryClosed(t *testing.T) {
	raw := aiPanoramaTestRecoveryResultWire(t, nil)
	result, err := parseAiPanoramaRecoveryClassificationResult(raw)
	if err != nil || result.Classification != "failed-clean" ||
		result.RawSHA256 != aiPanoramaRawSHA256(raw) {
		t.Fatal("valid classifier result rejected")
	}
	if _, err := parseAiPanoramaRecoveryClassificationResult(
		aiPanoramaTestRecoveryResultWire(
			t, map[string]any{"retry_authorized": true},
		),
	); err == nil {
		t.Fatal("retry-authorized classifier result accepted")
	}
	if _, err := parseAiPanoramaRecoveryClassificationResult(
		append(append([]byte(nil), raw...), '\n'),
	); err == nil {
		t.Fatal("multi-line classifier result accepted")
	}
}

func TestAiPanoramaHistoricalRecoveryContainerMountsOnlyExactLeaves(t *testing.T) {
	_, archive := aiPanoramaTestContextArchive(t)
	config := &Config{
		Digest:       strings.Repeat("a", 64),
		DeploymentID: strings.Repeat("b", 64),
		WebImage:     "registry.example/property@sha256:" + strings.Repeat("c", 64),
	}
	runtime := &aiPanoramaRuntimeObservation{
		PublicVolumeMountpoint: "/var/lib/docker/volumes/example/_data",
	}
	sealed := &aiPanoramaSealedArtifactObservation{FileCount: 1, TotalBytes: 1}
	network := &aiPanoramaNetworkObservation{
		Name: "pq-ai-panorama-" + strings.Repeat("b", 12),
		ID:   strings.Repeat("d", 64),
	}
	arguments, err := aiPanoramaHistoricalRecoveryContainerArguments(
		config, runtime, sealed, network,
		aiPanoramaDatabaseSecretMount, archive,
	)
	if err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(arguments, "\n")
	if strings.Contains(
		joined,
		"src="+archive.Path+",dst="+archive.Path,
	) || strings.Contains(
		joined,
		"src="+aiPanoramaPurposeKeyringPath+",dst="+aiPanoramaPurposeKeyringPath,
	) || !strings.Contains(joined, aiPanoramaRecoveryEntrypoint) {
		t.Fatal("classifier received an archive parent or current keyring source")
	}
	for _, file := range archive.Files {
		expected := "type=bind,src=" + file.Path + ",dst=" +
			file.MountTarget + ",bind-propagation=rprivate,readonly"
		if strings.Count(joined, expected) != 1 {
			t.Fatalf("missing exact historical leaf mount: %s", file.Kind)
		}
	}
	mounts, err := aiPanoramaRecoveryMountContract(
		runtime, "recovery", archive,
	)
	if err != nil || len(mounts) != 8 {
		t.Fatal("strict classifier recovery mount contract missing")
	}
	for _, mount := range mounts {
		if mount.Destination == aiPanoramaPublicMountTarget && mount.ReadWrite {
			t.Fatal("classifier received a writable public target")
		}
	}
}

func TestAiPanoramaRecoveryClassificationTerminalBindsControlRoot(t *testing.T) {
	root := t.TempDir()
	for _, relative := range []string{
		"var", "var/lib", "var/lib/propertyquarry",
		"var/lib/propertyquarry/release-control",
		"var/lib/propertyquarry/release-control/ai-panorama-install",
	} {
		if err := os.Mkdir(filepath.Join(root, relative), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	requestID := "0123456789abcdef0123456789abcdef"
	requestSHA256 := aiPanoramaRawSHA256([]byte(requestID))
	result := &aiPanoramaRecoveryClassificationResult{
		Classification:               "rolled-back",
		RequestIDSHA256:              requestSHA256,
		PermitSHA256:                 strings.Repeat("2", 64),
		OperationIDSHA256:            strings.Repeat("3", 64),
		OperationTerminalEntrySHA256: strings.Repeat("4", 64),
	}
	evidenceSHA256 := strings.Repeat("6", 64)
	value := map[string]any{
		"schema": aiPanoramaTerminalSchema, "version": json.Number("1"),
		"authority":                          "propertyquarry-release-control",
		"status":                             result.Classification,
		"request_id_sha256":                  requestSHA256,
		"permit_sha256":                      result.PermitSHA256,
		"operation_id_sha256":                result.OperationIDSHA256,
		"operation_terminal_entry_sha256":    result.OperationTerminalEntrySHA256,
		"operation_terminal_evidence_sha256": evidenceSHA256,
		"database_mutation_performed":        false,
		"public_target_mutation_performed":   false,
		"private_values_redacted":            true,
	}
	raw, err := canonicalJSON(value)
	if err != nil {
		t.Fatal(err)
	}
	raw = append(raw, '\n')
	result.TerminalReceiptSHA256 = aiPanoramaRawSHA256(raw)
	path, err := aiPanoramaTerminalPath(requestID)
	if err != nil || os.WriteFile(rooted(root, path), raw, 0o600) != nil {
		t.Fatal("failed to create classifier terminal")
	}
	controlInfo, err := os.Lstat(rooted(root, aiPanoramaControlRoot))
	controlMetadata, ok := infoSys(controlInfo)
	if err != nil || !ok {
		t.Fatal("failed to observe control root")
	}
	runtime := &aiPanoramaRuntimeObservation{
		ControlRootDevice: uint64(controlMetadata.Dev),
		ControlRootInode:  controlMetadata.Ino,
	}
	observedEvidence, err := readAiPanoramaRecoveryClassificationTerminal(
		root, requestID, runtime, result,
	)
	if err != nil || observedEvidence != evidenceSHA256 {
		t.Fatal("valid classifier terminal rejected")
	}
	runtime.ControlRootInode++
	if _, err := readAiPanoramaRecoveryClassificationTerminal(
		root, requestID, runtime, result,
	); err == nil {
		t.Fatal("classifier terminal accepted against replaced control root")
	}
}
