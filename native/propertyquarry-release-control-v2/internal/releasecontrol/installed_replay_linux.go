//go:build linux

package releasecontrol

import (
	"bytes"
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
	installedReplayClaimSchema         = "propertyquarry.release-control.replay-claim.v3"
	installedReplayClaimPrefix         = "claim-v3-"
	installedReplayClaimSuffix         = ".json"
	installedAuthorityGenerationSchema = "propertyquarry.release-control.installed-authority-generation.v1"
	installedStateGenerationSchema     = "propertyquarry.release-control.authority-state-generation.v1"
	installedReplayClaimMode           = 0o600
	maxInstalledReplayClaim            = 64 * 1024
	maxInstalledAuthorityGeneration    = 48 * 1024
	maxInstalledStateGeneration        = 2048
	maxInstalledReplayClaims           = 65536
)

type installedReplayClaim struct {
	RequestKeyID              string
	RequestID                 string
	Nonce                     string
	RawRequestDigest          string
	CanonicalEnvelopeDigest   string
	RootPolicyDigest          string
	AuthorityGenerationDigest string
	StateGenerationDigest     string
	StateGeneration           stableIdentity
	Canonical                 []byte
	Digest                    string
	Name                      string
}

func (claim *installedReplayClaim) release() {
	if claim == nil {
		return
	}
	zero(claim.Canonical)
	*claim = installedReplayClaim{}
}

type installedReplayRejectedError struct{}

func (installedReplayRejectedError) Error() string {
	return "installed request replay rejected"
}

type installedReplayState struct {
	fd       int
	identity stableIdentity
	paths    installedRuntimePaths
}

// lockedInstalledReplayAdoption is a live, package-private capability. Its
// replay-state descriptor remains flocked until close, and every method fails
// after close. Callers must not retain it beyond withLockedInstalledRequestReplay.
type lockedInstalledReplayAdoption struct {
	state    *installedReplayState
	expected *installedReplayClaim
}

func openInstalledReplayState(paths installedRuntimePaths) (*installedReplayState, error) {
	if err := validateInstalledPrincipalContract(paths); err != nil {
		return nil, err
	}
	fd, err := openRootedAbsolute(paths.Root, paths.StateRoot, syscall.O_DIRECTORY)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(fd, syscall.LOCK_EX); err != nil {
		_ = syscall.Close(fd)
		return nil, fmt.Errorf("installed replay state lock failed")
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
	state := &installedReplayState{fd: fd, identity: identity, paths: paths}
	if err := state.validatePath(); err != nil {
		state.close()
		return nil, err
	}
	return state, nil
}

func (state *installedReplayState) close() {
	if state == nil || state.fd < 0 {
		return
	}
	_ = syscall.Flock(state.fd, syscall.LOCK_UN)
	_ = syscall.Close(state.fd)
	state.fd = -1
	state.identity = stableIdentity{}
}

func (adoption *lockedInstalledReplayAdoption) close() {
	if adoption == nil {
		return
	}
	if adoption.expected != nil {
		adoption.expected.release()
		adoption.expected = nil
	}
	if adoption.state != nil {
		adoption.state.close()
		adoption.state = nil
	}
}

func (state *installedReplayState) validatePath() error {
	if state == nil || state.fd < 0 {
		return fmt.Errorf("installed replay state unavailable")
	}
	var current syscall.Stat_t
	if err := syscall.Fstat(state.fd, &current); err != nil ||
		!sameInstalledDirectoryObject(state.identity, identityFromStat(current)) {
		return fmt.Errorf("installed replay state changed")
	}
	reopened, err := openRootedAbsolute(
		state.paths.Root,
		state.paths.StateRoot,
		syscall.O_DIRECTORY,
	)
	if err != nil {
		return err
	}
	var reopenedStat syscall.Stat_t
	statErr := syscall.Fstat(reopened, &reopenedStat)
	_ = syscall.Close(reopened)
	if statErr != nil ||
		!sameInstalledDirectoryObject(state.identity, identityFromStat(reopenedStat)) {
		return fmt.Errorf("installed replay state path changed")
	}
	return nil
}

func installedAuthorityGenerationValue(
	verification *installedAuthorityVerification,
) (map[string]any, error) {
	if verification == nil ||
		!requestDigestPattern.MatchString(verification.AuthenticationDigest) ||
		!requestDigestPattern.MatchString(verification.PayloadTreeDigest) ||
		!requestDigestPattern.MatchString(verification.AuthorityKeyID) ||
		!requestDigestPattern.MatchString(verification.ManifestDigest) ||
		!requestDigestPattern.MatchString(verification.NativeBuildDigest) ||
		len(verification.Roles) != installedRoleCount {
		return nil, fmt.Errorf("installed authority generation invalid")
	}
	roles := make(map[string]any, len(verification.Roles))
	for name, role := range verification.Roles {
		if !requestIdentifierPattern.MatchString(name) ||
			role.Contract.Role != name ||
			!path.IsAbs(role.Contract.Path) ||
			path.Clean(role.Contract.Path) != role.Contract.Path ||
			role.Contract.Path == "/" ||
			len(role.Contract.Path) > 4096 ||
			role.Contract.Mode < 1 ||
			role.Contract.Mode > 0o7777 ||
			!requestDigestPattern.MatchString(role.Digest) ||
			role.Size < 1 ||
			role.Size > maxInstalledRoleBytes {
			return nil, fmt.Errorf("installed authority generation role invalid")
		}
		roles[name] = map[string]any{
			"role":    role.Contract.Role,
			"path":    role.Contract.Path,
			"mode":    json.Number(strconv.FormatUint(uint64(role.Contract.Mode), 10)),
			"private": role.Contract.Private,
			"digest":  role.Digest,
			"size":    json.Number(strconv.FormatInt(role.Size, 10)),
			"uid":     json.Number(strconv.FormatUint(uint64(role.UID), 10)),
			"gid":     json.Number(strconv.FormatUint(uint64(role.GID), 10)),
		}
	}
	return map[string]any{
		"schema":                installedAuthorityGenerationSchema,
		"authentication_digest": verification.AuthenticationDigest,
		"payload_tree_digest":   verification.PayloadTreeDigest,
		"authority_key_id":      verification.AuthorityKeyID,
		"manifest_digest":       verification.ManifestDigest,
		"native_build_digest":   verification.NativeBuildDigest,
		"roles":                 roles,
	}, nil
}

func parseInstalledAuthorityGeneration(
	value any,
) ([]byte, string, error) {
	outer, ok := value.(map[string]any)
	if !ok || !hasExactKeys(
		outer,
		"schema",
		"authentication_digest",
		"payload_tree_digest",
		"authority_key_id",
		"manifest_digest",
		"native_build_digest",
		"roles",
	) ||
		!exactStringEquals(outer["schema"], installedAuthorityGenerationSchema) {
		return nil, "", fmt.Errorf("installed authority generation invalid")
	}
	for _, key := range []string{
		"authentication_digest",
		"payload_tree_digest",
		"authority_key_id",
		"manifest_digest",
		"native_build_digest",
	} {
		digest, digestOK := exactString(outer[key])
		if !digestOK || !requestDigestPattern.MatchString(digest) {
			return nil, "", fmt.Errorf("installed authority generation invalid")
		}
	}
	roles, ok := outer["roles"].(map[string]any)
	if !ok || len(roles) != installedRoleCount {
		return nil, "", fmt.Errorf("installed authority generation roles invalid")
	}
	for name, rawRole := range roles {
		role, roleOK := rawRole.(map[string]any)
		if !roleOK || !hasExactKeys(
			role,
			"role",
			"path",
			"mode",
			"private",
			"digest",
			"size",
			"uid",
			"gid",
		) ||
			!requestIdentifierPattern.MatchString(name) {
			return nil, "", fmt.Errorf("installed authority generation role invalid")
		}
		roleName, roleNameOK := exactString(role["role"])
		rolePath, rolePathOK := exactString(role["path"])
		mode, modeOK := exactBoundedInt(role["mode"], 1)
		_, privateOK := role["private"].(bool)
		digest, digestOK := exactString(role["digest"])
		size, sizeOK := exactBoundedInt(role["size"], 1)
		uid, uidOK := exactBoundedInt(role["uid"], 0)
		gid, gidOK := exactBoundedInt(role["gid"], 0)
		if !roleNameOK || roleName != name ||
			!rolePathOK ||
			!path.IsAbs(rolePath) ||
			path.Clean(rolePath) != rolePath ||
			rolePath == "/" ||
			len(rolePath) > 4096 ||
			!modeOK ||
			mode > 0o7777 ||
			!privateOK ||
			!digestOK ||
			!requestDigestPattern.MatchString(digest) ||
			!sizeOK ||
			size > maxInstalledRoleBytes ||
			!uidOK ||
			uid > int64(^uint32(0)) ||
			!gidOK ||
			gid > int64(^uint32(0)) {
			return nil, "", fmt.Errorf("installed authority generation role invalid")
		}
	}
	canonical, err := canonicalJSON(outer)
	if err != nil ||
		len(canonical) < 1 ||
		len(canonical) > maxInstalledAuthorityGeneration {
		zero(canonical)
		return nil, "", fmt.Errorf("installed authority generation canonicalization failed")
	}
	return canonical, sha256Digest(canonical), nil
}

func canonicalInstalledAuthorityGeneration(
	verification *installedAuthorityVerification,
) (map[string]any, []byte, string, error) {
	value, err := installedAuthorityGenerationValue(verification)
	if err != nil {
		return nil, nil, "", err
	}
	canonical, digest, err := parseInstalledAuthorityGeneration(value)
	if err != nil {
		return nil, nil, "", err
	}
	return value, canonical, digest, nil
}

func exactInstalledUnsignedString(value any, bits int) (uint64, bool) {
	text, ok := exactString(value)
	if !ok || text == "" || (len(text) > 1 && text[0] == '0') {
		return 0, false
	}
	parsed, err := strconv.ParseUint(text, 10, bits)
	if err != nil || strconv.FormatUint(parsed, 10) != text {
		return 0, false
	}
	return parsed, true
}

func installedStateGenerationValue(
	identity stableIdentity,
	paths installedRuntimePaths,
) (map[string]any, error) {
	if err := validateDirectoryIdentity(
		identity,
		expectedFileMetadata{
			Mode: 0o700,
			UID:  paths.AuthorityUID,
			GID:  paths.AuthorityGID,
		},
	); err != nil || identity.Links < 1 {
		return nil, fmt.Errorf("installed authority state generation invalid")
	}
	return map[string]any{
		"schema":  installedStateGenerationSchema,
		"device":  strconv.FormatUint(identity.Device, 10),
		"inode":   strconv.FormatUint(identity.Inode, 10),
		"rdevice": strconv.FormatUint(identity.Rdevice, 10),
		"mode":    strconv.FormatUint(uint64(identity.Mode), 10),
		"links":   strconv.FormatUint(identity.Links, 10),
		"uid":     strconv.FormatUint(uint64(identity.UID), 10),
		"gid":     strconv.FormatUint(uint64(identity.GID), 10),
	}, nil
}

func parseInstalledStateGeneration(
	value any,
) (stableIdentity, []byte, string, error) {
	outer, ok := value.(map[string]any)
	if !ok || !hasExactKeys(
		outer,
		"schema",
		"device",
		"inode",
		"rdevice",
		"mode",
		"links",
		"uid",
		"gid",
	) ||
		!exactStringEquals(outer["schema"], installedStateGenerationSchema) {
		return stableIdentity{}, nil, "", fmt.Errorf("installed authority state generation invalid")
	}
	device, deviceOK := exactInstalledUnsignedString(outer["device"], 64)
	inode, inodeOK := exactInstalledUnsignedString(outer["inode"], 64)
	rdevice, rdeviceOK := exactInstalledUnsignedString(outer["rdevice"], 64)
	mode, modeOK := exactInstalledUnsignedString(outer["mode"], 32)
	links, linksOK := exactInstalledUnsignedString(outer["links"], 64)
	uid, uidOK := exactInstalledUnsignedString(outer["uid"], 32)
	gid, gidOK := exactInstalledUnsignedString(outer["gid"], 32)
	identity := stableIdentity{
		Device:  device,
		Inode:   inode,
		Rdevice: rdevice,
		Mode:    uint32(mode),
		Links:   links,
		UID:     uint32(uid),
		GID:     uint32(gid),
	}
	if !deviceOK ||
		!inodeOK ||
		!rdeviceOK ||
		!modeOK ||
		!linksOK ||
		links < 1 ||
		!uidOK ||
		!gidOK ||
		validateDirectoryIdentity(
			identity,
			expectedFileMetadata{
				Mode: 0o700,
				UID:  identity.UID,
				GID:  identity.GID,
			},
		) != nil {
		return stableIdentity{}, nil, "", fmt.Errorf("installed authority state generation invalid")
	}
	canonical, err := canonicalJSON(outer)
	if err != nil ||
		len(canonical) < 1 ||
		len(canonical) > maxInstalledStateGeneration {
		zero(canonical)
		return stableIdentity{}, nil, "", fmt.Errorf("installed authority state generation canonicalization failed")
	}
	return identity, canonical, sha256Digest(canonical), nil
}

func canonicalInstalledStateGeneration(
	identity stableIdentity,
	paths installedRuntimePaths,
) (map[string]any, []byte, string, error) {
	value, err := installedStateGenerationValue(identity, paths)
	if err != nil {
		return nil, nil, "", err
	}
	parsed, canonical, digest, err := parseInstalledStateGeneration(value)
	if err != nil || !sameInstalledDirectoryObject(parsed, identity) {
		zero(canonical)
		return nil, nil, "", fmt.Errorf("installed authority state generation invalid")
	}
	return value, canonical, digest, nil
}

func replayClaimForAuthenticatedRequest(
	paths installedRuntimePaths,
	request *quarantinedRequest,
	rootPolicyDigest string,
	verification *installedAuthorityVerification,
	stateGeneration stableIdentity,
) (*installedReplayClaim, error) {
	if request == nil ||
		verification == nil ||
		validateInstalledPrincipalContract(paths) != nil ||
		!request.authenticationEstablished ||
		!requestDigestPattern.MatchString(request.authenticatedKeyID) ||
		!requestDigestPattern.MatchString(request.rawBodyDigest) ||
		sha256Digest(request.rawBody) != request.rawBodyDigest ||
		!requestDigestPattern.MatchString(request.canonicalBodyDigest) ||
		sha256Digest(request.canonicalBody) != request.canonicalBodyDigest ||
		!request.envelopeDigestMatches ||
		request.claimedEnvelopeDigest != request.canonicalEnvelopeDigest ||
		sha256Digest(request.canonicalEnvelope) != request.canonicalEnvelopeDigest ||
		!installedReplayEnvelopeIntact(request) ||
		!requestIdentifierPattern.MatchString(request.envelope.RequestID) ||
		!requestIdentifierPattern.MatchString(request.envelope.Nonce) ||
		!requestDigestPattern.MatchString(request.canonicalEnvelopeDigest) ||
		!requestDigestPattern.MatchString(rootPolicyDigest) {
		return nil, fmt.Errorf("installed replay claim input invalid")
	}
	rootPolicyRole, ok := verification.Roles["root-policy"]
	if !ok || rootPolicyRole.Digest != rootPolicyDigest {
		return nil, fmt.Errorf("installed replay root policy binding invalid")
	}
	signature, err := parseRequestSignature(
		request.requestSignature,
		request.authenticatedKeyID,
	)
	zero(signature)
	if err != nil {
		return nil, fmt.Errorf("installed replay claim key binding invalid")
	}
	authorityValue, authorityCanonical, authorityDigest, err :=
		canonicalInstalledAuthorityGeneration(verification)
	if err != nil {
		return nil, err
	}
	defer zero(authorityCanonical)
	stateValue, stateCanonical, stateDigest, err :=
		canonicalInstalledStateGeneration(stateGeneration, paths)
	if err != nil {
		return nil, err
	}
	defer zero(stateCanonical)
	canonical, err := canonicalJSON(map[string]any{
		"schema":                      installedReplayClaimSchema,
		"request_key_id":              request.authenticatedKeyID,
		"request_id":                  request.envelope.RequestID,
		"nonce":                       request.envelope.Nonce,
		"raw_request_digest":          request.rawBodyDigest,
		"canonical_envelope_digest":   request.canonicalEnvelopeDigest,
		"root_policy_digest":          rootPolicyDigest,
		"authority_generation":        authorityValue,
		"authority_generation_digest": authorityDigest,
		"state_generation":            stateValue,
		"state_generation_digest":     stateDigest,
	})
	if err != nil || len(canonical) < 1 || len(canonical) > maxInstalledReplayClaim {
		zero(canonical)
		return nil, fmt.Errorf("installed replay claim canonicalization failed")
	}
	digest := sha256Digest(canonical)
	return &installedReplayClaim{
		RequestKeyID:              request.authenticatedKeyID,
		RequestID:                 request.envelope.RequestID,
		Nonce:                     request.envelope.Nonce,
		RawRequestDigest:          request.rawBodyDigest,
		CanonicalEnvelopeDigest:   request.canonicalEnvelopeDigest,
		RootPolicyDigest:          rootPolicyDigest,
		AuthorityGenerationDigest: authorityDigest,
		StateGenerationDigest:     stateDigest,
		StateGeneration:           stateGeneration,
		Canonical:                 canonical,
		Digest:                    digest,
		Name: installedReplayClaimPrefix +
			strings.TrimPrefix(digest, "sha256:") +
			installedReplayClaimSuffix,
	}, nil
}

func installedReplayEnvelopeIntact(request *quarantinedRequest) bool {
	if request == nil || len(request.canonicalEnvelope) == 0 {
		return false
	}
	value, err := decodeStrictJSON(request.canonicalEnvelope)
	if err != nil {
		return false
	}
	envelope, ok := value.(map[string]any)
	if !ok || !hasExactKeys(
		envelope,
		"operation",
		"request_id",
		"nonce",
		"issued_at",
		"expires_at",
		"identity",
	) {
		return false
	}
	canonical, err := canonicalJSON(envelope)
	if err != nil {
		return false
	}
	canonicalMatches := bytes.Equal(canonical, request.canonicalEnvelope)
	zero(canonical)
	if !canonicalMatches {
		return false
	}
	operation, operationOK := exactString(envelope["operation"])
	requestID, requestOK := exactString(envelope["request_id"])
	nonce, nonceOK := exactString(envelope["nonce"])
	issuedAt, issuedOK := exactBoundedInt(envelope["issued_at"], 0)
	expiresAt, expiresOK := exactBoundedInt(envelope["expires_at"], 0)
	identityObject, identityOK := envelope["identity"].(map[string]any)
	identity, parsedIdentity := parseQuarantinedIdentity(identityObject)
	return operationOK &&
		requestOK &&
		nonceOK &&
		issuedOK &&
		expiresOK &&
		identityOK &&
		parsedIdentity &&
		request.envelope == (quarantinedEnvelope{
			Operation: operation,
			RequestID: requestID,
			Nonce:     nonce,
			IssuedAt:  issuedAt,
			ExpiresAt: expiresAt,
			Identity:  identity,
		})
}

func parseInstalledReplayClaim(raw []byte, name string) (*installedReplayClaim, error) {
	if len(raw) < 1 || len(raw) > maxInstalledReplayClaim ||
		!validInstalledReplayClaimName(name) {
		return nil, fmt.Errorf("installed replay claim invalid")
	}
	value, err := decodeStrictJSON(raw)
	if err != nil {
		return nil, fmt.Errorf("installed replay claim invalid")
	}
	outer, ok := value.(map[string]any)
	if !ok || !hasExactKeys(
		outer,
		"schema",
		"request_key_id",
		"request_id",
		"nonce",
		"raw_request_digest",
		"canonical_envelope_digest",
		"root_policy_digest",
		"authority_generation",
		"authority_generation_digest",
		"state_generation",
		"state_generation_digest",
	) ||
		!exactStringEquals(outer["schema"], installedReplayClaimSchema) {
		return nil, fmt.Errorf("installed replay claim invalid")
	}
	requestKeyID, keyOK := exactString(outer["request_key_id"])
	requestID, requestOK := exactString(outer["request_id"])
	nonce, nonceOK := exactString(outer["nonce"])
	rawRequestDigest, rawRequestOK := exactString(outer["raw_request_digest"])
	envelopeDigest, envelopeOK := exactString(outer["canonical_envelope_digest"])
	rootPolicyDigest, rootPolicyOK := exactString(outer["root_policy_digest"])
	authorityDigest, authorityDigestOK := exactString(
		outer["authority_generation_digest"],
	)
	stateDigest, stateDigestOK := exactString(outer["state_generation_digest"])
	if !keyOK || !requestDigestPattern.MatchString(requestKeyID) ||
		!requestOK || !requestIdentifierPattern.MatchString(requestID) ||
		!nonceOK || !requestIdentifierPattern.MatchString(nonce) ||
		!rawRequestOK || !requestDigestPattern.MatchString(rawRequestDigest) ||
		!envelopeOK || !requestDigestPattern.MatchString(envelopeDigest) ||
		!rootPolicyOK || !requestDigestPattern.MatchString(rootPolicyDigest) ||
		!authorityDigestOK || !requestDigestPattern.MatchString(authorityDigest) ||
		!stateDigestOK || !requestDigestPattern.MatchString(stateDigest) {
		return nil, fmt.Errorf("installed replay claim invalid")
	}
	authorityCanonical, derivedAuthorityDigest, err :=
		parseInstalledAuthorityGeneration(outer["authority_generation"])
	if err != nil || authorityDigest != derivedAuthorityDigest {
		zero(authorityCanonical)
		return nil, fmt.Errorf("installed replay authority generation invalid")
	}
	zero(authorityCanonical)
	stateGeneration, stateCanonical, derivedStateDigest, err :=
		parseInstalledStateGeneration(outer["state_generation"])
	if err != nil || stateDigest != derivedStateDigest {
		zero(stateCanonical)
		return nil, fmt.Errorf("installed replay state generation invalid")
	}
	zero(stateCanonical)
	canonical, err := canonicalJSON(outer)
	if err != nil {
		return nil, fmt.Errorf("installed replay claim invalid")
	}
	if !bytes.Equal(raw, canonical) {
		zero(canonical)
		return nil, fmt.Errorf("installed replay claim is not canonical")
	}
	digest := sha256Digest(canonical)
	expectedName := installedReplayClaimPrefix +
		strings.TrimPrefix(digest, "sha256:") +
		installedReplayClaimSuffix
	if name != expectedName {
		zero(canonical)
		return nil, fmt.Errorf("installed replay claim name mismatch")
	}
	return &installedReplayClaim{
		RequestKeyID:              requestKeyID,
		RequestID:                 requestID,
		Nonce:                     nonce,
		RawRequestDigest:          rawRequestDigest,
		CanonicalEnvelopeDigest:   envelopeDigest,
		RootPolicyDigest:          rootPolicyDigest,
		AuthorityGenerationDigest: authorityDigest,
		StateGenerationDigest:     stateDigest,
		StateGeneration:           stateGeneration,
		Canonical:                 canonical,
		Digest:                    digest,
		Name:                      name,
	}, nil
}

func validInstalledReplayClaimName(name string) bool {
	if len(name) != len(installedReplayClaimPrefix)+64+len(installedReplayClaimSuffix) ||
		!strings.HasPrefix(name, installedReplayClaimPrefix) ||
		!strings.HasSuffix(name, installedReplayClaimSuffix) ||
		path.Base(name) != name {
		return false
	}
	encoded := strings.TrimSuffix(
		strings.TrimPrefix(name, installedReplayClaimPrefix),
		installedReplayClaimSuffix,
	)
	decoded, err := hex.DecodeString(encoded)
	if err != nil || len(decoded) != 32 || hex.EncodeToString(decoded) != encoded {
		return false
	}
	zero(decoded)
	return true
}

func (state *installedReplayState) claims() ([]*installedReplayClaim, error) {
	if err := state.validatePath(); err != nil {
		return nil, err
	}
	if _, err := syscall.Seek(state.fd, 0, 0); err != nil {
		return nil, fmt.Errorf("installed replay state seek failed")
	}
	names, err := directoryNames(state.fd)
	if err != nil || len(names) > maxInstalledReplayClaims {
		return nil, fmt.Errorf("installed replay state entries invalid")
	}
	claims := make([]*installedReplayClaim, 0, len(names))
	releaseClaims := func() {
		for _, claim := range claims {
			claim.release()
		}
	}
	requestIDs := make(map[string]struct{}, len(names))
	nonces := make(map[string]struct{}, len(names))
	for _, name := range names {
		if !validInstalledReplayClaimName(name) {
			releaseClaims()
			return nil, fmt.Errorf("installed replay state entry invalid")
		}
		fd, openErr := syscall.Openat(
			state.fd,
			name,
			syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW|syscall.O_NONBLOCK,
			0,
		)
		if openErr != nil {
			releaseClaims()
			return nil, fmt.Errorf("installed replay claim open failed")
		}
		raw, identity, readErr := readStableFD(
			fd,
			maxInstalledReplayClaim,
			expectedFileMetadata{
				Mode: installedReplayClaimMode,
				UID:  state.paths.AuthorityUID,
				GID:  state.paths.AuthorityGID,
			},
		)
		if readErr != nil {
			_ = syscall.Close(fd)
			zero(raw)
			releaseClaims()
			return nil, fmt.Errorf("installed replay claim read failed")
		}
		reopened, reopenErr := syscall.Openat(
			state.fd,
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
			releaseClaims()
			return nil, fmt.Errorf("installed replay claim path changed")
		}
		claim, parseErr := parseInstalledReplayClaim(raw, name)
		zero(raw)
		if parseErr != nil {
			releaseClaims()
			return nil, parseErr
		}
		if _, duplicate := requestIDs[claim.RequestID]; duplicate {
			claim.release()
			releaseClaims()
			return nil, fmt.Errorf("installed replay request id duplicated")
		}
		if _, duplicate := nonces[claim.Nonce]; duplicate {
			claim.release()
			releaseClaims()
			return nil, fmt.Errorf("installed replay nonce duplicated")
		}
		requestIDs[claim.RequestID] = struct{}{}
		nonces[claim.Nonce] = struct{}{}
		claims = append(claims, claim)
	}
	if err := state.validatePath(); err != nil {
		releaseClaims()
		return nil, err
	}
	return claims, nil
}

func releaseInstalledReplayClaims(claims []*installedReplayClaim) {
	for _, claim := range claims {
		claim.release()
	}
}

func sameInstalledReplayClaim(left, right *installedReplayClaim) bool {
	return left != nil &&
		right != nil &&
		left.RequestKeyID == right.RequestKeyID &&
		left.RequestID == right.RequestID &&
		left.Nonce == right.Nonce &&
		left.RawRequestDigest == right.RawRequestDigest &&
		left.CanonicalEnvelopeDigest == right.CanonicalEnvelopeDigest &&
		left.RootPolicyDigest == right.RootPolicyDigest &&
		left.AuthorityGenerationDigest == right.AuthorityGenerationDigest &&
		left.StateGenerationDigest == right.StateGenerationDigest &&
		sameInstalledDirectoryObject(
			left.StateGeneration,
			right.StateGeneration,
		) &&
		left.Digest == right.Digest &&
		left.Name == right.Name &&
		bytes.Equal(left.Canonical, right.Canonical)
}

func validateInstalledAuthorityState(
	paths installedRuntimePaths,
) (stableIdentity, error) {
	state, err := openInstalledReplayState(paths)
	if err != nil {
		return stableIdentity{}, err
	}
	defer state.close()
	claims, err := state.claims()
	releaseInstalledReplayClaims(claims)
	if err != nil {
		return stableIdentity{}, err
	}
	return state.identity, nil
}

func claimInstalledRequestReplay(
	paths installedRuntimePaths,
	request *quarantinedRequest,
	rootPolicyDigest string,
	verification *installedAuthorityVerification,
	stateGeneration stableIdentity,
) error {
	expected, err := replayClaimForAuthenticatedRequest(
		paths,
		request,
		rootPolicyDigest,
		verification,
		stateGeneration,
	)
	if err != nil {
		return err
	}
	defer expected.release()
	state, err := openInstalledReplayState(paths)
	if err != nil {
		return err
	}
	defer state.close()
	if !sameInstalledDirectoryObject(state.identity, stateGeneration) {
		return fmt.Errorf("installed replay state generation mismatch")
	}
	claims, err := state.claims()
	if err != nil {
		releaseInstalledReplayClaims(claims)
		return err
	}
	if len(claims) >= maxInstalledReplayClaims {
		releaseInstalledReplayClaims(claims)
		return fmt.Errorf("installed replay state capacity exhausted")
	}
	for _, existing := range claims {
		if sameInstalledReplayClaim(existing, expected) ||
			existing.RequestID == expected.RequestID ||
			existing.Nonce == expected.Nonce {
			releaseInstalledReplayClaims(claims)
			return installedReplayRejectedError{}
		}
	}
	releaseInstalledReplayClaims(claims)

	fd, err := syscall.Openat(
		state.fd,
		expected.Name,
		syscall.O_RDWR|syscall.O_CREAT|syscall.O_EXCL|syscall.O_CLOEXEC|syscall.O_NOFOLLOW,
		installedReplayClaimMode,
	)
	if err != nil {
		if errors.Is(err, syscall.EEXIST) {
			return installedReplayRejectedError{}
		}
		return fmt.Errorf("installed replay claim creation failed")
	}
	committed := false
	defer func() {
		if !committed {
			_ = syscall.Fsync(fd)
			_ = syscall.Fsync(state.fd)
		}
		_ = syscall.Close(fd)
	}()
	if err := syscall.Fchmod(fd, installedReplayClaimMode); err != nil {
		return fmt.Errorf("installed replay claim metadata failed")
	}
	var created syscall.Stat_t
	if err := syscall.Fstat(fd, &created); err != nil {
		return fmt.Errorf("installed replay claim metadata failed")
	}
	// A newly created file has size zero until its immutable canonical body is
	// written, so only its type, link count, mode, and ownership are checked.
	createdIdentity := identityFromStat(created)
	if createdIdentity.Mode&syscall.S_IFMT != syscall.S_IFREG ||
		createdIdentity.Links != 1 ||
		createdIdentity.Mode&0o7777 != installedReplayClaimMode ||
		createdIdentity.UID != paths.AuthorityUID ||
		createdIdentity.GID != paths.AuthorityGID ||
		createdIdentity.Size != 0 {
		return fmt.Errorf("installed replay claim metadata invalid")
	}
	for offset := 0; offset < len(expected.Canonical); {
		count, writeErr := syscall.Pwrite(fd, expected.Canonical[offset:], int64(offset))
		if writeErr != nil || count < 1 || count > len(expected.Canonical)-offset {
			return fmt.Errorf("installed replay claim write failed")
		}
		offset += count
	}
	if err := syscall.Fsync(fd); err != nil {
		return fmt.Errorf("installed replay claim file sync failed")
	}
	var after syscall.Stat_t
	if err := syscall.Fstat(fd, &after); err != nil ||
		after.Size != int64(len(expected.Canonical)) ||
		after.Dev != created.Dev ||
		after.Ino != created.Ino ||
		after.Nlink != 1 {
		return fmt.Errorf("installed replay claim changed")
	}
	readback := make([]byte, len(expected.Canonical))
	for offset := 0; offset < len(readback); {
		count, readErr := syscall.Pread(fd, readback[offset:], int64(offset))
		if readErr != nil || count < 1 || count > len(readback)-offset {
			zero(readback)
			return fmt.Errorf("installed replay claim readback failed")
		}
		offset += count
	}
	if !bytes.Equal(readback, expected.Canonical) {
		zero(readback)
		return fmt.Errorf("installed replay claim readback mismatch")
	}
	zero(readback)
	if err := syscall.Fsync(state.fd); err != nil {
		return fmt.Errorf("installed replay state sync failed")
	}
	if err := state.validatePath(); err != nil {
		return err
	}
	claims, err = state.claims()
	if err != nil {
		releaseInstalledReplayClaims(claims)
		return err
	}
	found := false
	for _, persisted := range claims {
		if sameInstalledReplayClaim(persisted, expected) {
			found = true
		}
	}
	releaseInstalledReplayClaims(claims)
	if !found {
		return fmt.Errorf("installed replay claim persistence failed")
	}
	committed = true
	return nil
}

func lockInstalledRequestReplay(
	paths installedRuntimePaths,
	request *quarantinedRequest,
	rootPolicyDigest string,
	verification *installedAuthorityVerification,
	stateGeneration stableIdentity,
) (*lockedInstalledReplayAdoption, error) {
	expected, err := replayClaimForAuthenticatedRequest(
		paths,
		request,
		rootPolicyDigest,
		verification,
		stateGeneration,
	)
	if err != nil {
		return nil, err
	}
	state, err := openInstalledReplayState(paths)
	if err != nil {
		expected.release()
		return nil, err
	}
	adoption := &lockedInstalledReplayAdoption{
		state:    state,
		expected: expected,
	}
	if !sameInstalledDirectoryObject(state.identity, stateGeneration) {
		adoption.close()
		return nil, fmt.Errorf("installed replay state generation mismatch")
	}
	if err := adoption.validateExact(); err != nil {
		adoption.close()
		return nil, err
	}
	return adoption, nil
}

func (adoption *lockedInstalledReplayAdoption) validateExact() error {
	if adoption == nil ||
		adoption.state == nil ||
		adoption.state.fd < 0 ||
		adoption.expected == nil {
		return fmt.Errorf("installed replay adoption unavailable")
	}
	if !sameInstalledDirectoryObject(
		adoption.state.identity,
		adoption.expected.StateGeneration,
	) {
		return fmt.Errorf("installed replay state generation mismatch")
	}
	claims, err := adoption.state.claims()
	if err != nil {
		releaseInstalledReplayClaims(claims)
		return err
	}
	defer releaseInstalledReplayClaims(claims)
	for _, existing := range claims {
		if sameInstalledReplayClaim(existing, adoption.expected) {
			return nil
		}
		if existing.RequestID == adoption.expected.RequestID ||
			existing.Nonce == adoption.expected.Nonce {
			return fmt.Errorf("installed replay claim binding mismatch")
		}
	}
	return fmt.Errorf("installed replay claim missing")
}

func (adoption *lockedInstalledReplayAdoption) snapshotStateGeneration() (
	stableIdentity,
	error,
) {
	if err := adoption.validateExact(); err != nil {
		return stableIdentity{}, err
	}
	return adoption.state.identity, nil
}

// withLockedInstalledRequestReplay is the only durable replay-adoption
// boundary. The callback runs while the exact claim and its state-directory
// descriptor remain under one flock; the exact claim is checked both before
// and after the callback. The adoption capability is invalidated on return.
func withLockedInstalledRequestReplay(
	paths installedRuntimePaths,
	request *quarantinedRequest,
	rootPolicyDigest string,
	verification *installedAuthorityVerification,
	stateGeneration stableIdentity,
	callback func(*lockedInstalledReplayAdoption) error,
) error {
	if callback == nil {
		return fmt.Errorf("installed replay adoption callback invalid")
	}
	adoption, err := lockInstalledRequestReplay(
		paths,
		request,
		rootPolicyDigest,
		verification,
		stateGeneration,
	)
	if err != nil {
		return err
	}
	defer adoption.close()
	callbackErr := callback(adoption)
	if err := adoption.validateExact(); err != nil {
		return err
	}
	return callbackErr
}

// adoptInstalledRequestReplay performs a point-in-time check only. It must not
// be used as a durable commit capability for a later response or release
// effect; such work belongs inside withLockedInstalledRequestReplay.
func adoptInstalledRequestReplay(
	paths installedRuntimePaths,
	request *quarantinedRequest,
	rootPolicyDigest string,
	verification *installedAuthorityVerification,
	stateGeneration stableIdentity,
) error {
	return withLockedInstalledRequestReplay(
		paths,
		request,
		rootPolicyDigest,
		verification,
		stateGeneration,
		func(*lockedInstalledReplayAdoption) error {
			return nil
		},
	)
}
