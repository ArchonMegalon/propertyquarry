//go:build linux

package releasecontrol

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

func newInstalledReplayTestPaths(t *testing.T) installedRuntimePaths {
	t.Helper()
	root := t.TempDir()
	uid := uint32(os.Geteuid())
	gid := uint32(os.Getegid())
	paths := installedRuntimePaths{
		Root:             root,
		StateRoot:        "/state",
		PackageUID:       uid,
		PackageGID:       gid,
		PrivateConfigGID: gid,
		AuthorityUID:     uid,
		AuthorityGID:     gid,
		SocketUID:        uid,
		SocketGID:        gid,
		CallerUID:        uid + 1,
		CallerGID:        gid,
	}
	if err := os.Mkdir(filepath.Join(root, "state"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(filepath.Join(root, "state"), 0o700); err != nil {
		t.Fatal(err)
	}
	return paths
}

func newInstalledReplayTestRequest(t *testing.T) *quarantinedRequest {
	t.Helper()
	request, policy, publicKey, keyID := authenticatedRequestFixture(t)
	if err := authenticateQuarantinedRequest(
		request,
		policy,
		publicKey,
		keyID,
		time.Unix(1050, 0),
	); err != nil {
		request.release()
		t.Fatal(err)
	}
	t.Cleanup(request.release)
	return request
}

type installedReplayTestBinding struct {
	rootPolicyDigest string
	verification     *installedAuthorityVerification
	stateGeneration  stableIdentity
}

func newInstalledReplayTestBinding(
	t *testing.T,
	paths installedRuntimePaths,
) installedReplayTestBinding {
	t.Helper()
	rootPolicyDigest := sha256Digest([]byte("installed-replay-test-root-policy"))
	roles := make(map[string]installedRole, len(installedRoleContracts))
	for index, contract := range installedRoleContracts {
		digest := sha256Digest([]byte("installed-replay-test-role:" + contract.Role))
		if contract.Role == "root-policy" {
			digest = rootPolicyDigest
		}
		roles[contract.Role] = installedRole{
			Contract: contract,
			Digest:   digest,
			Size:     int64(index + 1),
			UID:      paths.PackageUID,
			GID:      paths.PackageGID,
		}
	}
	verification := &installedAuthorityVerification{
		AuthenticationDigest: sha256Digest([]byte("installed-replay-test-authentication")),
		PayloadTreeDigest:    sha256Digest([]byte("installed-replay-test-payload-tree")),
		AuthorityKeyID:       sha256Digest([]byte("installed-replay-test-authority-key")),
		ManifestDigest:       sha256Digest([]byte("installed-replay-test-manifest")),
		NativeBuildDigest:    sha256Digest([]byte("installed-replay-test-native-build")),
		Roles:                roles,
	}
	stateGeneration, err := validateInstalledAuthorityState(paths)
	if err != nil {
		t.Fatal(err)
	}
	return installedReplayTestBinding{
		rootPolicyDigest: rootPolicyDigest,
		verification:     verification,
		stateGeneration:  stateGeneration,
	}
}

func cloneInstalledAuthorityVerification(
	verification *installedAuthorityVerification,
) *installedAuthorityVerification {
	if verification == nil {
		return nil
	}
	cloned := *verification
	cloned.Roles = make(map[string]installedRole, len(verification.Roles))
	for name, role := range verification.Roles {
		cloned.Roles[name] = role
	}
	return &cloned
}

func cloneInstalledReplayTestBinding(
	binding installedReplayTestBinding,
) installedReplayTestBinding {
	binding.verification = cloneInstalledAuthorityVerification(
		binding.verification,
	)
	return binding
}

func replayClaimForInstalledReplayTest(
	paths installedRuntimePaths,
	request *quarantinedRequest,
	binding installedReplayTestBinding,
) (*installedReplayClaim, error) {
	return replayClaimForAuthenticatedRequest(
		paths,
		request,
		binding.rootPolicyDigest,
		binding.verification,
		binding.stateGeneration,
	)
}

func claimInstalledReplayTest(
	paths installedRuntimePaths,
	request *quarantinedRequest,
	binding installedReplayTestBinding,
) error {
	return claimInstalledRequestReplay(
		paths,
		request,
		binding.rootPolicyDigest,
		binding.verification,
		binding.stateGeneration,
	)
}

func adoptInstalledReplayTest(
	paths installedRuntimePaths,
	request *quarantinedRequest,
	binding installedReplayTestBinding,
) error {
	return adoptInstalledRequestReplay(
		paths,
		request,
		binding.rootPolicyDigest,
		binding.verification,
		binding.stateGeneration,
	)
}

func newInstalledReplayTestRequestWithIdentifiers(
	t *testing.T,
	requestID string,
	nonce string,
) *quarantinedRequest {
	t.Helper()
	base, policy, publicKey, keyID := authenticatedRequestFixture(t)
	defer base.release()
	value, err := decodeStrictJSON(base.rawBody)
	if err != nil {
		t.Fatal(err)
	}
	outer := value.(map[string]any)
	envelope := outer["envelope"].(map[string]any)
	envelope["request_id"] = requestID
	envelope["nonce"] = nonce
	canonicalEnvelope, err := canonicalJSON(envelope)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(canonicalEnvelope)
	outer["envelope_digest"] = sha256Digest(canonicalEnvelope)
	outer["request_signature"] = "unsigned-replay-test"
	unsignedRaw, err := canonicalJSON(outer)
	if err != nil {
		t.Fatal(err)
	}
	unsigned, err := parseQuarantinedRequest(unsignedRaw)
	zero(unsignedRaw)
	if err != nil {
		t.Fatal(err)
	}
	message, err := requestSignatureMessage(
		unsigned.signaturePayload,
		unsigned.canonicalEnvelope,
	)
	unsigned.release()
	if err != nil {
		t.Fatal(err)
	}
	seed := bytes.Repeat([]byte{0x41}, ed25519.SeedSize)
	privateKey := ed25519.NewKeyFromSeed(seed)
	zero(seed)
	signature := ed25519.Sign(privateKey, message)
	zero(privateKey)
	zero(message)
	outer["request_signature"] = requestSignaturePrefix + "/" + keyID + "/" +
		base64.RawURLEncoding.EncodeToString(signature)
	zero(signature)
	raw, err := canonicalJSON(outer)
	if err != nil {
		t.Fatal(err)
	}
	request, err := parseQuarantinedRequest(raw)
	zero(raw)
	if err != nil {
		t.Fatal(err)
	}
	if err := authenticateQuarantinedRequest(
		request,
		policy,
		publicKey,
		keyID,
		time.Unix(1050, 0),
	); err != nil {
		request.release()
		t.Fatal(err)
	}
	t.Cleanup(request.release)
	return request
}

func installedReplayStateDirectory(paths installedRuntimePaths) string {
	return filepath.Join(paths.Root, strings.TrimPrefix(paths.StateRoot, "/"))
}

func writeInstalledReplayTestClaim(
	t *testing.T,
	paths installedRuntimePaths,
	claim *installedReplayClaim,
	name string,
	body []byte,
	mode os.FileMode,
) string {
	t.Helper()
	target := filepath.Join(installedReplayStateDirectory(paths), name)
	if err := os.WriteFile(target, body, mode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(target, mode); err != nil {
		t.Fatal(err)
	}
	return target
}

func TestInstalledReplayClaimIsCanonicalAndBindsEveryDimension(t *testing.T) {
	paths := newInstalledReplayTestPaths(t)
	request := newInstalledReplayTestRequest(t)
	binding := newInstalledReplayTestBinding(t, paths)
	expected, err := replayClaimForInstalledReplayTest(paths, request, binding)
	if err != nil {
		t.Fatal(err)
	}
	defer expected.release()
	if expected.RequestKeyID != request.authenticatedKeyID ||
		expected.RequestID != request.envelope.RequestID ||
		expected.Nonce != request.envelope.Nonce ||
		expected.RawRequestDigest != request.rawBodyDigest ||
		expected.CanonicalEnvelopeDigest != request.canonicalEnvelopeDigest ||
		expected.RootPolicyDigest != binding.rootPolicyDigest ||
		expected.Digest != sha256Digest(expected.Canonical) ||
		!validInstalledReplayClaimName(expected.Name) {
		t.Fatalf("replay claim binding changed: %#v", expected)
	}
	parsed, err := parseInstalledReplayClaim(expected.Canonical, expected.Name)
	if err != nil {
		t.Fatal(err)
	}
	defer parsed.release()
	if !sameInstalledReplayClaim(expected, parsed) {
		t.Fatal("canonical replay claim did not round-trip exactly")
	}
	if err := claimInstalledReplayTest(paths, request, binding); err != nil {
		t.Fatal(err)
	}
	if err := adoptInstalledReplayTest(paths, request, binding); err != nil {
		t.Fatalf("controller could not adopt supervisor claim: %v", err)
	}
	if err := adoptInstalledReplayTest(paths, request, binding); err != nil {
		t.Fatalf("controller adoption consumed the claim: %v", err)
	}

	mutations := map[string]func(*quarantinedRequest){
		"request-key-id": func(value *quarantinedRequest) {
			value.authenticatedKeyID = "sha256:" + strings.Repeat("1", 64)
		},
		"request-id": func(value *quarantinedRequest) {
			value.envelope.RequestID = "different-request-id"
		},
		"nonce": func(value *quarantinedRequest) {
			value.envelope.Nonce = "different-nonce"
		},
		"canonical-envelope-digest": func(value *quarantinedRequest) {
			value.canonicalEnvelopeDigest = "sha256:" + strings.Repeat("2", 64)
		},
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			changed := *request
			mutate(&changed)
			if err := adoptInstalledReplayTest(paths, &changed, binding); err == nil {
				t.Fatal("controller adopted a mismatched supervisor claim")
			}
		})
	}
}

func TestInstalledAuthorityGenerationBindsEverySameAuthorityField(t *testing.T) {
	paths := newInstalledReplayTestPaths(t)
	binding := newInstalledReplayTestBinding(t, paths)
	baselineValue, baselineCanonical, baselineDigest, err :=
		canonicalInstalledAuthorityGeneration(binding.verification)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(baselineCanonical)
	parsedCanonical, parsedDigest, err := parseInstalledAuthorityGeneration(
		baselineValue,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(parsedCanonical)
	if baselineDigest != parsedDigest ||
		!bytes.Equal(baselineCanonical, parsedCanonical) {
		t.Fatal("authority generation was not canonical and deterministic")
	}

	roleName := "controller-executable"
	mutations := map[string]func(*installedAuthorityVerification){
		"authentication-digest": func(value *installedAuthorityVerification) {
			value.AuthenticationDigest = sha256Digest([]byte("changed-authentication"))
		},
		"payload-tree-digest": func(value *installedAuthorityVerification) {
			value.PayloadTreeDigest = sha256Digest([]byte("changed-payload-tree"))
		},
		"authority-key-id": func(value *installedAuthorityVerification) {
			value.AuthorityKeyID = sha256Digest([]byte("changed-authority-key"))
		},
		"manifest-digest": func(value *installedAuthorityVerification) {
			value.ManifestDigest = sha256Digest([]byte("changed-manifest"))
		},
		"native-build-digest": func(value *installedAuthorityVerification) {
			value.NativeBuildDigest = sha256Digest([]byte("changed-native-build"))
		},
		"role-name": func(value *installedAuthorityVerification) {
			role := value.Roles[roleName]
			delete(value.Roles, roleName)
			role.Contract.Role = "controller-executable-generation"
			value.Roles[role.Contract.Role] = role
		},
		"role-path": func(value *installedAuthorityVerification) {
			role := value.Roles[roleName]
			role.Contract.Path = "/changed/controller-executable"
			value.Roles[roleName] = role
		},
		"role-mode": func(value *installedAuthorityVerification) {
			role := value.Roles[roleName]
			role.Contract.Mode ^= 0o001
			value.Roles[roleName] = role
		},
		"role-private": func(value *installedAuthorityVerification) {
			role := value.Roles[roleName]
			role.Contract.Private = !role.Contract.Private
			value.Roles[roleName] = role
		},
		"role-digest": func(value *installedAuthorityVerification) {
			role := value.Roles[roleName]
			role.Digest = sha256Digest([]byte("changed-role-digest"))
			value.Roles[roleName] = role
		},
		"role-size": func(value *installedAuthorityVerification) {
			role := value.Roles[roleName]
			role.Size++
			value.Roles[roleName] = role
		},
		"role-uid": func(value *installedAuthorityVerification) {
			role := value.Roles[roleName]
			role.UID++
			value.Roles[roleName] = role
		},
		"role-gid": func(value *installedAuthorityVerification) {
			role := value.Roles[roleName]
			role.GID++
			value.Roles[roleName] = role
		},
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			changed := cloneInstalledAuthorityVerification(binding.verification)
			mutate(changed)
			if sameInstalledAuthority(binding.verification, changed) {
				t.Fatal("sameInstalledAuthority ignored a generation field")
			}
			_, canonical, digest, err :=
				canonicalInstalledAuthorityGeneration(changed)
			if err != nil {
				t.Fatal(err)
			}
			defer zero(canonical)
			if digest == baselineDigest ||
				bytes.Equal(canonical, baselineCanonical) {
				t.Fatal("canonical authority generation ignored a field")
			}
		})
	}
	t.Run("role-count", func(t *testing.T) {
		changed := cloneInstalledAuthorityVerification(binding.verification)
		delete(changed.Roles, roleName)
		if sameInstalledAuthority(binding.verification, changed) {
			t.Fatal("sameInstalledAuthority ignored role count")
		}
		if _, _, _, err := canonicalInstalledAuthorityGeneration(changed); err == nil {
			t.Fatal("canonical authority generation accepted a missing role")
		}
	})
}

func TestInstalledStateGenerationBindsEverySameDirectoryObjectField(t *testing.T) {
	paths := newInstalledReplayTestPaths(t)
	request := newInstalledReplayTestRequest(t)
	binding := newInstalledReplayTestBinding(t, paths)
	baseline, err := replayClaimForInstalledReplayTest(paths, request, binding)
	if err != nil {
		t.Fatal(err)
	}
	defer baseline.release()
	if err := claimInstalledReplayTest(paths, request, binding); err != nil {
		t.Fatal(err)
	}

	mutations := []struct {
		name   string
		mutate func(*installedRuntimePaths, *installedReplayTestBinding)
	}{
		{
			name: "device",
			mutate: func(
				_ *installedRuntimePaths,
				value *installedReplayTestBinding,
			) {
				value.stateGeneration.Device++
			},
		},
		{
			name: "inode",
			mutate: func(
				_ *installedRuntimePaths,
				value *installedReplayTestBinding,
			) {
				value.stateGeneration.Inode++
			},
		},
		{
			name: "rdevice",
			mutate: func(
				_ *installedRuntimePaths,
				value *installedReplayTestBinding,
			) {
				value.stateGeneration.Rdevice++
			},
		},
		{
			name: "mode",
			mutate: func(
				_ *installedRuntimePaths,
				value *installedReplayTestBinding,
			) {
				// Preserve the directory type and permission bits while
				// changing the complete mode value compared by
				// sameInstalledDirectoryObject.
				value.stateGeneration.Mode ^= 1 << 31
			},
		},
		{
			name: "links",
			mutate: func(
				_ *installedRuntimePaths,
				value *installedReplayTestBinding,
			) {
				value.stateGeneration.Links++
			},
		},
		{
			name: "uid",
			mutate: func(
				paths *installedRuntimePaths,
				value *installedReplayTestBinding,
			) {
				value.stateGeneration.UID++
				paths.AuthorityUID = value.stateGeneration.UID
				paths.SocketUID = value.stateGeneration.UID
			},
		},
		{
			name: "gid",
			mutate: func(
				paths *installedRuntimePaths,
				value *installedReplayTestBinding,
			) {
				value.stateGeneration.GID++
				paths.AuthorityGID = value.stateGeneration.GID
				paths.SocketGID = value.stateGeneration.GID
				paths.PrivateConfigGID = value.stateGeneration.GID
				paths.CallerGID = value.stateGeneration.GID
			},
		},
	}
	for _, mutation := range mutations {
		t.Run(mutation.name, func(t *testing.T) {
			changedPaths := paths
			changed := cloneInstalledReplayTestBinding(binding)
			mutation.mutate(&changedPaths, &changed)
			if sameInstalledDirectoryObject(
				binding.stateGeneration,
				changed.stateGeneration,
			) {
				t.Fatal("sameInstalledDirectoryObject ignored a state field")
			}
			changedClaim, err := replayClaimForInstalledReplayTest(
				changedPaths,
				request,
				changed,
			)
			if err != nil {
				t.Fatal(err)
			}
			defer changedClaim.release()
			if bytes.Equal(baseline.Canonical, changedClaim.Canonical) ||
				baseline.Digest == changedClaim.Digest {
				t.Fatal("replay claim ignored a state generation field")
			}
			if err := adoptInstalledReplayTest(
				changedPaths,
				request,
				changed,
			); err == nil {
				t.Fatal("controller adopted a different state generation")
			}
		})
	}
}

func TestLockedInstalledReplayAdoptionRejectsPostCheckDeletionAndReplacement(
	t *testing.T,
) {
	mutations := []struct {
		name    string
		replace bool
	}{
		{name: "deleted"},
		{name: "replaced", replace: true},
	}
	for _, mutation := range mutations {
		t.Run(mutation.name, func(t *testing.T) {
			paths := newInstalledReplayTestPaths(t)
			request := newInstalledReplayTestRequest(t)
			binding := newInstalledReplayTestBinding(t, paths)
			if err := claimInstalledReplayTest(paths, request, binding); err != nil {
				t.Fatal(err)
			}
			expected, err := replayClaimForInstalledReplayTest(
				paths,
				request,
				binding,
			)
			if err != nil {
				t.Fatal(err)
			}
			defer expected.release()

			// This is the old point-in-time boundary: it succeeds before
			// the exact claim is removed or replaced.
			if err := adoptInstalledReplayTest(paths, request, binding); err != nil {
				t.Fatal(err)
			}
			if err := os.Remove(filepath.Join(
				installedReplayStateDirectory(paths),
				expected.Name,
			)); err != nil {
				t.Fatal(err)
			}
			if mutation.replace {
				changed := *request
				changed.rawBody = append(
					append([]byte(nil), request.rawBody...),
					'\n',
				)
				defer zero(changed.rawBody)
				changed.rawBodyDigest = sha256Digest(changed.rawBody)
				replacement, err := replayClaimForInstalledReplayTest(
					paths,
					&changed,
					binding,
				)
				if err != nil {
					t.Fatal(err)
				}
				defer replacement.release()
				if sameInstalledReplayClaim(expected, replacement) {
					t.Fatal("replacement claim did not change")
				}
				writeInstalledReplayTestClaim(
					t,
					paths,
					replacement,
					replacement.Name,
					replacement.Canonical,
					installedReplayClaimMode,
				)
			}

			callbackInvoked := false
			err = withLockedInstalledRequestReplay(
				paths,
				request,
				binding.rootPolicyDigest,
				binding.verification,
				binding.stateGeneration,
				func(*lockedInstalledReplayAdoption) error {
					callbackInvoked = true
					return nil
				},
			)
			if err == nil {
				t.Fatal("locked adoption accepted a changed exact claim")
			}
			if callbackInvoked {
				t.Fatal("callback ran without an exact locked claim")
			}
		})
	}
}

func TestLockedInstalledReplayAdoptionPostchecksAndInvalidatesCapability(
	t *testing.T,
) {
	paths := newInstalledReplayTestPaths(t)
	request := newInstalledReplayTestRequest(t)
	binding := newInstalledReplayTestBinding(t, paths)
	if err := claimInstalledReplayTest(paths, request, binding); err != nil {
		t.Fatal(err)
	}
	expected, err := replayClaimForInstalledReplayTest(paths, request, binding)
	if err != nil {
		t.Fatal(err)
	}
	defer expected.release()

	var escaped *lockedInstalledReplayAdoption
	callbackInvoked := false
	err = withLockedInstalledRequestReplay(
		paths,
		request,
		binding.rootPolicyDigest,
		binding.verification,
		binding.stateGeneration,
		func(adoption *lockedInstalledReplayAdoption) error {
			callbackInvoked = true
			escaped = adoption
			generation, err := adoption.snapshotStateGeneration()
			if err != nil {
				return err
			}
			if !sameInstalledDirectoryObject(
				generation,
				binding.stateGeneration,
			) {
				return errors.New("locked state generation changed")
			}
			return os.Remove(filepath.Join(
				installedReplayStateDirectory(paths),
				expected.Name,
			))
		},
	)
	if err == nil {
		t.Fatal("locked adoption missed claim deletion during callback")
	}
	if !callbackInvoked || escaped == nil {
		t.Fatal("locked adoption callback did not run")
	}
	if err := escaped.validateExact(); err == nil {
		t.Fatal("escaped adoption remained usable after callback")
	}
	if _, err := escaped.snapshotStateGeneration(); err == nil {
		t.Fatal("escaped adoption retained a state descriptor")
	}
}

func TestInstalledReplayRejectsCrossGenerationAdoption(t *testing.T) {
	paths := newInstalledReplayTestPaths(t)
	request := newInstalledReplayTestRequest(t)
	binding := newInstalledReplayTestBinding(t, paths)
	if err := claimInstalledReplayTest(paths, request, binding); err != nil {
		t.Fatal(err)
	}

	t.Run("raw-request", func(t *testing.T) {
		changed := *request
		changed.rawBody = append(append([]byte(nil), request.rawBody...), '\n')
		defer zero(changed.rawBody)
		changed.rawBodyDigest = sha256Digest(changed.rawBody)
		if err := adoptInstalledReplayTest(paths, &changed, binding); err == nil {
			t.Fatal("claim crossed an exact raw-request generation")
		}
	})
	t.Run("root-policy", func(t *testing.T) {
		changed := cloneInstalledReplayTestBinding(binding)
		changed.rootPolicyDigest = sha256Digest([]byte("changed-root-policy"))
		role := changed.verification.Roles["root-policy"]
		role.Digest = changed.rootPolicyDigest
		changed.verification.Roles["root-policy"] = role
		if err := adoptInstalledReplayTest(paths, request, changed); err == nil {
			t.Fatal("claim crossed an authenticated root-policy generation")
		}
	})
	t.Run("authority-top-level", func(t *testing.T) {
		changed := cloneInstalledReplayTestBinding(binding)
		changed.verification.AuthenticationDigest =
			sha256Digest([]byte("changed-installed-authentication"))
		if err := adoptInstalledReplayTest(paths, request, changed); err == nil {
			t.Fatal("claim crossed an installed authority generation")
		}
	})
	t.Run("authority-role", func(t *testing.T) {
		changed := cloneInstalledReplayTestBinding(binding)
		role := changed.verification.Roles["controller-executable"]
		role.Digest = sha256Digest([]byte("changed-controller-role"))
		changed.verification.Roles["controller-executable"] = role
		if err := adoptInstalledReplayTest(paths, request, changed); err == nil {
			t.Fatal("claim crossed an installed authority role generation")
		}
	})
	t.Run("state-object", func(t *testing.T) {
		changed := cloneInstalledReplayTestBinding(binding)
		changed.stateGeneration.Inode++
		if err := adoptInstalledReplayTest(paths, request, changed); err == nil {
			t.Fatal("claim crossed an authority state object")
		}
	})
}

func TestInstalledReplayRejectsCopiedClaimAcrossStateObjects(t *testing.T) {
	paths := newInstalledReplayTestPaths(t)
	request := newInstalledReplayTestRequest(t)
	binding := newInstalledReplayTestBinding(t, paths)
	claim, err := replayClaimForInstalledReplayTest(paths, request, binding)
	if err != nil {
		t.Fatal(err)
	}
	defer claim.release()
	if err := claimInstalledReplayTest(paths, request, binding); err != nil {
		t.Fatal(err)
	}
	statePath := installedReplayStateDirectory(paths)
	oldStatePath := filepath.Join(paths.Root, "old-state")
	if err := os.Rename(statePath, oldStatePath); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(statePath, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(statePath, 0o700); err != nil {
		t.Fatal(err)
	}
	writeInstalledReplayTestClaim(
		t,
		paths,
		claim,
		claim.Name,
		claim.Canonical,
		installedReplayClaimMode,
	)
	newBinding := cloneInstalledReplayTestBinding(binding)
	newBinding.stateGeneration, err = validateInstalledAuthorityState(paths)
	if err != nil {
		t.Fatal(err)
	}
	if sameInstalledDirectoryObject(
		binding.stateGeneration,
		newBinding.stateGeneration,
	) {
		t.Fatal("state replacement retained the same directory object")
	}
	if err := adoptInstalledReplayTest(paths, request, newBinding); err == nil {
		t.Fatal("copied claim crossed authority state directory objects")
	}
}

func TestInstalledReplayRejectsLegacyAndInternallyTamperedClaims(t *testing.T) {
	t.Run("legacy-v2-name", func(t *testing.T) {
		paths := newInstalledReplayTestPaths(t)
		body, err := canonicalJSON(map[string]any{
			"schema":                    "propertyquarry.release-control.replay-claim.v2",
			"request_key_id":            "sha256:" + strings.Repeat("1", 64),
			"request_id":                "legacy-request",
			"nonce":                     "legacy-nonce",
			"canonical_envelope_digest": "sha256:" + strings.Repeat("2", 64),
		})
		if err != nil {
			t.Fatal(err)
		}
		defer zero(body)
		name := "claim-v2-" +
			strings.TrimPrefix(sha256Digest(body), "sha256:") +
			".json"
		writeInstalledReplayTestClaim(
			t,
			paths,
			nil,
			name,
			body,
			installedReplayClaimMode,
		)
		if _, err := validateInstalledAuthorityState(paths); err == nil {
			t.Fatal("legacy replay claim name was accepted")
		}
	})
	t.Run("legacy-v2-body-renamed", func(t *testing.T) {
		paths := newInstalledReplayTestPaths(t)
		body, err := canonicalJSON(map[string]any{
			"schema":                    "propertyquarry.release-control.replay-claim.v2",
			"request_key_id":            "sha256:" + strings.Repeat("1", 64),
			"request_id":                "legacy-request",
			"nonce":                     "legacy-nonce",
			"canonical_envelope_digest": "sha256:" + strings.Repeat("2", 64),
		})
		if err != nil {
			t.Fatal(err)
		}
		defer zero(body)
		name := installedReplayClaimPrefix +
			strings.TrimPrefix(sha256Digest(body), "sha256:") +
			installedReplayClaimSuffix
		writeInstalledReplayTestClaim(
			t,
			paths,
			nil,
			name,
			body,
			installedReplayClaimMode,
		)
		if _, err := validateInstalledAuthorityState(paths); err == nil {
			t.Fatal("renamed legacy replay claim body was accepted")
		}
	})

	tamper := map[string]func(map[string]any){
		"authority-role-with-stale-digest": func(outer map[string]any) {
			authority := outer["authority_generation"].(map[string]any)
			roles := authority["roles"].(map[string]any)
			role := roles["controller-executable"].(map[string]any)
			role["digest"] = sha256Digest([]byte("tampered-role"))
		},
		"authority-generation-digest": func(outer map[string]any) {
			outer["authority_generation_digest"] =
				sha256Digest([]byte("tampered-authority-generation"))
		},
		"state-object-with-stale-digest": func(outer map[string]any) {
			state := outer["state_generation"].(map[string]any)
			inode, err := strconv.ParseUint(state["inode"].(string), 10, 64)
			if err != nil {
				panic(err)
			}
			state["inode"] = strconv.FormatUint(inode+1, 10)
		},
		"state-generation-digest": func(outer map[string]any) {
			outer["state_generation_digest"] =
				sha256Digest([]byte("tampered-state-generation"))
		},
	}
	for name, mutate := range tamper {
		t.Run(name, func(t *testing.T) {
			paths := newInstalledReplayTestPaths(t)
			request := newInstalledReplayTestRequest(t)
			binding := newInstalledReplayTestBinding(t, paths)
			claim, err := replayClaimForInstalledReplayTest(
				paths,
				request,
				binding,
			)
			if err != nil {
				t.Fatal(err)
			}
			defer claim.release()
			value, err := decodeStrictJSON(claim.Canonical)
			if err != nil {
				t.Fatal(err)
			}
			outer := value.(map[string]any)
			mutate(outer)
			body, err := canonicalJSON(outer)
			if err != nil {
				t.Fatal(err)
			}
			defer zero(body)
			recordName := installedReplayClaimPrefix +
				strings.TrimPrefix(sha256Digest(body), "sha256:") +
				installedReplayClaimSuffix
			writeInstalledReplayTestClaim(
				t,
				paths,
				nil,
				recordName,
				body,
				installedReplayClaimMode,
			)
			if _, err := validateInstalledAuthorityState(paths); err == nil {
				t.Fatal("internally inconsistent replay claim was accepted")
			}
		})
	}
}

func TestInstalledReplayAdmissionIsExclusiveUnderConcurrentDuplicate(t *testing.T) {
	paths := newInstalledReplayTestPaths(t)
	binding := newInstalledReplayTestBinding(t, paths)
	const contenders = 24
	requests := make([]*quarantinedRequest, 0, contenders)
	for index := 0; index < contenders; index++ {
		requests = append(requests, newInstalledReplayTestRequest(t))
	}
	start := make(chan struct{})
	results := make(chan error, contenders)
	var group sync.WaitGroup
	for _, request := range requests {
		group.Add(1)
		go func(request *quarantinedRequest) {
			defer group.Done()
			<-start
			results <- claimInstalledReplayTest(paths, request, binding)
		}(request)
	}
	close(start)
	group.Wait()
	close(results)
	admitted := 0
	rejected := 0
	for err := range results {
		if err == nil {
			admitted++
			continue
		}
		var replay installedReplayRejectedError
		if !errors.As(err, &replay) {
			t.Fatalf("duplicate failed for a non-replay reason: %v", err)
		}
		rejected++
	}
	if admitted != 1 || rejected != contenders-1 {
		t.Fatalf("concurrent admission was not exclusive: admitted=%d rejected=%d", admitted, rejected)
	}
	if _, err := validateInstalledAuthorityState(paths); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(installedReplayStateDirectory(paths))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 {
		t.Fatalf("exclusive claim created %d entries", len(entries))
	}
}

func TestInstalledReplayRejectsRequestIDAndNonceReuseIndependently(t *testing.T) {
	alternates := map[string]func(*testing.T, *quarantinedRequest) *quarantinedRequest{
		"request-id": func(t *testing.T, original *quarantinedRequest) *quarantinedRequest {
			return newInstalledReplayTestRequestWithIdentifiers(
				t,
				original.envelope.RequestID,
				"new-unique-nonce",
			)
		},
		"nonce": func(t *testing.T, original *quarantinedRequest) *quarantinedRequest {
			return newInstalledReplayTestRequestWithIdentifiers(
				t,
				"new-unique-request",
				original.envelope.Nonce,
			)
		},
	}
	for name, alternate := range alternates {
		t.Run(name, func(t *testing.T) {
			paths := newInstalledReplayTestPaths(t)
			binding := newInstalledReplayTestBinding(t, paths)
			request := newInstalledReplayTestRequest(t)
			if err := claimInstalledReplayTest(paths, request, binding); err != nil {
				t.Fatal(err)
			}
			changed := alternate(t, request)
			err := claimInstalledReplayTest(paths, changed, binding)
			var replay installedReplayRejectedError
			if !errors.As(err, &replay) {
				t.Fatalf("reused %s was not rejected as replay: %v", name, err)
			}
		})
	}
}

func TestInstalledReplayCrashAndRestartRemainAtMostOnce(t *testing.T) {
	t.Run("durable-claim", func(t *testing.T) {
		paths := newInstalledReplayTestPaths(t)
		binding := newInstalledReplayTestBinding(t, paths)
		request := newInstalledReplayTestRequest(t)
		if err := claimInstalledReplayTest(paths, request, binding); err != nil {
			t.Fatal(err)
		}
		if _, err := validateInstalledAuthorityState(paths); err != nil {
			t.Fatalf("restart rejected durable state: %v", err)
		}
		var replay installedReplayRejectedError
		if err := claimInstalledReplayTest(paths, request, binding); !errors.As(err, &replay) {
			t.Fatalf("restart readmitted a consumed request: %v", err)
		}
		if err := adoptInstalledReplayTest(paths, request, binding); err != nil {
			t.Fatalf("controller could not adopt durable supervisor claim: %v", err)
		}
	})

	t.Run("crash-after-exclusive-create", func(t *testing.T) {
		paths := newInstalledReplayTestPaths(t)
		binding := newInstalledReplayTestBinding(t, paths)
		request := newInstalledReplayTestRequest(t)
		expected, err := replayClaimForInstalledReplayTest(paths, request, binding)
		if err != nil {
			t.Fatal(err)
		}
		defer expected.release()
		writeInstalledReplayTestClaim(
			t,
			paths,
			expected,
			expected.Name,
			nil,
			installedReplayClaimMode,
		)
		if _, err := validateInstalledAuthorityState(paths); err == nil {
			t.Fatal("partial crash claim was accepted")
		}
		err = claimInstalledReplayTest(paths, request, binding)
		var replay installedReplayRejectedError
		if err == nil || errors.As(err, &replay) {
			t.Fatalf("partial crash claim was overwritten or treated as clean replay: %v", err)
		}
	})
}

func TestInstalledReplayStateRejectsHostileEntries(t *testing.T) {
	tests := map[string]func(
		*testing.T,
		installedRuntimePaths,
		*quarantinedRequest,
		installedReplayTestBinding,
	){
		"state-directory-mode": func(t *testing.T, paths installedRuntimePaths, _ *quarantinedRequest, _ installedReplayTestBinding) {
			if err := os.Chmod(installedReplayStateDirectory(paths), 0o750); err != nil {
				t.Fatal(err)
			}
		},
		"state-directory-symlink": func(t *testing.T, paths installedRuntimePaths, _ *quarantinedRequest, _ installedReplayTestBinding) {
			state := installedReplayStateDirectory(paths)
			if err := os.Remove(state); err != nil {
				t.Fatal(err)
			}
			replacement := filepath.Join(paths.Root, "replacement-state")
			if err := os.Mkdir(replacement, 0o700); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(replacement, state); err != nil {
				t.Fatal(err)
			}
		},
		"unexpected-name": func(t *testing.T, paths installedRuntimePaths, request *quarantinedRequest, binding installedReplayTestBinding) {
			claim, err := replayClaimForInstalledReplayTest(paths, request, binding)
			if err != nil {
				t.Fatal(err)
			}
			defer claim.release()
			writeInstalledReplayTestClaim(t, paths, claim, "unexpected", claim.Canonical, 0o600)
		},
		"symlink": func(t *testing.T, paths installedRuntimePaths, request *quarantinedRequest, binding installedReplayTestBinding) {
			claim, err := replayClaimForInstalledReplayTest(paths, request, binding)
			if err != nil {
				t.Fatal(err)
			}
			defer claim.release()
			if err := os.Symlink(
				"/dev/null",
				filepath.Join(installedReplayStateDirectory(paths), claim.Name),
			); err != nil {
				t.Fatal(err)
			}
		},
		"directory": func(t *testing.T, paths installedRuntimePaths, request *quarantinedRequest, binding installedReplayTestBinding) {
			claim, err := replayClaimForInstalledReplayTest(paths, request, binding)
			if err != nil {
				t.Fatal(err)
			}
			defer claim.release()
			if err := os.Mkdir(
				filepath.Join(installedReplayStateDirectory(paths), claim.Name),
				0o600,
			); err != nil {
				t.Fatal(err)
			}
		},
		"wrong-mode": func(t *testing.T, paths installedRuntimePaths, request *quarantinedRequest, binding installedReplayTestBinding) {
			claim, err := replayClaimForInstalledReplayTest(paths, request, binding)
			if err != nil {
				t.Fatal(err)
			}
			defer claim.release()
			writeInstalledReplayTestClaim(t, paths, claim, claim.Name, claim.Canonical, 0o644)
		},
		"hardlink": func(t *testing.T, paths installedRuntimePaths, request *quarantinedRequest, binding installedReplayTestBinding) {
			if err := claimInstalledReplayTest(paths, request, binding); err != nil {
				t.Fatal(err)
			}
			entries, err := os.ReadDir(installedReplayStateDirectory(paths))
			if err != nil || len(entries) != 1 {
				t.Fatalf("claim fixture unavailable: %v", err)
			}
			source := filepath.Join(installedReplayStateDirectory(paths), entries[0].Name())
			if err := os.Link(source, filepath.Join(installedReplayStateDirectory(paths), "unexpected")); err != nil {
				t.Fatal(err)
			}
		},
		"noncanonical": func(t *testing.T, paths installedRuntimePaths, request *quarantinedRequest, binding installedReplayTestBinding) {
			claim, err := replayClaimForInstalledReplayTest(paths, request, binding)
			if err != nil {
				t.Fatal(err)
			}
			defer claim.release()
			body := append([]byte(" "), claim.Canonical...)
			defer zero(body)
			writeInstalledReplayTestClaim(t, paths, claim, claim.Name, body, 0o600)
		},
		"name-digest-mismatch": func(t *testing.T, paths installedRuntimePaths, request *quarantinedRequest, binding installedReplayTestBinding) {
			claim, err := replayClaimForInstalledReplayTest(paths, request, binding)
			if err != nil {
				t.Fatal(err)
			}
			defer claim.release()
			name := installedReplayClaimPrefix + strings.Repeat("f", 64) + installedReplayClaimSuffix
			if name == claim.Name {
				name = installedReplayClaimPrefix + strings.Repeat("e", 64) + installedReplayClaimSuffix
			}
			writeInstalledReplayTestClaim(t, paths, claim, name, claim.Canonical, 0o600)
		},
		"duplicate-request-id": func(t *testing.T, paths installedRuntimePaths, request *quarantinedRequest, binding installedReplayTestBinding) {
			if err := claimInstalledReplayTest(paths, request, binding); err != nil {
				t.Fatal(err)
			}
			changed := newInstalledReplayTestRequestWithIdentifiers(
				t,
				request.envelope.RequestID,
				"second-nonce",
			)
			claim, err := replayClaimForInstalledReplayTest(paths, changed, binding)
			if err != nil {
				t.Fatal(err)
			}
			defer claim.release()
			writeInstalledReplayTestClaim(t, paths, claim, claim.Name, claim.Canonical, 0o600)
		},
	}
	for name, prepare := range tests {
		t.Run(name, func(t *testing.T) {
			paths := newInstalledReplayTestPaths(t)
			request := newInstalledReplayTestRequest(t)
			binding := newInstalledReplayTestBinding(t, paths)
			prepare(t, paths, request, binding)
			if _, err := validateInstalledAuthorityState(paths); err == nil {
				t.Fatal("hostile replay state was accepted")
			}
		})
	}
}

func TestControllerIndependentlyAdoptsExactSupervisorClaimWithoutConsumingIt(t *testing.T) {
	fixture := newInstalledAuthorityFixture(t)
	raw := signedInstalledFixtureRequest(t, fixture)
	defer zero(raw)
	supervisorRequest, err := parseQuarantinedRequest(raw)
	if err != nil {
		t.Fatal(err)
	}
	defer supervisorRequest.release()
	verification, err := validateInstalledLocalAuthority(Supervisor, fixture.paths)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	authenticated, err := authenticateInstalledRequestBindings(
		fixture.paths,
		verification,
		supervisorRequest,
		now,
	)
	if err != nil {
		t.Fatal(err)
	}
	stateGeneration, err := validateInstalledAuthorityState(fixture.paths)
	if err != nil {
		t.Fatal(err)
	}
	if err := claimInstalledRequestReplay(
		fixture.paths,
		supervisorRequest,
		authenticated.rootPolicyDigest,
		verification,
		stateGeneration,
	); err != nil {
		t.Fatal(err)
	}
	for attempt := 0; attempt < 2; attempt++ {
		var pipeFDs [2]int
		if err := syscall.Pipe2(pipeFDs[:], syscall.O_CLOEXEC); err != nil {
			t.Fatal(err)
		}
		for offset := 0; offset < len(raw); {
			count, writeErr := syscall.Write(pipeFDs[1], raw[offset:])
			if writeErr != nil || count < 1 {
				_ = syscall.Close(pipeFDs[0])
				_ = syscall.Close(pipeFDs[1])
				t.Fatalf("controller pipe write failed: %v", writeErr)
			}
			offset += count
		}
		if err := syscall.Close(pipeFDs[1]); err != nil {
			_ = syscall.Close(pipeFDs[0])
			t.Fatal(err)
		}
		eventID := "local-" + supervisorRequest.rawBodyDigest[len("sha256:"):]
		if err := authenticateControllerRequestWithPaths(
			pipeFDs[0],
			supervisorRequest.envelope.Operation,
			eventID,
			supervisorRequest.rawBodyDigest,
			now,
			fixture.paths,
		); err != nil {
			t.Fatalf("controller adoption attempt %d failed: %v", attempt+1, err)
		}
	}
	entries, err := os.ReadDir(installedReplayStateDirectory(fixture.paths))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 {
		t.Fatalf("controller adoption mutated replay state: %d entries", len(entries))
	}
}
