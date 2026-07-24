//go:build linux && amd64

package authority

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestAiPanoramaPhaseResultsAreExactMinimalProjections(t *testing.T) {
	preflight := map[string]any{
		"schema": aiPanoramaTerminalSchema, "status": "preflight-passed",
		"slug": aiPanoramaPraterSlug, "nonce_consumed": false,
		"database_access_performed": false, "private_values_redacted": true,
	}
	raw, err := canonicalJSON(preflight)
	if err != nil {
		t.Fatal(err)
	}
	raw = append(raw, '\n')
	result, err := parseAiPanoramaPhaseResult(raw, "preflight")
	if err != nil || result.Status != "preflight-passed" {
		t.Fatalf("valid preflight projection rejected: %#v %v", result, err)
	}
	preflight["unexpected"] = true
	raw, _ = canonicalJSON(preflight)
	raw = append(raw, '\n')
	if _, err := parseAiPanoramaPhaseResult(raw, "preflight"); err == nil {
		t.Fatal("preflight projection accepted an extra key")
	}

	apply := map[string]any{
		"schema": aiPanoramaTerminalSchema, "status": "committed",
		"slug": aiPanoramaPraterSlug, "control_path": aiPanoramaPraterControlPath,
		"terminal_receipt_sha256": strings.Repeat("a", 64),
		"private_values_redacted": true,
	}
	raw, _ = canonicalJSON(apply)
	raw = append(raw, '\n')
	result, err = parseAiPanoramaPhaseResult(raw, "apply")
	if err != nil || result.TerminalReceiptSHA256 != strings.Repeat("a", 64) {
		t.Fatalf("valid apply projection rejected: %#v %v", result, err)
	}
	apply["control_path"] = "/tours/rebound/control"
	raw, _ = canonicalJSON(apply)
	raw = append(raw, '\n')
	if _, err := parseAiPanoramaPhaseResult(raw, "apply"); err == nil {
		t.Fatal("apply projection accepted a rebound control path")
	}
}

func aiPanoramaTestInstallReceipt(
	status string,
	bindingStatus string,
	beforeSHA256 string,
	afterSHA256 string,
	permitSHA256 string,
	sealed *aiPanoramaSealedArtifactObservation,
) map[string]any {
	return map[string]any{
		"already_installed":                       status == "already_installed",
		"applied":                                 status == "installed",
		"authenticated_principal_verified":        true,
		"candidate_binding_verified":              true,
		"candidate_marker_sha256":                 aiPanoramaExpectedMarkerDigest,
		"contract":                                aiPanoramaInstallReceiptSchema,
		"control_path":                            aiPanoramaPraterControlPath,
		"controller_nonce_consumed":               true,
		"controller_permit_sha256":                permitSHA256,
		"controller_permit_verified":              true,
		"core_manifest_sha256":                    aiPanoramaExpectedCoreDigest,
		"install_request_contract":                aiPanoramaInstallRequestSchema,
		"listing_identity_verified":               true,
		"materialization_lineage_verified":        true,
		"materialization_receipt_sha256":          aiPanoramaExpectedReceiptDigest,
		"mode":                                    "apply",
		"principal_binding_verified":              true,
		"private_values_redacted":                 true,
		"property_url_sha256":                     aiPanoramaPropertyURLSHA256,
		"provider_key":                            "willhaben",
		"public_tour_volume_profile_verified":     true,
		"publication_authorization_record_sha256": beforeSHA256,
		"publication_authorization_verified":      true,
		"publication_binding_after_sha256":        afterSHA256,
		"publication_binding_before_sha256":       beforeSHA256,
		"publication_binding_status":              bindingStatus,
		"publication_binding_verified":            true,
		"release_eligible":                        true,
		"representation_kind":                     "ai_panorama_360",
		"run_binding_verified":                    true,
		"run_terminal_verified":                   true,
		"slug":                                    aiPanoramaPraterSlug,
		"source_file_count": json.Number(
			strconv.Itoa(sealed.FileCount),
		),
		"source_identity_contract":       aiPanoramaSourceIdentitySchema,
		"source_identity_verified":       true,
		"source_relative_path_semantics": aiPanoramaSourcePathSemantics,
		"source_relative_root":           ".",
		"source_total_bytes": json.Number(
			strconv.FormatInt(sealed.TotalBytes, 10),
		),
		"source_tour_sha256":    aiPanoramaExpectedTourDigest,
		"source_tree_algorithm": aiPanoramaSourceTreeAlgorithm,
		"source_tree_sha256":    aiPanoramaExpectedSourceTree,
		"status":                status,
	}
}

func TestAiPanoramaInstallReceiptIsExactAndIndependentlyStatusBound(t *testing.T) {
	sealed := &aiPanoramaSealedArtifactObservation{
		FileCount: 14, TotalBytes: 7970936,
	}
	permitSHA256 := strings.Repeat("a", 64)
	publicationSHA256 := strings.Repeat("b", 64)
	appliedSHA256 := strings.Repeat("c", 64)
	for _, status := range []string{"installed", "already_installed"} {
		for _, bindingStatus := range []string{"applied", "already_bound"} {
			t.Run(status+"-"+bindingStatus, func(t *testing.T) {
				afterSHA256 := appliedSHA256
				if bindingStatus == "already_bound" {
					afterSHA256 = publicationSHA256
				}
				receipt := aiPanoramaTestInstallReceipt(
					status, bindingStatus, publicationSHA256, afterSHA256,
					permitSHA256, sealed,
				)
				if err := validateAiPanoramaInstallReceipt(
					receipt, permitSHA256, publicationSHA256, bindingStatus,
					publicationSHA256, afterSHA256, sealed,
				); err != nil {
					t.Fatalf("valid independent status combination rejected: %v", err)
				}
			})
		}
	}

	valid := aiPanoramaTestInstallReceipt(
		"installed", "applied", publicationSHA256, appliedSHA256,
		permitSHA256, sealed,
	)
	assertRejected := func(name string, receipt map[string]any, expectedPublication string,
		bindingStatus string, beforeSHA256 string, afterSHA256 string,
	) {
		t.Helper()
		t.Run(name, func(t *testing.T) {
			if err := validateAiPanoramaInstallReceipt(
				receipt, permitSHA256, expectedPublication, bindingStatus,
				beforeSHA256, afterSHA256, sealed,
			); err == nil {
				t.Fatal("tampered install receipt accepted")
			}
		})
	}
	assertRejected(
		"arbitrary-nonempty", map[string]any{"status": "installed"},
		publicationSHA256, "applied", publicationSHA256, appliedSHA256,
	)
	assertRejected(
		"wrong-expected-publication", valid, strings.Repeat("d", 64),
		"applied", publicationSHA256, appliedSHA256,
	)
	innerBefore := cloneFields(valid)
	innerBefore["publication_binding_before_sha256"] = strings.Repeat("d", 64)
	assertRejected(
		"inner-before-mismatch", innerBefore, publicationSHA256,
		"applied", publicationSHA256, appliedSHA256,
	)
	innerAfter := cloneFields(valid)
	innerAfter["publication_binding_after_sha256"] = strings.Repeat("d", 64)
	assertRejected(
		"outer-inner-after-mismatch", innerAfter, publicationSHA256,
		"applied", publicationSHA256, appliedSHA256,
	)
	statusMismatch := cloneFields(valid)
	statusMismatch["applied"] = false
	assertRejected(
		"install-status-boolean-mismatch", statusMismatch, publicationSHA256,
		"applied", publicationSHA256, appliedSHA256,
	)
	appliedEqual := aiPanoramaTestInstallReceipt(
		"installed", "applied", publicationSHA256, publicationSHA256,
		permitSHA256, sealed,
	)
	assertRejected(
		"applied-equal-hashes", appliedEqual, publicationSHA256,
		"applied", publicationSHA256, publicationSHA256,
	)
	alreadyBoundUnequal := aiPanoramaTestInstallReceipt(
		"installed", "already_bound", publicationSHA256, appliedSHA256,
		permitSHA256, sealed,
	)
	assertRejected(
		"already-bound-unequal-hashes", alreadyBoundUnequal, publicationSHA256,
		"already_bound", publicationSHA256, appliedSHA256,
	)
	extra := cloneFields(valid)
	extra["unexpected"] = true
	assertRejected(
		"extra-key", extra, publicationSHA256,
		"applied", publicationSHA256, appliedSHA256,
	)
}

func TestAiPanoramaDiscoveryResultRejectsPrivateAndShapeDrift(t *testing.T) {
	requestID := strings.Repeat("b", 32)
	value := map[string]any{
		"schema": aiPanoramaDiscoveryResultSchema, "version": json.Number("1"),
		"authority": "propertyquarry-release-control", "status": "discovered",
		"owner_principal_id":                 "owner@example.invalid",
		"search_run_id":                      "98bed75e984549c6bd4371d602662ab8",
		"candidate_ref":                      "053ad185e1c44b2e",
		"expected_publication_record_sha256": strings.Repeat("c", 64),
		"request_id":                         requestID, "database_mutation_performed": false,
		"release_authorized": false, "private_projection": true,
	}
	raw, _ := canonicalJSON(value)
	raw = append(raw, '\n')
	result, err := parseAiPanoramaDiscoveryResult(raw, requestID)
	if err != nil || result.OwnerPrincipalID != "owner@example.invalid" {
		t.Fatalf("valid discovery projection rejected: %#v %v", result, err)
	}
	result.release()
	value["owner_principal_id"] = "München"
	raw, _ = canonicalJSON(value)
	raw = append(raw, '\n')
	if _, err := parseAiPanoramaDiscoveryResult(raw, requestID); err == nil {
		t.Fatal("non-ASCII owner principal accepted")
	}
	value["owner_principal_id"] = "owner@example.invalid"
	value["database_url"] = "secret"
	raw, _ = canonicalJSON(value)
	raw = append(raw, '\n')
	if _, err := parseAiPanoramaDiscoveryResult(raw, requestID); err == nil {
		t.Fatal("private discovery field accepted")
	}
}

func TestAiPanoramaGovernedVolumeVirginInitializationIsOneWay(t *testing.T) {
	value := map[string]any{
		"schema": aiPanoramaBootstrapSchema, "version": json.Number("1"),
		"status": "initialized", "root_device": json.Number("2049"),
		"root_inode": json.Number("3001"), "root_uid": json.Number("10001"),
		"root_gid": json.Number("10001"), "root_mode": json.Number("493"),
		"root_empty": true, "private_values_redacted": true,
	}
	raw, _ := canonicalJSON(value)
	raw = append(raw, '\n')
	result, err := parseAiPanoramaBootstrapResult(raw)
	if err != nil || result.RootDevice != 2049 || result.RootInode != 3001 {
		t.Fatalf("valid bootstrap result rejected: %#v %v", result, err)
	}
	value["root_mode"] = json.Number("511")
	raw, _ = canonicalJSON(value)
	raw = append(raw, '\n')
	if _, err := parseAiPanoramaBootstrapResult(raw); err == nil {
		t.Fatal("unsafe bootstrap root mode accepted")
	}

	before := &aiPanoramaRuntimeObservation{
		DockerRoot: "/var/lib/docker", ImageID: "sha256:" + strings.Repeat("1", 64),
		ControlRootDevice: 1, ControlRootInode: 2,
		PublicVolumeMountpoint: "/var/lib/docker/volumes/governed/_data",
		PublicVolumeDevice:     2049, PublicVolumeInode: 3001,
		PublicVolumeUID: 0, PublicVolumeGID: 0, PublicVolumeMode: 0o755,
		PublicVolumeNeedsInitialization: true,
		DatabaseContainerID:             "db", APIRuntimeContainerID: "api",
		SchedulerContainerID: "scheduler",
	}
	after := *before
	after.PublicVolumeUID, after.PublicVolumeGID = 10001, 10001
	after.PublicVolumeNeedsInitialization = false
	if !aiPanoramaRuntimeIdentityStable(before, &after) {
		t.Fatal("exact bootstrap identity transition rejected")
	}
	after.PublicVolumeInode++
	if aiPanoramaRuntimeIdentityStable(before, &after) {
		t.Fatal("bootstrap inode substitution accepted")
	}
}

func TestAiPanoramaGovernedRootInventoryRejectsUnrelatedEntries(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("requires root to construct the production 10001:10001 volume fixture")
	}
	for _, fixture := range []struct {
		name string
		make func(string) error
	}{
		{"file", func(root string) error {
			return os.WriteFile(filepath.Join(root, "another-tour.json"), []byte("x"), 0o644)
		}},
		{"directory", func(root string) error {
			return os.Mkdir(filepath.Join(root, "another-tour"), 0o755)
		}},
		{"symlink", func(root string) error {
			return os.Symlink(aiPanoramaPraterSlug, filepath.Join(root, "another-tour"))
		}},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			root := t.TempDir()
			if err := os.Chmod(root, 0o755); err != nil ||
				os.Chown(root, 10001, 10001) != nil {
				t.Fatal(err)
			}
			empty, err := snapshotAiPanoramaRelated(root)
			if err != nil || len(empty.Entries) != 0 {
				t.Fatalf("empty governed root rejected: %#v %v", empty, err)
			}
			if err := fixture.make(root); err != nil {
				t.Fatal(err)
			}
			if observed, err := snapshotAiPanoramaRelated(root); err == nil {
				t.Fatalf("unrelated %s accepted: %#v", fixture.name, observed)
			}
		})
	}
}

func TestAiPanoramaRevocationRequestAndContainerAreExactlyBound(t *testing.T) {
	now := time.Date(2026, 7, 24, 10, 11, 12, 345000000, time.UTC)
	raw, err := aiPanoramaRevocationWire(strings.Repeat("a", 32), now)
	if err != nil {
		t.Fatal(err)
	}
	expected := `{"authority":"propertyquarry-release-control","revocation_id":"` +
		strings.Repeat("a", 32) +
		`","revoked_at":"2026-07-24T10:11:12.345Z","schema":"` +
		aiPanoramaRevocationSchema + `","slug":"` + aiPanoramaPraterSlug +
		`","status":"revoked","tour_sha256":"` + aiPanoramaExpectedTourDigest +
		`","version":1}` + "\n"
	if string(raw) != expected {
		t.Fatalf("unexpected canonical closeout request: %q", raw)
	}
	config := aiPanoramaTestConfig()
	args, err := aiPanoramaContainerArguments(
		config, &aiPanoramaRuntimeObservation{}, nil, nil, "closeout", "",
	)
	if err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(args, "\x1f")
	for _, required := range []string{
		"--network\x1fnone", "--log-driver\x1fnone",
		"--cap-drop\x1fALL\x1f--security-opt",
		"--cap-add\x1fDAC_OVERRIDE",
		aiPanoramaBindMount(
			aiPanoramaCloseoutRequestPath, aiPanoramaCloseoutRequestPath, true,
		),
		aiPanoramaVolumeMount(
			aiPanoramaPublicVolumeName, aiPanoramaPublicMountTarget, false,
		),
		aiPanoramaCloseoutEntrypoint,
	} {
		if !strings.Contains(joined, required) {
			t.Fatalf("closeout contract missing %q", required)
		}
	}
	for _, forbidden := range []string{
		aiPanoramaControlRoot, aiPanoramaSealedArtifactRoot,
		aiPanoramaDatabaseSecretMount, "--cap-add\x1fCHOWN",
		"--cap-add\x1fFOWNER", "property_default",
	} {
		if strings.Contains(joined, forbidden) {
			t.Fatalf("closeout contract exposed %q", forbidden)
		}
	}
}

func TestAiPanoramaCloseoutConsumersMayBeAbsentOrStoppedButNeverUnexpectedOrRW(
	t *testing.T,
) {
	previous := executeAiPanoramaDocker
	t.Cleanup(func() { executeAiPanoramaDocker = previous })
	config := aiPanoramaTestConfig()
	proof := &aiPanoramaInstalledProof{
		WebImage: config.WebImage, WebImageID: "sha256:" + strings.Repeat("a", 64),
		RenderImage: config.RenderImage, RenderImageID: "sha256:" + strings.Repeat("b", 64),
	}
	mountpoint := "/var/lib/docker/volumes/" + aiPanoramaPublicVolumeName + "/_data"
	consumerIDs := map[string]string{
		strings.Repeat("1", 64): aiPanoramaAPIRuntimeService,
		strings.Repeat("2", 64): aiPanoramaSchedulerService,
		strings.Repeat("3", 64): aiPanoramaRenderService,
	}
	rows := ""
	readWrite := false
	executeAiPanoramaDocker = func(
		_ context.Context,
		_ string,
		arguments ...string,
	) ([]byte, error) {
		joined := strings.Join(arguments, " ")
		if strings.Contains(joined, "container ls") {
			return []byte(rows), nil
		}
		id := arguments[len(arguments)-1]
		service := consumerIDs[id]
		if strings.Contains(joined, "{{.Id}}|{{.Name}}") {
			image, imageID := proof.WebImage, proof.WebImageID
			if service == aiPanoramaRenderService {
				image, imageID = proof.RenderImage, proof.RenderImageID
			}
			return []byte(
				id + "|/stopped-" + service + "|" + imageID + "|" + image +
					"|false|" + ProjectName + "|" + service + "\n",
			), nil
		}
		mount, _ := canonicalJSON(map[string]any{
			"Destination": aiPanoramaPublicMountTarget,
			"Driver":      "local", "Mode": "z",
			"Name": aiPanoramaPublicVolumeName, "Propagation": "",
			"RW": readWrite, "Source": mountpoint, "Type": "volume",
		})
		return append(mount, '\n'), nil
	}
	if err := validateAiPanoramaCloseoutVolumeConsumers(
		context.Background(), mountpoint, proof,
	); err != nil {
		t.Fatalf("zero consumers rejected: %v", err)
	}
	orderedIDs := []string{
		strings.Repeat("1", 64), strings.Repeat("2", 64), strings.Repeat("3", 64),
	}
	for _, id := range orderedIDs {
		rows += id + "|" + ProjectName + "|" + consumerIDs[id] + "\n"
	}
	if err := validateAiPanoramaCloseoutVolumeConsumers(
		context.Background(), mountpoint, proof,
	); err != nil {
		t.Fatalf("exact stopped consumers rejected: %v", err)
	}
	readWrite = true
	if err := validateAiPanoramaCloseoutVolumeConsumers(
		context.Background(), mountpoint, proof,
	); err == nil {
		t.Fatal("RW closeout consumer accepted")
	}
	readWrite = false
	rows = strings.Repeat("4", 64) + "|" + ProjectName + "|unexpected-service\n"
	if err := validateAiPanoramaCloseoutVolumeConsumers(
		context.Background(), mountpoint, proof,
	); err == nil {
		t.Fatal("unexpected closeout consumer accepted")
	}
}

func TestAiPanoramaCloseoutRuntimeParsesImageBeforeZeroization(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("requires root to construct the production 10001:10001 volume fixture")
	}
	previous := executeAiPanoramaDocker
	t.Cleanup(func() { executeAiPanoramaDocker = previous })
	dockerRoot := t.TempDir()
	mountpoint := filepath.Join(
		dockerRoot, "volumes", aiPanoramaPublicVolumeName, "_data",
	)
	if err := os.MkdirAll(mountpoint, 0o755); err != nil ||
		os.Chmod(mountpoint, 0o755) != nil ||
		os.Chown(mountpoint, 10001, 10001) != nil {
		t.Fatal(err)
	}
	info, err := os.Lstat(mountpoint)
	metadata, ok := infoSys(info)
	if err != nil || !ok {
		t.Fatal("volume fixture metadata unavailable")
	}
	config := aiPanoramaTestConfig()
	proof := &aiPanoramaInstalledProof{
		WebImage: config.WebImage, WebImageID: "sha256:" + strings.Repeat("a", 64),
		DockerRoot: dockerRoot, PublicVolumeMountpoint: mountpoint,
		PublicVolumeDevice: uint64(metadata.Dev), PublicVolumeInode: metadata.Ino,
	}
	entrypointRaw, _ := canonicalJSON([]any{
		aiPanoramaControllerPython, "-I", "-S",
		"/usr/local/libexec/property_web_entrypoint.py",
	})
	repoDigestsRaw, _ := canonicalJSON([]any{proof.WebImage})
	imageRaw := []byte(
		proof.WebImageID + "|10001:10001|" + string(repoDigestsRaw) +
			"|" + string(entrypointRaw) + "\n",
	)
	zero(entrypointRaw)
	zero(repoDigestsRaw)
	volumeRaw, _ := canonicalJSON(map[string]any{
		"Name": aiPanoramaPublicVolumeName, "Driver": "local", "Scope": "local",
		"Mountpoint": mountpoint,
		"Labels": map[string]any{
			"com.docker.compose.project": ProjectName,
			"com.docker.compose.volume":  aiPanoramaPublicVolumeComposeKey,
		},
	})
	executeAiPanoramaDocker = func(
		_ context.Context,
		_ string,
		arguments ...string,
	) ([]byte, error) {
		joined := strings.Join(arguments, " ")
		switch {
		case strings.Contains(joined, "info --format"):
			raw, _ := json.Marshal(dockerRoot)
			return append(raw, '\n'), nil
		case strings.Contains(joined, "image inspect"):
			return imageRaw, nil
		case strings.Contains(joined, "volume inspect"):
			return append(append([]byte(nil), volumeRaw...), '\n'), nil
		case strings.Contains(joined, "container ls"):
			return []byte{}, nil
		default:
			return nil, errors.New("unexpected Docker observation")
		}
	}
	observed, err := observeAiPanoramaCloseoutRuntime(context.Background(), proof)
	zero(volumeRaw)
	if err != nil {
		t.Fatalf("valid closeout runtime rejected after image zeroization: %v", err)
	}
	if observed.PublicVolumeMountpoint != mountpoint ||
		observed.PublicVolumeDevice != proof.PublicVolumeDevice ||
		observed.PublicVolumeInode != proof.PublicVolumeInode {
		t.Fatal("closeout runtime observation lost exact volume identity")
	}
	for _, value := range imageRaw {
		if value != 0 {
			t.Fatal("image observation was not zeroized")
		}
	}
}

func aiPanoramaTestRuntimeRoot(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	for _, relative := range []string{
		"run", "run/propertyquarry-release-control",
		"run/propertyquarry-release-control/ai-panorama-install",
	} {
		if err := os.Mkdir(filepath.Join(root, relative), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func TestAiPanoramaCloseoutRequestRejectsTruncationAndNoncanonicalBytes(t *testing.T) {
	valid, err := aiPanoramaRevocationWire(
		strings.Repeat("a", 32),
		time.Date(2026, 7, 24, 10, 11, 12, 0, time.UTC),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Run("atomic-round-trip", func(t *testing.T) {
		root := aiPanoramaTestRuntimeRoot(t)
		if err := persistAiPanoramaCloseoutRequest(root, valid); err != nil {
			t.Fatal(err)
		}
		observed, err := readAiPanoramaCloseoutRequest(root)
		if err != nil || string(observed) != string(valid) {
			t.Fatalf("valid request did not round-trip: %v", err)
		}
		zero(observed)
		if err := persistAiPanoramaCloseoutRequest(root, valid); err != nil {
			t.Fatalf("idempotent request persistence failed: %v", err)
		}
		if err := removeAiPanoramaCloseoutRequest(root, valid); err != nil {
			t.Fatal(err)
		}
	})
	for _, fixture := range []struct {
		name string
		raw  []byte
	}{
		{"zero", []byte{}},
		{"truncated", valid[:len(valid)/2]},
		{"double-newline", append(append([]byte(nil), valid...), '\n')},
		{"noncanonical", append([]byte{' '}, valid...)},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			root := aiPanoramaTestRuntimeRoot(t)
			if err := os.WriteFile(
				rooted(root, aiPanoramaCloseoutRequestPath), fixture.raw, 0o400,
			); err != nil {
				t.Fatal(err)
			}
			if raw, err := readAiPanoramaCloseoutRequest(root); err == nil {
				zero(raw)
				t.Fatal("malformed closeout request accepted")
			}
		})
	}
}

func aiPanoramaTestGenesisRoot(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	for _, relative := range []string{
		"var", "var/lib", "var/lib/propertyquarry",
		"var/lib/propertyquarry-release-single-host-v2",
		"var/lib/propertyquarry-release-single-host-v2/journal",
		"var/lib/propertyquarry/release-control",
		"var/lib/propertyquarry/release-control/ai-panorama-install",
		"var/lib/propertyquarry/release-control/ai-panorama-install/permits",
		"var/lib/propertyquarry/release-control/ai-panorama-install/tombstones",
	} {
		if err := os.Mkdir(filepath.Join(root, relative), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func aiPanoramaTestOpenJournal(
	t *testing.T,
	root string,
	key ed25519.PrivateKey,
) *Journal {
	t.Helper()
	journal, err := OpenJournal(root, key)
	if err != nil {
		t.Fatal(err)
	}
	return journal
}

func aiPanoramaTestGenesisBase() map[string]any {
	return map[string]any{
		"run_id": "12345", "run_attempt": json.Number("1"),
		"request_id": "genesis-test-request", "oidc_jti": "genesis-test-jti",
	}
}

func aiPanoramaTestGenesisFileCount(t *testing.T, root string) int {
	t.Helper()
	count := 0
	for _, path := range []string{
		aiPanoramaLedgerPath, aiPanoramaLedgerLockPath,
		aiPanoramaOperationPath, aiPanoramaOperationLockPath,
	} {
		if _, err := os.Lstat(rooted(root, path)); err == nil {
			count++
		} else if !os.IsNotExist(err) {
			t.Fatal(err)
		}
	}
	return count
}

func aiPanoramaTestCompletedGenesis(
	t *testing.T,
) (string, *Journal, ed25519.PrivateKey) {
	t.Helper()
	root := aiPanoramaTestGenesisRoot(t)
	_, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	journal := aiPanoramaTestOpenJournal(t, root, key)
	if err := ensureAiPanoramaStateGenesis(
		root, journal, aiPanoramaTestGenesisBase(),
	); err != nil {
		journal.Close()
		zero(key)
		t.Fatal(err)
	}
	return root, journal, key
}

func TestAiPanoramaStateGenesisCrashRecoveryUsesIntentBoundBytes(t *testing.T) {
	crash := errors.New("simulated-genesis-crash")
	type faultCase struct {
		fileIndex     int
		boundary      string
		expectedFiles int
	}
	cases := []faultCase{{
		fileIndex: 0, boundary: aiPanoramaGenesisBoundaryIntent, expectedFiles: 0,
	}}
	for fileIndex := 1; fileIndex <= 4; fileIndex++ {
		for _, item := range []struct {
			boundary      string
			expectedFiles int
		}{
			{aiPanoramaGenesisBoundaryMidWrite, fileIndex - 1},
			{aiPanoramaGenesisBoundaryWritten, fileIndex - 1},
			{aiPanoramaGenesisBoundaryFileSync, fileIndex - 1},
			{aiPanoramaGenesisBoundaryLinked, fileIndex},
			{aiPanoramaGenesisBoundaryDirSync, fileIndex},
			{aiPanoramaGenesisBoundaryComplete, fileIndex},
		} {
			cases = append(cases, faultCase{
				fileIndex: fileIndex, boundary: item.boundary,
				expectedFiles: item.expectedFiles,
			})
		}
	}
	for _, fault := range cases {
		t.Run(fault.boundary+"-"+strconv.Itoa(fault.fileIndex), func(t *testing.T) {
			root := aiPanoramaTestGenesisRoot(t)
			_, key, err := ed25519.GenerateKey(rand.Reader)
			if err != nil {
				t.Fatal(err)
			}
			defer zero(key)
			journal := aiPanoramaTestOpenJournal(t, root, key)
			err = ensureAiPanoramaStateGenesisWithHook(
				root, journal, aiPanoramaTestGenesisBase(),
				func(fileIndex int, boundary string) error {
					if fileIndex == fault.fileIndex && boundary == fault.boundary {
						return crash
					}
					return nil
				},
			)
			if !errors.Is(err, crash) {
				journal.Close()
				t.Fatalf(
					"crash boundary %s/%d was not reached: %v",
					fault.boundary, fault.fileIndex, err,
				)
			}
			if got := aiPanoramaTestGenesisFileCount(t, root); got != fault.expectedFiles {
				journal.Close()
				t.Fatalf(
					"crash boundary %s/%d left %d files, want %d",
					fault.boundary, fault.fileIndex, got, fault.expectedFiles,
				)
			}
			if len(journal.events) != 1 ||
				journal.events[0].EventType != aiPanoramaStateGenesisIntentEvent {
				journal.Close()
				t.Fatal("durable genesis intent was not the only journal event")
			}
			intentReceipt := journal.events[0].ReceiptDigest
			journal.Close()
			journal = aiPanoramaTestOpenJournal(t, root, key)
			if err := ensureAiPanoramaStateGenesis(
				root, journal, aiPanoramaTestGenesisBase(),
			); err != nil {
				journal.Close()
				t.Fatalf("intent replay failed: %v", err)
			}
			genesis, completed, err := aiPanoramaStateGenesisFromEvent(journal)
			if err != nil || genesis == nil || !completed {
				journal.Close()
				t.Fatalf("replayed genesis did not complete: %#v %t %v", genesis, completed, err)
			}
			if genesis.IntentReceiptDigest != intentReceipt ||
				validateAiPanoramaStateGenesis(root, genesis) != nil ||
				len(journal.events) != 2 {
				genesis.release()
				journal.Close()
				t.Fatal("replay did not preserve the exact intent-bound state")
			}
			genesis.release()
			if err := ensureAiPanoramaStateGenesis(
				root, journal, aiPanoramaTestGenesisBase(),
			); err != nil || len(journal.events) != 2 {
				journal.Close()
				t.Fatalf("completed restart was not idempotent: %v", err)
			}
			journal.Close()
		})
	}
}

func TestAiPanoramaStateGenesisRejectsDeletionReplacementAndPartial(t *testing.T) {
	for _, path := range []string{
		aiPanoramaLedgerPath, aiPanoramaLedgerLockPath,
		aiPanoramaOperationPath, aiPanoramaOperationLockPath,
	} {
		t.Run("deleted-"+filepath.Base(path), func(t *testing.T) {
			root, journal, key := aiPanoramaTestCompletedGenesis(t)
			defer zero(key)
			defer journal.Close()
			if err := os.Remove(rooted(root, path)); err != nil {
				t.Fatal(err)
			}
			if err := ensureAiPanoramaStateGenesis(
				root, journal, aiPanoramaTestGenesisBase(),
			); err == nil {
				t.Fatal("deleted completed genesis file was recreated or accepted")
			}
			if _, err := os.Lstat(rooted(root, path)); !os.IsNotExist(err) {
				t.Fatal("deleted completed genesis file was recreated")
			}
		})
		t.Run("replaced-"+filepath.Base(path), func(t *testing.T) {
			root, journal, key := aiPanoramaTestCompletedGenesis(t)
			defer zero(key)
			defer journal.Close()
			replacement := []byte("replaced\n")
			if err := os.Remove(rooted(root, path)); err != nil ||
				os.WriteFile(rooted(root, path), replacement, 0o600) != nil {
				t.Fatal(err)
			}
			if err := ensureAiPanoramaStateGenesis(
				root, journal, aiPanoramaTestGenesisBase(),
			); err == nil {
				t.Fatal("replaced completed genesis file was accepted")
			}
			raw, err := os.ReadFile(rooted(root, path))
			if err != nil || string(raw) != string(replacement) {
				t.Fatal("replaced completed genesis file was regenerated")
			}
		})
	}
	t.Run("partial-before-intent", func(t *testing.T) {
		root := aiPanoramaTestGenesisRoot(t)
		_, key, err := ed25519.GenerateKey(rand.Reader)
		if err != nil {
			t.Fatal(err)
		}
		defer zero(key)
		journal := aiPanoramaTestOpenJournal(t, root, key)
		defer journal.Close()
		if err := os.WriteFile(
			rooted(root, aiPanoramaLedgerLockPath), []byte("lock\n"), 0o600,
		); err != nil {
			t.Fatal(err)
		}
		if err := ensureAiPanoramaStateGenesis(
			root, journal, aiPanoramaTestGenesisBase(),
		); err == nil {
			t.Fatal("partial preexisting genesis accepted")
		}
	})
	t.Run("root-replaced", func(t *testing.T) {
		root, journal, key := aiPanoramaTestCompletedGenesis(t)
		defer zero(key)
		defer journal.Close()
		if err := os.Chmod(rooted(root, aiPanoramaControlRoot), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := ensureAiPanoramaStateGenesis(
			root, journal, aiPanoramaTestGenesisBase(),
		); err == nil {
			t.Fatal("replaced genesis root accepted")
		}
	})
	t.Run("completed-temporary-alias", func(t *testing.T) {
		root, journal, key := aiPanoramaTestCompletedGenesis(t)
		defer zero(key)
		defer journal.Close()
		genesis, completed, err := aiPanoramaStateGenesisFromEvent(journal)
		if err != nil || genesis == nil || !completed {
			t.Fatal("completed genesis fixture invalid")
		}
		defer genesis.release()
		file := genesis.Files[0]
		if err := os.WriteFile(
			rooted(root, file.TemporaryPath), file.Raw, file.Mode,
		); err != nil {
			t.Fatal(err)
		}
		if err := ensureAiPanoramaStateGenesis(
			root, journal, aiPanoramaTestGenesisBase(),
		); err == nil {
			t.Fatal("post-completion temporary leaf was accepted or removed")
		}
		if _, err := os.Lstat(rooted(root, file.TemporaryPath)); err != nil {
			t.Fatal("post-completion temporary leaf was mutated")
		}
	})
}
