package installhelper

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"syscall"
	"time"
)

const (
	FixedGitHubCredentialFIFOPath    = "/input/github-api-token.pipe"
	FixedGitHubCredentialReceiptPath = "/output/propertyquarry-release-single-host-v2-github-credential-receipt.json"
	githubCredentialPendingName      = ".github-api-token.cred.pending"
	githubCredentialMaximumBytes     = 64 * 1024
	githubTokenMaximumBytes          = 4096
	githubCredentialInputUID         = 1000
	githubCredentialInputGID         = 1000
)

type credentialPublishResult struct {
	ciphertext        []byte
	disposition       string
	installPerformed  bool
	recoveryPerformed bool
}

// ProvisionFixedGitHubCredential verifies the release package before reading
// the token from the fixed FIFO. The plaintext is only ever passed to
// systemd-creds through stdin and is cleared from process-owned byte slices.
func ProvisionFixedGitHubCredential() ([]byte, error) {
	if os.Geteuid() != 0 || os.Getegid() != 0 {
		return nil, fmt.Errorf("credential-provision-root-required")
	}
	packageKey, packageKeyID, err := EmbeddedPackageAuthority()
	if err != nil {
		return nil, err
	}
	defer zero(packageKey)
	verified, err := VerifyPackageFile(FixedPackagePath, packageKey, packageKeyID)
	if err != nil {
		return nil, err
	}
	defer verified.Release()
	if err := validateInstallerSelfBinding(verified); err != nil {
		return nil, err
	}
	if err := validateCredentialGenesisBinding(verified); err != nil {
		return nil, err
	}
	installer := &Installer{HostRoot: FixedHostRoot, OwnerUID: 0, OwnerGID: 0}
	if err := installer.currentAdmissionError(verified); err != nil {
		return nil, err
	}
	receiptKey, err := parsePrivatePEM(
		verified.Files["/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.key"].Data,
	)
	if err != nil {
		return nil, fmt.Errorf("credential-receipt-key-invalid")
	}
	defer zero(receiptKey)
	token, err := readGitHubTokenFIFO(
		FixedGitHubCredentialFIFOPath,
		githubCredentialInputUID,
		githubCredentialInputGID,
	)
	if err != nil {
		return nil, err
	}
	defer zero(token)
	if err := validateHostSystemdCredentialRuntime(FixedHostRoot); err != nil {
		return nil, err
	}
	encrypted, err := runHostSystemdCredentialTransform(FixedHostRoot, "encrypt", token)
	if err != nil {
		return nil, err
	}
	defer zero(encrypted)
	if !validEncryptedCredential(encrypted) || bytes.Contains(encrypted, token) {
		return nil, fmt.Errorf("credential-encrypted-output-invalid")
	}
	decrypted, err := runHostSystemdCredentialTransform(FixedHostRoot, "decrypt", encrypted)
	if err != nil {
		return nil, err
	}
	matched := subtle.ConstantTimeCompare(decrypted, token) == 1
	zero(decrypted)
	if !matched {
		return nil, fmt.Errorf("credential-round-trip-invalid")
	}
	published, err := publishEncryptedGitHubCredential(
		FixedHostRoot,
		0,
		0,
		token,
		encrypted,
		func(ciphertext []byte) ([]byte, error) {
			return runHostSystemdCredentialTransform(FixedHostRoot, "decrypt", ciphertext)
		},
	)
	if err != nil {
		return nil, err
	}
	defer zero(published.ciphertext)
	payload := map[string]any{
		"archive_digest":               verified.ArchiveDigest,
		"authority_profile":            "single-host-production-v2",
		"credential_ciphertext_bytes":  json.Number(strconv.Itoa(len(published.ciphertext))),
		"credential_ciphertext_sha256": digest(published.ciphertext),
		"credential_install_performed": published.installPerformed,
		"credential_mode":              "0400",
		"credential_path":              githubCredentialSource,
		"credential_present":           true,
		"credential_source_transport":  "named-fifo-fd8",
		"credential_uid":               json.Number("0"),
		"credential_gid":               json.Number("0"),
		"disposition":                  published.disposition,
		"host_mutation_performed":      published.installPerformed,
		"installed_at":                 json.Number(strconv.FormatInt(time.Now().UTC().Unix(), 10)),
		"package_authority_key_id":     verified.PackageAuthorityKeyID,
		"plaintext_digest_recorded":    false,
		"production_ready":             false,
		"receipt_authority_key_id":     verified.ReceiptAuthorityKeyID,
		"recovery_performed":           published.recoveryPerformed,
		"release_generation":           json.Number(strconv.FormatInt(verified.ReleaseGeneration, 10)),
		"rotation_performed":           false,
		"round_trip_verified":          true,
		"runtime_sha":                  verified.RuntimeSHA,
		"schema":                       "propertyquarry.release-control.single-host-github-credential-receipt.v2",
		"systemd_credential_key":       "host",
		"systemd_credential_name":      "github-api-token",
		"token_material_recorded":      false,
		"version":                      json.Number("2"),
		"workflow_sha":                 verified.WorkflowSHA,
	}
	return signedWire(payload, receiptKey, verified.ReceiptAuthorityKeyID)
}

func validateCredentialGenesisBinding(verified *VerifiedPackage) error {
	if verified == nil || verified.ReleaseGeneration != 1 {
		return fmt.Errorf("credential-release-generation-invalid")
	}
	record := verified.Files["/etc/propertyquarry-release-single-host-v2/authority.v2.json"]
	if record == nil {
		return fmt.Errorf("credential-config-unavailable")
	}
	config, err := strictJSON(record.Data, maximumManifestBytes)
	if err != nil {
		return fmt.Errorf("credential-config-invalid")
	}
	predecessor, _ := exactString(config["predecessor_runtime_sha"])
	if predecessor != "genesis" {
		return fmt.Errorf("credential-predecessor-invalid")
	}
	return nil
}

func readGitHubTokenFIFO(path string, uid, gid uint32) ([]byte, error) {
	if path == "" || !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return nil, fmt.Errorf("credential-fifo-path-invalid")
	}
	before, err := os.Lstat(path)
	if err != nil || before.Mode()&os.ModeNamedPipe == 0 || before.Mode()&os.ModeSymlink != 0 || before.Mode().Perm() != 0o600 {
		return nil, fmt.Errorf("credential-fifo-invalid")
	}
	metadata, ok := before.Sys().(*syscall.Stat_t)
	if !ok || metadata.Uid != uid || metadata.Gid != gid || metadata.Nlink != 1 {
		return nil, fmt.Errorf("credential-fifo-invalid")
	}
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, fmt.Errorf("credential-fifo-open-failed")
	}
	defer file.Close()
	after, err := file.Stat()
	afterMetadata, metadataOK := afterSyscallStat(after)
	if err != nil || !metadataOK || after.Mode()&os.ModeNamedPipe == 0 || after.Mode().Perm() != 0o600 || afterMetadata.Uid != uid || afterMetadata.Gid != gid || afterMetadata.Nlink != 1 || !os.SameFile(before, after) {
		return nil, fmt.Errorf("credential-fifo-changed")
	}
	raw, err := io.ReadAll(io.LimitReader(file, githubTokenMaximumBytes+2))
	if err != nil {
		zero(raw)
		return nil, fmt.Errorf("credential-fifo-read-failed")
	}
	if len(raw) > 0 && raw[len(raw)-1] == '\n' {
		raw = raw[:len(raw)-1]
	}
	if !validGitHubToken(raw) {
		zero(raw)
		return nil, fmt.Errorf("credential-token-invalid")
	}
	return raw, nil
}

func afterSyscallStat(info os.FileInfo) (*syscall.Stat_t, bool) {
	if info == nil {
		return nil, false
	}
	metadata, ok := info.Sys().(*syscall.Stat_t)
	return metadata, ok
}

func validGitHubToken(raw []byte) bool {
	if len(raw) < 32 || len(raw) > githubTokenMaximumBytes {
		return false
	}
	for _, value := range raw {
		if value < 0x21 || value > 0x7e {
			return false
		}
	}
	return true
}

func validateHostSystemdCredentialRuntime(hostRoot string) error {
	for _, expected := range []struct {
		path    string
		mode    os.FileMode
		minimum int64
		maximum int64
	}{
		{path: "/usr/bin/systemd-creds", mode: 0o755, minimum: 1, maximum: 64 * 1024 * 1024},
		{path: "/var/lib/systemd/credential.secret", mode: 0o400, minimum: 32, maximum: 64 * 1024},
	} {
		path := filepath.Join(hostRoot, expected.path)
		info, err := os.Lstat(path)
		metadata, ok := afterSyscallStat(info)
		if err != nil || !ok || !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm() != expected.mode || info.Size() < expected.minimum || info.Size() > expected.maximum || metadata.Uid != 0 || metadata.Gid != 0 || metadata.Nlink != 1 {
			return fmt.Errorf("credential-host-runtime-invalid")
		}
	}
	return nil
}

func runHostSystemdCredentialTransform(hostRoot, operation string, input []byte) ([]byte, error) {
	if hostRoot == "" || !filepath.IsAbs(hostRoot) || filepath.Clean(hostRoot) != hostRoot || (operation != "encrypt" && operation != "decrypt") || len(input) < 1 || len(input) > githubCredentialMaximumBytes {
		return nil, fmt.Errorf("credential-transform-input-invalid")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	arguments := []string{"--name=github-api-token", "--with-key=host", "--newline=no", operation, "-", "-"}
	command := exec.CommandContext(ctx, "/usr/bin/systemd-creds", arguments...)
	command.Env = []string{"HOME=/nonexistent", "LANG=C", "LC_ALL=C", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "TZ=UTC"}
	command.Stdin = bytes.NewReader(input)
	output := &boundedCommandOutput{limit: githubCredentialMaximumBytes}
	command.Stdout = output
	command.Stderr = nil
	command.SysProcAttr = &syscall.SysProcAttr{
		Chroot:    hostRoot,
		Setpgid:   true,
		Pdeathsig: syscall.SIGKILL,
	}
	command.WaitDelay = 5 * time.Second
	err := command.Run()
	if err != nil || ctx.Err() != nil || output.overflow || len(output.raw) < 1 {
		zero(output.raw)
		return nil, fmt.Errorf("credential-transform-failed")
	}
	return output.raw, nil
}

func validEncryptedCredential(raw []byte) bool {
	if len(raw) < 64 || len(raw) > githubCredentialMaximumBytes || raw[len(raw)-1] != '\n' {
		return false
	}
	lines := bytes.Split(raw, []byte{'\n'})
	if len(lines) < 2 || len(lines[len(lines)-1]) != 0 {
		return false
	}
	compact := make([]byte, 0, len(raw))
	for index, line := range lines[:len(lines)-1] {
		if len(line) < 1 || len(line) > 79 || (index < len(lines)-2 && len(line) != 79) {
			zero(compact)
			return false
		}
		for _, value := range line {
			if (value < 'A' || value > 'Z') && (value < 'a' || value > 'z') && (value < '0' || value > '9') && value != '+' && value != '/' && value != '=' {
				zero(compact)
				return false
			}
		}
		compact = append(compact, line...)
	}
	if len(compact)%4 != 0 {
		zero(compact)
		return false
	}
	decoded := make([]byte, base64.StdEncoding.DecodedLen(len(compact)))
	written, err := base64.StdEncoding.Strict().Decode(decoded, compact)
	zero(compact)
	zero(decoded)
	return err == nil && written > 0
}

func publishEncryptedGitHubCredential(
	hostRoot string,
	uid, gid uint32,
	token, encrypted []byte,
	decrypt func([]byte) ([]byte, error),
) (*credentialPublishResult, error) {
	if !validGitHubToken(token) || !validEncryptedCredential(encrypted) || decrypt == nil {
		return nil, fmt.Errorf("credential-publish-input-invalid")
	}
	directory := filepath.Join(hostRoot, "/etc/propertyquarry-release-single-host-v2")
	if err := validateDirectoryChain(hostRoot, filepath.Dir(directory), uid); err != nil {
		return nil, fmt.Errorf("credential-directory-parent-invalid")
	}
	if err := ensureCredentialDirectory(directory, uid, gid); err != nil {
		return nil, err
	}
	directoryFile, err := os.OpenFile(directory, os.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW|syscall.O_DIRECTORY, 0)
	if err != nil {
		return nil, fmt.Errorf("credential-directory-open-failed")
	}
	defer directoryFile.Close()
	if err := syscall.Flock(int(directoryFile.Fd()), syscall.LOCK_EX); err != nil {
		return nil, fmt.Errorf("credential-directory-lock-failed")
	}
	if err := validateCredentialDirectory(directory, uid, gid); err != nil {
		return nil, err
	}
	target := filepath.Join(directory, filepath.Base(githubCredentialSource))
	pending := filepath.Join(directory, githubCredentialPendingName)
	entries, err := os.ReadDir(directory)
	if err != nil || len(entries) > 1 {
		return nil, fmt.Errorf("credential-directory-state-invalid")
	}
	if len(entries) == 1 && entries[0].Name() != filepath.Base(target) && entries[0].Name() != filepath.Base(pending) {
		return nil, fmt.Errorf("credential-directory-state-invalid")
	}
	if len(entries) == 1 && entries[0].Name() == filepath.Base(target) {
		current, err := readExactFile(target, 0o400, uid, gid, githubCredentialMaximumBytes)
		if err != nil || !validEncryptedCredential(current) {
			zero(current)
			return nil, fmt.Errorf("credential-current-invalid")
		}
		plaintext, decryptErr := decrypt(current)
		matched := decryptErr == nil && subtle.ConstantTimeCompare(plaintext, token) == 1
		zero(plaintext)
		if !matched {
			zero(current)
			return nil, fmt.Errorf("credential-current-cas-required")
		}
		return &credentialPublishResult{ciphertext: current, disposition: "already-provisioned"}, nil
	}
	if len(entries) == 1 {
		info, statErr := os.Lstat(pending)
		metadata, metadataOK := afterSyscallStat(info)
		if statErr != nil || !metadataOK || !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 || metadata.Uid != uid || metadata.Gid != gid || metadata.Nlink != 1 || info.Size() < 0 || info.Size() > githubCredentialMaximumBytes {
			return nil, fmt.Errorf("credential-pending-invalid")
		}
		if info.Mode().Perm() == 0 {
			if err := os.Remove(pending); err != nil || syncDirectory(directory) != nil {
				return nil, fmt.Errorf("credential-pending-cleanup-failed")
			}
		} else if info.Mode().Perm() == 0o400 {
			current, readErr := readExactFile(pending, 0o400, uid, gid, githubCredentialMaximumBytes)
			if readErr != nil || !validEncryptedCredential(current) {
				zero(current)
				return nil, fmt.Errorf("credential-pending-invalid")
			}
			plaintext, decryptErr := decrypt(current)
			matched := decryptErr == nil && subtle.ConstantTimeCompare(plaintext, token) == 1
			zero(plaintext)
			if !matched {
				zero(current)
				return nil, fmt.Errorf("credential-pending-cas-required")
			}
			if err := renameNoReplace(pending, target); err != nil || syncDirectory(directory) != nil {
				zero(current)
				return nil, fmt.Errorf("credential-pending-publish-failed")
			}
			return &credentialPublishResult{ciphertext: current, disposition: "recovered-and-provisioned", installPerformed: true, recoveryPerformed: true}, nil
		} else {
			return nil, fmt.Errorf("credential-pending-invalid")
		}
	}
	file, err := os.OpenFile(pending, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, fmt.Errorf("credential-stage-create-failed")
	}
	stageComplete := false
	defer func() {
		_ = file.Close()
		if !stageComplete {
			_ = os.Remove(pending)
			_ = syncDirectory(directory)
		}
	}()
	if err := file.Chown(int(uid), int(gid)); err != nil {
		return nil, fmt.Errorf("credential-stage-owner-failed")
	}
	if written, err := file.Write(encrypted); err != nil || written != len(encrypted) {
		return nil, fmt.Errorf("credential-stage-write-failed")
	}
	if err := file.Sync(); err != nil || file.Chmod(0o400) != nil || file.Sync() != nil || file.Close() != nil {
		return nil, fmt.Errorf("credential-stage-sync-failed")
	}
	if err := syncDirectory(directory); err != nil {
		return nil, fmt.Errorf("credential-stage-directory-sync-failed")
	}
	if _, err := os.Lstat(target); !os.IsNotExist(err) {
		return nil, fmt.Errorf("credential-target-raced")
	}
	if err := renameNoReplace(pending, target); err != nil {
		return nil, fmt.Errorf("credential-publish-failed")
	}
	stageComplete = true
	if err := syncDirectory(directory); err != nil {
		return nil, fmt.Errorf("credential-publish-sync-failed")
	}
	return &credentialPublishResult{ciphertext: append([]byte(nil), encrypted...), disposition: "provisioned", installPerformed: true}, nil
}

func ensureCredentialDirectory(path string, uid, gid uint32) error {
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		if err := os.Mkdir(path, 0o700); err != nil {
			return fmt.Errorf("credential-directory-create-failed")
		}
		if err := os.Chown(path, int(uid), int(gid)); err != nil || os.Chmod(path, 0o700) != nil || syncDirectory(filepath.Dir(path)) != nil {
			return fmt.Errorf("credential-directory-create-failed")
		}
		info, err = os.Lstat(path)
	}
	if err != nil {
		return fmt.Errorf("credential-directory-invalid")
	}
	return validateCredentialDirectoryInfo(info, uid, gid)
}

func validateCredentialDirectory(path string, uid, gid uint32) error {
	info, err := os.Lstat(path)
	if err != nil {
		return fmt.Errorf("credential-directory-invalid")
	}
	return validateCredentialDirectoryInfo(info, uid, gid)
}

func validateCredentialDirectoryInfo(info os.FileInfo, uid, gid uint32) error {
	metadata, ok := afterSyscallStat(info)
	if !ok || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm() != 0o700 || metadata.Uid != uid || metadata.Gid != gid {
		return fmt.Errorf("credential-directory-invalid")
	}
	return nil
}
