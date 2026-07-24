//go:build linux && amd64

package authority

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"syscall"
)

const (
	aiPanoramaDatabaseSecretSchema  = "propertyquarry.prater-ai-panorama-db-secrets.v1"
	aiPanoramaMaximumDatabaseSecret = 16 * 1024
)

var aiPanoramaDatabaseSecretPostRenameHook func() error

func aiPanoramaDatabaseEnvironmentValues(
	root string,
	expectedDigest string,
	databaseURLName string,
) ([]byte, []byte, error) {
	if databaseURLName != "PROPERTYQUARRY_SCHEDULER_DATABASE_URL" &&
		databaseURLName != "PROPERTYQUARRY_API_DATABASE_URL" {
		return nil, nil, fmt.Errorf("ai-panorama-database-role-unconfigured")
	}
	if err := validateDatabaseRuntimeEnvironment(root, expectedDigest); err != nil {
		return nil, nil, fmt.Errorf("ai-panorama-database-environment-invalid")
	}
	raw, err := readSecureFile(
		rooted(root, DatabaseRuntimeEnvironmentPath), 0o600,
		databaseRuntimeEnvironmentUID, databaseRuntimeEnvironmentGID, 32*1024,
	)
	if err != nil {
		return nil, nil, fmt.Errorf("ai-panorama-database-environment-unavailable")
	}
	defer zero(raw)
	values := make(map[string][]byte, len(databaseRuntimeEnvironmentNames))
	lines := bytes.Split(raw[:len(raw)-1], []byte{'\n'})
	if len(lines) != len(databaseRuntimeEnvironmentNames) {
		return nil, nil, fmt.Errorf("ai-panorama-database-environment-invalid")
	}
	for index, line := range lines {
		parts := bytes.SplitN(line, []byte{'='}, 2)
		if len(parts) != 2 || string(parts[0]) != databaseRuntimeEnvironmentNames[index] {
			return nil, nil, fmt.Errorf("ai-panorama-database-environment-invalid")
		}
		values[string(parts[0])] = parts[1]
	}
	databaseURL := append([]byte(nil), values[databaseURLName]...)
	erasureSecret := append([]byte(nil), values["PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"]...)
	if !validAiPanoramaDatabaseURL(databaseURL) ||
		!validAiPanoramaErasureSecret(erasureSecret) {
		zero(databaseURL)
		zero(erasureSecret)
		return nil, nil, fmt.Errorf("ai-panorama-database-secret-invalid")
	}
	return databaseURL, erasureSecret, nil
}

func validAiPanoramaDatabaseURL(raw []byte) bool {
	if !aiPanoramaPrintableNonspaceASCII(raw, 1, 4096) {
		return false
	}
	value, err := url.Parse(string(raw))
	return err == nil && value.Scheme == "postgresql" &&
		value.Hostname() == aiPanoramaDatabaseService && value.Port() == "5432" &&
		value.User != nil && value.User.Username() != "" &&
		strings.Trim(value.EscapedPath(), "/") != "" &&
		value.Fragment == ""
}

func validAiPanoramaErasureSecret(raw []byte) bool {
	return aiPanoramaPrintableNonspaceASCII(raw, 32, 4096)
}

func aiPanoramaPrintableNonspaceASCII(raw []byte, minimum, maximum int) bool {
	if len(raw) < minimum || len(raw) > maximum {
		return false
	}
	for _, value := range raw {
		if value < 0x21 || value > 0x7e {
			return false
		}
	}
	return true
}

func createAiPanoramaDatabaseSecret(
	root string,
	expectedEnvironmentDigest string,
	databaseURLName string,
) (string, error) {
	databaseURL, erasureSecret, err := aiPanoramaDatabaseEnvironmentValues(
		root, expectedEnvironmentDigest, databaseURLName,
	)
	if err != nil {
		return "", err
	}
	defer zero(databaseURL)
	defer zero(erasureSecret)
	wire, err := canonicalJSON(map[string]any{
		"schema": aiPanoramaDatabaseSecretSchema, "version": json.Number("1"),
		"DATABASE_URL": string(databaseURL),
		"PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET": string(erasureSecret),
	})
	if err != nil || len(wire)+1 > aiPanoramaMaximumDatabaseSecret {
		zero(wire)
		return "", fmt.Errorf("ai-panorama-database-secret-canonicalization-failed")
	}
	wire = append(wire, '\n')
	defer zero(wire)
	return persistAiPanoramaDatabaseSecret(root, wire)
}

func persistAiPanoramaDatabaseSecret(
	root string,
	wire []byte,
) (result string, resultErr error) {
	if len(wire) < 3 || len(wire) > aiPanoramaMaximumDatabaseSecret ||
		wire[len(wire)-1] != '\n' {
		return "", fmt.Errorf("ai-panorama-database-secret-input-invalid")
	}
	ownerUID, ownerGID := secureOwner(root)
	runtimeRoot := rooted(root, aiPanoramaRuntimeRoot)
	info, err := os.Lstat(runtimeRoot)
	metadata, ok := infoSys(info)
	if err != nil || !ok || !info.IsDir() || info.Mode().Perm() != 0o700 ||
		info.Mode()&os.ModeSymlink != 0 || metadata.Uid != ownerUID ||
		metadata.Gid != ownerGID || metadata.Nlink < 2 {
		return "", fmt.Errorf("ai-panorama-runtime-root-invalid")
	}
	target := rooted(root, aiPanoramaDatabaseSecretMount)
	temporaryName := ".db-secret-" + aiPanoramaRawSHA256(wire) + ".tmp"
	temporary := rooted(root, aiPanoramaRuntimeRoot+"/"+temporaryName)
	if _, err := os.Lstat(target); err == nil {
		persisted, readErr := readSecureFile(
			target, 0o400, ownerUID, ownerGID,
			aiPanoramaMaximumDatabaseSecret,
		)
		valid := readErr == nil && bytes.Equal(persisted, wire)
		zero(persisted)
		if !valid {
			return "", fmt.Errorf("ai-panorama-database-secret-conflict")
		}
		if err := destroyAiPanoramaDatabaseSecretIntentTemporary(
			root, temporary, wire,
		); err != nil {
			return "", err
		}
		if err := fsyncAiPanoramaDirectory(runtimeRoot); err != nil {
			return "", fmt.Errorf("ai-panorama-database-secret-durability-unknown")
		}
		return aiPanoramaDatabaseSecretMount, nil
	} else if !os.IsNotExist(err) {
		return "", fmt.Errorf("ai-panorama-database-secret-conflict")
	}
	defer func() {
		if resultErr == nil {
			return
		}
		if cleanupErr := destroyAiPanoramaDatabaseSecretIntent(
			root, temporary, target, wire,
		); cleanupErr != nil {
			result = ""
			resultErr = fmt.Errorf(
				"ai-panorama-database-secret-failure-cleanup-ambiguous",
			)
		}
	}()
	if _, err := os.Lstat(temporary); err == nil {
		if err := destroyAiPanoramaDatabaseSecretLeaf(
			root, temporary, true, wire,
		); err != nil {
			return "", err
		}
	} else if !os.IsNotExist(err) {
		return "", fmt.Errorf("ai-panorama-database-secret-temporary-invalid")
	}
	file, err := os.OpenFile(
		temporary,
		os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_CLOEXEC|syscall.O_NOFOLLOW,
		0o400,
	)
	if err != nil {
		return "", fmt.Errorf("ai-panorama-database-secret-create-failed")
	}
	if file.Chmod(0o400) != nil || writeAll(file, wire) != nil ||
		file.Sync() != nil {
		_ = file.Close()
		return "", fmt.Errorf("ai-panorama-database-secret-write-failed")
	}
	writtenInfo, statErr := file.Stat()
	writtenMetadata, metadataOK := infoSys(writtenInfo)
	pathInfo, pathErr := os.Lstat(temporary)
	if statErr != nil || !metadataOK || pathErr != nil ||
		!writtenInfo.Mode().IsRegular() || writtenInfo.Mode().Perm() != 0o400 ||
		writtenInfo.Size() != int64(len(wire)) ||
		writtenMetadata.Uid != ownerUID || writtenMetadata.Gid != ownerGID ||
		writtenMetadata.Nlink != 1 ||
		uint64(writtenMetadata.Dev) != uint64(metadata.Dev) ||
		!os.SameFile(writtenInfo, pathInfo) || file.Close() != nil {
		_ = file.Close()
		return "", fmt.Errorf("ai-panorama-database-secret-write-failed")
	}
	if err := fsyncAiPanoramaDirectory(runtimeRoot); err != nil {
		return "", fmt.Errorf("ai-panorama-database-secret-durability-unknown")
	}
	directory, err := os.OpenFile(
		runtimeRoot,
		os.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW,
		0,
	)
	if err != nil {
		return "", fmt.Errorf("ai-panorama-runtime-root-invalid")
	}
	if err := renameAtNoReplace(
		int(directory.Fd()), temporaryName,
		filepath.Base(aiPanoramaDatabaseSecretMount),
	); err != nil {
		_ = directory.Close()
		return "", fmt.Errorf("ai-panorama-database-secret-publish-failed")
	}
	if aiPanoramaDatabaseSecretPostRenameHook != nil &&
		aiPanoramaDatabaseSecretPostRenameHook() != nil {
		_ = directory.Close()
		return "", fmt.Errorf("ai-panorama-database-secret-publish-failed")
	}
	if directory.Sync() != nil || directory.Close() != nil {
		_ = directory.Close()
		return "", fmt.Errorf("ai-panorama-database-secret-publish-failed")
	}
	persisted, readErr := readSecureFile(
		target, 0o400, ownerUID, ownerGID, aiPanoramaMaximumDatabaseSecret,
	)
	valid := readErr == nil && bytes.Equal(persisted, wire)
	zero(persisted)
	if !valid {
		return "", fmt.Errorf("ai-panorama-database-secret-publish-invalid")
	}
	return aiPanoramaDatabaseSecretMount, nil
}

func destroyAiPanoramaDatabaseSecretIntent(
	root string,
	temporary string,
	target string,
	expected []byte,
) error {
	if err := destroyAiPanoramaDatabaseSecretIntentTemporary(
		root, temporary, expected,
	); err != nil {
		return err
	}
	if _, err := os.Lstat(target); os.IsNotExist(err) {
		return nil
	} else if err != nil {
		return fmt.Errorf("ai-panorama-database-secret-destroy-open-failed")
	}
	return destroyAiPanoramaDatabaseSecretLeaf(
		root, target, false, expected,
	)
}

func destroyAiPanoramaDatabaseSecretIntentTemporary(
	root string,
	temporary string,
	expected []byte,
) error {
	if _, err := os.Lstat(temporary); os.IsNotExist(err) {
		return nil
	} else if err != nil {
		return fmt.Errorf("ai-panorama-database-secret-temporary-invalid")
	}
	return destroyAiPanoramaDatabaseSecretLeaf(
		root, temporary, true, expected,
	)
}

func destroyAiPanoramaDatabaseSecret(root string) error {
	runtimeRoot := rooted(root, aiPanoramaRuntimeRoot)
	entries, err := os.ReadDir(runtimeRoot)
	if err != nil {
		return fmt.Errorf("ai-panorama-database-secret-destroy-root-unavailable")
	}
	for _, entry := range entries {
		name := entry.Name()
		if !strings.HasPrefix(name, ".db-secret-") {
			continue
		}
		if len(name) != len(".db-secret-")+64+len(".tmp") ||
			!aiPanoramaRawSHA256Pattern.MatchString(
				name[len(".db-secret-"):len(name)-len(".tmp")],
			) || !strings.HasSuffix(name, ".tmp") {
			return fmt.Errorf("ai-panorama-database-secret-temporary-invalid")
		}
		if err := destroyAiPanoramaDatabaseSecretLeaf(
			root, filepath.Join(runtimeRoot, name), true, nil,
		); err != nil {
			return err
		}
	}
	target := rooted(root, aiPanoramaDatabaseSecretMount)
	if _, err := os.Lstat(target); os.IsNotExist(err) {
		return nil
	} else if err != nil {
		return fmt.Errorf("ai-panorama-database-secret-destroy-open-failed")
	}
	return destroyAiPanoramaDatabaseSecretLeaf(root, target, false, nil)
}

func destroyAiPanoramaDatabaseSecretLeaf(
	root string,
	target string,
	allowEmpty bool,
	expected []byte,
) error {
	ownerUID, ownerGID := secureOwner(root)
	pathBefore, pathErr := os.Lstat(target)
	pathMetadata, pathOK := infoSys(pathBefore)
	runtimeInfo, runtimeErr := os.Lstat(rooted(root, aiPanoramaRuntimeRoot))
	runtimeMetadata, runtimeOK := infoSys(runtimeInfo)
	modeValid := pathBefore != nil &&
		(pathBefore.Mode().Perm() == 0o400 ||
			pathBefore.Mode().Perm() == 0o600)
	if allowEmpty && pathBefore != nil {
		modeValid = pathBefore.Mode().Perm()&^os.FileMode(0o400) == 0 ||
			pathBefore.Mode().Perm() == 0o600
	}
	if pathErr != nil || !pathOK || !pathBefore.Mode().IsRegular() ||
		!modeValid || pathMetadata.Uid != ownerUID ||
		pathMetadata.Gid != ownerGID || pathMetadata.Nlink != 1 ||
		pathBefore.Size() < 0 ||
		pathBefore.Size() > aiPanoramaMaximumDatabaseSecret ||
		runtimeErr != nil || !runtimeOK || !runtimeInfo.IsDir() ||
		uint64(pathMetadata.Dev) != uint64(runtimeMetadata.Dev) {
		return fmt.Errorf("ai-panorama-database-secret-destroy-binding-invalid")
	}
	if pathBefore.Mode().Perm() != 0o400 {
		if err := os.Chmod(target, 0o400); err != nil {
			return fmt.Errorf("ai-panorama-database-secret-destroy-binding-invalid")
		}
		afterChmod, err := os.Lstat(target)
		if err != nil || !os.SameFile(pathBefore, afterChmod) ||
			afterChmod.Mode().Perm() != 0o400 {
			return fmt.Errorf("ai-panorama-database-secret-destroy-binding-invalid")
		}
	}
	reader, err := os.OpenFile(
		target, os.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0,
	)
	if err != nil {
		return fmt.Errorf("ai-panorama-database-secret-destroy-open-failed")
	}
	info, err := reader.Stat()
	metadata, ok := infoSys(info)
	minimumSize := int64(1)
	if allowEmpty {
		minimumSize = 0
	}
	if err != nil || !ok || !info.Mode().IsRegular() ||
		info.Mode().Perm() != 0o400 || !os.SameFile(pathBefore, info) ||
		metadata.Uid != ownerUID || metadata.Gid != ownerGID || metadata.Nlink != 1 ||
		info.Size() < minimumSize || info.Size() > aiPanoramaMaximumDatabaseSecret ||
		runtimeErr != nil || !runtimeOK || !runtimeInfo.IsDir() ||
		uint64(metadata.Dev) != uint64(runtimeMetadata.Dev) {
		reader.Close()
		return fmt.Errorf("ai-panorama-database-secret-destroy-binding-invalid")
	}
	if expected != nil {
		observed := make([]byte, info.Size())
		readCount := 0
		var readErr error
		if len(observed) > 0 {
			readCount, readErr = reader.ReadAt(observed, 0)
		}
		contentValid := readErr == nil && readCount == len(observed)
		if allowEmpty {
			contentValid = contentValid && bytes.HasPrefix(expected, observed)
		} else {
			contentValid = contentValid && bytes.Equal(expected, observed)
		}
		zero(observed)
		if !contentValid {
			reader.Close()
			return fmt.Errorf("ai-panorama-database-secret-destroy-binding-invalid")
		}
	}
	pathInfo, pathErr := os.Lstat(target)
	if pathErr != nil || !os.SameFile(info, pathInfo) || reader.Close() != nil {
		return fmt.Errorf("ai-panorama-database-secret-destroy-binding-invalid")
	}
	if err := os.Chmod(target, 0o600); err != nil {
		return fmt.Errorf("ai-panorama-database-secret-destroy-overwrite-failed")
	}
	writableInfo, writableErr := os.Lstat(target)
	if writableErr != nil || !os.SameFile(pathBefore, writableInfo) ||
		writableInfo.Mode().Perm() != 0o600 {
		return fmt.Errorf("ai-panorama-database-secret-destroy-overwrite-failed")
	}
	file, err := os.OpenFile(
		target, os.O_RDWR|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0,
	)
	if err != nil {
		return fmt.Errorf("ai-panorama-database-secret-destroy-open-failed")
	}
	writeInfo, writeStatErr := file.Stat()
	if writeStatErr != nil || !os.SameFile(pathBefore, writeInfo) ||
		writeInfo.Mode().Perm() != 0o600 ||
		writeInfo.Size() != info.Size() {
		file.Close()
		return fmt.Errorf("ai-panorama-database-secret-destroy-binding-invalid")
	}
	zeros := make([]byte, info.Size())
	var writeErr error
	if len(zeros) > 0 {
		_, writeErr = file.WriteAt(zeros, 0)
	}
	zero(zeros)
	syncErr := file.Sync()
	pathInfo, pathErr = os.Lstat(target)
	same := pathErr == nil && os.SameFile(writeInfo, pathInfo)
	closeErr := file.Close()
	if writeErr != nil || syncErr != nil || !same || closeErr != nil {
		return fmt.Errorf("ai-panorama-database-secret-destroy-overwrite-failed")
	}
	if err := os.Remove(target); err != nil {
		return fmt.Errorf("ai-panorama-database-secret-destroy-unlink-failed")
	}
	if err := fsyncAiPanoramaDirectory(rooted(root, aiPanoramaRuntimeRoot)); err != nil {
		return fmt.Errorf("ai-panorama-database-secret-destroy-durability-unknown")
	}
	return nil
}
