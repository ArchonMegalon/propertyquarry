//go:build linux && amd64

package authority

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

func aiPanoramaRecoveryTestRuntime() *aiPanoramaRuntimeObservation {
	return &aiPanoramaRuntimeObservation{
		DockerRoot:              "/var/lib/docker",
		ImageID:                 "sha256:" + strings.Repeat("a", 64),
		ControlRootDevice:       71,
		ControlRootInode:        73,
		PublicVolumeMountpoint:  "/var/lib/docker/volumes/" + aiPanoramaPublicVolumeName + "/_data",
		PublicVolumeDevice:      79,
		PublicVolumeInode:       83,
		PublicVolumeUID:         10001,
		PublicVolumeGID:         10001,
		PublicVolumeMode:        0o755,
		DatabaseContainerID:     strings.Repeat("1", 64),
		DatabaseContainerName:   "propertyquarry-db-1",
		DatabaseImageID:         "sha256:" + strings.Repeat("b", 64),
		APIRuntimeContainerID:   strings.Repeat("2", 64),
		APIRuntimeContainerName: "propertyquarry-api-1",
		APIRuntimeImageID:       "sha256:" + strings.Repeat("a", 64),
		SchedulerContainerID:    strings.Repeat("3", 64),
		SchedulerContainerName:  "propertyquarry-scheduler-1",
		SchedulerImageID:        "sha256:" + strings.Repeat("a", 64),
	}
}

func aiPanoramaRecoveryTestContainer(
	t *testing.T,
	config *Config,
	runtime *aiPanoramaRuntimeObservation,
	phase string,
	network string,
) map[string]any {
	t.Helper()
	name, err := aiPanoramaContainerName(config, phase)
	if err != nil {
		t.Fatal(err)
	}
	mountContract, err := aiPanoramaRecoveryMountContract(runtime, phase)
	if err != nil {
		t.Fatal(err)
	}
	mounts := make([]any, 0, len(mountContract))
	for _, expected := range mountContract {
		value := map[string]any{
			"Type": expected.Type, "Source": expected.Source,
			"Destination": expected.Destination, "RW": expected.ReadWrite,
		}
		if expected.Type == "bind" {
			value["Propagation"] = "rprivate"
		} else {
			value["Name"] = expected.Name
			value["Driver"] = "local"
		}
		mounts = append(mounts, value)
	}
	entrypoint := aiPanoramaControllerEntrypoint
	capabilities := []any{}
	switch phase {
	case "discover":
		entrypoint = aiPanoramaDiscoveryEntrypoint
	case "preflight":
		entrypoint = aiPanoramaPreflightEntrypoint
	case "apply":
		capabilities = []any{"CHOWN", "DAC_OVERRIDE", "FOWNER"}
	case "closeout":
		entrypoint = aiPanoramaCloseoutEntrypoint
		capabilities = []any{"DAC_OVERRIDE"}
	}
	return map[string]any{
		"Id": strings.Repeat("4", 64), "Name": "/" + name,
		"Image": runtime.ImageID, "Path": aiPanoramaControllerPython,
		"Args": []any{"-I", "-B", entrypoint},
		"Config": map[string]any{
			"Image": config.WebImage, "User": "0:0",
			"Labels": map[string]any{
				aiPanoramaNetworkLabel:                         "v1",
				"propertyquarry.release-control.config-digest": config.Digest,
				"propertyquarry.release-control.deployment-id": config.DeploymentID,
				"propertyquarry.release-control.operation":     aiPanoramaOperationForPhase(phase),
				"propertyquarry.release-control.phase":         phase,
			},
		},
		"HostConfig": map[string]any{
			"NetworkMode": network, "Privileged": false,
			"ReadonlyRootfs": true, "PidMode": "", "AutoRemove": true,
			"LogConfig":     map[string]any{"Type": "none"},
			"RestartPolicy": map[string]any{"Name": "no"},
			"CapAdd":        capabilities, "CapDrop": []any{"ALL"},
			"SecurityOpt": []any{"no-new-privileges:true"},
			"PidsLimit":   json.Number("64"),
			"Memory":      json.Number("536870912"),
			"MemorySwap":  json.Number("536870912"),
			"NanoCpus":    json.Number("1000000000"),
			"Tmpfs": map[string]any{
				"/tmp": "rw,nosuid,nodev,noexec,size=67108864,mode=1777",
			},
		},
		"Mounts": mounts,
	}
}

func aiPanoramaRecoveryTestDocker(
	t *testing.T,
	container map[string]any,
	phaseName string,
	networkName string,
	networkExists bool,
) (*bool, *int) {
	t.Helper()
	exists := true
	removals := 0
	previous := executeAiPanoramaDocker
	t.Cleanup(func() { executeAiPanoramaDocker = previous })
	executeAiPanoramaDocker = func(
		ctx context.Context,
		_ string,
		arguments ...string,
	) ([]byte, error) {
		if ctx == nil || ctx.Err() != nil {
			return nil, fmt.Errorf("cleanup context was cancelled")
		}
		joined := strings.Join(arguments, " ")
		switch {
		case strings.Contains(joined, "container ls"):
			if exists && strings.Contains(
				joined, "name=^/"+phaseName+"$",
			) {
				return []byte(phaseName + "\n"), nil
			}
			return []byte{}, nil
		case strings.Contains(joined, "container inspect"):
			raw, err := canonicalJSON(container)
			return append(raw, '\n'), err
		case strings.Contains(joined, "container rm --force"):
			exists = false
			removals++
			return []byte(phaseName + "\n"), nil
		case strings.Contains(joined, "network ls"):
			if networkExists {
				return []byte(networkName + "\n"), nil
			}
			return []byte{}, nil
		default:
			return nil, fmt.Errorf("unexpected Docker command: %s", joined)
		}
	}
	return &exists, &removals
}

func aiPanoramaTestAttemptEvent(
	config *Config,
	releaseDigest string,
	sequence int64,
	retryOf string,
	receipt string,
) JournalEvent {
	return JournalEvent{
		EventType:     aiPanoramaInstallFailedNoEffectsEvent,
		Operation:     aiPanoramaInstallOperation,
		ReceiptDigest: receipt,
		Payload: map[string]any{
			"config_digest": config.Digest, "plan_digest": config.PlanDigest,
			"runtime_sha": config.RuntimeSHA, "workflow_sha": config.WorkflowSHA,
			"deployment_id":                                config.DeploymentID,
			"release_run_receipt_digest":                   releaseDigest,
			"ai_panorama_attempt_sequence":                 json.Number(fmt.Sprintf("%d", sequence)),
			"ai_panorama_retry_of_terminal_receipt_digest": retryOf,
		},
	}
}

func TestAiPanoramaAttemptLineageIsGlobalAndReleaseScoped(t *testing.T) {
	config := aiPanoramaTestConfig()
	releaseA := "sha256:" + strings.Repeat("a", 64)
	releaseB := "sha256:" + strings.Repeat("b", 64)
	receipt1 := "sha256:" + strings.Repeat("1", 64)
	receipt2 := "sha256:" + strings.Repeat("2", 64)
	receipt3 := "sha256:" + strings.Repeat("3", 64)
	journal := &Journal{events: []JournalEvent{
		aiPanoramaTestAttemptEvent(config, releaseA, 1, "genesis", receipt1),
		aiPanoramaTestAttemptEvent(config, releaseB, 1, receipt1, receipt2),
	}}
	lineage, prior, err := aiPanoramaAttemptLineageFor(journal, config, releaseA)
	if err != nil || lineage.Sequence != 2 || lineage.RetryOf != receipt2 ||
		prior == nil || prior.ReceiptDigest != receipt1 {
		t.Fatalf("cross-release predecessor was not preserved: %#v %#v %v", lineage, prior, err)
	}
	journal.events = append(
		journal.events,
		aiPanoramaTestAttemptEvent(config, releaseA, 2, receipt2, receipt3),
	)
	lineage, _, err = aiPanoramaAttemptLineageFor(journal, config, releaseA)
	if err != nil || lineage.Sequence != 3 || lineage.RetryOf != receipt3 {
		t.Fatalf("same-release sequence did not advance: %#v %v", lineage, err)
	}
	journal.events[2].Payload["ai_panorama_attempt_sequence"] = json.Number("1")
	if _, _, err := aiPanoramaAttemptLineageFor(
		journal, config, releaseA,
	); err == nil {
		t.Fatal("duplicate/rewound attempt sequence accepted")
	}
}

func TestAiPanoramaAttemptLineageCapsAtThirtyTwo(t *testing.T) {
	config := aiPanoramaTestConfig()
	release := "sha256:" + strings.Repeat("a", 64)
	journal := &Journal{}
	previous := "genesis"
	for sequence := int64(1); sequence <= aiPanoramaMaximumAttemptCount; sequence++ {
		receipt := "sha256:" + fmt.Sprintf("%064x", sequence)
		journal.events = append(
			journal.events,
			aiPanoramaTestAttemptEvent(
				config, release, sequence, previous, receipt,
			),
		)
		previous = receipt
	}
	if _, _, err := aiPanoramaAttemptLineageFor(
		journal, config, release,
	); err == nil {
		t.Fatal("attempt 33 was authorized")
	}
}

func TestUnresolvedWorkflowOperationsRetainsInstallAcrossCloseoutTerminal(
	t *testing.T,
) {
	journal := &Journal{events: []JournalEvent{
		{
			EventType: aiPanoramaInstallMutationStartedEvent,
			Operation: aiPanoramaInstallOperation,
			RequestID: strings.Repeat("1", 32), RunID: "install-run",
			RunAttempt: 1,
		},
		{
			EventType: aiPanoramaCloseoutPreparedEvent,
			Operation: aiPanoramaCloseoutOperation,
			RequestID: strings.Repeat("2", 32), RunID: "closeout-run",
			RunAttempt: 1,
		},
		{
			EventType: aiPanoramaCloseoutSucceededEvent,
			Operation: aiPanoramaCloseoutOperation,
			RequestID: strings.Repeat("2", 32), RunID: "closeout-run",
			RunAttempt: 1,
		},
	}}
	unresolved := unresolvedWorkflowOperations(journal)
	if len(unresolved) != 1 ||
		unresolved[0].Operation != aiPanoramaInstallOperation ||
		unresolved[0].RequestID != strings.Repeat("1", 32) {
		t.Fatalf("interleaved install was hidden: %#v", unresolved)
	}
	journal.events = append(journal.events, JournalEvent{
		EventType: aiPanoramaInstallFailedNoEffectsEvent,
		Operation: aiPanoramaInstallOperation,
		RequestID: strings.Repeat("1", 32), RunID: "install-run",
		RunAttempt: 1,
	})
	if unresolved = unresolvedWorkflowOperations(journal); len(unresolved) != 0 {
		t.Fatalf("terminal install remained unresolved: %#v", unresolved)
	}
}

func TestAiPanoramaCloseoutPreObservationRemovesExactApplyOrphan(
	t *testing.T,
) {
	config := aiPanoramaCloseoutTestConfig()
	runtime := aiPanoramaRecoveryTestRuntime()
	network, err := aiPanoramaNetworkName(config)
	if err != nil {
		t.Fatal(err)
	}
	name, err := aiPanoramaContainerName(config, "apply")
	if err != nil {
		t.Fatal(err)
	}
	container := aiPanoramaRecoveryTestContainer(
		t, config, runtime, "apply", network,
	)
	exists, removals := aiPanoramaRecoveryTestDocker(
		t, container, name, network, false,
	)
	root := t.TempDir()
	if err := os.MkdirAll(
		rooted(root, aiPanoramaRuntimeRoot), 0o700,
	); err != nil {
		t.Fatal(err)
	}
	receipt := "sha256:" + strings.Repeat("5", 64)
	request := &workflowRequest{
		Operation: aiPanoramaInstallOperation,
		RequestID: "ai-panorama-install-123-1",
	}
	identity := &Identity{RunID: "123", RunAttempt: 1}
	payload := authorityFields(config, request, identity)
	payload["release_effects_performed"] = true
	payload["ai_panorama_runtime_observation"] =
		aiPanoramaRuntimeObservationValue(runtime)
	journal := &Journal{events: []JournalEvent{{
		Sequence: 1, ReceiptDigest: receipt,
		EventType: aiPanoramaInstallMutationStartedEvent,
		Operation: aiPanoramaInstallOperation,
		RequestID: request.RequestID,
		RunID:     "123", RunAttempt: 1,
		Payload: payload,
	}}}
	installed := &aiPanoramaInstalledProof{
		ReceiptDigest: receipt, WebImage: config.WebImage,
		WebImageID: runtime.ImageID, DockerRoot: runtime.DockerRoot,
		PublicVolumeMountpoint: runtime.PublicVolumeMountpoint,
		PublicVolumeDevice:     runtime.PublicVolumeDevice,
		PublicVolumeInode:      runtime.PublicVolumeInode,
		Emergency:              true,
		BasisEventType:         aiPanoramaInstallMutationStartedEvent,
	}
	parent, cancel := context.WithCancel(context.Background())
	cancel()
	if err := cleanupAiPanoramaCloseoutOrphansBeforeObservation(
		parent, root, journal, config, installed, nil,
	); err != nil {
		t.Fatal(err)
	}
	if *exists || *removals != 1 {
		t.Fatalf(
			"exact apply orphan was not removed: exists=%t removals=%d",
			*exists, *removals,
		)
	}
}

func TestAiPanoramaCloseoutPreObservationRemovesExactPreflightOrphan(
	t *testing.T,
) {
	config := aiPanoramaCloseoutTestConfig()
	runtime := aiPanoramaRecoveryTestRuntime()
	name, err := aiPanoramaContainerName(config, "preflight")
	if err != nil {
		t.Fatal(err)
	}
	container := aiPanoramaRecoveryTestContainer(
		t, config, runtime, "preflight", "none",
	)
	exists, removals := aiPanoramaRecoveryTestDocker(
		t, container, name, "", false,
	)
	root := t.TempDir()
	if err := os.MkdirAll(
		rooted(root, aiPanoramaRuntimeRoot), 0o700,
	); err != nil {
		t.Fatal(err)
	}
	receipt := "sha256:" + strings.Repeat("6", 64)
	request := &workflowRequest{
		Operation: aiPanoramaInstallOperation,
		RequestID: "ai-panorama-install-124-1",
	}
	identity := &Identity{RunID: "124", RunAttempt: 1}
	payload := authorityFields(config, request, identity)
	payload["release_effects_performed"] = false
	payload["ai_panorama_runtime_observation"] =
		aiPanoramaRuntimeObservationValue(runtime)
	journal := &Journal{events: []JournalEvent{{
		Sequence: 1, ReceiptDigest: receipt,
		EventType: aiPanoramaInstallPreflightStartedEvent,
		Operation: aiPanoramaInstallOperation,
		RequestID: request.RequestID,
		RunID:     identity.RunID, RunAttempt: 1,
		Payload: payload,
	}}}
	installed := &aiPanoramaInstalledProof{
		ReceiptDigest: receipt, WebImage: config.WebImage,
		WebImageID: runtime.ImageID, DockerRoot: runtime.DockerRoot,
		PublicVolumeMountpoint: runtime.PublicVolumeMountpoint,
		PublicVolumeDevice:     runtime.PublicVolumeDevice,
		PublicVolumeInode:      runtime.PublicVolumeInode,
		Emergency:              true,
		BasisEventType:         aiPanoramaInstallPreflightStartedEvent,
	}
	parent, cancel := context.WithCancel(context.Background())
	cancel()
	if err := cleanupAiPanoramaCloseoutOrphansBeforeObservation(
		parent, root, journal, config, installed, nil,
	); err != nil {
		t.Fatal(err)
	}
	if *exists || *removals != 1 {
		t.Fatalf(
			"exact preflight orphan was not removed: exists=%t removals=%d",
			*exists, *removals,
		)
	}
}

func TestAiPanoramaCloseoutPreObservationUsesImmediatePredecessorConfig(
	t *testing.T,
) {
	current := aiPanoramaCloseoutTestConfig()
	current.ReleaseGeneration = 2
	current.PredecessorRuntimeSHA = strings.Repeat("9", 40)
	predecessor := *current
	predecessor.Digest = "sha256:" + strings.Repeat("8", 64)
	predecessor.PlanDigest = "sha256:" + strings.Repeat("7", 64)
	predecessor.RuntimeSHA = current.PredecessorRuntimeSHA
	predecessor.WorkflowSHA = strings.Repeat("6", 40)
	predecessor.DeploymentID = strings.Repeat("5", 64)
	predecessor.ReleaseGeneration = 1
	runtime := aiPanoramaRecoveryTestRuntime()
	network, err := aiPanoramaNetworkName(&predecessor)
	if err != nil {
		t.Fatal(err)
	}
	name, err := aiPanoramaContainerName(&predecessor, "apply")
	if err != nil {
		t.Fatal(err)
	}
	container := aiPanoramaRecoveryTestContainer(
		t, &predecessor, runtime, "apply", network,
	)
	exists, removals := aiPanoramaRecoveryTestDocker(
		t, container, name, network, false,
	)
	root := t.TempDir()
	if err := os.MkdirAll(
		rooted(root, aiPanoramaRuntimeRoot), 0o700,
	); err != nil {
		t.Fatal(err)
	}
	receipt := "sha256:" + strings.Repeat("4", 64)
	request := &workflowRequest{
		Operation: aiPanoramaInstallOperation,
		RequestID: "ai-panorama-install-125-1",
	}
	identity := &Identity{RunID: "125", RunAttempt: 1}
	payload := authorityFields(&predecessor, request, identity)
	payload["release_effects_performed"] = true
	payload["ai_panorama_runtime_observation"] =
		aiPanoramaRuntimeObservationValue(runtime)
	journal := &Journal{events: []JournalEvent{{
		Sequence: 1, ReceiptDigest: receipt,
		EventType: aiPanoramaInstallMutationStartedEvent,
		Operation: aiPanoramaInstallOperation,
		RequestID: request.RequestID,
		RunID:     identity.RunID, RunAttempt: 1,
		Payload: payload,
	}}}
	installed := &aiPanoramaInstalledProof{
		ReceiptDigest: receipt, WebImage: predecessor.WebImage,
		WebImageID: runtime.ImageID, DockerRoot: runtime.DockerRoot,
		PublicVolumeMountpoint: runtime.PublicVolumeMountpoint,
		PublicVolumeDevice:     runtime.PublicVolumeDevice,
		PublicVolumeInode:      runtime.PublicVolumeInode,
		Emergency:              true,
		BasisEventType:         aiPanoramaInstallMutationStartedEvent,
	}
	if err := cleanupAiPanoramaCloseoutOrphansBeforeObservation(
		context.Background(), root, journal, current, installed, nil,
	); err != nil {
		t.Fatal(err)
	}
	if *exists || *removals != 1 {
		t.Fatalf(
			"predecessor apply orphan was not removed: exists=%t removals=%d",
			*exists, *removals,
		)
	}
}

func TestAiPanoramaCloseoutPreObservationRejectsTooOldConfig(
	t *testing.T,
) {
	current := aiPanoramaCloseoutTestConfig()
	current.ReleaseGeneration = 3
	current.PredecessorRuntimeSHA = strings.Repeat("9", 40)
	old := *current
	old.Digest = "sha256:" + strings.Repeat("8", 64)
	old.PlanDigest = "sha256:" + strings.Repeat("7", 64)
	old.RuntimeSHA = current.PredecessorRuntimeSHA
	old.WorkflowSHA = strings.Repeat("6", 40)
	old.DeploymentID = strings.Repeat("5", 64)
	old.ReleaseGeneration = 1
	runtime := aiPanoramaRecoveryTestRuntime()
	network, err := aiPanoramaNetworkName(&old)
	if err != nil {
		t.Fatal(err)
	}
	name, err := aiPanoramaContainerName(&old, "apply")
	if err != nil {
		t.Fatal(err)
	}
	container := aiPanoramaRecoveryTestContainer(
		t, &old, runtime, "apply", network,
	)
	exists, removals := aiPanoramaRecoveryTestDocker(
		t, container, name, network, false,
	)
	receipt := "sha256:" + strings.Repeat("4", 64)
	request := &workflowRequest{
		Operation: aiPanoramaInstallOperation,
		RequestID: "ai-panorama-install-126-1",
	}
	identity := &Identity{RunID: "126", RunAttempt: 1}
	payload := authorityFields(&old, request, identity)
	payload["release_effects_performed"] = true
	payload["ai_panorama_runtime_observation"] =
		aiPanoramaRuntimeObservationValue(runtime)
	journal := &Journal{events: []JournalEvent{{
		Sequence: 1, ReceiptDigest: receipt,
		EventType: aiPanoramaInstallMutationStartedEvent,
		Operation: aiPanoramaInstallOperation,
		RequestID: request.RequestID,
		RunID:     identity.RunID, RunAttempt: 1,
		Payload: payload,
	}}}
	installed := &aiPanoramaInstalledProof{
		ReceiptDigest: receipt, WebImage: old.WebImage,
		WebImageID: runtime.ImageID, DockerRoot: runtime.DockerRoot,
		PublicVolumeMountpoint: runtime.PublicVolumeMountpoint,
		PublicVolumeDevice:     runtime.PublicVolumeDevice,
		PublicVolumeInode:      runtime.PublicVolumeInode,
		Emergency:              true,
		BasisEventType:         aiPanoramaInstallMutationStartedEvent,
	}
	if err := cleanupAiPanoramaCloseoutOrphansBeforeObservation(
		context.Background(), t.TempDir(), journal, current, installed, nil,
	); err == nil {
		t.Fatal("too-old cleanup basis was accepted")
	}
	if !*exists || *removals != 0 {
		t.Fatalf(
			"too-old orphan changed: exists=%t removals=%d",
			*exists, *removals,
		)
	}
}

func TestAiPanoramaCloseoutPreObservationRemovesExactCloseoutOrphan(
	t *testing.T,
) {
	config := aiPanoramaTestConfig()
	runtime := aiPanoramaRecoveryTestRuntime()
	name, err := aiPanoramaContainerName(config, "closeout")
	if err != nil {
		t.Fatal(err)
	}
	container := aiPanoramaRecoveryTestContainer(
		t, config, runtime, "closeout", "none",
	)
	exists, removals := aiPanoramaRecoveryTestDocker(
		t, container, name, "", false,
	)
	raw, err := aiPanoramaRevocationWire(
		strings.Repeat("6", 32),
		time.Date(2026, 7, 24, 12, 0, 0, 0, time.UTC),
	)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(raw)
	projection := &aiPanoramaProjection{
		Kind: "closeout-request", Path: aiPanoramaCloseoutRequestPath,
		Mode: 0o400, SHA256: aiPanoramaRawSHA256(raw), Raw: raw,
	}
	receipt := "sha256:" + strings.Repeat("7", 64)
	event := JournalEvent{
		Sequence: 1, ReceiptDigest: receipt,
		EventType: aiPanoramaCloseoutRequestPersisted,
		Operation: aiPanoramaCloseoutOperation,
		RequestID: "ai-panorama-closeout-123-1",
		RunID:     "123", RunAttempt: 1,
		Payload: map[string]any{
			"ai_panorama_runtime_observation":     aiPanoramaRuntimeObservationValue(runtime),
			"ai_panorama_closeout_projection":     projection.journalValue(),
			"ai_panorama_closeout_request_sha256": projection.SHA256,
		},
	}
	journal := &Journal{events: []JournalEvent{event}}
	installed := &aiPanoramaInstalledProof{
		WebImage: config.WebImage, WebImageID: runtime.ImageID,
		DockerRoot:             runtime.DockerRoot,
		PublicVolumeMountpoint: runtime.PublicVolumeMountpoint,
		PublicVolumeDevice:     runtime.PublicVolumeDevice,
		PublicVolumeInode:      runtime.PublicVolumeInode,
	}
	parent, cancel := context.WithCancel(context.Background())
	cancel()
	if err := cleanupAiPanoramaCloseoutOrphansBeforeObservation(
		parent, t.TempDir(), journal, config, installed,
		&journal.events[0],
	); err != nil {
		t.Fatal(err)
	}
	if *exists || *removals != 1 {
		t.Fatalf(
			"exact closeout orphan was not removed: exists=%t removals=%d",
			*exists, *removals,
		)
	}
}

func TestAiPanoramaRecoveryOrphanMismatchIsNotRemoved(t *testing.T) {
	for _, fixture := range []struct {
		name   string
		mutate func(map[string]any)
	}{
		{"name", func(value map[string]any) {
			value["Name"] = "/wrong"
		}},
		{"image", func(value map[string]any) {
			value["Image"] = "sha256:" + strings.Repeat("f", 64)
		}},
		{"network", func(value map[string]any) {
			value["HostConfig"].(map[string]any)["NetworkMode"] = "wrong"
		}},
		{"label", func(value map[string]any) {
			value["Config"].(map[string]any)["Labels"].(map[string]any)["propertyquarry.release-control.phase"] = "wrong"
		}},
		{"mount", func(value map[string]any) {
			mounts := value["Mounts"].([]any)
			mounts[len(mounts)-1].(map[string]any)["RW"] = false
		}},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			config := aiPanoramaTestConfig()
			runtime := aiPanoramaRecoveryTestRuntime()
			network, err := aiPanoramaNetworkName(config)
			if err != nil {
				t.Fatal(err)
			}
			name, err := aiPanoramaContainerName(config, "apply")
			if err != nil {
				t.Fatal(err)
			}
			container := aiPanoramaRecoveryTestContainer(
				t, config, runtime, "apply", network,
			)
			fixture.mutate(container)
			exists, removals := aiPanoramaRecoveryTestDocker(
				t, container, name, network, false,
			)
			if err := cleanupAiPanoramaRecoveryPhaseContainer(
				context.Background(), config, runtime, "apply", network,
			); err == nil {
				t.Fatal("mismatched orphan was removed")
			}
			if !*exists || *removals != 0 {
				t.Fatalf(
					"mismatched orphan changed: exists=%t removals=%d",
					*exists, *removals,
				)
			}
		})
	}
}

func aiPanoramaCloseoutTestInstallEvent(
	config *Config,
	eventType string,
) JournalEvent {
	requestID := strings.Repeat("8", 32)
	request := &workflowRequest{
		Operation: aiPanoramaInstallOperation, RequestID: requestID,
	}
	identity := &Identity{RunID: "install-run", RunAttempt: 1}
	payload := authorityFields(config, request, identity)
	payload["admitted_at"] = json.Number("1")
	payload["release_effects_authorized"] = true
	payload["ai_panorama_slug"] = aiPanoramaPraterSlug
	payload["ai_panorama_public_volume_name"] = aiPanoramaPublicVolumeName
	payload["ai_panorama_public_mount_target"] = aiPanoramaPublicMountTarget
	payload["ai_panorama_runtime_observation"] = map[string]any{
		"web_image_id":    "sha256:" + strings.Repeat("1", 64),
		"render_image_id": "sha256:" + strings.Repeat("2", 64),
		"docker_root":     "/var/lib/docker",
		"public_volume_mountpoint": "/var/lib/docker/volumes/" +
			aiPanoramaPublicVolumeName + "/_data",
		"public_volume_device": json.Number("71"),
		"public_volume_inode":  json.Number("73"),
	}
	return JournalEvent{
		EventType: eventType, Operation: aiPanoramaInstallOperation,
		RequestID: requestID, RunID: identity.RunID, RunAttempt: 1,
		ReceiptDigest: "sha256:" + strings.Repeat("3", 64),
		Payload:       payload,
	}
}

func aiPanoramaCloseoutTestConfig() *Config {
	config := aiPanoramaTestConfig()
	config.ReleaseGeneration = 1
	config.PredecessorRuntimeSHA = "genesis"
	config.HostMachineIDDigest = "sha256:" + strings.Repeat("f", 64)
	return config
}

func TestAiPanoramaEmergencyProofDoesNotResurrectTerminalAttempt(
	t *testing.T,
) {
	config := aiPanoramaCloseoutTestConfig()
	admitted := aiPanoramaCloseoutTestInstallEvent(
		config, aiPanoramaInstallAdmittedEvent,
	)
	terminal := admitted
	terminal.EventType = aiPanoramaInstallFailedNoEffectsEvent
	terminal.ReceiptDigest = "sha256:" + strings.Repeat("4", 64)
	journal := &Journal{events: []JournalEvent{admitted, terminal}}
	if _, err := findAiPanoramaInstalledProof(journal, config); err == nil {
		t.Fatal("terminal failed attempt resurrected its earlier admission")
	}
}

func TestAiPanoramaEmergencyProofRejectsIneligibleOrMismatchedBasis(
	t *testing.T,
) {
	config := aiPanoramaCloseoutTestConfig()
	valid := aiPanoramaCloseoutTestInstallEvent(
		config, aiPanoramaInstallMutationStartedEvent,
	)
	proof, err := findAiPanoramaInstalledProof(
		&Journal{events: []JournalEvent{valid}}, config,
	)
	if err != nil || proof == nil || !proof.Emergency {
		t.Fatalf("valid unresolved emergency basis rejected: %#v %v", proof, err)
	}
	for _, fixture := range []struct {
		name   string
		mutate func(*JournalEvent)
	}{
		{"ineligible-event", func(event *JournalEvent) {
			event.EventType = aiPanoramaInstallFenceReadyEvent
		}},
		{"mismatched-config", func(event *JournalEvent) {
			event.Payload["config_digest"] =
				"sha256:" + strings.Repeat("9", 64)
		}},
		{"mismatched-host", func(event *JournalEvent) {
			event.Payload["host_machine_id_digest"] =
				"sha256:" + strings.Repeat("9", 64)
		}},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			event := aiPanoramaCloseoutTestInstallEvent(
				config, aiPanoramaInstallMutationStartedEvent,
			)
			fixture.mutate(&event)
			journal := &Journal{events: []JournalEvent{event}}
			if _, err := findAiPanoramaInstalledProof(
				journal, config,
			); err == nil {
				t.Fatal("invalid emergency closeout basis accepted")
			}
		})
	}
}

func TestAiPanoramaProtectedNodeObservationDistinguishesAbsentAndSymlink(
	t *testing.T,
) {
	root := t.TempDir()
	info, err := os.Lstat(root)
	metadata, ok := infoSys(info)
	if err != nil || !ok {
		t.Fatal(err)
	}
	runtime := &aiPanoramaRuntimeObservation{
		PublicVolumeMountpoint: root,
		PublicVolumeDevice:     uint64(metadata.Dev),
		PublicVolumeInode:      metadata.Ino,
	}
	absent, err := observeAiPanoramaProtectedNode(runtime)
	if err != nil || absent.Present {
		t.Fatalf("absent protected node misclassified: %#v %v", absent, err)
	}
	if err := os.Symlink("/dev/null", filepath.Join(root, aiPanoramaPraterSlug)); err != nil {
		t.Fatal(err)
	}
	symlink, err := observeAiPanoramaProtectedNode(runtime)
	if err != nil || !symlink.Present ||
		symlink.Mode&uint32(os.ModeSymlink) == 0 ||
		symlink.Digest == absent.Digest {
		t.Fatalf("symlink protected node misclassified: %#v %v", symlink, err)
	}
}

func aiPanoramaCloseoutTestEvent(
	config *Config,
	requestID string,
	eventType string,
	projection *aiPanoramaProjection,
) JournalEvent {
	request := &workflowRequest{
		Operation: aiPanoramaCloseoutOperation, RequestID: requestID,
	}
	identity := &Identity{RunID: "closeout-run", RunAttempt: 1}
	payload := authorityFields(config, request, identity)
	if projection != nil {
		payload["ai_panorama_closeout_projection"] = projection.journalValue()
		payload["ai_panorama_closeout_request_sha256"] = projection.SHA256
	}
	return JournalEvent{
		Sequence: 1, ReceiptDigest: "sha256:" + strings.Repeat("d", 64),
		EventType: eventType, Operation: aiPanoramaCloseoutOperation,
		RequestID: requestID, RunID: identity.RunID, RunAttempt: 1,
		Payload: payload,
	}
}

func TestAiPanoramaCloseoutProjectionRejectsTerminalOrWrongLineage(
	t *testing.T,
) {
	config := aiPanoramaCloseoutTestConfig()
	raw, err := aiPanoramaRevocationWire(
		strings.Repeat("a", 32),
		time.Date(2026, 7, 24, 10, 11, 12, 0, time.UTC),
	)
	if err != nil {
		t.Fatal(err)
	}
	projection := &aiPanoramaProjection{
		Kind: "closeout-request", Path: aiPanoramaCloseoutRequestPath,
		Mode: 0o400, SHA256: aiPanoramaRawSHA256(raw), Raw: raw,
	}
	runtime := &aiPanoramaRuntimeObservation{
		PublicVolumeMountpoint: t.TempDir(),
		PublicVolumeDevice:     71, PublicVolumeInode: 73,
	}
	requestID := strings.Repeat("b", 32)
	prepared := aiPanoramaCloseoutTestEvent(
		config, requestID, aiPanoramaCloseoutPreparedEvent, projection,
	)
	if !aiPanoramaCloseoutRequestWasJournalBound(
		&Journal{events: []JournalEvent{prepared}}, raw,
	) {
		t.Fatal("exact unresolved closeout projection was not bound")
	}
	terminal := aiPanoramaCloseoutTestEvent(
		config, requestID, aiPanoramaCloseoutSucceededEvent, projection,
	)
	if aiPanoramaCloseoutRequestWasJournalBound(
		&Journal{events: []JournalEvent{prepared, terminal}}, raw,
	) {
		t.Fatal("terminal closeout projection was request-bound")
	}
	if value, err := findAiPanoramaCloseoutProjectionForMarker(
		&Journal{events: []JournalEvent{prepared, terminal}}, config, runtime,
	); err == nil {
		value.release()
		t.Fatal("terminal closeout projection was reused")
	}
	wrong := prepared
	wrong.Payload = cloneFields(prepared.Payload)
	wrong.Payload["request_id"] = strings.Repeat("c", 32)
	if value, err := findAiPanoramaCloseoutProjectionForMarker(
		&Journal{events: []JournalEvent{wrong}}, config, runtime,
	); err == nil {
		value.release()
		t.Fatal("wrong closeout request lineage was reused")
	}
}

func TestAiPanoramaFreshCloseoutRejectsUnresolvedLineage(t *testing.T) {
	config := aiPanoramaCloseoutTestConfig()
	requestID := strings.Repeat("a", 32)
	prepared := aiPanoramaCloseoutTestEvent(
		config, requestID, aiPanoramaCloseoutPreparedEvent, nil,
	)
	journal := &Journal{events: []JournalEvent{prepared}}
	before := len(journal.events)
	if err := validateAiPanoramaFreshCloseoutAdmission(journal); err == nil {
		t.Fatal("fresh closeout admitted over unresolved lineage")
	}
	if len(journal.events) != before {
		t.Fatal("fresh admission guard mutated the journal")
	}
	terminal := aiPanoramaCloseoutTestEvent(
		config, requestID, aiPanoramaCloseoutFailedEvent, nil,
	)
	journal.events = append(journal.events, terminal)
	if err := validateAiPanoramaFreshCloseoutAdmission(journal); err != nil {
		t.Fatalf("terminal closeout remained unresolved: %v", err)
	}
}

func TestAiPanoramaExistingMarkerRequiresLatestSucceededTerminal(
	t *testing.T,
) {
	raw, err := aiPanoramaRevocationWire(
		strings.Repeat("b", 32),
		time.Date(2026, 7, 24, 10, 11, 12, 0, time.UTC),
	)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(raw)
	observation := &aiPanoramaRevocationObservation{
		RevocationID: strings.Repeat("b", 32),
		RevokedAt:    "2026-07-24T10:11:12Z",
		SHA256:       aiPanoramaRawSHA256(raw),
	}
	journal := &Journal{}
	if aiPanoramaPriorCloseoutSucceededMarkerMatches(
		journal, observation, raw,
	) {
		t.Fatal("forged marker without terminal was accepted")
	}
	config := aiPanoramaCloseoutTestConfig()
	terminal := aiPanoramaCloseoutTestEvent(
		config, strings.Repeat("c", 32),
		aiPanoramaCloseoutSucceededEvent, nil,
	)
	terminal.Payload["production_ready"] = false
	terminal.Payload["disposition"] = "revoked"
	terminal.Payload["ai_panorama_revocation_verified"] = true
	terminal.Payload["ai_panorama_revocation"] = map[string]any{
		"path": aiPanoramaPublicMountTarget + "/" +
			aiPanoramaRevocationLeaf,
		"revocation_id_sha256": aiPanoramaRawSHA256(
			[]byte(observation.RevocationID),
		),
		"revoked_at": observation.RevokedAt,
		"sha256":     observation.SHA256,
		"created":    true,
	}
	journal.events = append(journal.events, terminal)
	if !aiPanoramaPriorCloseoutSucceededMarkerMatches(
		journal, observation, raw,
	) {
		t.Fatal("exact latest succeeded terminal did not bind marker")
	}
	later := aiPanoramaCloseoutTestEvent(
		config, strings.Repeat("d", 32),
		aiPanoramaCloseoutFailedEvent, nil,
	)
	journal.events = append(journal.events, later)
	if aiPanoramaPriorCloseoutSucceededMarkerMatches(
		journal, observation, raw,
	) {
		t.Fatal("stale succeeded terminal bound marker after later closeout")
	}
}

func TestAiPanoramaProjectionContractRejectsArbitraryOrSwappedPaths(t *testing.T) {
	raw := []byte("{}\n")
	valid := (&aiPanoramaProjection{
		Kind: "compose-plan", Path: aiPanoramaComposePlanPath, Mode: 0o400,
		SHA256: aiPanoramaRawSHA256(raw), Raw: raw,
	}).journalValue()
	if projection, err := parseAiPanoramaProjection(valid); err != nil {
		t.Fatal(err)
	} else {
		projection.release()
	}
	arbitrary := cloneFields(valid)
	arbitrary["path"] = "/etc/shadow"
	if _, err := parseAiPanoramaProjection(arbitrary); err == nil {
		t.Fatal("arbitrary cleanup path accepted")
	}
	swapped := cloneFields(valid)
	swapped["path"] = aiPanoramaVolumeProfilePath
	if _, err := parseAiPanoramaProjection(swapped); err == nil {
		t.Fatal("swapped kind/path accepted")
	}
}

func TestAiPanoramaProjectionPublicationRecoversPartialTempUnderUmask(
	t *testing.T,
) {
	root := t.TempDir()
	parent := rooted(root, aiPanoramaRuntimeRoot)
	if err := os.MkdirAll(parent, 0o700); err != nil ||
		os.Chmod(parent, 0o700) != nil {
		t.Fatal(err)
	}
	raw := []byte(`{"schema":"fixture","version":1}` + "\n")
	projection := &aiPanoramaProjection{
		Kind: "compose-plan", Path: aiPanoramaComposePlanPath, Mode: 0o400,
		SHA256: aiPanoramaRawSHA256(raw), Raw: raw,
	}
	temporary := filepath.Join(
		parent, aiPanoramaProjectionTemporaryName(projection),
	)
	if err := os.WriteFile(temporary, raw[:7], 0o000); err != nil ||
		os.Chmod(temporary, 0o000) != nil {
		t.Fatal(err)
	}
	oldMask := syscall.Umask(0o777)
	err := persistAiPanoramaProjectionFile(root, projection)
	syscall.Umask(oldMask)
	if err != nil {
		t.Fatal(err)
	}
	published := rooted(root, projection.Path)
	info, err := os.Lstat(published)
	observed, readErr := os.ReadFile(published)
	if err != nil || readErr != nil || info.Mode().Perm() != 0o400 ||
		!bytes.Equal(observed, raw) {
		t.Fatalf("projection publication invalid: %v %v %#o", err, readErr, info.Mode().Perm())
	}
	if _, err := os.Lstat(temporary); !os.IsNotExist(err) {
		t.Fatal("intent temporary survived publication")
	}
	if err := removeAiPanoramaProjection(root, projection); err != nil {
		t.Fatal(err)
	}
}

func aiPanoramaTestDatabaseSecretWire(t *testing.T) []byte {
	t.Helper()
	raw, err := canonicalJSON(map[string]any{
		"schema": aiPanoramaDatabaseSecretSchema, "version": json.Number("1"),
		"DATABASE_URL": "postgresql://fixture:secret@postgres:5432/propertyquarry",
		"PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET": strings.Repeat("e", 32),
	})
	if err != nil {
		t.Fatal(err)
	}
	return append(raw, '\n')
}

func aiPanoramaAttemptTestRuntimeRoot(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	runtimeRoot := rooted(root, aiPanoramaRuntimeRoot)
	if err := os.MkdirAll(runtimeRoot, 0o700); err != nil ||
		os.Chmod(runtimeRoot, 0o700) != nil {
		t.Fatal(err)
	}
	return root
}

func TestAiPanoramaDatabaseSecretPostRenameFailureCleansAndRetries(
	t *testing.T,
) {
	root := aiPanoramaAttemptTestRuntimeRoot(t)
	wire := aiPanoramaTestDatabaseSecretWire(t)
	defer zero(wire)
	aiPanoramaDatabaseSecretPostRenameHook = func() error {
		return fmt.Errorf("injected-post-rename-failure")
	}
	defer func() {
		aiPanoramaDatabaseSecretPostRenameHook = nil
	}()
	if _, err := persistAiPanoramaDatabaseSecret(root, wire); err == nil {
		t.Fatal("injected post-rename failure was ignored")
	}
	target := rooted(root, aiPanoramaDatabaseSecretMount)
	temporary := filepath.Join(
		rooted(root, aiPanoramaRuntimeRoot),
		".db-secret-"+aiPanoramaRawSHA256(wire)+".tmp",
	)
	for _, path := range []string{target, temporary} {
		if _, err := os.Lstat(path); !os.IsNotExist(err) {
			t.Fatalf("failed publication survived cleanup: %s: %v", path, err)
		}
	}
	aiPanoramaDatabaseSecretPostRenameHook = nil
	oldMask := syscall.Umask(0o777)
	path, err := persistAiPanoramaDatabaseSecret(root, wire)
	syscall.Umask(oldMask)
	if err != nil || path != aiPanoramaDatabaseSecretMount {
		t.Fatalf("retry failed: %q %v", path, err)
	}
	info, err := os.Lstat(target)
	mode := os.FileMode(0)
	if info != nil {
		mode = info.Mode().Perm()
	}
	if err != nil || mode != 0o400 {
		t.Fatalf("retry target mode invalid: %v %#o", err, mode)
	}
	if err := destroyAiPanoramaDatabaseSecret(root); err != nil {
		t.Fatal(err)
	}
}

func TestAiPanoramaDatabaseSecretRecoversCreatorModePartialTemporary(
	t *testing.T,
) {
	root := aiPanoramaAttemptTestRuntimeRoot(t)
	wire := aiPanoramaTestDatabaseSecretWire(t)
	defer zero(wire)
	temporary := filepath.Join(
		rooted(root, aiPanoramaRuntimeRoot),
		".db-secret-"+aiPanoramaRawSHA256(wire)+".tmp",
	)
	if err := os.WriteFile(temporary, wire[:11], 0o000); err != nil ||
		os.Chmod(temporary, 0o000) != nil {
		t.Fatal(err)
	}
	oldMask := syscall.Umask(0o777)
	_, err := persistAiPanoramaDatabaseSecret(root, wire)
	syscall.Umask(oldMask)
	if err != nil {
		t.Fatal(err)
	}
	target := rooted(root, aiPanoramaDatabaseSecretMount)
	info, statErr := os.Lstat(target)
	observed, readErr := os.ReadFile(target)
	mode := os.FileMode(0)
	if info != nil {
		mode = info.Mode().Perm()
	}
	if statErr != nil || readErr != nil || mode != 0o400 ||
		!bytes.Equal(observed, wire) {
		t.Fatalf(
			"recovered secret invalid: %v %v %#o",
			statErr, readErr, mode,
		)
	}
	if _, err := os.Lstat(temporary); !os.IsNotExist(err) {
		t.Fatal("creator-mode temporary survived recovery")
	}
	if err := destroyAiPanoramaDatabaseSecret(root); err != nil {
		t.Fatal(err)
	}
}

func aiPanoramaSealedStageTestContract() (
	*aiPanoramaSourceSnapshot,
	[]byte,
	[]byte,
) {
	tour := []byte(`{"schema":"fixture-tour"}` + "\n")
	panorama := []byte("fixture-panorama")
	return &aiPanoramaSourceSnapshot{
		Directories: []string{".", "panoramas"},
		Files: []aiPanoramaSourceFile{
			{
				Path: "panoramas/one.jpg", Size: int64(len(panorama)),
				Content: panorama,
			},
			{Path: "tour.json", Size: int64(len(tour)), Content: tour},
		},
	}, []byte("marker\n"), []byte("receipt\n")
}

func TestAiPanoramaSealedStageCleanupIsBoundedAndPrefixRecoverable(
	t *testing.T,
) {
	parentPath := t.TempDir()
	if err := os.Chmod(parentPath, 0o700); err != nil {
		t.Fatal(err)
	}
	parent, err := tourV4OpenDirectoryAbsolute(parentPath)
	if err != nil {
		t.Fatal(err)
	}
	defer parent.Close()
	parentInfo, err := parent.Stat()
	parentMetadata, ok := infoSys(parentInfo)
	parentMountID, mountIDErr := aiPanoramaFileMountID(parent)
	if err != nil || !ok || mountIDErr != nil {
		t.Fatal(err)
	}
	stageName := ".prater-v1.stage-" + strings.Repeat("a", 64)
	stagePath := filepath.Join(parentPath, stageName)
	snapshot, marker, receipt := aiPanoramaSealedStageTestContract()
	for _, path := range []string{
		stagePath,
		filepath.Join(stagePath, "bundle"),
		filepath.Join(stagePath, "bundle", aiPanoramaPraterSlug),
	} {
		if err := os.Mkdir(path, 0o700); err != nil ||
			os.Chmod(path, 0o700) != nil {
			t.Fatal(err)
		}
	}
	partialPath := filepath.Join(
		stagePath, "bundle", aiPanoramaPraterSlug, "tour.json",
	)
	if err := os.WriteFile(
		partialPath, snapshot.Files[1].Content[:7], 0o400,
	); err != nil || os.Chmod(partialPath, 0o400) != nil {
		t.Fatal(err)
	}
	if err := cleanupAiPanoramaSealedStage(
		parent, stageName, snapshot, marker, receipt,
		uint64(parentMetadata.Dev), parentMountID,
		uint32(os.Geteuid()), uint32(os.Getegid()),
	); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Lstat(stagePath); !os.IsNotExist(err) {
		t.Fatalf("bounded stage survived cleanup: %v", err)
	}
}

func TestAiPanoramaSealedStageRejectsUnknownTypeAndWrongPrefixWithoutDeletion(
	t *testing.T,
) {
	for _, fixture := range []struct {
		name string
		make func(string, *aiPanoramaSourceSnapshot) error
	}{
		{"symlink", func(stage string, _ *aiPanoramaSourceSnapshot) error {
			return os.Symlink("/tmp", filepath.Join(stage, "unexpected"))
		}},
		{"fifo", func(stage string, _ *aiPanoramaSourceSnapshot) error {
			return syscall.Mkfifo(filepath.Join(stage, "unexpected"), 0o600)
		}},
		{"wrong-prefix", func(stage string, snapshot *aiPanoramaSourceSnapshot) error {
			bundle := filepath.Join(stage, "bundle")
			slug := filepath.Join(bundle, aiPanoramaPraterSlug)
			if err := os.Mkdir(bundle, 0o700); err != nil ||
				os.Mkdir(slug, 0o700) != nil {
				return err
			}
			return os.WriteFile(
				filepath.Join(slug, "tour.json"),
				append([]byte("wrong"), snapshot.Files[1].Content...), 0o400,
			)
		}},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			parentPath := t.TempDir()
			if err := os.Chmod(parentPath, 0o700); err != nil {
				t.Fatal(err)
			}
			parent, err := tourV4OpenDirectoryAbsolute(parentPath)
			if err != nil {
				t.Fatal(err)
			}
			defer parent.Close()
			parentInfo, _ := parent.Stat()
			parentMetadata, _ := infoSys(parentInfo)
			parentMountID, mountIDErr := aiPanoramaFileMountID(parent)
			if mountIDErr != nil {
				t.Fatal(mountIDErr)
			}
			stageName := ".prater-v1.stage-" + strings.Repeat("b", 64)
			stagePath := filepath.Join(parentPath, stageName)
			if err := os.Mkdir(stagePath, 0o700); err != nil ||
				os.Chmod(stagePath, 0o700) != nil {
				t.Fatal(err)
			}
			snapshot, marker, receipt := aiPanoramaSealedStageTestContract()
			if err := fixture.make(stagePath, snapshot); err != nil {
				t.Fatal(err)
			}
			if err := cleanupAiPanoramaSealedStage(
				parent, stageName, snapshot, marker, receipt,
				uint64(parentMetadata.Dev), parentMountID,
				uint32(os.Geteuid()), uint32(os.Getegid()),
			); err == nil {
				t.Fatal("invalid stage was deleted")
			}
			if _, err := os.Lstat(stagePath); err != nil {
				t.Fatalf("rejected stage was mutated away: %v", err)
			}
		})
	}
}

func TestAiPanoramaSealedStageRejectsExpectedSpecialFileWithoutMutation(
	t *testing.T,
) {
	for _, kind := range []string{"symlink", "fifo", "hardlink"} {
		t.Run(kind, func(t *testing.T) {
			parentPath := t.TempDir()
			if err := os.Chmod(parentPath, 0o700); err != nil {
				t.Fatal(err)
			}
			parent, err := tourV4OpenDirectoryAbsolute(parentPath)
			if err != nil {
				t.Fatal(err)
			}
			defer parent.Close()
			parentInfo, err := parent.Stat()
			parentMetadata, metadataOK := infoSys(parentInfo)
			parentMountID, mountErr := aiPanoramaFileMountID(parent)
			if err != nil || !metadataOK || mountErr != nil {
				t.Fatal("parent observation failed")
			}
			stageName := ".prater-v1.stage-" + strings.Repeat("c", 64)
			stagePath := filepath.Join(parentPath, stageName)
			slugPath := filepath.Join(
				stagePath, "bundle", aiPanoramaPraterSlug,
			)
			if err := os.Mkdir(stagePath, 0o700); err != nil ||
				os.Mkdir(filepath.Join(stagePath, "bundle"), 0o700) != nil ||
				os.Mkdir(slugPath, 0o700) != nil {
				t.Fatal("stage fixture creation failed")
			}
			outside := filepath.Join(parentPath, "outside")
			if err := os.WriteFile(outside, []byte("outside"), 0o600); err != nil ||
				os.Chmod(outside, 0o600) != nil {
				t.Fatal(err)
			}
			expectedPath := filepath.Join(slugPath, "tour.json")
			switch kind {
			case "symlink":
				err = os.Symlink(outside, expectedPath)
			case "fifo":
				err = syscall.Mkfifo(expectedPath, 0o600)
			case "hardlink":
				err = os.Link(outside, expectedPath)
			}
			if err != nil {
				t.Fatal(err)
			}
			snapshot, marker, receipt :=
				aiPanoramaSealedStageTestContract()
			if err := cleanupAiPanoramaSealedStage(
				parent, stageName, snapshot, marker, receipt,
				uint64(parentMetadata.Dev), parentMountID,
				uint32(os.Geteuid()), uint32(os.Getegid()),
			); err == nil {
				t.Fatal("special file at expected path was deleted")
			}
			outsideInfo, statErr := os.Lstat(outside)
			outsideRaw, readErr := os.ReadFile(outside)
			if statErr != nil || readErr != nil ||
				outsideInfo.Mode().Perm() != 0o600 ||
				string(outsideRaw) != "outside" {
				t.Fatalf(
					"rejected stage mutated external leaf: stat=%v read=%v",
					statErr, readErr,
				)
			}
			if _, err := os.Lstat(expectedPath); err != nil {
				t.Fatalf("rejected expected leaf was removed: %v", err)
			}
		})
	}
}

func TestAiPanoramaSealedStageMountMismatchIsSideEffectFree(t *testing.T) {
	parentPath := t.TempDir()
	if err := os.Chmod(parentPath, 0o700); err != nil {
		t.Fatal(err)
	}
	parent, err := tourV4OpenDirectoryAbsolute(parentPath)
	if err != nil {
		t.Fatal(err)
	}
	defer parent.Close()
	parentInfo, err := parent.Stat()
	parentMetadata, metadataOK := infoSys(parentInfo)
	parentMountID, mountErr := aiPanoramaFileMountID(parent)
	if err != nil || !metadataOK || mountErr != nil {
		t.Fatal("parent observation failed")
	}
	stageName := ".prater-v1.stage-" + strings.Repeat("d", 64)
	stagePath := filepath.Join(parentPath, stageName)
	bundlePath := filepath.Join(stagePath, "bundle")
	slugPath := filepath.Join(bundlePath, aiPanoramaPraterSlug)
	for _, path := range []string{stagePath, bundlePath, slugPath} {
		if err := os.Mkdir(path, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	partialPath := filepath.Join(slugPath, "tour.json")
	snapshot, marker, receipt := aiPanoramaSealedStageTestContract()
	if err := os.WriteFile(
		partialPath, snapshot.Files[1].Content[:7], 0o400,
	); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{slugPath, bundlePath, stagePath} {
		if err := os.Chmod(path, 0o500); err != nil {
			t.Fatal(err)
		}
	}
	defer func() {
		for _, path := range []string{stagePath, bundlePath, slugPath} {
			_ = os.Chmod(path, 0o700)
		}
	}()
	if err := cleanupAiPanoramaSealedStage(
		parent, stageName, snapshot, marker, receipt,
		uint64(parentMetadata.Dev), parentMountID+1,
		uint32(os.Geteuid()), uint32(os.Getegid()),
	); err == nil {
		t.Fatal("wrong mount identity was accepted")
	}
	for _, path := range []string{stagePath, bundlePath, slugPath} {
		info, err := os.Lstat(path)
		if err != nil || info.Mode().Perm() != 0o500 {
			t.Fatalf("failed observation mutated %s: %v", path, err)
		}
	}
	raw, err := os.ReadFile(partialPath)
	if err != nil || !bytes.Equal(raw, snapshot.Files[1].Content[:7]) {
		t.Fatalf("failed observation mutated partial leaf: %v", err)
	}
}

func TestAiPanoramaPreparedEvidenceAcceptsExactPythonProjection(
	t *testing.T,
) {
	beforeSHA256 := strings.Repeat("a", 64)
	afterSHA256 := strings.Repeat("b", 64)
	volumeProfileSHA256 := strings.Repeat("c", 64)
	reservedSHA256 := strings.Repeat("d", 64)
	ledgerEntrySHA256 := strings.Repeat("e", 64)
	issuedAt := time.Date(2026, 7, 24, 10, 0, 0, 0, time.UTC)
	expected := &aiPanoramaAdvancedStateExpectation{
		PublicationRecordSHA256: beforeSHA256,
		VolumeProfileSHA256:     volumeProfileSHA256,
		BindingStatus:           "applied",
		BindingBeforeSHA256:     beforeSHA256,
		BindingAfterSHA256:      afterSHA256,
		PublicVolumeDevice:      71,
		PublicVolumeInode:       73,
		IssuedAt:                issuedAt,
		ExpiresAt:               issuedAt.Add(10 * time.Minute),
	}
	ledger := aiPanoramaAdvancedLedgerBinding{
		InstanceID:      strings.Repeat("1", 32),
		MatchedSequence: 4,
		EntrySHA256:     ledgerEntrySHA256,
	}
	evidence := map[string]any{
		"contract": "propertyquarry.prater_ai_panorama_governed_release.v1",
		"phase":    "prepared", "slug": aiPanoramaPraterSlug,
		"listing_url_sha256":             aiPanoramaPropertyURLSHA256,
		"source_tree_sha256":             aiPanoramaExpectedSourceTree,
		"tour_sha256":                    aiPanoramaExpectedTourDigest,
		"core_manifest_sha256":           aiPanoramaExpectedCoreDigest,
		"materialization_receipt_sha256": aiPanoramaExpectedReceiptDigest,
		"candidate_marker_sha256":        aiPanoramaExpectedMarkerDigest,
		"publication_record_sha256":      beforeSHA256,
		"volume_profile_sha256":          volumeProfileSHA256,
		"public_tour_volume_name":        aiPanoramaPublicVolumeName,
		"public_tour_mount_target":       aiPanoramaPublicMountTarget,
		"private_values_redacted":        true,
		"admission_recovery_binding": map[string]any{
			"ledger_instance_id":  ledger.InstanceID,
			"ledger_sequence":     json.Number("4"),
			"ledger_entry_sha256": ledgerEntrySHA256,
		},
		"publication_binding_preparation": map[string]any{
			"status": "change-required",
			"publication_binding_expected_before_sha256": beforeSHA256,
			"publication_binding_expected_after_sha256":  afterSHA256,
			"publication_binding_bound_at": issuedAt.Add(time.Second).Format(
				time.RFC3339Nano,
			),
			"database_mutation_performed": false,
			"private_values_redacted":     true,
		},
		"target_manifest": map[string]any{
			"state": "absent", "target_relpath": aiPanoramaPraterSlug,
			"public_root_device":      json.Number("71"),
			"public_root_inode":       json.Number("73"),
			"reserved_entry_count":    json.Number("0"),
			"reserved_entries_sha256": reservedSHA256,
		},
	}
	if err := validateAiPanoramaPreparedEvidence(
		evidence, expected, ledger,
	); err != nil {
		t.Fatal(err)
	}
	preparation := evidence["publication_binding_preparation"].(map[string]any)
	preparation["database_mutation_performed"] = true
	if err := validateAiPanoramaPreparedEvidence(
		evidence, expected, ledger,
	); err == nil {
		t.Fatal("prepared evidence claiming database mutation was accepted")
	}
	preparation["database_mutation_performed"] = false
	preparation["publication_binding_expected_after_sha256"] =
		strings.Repeat("f", 64)
	if err := validateAiPanoramaPreparedEvidence(
		evidence, expected, ledger,
	); err == nil {
		t.Fatal("prepared planned-after digest diverged from terminal binding")
	}
}

func aiPanoramaTestPermitInventory(
	t *testing.T,
) (string, *Journal, string, []byte) {
	t.Helper()
	root := t.TempDir()
	permitRoot := rooted(root, aiPanoramaPermitRoot)
	if err := os.MkdirAll(permitRoot, 0o700); err != nil ||
		os.Chmod(permitRoot, 0o700) != nil {
		t.Fatal(err)
	}
	requestID := strings.Repeat("7", 32)
	path, _ := aiPanoramaPermitPath(requestID)
	envelope := map[string]any{
		"schema": aiPanoramaPermitSchema, "version": json.Number("2"),
		"permit": map[string]any{"request_id": requestID},
		"signature": map[string]any{
			"algorithm": "Ed25519", "key_id": "test",
			"encoding": "base64url", "value": "AA",
		},
	}
	raw, _ := canonicalJSON(envelope)
	raw = append(raw, '\n')
	sha256Value := aiPanoramaRawSHA256(raw)
	journal := &Journal{events: []JournalEvent{{
		EventType: aiPanoramaPermitPersistenceIntentEvent,
		Payload: map[string]any{"ai_panorama_permit": map[string]any{
			"request_id": requestID, "path": path, "sha256": sha256Value,
		}},
	}}}
	return root, journal, path, raw
}

func TestAiPanoramaPermitInventoryRequiresEveryExactImmutableLeaf(t *testing.T) {
	root, journal, path, raw := aiPanoramaTestPermitInventory(t)
	if err := os.WriteFile(rooted(root, path), raw, 0o600); err != nil ||
		os.Chmod(rooted(root, path), 0o600) != nil {
		t.Fatal(err)
	}
	if err := validateAiPanoramaPermitInventory(root, journal); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(rooted(root, path)); err != nil {
		t.Fatal(err)
	}
	if err := validateAiPanoramaPermitInventory(root, journal); err == nil {
		t.Fatal("deleted immutable permit was accepted")
	}
}

func TestAiPanoramaPermitInventoryRejectsLegacyWrongCaseSymlinkAndHardlink(
	t *testing.T,
) {
	for _, fixture := range []struct {
		name string
		make func(string, string, []byte) error
	}{
		{"legacy", func(root, _ string, raw []byte) error {
			return os.WriteFile(rooted(root, aiPanoramaLegacyPermitPath), raw, 0o600)
		}},
		{"wrong-case", func(root, path string, raw []byte) error {
			return os.WriteFile(
				filepath.Join(
					rooted(root, aiPanoramaPermitRoot),
					strings.ToUpper(filepath.Base(path)),
				),
				raw, 0o600,
			)
		}},
		{"symlink", func(root, path string, _ []byte) error {
			return os.Symlink("/dev/null", rooted(root, path))
		}},
		{"hardlink", func(root, path string, raw []byte) error {
			target := rooted(root, path)
			if err := os.WriteFile(target, raw, 0o600); err != nil {
				return err
			}
			return os.Link(target, target+".copy")
		}},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			root, journal, path, raw := aiPanoramaTestPermitInventory(t)
			if err := fixture.make(root, path, raw); err != nil {
				t.Fatal(err)
			}
			if err := validateAiPanoramaPermitInventory(root, journal); err == nil {
				t.Fatal("invalid permit inventory accepted")
			}
		})
	}
}

func TestAiPanoramaReleaseRequestV2BindsDerivedPermitLeaf(t *testing.T) {
	requestID := strings.Repeat("a", 32)
	relpath, _ := aiPanoramaPermitRelpath(requestID)
	raw, err := aiPanoramaReleaseRequestWire(
		"owner@example.invalid", strings.Repeat("b", 64), requestID, relpath,
	)
	if err != nil {
		t.Fatal(err)
	}
	value, err := strictJSON(raw[:len(raw)-1], len(raw))
	if err != nil || !hasKeys(
		value, "schema", "version", "authority", "status",
		"owner_principal_id", "expected_publication_record_sha256",
		"request_id", "permit_relpath",
	) || value["schema"] != aiPanoramaReleaseRequestSchema ||
		value["version"] != json.Number("2") ||
		value["permit_relpath"] != relpath {
		t.Fatalf("release request mismatch: %#v %v", value, err)
	}
	if _, err := aiPanoramaReleaseRequestWire(
		"owner@example.invalid", strings.Repeat("b", 64), requestID,
		"prater-ai-panorama-install.json",
	); err == nil {
		t.Fatal("legacy permit path accepted")
	}
}
