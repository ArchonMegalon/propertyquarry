//go:build linux && amd64

package authority

import (
	"bytes"
	"crypto/ed25519"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

const (
	aiPanoramaTrustAssertionSchema = "propertyquarry.ai-panorama-install-trust-assertion.v1"
	aiPanoramaVolumeProfileSchema  = "propertyquarry.public-tour-volume-profile.v2"
	aiPanoramaComposePlanSchema    = "propertyquarry.public-tour-compose-plan.v1"
	aiPanoramaVolumeID             = "propertyquarry-governed-public-tours-production"
	aiPanoramaActorPrincipalID     = "propertyquarry-release-controller"
	aiPanoramaMaximumContextFile   = 256 * 1024
)

type aiPanoramaSigningContext struct {
	Subject                    string
	ActorPrincipalID           string
	ReviewReceiptSHA256        string
	WebImage                   string
	WebImageID                 string
	KeyID                      string
	KeyEpoch                   int64
	KeySHA256                  string
	KeyringSHA256              string
	VolumeProfileSHA256        string
	ComposePlanSHA256          string
	VolumeID                   string
	ArtifactRootDevice         uint64
	ArtifactRootInode          uint64
	PublicTourRootDevice       uint64
	PublicTourRootInode        uint64
	ExecutionLeaseSeconds      int64
	TrustAssertionSHA256       string
	TrustAssertionCanonicalRaw []byte
}

func (value *aiPanoramaSigningContext) release() {
	if value == nil {
		return
	}
	zero(value.TrustAssertionCanonicalRaw)
	*value = aiPanoramaSigningContext{}
}

func aiPanoramaReviewReceiptSHA256(receiptDigest string) (string, error) {
	if !digestPattern.MatchString(receiptDigest) {
		return "", fmt.Errorf("ai-panorama-review-receipt-invalid")
	}
	raw := strings.TrimPrefix(receiptDigest, "sha256:")
	if !aiPanoramaRawSHA256Pattern.MatchString(raw) {
		return "", fmt.Errorf("ai-panorama-review-receipt-invalid")
	}
	return raw, nil
}

func readAiPanoramaCanonicalContextFile(root, path string) (map[string]any, []byte, string, error) {
	ownerUID, ownerGID := secureOwner(root)
	raw, err := secureRead(root, path, 0o400, ownerUID, ownerGID, aiPanoramaMaximumContextFile)
	if err != nil || len(raw) < 3 || raw[len(raw)-1] != '\n' ||
		raw[len(raw)-2] == '\n' || bytes.IndexByte(raw[:len(raw)-1], '\n') >= 0 {
		zero(raw)
		return nil, nil, "", fmt.Errorf("ai-panorama-context-file-unavailable")
	}
	value, err := strictJSON(raw[:len(raw)-1], aiPanoramaMaximumContextFile)
	if err != nil {
		zero(raw)
		return nil, nil, "", fmt.Errorf("ai-panorama-context-file-invalid")
	}
	return value, raw, aiPanoramaRawSHA256(raw), nil
}

func loadAiPanoramaSigningContext(
	root string,
	config *Config,
	identity *Identity,
	runtime *aiPanoramaRuntimeObservation,
	sealed *aiPanoramaSealedArtifactObservation,
	key *aiPanoramaPurposeKey,
	releaseReceiptDigest string,
) (*aiPanoramaSigningContext, error) {
	if root == "" {
		root = "/"
	}
	if config == nil || identity == nil || runtime == nil || sealed == nil || key == nil ||
		identity.Subject != ImmutableOIDCSubjectPrefix+":environment:"+Environment ||
		identity.Repository != Repository || identity.Ref != "refs/heads/main" ||
		identity.CandidateSHA != config.WorkflowSHA || identity.WorkflowRef != WorkflowRef ||
		identity.Environment != Environment || config.WebImage == "" ||
		runtime.ImageID == "" || sealed.RootDevice < 1 || sealed.RootInode < 1 ||
		runtime.PublicVolumeDevice < 1 || runtime.PublicVolumeInode < 1 {
		return nil, fmt.Errorf("ai-panorama-signing-context-input-invalid")
	}
	reviewReceiptSHA256, err := aiPanoramaReviewReceiptSHA256(releaseReceiptDigest)
	if err != nil {
		return nil, err
	}

	compose, composeRaw, composeSHA256, err := readAiPanoramaCanonicalContextFile(root, aiPanoramaComposePlanPath)
	if err != nil {
		return nil, err
	}
	defer zero(composeRaw)
	if !hasKeys(compose,
		"schema", "version", "authority", "status", "environment", "web_image",
		"web_image_id", "volume_id", "storage_kind", "docker_volume_name",
		"container_mount_target", "artifact_mount_read_only", "web_mount_read_only",
		"publisher_mount_read_write", "artifact_root_device", "artifact_root_inode",
		"public_tour_root_device", "public_tour_root_inode",
	) || compose["schema"] != aiPanoramaComposePlanSchema ||
		compose["version"] != json.Number("1") ||
		compose["authority"] != "propertyquarry-release-control" ||
		compose["status"] != "active" || compose["environment"] != Environment ||
		compose["web_image"] != config.WebImage || compose["web_image_id"] != runtime.ImageID ||
		compose["volume_id"] != aiPanoramaVolumeID ||
		compose["storage_kind"] != "docker-named-volume" ||
		compose["docker_volume_name"] != aiPanoramaPublicVolumeName ||
		compose["container_mount_target"] != aiPanoramaPublicMountTarget ||
		compose["artifact_mount_read_only"] != true ||
		compose["web_mount_read_only"] != true ||
		compose["publisher_mount_read_write"] != true ||
		!aiPanoramaContextIdentityMatches(compose, sealed, runtime) {
		return nil, fmt.Errorf("ai-panorama-compose-plan-binding-invalid")
	}

	profile, profileRaw, profileSHA256, err := readAiPanoramaCanonicalContextFile(root, aiPanoramaVolumeProfilePath)
	if err != nil {
		return nil, err
	}
	defer zero(profileRaw)
	if !hasKeys(profile,
		"schema", "version", "authority", "status", "environment", "volume_id",
		"logical_purpose", "application_setting", "application_setting_value",
		"storage_kind", "docker_volume_name", "container_mount_source",
		"container_mount_target", "runtime_uid", "runtime_gid", "artifact_root",
		"artifact_root_device", "artifact_root_inode", "artifact_mount_read_only",
		"public_tour_root", "public_tour_root_device", "public_tour_root_inode",
		"compose_plan_sha256",
	) || profile["schema"] != aiPanoramaVolumeProfileSchema ||
		profile["version"] != json.Number("2") ||
		profile["authority"] != "propertyquarry-release-control" ||
		profile["status"] != "active" || profile["environment"] != Environment ||
		profile["volume_id"] != aiPanoramaVolumeID ||
		profile["logical_purpose"] != "governed-public-tours" ||
		profile["application_setting"] != "EA_GOVERNED_PUBLIC_TOUR_DIR" ||
		profile["application_setting_value"] != aiPanoramaPublicMountTarget ||
		profile["storage_kind"] != "docker-named-volume" ||
		profile["docker_volume_name"] != aiPanoramaPublicVolumeName ||
		profile["container_mount_source"] != runtime.PublicVolumeMountpoint ||
		profile["container_mount_target"] != aiPanoramaPublicMountTarget ||
		profile["artifact_root"] != aiPanoramaSealedArtifactRoot ||
		profile["public_tour_root"] != aiPanoramaPublicMountTarget ||
		profile["artifact_mount_read_only"] != true ||
		profile["compose_plan_sha256"] != composeSHA256 ||
		!aiPanoramaExactContextInt(profile["runtime_uid"], 10001) ||
		!aiPanoramaExactContextInt(profile["runtime_gid"], 10001) ||
		!aiPanoramaContextIdentityMatches(profile, sealed, runtime) {
		return nil, fmt.Errorf("ai-panorama-volume-profile-binding-invalid")
	}

	trust, trustRaw, trustSHA256, err := readAiPanoramaCanonicalContextFile(root, aiPanoramaTrustAssertionPath)
	if err != nil {
		return nil, err
	}
	context := &aiPanoramaSigningContext{TrustAssertionCanonicalRaw: trustRaw}
	if !hasKeys(trust,
		"schema", "version", "authority", "status", "subject", "actor_principal_id",
		"repository", "git_ref", "git_head_sha", "workflow_ref", "job", "environment",
		"review_receipt_sha256", "web_image", "web_image_id", "key_usage", "key_id",
		"key_epoch", "key_sha256", "keyring_sha256", "volume_profile_sha256",
		"compose_plan_sha256", "volume_id", "artifact_root_device",
		"artifact_root_inode", "public_tour_root_device", "public_tour_root_inode",
		"execution_lease_seconds",
	) || trust["schema"] != aiPanoramaTrustAssertionSchema ||
		trust["version"] != json.Number("1") ||
		trust["authority"] != "propertyquarry-release-control" ||
		trust["status"] != "active" || trust["subject"] != identity.Subject ||
		trust["actor_principal_id"] != aiPanoramaActorPrincipalID ||
		trust["repository"] != Repository || trust["git_ref"] != identity.Ref ||
		trust["git_head_sha"] != identity.CandidateSHA ||
		trust["workflow_ref"] != identity.WorkflowRef ||
		trust["job"] != ReleaseJob || trust["environment"] != identity.Environment ||
		trust["review_receipt_sha256"] != reviewReceiptSHA256 ||
		trust["web_image"] != config.WebImage || trust["web_image_id"] != runtime.ImageID ||
		trust["key_usage"] != aiPanoramaPermitKeyUsage || trust["key_id"] != key.KeyID ||
		trust["key_sha256"] != key.PublicSHA256 ||
		trust["keyring_sha256"] != key.KeyringSHA256 ||
		trust["volume_profile_sha256"] != profileSHA256 ||
		trust["compose_plan_sha256"] != composeSHA256 ||
		trust["volume_id"] != aiPanoramaVolumeID ||
		!aiPanoramaContextIdentityMatches(trust, sealed, runtime) {
		context.release()
		return nil, fmt.Errorf("ai-panorama-trust-assertion-binding-invalid")
	}
	keyEpoch, keyEpochOK := exactInt(trust["key_epoch"], key.Epoch, key.Epoch)
	lease, leaseOK := exactInt(trust["execution_lease_seconds"], 1, 900)
	if !keyEpochOK || keyEpoch != key.Epoch || !leaseOK {
		context.release()
		return nil, fmt.Errorf("ai-panorama-trust-assertion-proof-invalid")
	}
	context.Subject = identity.Subject
	context.ActorPrincipalID = aiPanoramaActorPrincipalID
	context.ReviewReceiptSHA256 = reviewReceiptSHA256
	context.WebImage = config.WebImage
	context.WebImageID = runtime.ImageID
	context.KeyID = key.KeyID
	context.KeyEpoch = key.Epoch
	context.KeySHA256 = key.PublicSHA256
	context.KeyringSHA256 = key.KeyringSHA256
	context.VolumeProfileSHA256 = profileSHA256
	context.ComposePlanSHA256 = composeSHA256
	context.VolumeID = aiPanoramaVolumeID
	context.ArtifactRootDevice = sealed.RootDevice
	context.ArtifactRootInode = sealed.RootInode
	context.PublicTourRootDevice = runtime.PublicVolumeDevice
	context.PublicTourRootInode = runtime.PublicVolumeInode
	context.ExecutionLeaseSeconds = lease
	context.TrustAssertionSHA256 = trustSHA256
	return context, nil
}

func prepareAiPanoramaSigningContext(
	root string,
	config *Config,
	identity *Identity,
	runtime *aiPanoramaRuntimeObservation,
	sealed *aiPanoramaSealedArtifactObservation,
	key *aiPanoramaPurposeKey,
	releaseReceiptDigest string,
	beforePersist func([]aiPanoramaProjection) error,
) (*aiPanoramaSigningContext, error) {
	if root == "" {
		root = "/"
	}
	if config == nil || identity == nil || runtime == nil || sealed == nil || key == nil {
		return nil, fmt.Errorf("ai-panorama-signing-context-input-invalid")
	}
	reviewReceiptSHA256, err := aiPanoramaReviewReceiptSHA256(releaseReceiptDigest)
	if err != nil {
		return nil, err
	}
	compose := map[string]any{
		"schema": aiPanoramaComposePlanSchema, "version": json.Number("1"),
		"authority": "propertyquarry-release-control", "status": "active",
		"environment": Environment, "web_image": config.WebImage,
		"web_image_id": runtime.ImageID, "volume_id": aiPanoramaVolumeID,
		"storage_kind":             "docker-named-volume",
		"docker_volume_name":       aiPanoramaPublicVolumeName,
		"container_mount_target":   aiPanoramaPublicMountTarget,
		"artifact_mount_read_only": true, "web_mount_read_only": true,
		"publisher_mount_read_write": true,
		"artifact_root_device":       json.Number(fmt.Sprintf("%d", sealed.RootDevice)),
		"artifact_root_inode":        json.Number(fmt.Sprintf("%d", sealed.RootInode)),
		"public_tour_root_device":    json.Number(fmt.Sprintf("%d", runtime.PublicVolumeDevice)),
		"public_tour_root_inode":     json.Number(fmt.Sprintf("%d", runtime.PublicVolumeInode)),
	}
	composeRaw, err := aiPanoramaContextWire(compose)
	if err != nil {
		return nil, err
	}
	defer zero(composeRaw)
	composeSHA256 := aiPanoramaRawSHA256(composeRaw)
	profile := map[string]any{
		"schema": aiPanoramaVolumeProfileSchema, "version": json.Number("2"),
		"authority": "propertyquarry-release-control", "status": "active",
		"environment": Environment, "volume_id": aiPanoramaVolumeID,
		"logical_purpose":           "governed-public-tours",
		"application_setting":       "EA_GOVERNED_PUBLIC_TOUR_DIR",
		"application_setting_value": aiPanoramaPublicMountTarget,
		"storage_kind":              "docker-named-volume",
		"docker_volume_name":        aiPanoramaPublicVolumeName,
		"container_mount_source":    runtime.PublicVolumeMountpoint,
		"container_mount_target":    aiPanoramaPublicMountTarget,
		"runtime_uid":               json.Number("10001"), "runtime_gid": json.Number("10001"),
		"artifact_root":            aiPanoramaSealedArtifactRoot,
		"artifact_root_device":     json.Number(fmt.Sprintf("%d", sealed.RootDevice)),
		"artifact_root_inode":      json.Number(fmt.Sprintf("%d", sealed.RootInode)),
		"artifact_mount_read_only": true,
		"public_tour_root":         aiPanoramaPublicMountTarget,
		"public_tour_root_device":  json.Number(fmt.Sprintf("%d", runtime.PublicVolumeDevice)),
		"public_tour_root_inode":   json.Number(fmt.Sprintf("%d", runtime.PublicVolumeInode)),
		"compose_plan_sha256":      composeSHA256,
	}
	profileRaw, err := aiPanoramaContextWire(profile)
	if err != nil {
		return nil, err
	}
	defer zero(profileRaw)
	profileSHA256 := aiPanoramaRawSHA256(profileRaw)
	trust := map[string]any{
		"schema": aiPanoramaTrustAssertionSchema, "version": json.Number("1"),
		"authority": "propertyquarry-release-control", "status": "active",
		"subject": identity.Subject, "actor_principal_id": aiPanoramaActorPrincipalID,
		"repository": Repository, "git_ref": identity.Ref,
		"git_head_sha": identity.CandidateSHA, "workflow_ref": identity.WorkflowRef,
		"job": ReleaseJob, "environment": identity.Environment,
		"review_receipt_sha256": reviewReceiptSHA256,
		"web_image":             config.WebImage, "web_image_id": runtime.ImageID,
		"key_usage": aiPanoramaPermitKeyUsage, "key_id": key.KeyID,
		"key_epoch":  json.Number(fmt.Sprintf("%d", key.Epoch)),
		"key_sha256": key.PublicSHA256, "keyring_sha256": key.KeyringSHA256,
		"volume_profile_sha256": profileSHA256, "compose_plan_sha256": composeSHA256,
		"volume_id":               aiPanoramaVolumeID,
		"artifact_root_device":    json.Number(fmt.Sprintf("%d", sealed.RootDevice)),
		"artifact_root_inode":     json.Number(fmt.Sprintf("%d", sealed.RootInode)),
		"public_tour_root_device": json.Number(fmt.Sprintf("%d", runtime.PublicVolumeDevice)),
		"public_tour_root_inode":  json.Number(fmt.Sprintf("%d", runtime.PublicVolumeInode)),
		"execution_lease_seconds": json.Number("600"),
	}
	trustRaw, err := aiPanoramaContextWire(trust)
	if err != nil {
		return nil, err
	}
	defer zero(trustRaw)
	projections := []aiPanoramaProjection{
		{
			Kind: "compose-plan", Path: aiPanoramaComposePlanPath, Mode: 0o400,
			SHA256: composeSHA256, Raw: composeRaw,
		},
		{
			Kind: "volume-profile", Path: aiPanoramaVolumeProfilePath, Mode: 0o400,
			SHA256: profileSHA256, Raw: profileRaw,
		},
		{
			Kind: "trust-assertion", Path: aiPanoramaTrustAssertionPath, Mode: 0o400,
			SHA256: aiPanoramaRawSHA256(trustRaw), Raw: trustRaw,
		},
	}
	if beforePersist == nil {
		return nil, fmt.Errorf("ai-panorama-context-projection-intent-missing")
	}
	if err := beforePersist(projections); err != nil {
		return nil, err
	}
	for index := range projections {
		if err := persistAiPanoramaProjectionFile(
			root, &projections[index],
		); err != nil {
			return nil, err
		}
	}
	return loadAiPanoramaSigningContext(
		root, config, identity, runtime, sealed, key, releaseReceiptDigest,
	)
}

func aiPanoramaContextWire(value map[string]any) ([]byte, error) {
	raw, err := canonicalJSON(value)
	if err != nil || len(raw) < 2 || len(raw)+1 > aiPanoramaMaximumContextFile {
		zero(raw)
		return nil, fmt.Errorf("ai-panorama-context-wire-invalid")
	}
	return append(raw, '\n'), nil
}

func aiPanoramaExactContextInt(value any, expected int64) bool {
	observed, ok := exactInt(value, expected, expected)
	return ok && observed == expected
}

func aiPanoramaContextIdentityMatches(
	value map[string]any,
	sealed *aiPanoramaSealedArtifactObservation,
	runtime *aiPanoramaRuntimeObservation,
) bool {
	if value == nil || sealed == nil || runtime == nil ||
		sealed.RootDevice > 1<<62 || sealed.RootInode > 1<<62 ||
		runtime.PublicVolumeDevice > 1<<62 || runtime.PublicVolumeInode > 1<<62 {
		return false
	}
	artifactDevice, artifactDeviceOK := exactInt(
		value["artifact_root_device"], int64(sealed.RootDevice), int64(sealed.RootDevice),
	)
	artifactInode, artifactInodeOK := exactInt(
		value["artifact_root_inode"], int64(sealed.RootInode), int64(sealed.RootInode),
	)
	publicDevice, publicDeviceOK := exactInt(
		value["public_tour_root_device"], int64(runtime.PublicVolumeDevice), int64(runtime.PublicVolumeDevice),
	)
	publicInode, publicInodeOK := exactInt(
		value["public_tour_root_inode"], int64(runtime.PublicVolumeInode), int64(runtime.PublicVolumeInode),
	)
	return artifactDeviceOK && uint64(artifactDevice) == sealed.RootDevice &&
		artifactInodeOK && uint64(artifactInode) == sealed.RootInode &&
		publicDeviceOK && uint64(publicDevice) == runtime.PublicVolumeDevice &&
		publicInodeOK && uint64(publicInode) == runtime.PublicVolumeInode
}

func aiPanoramaSigningKeyForContext(
	root string,
	private ed25519.PrivateKey,
	issuedAt time.Time,
) (*aiPanoramaPurposeKey, error) {
	return loadAiPanoramaPurposeKey(root, private, issuedAt, authorityNow().UTC())
}
