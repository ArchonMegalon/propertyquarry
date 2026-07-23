//go:build linux && amd64

package authority

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
)

func TestTourV4AuthorizedPermitValid(t *testing.T) {
	if len(tourV4AuthorizedPermits) != 1 {
		t.Fatalf("unexpected permit count: %d", len(tourV4AuthorizedPermits))
	}
	_, manifestSHA, err := tourV4PermitManifest(&tourV4AuthorizedPermits[0])
	if err != nil {
		t.Fatalf("authorized permit invalid: %v", err)
	}
	if !tourV4SHA256Pattern.MatchString(manifestSHA) {
		t.Fatalf("manifest digest invalid: %q", manifestSHA)
	}
}

func TestTourV4DetachedAuthorityNeedsNoInstalledControllerOrCredential(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	read := func(path string) []byte {
		raw, err := os.ReadFile(rooted(fixture.root, path))
		if err != nil {
			t.Fatal(err)
		}
		return raw
	}
	packageAnchor := read(PackageAnchorPath)
	receiptAnchor := read(ReceiptAnchorPath)
	receiptKey := read(ReceiptKeyPath)
	packageKeyID, err := publicKeyID(fixture.packageKey.Public().(ed25519.PublicKey))
	if err != nil {
		t.Fatal(err)
	}
	receiptKeyID, err := publicKeyID(fixture.receiptKey.Public().(ed25519.PublicKey))
	if err != nil {
		t.Fatal(err)
	}
	bootstrap, err := canonicalJSON(map[string]any{
		"created_at_epoch":                 json.Number("1800000000"),
		"package_authority_key_id":         packageKeyID,
		"package_authority_private_sha256": tourV4DetachedCanonicalPrivateDigest,
		"package_authority_public_sha256":  digest(packageAnchor),
		"package_authority_source":         tourV4DetachedCanonicalAuthorityRoot,
		"receipt_authority_key_id":         receiptKeyID,
		"receipt_authority_public_sha256":  digest(receiptAnchor),
		"schema":                           tourV4DetachedBootstrapSchema,
		"version":                          json.Number("2"),
	})
	if err != nil {
		t.Fatal(err)
	}
	bootstrapSignature := ed25519.Sign(
		fixture.packageKey,
		framed(tourV4DetachedBootstrapDomain, bootstrap),
	)
	_, manifestSHA, err := tourV4PermitManifest(&tourV4AuthorizedPermits[0])
	if err != nil {
		t.Fatal(err)
	}
	sourceDigest := digest([]byte("detached-source"))
	operations := make([]any, len(tourV4DetachedOperations))
	for index, operation := range tourV4DetachedOperations {
		operations[index] = operation
	}
	materialization, err := canonicalJSON(map[string]any{
		"accepted_installer_mode":                      "dispatch-tour-v4",
		"allowed_operations":                           operations,
		"artifact_bundle_path":                         tourV4DetachedBundlePath,
		"artifact_manifest_sha256":                     manifestSHA,
		"artifact_public_tree_sha256":                  tourV4AuthorizedPermits[0].PublicTreeSHA256,
		"artifact_slug":                                tourV4AuthorizedPermits[0].Slug,
		"authoritative":                                false,
		"authority_bootstrap_sha256":                   digest(bootstrap),
		"host_install_permitted":                       false,
		"host_machine_id_digest":                       digest([]byte("0123456789abcdef0123456789abcdef")),
		"materialized_at_epoch":                        json.Number("1800000000"),
		"native_build_receipt_sha256":                  digest([]byte("build-receipt")),
		"network_required":                             false,
		"package_authority_key_id":                     packageKeyID,
		"performs_release_effects":                     false,
		"persistent_credential_installation_permitted": false,
		"production_ready":                             false,
		"publication_dispatch_authorized":              true,
		"publication_target_root":                      tourV4LiveVolumeRoot,
		"receipt_authority_key_id":                     receiptKeyID,
		"receipt_authority_public_sha256":              digest(receiptAnchor),
		"root_helper_authorization_required":           true,
		"runtime_deployment_permitted":                 false,
		"schema":                                       tourV4DetachedMaterializationSchema,
		"source_manifest_digest":                       sourceDigest,
		"valid_until_epoch":                            json.Number("1800003600"),
		"version":                                      json.Number("4"),
	})
	if err != nil {
		t.Fatal(err)
	}
	materials := TourV4DetachedMaterials{
		AuthorityBootstrap:          bootstrap,
		AuthorityBootstrapSignature: bootstrapSignature,
		Materialization:             materialization,
		MaterializationSignature: ed25519.Sign(
			fixture.packageKey,
			framed(tourV4DetachedMaterializationDomain, materialization),
		),
		PackageAnchor: packageAnchor,
		ReceiptKey:    receiptKey,
		ReceiptAnchor: receiptAnchor,
	}
	for _, path := range []string{
		BaseEnvironmentPath,
		GoogleIdentityEnvPath,
		RegistrationEmailEnvPath,
		SceneVideoEnvPath,
		DatabaseRuntimeEnvironmentPath,
		AdmissionEnvPath,
	} {
		if err := os.Remove(rooted(fixture.root, path)); err != nil {
			t.Fatal(err)
		}
	}
	binding, key, err := tourV4DetachedAuthority(fixture.root, materials)
	if err != nil {
		t.Fatalf("detached signed tour authority required unrelated runtime credentials: %v", err)
	}
	if binding.Profile != tourV4DetachedProfile ||
		binding.SourceManifestDigest != sourceDigest ||
		!bytes.Equal(key, fixture.receiptKey) {
		t.Fatal("detached tour authority binding changed")
	}
	zero(key)

	tampered := materials
	tampered.MaterializationSignature = append(
		[]byte(nil), materials.MaterializationSignature...,
	)
	tampered.MaterializationSignature[0] ^= 1
	if _, key, err := tourV4DetachedAuthority(fixture.root, tampered); err == nil {
		zero(key)
		t.Fatal("tampered detached materialization signature was accepted")
	}
	writeFixture(
		t, rooted(fixture.root, "/etc/machine-id"),
		[]byte("ffffffffffffffffffffffffffffffff\n"), 0o444,
	)
	if _, key, err := tourV4DetachedAuthority(fixture.root, materials); err == nil {
		zero(key)
		t.Fatal("detached authority replayed to a different host")
	}
}

func tourV4TestSHA(raw []byte) string {
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:])
}

func tourV4TestWrite(t *testing.T, path string, raw []byte, mode os.FileMode) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, raw, mode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, mode); err != nil {
		t.Fatal(err)
	}
}

func tourV4TestCoverage() map[string]any {
	labels := []string{
		"living room", "bedroom", "living room detail 2", "bedroom detail 2",
		"living room detail 3", "bedroom detail 3", "living room detail 4",
		"bedroom detail 4",
	}
	expected := make([]any, len(labels))
	visited := make([]any, len(labels))
	segments := make([]any, len(labels))
	for index, label := range labels {
		expected[index], visited[index] = label, label
		segments[index] = map[string]any{
			"segment": label, "index": index + 1,
			"start": float64(index * 6), "end": float64((index + 1) * 6),
		}
	}
	return map[string]any{
		"status":            "pass",
		"source":            "propertyquarry_generated_reconstruction_viewer_capture",
		"segments_expected": expected, "segments_visited": visited,
		"coverage_segments": segments,
	}
}

func tourV4TestJSON(t *testing.T, value any) []byte {
	t.Helper()
	raw, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func tourV4TestFixture(t *testing.T) (string, tourV4Permit) {
	t.Helper()
	root := filepath.Join(t.TempDir(), "fixture")
	if err := os.Mkdir(root, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(root, 0o755); err != nil {
		t.Fatal(err)
	}
	slug := "test-layout-first-v4"
	disclosure := "Planning preview built from the floor plan and listing photos. Use it as a layout aid, not as a captured tour."
	public := map[string][]byte{
		"diorama-preview.png":                                                    []byte("diorama"),
		"generated-reconstruction/generated-walkthrough.mp4":                     []byte("walkthrough-video"),
		"generated-reconstruction/model.glb":                                     []byte("glb"),
		"generated-reconstruction/model.mtl":                                     []byte("material"),
		"generated-reconstruction/model.obj":                                     []byte("object"),
		"generated-reconstruction/source-floorplan.webp":                         []byte("floorplan"),
		"generated-reconstruction/vendor/examples/jsm/controls/OrbitControls.js": []byte("orbit"),
		"generated-reconstruction/vendor/three.module.js":                        []byte("three"),
		"generated-reconstruction/viewer.html":                                   []byte("<html>viewer</html>"),
		"telegram-preview.png":                                                   []byte("telegram"),
	}
	for index := 1; index <= 8; index++ {
		public[filepath.ToSlash(filepath.Join(
			"generated-reconstruction", "photo-"+formatTwoDigits(index)+".webp",
		))] = []byte("photo-" + formatTwoDigits(index))
	}
	coverage := tourV4TestCoverage()
	quality := map[string]any{
		"provider_key":        "propertyquarry_generated_reconstruction",
		"viewer_capture_mode": true, "duration_seconds": 48.0,
		"room_stop_count": 8, "walkthrough_coverage_proof": coverage,
	}
	public["generated-reconstruction/generated-walkthrough.quality.json"] = tourV4TestJSON(t, quality)
	reconstruction := map[string]any{
		"slug": slug, "provider": "propertyquarry_generated_reconstruction",
		"disclosure": disclosure, "verified_provider_capture": false,
		"satisfies_verified_tour_gate": false,
		"viewer": map[string]any{
			"version": "propertyquarry_3d_tour_viewer_v3",
			"sha256": strings.TrimPrefix(
				tourV4TestSHA(public["generated-reconstruction/viewer.html"]), "sha256:",
			),
		},
		"walkthrough": map[string]any{
			"status": "generated", "relpath": "generated-walkthrough.mp4",
			"sidecar_relpath": "generated-walkthrough.quality.json",
			"sha256": strings.TrimPrefix(
				tourV4TestSHA(public["generated-reconstruction/generated-walkthrough.mp4"]), "sha256:",
			),
			"sidecar_sha256": strings.TrimPrefix(
				tourV4TestSHA(public["generated-reconstruction/generated-walkthrough.quality.json"]), "sha256:",
			),
			"size_bytes":       len(public["generated-reconstruction/generated-walkthrough.mp4"]),
			"duration_seconds": 48.0, "coverage_proof": coverage,
		},
	}
	public["generated-reconstruction/reconstruction.json"] = tourV4TestJSON(t, reconstruction)
	tour := map[string]any{
		"slug": slug, "tour_privacy_mode": "anonymous_public",
		"publication_status": "ready", "creation_mode": "generated_reconstruction_tour",
		"public_url": "/tours/" + slug, "hosted_url": "/tours/" + slug,
		"video_provider_key":    "propertyquarry_generated_reconstruction",
		"video_relpath":         "generated-reconstruction/generated-walkthrough.mp4",
		"video_sidecar_relpath": "generated-reconstruction/generated-walkthrough.quality.json",
		"generated_reconstruction": map[string]any{
			"provider":       "propertyquarry_generated_reconstruction",
			"viewer_version": "propertyquarry_3d_tour_viewer_v3",
			"disclosure":     disclosure, "verified_provider_capture": false,
			"satisfies_verified_tour_gate": false, "walkthrough_stop_count": 8,
			"walkthrough_video_relpath":   "generated-reconstruction/generated-walkthrough.mp4",
			"walkthrough_sidecar_relpath": "generated-reconstruction/generated-walkthrough.quality.json",
			"walkthrough_coverage_proof":  coverage,
		},
	}
	public["tour.json"] = tourV4TestJSON(t, tour)
	private := map[string]any{
		"candidate_ref": "candidate-private", "external_id": "listing-private",
		"principal_id": "principal-private", "search_run_id": "run-private",
		"private_exact_location": "Private Test Street 99",
		"recipient_email":        "private-fixture@example.invalid",
	}
	control := map[string][]byte{
		"tour.private.json": tourV4TestJSON(t, private),
		".propertyquarry-render-commit.json": tourV4TestJSON(t, map[string]any{
			"schema": "propertyquarry.render_bundle_commit.v1", "slug": slug,
			"tour_manifest_sha256": strings.TrimPrefix(tourV4TestSHA(public["tour.json"]), "sha256:"),
			"transaction_id":       "0123456789abcdef0123456789abcdef",
		}),
	}
	for path, raw := range public {
		tourV4TestWrite(t, filepath.Join(root, filepath.FromSlash(path)), raw, 0o644)
	}
	for path, raw := range control {
		tourV4TestWrite(t, filepath.Join(root, path), raw, 0o600)
	}
	if err := filepath.WalkDir(root, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			return os.Chmod(path, 0o755)
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	return root, tourV4TestPermit(t, root, slug, disclosure)
}

func formatTwoDigits(value int) string {
	if value < 10 {
		return "0" + strconv.Itoa(value)
	}
	return strconv.Itoa(value)
}

func tourV4TestPermit(t *testing.T, root, slug, disclosure string) tourV4Permit {
	t.Helper()
	private := map[string]bool{
		".propertyquarry-render-commit.json": true, "tour.private.json": true,
	}
	files := []tourV4PermitFile{}
	directories := []tourV4DirectorySnapshot{{Path: ".", Mode: 0o755}}
	err := filepath.WalkDir(root, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if path == root {
			return nil
		}
		relative, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		relative = filepath.ToSlash(relative)
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if entry.IsDir() {
			directories = append(directories, tourV4DirectorySnapshot{
				Path: relative, Mode: uint32(info.Mode().Perm()),
			})
			return nil
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		files = append(files, tourV4PermitFile{
			Path: relative, Mode: uint32(info.Mode().Perm()), Size: info.Size(),
			SHA256: tourV4TestSHA(raw), Public: !private[relative],
		})
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	sort.Slice(files, func(left, right int) bool { return files[left].Path < files[right].Path })
	sort.Slice(directories, func(left, right int) bool {
		return directories[left].Path < directories[right].Path
	})
	allSnapshots := make([]tourV4FileSnapshot, 0, len(files))
	publicSnapshots := make([]tourV4FileSnapshot, 0, len(files)-2)
	for _, file := range files {
		row := tourV4FileSnapshot{
			Path: file.Path, Mode: file.Mode, Size: file.Size,
			SHA256: file.SHA256, Public: file.Public,
		}
		allSnapshots = append(allSnapshots, row)
		if file.Public {
			publicSnapshots = append(publicSnapshots, row)
		}
	}
	artifactTree, err := tourV4TreeDigest(directories, allSnapshots)
	if err != nil {
		t.Fatal(err)
	}
	publicTree, err := tourV4TreeDigest(directories, publicSnapshots)
	if err != nil {
		t.Fatal(err)
	}
	byPath := map[string]tourV4PermitFile{}
	for _, file := range files {
		byPath[file.Path] = file
	}
	return tourV4Permit{
		Slug: slug, ReconstructionKind: "layout_preview",
		Provider:      "propertyquarry_generated_reconstruction",
		ViewerVersion: "propertyquarry_3d_tour_viewer_v3",
		Disclosure:    disclosure, ArtifactTreeSHA256: artifactTree,
		PublicTreeSHA256:          publicTree,
		BrowserReceiptSHA256:      tourV4TestSHA([]byte("browser-receipt")),
		BrowserEvidenceTreeSHA256: tourV4TestSHA([]byte("browser-evidence")),
		QualityReceiptSHA256:      byPath["generated-reconstruction/generated-walkthrough.quality.json"].SHA256,
		WalkthroughSHA256:         byPath["generated-reconstruction/generated-walkthrough.mp4"].SHA256,
		TourManifestSHA256:        byPath["tour.json"].SHA256,
		ReconstructionSHA256:      byPath["generated-reconstruction/reconstruction.json"].SHA256,
		RenderCommitSHA256:        byPath[".propertyquarry-render-commit.json"].SHA256,
		Files:                     files,
	}
}

func tourV4TestAuthority(t *testing.T) (string, tourV4AuthorityBinding, ed25519.PrivateKey) {
	t.Helper()
	root := t.TempDir()
	for _, directory := range []struct {
		path string
		mode os.FileMode
	}{
		{tourV4RootPath(root, tourV4ReceiptRoot), 0o700},
		{tourV4RootPath(root, tourV4LiveVolumeRoot), 0o755},
	} {
		if err := os.MkdirAll(directory.path, directory.mode); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(directory.path, directory.mode); err != nil {
			t.Fatal(err)
		}
	}
	_, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	keyID, err := publicKeyID(key.Public().(ed25519.PublicKey))
	if err != nil {
		t.Fatal(err)
	}
	return root, tourV4AuthorityBinding{
		Profile:      "single-host-production-v2",
		ConfigDigest: tourV4TestSHA([]byte("config")),
		RuntimeSHA:   strings.Repeat("a", 40), WorkflowSHA: strings.Repeat("b", 40),
		DeploymentID: strings.Repeat("c", 64), ReceiptKeyID: keyID,
	}, key
}

func tourV4WithPermit(t *testing.T, permit tourV4Permit) string {
	t.Helper()
	original := tourV4AuthorizedPermits
	t.Cleanup(func() { tourV4AuthorizedPermits = original })
	tourV4AuthorizedPermits = []tourV4Permit{permit}
	_, manifestSHA, err := tourV4PermitManifest(&tourV4AuthorizedPermits[0])
	if err != nil {
		t.Fatal(err)
	}
	return manifestSHA
}

func TestTourV4AuditedArtifactMatchesCompiledPermit(t *testing.T) {
	path := os.Getenv("PROPERTYQUARRY_TOUR_V4_TEST_BUNDLE")
	if path == "" {
		t.Skip("set PROPERTYQUARRY_TOUR_V4_TEST_BUNDLE for the audited artifact acceptance")
	}
	snapshot, err := tourV4SnapshotTree(path, &tourV4AuthorizedPermits[0], false)
	if err != nil {
		t.Fatal(err)
	}
	defer snapshot.release()
	if err := tourV4ValidateArtifact(snapshot, &tourV4AuthorizedPermits[0]); err != nil {
		t.Fatal(err)
	}
	public := 0
	for _, file := range snapshot.Files {
		if file.Public {
			public++
		}
	}
	if public != 21 || len(snapshot.Files) != 23 {
		t.Fatalf("unexpected file counts: public=%d total=%d", public, len(snapshot.Files))
	}
}

func TestTourV4FirstPublishIsCASIdempotentAndExcludesPrivateFiles(t *testing.T) {
	bundle, permit := tourV4TestFixture(t)
	manifestSHA := tourV4WithPermit(t, permit)
	root, binding, key := tourV4TestAuthority(t)
	input := tourV4PublishInput{
		BundlePath: bundle, ExpectedOldTreeSHA256: tourV4AbsentSentinel,
		ExpectedManifestSHA256: manifestSHA,
		TransactionID:          "11111111111111111111111111111111",
	}
	raw, err := tourV4Publish(root, binding, key, input)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(raw)
	payload, canonical, err := verifySignedReceiptPayload(raw, key.Public().(ed25519.PublicKey))
	zero(canonical)
	if err != nil {
		t.Fatal(err)
	}
	if payload["schema"] != tourV4TerminalSchema || payload["status"] != "succeeded" ||
		payload["artifact_tree_sha256"] != permit.ArtifactTreeSHA256 ||
		payload["manifest_sha256"] != manifestSHA ||
		payload["private_source_files_published"] != false {
		t.Fatalf("terminal binding invalid: %#v", payload)
	}
	live := filepath.Join(tourV4RootPath(root, tourV4LiveVolumeRoot), permit.Slug)
	if _, err := os.Lstat(filepath.Join(live, "tour.private.json")); !os.IsNotExist(err) {
		t.Fatalf("private manifest entered live tree: %v", err)
	}
	if _, err := os.Lstat(filepath.Join(live, ".propertyquarry-render-commit.json")); !os.IsNotExist(err) {
		t.Fatalf("render commit entered live tree: %v", err)
	}
	liveSnapshot, err := tourV4SnapshotTree(live, &permit, true)
	if err != nil {
		t.Fatal(err)
	}
	if len(liveSnapshot.Files) != 21 || liveSnapshot.TreeSHA256 != permit.PublicTreeSHA256 {
		t.Fatalf("live tree invalid: files=%d sha=%s", len(liveSnapshot.Files), liveSnapshot.TreeSHA256)
	}
	liveSnapshot.release()
	replayed, err := tourV4Publish(root, binding, key, input)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(replayed)
	if !bytes.Equal(raw, replayed) {
		t.Fatal("idempotent replay returned a different terminal receipt")
	}
}

func TestTourV4ControlRootNameDriftReversesFirstPublication(t *testing.T) {
	bundle, permit := tourV4TestFixture(t)
	manifestSHA := tourV4WithPermit(t, permit)
	root, binding, key := tourV4TestAuthority(t)
	input := tourV4PublishInput{
		BundlePath: bundle, ExpectedOldTreeSHA256: tourV4AbsentSentinel,
		ExpectedManifestSHA256: manifestSHA,
		TransactionID:          "10101010101010101010101010101010",
	}
	controlPath := filepath.Join(
		tourV4RootPath(root, tourV4LiveVolumeRoot), tourV4ControlRelpath,
	)
	driftedPath := controlPath + ".drifted"
	originalHook := tourV4BeforeControlBindingCheck
	t.Cleanup(func() { tourV4BeforeControlBindingCheck = originalHook })
	var hookErr error
	tourV4BeforeControlBindingCheck = func() {
		hookErr = os.Rename(controlPath, driftedPath)
	}
	raw, err := tourV4Publish(root, binding, key, input)
	zero(raw)
	if hookErr != nil {
		t.Fatalf("could not simulate control-root name drift: %v", hookErr)
	}
	if err == nil || !strings.Contains(err.Error(), "control-root-drift-detected") {
		t.Fatalf("expected fail-closed control-root drift, got %v", err)
	}
	livePath := filepath.Join(tourV4RootPath(root, tourV4LiveVolumeRoot), permit.Slug)
	if _, statErr := os.Lstat(livePath); !os.IsNotExist(statErr) {
		t.Fatalf("candidate remained live after drift reversal: %v", statErr)
	}
	retainedCandidate, err := tourV4SnapshotTree(
		filepath.Join(driftedPath, "stage-v4-"+input.TransactionID), &permit, true,
	)
	if err != nil {
		t.Fatalf("reversed candidate was not retained by the opened control fd: %v", err)
	}
	defer retainedCandidate.release()
	if retainedCandidate.TreeSHA256 != permit.PublicTreeSHA256 {
		t.Fatal("reversed candidate tree digest changed")
	}
}

func TestTourV4InspectionReturnsSignedExactCASInput(t *testing.T) {
	_, permit := tourV4TestFixture(t)
	manifestSHA := tourV4WithPermit(t, permit)
	root, binding, key := tourV4TestAuthority(t)
	raw, err := tourV4Inspect(root, binding, key, manifestSHA)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(raw)
	payload, canonical, err := verifySignedReceiptPayload(raw, key.Public().(ed25519.PublicKey))
	zero(canonical)
	if err != nil {
		t.Fatal(err)
	}
	if payload["schema"] != tourV4InspectionSchema ||
		payload["expected_old_tree_argument"] != tourV4AbsentSentinel ||
		payload["live_tree"] != nil || payload["performs_release_effects"] != false {
		t.Fatalf("absent inspection invalid: %#v", payload)
	}
	old := tourV4TestOldTree(t, root, permit.Slug)
	defer old.release()
	raw, err = tourV4Inspect(root, binding, key, manifestSHA)
	if err != nil {
		t.Fatal(err)
	}
	payload, canonical, err = verifySignedReceiptPayload(raw, key.Public().(ed25519.PublicKey))
	zero(canonical)
	if err != nil {
		t.Fatal(err)
	}
	if payload["expected_old_tree_argument"] != old.TreeSHA256 {
		t.Fatalf("inspection CAS mismatch: got %#v want %s", payload["expected_old_tree_argument"], old.TreeSHA256)
	}
}

func tourV4TestOldTree(t *testing.T, root, slug string) *tourV4TreeSnapshot {
	t.Helper()
	path := filepath.Join(tourV4RootPath(root, tourV4LiveVolumeRoot), slug)
	if err := os.Mkdir(path, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, 0o755); err != nil {
		t.Fatal(err)
	}
	tourV4TestWrite(t, filepath.Join(path, "legacy.txt"), []byte("legacy-public-tree"), 0o644)
	snapshot, err := tourV4SnapshotTree(path, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	return snapshot
}

func TestTourV4ReplacementRecoversAfterExchangeAndRollsBackByCAS(t *testing.T) {
	bundle, permit := tourV4TestFixture(t)
	manifestSHA := tourV4WithPermit(t, permit)
	root, binding, key := tourV4TestAuthority(t)
	old := tourV4TestOldTree(t, root, permit.Slug)
	defer old.release()
	input := tourV4PublishInput{
		BundlePath: bundle, ExpectedOldTreeSHA256: old.TreeSHA256,
		ExpectedManifestSHA256: manifestSHA,
		TransactionID:          "22222222222222222222222222222222",
	}
	originalHook := tourV4AfterExchangeHook
	t.Cleanup(func() { tourV4AfterExchangeHook = originalHook })
	interrupted := false
	tourV4AfterExchangeHook = func() error {
		if !interrupted {
			interrupted = true
			return errors.New("simulated-crash-boundary")
		}
		return nil
	}
	if raw, err := tourV4Publish(root, binding, key, input); err == nil ||
		!strings.Contains(err.Error(), "post-exchange-interrupted") {
		zero(raw)
		t.Fatalf("expected post-exchange interruption, got %v", err)
	}
	tourV4AfterExchangeHook = nil
	recovered, err := tourV4Recover(
		root, binding, key, manifestSHA, old.TreeSHA256, input.TransactionID,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(recovered)
	livePath := filepath.Join(tourV4RootPath(root, tourV4LiveVolumeRoot), permit.Slug)
	live, err := tourV4SnapshotTree(livePath, &permit, true)
	if err != nil {
		t.Fatal(err)
	}
	if live.TreeSHA256 != permit.PublicTreeSHA256 {
		t.Fatal("recovery did not finalize the candidate tree")
	}
	live.release()
	rolledBack, err := tourV4Rollback(
		root, binding, key, manifestSHA, old.TreeSHA256,
		permit.PublicTreeSHA256, input.TransactionID,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(rolledBack)
	restored, err := tourV4SnapshotTree(livePath, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	if restored.TreeSHA256 != old.TreeSHA256 ||
		restored.Device != old.Device || restored.Inode != old.Inode {
		t.Fatal("rollback did not restore the exact old tree")
	}
	restored.release()
	replayed, err := tourV4Rollback(
		root, binding, key, manifestSHA, old.TreeSHA256,
		permit.PublicTreeSHA256, input.TransactionID,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(replayed)
	if !bytes.Equal(rolledBack, replayed) {
		t.Fatal("rollback replay returned a different terminal receipt")
	}
}

func TestTourV4FilesystemAdversariesFailBeforeLiveMutation(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(*testing.T, string)
	}{
		{
			name: "symlink",
			mutate: func(t *testing.T, root string) {
				target := filepath.Join(root, "generated-reconstruction/photo-01.webp")
				link := filepath.Join(root, "generated-reconstruction/photo-02.webp")
				if err := os.Remove(link); err != nil {
					t.Fatal(err)
				}
				if err := os.Symlink(target, link); err != nil {
					t.Fatal(err)
				}
			},
		},
		{
			name: "hardlink",
			mutate: func(t *testing.T, root string) {
				target := filepath.Join(root, "generated-reconstruction/photo-01.webp")
				link := filepath.Join(root, "generated-reconstruction/photo-02.webp")
				if err := os.Remove(link); err != nil {
					t.Fatal(err)
				}
				if err := os.Link(target, link); err != nil {
					t.Fatal(err)
				}
			},
		},
		{
			name: "extra-file",
			mutate: func(t *testing.T, root string) {
				tourV4TestWrite(t, filepath.Join(root, "unexpected.txt"), []byte("extra"), 0o644)
			},
		},
		{
			name: "bad-public-mode",
			mutate: func(t *testing.T, root string) {
				if err := os.Chmod(filepath.Join(root, "telegram-preview.png"), 0o600); err != nil {
					t.Fatal(err)
				}
			},
		},
		{
			name: "bad-private-mode",
			mutate: func(t *testing.T, root string) {
				if err := os.Chmod(filepath.Join(root, "tour.private.json"), 0o644); err != nil {
					t.Fatal(err)
				}
			},
		},
	}
	for index, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			bundle, permit := tourV4TestFixture(t)
			manifestSHA := tourV4WithPermit(t, permit)
			test.mutate(t, bundle)
			root, binding, key := tourV4TestAuthority(t)
			input := tourV4PublishInput{
				BundlePath: bundle, ExpectedOldTreeSHA256: tourV4AbsentSentinel,
				ExpectedManifestSHA256: manifestSHA,
				TransactionID:          strings.Repeat(strconv.FormatInt(int64(index+3), 16), 32),
			}
			raw, err := tourV4Publish(root, binding, key, input)
			zero(raw)
			if err == nil {
				t.Fatal("adversarial artifact was accepted")
			}
			live := filepath.Join(tourV4RootPath(root, tourV4LiveVolumeRoot), permit.Slug)
			if _, statErr := os.Lstat(live); !os.IsNotExist(statErr) {
				t.Fatalf("live tree mutated: %v", statErr)
			}
		})
	}
}

func tourV4RewriteTourAndCommit(
	t *testing.T,
	root string,
	mutate func(map[string]any),
) tourV4Permit {
	t.Helper()
	tourPath := filepath.Join(root, "tour.json")
	raw, err := os.ReadFile(tourPath)
	if err != nil {
		t.Fatal(err)
	}
	value := map[string]any{}
	if err := json.Unmarshal(raw, &value); err != nil {
		t.Fatal(err)
	}
	mutate(value)
	tourRaw := tourV4TestJSON(t, value)
	tourV4TestWrite(t, tourPath, tourRaw, 0o644)
	commitPath := filepath.Join(root, ".propertyquarry-render-commit.json")
	commitRaw, err := os.ReadFile(commitPath)
	if err != nil {
		t.Fatal(err)
	}
	commit := map[string]any{}
	if err := json.Unmarshal(commitRaw, &commit); err != nil {
		t.Fatal(err)
	}
	commit["tour_manifest_sha256"] = strings.TrimPrefix(tourV4TestSHA(tourRaw), "sha256:")
	tourV4TestWrite(t, commitPath, tourV4TestJSON(t, commit), 0o600)
	return tourV4TestPermit(
		t, root, value["slug"].(string),
		value["generated_reconstruction"].(map[string]any)["disclosure"].(string),
	)
}

func TestTourV4PrivacyRejectsCoordinatesAndPrivateValuesEvenWhenHashesArePermitted(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(map[string]any)
	}{
		{
			name: "geographic-coordinate-key",
			mutate: func(value map[string]any) {
				value["latitude"] = 48.2082
			},
		},
		{
			name: "private-value-in-public",
			mutate: func(value map[string]any) {
				value["public_note"] = "principal-private"
			},
		},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			bundle, _ := tourV4TestFixture(t)
			permit := tourV4RewriteTourAndCommit(t, bundle, test.mutate)
			snapshot, err := tourV4SnapshotTree(bundle, &permit, false)
			if err != nil {
				t.Fatal(err)
			}
			defer snapshot.release()
			if err := tourV4ValidateArtifact(snapshot, &permit); err == nil {
				t.Fatal("privacy-tainted artifact was accepted")
			}
		})
	}
}

func TestTourV4ExplicitCASMismatchLeavesNoStageOrReceipt(t *testing.T) {
	bundle, permit := tourV4TestFixture(t)
	manifestSHA := tourV4WithPermit(t, permit)
	root, binding, key := tourV4TestAuthority(t)
	old := tourV4TestOldTree(t, root, permit.Slug)
	defer old.release()
	input := tourV4PublishInput{
		BundlePath:             bundle,
		ExpectedOldTreeSHA256:  tourV4TestSHA([]byte("not-the-old-tree")),
		ExpectedManifestSHA256: manifestSHA,
		TransactionID:          "99999999999999999999999999999999",
	}
	raw, err := tourV4Publish(root, binding, key, input)
	zero(raw)
	if err == nil || !strings.Contains(err.Error(), "cas-old-tree-mismatch") {
		t.Fatalf("expected CAS failure, got %v", err)
	}
	after, err := tourV4SnapshotTree(
		filepath.Join(tourV4RootPath(root, tourV4LiveVolumeRoot), permit.Slug),
		nil, false,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer after.release()
	if after.TreeSHA256 != old.TreeSHA256 || after.Device != old.Device || after.Inode != old.Inode {
		t.Fatal("CAS mismatch changed the live tree")
	}
	receiptRoot := tourV4RootPath(root, tourV4ReceiptRoot)
	entries, err := os.ReadDir(receiptRoot)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if strings.Contains(entry.Name(), input.TransactionID) {
			t.Fatalf("CAS mismatch left a transaction receipt: %s", entry.Name())
		}
	}
}
