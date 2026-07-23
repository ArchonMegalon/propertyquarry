//go:build linux && amd64

package authority

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"syscall"
	"time"
)

const (
	RunnerLaunchTicketPath                   = "/var/lib/propertyquarry-release-single-host-v2/runner-launch-ticket.v2.json"
	runnerClaimPath                          = "/var/lib/propertyquarry-release-single-host-v2/runner-launch-claim.v2.json"
	runnerStartPath                          = "/var/lib/propertyquarry-release-single-host-v2/runner-start-authorization.v2.json"
	runnerTerminalRoot                       = "/var/lib/propertyquarry-release-single-host-v2"
	runnerTicketLockPath                     = "/var/lib/propertyquarry-release-single-host-v2/.runner-ticket.lock"
	runnerTicketSchema                       = "propertyquarry.release-control.single-host-runner-launch-ticket.v2"
	runnerClaimSchema                        = "propertyquarry.release-control.single-host-runner-launch-claim.v2"
	runnerStartSchema                        = "propertyquarry.release-control.single-host-runner-start-authorization.v2"
	runnerTerminalSchema                     = "propertyquarry.release-control.single-host-runner-launch-terminal.v2"
	runnerRecoveryTerminalSchema             = "propertyquarry.release-control.single-host-runner-recovery-terminal.v2"
	runnerTicketResultSchema                 = "propertyquarry.release-control.single-host-runner-ticket-result.v2"
	runnerStartResultSchema                  = "propertyquarry.release-control.single-host-runner-start-result.v2"
	runnerTicketSignatureDomain              = "propertyquarry.release-control.single-host-runner-launch-ticket-signature.v2\x00"
	runnerClaimSignatureDomain               = "propertyquarry.release-control.single-host-runner-launch-claim-signature.v2\x00"
	runnerStartSignatureDomain               = "propertyquarry.release-control.single-host-runner-start-authorization-signature.v2\x00"
	runnerTerminalSignatureDomain            = "propertyquarry.release-control.single-host-runner-launch-terminal-signature.v2\x00"
	runnerLabelDerivationDomain              = "propertyquarry.release-control.single-host-runner-label.v2\x00"
	runnerPackageManifestPath                = "/etc/propertyquarry-release-single-host-v2/package-manifest.v2.json"
	runnerPackageManifestSignaturePath       = "/etc/propertyquarry-release-single-host-v2/package-manifest.v2.sig"
	runnerExecutionTTL                       = 6 * time.Hour
	runnerUID                          int64 = 1999
	runnerGID                          int64 = 1999
	dockerSocketGID                    int64 = 112
)

var (
	runnerLabelPattern      = regexp.MustCompile(`^pqrelease-[0-9a-f]{32}$`)
	runnerNoncePattern      = regexp.MustCompile(`^[0-9a-f]{64}$`)
	runnerPendingPattern    = regexp.MustCompile(`^\.runner-pending-[0-9a-f]{64}\.tmp$`)
	runnerCommandIdentity   = func() (int, int) { return os.Geteuid(), os.Getegid() }
	runnerStartVerification = verifyRunnerStartForLauncher
)

type runnerDockerSocket struct {
	Device uint64
	Inode  uint64
	Mode   uint32
	UID    uint32
	GID    uint32
	Nlink  uint64
}

type runnerTicketBinding struct {
	DispatchTicketDigest string
	LaunchTicketDigest   string
	RunnerLabel          string
	RunnerNonce          string
	RunID                string
	RunAttempt           int64
	JobID                string
	RunnerImage          string
	BoundAt              int64
	TicketExpiresAt      int64
	ExecutionExpiresAt   int64
	RunnerID             string
	RunnerName           string
	SessionDevice        uint64
	SessionInode         uint64
	SessionTreeDigest    string
}

type runnerConfigBinding struct {
	ReservationDigest string
	Label             string
	RunID             string
	RunAttempt        int64
	JobID             string
}

func runnerTerminalPath(config *Config) string {
	if config == nil || !digestPattern.MatchString(config.Digest) {
		return runnerTerminalRoot + "/runner-launch-terminal-invalid.v2.json"
	}
	return runnerTerminalRoot + "/runner-launch-terminal-" + config.Digest[len("sha256:"):] + ".v2.json"
}

func configRunnerBinding(config *Config) (*runnerConfigBinding, error) {
	if config == nil {
		return nil, fmt.Errorf("runner-config-input-invalid")
	}
	value, err := strictJSON(config.Raw, maximumConfigBytes)
	if err != nil {
		return nil, fmt.Errorf("runner-config-invalid")
	}
	reservation, reservationOK := exactString(value["runner_reservation_sha256"])
	label, labelOK := exactString(value["runner_label"])
	runID, runOK := exactString(value["runner_run_id"])
	attempt, attemptOK := exactInt(value["runner_run_attempt"], 1, 1<<31-1)
	jobID, jobOK := exactString(value["runner_job_id"])
	if !reservationOK || !digestPattern.MatchString(reservation) || !labelOK || !runnerLabelPattern.MatchString(label) || !runOK || !decimal(runID) || !attemptOK || !jobOK || !decimal(jobID) {
		return nil, fmt.Errorf("runner-config-binding-invalid")
	}
	return &runnerConfigBinding{ReservationDigest: reservation, Label: label, RunID: runID, RunAttempt: attempt, JobID: jobID}, nil
}

func expectedSocketOwner(root string) (uint32, uint32) {
	if root == "" || root == "/" {
		return 0, uint32(dockerSocketGID)
	}
	return secureOwner(root)
}

func observeDockerSocket(root string) (runnerDockerSocket, error) {
	path := rooted(root, "/var/run/docker.sock")
	info, err := os.Lstat(path)
	metadata, ok := infoSys(info)
	expectedUID, expectedGID := expectedSocketOwner(root)
	if err != nil || !ok || info.Mode()&os.ModeSymlink != 0 || info.Mode()&os.ModeSocket == 0 || info.Mode().Perm() != 0o660 || metadata.Uid != expectedUID || metadata.Gid != expectedGID || metadata.Nlink != 1 {
		return runnerDockerSocket{}, fmt.Errorf("runner-docker-socket-invalid")
	}
	return runnerDockerSocket{Device: uint64(metadata.Dev), Inode: metadata.Ino, Mode: 0o660, UID: metadata.Uid, GID: metadata.Gid, Nlink: metadata.Nlink}, nil
}

func socketFromValue(value map[string]any, root string) (runnerDockerSocket, error) {
	device, deviceOK := exactInt(value["device"], 1, 1<<62)
	inode, inodeOK := exactInt(value["inode"], 1, 1<<62)
	expectedUID, expectedGID := expectedSocketOwner(root)
	uid, uidOK := exactInt(value["uid"], int64(expectedUID), int64(expectedUID))
	gid, gidOK := exactInt(value["gid"], int64(expectedGID), int64(expectedGID))
	nlink, nlinkOK := exactInt(value["nlink"], 1, 1)
	if !hasKeys(value, "device", "gid", "inode", "mode", "nlink", "path", "uid") || !deviceOK || !inodeOK || !uidOK || !gidOK || !nlinkOK || value["mode"] != "0660" || value["path"] != "/var/run/docker.sock" {
		return runnerDockerSocket{}, fmt.Errorf("runner-ticket-docker-socket-invalid")
	}
	return runnerDockerSocket{Device: uint64(device), Inode: uint64(inode), Mode: 0o660, UID: uint32(uid), GID: uint32(gid), Nlink: uint64(nlink)}, nil
}

func verifyRunnerWire(raw []byte, public ed25519.PublicKey, domain string) (map[string]any, error) {
	wrapper, err := strictJSON(raw, maximumJournalBytes)
	if err != nil || !hasKeys(wrapper, "payload", "signature", "signature_key_id") {
		return nil, fmt.Errorf("runner-wire-wrapper-invalid")
	}
	payload, payloadOK := wrapper["payload"].(map[string]any)
	signatureText, signatureOK := exactString(wrapper["signature"])
	keyID, keyErr := publicKeyID(public)
	if !payloadOK || !signatureOK || keyErr != nil || wrapper["signature_key_id"] != keyID {
		return nil, fmt.Errorf("runner-wire-binding-invalid")
	}
	signature, err := base64.RawURLEncoding.DecodeString(signatureText)
	canonical, canonicalErr := canonicalJSON(payload)
	if err != nil || canonicalErr != nil || len(signature) != ed25519.SignatureSize || base64.RawURLEncoding.EncodeToString(signature) != signatureText || !ed25519.Verify(public, framed(domain, canonical), signature) {
		zero(signature)
		zero(canonical)
		return nil, fmt.Errorf("runner-wire-signature-invalid")
	}
	zero(signature)
	zero(canonical)
	return payload, nil
}

func signRunnerWire(payload map[string]any, key ed25519.PrivateKey, domain string) ([]byte, error) {
	canonical, err := canonicalJSON(payload)
	if err != nil || len(canonical) > maximumJournalBytes/2 {
		zero(canonical)
		return nil, fmt.Errorf("runner-wire-payload-invalid")
	}
	signature := ed25519.Sign(key, framed(domain, canonical))
	zero(canonical)
	keyID, err := publicKeyID(key.Public().(ed25519.PublicKey))
	if err != nil {
		zero(signature)
		return nil, err
	}
	wire, err := canonicalJSON(map[string]any{"payload": payload, "signature": base64.RawURLEncoding.EncodeToString(signature), "signature_key_id": keyID})
	zero(signature)
	return wire, err
}

func readRunnerWire(root, absolute string, mode uint32, public ed25519.PublicKey, domain string) (map[string]any, []byte, error) {
	uid, gid := secureOwner(root)
	raw, err := secureRead(root, absolute, mode, uid, gid, maximumJournalBytes)
	if err != nil {
		return nil, nil, err
	}
	payload, err := verifyRunnerWire(raw, public, domain)
	if err != nil {
		zero(raw)
		return nil, nil, err
	}
	return payload, raw, nil
}

func validateLaunchTicket(root string, payload map[string]any, raw []byte, config *Config, now time.Time, enforceLaunchWindow bool) (*runnerTicketBinding, error) {
	configBinding, err := configRunnerBinding(config)
	if err != nil {
		return nil, err
	}
	label, labelOK := exactString(payload["runner_label"])
	nonce, nonceOK := exactString(payload["reservation_nonce"])
	dispatch, dispatchOK := exactString(payload["dispatch_ticket_sha256"])
	runID, runOK := exactString(payload["run_id"])
	attempt, attemptOK := exactInt(payload["run_attempt"], 1, 1<<31-1)
	jobID, jobOK := exactString(payload["job_id"])
	boundAt, boundOK := exactInt(payload["bound_at_epoch"], 1, 1<<62)
	expiresAt, expiresOK := exactInt(payload["expires_at_epoch"], 1, 1<<62)
	image, imageOK := exactString(payload["runner_image"])
	socketValue, socketOK := payload["docker_socket"].(map[string]any)
	expectedKeys := []string{
		"authority_profile", "bound_at_epoch", "config_digest", "dispatch_ticket_sha256", "docker_socket", "environment", "expires_at_epoch", "job_id", "plan_digest", "receipt_authority_key_id", "release_job", "repository", "repository_id", "repository_owner_id", "reservation_nonce", "run_attempt", "run_id", "runner_image", "runner_label", "runner_label_nonce", "runner_prerequisite_approval_payload_sha256", "runner_prerequisite_approval_sha256", "runner_prerequisite_intent_sha256", "runner_prerequisite_job_id", "runtime_sha", "schema", "version", "workflow_path", "workflow_ref", "workflow_sha",
	}
	if !hasKeys(payload, expectedKeys...) || payload["schema"] != runnerTicketSchema || payload["version"] != json.Number("2") || payload["authority_profile"] != "single-host-production-v2" || payload["environment"] != Environment || payload["repository"] != Repository || payload["repository_id"] != config.RepositoryID || payload["repository_owner_id"] != config.RepositoryOwnerID || payload["workflow_path"] != ".github/workflows/smoke-runtime.yml" || payload["workflow_ref"] != WorkflowRef || payload["workflow_sha"] != config.WorkflowSHA || payload["release_job"] != ReleaseJob || payload["runtime_sha"] != config.RuntimeSHA || payload["config_digest"] != config.Digest || payload["plan_digest"] != config.PlanDigest || payload["receipt_authority_key_id"] != config.ReceiptAuthorityKeyID || payload["runner_prerequisite_intent_sha256"] != config.RunnerPrerequisiteIntentDigest || payload["runner_prerequisite_approval_sha256"] != config.RunnerPrerequisiteApprovalDigest || payload["runner_prerequisite_approval_payload_sha256"] != config.RunnerPrerequisiteApprovalPayloadDigest || payload["runner_prerequisite_job_id"] != config.RunnerPrerequisiteJobID || !labelOK || label != configBinding.Label || !runnerLabelPattern.MatchString(label) || payload["runner_label_nonce"] != label[len("pqrelease-"):] || !nonceOK || !runnerNoncePattern.MatchString(nonce) || !dispatchOK || dispatch != configBinding.ReservationDigest || !runOK || runID != configBinding.RunID || !attemptOK || attempt != configBinding.RunAttempt || !jobOK || jobID != configBinding.JobID || !boundOK || !expiresOK || expiresAt <= boundAt || expiresAt-boundAt > 1800 || !imageOK || image != config.WebImage || !socketOK {
		return nil, fmt.Errorf("runner-ticket-binding-invalid")
	}
	nonceRaw, _ := hex.DecodeString(nonce)
	derived := sha256.Sum256(append([]byte(runnerLabelDerivationDomain), nonceRaw...))
	zero(nonceRaw)
	if label != "pqrelease-"+hex.EncodeToString(derived[:16]) {
		return nil, fmt.Errorf("runner-ticket-label-derivation-invalid")
	}
	expectedSocket, err := socketFromValue(socketValue, root)
	if err != nil {
		return nil, err
	}
	currentSocket, err := observeDockerSocket(root)
	if err != nil || currentSocket != expectedSocket {
		return nil, fmt.Errorf("runner-ticket-docker-socket-changed")
	}
	if now.IsZero() || now.UTC().Unix() < boundAt || (enforceLaunchWindow && now.UTC().Unix() > expiresAt) {
		return nil, fmt.Errorf("runner-ticket-time-invalid")
	}
	return &runnerTicketBinding{DispatchTicketDigest: dispatch, LaunchTicketDigest: digest(raw), RunnerLabel: label, RunnerNonce: label[len("pqrelease-"):], RunID: runID, RunAttempt: attempt, JobID: jobID, RunnerImage: image, BoundAt: boundAt, TicketExpiresAt: expiresAt}, nil
}

func verifyRunnerPackageBinding(root string, config *Config, ticketRaw []byte) error {
	uid, gid := secureOwner(root)
	manifestRaw, err := secureRead(root, runnerPackageManifestPath, 0o444, uid, gid, maximumJournalBytes)
	if err != nil {
		return fmt.Errorf("runner-package-manifest-unavailable")
	}
	defer zero(manifestRaw)
	signature, err := secureRead(root, runnerPackageManifestSignaturePath, 0o444, uid, gid, ed25519.SignatureSize)
	if err != nil {
		return fmt.Errorf("runner-package-signature-unavailable")
	}
	defer zero(signature)
	anchorRaw, err := secureRead(root, PackageAnchorPath, 0o444, uid, gid, 4096)
	if err != nil {
		return fmt.Errorf("runner-package-anchor-unavailable")
	}
	anchor, keyID, err := parsePublicKey(anchorRaw)
	zero(anchorRaw)
	if err != nil || keyID != config.PackageAuthorityKeyID || !ed25519.Verify(anchor, framed(packageManifestSignatureDomain, manifestRaw), signature) {
		zero(anchor)
		return fmt.Errorf("runner-package-authentication-failed")
	}
	zero(anchor)
	manifest, err := strictJSON(manifestRaw, maximumJournalBytes)
	items, itemsOK := manifest["files"].([]any)
	if err != nil || manifest["schema"] != "propertyquarry.release-control.single-host-package.v2" || manifest["config_digest"] != config.Digest || manifest["plan_digest"] != config.PlanDigest || manifest["receipt_authority_key_id"] != config.ReceiptAuthorityKeyID || !itemsOK {
		return fmt.Errorf("runner-package-binding-invalid")
	}
	found := false
	for _, rawItem := range items {
		item, ok := rawItem.(map[string]any)
		if !ok {
			return fmt.Errorf("runner-package-file-invalid")
		}
		if item["install_path"] != RunnerLaunchTicketPath {
			continue
		}
		size, sizeOK := exactInt(item["size"], int64(len(ticketRaw)), int64(len(ticketRaw)))
		if found || !hasKeys(item, "install_path", "mode", "package_path", "purpose", "sha256", "size") || item["mode"] != "0400" || item["purpose"] != "ephemeral-runner-launch-ticket" || item["sha256"] != digest(ticketRaw) || !sizeOK || size != int64(len(ticketRaw)) {
			return fmt.Errorf("runner-package-ticket-file-invalid")
		}
		found = true
	}
	if !found {
		return fmt.Errorf("runner-package-ticket-missing")
	}
	return nil
}

func acquireRunnerTicketLock(root string) (*os.File, error) {
	path := rooted(root, runnerTicketLockPath)
	uid, gid := secureOwner(root)
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return nil, fmt.Errorf("runner-ticket-lock-unavailable")
	}
	info, statErr := file.Stat()
	metadata, ok := infoSys(info)
	if statErr != nil || !ok || !info.Mode().IsRegular() || info.Mode().Perm() != 0o600 || metadata.Uid != uid || metadata.Gid != gid || metadata.Nlink != 1 || syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB) != nil {
		file.Close()
		return nil, fmt.Errorf("runner-ticket-lock-invalid")
	}
	if err := recoverRunnerPending(root); err != nil {
		_ = syscall.Flock(int(file.Fd()), syscall.LOCK_UN)
		file.Close()
		return nil, err
	}
	return file, nil
}

func recoverRunnerPending(root string) error {
	directoryPath := rooted(root, runnerTerminalRoot)
	directory, err := os.Open(directoryPath)
	if err != nil {
		return fmt.Errorf("runner-state-directory-unavailable")
	}
	defer directory.Close()
	items, err := directory.ReadDir(-1)
	if err != nil {
		return fmt.Errorf("runner-state-stage-scan-failed")
	}
	uid, gid := secureOwner(root)
	removed := false
	for _, item := range items {
		if !runnerPendingPattern.MatchString(item.Name()) {
			continue
		}
		path := filepath.Join(directoryPath, item.Name())
		info, statErr := os.Lstat(path)
		metadata, ok := infoSys(info)
		if statErr != nil || !ok || !info.Mode().IsRegular() || info.Mode().Perm() != 0o400 || metadata.Uid != uid || metadata.Gid != gid || metadata.Nlink != 1 || os.Remove(path) != nil {
			return fmt.Errorf("runner-state-stage-invalid")
		}
		removed = true
	}
	if removed && directory.Sync() != nil {
		return fmt.Errorf("runner-state-stage-sync-failed")
	}
	return nil
}

func randomRunnerPendingName() (string, error) {
	raw := make([]byte, 32)
	if _, err := io.ReadFull(rand.Reader, raw); err != nil {
		return "", fmt.Errorf("runner-state-random-unavailable")
	}
	name := ".runner-pending-" + hex.EncodeToString(raw) + ".tmp"
	zero(raw)
	return name, nil
}

func closeRunnerTicketLock(file *os.File) {
	if file != nil {
		_ = syscall.Flock(int(file.Fd()), syscall.LOCK_UN)
		_ = file.Close()
	}
}

func writeRunnerStateNoReplace(root, absolute string, raw []byte) error {
	path := rooted(root, absolute)
	uid, gid := secureOwner(root)
	directory, err := os.OpenFile(filepath.Dir(path), os.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return fmt.Errorf("runner-state-directory-unavailable")
	}
	defer directory.Close()
	pending, err := randomRunnerPendingName()
	if err != nil {
		return err
	}
	fd, err := syscall.Openat(int(directory.Fd()), pending, syscall.O_WRONLY|syscall.O_CREAT|syscall.O_EXCL|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0o400)
	if err != nil {
		return fmt.Errorf("runner-state-stage-unavailable")
	}
	file := os.NewFile(uintptr(fd), pending)
	succeeded := false
	defer func() {
		_ = file.Close()
		if !succeeded {
			_ = syscall.Unlinkat(int(directory.Fd()), pending)
		}
	}()
	if file.Chown(int(uid), int(gid)) != nil || file.Chmod(0o400) != nil || writeAll(file, raw) != nil || file.Sync() != nil || file.Close() != nil {
		return fmt.Errorf("runner-state-stage-write-failed")
	}
	if renameAtNoReplace(int(directory.Fd()), pending, filepath.Base(path)) != nil || directory.Sync() != nil {
		return fmt.Errorf("runner-state-publish-failed")
	}
	succeeded = true
	return nil
}

func removeRunnerState(root, absolute string, allowMissing bool) error {
	path := rooted(root, absolute)
	info, err := os.Lstat(path)
	if os.IsNotExist(err) && allowMissing {
		return nil
	}
	uid, gid := secureOwner(root)
	metadata, ok := infoSys(info)
	if err != nil || !ok || !info.Mode().IsRegular() || info.Mode().Perm() != 0o400 || metadata.Uid != uid || metadata.Gid != gid || metadata.Nlink != 1 {
		return fmt.Errorf("runner-state-remove-target-invalid")
	}
	if os.Remove(path) != nil {
		return fmt.Errorf("runner-state-remove-failed")
	}
	directory, err := os.Open(filepath.Dir(path))
	if err != nil {
		return fmt.Errorf("runner-state-directory-unavailable")
	}
	defer directory.Close()
	return directory.Sync()
}

func runnerStatePayload(binding *runnerTicketBinding, config *Config, schema string, timestamp int64) map[string]any {
	return map[string]any{
		"admitted_at_epoch": json.Number(strconv.FormatInt(timestamp, 10)), "config_digest": config.Digest,
		"dispatch_ticket_sha256": binding.DispatchTicketDigest, "job_id": binding.JobID, "launch_ticket_sha256": binding.LaunchTicketDigest,
		"plan_digest": config.PlanDigest, "receipt_authority_key_id": config.ReceiptAuthorityKeyID, "run_attempt": json.Number(strconv.FormatInt(binding.RunAttempt, 10)),
		"run_id": binding.RunID, "runner_image": binding.RunnerImage, "runner_label": binding.RunnerLabel, "runner_label_nonce": binding.RunnerNonce,
		"schema": schema, "ticket_expires_at_epoch": json.Number(strconv.FormatInt(binding.TicketExpiresAt, 10)), "version": json.Number("2"),
	}
}

func bindingFromState(payload map[string]any, config *Config, now time.Time, allowExpired bool) (*runnerTicketBinding, error) {
	configBinding, err := configRunnerBinding(config)
	if err != nil {
		return nil, err
	}
	label, labelOK := exactString(payload["runner_label"])
	nonce, nonceOK := exactString(payload["runner_label_nonce"])
	dispatch, dispatchOK := exactString(payload["dispatch_ticket_sha256"])
	launch, launchOK := exactString(payload["launch_ticket_sha256"])
	runID, runOK := exactString(payload["run_id"])
	attempt, attemptOK := exactInt(payload["run_attempt"], 1, 1<<31-1)
	jobID, jobOK := exactString(payload["job_id"])
	image, imageOK := exactString(payload["runner_image"])
	ticketExpires, expiresOK := exactInt(payload["ticket_expires_at_epoch"], 1, 1<<62)
	admitted, admittedOK := exactInt(payload["admitted_at_epoch"], 1, 1<<62)
	if payload["version"] != json.Number("2") || payload["config_digest"] != config.Digest || payload["plan_digest"] != config.PlanDigest || payload["receipt_authority_key_id"] != config.ReceiptAuthorityKeyID || !labelOK || label != configBinding.Label || !nonceOK || nonce != label[len("pqrelease-"):] || !dispatchOK || dispatch != configBinding.ReservationDigest || !launchOK || !digestPattern.MatchString(launch) || !runOK || runID != configBinding.RunID || !attemptOK || attempt != configBinding.RunAttempt || !jobOK || jobID != configBinding.JobID || !imageOK || image != config.WebImage || !expiresOK || !admittedOK || admitted > ticketExpires || (!allowExpired && now.UTC().Unix() > admitted+int64(runnerExecutionTTL/time.Second)) {
		return nil, fmt.Errorf("runner-state-binding-invalid")
	}
	return &runnerTicketBinding{DispatchTicketDigest: dispatch, LaunchTicketDigest: launch, RunnerLabel: label, RunnerNonce: nonce, RunID: runID, RunAttempt: attempt, JobID: jobID, RunnerImage: image, TicketExpiresAt: ticketExpires, ExecutionExpiresAt: admitted + int64(runnerExecutionTTL/time.Second)}, nil
}

func loadRunnerStateBinding(root, absolute, domain string, config *Config, public ed25519.PublicKey, now time.Time, allowExpired bool) (*runnerTicketBinding, map[string]any, error) {
	payload, raw, err := readRunnerWire(root, absolute, 0o400, public, domain)
	zero(raw)
	if err != nil {
		return nil, nil, err
	}
	binding, err := bindingFromState(payload, config, now, allowExpired)
	return binding, payload, err
}

func runnerCommonStateKeys() []string {
	return []string{"admitted_at_epoch", "config_digest", "dispatch_ticket_sha256", "job_id", "launch_ticket_sha256", "plan_digest", "receipt_authority_key_id", "run_attempt", "run_id", "runner_image", "runner_label", "runner_label_nonce", "schema", "ticket_expires_at_epoch", "version"}
}

func loadRunnerClaim(root string, config *Config, public ed25519.PublicKey, now time.Time, allowExpired bool) (*runnerTicketBinding, map[string]any, error) {
	binding, payload, err := loadRunnerStateBinding(root, runnerClaimPath, runnerClaimSignatureDomain, config, public, now, allowExpired)
	if err != nil || !hasKeys(payload, runnerCommonStateKeys()...) || payload["schema"] != runnerClaimSchema {
		return nil, nil, fmt.Errorf("runner-claim-binding-invalid")
	}
	return binding, payload, nil
}

func loadRunnerTerminal(root string, config *Config, public ed25519.PublicKey, now time.Time, allowExpired bool) (*runnerTicketBinding, map[string]any, error) {
	binding, payload, err := loadRunnerStateBinding(root, runnerTerminalPath(config), runnerTerminalSignatureDomain, config, public, now, allowExpired)
	consumed, consumedOK := exactInt(payload["consumed_at_epoch"], 1, 1<<62)
	runnerID, runnerIDOK := exactString(payload["runner_id"])
	runnerName, runnerNameOK := exactString(payload["runner_name"])
	normalKeys := append(runnerCommonStateKeys(), "consumed_at_epoch", "disposition", "runner_id", "runner_name")
	recoveryKeys := append(append([]string(nil), normalKeys...), "recovery_observation_sha256", "remote_runner_absent")
	normal := hasKeys(payload, normalKeys...) && payload["schema"] == runnerTerminalSchema && payload["disposition"] == "preflight-admitted-and-consumed" && decimal(runnerID)
	recoveryObservation, recoveryOK := exactString(payload["recovery_observation_sha256"])
	recovered := hasKeys(payload, recoveryKeys...) && payload["schema"] == runnerRecoveryTerminalSchema && payload["disposition"] == "interrupted-runner-recovered" && payload["remote_runner_absent"] == true && recoveryOK && digestPattern.MatchString(recoveryObservation) && (decimal(runnerID) || runnerID == "absent")
	if err != nil || (!normal && !recovered) || !consumedOK || !runnerIDOK || !runnerNameOK || runnerName != "pq-release-"+binding.RunnerNonce {
		return nil, nil, fmt.Errorf("runner-terminal-binding-invalid")
	}
	admitted, _ := exactInt(payload["admitted_at_epoch"], 1, 1<<62)
	if consumed < admitted {
		return nil, nil, fmt.Errorf("runner-terminal-time-invalid")
	}
	binding.RunnerID, binding.RunnerName = runnerID, runnerName
	return binding, payload, nil
}

func runnerStateExists(root, absolute string) (bool, error) {
	info, err := os.Lstat(rooted(root, absolute))
	if os.IsNotExist(err) {
		return false, nil
	}
	if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
		return false, fmt.Errorf("runner-state-presence-invalid")
	}
	return true, nil
}

func convergeRunnerTerminalResidue(root string, config *Config, public ed25519.PublicKey, terminal *runnerTicketBinding, now time.Time) error {
	if terminal == nil {
		return fmt.Errorf("runner-terminal-convergence-input-invalid")
	}
	claimPresent, err := runnerStateExists(root, runnerClaimPath)
	if err != nil {
		return err
	}
	if claimPresent {
		claim, _, claimErr := loadRunnerClaim(root, config, public, now, true)
		if claimErr != nil || claim.LaunchTicketDigest != terminal.LaunchTicketDigest || removeRunnerState(root, runnerClaimPath, false) != nil {
			return fmt.Errorf("runner-terminal-claim-convergence-failed")
		}
	}
	startPresent, err := runnerStateExists(root, runnerStartPath)
	if err != nil {
		return err
	}
	if startPresent {
		start, _, startErr := loadRunnerStart(root, config, public, now, true)
		if startErr != nil || start.LaunchTicketDigest != terminal.LaunchTicketDigest || removeRunnerState(root, runnerStartPath, false) != nil {
			return fmt.Errorf("runner-terminal-start-convergence-failed")
		}
	}
	return nil
}

func recoverRunnerLifecycle(root string, now time.Time, observationDigest string) (*runnerTicketBinding, error) {
	if now.IsZero() || !digestPattern.MatchString(observationDigest) {
		return nil, fmt.Errorf("runner-recovery-input-invalid")
	}
	lock, err := acquireRunnerTicketLock(root)
	if err != nil {
		return nil, err
	}
	defer closeRunnerTicketLock(lock)
	config, key, err := LoadConfig(root)
	if err != nil {
		return nil, err
	}
	defer config.release()
	defer zero(key)
	public := key.Public().(ed25519.PublicKey)
	if terminal, _, terminalErr := loadRunnerTerminal(root, config, public, now, true); terminalErr == nil {
		if err := convergeRunnerTerminalResidue(root, config, public, terminal, now); err != nil {
			return nil, err
		}
		return terminal, nil
	}
	claim, claimPayload, err := loadRunnerClaim(root, config, public, now, true)
	if err != nil {
		return nil, fmt.Errorf("runner-recovery-claim-invalid")
	}
	ticketPayload, ticketRaw, err := readRunnerWire(root, RunnerLaunchTicketPath, 0o400, public, runnerTicketSignatureDomain)
	if err != nil {
		return nil, fmt.Errorf("runner-recovery-ticket-unavailable")
	}
	observedTicket, ticketErr := validateLaunchTicket(root, ticketPayload, ticketRaw, config, now, false)
	zero(ticketRaw)
	if ticketErr != nil || observedTicket.LaunchTicketDigest != claim.LaunchTicketDigest {
		return nil, fmt.Errorf("runner-recovery-ticket-binding-invalid")
	}
	runnerID := "absent"
	runnerName := "pq-release-" + claim.RunnerNonce
	if present, presenceErr := runnerStateExists(root, runnerStartPath); presenceErr != nil {
		return nil, presenceErr
	} else if present {
		started, _, startErr := loadRunnerStart(root, config, public, now, true)
		if startErr != nil || started.LaunchTicketDigest != claim.LaunchTicketDigest {
			return nil, fmt.Errorf("runner-recovery-start-binding-invalid")
		}
		runnerID, runnerName = started.RunnerID, started.RunnerName
	}
	admittedAt, _ := exactInt(claimPayload["admitted_at_epoch"], 1, 1<<62)
	payload := runnerStatePayload(claim, config, runnerRecoveryTerminalSchema, admittedAt)
	payload["consumed_at_epoch"] = json.Number(strconv.FormatInt(now.UTC().Unix(), 10))
	payload["disposition"] = "interrupted-runner-recovered"
	payload["recovery_observation_sha256"] = observationDigest
	payload["remote_runner_absent"] = true
	payload["runner_id"] = runnerID
	payload["runner_name"] = runnerName
	wire, err := signRunnerWire(payload, key, runnerTerminalSignatureDomain)
	if err != nil {
		return nil, err
	}
	defer zero(wire)
	if writeRunnerStateNoReplace(root, runnerTerminalPath(config), wire) != nil {
		return nil, fmt.Errorf("runner-recovery-terminal-publish-failed")
	}
	terminal, _, err := loadRunnerTerminal(root, config, public, now, true)
	if err != nil || convergeRunnerTerminalResidue(root, config, public, terminal, now) != nil {
		return nil, fmt.Errorf("runner-recovery-convergence-failed")
	}
	return terminal, nil
}

func admitRunnerLaunch(root string, now time.Time) (map[string]any, error) {
	lock, err := acquireRunnerTicketLock(root)
	if err != nil {
		return nil, err
	}
	defer closeRunnerTicketLock(lock)
	config, key, err := LoadConfig(root)
	if err != nil {
		return nil, err
	}
	defer config.release()
	defer zero(key)
	plan, err := LoadPlan(root, config)
	if err != nil {
		return nil, err
	}
	plan.release()
	public := key.Public().(ed25519.PublicKey)
	if err := validateInstalledRunnerPrerequisiteGate(root, config, public, "", now); err != nil {
		return nil, fmt.Errorf("runner-ticket-prerequisite-invalid")
	}
	if terminal, _, err := loadRunnerTerminal(root, config, public, now, true); err == nil {
		if convergeRunnerTerminalResidue(root, config, public, terminal, now) != nil {
			return nil, fmt.Errorf("runner-ticket-terminal-convergence-failed")
		}
		return nil, fmt.Errorf("runner-ticket-terminal-replay")
	}
	if existing, _, existingErr := loadRunnerClaim(root, config, public, now, false); existingErr == nil {
		payload, raw, ticketErr := readRunnerWire(root, RunnerLaunchTicketPath, 0o400, public, runnerTicketSignatureDomain)
		if ticketErr != nil {
			return nil, fmt.Errorf("runner-ticket-admission-recovery-failed")
		}
		observed, verifyErr := validateLaunchTicket(root, payload, raw, config, now, false)
		zero(raw)
		if verifyErr != nil || observed.LaunchTicketDigest != existing.LaunchTicketDigest {
			return nil, fmt.Errorf("runner-ticket-admission-recovery-failed")
		}
		return runnerTicketResult("already-admitted", existing), nil
	}
	if _, _, expiredErr := loadRunnerClaim(root, config, public, now, true); expiredErr == nil {
		return nil, fmt.Errorf("runner-ticket-recovery-required")
	}
	payload, raw, err := readRunnerWire(root, RunnerLaunchTicketPath, 0o400, public, runnerTicketSignatureDomain)
	if err != nil {
		return nil, fmt.Errorf("runner-launch-ticket-unavailable")
	}
	defer zero(raw)
	binding, err := validateLaunchTicket(root, payload, raw, config, now, true)
	if err != nil || verifyRunnerPackageBinding(root, config, raw) != nil {
		return nil, fmt.Errorf("runner-launch-ticket-invalid")
	}
	claimPayload := runnerStatePayload(binding, config, runnerClaimSchema, now.UTC().Unix())
	claim, err := signRunnerWire(claimPayload, key, runnerClaimSignatureDomain)
	if err != nil {
		return nil, err
	}
	defer zero(claim)
	if writeRunnerStateNoReplace(root, runnerClaimPath, claim) != nil {
		return nil, fmt.Errorf("runner-ticket-admission-commit-failed")
	}
	binding.ExecutionExpiresAt = now.UTC().Add(runnerExecutionTTL).Unix()
	return runnerTicketResult("admitted", binding), nil
}

func authorizeRunnerStart(root string, binding *runnerTicketBinding, runnerID, observationDigest string, session *runnerSessionObservation, now time.Time) error {
	if binding == nil || !decimal(runnerID) || !digestPattern.MatchString(observationDigest) || session == nil || session.Device < 1 || session.Inode < 1 || !digestPattern.MatchString(session.TreeDigest) || now.UTC().Unix() > binding.TicketExpiresAt {
		return fmt.Errorf("runner-start-input-invalid")
	}
	lock, err := acquireRunnerTicketLock(root)
	if err != nil {
		return err
	}
	defer closeRunnerTicketLock(lock)
	config, key, err := LoadConfig(root)
	if err != nil {
		return err
	}
	defer config.release()
	defer zero(key)
	public := key.Public().(ed25519.PublicKey)
	if err := validateInstalledRunnerPrerequisiteGate(root, config, public, binding.LaunchTicketDigest, now); err != nil {
		return fmt.Errorf("runner-start-prerequisite-invalid")
	}
	active, _, err := loadRunnerClaim(root, config, public, now, false)
	if err != nil || active.LaunchTicketDigest != binding.LaunchTicketDigest {
		return fmt.Errorf("runner-start-claim-invalid")
	}
	payload := runnerStatePayload(active, config, runnerStartSchema, now.UTC().Unix())
	payload["authorized_at_epoch"] = json.Number(strconv.FormatInt(now.UTC().Unix(), 10))
	payload["execution_expires_at_epoch"] = json.Number(strconv.FormatInt(now.UTC().Add(runnerExecutionTTL).Unix(), 10))
	payload["github_observation_sha256"] = observationDigest
	payload["runner_id"] = runnerID
	payload["runner_name"] = "pq-release-" + active.RunnerNonce
	payload["runner_session_device"] = json.Number(strconv.FormatUint(session.Device, 10))
	payload["runner_session_inode"] = json.Number(strconv.FormatUint(session.Inode, 10))
	payload["runner_session_tree_sha256"] = session.TreeDigest
	wire, err := signRunnerWire(payload, key, runnerStartSignatureDomain)
	if err != nil {
		return err
	}
	defer zero(wire)
	return writeRunnerStateNoReplace(root, runnerStartPath, wire)
}

func loadRunnerStart(root string, config *Config, public ed25519.PublicKey, now time.Time, allowExpired bool) (*runnerTicketBinding, map[string]any, error) {
	binding, payload, err := loadRunnerStateBinding(root, runnerStartPath, runnerStartSignatureDomain, config, public, now, allowExpired)
	if err != nil {
		return nil, nil, err
	}
	authorized, authorizedOK := exactInt(payload["authorized_at_epoch"], 1, 1<<62)
	expires, expiresOK := exactInt(payload["execution_expires_at_epoch"], 1, 1<<62)
	runnerID, runnerOK := exactString(payload["runner_id"])
	runnerName, nameOK := exactString(payload["runner_name"])
	observation, observationOK := exactString(payload["github_observation_sha256"])
	sessionDevice, deviceOK := exactInt(payload["runner_session_device"], 1, 1<<62)
	sessionInode, inodeOK := exactInt(payload["runner_session_inode"], 1, 1<<62)
	sessionTree, treeOK := exactString(payload["runner_session_tree_sha256"])
	keys := append(runnerCommonStateKeys(), "authorized_at_epoch", "execution_expires_at_epoch", "github_observation_sha256", "runner_id", "runner_name", "runner_session_device", "runner_session_inode", "runner_session_tree_sha256")
	if !hasKeys(payload, keys...) || payload["schema"] != runnerStartSchema || !authorizedOK || !expiresOK || expires-authorized != int64(runnerExecutionTTL/time.Second) || now.UTC().Unix() < authorized || (!allowExpired && now.UTC().Unix() > expires) || !runnerOK || !decimal(runnerID) || !nameOK || runnerName != "pq-release-"+binding.RunnerNonce || !observationOK || !digestPattern.MatchString(observation) || !deviceOK || !inodeOK || !treeOK || !digestPattern.MatchString(sessionTree) {
		return nil, nil, fmt.Errorf("runner-start-binding-invalid")
	}
	binding.RunnerID, binding.RunnerName, binding.ExecutionExpiresAt = runnerID, runnerName, expires
	binding.SessionDevice, binding.SessionInode, binding.SessionTreeDigest = uint64(sessionDevice), uint64(sessionInode), sessionTree
	return binding, payload, nil
}

func loadAuthorizedRunnerBinding(root string, config *Config, public ed25519.PublicKey, now time.Time) (*runnerTicketBinding, error) {
	binding, _, err := loadRunnerStart(root, config, public, now, false)
	return binding, err
}

func verifyRunnerTicketForRequest(root string, config *Config, request *workflowRequest, identity *Identity, now time.Time) (*runnerTicketBinding, error) {
	if config == nil || request == nil || identity == nil {
		return nil, fmt.Errorf("runner-ticket-request-input-invalid")
	}
	uid, gid := secureOwner(root)
	anchorRaw, err := secureRead(root, ReceiptAnchorPath, 0o444, uid, gid, 4096)
	if err != nil {
		return nil, fmt.Errorf("runner-ticket-anchor-unavailable")
	}
	public, keyID, err := parsePublicKey(anchorRaw)
	zero(anchorRaw)
	if err != nil || keyID != config.ReceiptAuthorityKeyID {
		zero(public)
		return nil, fmt.Errorf("runner-ticket-anchor-invalid")
	}
	defer zero(public)
	binding, err := loadAuthorizedRunnerBinding(root, config, public, now)
	if err != nil {
		// A consumed terminal deliberately remains usable for the second
		// release-run request of the same authenticated job.
		binding, _, err = loadRunnerTerminal(root, config, public, now, false)
	}
	if err == nil {
		err = validateInstalledRunnerPrerequisiteGate(root, config, public, binding.LaunchTicketDigest, now)
	}
	if err != nil || binding.RunID != request.DiagnosticRunID || binding.RunAttempt != request.DiagnosticRunAttempt || binding.RunnerLabel != request.DiagnosticRunnerLabel || binding.DispatchTicketDigest != request.RunnerTicketDigest || binding.JobID != identity.CheckRunID || binding.RunID != identity.RunID || binding.RunAttempt != identity.RunAttempt || binding.RunnerLabel != identity.RunnerLabel || binding.RunnerID != "" && binding.RunnerID != identity.RunnerID || identity.RunnerName != "pq-release-"+binding.RunnerNonce {
		return nil, fmt.Errorf("runner-ticket-identity-binding-invalid")
	}
	return binding, nil
}

func consumeRunnerTicket(root string, binding *runnerTicketBinding) error {
	if binding == nil || !digestPattern.MatchString(binding.LaunchTicketDigest) || !decimal(binding.RunnerID) || binding.RunnerName != "pq-release-"+binding.RunnerNonce {
		return fmt.Errorf("runner-ticket-consume-input-invalid")
	}
	lock, err := acquireRunnerTicketLock(root)
	if err != nil {
		return err
	}
	defer closeRunnerTicketLock(lock)
	config, key, err := LoadConfig(root)
	if err != nil {
		return err
	}
	defer config.release()
	defer zero(key)
	public := key.Public().(ed25519.PublicKey)
	if terminal, _, terminalErr := loadRunnerTerminal(root, config, public, authorityNow().UTC(), false); terminalErr == nil {
		if terminal.LaunchTicketDigest == binding.LaunchTicketDigest {
			return convergeRunnerTerminalResidue(root, config, public, terminal, authorityNow().UTC())
		}
		return fmt.Errorf("runner-ticket-terminal-conflict")
	}
	active, _, err := loadRunnerClaim(root, config, public, authorityNow().UTC(), false)
	if err != nil || active.LaunchTicketDigest != binding.LaunchTicketDigest {
		return fmt.Errorf("runner-ticket-consume-binding-invalid")
	}
	payload := runnerStatePayload(active, config, runnerTerminalSchema, authorityNow().UTC().Unix())
	payload["consumed_at_epoch"] = json.Number(strconv.FormatInt(authorityNow().UTC().Unix(), 10))
	payload["disposition"] = "preflight-admitted-and-consumed"
	payload["runner_id"] = binding.RunnerID
	payload["runner_name"] = binding.RunnerName
	wire, err := signRunnerWire(payload, key, runnerTerminalSignatureDomain)
	if err != nil {
		return err
	}
	defer zero(wire)
	if writeRunnerStateNoReplace(root, runnerTerminalPath(config), wire) != nil {
		return fmt.Errorf("runner-ticket-consume-commit-failed")
	}
	terminal, _, err := loadRunnerTerminal(root, config, public, authorityNow().UTC(), false)
	if err != nil || convergeRunnerTerminalResidue(root, config, public, terminal, authorityNow().UTC()) != nil {
		return fmt.Errorf("runner-ticket-consume-convergence-failed")
	}
	return nil
}

func runnerTicketResult(disposition string, binding *runnerTicketBinding) map[string]any {
	return map[string]any{
		"dispatch_ticket_sha256": binding.DispatchTicketDigest, "disposition": disposition, "execution_expires_at_epoch": json.Number(strconv.FormatInt(binding.ExecutionExpiresAt, 10)),
		"job_id": binding.JobID, "launch_ticket_sha256": binding.LaunchTicketDigest, "run_attempt": json.Number(strconv.FormatInt(binding.RunAttempt, 10)), "run_id": binding.RunID,
		"runner_image": binding.RunnerImage, "runner_label": binding.RunnerLabel, "schema": runnerTicketResultSchema, "ticket_expires_at_epoch": json.Number(strconv.FormatInt(binding.TicketExpiresAt, 10)), "version": json.Number("2"),
	}
}

func verifyRunnerStartForLauncher(root, runnerLabel, launchDigest, runnerID string, now time.Time) (map[string]any, error) {
	if !runnerLabelPattern.MatchString(runnerLabel) || !digestPattern.MatchString(launchDigest) || !decimal(runnerID) || now.IsZero() {
		return nil, fmt.Errorf("runner-launcher-start-input-invalid")
	}
	config, key, err := LoadConfig(root)
	if err != nil {
		return nil, err
	}
	defer config.release()
	defer zero(key)
	public := key.Public().(ed25519.PublicKey)
	binding, _, err := loadRunnerStart(root, config, public, now, false)
	if err == nil {
		err = validateInstalledRunnerPrerequisiteGate(root, config, public, binding.LaunchTicketDigest, now)
	}
	if err != nil || binding.RunnerLabel != runnerLabel || binding.LaunchTicketDigest != launchDigest || binding.RunnerID != runnerID || binding.RunnerName != "pq-release-"+binding.RunnerNonce {
		return nil, fmt.Errorf("runner-launcher-start-binding-invalid")
	}
	return map[string]any{
		"execution_expires_at_epoch": json.Number(strconv.FormatInt(binding.ExecutionExpiresAt, 10)),
		"launch_ticket_sha256":       launchDigest, "runner_id": runnerID, "runner_label": runnerLabel,
		"session_device": json.Number(strconv.FormatUint(binding.SessionDevice, 10)), "session_inode": json.Number(strconv.FormatUint(binding.SessionInode, 10)),
		"session_tree_sha256": binding.SessionTreeDigest,
		"schema":              runnerStartResultSchema, "version": json.Number("2"),
	}, nil
}

func runnerTicketCommand(command string, args []string, stdout io.Writer) error {
	euid, egid := runnerCommandIdentity()
	if euid != 0 || egid != 0 {
		return fmt.Errorf("runner-ticket-command-input-invalid")
	}
	var value map[string]any
	var err error
	switch command {
	case "runner-ticket-admit":
		if len(args) != 0 {
			return fmt.Errorf("runner-ticket-command-input-invalid")
		}
		value, err = admitRunnerLaunch("/", authorityNow().UTC())
	case "runner-start-verify":
		if len(args) != 3 {
			return fmt.Errorf("runner-ticket-command-input-invalid")
		}
		value, err = runnerStartVerification("/", args[0], args[1], args[2], authorityNow().UTC())
	default:
		return fmt.Errorf("runner-ticket-command-invalid")
	}
	if err != nil {
		return err
	}
	raw, err := canonicalJSON(value)
	if err != nil {
		return err
	}
	defer zero(raw)
	raw = append(raw, '\n')
	written, err := stdout.Write(raw)
	if err != nil || written != len(raw) {
		return fmt.Errorf("runner-ticket-result-write-failed")
	}
	return nil
}
