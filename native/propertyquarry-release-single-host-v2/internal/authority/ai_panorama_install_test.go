//go:build linux && amd64

package authority

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"
)

func aiPanoramaTestConfig() *Config {
	return &Config{
		Digest:       "sha256:" + strings.Repeat("1", 64),
		PlanDigest:   "sha256:" + strings.Repeat("2", 64),
		RuntimeSHA:   strings.Repeat("3", 40),
		WorkflowSHA:  strings.Repeat("4", 40),
		EnvelopeSHA:  strings.Repeat("5", 64),
		DeploymentID: strings.Repeat("6", 64),
		WebImage:     "ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime@sha256:" + strings.Repeat("7", 64),
		RenderImage:  "ghcr.io/archonmegalon/propertyquarry-standalone-render-runtime@sha256:" + strings.Repeat("8", 64),
	}
}

func TestAiPanoramaOperationIsClosedAndRecoveryRequiredIsNonterminal(t *testing.T) {
	if !validWorkflowOperation(aiPanoramaInstallOperation) {
		t.Fatal("closed operation is not accepted by protocol parser")
	}
	requestID, err := requestIDForRun(aiPanoramaInstallOperation, "123", 2)
	if err != nil || requestID != "ai-panorama-install-123-2" {
		t.Fatalf("unexpected request ID: %q %v", requestID, err)
	}
	if terminalEvent(aiPanoramaInstallRecoveryRequiredEvent) {
		t.Fatal("recovery-required was made terminal")
	}
	if !terminalEvent(aiPanoramaInstallSucceededEvent) ||
		!terminalEvent(aiPanoramaInstallRolledBackEvent) ||
		!terminalEvent(aiPanoramaInstallFailedNoEffectsEvent) {
		t.Fatal("ai panorama terminal set is incomplete")
	}
}

func TestAiPanoramaContainerArgumentsHaveNoCallerOrPublicEgressSurface(t *testing.T) {
	config := aiPanoramaTestConfig()
	runtime := &aiPanoramaRuntimeObservation{
		ImageID:            "sha256:" + strings.Repeat("9", 64),
		PublicVolumeDevice: 12, PublicVolumeInode: 34,
	}
	sealed := &aiPanoramaSealedArtifactObservation{
		TreeSHA256: aiPanoramaExpectedSourceTree, TourSHA256: aiPanoramaExpectedTourDigest,
	}
	network := &aiPanoramaNetworkObservation{
		Name: "pq-ai-panorama-prater-" + config.DeploymentID[:16],
		ID:   strings.Repeat("a", 64), DBAttached: true,
	}
	preflight, err := aiPanoramaContainerArguments(config, runtime, sealed, nil, "preflight", "")
	if err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(preflight, "\x1f")
	for _, forbidden := range []string{
		"/docker/property/state", "/var/run/docker.sock", "property_default",
		"--privileged", "--pid=host", "--network=host",
	} {
		if strings.Contains(joined, forbidden) {
			t.Fatalf("forbidden container surface present: %s", forbidden)
		}
	}
	for _, required := range []string{
		"--network\x1fnone",
		"--read-only", "--cap-drop\x1fALL",
		aiPanoramaBindMount(aiPanoramaSealedArtifactRoot, aiPanoramaSealedArtifactRoot, true),
		aiPanoramaBindMount(aiPanoramaControlRoot, aiPanoramaControlRoot, true),
		aiPanoramaVolumeMount(aiPanoramaPublicVolumeName, aiPanoramaPublicMountTarget, true),
		"--entrypoint\x1f" + aiPanoramaControllerPython + "\x1f" +
			config.WebImage + "\x1f-I\x1f-B\x1f" + aiPanoramaPreflightEntrypoint,
	} {
		if !strings.Contains(joined, required) {
			t.Fatalf("required container contract missing: %s", required)
		}
	}
	apply, err := aiPanoramaContainerArguments(
		config, runtime, sealed, network, "apply", aiPanoramaDatabaseSecretMount,
	)
	if err != nil {
		t.Fatal(err)
	}
	applyJoined := strings.Join(apply, "\x1f")
	if !strings.Contains(applyJoined, aiPanoramaBindMount(aiPanoramaControlRoot, aiPanoramaControlRoot, false)) ||
		!strings.Contains(applyJoined, aiPanoramaVolumeMount(aiPanoramaPublicVolumeName, aiPanoramaPublicMountTarget, false)) ||
		!strings.Contains(applyJoined, "--cap-add\x1fCHOWN\x1f--cap-add\x1fDAC_OVERRIDE\x1f--cap-add\x1fFOWNER") {
		t.Fatal("apply phase lacks separately fenced RW mounts")
	}
	if strings.Contains(applyJoined, aiPanoramaLegacyPublicVolumeName) ||
		strings.Contains(applyJoined, aiPanoramaLegacyVolumeComposeKey) {
		t.Fatal("legacy dynamic public-tour volume entered governed operation")
	}
}

func TestAiPanoramaSourceSnapshotMatchesCanonicalFileManifest(t *testing.T) {
	root := t.TempDir()
	if err := os.Chmod(root, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(root, "panoramas"), 0o700); err != nil {
		t.Fatal(err)
	}
	files := map[string][]byte{
		"tour.json":         []byte(`{"slug":"fixture"}`),
		"panoramas/one.jpg": []byte("fixture-panorama"),
	}
	for path, content := range files {
		full := filepath.Join(root, filepath.FromSlash(path))
		if err := os.WriteFile(full, content, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	snapshot, err := snapshotAiPanoramaSource(root, uint32(os.Geteuid()), uint32(os.Getegid()), 0o700, 0o600, true)
	if err != nil {
		t.Fatal(err)
	}
	defer snapshot.release()
	rows := make([]any, 0, len(files))
	paths := []string{"panoramas/one.jpg", "tour.json"}
	for _, path := range paths {
		sum := sha256Bytes(files[path])
		rows = append(rows, map[string]any{
			"relpath": path, "sha256": sum,
			"size_bytes": json.Number(strconv.Itoa(len(files[path]))),
		})
	}
	raw, _ := canonicalJSON(rows)
	sum := sha256Bytes(raw)
	zero(raw)
	if snapshot.TreeSHA256 != sum || snapshot.TourSHA256 != sha256Bytes(files["tour.json"]) ||
		len(snapshot.Files) != 2 || snapshot.TotalBytes != int64(len(files["tour.json"])+len(files["panoramas/one.jpg"])) {
		t.Fatalf("unexpected snapshot: %#v", snapshot)
	}
	if err := os.WriteFile(filepath.Join(root, ".hidden"), []byte("forbidden"), 0o600); err != nil {
		t.Fatal(err)
	}
	if invalid, err := snapshotAiPanoramaSource(root, uint32(os.Geteuid()), uint32(os.Getegid()), 0o700, 0o600, false); err == nil {
		invalid.release()
		t.Fatal("hidden uncontracted content accepted")
	}
}

func sha256Bytes(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

func TestAiPanoramaPublicVolumeConsumersMustAllBeReadOnly(t *testing.T) {
	previous := executeAiPanoramaDocker
	readWrite := false
	consumerIDs := map[string]string{
		strings.Repeat("a", 64): aiPanoramaAPIRuntimeService,
		strings.Repeat("b", 64): aiPanoramaRenderService,
		strings.Repeat("c", 64): aiPanoramaSchedulerService,
	}
	executeAiPanoramaDocker = func(_ context.Context, _ string, arguments ...string) ([]byte, error) {
		joined := strings.Join(arguments, " ")
		if strings.Contains(joined, "container ls") {
			rows := make([]string, 0, len(consumerIDs))
			for id, service := range consumerIDs {
				rows = append(rows, id+"|"+ProjectName+"|"+service)
			}
			sort.Strings(rows)
			return []byte(strings.Join(rows, "\n") + "\n"), nil
		}
		if strings.Contains(joined, "container inspect") {
			id := arguments[len(arguments)-1]
			service := consumerIDs[id]
			if strings.Contains(joined, "{{.Id}}|{{.Name}}") {
				return []byte(
					id + "|/current-" + service + "|sha256:" + strings.Repeat("d", 64) +
						"|example.invalid/runtime@sha256:" + strings.Repeat("e", 64) +
						"|true|" + ProjectName + "|" + service + "\n",
				), nil
			}
			if strings.Contains(joined, "EA_GOVERNED_PUBLIC_TOUR_DIR") {
				return []byte("EA_GOVERNED_PUBLIC_TOUR_DIR=" + aiPanoramaPublicMountTarget + "\n"), nil
			}
			value := map[string]any{
				"Destination": aiPanoramaPublicMountTarget,
				"Driver":      "local", "Mode": "z",
				"Name":        aiPanoramaPublicVolumeName,
				"Propagation": "", "RW": readWrite,
				"Source": "/var/lib/docker/volumes/" + aiPanoramaPublicVolumeName + "/_data",
				"Type":   "volume",
			}
			raw, _ := canonicalJSON(value)
			return append(raw, '\n'), nil
		}
		return nil, nil
	}
	t.Cleanup(func() { executeAiPanoramaDocker = previous })
	mountpoint := "/var/lib/docker/volumes/" + aiPanoramaPublicVolumeName + "/_data"
	if err := validateAiPanoramaPublicVolumeConsumers(
		context.Background(), mountpoint, strings.Repeat("b", 64),
	); err != nil {
		t.Fatal(err)
	}
	readWrite = true
	if err := validateAiPanoramaPublicVolumeConsumers(
		context.Background(), mountpoint, strings.Repeat("b", 64),
	); err == nil {
		t.Fatal("RW application mount accepted")
	}
}

func TestAiPanoramaPermitCanonicalJSONMatchesPythonAndExactSignatureFraming(t *testing.T) {
	value := map[string]any{"a": "München", "z": "<>&"}
	raw, err := aiPanoramaCanonicalJSON(value)
	if err != nil {
		t.Fatal(err)
	}
	if string(raw) != `{"a":"M\u00fcnchen","z":"<>&"}` {
		t.Fatalf("unexpected Python-compatible canonical JSON: %q", raw)
	}

	seed := bytes.Repeat([]byte{0x42}, ed25519.SeedSize)
	private := ed25519.NewKeyFromSeed(seed)
	zero(seed)
	root := t.TempDir()
	for _, relative := range []string{
		"etc", "etc/propertyquarry", "etc/propertyquarry/release-control",
		"var", "var/lib", "var/lib/propertyquarry", "var/lib/propertyquarry/release-control",
		"var/lib/propertyquarry/release-control/ai-panorama-install",
		"var/lib/propertyquarry/release-control/ai-panorama-install/permits",
	} {
		if err := os.Mkdir(filepath.Join(root, relative), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	public := private.Public().(ed25519.PublicKey)
	keyring := map[string]any{
		"schema": aiPanoramaKeyringSchema, "version": json.Number("1"),
		"authority": "propertyquarry-release-control", "algorithm": "Ed25519",
		"status": "active", "usage": aiPanoramaPermitKeyUsage,
		"rotation_epoch": json.Number("1"), "minimum_accepted_epoch": json.Number("1"),
		"keys": []any{map[string]any{
			"key_id": "release-control-test-key", "epoch": json.Number("1"),
			"usage":             aiPanoramaPermitKeyUsage,
			"public_key":        base64.RawURLEncoding.EncodeToString(public),
			"public_key_sha256": aiPanoramaRawSHA256(public),
			"activates_at":      "2026-07-24T07:00:00Z",
			"accept_until":      nil, "revoked_at": nil,
		}},
	}
	keyringRaw, err := canonicalJSON(keyring)
	if err != nil {
		t.Fatal(err)
	}
	keyringRaw = append(keyringRaw, '\n')
	keyringPath := rooted(root, aiPanoramaPurposeKeyringPath)
	if err := os.WriteFile(keyringPath, keyringRaw, 0o444); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(keyringPath, 0o444); err != nil {
		t.Fatal(err)
	}
	keyringInfo, err := os.Lstat(keyringPath)
	if err != nil {
		t.Fatal(err)
	}
	probe, err := secureRead(
		root, aiPanoramaPurposeKeyringPath, 0o444,
		uint32(os.Geteuid()), uint32(os.Getegid()), aiPanoramaMaximumKeyring,
	)
	if err != nil {
		t.Fatalf(
			"test keyring does not meet secure storage contract: %v mode=%#o size=%d",
			err, keyringInfo.Mode().Perm(), keyringInfo.Size(),
		)
	}
	zero(probe)
	issued := time.Date(2026, 7, 24, 8, 0, 0, 0, time.UTC)
	permit := map[string]any{
		"audience": "propertyquarry-ai-panorama-install-controller",
		"issuer":   "propertyquarry-release-control", "operation": aiPanoramaInstallOperation,
		"subject":            ImmutableOIDCSubjectPrefix + ":environment:" + Environment,
		"actor_principal_id": "propertyquarry-release-controller",
		"owner_principal_id": "owner@example.invalid",
		"search_run_id":      "98bed75e984549c6bd4371d602662ab8",
		"candidate_ref":      "053ad185e1c44b2e", "external_id": "1807240910",
		"listing_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/naehe-prater-und-messe-wien-i-u1-u2-i-ruhelage-i-garage-i-maisonette-i-voll-moebliert-i-in-der-vorgartenstrasse-1807240910/",
		"source_ref":  "property-scout:1807240910", "provider_key": "willhaben",
		"expected_slug":                           aiPanoramaPraterSlug,
		"expected_source_tree_sha256":             aiPanoramaExpectedSourceTree,
		"expected_tour_sha256":                    aiPanoramaExpectedTourDigest,
		"expected_core_manifest_sha256":           aiPanoramaExpectedCoreDigest,
		"expected_materialization_receipt_sha256": aiPanoramaExpectedReceiptDigest,
		"expected_candidate_marker_sha256":        aiPanoramaExpectedMarkerDigest,
		"expected_publication_record_sha256":      strings.Repeat("6", 64),
		"artifact_relpath":                        "bundle/" + aiPanoramaPraterSlug,
		"materialization_receipt_relpath":         "materialization.receipt.json",
		"request_id":                              strings.Repeat("7", 32), "repository": Repository,
		"git_ref": "refs/heads/main", "git_head_sha": strings.Repeat("8", 40),
		"workflow_ref": WorkflowRef, "job": ReleaseJob, "environment": Environment,
		"review_receipt_sha256": strings.Repeat("9", 64),
		"web_image":             "ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime@sha256:" + strings.Repeat("e", 64),
		"web_image_id":          "sha256:" + strings.Repeat("f", 64),
		"key_usage":             aiPanoramaPermitKeyUsage, "key_epoch": json.Number("1"),
		"key_sha256":            aiPanoramaRawSHA256(public),
		"keyring_sha256":        aiPanoramaRawSHA256(keyringRaw),
		"volume_profile_sha256": strings.Repeat("a", 64),
		"compose_plan_sha256":   strings.Repeat("b", 64),
		"volume_id":             aiPanoramaVolumeID,
		"artifact_root_device":  json.Number("2049"), "artifact_root_inode": json.Number("10001"),
		"public_tour_root_device": json.Number("2050"), "public_tour_root_inode": json.Number("10002"),
		"execution_lease_seconds": json.Number("600"),
		"issued_at":               "2026-07-24T08:00:00Z", "expires_at": "2026-07-24T08:03:00Z",
		"nonce": strings.Repeat("c", 32),
	}
	wire, observation, err := signAiPanoramaPermit(root, permit, private, issued, issued)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(wire)
	if observation.SHA256 != aiPanoramaRawSHA256(wire) ||
		observation.KeyringSHA256 != aiPanoramaRawSHA256(keyringRaw) {
		t.Fatal("signed permit evidence is not byte-bound")
	}
	envelope, err := decodedJSONObject(bytes.TrimSuffix(wire, []byte{'\n'}), aiPanoramaMaximumPermit)
	if err != nil {
		t.Fatal(err)
	}
	signatureValue := envelope["signature"].(map[string]any)["value"].(string)
	signature, err := base64.RawURLEncoding.DecodeString(signatureValue)
	if err != nil {
		t.Fatal(err)
	}
	body := map[string]any{
		"domain": aiPanoramaPermitSignatureDomain, "schema": aiPanoramaPermitSchema,
		"version": json.Number("2"), "permit": permit,
		"signature_context": map[string]any{
			"algorithm": "Ed25519", "key_id": "release-control-test-key", "encoding": "base64url",
		},
	}
	bodyRaw, _ := aiPanoramaCanonicalJSON(body)
	preimage := append([]byte(aiPanoramaPermitSignatureDomain+"\x00"), make([]byte, 8)...)
	binary.BigEndian.PutUint64(preimage[len(preimage)-8:], uint64(len(bodyRaw)))
	preimage = append(preimage, bodyRaw...)
	if !ed25519.Verify(public, preimage, signature) ||
		observation.PreimageSHA256 != aiPanoramaRawSHA256(preimage) {
		t.Fatal("permit signature framing mismatch")
	}
	zero(preimage)
	zero(bodyRaw)
	zero(signature)
	if err := persistAiPanoramaPermit(root, wire, observation); err != nil {
		t.Fatal(err)
	}
	persisted, err := os.ReadFile(rooted(root, observation.Path))
	if err != nil {
		t.Fatal(err)
	}
	defer zero(persisted)
	if !bytes.Equal(persisted, wire) {
		t.Fatal("persisted permit differs from signed bytes")
	}

	goldenPermit := cloneFields(permit)
	goldenPermit["owner_principal_id"] = "golden-owner@example.invalid"
	goldenPermit["expected_source_tree_sha256"] = strings.Repeat("1", 64)
	goldenPermit["expected_tour_sha256"] = strings.Repeat("2", 64)
	goldenPermit["expected_core_manifest_sha256"] = strings.Repeat("3", 64)
	goldenPermit["expected_materialization_receipt_sha256"] = strings.Repeat("4", 64)
	goldenPermit["expected_candidate_marker_sha256"] = strings.Repeat("5", 64)
	goldenPermit["key_epoch"] = json.Number("7")
	goldenPermit["key_sha256"] = "56475aa75463474c0285df5dbf2bcab73da651358839e9b77481b2eab107708c"
	goldenPermit["keyring_sha256"] = strings.Repeat("d", 64)
	goldenPermit["listing_url"] = "https://www.willhaben.at/iad/immobilien/d/1807240910/"
	goldenPermit["volume_id"] = "propertyquarry-public-tours-production"
	goldenBody := map[string]any{
		"domain": aiPanoramaPermitSignatureDomain, "schema": aiPanoramaPermitSchema,
		"version": json.Number("2"), "permit": goldenPermit,
		"signature_context": map[string]any{
			"algorithm": "Ed25519", "key_id": "release-control-golden-key", "encoding": "base64url",
		},
	}
	goldenBodyRaw, err := aiPanoramaCanonicalJSON(goldenBody)
	if err != nil {
		t.Fatal(err)
	}
	goldenPreimage := append([]byte(aiPanoramaPermitSignatureDomain+"\x00"), make([]byte, 8)...)
	binary.BigEndian.PutUint64(goldenPreimage[len(goldenPreimage)-8:], uint64(len(goldenBodyRaw)))
	goldenPreimage = append(goldenPreimage, goldenBodyRaw...)
	goldenPublic, err := base64.RawURLEncoding.DecodeString("A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg")
	if err != nil {
		t.Fatal(err)
	}
	goldenSignature, err := base64.RawURLEncoding.DecodeString("R_aV-vw_iwQQacGByyVZa8qiRK1L_gZYvTjh-loB_G2wT79_qlOvr_vSwJdX2C-zlY2Sn-wv7OryVWZ57DobAw")
	if err != nil {
		t.Fatal(err)
	}
	if aiPanoramaRawSHA256(goldenPreimage) != "3cb0f4a155dd9b86d8852b13f9275200e6658200ee7b6ac568a3a2b31b390125" ||
		!ed25519.Verify(ed25519.PublicKey(goldenPublic), goldenPreimage, goldenSignature) {
		t.Fatal("independent Python golden vector does not match native signature protocol")
	}
	goldenPermit["owner_principal_id"] = "München"
	if err := validateAiPanoramaPermitForSigning(goldenPermit, &aiPanoramaPurposeKey{
		KeyID: "release-control-golden-key", Epoch: 7,
		PublicSHA256:   "56475aa75463474c0285df5dbf2bcab73da651358839e9b77481b2eab107708c",
		KeyringSHA256:  strings.Repeat("d", 64),
		ActivationTime: time.Date(2026, 7, 24, 7, 0, 0, 0, time.UTC),
	}, issued); err == nil {
		t.Fatal("non-ASCII owner principal accepted despite cross-language grammar")
	}
	zero(goldenBodyRaw)
	zero(goldenPreimage)
	zero(goldenPublic)
	zero(goldenSignature)
}
