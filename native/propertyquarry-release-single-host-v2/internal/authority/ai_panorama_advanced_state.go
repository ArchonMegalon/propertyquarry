//go:build linux && amd64

package authority

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strconv"
	"syscall"
	"time"
)

const (
	aiPanoramaAdvancedStateSchema      = "propertyquarry.ai-panorama-install-advanced-state-profile.v1"
	aiPanoramaLedgerEntryDomain        = "propertyquarry.ai-panorama-install-ledger-entry.v2\x00"
	aiPanoramaOperationEntryDomain     = "propertyquarry.ai-panorama-install-operation-entry.v1\x00"
	aiPanoramaOperationIDDomain        = "propertyquarry.ai-panorama-install-operation.v1\x00"
	aiPanoramaTombstoneSchema          = "propertyquarry.ai-panorama-install-tombstone.v1"
	aiPanoramaMaximumLedgerBytes       = 16 * 1024 * 1024
	aiPanoramaMaximumOperationBytes    = 32 * 1024 * 1024
	aiPanoramaMaximumStateEntries      = 100_000
	aiPanoramaMaximumOperationEvidence = 256 * 1024
)

func aiPanoramaTombstonePath(kind string, valueSHA256 string) string {
	return aiPanoramaControlRoot + "/tombstones/" + kind + "-" + valueSHA256 + ".json"
}

type aiPanoramaStateLeafProfile struct {
	Path   string
	Device uint64
	Inode  uint64
	Mode   uint32
	UID    uint32
	GID    uint32
	Nlink  uint64
	Size   int64
	SHA256 string
}

type aiPanoramaAdvancedLedgerBinding struct {
	InstanceID      string
	Sequence        int64
	MatchedSequence int64
	TipSHA256       string
	EntrySHA256     string
	PermitSHA256    string
	RequestIDSHA256 string
	NonceSHA256     string
	ContextSHA256   string
}

type aiPanoramaAdvancedOperationBinding struct {
	InstanceID          string
	Sequence            int64
	TipSHA256           string
	OperationID         string
	PreparedEntrySHA256 string
	TerminalEvent       string
	TerminalEntrySHA256 string
}

type aiPanoramaAdvancedStateProfile struct {
	Root       aiPanoramaGenesisRootIdentity
	Files      []aiPanoramaStateLeafProfile
	Tombstones []aiPanoramaStateLeafProfile
	Ledger     aiPanoramaAdvancedLedgerBinding
	Operation  aiPanoramaAdvancedOperationBinding
}

type aiPanoramaAdvancedStateExpectation struct {
	PermitSHA256            string
	RequestIDSHA256         string
	NonceSHA256             string
	ContextSHA256           string
	SignedPreimageSHA256    string
	TrustAssertionSHA256    string
	KeyID                   string
	KeyEpoch                int64
	KeySHA256               string
	KeyringSHA256           string
	VolumeProfileSHA256     string
	PublicationRecordSHA256 string
	BindingStatus           string
	BindingBeforeSHA256     string
	BindingAfterSHA256      string
	PublicVolumeDevice      uint64
	PublicVolumeInode       uint64
	TerminalEvent           string
	IssuedAt                time.Time
	ExpiresAt               time.Time
	ExecutionLease          int64
}

func aiPanoramaPythonCanonicalJSON(value any) ([]byte, error) {
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	raw := output.Bytes()
	if len(raw) < 1 || raw[len(raw)-1] != '\n' {
		return nil, fmt.Errorf("ai-panorama-python-canonical-json-invalid")
	}
	result := append([]byte(nil), raw[:len(raw)-1]...)
	result = bytes.ReplaceAll(result, []byte(`\u2028`), []byte("\u2028"))
	result = bytes.ReplaceAll(result, []byte(`\u2029`), []byte("\u2029"))
	return result, nil
}

type aiPanoramaLedgerEntryBinding struct {
	Sequence        int64
	EntrySHA256     string
	PermitSHA256    string
	RequestIDSHA256 string
	NonceSHA256     string
	ContextSHA256   string
}

type aiPanoramaOperationEntryBinding struct {
	OperationID         string
	PreparedEntrySHA256 string
	TerminalEvent       string
	TerminalEntrySHA256 string
}

func aiPanoramaStateLeafProfileValue(value aiPanoramaStateLeafProfile) map[string]any {
	return map[string]any{
		"path":       value.Path,
		"device":     json.Number(strconv.FormatUint(value.Device, 10)),
		"inode":      json.Number(strconv.FormatUint(value.Inode, 10)),
		"mode":       json.Number(strconv.FormatUint(uint64(value.Mode), 10)),
		"uid":        json.Number(strconv.FormatUint(uint64(value.UID), 10)),
		"gid":        json.Number(strconv.FormatUint(uint64(value.GID), 10)),
		"nlink":      json.Number(strconv.FormatUint(value.Nlink, 10)),
		"size_bytes": json.Number(strconv.FormatInt(value.Size, 10)),
		"sha256":     value.SHA256,
	}
}

func aiPanoramaStateLeafProfilesValue(values []aiPanoramaStateLeafProfile) []any {
	result := make([]any, 0, len(values))
	for _, value := range values {
		result = append(result, aiPanoramaStateLeafProfileValue(value))
	}
	return result
}

func observeAiPanoramaGenesisCompletionFiles(
	root string,
	genesis *aiPanoramaStateGenesis,
) ([]aiPanoramaStateLeafProfile, error) {
	if genesis == nil || len(genesis.Files) != 4 ||
		validateAiPanoramaGenesisRoot(root, genesis.Root) != nil {
		return nil, fmt.Errorf("ai-panorama-state-genesis-completion-invalid")
	}
	result := make([]aiPanoramaStateLeafProfile, 0, len(genesis.Files))
	for _, expected := range genesis.Files {
		raw, profile, err := readAiPanoramaStateLeaf(
			root, genesis.Root, expected.Path, int64(len(expected.Raw)),
		)
		valid := err == nil && bytes.Equal(raw, expected.Raw) &&
			profile.SHA256 == expected.SHA256 &&
			profile.Size == int64(len(expected.Raw))
		zero(raw)
		if !valid {
			return nil, fmt.Errorf("ai-panorama-state-genesis-completion-invalid")
		}
		result = append(result, profile)
	}
	return result, nil
}

func parseAiPanoramaGenesisCompletionFiles(
	raw any,
	genesis *aiPanoramaStateGenesis,
) ([]aiPanoramaStateLeafProfile, error) {
	values, ok := raw.([]any)
	if !ok || genesis == nil || len(genesis.Files) != 4 ||
		len(values) != len(genesis.Files) {
		return nil, fmt.Errorf("ai-panorama-state-genesis-completion-invalid")
	}
	result := make([]aiPanoramaStateLeafProfile, 0, len(values))
	for index, rawValue := range values {
		expected := genesis.Files[index]
		profile, err := parseAiPanoramaStateLeafProfile(
			rawValue, expected.Path, genesis.Root,
		)
		if err != nil || profile.Size != int64(len(expected.Raw)) ||
			profile.SHA256 != expected.SHA256 {
			return nil, fmt.Errorf("ai-panorama-state-genesis-completion-invalid")
		}
		result = append(result, profile)
	}
	return result, nil
}

func validateAiPanoramaGenesisCompletionFiles(
	root string,
	genesis *aiPanoramaStateGenesis,
) error {
	if genesis == nil || len(genesis.CompletionFiles) != 4 ||
		len(genesis.Files) != 4 {
		return fmt.Errorf("ai-panorama-state-genesis-completion-invalid")
	}
	for index, expected := range genesis.CompletionFiles {
		raw, observed, err := readAiPanoramaStateLeaf(
			root, genesis.Root, expected.Path, int64(len(genesis.Files[index].Raw)),
		)
		valid := err == nil && observed == expected &&
			bytes.Equal(raw, genesis.Files[index].Raw)
		zero(raw)
		if !valid {
			return fmt.Errorf("ai-panorama-state-genesis-completion-changed")
		}
	}
	return nil
}

func (value *aiPanoramaAdvancedStateProfile) journalValue() map[string]any {
	return map[string]any{
		"schema":              aiPanoramaAdvancedStateSchema,
		"version":             json.Number("1"),
		"authority":           "propertyquarry-release-control",
		"control_root":        value.Root.Path,
		"control_root_device": json.Number(strconv.FormatUint(value.Root.Device, 10)),
		"control_root_inode":  json.Number(strconv.FormatUint(value.Root.Inode, 10)),
		"control_root_mode":   json.Number(strconv.FormatUint(uint64(value.Root.Mode), 10)),
		"control_root_uid":    json.Number(strconv.FormatUint(uint64(value.Root.UID), 10)),
		"control_root_gid":    json.Number(strconv.FormatUint(uint64(value.Root.GID), 10)),
		"files":               aiPanoramaStateLeafProfilesValue(value.Files),
		"tombstones":          aiPanoramaStateLeafProfilesValue(value.Tombstones),
		"ledger": map[string]any{
			"instance_id":          value.Ledger.InstanceID,
			"sequence":             json.Number(strconv.FormatInt(value.Ledger.Sequence, 10)),
			"matched_sequence":     json.Number(strconv.FormatInt(value.Ledger.MatchedSequence, 10)),
			"tip_sha256":           value.Ledger.TipSHA256,
			"matched_entry_sha256": value.Ledger.EntrySHA256,
			"permit_sha256":        value.Ledger.PermitSHA256,
			"request_id_sha256":    value.Ledger.RequestIDSHA256,
			"nonce_sha256":         value.Ledger.NonceSHA256,
			"context_sha256":       value.Ledger.ContextSHA256,
		},
		"operation": map[string]any{
			"instance_id":           value.Operation.InstanceID,
			"sequence":              json.Number(strconv.FormatInt(value.Operation.Sequence, 10)),
			"tip_sha256":            value.Operation.TipSHA256,
			"operation_id":          value.Operation.OperationID,
			"prepared_entry_sha256": value.Operation.PreparedEntrySHA256,
			"terminal_event":        value.Operation.TerminalEvent,
			"terminal_entry_sha256": value.Operation.TerminalEntrySHA256,
		},
	}
}

func parseAiPanoramaStateLeafProfile(
	raw any,
	expectedPath string,
	expectedRoot aiPanoramaGenesisRootIdentity,
) (aiPanoramaStateLeafProfile, error) {
	value, ok := raw.(map[string]any)
	if !ok || !hasKeys(
		value, "path", "device", "inode", "mode", "uid", "gid",
		"nlink", "size_bytes", "sha256",
	) || value["path"] != expectedPath {
		return aiPanoramaStateLeafProfile{}, fmt.Errorf("ai-panorama-state-profile-file-invalid")
	}
	device, deviceOK := exactInt(value["device"], 1, 1<<62)
	inode, inodeOK := exactInt(value["inode"], 1, 1<<62)
	mode, modeOK := exactInt(value["mode"], 0, 0o777)
	uid, uidOK := exactInt(value["uid"], 0, 1<<32-1)
	gid, gidOK := exactInt(value["gid"], 0, 1<<32-1)
	nlink, nlinkOK := exactInt(value["nlink"], 1, 1<<30)
	size, sizeOK := exactInt(value["size_bytes"], 1, aiPanoramaMaximumOperationBytes)
	sha256Value, shaOK := exactString(value["sha256"])
	if !deviceOK || uint64(device) != expectedRoot.Device || !inodeOK ||
		!modeOK || mode != 0o600 || !uidOK || uint32(uid) != expectedRoot.UID ||
		!gidOK || uint32(gid) != expectedRoot.GID || !nlinkOK || nlink != 1 ||
		!sizeOK || !shaOK || !aiPanoramaRawSHA256Pattern.MatchString(sha256Value) {
		return aiPanoramaStateLeafProfile{}, fmt.Errorf("ai-panorama-state-profile-file-invalid")
	}
	return aiPanoramaStateLeafProfile{
		Path: expectedPath, Device: uint64(device), Inode: uint64(inode),
		Mode: uint32(mode), UID: uint32(uid), GID: uint32(gid),
		Nlink: uint64(nlink), Size: size, SHA256: sha256Value,
	}, nil
}

func parseAiPanoramaAdvancedStateProfile(raw any) (*aiPanoramaAdvancedStateProfile, error) {
	value, ok := raw.(map[string]any)
	if !ok || !hasKeys(
		value, "schema", "version", "authority", "control_root",
		"control_root_device", "control_root_inode", "control_root_mode",
		"control_root_uid", "control_root_gid", "files", "tombstones",
		"ledger", "operation",
	) || value["schema"] != aiPanoramaAdvancedStateSchema ||
		value["version"] != json.Number("1") ||
		value["authority"] != "propertyquarry-release-control" ||
		value["control_root"] != aiPanoramaControlRoot {
		return nil, fmt.Errorf("ai-panorama-state-profile-invalid")
	}
	device, deviceOK := exactInt(value["control_root_device"], 1, 1<<62)
	inode, inodeOK := exactInt(value["control_root_inode"], 1, 1<<62)
	mode, modeOK := exactInt(value["control_root_mode"], 0, 0o777)
	uid, uidOK := exactInt(value["control_root_uid"], 0, 1<<32-1)
	gid, gidOK := exactInt(value["control_root_gid"], 0, 1<<32-1)
	if !deviceOK || !inodeOK || !modeOK || mode != 0o700 || !uidOK || !gidOK {
		return nil, fmt.Errorf("ai-panorama-state-profile-invalid")
	}
	profile := &aiPanoramaAdvancedStateProfile{Root: aiPanoramaGenesisRootIdentity{
		Path: aiPanoramaControlRoot, Device: uint64(device), Inode: uint64(inode),
		Mode: os.FileMode(mode), UID: uint32(uid), GID: uint32(gid),
	}}
	fileValues, ok := value["files"].([]any)
	expectedPaths := []string{
		aiPanoramaLedgerPath, aiPanoramaLedgerLockPath,
		aiPanoramaOperationPath, aiPanoramaOperationLockPath,
	}
	if !ok || len(fileValues) != len(expectedPaths) {
		return nil, fmt.Errorf("ai-panorama-state-profile-invalid")
	}
	for index, fileValue := range fileValues {
		file, err := parseAiPanoramaStateLeafProfile(
			fileValue, expectedPaths[index], profile.Root,
		)
		if err != nil {
			return nil, err
		}
		profile.Files = append(profile.Files, file)
	}
	ledger, ledgerOK := value["ledger"].(map[string]any)
	if !ledgerOK || !hasKeys(
		ledger, "instance_id", "sequence", "matched_sequence", "tip_sha256", "matched_entry_sha256",
		"permit_sha256", "request_id_sha256", "nonce_sha256", "context_sha256",
	) {
		return nil, fmt.Errorf("ai-panorama-state-profile-ledger-invalid")
	}
	ledgerInstance, instanceOK := exactString(ledger["instance_id"])
	ledgerSequence, sequenceOK := exactInt(ledger["sequence"], 1, aiPanoramaMaximumStateEntries)
	matchedSequence, matchedSequenceOK := exactInt(
		ledger["matched_sequence"], 1, ledgerSequence,
	)
	ledgerTip, tipOK := exactString(ledger["tip_sha256"])
	ledgerEntry, entryOK := exactString(ledger["matched_entry_sha256"])
	permitSHA256, permitOK := exactString(ledger["permit_sha256"])
	requestSHA256, requestOK := exactString(ledger["request_id_sha256"])
	nonceSHA256, nonceOK := exactString(ledger["nonce_sha256"])
	contextSHA256, contextOK := exactString(ledger["context_sha256"])
	if !instanceOK || !aiPanoramaNoncePattern.MatchString(ledgerInstance) ||
		!sequenceOK || !matchedSequenceOK || !tipOK || !entryOK || !permitOK || !requestOK ||
		!nonceOK || !contextOK {
		return nil, fmt.Errorf("ai-panorama-state-profile-ledger-invalid")
	}
	for _, digestValue := range []string{
		ledgerTip, ledgerEntry, permitSHA256, requestSHA256, nonceSHA256, contextSHA256,
	} {
		if !aiPanoramaRawSHA256Pattern.MatchString(digestValue) {
			return nil, fmt.Errorf("ai-panorama-state-profile-ledger-invalid")
		}
	}
	profile.Ledger = aiPanoramaAdvancedLedgerBinding{
		InstanceID: ledgerInstance, Sequence: ledgerSequence,
		MatchedSequence: matchedSequence, TipSHA256: ledgerTip,
		EntrySHA256: ledgerEntry, PermitSHA256: permitSHA256,
		RequestIDSHA256: requestSHA256, NonceSHA256: nonceSHA256,
		ContextSHA256: contextSHA256,
	}
	tombstoneValues, tombstonesOK := value["tombstones"].([]any)
	tombstonePaths := []string{
		aiPanoramaTombstonePath("request", requestSHA256),
		aiPanoramaTombstonePath("nonce", nonceSHA256),
		aiPanoramaTombstonePath("permit", permitSHA256),
	}
	if !tombstonesOK || len(tombstoneValues) != len(tombstonePaths) {
		return nil, fmt.Errorf("ai-panorama-state-profile-tombstones-invalid")
	}
	for index, tombstoneValue := range tombstoneValues {
		tombstone, err := parseAiPanoramaStateLeafProfile(
			tombstoneValue, tombstonePaths[index], profile.Root,
		)
		if err != nil || tombstone.Size > 16*1024 {
			return nil, fmt.Errorf("ai-panorama-state-profile-tombstones-invalid")
		}
		profile.Tombstones = append(profile.Tombstones, tombstone)
	}
	operation, operationOK := value["operation"].(map[string]any)
	if !operationOK || !hasKeys(
		operation, "instance_id", "sequence", "tip_sha256", "operation_id",
		"prepared_entry_sha256", "terminal_event", "terminal_entry_sha256",
	) {
		return nil, fmt.Errorf("ai-panorama-state-profile-operation-invalid")
	}
	operationInstance, operationInstanceOK := exactString(operation["instance_id"])
	operationSequence, operationSequenceOK := exactInt(
		operation["sequence"], 2, aiPanoramaMaximumStateEntries,
	)
	operationTip, operationTipOK := exactString(operation["tip_sha256"])
	operationID, operationIDOK := exactString(operation["operation_id"])
	preparedSHA256, preparedOK := exactString(operation["prepared_entry_sha256"])
	terminalEvent, terminalEventOK := exactString(operation["terminal_event"])
	terminalSHA256, terminalOK := exactString(operation["terminal_entry_sha256"])
	if !operationInstanceOK || !aiPanoramaNoncePattern.MatchString(operationInstance) ||
		!operationSequenceOK || !operationTipOK || !operationIDOK ||
		!preparedOK || !terminalEventOK ||
		(terminalEvent != "committed" && terminalEvent != "failed-clean" &&
			terminalEvent != "rolled-back") || !terminalOK {
		return nil, fmt.Errorf("ai-panorama-state-profile-operation-invalid")
	}
	for _, digestValue := range []string{
		operationTip, operationID, preparedSHA256, terminalSHA256,
	} {
		if !aiPanoramaRawSHA256Pattern.MatchString(digestValue) {
			return nil, fmt.Errorf("ai-panorama-state-profile-operation-invalid")
		}
	}
	profile.Operation = aiPanoramaAdvancedOperationBinding{
		InstanceID: operationInstance, Sequence: operationSequence,
		TipSHA256: operationTip, OperationID: operationID,
		PreparedEntrySHA256: preparedSHA256, TerminalEvent: terminalEvent,
		TerminalEntrySHA256: terminalSHA256,
	}
	return profile, nil
}

func readAiPanoramaStateLeaf(
	root string,
	expectedRoot aiPanoramaGenesisRootIdentity,
	path string,
	maximum int64,
) ([]byte, aiPanoramaStateLeafProfile, error) {
	if maximum < 1 {
		return nil, aiPanoramaStateLeafProfile{}, fmt.Errorf("ai-panorama-state-leaf-input-invalid")
	}
	target := rooted(root, path)
	file, err := os.OpenFile(
		target, os.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0,
	)
	if err != nil {
		return nil, aiPanoramaStateLeafProfile{}, fmt.Errorf("ai-panorama-state-leaf-unavailable")
	}
	defer file.Close()
	before, err := file.Stat()
	metadata, ok := infoSys(before)
	if err != nil || !ok || !before.Mode().IsRegular() ||
		before.Mode().Perm() != 0o600 ||
		uint64(metadata.Dev) != expectedRoot.Device ||
		metadata.Uid != expectedRoot.UID || metadata.Gid != expectedRoot.GID ||
		metadata.Nlink != 1 || before.Size() < 1 || before.Size() > maximum {
		return nil, aiPanoramaStateLeafProfile{}, fmt.Errorf("ai-panorama-state-leaf-metadata-invalid")
	}
	raw := make([]byte, before.Size())
	if _, err := io.ReadFull(file, raw); err != nil {
		zero(raw)
		return nil, aiPanoramaStateLeafProfile{}, fmt.Errorf("ai-panorama-state-leaf-read-failed")
	}
	extra := []byte{0}
	count, readErr := file.Read(extra)
	zero(extra)
	after, statErr := file.Stat()
	pathAfter, pathErr := os.Lstat(target)
	if count != 0 || (readErr != nil && readErr != io.EOF) ||
		statErr != nil || pathErr != nil ||
		!os.SameFile(before, after) || !os.SameFile(before, pathAfter) {
		zero(raw)
		return nil, aiPanoramaStateLeafProfile{}, fmt.Errorf("ai-panorama-state-leaf-changed")
	}
	return raw, aiPanoramaStateLeafProfile{
		Path: path, Device: uint64(metadata.Dev), Inode: metadata.Ino,
		Mode: uint32(before.Mode().Perm()), UID: metadata.Uid, GID: metadata.Gid,
		Nlink: metadata.Nlink, Size: before.Size(), SHA256: aiPanoramaRawSHA256(raw),
	}, nil
}

func readAiPanoramaCanonicalStateObject(
	root string,
	expectedRoot aiPanoramaGenesisRootIdentity,
	path string,
	maximum int64,
) (map[string]any, []byte, aiPanoramaStateLeafProfile, error) {
	raw, profile, err := readAiPanoramaStateLeaf(root, expectedRoot, path, maximum)
	if err != nil {
		return nil, nil, aiPanoramaStateLeafProfile{}, err
	}
	if len(raw) < 3 || raw[len(raw)-1] != '\n' || raw[len(raw)-2] == '\n' ||
		bytes.IndexByte(raw[:len(raw)-1], '\n') >= 0 {
		zero(raw)
		return nil, nil, aiPanoramaStateLeafProfile{}, fmt.Errorf("ai-panorama-state-json-framing-invalid")
	}
	value, err := strictJSON(raw[:len(raw)-1], int(maximum))
	if err != nil {
		zero(raw)
		return nil, nil, aiPanoramaStateLeafProfile{}, fmt.Errorf("ai-panorama-state-json-invalid")
	}
	canonical, err := aiPanoramaPythonCanonicalJSON(value)
	canonical = append(canonical, '\n')
	valid := err == nil && bytes.Equal(canonical, raw)
	zero(canonical)
	if !valid {
		zero(raw)
		return nil, nil, aiPanoramaStateLeafProfile{}, fmt.Errorf("ai-panorama-state-json-noncanonical")
	}
	return value, raw, profile, nil
}

func aiPanoramaDomainDigest(domain string, value map[string]any) (string, error) {
	raw, err := aiPanoramaPythonCanonicalJSON(value)
	if err != nil {
		return "", fmt.Errorf("ai-panorama-state-entry-canonicalization-failed")
	}
	preimage := append([]byte(domain), raw...)
	zero(raw)
	result := aiPanoramaRawSHA256(preimage)
	zero(preimage)
	return result, nil
}

func aiPanoramaExactRawDigest(value any) (string, bool) {
	text, ok := exactString(value)
	return text, ok && aiPanoramaRawSHA256Pattern.MatchString(text)
}

func validateAiPanoramaConsumptionTombstones(
	root string,
	expectedRoot aiPanoramaGenesisRootIdentity,
	ledgerInstanceID string,
	entry map[string]any,
	expected *aiPanoramaAdvancedStateExpectation,
) ([]aiPanoramaStateLeafProfile, error) {
	tombstoneRoot := rooted(root, aiPanoramaControlRoot+"/tombstones")
	info, err := os.Lstat(tombstoneRoot)
	metadata, metadataOK := infoSys(info)
	if err != nil || !metadataOK || !info.IsDir() ||
		info.Mode().Perm() != 0o700 || info.Mode()&os.ModeSymlink != 0 ||
		uint64(metadata.Dev) != expectedRoot.Device ||
		metadata.Uid != expectedRoot.UID || metadata.Gid != expectedRoot.GID ||
		metadata.Nlink < 2 {
		return nil, fmt.Errorf("ai-panorama-tombstone-root-invalid")
	}
	entrySequence, sequenceOK := exactInt(
		entry["sequence"], 1, aiPanoramaMaximumStateEntries,
	)
	if !sequenceOK {
		return nil, fmt.Errorf("ai-panorama-tombstone-binding-invalid")
	}
	type tombstoneBinding struct {
		kind        string
		valueSHA256 string
		digestKey   string
	}
	bindings := []tombstoneBinding{
		{"request", expected.RequestIDSHA256, "request_tombstone_sha256"},
		{"nonce", expected.NonceSHA256, "nonce_tombstone_sha256"},
		{"permit", expected.PermitSHA256, "permit_tombstone_sha256"},
	}
	result := make([]aiPanoramaStateLeafProfile, 0, len(bindings))
	for _, binding := range bindings {
		expectedDigest, digestOK := aiPanoramaExactRawDigest(entry[binding.digestKey])
		if !digestOK {
			return nil, fmt.Errorf("ai-panorama-tombstone-binding-invalid")
		}
		path := aiPanoramaTombstonePath(binding.kind, binding.valueSHA256)
		value, raw, profile, err := readAiPanoramaCanonicalStateObject(
			root, expectedRoot, path, 16*1024,
		)
		if err != nil {
			return nil, err
		}
		valid := profile.SHA256 == expectedDigest && hasKeys(
			value, "schema", "version", "authority", "status", "kind",
			"value_sha256", "permit_sha256", "request_id_sha256",
			"nonce_sha256", "context_sha256", "signed_preimage_sha256",
			"trust_assertion_sha256", "key_id", "key_epoch", "key_sha256",
			"keyring_sha256", "key_usage", "ledger_instance_id",
			"ledger_sequence", "consumed_at", "execution_lease_seconds",
			"execution_lease_expires_at",
		) && value["schema"] == aiPanoramaTombstoneSchema &&
			value["version"] == json.Number("1") &&
			value["authority"] == "propertyquarry-release-control" &&
			value["status"] == "consumed" && value["kind"] == binding.kind &&
			value["value_sha256"] == binding.valueSHA256 &&
			value["permit_sha256"] == expected.PermitSHA256 &&
			value["request_id_sha256"] == expected.RequestIDSHA256 &&
			value["nonce_sha256"] == expected.NonceSHA256 &&
			value["context_sha256"] == expected.ContextSHA256 &&
			value["signed_preimage_sha256"] == expected.SignedPreimageSHA256 &&
			value["trust_assertion_sha256"] == expected.TrustAssertionSHA256 &&
			value["key_id"] == expected.KeyID &&
			value["key_epoch"] == json.Number(strconv.FormatInt(expected.KeyEpoch, 10)) &&
			value["key_sha256"] == expected.KeySHA256 &&
			value["keyring_sha256"] == expected.KeyringSHA256 &&
			value["key_usage"] == aiPanoramaPermitKeyUsage &&
			value["ledger_instance_id"] == ledgerInstanceID &&
			value["ledger_sequence"] == json.Number(strconv.FormatInt(entrySequence, 10)) &&
			value["consumed_at"] == entry["consumed_at"] &&
			value["execution_lease_seconds"] == entry["execution_lease_seconds"] &&
			value["execution_lease_expires_at"] == entry["execution_lease_expires_at"]
		zero(raw)
		if !valid {
			return nil, fmt.Errorf("ai-panorama-tombstone-binding-invalid")
		}
		result = append(result, profile)
	}
	return result, nil
}

func validateAiPanoramaLedger(
	root string,
	expectedRoot aiPanoramaGenesisRootIdentity,
	value map[string]any,
	expectedInstance string,
	expected *aiPanoramaAdvancedStateExpectation,
) (aiPanoramaAdvancedLedgerBinding, []aiPanoramaStateLeafProfile, error) {
	if expected == nil || !hasKeys(
		value, "schema", "authority", "instance_id", "sequence", "tip_sha256", "entries",
	) || value["schema"] != aiPanoramaLedgerSchema ||
		value["authority"] != "propertyquarry-release-control" ||
		value["instance_id"] != expectedInstance {
		return aiPanoramaAdvancedLedgerBinding{}, nil, fmt.Errorf("ai-panorama-ledger-invalid")
	}
	sequence, sequenceOK := exactInt(value["sequence"], 1, aiPanoramaMaximumStateEntries)
	entries, entriesOK := value["entries"].([]any)
	tip, tipOK := aiPanoramaExactRawDigest(value["tip_sha256"])
	if !sequenceOK || !entriesOK || int64(len(entries)) != sequence || !tipOK {
		return aiPanoramaAdvancedLedgerBinding{}, nil, fmt.Errorf("ai-panorama-ledger-invalid")
	}
	previous := string(bytes.Repeat([]byte{'0'}, 64))
	requests := make(map[string]bool, len(entries))
	nonces := make(map[string]bool, len(entries))
	permits := make(map[string]bool, len(entries))
	var matched *aiPanoramaLedgerEntryBinding
	var matchedValue map[string]any
	for index, rawEntry := range entries {
		entry, ok := rawEntry.(map[string]any)
		if !ok || !hasKeys(
			entry, "sequence", "permit_sha256", "request_id_sha256",
			"nonce_sha256", "context_sha256", "signed_preimage_sha256",
			"trust_assertion_sha256", "key_id", "key_epoch", "key_sha256",
			"keyring_sha256", "key_usage", "consumed_at",
			"execution_lease_seconds", "execution_lease_expires_at",
			"request_tombstone_sha256", "nonce_tombstone_sha256",
			"permit_tombstone_sha256", "previous_entry_sha256", "entry_sha256",
		) {
			return aiPanoramaAdvancedLedgerBinding{}, nil, fmt.Errorf("ai-panorama-ledger-invalid")
		}
		entrySequence, entrySequenceOK := exactInt(
			entry["sequence"], int64(index+1), int64(index+1),
		)
		keyEpoch, keyEpochOK := exactInt(entry["key_epoch"], 1, 1<<62)
		leaseSeconds, leaseOK := exactInt(
			entry["execution_lease_seconds"], 1, 900,
		)
		if !entrySequenceOK || entrySequence != int64(index+1) ||
			!keyEpochOK || !leaseOK ||
			entry["key_usage"] != aiPanoramaPermitKeyUsage ||
			entry["previous_entry_sha256"] != previous {
			return aiPanoramaAdvancedLedgerBinding{}, nil, fmt.Errorf("ai-panorama-ledger-invalid")
		}
		digestKeys := []string{
			"permit_sha256", "request_id_sha256", "nonce_sha256",
			"context_sha256", "signed_preimage_sha256",
			"trust_assertion_sha256", "key_sha256", "keyring_sha256",
			"request_tombstone_sha256", "nonce_tombstone_sha256",
			"permit_tombstone_sha256", "previous_entry_sha256", "entry_sha256",
		}
		for _, key := range digestKeys {
			if _, ok := aiPanoramaExactRawDigest(entry[key]); !ok {
				return aiPanoramaAdvancedLedgerBinding{}, nil, fmt.Errorf("ai-panorama-ledger-invalid")
			}
		}
		keyID, keyIDOK := exactString(entry["key_id"])
		consumedAt, consumedOK := parseAiPanoramaTimestamp(entry["consumed_at"])
		expiresAt, expiresOK := parseAiPanoramaTimestamp(entry["execution_lease_expires_at"])
		if !keyIDOK || !aiPanoramaSafeIDPattern.MatchString(keyID) ||
			!consumedOK || !expiresOK || !expiresAt.After(consumedAt) ||
			expiresAt.Sub(consumedAt) != time.Duration(leaseSeconds)*time.Second {
			return aiPanoramaAdvancedLedgerBinding{}, nil, fmt.Errorf("ai-panorama-ledger-invalid")
		}
		unsigned := cloneFields(entry)
		claimed, _ := exactString(unsigned["entry_sha256"])
		delete(unsigned, "entry_sha256")
		calculated, err := aiPanoramaDomainDigest(aiPanoramaLedgerEntryDomain, unsigned)
		if err != nil || claimed != calculated {
			return aiPanoramaAdvancedLedgerBinding{}, nil, fmt.Errorf("ai-panorama-ledger-invalid")
		}
		permitSHA256, _ := exactString(entry["permit_sha256"])
		requestSHA256, _ := exactString(entry["request_id_sha256"])
		nonceSHA256, _ := exactString(entry["nonce_sha256"])
		contextSHA256, _ := exactString(entry["context_sha256"])
		if requests[requestSHA256] || nonces[nonceSHA256] || permits[permitSHA256] {
			return aiPanoramaAdvancedLedgerBinding{}, nil, fmt.Errorf("ai-panorama-ledger-invalid")
		}
		requests[requestSHA256] = true
		nonces[nonceSHA256] = true
		permits[permitSHA256] = true
		if permitSHA256 == expected.PermitSHA256 &&
			requestSHA256 == expected.RequestIDSHA256 {
			if matched != nil ||
				nonceSHA256 != expected.NonceSHA256 ||
				contextSHA256 != expected.ContextSHA256 ||
				entry["signed_preimage_sha256"] != expected.SignedPreimageSHA256 ||
				entry["trust_assertion_sha256"] != expected.TrustAssertionSHA256 ||
				entry["key_id"] != expected.KeyID || keyEpoch != expected.KeyEpoch ||
				entry["key_sha256"] != expected.KeySHA256 ||
				entry["keyring_sha256"] != expected.KeyringSHA256 ||
				leaseSeconds != expected.ExecutionLease ||
				consumedAt.Before(expected.IssuedAt) ||
				!consumedAt.Before(expected.ExpiresAt) {
				return aiPanoramaAdvancedLedgerBinding{}, nil, fmt.Errorf("ai-panorama-ledger-install-binding-invalid")
			}
			matched = &aiPanoramaLedgerEntryBinding{
				Sequence: entrySequence, EntrySHA256: claimed,
				PermitSHA256: permitSHA256, RequestIDSHA256: requestSHA256,
				NonceSHA256: nonceSHA256, ContextSHA256: contextSHA256,
			}
			matchedValue = cloneFields(entry)
		}
		previous = claimed
	}
	if tip != previous || matched == nil {
		return aiPanoramaAdvancedLedgerBinding{}, nil, fmt.Errorf("ai-panorama-ledger-install-binding-invalid")
	}
	tombstones, err := validateAiPanoramaConsumptionTombstones(
		root, expectedRoot, expectedInstance, matchedValue, expected,
	)
	if err != nil {
		return aiPanoramaAdvancedLedgerBinding{}, nil, err
	}
	return aiPanoramaAdvancedLedgerBinding{
		InstanceID: expectedInstance, Sequence: sequence,
		MatchedSequence: matched.Sequence, TipSHA256: tip,
		EntrySHA256: matched.EntrySHA256, PermitSHA256: matched.PermitSHA256,
		RequestIDSHA256: matched.RequestIDSHA256, NonceSHA256: matched.NonceSHA256,
		ContextSHA256: matched.ContextSHA256,
	}, tombstones, nil
}

func aiPanoramaOperationID(
	permitSHA256 string,
	requestSHA256 string,
	nonceSHA256 string,
	contextSHA256 string,
) (string, error) {
	preimage := []byte(aiPanoramaOperationIDDomain)
	for _, value := range []string{
		permitSHA256, requestSHA256, nonceSHA256, contextSHA256,
	} {
		decoded, err := hex.DecodeString(value)
		if err != nil || len(decoded) != 32 {
			zero(preimage)
			zero(decoded)
			return "", fmt.Errorf("ai-panorama-operation-id-input-invalid")
		}
		preimage = append(preimage, decoded...)
		zero(decoded)
	}
	result := aiPanoramaRawSHA256(preimage)
	zero(preimage)
	return result, nil
}

func validateAiPanoramaOperationEvidenceBase(
	evidence map[string]any,
	phase string,
	expected *aiPanoramaAdvancedStateExpectation,
	extraKeys ...string,
) error {
	keys := []string{
		"contract", "phase", "slug", "listing_url_sha256",
		"source_tree_sha256", "tour_sha256", "core_manifest_sha256",
		"materialization_receipt_sha256", "candidate_marker_sha256",
		"publication_record_sha256", "volume_profile_sha256",
		"public_tour_volume_name", "public_tour_mount_target",
		"target_manifest", "private_values_redacted",
	}
	keys = append(keys, extraKeys...)
	if !hasKeys(evidence, keys...) ||
		evidence["contract"] != "propertyquarry.prater_ai_panorama_governed_release.v1" ||
		evidence["phase"] != phase || evidence["slug"] != aiPanoramaPraterSlug ||
		evidence["listing_url_sha256"] != aiPanoramaPropertyURLSHA256 ||
		evidence["source_tree_sha256"] != aiPanoramaExpectedSourceTree ||
		evidence["tour_sha256"] != aiPanoramaExpectedTourDigest ||
		evidence["core_manifest_sha256"] != aiPanoramaExpectedCoreDigest ||
		evidence["materialization_receipt_sha256"] != aiPanoramaExpectedReceiptDigest ||
		evidence["candidate_marker_sha256"] != aiPanoramaExpectedMarkerDigest ||
		evidence["publication_record_sha256"] != expected.PublicationRecordSHA256 ||
		evidence["volume_profile_sha256"] != expected.VolumeProfileSHA256 ||
		evidence["public_tour_volume_name"] != aiPanoramaPublicVolumeName ||
		evidence["public_tour_mount_target"] != aiPanoramaPublicMountTarget ||
		evidence["private_values_redacted"] != true {
		return fmt.Errorf("ai-panorama-operation-evidence-invalid")
	}
	return nil
}

func validateAiPanoramaPreparedEvidence(
	evidence map[string]any,
	expected *aiPanoramaAdvancedStateExpectation,
	ledger aiPanoramaAdvancedLedgerBinding,
) error {
	if validateAiPanoramaOperationEvidenceBase(
		evidence, "prepared", expected, "admission_recovery_binding",
		"publication_binding_preparation",
	) != nil {
		return fmt.Errorf("ai-panorama-operation-prepared-evidence-invalid")
	}
	recovery, recoveryOK := evidence["admission_recovery_binding"].(map[string]any)
	preparation, preparationOK :=
		evidence["publication_binding_preparation"].(map[string]any)
	target, targetOK := evidence["target_manifest"].(map[string]any)
	if !recoveryOK || !hasKeys(
		recovery, "ledger_instance_id", "ledger_sequence", "ledger_entry_sha256",
	) || recovery["ledger_instance_id"] != ledger.InstanceID ||
		recovery["ledger_entry_sha256"] != ledger.EntrySHA256 ||
		!targetOK || !hasKeys(
		target, "state", "target_relpath", "public_root_device",
		"public_root_inode", "reserved_entry_count", "reserved_entries_sha256",
	) || target["state"] != "absent" || target["target_relpath"] != aiPanoramaPraterSlug ||
		target["reserved_entry_count"] != json.Number("0") ||
		!preparationOK || !hasKeys(
		preparation, "status",
		"publication_binding_expected_before_sha256",
		"publication_binding_expected_after_sha256",
		"publication_binding_bound_at", "database_mutation_performed",
		"private_values_redacted",
	) || preparation["database_mutation_performed"] != false ||
		preparation["private_values_redacted"] != true {
		return fmt.Errorf("ai-panorama-operation-prepared-evidence-invalid")
	}
	ledgerSequence, ok := exactInt(
		recovery["ledger_sequence"], ledger.MatchedSequence, ledger.MatchedSequence,
	)
	rootDevice, deviceOK := exactInt(
		target["public_root_device"], int64(expected.PublicVolumeDevice), int64(expected.PublicVolumeDevice),
	)
	rootInode, inodeOK := exactInt(
		target["public_root_inode"], int64(expected.PublicVolumeInode), int64(expected.PublicVolumeInode),
	)
	reservedSHA256, digestOK := aiPanoramaExactRawDigest(target["reserved_entries_sha256"])
	preparationStatus, preparationStatusOK := exactString(preparation["status"])
	preparationBefore, preparationBeforeOK := aiPanoramaExactRawDigest(
		preparation["publication_binding_expected_before_sha256"],
	)
	preparationAfter, preparationAfterOK := aiPanoramaExactRawDigest(
		preparation["publication_binding_expected_after_sha256"],
	)
	boundAt, boundAtOK := parseAiPanoramaTimestamp(
		preparation["publication_binding_bound_at"],
	)
	expectedPreparationStatus := "change-required"
	if preparationBefore == preparationAfter {
		expectedPreparationStatus = "already-bound"
	}
	terminalStatusMatches := expected.BindingStatus == "" ||
		(expected.BindingStatus == "applied" &&
			preparationStatus == "change-required") ||
		(expected.BindingStatus == "already_bound" &&
			preparationStatus == "already-bound")
	terminalBeforeMatches := expected.BindingBeforeSHA256 == "" ||
		preparationBefore == expected.BindingBeforeSHA256
	terminalAfterMatches := expected.BindingAfterSHA256 == "" ||
		preparationAfter == expected.BindingAfterSHA256
	if !ok || ledgerSequence != ledger.MatchedSequence || !deviceOK || !inodeOK ||
		uint64(rootDevice) != expected.PublicVolumeDevice ||
		uint64(rootInode) != expected.PublicVolumeInode || !digestOK ||
		reservedSHA256 == "" || !preparationStatusOK ||
		preparationStatus != expectedPreparationStatus ||
		!preparationBeforeOK || !preparationAfterOK ||
		preparationBefore != expected.PublicationRecordSHA256 ||
		!terminalStatusMatches || !terminalBeforeMatches ||
		!terminalAfterMatches || !boundAtOK ||
		boundAt.Before(expected.IssuedAt) ||
		!boundAt.Before(expected.ExpiresAt) {
		return fmt.Errorf("ai-panorama-operation-prepared-evidence-invalid")
	}
	return nil
}

func validateAiPanoramaCommittedEvidence(
	evidence map[string]any,
	expected *aiPanoramaAdvancedStateExpectation,
) error {
	if validateAiPanoramaOperationEvidenceBase(
		evidence, "committed", expected, "install",
	) != nil {
		return fmt.Errorf("ai-panorama-operation-committed-evidence-invalid")
	}
	target, targetOK := evidence["target_manifest"].(map[string]any)
	install, installOK := evidence["install"].(map[string]any)
	if !targetOK || !hasKeys(
		target, "state", "target_relpath", "public_root_device",
		"public_root_inode", "target_device", "target_inode", "tree_sha256",
		"tour_private_sha256", "file_count", "directory_count", "total_bytes",
		"reserved_entry_count", "reserved_entries_sha256",
	) || target["state"] != "present" ||
		target["target_relpath"] != aiPanoramaPraterSlug ||
		!installOK || !hasKeys(
		install, "status", "already_installed", "source_tree_sha256",
		"source_tour_sha256", "publication_binding_status",
		"publication_binding_before_sha256", "publication_binding_after_sha256",
	) || install["source_tree_sha256"] != aiPanoramaExpectedSourceTree ||
		install["source_tour_sha256"] != aiPanoramaExpectedTourDigest ||
		install["publication_binding_status"] != expected.BindingStatus ||
		install["publication_binding_before_sha256"] != expected.BindingBeforeSHA256 ||
		install["publication_binding_after_sha256"] != expected.BindingAfterSHA256 {
		return fmt.Errorf("ai-panorama-operation-committed-evidence-invalid")
	}
	status, statusOK := exactString(install["status"])
	if !statusOK ||
		(status == "installed" && install["already_installed"] != false) ||
		(status == "already_installed" && install["already_installed"] != true) ||
		(status != "installed" && status != "already_installed") {
		return fmt.Errorf("ai-panorama-operation-committed-evidence-invalid")
	}
	rootDevice, deviceOK := exactInt(
		target["public_root_device"], int64(expected.PublicVolumeDevice), int64(expected.PublicVolumeDevice),
	)
	rootInode, inodeOK := exactInt(
		target["public_root_inode"], int64(expected.PublicVolumeInode), int64(expected.PublicVolumeInode),
	)
	targetDevice, targetDeviceOK := exactInt(target["target_device"], 1, 1<<62)
	targetInode, targetInodeOK := exactInt(target["target_inode"], 1, 1<<62)
	fileCount, fileCountOK := exactInt(target["file_count"], 1, aiPanoramaMaximumFiles)
	directoryCount, directoryCountOK := exactInt(
		target["directory_count"], 1, aiPanoramaMaximumDirectories,
	)
	totalBytes, totalBytesOK := exactInt(target["total_bytes"], 1, aiPanoramaMaximumTreeBytes)
	reservedCount, reservedCountOK := exactInt(target["reserved_entry_count"], 0, 0)
	if !deviceOK || !inodeOK || !targetDeviceOK || !targetInodeOK ||
		!fileCountOK || !directoryCountOK || !totalBytesOK || !reservedCountOK ||
		uint64(rootDevice) != expected.PublicVolumeDevice ||
		uint64(rootInode) != expected.PublicVolumeInode ||
		uint64(targetDevice) != expected.PublicVolumeDevice ||
		targetInode < 1 || fileCount < 1 || directoryCount < 1 ||
		totalBytes < 1 || reservedCount != 0 {
		return fmt.Errorf("ai-panorama-operation-committed-evidence-invalid")
	}
	for _, key := range []string{
		"tree_sha256", "tour_private_sha256", "reserved_entries_sha256",
	} {
		if _, ok := aiPanoramaExactRawDigest(target[key]); !ok {
			return fmt.Errorf("ai-panorama-operation-committed-evidence-invalid")
		}
	}
	return nil
}

func validateAiPanoramaFailedEvidence(
	evidence map[string]any,
	expected *aiPanoramaAdvancedStateExpectation,
	event string,
) error {
	if (event != "failed-clean" && event != "rolled-back") ||
		validateAiPanoramaOperationEvidenceBase(
			evidence, event, expected, "error_code", "publication_outcome",
		) != nil {
		return fmt.Errorf("ai-panorama-operation-failure-evidence-invalid")
	}
	errorCode, errorOK := exactString(evidence["error_code"])
	outcome, outcomeOK := exactString(evidence["publication_outcome"])
	target, targetOK := evidence["target_manifest"].(map[string]any)
	if !errorOK || !idPattern.MatchString(errorCode) || !outcomeOK ||
		(outcome != "uncommitted" && outcome != "unknown") ||
		(event == "rolled-back" && outcome != "uncommitted") ||
		!targetOK || !hasKeys(
		target, "state", "target_relpath", "public_root_device",
		"public_root_inode", "reserved_entry_count", "reserved_entries_sha256",
	) || target["state"] != "absent" ||
		target["target_relpath"] != aiPanoramaPraterSlug {
		return fmt.Errorf("ai-panorama-operation-failure-evidence-invalid")
	}
	rootDevice, deviceOK := exactInt(
		target["public_root_device"],
		int64(expected.PublicVolumeDevice), int64(expected.PublicVolumeDevice),
	)
	rootInode, inodeOK := exactInt(
		target["public_root_inode"],
		int64(expected.PublicVolumeInode), int64(expected.PublicVolumeInode),
	)
	reservedCount, reservedOK := exactInt(target["reserved_entry_count"], 0, 0)
	if !deviceOK || !inodeOK ||
		uint64(rootDevice) != expected.PublicVolumeDevice ||
		uint64(rootInode) != expected.PublicVolumeInode ||
		!reservedOK || reservedCount != 0 {
		return fmt.Errorf("ai-panorama-operation-failure-evidence-invalid")
	}
	if _, ok := aiPanoramaExactRawDigest(target["reserved_entries_sha256"]); !ok {
		return fmt.Errorf("ai-panorama-operation-failure-evidence-invalid")
	}
	return nil
}

func validateAiPanoramaOperationJournal(
	value map[string]any,
	expectedInstance string,
	expected *aiPanoramaAdvancedStateExpectation,
	ledger aiPanoramaAdvancedLedgerBinding,
) (aiPanoramaAdvancedOperationBinding, error) {
	if expected == nil || !hasKeys(
		value, "schema", "authority", "instance_id", "sequence", "tip_sha256", "entries",
	) || value["schema"] != aiPanoramaOperationSchema ||
		value["authority"] != "propertyquarry-release-control" ||
		value["instance_id"] != expectedInstance {
		return aiPanoramaAdvancedOperationBinding{}, fmt.Errorf("ai-panorama-operation-journal-invalid")
	}
	sequence, sequenceOK := exactInt(value["sequence"], 2, aiPanoramaMaximumStateEntries)
	entries, entriesOK := value["entries"].([]any)
	tip, tipOK := aiPanoramaExactRawDigest(value["tip_sha256"])
	if !sequenceOK || !entriesOK || int64(len(entries)) != sequence || !tipOK {
		return aiPanoramaAdvancedOperationBinding{}, fmt.Errorf("ai-panorama-operation-journal-invalid")
	}
	type operationState struct {
		event          string
		bindings       [4]string
		preparedSHA256 string
	}
	states := make(map[string]operationState)
	previous := string(bytes.Repeat([]byte{'0'}, 64))
	expectedOperationID, err := aiPanoramaOperationID(
		expected.PermitSHA256, expected.RequestIDSHA256,
		expected.NonceSHA256, expected.ContextSHA256,
	)
	if err != nil {
		return aiPanoramaAdvancedOperationBinding{}, err
	}
	var matched *aiPanoramaOperationEntryBinding
	for index, rawEntry := range entries {
		entry, ok := rawEntry.(map[string]any)
		if !ok || !hasKeys(
			entry, "sequence", "operation_id", "event", "permit_sha256",
			"request_id_sha256", "nonce_sha256", "context_sha256", "evidence",
			"evidence_sha256", "previous_entry_sha256", "entry_sha256",
		) {
			return aiPanoramaAdvancedOperationBinding{}, fmt.Errorf("ai-panorama-operation-journal-invalid")
		}
		entrySequence, sequenceOK := exactInt(
			entry["sequence"], int64(index+1), int64(index+1),
		)
		event, eventOK := exactString(entry["event"])
		operationID, operationIDOK := aiPanoramaExactRawDigest(entry["operation_id"])
		if !sequenceOK || entrySequence != int64(index+1) || !eventOK ||
			(event != "prepared" && event != "committed" && event != "failed-clean" &&
				event != "rolled-back" && event != "recovery-required") ||
			!operationIDOK || entry["previous_entry_sha256"] != previous {
			return aiPanoramaAdvancedOperationBinding{}, fmt.Errorf("ai-panorama-operation-journal-invalid")
		}
		bindings := [4]string{}
		for bindingIndex, key := range []string{
			"permit_sha256", "request_id_sha256", "nonce_sha256", "context_sha256",
		} {
			value, ok := aiPanoramaExactRawDigest(entry[key])
			if !ok {
				return aiPanoramaAdvancedOperationBinding{}, fmt.Errorf("ai-panorama-operation-journal-invalid")
			}
			bindings[bindingIndex] = value
		}
		for _, key := range []string{
			"evidence_sha256", "previous_entry_sha256", "entry_sha256",
		} {
			if _, ok := aiPanoramaExactRawDigest(entry[key]); !ok {
				return aiPanoramaAdvancedOperationBinding{}, fmt.Errorf("ai-panorama-operation-journal-invalid")
			}
		}
		calculatedOperationID, err := aiPanoramaOperationID(
			bindings[0], bindings[1], bindings[2], bindings[3],
		)
		if err != nil || operationID != calculatedOperationID {
			return aiPanoramaAdvancedOperationBinding{}, fmt.Errorf("ai-panorama-operation-journal-invalid")
		}
		evidence, evidenceOK := entry["evidence"].(map[string]any)
		if !evidenceOK {
			return aiPanoramaAdvancedOperationBinding{}, fmt.Errorf("ai-panorama-operation-journal-invalid")
		}
		evidenceRaw, err := aiPanoramaPythonCanonicalJSON(evidence)
		if err != nil || len(evidenceRaw) < 1 ||
			len(evidenceRaw) > aiPanoramaMaximumOperationEvidence ||
			aiPanoramaRawSHA256(evidenceRaw) != entry["evidence_sha256"] {
			zero(evidenceRaw)
			return aiPanoramaAdvancedOperationBinding{}, fmt.Errorf("ai-panorama-operation-journal-invalid")
		}
		zero(evidenceRaw)
		unsigned := cloneFields(entry)
		claimed, _ := exactString(unsigned["entry_sha256"])
		delete(unsigned, "entry_sha256")
		calculated, err := aiPanoramaDomainDigest(aiPanoramaOperationEntryDomain, unsigned)
		if err != nil || claimed != calculated {
			return aiPanoramaAdvancedOperationBinding{}, fmt.Errorf("ai-panorama-operation-journal-invalid")
		}
		state, exists := states[operationID]
		if (event == "prepared" && exists) ||
			(event != "prepared" && (!exists || state.event != "prepared" ||
				state.bindings != bindings)) {
			return aiPanoramaAdvancedOperationBinding{}, fmt.Errorf("ai-panorama-operation-journal-transition-invalid")
		}
		if event == "prepared" {
			states[operationID] = operationState{
				event: event, bindings: bindings, preparedSHA256: claimed,
			}
			if operationID == expectedOperationID &&
				validateAiPanoramaPreparedEvidence(evidence, expected, ledger) != nil {
				return aiPanoramaAdvancedOperationBinding{}, fmt.Errorf("ai-panorama-operation-install-binding-invalid")
			}
		} else {
			states[operationID] = operationState{
				event: event, bindings: bindings, preparedSHA256: state.preparedSHA256,
			}
			if operationID == expectedOperationID && event == expected.TerminalEvent {
				var evidenceErr error
				if event == "committed" {
					evidenceErr = validateAiPanoramaCommittedEvidence(evidence, expected)
				} else {
					evidenceErr = validateAiPanoramaFailedEvidence(evidence, expected, event)
				}
				if matched != nil || evidenceErr != nil {
					return aiPanoramaAdvancedOperationBinding{}, fmt.Errorf("ai-panorama-operation-install-binding-invalid")
				}
				matched = &aiPanoramaOperationEntryBinding{
					OperationID: operationID, PreparedEntrySHA256: state.preparedSHA256,
					TerminalEvent: event, TerminalEntrySHA256: claimed,
				}
			}
		}
		previous = claimed
	}
	if tip != previous || matched == nil {
		return aiPanoramaAdvancedOperationBinding{}, fmt.Errorf("ai-panorama-operation-install-binding-invalid")
	}
	return aiPanoramaAdvancedOperationBinding{
		InstanceID: expectedInstance, Sequence: sequence, TipSHA256: tip,
		OperationID:         matched.OperationID,
		PreparedEntrySHA256: matched.PreparedEntrySHA256,
		TerminalEvent:       matched.TerminalEvent,
		TerminalEntrySHA256: matched.TerminalEntrySHA256,
	}, nil
}

func observeAiPanoramaAdvancedState(
	root string,
	genesis *aiPanoramaStateGenesis,
	expected *aiPanoramaAdvancedStateExpectation,
) (*aiPanoramaAdvancedStateProfile, error) {
	if genesis == nil || expected == nil ||
		validateAiPanoramaGenesisRoot(root, genesis.Root) != nil ||
		len(genesis.Files) != 4 || len(genesis.CompletionFiles) != 4 {
		return nil, fmt.Errorf("ai-panorama-advanced-state-input-invalid")
	}
	for _, file := range genesis.Files {
		if _, err := os.Lstat(rooted(root, file.TemporaryPath)); err == nil {
			return nil, fmt.Errorf("ai-panorama-advanced-state-temporary-present")
		} else if !os.IsNotExist(err) {
			return nil, fmt.Errorf("ai-panorama-advanced-state-observation-failed")
		}
	}
	ledgerValue, ledgerRaw, ledgerProfile, err := readAiPanoramaCanonicalStateObject(
		root, genesis.Root, aiPanoramaLedgerPath, aiPanoramaMaximumLedgerBytes,
	)
	if err != nil {
		return nil, err
	}
	defer zero(ledgerRaw)
	ledger, tombstones, err := validateAiPanoramaLedger(
		root, genesis.Root, ledgerValue, genesis.LedgerInstanceID, expected,
	)
	if err != nil {
		return nil, err
	}
	ledgerLockRaw, ledgerLockProfile, err := readAiPanoramaStateLeaf(
		root, genesis.Root, aiPanoramaLedgerLockPath, 64,
	)
	if err != nil || !bytes.Equal(ledgerLockRaw, []byte("lock\n")) {
		zero(ledgerLockRaw)
		return nil, fmt.Errorf("ai-panorama-ledger-lock-invalid")
	}
	zero(ledgerLockRaw)
	if ledgerLockProfile != genesis.CompletionFiles[1] {
		return nil, fmt.Errorf("ai-panorama-ledger-lock-replaced")
	}
	operationValue, operationRaw, operationProfile, err := readAiPanoramaCanonicalStateObject(
		root, genesis.Root, aiPanoramaOperationPath, aiPanoramaMaximumOperationBytes,
	)
	if err != nil {
		return nil, err
	}
	defer zero(operationRaw)
	operation, err := validateAiPanoramaOperationJournal(
		operationValue, genesis.OperationInstanceID, expected, ledger,
	)
	if err != nil {
		return nil, err
	}
	operationLockRaw, operationLockProfile, err := readAiPanoramaStateLeaf(
		root, genesis.Root, aiPanoramaOperationLockPath, 64,
	)
	if err != nil || !bytes.Equal(operationLockRaw, []byte("lock\n")) {
		zero(operationLockRaw)
		return nil, fmt.Errorf("ai-panorama-operation-lock-invalid")
	}
	zero(operationLockRaw)
	if operationLockProfile != genesis.CompletionFiles[3] {
		return nil, fmt.Errorf("ai-panorama-operation-lock-replaced")
	}
	return &aiPanoramaAdvancedStateProfile{
		Root: genesis.Root,
		Files: []aiPanoramaStateLeafProfile{
			ledgerProfile, ledgerLockProfile, operationProfile, operationLockProfile,
		},
		Tombstones: tombstones,
		Ledger:     ledger, Operation: operation,
	}, nil
}

func validateAiPanoramaAdvancedStateProfile(
	root string,
	genesis *aiPanoramaStateGenesis,
	expected *aiPanoramaAdvancedStateProfile,
) error {
	if genesis == nil || expected == nil ||
		genesis.Root != expected.Root ||
		genesis.LedgerInstanceID != expected.Ledger.InstanceID ||
		genesis.OperationInstanceID != expected.Operation.InstanceID ||
		len(expected.Files) != 4 || len(expected.Tombstones) != 3 ||
		len(genesis.CompletionFiles) != 4 ||
		expected.Files[1] != genesis.CompletionFiles[1] ||
		expected.Files[3] != genesis.CompletionFiles[3] ||
		validateAiPanoramaGenesisRoot(root, genesis.Root) != nil {
		return fmt.Errorf("ai-panorama-advanced-state-profile-invalid")
	}
	tombstonePaths := []string{
		aiPanoramaTombstonePath("request", expected.Ledger.RequestIDSHA256),
		aiPanoramaTombstonePath("nonce", expected.Ledger.NonceSHA256),
		aiPanoramaTombstonePath("permit", expected.Ledger.PermitSHA256),
	}
	for index, expectedTombstone := range expected.Tombstones {
		if expectedTombstone.Path != tombstonePaths[index] {
			return fmt.Errorf("ai-panorama-advanced-state-profile-invalid")
		}
		raw, observed, err := readAiPanoramaStateLeaf(
			root, genesis.Root, expectedTombstone.Path, 16*1024,
		)
		if err != nil || observed != expectedTombstone ||
			len(raw) < 3 || raw[len(raw)-1] != '\n' {
			zero(raw)
			return fmt.Errorf("ai-panorama-advanced-state-tombstone-changed")
		}
		value, parseErr := strictJSON(raw[:len(raw)-1], 16*1024)
		canonical, canonicalErr := aiPanoramaPythonCanonicalJSON(value)
		canonical = append(canonical, '\n')
		valid := parseErr == nil && canonicalErr == nil && bytes.Equal(canonical, raw)
		zero(canonical)
		zero(raw)
		if !valid {
			return fmt.Errorf("ai-panorama-advanced-state-tombstone-changed")
		}
	}
	maximums := []int64{
		aiPanoramaMaximumLedgerBytes, 64,
		aiPanoramaMaximumOperationBytes, 64,
	}
	var ledgerValue map[string]any
	var operationValue map[string]any
	for index, expectedFile := range expected.Files {
		raw, observed, err := readAiPanoramaStateLeaf(
			root, genesis.Root, expectedFile.Path, maximums[index],
		)
		if err != nil || observed != expectedFile {
			zero(raw)
			return fmt.Errorf("ai-panorama-advanced-state-profile-changed")
		}
		if index == 1 || index == 3 {
			if !bytes.Equal(raw, []byte("lock\n")) {
				zero(raw)
				return fmt.Errorf("ai-panorama-advanced-state-lock-changed")
			}
		} else {
			if len(raw) < 3 || raw[len(raw)-1] != '\n' {
				zero(raw)
				return fmt.Errorf("ai-panorama-advanced-state-json-invalid")
			}
			value, err := strictJSON(raw[:len(raw)-1], int(maximums[index]))
			canonical, canonicalErr := aiPanoramaPythonCanonicalJSON(value)
			canonical = append(canonical, '\n')
			valid := err == nil && canonicalErr == nil && bytes.Equal(canonical, raw)
			zero(canonical)
			if !valid {
				zero(raw)
				return fmt.Errorf("ai-panorama-advanced-state-json-invalid")
			}
			if index == 0 {
				ledgerValue = value
			} else {
				operationValue = value
			}
		}
		zero(raw)
	}
	if ledgerValue == nil || operationValue == nil ||
		ledgerValue["instance_id"] != expected.Ledger.InstanceID ||
		ledgerValue["sequence"] != json.Number(strconv.FormatInt(expected.Ledger.Sequence, 10)) ||
		ledgerValue["tip_sha256"] != expected.Ledger.TipSHA256 ||
		operationValue["instance_id"] != expected.Operation.InstanceID ||
		operationValue["sequence"] != json.Number(strconv.FormatInt(expected.Operation.Sequence, 10)) ||
		operationValue["tip_sha256"] != expected.Operation.TipSHA256 {
		return fmt.Errorf("ai-panorama-advanced-state-profile-chain-changed")
	}
	return nil
}
