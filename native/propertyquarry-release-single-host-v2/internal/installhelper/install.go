package installhelper

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"propertyquarry.local/release-single-host-v2/internal/authority"
)

const (
	FixedPackagePath        = "/input/propertyquarry-release-single-host-v2.tar"
	FixedHostRoot           = "/host"
	FixedReceiptPath        = "/output/propertyquarry-release-single-host-v2-install-receipt.json"
	installReceiptDomain    = "propertyquarry.release-control.single-host-install-receipt-signature.v2\x00"
	installJournalDomain    = "propertyquarry.release-control.single-host-install-journal-signature.v2\x00"
	backupEncryptionKeyPath = "/home/tibor/.local/share/propertyquarry-backup-keys/propertyquarry-predeploy-backup-v2.key"
	preAdmissionPath        = "/etc/propertyquarry-release-single-host-v2/.backup-key-pre-admission.v2.json"
	preAdmissionPendingPath = "/etc/propertyquarry-release-single-host-v2/.backup-key-pre-admission.v2.pending"
	preAdmissionDomain      = "propertyquarry.release-control.single-host-pre-admission-signature.v2\x00"
	backupEncryptionKeyUID  = 1000
	backupEncryptionKeyGID  = 1000
	githubCredentialSource  = "/etc/propertyquarry-release-single-host-v2/github-api-token.cred"
	remoteBackupDirectory   = "/mnt/pcloud/propertyquarry/releases/backups/v2"
	remoteBackupOwnerUID    = 1000
	remoteBackupOwnerGID    = 1000
	linuxAMD64Renameat2     = 316
	authorityDrainTimeout   = 315 * time.Minute
)

var installJournalNamePattern = regexp.MustCompile(`^([0-9a-f]{32})-([0-9]{8})-([a-z][a-z0-9-]*)\.json$`)
var authorityInstanceNamePattern = regexp.MustCompile(`^propertyquarry-release-single-host-v2@[^[:space:]]+\.service$`)

var errInstallInterrupted = fmt.Errorf("install-simulated-interruption")
var errActivationAlreadyAborted = fmt.Errorf("activation-failed-and-aborted")

type Installer struct {
	HostRoot        string
	OwnerUID        uint32
	OwnerGID        uint32
	Activate        func() (*activationAttempt, error)
	Deactivate      func() error
	AbortActivation func() error
	// Interrupt is a test-only crash boundary. Returning true simulates abrupt
	// process loss: no compensating action is attempted by the current call.
	Interrupt                  func(point string) bool
	backupEncryptionKeyID      string
	backupEncryptionKeyCreated bool
	preAdmission               *preAdmissionState
	preAdmissionRequired       bool
	preAdmissionPendingRepair  bool
	preflightReplay            *journalReplay
}

type preAdmissionState struct {
	raw       []byte
	secret    []byte
	keyID     string
	createdAt int64
	path      string
}

func (installer *Installer) clearPreAdmissionMemory() {
	if installer == nil {
		return
	}
	if installer.preAdmission != nil {
		zero(installer.preAdmission.raw)
		zero(installer.preAdmission.secret)
	}
	installer.preAdmission = nil
	installer.preAdmissionPendingRepair = false
}

type activationAttempt struct {
	receipt             []byte
	challengeDigest     string
	challengeCreatedAt  int64
	canaryStartedAt     int64
	installedStateProof *authority.ActivationCanaryProof
}

type installTarget struct {
	path         string
	data         []byte
	mode         os.FileMode
	size         int64
	digest       string
	stage        string
	backup       string
	priorPresent bool
	priorMode    os.FileMode
	priorSize    int64
	priorDigest  string
}

type installReceiptState struct {
	disposition           string
	candidateInstalled    bool
	priorRestored         bool
	previousStateRestored bool
	socketActive          bool
	activationPerformed   bool
	activationSucceeded   bool
	activationCanary      *authority.ActivationCanaryProof
	recoveryPerformed     bool
	recoverySucceeded     bool
	rollbackPerformed     bool
	rollbackSucceeded     bool
	deactivationPerformed bool
	deactivationSucceeded bool
	reactivationPerformed bool
	reactivationSucceeded bool
	hadPrior              bool
}

type journalReplay struct {
	lastSequence                      int64
	lastAttempt                       int64
	lastEvent                         string
	targets                           []*installTarget
	backupKeyContinuityRecordObserved bool
}

type boundedCommandOutput struct {
	raw      []byte
	limit    int
	overflow bool
}

func (output *boundedCommandOutput) Write(raw []byte) (int, error) {
	remaining := output.limit - len(output.raw)
	if remaining < len(raw) {
		output.overflow = true
		if remaining > 0 {
			output.raw = append(output.raw, raw[:remaining]...)
		}
		return len(raw), nil
	}
	output.raw = append(output.raw, raw...)
	return len(raw), nil
}

func InstallFixedPackage() ([]byte, error) {
	if os.Geteuid() != 0 || os.Getegid() != 0 {
		return nil, fmt.Errorf("installer-root-required")
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
	installer := &Installer{HostRoot: FixedHostRoot, OwnerUID: 0, OwnerGID: 0, Activate: activateThroughChild, Deactivate: deactivateThroughChild, AbortActivation: abortActivationThroughChild}
	receipt, err := installer.Install(verified)
	if err != nil {
		return receipt, err
	}
	if !successfulInstallReceipt(receipt) {
		return receipt, fmt.Errorf("install-terminal-not-successful")
	}
	return receipt, nil
}

func successfulInstallReceipt(raw []byte) bool {
	wrapper, err := strictJSON(raw, maximumManifestBytes)
	if err != nil {
		return false
	}
	payload, ok := wrapper["payload"].(map[string]any)
	if !ok || payload["authority_installed"] != true || payload["systemd_socket_active"] != true || payload["activation_canary_verified"] != true || payload["activation_canary_receipt"] == nil {
		return false
	}
	disposition, ok := exactString(payload["disposition"])
	return ok && (disposition == "installed-and-active" || disposition == "already-installed")
}

func (installer *Installer) Install(verified *VerifiedPackage) ([]byte, error) {
	if installer == nil || verified == nil || !filepath.IsAbs(installer.HostRoot) || filepath.Clean(installer.HostRoot) != installer.HostRoot || len(verified.Files) == 0 {
		return nil, fmt.Errorf("install-input-invalid")
	}
	if installer.Activate == nil {
		installer.Activate = func() (*activationAttempt, error) { return nil, fmt.Errorf("install-activation-unavailable") }
	}
	if installer.Deactivate == nil {
		installer.Deactivate = func() error { return nil }
	}
	if installer.AbortActivation == nil {
		installer.AbortActivation = installer.Deactivate
	}
	installer.clearPreAdmissionMemory()
	installer.preAdmissionRequired = false
	installer.preflightReplay = nil
	defer installer.clearPreAdmissionMemory()
	hostBindingErr := installer.validateHostBinding(verified)
	if hostBindingErr != nil && !recoverableMutableHostBindingError(hostBindingErr) {
		return nil, hostBindingErr
	}
	receiptKey, err := parsePrivatePEM(verified.Files["/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.key"].Data)
	if err != nil {
		return nil, fmt.Errorf("install-receipt-key-invalid")
	}
	defer zero(receiptKey)
	lock, err := installer.acquireInstallLock()
	if err != nil {
		return nil, err
	}
	defer lock.Close()
	if installer.interrupted("after-lock") {
		return nil, errInstallInterrupted
	}
	if err := installer.enforceGenesisPrestate(verified, receiptKey.Public().(ed25519.PublicKey)); err != nil {
		return nil, err
	}
	journalResume := installer.preflightReplay != nil && installer.preflightReplay.lastEvent != ""
	preAdmissionResume := installer.preAdmission != nil || installer.preAdmissionPendingRepair
	if !journalResume && !preAdmissionResume {
		if err := installer.currentAdmissionError(verified); err != nil {
			return nil, err
		}
		if err := installer.enforceReceiptKeyContinuity(verified); err != nil {
			return nil, err
		}
	} else if preAdmissionResume && !journalResume {
		if err := installer.currentAdmissionError(verified); err != nil {
			if rollbackErr := installer.rollbackPreAdmission(); rollbackErr != nil {
				return nil, rollbackErr
			}
			return installer.signedReceipt(verified, receiptKey, installReceiptState{disposition: "pre-admission-rolled-back-readmission-required", recoveryPerformed: true, recoverySucceeded: true, previousStateRestored: true})
		}
		if err := installer.enforceReceiptKeyContinuity(verified); err != nil {
			return nil, err
		}
	}
	if installer.preAdmissionRequired {
		if err := installer.ensurePreAdmission(verified, receiptKey); err != nil {
			return nil, err
		}
		if installer.interrupted("after-pre-admission") {
			return nil, errInstallInterrupted
		}
	}
	backupKeyID, backupKeyCreated, err := installer.ensureBackupEncryptionKey()
	if err != nil {
		return nil, err
	}
	installer.backupEncryptionKeyID = backupKeyID
	installer.backupEncryptionKeyCreated = backupKeyCreated
	for index, directory := range []struct {
		path string
		mode os.FileMode
	}{
		{"/etc/propertyquarry-release-single-host-v2", 0o700},
		{"/usr/libexec/propertyquarry-release-control", 0o755},
		{"/usr/lib/propertyquarry-release-runner-v2", 0o755},
		{"/var/lib/propertyquarry-release-single-host-v2", 0o700},
		{"/var/lib/propertyquarry-release-single-host-v2/install-journal", 0o700},
	} {
		if err := installer.ensureDirectory(directory.path, directory.mode, fmt.Sprintf("install-directory-%d", index)); err != nil {
			return nil, err
		}
		if installer.interrupted(fmt.Sprintf("after-install-directory-%d", index)) {
			return nil, errInstallInterrupted
		}
	}
	journalDirectory, err := installer.hostPath("/var/lib/propertyquarry-release-single-host-v2/install-journal")
	if err != nil {
		return nil, err
	}
	txID := strings.TrimPrefix(verified.ArchiveDigest, "sha256:")[:32]
	replay, err := installer.replayJournal(journalDirectory, verified, receiptKey.Public().(ed25519.PublicKey), txID)
	if err != nil {
		return nil, err
	}
	eventSequence := replay.lastSequence
	attempt := replay.lastAttempt
	journalBroken := false
	appendEvent := func(event string, fields map[string]any) error {
		if journalBroken {
			return fmt.Errorf("install-journal-unavailable")
		}
		eventSequence++
		payload := map[string]any{
			"archive_digest": verified.ArchiveDigest, "attempt": json.Number(strconv.FormatInt(attempt, 10)),
			"backup_encryption_key_id": installer.backupEncryptionKeyID,
			"config_digest":            verified.ConfigDigest, "event": event,
			"host_machine_id_digest":   verified.HostMachineIDDigest,
			"package_authority_key_id": verified.PackageAuthorityKeyID,
			"receipt_authority_key_id": verified.ReceiptAuthorityKeyID,
			"release_generation":       json.Number(strconv.FormatInt(verified.ReleaseGeneration, 10)),
			"runtime_sha":              verified.RuntimeSHA, "schema": "propertyquarry.release-control.single-host-install-journal-event.v2",
			"sequence": json.Number(strconv.FormatInt(eventSequence, 10)), "transaction_id": txID,
			"version": json.Number("2"),
		}
		for key, value := range fields {
			payload[key] = value
		}
		wire, wireErr := signedJournalWire(payload, receiptKey, verified.ReceiptAuthorityKeyID)
		if wireErr != nil {
			return wireErr
		}
		defer zero(wire)
		name := fmt.Sprintf("%s-%08d-%s.json", txID, eventSequence, event)
		if writeErr := installer.writeJournalEventNoReplace(journalDirectory, name, wire); writeErr != nil {
			journalBroken = true
			return writeErr
		}
		return nil
	}
	recoveryState := installReceiptState{}
	var recoveryErr error
	if replay.lastEvent != "" && !terminalJournalEvent(replay.lastEvent) {
		recoveryState, recoveryErr = installer.recoverInterruptedAttempt(verified, replay.targets, replay.lastEvent, appendEvent)
		if recoveryErr == errInstallInterrupted {
			return nil, recoveryErr
		}
		if recoveryErr != nil {
			return installer.signedReceipt(verified, receiptKey, recoveryState)
		}
		if err := installer.completePreAdmission(); err != nil {
			return nil, err
		}
		if err := installer.currentAdmissionError(verified); err != nil {
			recoveryState.disposition = "recovered-prior-readmission-required"
			return installer.signedReceipt(verified, receiptKey, recoveryState)
		}
		if err := installer.enforceReceiptKeyContinuity(verified); err != nil {
			recoveryState.disposition = "recovered-prior-readmission-required"
			return installer.signedReceipt(verified, receiptKey, recoveryState)
		}
	}
	emitReceipt := func(state installReceiptState) ([]byte, error) {
		return installer.signedReceipt(verified, receiptKey, mergeRecoveryState(state, recoveryState))
	}
	if replay.lastEvent == "succeeded" {
		if err := installer.verifyInstalledFiles(verified); err != nil {
			return nil, fmt.Errorf("install-journal-success-state-invalid")
		}
		if err := installer.cleanupCommittedTargets(replay.targets); err != nil {
			if admissionErr := installer.currentAdmissionError(verified); admissionErr != nil {
				return emitReceipt(installReceiptState{disposition: "installed-cleanup-pending-readmission-required", candidateInstalled: true, hadPrior: targetsHadPrior(replay.targets)})
			}
			activationCanary, activationErr := installer.activateCandidate(verified)
			if activationErr != nil {
				return emitReceipt(installReceiptState{disposition: "activation-failed", candidateInstalled: true, activationPerformed: true, hadPrior: targetsHadPrior(replay.targets)})
			}
			state := successfulReceiptState("installed-active-cleanup-pending", targetsHadPrior(replay.targets), activationCanary)
			return emitReceipt(state)
		}
	}
	if journalResume {
		if err := installer.currentAdmissionError(verified); err != nil {
			state := installReceiptState{disposition: "recovery-complete-readmission-required", recoveryPerformed: true, recoverySucceeded: true}
			switch replay.lastEvent {
			case "succeeded":
				state.candidateInstalled = true
				state.hadPrior = targetsHadPrior(replay.targets)
			case "recovered-prior", "rolled-back", "deactivation-failed":
				state.previousStateRestored = true
				state.priorRestored = targetsHadPrior(replay.targets)
				state.hadPrior = targetsHadPrior(replay.targets)
			}
			return emitReceipt(state)
		}
		if err := installer.enforceReceiptKeyContinuity(verified); err != nil {
			return emitReceipt(installReceiptState{disposition: "recovery-complete-readmission-required", recoveryPerformed: true, recoverySucceeded: true})
		}
	}
	idempotent, hadPrior, err := installer.enforceInstalledGeneration(verified)
	if err != nil {
		return nil, err
	}
	if hadPrior && !replay.backupKeyContinuityRecordObserved {
		return nil, fmt.Errorf("install-backup-key-continuity-record-missing")
	}
	if idempotent {
		if err := installer.verifyInstalledFiles(verified); err != nil {
			return nil, err
		}
		activationCanary, err := installer.activateCandidate(verified)
		if err != nil {
			return emitReceipt(installReceiptState{disposition: "activation-failed", candidateInstalled: true, activationPerformed: true, hadPrior: hadPrior})
		}
		return emitReceipt(installReceiptState{disposition: "already-installed", candidateInstalled: true, socketActive: true, activationPerformed: true, activationSucceeded: true, activationCanary: activationCanary, hadPrior: hadPrior})
	}
	attempt++
	targets, err := installer.prepareTargets(verified, txID)
	if err != nil {
		return nil, err
	}
	defer cleanupTargetData(targets)
	if !hadPrior && targetsContainPrior(targets) {
		return nil, fmt.Errorf("install-genesis-targets-present")
	}
	if err := appendEvent("admitted", map[string]any{"had_prior_install": hadPrior, "targets": targetSnapshotsJSON(targets)}); err != nil {
		return nil, err
	}
	if installer.interrupted("after-admitted-before-pre-admission-clear") {
		return nil, errInstallInterrupted
	}
	if err := installer.completePreAdmission(); err != nil {
		return nil, err
	}
	if installer.interrupted("after-admitted") {
		return nil, errInstallInterrupted
	}
	if err := installer.stageTargets(targets); err != nil {
		return nil, err
	}
	if err := appendEvent("staged", map[string]any{"file_count": json.Number(strconv.Itoa(len(targets)))}); err != nil {
		return nil, err
	}
	if installer.interrupted("after-staged") {
		return nil, errInstallInterrupted
	}
	if hadPrior {
		if err := installer.Deactivate(); err != nil {
			installer.removeStages(targets)
			reactivationErr := installer.reactivateCurrent()
			_ = appendEvent("deactivation-failed", map[string]any{"reactivation_succeeded": reactivationErr == nil})
			return emitReceipt(installReceiptState{disposition: "deactivation-failed", priorRestored: true, previousStateRestored: true, socketActive: reactivationErr == nil, deactivationPerformed: true, reactivationPerformed: true, reactivationSucceeded: reactivationErr == nil, hadPrior: true})
		}
		if err := appendEvent("deactivated", map[string]any{"systemd_socket_active": false}); err != nil {
			reactivationErr := installer.reactivateCurrent()
			installer.removeStages(targets)
			return emitReceipt(installReceiptState{disposition: "deactivation-journal-failed", priorRestored: true, previousStateRestored: true, socketActive: reactivationErr == nil, deactivationPerformed: true, deactivationSucceeded: true, reactivationPerformed: true, reactivationSucceeded: reactivationErr == nil, hadPrior: true})
		}
		if installer.interrupted("after-deactivated") {
			return nil, errInstallInterrupted
		}
	}
	mutationErr := installer.commitTargets(targets, appendEvent)
	if mutationErr == errInstallInterrupted {
		return nil, mutationErr
	}
	if mutationErr == nil {
		mutationErr = appendEvent("files-installed", map[string]any{"file_count": json.Number(strconv.Itoa(len(targets)))})
	}
	if mutationErr == nil {
		if err := installer.verifyInstalledFiles(verified); err != nil || !installer.candidateTargetsComplete(targets) {
			mutationErr = fmt.Errorf("install-candidate-proof-failed")
		}
	}
	activationAttempted := false
	activationSucceeded := false
	var activationCanary *authority.ActivationCanaryProof
	if mutationErr == nil {
		activationAttempted = true
		activationCanary, mutationErr = installer.activateCandidate(verified)
		activationSucceeded = mutationErr == nil
	}
	if mutationErr == nil {
		mutationErr = appendEvent("activated", map[string]any{"activation_canary_challenge_sha256": activationCanary.ChallengeDigest, "activation_canary_receipt_digest": activationCanary.ReceiptDigest, "activation_canary_unit_sha256": activationCanary.UnitDigest, "activation_canary_verified": true, "activation_canary_verified_at": json.Number(strconv.FormatInt(activationCanary.VerifiedAt, 10)), "systemd_socket_active": true})
	}
	if mutationErr != nil {
		state := installer.rollbackFailedInstall(targets, hadPrior, activationAttempted, activationSucceeded, activationCanary, appendEvent)
		return emitReceipt(state)
	}
	if installer.interrupted("after-activated") {
		return nil, errInstallInterrupted
	}
	if err := appendEvent("succeeded", map[string]any{"candidate_authority_installed": true, "systemd_socket_active": true}); err != nil {
		state := installer.rollbackFailedInstall(targets, hadPrior, true, true, activationCanary, appendEvent)
		if state.disposition == "rolled-back" {
			state.disposition = "success-journal-failed-rolled-back"
		}
		return emitReceipt(state)
	}
	if installer.interrupted("after-succeeded") {
		return nil, errInstallInterrupted
	}
	if err := installer.cleanupCommittedTargets(targets); err != nil {
		return emitReceipt(successfulReceiptState("installed-active-cleanup-pending", hadPrior, activationCanary))
	}
	return emitReceipt(successfulReceiptState("installed-and-active", hadPrior, activationCanary))
}

func (installer *Installer) inspectPreAdmission(verified *VerifiedPackage, receiptPublic ed25519.PublicKey) (bool, bool, error) {
	finalPath, err := installer.hostPath(preAdmissionPath)
	if err != nil {
		return false, false, err
	}
	pendingPath, err := installer.hostPath(preAdmissionPendingPath)
	if err != nil {
		return false, false, err
	}
	_, finalErr := os.Lstat(finalPath)
	_, pendingErr := os.Lstat(pendingPath)
	finalPresent := finalErr == nil
	pendingPresent := pendingErr == nil
	if (!finalPresent && !os.IsNotExist(finalErr)) || (!pendingPresent && !os.IsNotExist(pendingErr)) || (finalPresent && pendingPresent) {
		return false, false, fmt.Errorf("install-pre-admission-state-invalid")
	}
	if finalPresent {
		state, err := installer.loadPreAdmission(finalPath, verified, receiptPublic)
		if err != nil {
			return false, false, err
		}
		installer.preAdmission = state
		return true, false, nil
	}
	if !pendingPresent {
		return false, false, nil
	}
	state, loadErr := installer.loadPreAdmission(pendingPath, verified, receiptPublic)
	if loadErr == nil {
		installer.preAdmission = state
		return true, false, nil
	}
	info, statErr := os.Lstat(pendingPath)
	// A process loss before the explicit chmod or before the complete write can
	// leave a narrower-mode, partial fixed-name stage.  It has no authenticated
	// authority yet, so only this exact regular single-link file may be removed
	// and regenerated from a new signed pre-admission intent.
	if statErr != nil || !validOwnedRegular(info, installer.OwnerUID, installer.OwnerGID) || info.Mode().Perm()&^os.FileMode(0o400) != 0 || info.Size() < 0 || info.Size() > maximumManifestBytes {
		return false, false, fmt.Errorf("install-pre-admission-pending-invalid")
	}
	return false, true, nil
}

func (installer *Installer) loadPreAdmission(path string, verified *VerifiedPackage, receiptPublic ed25519.PublicKey) (*preAdmissionState, error) {
	raw, err := readExactFile(path, 0o400, installer.OwnerUID, installer.OwnerGID, maximumManifestBytes)
	if err != nil {
		return nil, fmt.Errorf("install-pre-admission-unavailable")
	}
	wrapper, err := strictJSON(raw, maximumManifestBytes)
	if err != nil || !hasKeys(wrapper, "payload", "signature", "signature_key_id") {
		zero(raw)
		return nil, fmt.Errorf("install-pre-admission-wire-invalid")
	}
	canonicalWrapper, canonicalErr := canonicalJSON(wrapper)
	if canonicalErr != nil {
		zero(raw)
		return nil, fmt.Errorf("install-pre-admission-wire-invalid")
	}
	if !bytes.Equal(raw, canonicalWrapper) {
		zero(raw)
		zero(canonicalWrapper)
		return nil, fmt.Errorf("install-pre-admission-wire-noncanonical")
	}
	zero(canonicalWrapper)
	payload, payloadOK := wrapper["payload"].(map[string]any)
	signatureText, signatureOK := exactString(wrapper["signature"])
	keyID, keyOK := exactString(wrapper["signature_key_id"])
	if !payloadOK || !signatureOK || !keyOK || keyID != verified.ReceiptAuthorityKeyID || !hasKeys(payload,
		"archive_digest", "backup_encryption_key", "backup_encryption_key_id", "config_digest", "created_at_epoch",
		"host_machine_id_digest", "package_authority_key_id", "plan_digest", "receipt_authority_key_id",
		"release_generation", "runtime_sha", "schema", "transaction_id", "valid_until_epoch", "version", "workflow_sha") {
		zero(raw)
		return nil, fmt.Errorf("install-pre-admission-binding-invalid")
	}
	payloadRaw, canonicalErr := canonicalJSON(payload)
	signature, signatureErr := base64.RawURLEncoding.DecodeString(signatureText)
	if canonicalErr != nil || signatureErr != nil || len(signature) != ed25519.SignatureSize || !ed25519.Verify(receiptPublic, framed(preAdmissionDomain, payloadRaw), signature) {
		zero(raw)
		zero(payloadRaw)
		zero(signature)
		return nil, fmt.Errorf("install-pre-admission-signature-invalid")
	}
	zero(payloadRaw)
	zero(signature)
	schema, _ := exactString(payload["schema"])
	version, versionOK := exactInt(payload["version"], 2, 2)
	archiveDigest, _ := exactString(payload["archive_digest"])
	configDigest, _ := exactString(payload["config_digest"])
	planDigest, _ := exactString(payload["plan_digest"])
	hostDigest, _ := exactString(payload["host_machine_id_digest"])
	packageKeyID, _ := exactString(payload["package_authority_key_id"])
	receiptKeyID, _ := exactString(payload["receipt_authority_key_id"])
	runtimeSHA, _ := exactString(payload["runtime_sha"])
	workflowSHA, _ := exactString(payload["workflow_sha"])
	txID, _ := exactString(payload["transaction_id"])
	keyText, _ := exactString(payload["backup_encryption_key"])
	backupKeyID, _ := exactString(payload["backup_encryption_key_id"])
	generation, generationOK := exactInt(payload["release_generation"], 1, 1<<62)
	createdAt, createdOK := exactInt(payload["created_at_epoch"], 1, 1<<62)
	validUntil, validUntilOK := exactInt(payload["valid_until_epoch"], 1, 1<<62)
	secret := make([]byte, 32)
	decoded, decodeErr := hex.Decode(secret, []byte(keyText))
	if schema != "propertyquarry.release-control.single-host-pre-admission.v2" || !versionOK || version != 2 ||
		archiveDigest != verified.ArchiveDigest || configDigest != verified.ConfigDigest || planDigest != verified.PlanDigest || hostDigest != verified.HostMachineIDDigest ||
		packageKeyID != verified.PackageAuthorityKeyID || receiptKeyID != verified.ReceiptAuthorityKeyID || runtimeSHA != verified.RuntimeSHA || workflowSHA != verified.WorkflowSHA ||
		txID != strings.TrimPrefix(verified.ArchiveDigest, "sha256:")[:32] || !generationOK || generation != verified.ReleaseGeneration ||
		!createdOK || createdAt < verified.TransactionStartedAt || createdAt > verified.MaterializationValidUntil || !validUntilOK || validUntil != verified.MaterializationValidUntil ||
		len(keyText) != 64 || decodeErr != nil || decoded != len(secret) || backupKeyID != digest(secret) {
		zero(raw)
		zero(secret)
		return nil, fmt.Errorf("install-pre-admission-binding-invalid")
	}
	return &preAdmissionState{raw: raw, secret: secret, keyID: backupKeyID, createdAt: createdAt, path: path}, nil
}

func (installer *Installer) ensurePreAdmission(verified *VerifiedPackage, receiptKey ed25519.PrivateKey) error {
	if !installer.preAdmissionRequired {
		return nil
	}
	finalPath, _ := installer.hostPath(preAdmissionPath)
	pendingPath, _ := installer.hostPath(preAdmissionPendingPath)
	if installer.preAdmissionPendingRepair {
		if installer.preAdmission != nil {
			return fmt.Errorf("install-pre-admission-repair-state-invalid")
		}
		if err := os.Remove(pendingPath); err != nil {
			return fmt.Errorf("install-pre-admission-pending-remove-failed")
		}
		if err := syncDirectory(filepath.Dir(pendingPath)); err != nil {
			return err
		}
		installer.preAdmissionPendingRepair = false
	}
	if installer.preAdmission != nil {
		if installer.preAdmission.path == pendingPath {
			if err := renameNoReplace(pendingPath, finalPath); err != nil {
				return fmt.Errorf("install-pre-admission-publish-failed")
			}
			if err := syncDirectory(filepath.Dir(finalPath)); err != nil {
				return err
			}
			installer.preAdmission.path = finalPath
		}
		return nil
	}
	secret := make([]byte, 32)
	if _, err := io.ReadFull(rand.Reader, secret); err != nil {
		zero(secret)
		return fmt.Errorf("install-pre-admission-random-failed")
	}
	createdAt := time.Now().UTC().Unix()
	keyID := digest(secret)
	keyText := make([]byte, hex.EncodedLen(len(secret)))
	hex.Encode(keyText, secret)
	payload := map[string]any{
		"archive_digest": verified.ArchiveDigest, "backup_encryption_key": string(keyText), "backup_encryption_key_id": keyID,
		"config_digest": verified.ConfigDigest, "created_at_epoch": json.Number(strconv.FormatInt(createdAt, 10)),
		"host_machine_id_digest": verified.HostMachineIDDigest, "package_authority_key_id": verified.PackageAuthorityKeyID,
		"plan_digest": verified.PlanDigest, "receipt_authority_key_id": verified.ReceiptAuthorityKeyID,
		"release_generation": json.Number(strconv.FormatInt(verified.ReleaseGeneration, 10)), "runtime_sha": verified.RuntimeSHA,
		"schema": "propertyquarry.release-control.single-host-pre-admission.v2", "transaction_id": strings.TrimPrefix(verified.ArchiveDigest, "sha256:")[:32],
		"valid_until_epoch": json.Number(strconv.FormatInt(verified.MaterializationValidUntil, 10)), "version": json.Number("2"), "workflow_sha": verified.WorkflowSHA,
	}
	zero(keyText)
	payloadRaw, err := canonicalJSON(payload)
	if err != nil {
		zero(secret)
		return err
	}
	signature := ed25519.Sign(receiptKey, framed(preAdmissionDomain, payloadRaw))
	zero(payloadRaw)
	wire, err := canonicalJSON(map[string]any{"payload": payload, "signature": base64.RawURLEncoding.EncodeToString(signature), "signature_key_id": verified.ReceiptAuthorityKeyID})
	zero(signature)
	if err != nil {
		zero(secret)
		return err
	}
	if err := installer.writeNoReplace(pendingPath, wire, 0o400, installer.OwnerUID, installer.OwnerGID, "pre-admission-stage"); err != nil {
		zero(secret)
		zero(wire)
		return fmt.Errorf("install-pre-admission-stage-failed")
	}
	installer.preAdmission = &preAdmissionState{raw: wire, secret: secret, keyID: keyID, createdAt: createdAt, path: pendingPath}
	if installer.interrupted("after-pre-admission-pending") {
		return errInstallInterrupted
	}
	if err := renameNoReplace(pendingPath, finalPath); err != nil {
		return fmt.Errorf("install-pre-admission-publish-failed")
	}
	if installer.interrupted("pre-admission-publish-after-rename") {
		return errInstallInterrupted
	}
	if err := syncDirectory(filepath.Dir(finalPath)); err != nil {
		return err
	}
	if installer.interrupted("pre-admission-publish-after-parent-fsync") {
		return errInstallInterrupted
	}
	installer.preAdmission.path = finalPath
	return nil
}

func (installer *Installer) completePreAdmission() error {
	if installer.preAdmission == nil || !installer.preAdmissionRequired {
		return nil
	}
	finalPath, _ := installer.hostPath(preAdmissionPath)
	raw, err := readExactFile(finalPath, 0o400, installer.OwnerUID, installer.OwnerGID, maximumManifestBytes)
	if err != nil || !bytes.Equal(raw, installer.preAdmission.raw) {
		zero(raw)
		return fmt.Errorf("install-pre-admission-final-invalid")
	}
	zero(raw)
	if err := os.Remove(finalPath); err != nil {
		return fmt.Errorf("install-pre-admission-remove-failed")
	}
	if err := syncDirectory(filepath.Dir(finalPath)); err != nil {
		return err
	}
	installer.preAdmissionRequired = false
	return nil
}

func (installer *Installer) rollbackPreAdmission() error {
	keyPath, _ := installer.hostPath(backupEncryptionKeyPath)
	keyDirectory := filepath.Dir(keyPath)
	for _, path := range []string{keyPath + ".pending", keyPath} {
		info, err := os.Lstat(path)
		if os.IsNotExist(err) {
			continue
		}
		if err != nil || installer.preAdmission == nil {
			return fmt.Errorf("install-pre-admission-rollback-key-invalid")
		}
		if path == keyPath {
			keyID, readErr := installer.readExistingBackupEncryptionKey()
			if readErr != nil || keyID != installer.preAdmission.keyID {
				return fmt.Errorf("install-pre-admission-rollback-key-invalid")
			}
		} else {
			metadata, ok := info.Sys().(*syscall.Stat_t)
			if !ok || !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 || metadata.Nlink != 1 || (metadata.Uid != installer.OwnerUID && metadata.Uid != backupEncryptionKeyUID) || info.Size() < 0 || info.Size() > 65 {
				return fmt.Errorf("install-pre-admission-rollback-key-invalid")
			}
		}
		if err := os.Remove(path); err != nil {
			return fmt.Errorf("install-pre-admission-rollback-key-remove-failed")
		}
		if err := syncDirectory(keyDirectory); err != nil {
			return err
		}
	}
	if info, err := os.Lstat(keyDirectory); err == nil {
		if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("install-pre-admission-rollback-key-directory-invalid")
		}
		if err := os.Remove(keyDirectory); err != nil {
			return fmt.Errorf("install-pre-admission-rollback-key-directory-remove-failed")
		}
		if err := syncDirectory(filepath.Dir(keyDirectory)); err != nil {
			return err
		}
	} else if !os.IsNotExist(err) {
		return fmt.Errorf("install-pre-admission-rollback-key-directory-invalid")
	}
	for _, absolute := range []string{
		"/var/lib/propertyquarry-release-single-host-v2/install-journal",
		"/var/lib/propertyquarry-release-single-host-v2",
		"/usr/lib/propertyquarry-release-runner-v2",
		"/usr/libexec/propertyquarry-release-control",
	} {
		path, _ := installer.hostPath(absolute)
		if err := os.Remove(path); err == nil {
			if syncErr := syncDirectory(filepath.Dir(path)); syncErr != nil {
				return syncErr
			}
		} else if !os.IsNotExist(err) {
			return fmt.Errorf("install-pre-admission-rollback-directory-remove-failed")
		}
	}
	finalPath, _ := installer.hostPath(preAdmissionPath)
	pendingPath, _ := installer.hostPath(preAdmissionPendingPath)
	for _, path := range []string{pendingPath, finalPath} {
		if _, err := os.Lstat(path); os.IsNotExist(err) {
			continue
		} else if err != nil {
			return fmt.Errorf("install-pre-admission-rollback-record-invalid")
		}
		if err := os.Remove(path); err != nil {
			return fmt.Errorf("install-pre-admission-rollback-record-remove-failed")
		}
		if err := syncDirectory(filepath.Dir(path)); err != nil {
			return err
		}
	}
	installer.preAdmissionRequired = false
	return nil
}

func (installer *Installer) validateRecoverableGenesisDirectories() error {
	specifications := []struct {
		path    string
		mode    os.FileMode
		allowed map[string]bool
	}{
		{"/usr/libexec/propertyquarry-release-control", 0o755, map[string]bool{}},
		{"/usr/lib/propertyquarry-release-runner-v2", 0o755, map[string]bool{}},
		{"/var/lib/propertyquarry-release-single-host-v2", 0o700, map[string]bool{"install-journal": true}},
		{"/var/lib/propertyquarry-release-single-host-v2/install-journal", 0o700, map[string]bool{}},
	}
	for _, specification := range specifications {
		path, _ := installer.hostPath(specification.path)
		info, err := os.Lstat(path)
		if os.IsNotExist(err) {
			continue
		}
		metadata, ok := info.Sys().(*syscall.Stat_t)
		if err != nil || !ok || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&^specification.mode != 0 || metadata.Uid != installer.OwnerUID || metadata.Gid != installer.OwnerGID {
			return fmt.Errorf("install-genesis-recovery-directory-invalid")
		}
		if info.Mode().Perm() != specification.mode {
			if err := os.Chmod(path, specification.mode); err != nil {
				return fmt.Errorf("install-genesis-recovery-directory-repair-failed")
			}
			if err := syncDirectory(filepath.Dir(path)); err != nil {
				return err
			}
		}
		entries, err := os.ReadDir(path)
		if err != nil {
			return fmt.Errorf("install-genesis-recovery-directory-invalid")
		}
		for _, entry := range entries {
			if !specification.allowed[entry.Name()] || !entry.IsDir() {
				return fmt.Errorf("install-genesis-recovery-directory-content-invalid")
			}
		}
	}
	return nil
}

func (installer *Installer) validateRecoverableBackupKey() error {
	keyPath, _ := installer.hostPath(backupEncryptionKeyPath)
	keyDirectory := filepath.Dir(keyPath)
	info, err := os.Lstat(keyDirectory)
	if os.IsNotExist(err) {
		return nil
	}
	if installer.preAdmission == nil {
		return fmt.Errorf("install-genesis-recovery-backup-key-unbound")
	}
	metadata, ok := info.Sys().(*syscall.Stat_t)
	if err != nil || !ok || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&^os.FileMode(0o700) != 0 || (metadata.Uid != backupEncryptionKeyUID && metadata.Uid != installer.OwnerUID) || (metadata.Uid == backupEncryptionKeyUID && metadata.Gid != backupEncryptionKeyGID) {
		return fmt.Errorf("install-genesis-recovery-backup-key-directory-invalid")
	}
	if info.Mode().Perm() != 0o700 || metadata.Uid != backupEncryptionKeyUID || metadata.Gid != backupEncryptionKeyGID {
		if err := os.Chown(keyDirectory, backupEncryptionKeyUID, backupEncryptionKeyGID); err != nil || os.Chmod(keyDirectory, 0o700) != nil {
			return fmt.Errorf("install-genesis-recovery-backup-key-directory-repair-failed")
		}
		if err := syncDirectory(filepath.Dir(keyDirectory)); err != nil {
			return err
		}
	}
	entries, err := os.ReadDir(keyDirectory)
	if err != nil || len(entries) > 1 {
		return fmt.Errorf("install-genesis-recovery-backup-key-state-invalid")
	}
	if len(entries) == 0 {
		return nil
	}
	name := entries[0].Name()
	if name != filepath.Base(keyPath) && name != filepath.Base(keyPath)+".pending" {
		return fmt.Errorf("install-genesis-recovery-backup-key-state-invalid")
	}
	entryPath := filepath.Join(keyDirectory, name)
	entryInfo, err := os.Lstat(entryPath)
	if err != nil {
		return fmt.Errorf("install-genesis-recovery-backup-key-state-invalid")
	}
	entryMetadata, metadataOK := entryInfo.Sys().(*syscall.Stat_t)
	if !metadataOK || !entryInfo.Mode().IsRegular() || entryInfo.Mode()&os.ModeSymlink != 0 || entryMetadata.Nlink != 1 || (entryMetadata.Uid != backupEncryptionKeyUID && entryMetadata.Uid != installer.OwnerUID) || entryInfo.Mode().Perm()&^os.FileMode(0o600) != 0 || entryInfo.Size() < 0 || entryInfo.Size() > 65 {
		return fmt.Errorf("install-genesis-recovery-backup-key-state-invalid")
	}
	if name == filepath.Base(keyPath) {
		keyID, err := installer.readExistingBackupEncryptionKey()
		if err != nil || keyID != installer.preAdmission.keyID {
			return fmt.Errorf("install-genesis-recovery-backup-key-invalid")
		}
	}
	return nil
}

func (installer *Installer) enforceGenesisPrestate(verified *VerifiedPackage, receiptPublic ed25519.PublicKey) error {
	if installer == nil || verified == nil {
		return fmt.Errorf("install-genesis-prestate-input-invalid")
	}
	manifestPath, _ := installer.hostPath("/etc/propertyquarry-release-single-host-v2/package-manifest.v2.json")
	signaturePath, _ := installer.hostPath("/etc/propertyquarry-release-single-host-v2/package-manifest.v2.sig")
	_, manifestErr := os.Lstat(manifestPath)
	_, signatureErr := os.Lstat(signaturePath)
	preAdmissionPresent, pendingRepair, preAdmissionErr := installer.inspectPreAdmission(verified, receiptPublic)
	if preAdmissionErr != nil {
		return preAdmissionErr
	}
	installer.preAdmissionRequired = preAdmissionPresent || pendingRepair
	installer.preAdmissionPendingRepair = pendingRepair
	journalDirectory, journalPathErr := installer.hostPath("/var/lib/propertyquarry-release-single-host-v2/install-journal")
	if journalPathErr != nil {
		return journalPathErr
	}
	if _, journalErr := os.Lstat(journalDirectory); journalErr == nil {
		entries, readErr := os.ReadDir(journalDirectory)
		if readErr != nil {
			return fmt.Errorf("install-genesis-recovery-install-journal-invalid")
		}
		if len(entries) == 0 && installer.preAdmission != nil && !pendingRepair {
			return installer.validateGenesisScaffold(verified, true)
		}
		backupKeyID, keyErr := installer.readExistingBackupEncryptionKey()
		if keyErr != nil {
			return fmt.Errorf("install-genesis-recovery-backup-key-invalid")
		}
		installer.backupEncryptionKeyID = backupKeyID
		installer.backupEncryptionKeyCreated = false
		txID := strings.TrimPrefix(verified.ArchiveDigest, "sha256:")[:32]
		replay, replayErr := installer.replayJournal(journalDirectory, verified, receiptPublic, txID)
		if replayErr != nil {
			return replayErr
		}
		if replay.lastEvent == "" && installer.preAdmission != nil && !pendingRepair {
			entriesAfterRecovery, readAfterErr := os.ReadDir(journalDirectory)
			if readAfterErr != nil {
				return fmt.Errorf("install-genesis-recovery-install-journal-invalid")
			}
			if len(entriesAfterRecovery) == 0 {
				return installer.validateGenesisScaffold(verified, true)
			}
		}
		if !replay.backupKeyContinuityRecordObserved {
			return fmt.Errorf("install-genesis-recovery-install-journal-invalid")
		}
		installer.preflightReplay = replay
		manifestPresent := manifestErr == nil
		signaturePresent := signatureErr == nil
		if (!manifestPresent || !signaturePresent) && replay.lastEvent == "" {
			return fmt.Errorf("install-genesis-recovery-manifest-state-invalid")
		}
		activeRecovery := replay.lastEvent != "" && !terminalJournalEvent(replay.lastEvent)
		if manifestPresent && signaturePresent && !activeRecovery {
			if err := installer.enforceReceiptKeyContinuity(verified); err != nil {
				return err
			}
			if _, hadPrior, err := installer.enforceInstalledGeneration(verified); err != nil || !hadPrior {
				if err != nil {
					return err
				}
				return fmt.Errorf("install-genesis-recovery-installed-state-invalid")
			}
		} else if (!os.IsNotExist(manifestErr) && manifestErr != nil) || (!os.IsNotExist(signatureErr) && signatureErr != nil) {
			return fmt.Errorf("install-genesis-recovery-manifest-state-invalid")
		}
		return nil
	} else if !os.IsNotExist(journalErr) {
		return fmt.Errorf("install-genesis-recovery-state-invalid")
	}
	if installer.preAdmissionRequired {
		if manifestErr == nil || signatureErr == nil || !os.IsNotExist(manifestErr) || !os.IsNotExist(signatureErr) {
			return fmt.Errorf("install-genesis-pre-admission-manifest-state-invalid")
		}
		return installer.validateGenesisScaffold(verified, true)
	}
	if manifestErr == nil || signatureErr == nil || !os.IsNotExist(manifestErr) || !os.IsNotExist(signatureErr) {
		return fmt.Errorf("install-genesis-manifest-state-invalid")
	}
	if err := installer.validateGenesisScaffold(verified, false); err != nil {
		return err
	}
	installer.preAdmissionRequired = true
	return nil
}

func (installer *Installer) validateGenesisScaffold(verified *VerifiedPackage, recovering bool) error {
	config, err := strictJSON(verified.Files["/etc/propertyquarry-release-single-host-v2/authority.v2.json"].Data, maximumManifestBytes)
	if err != nil {
		return fmt.Errorf("install-genesis-config-invalid")
	}
	predecessor, _ := exactString(config["predecessor_runtime_sha"])
	runnerUID, runnerUIDOK := exactInt(config["allowed_runner_uid"], 1999, 1999)
	runnerGID, runnerGIDOK := exactInt(config["allowed_runner_gid"], 1999, 1999)
	if verified.ReleaseGeneration != 1 || predecessor != "genesis" || !runnerUIDOK || runnerUID != 1999 || !runnerGIDOK || runnerGID != 1999 {
		return fmt.Errorf("install-genesis-binding-invalid")
	}
	authorityDirectory, _ := installer.hostPath("/etc/propertyquarry-release-single-host-v2")
	info, err := os.Lstat(authorityDirectory)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm() != 0o700 {
		return fmt.Errorf("install-genesis-authority-directory-invalid")
	}
	metadata, ok := info.Sys().(*syscall.Stat_t)
	if !ok || metadata.Uid != installer.OwnerUID || metadata.Gid != installer.OwnerGID {
		return fmt.Errorf("install-genesis-authority-directory-invalid")
	}
	entries, err := os.ReadDir(authorityDirectory)
	expectedEntries := map[string]bool{"github-api-token.cred": false}
	if recovering {
		if installer.preAdmission != nil {
			expectedEntries[filepath.Base(installer.preAdmission.path)] = false
		} else if installer.preAdmissionPendingRepair {
			expectedEntries[filepath.Base(preAdmissionPendingPath)] = false
		}
	}
	if err != nil || len(entries) != len(expectedEntries) {
		return fmt.Errorf("install-genesis-authority-state-invalid")
	}
	for _, entry := range entries {
		seen, ok := expectedEntries[entry.Name()]
		if !ok || seen || entry.IsDir() {
			return fmt.Errorf("install-genesis-authority-state-invalid")
		}
		expectedEntries[entry.Name()] = true
	}
	credentialPath := filepath.Join(authorityDirectory, "github-api-token.cred")
	credential, err := readExactFile(credentialPath, 0o400, installer.OwnerUID, installer.OwnerGID, 64*1024)
	if err != nil || len(credential) < 32 {
		zero(credential)
		return fmt.Errorf("install-genesis-credential-invalid")
	}
	allZero := true
	for _, value := range credential {
		if value != 0 {
			allZero = false
			break
		}
	}
	zero(credential)
	if allZero {
		return fmt.Errorf("install-genesis-credential-invalid")
	}
	legacyAnchorPath, _ := installer.hostPath("/etc/propertyquarry-release-control-v2/package-authority-v2.pem")
	legacyAnchor, err := readExactFile(legacyAnchorPath, 0o444, installer.OwnerUID, installer.OwnerGID, 4096)
	if err != nil || !bytes.Equal(legacyAnchor, verified.Files["/etc/propertyquarry-release-single-host-v2/package-authority-v2.pem"].Data) {
		zero(legacyAnchor)
		return fmt.Errorf("install-genesis-package-authority-continuity-invalid")
	}
	zero(legacyAnchor)
	if recovering {
		if err := installer.validateRecoverableGenesisDirectories(); err != nil {
			return err
		}
		if err := installer.validateRecoverableBackupKey(); err != nil {
			return err
		}
	}
	for _, record := range verified.Files {
		path, pathErr := installer.hostPath(record.InstallPath)
		if pathErr != nil {
			return pathErr
		}
		if _, statErr := os.Lstat(path); !os.IsNotExist(statErr) {
			return fmt.Errorf("install-genesis-payload-remnant")
		}
	}
	for _, absolute := range []string{
		"/var/lib/propertyquarry-release-single-host-v2",
		"/run/propertyquarry-release-single-host-v2",
		"/usr/libexec/propertyquarry-release-control",
		"/usr/lib/propertyquarry-release-runner-v2",
		"/var/lib/propertyquarry-release-runner-v2",
		backupEncryptionKeyPath,
		"/etc/systemd/system/propertyquarry-release-single-host-v2.socket.d",
		"/etc/systemd/system/propertyquarry-release-single-host-v2@.service.d",
		"/etc/systemd/system/propertyquarry-release-single-host-v2-activation-canary.service.d",
		"/run/systemd/system/propertyquarry-release-single-host-v2.socket.d",
		"/run/systemd/system/propertyquarry-release-single-host-v2@.service.d",
		"/run/systemd/system/propertyquarry-release-single-host-v2-activation-canary.service.d",
	} {
		if recovering && (absolute == "/var/lib/propertyquarry-release-single-host-v2" || absolute == "/usr/libexec/propertyquarry-release-control" || absolute == "/usr/lib/propertyquarry-release-runner-v2" || absolute == backupEncryptionKeyPath) {
			continue
		}
		path, pathErr := installer.hostPath(absolute)
		if pathErr != nil {
			return pathErr
		}
		if _, statErr := os.Lstat(path); !os.IsNotExist(statErr) {
			return fmt.Errorf("install-genesis-host-remnant")
		}
	}
	passwdPath, _ := installer.hostPath("/etc/passwd")
	groupPath, _ := installer.hostPath("/etc/group")
	passwd, passwdErr := readExactFile(passwdPath, 0o644, installer.OwnerUID, installer.OwnerGID, 4*1024*1024)
	group, groupErr := readExactFile(groupPath, 0o644, installer.OwnerUID, installer.OwnerGID, 4*1024*1024)
	if passwdErr != nil || groupErr != nil || identityRecordPresent(passwd, "propertyquarry-runner-v2", "1999", 2) || identityRecordPresent(passwd, "propertyquarry-runner-v2", "1999", 3) || identityRecordPresent(group, "propertyquarry-release-v2", "1999", 2) {
		zero(passwd)
		zero(group)
		return fmt.Errorf("install-genesis-runner-identity-remnant")
	}
	zero(passwd)
	zero(group)
	return nil
}

func (installer *Installer) readExistingBackupEncryptionKey() (string, error) {
	keyPath, err := installer.hostPath(backupEncryptionKeyPath)
	if err != nil {
		return "", err
	}
	keyDirectory := filepath.Dir(keyPath)
	keyParent := filepath.Dir(keyDirectory)
	if err := validateExternalDirectoryChain(installer.HostRoot, keyParent, backupEncryptionKeyUID, backupEncryptionKeyGID); err != nil {
		return "", fmt.Errorf("install-backup-key-parent-invalid")
	}
	keyDirectoryInfo, err := os.Lstat(keyDirectory)
	if err != nil {
		return "", fmt.Errorf("install-backup-key-directory-invalid")
	}
	keyDirectoryMetadata, metadataOK := keyDirectoryInfo.Sys().(*syscall.Stat_t)
	if !metadataOK || !keyDirectoryInfo.IsDir() || keyDirectoryInfo.Mode()&os.ModeSymlink != 0 || keyDirectoryInfo.Mode().Perm() != 0o700 || keyDirectoryMetadata.Uid != backupEncryptionKeyUID || keyDirectoryMetadata.Gid != backupEncryptionKeyGID {
		return "", fmt.Errorf("install-backup-key-directory-invalid")
	}
	raw, err := readExactFile(keyPath, 0o600, backupEncryptionKeyUID, backupEncryptionKeyGID, 65)
	if err != nil || len(raw) != 65 || raw[64] != '\n' {
		zero(raw)
		return "", fmt.Errorf("install-backup-key-invalid")
	}
	defer zero(raw)
	for _, character := range raw[:64] {
		if (character < '0' || character > '9') && (character < 'a' || character > 'f') {
			return "", fmt.Errorf("install-backup-key-invalid")
		}
	}
	decoded := make([]byte, 32)
	decodedCount, err := hex.Decode(decoded, raw[:64])
	if err != nil || decodedCount != len(decoded) {
		zero(decoded)
		return "", fmt.Errorf("install-backup-key-invalid")
	}
	defer zero(decoded)
	return digest(decoded), nil
}

func identityRecordPresent(raw []byte, name, numeric string, numericIndex int) bool {
	for _, line := range bytes.Split(raw, []byte{'\n'}) {
		if len(line) == 0 {
			continue
		}
		parts := bytes.Split(line, []byte{':'})
		if len(parts) <= numericIndex || string(parts[0]) == name || string(parts[numericIndex]) == numeric {
			return true
		}
	}
	return false
}

func recoverableMutableHostBindingError(err error) bool {
	if err == nil {
		return false
	}
	message := err.Error()
	for _, prefix := range []string{
		"install-google-envelope-",
		"install-registration-envelope-",
		"install-scene-video-envelope-",
	} {
		if strings.HasPrefix(message, prefix) {
			return true
		}
	}
	return false
}

func (installer *Installer) currentAdmissionError(verified *VerifiedPackage) error {
	current := time.Now().UTC().Unix()
	if verified.MaterializationValidUntil < 1 || current < verified.TransactionStartedAt || current > verified.MaterializationValidUntil {
		return fmt.Errorf("install-materialization-receipt-expired")
	}
	return installer.validateHostBinding(verified)
}

func (installer *Installer) validateHostBinding(verified *VerifiedPackage) error {
	machinePath, err := installer.hostPath("/etc/machine-id")
	if err != nil {
		return err
	}
	if err := validateDirectoryChain(installer.HostRoot, filepath.Dir(machinePath), installer.OwnerUID); err != nil {
		return fmt.Errorf("install-machine-id-parent-invalid")
	}
	raw, err := readExactFile(machinePath, 0o444, installer.OwnerUID, installer.OwnerGID, 64)
	if err != nil {
		return fmt.Errorf("install-machine-id-unavailable")
	}
	machineID := strings.TrimSpace(string(raw))
	zero(raw)
	if !regexp.MustCompile(`^[0-9a-f]{32}$`).MatchString(machineID) || digest([]byte(machineID)) != verified.HostMachineIDDigest {
		return fmt.Errorf("install-host-binding-invalid")
	}
	config := verified.Files["/etc/propertyquarry-release-single-host-v2/authority.v2.json"].Data
	value, err := strictJSON(config, maximumManifestBytes)
	if err != nil {
		return fmt.Errorf("install-config-invalid")
	}
	configuredAPIHostIP, apiHostIPOK := exactString(value["api_host_ip"])
	configuredAPIHostPort, apiHostPortOK := exactInt(value["api_host_port"], apiHostPort, apiHostPort)
	configuredAPIContainerPort, apiContainerPortOK := exactInt(value["api_container_port"], apiContainerPort, apiContainerPort)
	if !apiHostIPOK || configuredAPIHostIP != apiHostIP || !apiHostPortOK || configuredAPIHostPort != apiHostPort || !apiContainerPortOK || configuredAPIContainerPort != apiContainerPort || verified.APIHostIP != apiHostIP || verified.APIHostPort != apiHostPort || verified.APIContainerPort != apiContainerPort {
		return fmt.Errorf("install-api-boundary-invalid")
	}
	prePurgeRootEnvDigest, prePurgeRootEnvDigestOK := exactString(value["pre_purge_root_env_digest"])
	if !prePurgeRootEnvDigestOK || !digestPattern.MatchString(prePurgeRootEnvDigest) || prePurgeRootEnvDigest != verified.PrePurgeRootEnvDigest {
		return fmt.Errorf("install-pre-purge-root-env-binding-invalid")
	}
	configuredDatabaseImage, databaseImageOK := exactString(value["database_image"])
	if !databaseImageOK || configuredDatabaseImage != databaseImage || verified.DatabaseImage != databaseImage {
		return fmt.Errorf("install-database-image-binding-invalid")
	}
	envelopeDigest, ok := exactString(value["github_identity_env_digest"])
	envelopeConfiguredPath, pathOK := exactString(value["github_identity_env_path"])
	envelopeMode, modeOK := exactString(value["github_identity_env_mode"])
	uid, uidOK := exactInt(value["github_identity_env_uid"], 0, 1<<31-1)
	gid, gidOK := exactInt(value["github_identity_env_gid"], 0, 1<<31-1)
	if !ok || !pathOK || !modeOK || envelopeConfiguredPath != "/docker/property/state/runtime/propertyquarry_google_identity.env" || envelopeMode != "0600" || !uidOK || !gidOK {
		return fmt.Errorf("install-google-envelope-binding-invalid")
	}
	envelopePath, err := installer.hostPath(envelopeConfiguredPath)
	if err != nil {
		return err
	}
	if err := validateExternalDirectoryChain(installer.HostRoot, filepath.Dir(envelopePath), uint32(uid), uint32(gid)); err != nil {
		return fmt.Errorf("install-google-envelope-parent-invalid")
	}
	envelope, err := readExactFile(envelopePath, 0o600, uint32(uid), uint32(gid), 32*1024)
	if err != nil {
		return fmt.Errorf("install-google-envelope-unavailable")
	}
	defer zero(envelope)
	if digest(envelope) != envelopeDigest || !validGoogleEnvelope(envelope) {
		return fmt.Errorf("install-google-envelope-invalid")
	}
	registrationDigest, digestOK := exactString(value["registration_email_env_digest"])
	registrationConfiguredPath, registrationPathOK := exactString(value["registration_email_env_path"])
	registrationMode, registrationModeOK := exactString(value["registration_email_env_mode"])
	registrationUID, registrationUIDOK := exactInt(value["registration_email_env_uid"], 0, 1<<31-1)
	registrationGID, registrationGIDOK := exactInt(value["registration_email_env_gid"], 0, 1<<31-1)
	if !digestOK || !digestPattern.MatchString(registrationDigest) || !registrationPathOK || registrationConfiguredPath != "/docker/property/state/runtime/propertyquarry_registration_email.env" || !registrationModeOK || registrationMode != "0600" || !registrationUIDOK || !registrationGIDOK {
		return fmt.Errorf("install-registration-envelope-binding-invalid")
	}
	registrationPath, err := installer.hostPath(registrationConfiguredPath)
	if err != nil {
		return err
	}
	if err := validateExternalDirectoryChain(installer.HostRoot, filepath.Dir(registrationPath), uint32(registrationUID), uint32(registrationGID)); err != nil {
		return fmt.Errorf("install-registration-envelope-parent-invalid")
	}
	registration, err := readExactFile(registrationPath, 0o600, uint32(registrationUID), uint32(registrationGID), 32*1024)
	if err != nil {
		return fmt.Errorf("install-registration-envelope-unavailable")
	}
	defer zero(registration)
	if digest(registration) != registrationDigest || !validRegistrationEmailEnvelope(registration) {
		return fmt.Errorf("install-registration-envelope-invalid")
	}
	sceneDigest, digestOK := exactString(value["scene_video_env_digest"])
	scenePath, pathOK := exactString(value["scene_video_env_path"])
	sceneMode, modeOK := exactInt(value["scene_video_env_mode"], 384, 384)
	sceneUID, uidOK := exactInt(value["scene_video_env_uid"], 1000, 1000)
	sceneGID, gidOK := exactInt(value["scene_video_env_gid"], 1000, 1000)
	if !digestOK || !digestPattern.MatchString(sceneDigest) || !pathOK || scenePath != "/docker/property/state/runtime/property_scene_video_shared.env" || !modeOK || sceneMode != 384 || !uidOK || !gidOK {
		return fmt.Errorf("install-scene-video-envelope-binding-invalid")
	}
	sceneHostPath, err := installer.hostPath(scenePath)
	if err != nil {
		return err
	}
	if err := validateExternalDirectoryChain(installer.HostRoot, filepath.Dir(sceneHostPath), uint32(sceneUID), uint32(sceneGID)); err != nil {
		return fmt.Errorf("install-scene-video-envelope-parent-invalid")
	}
	scene, err := readExactFile(sceneHostPath, 0o600, uint32(sceneUID), uint32(sceneGID), 256*1024)
	if err != nil {
		return fmt.Errorf("install-scene-video-envelope-unavailable")
	}
	defer zero(scene)
	if digest(scene) != sceneDigest {
		return fmt.Errorf("install-scene-video-envelope-invalid")
	}
	return nil
}

func validGoogleEnvelope(raw []byte) bool {
	if len(raw) < 1 || raw[len(raw)-1] != '\n' || bytes.IndexAny(raw, "\x00\r") >= 0 {
		return false
	}
	expected := map[string]bool{"PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID": false, "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET": false, "PROPERTYQUARRY_GOOGLE_OAUTH_REDIRECT_URI": false, "PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET": false, "PROPERTYQUARRY_IDENTITY_SESSION_SECRET": false}
	lines := bytes.Split(raw[:len(raw)-1], []byte{'\n'})
	if len(lines) != len(expected) {
		return false
	}
	for _, line := range lines {
		parts := bytes.SplitN(line, []byte{'='}, 2)
		if len(parts) != 2 || !validLiteralEnvelopeValue(parts[1]) {
			return false
		}
		name := string(parts[0])
		seen, ok := expected[name]
		if !ok || seen {
			return false
		}
		expected[name] = true
	}
	return true
}

func validRegistrationEmailEnvelope(raw []byte) bool {
	expected := authority.RegistrationEmailEnvironmentNames()
	if len(raw) < 1 || raw[len(raw)-1] != '\n' || bytes.IndexAny(raw, "\x00\r") >= 0 {
		return false
	}
	lines := bytes.Split(raw[:len(raw)-1], []byte{'\n'})
	if len(expected) != int(authority.RegistrationEmailKeyCount) || len(lines) != len(expected) {
		return false
	}
	for index, line := range lines {
		parts := bytes.SplitN(line, []byte{'='}, 2)
		if len(parts) != 2 || !validLiteralEnvelopeValue(parts[1]) {
			return false
		}
		name := string(parts[0])
		if name != expected[index] {
			return false
		}
		if name == "EA_REGISTRATION_EMAIL_FORCE_FALLBACK" && !bytes.Equal(parts[1], []byte("true")) && !bytes.Equal(parts[1], []byte("false")) {
			return false
		}
	}
	return true
}

func validLiteralEnvelopeValue(value []byte) bool {
	if len(value) == 0 || value[0] == ' ' || value[len(value)-1] == ' ' {
		return false
	}
	for _, character := range value {
		if character < 0x20 || character > 0x7e || character == '$' || character == '\'' || character == '"' || character == '\\' || character == '#' || character == '`' {
			return false
		}
	}
	return true
}

func (installer *Installer) enforceInstalledGeneration(verified *VerifiedPackage) (bool, bool, error) {
	manifestPath, _ := installer.hostPath("/etc/propertyquarry-release-single-host-v2/package-manifest.v2.json")
	signaturePath, _ := installer.hostPath("/etc/propertyquarry-release-single-host-v2/package-manifest.v2.sig")
	manifestRaw, manifestErr := readExactFile(manifestPath, 0o444, installer.OwnerUID, installer.OwnerGID, maximumManifestBytes)
	signature, signatureErr := readExactFile(signaturePath, 0o444, installer.OwnerUID, installer.OwnerGID, ed25519.SignatureSize)
	if os.IsNotExist(unwrapPathError(manifestErr)) && os.IsNotExist(unwrapPathError(signatureErr)) {
		config, _ := strictJSON(verified.Files["/etc/propertyquarry-release-single-host-v2/authority.v2.json"].Data, maximumManifestBytes)
		predecessor, _ := exactString(config["predecessor_runtime_sha"])
		if verified.ReleaseGeneration != 1 || predecessor != "genesis" {
			return false, false, fmt.Errorf("install-genesis-binding-invalid")
		}
		return false, false, nil
	}
	defer zero(manifestRaw)
	defer zero(signature)
	if manifestErr != nil || signatureErr != nil || len(signature) != ed25519.SignatureSize {
		return false, true, fmt.Errorf("installed-manifest-incomplete")
	}
	packageKey, _, err := EmbeddedPackageAuthority()
	if err != nil {
		return false, true, err
	}
	defer zero(packageKey)
	if !ed25519.Verify(packageKey, framed(packageSignatureDomain, manifestRaw), signature) {
		return false, true, fmt.Errorf("installed-manifest-signature-invalid")
	}
	previous, err := strictJSON(manifestRaw, maximumManifestBytes)
	if err != nil {
		return false, true, err
	}
	previousGeneration, generationOK := exactInt(previous["release_generation"], 1, 1<<62)
	previousRuntime, runtimeOK := exactString(previous["runtime_sha"])
	previousConfig, configOK := exactString(previous["config_digest"])
	previousReceiptKeyID, receiptKeyOK := exactString(previous["receipt_authority_key_id"])
	if !generationOK || !runtimeOK || !shaPattern.MatchString(previousRuntime) || !configOK || !digestPattern.MatchString(previousConfig) || !receiptKeyOK || previousReceiptKeyID != verified.ReceiptAuthorityKeyID {
		return false, true, fmt.Errorf("installed-manifest-binding-invalid")
	}
	if err := installer.verifyPriorManifestPayload(previous); err != nil {
		return false, true, err
	}
	if verified.ReleaseGeneration == previousGeneration {
		if verified.RuntimeSHA != previousRuntime || verified.ConfigDigest != previousConfig || !bytes.Equal(verified.ManifestRaw, manifestRaw) || !bytes.Equal(verified.ManifestSignature, signature) {
			return false, true, fmt.Errorf("install-generation-rebinding")
		}
		return true, true, nil
	}
	if verified.ReleaseGeneration != previousGeneration+1 {
		return false, true, fmt.Errorf("install-generation-downgrade-or-skip")
	}
	config, _ := strictJSON(verified.Files["/etc/propertyquarry-release-single-host-v2/authority.v2.json"].Data, maximumManifestBytes)
	predecessor, _ := exactString(config["predecessor_runtime_sha"])
	if predecessor != previousRuntime {
		return false, true, fmt.Errorf("install-predecessor-invalid")
	}
	return false, true, nil
}

func (installer *Installer) enforceReceiptKeyContinuity(verified *VerifiedPackage) error {
	manifestPath, _ := installer.hostPath("/etc/propertyquarry-release-single-host-v2/package-manifest.v2.json")
	signaturePath, _ := installer.hostPath("/etc/propertyquarry-release-single-host-v2/package-manifest.v2.sig")
	manifestRaw, manifestErr := readExactFile(manifestPath, 0o444, installer.OwnerUID, installer.OwnerGID, maximumManifestBytes)
	signature, signatureErr := readExactFile(signaturePath, 0o444, installer.OwnerUID, installer.OwnerGID, ed25519.SignatureSize)
	if os.IsNotExist(unwrapPathError(manifestErr)) && os.IsNotExist(unwrapPathError(signatureErr)) {
		if verified.ReleaseGeneration != 1 {
			return fmt.Errorf("install-receipt-key-genesis-invalid")
		}
		return nil
	}
	defer zero(manifestRaw)
	defer zero(signature)
	if manifestErr != nil || signatureErr != nil || len(signature) != ed25519.SignatureSize {
		return fmt.Errorf("installed-manifest-incomplete")
	}
	packageKey, _, err := EmbeddedPackageAuthority()
	if err != nil {
		return err
	}
	defer zero(packageKey)
	if !ed25519.Verify(packageKey, framed(packageSignatureDomain, manifestRaw), signature) {
		return fmt.Errorf("installed-manifest-signature-invalid")
	}
	previous, err := strictJSON(manifestRaw, maximumManifestBytes)
	if err != nil {
		return err
	}
	previousReceiptKeyID, ok := exactString(previous["receipt_authority_key_id"])
	if !ok || !digestPattern.MatchString(previousReceiptKeyID) || previousReceiptKeyID != verified.ReceiptAuthorityKeyID {
		return fmt.Errorf("install-receipt-key-rotation-forbidden")
	}
	return nil
}

func (installer *Installer) ensureBackupEncryptionKey() (string, bool, error) {
	keyPath, err := installer.hostPath(backupEncryptionKeyPath)
	if err != nil {
		return "", false, err
	}
	keyDirectory := filepath.Dir(keyPath)
	keyParent := filepath.Dir(keyDirectory)
	if err := validateExternalDirectoryChain(installer.HostRoot, keyParent, backupEncryptionKeyUID, backupEncryptionKeyGID); err != nil {
		return "", false, fmt.Errorf("install-backup-key-parent-invalid")
	}
	createdDirectory := false
	if err := os.Mkdir(keyDirectory, 0o700); err == nil {
		createdDirectory = true
		if installer.interrupted("backup-key-directory-after-mkdir") {
			return "", false, errInstallInterrupted
		}
		if err := os.Chown(keyDirectory, backupEncryptionKeyUID, backupEncryptionKeyGID); err != nil {
			return "", false, fmt.Errorf("install-backup-key-directory-chown-failed")
		}
		if installer.interrupted("backup-key-directory-after-chown") {
			return "", false, errInstallInterrupted
		}
		if err := os.Chmod(keyDirectory, 0o700); err != nil {
			return "", false, fmt.Errorf("install-backup-key-directory-chmod-failed")
		}
		if installer.interrupted("backup-key-directory-after-chmod") {
			return "", false, errInstallInterrupted
		}
	} else if !os.IsExist(err) {
		return "", false, fmt.Errorf("install-backup-key-directory-create-failed")
	}
	keyDirectoryInfo, err := os.Lstat(keyDirectory)
	if err != nil {
		return "", false, fmt.Errorf("install-backup-key-directory-invalid")
	}
	keyDirectoryMetadata, metadataOK := keyDirectoryInfo.Sys().(*syscall.Stat_t)
	if !metadataOK || !keyDirectoryInfo.IsDir() || keyDirectoryInfo.Mode()&os.ModeSymlink != 0 {
		return "", false, fmt.Errorf("install-backup-key-directory-invalid")
	}
	if keyDirectoryInfo.Mode().Perm() != 0o700 || keyDirectoryMetadata.Uid != backupEncryptionKeyUID || keyDirectoryMetadata.Gid != backupEncryptionKeyGID {
		if installer.preAdmission == nil || !installer.preAdmissionRequired || keyDirectoryInfo.Mode().Perm()&^os.FileMode(0o700) != 0 || (keyDirectoryMetadata.Uid != installer.OwnerUID && keyDirectoryMetadata.Uid != backupEncryptionKeyUID) {
			return "", false, fmt.Errorf("install-backup-key-directory-invalid")
		}
		if err := os.Chown(keyDirectory, backupEncryptionKeyUID, backupEncryptionKeyGID); err != nil || os.Chmod(keyDirectory, 0o700) != nil || syncDirectory(keyParent) != nil {
			return "", false, fmt.Errorf("install-backup-key-directory-repair-failed")
		}
	}
	if createdDirectory {
		if err := syncDirectory(keyParent); err != nil {
			return "", false, err
		}
		if installer.interrupted("backup-key-directory-after-parent-fsync") {
			return "", false, errInstallInterrupted
		}
	}

	if installer.interrupted("after-backup-key-directory") {
		return "", false, errInstallInterrupted
	}
	createdKey := false
	if _, statErr := os.Lstat(keyPath); os.IsNotExist(statErr) {
		if installer.preAdmission == nil || !installer.preAdmissionRequired || len(installer.preAdmission.secret) != 32 || !digestPattern.MatchString(installer.preAdmission.keyID) || digest(installer.preAdmission.secret) != installer.preAdmission.keyID {
			return "", false, fmt.Errorf("install-backup-key-pre-admission-required")
		}
		encoded := make([]byte, hex.EncodedLen(len(installer.preAdmission.secret))+1)
		hex.Encode(encoded[:len(encoded)-1], installer.preAdmission.secret)
		encoded[len(encoded)-1] = '\n'
		defer zero(encoded)
		pending := keyPath + ".pending"
		if pendingRaw, pendingErr := readExactFile(pending, 0o600, backupEncryptionKeyUID, backupEncryptionKeyGID, 65); pendingErr == nil {
			matches := bytes.Equal(pendingRaw, encoded)
			zero(pendingRaw)
			if !matches {
				if err := os.Remove(pending); err != nil || syncDirectory(keyDirectory) != nil {
					return "", false, fmt.Errorf("install-backup-key-pending-repair-failed")
				}
				if err := installer.writeNoReplace(pending, encoded, 0o600, backupEncryptionKeyUID, backupEncryptionKeyGID, "backup-key-stage"); err != nil {
					return "", false, fmt.Errorf("install-backup-key-stage-failed")
				}
			}
		} else if os.IsNotExist(unwrapPathError(pendingErr)) {
			if err := installer.writeNoReplace(pending, encoded, 0o600, backupEncryptionKeyUID, backupEncryptionKeyGID, "backup-key-stage"); err != nil {
				return "", false, fmt.Errorf("install-backup-key-stage-failed")
			}
		} else {
			// A torn pending write cannot authorize state. The already-authenticated
			// pre-admission record remains the sole source of key bytes, so replace
			// only this fixed staging name and never a published key.
			pendingInfo, infoErr := os.Lstat(pending)
			if infoErr != nil {
				return "", false, fmt.Errorf("install-backup-key-pending-invalid")
			}
			pendingMetadata, metadataOK := pendingInfo.Sys().(*syscall.Stat_t)
			if !metadataOK || !pendingInfo.Mode().IsRegular() || pendingInfo.Mode()&os.ModeSymlink != 0 || pendingMetadata.Nlink != 1 || (pendingMetadata.Uid != installer.OwnerUID && pendingMetadata.Uid != backupEncryptionKeyUID) || pendingInfo.Mode().Perm()&^os.FileMode(0o600) != 0 || pendingInfo.Size() < 0 || pendingInfo.Size() > 65 {
				return "", false, fmt.Errorf("install-backup-key-pending-invalid")
			}
			if err := os.Remove(pending); err != nil || syncDirectory(keyDirectory) != nil {
				return "", false, fmt.Errorf("install-backup-key-pending-repair-failed")
			}
			if err := installer.writeNoReplace(pending, encoded, 0o600, backupEncryptionKeyUID, backupEncryptionKeyGID, "backup-key-stage"); err != nil {
				return "", false, fmt.Errorf("install-backup-key-stage-failed")
			}
		}
		if installer.interrupted("after-backup-key-stage") {
			return "", false, errInstallInterrupted
		}
		if err := renameNoReplace(pending, keyPath); err != nil {
			return "", false, fmt.Errorf("install-backup-key-publish-failed")
		}
		if installer.interrupted("backup-key-publish-after-rename") {
			return "", false, errInstallInterrupted
		}
		if err := syncDirectory(keyDirectory); err != nil {
			return "", false, err
		}
		if installer.interrupted("backup-key-publish-after-parent-fsync") {
			return "", false, errInstallInterrupted
		}
		createdKey = true
		if installer.interrupted("after-backup-key-publish") {
			return "", false, errInstallInterrupted
		}
	} else if statErr != nil {
		return "", false, fmt.Errorf("install-backup-key-state-invalid")
	}
	keyID, err := installer.readExistingBackupEncryptionKey()
	if err != nil {
		return "", false, err
	}
	if installer.preAdmission != nil && keyID != installer.preAdmission.keyID {
		return "", false, fmt.Errorf("install-backup-key-pre-admission-mismatch")
	}
	return keyID, createdKey, nil
}

func (installer *Installer) verifyPriorManifestPayload(manifest map[string]any) error {
	items, ok := manifest["files"].([]any)
	if !ok || len(items) < 10 || len(items) > 64 {
		return fmt.Errorf("installed-manifest-file-list-invalid")
	}
	seen := make(map[string]bool, len(items))
	for _, item := range items {
		record, ok := item.(map[string]any)
		if !ok || !hasKeys(record, "install_path", "mode", "package_path", "purpose", "sha256", "size") {
			return fmt.Errorf("installed-manifest-file-entry-invalid")
		}
		installPath, pathOK := record["install_path"].(string)
		packagePath, packageOK := record["package_path"].(string)
		modeText, modeOK := record["mode"].(string)
		expectedDigest, digestOK := record["sha256"].(string)
		size, sizeOK := exactInt(record["size"], 1, maximumMemberBytes)
		purpose, purposeOK := record["purpose"].(string)
		modeValue, modeErr := strconv.ParseUint(modeText, 8, 12)
		if !pathOK || !packageOK || !modeOK || !digestOK || !sizeOK || !purposeOK || purpose == "" || !validInstallPath(installPath) || packagePath != "payload"+installPath || !modePattern.MatchString(modeText) || !digestPattern.MatchString(expectedDigest) || modeErr != nil || seen[installPath] {
			return fmt.Errorf("installed-manifest-file-binding-invalid")
		}
		seen[installPath] = true
		hostPath, err := installer.hostPath(installPath)
		if err != nil {
			return err
		}
		raw, err := readExactFile(hostPath, os.FileMode(modeValue), installer.OwnerUID, installer.OwnerGID, int(size))
		if err != nil || int64(len(raw)) != size || digest(raw) != expectedDigest {
			zero(raw)
			return fmt.Errorf("installed-manifest-payload-invalid")
		}
		zero(raw)
	}
	return nil
}

func (installer *Installer) prepareTargets(verified *VerifiedPackage, txID string) ([]*installTarget, error) {
	targets, err := installer.candidateTargets(verified, txID)
	if err != nil {
		return nil, err
	}
	for _, target := range targets {
		hostPath, _ := installer.hostPath(target.path)
		info, statErr := os.Lstat(hostPath)
		if statErr == nil {
			if !validOwnedRegular(info, installer.OwnerUID, installer.OwnerGID) || info.Size() < 1 || info.Size() > maximumMemberBytes {
				return nil, fmt.Errorf("install-prior-target-invalid")
			}
			raw, readErr := readExactFile(hostPath, info.Mode().Perm(), installer.OwnerUID, installer.OwnerGID, maximumMemberBytes)
			if readErr != nil {
				return nil, fmt.Errorf("install-prior-target-invalid")
			}
			target.priorPresent = true
			target.priorMode = info.Mode().Perm()
			target.priorSize = info.Size()
			target.priorDigest = digest(raw)
			zero(raw)
		} else if !os.IsNotExist(statErr) {
			return nil, fmt.Errorf("install-prior-target-state-invalid")
		}
	}
	return targets, nil
}

func (installer *Installer) candidateTargets(verified *VerifiedPackage, txID string) ([]*installTarget, error) {
	paths := SortedInstallPaths(verified.Files)
	targets := make([]*installTarget, 0, len(paths)+2)
	for _, path := range paths {
		file := verified.Files[path]
		targets = append(targets, &installTarget{path: path, data: file.Data, mode: file.Mode, size: file.Size, digest: file.Digest})
	}
	targets = append(targets,
		&installTarget{path: "/etc/propertyquarry-release-single-host-v2/package-manifest.v2.json", data: verified.ManifestRaw, mode: 0o444, size: int64(len(verified.ManifestRaw)), digest: digest(verified.ManifestRaw)},
		&installTarget{path: "/etc/propertyquarry-release-single-host-v2/package-manifest.v2.sig", data: verified.ManifestSignature, mode: 0o444, size: int64(len(verified.ManifestSignature)), digest: digest(verified.ManifestSignature)},
	)
	sort.Slice(targets, func(left, right int) bool {
		leftRank, rightRank := installTargetRank(targets[left].path), installTargetRank(targets[right].path)
		if leftRank != rightRank {
			return leftRank < rightRank
		}
		return targets[left].path < targets[right].path
	})
	for _, target := range targets {
		hostPath, err := installer.hostPath(target.path)
		if err != nil {
			return nil, err
		}
		parent := filepath.Dir(hostPath)
		if err := validateDirectoryChain(installer.HostRoot, parent, installer.OwnerUID); err != nil {
			return nil, fmt.Errorf("install-target-parent-invalid")
		}
		base := filepath.Base(hostPath)
		target.stage = filepath.Join(parent, "."+base+".pqinstall-"+txID)
		target.backup = filepath.Join(parent, "."+base+".pqbackup-"+txID)
	}
	return targets, nil
}

func installTargetRank(path string) int {
	switch path {
	case "/etc/propertyquarry-release-single-host-v2/package-manifest.v2.sig":
		return 1
	case "/etc/propertyquarry-release-single-host-v2/package-manifest.v2.json":
		return 2
	default:
		return 0
	}
}

func (installer *Installer) stageTargets(targets []*installTarget) error {
	for index, target := range targets {
		if err := installer.writeNoReplace(target.stage, target.data, target.mode, installer.OwnerUID, installer.OwnerGID, fmt.Sprintf("target-stage-%d", index)); err != nil {
			return err
		}
	}
	return nil
}

func (installer *Installer) commitTargets(targets []*installTarget, appendEvent func(string, map[string]any) error) error {
	for index, target := range targets {
		hostPath, _ := installer.hostPath(target.path)
		if target.priorPresent {
			if !installer.targetMatches(hostPath, target.priorMode, target.priorSize, target.priorDigest) {
				return fmt.Errorf("install-prior-target-changed")
			}
			if err := renameNoReplace(hostPath, target.backup); err != nil {
				return fmt.Errorf("install-backup-failed")
			}
			if installer.interrupted(fmt.Sprintf("backup-%d-after-rename", index)) {
				return errInstallInterrupted
			}
			if err := syncDirectory(filepath.Dir(hostPath)); err != nil {
				return err
			}
			if installer.interrupted(fmt.Sprintf("backup-%d-after-parent-fsync", index)) {
				return errInstallInterrupted
			}
			if installer.interrupted(fmt.Sprintf("after-backup-%d", index)) {
				return errInstallInterrupted
			}
		} else if _, err := os.Lstat(hostPath); !os.IsNotExist(err) {
			return fmt.Errorf("install-unexpected-prior-target")
		}
		if err := renameNoReplace(target.stage, hostPath); err != nil {
			return fmt.Errorf("install-commit-failed")
		}
		if installer.interrupted(fmt.Sprintf("install-%d-after-rename", index)) {
			return errInstallInterrupted
		}
		if err := syncDirectory(filepath.Dir(hostPath)); err != nil {
			return err
		}
		if installer.interrupted(fmt.Sprintf("install-%d-after-parent-fsync", index)) {
			return errInstallInterrupted
		}
		if installer.interrupted(fmt.Sprintf("after-install-%d", index)) {
			return errInstallInterrupted
		}
		if err := appendEvent("file-installed", map[string]any{"file_index": json.Number(strconv.Itoa(index)), "install_path": target.path}); err != nil {
			return err
		}
	}
	return nil
}

func (installer *Installer) restorePreviousTargets(targets []*installTarget) error {
	for index := len(targets) - 1; index >= 0; index-- {
		target := targets[index]
		hostPath, _ := installer.hostPath(target.path)
		if target.priorPresent {
			backupExists, err := installer.pathMatchesOrAbsent(target.backup, target.priorMode, target.priorSize, target.priorDigest)
			if err != nil {
				return fmt.Errorf("install-recovery-backup-invalid")
			}
			if backupExists {
				if hostExists, matchErr := installer.pathMatchesOrAbsent(hostPath, target.mode, target.size, target.digest); matchErr != nil {
					return fmt.Errorf("install-recovery-candidate-invalid")
				} else if hostExists {
					if err := os.Remove(hostPath); err != nil {
						return fmt.Errorf("install-recovery-candidate-remove-failed")
					}
					if installer.interrupted(fmt.Sprintf("restore-%d-after-remove", index)) {
						return errInstallInterrupted
					}
					if err := syncDirectory(filepath.Dir(hostPath)); err != nil {
						return err
					}
					if installer.interrupted(fmt.Sprintf("restore-%d-after-remove-parent-fsync", index)) {
						return errInstallInterrupted
					}
				}
				if installer.interrupted(fmt.Sprintf("during-restore-%d", index)) {
					return errInstallInterrupted
				}
				if err := renameNoReplace(target.backup, hostPath); err != nil {
					return fmt.Errorf("install-recovery-restore-failed")
				}
				if installer.interrupted(fmt.Sprintf("restore-%d-after-rename", index)) {
					return errInstallInterrupted
				}
				if err := syncDirectory(filepath.Dir(hostPath)); err != nil {
					return err
				}
				if installer.interrupted(fmt.Sprintf("restore-%d-after-parent-fsync", index)) {
					return errInstallInterrupted
				}
			} else if !installer.targetMatches(hostPath, target.priorMode, target.priorSize, target.priorDigest) {
				return fmt.Errorf("install-recovery-prior-missing")
			}
		} else {
			if _, err := os.Lstat(target.backup); err == nil || !os.IsNotExist(err) {
				return fmt.Errorf("install-recovery-unexpected-backup")
			}
			if hostExists, matchErr := installer.pathMatchesOrAbsent(hostPath, target.mode, target.size, target.digest); matchErr != nil {
				return fmt.Errorf("install-recovery-candidate-invalid")
			} else if hostExists {
				if err := os.Remove(hostPath); err != nil {
					return fmt.Errorf("install-recovery-candidate-remove-failed")
				}
				if installer.interrupted(fmt.Sprintf("restore-%d-after-remove", index)) {
					return errInstallInterrupted
				}
				if err := syncDirectory(filepath.Dir(hostPath)); err != nil {
					return err
				}
				if installer.interrupted(fmt.Sprintf("restore-%d-after-remove-parent-fsync", index)) {
					return errInstallInterrupted
				}
			}
		}
		if stageInfo, stageErr := os.Lstat(target.stage); stageErr == nil {
			if !validOwnedRegular(stageInfo, installer.OwnerUID, installer.OwnerGID) || stageInfo.Size() < 0 || stageInfo.Size() > maximumMemberBytes {
				return fmt.Errorf("install-recovery-stage-invalid")
			}
			if err := os.Remove(target.stage); err != nil {
				return fmt.Errorf("install-recovery-stage-remove-failed")
			}
		} else if !os.IsNotExist(stageErr) {
			return fmt.Errorf("install-recovery-stage-invalid")
		}
		if err := syncDirectory(filepath.Dir(hostPath)); err != nil {
			return err
		}
	}
	return nil
}

func (installer *Installer) removeStages(targets []*installTarget) {
	for _, target := range targets {
		if target.stage != "" && installer.targetMatches(target.stage, target.mode, target.size, target.digest) {
			_ = os.Remove(target.stage)
			_ = syncDirectory(filepath.Dir(target.stage))
		}
	}
}

func (installer *Installer) cleanupCommittedTargets(targets []*installTarget) error {
	for _, target := range targets {
		hostPath, _ := installer.hostPath(target.path)
		if !installer.targetMatches(hostPath, target.mode, target.size, target.digest) {
			return fmt.Errorf("install-cleanup-candidate-invalid")
		}
		if target.priorPresent {
			exists, err := installer.pathMatchesOrAbsent(target.backup, target.priorMode, target.priorSize, target.priorDigest)
			if err != nil {
				return fmt.Errorf("install-cleanup-backup-invalid")
			}
			if exists {
				if err := os.Remove(target.backup); err != nil {
					return fmt.Errorf("install-cleanup-backup-remove-failed")
				}
			}
		} else if _, err := os.Lstat(target.backup); err == nil || !os.IsNotExist(err) {
			return fmt.Errorf("install-cleanup-unexpected-backup")
		}
		if exists, err := installer.pathMatchesOrAbsent(target.stage, target.mode, target.size, target.digest); err != nil {
			return fmt.Errorf("install-cleanup-stage-invalid")
		} else if exists {
			if err := os.Remove(target.stage); err != nil {
				return fmt.Errorf("install-cleanup-stage-remove-failed")
			}
		}
		if err := syncDirectory(filepath.Dir(hostPath)); err != nil {
			return err
		}
	}
	return nil
}

func cleanupTargetData(targets []*installTarget) {
	for _, target := range targets {
		target.data = nil
	}
}

func targetSnapshotsJSON(targets []*installTarget) []any {
	items := make([]any, 0, len(targets))
	for _, target := range targets {
		priorMode, priorDigest := "", ""
		if target.priorPresent {
			priorMode = fmt.Sprintf("%04o", target.priorMode.Perm())
			priorDigest = target.priorDigest
		}
		items = append(items, map[string]any{
			"candidate_digest": target.digest, "candidate_mode": fmt.Sprintf("%04o", target.mode.Perm()),
			"candidate_size": json.Number(strconv.FormatInt(target.size, 10)), "install_path": target.path,
			"prior_digest": priorDigest, "prior_mode": priorMode, "prior_present": target.priorPresent,
			"prior_size": json.Number(strconv.FormatInt(target.priorSize, 10)),
		})
	}
	return items
}

func (installer *Installer) parseTargetSnapshots(value any, verified *VerifiedPackage, txID string) ([]*installTarget, error) {
	items, ok := value.([]any)
	if !ok {
		return nil, fmt.Errorf("install-journal-target-list-invalid")
	}
	targets, err := installer.candidateTargets(verified, txID)
	if err != nil || len(items) != len(targets) {
		return nil, fmt.Errorf("install-journal-target-count-invalid")
	}
	for index, item := range items {
		record, ok := item.(map[string]any)
		if !ok || !hasKeys(record, "candidate_digest", "candidate_mode", "candidate_size", "install_path", "prior_digest", "prior_mode", "prior_present", "prior_size") {
			return nil, fmt.Errorf("install-journal-target-shape-invalid")
		}
		candidateDigest, digestOK := record["candidate_digest"].(string)
		candidateMode, modeOK := record["candidate_mode"].(string)
		candidateSize, sizeOK := exactInt(record["candidate_size"], 1, maximumMemberBytes)
		installPath, pathOK := record["install_path"].(string)
		priorPresent, priorOK := record["prior_present"].(bool)
		priorDigest, priorDigestOK := record["prior_digest"].(string)
		priorMode, priorModeOK := record["prior_mode"].(string)
		priorSize, priorSizeOK := exactInt(record["prior_size"], 0, maximumMemberBytes)
		target := targets[index]
		if !digestOK || !modeOK || !sizeOK || !pathOK || !priorOK || !priorDigestOK || !priorModeOK || !priorSizeOK || installPath != target.path || candidateDigest != target.digest || candidateMode != fmt.Sprintf("%04o", target.mode.Perm()) || candidateSize != target.size {
			return nil, fmt.Errorf("install-journal-candidate-binding-invalid")
		}
		if priorPresent {
			parsedMode, parseErr := strconv.ParseUint(priorMode, 8, 12)
			if parseErr != nil || !modePattern.MatchString(priorMode) || !digestPattern.MatchString(priorDigest) || priorSize < 1 {
				return nil, fmt.Errorf("install-journal-prior-binding-invalid")
			}
			target.priorPresent = true
			target.priorMode = os.FileMode(parsedMode)
			target.priorSize = priorSize
			target.priorDigest = priorDigest
		} else if priorMode != "" || priorDigest != "" || priorSize != 0 {
			return nil, fmt.Errorf("install-journal-absent-prior-invalid")
		}
	}
	return targets, nil
}

type journalDiskEvent struct {
	name     string
	event    string
	txID     string
	sequence int64
	payload  map[string]any
}

func (installer *Installer) writeJournalEventNoReplace(directory, name string, wire []byte) error {
	matches := installJournalNamePattern.FindStringSubmatch(name)
	if len(matches) != 4 {
		return fmt.Errorf("install-journal-name-invalid")
	}
	pending := filepath.Join(directory, ".pending-"+name)
	final := filepath.Join(directory, name)
	scope := "journal-" + matches[3]
	if err := installer.writeNoReplace(pending, wire, 0o600, installer.OwnerUID, installer.OwnerGID, scope); err != nil {
		return err
	}
	if err := renameNoReplace(pending, final); err != nil {
		return fmt.Errorf("install-journal-publish-failed")
	}
	if installer.interrupted(scope + "-publish-after-rename") {
		return errInstallInterrupted
	}
	if err := syncDirectory(directory); err != nil {
		return err
	}
	if installer.interrupted(scope + "-publish-after-parent-fsync") {
		return errInstallInterrupted
	}
	return nil
}

func (installer *Installer) recoverPendingJournalWrites(directory string, verified *VerifiedPackage, key ed25519.PublicKey, currentTxID string) error {
	entries, err := os.ReadDir(directory)
	if err != nil {
		return fmt.Errorf("install-journal-unavailable")
	}
	for _, entry := range entries {
		if !strings.HasPrefix(entry.Name(), ".pending-") {
			continue
		}
		finalName := strings.TrimPrefix(entry.Name(), ".pending-")
		matches := installJournalNamePattern.FindStringSubmatch(finalName)
		if entry.IsDir() || len(matches) != 4 || matches[1] != currentTxID {
			return fmt.Errorf("install-journal-pending-entry-invalid")
		}
		pendingPath := filepath.Join(directory, entry.Name())
		finalPath := filepath.Join(directory, finalName)
		if _, statErr := os.Lstat(finalPath); statErr == nil || !os.IsNotExist(statErr) {
			return fmt.Errorf("install-journal-pending-collision")
		}
		info, statErr := os.Lstat(pendingPath)
		if statErr != nil || !validOwnedRegular(info, installer.OwnerUID, installer.OwnerGID) || info.Size() < 0 || info.Size() > maximumManifestBytes {
			return fmt.Errorf("install-journal-pending-metadata-invalid")
		}
		valid := false
		structured := false
		if info.Size() > 0 && info.Mode().Perm() == 0o600 {
			raw, readErr := readExactFile(pendingPath, 0o600, installer.OwnerUID, installer.OwnerGID, maximumManifestBytes)
			if readErr == nil {
				wrapper, decodeErr := strictJSON(raw, maximumManifestBytes)
				zero(raw)
				if decodeErr == nil && hasKeys(wrapper, "payload", "signature", "signature_key_id") {
					structured = true
					payload, payloadOK := wrapper["payload"].(map[string]any)
					signatureText, signatureOK := wrapper["signature"].(string)
					keyID, keyOK := wrapper["signature_key_id"].(string)
					signature, signatureErr := base64.RawURLEncoding.DecodeString(signatureText)
					payloadRaw, canonicalErr := canonicalJSON(payload)
					sequence, sequenceErr := strconv.ParseInt(matches[2], 10, 64)
					valid = payloadOK && signatureOK && keyOK && keyID == verified.ReceiptAuthorityKeyID && signatureErr == nil && canonicalErr == nil && sequenceErr == nil && len(signature) == ed25519.SignatureSize && ed25519.Verify(key, framed(installJournalDomain, payloadRaw), signature) && validateJournalEventBase(payload, matches[1], sequence, matches[3], keyID) == nil && validateCurrentJournalBinding(journalImmutableFields(payload), verified, installer.backupEncryptionKeyID) == nil
					zero(signature)
					zero(payloadRaw)
				}
			}
		}
		if structured && !valid {
			return fmt.Errorf("install-journal-pending-signature-invalid")
		}
		if valid {
			if err := renameNoReplace(pendingPath, finalPath); err != nil {
				return fmt.Errorf("install-journal-pending-promote-failed")
			}
		} else if err := os.Remove(pendingPath); err != nil {
			return fmt.Errorf("install-journal-pending-remove-failed")
		}
		if err := syncDirectory(directory); err != nil {
			return err
		}
	}
	return nil
}

func (installer *Installer) replayJournal(directory string, verified *VerifiedPackage, key ed25519.PublicKey, currentTxID string) (*journalReplay, error) {
	if err := installer.recoverPendingJournalWrites(directory, verified, key, currentTxID); err != nil {
		return nil, err
	}
	entries, err := os.ReadDir(directory)
	if err != nil {
		return nil, fmt.Errorf("install-journal-unavailable")
	}
	transactions := map[string][]journalDiskEvent{}
	for _, entry := range entries {
		matches := installJournalNamePattern.FindStringSubmatch(entry.Name())
		if entry.IsDir() || len(matches) != 4 {
			return nil, fmt.Errorf("install-journal-entry-invalid")
		}
		sequence, parseErr := strconv.ParseInt(matches[2], 10, 64)
		if parseErr != nil || sequence < 1 {
			return nil, fmt.Errorf("install-journal-sequence-invalid")
		}
		raw, readErr := readExactFile(filepath.Join(directory, entry.Name()), 0o600, installer.OwnerUID, installer.OwnerGID, maximumManifestBytes)
		if readErr != nil {
			return nil, fmt.Errorf("install-journal-event-unavailable")
		}
		wrapper, decodeErr := strictJSON(raw, maximumManifestBytes)
		zero(raw)
		if decodeErr != nil || !hasKeys(wrapper, "payload", "signature", "signature_key_id") {
			return nil, fmt.Errorf("install-journal-wire-invalid")
		}
		payload, payloadOK := wrapper["payload"].(map[string]any)
		signatureText, signatureOK := wrapper["signature"].(string)
		keyID, keyOK := wrapper["signature_key_id"].(string)
		if !payloadOK || !signatureOK || !keyOK || keyID != verified.ReceiptAuthorityKeyID {
			return nil, fmt.Errorf("install-journal-key-binding-invalid")
		}
		signature, signatureErr := base64.RawURLEncoding.DecodeString(signatureText)
		payloadRaw, canonicalErr := canonicalJSON(payload)
		if signatureErr != nil || canonicalErr != nil || len(signature) != ed25519.SignatureSize || !ed25519.Verify(key, framed(installJournalDomain, payloadRaw), signature) {
			zero(signature)
			zero(payloadRaw)
			return nil, fmt.Errorf("install-journal-signature-invalid")
		}
		zero(signature)
		zero(payloadRaw)
		if err := validateJournalEventBase(payload, matches[1], sequence, matches[3], keyID); err != nil {
			return nil, err
		}
		transactions[matches[1]] = append(transactions[matches[1]], journalDiskEvent{name: entry.Name(), event: matches[3], txID: matches[1], sequence: sequence, payload: payload})
	}
	result := &journalReplay{}
	for txID, events := range transactions {
		sort.Slice(events, func(left, right int) bool { return events[left].sequence < events[right].sequence })
		active := false
		lastAttempt := int64(0)
		lastEvent := ""
		var targets []*installTarget
		var immutable map[string]string
		for index, diskEvent := range events {
			if diskEvent.sequence != int64(index+1) {
				return nil, fmt.Errorf("install-journal-sequence-gap")
			}
			attempt, ok := exactInt(diskEvent.payload["attempt"], 1, 1<<62)
			if !ok || !knownJournalEvent(diskEvent.event) {
				return nil, fmt.Errorf("install-journal-state-invalid")
			}
			currentImmutable := journalImmutableFields(diskEvent.payload)
			if immutable == nil {
				immutable = currentImmutable
			} else if !equalStringMaps(immutable, currentImmutable) {
				return nil, fmt.Errorf("install-journal-transaction-rebound")
			}
			if diskEvent.event == "admitted" {
				if active || attempt != lastAttempt+1 {
					return nil, fmt.Errorf("install-journal-attempt-order-invalid")
				}
				lastAttempt = attempt
				active = true
				if txID == currentTxID {
					parsed, parseErr := installer.parseTargetSnapshots(diskEvent.payload["targets"], verified, currentTxID)
					if parseErr != nil {
						return nil, parseErr
					}
					hadPrior, hadPriorOK := diskEvent.payload["had_prior_install"].(bool)
					if !hadPriorOK || hadPrior != targetsHadPrior(parsed) {
						return nil, fmt.Errorf("install-journal-prior-state-invalid")
					}
					targets = parsed
				}
			} else if !active || attempt != lastAttempt {
				return nil, fmt.Errorf("install-journal-event-order-invalid")
			}
			if terminalJournalEvent(diskEvent.event) {
				active = false
			}
			lastEvent = diskEvent.event
		}
		if immutable["backup_encryption_key_id"] != installer.backupEncryptionKeyID {
			return nil, fmt.Errorf("install-backup-key-rotation-forbidden")
		}
		result.backupKeyContinuityRecordObserved = true
		if txID != currentTxID {
			if active {
				return nil, fmt.Errorf("install-journal-foreign-transaction-incomplete")
			}
			continue
		}
		if err := validateCurrentJournalBinding(immutable, verified, installer.backupEncryptionKeyID); err != nil {
			return nil, err
		}
		result.lastSequence = events[len(events)-1].sequence
		result.lastAttempt = lastAttempt
		result.lastEvent = lastEvent
		result.targets = targets
	}
	return result, nil
}

func validateJournalEventBase(payload map[string]any, txID string, sequence int64, event, keyID string) error {
	schema, _ := payload["schema"].(string)
	version, versionOK := exactInt(payload["version"], 2, 2)
	payloadTx, _ := payload["transaction_id"].(string)
	payloadEvent, _ := payload["event"].(string)
	payloadSequence, sequenceOK := exactInt(payload["sequence"], 1, 1<<62)
	payloadKeyID, _ := payload["receipt_authority_key_id"].(string)
	archiveDigest, _ := payload["archive_digest"].(string)
	configDigest, _ := payload["config_digest"].(string)
	hostDigest, _ := payload["host_machine_id_digest"].(string)
	packageKeyID, _ := payload["package_authority_key_id"].(string)
	backupKeyID, _ := payload["backup_encryption_key_id"].(string)
	runtimeSHA, _ := payload["runtime_sha"].(string)
	generation, generationOK := exactInt(payload["release_generation"], 1, 1<<62)
	attempt, attemptOK := exactInt(payload["attempt"], 1, 1<<62)
	if schema != "propertyquarry.release-control.single-host-install-journal-event.v2" || !versionOK || version != 2 || payloadTx != txID || payloadEvent != event || !sequenceOK || payloadSequence != sequence || payloadKeyID != keyID || !digestPattern.MatchString(archiveDigest) || !digestPattern.MatchString(backupKeyID) || !digestPattern.MatchString(configDigest) || !digestPattern.MatchString(hostDigest) || !digestPattern.MatchString(packageKeyID) || !digestPattern.MatchString(payloadKeyID) || !shaPattern.MatchString(runtimeSHA) || !generationOK || generation < 1 || !attemptOK || attempt < 1 {
		return fmt.Errorf("install-journal-filename-binding-invalid")
	}
	return nil
}

func journalImmutableFields(payload map[string]any) map[string]string {
	result := map[string]string{}
	for _, name := range []string{"archive_digest", "backup_encryption_key_id", "config_digest", "host_machine_id_digest", "package_authority_key_id", "receipt_authority_key_id", "runtime_sha"} {
		value, _ := payload[name].(string)
		result[name] = value
	}
	generation, _ := exactInt(payload["release_generation"], 1, 1<<62)
	result["release_generation"] = strconv.FormatInt(generation, 10)
	return result
}

func validateCurrentJournalBinding(fields map[string]string, verified *VerifiedPackage, backupKeyID string) error {
	if fields == nil || fields["archive_digest"] != verified.ArchiveDigest || fields["backup_encryption_key_id"] != backupKeyID || fields["config_digest"] != verified.ConfigDigest || fields["host_machine_id_digest"] != verified.HostMachineIDDigest || fields["package_authority_key_id"] != verified.PackageAuthorityKeyID || fields["receipt_authority_key_id"] != verified.ReceiptAuthorityKeyID || fields["runtime_sha"] != verified.RuntimeSHA || fields["release_generation"] != strconv.FormatInt(verified.ReleaseGeneration, 10) {
		return fmt.Errorf("install-journal-package-binding-invalid")
	}
	return nil
}

func equalStringMaps(left, right map[string]string) bool {
	if len(left) != len(right) {
		return false
	}
	for key, value := range left {
		if right[key] != value {
			return false
		}
	}
	return true
}

func knownJournalEvent(event string) bool {
	switch event {
	case "admitted", "staged", "deactivated", "deactivation-failed", "file-installed", "files-installed", "activated", "succeeded", "rollback-deactivation-failed", "rollback-started", "rollback-failed", "rolled-back", "recovered-prior":
		return true
	default:
		return false
	}
}

func terminalJournalEvent(event string) bool {
	switch event {
	case "deactivation-failed", "succeeded", "rolled-back", "recovered-prior":
		return true
	default:
		return false
	}
}

func (installer *Installer) recoverInterruptedAttempt(verified *VerifiedPackage, targets []*installTarget, lastEvent string, appendEvent func(string, map[string]any) error) (installReceiptState, error) {
	hadPrior := targetsHadPrior(targets)
	state := installReceiptState{disposition: "recovery-failed", recoveryPerformed: true, hadPrior: hadPrior, candidateInstalled: installer.candidateTargetsComplete(targets)}
	if len(targets) == 0 {
		return state, fmt.Errorf("install-journal-recovery-targets-missing")
	}
	mutationObserved, err := installer.mutationObserved(targets)
	if err != nil {
		return state, err
	}
	if mutationObserved {
		state.deactivationPerformed = true
		if err := installer.Deactivate(); err != nil {
			state.disposition = "recovery-deactivation-failed"
			_ = appendEvent("rollback-deactivation-failed", map[string]any{"recovery": true})
			return state, fmt.Errorf("install-recovery-deactivation-failed")
		}
		state.deactivationSucceeded = true
	}
	state.candidateInstalled = false
	if err := installer.restorePreviousTargets(targets); err != nil {
		if err == errInstallInterrupted {
			return state, err
		}
		state.candidateInstalled = installer.candidateTargetsComplete(targets)
		state.previousStateRestored = installer.previousTargetsComplete(targets)
		state.priorRestored = hadPrior && state.previousStateRestored
		state.disposition = "recovery-rollback-failed"
		_ = appendEvent("rollback-failed", map[string]any{"recovery": true})
		return state, err
	}
	state.previousStateRestored = true
	state.priorRestored = hadPrior
	state.recoverySucceeded = true
	if hadPrior {
		state.reactivationPerformed = true
		if err := installer.reactivateCurrent(); err != nil {
			state.disposition = "recovered-prior-reactivation-failed"
			if appendErr := appendEvent("recovered-prior", map[string]any{"reactivation_succeeded": false}); appendErr != nil {
				state.disposition = "recovery-journal-failed"
			}
			return state, fmt.Errorf("install-recovery-reactivation-failed")
		}
		state.reactivationSucceeded = true
		state.socketActive = true
	}
	if err := appendEvent("recovered-prior", map[string]any{"reactivation_succeeded": state.reactivationSucceeded}); err != nil {
		state.disposition = "recovery-journal-failed"
		return state, err
	}
	state.disposition = "recovered-prior"
	_ = lastEvent
	return state, nil
}

func (installer *Installer) rollbackFailedInstall(targets []*installTarget, hadPrior, activationAttempted, activationSucceeded bool, activationCanary *authority.ActivationCanaryProof, appendEvent func(string, map[string]any) error) installReceiptState {
	state := installReceiptState{disposition: "rollback-failed", candidateInstalled: activationAttempted, hadPrior: hadPrior, activationPerformed: activationAttempted, activationSucceeded: activationSucceeded, activationCanary: activationCanary, deactivationPerformed: hadPrior, deactivationSucceeded: hadPrior}
	if activationAttempted {
		state.deactivationPerformed = true
		if err := installer.Deactivate(); err != nil {
			state.deactivationSucceeded = false
			state.disposition = "rollback-deactivation-failed"
			_ = appendEvent("rollback-deactivation-failed", map[string]any{"recovery": false})
			return state
		}
		state.deactivationSucceeded = true
	}
	journalFailed := appendEvent("rollback-started", map[string]any{"activation_attempted": activationAttempted}) != nil
	state.rollbackPerformed = true
	state.candidateInstalled = false
	if err := installer.restorePreviousTargets(targets); err != nil {
		state.candidateInstalled = installer.candidateTargetsComplete(targets)
		state.previousStateRestored = installer.previousTargetsComplete(targets)
		state.priorRestored = hadPrior && state.previousStateRestored
		state.disposition = "rollback-failed"
		_ = appendEvent("rollback-failed", map[string]any{"recovery": false})
		return state
	}
	state.rollbackSucceeded = true
	state.previousStateRestored = true
	state.priorRestored = hadPrior
	if hadPrior {
		state.reactivationPerformed = true
		if err := installer.reactivateCurrent(); err != nil {
			state.disposition = "prior-restored-reactivation-failed"
		} else {
			state.reactivationSucceeded = true
			state.socketActive = true
		}
	}
	if journalFailed {
		state.disposition = "rollback-journal-failed"
		return state
	}
	if err := appendEvent("rolled-back", map[string]any{"prior_authority_restored": state.priorRestored, "reactivation_succeeded": state.reactivationSucceeded, "rollback_succeeded": true}); err != nil {
		state.disposition = "rollback-journal-failed"
		return state
	}
	if state.disposition != "prior-restored-reactivation-failed" {
		state.disposition = "rolled-back"
	}
	return state
}

func successfulReceiptState(disposition string, hadPrior bool, activationCanary *authority.ActivationCanaryProof) installReceiptState {
	return installReceiptState{disposition: disposition, candidateInstalled: true, socketActive: true, activationPerformed: true, activationSucceeded: true, activationCanary: activationCanary, hadPrior: hadPrior, deactivationPerformed: hadPrior, deactivationSucceeded: hadPrior}
}

func mergeRecoveryState(current, recovery installReceiptState) installReceiptState {
	if !recovery.recoveryPerformed {
		return current
	}
	current.recoveryPerformed = true
	current.recoverySucceeded = recovery.recoverySucceeded
	current.previousStateRestored = current.previousStateRestored || recovery.previousStateRestored
	current.priorRestored = current.priorRestored || recovery.priorRestored
	if !current.deactivationPerformed && recovery.deactivationPerformed {
		current.deactivationPerformed = true
		current.deactivationSucceeded = recovery.deactivationSucceeded
	}
	if !current.reactivationPerformed && recovery.reactivationPerformed {
		current.reactivationPerformed = true
		current.reactivationSucceeded = recovery.reactivationSucceeded
	}
	return current
}

func targetsHadPrior(targets []*installTarget) bool {
	for _, target := range targets {
		if target.path == "/etc/propertyquarry-release-single-host-v2/package-manifest.v2.json" {
			return target.priorPresent
		}
	}
	return false
}

func targetsContainPrior(targets []*installTarget) bool {
	for _, target := range targets {
		if target.priorPresent {
			return true
		}
	}
	return false
}

func (installer *Installer) mutationObserved(targets []*installTarget) (bool, error) {
	for _, target := range targets {
		if _, err := os.Lstat(target.backup); err == nil {
			return true, nil
		} else if !os.IsNotExist(err) {
			return false, err
		}
		hostPath, _ := installer.hostPath(target.path)
		if target.priorPresent {
			if !installer.targetMatches(hostPath, target.priorMode, target.priorSize, target.priorDigest) {
				if installer.targetMatches(hostPath, target.mode, target.size, target.digest) {
					return true, nil
				}
				return false, fmt.Errorf("install-recovery-host-state-invalid")
			}
		} else if _, err := os.Lstat(hostPath); err == nil {
			if installer.targetMatches(hostPath, target.mode, target.size, target.digest) {
				return true, nil
			}
			return false, fmt.Errorf("install-recovery-host-state-invalid")
		} else if !os.IsNotExist(err) {
			return false, err
		}
	}
	return false, nil
}

func (installer *Installer) candidateTargetsComplete(targets []*installTarget) bool {
	if len(targets) == 0 {
		return false
	}
	for _, target := range targets {
		hostPath, _ := installer.hostPath(target.path)
		if !installer.targetMatches(hostPath, target.mode, target.size, target.digest) {
			return false
		}
	}
	return true
}

func (installer *Installer) previousTargetsComplete(targets []*installTarget) bool {
	if len(targets) == 0 {
		return false
	}
	for _, target := range targets {
		hostPath, _ := installer.hostPath(target.path)
		if target.priorPresent {
			if !installer.targetMatches(hostPath, target.priorMode, target.priorSize, target.priorDigest) {
				return false
			}
		} else if _, err := os.Lstat(hostPath); !os.IsNotExist(err) {
			return false
		}
	}
	return true
}

func (installer *Installer) pathMatchesOrAbsent(path string, mode os.FileMode, size int64, expectedDigest string) (bool, error) {
	if _, err := os.Lstat(path); os.IsNotExist(err) {
		return false, nil
	} else if err != nil {
		return false, err
	}
	if !installer.targetMatches(path, mode, size, expectedDigest) {
		return false, fmt.Errorf("install-artifact-content-invalid")
	}
	return true, nil
}

func (installer *Installer) targetMatches(path string, mode os.FileMode, size int64, expectedDigest string) bool {
	if size < 1 || size > maximumMemberBytes || !digestPattern.MatchString(expectedDigest) {
		return false
	}
	raw, err := readExactFile(path, mode, installer.OwnerUID, installer.OwnerGID, maximumMemberBytes)
	if err != nil {
		return false
	}
	defer zero(raw)
	return int64(len(raw)) == size && digest(raw) == expectedDigest
}

func validOwnedRegular(info os.FileInfo, uid, gid uint32) bool {
	metadata, ok := info.Sys().(*syscall.Stat_t)
	return ok && info.Mode().IsRegular() && info.Mode()&os.ModeSymlink == 0 && metadata.Uid == uid && metadata.Gid == gid && metadata.Nlink == 1
}

func (installer *Installer) interrupted(point string) bool {
	return installer.Interrupt != nil && installer.Interrupt(point)
}

func (installer *Installer) verifyInstalledFiles(verified *VerifiedPackage) error {
	for path, file := range verified.Files {
		hostPath, _ := installer.hostPath(path)
		raw, err := readExactFile(hostPath, file.Mode, installer.OwnerUID, installer.OwnerGID, int(file.Size))
		if err != nil || int64(len(raw)) != file.Size || digest(raw) != file.Digest {
			zero(raw)
			return fmt.Errorf("installed-file-invalid")
		}
		zero(raw)
	}
	return nil
}

func (installer *Installer) signedReceipt(verified *VerifiedPackage, key ed25519.PrivateKey, state installReceiptState) ([]byte, error) {
	canaryReceipt := any(nil)
	canaryDigest, canaryChallenge, canaryUnit := "", "", ""
	canaryVerifiedAt, canaryValidUntil := int64(0), int64(0)
	if state.activationCanary != nil {
		canaryReceipt = state.activationCanary.Receipt
		canaryDigest = state.activationCanary.ReceiptDigest
		canaryChallenge = state.activationCanary.ChallengeDigest
		canaryUnit = state.activationCanary.UnitDigest
		canaryVerifiedAt = state.activationCanary.VerifiedAt
		canaryValidUntil = state.activationCanary.ValidUntil
	}
	payload := map[string]any{
		"archive_digest": verified.ArchiveDigest, "authority_installed": state.candidateInstalled,
		"activation_performed": state.activationPerformed, "activation_succeeded": state.activationSucceeded,
		"activation_canary_challenge_sha256": canaryChallenge, "activation_canary_receipt": canaryReceipt,
		"activation_canary_receipt_digest": canaryDigest, "activation_canary_unit_sha256": canaryUnit,
		"activation_canary_valid_until": json.Number(strconv.FormatInt(canaryValidUntil, 10)),
		"activation_canary_verified":    state.activationCanary != nil,
		"activation_canary_verified_at": json.Number(strconv.FormatInt(canaryVerifiedAt, 10)),
		"authority_profile":             "single-host-production-v2", "candidate_authority_installed": state.candidateInstalled,
		"backup_encryption_key_created": installer.backupEncryptionKeyCreated, "backup_encryption_key_id": installer.backupEncryptionKeyID,
		"config_digest": verified.ConfigDigest, "deactivation_performed": state.deactivationPerformed,
		"deactivation_succeeded": state.deactivationSucceeded, "disposition": state.disposition,
		"envelope_sha": verified.EnvelopeSHA, "host_machine_id_digest": verified.HostMachineIDDigest,
		"installed_at": json.Number(strconv.FormatInt(time.Now().UTC().Unix(), 10)), "installer_binary_sha256": executableDigest(),
		"installer_source_manifest_digest": InstallerSourceManifestDigest, "package_authority_key_id": verified.PackageAuthorityKeyID,
		"plan_digest": verified.PlanDigest, "previous_state_restored": state.previousStateRestored,
		"prior_authority_restored": state.priorRestored, "production_release_performed": false, "production_ready": false,
		"recovery_performed": state.recoveryPerformed, "recovery_succeeded": state.recoverySucceeded,
		"reactivation_performed": state.reactivationPerformed, "reactivation_succeeded": state.reactivationSucceeded,
		"receipt_authority_key_id": verified.ReceiptAuthorityKeyID, "release_generation": json.Number(strconv.FormatInt(verified.ReleaseGeneration, 10)),
		"rollback_performed": state.rollbackPerformed, "rollback_succeeded": state.rollbackSucceeded,
		"runtime_sha": verified.RuntimeSHA, "workflow_sha": verified.WorkflowSHA, "schema": "propertyquarry.release-control.single-host-install-receipt.v2",
		"systemd_socket_active": state.socketActive, "upgraded_existing_authority": state.hadPrior, "version": json.Number("2"),
	}
	return signedWire(payload, key, verified.ReceiptAuthorityKeyID)
}

func signedJournalWire(payload map[string]any, key ed25519.PrivateKey, keyID string) ([]byte, error) {
	payloadRaw, err := canonicalJSON(payload)
	if err != nil {
		return nil, err
	}
	defer zero(payloadRaw)
	signature := ed25519.Sign(key, framed(installJournalDomain, payloadRaw))
	defer zero(signature)
	wrapper := map[string]any{"payload": payload, "signature": base64.RawURLEncoding.EncodeToString(signature), "signature_key_id": keyID}
	return canonicalJSON(wrapper)
}

func signedWire(payload map[string]any, key ed25519.PrivateKey, keyID string) ([]byte, error) {
	payloadRaw, err := canonicalJSON(payload)
	if err != nil {
		return nil, err
	}
	defer zero(payloadRaw)
	signature := ed25519.Sign(key, framed(installReceiptDomain, payloadRaw))
	defer zero(signature)
	wrapper := map[string]any{"payload": payload, "signature": base64.RawURLEncoding.EncodeToString(signature), "signature_key_id": keyID}
	return canonicalJSON(wrapper)
}

func (installer *Installer) acquireInstallLock() (*os.File, error) {
	etcPath, err := installer.hostPath("/etc")
	if err != nil {
		return nil, err
	}
	if err := validateDirectoryChain(installer.HostRoot, etcPath, installer.OwnerUID); err != nil {
		return nil, fmt.Errorf("install-lock-parent-invalid")
	}
	lockPath := filepath.Join(etcPath, ".propertyquarry-release-single-host-v2.install.lock")
	lock, err := os.OpenFile(lockPath, os.O_CREATE|os.O_EXCL|os.O_RDWR|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0o600)
	if err == nil {
		if chownErr := lock.Chown(int(installer.OwnerUID), int(installer.OwnerGID)); chownErr != nil {
			lock.Close()
			_ = os.Remove(lockPath)
			return nil, fmt.Errorf("install-lock-chown-failed")
		}
		if chmodErr := lock.Chmod(0o600); chmodErr != nil {
			lock.Close()
			_ = os.Remove(lockPath)
			return nil, fmt.Errorf("install-lock-chmod-failed")
		}
		if syncErr := lock.Sync(); syncErr != nil {
			lock.Close()
			_ = os.Remove(lockPath)
			return nil, fmt.Errorf("install-lock-sync-failed")
		}
		if syncErr := syncDirectory(etcPath); syncErr != nil {
			lock.Close()
			return nil, syncErr
		}
	} else if os.IsExist(err) {
		lock, err = os.OpenFile(lockPath, os.O_RDWR|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	}
	if err != nil {
		return nil, fmt.Errorf("install-lock-unavailable")
	}
	if err := installer.validateLock(lock); err != nil {
		lock.Close()
		return nil, err
	}
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX); err != nil {
		lock.Close()
		return nil, fmt.Errorf("install-lock-failed")
	}
	if err := installer.validateLock(lock); err != nil {
		lock.Close()
		return nil, err
	}
	return lock, nil
}

func (installer *Installer) ensureDirectory(absolute string, mode os.FileMode, scope string) error {
	path, err := installer.hostPath(absolute)
	if err != nil {
		return err
	}
	if err := validateDirectoryChain(installer.HostRoot, filepath.Dir(path), installer.OwnerUID); err != nil {
		return fmt.Errorf("install-directory-parent-invalid")
	}
	created := false
	if err := os.Mkdir(path, mode); err == nil {
		created = true
		if installer.interrupted(scope + "-after-mkdir") {
			return errInstallInterrupted
		}
	} else if !os.IsExist(err) {
		return fmt.Errorf("install-directory-create-failed")
	}
	if created {
		if err := os.Chown(path, int(installer.OwnerUID), int(installer.OwnerGID)); err != nil {
			return fmt.Errorf("install-directory-chown-failed")
		}
		if installer.interrupted(scope + "-after-chown") {
			return errInstallInterrupted
		}
		if err := os.Chmod(path, mode); err != nil {
			return fmt.Errorf("install-directory-chmod-failed")
		}
		if installer.interrupted(scope + "-after-chmod") {
			return errInstallInterrupted
		}
	}
	info, err := os.Lstat(path)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("install-directory-metadata-invalid")
	}
	metadata, ok := info.Sys().(*syscall.Stat_t)
	if !ok || metadata.Uid != installer.OwnerUID || metadata.Gid != installer.OwnerGID {
		return fmt.Errorf("install-directory-owner-invalid")
	}
	if info.Mode().Perm() != mode {
		if !installer.preAdmissionRequired || installer.preAdmission == nil || info.Mode().Perm()&^mode != 0 {
			return fmt.Errorf("install-directory-metadata-invalid")
		}
		if err := os.Chmod(path, mode); err != nil {
			return fmt.Errorf("install-directory-chmod-failed")
		}
	}
	if err := syncDirectory(filepath.Dir(path)); err != nil {
		return err
	}
	if installer.interrupted(scope + "-after-parent-fsync") {
		return errInstallInterrupted
	}
	return nil
}

func (installer *Installer) hostPath(absolute string) (string, error) {
	if !filepath.IsAbs(absolute) || filepath.Clean(absolute) != absolute {
		return "", fmt.Errorf("install-path-invalid")
	}
	path := filepath.Join(installer.HostRoot, strings.TrimPrefix(absolute, "/"))
	relative, err := filepath.Rel(installer.HostRoot, path)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(os.PathSeparator)) {
		return "", fmt.Errorf("install-path-escape")
	}
	return path, nil
}

func (installer *Installer) validateLock(lock *os.File) error {
	info, err := lock.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm() != 0o600 {
		return fmt.Errorf("install-lock-invalid")
	}
	metadata, ok := info.Sys().(*syscall.Stat_t)
	if !ok || metadata.Uid != installer.OwnerUID || metadata.Gid != installer.OwnerGID || metadata.Nlink != 1 {
		return fmt.Errorf("install-lock-owner-invalid")
	}
	return nil
}

func validateDirectoryChain(root, path string, owner uint32) error {
	root = filepath.Clean(root)
	current := filepath.Clean(path)
	relative, err := filepath.Rel(root, current)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(os.PathSeparator)) {
		return fmt.Errorf("directory-chain-escape")
	}
	for {
		info, err := os.Lstat(current)
		if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0o022 != 0 {
			return fmt.Errorf("directory-invalid")
		}
		metadata, ok := info.Sys().(*syscall.Stat_t)
		if !ok || metadata.Uid != owner {
			return fmt.Errorf("directory-owner-invalid")
		}
		if current == root {
			return nil
		}
		next := filepath.Dir(current)
		if next == current {
			return fmt.Errorf("directory-chain-boundary-missing")
		}
		current = next
	}
}

func validateExternalDirectoryChain(root, path string, ownerUID, ownerGID uint32) error {
	root = filepath.Clean(root)
	current := filepath.Clean(path)
	relative, err := filepath.Rel(root, current)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(os.PathSeparator)) {
		return fmt.Errorf("external-directory-chain-escape")
	}
	for {
		info, err := os.Lstat(current)
		if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0o002 != 0 {
			return fmt.Errorf("external-directory-invalid")
		}
		metadata, ok := info.Sys().(*syscall.Stat_t)
		if !ok || (metadata.Uid != 0 && metadata.Uid != ownerUID) || (info.Mode().Perm()&0o020 != 0 && (metadata.Uid != ownerUID || metadata.Gid != ownerGID)) {
			return fmt.Errorf("external-directory-owner-invalid")
		}
		if current == root {
			return nil
		}
		next := filepath.Dir(current)
		if next == current {
			return fmt.Errorf("external-directory-chain-boundary-missing")
		}
		current = next
	}
}

func readExactFile(path string, mode os.FileMode, uid, gid uint32, maximum int) ([]byte, error) {
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm() != mode || info.Size() < 1 || info.Size() > int64(maximum) {
		return nil, fmt.Errorf("file-metadata-invalid")
	}
	metadata, ok := info.Sys().(*syscall.Stat_t)
	if !ok || metadata.Uid != uid || metadata.Gid != gid || metadata.Nlink != 1 {
		return nil, fmt.Errorf("file-owner-invalid")
	}
	raw := make([]byte, info.Size())
	if _, err := io.ReadFull(file, raw); err != nil {
		zero(raw)
		return nil, err
	}
	after, err := file.Stat()
	if err != nil || !os.SameFile(info, after) {
		zero(raw)
		return nil, fmt.Errorf("file-changed")
	}
	return raw, nil
}

func (installer *Installer) writeNoReplace(path string, raw []byte, mode os.FileMode, uid, gid uint32, scope string) error {
	checkpoint := func(operation string) error {
		if installer.interrupted(scope + "-after-" + operation) {
			return errInstallInterrupted
		}
		return nil
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, mode)
	if err != nil {
		return fmt.Errorf("file-create-failed")
	}
	succeeded := false
	defer func() {
		_ = file.Close()
		if !succeeded {
			_ = os.Remove(path)
		}
	}()
	if err := checkpoint("create"); err != nil {
		return err
	}
	if err := file.Chown(int(uid), int(gid)); err != nil {
		return fmt.Errorf("file-chown-failed")
	}
	if err := checkpoint("chown"); err != nil {
		return err
	}
	if err := file.Chmod(mode); err != nil {
		return fmt.Errorf("file-chmod-failed")
	}
	if err := checkpoint("chmod"); err != nil {
		return err
	}
	writeAll := func(chunk []byte) error {
		for len(chunk) > 0 {
			written, writeErr := file.Write(chunk)
			if writeErr != nil || written <= 0 || written > len(chunk) {
				return fmt.Errorf("file-write-failed")
			}
			chunk = chunk[written:]
		}
		return nil
	}
	split := len(raw)
	if split > 1 {
		split /= 2
	}
	if err := writeAll(raw[:split]); err != nil {
		return fmt.Errorf("file-write-failed")
	}
	if err := checkpoint("partial-write"); err != nil {
		return err
	}
	if err := writeAll(raw[split:]); err != nil {
		return fmt.Errorf("file-write-failed")
	}
	if err := checkpoint("write"); err != nil {
		return err
	}
	if err := file.Sync(); err != nil {
		return fmt.Errorf("file-sync-failed")
	}
	if err := checkpoint("file-fsync"); err != nil {
		return err
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("file-close-failed")
	}
	if err := checkpoint("close"); err != nil {
		return err
	}
	if err := syncDirectory(filepath.Dir(path)); err != nil {
		return err
	}
	if err := checkpoint("parent-fsync"); err != nil {
		return err
	}
	succeeded = true
	return nil
}

func renameNoReplace(source, destination string) error {
	sourceRaw, err := syscall.BytePtrFromString(source)
	if err != nil {
		return err
	}
	destinationRaw, err := syscall.BytePtrFromString(destination)
	if err != nil {
		return err
	}
	const renameNoReplaceFlag = 1
	const atFDCWD = ^uintptr(99)
	_, _, errno := syscall.RawSyscall6(linuxAMD64Renameat2, atFDCWD, uintptr(unsafe.Pointer(sourceRaw)), atFDCWD, uintptr(unsafe.Pointer(destinationRaw)), uintptr(renameNoReplaceFlag), 0)
	if errno != 0 {
		return errno
	}
	return nil
}

func syncDirectory(path string) error {
	directory, err := os.OpenFile(path, os.O_RDONLY|syscall.O_CLOEXEC|syscall.O_DIRECTORY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return fmt.Errorf("directory-open-failed")
	}
	defer directory.Close()
	if err := directory.Sync(); err != nil {
		return fmt.Errorf("directory-sync-failed")
	}
	return nil
}

func unwrapPathError(err error) error {
	if pathError, ok := err.(*os.PathError); ok {
		return pathError.Err
	}
	return err
}

func executableDigest() string {
	digest, _, err := executableIdentity()
	if err != nil {
		return "sha256:unavailable"
	}
	return digest
}

func validateInstallerSelfBinding(verified *VerifiedPackage) error {
	if verified == nil || !digestPattern.MatchString(verified.InstallerBinaryDigest) || verified.InstallerBinarySize < 1 || verified.InstallerBinarySize > maximumMemberBytes {
		return fmt.Errorf("installer-self-binding-invalid")
	}
	actualDigest, actualSize, info, err := executableIdentityDetails()
	if err != nil {
		return fmt.Errorf("installer-self-binding-mismatch")
	}
	metadata, ok := info.Sys().(*syscall.Stat_t)
	if !ok || info.Mode().Perm() != 0o555 || metadata.Uid != 0 || metadata.Gid != 0 || metadata.Nlink != 1 {
		return fmt.Errorf("installer-self-metadata-invalid")
	}
	return installerIdentityBindingMatches(verified, actualDigest, actualSize)
}

func installerIdentityBindingMatches(verified *VerifiedPackage, actualDigest string, actualSize int64) error {
	if verified == nil || actualDigest != verified.InstallerBinaryDigest || actualSize != verified.InstallerBinarySize {
		return fmt.Errorf("installer-self-binding-mismatch")
	}
	return nil
}

func executableIdentity() (string, int64, error) {
	digestValue, size, _, err := executableIdentityDetails()
	return digestValue, size, err
}

func executableIdentityDetails() (string, int64, os.FileInfo, error) {
	file, err := os.OpenFile("/proc/self/exe", os.O_RDONLY|syscall.O_CLOEXEC, 0)
	if err != nil {
		return "", 0, nil, err
	}
	defer file.Close()
	before, err := file.Stat()
	if err != nil || !before.Mode().IsRegular() || before.Size() < 1 || before.Size() > maximumMemberBytes {
		return "", 0, nil, fmt.Errorf("installer-executable-metadata-invalid")
	}
	hasher := sha256.New()
	written, err := io.Copy(hasher, io.LimitReader(file, before.Size()+1))
	if err != nil || written != before.Size() {
		return "", 0, nil, fmt.Errorf("installer-executable-read-failed")
	}
	after, err := file.Stat()
	if err != nil || !os.SameFile(before, after) || after.Size() != before.Size() {
		return "", 0, nil, fmt.Errorf("installer-executable-changed")
	}
	return "sha256:" + fmt.Sprintf("%x", hasher.Sum(nil)), before.Size(), before, nil
}

func (installer *Installer) activateCandidate(verified *VerifiedPackage) (*authority.ActivationCanaryProof, error) {
	if installer == nil || verified == nil || installer.Activate == nil {
		return nil, fmt.Errorf("activation-canary-input-invalid")
	}
	attempt, err := installer.Activate()
	if err != nil {
		if err == errActivationAlreadyAborted {
			return nil, err
		}
		if installer.AbortActivation == nil || installer.AbortActivation() != nil {
			return nil, fmt.Errorf("activation-failed-and-deactivation-failed")
		}
		return nil, err
	}
	if attempt == nil {
		if installer.AbortActivation == nil || installer.AbortActivation() != nil {
			return nil, fmt.Errorf("activation-canary-attempt-and-deactivation-failed")
		}
		return nil, fmt.Errorf("activation-canary-attempt-invalid")
	}
	defer zero(attempt.receipt)
	compensate := func(cause error) error {
		if installer.AbortActivation == nil || installer.AbortActivation() != nil {
			return fmt.Errorf("activation-canary-verification-and-deactivation-failed")
		}
		return cause
	}
	anchorRecord := verified.Files["/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.pem"]
	controllerRecord := verified.Files[authority.ControllerBinaryPath]
	unitRecord := verified.Files[authority.ActivationCanaryUnitPath]
	if anchorRecord == nil || controllerRecord == nil || unitRecord == nil {
		return nil, compensate(fmt.Errorf("activation-canary-package-binding-invalid"))
	}
	public, der, keyID, err := parsePublicPEM(anchorRecord.Data)
	zero(der)
	if err != nil || keyID != verified.ReceiptAuthorityKeyID {
		zero(public)
		return nil, compensate(fmt.Errorf("activation-canary-key-invalid"))
	}
	defer zero(public)
	expected := authority.ActivationCanaryExpected{
		ChallengeDigest: attempt.challengeDigest, ChallengeCreatedAt: attempt.challengeCreatedAt, CanaryStartedAt: attempt.canaryStartedAt,
		ConfigDigest: verified.ConfigDigest, ControllerDigest: controllerRecord.Digest,
		PackageManifestDigest: digest(verified.ManifestRaw), PlanDigest: verified.PlanDigest, UnitDigest: unitRecord.Digest,
		RuntimeSHA: verified.RuntimeSHA, WorkflowSHA: verified.WorkflowSHA, PackageAuthorityKeyID: verified.PackageAuthorityKeyID,
		ReceiptAuthorityKeyID: verified.ReceiptAuthorityKeyID,
	}
	proof, err := authority.VerifyActivationCanaryReceipt(attempt.receipt, public, expected, time.Now().UTC())
	if err != nil {
		return nil, compensate(err)
	}
	return proof, nil
}

func (installer *Installer) reactivateCurrent() error {
	if installer == nil || installer.Activate == nil {
		return fmt.Errorf("activation-unavailable")
	}
	attempt, err := installer.Activate()
	if err != nil {
		if err == errActivationAlreadyAborted {
			return err
		}
		if installer.AbortActivation == nil || installer.AbortActivation() != nil {
			return fmt.Errorf("reactivation-failed-and-abort-failed")
		}
		return err
	}
	if attempt == nil {
		if installer.AbortActivation == nil || installer.AbortActivation() != nil {
			return fmt.Errorf("reactivation-proof-and-abort-failed")
		}
		return fmt.Errorf("reactivation-proof-invalid")
	}
	defer zero(attempt.receipt)
	expected, public, verifyErr := installedActivationExpected(installer.HostRoot, attempt.challengeDigest)
	if verifyErr == nil {
		expected.ChallengeCreatedAt = attempt.challengeCreatedAt
		expected.CanaryStartedAt = attempt.canaryStartedAt
		var proof *authority.ActivationCanaryProof
		proof, verifyErr = authority.VerifyActivationCanaryReceipt(attempt.receipt, public, expected, time.Now().UTC())
		if verifyErr == nil && (attempt.installedStateProof == nil || attempt.installedStateProof.ReceiptDigest != proof.ReceiptDigest || attempt.installedStateProof.ChallengeDigest != proof.ChallengeDigest || attempt.installedStateProof.UnitDigest != proof.UnitDigest || attempt.installedStateProof.VerifiedAt != proof.VerifiedAt || attempt.installedStateProof.ValidUntil != proof.ValidUntil) {
			verifyErr = fmt.Errorf("reactivation-proof-rebound")
		}
	}
	zero(public)
	if verifyErr != nil {
		if installer.AbortActivation == nil || installer.AbortActivation() != nil {
			return fmt.Errorf("reactivation-proof-and-abort-failed")
		}
		return fmt.Errorf("reactivation-proof-invalid")
	}
	return nil
}

func installedActivationExpected(root, challengeDigest string) (authority.ActivationCanaryExpected, ed25519.PublicKey, error) {
	if !filepath.IsAbs(root) || filepath.Clean(root) != root || !digestPattern.MatchString(challengeDigest) {
		return authority.ActivationCanaryExpected{}, nil, fmt.Errorf("installed-activation-binding-invalid")
	}
	ownerUID, ownerGID := uint32(os.Geteuid()), uint32(os.Getegid())
	if root == "/" || root == FixedHostRoot {
		ownerUID, ownerGID = 0, 0
	}
	path := func(absolute string) string {
		if root == "/" {
			return absolute
		}
		return filepath.Join(root, strings.TrimPrefix(absolute, "/"))
	}
	configRaw, err := readExactFile(path(authority.ConfigPath), 0o400, ownerUID, ownerGID, maximumManifestBytes)
	if err != nil {
		return authority.ActivationCanaryExpected{}, nil, fmt.Errorf("installed-activation-config-invalid")
	}
	defer zero(configRaw)
	configSignature, err := readExactFile(path(authority.ConfigSignaturePath), 0o444, ownerUID, ownerGID, ed25519.SignatureSize)
	if err != nil {
		return authority.ActivationCanaryExpected{}, nil, fmt.Errorf("installed-activation-config-invalid")
	}
	defer zero(configSignature)
	packageAnchorRaw, err := readExactFile(path(authority.PackageAnchorPath), 0o444, ownerUID, ownerGID, 4096)
	if err != nil {
		return authority.ActivationCanaryExpected{}, nil, fmt.Errorf("installed-activation-config-invalid")
	}
	packagePublic, der, packageKeyID, err := parsePublicPEM(packageAnchorRaw)
	zero(packageAnchorRaw)
	zero(der)
	if err != nil || !ed25519.Verify(packagePublic, framed(configSignatureDomain, configRaw), configSignature) {
		zero(packagePublic)
		return authority.ActivationCanaryExpected{}, nil, fmt.Errorf("installed-activation-config-invalid")
	}
	zero(packagePublic)
	config, err := strictJSON(configRaw, maximumManifestBytes)
	if err != nil {
		return authority.ActivationCanaryExpected{}, nil, fmt.Errorf("installed-activation-config-invalid")
	}
	get := func(key string) (string, bool) { return exactString(config[key]) }
	runtimeSHA, runtimeOK := get("runtime_sha")
	workflowSHA, workflowOK := get("workflow_sha")
	planDigest, planOK := get("plan_digest")
	configuredPackageKeyID, packageOK := get("package_authority_key_id")
	receiptKeyID, receiptOK := get("receipt_authority_key_id")
	if !runtimeOK || !workflowOK || !planOK || !packageOK || !receiptOK || !shaPattern.MatchString(runtimeSHA) || !shaPattern.MatchString(workflowSHA) || runtimeSHA == workflowSHA || !digestPattern.MatchString(planDigest) || configuredPackageKeyID != packageKeyID || !digestPattern.MatchString(receiptKeyID) {
		return authority.ActivationCanaryExpected{}, nil, fmt.Errorf("installed-activation-config-invalid")
	}
	receiptAnchorRaw, err := readExactFile(path(authority.ReceiptAnchorPath), 0o444, ownerUID, ownerGID, 4096)
	if err != nil {
		return authority.ActivationCanaryExpected{}, nil, fmt.Errorf("installed-activation-key-invalid")
	}
	receiptPublic, receiptDER, observedReceiptKeyID, err := parsePublicPEM(receiptAnchorRaw)
	zero(receiptAnchorRaw)
	zero(receiptDER)
	if err != nil || observedReceiptKeyID != receiptKeyID {
		zero(receiptPublic)
		return authority.ActivationCanaryExpected{}, nil, fmt.Errorf("installed-activation-key-invalid")
	}
	readDigest := func(absolute string, mode os.FileMode, maximum int) (string, error) {
		raw, readErr := readExactFile(path(absolute), mode, ownerUID, ownerGID, maximum)
		if readErr != nil {
			return "", readErr
		}
		defer zero(raw)
		return digest(raw), nil
	}
	controllerDigest, controllerErr := readDigest(authority.ControllerBinaryPath, 0o755, maximumMemberBytes)
	unitDigest, unitErr := readDigest(authority.ActivationCanaryUnitPath, 0o444, 65_536)
	manifestDigest, manifestErr := readDigest(authority.PackageManifestPath, 0o444, maximumManifestBytes)
	if controllerErr != nil || unitErr != nil || manifestErr != nil {
		zero(receiptPublic)
		return authority.ActivationCanaryExpected{}, nil, fmt.Errorf("installed-activation-payload-invalid")
	}
	return authority.ActivationCanaryExpected{
		ChallengeDigest: challengeDigest, ConfigDigest: digest(configRaw), ControllerDigest: controllerDigest,
		PackageManifestDigest: manifestDigest, PlanDigest: planDigest, UnitDigest: unitDigest, RuntimeSHA: runtimeSHA,
		WorkflowSHA: workflowSHA, PackageAuthorityKeyID: packageKeyID, ReceiptAuthorityKeyID: receiptKeyID,
	}, receiptPublic, nil
}

func activateThroughChild() (attempt *activationAttempt, returnErr error) {
	if err := runHelperChild("prepare-activation-host"); err != nil {
		return nil, err
	}
	challenge := make([]byte, 32)
	if _, err := io.ReadFull(rand.Reader, challenge); err != nil {
		zero(challenge)
		return nil, fmt.Errorf("activation-challenge-random-failed")
	}
	challengeDigest := digest(challenge)
	challengeCreatedAt := time.Now().UTC().Unix()
	if err := writeMountedActivationChallenge(challenge); err != nil {
		zero(challenge)
		return nil, err
	}
	zero(challenge)
	canaryStartedAt := time.Now().UTC().Unix()
	if err := runHelperChild("activate-host"); err != nil {
		challengeErr := removeMountedActivationArtifact(authority.ActivationCanaryChallengePath, 0o400)
		resultErr := removeMountedActivationArtifact(authority.ActivationCanaryResultPath, 0o600)
		deactivationErr := abortActivationThroughChild()
		if deactivationErr != nil {
			return nil, fmt.Errorf("activation-host-and-deactivation-failed")
		}
		if challengeErr != nil || resultErr != nil {
			return nil, fmt.Errorf("activation-host-and-artifact-cleanup-failed")
		}
		return nil, errActivationAlreadyAborted
	}
	hostActivated := true
	defer func() {
		if !hostActivated {
			return
		}
		challengeErr := removeMountedActivationArtifact(authority.ActivationCanaryChallengePath, 0o400)
		resultErr := removeMountedActivationArtifact(authority.ActivationCanaryResultPath, 0o600)
		deactivationErr := abortActivationThroughChild()
		attempt = nil
		if deactivationErr != nil {
			returnErr = fmt.Errorf("activation-parent-verification-and-deactivation-failed")
		} else if challengeErr != nil || resultErr != nil {
			returnErr = fmt.Errorf("activation-parent-verification-and-artifact-cleanup-failed")
		} else {
			returnErr = errActivationAlreadyAborted
		}
	}()
	path := filepath.Join(FixedHostRoot, strings.TrimPrefix(authority.ActivationCanaryResultPath, "/"))
	if err := validateDirectoryChain(FixedHostRoot, filepath.Dir(path), 0); err != nil {
		return nil, fmt.Errorf("activation-canary-result-parent-invalid")
	}
	raw, err := readExactFile(path, 0o600, 0, 0, maximumManifestBytes)
	if err != nil {
		return nil, fmt.Errorf("activation-canary-result-invalid")
	}
	if err := os.Remove(path); err != nil || syncDirectory(filepath.Dir(path)) != nil {
		zero(raw)
		return nil, fmt.Errorf("activation-canary-result-consume-failed")
	}
	expected, public, err := authority.InstalledActivationCanaryExpected(FixedHostRoot, challengeDigest)
	if err != nil {
		zero(raw)
		return nil, err
	}
	defer zero(public)
	expected.ChallengeCreatedAt = challengeCreatedAt
	expected.CanaryStartedAt = canaryStartedAt
	proof, err := authority.VerifyActivationCanaryReceipt(raw, public, expected, time.Now().UTC())
	if err != nil {
		zero(raw)
		return nil, err
	}
	if err := removeMountedActivationArtifact(authority.ActivationCanaryChallengePath, 0o400); err != nil {
		zero(raw)
		return nil, err
	}
	if err := removeMountedActivationArtifact(authority.ActivationCanaryResultPath, 0o600); err != nil {
		zero(raw)
		return nil, err
	}
	hostActivated = false
	return &activationAttempt{receipt: raw, challengeDigest: challengeDigest, challengeCreatedAt: challengeCreatedAt, canaryStartedAt: canaryStartedAt, installedStateProof: proof}, nil
}

func deactivateThroughChild() error      { return runHelperChild("deactivate-host") }
func abortActivationThroughChild() error { return runHelperChild("abort-activation-host") }

func writeMountedActivationChallenge(raw []byte) error {
	if len(raw) != 32 {
		return fmt.Errorf("activation-challenge-invalid")
	}
	path := filepath.Join(FixedHostRoot, strings.TrimPrefix(authority.ActivationCanaryChallengePath, "/"))
	if err := validateDirectoryChain(FixedHostRoot, filepath.Dir(path), 0); err != nil {
		return fmt.Errorf("activation-challenge-parent-invalid")
	}
	parentInfo, err := os.Lstat(filepath.Dir(path))
	if err != nil {
		return fmt.Errorf("activation-challenge-parent-invalid")
	}
	parentMetadata, ok := parentInfo.Sys().(*syscall.Stat_t)
	if !ok || !parentInfo.IsDir() || parentInfo.Mode()&os.ModeSymlink != 0 || parentInfo.Mode().Perm() != 0o750 || parentMetadata.Uid != 0 || parentMetadata.Gid != 0 || parentMetadata.Nlink != 2 {
		return fmt.Errorf("activation-challenge-parent-invalid")
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0o400)
	if err != nil {
		return fmt.Errorf("activation-challenge-create-failed")
	}
	succeeded := false
	defer func() {
		_ = file.Close()
		if !succeeded {
			_ = os.Remove(path)
		}
	}()
	if err := file.Chown(0, 0); err != nil || file.Chmod(0o400) != nil {
		return fmt.Errorf("activation-challenge-metadata-failed")
	}
	written, err := file.Write(raw)
	if err != nil || written != len(raw) || file.Sync() != nil || file.Close() != nil || syncDirectory(filepath.Dir(path)) != nil {
		return fmt.Errorf("activation-challenge-write-failed")
	}
	succeeded = true
	return nil
}

func removeMountedActivationArtifact(absolute string, mode os.FileMode) error {
	if (absolute != authority.ActivationCanaryChallengePath && absolute != authority.ActivationCanaryResultPath) || (mode != 0o400 && mode != 0o600) {
		return fmt.Errorf("activation-artifact-path-invalid")
	}
	path := filepath.Join(FixedHostRoot, strings.TrimPrefix(absolute, "/"))
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("activation-artifact-metadata-invalid")
	}
	metadata, ok := info.Sys().(*syscall.Stat_t)
	if !ok || !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm() != mode || metadata.Uid != 0 || metadata.Gid != 0 || metadata.Nlink != 1 {
		return fmt.Errorf("activation-artifact-metadata-invalid")
	}
	if err := os.Remove(path); err != nil {
		return fmt.Errorf("activation-artifact-remove-failed")
	}
	return syncDirectory(filepath.Dir(path))
}

func runHelperChild(mode string) error {
	ctx, cancel := context.WithTimeout(context.Background(), authorityDrainTimeout+15*time.Minute)
	defer cancel()
	command := exec.CommandContext(ctx, "/proc/self/exe", mode)
	command.Env = []string{"HOME=/nonexistent", "LANG=C", "LC_ALL=C", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "TZ=UTC"}
	command.Stdin = nil
	command.Stdout = nil
	command.Stderr = nil
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGKILL}
	command.WaitDelay = 5 * time.Second
	if err := command.Run(); err != nil || ctx.Err() != nil {
		return fmt.Errorf("host-systemd-operation-failed")
	}
	return nil
}

type hostCommandSpec struct {
	argv           []string
	expectedOutput *string
}

func exactHostOutput(value string) *string { return &value }

func runHostCommand(specification hostCommandSpec, timeout time.Duration) ([]byte, error) {
	if len(specification.argv) < 1 || timeout <= 0 {
		return nil, fmt.Errorf("host-systemd-command-invalid")
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	command := exec.CommandContext(ctx, specification.argv[0], specification.argv[1:]...)
	command.Env = []string{"HOME=/nonexistent", "LANG=C", "LC_ALL=C", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "TZ=UTC"}
	output := &boundedCommandOutput{limit: 32 * 1024}
	command.Stdin, command.Stderr, command.Stdout = nil, nil, output
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGKILL}
	command.WaitDelay = 5 * time.Second
	err := command.Run()
	if err != nil || ctx.Err() != nil || output.overflow || (specification.expectedOutput != nil && string(output.raw) != *specification.expectedOutput) {
		zero(output.raw)
		return nil, fmt.Errorf("host-systemd-command-failed")
	}
	return output.raw, nil
}

func authorityServiceInstances(states string) ([]string, error) {
	argv := []string{"/usr/bin/systemctl", "list-units", "--type=service", "--all", "--no-legend", "--plain", "--full"}
	if states != "" {
		argv = append(argv, "--state="+states)
	}
	argv = append(argv, "propertyquarry-release-single-host-v2@*.service")
	raw, err := runHostCommand(hostCommandSpec{argv: argv}, 30*time.Second)
	if err != nil {
		return nil, err
	}
	defer zero(raw)
	if len(raw) == 0 {
		return []string{}, nil
	}
	lines := strings.Split(strings.TrimSuffix(string(raw), "\n"), "\n")
	instances := make([]string, 0, len(lines))
	for _, line := range lines {
		fields := strings.Fields(line)
		if len(fields) < 1 || !authorityInstanceNamePattern.MatchString(fields[0]) {
			return nil, fmt.Errorf("host-systemd-instance-list-invalid")
		}
		instances = append(instances, fields[0])
	}
	return instances, nil
}

func waitForAuthorityServicesToDrain() error {
	deadline := time.Now().Add(authorityDrainTimeout)
	for {
		instances, err := authorityServiceInstances("active,activating,deactivating,reloading")
		if err != nil {
			return err
		}
		if len(instances) == 0 {
			return nil
		}
		if !time.Now().Before(deadline) {
			return fmt.Errorf("host-systemd-drain-timeout")
		}
		time.Sleep(time.Second)
	}
}

func socketAcceptedCount() (int64, error) {
	raw, err := runHostCommand(hostCommandSpec{argv: []string{"/usr/bin/systemctl", "show", "--property=NAccepted", "--value", "propertyquarry-release-single-host-v2.socket"}}, 30*time.Second)
	if err != nil {
		return 0, err
	}
	defer zero(raw)
	value, err := strconv.ParseInt(strings.TrimSpace(string(raw)), 10, 64)
	if err != nil || value < 0 {
		return 0, fmt.Errorf("host-systemd-socket-counter-invalid")
	}
	return value, nil
}

func probeActivatedAuthorityService() error {
	if _, err := runHostCommand(hostCommandSpec{argv: []string{"/usr/bin/systemctl", "reset-failed", "propertyquarry-release-single-host-v2@*.service"}}, 30*time.Second); err != nil {
		return err
	}
	instances, err := authorityServiceInstances("")
	if err != nil || len(instances) != 0 {
		return fmt.Errorf("host-systemd-stale-instance")
	}
	before, err := socketAcceptedCount()
	if err != nil {
		return err
	}
	connection, err := net.DialTimeout("unix", "/run/propertyquarry-release-single-host-v2/request.sock", 5*time.Second)
	if err != nil {
		return fmt.Errorf("host-systemd-service-probe-connect-failed")
	}
	if err := connection.Close(); err != nil {
		return fmt.Errorf("host-systemd-service-probe-close-failed")
	}
	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) {
		after, countErr := socketAcceptedCount()
		instances, listErr := authorityServiceInstances("")
		if countErr == nil && listErr == nil && after == before+1 && len(instances) == 1 {
			unit := instances[0]
			checks := []hostCommandSpec{
				{argv: []string{"/usr/bin/systemctl", "show", "--property=FragmentPath", "--value", unit}, expectedOutput: exactHostOutput("/usr/lib/systemd/system/propertyquarry-release-single-host-v2@.service\n")},
				{argv: []string{"/usr/bin/systemctl", "show", "--property=DropInPaths", "--value", unit}, expectedOutput: exactHostOutput("\n")},
				{argv: []string{"/usr/bin/systemctl", "show", "--property=ExecMainCode", "--value", unit}, expectedOutput: exactHostOutput("1\n")},
				{argv: []string{"/usr/bin/systemctl", "show", "--property=ExecMainStatus", "--value", unit}, expectedOutput: exactHostOutput("50\n")},
				{argv: []string{"/usr/bin/systemctl", "show", "--property=Result", "--value", unit}, expectedOutput: exactHostOutput("exit-code\n")},
			}
			valid := true
			for _, check := range checks {
				if raw, checkErr := runHostCommand(check, 30*time.Second); checkErr != nil {
					valid = false
				} else {
					zero(raw)
				}
			}
			if valid {
				if raw, resetErr := runHostCommand(hostCommandSpec{argv: []string{"/usr/bin/systemctl", "reset-failed", unit}}, 30*time.Second); resetErr != nil {
					return resetErr
				} else {
					zero(raw)
				}
				return nil
			}
		}
		time.Sleep(250 * time.Millisecond)
	}
	return fmt.Errorf("host-systemd-service-probe-failed")
}

func runHostSpecs(specifications []hostCommandSpec, timeout time.Duration) error {
	for _, specification := range specifications {
		raw, err := runHostCommand(specification, timeout)
		zero(raw)
		if err != nil {
			return err
		}
	}
	return nil
}

func removeHostActivationArtifact(path string, mode os.FileMode) error {
	if (path != authority.ActivationCanaryChallengePath && path != authority.ActivationCanaryResultPath) || (mode != 0o400 && mode != 0o600) {
		return fmt.Errorf("host-activation-artifact-path-invalid")
	}
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("host-activation-artifact-invalid")
	}
	metadata, ok := info.Sys().(*syscall.Stat_t)
	if !ok || !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm() != mode || metadata.Uid != 0 || metadata.Gid != 0 || metadata.Nlink != 1 {
		return fmt.Errorf("host-activation-artifact-invalid")
	}
	if err := os.Remove(path); err != nil {
		return fmt.Errorf("host-activation-artifact-remove-failed")
	}
	return syncDirectory(filepath.Dir(path))
}

func HostSystemdOperation(mode string) error {
	if os.Geteuid() != 0 || (mode != "prepare-activation-host" && mode != "activate-host" && mode != "deactivate-host" && mode != "abort-activation-host") {
		return fmt.Errorf("host-systemd-mode-invalid")
	}
	if err := syscall.Chroot(FixedHostRoot); err != nil {
		return fmt.Errorf("host-chroot-failed")
	}
	if err := os.Chdir("/"); err != nil {
		return fmt.Errorf("host-chdir-failed")
	}
	comm, err := os.ReadFile("/proc/1/comm")
	if err != nil || strings.TrimSpace(string(comm)) != "systemd" {
		zero(comm)
		return fmt.Errorf("host-systemd-pid-invalid")
	}
	zero(comm)
	canaryUnit := "propertyquarry-release-single-host-v2-activation-canary.service"
	socketUnit := "propertyquarry-release-single-host-v2.socket"
	if mode == "abort-activation-host" {
		errorsObserved := 0
		for _, specification := range []hostCommandSpec{
			{argv: []string{"/usr/bin/systemctl", "disable", "--now", socketUnit}},
			{argv: []string{"/usr/bin/systemctl", "stop", canaryUnit}},
			{argv: []string{"/usr/bin/systemctl", "reset-failed", canaryUnit}},
		} {
			raw, commandErr := runHostCommand(specification, 30*time.Second)
			zero(raw)
			if commandErr != nil {
				errorsObserved++
			}
		}
		if removeHostActivationArtifact(authority.ActivationCanaryChallengePath, 0o400) != nil {
			errorsObserved++
		}
		if removeHostActivationArtifact(authority.ActivationCanaryResultPath, 0o600) != nil {
			errorsObserved++
		}
		if errorsObserved != 0 {
			return fmt.Errorf("host-activation-abort-incomplete")
		}
		return nil
	}
	if mode == "deactivate-host" {
		errorsObserved := 0
		for _, specification := range []hostCommandSpec{
			{argv: []string{"/usr/bin/systemctl", "stop", canaryUnit}},
			{argv: []string{"/usr/bin/systemctl", "reset-failed", canaryUnit}},
			{argv: []string{"/usr/bin/systemctl", "disable", "--now", socketUnit}},
		} {
			raw, commandErr := runHostCommand(specification, 30*time.Second)
			zero(raw)
			if commandErr != nil {
				errorsObserved++
			}
		}
		if err := removeHostActivationArtifact(authority.ActivationCanaryChallengePath, 0o400); err != nil {
			errorsObserved++
		}
		if err := removeHostActivationArtifact(authority.ActivationCanaryResultPath, 0o600); err != nil {
			errorsObserved++
		}
		if err := waitForAuthorityServicesToDrain(); err != nil {
			errorsObserved++
		}
		if err := runHostSpecs([]hostCommandSpec{{argv: []string{"/usr/bin/systemctl", "daemon-reload"}}}, 30*time.Second); err != nil {
			errorsObserved++
		}
		if errorsObserved != 0 {
			return fmt.Errorf("host-systemd-deactivation-incomplete")
		}
		return nil
	}
	if err := ensureActivationPrerequisites(); err != nil {
		return err
	}
	common := []hostCommandSpec{
		{argv: []string{"/usr/bin/systemd-sysusers", "/usr/lib/sysusers.d/propertyquarry-release-single-host-v2.conf"}},
		{argv: []string{"/usr/bin/systemd-tmpfiles", "--create", "/usr/lib/tmpfiles.d/propertyquarry-release-single-host-v2.conf"}},
		{argv: []string{"/usr/bin/systemd-analyze", "verify", "/usr/lib/systemd/system/propertyquarry-release-single-host-v2.socket", "/usr/lib/systemd/system/propertyquarry-release-single-host-v2@.service", authority.ActivationCanaryUnitPath}},
		{argv: []string{"/usr/bin/systemctl", "daemon-reload"}},
		{argv: []string{"/usr/bin/systemctl", "show", "--property=FragmentPath", "--value", socketUnit}, expectedOutput: exactHostOutput("/usr/lib/systemd/system/propertyquarry-release-single-host-v2.socket\n")},
		{argv: []string{"/usr/bin/systemctl", "show", "--property=FragmentPath", "--value", "propertyquarry-release-single-host-v2@.service"}, expectedOutput: exactHostOutput("/usr/lib/systemd/system/propertyquarry-release-single-host-v2@.service\n")},
		{argv: []string{"/usr/bin/systemctl", "show", "--property=FragmentPath", "--value", canaryUnit}, expectedOutput: exactHostOutput(authority.ActivationCanaryUnitPath + "\n")},
		{argv: []string{"/usr/bin/systemctl", "show", "--property=DropInPaths", "--value", socketUnit}, expectedOutput: exactHostOutput("\n")},
		{argv: []string{"/usr/bin/systemctl", "show", "--property=DropInPaths", "--value", "propertyquarry-release-single-host-v2@.service"}, expectedOutput: exactHostOutput("\n")},
		{argv: []string{"/usr/bin/systemctl", "show", "--property=DropInPaths", "--value", canaryUnit}, expectedOutput: exactHostOutput("\n")},
		{argv: []string{"/usr/bin/systemctl", "disable", "--now", socketUnit}},
		{argv: []string{"/usr/bin/systemctl", "stop", canaryUnit}},
		{argv: []string{"/usr/bin/systemctl", "reset-failed", canaryUnit}},
		{argv: []string{"/usr/bin/systemctl", "show", "--property=ActiveState", "--value", canaryUnit}, expectedOutput: exactHostOutput("inactive\n")},
	}
	if err := runHostSpecs(common, 30*time.Second); err != nil {
		return err
	}
	if err := waitForAuthorityServicesToDrain(); err != nil {
		return err
	}
	if mode == "prepare-activation-host" {
		if err := removeHostActivationArtifact(authority.ActivationCanaryChallengePath, 0o400); err != nil {
			return err
		}
		return removeHostActivationArtifact(authority.ActivationCanaryResultPath, 0o600)
	}
	cleanupFailure := func() error {
		errorsObserved := 0
		for _, specification := range []hostCommandSpec{
			{argv: []string{"/usr/bin/systemctl", "disable", "--now", socketUnit}},
			{argv: []string{"/usr/bin/systemctl", "stop", canaryUnit}},
			{argv: []string{"/usr/bin/systemctl", "reset-failed", canaryUnit}},
		} {
			raw, commandErr := runHostCommand(specification, 30*time.Second)
			zero(raw)
			if commandErr != nil {
				errorsObserved++
			}
		}
		if removeHostActivationArtifact(authority.ActivationCanaryChallengePath, 0o400) != nil {
			errorsObserved++
		}
		if removeHostActivationArtifact(authority.ActivationCanaryResultPath, 0o600) != nil {
			errorsObserved++
		}
		if errorsObserved != 0 {
			return fmt.Errorf("host-activation-cleanup-failed")
		}
		return nil
	}
	failActivation := func(cause error) error {
		if cleanupFailure() != nil {
			return fmt.Errorf("host-activation-and-cleanup-failed")
		}
		return cause
	}
	if _, err := os.Lstat(authority.ActivationCanaryResultPath); !os.IsNotExist(err) {
		return failActivation(fmt.Errorf("host-activation-stale-result"))
	}
	challengeInfo, err := os.Lstat(authority.ActivationCanaryChallengePath)
	if err != nil {
		return failActivation(fmt.Errorf("host-activation-challenge-invalid"))
	}
	challengeMetadata, metadataOK := challengeInfo.Sys().(*syscall.Stat_t)
	if !metadataOK || !challengeInfo.Mode().IsRegular() || challengeInfo.Mode().Perm() != 0o400 || challengeMetadata.Uid != 0 || challengeMetadata.Gid != 0 || challengeMetadata.Nlink != 1 {
		return failActivation(fmt.Errorf("host-activation-challenge-invalid"))
	}
	challenge, err := readExactFile(authority.ActivationCanaryChallengePath, 0o400, 0, 0, 32)
	if err != nil || len(challenge) != 32 {
		zero(challenge)
		return failActivation(fmt.Errorf("host-activation-challenge-invalid"))
	}
	challengeDigest := digest(challenge)
	zero(challenge)
	challengeCreatedAt := challengeMetadata.Mtim.Sec
	canaryStartedAt := time.Now().UTC().Unix()
	if canaryStartedAt < challengeCreatedAt || canaryStartedAt-challengeCreatedAt > 30 {
		return failActivation(fmt.Errorf("host-activation-challenge-stale"))
	}
	if err := runHostSpecs([]hostCommandSpec{
		{argv: []string{"/usr/bin/systemctl", "start", canaryUnit}},
		{argv: []string{"/usr/bin/systemctl", "show", "--property=Result", "--value", canaryUnit}, expectedOutput: exactHostOutput("success\n")},
		{argv: []string{"/usr/bin/systemctl", "show", "--property=ExecMainCode", "--value", canaryUnit}, expectedOutput: exactHostOutput("1\n")},
		{argv: []string{"/usr/bin/systemctl", "show", "--property=ExecMainStatus", "--value", canaryUnit}, expectedOutput: exactHostOutput("0\n")},
		{argv: []string{"/usr/bin/systemctl", "show", "--property=ActiveState", "--value", canaryUnit}, expectedOutput: exactHostOutput("inactive\n")},
	}, 75*time.Second); err != nil {
		return failActivation(err)
	}
	result, err := readExactFile(authority.ActivationCanaryResultPath, 0o600, 0, 0, maximumManifestBytes)
	if err != nil {
		return failActivation(fmt.Errorf("host-activation-canary-result-invalid"))
	}
	expected, public, err := authority.InstalledActivationCanaryExpected("/", challengeDigest)
	if err == nil {
		expected.ChallengeCreatedAt = challengeCreatedAt
		expected.CanaryStartedAt = canaryStartedAt
		_, err = authority.VerifyActivationCanaryReceipt(result, public, expected, time.Now().UTC())
	}
	zero(public)
	zero(result)
	if err != nil {
		return failActivation(fmt.Errorf("host-activation-canary-proof-invalid"))
	}
	if err := removeHostActivationArtifact(authority.ActivationCanaryChallengePath, 0o400); err != nil {
		return failActivation(err)
	}
	if err := runHostSpecs([]hostCommandSpec{
		{argv: []string{"/usr/bin/systemctl", "enable", "--now", socketUnit}},
		{argv: []string{"/usr/bin/systemctl", "is-active", "--quiet", socketUnit}},
	}, 30*time.Second); err != nil {
		return failActivation(err)
	}
	if err := probeActivatedAuthorityService(); err != nil {
		return failActivation(err)
	}
	return nil
}

func ensureActivationPrerequisites() error {
	credential, err := readExactFile(githubCredentialSource, 0o400, 0, 0, 64*1024)
	if err != nil || len(credential) < 32 {
		zero(credential)
		return fmt.Errorf("host-github-encrypted-credential-invalid")
	}
	allZero := true
	for _, value := range credential {
		if value != 0 {
			allZero = false
			break
		}
	}
	zero(credential)
	if allZero {
		return fmt.Errorf("host-github-encrypted-credential-invalid")
	}
	for _, path := range []string{
		"/mnt/pcloud",
		"/mnt/pcloud/propertyquarry",
		"/mnt/pcloud/propertyquarry/releases",
	} {
		info, err := os.Lstat(path)
		if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("host-remote-backup-parent-invalid")
		}
	}
	for _, path := range []string{
		"/mnt/pcloud/propertyquarry/releases/backups",
		remoteBackupDirectory,
	} {
		if err := ensureExactRemoteBackupDirectory(path); err != nil {
			return err
		}
	}
	return nil
}

func ensureExactRemoteBackupDirectory(path string) error {
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		if err := os.Mkdir(path, 0o775); err != nil {
			return fmt.Errorf("host-remote-backup-directory-create-failed")
		}
		if err := os.Chmod(path, 0o775); err != nil {
			return fmt.Errorf("host-remote-backup-directory-chmod-failed")
		}
		if err := os.Chown(path, remoteBackupOwnerUID, remoteBackupOwnerGID); err != nil {
			return fmt.Errorf("host-remote-backup-directory-chown-failed")
		}
		info, err = os.Lstat(path)
	}
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm() != 0o775 {
		return fmt.Errorf("host-remote-backup-directory-invalid")
	}
	metadata, ok := info.Sys().(*syscall.Stat_t)
	if !ok || metadata.Uid != remoteBackupOwnerUID || metadata.Gid != remoteBackupOwnerGID || metadata.Nlink != 1 {
		return fmt.Errorf("host-remote-backup-directory-metadata-invalid")
	}
	return nil
}

func EncodePublicKeyDER(key ed25519.PublicKey) (string, string, error) {
	der, err := x509.MarshalPKIXPublicKey(key)
	if err != nil {
		return "", "", err
	}
	defer zero(der)
	return base64.RawStdEncoding.EncodeToString(der), digest(der), nil
}

func EncodePrivateKeyPEM(key ed25519.PrivateKey) ([]byte, error) {
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return nil, err
	}
	defer zero(der)
	return pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}), nil
}
