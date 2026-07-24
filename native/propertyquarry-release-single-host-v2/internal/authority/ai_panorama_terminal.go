//go:build linux && amd64

package authority

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

const (
	aiPanoramaTerminalSchema       = "propertyquarry.prater-ai-panorama-terminal-receipt.v1"
	aiPanoramaMaximumTerminalBytes = 2 * 1024 * 1024
	aiPanoramaPraterControlPath    = "/tours/" + aiPanoramaPraterSlug + "/control"
	aiPanoramaInstallReceiptSchema = "propertyquarry.ai_panorama_sealed_install_receipt.v1"
	aiPanoramaInstallRequestSchema = "propertyquarry.ai_panorama_sealed_install_request.v2"
	aiPanoramaPropertyURLSHA256    = "f451d904167c5b1a2b27f698ec38c18f6760fe55b79cca32c99bc986f8293d8e"
	aiPanoramaSourceIdentitySchema = "propertyquarry.ai_panorama_installer_source_identity.v1"
	aiPanoramaSourceTreeAlgorithm  = "sha256-canonical-json-sorted-file-records.v1"
	aiPanoramaSourcePathSemantics  = "sealed-bundle-root-relative-posix-paths"
)

type aiPanoramaPhaseResult struct {
	Phase                 string
	Status                string
	TerminalReceiptSHA256 string
	RawSHA256             string
}

type aiPanoramaTerminalObservation struct {
	Status            string
	RequestIDSHA256   string
	PermitSHA256      string
	ReceiptSHA256     string
	BindingStatus     string
	BeforeSHA256      string
	AfterSHA256       string
	ControlRootDevice uint64
	ControlRootInode  uint64
}

func parseAiPanoramaPhaseResult(raw []byte, phase string) (*aiPanoramaPhaseResult, error) {
	if (phase != "preflight" && phase != "apply") || len(raw) < 3 ||
		len(raw) > aiPanoramaMaximumCommandOutput || raw[len(raw)-1] != '\n' ||
		raw[len(raw)-2] == '\n' || bytes.IndexByte(raw[:len(raw)-1], '\n') >= 0 {
		return nil, fmt.Errorf("ai-panorama-phase-result-framing-invalid")
	}
	value, err := strictJSON(raw[:len(raw)-1], aiPanoramaMaximumCommandOutput)
	if err != nil || value["schema"] != aiPanoramaTerminalSchema ||
		value["slug"] != aiPanoramaPraterSlug ||
		value["private_values_redacted"] != true {
		return nil, fmt.Errorf("ai-panorama-phase-result-invalid")
	}
	result := &aiPanoramaPhaseResult{Phase: phase, RawSHA256: aiPanoramaRawSHA256(raw)}
	if phase == "preflight" {
		if !hasKeys(value,
			"schema", "status", "slug", "nonce_consumed",
			"database_access_performed", "private_values_redacted",
		) || value["status"] != "preflight-passed" ||
			value["nonce_consumed"] != false ||
			value["database_access_performed"] != false {
			return nil, fmt.Errorf("ai-panorama-preflight-result-invalid")
		}
		result.Status = "preflight-passed"
		return result, nil
	}
	if !hasKeys(value,
		"schema", "status", "slug", "control_path",
		"terminal_receipt_sha256", "private_values_redacted",
	) || value["status"] != "committed" ||
		value["control_path"] != aiPanoramaPraterControlPath {
		return nil, fmt.Errorf("ai-panorama-apply-result-invalid")
	}
	terminalSHA256, terminalOK := exactString(value["terminal_receipt_sha256"])
	if !terminalOK || !aiPanoramaRawSHA256Pattern.MatchString(terminalSHA256) {
		return nil, fmt.Errorf("ai-panorama-apply-result-invalid")
	}
	result.Status = "committed"
	result.TerminalReceiptSHA256 = terminalSHA256
	return result, nil
}

func aiPanoramaTerminalPath(requestID string) (string, error) {
	if !aiPanoramaNoncePattern.MatchString(requestID) {
		return "", fmt.Errorf("ai-panorama-terminal-request-id-invalid")
	}
	return filepath.Join(aiPanoramaControlRoot, "terminal-"+requestID+".v1.json"), nil
}

func readAiPanoramaTerminal(
	root string,
	requestID string,
	expectedPermitSHA256 string,
	expectedTerminalSHA256 string,
	runtime *aiPanoramaRuntimeObservation,
	sealed *aiPanoramaSealedArtifactObservation,
	expectedPublicationRecordSHA256 string,
) (*aiPanoramaTerminalObservation, error) {
	if root == "" {
		root = "/"
	}
	path, err := aiPanoramaTerminalPath(requestID)
	if err != nil || runtime == nil ||
		sealed == nil || sealed.FileCount < 1 || sealed.TotalBytes < 1 ||
		!aiPanoramaRawSHA256Pattern.MatchString(expectedPermitSHA256) ||
		!aiPanoramaRawSHA256Pattern.MatchString(expectedPublicationRecordSHA256) ||
		(expectedTerminalSHA256 != "" && !aiPanoramaRawSHA256Pattern.MatchString(expectedTerminalSHA256)) {
		return nil, fmt.Errorf("ai-panorama-terminal-input-invalid")
	}
	ownerUID, ownerGID := secureOwner(root)
	raw, err := secureRead(root, path, 0o600, ownerUID, ownerGID, aiPanoramaMaximumTerminalBytes)
	if err != nil || len(raw) < 3 || raw[len(raw)-1] != '\n' ||
		raw[len(raw)-2] == '\n' || bytes.IndexByte(raw[:len(raw)-1], '\n') >= 0 {
		zero(raw)
		return nil, fmt.Errorf("ai-panorama-terminal-unavailable")
	}
	defer zero(raw)
	terminalSHA256 := aiPanoramaRawSHA256(raw)
	if expectedTerminalSHA256 != "" && terminalSHA256 != expectedTerminalSHA256 {
		return nil, fmt.Errorf("ai-panorama-terminal-digest-invalid")
	}
	value, err := strictJSON(raw[:len(raw)-1], aiPanoramaMaximumTerminalBytes)
	if err != nil || value["schema"] != aiPanoramaTerminalSchema ||
		value["version"] != json.Number("1") ||
		value["authority"] != "propertyquarry-release-control" ||
		value["private_values_redacted"] != true {
		return nil, fmt.Errorf("ai-panorama-terminal-invalid")
	}
	requestIDSHA256, err := aiPanoramaRequestIDHash(requestID)
	if err != nil || value["request_id_sha256"] != requestIDSHA256 {
		return nil, fmt.Errorf("ai-panorama-terminal-request-binding-invalid")
	}
	controlInfo, err := os.Lstat(rooted(root, aiPanoramaControlRoot))
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-terminal-control-root-invalid")
	}
	controlMetadata, controlOK := infoSys(controlInfo)
	if !controlOK || !controlInfo.IsDir() || controlInfo.Mode().Perm() != 0o700 ||
		controlInfo.Mode()&os.ModeSymlink != 0 ||
		uint64(controlMetadata.Dev) != runtime.ControlRootDevice ||
		controlMetadata.Ino != runtime.ControlRootInode {
		return nil, fmt.Errorf("ai-panorama-terminal-control-root-invalid")
	}
	status, statusOK := exactString(value["status"])
	if !statusOK {
		return nil, fmt.Errorf("ai-panorama-terminal-status-invalid")
	}
	observation := &aiPanoramaTerminalObservation{
		Status: status, RequestIDSHA256: requestIDSHA256,
		ReceiptSHA256: terminalSHA256, ControlRootDevice: uint64(controlMetadata.Dev),
		ControlRootInode: controlMetadata.Ino,
	}
	if status != "committed" {
		if !hasKeys(value,
			"schema", "version", "authority", "status", "request_id_sha256",
			"error", "private_values_redacted",
		) || (status != "failed" && status != "failed-clean" &&
			status != "rolled-back" && status != "recovery-required") {
			return nil, fmt.Errorf("ai-panorama-terminal-failure-invalid")
		}
		if errorText, ok := exactString(value["error"]); !ok || len(errorText) > 1024 ||
			bytes.IndexAny([]byte(errorText), "\x00\r\n") >= 0 {
			return nil, fmt.Errorf("ai-panorama-terminal-failure-invalid")
		}
		return observation, nil
	}
	if !hasKeys(value,
		"schema", "version", "authority", "status", "request_id_sha256",
		"permit_sha256", "result", "private_values_redacted",
	) || value["permit_sha256"] != expectedPermitSHA256 {
		return nil, fmt.Errorf("ai-panorama-terminal-permit-binding-invalid")
	}
	result, resultOK := value["result"].(map[string]any)
	if !resultOK || !hasKeys(result,
		"contract", "mode", "status", "slug", "control_path", "install_receipt",
		"binding_status", "binding_receipt", "release_eligible",
		"private_values_redacted",
	) || result["contract"] != "propertyquarry.prater_ai_panorama_governed_release.v1" ||
		result["mode"] != "apply" || result["status"] != "released" ||
		result["slug"] != aiPanoramaPraterSlug ||
		result["control_path"] != aiPanoramaPraterControlPath ||
		result["release_eligible"] != true ||
		result["private_values_redacted"] != true {
		return nil, fmt.Errorf("ai-panorama-terminal-result-invalid")
	}
	bindingStatus, bindingStatusOK := exactString(result["binding_status"])
	binding, bindingOK := result["binding_receipt"].(map[string]any)
	if !bindingStatusOK ||
		(bindingStatus != "applied" && bindingStatus != "already_bound") ||
		!bindingOK || !hasKeys(binding, "status", "mode", "before_sha256", "after_sha256") ||
		binding["status"] != bindingStatus || binding["mode"] != "apply" {
		return nil, fmt.Errorf("ai-panorama-terminal-binding-result-invalid")
	}
	beforeSHA256, beforeOK := exactString(binding["before_sha256"])
	afterSHA256, afterOK := exactString(binding["after_sha256"])
	if !beforeOK || !afterOK || !aiPanoramaRawSHA256Pattern.MatchString(beforeSHA256) ||
		!aiPanoramaRawSHA256Pattern.MatchString(afterSHA256) {
		return nil, fmt.Errorf("ai-panorama-terminal-binding-result-invalid")
	}
	installReceipt, installOK := result["install_receipt"].(map[string]any)
	if !installOK || validateAiPanoramaInstallReceipt(
		installReceipt, expectedPermitSHA256, expectedPublicationRecordSHA256,
		bindingStatus, beforeSHA256, afterSHA256, sealed,
	) != nil {
		return nil, fmt.Errorf("ai-panorama-terminal-install-receipt-invalid")
	}
	observation.PermitSHA256 = expectedPermitSHA256
	observation.BindingStatus = bindingStatus
	observation.BeforeSHA256 = beforeSHA256
	observation.AfterSHA256 = afterSHA256
	return observation, nil
}

func aiPanoramaInstallReceiptFields() []string {
	return []string{
		"already_installed", "applied", "authenticated_principal_verified",
		"candidate_binding_verified", "candidate_marker_sha256", "contract",
		"control_path", "controller_nonce_consumed", "controller_permit_sha256",
		"controller_permit_verified", "core_manifest_sha256",
		"install_request_contract", "listing_identity_verified",
		"materialization_lineage_verified", "materialization_receipt_sha256",
		"mode", "principal_binding_verified", "private_values_redacted",
		"property_url_sha256", "provider_key",
		"public_tour_volume_profile_verified",
		"publication_authorization_record_sha256",
		"publication_authorization_verified",
		"publication_binding_after_sha256", "publication_binding_before_sha256",
		"publication_binding_status", "publication_binding_verified",
		"release_eligible", "representation_kind", "run_binding_verified",
		"run_terminal_verified", "slug", "source_file_count",
		"source_identity_contract", "source_identity_verified",
		"source_relative_path_semantics", "source_relative_root",
		"source_total_bytes", "source_tour_sha256", "source_tree_algorithm",
		"source_tree_sha256", "status",
	}
}

func validateAiPanoramaInstallReceipt(
	receipt map[string]any,
	expectedPermitSHA256 string,
	expectedPublicationRecordSHA256 string,
	outerBindingStatus string,
	outerBeforeSHA256 string,
	outerAfterSHA256 string,
	sealed *aiPanoramaSealedArtifactObservation,
) error {
	if receipt == nil || sealed == nil ||
		!hasKeys(receipt, aiPanoramaInstallReceiptFields()...) ||
		receipt["contract"] != aiPanoramaInstallReceiptSchema ||
		receipt["mode"] != "apply" ||
		receipt["slug"] != aiPanoramaPraterSlug ||
		receipt["control_path"] != aiPanoramaPraterControlPath ||
		receipt["representation_kind"] != "ai_panorama_360" ||
		receipt["provider_key"] != "willhaben" ||
		receipt["property_url_sha256"] != aiPanoramaPropertyURLSHA256 ||
		receipt["core_manifest_sha256"] != aiPanoramaExpectedCoreDigest ||
		receipt["source_identity_contract"] != aiPanoramaSourceIdentitySchema ||
		receipt["source_tree_algorithm"] != aiPanoramaSourceTreeAlgorithm ||
		receipt["source_relative_root"] != "." ||
		receipt["source_relative_path_semantics"] != aiPanoramaSourcePathSemantics ||
		receipt["source_tree_sha256"] != aiPanoramaExpectedSourceTree ||
		receipt["source_tour_sha256"] != aiPanoramaExpectedTourDigest ||
		receipt["install_request_contract"] != aiPanoramaInstallRequestSchema ||
		receipt["materialization_receipt_sha256"] != aiPanoramaExpectedReceiptDigest ||
		receipt["candidate_marker_sha256"] != aiPanoramaExpectedMarkerDigest ||
		receipt["controller_permit_sha256"] != expectedPermitSHA256 ||
		receipt["publication_authorization_record_sha256"] != expectedPublicationRecordSHA256 ||
		receipt["publication_binding_before_sha256"] != expectedPublicationRecordSHA256 ||
		receipt["publication_binding_before_sha256"] != outerBeforeSHA256 ||
		receipt["publication_binding_after_sha256"] != outerAfterSHA256 ||
		receipt["publication_binding_status"] != outerBindingStatus {
		return fmt.Errorf("ai-panorama-install-receipt-binding-invalid")
	}
	for _, field := range []string{
		"authenticated_principal_verified", "candidate_binding_verified",
		"controller_nonce_consumed", "controller_permit_verified",
		"listing_identity_verified", "materialization_lineage_verified",
		"principal_binding_verified", "private_values_redacted",
		"public_tour_volume_profile_verified", "publication_authorization_verified",
		"publication_binding_verified", "release_eligible",
		"run_binding_verified", "run_terminal_verified", "source_identity_verified",
	} {
		if receipt[field] != true {
			return fmt.Errorf("ai-panorama-install-receipt-authority-invalid")
		}
	}
	fileCount, fileCountOK := exactInt(
		receipt["source_file_count"], int64(sealed.FileCount), int64(sealed.FileCount),
	)
	totalBytes, totalBytesOK := exactInt(
		receipt["source_total_bytes"], sealed.TotalBytes, sealed.TotalBytes,
	)
	if !fileCountOK || fileCount != int64(sealed.FileCount) ||
		!totalBytesOK || totalBytes != sealed.TotalBytes ||
		!aiPanoramaRawSHA256Pattern.MatchString(outerBeforeSHA256) ||
		!aiPanoramaRawSHA256Pattern.MatchString(outerAfterSHA256) {
		return fmt.Errorf("ai-panorama-install-receipt-source-invalid")
	}
	status, statusOK := exactString(receipt["status"])
	if !statusOK {
		return fmt.Errorf("ai-panorama-install-receipt-status-invalid")
	}
	switch status {
	case "installed":
		if receipt["applied"] != true || receipt["already_installed"] != false {
			return fmt.Errorf("ai-panorama-install-receipt-status-invalid")
		}
	case "already_installed":
		if receipt["applied"] != false || receipt["already_installed"] != true {
			return fmt.Errorf("ai-panorama-install-receipt-status-invalid")
		}
	default:
		return fmt.Errorf("ai-panorama-install-receipt-status-invalid")
	}
	switch outerBindingStatus {
	case "applied":
		if outerBeforeSHA256 == outerAfterSHA256 {
			return fmt.Errorf("ai-panorama-install-receipt-binding-status-invalid")
		}
	case "already_bound":
		if outerBeforeSHA256 != outerAfterSHA256 {
			return fmt.Errorf("ai-panorama-install-receipt-binding-status-invalid")
		}
	default:
		return fmt.Errorf("ai-panorama-install-receipt-binding-status-invalid")
	}
	return nil
}

func aiPanoramaTerminalValue(value *aiPanoramaTerminalObservation) map[string]any {
	if value == nil {
		return nil
	}
	return map[string]any{
		"status":                  value.Status,
		"request_id_sha256":       value.RequestIDSHA256,
		"permit_sha256":           value.PermitSHA256,
		"terminal_receipt_sha256": value.ReceiptSHA256,
		"binding_status":          value.BindingStatus,
		"before_sha256":           value.BeforeSHA256,
		"after_sha256":            value.AfterSHA256,
		"control_root_device":     json.Number(fmt.Sprintf("%d", value.ControlRootDevice)),
		"control_root_inode":      json.Number(fmt.Sprintf("%d", value.ControlRootInode)),
	}
}
