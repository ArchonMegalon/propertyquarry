//go:build linux

package releasecontrol

import (
	"bytes"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"path"
	"strconv"
	"strings"
	"syscall"
)

const (
	installedResultJournalSchema     = "propertyquarry.release-control.result-journal.v2"
	installedResultJournalRootSuffix = "-results-v2"
	installedResultJournalPrefix     = "result-v2-"
	installedResultJournalSuffix     = ".bin"
	installedResultJournalMagic      = "propertyquarry.release-control.result-journal.v2\x00"
	installedResultJournalMode       = 0o600
	maxInstalledResultMetadata       = 4096
	maxInstalledResultResponse       = 4 * 1024 * 1024
	maxInstalledResultRecords        = maxInstalledReplayClaims
)

type installedResultJournalRecord struct {
	ClaimDigest               string
	ClaimName                 string
	RequestKeyID              string
	RequestID                 string
	Nonce                     string
	RawRequestDigest          string
	CanonicalEnvelopeDigest   string
	RootPolicyDigest          string
	AuthorityGenerationDigest string
	StateGenerationDigest     string
	ResponseDigest            string
	Metadata                  []byte
	Response                  []byte
	Name                      string
}

func (record *installedResultJournalRecord) release() {
	if record == nil {
		return
	}
	zero(record.Metadata)
	zero(record.Response)
	*record = installedResultJournalRecord{}
}

func (record *installedResultJournalRecord) replayBytes() []byte {
	if record == nil {
		return nil
	}
	response := make([]byte, len(record.Response))
	copy(response, record.Response)
	return response
}

type installedResultConflictError struct{}

func (installedResultConflictError) Error() string {
	return "installed result journal conflict"
}

type installedResultJournal struct {
	fd       int
	identity stableIdentity
	paths    installedRuntimePaths
	root     string
}

func installedResultJournalRoot(paths installedRuntimePaths) (string, error) {
	if !path.IsAbs(paths.StateRoot) ||
		path.Clean(paths.StateRoot) != paths.StateRoot ||
		paths.StateRoot == "/" {
		return "", fmt.Errorf("installed result journal path invalid")
	}
	root := paths.StateRoot + installedResultJournalRootSuffix
	if !path.IsAbs(root) || path.Clean(root) != root || root == "/" {
		return "", fmt.Errorf("installed result journal path invalid")
	}
	return root, nil
}

func openInstalledResultJournal(paths installedRuntimePaths) (*installedResultJournal, error) {
	if err := validateInstalledPrincipalContract(paths); err != nil {
		return nil, err
	}
	root, err := installedResultJournalRoot(paths)
	if err != nil {
		return nil, err
	}
	fd, err := openRootedAbsolute(paths.Root, root, syscall.O_DIRECTORY)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(fd, syscall.LOCK_EX); err != nil {
		_ = syscall.Close(fd)
		return nil, fmt.Errorf("installed result journal lock failed")
	}
	var stat syscall.Stat_t
	if err := syscall.Fstat(fd, &stat); err != nil {
		_ = syscall.Flock(fd, syscall.LOCK_UN)
		_ = syscall.Close(fd)
		return nil, err
	}
	identity := identityFromStat(stat)
	if err := validateDirectoryIdentity(
		identity,
		expectedFileMetadata{
			Mode: 0o700,
			UID:  paths.AuthorityUID,
			GID:  paths.AuthorityGID,
		},
	); err != nil {
		_ = syscall.Flock(fd, syscall.LOCK_UN)
		_ = syscall.Close(fd)
		return nil, err
	}
	journal := &installedResultJournal{
		fd:       fd,
		identity: identity,
		paths:    paths,
		root:     root,
	}
	if err := journal.validatePath(); err != nil {
		journal.close()
		return nil, err
	}
	return journal, nil
}

func (journal *installedResultJournal) close() {
	if journal == nil || journal.fd < 0 {
		return
	}
	_ = syscall.Flock(journal.fd, syscall.LOCK_UN)
	_ = syscall.Close(journal.fd)
	journal.fd = -1
	journal.identity = stableIdentity{}
}

func (journal *installedResultJournal) validatePath() error {
	if journal == nil || journal.fd < 0 {
		return fmt.Errorf("installed result journal unavailable")
	}
	var current syscall.Stat_t
	if err := syscall.Fstat(journal.fd, &current); err != nil ||
		!sameInstalledDirectoryObject(journal.identity, identityFromStat(current)) {
		return fmt.Errorf("installed result journal changed")
	}
	reopened, err := openRootedAbsolute(
		journal.paths.Root,
		journal.root,
		syscall.O_DIRECTORY,
	)
	if err != nil {
		return err
	}
	var reopenedStat syscall.Stat_t
	statErr := syscall.Fstat(reopened, &reopenedStat)
	_ = syscall.Close(reopened)
	if statErr != nil ||
		!sameInstalledDirectoryObject(journal.identity, identityFromStat(reopenedStat)) {
		return fmt.Errorf("installed result journal path changed")
	}
	return nil
}

func validInstalledResultJournalName(name string) bool {
	if len(name) != len(installedResultJournalPrefix)+64+len(installedResultJournalSuffix) ||
		!strings.HasPrefix(name, installedResultJournalPrefix) ||
		!strings.HasSuffix(name, installedResultJournalSuffix) ||
		path.Base(name) != name {
		return false
	}
	encoded := strings.TrimSuffix(
		strings.TrimPrefix(name, installedResultJournalPrefix),
		installedResultJournalSuffix,
	)
	decoded, err := hex.DecodeString(encoded)
	if err != nil || len(decoded) != 32 || hex.EncodeToString(decoded) != encoded {
		zero(decoded)
		return false
	}
	zero(decoded)
	return true
}

func installedResultJournalName(claimDigest string) (string, error) {
	if !requestDigestPattern.MatchString(claimDigest) {
		return "", fmt.Errorf("installed result claim digest invalid")
	}
	name := installedResultJournalPrefix +
		strings.TrimPrefix(claimDigest, "sha256:") +
		installedResultJournalSuffix
	if !validInstalledResultJournalName(name) {
		return "", fmt.Errorf("installed result journal name invalid")
	}
	return name, nil
}

func canonicalInstalledResultMetadata(
	claim *installedReplayClaim,
	response []byte,
) ([]byte, error) {
	if claim == nil ||
		!requestDigestPattern.MatchString(claim.Digest) ||
		!validInstalledReplayClaimName(claim.Name) ||
		len(response) < 1 ||
		len(response) > maxInstalledResultResponse {
		return nil, fmt.Errorf("installed result journal input invalid")
	}
	canonical, err := canonicalJSON(map[string]any{
		"schema":                      installedResultJournalSchema,
		"claim_digest":                claim.Digest,
		"claim_name":                  claim.Name,
		"request_key_id":              claim.RequestKeyID,
		"request_id":                  claim.RequestID,
		"nonce":                       claim.Nonce,
		"raw_request_digest":          claim.RawRequestDigest,
		"canonical_envelope_digest":   claim.CanonicalEnvelopeDigest,
		"root_policy_digest":          claim.RootPolicyDigest,
		"authority_generation_digest": claim.AuthorityGenerationDigest,
		"state_generation_digest":     claim.StateGenerationDigest,
		"response_sha256":             sha256Digest(response),
		"response_bytes":              json.Number(strconv.Itoa(len(response))),
	})
	if err != nil || len(canonical) < 1 || len(canonical) > maxInstalledResultMetadata {
		zero(canonical)
		return nil, fmt.Errorf("installed result metadata canonicalization failed")
	}
	return canonical, nil
}

func encodeInstalledResultJournalFrame(metadata, response []byte) ([]byte, error) {
	if len(metadata) < 1 ||
		len(metadata) > maxInstalledResultMetadata ||
		len(response) < 1 ||
		len(response) > maxInstalledResultResponse {
		return nil, fmt.Errorf("installed result journal frame invalid")
	}
	size := len(installedResultJournalMagic) + 8 + len(metadata) + len(response)
	frame := make([]byte, size)
	offset := copy(frame, installedResultJournalMagic)
	binary.BigEndian.PutUint64(frame[offset:offset+8], uint64(len(metadata)))
	offset += 8
	offset += copy(frame[offset:], metadata)
	copy(frame[offset:], response)
	return frame, nil
}

func parseInstalledResultJournalFrame(
	raw []byte,
	name string,
) (*installedResultJournalRecord, error) {
	headerBytes := len(installedResultJournalMagic) + 8
	maximum := headerBytes + maxInstalledResultMetadata + maxInstalledResultResponse
	if len(raw) <= headerBytes ||
		len(raw) > maximum ||
		!validInstalledResultJournalName(name) ||
		!bytes.Equal(raw[:len(installedResultJournalMagic)], []byte(installedResultJournalMagic)) {
		return nil, fmt.Errorf("installed result journal record invalid")
	}
	metadataSize := binary.BigEndian.Uint64(
		raw[len(installedResultJournalMagic):headerBytes],
	)
	if metadataSize < 1 ||
		metadataSize > maxInstalledResultMetadata ||
		metadataSize > uint64(len(raw)-headerBytes) {
		return nil, fmt.Errorf("installed result journal record invalid")
	}
	metadataEnd := headerBytes + int(metadataSize)
	metadata := raw[headerBytes:metadataEnd]
	response := raw[metadataEnd:]
	if len(response) < 1 || len(response) > maxInstalledResultResponse {
		return nil, fmt.Errorf("installed result journal record invalid")
	}
	value, err := decodeStrictJSON(metadata)
	if err != nil {
		return nil, fmt.Errorf("installed result journal metadata invalid")
	}
	outer, ok := value.(map[string]any)
	if !ok || !hasExactKeys(
		outer,
		"schema",
		"claim_digest",
		"claim_name",
		"request_key_id",
		"request_id",
		"nonce",
		"raw_request_digest",
		"canonical_envelope_digest",
		"root_policy_digest",
		"authority_generation_digest",
		"state_generation_digest",
		"response_sha256",
		"response_bytes",
	) ||
		!exactStringEquals(outer["schema"], installedResultJournalSchema) {
		return nil, fmt.Errorf("installed result journal metadata invalid")
	}
	claimDigest, claimDigestOK := exactString(outer["claim_digest"])
	claimName, claimNameOK := exactString(outer["claim_name"])
	requestKeyID, requestKeyOK := exactString(outer["request_key_id"])
	requestID, requestIDOK := exactString(outer["request_id"])
	nonce, nonceOK := exactString(outer["nonce"])
	rawRequestDigest, rawRequestDigestOK := exactString(
		outer["raw_request_digest"],
	)
	envelopeDigest, envelopeDigestOK := exactString(
		outer["canonical_envelope_digest"],
	)
	rootPolicyDigest, rootPolicyDigestOK := exactString(
		outer["root_policy_digest"],
	)
	authorityGenerationDigest, authorityGenerationDigestOK := exactString(
		outer["authority_generation_digest"],
	)
	stateGenerationDigest, stateGenerationDigestOK := exactString(
		outer["state_generation_digest"],
	)
	responseDigest, responseDigestOK := exactString(outer["response_sha256"])
	responseBytes, responseBytesOK := exactBoundedInt(outer["response_bytes"], 1)
	if !claimDigestOK || !requestDigestPattern.MatchString(claimDigest) ||
		!claimNameOK || !validInstalledReplayClaimName(claimName) ||
		!requestKeyOK || !requestDigestPattern.MatchString(requestKeyID) ||
		!requestIDOK || !requestIdentifierPattern.MatchString(requestID) ||
		!nonceOK || !requestIdentifierPattern.MatchString(nonce) ||
		!rawRequestDigestOK ||
		!requestDigestPattern.MatchString(rawRequestDigest) ||
		!envelopeDigestOK || !requestDigestPattern.MatchString(envelopeDigest) ||
		!rootPolicyDigestOK ||
		!requestDigestPattern.MatchString(rootPolicyDigest) ||
		!authorityGenerationDigestOK ||
		!requestDigestPattern.MatchString(authorityGenerationDigest) ||
		!stateGenerationDigestOK ||
		!requestDigestPattern.MatchString(stateGenerationDigest) ||
		!responseDigestOK || !requestDigestPattern.MatchString(responseDigest) ||
		!responseBytesOK ||
		responseBytes > maxInstalledResultResponse ||
		responseBytes != int64(len(response)) ||
		responseDigest != sha256Digest(response) {
		return nil, fmt.Errorf("installed result journal metadata invalid")
	}
	canonical, err := canonicalJSON(outer)
	if err != nil || !bytes.Equal(canonical, metadata) {
		zero(canonical)
		return nil, fmt.Errorf("installed result journal metadata is not canonical")
	}
	if claimName != installedReplayClaimPrefix+
		strings.TrimPrefix(claimDigest, "sha256:")+
		installedReplayClaimSuffix {
		zero(canonical)
		return nil, fmt.Errorf("installed result journal claim binding invalid")
	}
	expectedName, err := installedResultJournalName(claimDigest)
	if err != nil || name != expectedName {
		zero(canonical)
		return nil, fmt.Errorf("installed result journal name mismatch")
	}
	responseCopy := make([]byte, len(response))
	copy(responseCopy, response)
	return &installedResultJournalRecord{
		ClaimDigest:               claimDigest,
		ClaimName:                 claimName,
		RequestKeyID:              requestKeyID,
		RequestID:                 requestID,
		Nonce:                     nonce,
		RawRequestDigest:          rawRequestDigest,
		CanonicalEnvelopeDigest:   envelopeDigest,
		RootPolicyDigest:          rootPolicyDigest,
		AuthorityGenerationDigest: authorityGenerationDigest,
		StateGenerationDigest:     stateGenerationDigest,
		ResponseDigest:            responseDigest,
		Metadata:                  canonical,
		Response:                  responseCopy,
		Name:                      name,
	}, nil
}

func installedResultRecordMatchesClaim(
	record *installedResultJournalRecord,
	claim *installedReplayClaim,
) bool {
	return record != nil &&
		claim != nil &&
		record.ClaimDigest == claim.Digest &&
		record.ClaimName == claim.Name &&
		record.RequestKeyID == claim.RequestKeyID &&
		record.RequestID == claim.RequestID &&
		record.Nonce == claim.Nonce &&
		record.RawRequestDigest == claim.RawRequestDigest &&
		record.CanonicalEnvelopeDigest == claim.CanonicalEnvelopeDigest &&
		record.RootPolicyDigest == claim.RootPolicyDigest &&
		record.AuthorityGenerationDigest == claim.AuthorityGenerationDigest &&
		record.StateGenerationDigest == claim.StateGenerationDigest
}

func sameInstalledResultJournalRecord(
	left *installedResultJournalRecord,
	right *installedResultJournalRecord,
) bool {
	return left != nil &&
		right != nil &&
		left.ClaimDigest == right.ClaimDigest &&
		left.ClaimName == right.ClaimName &&
		left.RequestKeyID == right.RequestKeyID &&
		left.RequestID == right.RequestID &&
		left.Nonce == right.Nonce &&
		left.RawRequestDigest == right.RawRequestDigest &&
		left.CanonicalEnvelopeDigest == right.CanonicalEnvelopeDigest &&
		left.RootPolicyDigest == right.RootPolicyDigest &&
		left.AuthorityGenerationDigest == right.AuthorityGenerationDigest &&
		left.StateGenerationDigest == right.StateGenerationDigest &&
		left.ResponseDigest == right.ResponseDigest &&
		left.Name == right.Name &&
		bytes.Equal(left.Metadata, right.Metadata) &&
		bytes.Equal(left.Response, right.Response)
}

func (journal *installedResultJournal) records(
	claims []*installedReplayClaim,
) ([]*installedResultJournalRecord, error) {
	if err := journal.validatePath(); err != nil {
		return nil, err
	}
	if _, err := syscall.Seek(journal.fd, 0, 0); err != nil {
		return nil, fmt.Errorf("installed result journal seek failed")
	}
	names, err := directoryNames(journal.fd)
	if err != nil || len(names) > maxInstalledResultRecords {
		return nil, fmt.Errorf("installed result journal entries invalid")
	}
	claimsByDigest := make(map[string]*installedReplayClaim, len(claims))
	for _, claim := range claims {
		if claim == nil {
			return nil, fmt.Errorf("installed result replay claim invalid")
		}
		if _, duplicate := claimsByDigest[claim.Digest]; duplicate {
			return nil, fmt.Errorf("installed result replay claim duplicated")
		}
		claimsByDigest[claim.Digest] = claim
	}
	records := make([]*installedResultJournalRecord, 0, len(names))
	releaseRecords := func() {
		releaseInstalledResultJournalRecords(records)
	}
	for _, name := range names {
		if !validInstalledResultJournalName(name) {
			releaseRecords()
			return nil, fmt.Errorf("installed result journal entry invalid")
		}
		fd, openErr := syscall.Openat(
			journal.fd,
			name,
			syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW|syscall.O_NONBLOCK,
			0,
		)
		if openErr != nil {
			releaseRecords()
			return nil, fmt.Errorf("installed result journal record open failed")
		}
		raw, identity, readErr := readStableFD(
			fd,
			int64(
				len(installedResultJournalMagic)+
					8+
					maxInstalledResultMetadata+
					maxInstalledResultResponse,
			),
			expectedFileMetadata{
				Mode: installedResultJournalMode,
				UID:  journal.paths.AuthorityUID,
				GID:  journal.paths.AuthorityGID,
			},
		)
		if readErr != nil {
			_ = syscall.Close(fd)
			zero(raw)
			releaseRecords()
			return nil, fmt.Errorf("installed result journal record read failed")
		}
		reopened, reopenErr := syscall.Openat(
			journal.fd,
			name,
			syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW|syscall.O_NONBLOCK,
			0,
		)
		var reopenedStat syscall.Stat_t
		if reopenErr == nil {
			reopenErr = syscall.Fstat(reopened, &reopenedStat)
			_ = syscall.Close(reopened)
		}
		_ = syscall.Close(fd)
		if reopenErr != nil || identityFromStat(reopenedStat) != identity {
			zero(raw)
			releaseRecords()
			return nil, fmt.Errorf("installed result journal record path changed")
		}
		record, parseErr := parseInstalledResultJournalFrame(raw, name)
		zero(raw)
		if parseErr != nil {
			releaseRecords()
			return nil, parseErr
		}
		claim, exists := claimsByDigest[record.ClaimDigest]
		if !exists || !installedResultRecordMatchesClaim(record, claim) {
			record.release()
			releaseRecords()
			return nil, fmt.Errorf("installed result journal replay claim missing")
		}
		records = append(records, record)
	}
	if err := journal.validatePath(); err != nil {
		releaseRecords()
		return nil, err
	}
	return records, nil
}

func releaseInstalledResultJournalRecords(records []*installedResultJournalRecord) {
	for _, record := range records {
		record.release()
	}
}

func findInstalledResultRecord(
	records []*installedResultJournalRecord,
	claim *installedReplayClaim,
) *installedResultJournalRecord {
	for _, record := range records {
		if installedResultRecordMatchesClaim(record, claim) {
			return record
		}
	}
	return nil
}

func installedReplayClaimsForResult(
	state *installedReplayState,
	expected *installedReplayClaim,
) ([]*installedReplayClaim, error) {
	if state == nil || expected == nil {
		return nil, fmt.Errorf("installed result replay claim unavailable")
	}
	claims, err := state.claims()
	if err != nil {
		releaseInstalledReplayClaims(claims)
		return nil, err
	}
	for _, claim := range claims {
		if sameInstalledReplayClaim(claim, expected) {
			return claims, nil
		}
		if claim.RequestID == expected.RequestID ||
			claim.Nonce == expected.Nonce ||
			claim.Digest == expected.Digest ||
			claim.Name == expected.Name {
			releaseInstalledReplayClaims(claims)
			return nil, fmt.Errorf("installed result replay claim binding mismatch")
		}
	}
	releaseInstalledReplayClaims(claims)
	return nil, fmt.Errorf("installed result replay claim missing")
}

func expectedInstalledResultJournalRecord(
	claim *installedReplayClaim,
	response []byte,
) (*installedResultJournalRecord, []byte, error) {
	metadata, err := canonicalInstalledResultMetadata(claim, response)
	if err != nil {
		return nil, nil, err
	}
	frame, err := encodeInstalledResultJournalFrame(metadata, response)
	if err != nil {
		zero(metadata)
		return nil, nil, err
	}
	name, err := installedResultJournalName(claim.Digest)
	if err != nil {
		zero(metadata)
		zero(frame)
		return nil, nil, err
	}
	record, err := parseInstalledResultJournalFrame(frame, name)
	if err != nil {
		zero(metadata)
		zero(frame)
		return nil, nil, err
	}
	zero(metadata)
	return record, frame, nil
}

func commitInstalledResultJournal(
	paths installedRuntimePaths,
	claim *installedReplayClaim,
	response []byte,
) (*installedResultJournalRecord, error) {
	expected, frame, err := expectedInstalledResultJournalRecord(claim, response)
	if err != nil {
		return nil, err
	}
	defer expected.release()
	defer zero(frame)

	state, err := openInstalledReplayState(paths)
	if err != nil {
		return nil, err
	}
	defer state.close()
	claims, err := installedReplayClaimsForResult(state, claim)
	if err != nil {
		return nil, err
	}
	defer func() {
		releaseInstalledReplayClaims(claims)
	}()

	journal, err := openInstalledResultJournal(paths)
	if err != nil {
		return nil, err
	}
	defer journal.close()
	records, err := journal.records(claims)
	if err != nil {
		releaseInstalledResultJournalRecords(records)
		return nil, err
	}
	existing := findInstalledResultRecord(records, claim)
	if existing != nil {
		if !sameInstalledResultJournalRecord(existing, expected) {
			releaseInstalledResultJournalRecords(records)
			return nil, installedResultConflictError{}
		}
		result := copyInstalledResultJournalRecord(existing)
		releaseInstalledResultJournalRecords(records)
		if result == nil {
			return nil, fmt.Errorf("installed result journal copy failed")
		}
		currentClaims, validateErr := installedReplayClaimsForResult(state, claim)
		if validateErr != nil {
			releaseInstalledReplayClaims(currentClaims)
			result.release()
			return nil, validateErr
		}
		releaseInstalledReplayClaims(claims)
		claims = currentClaims
		records, validateErr = journal.records(claims)
		if validateErr != nil {
			releaseInstalledResultJournalRecords(records)
			result.release()
			return nil, validateErr
		}
		current := findInstalledResultRecord(records, claim)
		if current == nil || !sameInstalledResultJournalRecord(current, result) {
			releaseInstalledResultJournalRecords(records)
			result.release()
			return nil, fmt.Errorf("installed result journal changed during adoption")
		}
		releaseInstalledResultJournalRecords(records)
		return result, nil
	}
	releaseInstalledResultJournalRecords(records)

	fd, err := syscall.Openat(
		journal.fd,
		expected.Name,
		syscall.O_RDWR|syscall.O_CREAT|syscall.O_EXCL|syscall.O_CLOEXEC|syscall.O_NOFOLLOW,
		installedResultJournalMode,
	)
	if err != nil {
		if errors.Is(err, syscall.EEXIST) {
			return nil, installedResultConflictError{}
		}
		return nil, fmt.Errorf("installed result journal creation failed")
	}
	committed := false
	defer func() {
		if !committed {
			_ = syscall.Fsync(fd)
			_ = syscall.Fsync(journal.fd)
		}
		_ = syscall.Close(fd)
	}()
	if err := syscall.Fchmod(fd, installedResultJournalMode); err != nil {
		return nil, fmt.Errorf("installed result journal metadata failed")
	}
	var created syscall.Stat_t
	if err := syscall.Fstat(fd, &created); err != nil {
		return nil, fmt.Errorf("installed result journal metadata failed")
	}
	createdIdentity := identityFromStat(created)
	if createdIdentity.Mode&syscall.S_IFMT != syscall.S_IFREG ||
		createdIdentity.Links != 1 ||
		createdIdentity.Mode&0o7777 != installedResultJournalMode ||
		createdIdentity.UID != paths.AuthorityUID ||
		createdIdentity.GID != paths.AuthorityGID ||
		createdIdentity.Size != 0 {
		return nil, fmt.Errorf("installed result journal metadata invalid")
	}
	for offset := 0; offset < len(frame); {
		count, writeErr := syscall.Pwrite(fd, frame[offset:], int64(offset))
		if writeErr != nil || count < 1 || count > len(frame)-offset {
			return nil, fmt.Errorf("installed result journal write failed")
		}
		offset += count
	}
	if err := syscall.Fsync(fd); err != nil {
		return nil, fmt.Errorf("installed result journal file sync failed")
	}
	var after syscall.Stat_t
	if err := syscall.Fstat(fd, &after); err != nil ||
		after.Size != int64(len(frame)) ||
		after.Dev != created.Dev ||
		after.Ino != created.Ino ||
		after.Nlink != 1 ||
		after.Mode != created.Mode ||
		after.Uid != created.Uid ||
		after.Gid != created.Gid {
		return nil, fmt.Errorf("installed result journal record changed")
	}
	readback := make([]byte, len(frame))
	for offset := 0; offset < len(readback); {
		count, readErr := syscall.Pread(fd, readback[offset:], int64(offset))
		if readErr != nil || count < 1 || count > len(readback)-offset {
			zero(readback)
			return nil, fmt.Errorf("installed result journal readback failed")
		}
		offset += count
	}
	if !bytes.Equal(readback, frame) {
		zero(readback)
		return nil, fmt.Errorf("installed result journal readback mismatch")
	}
	zero(readback)
	if err := syscall.Fsync(journal.fd); err != nil {
		return nil, fmt.Errorf("installed result journal directory sync failed")
	}
	if err := journal.validatePath(); err != nil {
		return nil, err
	}

	currentClaims, err := installedReplayClaimsForResult(state, claim)
	if err != nil {
		return nil, err
	}
	releaseInstalledReplayClaims(claims)
	claims = currentClaims
	records, err = journal.records(claims)
	if err != nil {
		releaseInstalledResultJournalRecords(records)
		return nil, err
	}
	persisted := findInstalledResultRecord(records, claim)
	if persisted == nil || !sameInstalledResultJournalRecord(persisted, expected) {
		releaseInstalledResultJournalRecords(records)
		return nil, fmt.Errorf("installed result journal persistence failed")
	}
	result := copyInstalledResultJournalRecord(persisted)
	releaseInstalledResultJournalRecords(records)
	if result == nil {
		return nil, fmt.Errorf("installed result journal copy failed")
	}
	committed = true
	return result, nil
}

func loadInstalledResultJournal(
	paths installedRuntimePaths,
	claim *installedReplayClaim,
) (*installedResultJournalRecord, error) {
	state, err := openInstalledReplayState(paths)
	if err != nil {
		return nil, err
	}
	defer state.close()
	claims, err := installedReplayClaimsForResult(state, claim)
	if err != nil {
		return nil, err
	}
	defer releaseInstalledReplayClaims(claims)
	journal, err := openInstalledResultJournal(paths)
	if err != nil {
		return nil, err
	}
	defer journal.close()
	records, err := journal.records(claims)
	if err != nil {
		releaseInstalledResultJournalRecords(records)
		return nil, err
	}
	record := findInstalledResultRecord(records, claim)
	if record == nil {
		releaseInstalledResultJournalRecords(records)
		return nil, fmt.Errorf("installed result journal record missing")
	}
	result := copyInstalledResultJournalRecord(record)
	releaseInstalledResultJournalRecords(records)
	if result == nil {
		return nil, fmt.Errorf("installed result journal copy failed")
	}
	currentClaims, err := installedReplayClaimsForResult(state, claim)
	if err != nil {
		releaseInstalledReplayClaims(currentClaims)
		result.release()
		return nil, err
	}
	records, err = journal.records(currentClaims)
	releaseInstalledReplayClaims(currentClaims)
	if err != nil {
		releaseInstalledResultJournalRecords(records)
		result.release()
		return nil, err
	}
	current := findInstalledResultRecord(records, claim)
	if current == nil || !sameInstalledResultJournalRecord(current, result) {
		releaseInstalledResultJournalRecords(records)
		result.release()
		return nil, fmt.Errorf("installed result journal changed during load")
	}
	releaseInstalledResultJournalRecords(records)
	if err := journal.validatePath(); err != nil {
		result.release()
		return nil, err
	}
	return result, nil
}

func copyInstalledResultJournalRecord(
	record *installedResultJournalRecord,
) *installedResultJournalRecord {
	if record == nil {
		return nil
	}
	result := *record
	result.Metadata = make([]byte, len(record.Metadata))
	copy(result.Metadata, record.Metadata)
	result.Response = make([]byte, len(record.Response))
	copy(result.Response, record.Response)
	return &result
}
