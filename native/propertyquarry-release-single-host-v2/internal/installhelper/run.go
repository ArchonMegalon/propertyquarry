package installhelper

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"syscall"
)

const ExitFailure = 50

func Run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 1 && (args[0] == "--self-test" || args[0] == "--build-info-json") {
		_, keyID, keyErr := EmbeddedPackageAuthority()
		value := map[string]any{
			"authoritative": false, "component": "propertyquarry-release-single-host-installer-v2",
			"embedded_package_authority_bound": keyErr == nil, "embedded_package_authority_key_id": keyID,
			"host_install_performed": false, "performs_release_effects": false, "production_ready": false,
			"root_helper_required": true, "schema": "propertyquarry.release-control.single-host-installer-build-info.v2",
			"self_test": args[0] == "--self-test", "source_manifest_digest": InstallerSourceManifestDigest,
			"version": json.Number("2"),
		}
		raw, err := canonicalJSON(value)
		if err != nil {
			return ExitFailure
		}
		raw = append(raw, '\n')
		written, err := stdout.Write(raw)
		zero(raw)
		if err != nil || written < 1 {
			return ExitFailure
		}
		return 0
	}
	var err error
	if len(args) == 1 && args[0] == "host-systemd-canary" {
		var receipt []byte
		receipt, err = RunHostSystemdMutationCanary()
		if err == nil {
			written, writeErr := stdout.Write(receipt)
			if writeErr != nil || written != len(receipt) {
				err = fmt.Errorf("host-systemd-canary-receipt-write-failed")
			}
		}
		zero(receipt)
	} else if len(args) == 1 && (args[0] == "install" || args[0] == "install-runner") {
		var receipt []byte
		var installErr error
		receiptPath := FixedReceiptPath
		if args[0] == "install" {
			receipt, installErr = InstallFixedPackage()
		} else {
			receipt, installErr = InstallFixedRunnerArchive()
			receiptPath = FixedRunnerReceiptPath
		}
		if len(receipt) > 0 {
			if receiptErr := WriteReceipt(receiptPath, receipt); receiptErr != nil {
				err = receiptErr
			} else {
				err = installErr
			}
		} else {
			err = installErr
		}
		zero(receipt)
	} else if len(args) == 1 && (args[0] == "prepare-activation-host" || args[0] == "activate-host" || args[0] == "deactivate-host" || args[0] == "abort-activation-host") {
		err = HostSystemdOperation(args[0])
	} else {
		err = fmt.Errorf("installer-mode-invalid")
	}
	if err != nil {
		_, _ = io.WriteString(stderr, "propertyquarry-single-host-installer-rejected\n")
		return ExitFailure
	}
	return 0
}

func WriteFixedReceipt(raw []byte) error {
	return WriteReceipt(FixedReceiptPath, raw)
}

func WriteReceipt(path string, raw []byte) error {
	if len(raw) < 1 || len(raw) > maximumManifestBytes {
		return fmt.Errorf("install-receipt-size-invalid")
	}
	if _, err := strictJSON(raw, maximumManifestBytes); err != nil {
		return fmt.Errorf("install-receipt-invalid")
	}
	if path != FixedReceiptPath && path != FixedRunnerReceiptPath {
		return fmt.Errorf("install-receipt-path-invalid")
	}
	directoryInfo, err := os.Lstat(filepath.Dir(path))
	if err != nil || !directoryInfo.IsDir() || directoryInfo.Mode()&os.ModeSymlink != 0 || directoryInfo.Mode().Perm() != 0o700 {
		return fmt.Errorf("install-receipt-directory-invalid")
	}
	directoryMetadata, ok := directoryInfo.Sys().(*syscall.Stat_t)
	if !ok {
		return fmt.Errorf("install-receipt-directory-owner-invalid")
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return fmt.Errorf("install-receipt-create-failed")
	}
	succeeded := false
	defer func() {
		_ = file.Close()
		if !succeeded {
			_ = os.Remove(path)
		}
	}()
	if err := file.Chown(int(directoryMetadata.Uid), int(directoryMetadata.Gid)); err != nil {
		return fmt.Errorf("install-receipt-chown-failed")
	}
	if err := file.Chmod(0o600); err != nil {
		return fmt.Errorf("install-receipt-chmod-failed")
	}
	if written, err := file.Write(raw); err != nil || written != len(raw) {
		return fmt.Errorf("install-receipt-write-failed")
	}
	if err := file.Sync(); err != nil {
		return fmt.Errorf("install-receipt-sync-failed")
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("install-receipt-close-failed")
	}
	succeeded = true
	directory, err := os.OpenFile(filepath.Dir(path), os.O_RDONLY|syscall.O_CLOEXEC|syscall.O_DIRECTORY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return fmt.Errorf("install-receipt-directory-unavailable")
	}
	defer directory.Close()
	if err := directory.Sync(); err != nil {
		return fmt.Errorf("install-receipt-directory-sync-failed")
	}
	return nil
}
