//go:build linux && amd64

package authority

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"regexp"
	"sort"
	"strconv"
	"syscall"
	"unsafe"
)

const (
	journalSchema          = "propertyquarry.release-control.single-host-journal-event.v2"
	receiptSignatureDomain = "propertyquarry.release-control.single-host-receipt-signature.v2\x00"
	journalGenesisDigest   = "sha256:9f78b44f08d95d17d25ee6b12c363cc63fd8d7373537178e92b13dd40ef0f483"
	journalLockName        = ".single-host-v2.lock"
	maximumJournalEvents   = 100000
	maximumJournalBytes    = 2 * 1024 * 1024
	renameNoReplace        = 1
	sysRenameat2           = 316
)

var (
	journalEventPattern   = regexp.MustCompile(`^event-([0-9]{20})\.v2\.json$`)
	journalPendingPattern = regexp.MustCompile(`^\.pending-([0-9a-f]{64})\.tmp$`)
)

type Journal struct {
	directory *os.File
	lock      *os.File
	key       ed25519.PrivateKey
	keyID     string
	ownerUID  uint32
	ownerGID  uint32
	events    []JournalEvent
	// afterPendingSync is a test-only crash boundary. Production journals leave
	// it nil; tests use it to stop a subprocess after the pending event is
	// durable but before its atomic publication.
	afterPendingSync func()
}

type JournalEvent struct {
	Sequence          int64
	PredecessorDigest string
	EventType         string
	Operation         string
	RunID             string
	RunAttempt        int64
	RequestID         string
	OIDCJTI           string
	ReceiptDigest     string
	Payload           map[string]any
	Canonical         []byte
	Wire              []byte
}

func (event *JournalEvent) release() {
	if event == nil {
		return
	}
	zero(event.Canonical)
	zero(event.Wire)
	*event = JournalEvent{}
}

func OpenJournal(root string, key ed25519.PrivateKey) (*Journal, error) {
	if len(key) != ed25519.PrivateKeySize {
		return nil, fmt.Errorf("journal-key-invalid")
	}
	path := rooted(root, JournalPath)
	ownerUID, ownerGID := secureOwner(root)
	if err := validateSecureParentChain(root, path, ownerUID); err != nil {
		return nil, fmt.Errorf("journal-parent-invalid")
	}
	directory, err := os.OpenFile(path, os.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, fmt.Errorf("journal-unavailable")
	}
	info, err := directory.Stat()
	metadata, ok := infoSys(info)
	if err != nil || !ok || !info.IsDir() || info.Mode().Perm() != 0o700 || metadata.Uid != ownerUID || metadata.Gid != ownerGID {
		directory.Close()
		return nil, fmt.Errorf("journal-metadata-invalid")
	}
	lockFD, err := syscall.Openat(int(directory.Fd()), journalLockName, syscall.O_CREAT|syscall.O_RDWR|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		directory.Close()
		return nil, fmt.Errorf("journal-lock-unavailable")
	}
	lock := os.NewFile(uintptr(lockFD), journalLockName)
	lockInfo, err := lock.Stat()
	lockMetadata, lockOK := infoSys(lockInfo)
	if err != nil || !lockOK || !lockInfo.Mode().IsRegular() || lockInfo.Mode().Perm() != 0o600 || lockMetadata.Uid != ownerUID || lockMetadata.Gid != ownerGID || lockMetadata.Nlink != 1 {
		lock.Close()
		directory.Close()
		return nil, fmt.Errorf("journal-lock-invalid")
	}
	if err := syscall.Flock(lockFD, syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		lock.Close()
		directory.Close()
		return nil, fmt.Errorf("journal-busy")
	}
	public := key.Public().(ed25519.PublicKey)
	keyID, err := publicKeyID(public)
	if err != nil {
		lock.Close()
		directory.Close()
		return nil, err
	}
	journal := &Journal{directory: directory, lock: lock, key: append(ed25519.PrivateKey(nil), key...), keyID: keyID, ownerUID: ownerUID, ownerGID: ownerGID}
	if err := journal.rebuild(); err != nil {
		journal.Close()
		return nil, err
	}
	return journal, nil
}

func (journal *Journal) Close() {
	if journal == nil {
		return
	}
	for index := range journal.events {
		journal.events[index].release()
	}
	zero(journal.key)
	if journal.lock != nil {
		_ = syscall.Flock(int(journal.lock.Fd()), syscall.LOCK_UN)
		_ = journal.lock.Close()
	}
	if journal.directory != nil {
		_ = journal.directory.Close()
	}
	*journal = Journal{}
}

func (journal *Journal) Events() []JournalEvent {
	result := make([]JournalEvent, 0, len(journal.events))
	for _, event := range journal.events {
		result = append(result, JournalEvent{Sequence: event.Sequence, PredecessorDigest: event.PredecessorDigest,
			EventType: event.EventType, Operation: event.Operation, RunID: event.RunID, RunAttempt: event.RunAttempt,
			RequestID: event.RequestID, OIDCJTI: event.OIDCJTI, ReceiptDigest: event.ReceiptDigest,
			Payload: event.Payload, Canonical: append([]byte(nil), event.Canonical...), Wire: append([]byte(nil), event.Wire...)})
	}
	return result
}

func (journal *Journal) HeadDigest() string {
	if len(journal.events) == 0 {
		return journalGenesisDigest
	}
	return journal.events[len(journal.events)-1].ReceiptDigest
}

func (journal *Journal) Append(eventType string, fields map[string]any) ([]byte, error) {
	if !idPattern.MatchString(eventType) || fields == nil || len(journal.events) >= maximumJournalEvents {
		return nil, fmt.Errorf("journal-event-invalid")
	}
	for key := range fields {
		if key == "schema" || key == "version" || key == "journal_sequence" || key == "journal_predecessor_digest" || key == "event_type" || key == "receipt_key_id" {
			return nil, fmt.Errorf("journal-reserved-field")
		}
	}
	sequence := int64(len(journal.events) + 1)
	payload := map[string]any{
		"schema": journalSchema, "version": json.Number("2"),
		"journal_sequence":           json.Number(strconv.FormatInt(sequence, 10)),
		"journal_predecessor_digest": journal.HeadDigest(), "event_type": eventType,
		"receipt_key_id": journal.keyID,
	}
	for key, value := range fields {
		payload[key] = value
	}
	wire, err := signReceipt(payload, journal.key)
	if err != nil {
		return nil, err
	}
	event, err := verifyReceipt(wire, journal.key.Public().(ed25519.PublicKey), sequence, journal.HeadDigest())
	if err != nil {
		zero(wire)
		return nil, err
	}
	name := journalEventName(sequence)
	pending, err := randomPendingName()
	if err != nil {
		event.release()
		zero(wire)
		return nil, err
	}
	fd, err := syscall.Openat(int(journal.directory.Fd()), pending, syscall.O_WRONLY|syscall.O_CREAT|syscall.O_EXCL|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		event.release()
		zero(wire)
		return nil, fmt.Errorf("journal-pending-create-failed")
	}
	file := os.NewFile(uintptr(fd), pending)
	committed := false
	defer func() {
		if file != nil {
			_ = file.Close()
		}
		if !committed {
			_ = syscall.Unlinkat(int(journal.directory.Fd()), pending)
		}
	}()
	if err := writeAll(file, wire); err != nil || file.Sync() != nil || file.Close() != nil {
		event.release()
		zero(wire)
		return nil, fmt.Errorf("journal-pending-write-failed")
	}
	file = nil
	if journal.afterPendingSync != nil {
		journal.afterPendingSync()
	}
	if err := renameAtNoReplace(int(journal.directory.Fd()), pending, name); err != nil {
		event.release()
		zero(wire)
		return nil, fmt.Errorf("journal-publish-failed")
	}
	committed = true
	if err := journal.directory.Sync(); err != nil {
		event.release()
		zero(wire)
		return nil, fmt.Errorf("journal-durability-unknown")
	}
	stored, err := journal.readEvent(name, sequence, journal.HeadDigest())
	if err != nil || !bytes.Equal(stored.Wire, wire) {
		if stored != nil {
			stored.release()
		}
		event.release()
		zero(wire)
		return nil, fmt.Errorf("journal-postpublish-invalid")
	}
	event.release()
	journal.events = append(journal.events, *stored)
	return wire, nil
}

func (journal *Journal) rebuild() error {
	entries, err := journal.directory.ReadDir(-1)
	if err != nil {
		return fmt.Errorf("journal-enumeration-failed")
	}
	names := make([]string, 0, len(entries))
	pendingNames := make([]string, 0, 1)
	for _, entry := range entries {
		name := entry.Name()
		if name == journalLockName {
			continue
		}
		if journalPendingPattern.MatchString(name) {
			pendingNames = append(pendingNames, name)
			continue
		}
		if !journalEventPattern.MatchString(name) {
			return fmt.Errorf("journal-extra-entry")
		}
		names = append(names, name)
	}
	if len(names)+len(pendingNames) > maximumJournalEvents {
		return fmt.Errorf("journal-too-many-events")
	}
	if len(pendingNames) > 1 {
		return fmt.Errorf("journal-pending-ambiguous")
	}
	sort.Strings(names)
	predecessor := journalGenesisDigest
	for index, name := range names {
		sequence := int64(index + 1)
		if name != journalEventName(sequence) {
			return fmt.Errorf("journal-sequence-gap")
		}
		event, err := journal.readEvent(name, sequence, predecessor)
		if err != nil {
			return err
		}
		journal.events = append(journal.events, *event)
		predecessor = event.ReceiptDigest
	}
	if len(pendingNames) == 1 {
		if err := journal.recoverPending(pendingNames[0], int64(len(names)+1), predecessor); err != nil {
			return err
		}
	}
	return nil
}

func (journal *Journal) recoverPending(pendingName string, sequence int64, predecessor string) error {
	pendingEvent, err := journal.readEvent(pendingName, sequence, predecessor)
	if err != nil {
		return fmt.Errorf("journal-pending-invalid")
	}
	defer pendingEvent.release()
	finalName := journalEventName(sequence)
	if err := renameAtNoReplace(int(journal.directory.Fd()), pendingName, finalName); err != nil {
		return fmt.Errorf("journal-pending-publish-failed")
	}
	if err := journal.directory.Sync(); err != nil {
		return fmt.Errorf("journal-pending-durability-unknown")
	}
	stored, err := journal.readEvent(finalName, sequence, predecessor)
	if err != nil {
		return fmt.Errorf("journal-pending-postpublish-invalid")
	}
	if !bytes.Equal(stored.Wire, pendingEvent.Wire) {
		stored.release()
		return fmt.Errorf("journal-pending-postpublish-invalid")
	}
	journal.events = append(journal.events, *stored)
	return nil
}

func (journal *Journal) readEvent(name string, sequence int64, predecessor string) (*JournalEvent, error) {
	fd, err := syscall.Openat(int(journal.directory.Fd()), name, syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, fmt.Errorf("journal-event-unavailable")
	}
	file := os.NewFile(uintptr(fd), name)
	defer file.Close()
	info, err := file.Stat()
	metadata, ok := infoSys(info)
	if err != nil || !ok || !info.Mode().IsRegular() || info.Mode().Perm() != 0o600 || info.Size() < 1 || info.Size() > maximumJournalBytes || metadata.Uid != journal.ownerUID || metadata.Gid != journal.ownerGID || metadata.Nlink != 1 {
		return nil, fmt.Errorf("journal-event-metadata-invalid")
	}
	raw := make([]byte, info.Size())
	if _, err := io.ReadFull(file, raw); err != nil {
		zero(raw)
		return nil, fmt.Errorf("journal-event-read-failed")
	}
	after, err := file.Stat()
	if err != nil || !os.SameFile(info, after) {
		zero(raw)
		return nil, fmt.Errorf("journal-event-changed")
	}
	event, err := verifyReceipt(raw, journal.key.Public().(ed25519.PublicKey), sequence, predecessor)
	zero(raw)
	return event, err
}

func signReceipt(payload map[string]any, key ed25519.PrivateKey) ([]byte, error) {
	canonicalPayload, err := canonicalJSON(payload)
	if err != nil || len(canonicalPayload) > maximumJournalBytes/2 {
		zero(canonicalPayload)
		return nil, fmt.Errorf("receipt-payload-invalid")
	}
	message := framed(receiptSignatureDomain, canonicalPayload)
	signature := ed25519.Sign(key, message)
	zero(message)
	keyID, err := publicKeyID(key.Public().(ed25519.PublicKey))
	if err != nil {
		zero(canonicalPayload)
		zero(signature)
		return nil, err
	}
	wrapper, err := canonicalJSON(map[string]any{
		"payload":           payload,
		"signature":         base64.RawURLEncoding.EncodeToString(signature),
		"signature_profile": map[string]any{"algorithm": "ed25519", "encoding": "base64url-no-padding", "key_id": keyID, "signed_message": "domain-separated-uint64be-length-prefixed-canonical-json"},
	})
	zero(canonicalPayload)
	zero(signature)
	if err != nil || len(wrapper) > maximumJournalBytes {
		zero(wrapper)
		return nil, fmt.Errorf("receipt-wrapper-invalid")
	}
	return wrapper, nil
}

func verifyReceipt(raw []byte, public ed25519.PublicKey, sequence int64, predecessor string) (*JournalEvent, error) {
	payload, canonicalPayload, err := verifySignedReceiptPayload(raw, public)
	if err != nil {
		return nil, err
	}
	recordSequence, sequenceOK := exactInt(payload["journal_sequence"], sequence, sequence)
	recordPredecessor, predecessorOK := exactString(payload["journal_predecessor_digest"])
	eventType, eventOK := exactString(payload["event_type"])
	operation, operationOK := exactString(payload["operation"])
	runID, runOK := exactString(payload["run_id"])
	runAttempt, attemptOK := exactInt(payload["run_attempt"], 1, 1<<31-1)
	requestID, requestOK := exactString(payload["request_id"])
	oidcJTI, jtiOK := exactString(payload["oidc_jti"])
	if payload["schema"] != journalSchema || payload["version"] != json.Number("2") || !sequenceOK || recordSequence != sequence ||
		!predecessorOK || recordPredecessor != predecessor || !eventOK || !idPattern.MatchString(eventType) ||
		!operationOK || !requestOK || !runOK || !decimal(runID) || !attemptOK || !jtiOK {
		zero(canonicalPayload)
		return nil, fmt.Errorf("receipt-binding-invalid")
	}
	return &JournalEvent{Sequence: sequence, PredecessorDigest: predecessor, EventType: eventType, Operation: operation,
		RunID: runID, RunAttempt: runAttempt, RequestID: requestID, OIDCJTI: oidcJTI,
		ReceiptDigest: digest(raw), Payload: payload, Canonical: canonicalPayload, Wire: append([]byte(nil), raw...)}, nil
}

func verifySignedReceiptPayload(raw []byte, public ed25519.PublicKey) (map[string]any, []byte, error) {
	wrapper, err := strictJSON(raw, maximumJournalBytes)
	if err != nil || !hasKeys(wrapper, "payload", "signature", "signature_profile") {
		return nil, nil, fmt.Errorf("receipt-wrapper-invalid")
	}
	payload, ok := wrapper["payload"].(map[string]any)
	profile, profileOK := wrapper["signature_profile"].(map[string]any)
	signatureText, sigOK := exactString(wrapper["signature"])
	keyID, keyErr := publicKeyID(public)
	if !ok || !profileOK || !sigOK || keyErr != nil || !hasKeys(profile, "algorithm", "encoding", "key_id", "signed_message") ||
		profile["algorithm"] != "ed25519" || profile["encoding"] != "base64url-no-padding" || profile["key_id"] != keyID ||
		profile["signed_message"] != "domain-separated-uint64be-length-prefixed-canonical-json" {
		return nil, nil, fmt.Errorf("receipt-signature-profile-invalid")
	}
	signature, err := base64.RawURLEncoding.DecodeString(signatureText)
	if err != nil || len(signature) != ed25519.SignatureSize {
		zero(signature)
		return nil, nil, fmt.Errorf("receipt-signature-invalid")
	}
	canonicalPayload, err := canonicalJSON(payload)
	if err != nil || !ed25519.Verify(public, framed(receiptSignatureDomain, canonicalPayload), signature) {
		zero(signature)
		zero(canonicalPayload)
		return nil, nil, fmt.Errorf("receipt-authentication-failed")
	}
	zero(signature)
	return payload, canonicalPayload, nil
}

func publicKeyID(public ed25519.PublicKey) (string, error) {
	der, err := x509.MarshalPKIXPublicKey(public)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(der)
	zero(der)
	return "sha256:" + hex.EncodeToString(sum[:]), nil
}

func randomPendingName() (string, error) {
	raw := make([]byte, 32)
	if _, err := io.ReadFull(rand.Reader, raw); err != nil {
		return "", err
	}
	name := ".pending-" + hex.EncodeToString(raw) + ".tmp"
	zero(raw)
	return name, nil
}

func journalEventName(sequence int64) string { return fmt.Sprintf("event-%020d.v2.json", sequence) }

func writeAll(file *os.File, raw []byte) error {
	for len(raw) > 0 {
		written, err := file.Write(raw)
		if err != nil || written < 1 {
			return fmt.Errorf("short-write")
		}
		raw = raw[written:]
	}
	return nil
}

func renameAtNoReplace(directoryFD int, oldName, newName string) error {
	oldPointer, err := syscall.BytePtrFromString(oldName)
	if err != nil {
		return err
	}
	newPointer, err := syscall.BytePtrFromString(newName)
	if err != nil {
		return err
	}
	_, _, errno := syscall.Syscall6(sysRenameat2, uintptr(directoryFD), uintptr(unsafe.Pointer(oldPointer)), uintptr(directoryFD), uintptr(unsafe.Pointer(newPointer)), renameNoReplace, 0)
	if errno != 0 {
		return errno
	}
	return nil
}

func infoSys(info os.FileInfo) (*syscall.Stat_t, bool) {
	if info == nil {
		return nil, false
	}
	value, ok := info.Sys().(*syscall.Stat_t)
	return value, ok
}
