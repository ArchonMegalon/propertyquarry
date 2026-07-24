//go:build linux && amd64

package authority

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	aiPanoramaCloseoutOperation         = "ai-panorama-closeout"
	aiPanoramaCloseoutAdmittedEvent     = "ai-panorama-closeout-admitted"
	aiPanoramaCloseoutPreparedEvent     = "ai-panorama-closeout-prepared"
	aiPanoramaCloseoutRequestPersisted  = "ai-panorama-closeout-request-persisted"
	aiPanoramaCloseoutContainerVerified = "ai-panorama-closeout-container-verified"
	aiPanoramaCloseoutSucceededEvent    = "ai-panorama-closeout-succeeded"
	aiPanoramaCloseoutFailedEvent       = "ai-panorama-closeout-failed-no-effects"
	aiPanoramaCloseoutRecoveryRequired  = "ai-panorama-closeout-recovery-required"
	aiPanoramaRevocationSchema          = "propertyquarry.governed-public-tour-revocation.v1"
	aiPanoramaCloseoutRequestPath       = aiPanoramaRuntimeRoot + "/prater-ai-panorama-closeout-request.v1.json"
	aiPanoramaCloseoutReasonCode        = "operator-authenticated-governed-tour-closeout"
	aiPanoramaMaximumRevocationBytes    = 4096
)

type aiPanoramaRevocationObservation struct {
	RevocationID string
	RevokedAt    string
	SHA256       string
}

type aiPanoramaInstalledProof struct {
	ReceiptDigest          string
	WebImage               string
	WebImageID             string
	RenderImage            string
	RenderImageID          string
	DockerRoot             string
	PublicVolumeMountpoint string
	PublicVolumeDevice     uint64
	PublicVolumeInode      uint64
	ManifestSHA256         string
	StateProfile           *aiPanoramaAdvancedStateProfile
	Emergency              bool
	BasisEventType         string
}

type aiPanoramaProtectedNodeObservation struct {
	Present       bool
	Device        uint64
	Inode         uint64
	Mode          uint32
	UID           uint32
	GID           uint32
	Nlink         uint64
	Size          int64
	SubtreeSHA256 string
	Digest        string
}

func aiPanoramaProtectedNodeValue(
	observation *aiPanoramaProtectedNodeObservation,
) map[string]any {
	value := map[string]any{
		"path":  aiPanoramaPublicMountTarget + "/" + aiPanoramaPraterSlug,
		"state": "absent",
	}
	if observation != nil && observation.Present {
		value["state"] = "present"
		value["device"] = json.Number(strconv.FormatUint(observation.Device, 10))
		value["inode"] = json.Number(strconv.FormatUint(observation.Inode, 10))
		value["mode"] = json.Number(strconv.FormatUint(uint64(observation.Mode), 10))
		value["uid"] = json.Number(strconv.FormatUint(uint64(observation.UID), 10))
		value["gid"] = json.Number(strconv.FormatUint(uint64(observation.GID), 10))
		value["nlink"] = json.Number(strconv.FormatUint(observation.Nlink, 10))
		value["size_bytes"] = json.Number(strconv.FormatInt(observation.Size, 10))
		value["subtree_sha256"] = observation.SubtreeSHA256
	}
	if observation != nil {
		value["sha256"] = observation.Digest
	}
	return value
}

func aiPanoramaProtectedNodePayloadMatches(
	value map[string]any,
	expectedDigest string,
	runtime *aiPanoramaRuntimeObservation,
) bool {
	if value == nil || runtime == nil ||
		!aiPanoramaRawSHA256Pattern.MatchString(expectedDigest) ||
		!hasKeys(
			value, "path", "state", "device", "inode", "mode", "uid",
			"gid", "nlink", "size_bytes", "subtree_sha256", "sha256",
		) ||
		value["path"] !=
			aiPanoramaPublicMountTarget+"/"+aiPanoramaPraterSlug ||
		value["state"] != "present" || value["sha256"] != expectedDigest {
		return false
	}
	subtreeSHA256, subtreeOK := exactString(value["subtree_sha256"])
	device, deviceOK := exactInt(value["device"], 1, 1<<62)
	inode, inodeOK := exactInt(value["inode"], 1, 1<<62)
	mode, modeOK := exactInt(value["mode"], 0, 1<<32-1)
	uid, uidOK := exactInt(value["uid"], 0, 1<<32-1)
	gid, gidOK := exactInt(value["gid"], 0, 1<<32-1)
	nlink, nlinkOK := exactInt(value["nlink"], 1, 1<<62)
	size, sizeOK := exactInt(value["size_bytes"], 0, 1<<62)
	if !subtreeOK ||
		!aiPanoramaRawSHA256Pattern.MatchString(subtreeSHA256) ||
		!deviceOK || !inodeOK || !modeOK || !uidOK || !gidOK ||
		!nlinkOK || !sizeOK || uint64(device) != runtime.PublicVolumeDevice {
		return false
	}
	observation := &aiPanoramaProtectedNodeObservation{
		Present:       true,
		Device:        uint64(device),
		Inode:         uint64(inode),
		Mode:          uint32(mode),
		UID:           uint32(uid),
		GID:           uint32(gid),
		Nlink:         uint64(nlink),
		Size:          size,
		SubtreeSHA256: subtreeSHA256,
		Digest:        expectedDigest,
	}
	reconstructed := aiPanoramaProtectedNodeValue(observation)
	digestValue := cloneFields(reconstructed)
	delete(digestValue, "sha256")
	raw, err := canonicalJSON(digestValue)
	if err != nil {
		zero(raw)
		return false
	}
	digestMatches := aiPanoramaRawSHA256(raw) == expectedDigest
	zero(raw)
	return digestMatches && canonicalValuesEqual(value, reconstructed)
}

func observeAiPanoramaProtectedNode(
	runtime *aiPanoramaRuntimeObservation,
) (*aiPanoramaProtectedNodeObservation, error) {
	if runtime == nil || runtime.PublicVolumeMountpoint == "" ||
		runtime.PublicVolumeDevice < 1 || runtime.PublicVolumeInode < 1 {
		return nil, fmt.Errorf("ai-panorama-protected-node-input-invalid")
	}
	rootBefore, err := os.Lstat(runtime.PublicVolumeMountpoint)
	rootMetadata, rootOK := infoSys(rootBefore)
	if err != nil || !rootOK || !rootBefore.IsDir() ||
		rootBefore.Mode()&os.ModeSymlink != 0 ||
		uint64(rootMetadata.Dev) != runtime.PublicVolumeDevice ||
		rootMetadata.Ino != runtime.PublicVolumeInode {
		return nil, fmt.Errorf("ai-panorama-protected-node-root-invalid")
	}
	root, err := tourV4OpenDirectoryAbsolute(runtime.PublicVolumeMountpoint)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-protected-node-root-invalid")
	}
	defer root.Close()
	rootDescriptorBefore, rootDescriptorErr := root.Stat()
	if rootDescriptorErr != nil ||
		!os.SameFile(rootBefore, rootDescriptorBefore) {
		return nil, fmt.Errorf("ai-panorama-protected-node-root-changed")
	}
	target := filepath.Join(runtime.PublicVolumeMountpoint, aiPanoramaPraterSlug)
	info, targetErr := os.Lstat(target)
	if targetErr != nil && !os.IsNotExist(targetErr) {
		return nil, fmt.Errorf("ai-panorama-protected-node-observation-failed")
	}
	rootAfter, rootAfterErr := os.Lstat(runtime.PublicVolumeMountpoint)
	if rootAfterErr != nil || !os.SameFile(rootBefore, rootAfter) {
		return nil, fmt.Errorf("ai-panorama-protected-node-root-changed")
	}
	observation := &aiPanoramaProtectedNodeObservation{}
	if targetErr == nil {
		metadata, ok := infoSys(info)
		if !ok || uint64(metadata.Dev) != runtime.PublicVolumeDevice {
			return nil, fmt.Errorf("ai-panorama-protected-node-metadata-invalid")
		}
		observation.Present = true
		observation.Device = uint64(metadata.Dev)
		observation.Inode = metadata.Ino
		observation.Mode = uint32(info.Mode())
		observation.UID = metadata.Uid
		observation.GID = metadata.Gid
		observation.Nlink = metadata.Nlink
		observation.Size = info.Size()
		if info.IsDir() {
			subtree, subtreeErr := tourV4SnapshotTreeAt(
				root, aiPanoramaPraterSlug,
				aiPanoramaPublicMountTarget+"/"+aiPanoramaPraterSlug,
				nil, false,
			)
			if subtreeErr != nil {
				return nil, fmt.Errorf(
					"ai-panorama-protected-node-subtree-invalid: %w",
					subtreeErr,
				)
			}
			subtreeSHA256 := strings.TrimPrefix(
				subtree.TreeSHA256, "sha256:",
			)
			subtreeValid := subtree.Device == observation.Device &&
				subtree.Inode == observation.Inode &&
				subtree.Mode == uint32(info.Mode().Perm()) &&
				subtree.UID == observation.UID &&
				subtree.GID == observation.GID &&
				subtreeSHA256 != subtree.TreeSHA256 &&
				aiPanoramaRawSHA256Pattern.MatchString(
					subtreeSHA256,
				)
			observation.SubtreeSHA256 = subtreeSHA256
			subtree.release()
			if !subtreeValid {
				return nil, fmt.Errorf(
					"ai-panorama-protected-node-subtree-changed",
				)
			}
		} else {
			observation.SubtreeSHA256 = aiPanoramaRawSHA256(
				[]byte(
					"propertyquarry-ai-panorama-protected-subtree:" +
						"non-directory:v1",
				),
			)
		}
		targetAfter, targetAfterErr := os.Lstat(target)
		if targetAfterErr != nil || !os.SameFile(info, targetAfter) {
			return nil, fmt.Errorf(
				"ai-panorama-protected-node-target-changed",
			)
		}
	}
	value := aiPanoramaProtectedNodeValue(observation)
	delete(value, "sha256")
	raw, err := canonicalJSON(value)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-protected-node-digest-failed")
	}
	observation.Digest = aiPanoramaRawSHA256(raw)
	zero(raw)
	rootDescriptorAfter, rootDescriptorAfterErr := root.Stat()
	rootPathAfter, rootPathAfterErr := os.Lstat(
		runtime.PublicVolumeMountpoint,
	)
	if rootDescriptorAfterErr != nil || rootPathAfterErr != nil ||
		!os.SameFile(rootBefore, rootPathAfter) ||
		!tourV4SameFingerprint(
			rootDescriptorBefore, rootDescriptorAfter,
		) {
		return nil, fmt.Errorf("ai-panorama-protected-node-root-changed")
	}
	return observation, nil
}

func aiPanoramaCloseoutInstallConfigBasisMatches(
	payload map[string]any,
	config *Config,
) bool {
	if payload == nil || config == nil {
		return false
	}
	generation, generationOK := exactInt(
		payload["release_generation"], 1, config.ReleaseGeneration,
	)
	if !generationOK ||
		payload["repository"] != Repository ||
		payload["workflow_ref"] != WorkflowRef ||
		payload["authority_profile"] != "single-host-production-v2" ||
		payload["host_machine_id_digest"] != config.HostMachineIDDigest ||
		payload["authority_scope"] !=
			"host:"+config.HostMachineIDDigest+"/project:"+ProjectName ||
		payload["authoritative"] != true ||
		payload["single_host_authority"] != true ||
		payload["external_cas_profile"] != false {
		return false
	}
	if generation == config.ReleaseGeneration {
		return payload["config_digest"] == config.Digest &&
			payload["plan_digest"] == config.PlanDigest &&
			payload["runtime_sha"] == config.RuntimeSHA &&
			payload["workflow_sha"] == config.WorkflowSHA &&
			payload["deployment_id"] == config.DeploymentID
	}
	configDigest, configOK := exactString(payload["config_digest"])
	planDigest, planOK := exactString(payload["plan_digest"])
	runtimeSHA, runtimeOK := exactString(payload["runtime_sha"])
	workflowSHA, workflowOK := exactString(payload["workflow_sha"])
	deploymentID, deploymentOK := exactString(payload["deployment_id"])
	return generation+1 == config.ReleaseGeneration &&
		config.PredecessorRuntimeSHA != "genesis" &&
		configOK && digestPattern.MatchString(configDigest) &&
		planOK && digestPattern.MatchString(planDigest) &&
		runtimeOK && runtimeSHA == config.PredecessorRuntimeSHA &&
		workflowOK && shaPattern.MatchString(workflowSHA) &&
		deploymentOK && deploymentIDPattern.MatchString(deploymentID)
}

func aiPanoramaCloseoutRequestAuthorityMatches(
	event *JournalEvent,
	config *Config,
) bool {
	if event == nil || config == nil ||
		event.Operation != aiPanoramaCloseoutOperation ||
		event.Payload["operation"] != aiPanoramaCloseoutOperation ||
		event.Payload["request_id"] != event.RequestID ||
		event.Payload["run_id"] != event.RunID ||
		event.Payload["config_digest"] != config.Digest ||
		event.Payload["plan_digest"] != config.PlanDigest ||
		event.Payload["runtime_sha"] != config.RuntimeSHA ||
		event.Payload["workflow_sha"] != config.WorkflowSHA ||
		event.Payload["deployment_id"] != config.DeploymentID ||
		event.Payload["host_machine_id_digest"] != config.HostMachineIDDigest ||
		event.Payload["authority_scope"] !=
			"host:"+config.HostMachineIDDigest+"/project:"+ProjectName ||
		event.Payload["authoritative"] != true ||
		event.Payload["single_host_authority"] != true ||
		event.Payload["external_cas_profile"] != false {
		return false
	}
	runAttempt, ok := exactInt(
		event.Payload["run_attempt"], event.RunAttempt, event.RunAttempt,
	)
	return ok && runAttempt == event.RunAttempt
}

func aiPanoramaCloseoutEmergencyBasisEvent(eventType string) bool {
	switch eventType {
	case aiPanoramaInstallAdmittedEvent,
		aiPanoramaInstallPreflightStartedEvent,
		aiPanoramaInstallPreflightReadyEvent,
		aiPanoramaInstallMutationStartedEvent,
		aiPanoramaInstallMutationVerifiedEvent,
		aiPanoramaInstallRecoveryRequiredEvent,
		aiPanoramaAttemptProjectionsCleanedEvent:
		return true
	default:
		return false
	}
}

func aiPanoramaCloseoutRecoveryEvent(eventType string) bool {
	switch eventType {
	case aiPanoramaCloseoutAdmittedEvent,
		aiPanoramaCloseoutPreparedEvent,
		aiPanoramaCloseoutRequestPersisted,
		aiPanoramaCloseoutContainerVerified,
		aiPanoramaCloseoutRecoveryRequired:
		return true
	default:
		return false
	}
}

func findAiPanoramaInstalledProof(
	journal *Journal,
	config *Config,
) (*aiPanoramaInstalledProof, error) {
	if journal == nil || config == nil {
		return nil, fmt.Errorf("ai-panorama-installed-proof-journal-missing")
	}
	var unresolvedInstall *JournalEvent
	for _, event := range unresolvedWorkflowOperations(journal) {
		if event.Operation == aiPanoramaInstallOperation {
			if unresolvedInstall != nil {
				return nil, fmt.Errorf("ai-panorama-installed-proof-lineage-ambiguous")
			}
			unresolvedInstall = event
		}
	}
	if unresolvedInstall != nil {
		_, admitted := exactInt(
			unresolvedInstall.Payload["admitted_at"], 1, 1<<62,
		)
		if !admitted ||
			!aiPanoramaCloseoutEmergencyBasisEvent(unresolvedInstall.EventType) ||
			unresolvedInstall.Payload["release_effects_authorized"] != true ||
			terminalEvent(unresolvedInstall.EventType) {
			return nil, fmt.Errorf("ai-panorama-installed-proof-emergency-basis-invalid")
		}
	}
	for index := len(journal.events) - 1; index >= 0; index-- {
		event := &journal.events[index]
		if event.Operation != aiPanoramaInstallOperation {
			continue
		}
		if unresolvedInstall != nil && event != unresolvedInstall {
			continue
		}
		succeeded := event.EventType == aiPanoramaInstallSucceededEvent
		_, admitted := exactInt(event.Payload["admitted_at"], 1, 1<<62)
		emergency := !succeeded && event == unresolvedInstall && admitted &&
			aiPanoramaCloseoutEmergencyBasisEvent(event.EventType) &&
			event.Payload["release_effects_authorized"] == true &&
			!terminalEvent(event.EventType)
		if !succeeded && !emergency {
			continue
		}
		if !aiPanoramaCloseoutInstallConfigBasisMatches(event.Payload, config) {
			return nil, fmt.Errorf("ai-panorama-installed-proof-config-invalid")
		}
		runtime, runtimeOK := event.Payload["ai_panorama_runtime_observation"].(map[string]any)
		webImage, webImageOK := exactString(event.Payload["web_image"])
		webImageID, webImageIDOK := exactString(runtime["web_image_id"])
		renderImage, renderImageOK := exactString(event.Payload["render_image"])
		renderImageID, _ := exactString(runtime["render_image_id"])
		dockerRoot, dockerRootOK := exactString(runtime["docker_root"])
		mountpoint, mountpointOK := exactString(runtime["public_volume_mountpoint"])
		device, deviceOK := exactInt(runtime["public_volume_device"], 1, 1<<62)
		inode, inodeOK := exactInt(runtime["public_volume_inode"], 1, 1<<62)
		manifestSHA256, manifestOK := exactString(
			event.Payload["ai_panorama_after_manifest_sha256"],
		)
		var stateProfile *aiPanoramaAdvancedStateProfile
		var stateProfileErr error
		if succeeded {
			stateProfile, stateProfileErr = parseAiPanoramaAdvancedStateProfile(
				event.Payload["ai_panorama_advanced_state_profile"],
			)
		}
		if (succeeded && event.Payload["ai_panorama_install_verified"] != true) ||
			event.Payload["ai_panorama_slug"] != aiPanoramaPraterSlug ||
			event.Payload["ai_panorama_public_volume_name"] != aiPanoramaPublicVolumeName ||
			event.Payload["ai_panorama_public_mount_target"] != aiPanoramaPublicMountTarget ||
			!runtimeOK ||
			!digestPattern.MatchString(event.ReceiptDigest) ||
			!webImageOK || !imagePattern.MatchString(webImage) ||
			!webImageIDOK || !digestPattern.MatchString(webImageID) ||
			!renderImageOK || !imagePattern.MatchString(renderImage) ||
			(renderImageID != "" && !digestPattern.MatchString(renderImageID)) ||
			!dockerRootOK || !mountpointOK ||
			!deviceOK || !inodeOK ||
			(succeeded && (!manifestOK ||
				!digestPattern.MatchString(manifestSHA256) ||
				stateProfileErr != nil)) {
			return nil, fmt.Errorf("ai-panorama-installed-proof-invalid")
		}
		return &aiPanoramaInstalledProof{
			ReceiptDigest: event.ReceiptDigest,
			WebImage:      webImage, WebImageID: webImageID,
			RenderImage: renderImage, RenderImageID: renderImageID,
			DockerRoot: dockerRoot, PublicVolumeMountpoint: mountpoint,
			PublicVolumeDevice: uint64(device), PublicVolumeInode: uint64(inode),
			ManifestSHA256: manifestSHA256, StateProfile: stateProfile,
			Emergency: emergency, BasisEventType: event.EventType,
		}, nil
	}
	return nil, fmt.Errorf("ai-panorama-installed-proof-missing")
}

func observeAiPanoramaCloseoutRuntime(
	ctx context.Context,
	proof *aiPanoramaInstalledProof,
) (*aiPanoramaRuntimeObservation, error) {
	if ctx == nil || proof == nil {
		return nil, fmt.Errorf("ai-panorama-closeout-runtime-input-invalid")
	}
	dockerRootRaw, err := executeAiPanoramaDocker(
		ctx, DockerExecutablePath, "info", "--format", "{{json .DockerRootDir}}",
	)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-closeout-docker-root-unavailable")
	}
	var dockerRoot string
	if json.Unmarshal(bytes.TrimSpace(dockerRootRaw), &dockerRoot) != nil ||
		dockerRoot != proof.DockerRoot {
		zero(dockerRootRaw)
		return nil, fmt.Errorf("ai-panorama-closeout-docker-root-invalid")
	}
	zero(dockerRootRaw)
	imageRaw, err := executeAiPanoramaDocker(
		ctx, DockerExecutablePath, "image", "inspect", "--format",
		"{{.Id}}|{{.Config.User}}|{{json .RepoDigests}}|{{json .Config.Entrypoint}}",
		proof.WebImage,
	)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-closeout-image-unavailable")
	}
	imageParts := bytes.Split(bytes.TrimSpace(imageRaw), []byte{'|'})
	if len(imageParts) != 4 {
		zero(imageRaw)
		return nil, fmt.Errorf("ai-panorama-closeout-image-invalid")
	}
	imageID := string(imageParts[0])
	imageUser := string(imageParts[1])
	var repoDigests, entrypoint []any
	imageMetadataValid := json.Unmarshal(imageParts[2], &repoDigests) == nil &&
		json.Unmarshal(imageParts[3], &entrypoint) == nil
	zero(imageRaw)
	if !imageMetadataValid || imageID != proof.WebImageID ||
		(imageUser != "10001:10001" && imageUser != "10001") ||
		!aiPanoramaStringArrayContains(repoDigests, proof.WebImage) ||
		!aiPanoramaImageEntrypointValid(entrypoint) {
		return nil, fmt.Errorf("ai-panorama-closeout-image-invalid")
	}
	volumeRaw, err := executeAiPanoramaDocker(
		ctx, DockerExecutablePath, "volume", "inspect", "--format", "{{json .}}",
		aiPanoramaPublicVolumeName,
	)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-closeout-volume-unavailable")
	}
	volume, err := aiPanoramaDockerObject(volumeRaw)
	zero(volumeRaw)
	if err != nil {
		return nil, err
	}
	mountpoint, mountOK := exactString(volume["Mountpoint"])
	labels, labelsOK := volume["Labels"].(map[string]any)
	if volume["Name"] != aiPanoramaPublicVolumeName ||
		volume["Driver"] != "local" || volume["Scope"] != "local" ||
		!mountOK || mountpoint != proof.PublicVolumeMountpoint ||
		mountpoint != filepath.Join(dockerRoot, "volumes", aiPanoramaPublicVolumeName, "_data") ||
		!labelsOK || !validStringMap(labels, 64) ||
		labels["com.docker.compose.project"] != ProjectName ||
		labels["com.docker.compose.volume"] != aiPanoramaPublicVolumeComposeKey {
		return nil, fmt.Errorf("ai-panorama-closeout-volume-binding-invalid")
	}
	info, err := os.Lstat(mountpoint)
	metadata, ok := infoSys(info)
	if err != nil || !ok || !info.IsDir() || info.Mode().Perm() != 0o755 ||
		info.Mode()&os.ModeSymlink != 0 || metadata.Uid != 10001 ||
		metadata.Gid != 10001 || metadata.Nlink < 2 ||
		uint64(metadata.Dev) != proof.PublicVolumeDevice ||
		metadata.Ino != proof.PublicVolumeInode {
		return nil, fmt.Errorf("ai-panorama-closeout-volume-identity-invalid")
	}
	if err := validateAiPanoramaCloseoutVolumeConsumers(ctx, mountpoint, proof); err != nil {
		return nil, err
	}
	return &aiPanoramaRuntimeObservation{
		DockerRoot: dockerRoot, ImageID: proof.WebImageID,
		PublicVolumeMountpoint: mountpoint,
		PublicVolumeDevice:     uint64(metadata.Dev), PublicVolumeInode: metadata.Ino,
		PublicVolumeUID: metadata.Uid, PublicVolumeGID: metadata.Gid,
		PublicVolumeMode: uint32(info.Mode().Perm()),
	}, nil
}

func validateAiPanoramaCloseoutVolumeConsumers(
	ctx context.Context,
	mountpoint string,
	proof *aiPanoramaInstalledProof,
) error {
	raw, err := executeAiPanoramaDocker(
		ctx, DockerExecutablePath, "container", "ls", "--all", "--no-trunc",
		"--filter", "volume="+aiPanoramaPublicVolumeName,
		"--format", `{{.ID}}|{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.service"}}`,
	)
	if err != nil {
		return fmt.Errorf("ai-panorama-closeout-consumers-unavailable")
	}
	rows := strings.Fields(string(raw))
	zero(raw)
	seen := make(map[string]bool, 3)
	for _, row := range rows {
		parts := strings.Split(row, "|")
		if len(parts) != 3 || !runtimeContainerIDPattern.MatchString(parts[0]) ||
			parts[1] != ProjectName || seen[parts[2]] ||
			(parts[2] != aiPanoramaAPIRuntimeService &&
				parts[2] != aiPanoramaSchedulerService &&
				parts[2] != aiPanoramaRenderService) {
			return fmt.Errorf("ai-panorama-closeout-consumer-set-invalid")
		}
		seen[parts[2]] = true
		container, err := observeAiPanoramaContainer(ctx, parts[0], parts[2])
		if err != nil {
			return fmt.Errorf("ai-panorama-closeout-consumer-invalid")
		}
		expectedImage := proof.WebImage
		expectedImageID := proof.WebImageID
		if parts[2] == aiPanoramaRenderService {
			expectedImage = proof.RenderImage
			expectedImageID = proof.RenderImageID
		}
		if expectedImageID == "" || container.ConfiguredImage != expectedImage ||
			container.ImageID != expectedImageID {
			return fmt.Errorf("ai-panorama-closeout-consumer-image-invalid")
		}
		mountRaw, err := executeAiPanoramaDocker(
			ctx, DockerExecutablePath, "container", "inspect", "--format",
			"{{range .Mounts}}{{json .}}{{println}}{{end}}", parts[0],
		)
		if err != nil {
			return fmt.Errorf("ai-panorama-closeout-consumer-mount-unavailable")
		}
		mountRows := bytes.Split(bytes.TrimSpace(mountRaw), []byte{'\n'})
		found := 0
		for _, mountRow := range mountRows {
			if len(bytes.TrimSpace(mountRow)) == 0 {
				continue
			}
			mount, err := strictJSON(bytes.TrimSpace(mountRow), 64*1024)
			if err != nil {
				zero(mountRaw)
				return fmt.Errorf("ai-panorama-closeout-consumer-mount-invalid")
			}
			if mount["Name"] != aiPanoramaPublicVolumeName {
				continue
			}
			if mount["Type"] != "volume" || mount["Source"] != mountpoint ||
				mount["Destination"] != aiPanoramaPublicMountTarget ||
				mount["Driver"] != "local" || mount["RW"] != false {
				zero(mountRaw)
				return fmt.Errorf("ai-panorama-closeout-consumer-mount-not-read-only")
			}
			found++
		}
		zero(mountRaw)
		if found != 1 {
			return fmt.Errorf("ai-panorama-closeout-consumer-mount-invalid")
		}
	}
	return nil
}

func aiPanoramaRevocationWire(revocationID string, revokedAt time.Time) ([]byte, error) {
	if !aiPanoramaNoncePattern.MatchString(revocationID) ||
		revokedAt.IsZero() || revokedAt.Location() != time.UTC {
		return nil, fmt.Errorf("ai-panorama-revocation-input-invalid")
	}
	raw, err := canonicalJSON(map[string]any{
		"authority":     "propertyquarry-release-control",
		"revocation_id": revocationID,
		"revoked_at":    revokedAt.Format(time.RFC3339Nano),
		"schema":        aiPanoramaRevocationSchema,
		"slug":          aiPanoramaPraterSlug,
		"status":        "revoked",
		"tour_sha256":   aiPanoramaExpectedTourDigest,
		"version":       json.Number("1"),
	})
	if err != nil || len(raw)+1 > aiPanoramaMaximumRevocationBytes {
		zero(raw)
		return nil, fmt.Errorf("ai-panorama-revocation-canonicalization-failed")
	}
	return append(raw, '\n'), nil
}

func readAiPanoramaRevocationAt(
	directory *os.File,
	expectedDevice uint64,
) (*aiPanoramaRevocationObservation, []byte, error) {
	if directory == nil || expectedDevice < 1 {
		return nil, nil, fmt.Errorf("ai-panorama-revocation-read-input-invalid")
	}
	fd, err := syscall.Openat(
		int(directory.Fd()), aiPanoramaRevocationLeaf,
		syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0,
	)
	if err != nil {
		if err == syscall.ENOENT {
			return nil, nil, os.ErrNotExist
		}
		return nil, nil, fmt.Errorf("ai-panorama-revocation-unavailable")
	}
	file := os.NewFile(uintptr(fd), aiPanoramaRevocationLeaf)
	defer file.Close()
	info, err := file.Stat()
	metadata, ok := infoSys(info)
	if err != nil || !ok || !info.Mode().IsRegular() || info.Mode().Perm() != 0o444 ||
		uint64(metadata.Dev) != expectedDevice || metadata.Uid != 0 ||
		metadata.Gid != 0 || metadata.Nlink != 1 ||
		info.Size() < 3 || info.Size() > aiPanoramaMaximumRevocationBytes {
		return nil, nil, fmt.Errorf("ai-panorama-revocation-metadata-invalid")
	}
	raw := make([]byte, info.Size())
	if _, err := io.ReadFull(file, raw); err != nil {
		zero(raw)
		return nil, nil, fmt.Errorf("ai-panorama-revocation-read-failed")
	}
	extra := []byte{0}
	count, readErr := file.Read(extra)
	zero(extra)
	if count != 0 || (readErr != nil && readErr != io.EOF) ||
		raw[len(raw)-1] != '\n' || raw[len(raw)-2] == '\n' ||
		bytes.IndexByte(raw[:len(raw)-1], '\n') >= 0 {
		zero(raw)
		return nil, nil, fmt.Errorf("ai-panorama-revocation-framing-invalid")
	}
	value, err := strictJSON(raw[:len(raw)-1], aiPanoramaMaximumRevocationBytes)
	if err != nil || !hasKeys(value,
		"authority", "revocation_id", "revoked_at", "schema",
		"slug", "status", "tour_sha256", "version",
	) || value["authority"] != "propertyquarry-release-control" ||
		value["schema"] != aiPanoramaRevocationSchema ||
		value["version"] != json.Number("1") ||
		value["slug"] != aiPanoramaPraterSlug ||
		value["status"] != "revoked" ||
		value["tour_sha256"] != aiPanoramaExpectedTourDigest {
		zero(raw)
		return nil, nil, fmt.Errorf("ai-panorama-revocation-invalid")
	}
	revocationID, idOK := exactString(value["revocation_id"])
	revokedAt, timeOK := parseAiPanoramaTimestamp(value["revoked_at"])
	canonical, canonicalErr := canonicalJSON(value)
	canonical = append(canonical, '\n')
	canonicalOK := canonicalErr == nil && bytes.Equal(canonical, raw)
	zero(canonical)
	if !idOK || !aiPanoramaNoncePattern.MatchString(revocationID) || !timeOK || !canonicalOK {
		zero(raw)
		return nil, nil, fmt.Errorf("ai-panorama-revocation-proof-invalid")
	}
	return &aiPanoramaRevocationObservation{
		RevocationID: revocationID,
		RevokedAt:    revokedAt.Format(time.RFC3339Nano),
		SHA256:       aiPanoramaRawSHA256(raw),
	}, raw, nil
}

func readAiPanoramaRevocation(
	runtime *aiPanoramaRuntimeObservation,
) (*aiPanoramaRevocationObservation, []byte, error) {
	if runtime == nil || runtime.PublicVolumeDevice < 1 ||
		runtime.PublicVolumeInode < 1 || runtime.PublicVolumeNeedsInitialization {
		return nil, nil, fmt.Errorf("ai-panorama-revocation-runtime-invalid")
	}
	directory, err := os.OpenFile(
		runtime.PublicVolumeMountpoint,
		os.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0,
	)
	if err != nil {
		return nil, nil, fmt.Errorf("ai-panorama-revocation-root-unavailable")
	}
	defer directory.Close()
	info, err := directory.Stat()
	metadata, ok := infoSys(info)
	if err != nil || !ok || uint64(metadata.Dev) != runtime.PublicVolumeDevice ||
		metadata.Ino != runtime.PublicVolumeInode || metadata.Uid != 10001 ||
		metadata.Gid != 10001 || info.Mode().Perm() != 0o755 {
		return nil, nil, fmt.Errorf("ai-panorama-revocation-root-changed")
	}
	return readAiPanoramaRevocationAt(directory, runtime.PublicVolumeDevice)
}

func inspectAiPanoramaRevocationForRequest(
	runtime *aiPanoramaRuntimeObservation,
	expected []byte,
) (string, *aiPanoramaRevocationObservation, error) {
	if runtime == nil || len(expected) < 3 ||
		len(expected) > aiPanoramaMaximumRevocationBytes {
		return "", nil, fmt.Errorf("ai-panorama-revocation-inspection-input-invalid")
	}
	directory, err := os.OpenFile(
		runtime.PublicVolumeMountpoint,
		os.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0,
	)
	if err != nil {
		return "", nil, fmt.Errorf("ai-panorama-revocation-root-unavailable")
	}
	defer directory.Close()
	rootInfo, err := directory.Stat()
	rootMetadata, rootOK := infoSys(rootInfo)
	if err != nil || !rootOK || uint64(rootMetadata.Dev) != runtime.PublicVolumeDevice ||
		rootMetadata.Ino != runtime.PublicVolumeInode {
		return "", nil, fmt.Errorf("ai-panorama-revocation-root-changed")
	}
	fd, err := syscall.Openat(
		int(directory.Fd()), aiPanoramaRevocationLeaf,
		syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0,
	)
	if err == syscall.ENOENT {
		return "absent", nil, nil
	}
	if err != nil {
		return "", nil, fmt.Errorf("ai-panorama-revocation-unavailable")
	}
	file := os.NewFile(uintptr(fd), aiPanoramaRevocationLeaf)
	info, statErr := file.Stat()
	metadata, metadataOK := infoSys(info)
	mode := os.FileMode(0)
	if info != nil {
		mode = info.Mode().Perm()
	}
	modeOK := mode == 0o444 || mode&^os.FileMode(0o600) == 0
	if statErr != nil || !metadataOK || !info.Mode().IsRegular() ||
		!modeOK || uint64(metadata.Dev) != runtime.PublicVolumeDevice ||
		metadata.Uid != 0 || metadata.Gid != 0 || metadata.Nlink != 1 ||
		info.Size() < 0 || info.Size() > int64(len(expected)) {
		file.Close()
		return "", nil, fmt.Errorf("ai-panorama-revocation-metadata-invalid")
	}
	raw := make([]byte, info.Size())
	if len(raw) > 0 {
		if _, err := io.ReadFull(file, raw); err != nil {
			zero(raw)
			file.Close()
			return "", nil, fmt.Errorf("ai-panorama-revocation-read-failed")
		}
	}
	pathInfo, pathErr := os.Lstat(
		filepath.Join(runtime.PublicVolumeMountpoint, aiPanoramaRevocationLeaf),
	)
	valid := pathErr == nil && os.SameFile(info, pathInfo) &&
		bytes.Equal(raw, expected[:len(raw)])
	zero(raw)
	if file.Close() != nil || !valid {
		return "", nil, fmt.Errorf("ai-panorama-revocation-prefix-invalid")
	}
	if mode != 0o444 {
		return "incomplete", nil, nil
	}
	if info.Size() != int64(len(expected)) {
		return "", nil, fmt.Errorf("ai-panorama-revocation-final-invalid")
	}
	observation, finalRaw, err := readAiPanoramaRevocation(runtime)
	finalValid := err == nil && bytes.Equal(finalRaw, expected)
	zero(finalRaw)
	if !finalValid {
		return "", nil, fmt.Errorf("ai-panorama-revocation-final-invalid")
	}
	return "final", observation, nil
}

func persistAiPanoramaCloseoutRequest(root string, raw []byte) error {
	if len(raw) < 3 || len(raw) > aiPanoramaMaximumRevocationBytes {
		return fmt.Errorf("ai-panorama-closeout-request-input-invalid")
	}
	projection := &aiPanoramaProjection{
		Kind: "closeout-request", Path: aiPanoramaCloseoutRequestPath,
		Mode: 0o400, SHA256: aiPanoramaRawSHA256(raw), Raw: raw,
	}
	return persistAiPanoramaProjectionFile(root, projection)
}

func readAiPanoramaCloseoutRequest(root string) ([]byte, error) {
	ownerUID, ownerGID := secureOwner(root)
	raw, err := secureRead(
		root, aiPanoramaCloseoutRequestPath, 0o400,
		ownerUID, ownerGID, aiPanoramaMaximumRevocationBytes,
	)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-closeout-request-unavailable")
	}
	if len(raw) < 3 || len(raw) > aiPanoramaMaximumRevocationBytes ||
		raw[len(raw)-1] != '\n' || raw[len(raw)-2] == '\n' ||
		bytes.IndexByte(raw[:len(raw)-1], '\n') >= 0 {
		zero(raw)
		return nil, fmt.Errorf("ai-panorama-closeout-request-framing-invalid")
	}
	value, err := strictJSON(raw[:len(raw)-1], aiPanoramaMaximumRevocationBytes)
	if err != nil {
		zero(raw)
		return nil, fmt.Errorf("ai-panorama-closeout-request-invalid")
	}
	revocationID, idOK := exactString(value["revocation_id"])
	revokedAt, timeOK := parseAiPanoramaTimestamp(value["revoked_at"])
	expected, wireErr := aiPanoramaRevocationWire(revocationID, revokedAt)
	valid := idOK && timeOK && wireErr == nil && bytes.Equal(expected, raw)
	zero(expected)
	if !valid {
		zero(raw)
		return nil, fmt.Errorf("ai-panorama-closeout-request-invalid")
	}
	return raw, nil
}

func removeAiPanoramaCloseoutRequest(root string, raw []byte) error {
	if len(raw) < 3 || len(raw) > aiPanoramaMaximumRevocationBytes {
		return fmt.Errorf("ai-panorama-closeout-request-remove-input-invalid")
	}
	projection := &aiPanoramaProjection{
		Kind: "closeout-request", Path: aiPanoramaCloseoutRequestPath,
		Mode: 0o400, SHA256: aiPanoramaRawSHA256(raw), Raw: raw,
	}
	return removeAiPanoramaProjection(root, projection)
}

func aiPanoramaCloseoutRequestWasJournalBound(
	journal *Journal,
	raw []byte,
) bool {
	if journal == nil || len(raw) < 3 {
		return false
	}
	var lineage *JournalEvent
	for _, event := range unresolvedWorkflowOperations(journal) {
		if event.Operation != aiPanoramaCloseoutOperation {
			continue
		}
		if lineage != nil {
			return false
		}
		lineage = event
	}
	if lineage == nil ||
		!aiPanoramaCloseoutRecoveryEvent(lineage.EventType) ||
		!exactUniqueUnresolvedWorkflowEvent(
			journal, lineage, aiPanoramaCloseoutOperation,
		) {
		return false
	}
	projection, err := aiPanoramaCloseoutProjectionFromPayload(lineage.Payload)
	if projection != nil {
		defer projection.release()
	}
	return err == nil && bytes.Equal(projection.Raw, raw)
}

func aiPanoramaPriorCloseoutSucceededMarkerMatches(
	journal *Journal,
	observation *aiPanoramaRevocationObservation,
	raw []byte,
) bool {
	if journal == nil || observation == nil || len(raw) < 3 ||
		observation.SHA256 != aiPanoramaRawSHA256(raw) {
		return false
	}
	var terminal *JournalEvent
	for index := range journal.events {
		event := &journal.events[index]
		if event.Operation == aiPanoramaCloseoutOperation {
			terminal = event
		}
	}
	if terminal == nil ||
		terminal.EventType != aiPanoramaCloseoutSucceededEvent ||
		terminal.Payload["operation"] != aiPanoramaCloseoutOperation ||
		terminal.Payload["request_id"] != terminal.RequestID ||
		terminal.Payload["run_id"] != terminal.RunID ||
		terminal.Payload["ai_panorama_revocation_verified"] != true ||
		terminal.Payload["disposition"] != "revoked" ||
		terminal.Payload["production_ready"] != false {
		return false
	}
	runAttempt, runAttemptOK := exactInt(
		terminal.Payload["run_attempt"], 1, 1<<31-1,
	)
	revocation, revocationOK :=
		terminal.Payload["ai_panorama_revocation"].(map[string]any)
	if !runAttemptOK || runAttempt != terminal.RunAttempt ||
		!revocationOK || !hasKeys(
		revocation, "path", "revocation_id_sha256", "revoked_at",
		"sha256", "created",
	) {
		return false
	}
	_, createdOK := revocation["created"].(bool)
	return createdOK &&
		revocation["path"] ==
			aiPanoramaPublicMountTarget+"/"+aiPanoramaRevocationLeaf &&
		revocation["revocation_id_sha256"] ==
			aiPanoramaRawSHA256([]byte(observation.RevocationID)) &&
		revocation["revoked_at"] == observation.RevokedAt &&
		revocation["sha256"] == observation.SHA256
}

func validateAiPanoramaFreshCloseoutAdmission(journal *Journal) error {
	if journal == nil {
		return fmt.Errorf("ai-panorama-closeout-admission-journal-invalid")
	}
	for _, event := range unresolvedWorkflowOperations(journal) {
		if event.Operation == aiPanoramaCloseoutOperation {
			return fmt.Errorf(
				"ai-panorama-closeout-admission-unresolved-closeout",
			)
		}
	}
	return nil
}

func aiPanoramaRevokedManifestValid(manifest *aiPanoramaRelatedManifest) bool {
	if manifest == nil {
		return false
	}
	markerCount := 0
	copy := *manifest
	copy.Entries = make([]aiPanoramaManifestEntry, 0, len(manifest.Entries))
	for _, entry := range manifest.Entries {
		if entry.Path == aiPanoramaRevocationLeaf {
			if entry.Kind != "file" || entry.Mode != 0o444 ||
				entry.UID != 0 || entry.GID != 0 || entry.Nlink != 1 {
				return false
			}
			markerCount++
			continue
		}
		copy.Entries = append(copy.Entries, entry)
	}
	return markerCount == 1 && aiPanoramaPublishedManifestValid(&copy)
}

func aiPanoramaCloseoutBase(
	config *Config,
	request *workflowRequest,
	identity *Identity,
	installed *aiPanoramaInstalledProof,
	runtime *aiPanoramaRuntimeObservation,
	before *aiPanoramaProtectedNodeObservation,
) map[string]any {
	fields := authorityFields(config, request, identity)
	fields["ai_panorama_install_receipt_digest"] = installed.ReceiptDigest
	if installed.ManifestSHA256 != "" {
		fields["ai_panorama_installed_manifest_sha256"] = installed.ManifestSHA256
	}
	fields["ai_panorama_installed_web_image"] = installed.WebImage
	fields["ai_panorama_installed_web_image_id"] = installed.WebImageID
	fields["ai_panorama_closeout_emergency"] = installed.Emergency
	fields["ai_panorama_closeout_install_basis_event"] = installed.BasisEventType
	fields["ai_panorama_slug"] = aiPanoramaPraterSlug
	fields["ai_panorama_control_url"] = aiPanoramaPraterControlURL
	fields["ai_panorama_runtime_observation"] = aiPanoramaRuntimeObservationValue(runtime)
	fields["ai_panorama_before_protected_node"] =
		aiPanoramaProtectedNodeValue(before)
	fields["ai_panorama_before_protected_node_sha256"] = before.Digest
	fields["ai_panorama_closeout_reason_code"] = aiPanoramaCloseoutReasonCode
	fields["ai_panorama_closeout_reason_sha256"] =
		aiPanoramaRawSHA256([]byte(aiPanoramaCloseoutReasonCode))
	fields["ready"] = false
	fields["production_ready"] = false
	fields["release_effects_authorized"] = true
	fields["release_effects_performed"] = false
	fields["rollback_performed"] = false
	fields["recovery"] = false
	return fields
}

func aiPanoramaCloseoutTerminal(
	journal *Journal,
	base map[string]any,
	observation *aiPanoramaRevocationObservation,
	after *aiPanoramaProtectedNodeObservation,
	created bool,
) ([]byte, error) {
	terminal := cloneFields(base)
	terminal["ai_panorama_revocation"] = map[string]any{
		"path":                 aiPanoramaPublicMountTarget + "/" + aiPanoramaRevocationLeaf,
		"revocation_id_sha256": aiPanoramaRawSHA256([]byte(observation.RevocationID)),
		"revoked_at":           observation.RevokedAt, "sha256": observation.SHA256,
		"created": created,
	}
	terminal["ai_panorama_after_protected_node"] =
		aiPanoramaProtectedNodeValue(after)
	terminal["ai_panorama_after_protected_node_sha256"] = after.Digest
	terminal["ai_panorama_protected_node_unchanged"] =
		after.Digest == stringValue(base["ai_panorama_before_protected_node_sha256"])
	terminal["completed_at"] = json.Number(strconv.FormatInt(authorityNow().UTC().Unix(), 10))
	terminal["disposition"] = "revoked"
	terminal["production_ready"] = false
	terminal["release_effects_performed"] = created
	terminal["ai_panorama_revocation_verified"] = true
	return journal.Append(aiPanoramaCloseoutSucceededEvent, terminal)
}

func aiPanoramaCloseoutProjectionFromPayload(
	payload map[string]any,
) (*aiPanoramaProjection, error) {
	if payload == nil {
		return nil, fmt.Errorf("ai-panorama-closeout-projection-missing")
	}
	projection, err := parseAiPanoramaProjection(
		payload["ai_panorama_closeout_projection"],
	)
	if err != nil || projection.Kind != "closeout-request" ||
		projection.Path != aiPanoramaCloseoutRequestPath ||
		projection.Mode != 0o400 {
		if projection != nil {
			projection.release()
		}
		return nil, fmt.Errorf("ai-panorama-closeout-projection-invalid")
	}
	if payload["ai_panorama_closeout_request_sha256"] != projection.SHA256 {
		projection.release()
		return nil, fmt.Errorf("ai-panorama-closeout-projection-binding-invalid")
	}
	return projection, nil
}

func findAiPanoramaCloseoutProjectionForMarker(
	journal *Journal,
	config *Config,
	runtime *aiPanoramaRuntimeObservation,
) (*aiPanoramaProjection, error) {
	if journal == nil || config == nil || runtime == nil {
		return nil, fmt.Errorf("ai-panorama-closeout-projection-search-invalid")
	}
	var lineage *JournalEvent
	for _, event := range unresolvedWorkflowOperations(journal) {
		if event.Operation == aiPanoramaCloseoutOperation {
			if lineage != nil {
				return nil, fmt.Errorf("ai-panorama-closeout-marker-binding-ambiguous")
			}
			lineage = event
		}
	}
	if lineage == nil ||
		!aiPanoramaCloseoutRecoveryEvent(lineage.EventType) ||
		!aiPanoramaCloseoutRequestAuthorityMatches(lineage, config) {
		return nil, fmt.Errorf("ai-panorama-closeout-marker-unbound")
	}
	projection, err := aiPanoramaCloseoutProjectionFromPayload(lineage.Payload)
	if err != nil {
		return nil, err
	}
	state, _, inspectErr := inspectAiPanoramaRevocationForRequest(
		runtime, projection.Raw,
	)
	if inspectErr != nil || (state != "incomplete" && state != "final") {
		projection.release()
		if inspectErr != nil {
			return nil, inspectErr
		}
		return nil, fmt.Errorf("ai-panorama-closeout-marker-unbound")
	}
	return projection, nil
}

func continueAiPanoramaCloseout(
	parent context.Context,
	root string,
	journal *Journal,
	config *Config,
	installed *aiPanoramaInstalledProof,
	runtime *aiPanoramaRuntimeObservation,
	base map[string]any,
	projection *aiPanoramaProjection,
) ([]byte, error) {
	if parent == nil || journal == nil || config == nil || installed == nil ||
		runtime == nil || base == nil || projection == nil {
		return nil, fmt.Errorf("ai-panorama-closeout-continuation-input-invalid")
	}
	state, observation, inspectErr := inspectAiPanoramaRevocationForRequest(
		runtime, projection.Raw,
	)
	containerVerified := base["ai_panorama_closeout_container_verified"] == true
	if inspectErr != nil {
		return nil, aiPanoramaCloseoutRecoveryError(
			journal, base, "revocation-prefix-classification-ambiguous",
		)
	}
	if state != "final" || !containerVerified {
		if err := persistAiPanoramaCloseoutRequest(
			root, projection.Raw,
		); err != nil {
			return nil, aiPanoramaCloseoutRecoveryError(
				journal, base, "request-publication-ambiguous",
			)
		}
		persisted := cloneFields(base)
		persisted["disposition"] = "closeout-request-persisted"
		if err := appendAiPanoramaJournalEvent(
			journal, aiPanoramaCloseoutRequestPersisted, persisted,
		); err != nil {
			return nil, err
		}
		closeoutConfig := *config
		closeoutConfig.WebImage = installed.WebImage
		cleanupContext, cancel := context.WithTimeout(
			context.WithoutCancel(parent), aiPanoramaCleanupTimeout,
		)
		if err := cleanupAiPanoramaPhaseContainer(
			cleanupContext, &closeoutConfig, runtime, nil, "closeout",
		); err != nil {
			cancel()
			return nil, aiPanoramaCloseoutRecoveryError(
				journal, base, "closeout-container-cleanup-ambiguous",
			)
		}
		cancel()
		stdout, phaseErr := runAiPanoramaContainerRaw(
			parent, &closeoutConfig, runtime, nil, nil, "closeout", "",
		)
		stdoutMatches := phaseErr == nil && bytes.Equal(stdout, projection.Raw)
		zero(stdout)
		if !stdoutMatches {
			return nil, aiPanoramaCloseoutRecoveryError(
				journal, base, "closeout-container-not-verified",
			)
		}
		state, observation, inspectErr = inspectAiPanoramaRevocationForRequest(
			runtime, projection.Raw,
		)
		if inspectErr != nil || state != "final" || observation == nil {
			return nil, aiPanoramaCloseoutRecoveryError(
				journal, base, "revocation-publication-ambiguous",
			)
		}
		base["ai_panorama_closeout_container_verified"] = true
		base["ai_panorama_revocation_sha256"] = observation.SHA256
		verified := cloneFields(base)
		verified["disposition"] = "closeout-container-verified"
		if err := appendAiPanoramaJournalEvent(
			journal, aiPanoramaCloseoutContainerVerified, verified,
		); err != nil {
			return nil, err
		}
	}
	if observation == nil {
		state, observation, inspectErr = inspectAiPanoramaRevocationForRequest(
			runtime, projection.Raw,
		)
		if inspectErr != nil || state != "final" || observation == nil {
			return nil, aiPanoramaCloseoutRecoveryError(
				journal, base, "revocation-final-classification-ambiguous",
			)
		}
	}
	after, afterErr := observeAiPanoramaProtectedNode(runtime)
	beforeDigest, beforeOK := exactString(
		base["ai_panorama_before_protected_node_sha256"],
	)
	if afterErr != nil || !beforeOK || after.Digest != beforeDigest {
		return nil, aiPanoramaCloseoutRecoveryError(
			journal, base, "protected-node-audit-ambiguous",
		)
	}
	if err := removeAiPanoramaCloseoutRequest(
		root, projection.Raw,
	); err != nil {
		return nil, aiPanoramaCloseoutRecoveryError(
			journal, base, "request-cleanup-ambiguous",
		)
	}
	return aiPanoramaCloseoutTerminal(
		journal, base, observation, after, true,
	)
}

func aiPanoramaOrphanRuntimeMatchesInstalled(
	runtime *aiPanoramaRuntimeObservation,
	installed *aiPanoramaInstalledProof,
) bool {
	return runtime != nil && installed != nil &&
		runtime.DockerRoot == installed.DockerRoot &&
		runtime.ImageID == installed.WebImageID &&
		runtime.PublicVolumeMountpoint == installed.PublicVolumeMountpoint &&
		runtime.PublicVolumeDevice == installed.PublicVolumeDevice &&
		runtime.PublicVolumeInode == installed.PublicVolumeInode &&
		runtime.PublicVolumeUID == 10001 &&
		runtime.PublicVolumeGID == 10001 &&
		runtime.PublicVolumeMode == 0o755 &&
		!runtime.PublicVolumeNeedsInitialization
}

func aiPanoramaApplyOrphanCleanupAuthorized(event *JournalEvent) bool {
	if event == nil || event.Operation != aiPanoramaInstallOperation ||
		event.Payload["release_effects_performed"] != true {
		return false
	}
	switch event.EventType {
	case aiPanoramaInstallMutationStartedEvent,
		aiPanoramaInstallMutationVerifiedEvent,
		aiPanoramaInstallRecoveryRequiredEvent,
		aiPanoramaAttemptProjectionsCleanedEvent:
		return true
	default:
		return false
	}
}

func aiPanoramaPreflightOrphanCleanupAuthorized(event *JournalEvent) bool {
	if event == nil || event.Operation != aiPanoramaInstallOperation {
		return false
	}
	switch event.EventType {
	case aiPanoramaInstallPreflightStartedEvent,
		aiPanoramaInstallPreflightReadyEvent,
		aiPanoramaInstallMutationStartedEvent,
		aiPanoramaInstallMutationVerifiedEvent,
		aiPanoramaInstallRecoveryRequiredEvent,
		aiPanoramaAttemptProjectionsCleanedEvent:
		return true
	default:
		return false
	}
}

func aiPanoramaCleanupConfigFromInstallEvent(
	event *JournalEvent,
	current *Config,
	installed *aiPanoramaInstalledProof,
) (*Config, error) {
	if event == nil || current == nil || installed == nil ||
		!aiPanoramaCloseoutInstallConfigBasisMatches(event.Payload, current) {
		return nil, fmt.Errorf("ai-panorama-cleanup-config-basis-invalid")
	}
	configDigest, configOK := exactString(event.Payload["config_digest"])
	runtimeSHA, runtimeOK := exactString(event.Payload["runtime_sha"])
	deploymentID, deploymentOK := exactString(event.Payload["deployment_id"])
	webImage, imageOK := exactString(event.Payload["web_image"])
	if !configOK || !digestPattern.MatchString(configDigest) ||
		!runtimeOK || !shaPattern.MatchString(runtimeSHA) ||
		!deploymentOK || !deploymentIDPattern.MatchString(deploymentID) ||
		!imageOK || webImage != installed.WebImage {
		return nil, fmt.Errorf("ai-panorama-cleanup-config-invalid")
	}
	return &Config{
		Digest:       configDigest,
		RuntimeSHA:   runtimeSHA,
		DeploymentID: deploymentID,
		WebImage:     webImage,
	}, nil
}

func aiPanoramaCloseoutOrphanCleanupAuthorized(event *JournalEvent) bool {
	if event == nil || event.Operation != aiPanoramaCloseoutOperation ||
		!aiPanoramaCloseoutRecoveryEvent(event.EventType) {
		return false
	}
	switch event.EventType {
	case aiPanoramaCloseoutRequestPersisted,
		aiPanoramaCloseoutContainerVerified,
		aiPanoramaCloseoutRecoveryRequired:
		projection, err :=
			aiPanoramaCloseoutProjectionFromPayload(event.Payload)
		if projection != nil {
			projection.release()
		}
		return err == nil
	default:
		return false
	}
}

func cleanupAiPanoramaCloseoutOrphansBeforeObservation(
	parent context.Context,
	root string,
	journal *Journal,
	config *Config,
	installed *aiPanoramaInstalledProof,
	closeout *JournalEvent,
) error {
	if parent == nil || !filepath.IsAbs(root) ||
		filepath.Clean(root) != root || journal == nil || config == nil ||
		installed == nil {
		return fmt.Errorf("ai-panorama-closeout-orphan-cleanup-input-invalid")
	}
	cleanupContext, cancel := context.WithTimeout(
		context.WithoutCancel(parent), aiPanoramaCleanupTimeout,
	)
	defer cancel()
	if installed.Emergency {
		var install *JournalEvent
		for _, candidate := range unresolvedWorkflowOperations(journal) {
			if candidate.Operation != aiPanoramaInstallOperation {
				continue
			}
			if install != nil {
				return fmt.Errorf(
					"ai-panorama-closeout-install-cleanup-lineage-ambiguous",
				)
			}
			install = candidate
		}
		if install == nil || install.ReceiptDigest != installed.ReceiptDigest {
			return fmt.Errorf(
				"ai-panorama-closeout-install-cleanup-lineage-invalid",
			)
		}
		runtimeValue, runtimeOK :=
			install.Payload["ai_panorama_runtime_observation"].(map[string]any)
		runtime, runtimeErr :=
			parseAiPanoramaRuntimeObservationValue(runtimeValue)
		if !runtimeOK || runtimeErr != nil ||
			!aiPanoramaOrphanRuntimeMatchesInstalled(runtime, installed) {
			return fmt.Errorf(
				"ai-panorama-closeout-install-cleanup-runtime-invalid",
			)
		}
		cleanupConfig, configErr :=
			aiPanoramaCleanupConfigFromInstallEvent(
				install, config, installed,
			)
		if configErr != nil {
			return configErr
		}
		preflightExists, err := aiPanoramaPhaseContainerExists(
			cleanupContext, cleanupConfig, "preflight",
		)
		if err != nil {
			return err
		}
		applyExists, err := aiPanoramaPhaseContainerExists(
			cleanupContext, cleanupConfig, "apply",
		)
		if err != nil {
			return err
		}
		networkName, networkErr := aiPanoramaNetworkName(cleanupConfig)
		if networkErr != nil {
			return networkErr
		}
		networkExists, err := aiPanoramaNetworkExists(
			cleanupContext, networkName,
		)
		if err != nil {
			return err
		}
		if (applyExists || networkExists) &&
			!aiPanoramaApplyOrphanCleanupAuthorized(install) {
			return fmt.Errorf(
				"ai-panorama-closeout-install-cleanup-not-authorized",
			)
		}
		if preflightExists &&
			!aiPanoramaPreflightOrphanCleanupAuthorized(install) {
			return fmt.Errorf(
				"ai-panorama-closeout-preflight-cleanup-not-authorized",
			)
		}
		if preflightExists {
			if err := cleanupAiPanoramaRecoveryPhaseContainer(
				cleanupContext, cleanupConfig, runtime,
				"preflight", "none",
			); err != nil {
				return err
			}
		}
		if applyExists {
			if err := cleanupAiPanoramaRecoveryPhaseContainer(
				cleanupContext, cleanupConfig, runtime, "apply", networkName,
			); err != nil {
				return err
			}
		}
		if networkExists {
			if runtime.DatabaseContainerID == "" ||
				cleanupAiPanoramaNetwork(
					cleanupContext, cleanupConfig, runtime,
				) != nil {
				return fmt.Errorf(
					"ai-panorama-closeout-install-network-cleanup-failed",
				)
			}
		}
		if err := destroyAiPanoramaDatabaseSecret(root); err != nil {
			return fmt.Errorf(
				"ai-panorama-closeout-install-secret-cleanup-failed",
			)
		}
	}
	if closeout == nil {
		return nil
	}
	if !exactUniqueUnresolvedWorkflowEvent(
		journal, closeout, aiPanoramaCloseoutOperation,
	) {
		return fmt.Errorf("ai-panorama-closeout-orphan-lineage-invalid")
	}
	exists, err := aiPanoramaPhaseContainerExists(
		cleanupContext, config, "closeout",
	)
	if err != nil || !exists {
		return err
	}
	if !aiPanoramaCloseoutOrphanCleanupAuthorized(closeout) {
		return fmt.Errorf("ai-panorama-closeout-orphan-cleanup-not-authorized")
	}
	runtimeValue, runtimeOK :=
		closeout.Payload["ai_panorama_runtime_observation"].(map[string]any)
	runtime, runtimeErr := parseAiPanoramaRuntimeObservationValue(runtimeValue)
	if !runtimeOK || runtimeErr != nil ||
		!aiPanoramaOrphanRuntimeMatchesInstalled(runtime, installed) {
		return fmt.Errorf("ai-panorama-closeout-orphan-runtime-invalid")
	}
	closeoutConfig := *config
	closeoutConfig.WebImage = installed.WebImage
	return cleanupAiPanoramaRecoveryPhaseContainer(
		cleanupContext, &closeoutConfig, runtime, "closeout", "none",
	)
}

func executeAiPanoramaCloseout(
	parent context.Context,
	root string,
	journal *Journal,
	config *Config,
	request *workflowRequest,
	identity *Identity,
) ([]byte, error) {
	if parent == nil || root != "/" || journal == nil || config == nil ||
		request == nil || identity == nil {
		return nil, fmt.Errorf("ai-panorama-closeout-input-invalid")
	}
	if err := validateAiPanoramaFreshCloseoutAdmission(journal); err != nil {
		return nil, err
	}
	installed, err := findAiPanoramaInstalledProof(journal, config)
	if err != nil {
		return nil, err
	}
	if err := cleanupAiPanoramaCloseoutOrphansBeforeObservation(
		parent, root, journal, config, installed, nil,
	); err != nil {
		return nil, fmt.Errorf("ai-panorama-closeout-orphan-cleanup-invalid")
	}
	runtime, err := observeAiPanoramaCloseoutRuntime(parent, installed)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-closeout-runtime-invalid")
	}
	before, err := observeAiPanoramaProtectedNode(runtime)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-closeout-protected-node-unavailable")
	}
	if !before.Present {
		return nil, fmt.Errorf("ai-panorama-closeout-protected-node-absent")
	}
	observation, markerRaw, markerErr := readAiPanoramaRevocation(runtime)
	if markerRaw != nil {
		defer zero(markerRaw)
	}
	markerPath := filepath.Join(
		runtime.PublicVolumeMountpoint, aiPanoramaRevocationLeaf,
	)
	_, markerStatErr := os.Lstat(markerPath)
	markerPresent := markerStatErr == nil
	if markerStatErr != nil && !os.IsNotExist(markerStatErr) {
		return nil, fmt.Errorf(
			"ai-panorama-closeout-marker-observation-ambiguous",
		)
	}
	requestPath := rooted(root, aiPanoramaCloseoutRequestPath)
	_, requestErr := os.Lstat(requestPath)
	requestPresent := requestErr == nil
	if requestErr != nil && !os.IsNotExist(requestErr) {
		return nil, fmt.Errorf(
			"ai-panorama-closeout-request-observation-ambiguous",
		)
	}
	if markerErr == nil {
		if !markerPresent {
			return nil, fmt.Errorf(
				"ai-panorama-closeout-marker-identity-ambiguous",
			)
		}
		if requestPresent {
			if !aiPanoramaCloseoutRequestWasJournalBound(
				journal, markerRaw,
			) {
				return nil, fmt.Errorf(
					"ai-panorama-closeout-existing-request-unbound",
				)
			}
		} else if !aiPanoramaPriorCloseoutSucceededMarkerMatches(
			journal, observation, markerRaw,
		) {
			return nil, fmt.Errorf(
				"ai-panorama-closeout-existing-marker-unbound",
			)
		}
	} else {
		if markerPresent {
			return nil, fmt.Errorf(
				"ai-panorama-closeout-incomplete-marker-unbound",
			)
		}
		if !os.IsNotExist(markerErr) {
			return nil, fmt.Errorf(
				"ai-panorama-closeout-marker-observation-ambiguous",
			)
		}
		if requestPresent {
			return nil, fmt.Errorf(
				"ai-panorama-closeout-existing-request-unbound",
			)
		}
	}
	base := aiPanoramaCloseoutBase(config, request, identity, installed, runtime, before)
	base["admitted_at"] = json.Number(strconv.FormatInt(authorityNow().UTC().Unix(), 10))
	base["disposition"] = "closeout-admitted"
	if err := appendAiPanoramaJournalEvent(
		journal, aiPanoramaCloseoutAdmittedEvent, cloneFields(base),
	); err != nil {
		return nil, err
	}
	if markerErr == nil {
		if requestPresent {
			if err := removeAiPanoramaCloseoutRequest(
				root, markerRaw,
			); err != nil {
				return nil, aiPanoramaCloseoutRecoveryError(
					journal, base,
					"existing-revocation-request-cleanup-ambiguous",
				)
			}
		}
		after, afterErr := observeAiPanoramaProtectedNode(runtime)
		if afterErr != nil || after.Digest != before.Digest {
			return nil, aiPanoramaCloseoutRecoveryError(
				journal, base, "existing-revocation-audit-ambiguous",
			)
		}
		return aiPanoramaCloseoutTerminal(
			journal, base, observation, after, false,
		)
	}
	revocationID, randomErr := newAiPanoramaInstanceID()
	if randomErr != nil {
		return nil, randomErr
	}
	requestRaw, err := aiPanoramaRevocationWire(
		revocationID, authorityNow().UTC().Truncate(time.Microsecond),
	)
	if err != nil {
		return nil, err
	}
	defer zero(requestRaw)
	projection := &aiPanoramaProjection{
		Kind: "closeout-request", Path: aiPanoramaCloseoutRequestPath,
		Mode: 0o400, SHA256: aiPanoramaRawSHA256(requestRaw),
		Raw: requestRaw,
	}
	base["ai_panorama_closeout_projection"] = projection.journalValue()
	base["ai_panorama_closeout_request_sha256"] = projection.SHA256
	base["disposition"] = "closeout-prepared"
	if err := appendAiPanoramaJournalEvent(
		journal, aiPanoramaCloseoutPreparedEvent, cloneFields(base),
	); err != nil {
		return nil, err
	}
	return continueAiPanoramaCloseout(
		parent, root, journal, config, installed, runtime, base, projection,
	)
}

func aiPanoramaCloseoutRecoveryError(
	journal *Journal,
	base map[string]any,
	disposition string,
) error {
	fields := cloneFields(base)
	fields["disposition"] = disposition
	fields["production_ready"] = false
	fields["recovery_required"] = true
	fields["observed_at"] = json.Number(strconv.FormatInt(authorityNow().UTC().Unix(), 10))
	wire, err := journal.Append(aiPanoramaCloseoutRecoveryRequired, fields)
	zero(wire)
	if err != nil {
		return err
	}
	return fmt.Errorf("ai-panorama-closeout-recovery-required")
}

func recoverIncompleteAiPanoramaCloseout(
	parent context.Context,
	root string,
	journal *Journal,
	config *Config,
	last *JournalEvent,
) error {
	if parent == nil || root != "/" || journal == nil || config == nil || last == nil ||
		last.Operation != aiPanoramaCloseoutOperation ||
		!exactUniqueUnresolvedWorkflowEvent(
			journal, last, aiPanoramaCloseoutOperation,
		) {
		return fmt.Errorf("ai-panorama-closeout-recovery-binding-invalid")
	}
	if !aiPanoramaCloseoutRecoveryEvent(last.EventType) ||
		!aiPanoramaCloseoutRequestAuthorityMatches(last, config) {
		return fmt.Errorf("ai-panorama-closeout-recovery-authority-invalid")
	}
	installed, err := findAiPanoramaInstalledProof(journal, config)
	if err != nil {
		return aiPanoramaCloseoutRecoveryError(journal, last.Payload, "installed-proof-classification-failed")
	}
	if err := cleanupAiPanoramaCloseoutOrphansBeforeObservation(
		parent, root, journal, config, installed, last,
	); err != nil {
		return aiPanoramaCloseoutRecoveryError(
			journal, last.Payload, "orphan-cleanup-classification-failed",
		)
	}
	runtime, err := observeAiPanoramaCloseoutRuntime(parent, installed)
	if err != nil {
		return aiPanoramaCloseoutRecoveryError(journal, last.Payload, "runtime-classification-failed")
	}
	runtimeValue, runtimeOK :=
		last.Payload["ai_panorama_runtime_observation"].(map[string]any)
	beforeValue, beforeValueOK :=
		last.Payload["ai_panorama_before_protected_node"].(map[string]any)
	beforeDigest, beforeOK := exactString(
		last.Payload["ai_panorama_before_protected_node_sha256"],
	)
	manifestValue, manifestPresent :=
		last.Payload["ai_panorama_installed_manifest_sha256"]
	manifestMatches := (!manifestPresent && installed.ManifestSHA256 == "") ||
		(manifestPresent && manifestValue == installed.ManifestSHA256 &&
			digestPattern.MatchString(installed.ManifestSHA256))
	if !runtimeOK || !canonicalValuesEqual(
		runtimeValue, aiPanoramaRuntimeObservationValue(runtime),
	) ||
		last.Payload["ai_panorama_install_receipt_digest"] !=
			installed.ReceiptDigest ||
		last.Payload["ai_panorama_installed_web_image"] !=
			installed.WebImage ||
		last.Payload["ai_panorama_installed_web_image_id"] !=
			installed.WebImageID ||
		last.Payload["ai_panorama_closeout_emergency"] !=
			installed.Emergency ||
		last.Payload["ai_panorama_closeout_install_basis_event"] !=
			installed.BasisEventType ||
		!manifestMatches ||
		!beforeValueOK || !beforeOK ||
		!aiPanoramaProtectedNodePayloadMatches(
			beforeValue, beforeDigest, runtime,
		) {
		return fmt.Errorf("ai-panorama-closeout-recovery-state-binding-invalid")
	}
	currentProtectedNode, currentErr :=
		observeAiPanoramaProtectedNode(runtime)
	if currentErr != nil || currentProtectedNode == nil ||
		!currentProtectedNode.Present ||
		currentProtectedNode.Digest != beforeDigest ||
		!canonicalValuesEqual(
			beforeValue,
			aiPanoramaProtectedNodeValue(currentProtectedNode),
		) {
		return fmt.Errorf("ai-panorama-closeout-recovery-protected-node-changed")
	}
	projection, projectionErr := aiPanoramaCloseoutProjectionFromPayload(
		last.Payload,
	)
	if projectionErr != nil {
		markerPath := filepath.Join(
			runtime.PublicVolumeMountpoint, aiPanoramaRevocationLeaf,
		)
		if _, markerErr := os.Lstat(markerPath); !os.IsNotExist(markerErr) {
			return aiPanoramaCloseoutRecoveryError(
				journal, last.Payload, "unbound-marker-classification-ambiguous",
			)
		}
		terminal := aiPanoramaRecoveryFields(last.Payload)
		terminal["completed_at"] = json.Number(strconv.FormatInt(authorityNow().UTC().Unix(), 10))
		terminal["disposition"] = "recovered-before-closeout-request-intent"
		terminal["production_ready"] = false
		terminal["release_effects_performed"] = false
		wire, err := journal.Append(aiPanoramaCloseoutFailedEvent, terminal)
		zero(wire)
		return err
	}
	defer projection.release()
	base := aiPanoramaRecoveryFields(last.Payload)
	wire, err := continueAiPanoramaCloseout(
		parent, root, journal, config, installed, runtime, base, projection,
	)
	zero(wire)
	return err
}
