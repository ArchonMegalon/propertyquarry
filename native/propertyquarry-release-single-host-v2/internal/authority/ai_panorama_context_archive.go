//go:build linux && amd64

package authority

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"syscall"
)

const (
	aiPanoramaContextArchiveCompletedEvent = "ai-panorama-install-context-archive-completed"
	aiPanoramaContextArchiveSchema         = "propertyquarry.ai-panorama-context-archive.v1"
	aiPanoramaContextArchiveObservation    = "propertyquarry.ai-panorama-context-archive-observation.v1"
	aiPanoramaContextArchiveRoot           = "/var/lib/propertyquarry-release-single-host-v2/ai-panorama-context-archives"
)

type aiPanoramaContextArchiveFile struct {
	Kind        string
	Path        string
	MountTarget string
	SHA256      string
	Raw         []byte
}

func (value *aiPanoramaContextArchiveFile) release() {
	if value == nil {
		return
	}
	zero(value.Raw)
	*value = aiPanoramaContextArchiveFile{}
}

type aiPanoramaContextArchive struct {
	RequestID       string
	RequestIDSHA256 string
	PermitSHA256    string
	Path            string
	StagePath       string
	KeyID           string
	KeyEpoch        int64
	KeySHA256       string
	KeyringSHA256   string
	Files           []aiPanoramaContextArchiveFile
}

func (value *aiPanoramaContextArchive) release() {
	if value == nil {
		return
	}
	for index := range value.Files {
		value.Files[index].release()
	}
	*value = aiPanoramaContextArchive{}
}

func aiPanoramaContextArchivePaths(
	permitSHA256 string,
) (string, string, error) {
	if !aiPanoramaRawSHA256Pattern.MatchString(permitSHA256) {
		return "", "", fmt.Errorf("ai-panorama-context-archive-permit-invalid")
	}
	return aiPanoramaContextArchiveRoot + "/" + permitSHA256,
		aiPanoramaContextArchiveRoot + "/.stage-" + permitSHA256, nil
}

func aiPanoramaContextArchiveFileContract(
	kind string,
) (string, string, bool) {
	switch kind {
	case "compose-plan":
		return "public-tour-compose-plan.v1.json", aiPanoramaComposePlanPath, true
	case "volume-profile":
		return "public-tour-volume-profile.v2.json", aiPanoramaVolumeProfilePath, true
	case "trust-assertion":
		return "ai-panorama-install-trust-assertion.v1.json", aiPanoramaTrustAssertionPath, true
	case "keyring":
		return "ai-panorama-install-keyring.v1.json", aiPanoramaPurposeKeyringPath, true
	default:
		return "", "", false
	}
}

func newAiPanoramaContextArchive(
	signing *aiPanoramaSigningContext,
	key *aiPanoramaPurposeKey,
	permit *aiPanoramaSignedPermit,
) (*aiPanoramaContextArchive, error) {
	if signing == nil || key == nil || permit == nil ||
		!aiPanoramaNoncePattern.MatchString(permit.RequestID) ||
		permit.KeyID != key.KeyID || permit.KeyEpoch != key.Epoch ||
		permit.KeySHA256 != key.PublicSHA256 ||
		permit.KeyringSHA256 != key.KeyringSHA256 {
		return nil, fmt.Errorf("ai-panorama-context-archive-input-invalid")
	}
	path, stagePath, err := aiPanoramaContextArchivePaths(permit.SHA256)
	if err != nil {
		return nil, err
	}
	type input struct {
		kind   string
		raw    []byte
		digest string
	}
	inputs := []input{
		{"keyring", key.Raw, key.KeyringSHA256},
		{"trust-assertion", signing.TrustAssertionCanonicalRaw, signing.TrustAssertionSHA256},
		{"compose-plan", signing.ComposePlanCanonicalRaw, signing.ComposePlanSHA256},
		{"volume-profile", signing.VolumeProfileCanonicalRaw, signing.VolumeProfileSHA256},
	}
	archive := &aiPanoramaContextArchive{
		RequestID: permit.RequestID, RequestIDSHA256: aiPanoramaRawSHA256([]byte(permit.RequestID)),
		PermitSHA256: permit.SHA256, Path: path, StagePath: stagePath,
		KeyID: key.KeyID, KeyEpoch: key.Epoch, KeySHA256: key.PublicSHA256,
		KeyringSHA256: key.KeyringSHA256,
		Files:         make([]aiPanoramaContextArchiveFile, 0, len(inputs)),
	}
	for _, item := range inputs {
		name, target, ok := aiPanoramaContextArchiveFileContract(item.kind)
		maximum := aiPanoramaMaximumContextFile
		if item.kind == "keyring" {
			maximum = aiPanoramaMaximumKeyring
		}
		if !ok || len(item.raw) < 2 || len(item.raw) > maximum ||
			item.raw[len(item.raw)-1] != '\n' ||
			aiPanoramaRawSHA256(item.raw) != item.digest {
			archive.release()
			return nil, fmt.Errorf("ai-panorama-context-archive-file-invalid")
		}
		archive.Files = append(archive.Files, aiPanoramaContextArchiveFile{
			Kind: item.kind, Path: path + "/" + name, MountTarget: target,
			SHA256: item.digest, Raw: append([]byte(nil), item.raw...),
		})
	}
	return archive, nil
}

func (value *aiPanoramaContextArchive) journalValue() map[string]any {
	files := make([]any, 0, len(value.Files))
	for _, file := range value.Files {
		files = append(files, map[string]any{
			"kind": file.Kind, "path": file.Path,
			"mount_target": file.MountTarget,
			"mode":         json.Number(strconv.FormatUint(0o400, 10)),
			"size_bytes":   json.Number(strconv.Itoa(len(file.Raw))),
			"sha256":       file.SHA256,
		})
	}
	return map[string]any{
		"schema": aiPanoramaContextArchiveSchema, "version": json.Number("1"),
		"request_id": value.RequestID, "request_id_sha256": value.RequestIDSHA256,
		"permit_sha256": value.PermitSHA256, "path": value.Path,
		"stage_path": value.StagePath,
		"key_id":     value.KeyID, "key_epoch": json.Number(strconv.FormatInt(value.KeyEpoch, 10)),
		"key_sha256": value.KeySHA256, "keyring_sha256": value.KeyringSHA256,
		"keyring_canonical_bytes_base64": base64.RawStdEncoding.EncodeToString(
			value.Files[0].Raw,
		),
		"files": files,
	}
}

func parseAiPanoramaContextArchive(
	base map[string]any,
) (*aiPanoramaContextArchive, error) {
	value, ok := base["ai_panorama_context_archive"].(map[string]any)
	if !ok || !hasKeys(
		value, "schema", "version", "request_id", "request_id_sha256",
		"permit_sha256", "path", "stage_path", "key_id", "key_epoch",
		"key_sha256", "keyring_sha256", "keyring_canonical_bytes_base64",
		"files",
	) || value["schema"] != aiPanoramaContextArchiveSchema ||
		value["version"] != json.Number("1") {
		return nil, fmt.Errorf("ai-panorama-context-archive-invalid")
	}
	requestID, requestOK := exactString(value["request_id"])
	requestIDSHA256, requestDigestOK := exactString(value["request_id_sha256"])
	permitSHA256, permitOK := exactString(value["permit_sha256"])
	path, pathOK := exactString(value["path"])
	stagePath, stageOK := exactString(value["stage_path"])
	keyID, keyIDOK := exactString(value["key_id"])
	keyEpoch, epochOK := exactInt(value["key_epoch"], 1, 1<<62)
	keySHA256, keySHAOK := exactString(value["key_sha256"])
	keyringSHA256, keyringSHAOK := exactString(value["keyring_sha256"])
	keyringEncoded, encodedOK := exactString(value["keyring_canonical_bytes_base64"])
	keyringRaw, decodeErr := base64.RawStdEncoding.Strict().DecodeString(keyringEncoded)
	expectedPath, expectedStage, pathErr := aiPanoramaContextArchivePaths(permitSHA256)
	rawFiles, filesOK := value["files"].([]any)
	rawContexts, contextsOK := base["ai_panorama_context_projections"].([]any)
	if !requestOK || !aiPanoramaNoncePattern.MatchString(requestID) ||
		!requestDigestOK || requestIDSHA256 != aiPanoramaRawSHA256([]byte(requestID)) ||
		!permitOK || !aiPanoramaRawSHA256Pattern.MatchString(permitSHA256) ||
		pathErr != nil || !pathOK || path != expectedPath ||
		!stageOK || stagePath != expectedStage ||
		!keyIDOK || !aiPanoramaSafeIDPattern.MatchString(keyID) || !epochOK ||
		!keySHAOK || !aiPanoramaRawSHA256Pattern.MatchString(keySHA256) ||
		!keyringSHAOK || !aiPanoramaRawSHA256Pattern.MatchString(keyringSHA256) ||
		!encodedOK || decodeErr != nil || len(keyringRaw) < 2 ||
		len(keyringRaw) > aiPanoramaMaximumKeyring ||
		aiPanoramaRawSHA256(keyringRaw) != keyringSHA256 ||
		!filesOK || len(rawFiles) != 4 ||
		!contextsOK || len(rawContexts) != 3 {
		zero(keyringRaw)
		return nil, fmt.Errorf("ai-panorama-context-archive-invalid")
	}
	archive := &aiPanoramaContextArchive{
		RequestID: requestID, RequestIDSHA256: requestIDSHA256,
		PermitSHA256: permitSHA256, Path: path, StagePath: stagePath,
		KeyID: keyID, KeyEpoch: keyEpoch, KeySHA256: keySHA256,
		KeyringSHA256: keyringSHA256,
		Files:         make([]aiPanoramaContextArchiveFile, 0, 4),
	}
	contextsByKind := make(map[string][]byte, len(rawContexts))
	for _, rawContext := range rawContexts {
		projection, projectionErr := parseAiPanoramaProjection(rawContext)
		if projectionErr != nil || contextsByKind[projection.Kind] != nil {
			if projection != nil {
				projection.release()
			}
			archive.release()
			zero(keyringRaw)
			return nil, fmt.Errorf("ai-panorama-context-archive-file-invalid")
		}
		contextsByKind[projection.Kind] = append([]byte(nil), projection.Raw...)
		projection.release()
	}
	defer func() {
		for _, raw := range contextsByKind {
			zero(raw)
		}
	}()
	expectedKinds := []string{"keyring", "trust-assertion", "compose-plan", "volume-profile"}
	for index, rawFile := range rawFiles {
		fileValue, fileOK := rawFile.(map[string]any)
		if !fileOK || !hasKeys(
			fileValue, "kind", "path", "mount_target", "mode",
			"size_bytes", "sha256",
		) {
			archive.release()
			zero(keyringRaw)
			return nil, fmt.Errorf("ai-panorama-context-archive-file-invalid")
		}
		kind, kindOK := exactString(fileValue["kind"])
		filePath, filePathOK := exactString(fileValue["path"])
		mountTarget, targetOK := exactString(fileValue["mount_target"])
		_, modeOK := exactInt(fileValue["mode"], 0o400, 0o400)
		size, sizeOK := exactInt(fileValue["size_bytes"], 2, aiPanoramaMaximumContextFile)
		sha256Value, shaOK := exactString(fileValue["sha256"])
		name, expectedTarget, contractOK :=
			aiPanoramaContextArchiveFileContract(kind)
		var raw []byte
		if kind == "keyring" {
			raw = keyringRaw
			keyringRaw = nil
			if parsed, ok := exactInt(
				fileValue["size_bytes"], 2, aiPanoramaMaximumKeyring,
			); ok {
				size, sizeOK = parsed, true
			}
		} else {
			raw = append([]byte(nil), contextsByKind[kind]...)
		}
		if !kindOK || kind != expectedKinds[index] || !contractOK ||
			!filePathOK || filePath != path+"/"+name ||
			!targetOK || mountTarget != expectedTarget || !modeOK ||
			!sizeOK || size != int64(len(raw)) ||
			!shaOK || !aiPanoramaRawSHA256Pattern.MatchString(sha256Value) ||
			aiPanoramaRawSHA256(raw) != sha256Value {
			zero(raw)
			archive.release()
			zero(keyringRaw)
			return nil, fmt.Errorf("ai-panorama-context-archive-file-invalid")
		}
		archive.Files = append(archive.Files, aiPanoramaContextArchiveFile{
			Kind: kind, Path: filePath, MountTarget: mountTarget,
			SHA256: sha256Value, Raw: raw,
		})
	}
	permitEvidence, evidenceOK := base["ai_panorama_permit"].(map[string]any)
	evidenceRequest, evidenceRequestOK := exactString(permitEvidence["request_id"])
	evidencePermit, evidencePermitOK := exactString(permitEvidence["sha256"])
	evidenceKeyID, evidenceKeyIDOK := exactString(permitEvidence["key_id"])
	evidenceEpoch, evidenceEpochOK := exactInt(permitEvidence["key_epoch"], keyEpoch, keyEpoch)
	evidenceKeySHA, evidenceKeySHAOK := exactString(permitEvidence["key_sha256"])
	evidenceKeyringSHA, evidenceKeyringSHAOK := exactString(permitEvidence["keyring_sha256"])
	if !evidenceOK || !evidenceRequestOK || evidenceRequest != requestID ||
		!evidencePermitOK || evidencePermit != permitSHA256 ||
		!evidenceKeyIDOK || evidenceKeyID != keyID ||
		!evidenceEpochOK || evidenceEpoch != keyEpoch ||
		!evidenceKeySHAOK || evidenceKeySHA != keySHA256 ||
		!evidenceKeyringSHAOK || evidenceKeyringSHA != keyringSHA256 {
		archive.release()
		return nil, fmt.Errorf("ai-panorama-context-archive-permit-binding-invalid")
	}
	return archive, nil
}

func aiPanoramaContextArchiveDirectory(
	path string,
	mode os.FileMode,
	uid uint32,
	gid uint32,
	device uint64,
) (*os.File, os.FileInfo, uint64, error) {
	file, err := os.OpenFile(
		path, os.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0,
	)
	if err != nil {
		return nil, nil, 0, fmt.Errorf("ai-panorama-context-archive-directory-unavailable")
	}
	info, statErr := file.Stat()
	metadata, metadataOK := infoSys(info)
	mountID, mountErr := aiPanoramaFileMountID(file)
	if statErr != nil || !metadataOK || !info.IsDir() ||
		info.Mode().Perm() != mode || metadata.Uid != uid ||
		metadata.Gid != gid || metadata.Nlink < 2 ||
		(device != 0 && uint64(metadata.Dev) != device) ||
		mountErr != nil {
		file.Close()
		return nil, nil, 0, fmt.Errorf("ai-panorama-context-archive-directory-invalid")
	}
	return file, info, mountID, nil
}

func aiPanoramaContextArchiveStageInventory(
	stagePath string,
	archive *aiPanoramaContextArchive,
	uid uint32,
	gid uint32,
	parentDevice uint64,
) error {
	entries, err := os.ReadDir(stagePath)
	if err != nil || len(entries) > len(archive.Files) {
		return fmt.Errorf("ai-panorama-context-archive-stage-invalid")
	}
	expected := make(map[string]*aiPanoramaContextArchiveFile, len(archive.Files))
	for index := range archive.Files {
		expected[filepath.Base(archive.Files[index].Path)] = &archive.Files[index]
	}
	for _, entry := range entries {
		file := expected[entry.Name()]
		if file == nil {
			return fmt.Errorf("ai-panorama-context-archive-stage-extra-entry")
		}
		path := filepath.Join(stagePath, entry.Name())
		raw, readErr := readSecureFile(
			path, 0o400, uid, gid, aiPanoramaMaximumKeyring,
		)
		info, statErr := os.Lstat(path)
		metadata, metadataOK := infoSys(info)
		valid := readErr == nil && statErr == nil && metadataOK &&
			info.Mode().IsRegular() && info.Mode().Perm() == 0o400 &&
			metadata.Uid == uid && metadata.Gid == gid && metadata.Nlink == 1 &&
			uint64(metadata.Dev) == parentDevice &&
			bytes.Equal(raw, file.Raw) && aiPanoramaRawSHA256(raw) == file.SHA256
		zero(raw)
		if !valid {
			return fmt.Errorf("ai-panorama-context-archive-stage-entry-invalid")
		}
	}
	return nil
}

func ensureAiPanoramaContextArchive(
	root string,
	archive *aiPanoramaContextArchive,
) (map[string]any, error) {
	if root == "" {
		root = "/"
	}
	if archive == nil || len(archive.Files) != 4 {
		return nil, fmt.Errorf("ai-panorama-context-archive-ensure-input-invalid")
	}
	uid, gid := secureOwner(root)
	parentPath := rooted(root, aiPanoramaContextArchiveRoot)
	parent, parentInfo, parentMountID, err := aiPanoramaContextArchiveDirectory(
		parentPath, 0o700, uid, gid, 0,
	)
	if err != nil {
		return nil, err
	}
	defer parent.Close()
	parentMetadata, _ := infoSys(parentInfo)
	finalPath := rooted(root, archive.Path)
	stagePath := rooted(root, archive.StagePath)
	if _, err := os.Lstat(finalPath); err == nil {
		if _, stageErr := os.Lstat(stagePath); !os.IsNotExist(stageErr) {
			return nil, fmt.Errorf("ai-panorama-context-archive-residue-ambiguous")
		}
		return observeAiPanoramaContextArchive(root, archive)
	} else if !os.IsNotExist(err) {
		return nil, fmt.Errorf("ai-panorama-context-archive-target-ambiguous")
	}
	stageMode := os.FileMode(0o700)
	if stageInfo, stageErr := os.Lstat(stagePath); os.IsNotExist(stageErr) {
		if err := os.Mkdir(stagePath, stageMode); err != nil ||
			parent.Sync() != nil {
			return nil, fmt.Errorf("ai-panorama-context-archive-stage-create-failed")
		}
	} else if stageErr != nil {
		return nil, fmt.Errorf("ai-panorama-context-archive-stage-ambiguous")
	} else if stageInfo.Mode().Perm() == 0o500 {
		if err := aiPanoramaContextArchiveStageInventory(
			stagePath, archive, uid, gid, uint64(parentMetadata.Dev),
		); err != nil || os.Chmod(stagePath, stageMode) != nil {
			return nil, fmt.Errorf("ai-panorama-context-archive-stage-unseal-failed")
		}
	}
	stage, _, stageMountID, err := aiPanoramaContextArchiveDirectory(
		stagePath, stageMode, uid, gid, uint64(parentMetadata.Dev),
	)
	if err != nil {
		return nil, err
	}
	if stageMountID != parentMountID {
		stage.Close()
		return nil, fmt.Errorf("ai-panorama-context-archive-stage-mount-invalid")
	}
	if err := aiPanoramaContextArchiveStageInventory(
		stagePath, archive, uid, gid, uint64(parentMetadata.Dev),
	); err != nil {
		stage.Close()
		return nil, err
	}
	for index := range archive.Files {
		file := &archive.Files[index]
		stageFile := &aiPanoramaProjection{
			Kind: file.Kind,
			Path: archive.StagePath + "/" + filepath.Base(file.Path),
			Mode: 0o400, SHA256: file.SHA256, Raw: file.Raw,
		}
		if err := persistAiPanoramaProjectionFile(root, stageFile); err != nil {
			stage.Close()
			return nil, err
		}
	}
	if err := stage.Sync(); err != nil || stage.Chmod(0o500) != nil ||
		stage.Sync() != nil {
		stage.Close()
		return nil, fmt.Errorf("ai-panorama-context-archive-stage-seal-failed")
	}
	if err := stage.Close(); err != nil {
		return nil, fmt.Errorf("ai-panorama-context-archive-stage-seal-failed")
	}
	if err := renameAtNoReplace(
		int(parent.Fd()), filepath.Base(archive.StagePath), filepath.Base(archive.Path),
	); err != nil {
		return nil, fmt.Errorf("ai-panorama-context-archive-publish-failed")
	}
	if err := parent.Sync(); err != nil {
		return nil, fmt.Errorf("ai-panorama-context-archive-durability-unknown")
	}
	return observeAiPanoramaContextArchive(root, archive)
}

func aiPanoramaContextArchiveFileObservation(
	path string,
	expected *aiPanoramaContextArchiveFile,
	uid uint32,
	gid uint32,
	device uint64,
	mountID uint64,
) (map[string]any, error) {
	file, err := os.OpenFile(
		path, os.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0,
	)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-context-archive-file-unavailable")
	}
	defer file.Close()
	before, statErr := file.Stat()
	metadata, metadataOK := infoSys(before)
	fileMountID, mountErr := aiPanoramaFileMountID(file)
	if statErr != nil || !metadataOK || !before.Mode().IsRegular() ||
		before.Mode().Perm() != 0o400 || metadata.Uid != uid ||
		metadata.Gid != gid || metadata.Nlink != 1 ||
		uint64(metadata.Dev) != device || mountErr != nil ||
		fileMountID != mountID || before.Size() != int64(len(expected.Raw)) {
		return nil, fmt.Errorf("ai-panorama-context-archive-file-invalid")
	}
	raw := make([]byte, before.Size())
	if _, err := io.ReadFull(file, raw); err != nil {
		zero(raw)
		return nil, fmt.Errorf("ai-panorama-context-archive-file-read-failed")
	}
	extra := []byte{0}
	count, readErr := file.Read(extra)
	zero(extra)
	after, afterErr := file.Stat()
	pathInfo, pathErr := os.Lstat(path)
	valid := count == 0 && (readErr == nil || readErr == io.EOF) &&
		afterErr == nil && tourV4SameFingerprint(before, after) &&
		pathErr == nil && os.SameFile(before, pathInfo) &&
		bytes.Equal(raw, expected.Raw) &&
		aiPanoramaRawSHA256(raw) == expected.SHA256
	zero(raw)
	if !valid {
		return nil, fmt.Errorf("ai-panorama-context-archive-file-changed")
	}
	mtime, ctime := tourV4StatTimes(metadata)
	return map[string]any{
		"kind": expected.Kind, "path": expected.Path,
		"mount_target": expected.MountTarget,
		"device":       json.Number(strconv.FormatUint(uint64(metadata.Dev), 10)),
		"inode":        json.Number(strconv.FormatUint(metadata.Ino, 10)),
		"mount_id":     json.Number(strconv.FormatUint(fileMountID, 10)),
		"mode":         json.Number(strconv.FormatUint(uint64(before.Mode().Perm()), 10)),
		"uid":          json.Number(strconv.FormatUint(uint64(metadata.Uid), 10)),
		"gid":          json.Number(strconv.FormatUint(uint64(metadata.Gid), 10)),
		"nlink":        json.Number(strconv.FormatUint(uint64(metadata.Nlink), 10)),
		"size_bytes":   json.Number(strconv.FormatInt(before.Size(), 10)),
		"sha256":       expected.SHA256,
		"mtime_ns":     json.Number(strconv.FormatInt(mtime, 10)),
		"ctime_ns":     json.Number(strconv.FormatInt(ctime, 10)),
	}, nil
}

func observeAiPanoramaContextArchive(
	root string,
	archive *aiPanoramaContextArchive,
) (map[string]any, error) {
	if root == "" {
		root = "/"
	}
	if archive == nil || len(archive.Files) != 4 {
		return nil, fmt.Errorf("ai-panorama-context-archive-observe-input-invalid")
	}
	uid, gid := secureOwner(root)
	parentPath := rooted(root, aiPanoramaContextArchiveRoot)
	parent, parentInfo, parentMountID, err := aiPanoramaContextArchiveDirectory(
		parentPath, 0o700, uid, gid, 0,
	)
	if err != nil {
		return nil, err
	}
	defer parent.Close()
	parentMetadata, _ := infoSys(parentInfo)
	finalPath := rooted(root, archive.Path)
	directory, directoryInfo, directoryMountID, err :=
		aiPanoramaContextArchiveDirectory(
			finalPath, 0o500, uid, gid, uint64(parentMetadata.Dev),
		)
	if err != nil {
		return nil, err
	}
	defer directory.Close()
	if directoryMountID != parentMountID {
		return nil, fmt.Errorf("ai-panorama-context-archive-mount-invalid")
	}
	entries, err := aiPanoramaDirectoryNames(directory)
	if err != nil || len(entries) != len(archive.Files) {
		return nil, fmt.Errorf("ai-panorama-context-archive-inventory-invalid")
	}
	files := make([]any, 0, len(archive.Files))
	for index := range archive.Files {
		expectedName := filepath.Base(archive.Files[index].Path)
		if entries[index] != expectedName {
			return nil, fmt.Errorf("ai-panorama-context-archive-inventory-invalid")
		}
		observation, err := aiPanoramaContextArchiveFileObservation(
			filepath.Join(finalPath, expectedName), &archive.Files[index],
			uid, gid, uint64(parentMetadata.Dev), parentMountID,
		)
		if err != nil {
			return nil, err
		}
		files = append(files, observation)
	}
	directoryMetadata, _ := infoSys(directoryInfo)
	directoryMtime, directoryCtime := tourV4StatTimes(directoryMetadata)
	value := map[string]any{
		"schema":            aiPanoramaContextArchiveObservation,
		"version":           json.Number("1"),
		"request_id_sha256": archive.RequestIDSHA256,
		"permit_sha256":     archive.PermitSHA256,
		"path":              archive.Path,
		"root_device": json.Number(
			strconv.FormatUint(uint64(parentMetadata.Dev), 10),
		),
		"root_inode": json.Number(strconv.FormatUint(parentMetadata.Ino, 10)),
		"root_mount_id": json.Number(
			strconv.FormatUint(parentMountID, 10),
		),
		"directory_device": json.Number(
			strconv.FormatUint(uint64(directoryMetadata.Dev), 10),
		),
		"directory_inode": json.Number(
			strconv.FormatUint(directoryMetadata.Ino, 10),
		),
		"directory_mount_id": json.Number(
			strconv.FormatUint(directoryMountID, 10),
		),
		"directory_mode": json.Number(
			strconv.FormatUint(uint64(directoryInfo.Mode().Perm()), 10),
		),
		"directory_uid": json.Number(
			strconv.FormatUint(uint64(directoryMetadata.Uid), 10),
		),
		"directory_gid": json.Number(
			strconv.FormatUint(uint64(directoryMetadata.Gid), 10),
		),
		"directory_nlink": json.Number(
			strconv.FormatUint(uint64(directoryMetadata.Nlink), 10),
		),
		"directory_mtime_ns": json.Number(
			strconv.FormatInt(directoryMtime, 10),
		),
		"directory_ctime_ns": json.Number(
			strconv.FormatInt(directoryCtime, 10),
		),
		"files": files,
	}
	raw, err := canonicalJSON(value)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-context-archive-observation-invalid")
	}
	value["observation_sha256"] = aiPanoramaRawSHA256(raw)
	zero(raw)
	return value, nil
}

func aiPanoramaContextArchiveCompletion(
	root string,
	journal *Journal,
	base map[string]any,
	archive *aiPanoramaContextArchive,
	intentReceiptDigest string,
) error {
	if journal == nil || base == nil || archive == nil ||
		!digestPattern.MatchString(intentReceiptDigest) {
		return fmt.Errorf("ai-panorama-context-archive-completion-input-invalid")
	}
	observation, err := observeAiPanoramaContextArchive(root, archive)
	if err != nil {
		return err
	}
	fields := cloneFields(base)
	fields["ai_panorama_context_archive_intent_receipt_digest"] =
		intentReceiptDigest
	fields["ai_panorama_context_archive_observation"] = observation
	fields["disposition"] = "historical-context-archive-completed"
	return appendAiPanoramaJournalEvent(
		journal, aiPanoramaContextArchiveCompletedEvent, fields,
	)
}

func validateAiPanoramaContextArchiveCompletion(
	root string,
	base map[string]any,
	archive *aiPanoramaContextArchive,
	intentReceiptDigest string,
) error {
	recordedIntent, intentOK := exactString(
		base["ai_panorama_context_archive_intent_receipt_digest"],
	)
	recorded, recordedOK :=
		base["ai_panorama_context_archive_observation"].(map[string]any)
	observed, err := observeAiPanoramaContextArchive(root, archive)
	if !intentOK || recordedIntent != intentReceiptDigest ||
		!recordedOK || err != nil || !canonicalValuesEqual(recorded, observed) {
		return fmt.Errorf("ai-panorama-context-archive-completion-invalid")
	}
	return nil
}

func validateAiPanoramaContextArchiveInventory(
	root string,
	journal *Journal,
) error {
	if journal == nil {
		return fmt.Errorf("ai-panorama-context-archive-inventory-input-invalid")
	}
	type authorization struct {
		archive       *aiPanoramaContextArchive
		intentReceipt string
		completed     bool
	}
	authorized := make(map[string]*authorization)
	defer func() {
		for _, item := range authorized {
			item.archive.release()
		}
	}()
	for index := range journal.events {
		event := &journal.events[index]
		switch event.EventType {
		case aiPanoramaPermitPersistenceIntentEvent:
			archive, err := parseAiPanoramaContextArchive(event.Payload)
			if err != nil || authorized[archive.Path] != nil {
				if archive != nil {
					archive.release()
				}
				return fmt.Errorf("ai-panorama-context-archive-journal-invalid")
			}
			authorized[archive.Path] = &authorization{
				archive: archive, intentReceipt: event.ReceiptDigest,
			}
		case aiPanoramaContextArchiveCompletedEvent:
			archive, err := parseAiPanoramaContextArchive(event.Payload)
			if err != nil {
				if archive != nil {
					archive.release()
				}
				return fmt.Errorf("ai-panorama-context-archive-completion-invalid")
			}
			item := authorized[archive.Path]
			if item == nil || item.completed ||
				!canonicalValuesEqual(
					event.Payload["ai_panorama_context_archive"],
					item.archive.journalValue(),
				) ||
				validateAiPanoramaContextArchiveCompletion(
					root, event.Payload, item.archive, item.intentReceipt,
				) != nil {
				if archive != nil {
					archive.release()
				}
				return fmt.Errorf("ai-panorama-context-archive-completion-invalid")
			}
			archive.release()
			item.completed = true
		}
	}
	rootPath := rooted(root, aiPanoramaContextArchiveRoot)
	entries, err := os.ReadDir(rootPath)
	if os.IsNotExist(err) && len(authorized) == 0 {
		return nil
	}
	if err != nil || len(entries) != len(authorized) {
		return fmt.Errorf("ai-panorama-context-archive-inventory-invalid")
	}
	seen := make(map[string]bool, len(entries))
	for _, entry := range entries {
		path := aiPanoramaContextArchiveRoot + "/" + entry.Name()
		item := authorized[path]
		if item == nil || item.completed == false || seen[path] ||
			stringsHasPrefixDotStage(entry.Name()) ||
			func() bool {
				_, observeErr := observeAiPanoramaContextArchive(root, item.archive)
				return observeErr != nil
			}() {
			return fmt.Errorf("ai-panorama-context-archive-inventory-invalid")
		}
		seen[path] = true
	}
	return nil
}

func stringsHasPrefixDotStage(value string) bool {
	return len(value) >= len(".stage-") && value[:len(".stage-")] == ".stage-"
}
