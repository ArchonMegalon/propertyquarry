package installhelper

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"syscall"
	"time"
)

const (
	FixedRunnerInputPath   = "/runner-input/actions-runner-linux-x64-2.335.1.tar.gz"
	FixedRunnerTargetPath  = "/usr/lib/propertyquarry-release-runner-v2/actions-runner-linux-x64-2.335.1.tar.gz"
	FixedRunnerReceiptPath = "/output/propertyquarry-release-single-host-v2-runner-install-receipt.json"
	runnerArchiveBytes     = int64(225628509)
	runnerArchiveSHA256    = "sha256:4ef2f25285f0ae4477f1fe1e346db76d2f3ebf03824e2ddd1973a2819bf6c8cf"
	runnerLockSHA256       = "sha256:3c64d67b754df566928c8d779410a28156552a670a34f0e1612bc1a43dcbab3e"
)

func InstallFixedRunnerArchive() ([]byte, error) {
	if os.Geteuid() != 0 || os.Getegid() != 0 {
		return nil, fmt.Errorf("runner-installer-root-required")
	}
	packageKey, packageKeyID, err := EmbeddedPackageAuthority()
	if err != nil {
		return nil, err
	}
	defer zero(packageKey)
	installer := &Installer{HostRoot: FixedHostRoot, OwnerUID: 0, OwnerGID: 0}
	receiptKey, receiptKeyID, installedManifestDigest, err := installer.loadInstalledReceiptAuthority(packageKey, packageKeyID)
	if err != nil {
		return nil, err
	}
	defer zero(receiptKey)
	input, inputInfo, err := openRunnerArchive(FixedRunnerInputPath, 0o400, nil, nil)
	if err != nil {
		return nil, err
	}
	defer input.Close()
	target, err := installer.hostPath(FixedRunnerTargetPath)
	if err != nil {
		return nil, err
	}
	if err := validateDirectoryChain(installer.HostRoot, filepath.Dir(target), 0); err != nil {
		return nil, fmt.Errorf("runner-target-parent-invalid")
	}
	lockPath, _ := installer.hostPath("/var/lib/propertyquarry-release-single-host-v2/install.lock")
	lock, err := os.OpenFile(lockPath, os.O_CREATE|os.O_RDWR|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return nil, fmt.Errorf("runner-install-lock-unavailable")
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX); err != nil {
		return nil, fmt.Errorf("runner-install-lock-failed")
	}
	if err := installer.validateLock(lock); err != nil {
		return nil, err
	}
	idempotent := false
	if existing, _, existingErr := openRunnerArchive(target, 0o444, uint32Pointer(0), uint32Pointer(0)); existingErr == nil {
		existing.Close()
		idempotent = true
	} else if !os.IsNotExist(unwrapPathError(existingErr)) {
		return nil, fmt.Errorf("runner-existing-archive-invalid")
	}
	if !idempotent {
		stage := filepath.Join(filepath.Dir(target), ".actions-runner-linux-x64-2.335.1.tar.gz.pqinstall")
		if err := copyRunnerArchive(input, inputInfo, stage); err != nil {
			return nil, err
		}
		if err := renameNoReplace(stage, target); err != nil {
			_ = os.Remove(stage)
			return nil, fmt.Errorf("runner-install-publish-failed")
		}
		if err := syncDirectory(filepath.Dir(target)); err != nil {
			return nil, err
		}
	}
	payload := map[string]any{
		"archive_bytes": json.Number(strconv.FormatInt(runnerArchiveBytes, 10)), "archive_sha256": runnerArchiveSHA256,
		"authority_manifest_digest": installedManifestDigest, "authority_profile": "single-host-production-v2",
		"disposition":  map[bool]string{true: "already-installed", false: "installed"}[idempotent],
		"installed_at": json.Number(strconv.FormatInt(time.Now().UTC().Unix(), 10)), "installed_path": FixedRunnerTargetPath,
		"package_authority_key_id": packageKeyID, "production_ready": false, "receipt_authority_key_id": receiptKeyID,
		"runner_archive_installed": true, "runner_registered": false,
		"schema": "propertyquarry.release-control.single-host-runner-install-receipt.v2", "version": json.Number("2"),
	}
	return signedWire(payload, receiptKey, receiptKeyID)
}

func (installer *Installer) loadInstalledReceiptAuthority(packageKey ed25519.PublicKey, packageKeyID string) (ed25519.PrivateKey, string, string, error) {
	manifestPath, _ := installer.hostPath("/etc/propertyquarry-release-single-host-v2/package-manifest.v2.json")
	signaturePath, _ := installer.hostPath("/etc/propertyquarry-release-single-host-v2/package-manifest.v2.sig")
	manifestRaw, err := readExactFile(manifestPath, 0o444, installer.OwnerUID, installer.OwnerGID, maximumManifestBytes)
	if err != nil {
		return nil, "", "", fmt.Errorf("runner-installed-manifest-unavailable")
	}
	defer zero(manifestRaw)
	signature, err := readExactFile(signaturePath, 0o444, installer.OwnerUID, installer.OwnerGID, ed25519.SignatureSize)
	if err != nil {
		return nil, "", "", fmt.Errorf("runner-installed-manifest-signature-unavailable")
	}
	defer zero(signature)
	if !ed25519.Verify(packageKey, framed(packageSignatureDomain, manifestRaw), signature) {
		return nil, "", "", fmt.Errorf("runner-installed-manifest-signature-invalid")
	}
	manifest, err := strictJSON(manifestRaw, maximumManifestBytes)
	if err != nil {
		return nil, "", "", err
	}
	manifestPackageKeyID, _ := exactString(manifest["package_authority_key_id"])
	receiptKeyID, _ := exactString(manifest["receipt_authority_key_id"])
	if manifestPackageKeyID != packageKeyID || !digestPattern.MatchString(receiptKeyID) {
		return nil, "", "", fmt.Errorf("runner-installed-manifest-binding-invalid")
	}
	fileDigests := map[string]string{}
	items, ok := manifest["files"].([]any)
	if !ok {
		return nil, "", "", fmt.Errorf("runner-installed-file-list-invalid")
	}
	for _, item := range items {
		entry, ok := item.(map[string]any)
		if !ok {
			return nil, "", "", fmt.Errorf("runner-installed-file-entry-invalid")
		}
		path, pathOK := exactString(entry["install_path"])
		expectedDigest, digestOK := exactString(entry["sha256"])
		if !pathOK || !digestOK || !digestPattern.MatchString(expectedDigest) {
			return nil, "", "", fmt.Errorf("runner-installed-file-entry-invalid")
		}
		fileDigests[path] = expectedDigest
	}
	keyPath := "/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.key"
	anchorPath := "/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.pem"
	lockPath := "/usr/lib/propertyquarry-release-runner-v2/runner.lock.json"
	keyRaw, err := readExactFile(mustHostPath(installer, keyPath), 0o400, installer.OwnerUID, installer.OwnerGID, 4096)
	if err != nil || digest(keyRaw) != fileDigests[keyPath] {
		zero(keyRaw)
		return nil, "", "", fmt.Errorf("runner-receipt-key-unavailable")
	}
	defer zero(keyRaw)
	receiptKey, err := parsePrivatePEM(keyRaw)
	if err != nil {
		return nil, "", "", fmt.Errorf("runner-receipt-key-invalid")
	}
	anchorRaw, err := readExactFile(mustHostPath(installer, anchorPath), 0o444, installer.OwnerUID, installer.OwnerGID, 4096)
	if err != nil || digest(anchorRaw) != fileDigests[anchorPath] {
		zero(anchorRaw)
		zero(receiptKey)
		return nil, "", "", fmt.Errorf("runner-receipt-anchor-unavailable")
	}
	defer zero(anchorRaw)
	anchor, der, actualReceiptKeyID, err := parsePublicPEM(anchorRaw)
	defer zero(anchor)
	defer zero(der)
	if err != nil || actualReceiptKeyID != receiptKeyID || !bytes.Equal(anchor, receiptKey.Public().(ed25519.PublicKey)) {
		zero(receiptKey)
		return nil, "", "", fmt.Errorf("runner-receipt-authority-invalid")
	}
	lockRaw, err := readExactFile(mustHostPath(installer, lockPath), 0o444, installer.OwnerUID, installer.OwnerGID, 4096)
	if err != nil || digest(lockRaw) != fileDigests[lockPath] || digest(lockRaw) != runnerLockSHA256 {
		zero(lockRaw)
		zero(receiptKey)
		return nil, "", "", fmt.Errorf("runner-lock-invalid")
	}
	zero(lockRaw)
	return receiptKey, receiptKeyID, digest(manifestRaw), nil
}

func openRunnerArchive(path string, expectedMode os.FileMode, expectedUID, expectedGID *uint32) (*os.File, os.FileInfo, error) {
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, nil, err
	}
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm() != expectedMode || info.Size() != runnerArchiveBytes {
		file.Close()
		return nil, nil, fmt.Errorf("runner-archive-metadata-invalid")
	}
	metadata, ok := info.Sys().(*syscall.Stat_t)
	if !ok || metadata.Nlink != 1 || (expectedUID != nil && metadata.Uid != *expectedUID) || (expectedGID != nil && metadata.Gid != *expectedGID) {
		file.Close()
		return nil, nil, fmt.Errorf("runner-archive-owner-invalid")
	}
	hasher := sha256.New()
	if _, err := io.Copy(hasher, file); err != nil || "sha256:"+fmt.Sprintf("%x", hasher.Sum(nil)) != runnerArchiveSHA256 {
		file.Close()
		return nil, nil, fmt.Errorf("runner-archive-digest-invalid")
	}
	if _, err := file.Seek(0, io.SeekStart); err != nil {
		file.Close()
		return nil, nil, fmt.Errorf("runner-archive-seek-failed")
	}
	return file, info, nil
}

func copyRunnerArchive(source *os.File, sourceInfo os.FileInfo, destination string) error {
	file, err := os.OpenFile(destination, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0o444)
	if err != nil {
		return fmt.Errorf("runner-stage-create-failed")
	}
	succeeded := false
	defer func() {
		_ = file.Close()
		if !succeeded {
			_ = os.Remove(destination)
		}
	}()
	if err := file.Chown(0, 0); err != nil || file.Chmod(0o444) != nil {
		return fmt.Errorf("runner-stage-metadata-failed")
	}
	hasher := sha256.New()
	written, err := io.Copy(io.MultiWriter(file, hasher), source)
	if err != nil || written != sourceInfo.Size() || "sha256:"+fmt.Sprintf("%x", hasher.Sum(nil)) != runnerArchiveSHA256 {
		return fmt.Errorf("runner-stage-copy-failed")
	}
	if err := file.Sync(); err != nil || file.Close() != nil {
		return fmt.Errorf("runner-stage-sync-failed")
	}
	succeeded = true
	return syncDirectory(filepath.Dir(destination))
}

func mustHostPath(installer *Installer, path string) string {
	result, err := installer.hostPath(path)
	if err != nil {
		panic("fixed host path invalid")
	}
	return result
}

func uint32Pointer(value uint32) *uint32 { return &value }
