//go:build linux && amd64

package authority

import (
	"bytes"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strconv"
	"syscall"
)

const (
	aiPanoramaStateGenesisIntentEvent = "ai-panorama-install-state-genesis-intent"
	aiPanoramaStateGenesisEvent       = "ai-panorama-install-state-genesis-completed"
	aiPanoramaStateOperation          = "ai-panorama-state-genesis"
	aiPanoramaLedgerSchema            = "propertyquarry.ai-panorama-install-consumption-ledger.v2"
	aiPanoramaOperationSchema         = "propertyquarry.ai-panorama-install-operation-journal.v1"
	aiPanoramaLedgerPath              = aiPanoramaControlRoot + "/consumption-ledger.v2.json"
	aiPanoramaLedgerLockPath          = aiPanoramaControlRoot + "/consumption-ledger.v2.lock"
	aiPanoramaOperationPath           = aiPanoramaControlRoot + "/operation-journal.v1.json"
	aiPanoramaOperationLockPath       = aiPanoramaControlRoot + "/operation-journal.v1.lock"
	aiPanoramaGenesisBoundaryIntent   = "intent-durable"
	aiPanoramaGenesisBoundaryMidWrite = "temporary-mid-write"
	aiPanoramaGenesisBoundaryWritten  = "temporary-full-write"
	aiPanoramaGenesisBoundaryFileSync = "temporary-file-synced"
	aiPanoramaGenesisBoundaryLinked   = "final-linked"
	aiPanoramaGenesisBoundaryDirSync  = "final-link-directory-synced"
	aiPanoramaGenesisBoundaryComplete = "file-complete"
)

type aiPanoramaGenesisRootIdentity struct {
	Path   string
	Device uint64
	Inode  uint64
	UID    uint32
	GID    uint32
	Mode   os.FileMode
}

type aiPanoramaGenesisFile struct {
	Path          string
	TemporaryPath string
	Mode          os.FileMode
	UID           uint32
	GID           uint32
	SHA256        string
	Raw           []byte
}

type aiPanoramaStateGenesis struct {
	LedgerInstanceID    string
	OperationInstanceID string
	Root                aiPanoramaGenesisRootIdentity
	Files               []aiPanoramaGenesisFile
	CompletionFiles     []aiPanoramaStateLeafProfile
	IntentReceiptDigest string
}

func (value *aiPanoramaStateGenesis) release() {
	if value == nil {
		return
	}
	for index := range value.Files {
		zero(value.Files[index].Raw)
	}
	*value = aiPanoramaStateGenesis{}
}

func newAiPanoramaInstanceID() (string, error) {
	raw := make([]byte, 16)
	if _, err := rand.Read(raw); err != nil {
		zero(raw)
		return "", fmt.Errorf("ai-panorama-state-genesis-random-failed")
	}
	value := hex.EncodeToString(raw)
	zero(raw)
	return value, nil
}

func aiPanoramaStateWire(schema, instanceID string) ([]byte, error) {
	if (schema != aiPanoramaLedgerSchema && schema != aiPanoramaOperationSchema) ||
		!aiPanoramaNoncePattern.MatchString(instanceID) {
		return nil, fmt.Errorf("ai-panorama-state-genesis-input-invalid")
	}
	raw, err := canonicalJSON(map[string]any{
		"authority":   "propertyquarry-release-control",
		"entries":     []any{},
		"instance_id": instanceID,
		"schema":      schema,
		"sequence":    json.Number("0"),
		"tip_sha256":  string(bytes.Repeat([]byte{'0'}, 64)),
	})
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-state-genesis-canonicalization-failed")
	}
	return append(raw, '\n'), nil
}

func validateAiPanoramaControlDirectories(root string) error {
	ownerUID, ownerGID := secureOwner(root)
	for _, path := range []string{
		aiPanoramaControlRoot,
		aiPanoramaPermitRoot,
		aiPanoramaControlRoot + "/tombstones",
	} {
		info, err := os.Lstat(rooted(root, path))
		metadata, ok := infoSys(info)
		if err != nil || !ok || !info.IsDir() || info.Mode().Perm() != 0o700 ||
			info.Mode()&os.ModeSymlink != 0 || metadata.Uid != ownerUID ||
			metadata.Gid != ownerGID || metadata.Nlink < 2 {
			return fmt.Errorf("ai-panorama-state-directory-invalid")
		}
	}
	return nil
}

func observeAiPanoramaGenesisRoot(root string) (aiPanoramaGenesisRootIdentity, error) {
	info, err := os.Lstat(rooted(root, aiPanoramaControlRoot))
	metadata, ok := infoSys(info)
	ownerUID, ownerGID := secureOwner(root)
	if err != nil || !ok || !info.IsDir() || info.Mode().Perm() != 0o700 ||
		info.Mode()&os.ModeSymlink != 0 || metadata.Uid != ownerUID ||
		metadata.Gid != ownerGID || metadata.Nlink < 2 ||
		metadata.Dev == 0 || metadata.Ino == 0 {
		return aiPanoramaGenesisRootIdentity{}, fmt.Errorf("ai-panorama-state-genesis-root-invalid")
	}
	return aiPanoramaGenesisRootIdentity{
		Path: aiPanoramaControlRoot, Device: metadata.Dev, Inode: metadata.Ino,
		UID: metadata.Uid, GID: metadata.Gid, Mode: info.Mode().Perm(),
	}, nil
}

type aiPanoramaGenesisFaultHook func(int, string) error

func aiPanoramaGenesisTemporaryPath(
	index int,
	ledgerID string,
	operationID string,
	path string,
	sha256Value string,
) string {
	binding := []byte(fmt.Sprintf(
		"%d\x00%s\x00%s\x00%s\x00%s",
		index, ledgerID, operationID, path, sha256Value,
	))
	bindingSHA256 := aiPanoramaRawSHA256(binding)
	zero(binding)
	return fmt.Sprintf("%s/.genesis-%s.tmp", aiPanoramaControlRoot, bindingSHA256)
}

func newAiPanoramaStateGenesis(root string) (*aiPanoramaStateGenesis, error) {
	ledgerID, err := newAiPanoramaInstanceID()
	if err != nil {
		return nil, err
	}
	operationID, err := newAiPanoramaInstanceID()
	if err != nil || operationID == ledgerID {
		return nil, fmt.Errorf("ai-panorama-state-genesis-random-failed")
	}
	rootIdentity, err := observeAiPanoramaGenesisRoot(root)
	if err != nil {
		return nil, err
	}
	ledgerRaw, err := aiPanoramaStateWire(aiPanoramaLedgerSchema, ledgerID)
	if err != nil {
		return nil, err
	}
	operationRaw, err := aiPanoramaStateWire(aiPanoramaOperationSchema, operationID)
	if err != nil {
		zero(ledgerRaw)
		return nil, err
	}
	lockRaw := []byte("lock\n")
	genesis := &aiPanoramaStateGenesis{
		LedgerInstanceID: ledgerID, OperationInstanceID: operationID,
		Root: rootIdentity,
		Files: []aiPanoramaGenesisFile{
			{
				Path: aiPanoramaLedgerPath, Mode: 0o600,
				UID: rootIdentity.UID, GID: rootIdentity.GID,
				SHA256: aiPanoramaRawSHA256(ledgerRaw), Raw: ledgerRaw,
			},
			{
				Path: aiPanoramaLedgerLockPath, Mode: 0o600,
				UID: rootIdentity.UID, GID: rootIdentity.GID,
				SHA256: aiPanoramaRawSHA256(lockRaw), Raw: append([]byte(nil), lockRaw...),
			},
			{
				Path: aiPanoramaOperationPath, Mode: 0o600,
				UID: rootIdentity.UID, GID: rootIdentity.GID,
				SHA256: aiPanoramaRawSHA256(operationRaw), Raw: operationRaw,
			},
			{
				Path: aiPanoramaOperationLockPath, Mode: 0o600,
				UID: rootIdentity.UID, GID: rootIdentity.GID,
				SHA256: aiPanoramaRawSHA256(lockRaw), Raw: append([]byte(nil), lockRaw...),
			},
		},
	}
	for index := range genesis.Files {
		genesis.Files[index].TemporaryPath = aiPanoramaGenesisTemporaryPath(
			index, ledgerID, operationID,
			genesis.Files[index].Path, genesis.Files[index].SHA256,
		)
	}
	return genesis, nil
}

func aiPanoramaGenesisIntentValue(value *aiPanoramaStateGenesis) map[string]any {
	files := make([]any, 0, len(value.Files))
	for _, file := range value.Files {
		files = append(files, map[string]any{
			"path": file.Path, "temporary_path": file.TemporaryPath,
			"mode":                   json.Number(strconv.FormatUint(uint64(file.Mode), 10)),
			"uid":                    json.Number(strconv.FormatUint(uint64(file.UID), 10)),
			"gid":                    json.Number(strconv.FormatUint(uint64(file.GID), 10)),
			"sha256":                 file.SHA256,
			"canonical_bytes_base64": base64.RawStdEncoding.EncodeToString(file.Raw),
		})
	}
	return map[string]any{
		"ledger_instance_id":    value.LedgerInstanceID,
		"operation_instance_id": value.OperationInstanceID,
		"root_identity": map[string]any{
			"path":   value.Root.Path,
			"device": json.Number(strconv.FormatUint(value.Root.Device, 10)),
			"inode":  json.Number(strconv.FormatUint(value.Root.Inode, 10)),
			"uid":    json.Number(strconv.FormatUint(uint64(value.Root.UID), 10)),
			"gid":    json.Number(strconv.FormatUint(uint64(value.Root.GID), 10)),
			"mode":   json.Number(strconv.FormatUint(uint64(value.Root.Mode), 10)),
		},
		"files": files,
	}
}

func parseAiPanoramaGenesisIntent(raw any) (*aiPanoramaStateGenesis, error) {
	value, ok := raw.(map[string]any)
	if !ok || !hasKeys(
		value, "ledger_instance_id", "operation_instance_id", "root_identity", "files",
	) {
		return nil, fmt.Errorf("ai-panorama-state-genesis-intent-invalid")
	}
	ledgerID, ledgerOK := exactString(value["ledger_instance_id"])
	operationID, operationOK := exactString(value["operation_instance_id"])
	if !ledgerOK || !operationOK || ledgerID == operationID ||
		!aiPanoramaNoncePattern.MatchString(ledgerID) ||
		!aiPanoramaNoncePattern.MatchString(operationID) {
		return nil, fmt.Errorf("ai-panorama-state-genesis-intent-invalid")
	}
	rootValue, ok := value["root_identity"].(map[string]any)
	if !ok || !hasKeys(rootValue, "path", "device", "inode", "uid", "gid", "mode") ||
		rootValue["path"] != aiPanoramaControlRoot {
		return nil, fmt.Errorf("ai-panorama-state-genesis-intent-invalid")
	}
	device, deviceOK := exactInt(rootValue["device"], 1, 1<<62)
	inode, inodeOK := exactInt(rootValue["inode"], 1, 1<<62)
	uid, uidOK := exactInt(rootValue["uid"], 0, 1<<32-1)
	gid, gidOK := exactInt(rootValue["gid"], 0, 1<<32-1)
	mode, modeOK := exactInt(rootValue["mode"], 0, 0o777)
	if !deviceOK || !inodeOK || !uidOK || !gidOK || !modeOK || mode != 0o700 {
		return nil, fmt.Errorf("ai-panorama-state-genesis-intent-invalid")
	}
	fileValues, ok := value["files"].([]any)
	if !ok || len(fileValues) != 4 {
		return nil, fmt.Errorf("ai-panorama-state-genesis-intent-invalid")
	}
	expectedPaths := []string{
		aiPanoramaLedgerPath, aiPanoramaLedgerLockPath,
		aiPanoramaOperationPath, aiPanoramaOperationLockPath,
	}
	expectedLedger, err := aiPanoramaStateWire(aiPanoramaLedgerSchema, ledgerID)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-state-genesis-intent-invalid")
	}
	expectedOperation, err := aiPanoramaStateWire(aiPanoramaOperationSchema, operationID)
	if err != nil {
		zero(expectedLedger)
		return nil, fmt.Errorf("ai-panorama-state-genesis-intent-invalid")
	}
	expectedRaw := [][]byte{
		expectedLedger, []byte("lock\n"), expectedOperation, []byte("lock\n"),
	}
	files := make([]aiPanoramaGenesisFile, 0, len(fileValues))
	valid := true
	for index, untyped := range fileValues {
		fileValue, itemOK := untyped.(map[string]any)
		if !itemOK || !hasKeys(
			fileValue, "path", "temporary_path", "mode", "uid", "gid",
			"sha256", "canonical_bytes_base64",
		) {
			valid = false
			break
		}
		path, pathOK := exactString(fileValue["path"])
		temporaryPath, temporaryPathOK := exactString(fileValue["temporary_path"])
		fileMode, fileModeOK := exactInt(fileValue["mode"], 0, 0o777)
		fileUID, fileUIDOK := exactInt(fileValue["uid"], 0, 1<<32-1)
		fileGID, fileGIDOK := exactInt(fileValue["gid"], 0, 1<<32-1)
		sha256Value, shaOK := exactString(fileValue["sha256"])
		encoded, encodedOK := exactString(fileValue["canonical_bytes_base64"])
		decoded, decodeErr := base64.RawStdEncoding.Strict().DecodeString(encoded)
		itemValid := pathOK && path == expectedPaths[index] &&
			temporaryPathOK &&
			temporaryPath == aiPanoramaGenesisTemporaryPath(
				index, ledgerID, operationID, path, sha256Value,
			) &&
			fileModeOK && fileMode == 0o600 &&
			fileUIDOK && fileUID == uid && fileGIDOK && fileGID == gid &&
			shaOK && aiPanoramaRawSHA256Pattern.MatchString(sha256Value) &&
			encodedOK && decodeErr == nil && bytes.Equal(decoded, expectedRaw[index]) &&
			aiPanoramaRawSHA256(decoded) == sha256Value
		if !itemValid {
			zero(decoded)
			valid = false
			break
		}
		files = append(files, aiPanoramaGenesisFile{
			Path: path, TemporaryPath: temporaryPath, Mode: os.FileMode(fileMode),
			UID: uint32(fileUID), GID: uint32(fileGID),
			SHA256: sha256Value, Raw: decoded,
		})
	}
	for index := range expectedRaw {
		zero(expectedRaw[index])
	}
	if !valid {
		for index := range files {
			zero(files[index].Raw)
		}
		return nil, fmt.Errorf("ai-panorama-state-genesis-intent-invalid")
	}
	return &aiPanoramaStateGenesis{
		LedgerInstanceID: ledgerID, OperationInstanceID: operationID,
		Root: aiPanoramaGenesisRootIdentity{
			Path: aiPanoramaControlRoot, Device: uint64(device), Inode: uint64(inode),
			UID: uint32(uid), GID: uint32(gid), Mode: os.FileMode(mode),
		},
		Files: files,
	}, nil
}

func aiPanoramaStateGenesisFromEvent(
	journal *Journal,
) (*aiPanoramaStateGenesis, bool, error) {
	if journal == nil {
		return nil, false, fmt.Errorf("ai-panorama-state-genesis-journal-missing")
	}
	var intent *JournalEvent
	var completed *JournalEvent
	for index := range journal.events {
		event := &journal.events[index]
		switch event.EventType {
		case aiPanoramaStateGenesisIntentEvent:
			if intent != nil || completed != nil {
				return nil, false, fmt.Errorf("ai-panorama-state-genesis-event-duplicated")
			}
			intent = event
		case aiPanoramaStateGenesisEvent:
			if intent == nil || completed != nil {
				return nil, false, fmt.Errorf("ai-panorama-state-genesis-event-invalid")
			}
			completed = event
		}
	}
	if intent == nil {
		return nil, false, nil
	}
	if intent.Operation != aiPanoramaStateOperation ||
		!digestPattern.MatchString(intent.ReceiptDigest) {
		return nil, false, fmt.Errorf("ai-panorama-state-genesis-event-invalid")
	}
	genesis, err := parseAiPanoramaGenesisIntent(
		intent.Payload["ai_panorama_state_genesis_intent"],
	)
	if err != nil {
		return nil, false, err
	}
	genesis.IntentReceiptDigest = intent.ReceiptDigest
	if completed == nil {
		return genesis, false, nil
	}
	completion, ok := completed.Payload["ai_panorama_state_genesis_completion"].(map[string]any)
	if completed.Operation != aiPanoramaStateOperation || !ok ||
		!hasKeys(completion, "intent_receipt_sha256", "verified", "files") ||
		completion["intent_receipt_sha256"] != intent.ReceiptDigest ||
		completion["verified"] != true {
		genesis.release()
		return nil, false, fmt.Errorf("ai-panorama-state-genesis-event-invalid")
	}
	completionFiles, err := parseAiPanoramaGenesisCompletionFiles(
		completion["files"], genesis,
	)
	if err != nil {
		genesis.release()
		return nil, false, err
	}
	genesis.CompletionFiles = completionFiles
	return genesis, true, nil
}

func validateAiPanoramaGenesisRoot(
	root string,
	expected aiPanoramaGenesisRootIdentity,
) error {
	observed, err := observeAiPanoramaGenesisRoot(root)
	if err != nil || observed != expected {
		return fmt.Errorf("ai-panorama-state-genesis-root-replaced")
	}
	return nil
}

func validateAiPanoramaGenesisFile(
	root string,
	expected aiPanoramaGenesisFile,
) error {
	raw, _, err := readAiPanoramaGenesisLeaf(root, expected.Path, expected, 1)
	valid := err == nil && bytes.Equal(raw, expected.Raw) &&
		aiPanoramaRawSHA256(raw) == expected.SHA256
	zero(raw)
	if !valid {
		return fmt.Errorf("ai-panorama-state-genesis-file-invalid")
	}
	return nil
}

func validateAiPanoramaStateGenesis(root string, expected *aiPanoramaStateGenesis) error {
	if expected == nil || validateAiPanoramaGenesisRoot(root, expected.Root) != nil ||
		len(expected.Files) != 4 {
		return fmt.Errorf("ai-panorama-state-genesis-missing")
	}
	for _, file := range expected.Files {
		if err := validateAiPanoramaGenesisFile(root, file); err != nil {
			return err
		}
		if _, err := os.Lstat(rooted(root, file.TemporaryPath)); err == nil {
			return fmt.Errorf("ai-panorama-state-genesis-temporary-present")
		} else if !os.IsNotExist(err) {
			return fmt.Errorf("ai-panorama-state-genesis-observation-failed")
		}
	}
	return nil
}

func readAiPanoramaGenesisLeaf(
	root string,
	path string,
	expected aiPanoramaGenesisFile,
	requiredNlink uint64,
) ([]byte, os.FileInfo, error) {
	file, err := os.OpenFile(
		rooted(root, path),
		os.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW,
		0,
	)
	if err != nil {
		return nil, nil, fmt.Errorf("ai-panorama-state-genesis-leaf-unavailable")
	}
	defer file.Close()
	info, err := file.Stat()
	metadata, ok := infoSys(info)
	if err != nil || !ok || !info.Mode().IsRegular() ||
		info.Mode().Perm() != expected.Mode ||
		metadata.Uid != expected.UID || metadata.Gid != expected.GID ||
		metadata.Nlink != requiredNlink ||
		info.Size() < 0 || info.Size() > int64(len(expected.Raw)) {
		return nil, nil, fmt.Errorf("ai-panorama-state-genesis-leaf-metadata-invalid")
	}
	raw := make([]byte, info.Size())
	if _, err := io.ReadFull(file, raw); err != nil {
		zero(raw)
		return nil, nil, fmt.Errorf("ai-panorama-state-genesis-leaf-read-failed")
	}
	after, err := file.Stat()
	if err != nil || !os.SameFile(info, after) {
		zero(raw)
		return nil, nil, fmt.Errorf("ai-panorama-state-genesis-leaf-changed")
	}
	return raw, info, nil
}

func removeAiPanoramaGenesisTemporary(
	root string,
	expected aiPanoramaGenesisFile,
	requireExact bool,
) error {
	raw, _, err := readAiPanoramaGenesisLeaf(
		root, expected.TemporaryPath, expected, 1,
	)
	valid := err == nil && bytes.HasPrefix(expected.Raw, raw) &&
		(!requireExact || bytes.Equal(raw, expected.Raw))
	zero(raw)
	if !valid {
		return fmt.Errorf("ai-panorama-state-genesis-temporary-invalid")
	}
	if err := os.Remove(rooted(root, expected.TemporaryPath)); err != nil {
		return fmt.Errorf("ai-panorama-state-genesis-temporary-remove-failed")
	}
	if err := fsyncAiPanoramaDirectory(rooted(root, aiPanoramaControlRoot)); err != nil {
		return fmt.Errorf("ai-panorama-state-genesis-durability-unknown")
	}
	if _, err := os.Lstat(rooted(root, expected.TemporaryPath)); !os.IsNotExist(err) {
		return fmt.Errorf("ai-panorama-state-genesis-temporary-remove-failed")
	}
	return nil
}

func invokeAiPanoramaGenesisFault(
	hook aiPanoramaGenesisFaultHook,
	index int,
	boundary string,
) error {
	if hook == nil {
		return nil
	}
	return hook(index+1, boundary)
}

func prepareAiPanoramaGenesisTemporary(
	root string,
	index int,
	expected aiPanoramaGenesisFile,
	hook aiPanoramaGenesisFaultHook,
) error {
	if _, err := os.Lstat(rooted(root, expected.TemporaryPath)); err == nil {
		raw, _, readErr := readAiPanoramaGenesisLeaf(
			root, expected.TemporaryPath, expected, 1,
		)
		exact := readErr == nil && bytes.Equal(raw, expected.Raw) &&
			aiPanoramaRawSHA256(raw) == expected.SHA256
		prefix := readErr == nil && bytes.HasPrefix(expected.Raw, raw)
		zero(raw)
		if exact {
			return fsyncAiPanoramaGenesisTemporary(root, expected)
		}
		if !prefix {
			return fmt.Errorf("ai-panorama-state-genesis-temporary-invalid")
		}
		if err := removeAiPanoramaGenesisTemporary(root, expected, false); err != nil {
			return err
		}
	} else if !os.IsNotExist(err) {
		return fmt.Errorf("ai-panorama-state-genesis-observation-failed")
	}
	file, err := os.OpenFile(
		rooted(root, expected.TemporaryPath),
		os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_CLOEXEC|syscall.O_NOFOLLOW,
		expected.Mode,
	)
	if err != nil {
		return fmt.Errorf("ai-panorama-state-genesis-temporary-create-failed")
	}
	closed := false
	defer func() {
		if !closed {
			_ = file.Close()
		}
	}()
	middle := len(expected.Raw) / 2
	if middle < 1 {
		middle = 1
	}
	if err := writeAll(file, expected.Raw[:middle]); err != nil {
		return fmt.Errorf("ai-panorama-state-genesis-temporary-write-failed")
	}
	if err := invokeAiPanoramaGenesisFault(
		hook, index, aiPanoramaGenesisBoundaryMidWrite,
	); err != nil {
		return err
	}
	if err := writeAll(file, expected.Raw[middle:]); err != nil {
		return fmt.Errorf("ai-panorama-state-genesis-temporary-write-failed")
	}
	if err := invokeAiPanoramaGenesisFault(
		hook, index, aiPanoramaGenesisBoundaryWritten,
	); err != nil {
		return err
	}
	if file.Sync() != nil {
		return fmt.Errorf("ai-panorama-state-genesis-temporary-write-failed")
	}
	if err := invokeAiPanoramaGenesisFault(
		hook, index, aiPanoramaGenesisBoundaryFileSync,
	); err != nil {
		return err
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("ai-panorama-state-genesis-temporary-write-failed")
	}
	closed = true
	raw, _, err := readAiPanoramaGenesisLeaf(
		root, expected.TemporaryPath, expected, 1,
	)
	valid := err == nil && bytes.Equal(raw, expected.Raw) &&
		aiPanoramaRawSHA256(raw) == expected.SHA256
	zero(raw)
	if !valid {
		return fmt.Errorf("ai-panorama-state-genesis-temporary-invalid")
	}
	return nil
}

func fsyncAiPanoramaGenesisTemporary(
	root string,
	expected aiPanoramaGenesisFile,
) error {
	file, err := os.OpenFile(
		rooted(root, expected.TemporaryPath),
		os.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW,
		0,
	)
	if err != nil {
		return fmt.Errorf("ai-panorama-state-genesis-temporary-unavailable")
	}
	info, err := file.Stat()
	metadata, ok := infoSys(info)
	valid := err == nil && ok && info.Mode().IsRegular() &&
		info.Mode().Perm() == expected.Mode &&
		metadata.Uid == expected.UID && metadata.Gid == expected.GID &&
		metadata.Nlink == 1 && info.Size() == int64(len(expected.Raw))
	if !valid || file.Sync() != nil || file.Close() != nil {
		_ = file.Close()
		return fmt.Errorf("ai-panorama-state-genesis-temporary-sync-failed")
	}
	raw, _, err := readAiPanoramaGenesisLeaf(
		root, expected.TemporaryPath, expected, 1,
	)
	valid = err == nil && bytes.Equal(raw, expected.Raw) &&
		aiPanoramaRawSHA256(raw) == expected.SHA256
	zero(raw)
	if !valid {
		return fmt.Errorf("ai-panorama-state-genesis-temporary-invalid")
	}
	return nil
}

func recoverAiPanoramaGenesisPublishedFile(
	root string,
	expected aiPanoramaGenesisFile,
) error {
	if _, err := os.Lstat(rooted(root, expected.TemporaryPath)); os.IsNotExist(err) {
		if err := validateAiPanoramaGenesisFile(root, expected); err != nil {
			return err
		}
		if err := fsyncAiPanoramaDirectory(rooted(root, aiPanoramaControlRoot)); err != nil {
			return fmt.Errorf("ai-panorama-state-genesis-durability-unknown")
		}
		return nil
	} else if err != nil {
		return fmt.Errorf("ai-panorama-state-genesis-observation-failed")
	}
	finalRaw, finalInfo, finalErr := readAiPanoramaGenesisLeaf(
		root, expected.Path, expected, 2,
	)
	tempRaw, tempInfo, tempErr := readAiPanoramaGenesisLeaf(
		root, expected.TemporaryPath, expected, 2,
	)
	valid := finalErr == nil && tempErr == nil &&
		os.SameFile(finalInfo, tempInfo) &&
		bytes.Equal(finalRaw, expected.Raw) &&
		bytes.Equal(tempRaw, expected.Raw) &&
		aiPanoramaRawSHA256(finalRaw) == expected.SHA256 &&
		aiPanoramaRawSHA256(tempRaw) == expected.SHA256
	zero(finalRaw)
	zero(tempRaw)
	if !valid {
		return fmt.Errorf("ai-panorama-state-genesis-published-alias-invalid")
	}
	if err := fsyncAiPanoramaDirectory(rooted(root, aiPanoramaControlRoot)); err != nil {
		return fmt.Errorf("ai-panorama-state-genesis-durability-unknown")
	}
	if err := os.Remove(rooted(root, expected.TemporaryPath)); err != nil {
		return fmt.Errorf("ai-panorama-state-genesis-temporary-remove-failed")
	}
	if err := fsyncAiPanoramaDirectory(rooted(root, aiPanoramaControlRoot)); err != nil {
		return fmt.Errorf("ai-panorama-state-genesis-durability-unknown")
	}
	return validateAiPanoramaGenesisFile(root, expected)
}

func publishAiPanoramaGenesisFile(
	root string,
	index int,
	expected aiPanoramaGenesisFile,
	hook aiPanoramaGenesisFaultHook,
) error {
	if err := prepareAiPanoramaGenesisTemporary(root, index, expected, hook); err != nil {
		return err
	}
	if err := os.Link(
		rooted(root, expected.TemporaryPath),
		rooted(root, expected.Path),
	); err != nil {
		return fmt.Errorf("ai-panorama-state-genesis-publish-failed")
	}
	if err := invokeAiPanoramaGenesisFault(
		hook, index, aiPanoramaGenesisBoundaryLinked,
	); err != nil {
		return err
	}
	if err := fsyncAiPanoramaDirectory(rooted(root, aiPanoramaControlRoot)); err != nil {
		return fmt.Errorf("ai-panorama-state-genesis-durability-unknown")
	}
	if err := invokeAiPanoramaGenesisFault(
		hook, index, aiPanoramaGenesisBoundaryDirSync,
	); err != nil {
		return err
	}
	finalRaw, finalInfo, finalErr := readAiPanoramaGenesisLeaf(
		root, expected.Path, expected, 2,
	)
	tempRaw, tempInfo, tempErr := readAiPanoramaGenesisLeaf(
		root, expected.TemporaryPath, expected, 2,
	)
	valid := finalErr == nil && tempErr == nil &&
		os.SameFile(finalInfo, tempInfo) &&
		bytes.Equal(finalRaw, expected.Raw) &&
		bytes.Equal(tempRaw, expected.Raw) &&
		aiPanoramaRawSHA256(finalRaw) == expected.SHA256 &&
		aiPanoramaRawSHA256(tempRaw) == expected.SHA256
	zero(finalRaw)
	zero(tempRaw)
	if !valid {
		return fmt.Errorf("ai-panorama-state-genesis-publish-invalid")
	}
	if err := os.Remove(rooted(root, expected.TemporaryPath)); err != nil {
		return fmt.Errorf("ai-panorama-state-genesis-temporary-remove-failed")
	}
	if err := fsyncAiPanoramaDirectory(rooted(root, aiPanoramaControlRoot)); err != nil {
		return fmt.Errorf("ai-panorama-state-genesis-durability-unknown")
	}
	if err := invokeAiPanoramaGenesisFault(
		hook, index, aiPanoramaGenesisBoundaryComplete,
	); err != nil {
		return err
	}
	return validateAiPanoramaGenesisFile(root, expected)
}

func materializeAiPanoramaStateGenesis(
	root string,
	expected *aiPanoramaStateGenesis,
	hook aiPanoramaGenesisFaultHook,
) error {
	if expected == nil || validateAiPanoramaGenesisRoot(root, expected.Root) != nil {
		return fmt.Errorf("ai-panorama-state-genesis-root-replaced")
	}
	for index, file := range expected.Files {
		_, err := os.Lstat(rooted(root, file.Path))
		switch {
		case err == nil:
			if err := recoverAiPanoramaGenesisPublishedFile(root, file); err != nil {
				return err
			}
		case os.IsNotExist(err):
			if err := publishAiPanoramaGenesisFile(root, index, file, hook); err != nil {
				return err
			}
		default:
			return fmt.Errorf("ai-panorama-state-genesis-observation-failed")
		}
	}
	if err := fsyncAiPanoramaDirectory(rooted(root, aiPanoramaControlRoot)); err != nil {
		return fmt.Errorf("ai-panorama-state-genesis-durability-unknown")
	}
	return validateAiPanoramaStateGenesis(root, expected)
}

func ensureAiPanoramaStateGenesis(root string, journal *Journal, base map[string]any) error {
	return ensureAiPanoramaStateGenesisWithHook(root, journal, base, nil)
}

func ensureAiPanoramaStateGenesisWithHook(
	root string,
	journal *Journal,
	base map[string]any,
	hook aiPanoramaGenesisFaultHook,
) error {
	if root == "" {
		root = "/"
	}
	if journal == nil || base == nil || validateAiPanoramaControlDirectories(root) != nil {
		return fmt.Errorf("ai-panorama-state-genesis-input-invalid")
	}
	existing, completed, err := aiPanoramaStateGenesisFromEvent(journal)
	if err != nil {
		return err
	}
	if existing != nil {
		defer existing.release()
		if completed {
			if err := validateAiPanoramaStateGenesis(root, existing); err != nil {
				return err
			}
			return validateAiPanoramaGenesisCompletionFiles(root, existing)
		}
		if err := materializeAiPanoramaStateGenesis(root, existing, hook); err != nil {
			return err
		}
		fields := cloneFields(base)
		fields["operation"] = aiPanoramaStateOperation
		completionFiles, err := observeAiPanoramaGenesisCompletionFiles(root, existing)
		if err != nil {
			return err
		}
		fields["ai_panorama_state_genesis_completion"] = map[string]any{
			"intent_receipt_sha256": existing.IntentReceiptDigest,
			"verified":              true,
			"files":                 aiPanoramaStateLeafProfilesValue(completionFiles),
		}
		wire, err := journal.Append(aiPanoramaStateGenesisEvent, fields)
		zero(wire)
		return err
	}
	for _, path := range []string{
		aiPanoramaLedgerPath, aiPanoramaLedgerLockPath,
		aiPanoramaOperationPath, aiPanoramaOperationLockPath,
	} {
		if _, err := os.Lstat(rooted(root, path)); err == nil {
			return fmt.Errorf("ai-panorama-state-genesis-partial")
		} else if !os.IsNotExist(err) {
			return fmt.Errorf("ai-panorama-state-genesis-observation-failed")
		}
	}
	genesis, err := newAiPanoramaStateGenesis(root)
	if err != nil {
		return err
	}
	defer genesis.release()
	for _, file := range genesis.Files {
		if _, err := os.Lstat(rooted(root, file.TemporaryPath)); err == nil {
			return fmt.Errorf("ai-panorama-state-genesis-partial")
		} else if !os.IsNotExist(err) {
			return fmt.Errorf("ai-panorama-state-genesis-observation-failed")
		}
	}
	fields := cloneFields(base)
	fields["operation"] = aiPanoramaStateOperation
	fields["ai_panorama_state_genesis_intent"] = aiPanoramaGenesisIntentValue(genesis)
	wire, err := journal.Append(aiPanoramaStateGenesisIntentEvent, fields)
	zero(wire)
	if err != nil {
		return err
	}
	genesis.IntentReceiptDigest = journal.events[len(journal.events)-1].ReceiptDigest
	if hook != nil {
		if err := hook(0, aiPanoramaGenesisBoundaryIntent); err != nil {
			return err
		}
	}
	if err := materializeAiPanoramaStateGenesis(root, genesis, hook); err != nil {
		return err
	}
	completionFiles, err := observeAiPanoramaGenesisCompletionFiles(root, genesis)
	if err != nil {
		return err
	}
	fields = cloneFields(base)
	fields["operation"] = aiPanoramaStateOperation
	fields["ai_panorama_state_genesis_completion"] = map[string]any{
		"intent_receipt_sha256": genesis.IntentReceiptDigest,
		"verified":              true,
		"files":                 aiPanoramaStateLeafProfilesValue(completionFiles),
	}
	wire, err = journal.Append(aiPanoramaStateGenesisEvent, fields)
	zero(wire)
	return err
}
