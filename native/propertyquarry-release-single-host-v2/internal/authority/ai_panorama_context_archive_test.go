//go:build linux && amd64

package authority

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

func aiPanoramaTestContextArchive(
	t *testing.T,
) (string, *aiPanoramaContextArchive) {
	t.Helper()
	root := t.TempDir()
	for _, relative := range []string{
		"var",
		"var/lib",
		"var/lib/propertyquarry-release-single-host-v2",
		"var/lib/propertyquarry-release-single-host-v2/ai-panorama-context-archives",
	} {
		if err := os.Mkdir(filepath.Join(root, relative), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	contextRaw := func(kind string) []byte {
		return []byte(`{"kind":"` + kind + `"}` + "\n")
	}
	compose := contextRaw("compose")
	profile := contextRaw("profile")
	trust := contextRaw("trust")
	keyring := contextRaw("keyring-a")
	signing := &aiPanoramaSigningContext{
		ComposePlanCanonicalRaw:    compose,
		VolumeProfileCanonicalRaw:  profile,
		TrustAssertionCanonicalRaw: trust,
		ComposePlanSHA256:          aiPanoramaRawSHA256(compose),
		VolumeProfileSHA256:        aiPanoramaRawSHA256(profile),
		TrustAssertionSHA256:       aiPanoramaRawSHA256(trust),
	}
	key := &aiPanoramaPurposeKey{
		KeyID: "archive-key-a", Epoch: 7,
		PublicSHA256:  aiPanoramaRawSHA256(bytes.Repeat([]byte{0x77}, 32)),
		KeyringSHA256: aiPanoramaRawSHA256(keyring),
		Raw:           keyring,
	}
	permit := &aiPanoramaSignedPermit{
		RequestID: "0123456789abcdef0123456789abcdef",
		SHA256:    aiPanoramaRawSHA256([]byte("permit-a")),
		KeyID:     key.KeyID, KeyEpoch: key.Epoch,
		KeySHA256: key.PublicSHA256, KeyringSHA256: key.KeyringSHA256,
	}
	archive, err := newAiPanoramaContextArchive(signing, key, permit)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(archive.release)
	t.Cleanup(func() {
		_ = os.Chmod(rooted(root, archive.Path), 0o700)
		_ = os.Chmod(rooted(root, archive.StagePath), 0o700)
	})
	return root, archive
}

func TestAiPanoramaContextArchiveConvergesPartialStageAndSurvivesRotation(t *testing.T) {
	root, archive := aiPanoramaTestContextArchive(t)
	stagePath := rooted(root, archive.StagePath)
	if err := os.Mkdir(stagePath, 0o700); err != nil {
		t.Fatal(err)
	}
	first := archive.Files[0]
	if err := persistAiPanoramaProjectionFile(root, &aiPanoramaProjection{
		Kind: first.Kind,
		Path: archive.StagePath + "/" + filepath.Base(first.Path),
		Mode: 0o400, SHA256: first.SHA256, Raw: first.Raw,
	}); err != nil {
		t.Fatal(err)
	}
	firstInode, err := os.Lstat(
		filepath.Join(stagePath, filepath.Base(first.Path)),
	)
	if err != nil {
		t.Fatal(err)
	}
	observation, err := ensureAiPanoramaContextArchive(root, archive)
	if err != nil {
		t.Fatal(err)
	}
	if observation["permit_sha256"] != archive.PermitSHA256 {
		t.Fatal("archive observation lost permit binding")
	}
	persistedFirst, err := os.Lstat(rooted(root, first.Path))
	if err != nil || !os.SameFile(firstInode, persistedFirst) {
		t.Fatal("valid partial archive leaf was not converged in place")
	}
	rotated := []byte("{\"kind\":\"keyring-b\"}\n")
	if bytes.Equal(rotated, archive.Files[0].Raw) {
		t.Fatal("test rotation fixture is not distinct")
	}
	raw, err := os.ReadFile(rooted(root, archive.Files[0].Path))
	if err != nil || !bytes.Equal(raw, archive.Files[0].Raw) {
		t.Fatal("historical keyring was substituted after rotation")
	}
	second, err := ensureAiPanoramaContextArchive(root, archive)
	if err != nil || !canonicalValuesEqual(observation, second) {
		t.Fatal("published archive is not idempotent")
	}
}

func TestAiPanoramaContextArchiveRejectsUnknownStageResidueWithoutCleanup(t *testing.T) {
	root, archive := aiPanoramaTestContextArchive(t)
	stagePath := rooted(root, archive.StagePath)
	if err := os.Mkdir(stagePath, 0o700); err != nil {
		t.Fatal(err)
	}
	residue := filepath.Join(stagePath, "untrusted")
	if err := os.WriteFile(residue, []byte("do-not-delete"), 0o400); err != nil {
		t.Fatal(err)
	}
	if _, err := ensureAiPanoramaContextArchive(root, archive); err == nil {
		t.Fatal("unknown stage residue was accepted")
	}
	if raw, err := os.ReadFile(residue); err != nil ||
		string(raw) != "do-not-delete" {
		t.Fatal("unknown stage residue was mutated")
	}
	if _, err := os.Lstat(rooted(root, archive.Path)); !os.IsNotExist(err) {
		t.Fatal("archive was published from ambiguous residue")
	}
}

func TestAiPanoramaContextArchiveObservationRejectsSameByteInodeReplacement(t *testing.T) {
	root, archive := aiPanoramaTestContextArchive(t)
	before, err := ensureAiPanoramaContextArchive(root, archive)
	if err != nil {
		t.Fatal(err)
	}
	finalPath := rooted(root, archive.Path)
	target := rooted(root, archive.Files[2].Path)
	if err := os.Chmod(finalPath, 0o700); err != nil {
		t.Fatal(err)
	}
	replacement := filepath.Join(finalPath, ".replacement")
	if err := os.WriteFile(replacement, archive.Files[2].Raw, 0o400); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(replacement, target); err != nil ||
		os.Chmod(finalPath, 0o500) != nil {
		t.Fatal("failed to replace archive leaf")
	}
	after, err := observeAiPanoramaContextArchive(root, archive)
	if err != nil {
		t.Fatal(err)
	}
	if canonicalValuesEqual(before, after) {
		t.Fatal("same-byte inode replacement preserved signed observation")
	}
}
