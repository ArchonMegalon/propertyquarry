//go:build linux && amd64

package authority

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"strconv"
)

const aiPanoramaBootstrapSchema = "propertyquarry.prater-governed-volume-bootstrap-result.v1"

// Test-only observation seam for the crash window after the bootstrap
// container changed ownership but before the initialized event was appended.
var observeAiPanoramaInterruptedBootstrapRuntime = observeAiPanoramaRuntime

func recordAiPanoramaPreparationResolution(
	journal *Journal,
	payload map[string]any,
	disposition string,
) error {
	if journal == nil || payload == nil || disposition == "" {
		return fmt.Errorf("ai-panorama-preparation-resolution-input-invalid")
	}
	fields := aiPanoramaRecoveryFields(payload)
	fields["completed_at"] = json.Number(
		strconv.FormatInt(authorityNow().UTC().Unix(), 10),
	)
	fields["ready"] = false
	fields["production_ready"] = false
	fields["release_effects_performed"] = false
	fields["rollback_performed"] = false
	fields["recovery"] = true
	fields["pre_attempt_resolution"] = true
	fields["disposition"] = disposition
	return appendAiPanoramaJournalEvent(
		journal, aiPanoramaInstallPreparationResolvedEvent, fields,
	)
}

type aiPanoramaBootstrapResult struct {
	RootDevice uint64
	RootInode  uint64
	RawSHA256  string
}

func parseAiPanoramaBootstrapResult(raw []byte) (*aiPanoramaBootstrapResult, error) {
	if len(raw) < 3 || len(raw) > 4096 || raw[len(raw)-1] != '\n' ||
		raw[len(raw)-2] == '\n' || bytes.IndexByte(raw[:len(raw)-1], '\n') >= 0 {
		return nil, fmt.Errorf("ai-panorama-bootstrap-result-framing-invalid")
	}
	value, err := strictJSON(raw[:len(raw)-1], 4096)
	if err != nil || !hasKeys(value,
		"schema", "version", "status", "root_device", "root_inode",
		"root_uid", "root_gid", "root_mode", "root_empty",
		"private_values_redacted",
	) || value["schema"] != aiPanoramaBootstrapSchema ||
		value["version"] != json.Number("1") ||
		value["status"] != "initialized" ||
		value["root_empty"] != true ||
		value["private_values_redacted"] != true {
		return nil, fmt.Errorf("ai-panorama-bootstrap-result-invalid")
	}
	device, deviceOK := exactInt(value["root_device"], 1, 1<<62)
	inode, inodeOK := exactInt(value["root_inode"], 1, 1<<62)
	uid, uidOK := exactInt(value["root_uid"], 10001, 10001)
	gid, gidOK := exactInt(value["root_gid"], 10001, 10001)
	mode, modeOK := exactInt(value["root_mode"], 493, 493)
	if !deviceOK || !inodeOK || !uidOK || uid != 10001 ||
		!gidOK || gid != 10001 || !modeOK || mode != 493 {
		return nil, fmt.Errorf("ai-panorama-bootstrap-result-proof-invalid")
	}
	return &aiPanoramaBootstrapResult{
		RootDevice: uint64(device), RootInode: uint64(inode),
		RawSHA256: aiPanoramaRawSHA256(raw),
	}, nil
}

func aiPanoramaRuntimeIdentityStable(
	before *aiPanoramaRuntimeObservation,
	after *aiPanoramaRuntimeObservation,
) bool {
	return before != nil && after != nil &&
		before.DockerRoot == after.DockerRoot &&
		before.ImageID == after.ImageID &&
		before.ControlRootDevice == after.ControlRootDevice &&
		before.ControlRootInode == after.ControlRootInode &&
		before.PublicVolumeMountpoint == after.PublicVolumeMountpoint &&
		before.PublicVolumeDevice == after.PublicVolumeDevice &&
		before.PublicVolumeInode == after.PublicVolumeInode &&
		before.PublicVolumeUID == 0 && before.PublicVolumeGID == 0 &&
		before.PublicVolumeMode == 0o755 &&
		before.PublicVolumeNeedsInitialization &&
		after.PublicVolumeUID == 10001 && after.PublicVolumeGID == 10001 &&
		after.PublicVolumeMode == 0o755 &&
		!after.PublicVolumeNeedsInitialization &&
		before.DatabaseContainerID == after.DatabaseContainerID &&
		before.DatabaseContainerName == after.DatabaseContainerName &&
		before.DatabaseImageID == after.DatabaseImageID &&
		before.APIRuntimeContainerID == after.APIRuntimeContainerID &&
		before.APIRuntimeContainerName == after.APIRuntimeContainerName &&
		before.APIRuntimeImageID == after.APIRuntimeImageID &&
		before.SchedulerContainerID == after.SchedulerContainerID &&
		before.SchedulerContainerName == after.SchedulerContainerName &&
		before.SchedulerImageID == after.SchedulerImageID &&
		before.RenderContainerID == after.RenderContainerID &&
		before.RenderContainerName == after.RenderContainerName &&
		before.RenderImageID == after.RenderImageID
}

func bootstrapAiPanoramaGovernedVolume(
	parent context.Context,
	root string,
	config *Config,
	before *aiPanoramaRuntimeObservation,
) (*aiPanoramaRuntimeObservation, *aiPanoramaBootstrapResult, error) {
	if parent == nil || root != "/" || config == nil || before == nil ||
		!before.PublicVolumeNeedsInitialization {
		return nil, nil, fmt.Errorf("ai-panorama-bootstrap-input-invalid")
	}
	virgin, err := snapshotAiPanoramaRelated(before.PublicVolumeMountpoint)
	if err != nil || len(virgin.Entries) != 0 {
		return nil, nil, fmt.Errorf("ai-panorama-bootstrap-volume-not-virgin")
	}
	raw, err := runAiPanoramaContainerRaw(
		parent, config, before, nil, nil, "bootstrap", "",
	)
	if err != nil {
		return nil, nil, err
	}
	result, err := parseAiPanoramaBootstrapResult(raw)
	zero(raw)
	if err != nil {
		return nil, nil, err
	}
	after, err := observeAiPanoramaRuntime(parent, root, config)
	if err != nil || !aiPanoramaRuntimeIdentityStable(before, after) ||
		result.RootDevice != after.PublicVolumeDevice ||
		result.RootInode != after.PublicVolumeInode {
		return nil, nil, fmt.Errorf("ai-panorama-bootstrap-postaudit-invalid")
	}
	empty, err := snapshotAiPanoramaRelated(after.PublicVolumeMountpoint)
	if err != nil || len(empty.Entries) != 0 {
		return nil, nil, fmt.Errorf("ai-panorama-bootstrap-postaudit-invalid")
	}
	return after, result, nil
}

func recoverAiPanoramaInterruptedBootstrap(
	parent context.Context,
	root string,
	journal *Journal,
	config *Config,
	last *JournalEvent,
) error {
	if parent == nil || journal == nil || config == nil || last == nil ||
		(last.EventType != aiPanoramaInstallBootstrapPreparedEvent &&
			last.EventType != aiPanoramaInstallVolumeInitializedEvent) {
		return fmt.Errorf("ai-panorama-bootstrap-recovery-input-invalid")
	}
	recoveryBase := aiPanoramaRecoveryFields(last.Payload)
	before, ok := last.Payload["ai_panorama_bootstrap_before"].(map[string]any)
	mountpoint, mountOK := exactString(before["public_volume_mountpoint"])
	device, deviceOK := exactInt(before["public_volume_device"], 1, 1<<62)
	inode, inodeOK := exactInt(before["public_volume_inode"], 1, 1<<62)
	if !ok || !mountOK || !deviceOK || !inodeOK {
		return aiPanoramaRecordRecoveryRequired(
			journal, recoveryBase, "bootstrap-recovery-intent-invalid",
		)
	}
	runtime, err := observeAiPanoramaInterruptedBootstrapRuntime(
		parent, root, config,
	)
	if err != nil || runtime.PublicVolumeMountpoint != mountpoint ||
		runtime.PublicVolumeDevice != uint64(device) ||
		runtime.PublicVolumeInode != uint64(inode) ||
		runtime.PublicVolumeMode != 0o755 {
		return aiPanoramaRecordRecoveryRequired(
			journal, recoveryBase, "bootstrap-recovery-identity-ambiguous",
		)
	}
	empty, err := snapshotAiPanoramaRelated(runtime.PublicVolumeMountpoint)
	if err != nil || len(empty.Entries) != 0 {
		return aiPanoramaRecordRecoveryRequired(
			journal, recoveryBase, "bootstrap-recovery-inventory-ambiguous",
		)
	}
	fields := cloneFields(recoveryBase)
	if runtime.PublicVolumeUID == 10001 && runtime.PublicVolumeGID == 10001 &&
		!runtime.PublicVolumeNeedsInitialization {
		fields["ai_panorama_bootstrap_after"] =
			aiPanoramaRuntimeObservationValue(runtime)
		fields["disposition"] = "recovered-volume-bootstrap-verified"
		if last.EventType != aiPanoramaInstallVolumeInitializedEvent {
			if err := appendAiPanoramaJournalEvent(
				journal, aiPanoramaInstallVolumeInitializedEvent,
				cloneFields(fields),
			); err != nil {
				return err
			}
		}
	} else if last.EventType == aiPanoramaInstallBootstrapPreparedEvent &&
		runtime.PublicVolumeUID == 0 && runtime.PublicVolumeGID == 0 &&
		runtime.PublicVolumeNeedsInitialization {
		fields["disposition"] =
			"recovered-before-volume-bootstrap-mutation"
	} else {
		return aiPanoramaRecordRecoveryRequired(
			journal, recoveryBase, "bootstrap-recovery-ownership-ambiguous",
		)
	}
	return recordAiPanoramaPreparationResolution(
		journal, fields, stringValue(fields["disposition"]),
	)
}
