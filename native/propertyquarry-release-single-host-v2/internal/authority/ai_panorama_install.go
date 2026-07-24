//go:build linux && amd64

package authority

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

const (
	aiPanoramaInstallOperation             = "ai-panorama-install"
	aiPanoramaInstallAdmittedEvent         = "ai-panorama-install-admitted"
	aiPanoramaInstallFenceReadyEvent       = "ai-panorama-install-fence-ready"
	aiPanoramaInstallPreflightStartedEvent = "ai-panorama-install-preflight-started"
	aiPanoramaInstallPreflightReadyEvent   = "ai-panorama-install-preflight-ready"
	aiPanoramaInstallMutationStartedEvent  = "ai-panorama-install-mutation-started"
	aiPanoramaInstallMutationVerifiedEvent = "ai-panorama-install-mutation-verified"
	aiPanoramaInstallRecoveryStartedEvent  = "ai-panorama-install-recovery-started"
	aiPanoramaInstallSucceededEvent        = "ai-panorama-install-succeeded"
	aiPanoramaInstallRolledBackEvent       = "ai-panorama-install-rolled-back"
	aiPanoramaInstallRecoveryRequiredEvent = "ai-panorama-install-recovery-required"
	aiPanoramaInstallFailedNoEffectsEvent  = "ai-panorama-install-failed-no-effects"
	aiPanoramaSealedArtifactIntentEvent    = "ai-panorama-install-sealed-artifact-intent"
	aiPanoramaSealedArtifactCleanedEvent   = "ai-panorama-install-sealed-artifact-stage-cleaned"

	aiPanoramaPraterSlug             = "prater-messe-maisonette-ai-360-053ad185e1c44b2e"
	aiPanoramaPraterControlURL       = "https://propertyquarry.com/tours/" + aiPanoramaPraterSlug + "/control"
	aiPanoramaPublicVolumeName       = "property_propertyquarry_governed_public_tours"
	aiPanoramaPublicMountTarget      = "/data/governed_public_property_tours"
	aiPanoramaPublicVolumeComposeKey = "propertyquarry_governed_public_tours"
	aiPanoramaLegacyPublicVolumeName = "property_propertyquarry_public_tours"
	aiPanoramaLegacyVolumeComposeKey = "propertyquarry_public_tours"
	aiPanoramaRevocationLeaf         = "." + aiPanoramaPraterSlug + ".revoked.v1.json"
	aiPanoramaReviewedArtifactRoot   = "/docker/property/state"
	aiPanoramaSealedArtifactRoot     = "/var/lib/propertyquarry-release-single-host-v2/ai-panorama-artifacts/prater-v1"
	aiPanoramaSealedArtifactParent   = "/var/lib/propertyquarry-release-single-host-v2/ai-panorama-artifacts"
	aiPanoramaSealedBundleRoot       = aiPanoramaSealedArtifactRoot + "/bundle"
	aiPanoramaSealedBundlePath       = aiPanoramaSealedBundleRoot + "/" + aiPanoramaPraterSlug
	aiPanoramaSealedMarkerPath       = aiPanoramaSealedBundleRoot + "/.propertyquarry-ai-panorama-candidate.json"
	aiPanoramaSealedReceiptPath      = aiPanoramaSealedArtifactRoot + "/materialization.receipt.json"
	aiPanoramaReviewedBundlePath     = aiPanoramaReviewedArtifactRoot + "/incoming_property_tours/prater-053ad185e1c44b2e/ai-panorama-v2-yaw65-final/" + aiPanoramaPraterSlug
	aiPanoramaReviewedMarkerPath     = aiPanoramaReviewedArtifactRoot + "/incoming_property_tours/prater-053ad185e1c44b2e/ai-panorama-v2-yaw65-final/.propertyquarry-ai-panorama-candidate.json"
	aiPanoramaReviewedReceiptPath    = aiPanoramaReviewedArtifactRoot + "/runtime/propertyquarry_source_reconcile_27c27669_20260723T194218Z.private/incoming-canonical-before/prater-053ad185e1c44b2e/ai-panorama-v2-yaw65-final.receipt.json"
	aiPanoramaExpectedSourceTree     = "fe2bdc9162d82236d70d0e74deb283bb06186026fd2c31c90431711cb87a775c"
	aiPanoramaExpectedTourDigest     = "c3795ca2956c18e3e8b1749611660052dac794a08dec7f47db212b51049cf849"
	aiPanoramaExpectedCoreDigest     = "15e9b6bac56c47363da0fe49b99697215833d9ea6c94ae43253bde4e288c401d"
	aiPanoramaExpectedMarkerDigest   = "bf436b0645e44b203fe9b0c2f01c88d1ddce25aa7b1a45d04fa27b805eaf73fd"
	aiPanoramaExpectedReceiptDigest  = "accba9c5b5575020d9cd6fcc299ed9653f6d8f094d58598e7bfc13db0061daba"
	aiPanoramaControlRoot            = "/var/lib/propertyquarry/release-control/ai-panorama-install"
	aiPanoramaRuntimeRoot            = "/run/propertyquarry-release-control/ai-panorama-install"
	aiPanoramaVolumeProfilePath      = aiPanoramaRuntimeRoot + "/public-tour-volume-profile.v2.json"
	aiPanoramaTrustAssertionPath     = aiPanoramaRuntimeRoot + "/ai-panorama-install-trust-assertion.v1.json"
	aiPanoramaPurposeKeyringPath     = "/etc/propertyquarry/release-control/ai-panorama-install-keyring.v1.json"
	aiPanoramaDatabaseSecretMount    = aiPanoramaRuntimeRoot + "/prater-ai-panorama-db-secrets.v1.json"
	aiPanoramaPermitSchema           = "propertyquarry.ai-panorama-install-permit.v2"
	aiPanoramaPermitSignatureDomain  = "propertyquarry.ai-panorama-install-permit.signature.v2"
	aiPanoramaPermitKeyUsage         = "propertyquarry.ai-panorama-install-permit.signing.v1"
	aiPanoramaKeyringSchema          = "propertyquarry.ai-panorama-install-keyring.v1"
	aiPanoramaControllerPython       = "/usr/local/bin/python"
	aiPanoramaDiscoveryEntrypoint    = "/usr/local/libexec/propertyquarry-prater-ai-panorama-record-discovery-v1.py"
	aiPanoramaPreflightEntrypoint    = "/usr/local/libexec/propertyquarry-prater-ai-panorama-preflight-v1.py"
	aiPanoramaControllerEntrypoint   = "/usr/local/libexec/propertyquarry-prater-ai-panorama-controller-v1.py"
	aiPanoramaBootstrapEntrypoint    = "/usr/local/libexec/propertyquarry-prater-governed-volume-bootstrap-v1.py"
	aiPanoramaCloseoutEntrypoint     = "/usr/local/libexec/propertyquarry-prater-ai-panorama-closeout-v1.py"
	aiPanoramaDatabaseService        = "propertyquarry-db"
	aiPanoramaAPIRuntimeService      = "propertyquarry-api"
	aiPanoramaSchedulerService       = "propertyquarry-scheduler"
	aiPanoramaRenderService          = "propertyquarry-render-tools"
	aiPanoramaNetworkLabel           = "propertyquarry.release-control.ai-panorama-install"
	aiPanoramaMaximumCommandOutput   = 2 * 1024 * 1024
	aiPanoramaMaximumFiles           = 512
	aiPanoramaMaximumDirectories     = 256
	aiPanoramaMaximumTreeBytes       = int64(128 * 1024 * 1024)
	aiPanoramaMaximumFileBytes       = int64(64 * 1024 * 1024)
	aiPanoramaCleanupTimeout         = time.Minute
	aiPanoramaBootstrapPhaseTimeout  = 3 * time.Minute
	aiPanoramaDiscoveryPhaseTimeout  = 4 * time.Minute
	aiPanoramaPreflightPhaseTimeout  = 4 * time.Minute
	aiPanoramaApplyPhaseTimeout      = 9 * time.Minute
	aiPanoramaCloseoutPhaseTimeout   = 4 * time.Minute
)

type aiPanoramaDockerCommand func(context.Context, string, ...string) ([]byte, error)

var executeAiPanoramaDocker aiPanoramaDockerCommand = runAiPanoramaDocker

type aiPanoramaRuntimeObservation struct {
	DockerRoot                      string
	ImageID                         string
	ControlRootDevice               uint64
	ControlRootInode                uint64
	PublicVolumeMountpoint          string
	PublicVolumeDevice              uint64
	PublicVolumeInode               uint64
	PublicVolumeUID                 uint32
	PublicVolumeGID                 uint32
	PublicVolumeMode                uint32
	PublicVolumeNeedsInitialization bool
	DatabaseContainerID             string
	DatabaseContainerName           string
	DatabaseImageID                 string
	APIRuntimeContainerID           string
	APIRuntimeContainerName         string
	APIRuntimeImageID               string
	SchedulerContainerID            string
	SchedulerContainerName          string
	SchedulerImageID                string
	RenderContainerID               string
	RenderContainerName             string
	RenderImageID                   string
}

type aiPanoramaManifestEntry struct {
	Path   string
	Kind   string
	Mode   uint32
	UID    uint32
	GID    uint32
	Nlink  uint64
	Size   int64
	SHA256 string
}

type aiPanoramaRelatedManifest struct {
	RootDevice uint64
	RootInode  uint64
	RootMode   uint32
	RootUID    uint32
	RootGID    uint32
	Entries    []aiPanoramaManifestEntry
	Digest     string
}

type aiPanoramaSourceFile struct {
	Path    string
	Size    int64
	SHA256  string
	Device  uint64
	Inode   uint64
	MountID uint64
	MtimeNS int64
	CtimeNS int64
	Content []byte
}

type aiPanoramaSourceSnapshot struct {
	Directories []string
	Files       []aiPanoramaSourceFile
	RootMountID uint64
	TreeSHA256  string
	TourSHA256  string
	TotalBytes  int64
}

func (snapshot *aiPanoramaSourceSnapshot) release() {
	if snapshot == nil {
		return
	}
	for index := range snapshot.Files {
		zero(snapshot.Files[index].Content)
	}
	*snapshot = aiPanoramaSourceSnapshot{}
}

func aiPanoramaRelatedName(name string) bool {
	return name == aiPanoramaPraterSlug || name == aiPanoramaRevocationLeaf ||
		strings.HasPrefix(name, "."+aiPanoramaPraterSlug+".ai-intake-") ||
		strings.HasPrefix(name, "."+aiPanoramaPraterSlug+".ai-rollback-")
}

func aiPanoramaManifestValue(manifest *aiPanoramaRelatedManifest) map[string]any {
	entries := make([]any, 0, len(manifest.Entries))
	for _, entry := range manifest.Entries {
		value := map[string]any{
			"gid":        json.Number(strconv.FormatUint(uint64(entry.GID), 10)),
			"kind":       entry.Kind,
			"mode":       json.Number(strconv.FormatUint(uint64(entry.Mode), 10)),
			"nlink":      json.Number(strconv.FormatUint(entry.Nlink, 10)),
			"path":       entry.Path,
			"size_bytes": json.Number(strconv.FormatInt(entry.Size, 10)),
			"uid":        json.Number(strconv.FormatUint(uint64(entry.UID), 10)),
		}
		if entry.Kind == "file" {
			value["sha256"] = entry.SHA256
		}
		entries = append(entries, value)
	}
	return map[string]any{
		"digest":      manifest.Digest,
		"entries":     entries,
		"root_device": json.Number(strconv.FormatUint(manifest.RootDevice, 10)),
		"root_gid":    json.Number(strconv.FormatUint(uint64(manifest.RootGID), 10)),
		"root_inode":  json.Number(strconv.FormatUint(manifest.RootInode, 10)),
		"root_mode":   json.Number(strconv.FormatUint(uint64(manifest.RootMode), 10)),
		"root_uid":    json.Number(strconv.FormatUint(uint64(manifest.RootUID), 10)),
	}
}

func aiPanoramaManifestDigest(manifest *aiPanoramaRelatedManifest) (string, error) {
	if manifest == nil {
		return "", fmt.Errorf("ai-panorama-manifest-missing")
	}
	value := aiPanoramaManifestValue(manifest)
	delete(value, "digest")
	raw, err := canonicalJSON(value)
	if err != nil {
		return "", fmt.Errorf("ai-panorama-manifest-canonicalization-failed")
	}
	defer zero(raw)
	return digest(raw), nil
}

func snapshotAiPanoramaSource(path string, uid, gid uint32, directoryMode, fileMode uint32, retain bool) (*aiPanoramaSourceSnapshot, error) {
	root, err := tourV4OpenDirectoryAbsolute(path)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-source-root-unavailable")
	}
	defer root.Close()
	rootInfo, err := root.Stat()
	rootMetadata, metadataErr := tourV4StatMetadata(rootInfo)
	rootMountID, mountIDErr := aiPanoramaFileMountID(root)
	if err != nil || metadataErr != nil || !rootInfo.IsDir() ||
		uint32(rootInfo.Mode().Perm()) != directoryMode ||
		rootMetadata.Uid != uid || rootMetadata.Gid != gid ||
		rootMetadata.Nlink < 2 || mountIDErr != nil {
		return nil, fmt.Errorf("ai-panorama-source-root-invalid")
	}
	snapshot := &aiPanoramaSourceSnapshot{
		Directories: []string{"."},
		RootMountID: rootMountID,
	}
	var walk func(*os.File, string) error
	walk = func(directory *os.File, prefix string) error {
		before, err := directory.Stat()
		directoryMountID, mountErr := aiPanoramaFileMountID(directory)
		if err != nil || mountErr != nil ||
			directoryMountID != rootMountID {
			return fmt.Errorf("ai-panorama-source-directory-stat-failed")
		}
		duplicate, err := syscall.Dup(int(directory.Fd()))
		if err != nil {
			return fmt.Errorf("ai-panorama-source-directory-dup-failed")
		}
		reader := os.NewFile(uintptr(duplicate), "ai-panorama-source-directory-reader")
		entries, err := reader.ReadDir(-1)
		reader.Close()
		if err != nil {
			return fmt.Errorf("ai-panorama-source-directory-read-failed")
		}
		sort.Slice(entries, func(left, right int) bool { return entries[left].Name() < entries[right].Name() })
		for _, entry := range entries {
			name := entry.Name()
			relpath := name
			if prefix != "" {
				relpath = prefix + "/" + name
			}
			if !tourV4SafeEntryName(name) || !tourV4SafeRelativePath(relpath) ||
				strings.HasPrefix(name, ".") {
				return fmt.Errorf("ai-panorama-source-entry-name-invalid")
			}
			child, directoryErr := tourV4OpenDirectoryAt(directory, name)
			if directoryErr == nil {
				childInfo, err := child.Stat()
				childMetadata, metadataErr := tourV4StatMetadata(childInfo)
				childMountID, mountErr := aiPanoramaFileMountID(child)
				if err != nil || metadataErr != nil ||
					uint32(childInfo.Mode().Perm()) != directoryMode ||
					childMetadata.Uid != uid || childMetadata.Gid != gid ||
					childMetadata.Nlink < 2 ||
					uint64(childMetadata.Dev) != uint64(rootMetadata.Dev) ||
					mountErr != nil || childMountID != rootMountID {
					child.Close()
					return fmt.Errorf("ai-panorama-source-directory-invalid")
				}
				snapshot.Directories = append(snapshot.Directories, relpath)
				if len(snapshot.Directories) > aiPanoramaMaximumDirectories {
					child.Close()
					return fmt.Errorf("ai-panorama-source-budget-exceeded")
				}
				if err := walk(child, relpath); err != nil {
					child.Close()
					return err
				}
				child.Close()
				continue
			}
			file, err := readAiPanoramaSourceFileAt(
				directory, name, relpath, uid, gid, fileMode, rootMountID,
			)
			if err != nil {
				return err
			}
			if file.Device != uint64(rootMetadata.Dev) {
				zero(file.Content)
				return fmt.Errorf("ai-panorama-source-device-changed")
			}
			snapshot.TotalBytes += file.Size
			snapshot.Files = append(snapshot.Files, file)
			if len(snapshot.Files) > 256 || snapshot.TotalBytes > 64*1024*1024 {
				return fmt.Errorf("ai-panorama-source-budget-exceeded")
			}
		}
		after, err := directory.Stat()
		if err != nil || !tourV4SameFingerprint(before, after) {
			return fmt.Errorf("ai-panorama-source-directory-race-detected")
		}
		return nil
	}
	if err := walk(root, ""); err != nil {
		snapshot.release()
		return nil, err
	}
	sort.Strings(snapshot.Directories)
	sort.Slice(snapshot.Files, func(left, right int) bool { return snapshot.Files[left].Path < snapshot.Files[right].Path })
	rows := make([]any, 0, len(snapshot.Files))
	for index := range snapshot.Files {
		file := &snapshot.Files[index]
		rows = append(rows, map[string]any{
			"relpath": file.Path, "sha256": file.SHA256,
			"size_bytes": json.Number(strconv.FormatInt(file.Size, 10)),
		})
		if file.Path == "tour.json" {
			snapshot.TourSHA256 = file.SHA256
		}
	}
	raw, err := canonicalJSON(rows)
	if err != nil || len(snapshot.Files) == 0 || snapshot.TourSHA256 == "" {
		zero(raw)
		snapshot.release()
		return nil, fmt.Errorf("ai-panorama-source-manifest-invalid")
	}
	sum := sha256.Sum256(raw)
	zero(raw)
	snapshot.TreeSHA256 = hex.EncodeToString(sum[:])
	if !retain {
		for index := range snapshot.Files {
			zero(snapshot.Files[index].Content)
			snapshot.Files[index].Content = nil
		}
	}
	return snapshot, nil
}

func readAiPanoramaSourceFileAt(
	parent *os.File,
	name, relpath string,
	uid, gid uint32,
	mode uint32,
	expectedMountID uint64,
) (aiPanoramaSourceFile, error) {
	if parent == nil || !tourV4SafeEntryName(name) || !tourV4SafeRelativePath(relpath) {
		return aiPanoramaSourceFile{}, fmt.Errorf("ai-panorama-source-file-input-invalid")
	}
	fd, err := syscall.Openat(int(parent.Fd()), name, syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return aiPanoramaSourceFile{}, fmt.Errorf("ai-panorama-source-file-unavailable")
	}
	file := os.NewFile(uintptr(fd), name)
	defer file.Close()
	before, err := file.Stat()
	metadata, metadataErr := tourV4StatMetadata(before)
	mountID, mountErr := aiPanoramaFileMountID(file)
	if err != nil || metadataErr != nil || !before.Mode().IsRegular() ||
		uint32(before.Mode().Perm()) != mode || metadata.Uid != uid || metadata.Gid != gid ||
		metadata.Nlink != 1 || before.Size() < 1 ||
		before.Size() > aiPanoramaMaximumFileBytes ||
		mountErr != nil || mountID != expectedMountID {
		return aiPanoramaSourceFile{}, fmt.Errorf("ai-panorama-source-file-invalid")
	}
	content := make([]byte, before.Size())
	if _, err := io.ReadFull(file, content); err != nil {
		zero(content)
		return aiPanoramaSourceFile{}, fmt.Errorf("ai-panorama-source-file-read-failed")
	}
	extra := []byte{0}
	count, readErr := file.Read(extra)
	zero(extra)
	if count != 0 || (readErr != nil && readErr != io.EOF) {
		zero(content)
		return aiPanoramaSourceFile{}, fmt.Errorf("ai-panorama-source-file-size-changed")
	}
	after, err := file.Stat()
	if err != nil || !tourV4SameFingerprint(before, after) {
		zero(content)
		return aiPanoramaSourceFile{}, fmt.Errorf("ai-panorama-source-file-race-detected")
	}
	sum := sha256.Sum256(content)
	mtime, ctime := tourV4StatTimes(metadata)
	return aiPanoramaSourceFile{
		Path: relpath, Size: before.Size(), SHA256: hex.EncodeToString(sum[:]),
		Device: uint64(metadata.Dev), Inode: metadata.Ino, MountID: mountID,
		MtimeNS: mtime,
		CtimeNS: ctime, Content: content,
	}, nil
}

func aiPanoramaSourceSnapshotsEqual(left, right *aiPanoramaSourceSnapshot) bool {
	if left == nil || right == nil || left.TreeSHA256 != right.TreeSHA256 ||
		left.RootMountID != right.RootMountID ||
		left.TourSHA256 != right.TourSHA256 || left.TotalBytes != right.TotalBytes ||
		len(left.Directories) != len(right.Directories) || len(left.Files) != len(right.Files) {
		return false
	}
	for index := range left.Directories {
		if left.Directories[index] != right.Directories[index] {
			return false
		}
	}
	for index := range left.Files {
		a, b := left.Files[index], right.Files[index]
		if a.Path != b.Path || a.Size != b.Size || a.SHA256 != b.SHA256 ||
			a.Device != b.Device || a.Inode != b.Inode ||
			a.MountID != b.MountID ||
			a.MtimeNS != b.MtimeNS || a.CtimeNS != b.CtimeNS {
			return false
		}
	}
	return true
}

func readAiPanoramaExactFile(path string, uid, gid uint32, mode uint32, maximum int64) (aiPanoramaSourceFile, error) {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || maximum < 1 {
		return aiPanoramaSourceFile{}, fmt.Errorf("ai-panorama-exact-file-path-invalid")
	}
	parent, err := tourV4OpenDirectoryAbsolute(filepath.Dir(path))
	if err != nil {
		return aiPanoramaSourceFile{}, fmt.Errorf("ai-panorama-exact-file-parent-invalid")
	}
	defer parent.Close()
	parentMountID, mountErr := aiPanoramaFileMountID(parent)
	if mountErr != nil {
		return aiPanoramaSourceFile{},
			fmt.Errorf("ai-panorama-exact-file-parent-invalid")
	}
	file, err := readAiPanoramaSourceFileAt(
		parent, filepath.Base(path), filepath.Base(path),
		uid, gid, mode, parentMountID,
	)
	if err != nil || file.Size > maximum {
		zero(file.Content)
		return aiPanoramaSourceFile{}, fmt.Errorf("ai-panorama-exact-file-invalid")
	}
	return file, nil
}

type aiPanoramaSealedArtifactObservation struct {
	RootDevice    uint64
	RootInode     uint64
	BundleDevice  uint64
	BundleInode   uint64
	TreeSHA256    string
	TourSHA256    string
	MarkerSHA256  string
	ReceiptSHA256 string
	FileCount     int
	TotalBytes    int64
}

func aiPanoramaSealedArtifactValue(observation *aiPanoramaSealedArtifactObservation) map[string]any {
	return map[string]any{
		"bundle_device":      json.Number(strconv.FormatUint(observation.BundleDevice, 10)),
		"bundle_inode":       json.Number(strconv.FormatUint(observation.BundleInode, 10)),
		"file_count":         json.Number(strconv.Itoa(observation.FileCount)),
		"marker_sha256":      observation.MarkerSHA256,
		"receipt_sha256":     observation.ReceiptSHA256,
		"root_device":        json.Number(strconv.FormatUint(observation.RootDevice, 10)),
		"root_inode":         json.Number(strconv.FormatUint(observation.RootInode, 10)),
		"sealed_root":        aiPanoramaSealedArtifactRoot,
		"source_tree_sha256": observation.TreeSHA256,
		"source_tour_sha256": observation.TourSHA256,
		"total_bytes":        json.Number(strconv.FormatInt(observation.TotalBytes, 10)),
	}
}

func validateAiPanoramaSealedArtifact() (*aiPanoramaSealedArtifactObservation, error) {
	parent, err := tourV4OpenDirectoryAbsolute(aiPanoramaSealedArtifactParent)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-sealed-parent-unavailable")
	}
	defer parent.Close()
	parentInfo, err := parent.Stat()
	parentMetadata, parentOK := infoSys(parentInfo)
	parentMountID, parentMountErr := aiPanoramaFileMountID(parent)
	parentNames, namesErr := aiPanoramaDirectoryNames(parent)
	if err != nil || !parentOK || namesErr != nil ||
		parentInfo.Mode().Perm() != 0o700 ||
		parentMetadata.Uid != 0 || parentMetadata.Gid != 0 ||
		parentMetadata.Nlink < 2 || parentMountErr != nil ||
		len(parentNames) != 1 ||
		parentNames[0] != filepath.Base(aiPanoramaSealedArtifactRoot) {
		return nil, fmt.Errorf("ai-panorama-sealed-parent-invalid")
	}
	root, err := tourV4OpenDirectoryAbsolute(aiPanoramaSealedArtifactRoot)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-sealed-root-unavailable")
	}
	defer root.Close()
	rootInfo, err := root.Stat()
	rootMetadata, metadataOK := infoSys(rootInfo)
	rootMountID, rootMountErr := aiPanoramaFileMountID(root)
	if err != nil || !metadataOK || rootInfo.Mode().Perm() != 0o500 ||
		rootMetadata.Uid != 0 || rootMetadata.Gid != 0 || rootMetadata.Nlink != 3 ||
		uint64(rootMetadata.Dev) != uint64(parentMetadata.Dev) ||
		rootMountErr != nil || rootMountID != parentMountID {
		return nil, fmt.Errorf("ai-panorama-sealed-root-invalid")
	}
	names, err := aiPanoramaDirectoryNames(root)
	if err != nil || len(names) != 2 || names[0] != "bundle" || names[1] != "materialization.receipt.json" {
		return nil, fmt.Errorf("ai-panorama-sealed-root-layout-invalid")
	}
	bundleRoot, err := tourV4OpenDirectoryAt(root, "bundle")
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-sealed-bundle-root-invalid")
	}
	bundleRootInfo, err := bundleRoot.Stat()
	bundleRootMetadata, bundleRootOK := infoSys(bundleRootInfo)
	bundleRootMountID, bundleRootMountErr := aiPanoramaFileMountID(bundleRoot)
	if err != nil || !bundleRootOK || bundleRootInfo.Mode().Perm() != 0o500 ||
		bundleRootMetadata.Uid != 0 || bundleRootMetadata.Gid != 0 ||
		bundleRootMetadata.Nlink != 3 ||
		uint64(bundleRootMetadata.Dev) != uint64(rootMetadata.Dev) ||
		bundleRootMountErr != nil || bundleRootMountID != parentMountID {
		bundleRoot.Close()
		return nil, fmt.Errorf("ai-panorama-sealed-bundle-root-invalid")
	}
	bundleNames, err := aiPanoramaDirectoryNames(bundleRoot)
	bundleRoot.Close()
	if err != nil || len(bundleNames) != 2 ||
		bundleNames[0] != ".propertyquarry-ai-panorama-candidate.json" ||
		bundleNames[1] != aiPanoramaPraterSlug {
		return nil, fmt.Errorf("ai-panorama-sealed-bundle-layout-invalid")
	}
	snapshot, err := snapshotAiPanoramaSource(aiPanoramaSealedBundlePath, 0, 0, 0o500, 0o400, false)
	if err != nil {
		return nil, err
	}
	defer snapshot.release()
	if snapshot.TreeSHA256 != aiPanoramaExpectedSourceTree ||
		snapshot.TourSHA256 != aiPanoramaExpectedTourDigest ||
		snapshot.RootMountID != parentMountID {
		return nil, fmt.Errorf("ai-panorama-sealed-bundle-digest-invalid")
	}
	marker, err := readAiPanoramaExactFile(aiPanoramaSealedMarkerPath, 0, 0, 0o400, 1024*1024)
	if err != nil {
		return nil, err
	}
	defer zero(marker.Content)
	receipt, err := readAiPanoramaExactFile(aiPanoramaSealedReceiptPath, 0, 0, 0o400, 1024*1024)
	if err != nil {
		return nil, err
	}
	defer zero(receipt.Content)
	if marker.SHA256 != aiPanoramaExpectedMarkerDigest ||
		receipt.SHA256 != aiPanoramaExpectedReceiptDigest ||
		marker.Device != uint64(rootMetadata.Dev) ||
		receipt.Device != uint64(rootMetadata.Dev) ||
		marker.MountID != parentMountID ||
		receipt.MountID != parentMountID {
		return nil, fmt.Errorf("ai-panorama-sealed-lineage-digest-invalid")
	}
	bundle, err := tourV4OpenDirectoryAbsolute(aiPanoramaSealedBundlePath)
	if err != nil {
		return nil, err
	}
	bundleInfo, err := bundle.Stat()
	bundleMetadata, bundleOK := infoSys(bundleInfo)
	bundleMountID, bundleMountErr := aiPanoramaFileMountID(bundle)
	bundle.Close()
	if err != nil || !bundleOK ||
		uint64(bundleMetadata.Dev) != uint64(rootMetadata.Dev) ||
		bundleMountErr != nil || bundleMountID != parentMountID {
		return nil, fmt.Errorf("ai-panorama-sealed-bundle-identity-invalid")
	}
	return &aiPanoramaSealedArtifactObservation{
		RootDevice: uint64(rootMetadata.Dev), RootInode: rootMetadata.Ino,
		BundleDevice: uint64(bundleMetadata.Dev), BundleInode: bundleMetadata.Ino,
		TreeSHA256: snapshot.TreeSHA256, TourSHA256: snapshot.TourSHA256,
		MarkerSHA256: marker.SHA256, ReceiptSHA256: receipt.SHA256,
		FileCount: len(snapshot.Files), TotalBytes: snapshot.TotalBytes,
	}, nil
}

func aiPanoramaDirectoryNames(directory *os.File) ([]string, error) {
	if directory == nil {
		return nil, fmt.Errorf("ai-panorama-directory-missing")
	}
	duplicate, err := syscall.Dup(int(directory.Fd()))
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-directory-dup-failed")
	}
	reader := os.NewFile(uintptr(duplicate), "ai-panorama-directory-reader")
	entries, err := reader.ReadDir(-1)
	reader.Close()
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-directory-read-failed")
	}
	names := make([]string, len(entries))
	for index, entry := range entries {
		if !tourV4SafeEntryName(entry.Name()) {
			return nil, fmt.Errorf("ai-panorama-directory-entry-invalid")
		}
		names[index] = entry.Name()
	}
	sort.Strings(names)
	return names, nil
}

func aiPanoramaSealedStageName() string {
	binding := []byte(
		aiPanoramaExpectedSourceTree + "\x00" +
			aiPanoramaExpectedMarkerDigest + "\x00" +
			aiPanoramaExpectedReceiptDigest,
	)
	name := ".prater-v1.stage-" + aiPanoramaRawSHA256(binding)
	zero(binding)
	return name
}

func aiPanoramaSealedStageWasJournalBound(
	journal *Journal,
	pendingPath string,
	intentFields map[string]any,
) bool {
	if journal == nil || pendingPath == "" || intentFields == nil {
		return false
	}
	var unresolved *JournalEvent
	for _, event := range unresolvedWorkflowOperations(journal) {
		if event.Operation == aiPanoramaInstallOperation {
			if unresolved != nil {
				return false
			}
			unresolved = event
		}
	}
	if unresolved == nil ||
		unresolved.EventType != aiPanoramaSealedArtifactIntentEvent ||
		unresolved.Payload["ai_panorama_sealed_stage_path"] != pendingPath ||
		unresolved.Payload["ai_panorama_sealed_target_path"] !=
			aiPanoramaSealedArtifactRoot ||
		unresolved.Payload["ai_panorama_sealed_source_tree_sha256"] !=
			aiPanoramaExpectedSourceTree ||
		unresolved.Payload["ai_panorama_sealed_marker_sha256"] !=
			aiPanoramaExpectedMarkerDigest ||
		unresolved.Payload["ai_panorama_sealed_receipt_sha256"] !=
			aiPanoramaExpectedReceiptDigest {
		return false
	}
	for _, key := range []string{
		"operation", "request_id", "run_id", "run_attempt",
		"config_digest", "plan_digest", "runtime_sha", "workflow_sha",
		"deployment_id", "host_machine_id_digest", "authority_scope",
	} {
		if !canonicalValuesEqual(unresolved.Payload[key], intentFields[key]) {
			return false
		}
	}
	return unresolved.Payload["authoritative"] == true &&
		unresolved.Payload["single_host_authority"] == true &&
		unresolved.Payload["external_cas_profile"] == false
}

type aiPanoramaSealedStageDirectory struct {
	File    *os.File
	Parent  *os.File
	Name    string
	Path    string
	Info    os.FileInfo
	MountID uint64
}

type aiPanoramaSealedStageFile struct {
	Parent   *os.File
	File     *os.File
	Name     string
	Path     string
	Expected []byte
	Device   uint64
	Inode    uint64
	Info     os.FileInfo
	MountID  uint64
}

type aiPanoramaSealedStageTree struct {
	Directories []aiPanoramaSealedStageDirectory
	Files       []aiPanoramaSealedStageFile
}

func (tree *aiPanoramaSealedStageTree) close() {
	if tree == nil {
		return
	}
	for index := range tree.Files {
		if tree.Files[index].File != nil {
			_ = tree.Files[index].File.Close()
		}
	}
	for index := range tree.Directories {
		if tree.Directories[index].File != nil {
			_ = tree.Directories[index].File.Close()
		}
	}
	*tree = aiPanoramaSealedStageTree{}
}

type aiPanoramaStatxTimestamp struct {
	Sec  int64
	Nsec uint32
	Pad  int32
}

type aiPanoramaStatx struct {
	Mask                   uint32
	BlockSize              uint32
	Attributes             uint64
	Nlink                  uint32
	UID                    uint32
	GID                    uint32
	Mode                   uint16
	Pad1                   uint16
	Inode                  uint64
	Size                   uint64
	Blocks                 uint64
	AttributesMask         uint64
	AccessTime             aiPanoramaStatxTimestamp
	BirthTime              aiPanoramaStatxTimestamp
	ChangeTime             aiPanoramaStatxTimestamp
	ModificationTime       aiPanoramaStatxTimestamp
	DeviceMajor            uint32
	DeviceMinor            uint32
	FilesystemDeviceMajor  uint32
	FilesystemDeviceMinor  uint32
	MountID                uint64
	DirectIOMemoryAlign    uint32
	DirectIOOffsetAlign    uint32
	Subvolume              uint64
	AtomicWriteUnitMinimum uint32
	AtomicWriteUnitMaximum uint32
	AtomicWriteSegmentsMax uint32
	DirectIOReadOffset     uint32
	AtomicWriteUnitMaxOpt  uint32
	Pad2                   uint32
	Spare                  [8]uint64
}

func aiPanoramaFileMountID(file *os.File) (uint64, error) {
	if file == nil {
		return 0, fmt.Errorf("ai-panorama-mount-id-input-invalid")
	}
	const (
		linuxATEmptyPath   = 0x1000
		linuxATNoAutomount = 0x800
		linuxStatxMountID  = 0x1000
		linuxAMD64Statx    = 332
	)
	empty := [1]byte{0}
	var observation aiPanoramaStatx
	_, _, errno := syscall.Syscall6(
		linuxAMD64Statx,
		file.Fd(),
		uintptr(unsafe.Pointer(&empty[0])),
		linuxATEmptyPath|linuxATNoAutomount,
		linuxStatxMountID,
		uintptr(unsafe.Pointer(&observation)),
		0,
	)
	if errno != 0 || observation.Mask&linuxStatxMountID == 0 ||
		observation.MountID == 0 {
		return 0, fmt.Errorf("ai-panorama-mount-id-unavailable")
	}
	return observation.MountID, nil
}

func aiPanoramaSealedStageContract(
	source *aiPanoramaSourceSnapshot,
	marker []byte,
	receipt []byte,
) (map[string]bool, map[string][]byte, error) {
	if source == nil || len(source.Directories) < 1 ||
		len(source.Files) < 1 || len(marker) < 1 || len(receipt) < 1 {
		return nil, nil, fmt.Errorf("ai-panorama-sealed-stage-contract-invalid")
	}
	directories := map[string]bool{
		"": true, "bundle": true,
		"bundle/" + aiPanoramaPraterSlug: true,
	}
	for _, directory := range source.Directories {
		if directory == "." {
			continue
		}
		if !tourV4SafeRelativePath(directory) {
			return nil, nil, fmt.Errorf("ai-panorama-sealed-stage-contract-invalid")
		}
		directories["bundle/"+aiPanoramaPraterSlug+"/"+directory] = true
	}
	files := map[string][]byte{
		"bundle/.propertyquarry-ai-panorama-candidate.json": marker,
		"materialization.receipt.json":                      receipt,
	}
	for index := range source.Files {
		file := &source.Files[index]
		if !tourV4SafeRelativePath(file.Path) ||
			len(file.Content) != int(file.Size) {
			return nil, nil, fmt.Errorf("ai-panorama-sealed-stage-contract-invalid")
		}
		files["bundle/"+aiPanoramaPraterSlug+"/"+file.Path] = file.Content
	}
	return directories, files, nil
}

func readAiPanoramaSealedStageFileAt(
	parent *os.File,
	name string,
	expected []byte,
	expectedDevice uint64,
	expectedMountID uint64,
	expectedUID uint32,
	expectedGID uint32,
) (*os.File, os.FileInfo, error) {
	if parent == nil || !tourV4SafeEntryName(name) || len(expected) < 1 {
		return nil, nil,
			fmt.Errorf("ai-panorama-sealed-stage-file-input-invalid")
	}
	fd, err := syscall.Openat(
		int(parent.Fd()), name,
		syscall.O_RDONLY|syscall.O_NONBLOCK|syscall.O_CLOEXEC|
			syscall.O_NOFOLLOW, 0,
	)
	if err != nil {
		return nil, nil,
			fmt.Errorf("ai-panorama-sealed-stage-file-unavailable")
	}
	file := os.NewFile(uintptr(fd), name)
	before, err := file.Stat()
	metadata, ok := infoSys(before)
	mountID, mountErr := aiPanoramaFileMountID(file)
	if err != nil || !ok || !before.Mode().IsRegular() ||
		before.Mode().Perm()&^os.FileMode(0o400) != 0 ||
		uint64(metadata.Dev) != expectedDevice ||
		mountErr != nil || mountID != expectedMountID ||
		metadata.Uid != expectedUID || metadata.Gid != expectedGID ||
		metadata.Nlink != 1 || before.Size() < 0 ||
		before.Size() > int64(len(expected)) {
		file.Close()
		return nil, nil,
			fmt.Errorf("ai-panorama-sealed-stage-file-invalid")
	}
	raw := make([]byte, before.Size())
	if len(raw) > 0 {
		if _, err := io.ReadFull(file, raw); err != nil {
			zero(raw)
			file.Close()
			return nil, nil,
				fmt.Errorf("ai-panorama-sealed-stage-file-read-failed")
		}
	}
	after, statErr := file.Stat()
	valid := statErr == nil && os.SameFile(before, after) &&
		tourV4SameFingerprint(before, after) &&
		bytes.Equal(raw, expected[:len(raw)])
	zero(raw)
	if !valid {
		file.Close()
		return nil, nil,
			fmt.Errorf("ai-panorama-sealed-stage-file-prefix-invalid")
	}
	return file, before, nil
}

func observeAiPanoramaSealedStage(
	parent *os.File,
	stageName string,
	source *aiPanoramaSourceSnapshot,
	marker []byte,
	receipt []byte,
	expectedDevice uint64,
	expectedMountID uint64,
	expectedUID uint32,
	expectedGID uint32,
) (*aiPanoramaSealedStageTree, error) {
	if parent == nil || !tourV4SafeEntryName(stageName) {
		return nil, fmt.Errorf("ai-panorama-sealed-stage-observation-input-invalid")
	}
	directories, files, err := aiPanoramaSealedStageContract(
		source, marker, receipt,
	)
	if err != nil {
		return nil, err
	}
	stage, err := tourV4OpenDirectoryAt(parent, stageName)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-sealed-stage-unavailable")
	}
	tree := &aiPanoramaSealedStageTree{}
	var walk func(*os.File, *os.File, string, string) error
	walk = func(
		directory *os.File,
		directoryParent *os.File,
		name string,
		relpath string,
	) error {
		before, err := directory.Stat()
		metadata, ok := infoSys(before)
		mountID, mountErr := aiPanoramaFileMountID(directory)
		if err != nil || !ok || !before.IsDir() ||
			(before.Mode().Perm()&^os.FileMode(0o700) != 0 &&
				before.Mode().Perm() != 0o500) ||
			uint64(metadata.Dev) != expectedDevice ||
			mountErr != nil || mountID != expectedMountID ||
			metadata.Uid != expectedUID || metadata.Gid != expectedGID ||
			metadata.Nlink < 2 || !directories[relpath] {
			return fmt.Errorf("ai-panorama-sealed-stage-directory-invalid")
		}
		tree.Directories = append(tree.Directories, aiPanoramaSealedStageDirectory{
			File: directory, Parent: directoryParent, Name: name, Path: relpath,
			Info: before, MountID: mountID,
		})
		names, err := aiPanoramaDirectoryNames(directory)
		if err != nil {
			return err
		}
		for _, childName := range names {
			childPath := childName
			if relpath != "" {
				childPath = relpath + "/" + childName
			}
			if directories[childPath] {
				child, err := tourV4OpenDirectoryAt(directory, childName)
				if err != nil {
					return fmt.Errorf("ai-panorama-sealed-stage-directory-invalid")
				}
				if err := walk(
					child, directory, childName, childPath,
				); err != nil {
					_ = child.Close()
					return err
				}
				continue
			}
			expected, expectedOK := files[childPath]
			if !expectedOK {
				return fmt.Errorf("ai-panorama-sealed-stage-entry-unexpected")
			}
			file, info, err := readAiPanoramaSealedStageFileAt(
				directory, childName, expected, expectedDevice,
				expectedMountID, expectedUID, expectedGID,
			)
			if err != nil {
				return err
			}
			metadata, metadataOK := infoSys(info)
			if !metadataOK {
				file.Close()
				return fmt.Errorf("ai-panorama-sealed-stage-file-invalid")
			}
			tree.Files = append(tree.Files, aiPanoramaSealedStageFile{
				Parent: directory, File: file, Name: childName,
				Path: childPath, Expected: expected,
				Device: expectedDevice, Inode: metadata.Ino,
				Info: info, MountID: expectedMountID,
			})
		}
		after, err := directory.Stat()
		if err != nil || !tourV4SameFingerprint(before, after) {
			return fmt.Errorf("ai-panorama-sealed-stage-directory-changed")
		}
		return nil
	}
	if err := walk(stage, parent, stageName, ""); err != nil {
		tree.close()
		return nil, err
	}
	return tree, nil
}

func unlinkAiPanoramaAt(parent *os.File, name string, directory bool) error {
	if parent == nil || !tourV4SafeEntryName(name) {
		return fmt.Errorf("ai-panorama-unlink-input-invalid")
	}
	pointer, err := syscall.BytePtrFromString(name)
	if err != nil {
		return err
	}
	flags := uintptr(0)
	if directory {
		flags = 0x200
	}
	_, _, errno := syscall.Syscall(
		syscall.SYS_UNLINKAT,
		parent.Fd(),
		uintptr(unsafe.Pointer(pointer)),
		flags,
	)
	if errno != 0 {
		return errno
	}
	return nil
}

func openAiPanoramaPathAt(
	parent *os.File,
	name string,
	directory bool,
) (*os.File, error) {
	if parent == nil || !tourV4SafeEntryName(name) {
		return nil, fmt.Errorf("ai-panorama-path-observation-input-invalid")
	}
	const linuxOPath = 0x200000
	flags := linuxOPath | syscall.O_CLOEXEC | syscall.O_NOFOLLOW
	if directory {
		flags |= syscall.O_DIRECTORY
	}
	fd, err := syscall.Openat(int(parent.Fd()), name, flags, 0)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-path-observation-failed")
	}
	return os.NewFile(uintptr(fd), name), nil
}

func aiPanoramaSealedStageFileStillBound(
	file *aiPanoramaSealedStageFile,
	expectedUID uint32,
	expectedGID uint32,
) (*os.File, error) {
	if file == nil || file.File == nil || file.Parent == nil ||
		len(file.Expected) < 1 {
		return nil, fmt.Errorf("ai-panorama-sealed-stage-file-binding-invalid")
	}
	if _, err := file.File.Seek(0, io.SeekStart); err != nil {
		return nil, fmt.Errorf("ai-panorama-sealed-stage-file-binding-invalid")
	}
	info, err := file.File.Stat()
	metadata, metadataOK := infoSys(info)
	mountID, mountErr := aiPanoramaFileMountID(file.File)
	if err != nil || !metadataOK || !info.Mode().IsRegular() ||
		info.Mode().Perm() != 0o400 || info.Size() < 0 ||
		info.Size() > int64(len(file.Expected)) ||
		uint64(metadata.Dev) != file.Device || metadata.Ino != file.Inode ||
		metadata.Uid != expectedUID || metadata.Gid != expectedGID ||
		metadata.Nlink != 1 || mountErr != nil || mountID != file.MountID {
		return nil, fmt.Errorf("ai-panorama-sealed-stage-file-binding-invalid")
	}
	raw := make([]byte, info.Size())
	if len(raw) > 0 {
		if _, err := io.ReadFull(file.File, raw); err != nil {
			zero(raw)
			return nil,
				fmt.Errorf("ai-panorama-sealed-stage-file-binding-invalid")
		}
	}
	valid := bytes.Equal(raw, file.Expected[:len(raw)])
	zero(raw)
	if !valid {
		return nil, fmt.Errorf("ai-panorama-sealed-stage-file-binding-invalid")
	}
	path, err := openAiPanoramaPathAt(file.Parent, file.Name, false)
	if err != nil {
		return nil, err
	}
	pathInfo, statErr := path.Stat()
	pathMountID, mountErr := aiPanoramaFileMountID(path)
	if statErr != nil || mountErr != nil ||
		pathMountID != file.MountID || !os.SameFile(info, pathInfo) {
		path.Close()
		return nil, fmt.Errorf("ai-panorama-sealed-stage-file-binding-invalid")
	}
	return path, nil
}

func aiPanoramaSealedStageDirectoryStillBound(
	directory *aiPanoramaSealedStageDirectory,
	expectedDevice uint64,
	expectedMountID uint64,
	expectedUID uint32,
	expectedGID uint32,
) (*os.File, error) {
	if directory == nil || directory.File == nil || directory.Parent == nil {
		return nil,
			fmt.Errorf("ai-panorama-sealed-stage-directory-binding-invalid")
	}
	info, err := directory.File.Stat()
	metadata, metadataOK := infoSys(info)
	mountID, mountErr := aiPanoramaFileMountID(directory.File)
	if err != nil || !metadataOK || !info.IsDir() ||
		info.Mode().Perm() != 0o700 ||
		uint64(metadata.Dev) != expectedDevice ||
		metadata.Uid != expectedUID || metadata.Gid != expectedGID ||
		metadata.Nlink < 2 || mountErr != nil || mountID != expectedMountID {
		return nil,
			fmt.Errorf("ai-panorama-sealed-stage-directory-binding-invalid")
	}
	path, err := openAiPanoramaPathAt(
		directory.Parent, directory.Name, true,
	)
	if err != nil {
		return nil, err
	}
	pathInfo, statErr := path.Stat()
	pathMountID, pathMountErr := aiPanoramaFileMountID(path)
	if statErr != nil || pathMountErr != nil ||
		pathMountID != expectedMountID || !os.SameFile(info, pathInfo) {
		path.Close()
		return nil,
			fmt.Errorf("ai-panorama-sealed-stage-directory-binding-invalid")
	}
	return path, nil
}

func cleanupAiPanoramaSealedStage(
	parent *os.File,
	stageName string,
	source *aiPanoramaSourceSnapshot,
	marker []byte,
	receipt []byte,
	expectedDevice uint64,
	expectedMountID uint64,
	expectedUID uint32,
	expectedGID uint32,
) error {
	tree, err := observeAiPanoramaSealedStage(
		parent, stageName, source, marker, receipt,
		expectedDevice, expectedMountID, expectedUID, expectedGID,
	)
	if err != nil {
		return err
	}
	defer tree.close()
	for index := range tree.Files {
		file := &tree.Files[index]
		if file.File.Chmod(0o400) != nil || file.File.Sync() != nil {
			return fmt.Errorf("ai-panorama-sealed-stage-file-mode-recovery-failed")
		}
	}
	for index := len(tree.Directories) - 1; index >= 0; index-- {
		directory := &tree.Directories[index]
		if directory.File.Chmod(0o700) != nil ||
			directory.File.Sync() != nil {
			return fmt.Errorf(
				"ai-panorama-sealed-stage-directory-mode-recovery-failed",
			)
		}
	}
	for index := len(tree.Files) - 1; index >= 0; index-- {
		file := &tree.Files[index]
		path, err := aiPanoramaSealedStageFileStillBound(
			file, expectedUID, expectedGID,
		)
		if err != nil {
			return fmt.Errorf("ai-panorama-sealed-stage-file-cleanup-failed")
		}
		unlinkErr := unlinkAiPanoramaAt(file.Parent, file.Name, false)
		closeErr := path.Close()
		if unlinkErr != nil || closeErr != nil ||
			file.Parent.Sync() != nil {
			return fmt.Errorf("ai-panorama-sealed-stage-file-cleanup-failed")
		}
	}
	for index := len(tree.Directories) - 1; index >= 0; index-- {
		directory := &tree.Directories[index]
		names, err := aiPanoramaDirectoryNames(directory.File)
		if err != nil || len(names) != 0 {
			return fmt.Errorf("ai-panorama-sealed-stage-directory-cleanup-failed")
		}
		path, err := aiPanoramaSealedStageDirectoryStillBound(
			directory, expectedDevice, expectedMountID,
			expectedUID, expectedGID,
		)
		if err != nil {
			return fmt.Errorf("ai-panorama-sealed-stage-directory-cleanup-failed")
		}
		unlinkErr := unlinkAiPanoramaAt(
			directory.Parent, directory.Name, true,
		)
		closeErr := path.Close()
		if unlinkErr != nil || closeErr != nil ||
			directory.Parent.Sync() != nil {
			return fmt.Errorf("ai-panorama-sealed-stage-directory-cleanup-failed")
		}
	}
	return nil
}

func prepareAiPanoramaSealedArtifact(
	journal *Journal,
	intentFields map[string]any,
) (*aiPanoramaSealedArtifactObservation, error) {
	if journal == nil || intentFields == nil {
		return nil, fmt.Errorf("ai-panorama-sealed-intent-missing")
	}
	parent, err := tourV4OpenDirectoryAbsolute(aiPanoramaSealedArtifactParent)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-sealed-parent-unavailable")
	}
	defer parent.Close()
	parentInfo, err := parent.Stat()
	parentMetadata, metadataOK := infoSys(parentInfo)
	parentMountID, mountIDErr := aiPanoramaFileMountID(parent)
	if err != nil || !metadataOK || parentInfo.Mode().Perm() != 0o700 ||
		parentMetadata.Uid != 0 || parentMetadata.Gid != 0 ||
		parentMetadata.Nlink < 2 || mountIDErr != nil {
		return nil, fmt.Errorf("ai-panorama-sealed-parent-invalid")
	}
	pending := aiPanoramaSealedStageName()
	pendingPath := filepath.Join(aiPanoramaSealedArtifactParent, pending)
	names, err := aiPanoramaDirectoryNames(parent)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-sealed-parent-inventory-invalid")
	}
	for _, name := range names {
		if name != filepath.Base(aiPanoramaSealedArtifactRoot) &&
			name != pending {
			return nil, fmt.Errorf("ai-panorama-sealed-parent-inventory-invalid")
		}
	}
	if _, rootErr := os.Lstat(aiPanoramaSealedArtifactRoot); rootErr == nil {
		if len(names) != 1 ||
			names[0] != filepath.Base(aiPanoramaSealedArtifactRoot) {
			return nil, fmt.Errorf("ai-panorama-sealed-parent-inventory-invalid")
		}
		if parent.Sync() != nil {
			return nil, fmt.Errorf("ai-panorama-sealed-parent-sync-failed")
		}
		return validateAiPanoramaSealedArtifact()
	} else if !os.IsNotExist(rootErr) {
		return nil, fmt.Errorf("ai-panorama-sealed-root-conflict")
	}
	if len(names) > 1 || (len(names) == 1 && names[0] != pending) {
		return nil, fmt.Errorf("ai-panorama-sealed-parent-inventory-invalid")
	}
	source, err := snapshotAiPanoramaSource(aiPanoramaReviewedBundlePath, 1000, 1000, 0o700, 0o600, true)
	if err != nil {
		return nil, err
	}
	defer source.release()
	if source.TreeSHA256 != aiPanoramaExpectedSourceTree ||
		source.TourSHA256 != aiPanoramaExpectedTourDigest {
		return nil, fmt.Errorf("ai-panorama-reviewed-source-digest-invalid")
	}
	marker, err := readAiPanoramaExactFile(aiPanoramaReviewedMarkerPath, 1000, 1000, 0o600, 1024*1024)
	if err != nil {
		return nil, err
	}
	defer zero(marker.Content)
	receipt, err := readAiPanoramaExactFile(aiPanoramaReviewedReceiptPath, 1000, 1000, 0o600, 1024*1024)
	if err != nil {
		return nil, err
	}
	defer zero(receipt.Content)
	if marker.SHA256 != aiPanoramaExpectedMarkerDigest ||
		receipt.SHA256 != aiPanoramaExpectedReceiptDigest {
		return nil, fmt.Errorf("ai-panorama-reviewed-lineage-digest-invalid")
	}
	if stageInfo, stageErr := os.Lstat(pendingPath); stageErr == nil {
		stageMetadata, stageOK := infoSys(stageInfo)
		if !aiPanoramaSealedStageWasJournalBound(
			journal, pendingPath, intentFields,
		) ||
			!stageOK || !stageInfo.IsDir() ||
			stageInfo.Mode()&os.ModeSymlink != 0 ||
			(stageInfo.Mode().Perm() != 0o700 &&
				stageInfo.Mode().Perm() != 0o500) ||
			stageMetadata.Uid != 0 || stageMetadata.Gid != 0 ||
			uint64(stageMetadata.Dev) != uint64(parentMetadata.Dev) ||
			stageMetadata.Nlink < 2 {
			return nil, fmt.Errorf("ai-panorama-sealed-stage-invalid")
		}
		if err := cleanupAiPanoramaSealedStage(
			parent, pending, source, marker.Content, receipt.Content,
			uint64(parentMetadata.Dev), parentMountID, 0, 0,
		); err != nil {
			return nil, fmt.Errorf("ai-panorama-sealed-stage-cleanup-failed")
		}
		cleaned := cloneFields(intentFields)
		cleaned["ai_panorama_sealed_stage_path"] = pendingPath
		cleaned["ai_panorama_sealed_target_path"] =
			aiPanoramaSealedArtifactRoot
		cleaned["ai_panorama_sealed_source_tree_sha256"] =
			aiPanoramaExpectedSourceTree
		cleaned["ai_panorama_sealed_marker_sha256"] =
			aiPanoramaExpectedMarkerDigest
		cleaned["ai_panorama_sealed_receipt_sha256"] =
			aiPanoramaExpectedReceiptDigest
		cleaned["disposition"] = "sealed-artifact-stage-cleaned"
		if err := appendAiPanoramaJournalEvent(
			journal, aiPanoramaSealedArtifactCleanedEvent, cleaned,
		); err != nil {
			return nil, err
		}
	} else if !os.IsNotExist(stageErr) {
		return nil, fmt.Errorf("ai-panorama-sealed-stage-invalid")
	}
	intent := cloneFields(intentFields)
	intent["ai_panorama_sealed_stage_path"] = pendingPath
	intent["ai_panorama_sealed_target_path"] = aiPanoramaSealedArtifactRoot
	intent["ai_panorama_sealed_source_tree_sha256"] =
		aiPanoramaExpectedSourceTree
	intent["ai_panorama_sealed_marker_sha256"] =
		aiPanoramaExpectedMarkerDigest
	intent["ai_panorama_sealed_receipt_sha256"] =
		aiPanoramaExpectedReceiptDigest
	intent["disposition"] = "sealed-artifact-intent"
	if err := appendAiPanoramaJournalEvent(
		journal, aiPanoramaSealedArtifactIntentEvent, intent,
	); err != nil {
		return nil, err
	}
	if createAiPanoramaSealedDirectory(pendingPath) != nil ||
		createAiPanoramaSealedDirectory(filepath.Join(pendingPath, "bundle")) != nil ||
		createAiPanoramaSealedDirectory(
			filepath.Join(pendingPath, "bundle", aiPanoramaPraterSlug),
		) != nil {
		return nil, fmt.Errorf("ai-panorama-sealed-stage-create-failed")
	}
	for _, directory := range source.Directories {
		if directory == "." {
			continue
		}
		if createAiPanoramaSealedDirectory(
			filepath.Join(
				pendingPath, "bundle", aiPanoramaPraterSlug,
				filepath.FromSlash(directory),
			),
		) != nil {
			return nil, fmt.Errorf("ai-panorama-sealed-directory-create-failed")
		}
	}
	for _, file := range source.Files {
		target := filepath.Join(pendingPath, "bundle", aiPanoramaPraterSlug, filepath.FromSlash(file.Path))
		if err := writeAiPanoramaSealedFile(target, file.Content); err != nil {
			return nil, err
		}
	}
	if err := writeAiPanoramaSealedFile(filepath.Join(pendingPath, "bundle", ".propertyquarry-ai-panorama-candidate.json"), marker.Content); err != nil {
		return nil, err
	}
	if err := writeAiPanoramaSealedFile(filepath.Join(pendingPath, "materialization.receipt.json"), receipt.Content); err != nil {
		return nil, err
	}
	for index := len(source.Directories) - 1; index >= 0; index-- {
		directory := filepath.Join(pendingPath, "bundle", aiPanoramaPraterSlug)
		if source.Directories[index] != "." {
			directory = filepath.Join(directory, filepath.FromSlash(source.Directories[index]))
		}
		if os.Chmod(directory, 0o500) != nil || fsyncAiPanoramaDirectory(directory) != nil {
			return nil, fmt.Errorf("ai-panorama-sealed-directory-finalize-failed")
		}
	}
	if os.Chmod(filepath.Join(pendingPath, "bundle"), 0o500) != nil ||
		fsyncAiPanoramaDirectory(filepath.Join(pendingPath, "bundle")) != nil ||
		os.Chmod(pendingPath, 0o500) != nil || fsyncAiPanoramaDirectory(pendingPath) != nil {
		return nil, fmt.Errorf("ai-panorama-sealed-stage-finalize-failed")
	}
	sourceAfter, err := snapshotAiPanoramaSource(aiPanoramaReviewedBundlePath, 1000, 1000, 0o700, 0o600, false)
	if err != nil {
		return nil, err
	}
	defer sourceAfter.release()
	markerAfter, markerErr := readAiPanoramaExactFile(aiPanoramaReviewedMarkerPath, 1000, 1000, 0o600, 1024*1024)
	receiptAfter, receiptErr := readAiPanoramaExactFile(aiPanoramaReviewedReceiptPath, 1000, 1000, 0o600, 1024*1024)
	defer zero(markerAfter.Content)
	defer zero(receiptAfter.Content)
	if !aiPanoramaSourceSnapshotsEqual(source, sourceAfter) || markerErr != nil || receiptErr != nil ||
		markerAfter.Device != marker.Device || markerAfter.Inode != marker.Inode ||
		markerAfter.MtimeNS != marker.MtimeNS || markerAfter.CtimeNS != marker.CtimeNS ||
		markerAfter.SHA256 != marker.SHA256 ||
		receiptAfter.Device != receipt.Device || receiptAfter.Inode != receipt.Inode ||
		receiptAfter.MtimeNS != receipt.MtimeNS || receiptAfter.CtimeNS != receipt.CtimeNS ||
		receiptAfter.SHA256 != receipt.SHA256 {
		return nil, fmt.Errorf("ai-panorama-reviewed-source-changed")
	}
	if err := validateAiPanoramaCompletedSealedStage(
		pendingPath, uint64(parentMetadata.Dev), parentMountID,
	); err != nil {
		return nil, err
	}
	if err := renameAtNoReplace(int(parent.Fd()), pending, filepath.Base(aiPanoramaSealedArtifactRoot)); err != nil {
		return nil, fmt.Errorf("ai-panorama-sealed-publish-failed")
	}
	if parent.Sync() != nil {
		return nil, fmt.Errorf("ai-panorama-sealed-publish-durability-unknown")
	}
	return validateAiPanoramaSealedArtifact()
}

func recoverAiPanoramaSealedArtifactIntent(
	journal *Journal,
	last *JournalEvent,
) error {
	if journal == nil || last == nil ||
		last.EventType != aiPanoramaSealedArtifactIntentEvent ||
		last.Operation != aiPanoramaInstallOperation ||
		!exactUniqueUnresolvedWorkflowEvent(
			journal, last, aiPanoramaInstallOperation,
		) {
		return fmt.Errorf("ai-panorama-sealed-recovery-input-invalid")
	}
	pending := aiPanoramaSealedStageName()
	pendingPath := filepath.Join(aiPanoramaSealedArtifactParent, pending)
	if last.Payload["ai_panorama_sealed_stage_path"] != pendingPath ||
		!aiPanoramaSealedStageWasJournalBound(
			journal, pendingPath, last.Payload,
		) {
		return fmt.Errorf("ai-panorama-sealed-recovery-intent-invalid")
	}
	parent, err := tourV4OpenDirectoryAbsolute(aiPanoramaSealedArtifactParent)
	if err != nil {
		return fmt.Errorf("ai-panorama-sealed-parent-unavailable")
	}
	defer parent.Close()
	parentInfo, err := parent.Stat()
	parentMetadata, parentOK := infoSys(parentInfo)
	parentMountID, mountIDErr := aiPanoramaFileMountID(parent)
	names, namesErr := aiPanoramaDirectoryNames(parent)
	if err != nil || !parentOK || namesErr != nil ||
		parentInfo.Mode().Perm() != 0o700 ||
		parentMetadata.Uid != 0 || parentMetadata.Gid != 0 ||
		parentMetadata.Nlink < 2 || mountIDErr != nil || len(names) > 1 ||
		(len(names) == 1 && names[0] != pending &&
			names[0] != filepath.Base(aiPanoramaSealedArtifactRoot)) {
		return fmt.Errorf("ai-panorama-sealed-recovery-parent-invalid")
	}
	if len(names) == 1 &&
		names[0] == filepath.Base(aiPanoramaSealedArtifactRoot) {
		if _, err := validateAiPanoramaSealedArtifact(); err != nil {
			return err
		}
		if parent.Sync() != nil {
			return fmt.Errorf("ai-panorama-sealed-recovery-parent-sync-failed")
		}
	} else if len(names) == 1 {
		source, err := snapshotAiPanoramaSource(
			aiPanoramaReviewedBundlePath, 1000, 1000, 0o700, 0o600, true,
		)
		if err != nil {
			return err
		}
		defer source.release()
		marker, err := readAiPanoramaExactFile(
			aiPanoramaReviewedMarkerPath, 1000, 1000, 0o600, 1024*1024,
		)
		if err != nil {
			return err
		}
		defer zero(marker.Content)
		receipt, err := readAiPanoramaExactFile(
			aiPanoramaReviewedReceiptPath, 1000, 1000, 0o600, 1024*1024,
		)
		if err != nil {
			return err
		}
		defer zero(receipt.Content)
		if source.TreeSHA256 != aiPanoramaExpectedSourceTree ||
			source.TourSHA256 != aiPanoramaExpectedTourDigest ||
			marker.SHA256 != aiPanoramaExpectedMarkerDigest ||
			receipt.SHA256 != aiPanoramaExpectedReceiptDigest ||
			cleanupAiPanoramaSealedStage(
				parent, pending, source, marker.Content, receipt.Content,
				uint64(parentMetadata.Dev), parentMountID, 0, 0,
			) != nil {
			return fmt.Errorf("ai-panorama-sealed-recovery-stage-invalid")
		}
	} else if parent.Sync() != nil {
		return fmt.Errorf("ai-panorama-sealed-recovery-parent-sync-failed")
	}
	fields := cloneFields(last.Payload)
	fields["ai_panorama_sealed_stage_cleanup_verified"] = true
	fields["disposition"] = "sealed-artifact-stage-cleaned"
	return appendAiPanoramaJournalEvent(
		journal, aiPanoramaSealedArtifactCleanedEvent, fields,
	)
}

func validateAiPanoramaCompletedSealedStage(
	stagePath string,
	parentDevice uint64,
	parentMountID uint64,
) error {
	stage, err := tourV4OpenDirectoryAbsolute(stagePath)
	if err != nil {
		return fmt.Errorf("ai-panorama-sealed-stage-unavailable")
	}
	defer stage.Close()
	stageInfo, err := stage.Stat()
	stageMetadata, stageOK := infoSys(stageInfo)
	stageMountID, mountIDErr := aiPanoramaFileMountID(stage)
	stageNames, namesErr := aiPanoramaDirectoryNames(stage)
	if err != nil || !stageOK || namesErr != nil ||
		stageInfo.Mode().Perm() != 0o500 ||
		stageMetadata.Uid != 0 || stageMetadata.Gid != 0 ||
		uint64(stageMetadata.Dev) != parentDevice ||
		mountIDErr != nil || stageMountID != parentMountID ||
		stageMetadata.Nlink != 3 || len(stageNames) != 2 ||
		stageNames[0] != "bundle" ||
		stageNames[1] != "materialization.receipt.json" {
		return fmt.Errorf("ai-panorama-sealed-stage-layout-invalid")
	}
	bundlePath := filepath.Join(stagePath, "bundle")
	bundle, err := tourV4OpenDirectoryAbsolute(bundlePath)
	if err != nil {
		return fmt.Errorf("ai-panorama-sealed-stage-bundle-invalid")
	}
	bundleInfo, statErr := bundle.Stat()
	bundleMetadata, bundleOK := infoSys(bundleInfo)
	bundleMountID, bundleMountErr := aiPanoramaFileMountID(bundle)
	bundleNames, bundleNamesErr := aiPanoramaDirectoryNames(bundle)
	bundle.Close()
	if statErr != nil || !bundleOK || bundleNamesErr != nil ||
		bundleInfo.Mode().Perm() != 0o500 ||
		bundleMetadata.Uid != 0 || bundleMetadata.Gid != 0 ||
		uint64(bundleMetadata.Dev) != parentDevice ||
		bundleMountErr != nil || bundleMountID != parentMountID ||
		bundleMetadata.Nlink != 3 || len(bundleNames) != 2 ||
		bundleNames[0] != ".propertyquarry-ai-panorama-candidate.json" ||
		bundleNames[1] != aiPanoramaPraterSlug {
		return fmt.Errorf("ai-panorama-sealed-stage-bundle-invalid")
	}
	source, err := snapshotAiPanoramaSource(
		filepath.Join(bundlePath, aiPanoramaPraterSlug),
		0, 0, 0o500, 0o400, false,
	)
	if err != nil {
		return err
	}
	defer source.release()
	marker, markerErr := readAiPanoramaExactFile(
		filepath.Join(
			bundlePath, ".propertyquarry-ai-panorama-candidate.json",
		),
		0, 0, 0o400, 1024*1024,
	)
	defer zero(marker.Content)
	receipt, receiptErr := readAiPanoramaExactFile(
		filepath.Join(stagePath, "materialization.receipt.json"),
		0, 0, 0o400, 1024*1024,
	)
	defer zero(receipt.Content)
	if markerErr != nil || receiptErr != nil ||
		source.TreeSHA256 != aiPanoramaExpectedSourceTree ||
		source.TourSHA256 != aiPanoramaExpectedTourDigest ||
		source.RootMountID != parentMountID ||
		marker.SHA256 != aiPanoramaExpectedMarkerDigest ||
		receipt.SHA256 != aiPanoramaExpectedReceiptDigest ||
		marker.Device != parentDevice || receipt.Device != parentDevice ||
		marker.MountID != parentMountID ||
		receipt.MountID != parentMountID {
		return fmt.Errorf("ai-panorama-sealed-stage-content-invalid")
	}
	return nil
}

func createAiPanoramaSealedDirectory(path string) error {
	if err := os.Mkdir(path, 0o700); err != nil {
		return err
	}
	if err := os.Chmod(path, 0o700); err != nil {
		return err
	}
	info, err := os.Lstat(path)
	metadata, ok := infoSys(info)
	if err != nil || !ok || !info.IsDir() ||
		info.Mode()&os.ModeSymlink != 0 ||
		info.Mode().Perm() != 0o700 ||
		metadata.Uid != 0 || metadata.Gid != 0 ||
		metadata.Nlink < 2 {
		return fmt.Errorf("ai-panorama-sealed-directory-invalid")
	}
	return nil
}

func writeAiPanoramaSealedFile(path string, content []byte) error {
	if len(content) < 1 || len(content) > int(aiPanoramaMaximumFileBytes) {
		return fmt.Errorf("ai-panorama-sealed-file-input-invalid")
	}
	parentInfo, parentErr := os.Lstat(filepath.Dir(path))
	parentMetadata, parentOK := infoSys(parentInfo)
	if parentErr != nil || !parentOK || !parentInfo.IsDir() ||
		parentInfo.Mode().Perm() != 0o700 ||
		parentMetadata.Uid != 0 || parentMetadata.Gid != 0 {
		return fmt.Errorf("ai-panorama-sealed-file-parent-invalid")
	}
	file, err := os.OpenFile(
		path,
		os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_CLOEXEC|syscall.O_NOFOLLOW,
		0o400,
	)
	if err != nil {
		return fmt.Errorf("ai-panorama-sealed-file-create-failed")
	}
	if file.Chmod(0o400) != nil || writeAll(file, content) != nil ||
		file.Sync() != nil {
		_ = file.Close()
		return fmt.Errorf("ai-panorama-sealed-file-write-failed")
	}
	written, statErr := file.Stat()
	metadata, metadataOK := infoSys(written)
	pathInfo, pathErr := os.Lstat(path)
	if statErr != nil || !metadataOK || pathErr != nil ||
		!written.Mode().IsRegular() || written.Mode().Perm() != 0o400 ||
		written.Size() != int64(len(content)) ||
		metadata.Uid != 0 || metadata.Gid != 0 || metadata.Nlink != 1 ||
		uint64(metadata.Dev) != uint64(parentMetadata.Dev) ||
		!os.SameFile(written, pathInfo) || file.Close() != nil {
		_ = file.Close()
		return fmt.Errorf("ai-panorama-sealed-file-write-failed")
	}
	observed, err := readAiPanoramaExactFile(
		path, 0, 0, 0o400, int64(len(content)),
	)
	valid := err == nil && observed.Size == int64(len(content)) &&
		bytes.Equal(observed.Content, content) &&
		observed.SHA256 == aiPanoramaRawSHA256(content)
	zero(observed.Content)
	if !valid {
		return fmt.Errorf("ai-panorama-sealed-file-verification-failed")
	}
	return nil
}

func fsyncAiPanoramaDirectory(path string) error {
	directory, err := tourV4OpenDirectoryAbsolute(path)
	if err != nil {
		return err
	}
	defer directory.Close()
	return directory.Sync()
}

func snapshotAiPanoramaRelated(rootPath string) (*aiPanoramaRelatedManifest, error) {
	if !filepath.IsAbs(rootPath) || filepath.Clean(rootPath) != rootPath || rootPath == "/" {
		return nil, fmt.Errorf("ai-panorama-public-root-invalid")
	}
	root, err := tourV4OpenDirectoryAbsolute(rootPath)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-public-root-unavailable")
	}
	defer root.Close()
	rootBefore, err := root.Stat()
	rootMetadata, metadataErr := tourV4StatMetadata(rootBefore)
	if err != nil || metadataErr != nil || !rootBefore.IsDir() ||
		rootBefore.Mode().Perm() != 0o755 || rootMetadata.Nlink < 2 {
		return nil, fmt.Errorf("ai-panorama-public-root-metadata-invalid")
	}
	duplicate, err := syscall.Dup(int(root.Fd()))
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-public-root-dup-failed")
	}
	reader := os.NewFile(uintptr(duplicate), "ai-panorama-public-root-reader")
	entries, err := reader.ReadDir(-1)
	reader.Close()
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-public-root-read-failed")
	}
	sort.Slice(entries, func(left, right int) bool { return entries[left].Name() < entries[right].Name() })
	manifest := &aiPanoramaRelatedManifest{
		RootDevice: uint64(rootMetadata.Dev), RootInode: rootMetadata.Ino,
		RootMode: uint32(rootBefore.Mode().Perm()), RootUID: rootMetadata.Uid, RootGID: rootMetadata.Gid,
	}
	for _, entry := range entries {
		if !aiPanoramaRelatedName(entry.Name()) {
			return nil, fmt.Errorf("ai-panorama-governed-root-inventory-invalid")
		}
		if !tourV4SafeEntryName(entry.Name()) {
			return nil, fmt.Errorf("ai-panorama-related-entry-name-invalid")
		}
		if err := snapshotAiPanoramaEntry(root, entry.Name(), entry.Name(), manifest); err != nil {
			return nil, err
		}
	}
	rootAfter, err := root.Stat()
	if err != nil || !tourV4SameFingerprint(rootBefore, rootAfter) {
		return nil, fmt.Errorf("ai-panorama-public-root-race-detected")
	}
	sort.Slice(manifest.Entries, func(left, right int) bool { return manifest.Entries[left].Path < manifest.Entries[right].Path })
	manifest.Digest, err = aiPanoramaManifestDigest(manifest)
	if err != nil {
		return nil, err
	}
	return manifest, nil
}

func snapshotAiPanoramaEntry(parent *os.File, name, relpath string, manifest *aiPanoramaRelatedManifest) error {
	if parent == nil || manifest == nil || !tourV4SafeEntryName(name) || !tourV4SafeRelativePath(relpath) {
		return fmt.Errorf("ai-panorama-entry-input-invalid")
	}
	if directory, err := tourV4OpenDirectoryAt(parent, name); err == nil {
		defer directory.Close()
		before, err := directory.Stat()
		metadata, metadataErr := tourV4StatMetadata(before)
		if err != nil || metadataErr != nil || before.Mode().Perm() != 0o755 ||
			uint64(metadata.Dev) != manifest.RootDevice ||
			metadata.Uid != 10001 || metadata.Gid != 10001 || metadata.Nlink < 2 {
			return fmt.Errorf("ai-panorama-directory-metadata-invalid")
		}
		manifest.Entries = append(manifest.Entries, aiPanoramaManifestEntry{
			Path: relpath, Kind: "directory", Mode: 0o755, UID: metadata.Uid,
			GID: metadata.Gid, Nlink: metadata.Nlink,
		})
		if len(manifest.Entries) > aiPanoramaMaximumFiles+aiPanoramaMaximumDirectories {
			return fmt.Errorf("ai-panorama-tree-too-large")
		}
		duplicate, err := syscall.Dup(int(directory.Fd()))
		if err != nil {
			return fmt.Errorf("ai-panorama-directory-dup-failed")
		}
		reader := os.NewFile(uintptr(duplicate), "ai-panorama-directory-reader")
		children, err := reader.ReadDir(-1)
		reader.Close()
		if err != nil {
			return fmt.Errorf("ai-panorama-directory-read-failed")
		}
		sort.Slice(children, func(left, right int) bool { return children[left].Name() < children[right].Name() })
		for _, child := range children {
			childPath := relpath + "/" + child.Name()
			if err := snapshotAiPanoramaEntry(directory, child.Name(), childPath, manifest); err != nil {
				return err
			}
		}
		after, err := directory.Stat()
		if err != nil || !tourV4SameFingerprint(before, after) {
			return fmt.Errorf("ai-panorama-directory-race-detected")
		}
		return nil
	}
	file, err := tourV4ReadRegularAt(parent, name, relpath, aiPanoramaMaximumFileBytes)
	if err != nil {
		return fmt.Errorf("ai-panorama-file-invalid")
	}
	defer zero(file.Content)
	expectedMode := uint32(0o644)
	expectedUID := uint32(10001)
	expectedGID := uint32(10001)
	if relpath == aiPanoramaPraterSlug+"/tour.private.json" {
		expectedMode = 0o600
	}
	if relpath == aiPanoramaRevocationLeaf {
		expectedMode = 0o444
		expectedUID = 0
		expectedGID = 0
		if file.Size > 4096 {
			return fmt.Errorf("ai-panorama-revocation-marker-oversized")
		}
	}
	if file.Mode != expectedMode {
		return fmt.Errorf("ai-panorama-file-mode-invalid")
	}
	metadataUID, metadataGID, device, nlink, err := aiPanoramaEntryOwnership(parent, name)
	if err != nil || device != manifest.RootDevice ||
		metadataUID != expectedUID || metadataGID != expectedGID || nlink != 1 {
		return fmt.Errorf("ai-panorama-file-ownership-invalid")
	}
	manifest.Entries = append(manifest.Entries, aiPanoramaManifestEntry{
		Path: relpath, Kind: "file", Mode: file.Mode, UID: metadataUID,
		GID: metadataGID, Nlink: nlink, Size: file.Size, SHA256: file.SHA256,
	})
	if len(manifest.Entries) > aiPanoramaMaximumFiles+aiPanoramaMaximumDirectories {
		return fmt.Errorf("ai-panorama-tree-too-large")
	}
	var total int64
	var files, directories int
	for _, entry := range manifest.Entries {
		if entry.Kind == "file" {
			files++
			total += entry.Size
		} else {
			directories++
		}
	}
	if files > aiPanoramaMaximumFiles || directories > aiPanoramaMaximumDirectories || total > aiPanoramaMaximumTreeBytes {
		return fmt.Errorf("ai-panorama-tree-too-large")
	}
	return nil
}

func aiPanoramaEntryOwnership(parent *os.File, name string) (uint32, uint32, uint64, uint64, error) {
	if parent == nil || !tourV4SafeEntryName(name) {
		return 0, 0, 0, 0, fmt.Errorf("ai-panorama-entry-ownership-input-invalid")
	}
	fd, err := syscall.Openat(int(parent.Fd()), name, syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return 0, 0, 0, 0, err
	}
	file := os.NewFile(uintptr(fd), name)
	defer file.Close()
	info, err := file.Stat()
	metadata, ok := infoSys(info)
	if err != nil || !ok {
		return 0, 0, 0, 0, fmt.Errorf("ai-panorama-entry-stat-invalid")
	}
	return metadata.Uid, metadata.Gid, uint64(metadata.Dev), metadata.Nlink, nil
}

func runAiPanoramaDocker(ctx context.Context, executable string, arguments ...string) ([]byte, error) {
	if ctx == nil || executable != DockerExecutablePath || len(arguments) == 0 {
		return nil, fmt.Errorf("ai-panorama-docker-command-invalid")
	}
	for _, argument := range arguments {
		if argument == "" || len(argument) > 8192 || containsForbiddenArgumentByte(argument) {
			return nil, fmt.Errorf("ai-panorama-docker-argument-invalid")
		}
	}
	command := exec.CommandContext(ctx, executable, arguments...)
	command.Env = []string{"HOME=/", "LANG=C.UTF-8", "PATH=/usr/bin:/bin"}
	command.Stdin = bytes.NewReader(nil)
	var stdout, stderr aiPanoramaBoundedBuffer
	command.Stdout = &stdout
	command.Stderr = &stderr
	err := command.Run()
	if stdout.Overflow || stderr.Overflow {
		zero(stdout.Raw)
		zero(stderr.Raw)
		return nil, fmt.Errorf("ai-panorama-docker-output-too-large")
	}
	zero(stderr.Raw)
	if err != nil {
		zero(stdout.Raw)
		return nil, fmt.Errorf("ai-panorama-docker-command-failed")
	}
	result := append([]byte(nil), stdout.Raw...)
	zero(stdout.Raw)
	return result, nil
}

type aiPanoramaBoundedBuffer struct {
	Raw      []byte
	Overflow bool
}

func (buffer *aiPanoramaBoundedBuffer) Write(raw []byte) (int, error) {
	if buffer == nil {
		return 0, fmt.Errorf("ai-panorama-output-buffer-invalid")
	}
	if len(buffer.Raw)+len(raw) > aiPanoramaMaximumCommandOutput {
		remaining := aiPanoramaMaximumCommandOutput - len(buffer.Raw)
		if remaining > 0 {
			buffer.Raw = append(buffer.Raw, raw[:remaining]...)
		}
		buffer.Overflow = true
		return len(raw), nil
	}
	buffer.Raw = append(buffer.Raw, raw...)
	return len(raw), nil
}

func aiPanoramaDockerObject(raw []byte) (map[string]any, error) {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) < 2 || len(trimmed) > aiPanoramaMaximumCommandOutput {
		return nil, fmt.Errorf("ai-panorama-docker-observation-invalid")
	}
	if trimmed[0] == '[' {
		var items []json.RawMessage
		decoder := json.NewDecoder(bytes.NewReader(trimmed))
		if err := decoder.Decode(&items); err != nil || len(items) != 1 {
			return nil, fmt.Errorf("ai-panorama-docker-observation-invalid")
		}
		return strictJSON(items[0], aiPanoramaMaximumCommandOutput)
	}
	return strictJSON(trimmed, aiPanoramaMaximumCommandOutput)
}

func aiPanoramaDockerString(ctx context.Context, arguments ...string) (string, error) {
	raw, err := executeAiPanoramaDocker(ctx, DockerExecutablePath, arguments...)
	if err != nil {
		return "", err
	}
	defer zero(raw)
	value := strings.TrimSpace(string(raw))
	if value == "" || len(value) > 8192 || strings.ContainsAny(value, "\x00\r\n") {
		return "", fmt.Errorf("ai-panorama-docker-string-invalid")
	}
	return value, nil
}

func observeAiPanoramaRuntime(ctx context.Context, root string, config *Config) (*aiPanoramaRuntimeObservation, error) {
	if ctx == nil || config == nil || root != "/" {
		return nil, fmt.Errorf("ai-panorama-runtime-observation-input-invalid")
	}
	if err := validateCurrentRuntimeFileObservation(root, config.RuntimeDeploy["docker_executable"]); err != nil {
		return nil, fmt.Errorf("ai-panorama-docker-executable-changed")
	}
	if err := validateExactCurrentRuntimeInputs(root, config.RuntimeInputs); err != nil {
		return nil, fmt.Errorf("ai-panorama-runtime-inputs-changed")
	}
	dockerRootRaw, err := executeAiPanoramaDocker(ctx, DockerExecutablePath, "info", "--format", "{{json .DockerRootDir}}")
	if err != nil {
		return nil, err
	}
	var dockerRoot string
	if json.Unmarshal(bytes.TrimSpace(dockerRootRaw), &dockerRoot) != nil ||
		!filepath.IsAbs(dockerRoot) || filepath.Clean(dockerRoot) != dockerRoot || dockerRoot == "/" {
		zero(dockerRootRaw)
		return nil, fmt.Errorf("ai-panorama-docker-root-invalid")
	}
	zero(dockerRootRaw)

	imageRaw, err := executeAiPanoramaDocker(ctx, DockerExecutablePath, "image", "inspect", "--format", "{{.Id}}|{{.Config.User}}|{{json .RepoDigests}}|{{json .Config.Entrypoint}}", config.WebImage)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-image-unavailable")
	}
	imageParts := strings.Split(strings.TrimSpace(string(imageRaw)), "|")
	zero(imageRaw)
	if len(imageParts) != 4 || !digestPattern.MatchString(imageParts[0]) ||
		(imageParts[1] != "10001:10001" && imageParts[1] != "10001") {
		return nil, fmt.Errorf("ai-panorama-image-binding-invalid")
	}
	var repoDigests, entrypoint []any
	if json.Unmarshal([]byte(imageParts[2]), &repoDigests) != nil ||
		json.Unmarshal([]byte(imageParts[3]), &entrypoint) != nil ||
		!aiPanoramaStringArrayContains(repoDigests, config.WebImage) ||
		!aiPanoramaImageEntrypointValid(entrypoint) {
		return nil, fmt.Errorf("ai-panorama-image-binding-invalid")
	}
	imageID := imageParts[0]
	renderImageRaw, err := executeAiPanoramaDocker(ctx, DockerExecutablePath, "image", "inspect", "--format", "{{.Id}}|{{.Config.User}}|{{json .RepoDigests}}", config.RenderImage)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-render-image-unavailable")
	}
	renderImageParts := strings.Split(strings.TrimSpace(string(renderImageRaw)), "|")
	zero(renderImageRaw)
	var renderRepoDigests []any
	if len(renderImageParts) != 3 || !digestPattern.MatchString(renderImageParts[0]) ||
		(renderImageParts[1] != "10001:10001" && renderImageParts[1] != "10001") ||
		json.Unmarshal([]byte(renderImageParts[2]), &renderRepoDigests) != nil ||
		!aiPanoramaStringArrayContains(renderRepoDigests, config.RenderImage) {
		return nil, fmt.Errorf("ai-panorama-render-image-binding-invalid")
	}
	renderImageID := renderImageParts[0]

	volumeRaw, err := executeAiPanoramaDocker(ctx, DockerExecutablePath, "volume", "inspect", "--format", "{{json .}}", aiPanoramaPublicVolumeName)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-public-volume-unavailable")
	}
	volume, err := aiPanoramaDockerObject(volumeRaw)
	zero(volumeRaw)
	if err != nil {
		return nil, err
	}
	mountpoint, mountOK := exactString(volume["Mountpoint"])
	labels, labelsOK := volume["Labels"].(map[string]any)
	expectedMountpoint := filepath.Join(dockerRoot, "volumes", aiPanoramaPublicVolumeName, "_data")
	if volume["Name"] != aiPanoramaPublicVolumeName || volume["Driver"] != "local" ||
		volume["Scope"] != "local" || !mountOK || mountpoint != expectedMountpoint ||
		!labelsOK || !validStringMap(labels, 64) ||
		labels["com.docker.compose.project"] != ProjectName ||
		labels["com.docker.compose.volume"] != aiPanoramaPublicVolumeComposeKey {
		return nil, fmt.Errorf("ai-panorama-public-volume-binding-invalid")
	}
	volumeInfo, err := os.Lstat(mountpoint)
	volumeMetadata, metadataOK := infoSys(volumeInfo)
	if err != nil || !metadataOK || !volumeInfo.IsDir() ||
		volumeInfo.Mode().Perm() != 0o755 || volumeInfo.Mode()&os.ModeSymlink != 0 ||
		volumeMetadata.Nlink < 2 ||
		!((volumeMetadata.Uid == 0 && volumeMetadata.Gid == 0) ||
			(volumeMetadata.Uid == 10001 && volumeMetadata.Gid == 10001)) {
		return nil, fmt.Errorf("ai-panorama-public-volume-metadata-invalid")
	}
	controlInfo, err := os.Lstat(aiPanoramaControlRoot)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-control-root-metadata-invalid")
	}
	controlMetadata, controlOK := infoSys(controlInfo)
	if !controlOK || !controlInfo.IsDir() || controlInfo.Mode().Perm() != 0o700 ||
		controlInfo.Mode()&os.ModeSymlink != 0 || controlMetadata.Uid != 0 ||
		controlMetadata.Gid != 0 || controlMetadata.Nlink < 2 {
		return nil, fmt.Errorf("ai-panorama-control-root-metadata-invalid")
	}

	if config.DatabaseSubstrate == nil {
		return nil, fmt.Errorf("ai-panorama-database-container-binding-invalid")
	}
	database, err := observeAiPanoramaContainer(ctx, config.DatabaseSubstrate.containerID, aiPanoramaDatabaseService)
	if err != nil ||
		database.ID != config.DatabaseSubstrate.containerID ||
		database.ImageID != config.DatabaseSubstrate.imageID ||
		database.ConfiguredImage != config.DatabaseImage ||
		!database.Running {
		return nil, fmt.Errorf("ai-panorama-database-container-binding-invalid")
	}
	api, err := observeAiPanoramaComposeService(ctx, aiPanoramaAPIRuntimeService)
	if err != nil || api.ImageID != imageID || api.ConfiguredImage != config.WebImage || !api.Running {
		return nil, fmt.Errorf("ai-panorama-api-container-binding-invalid")
	}
	scheduler, err := observeAiPanoramaComposeService(ctx, aiPanoramaSchedulerService)
	if err != nil || scheduler.ImageID != imageID ||
		scheduler.ConfiguredImage != config.WebImage || !scheduler.Running {
		return nil, fmt.Errorf("ai-panorama-scheduler-container-binding-invalid")
	}
	render, err := observeAiPanoramaOptionalComposeService(ctx, aiPanoramaRenderService)
	if err != nil || (render != nil && (render.ImageID != renderImageID ||
		render.ConfiguredImage != config.RenderImage)) {
		return nil, fmt.Errorf("ai-panorama-render-container-binding-invalid")
	}
	renderContainerID := ""
	if render != nil {
		renderContainerID = render.ID
	}
	if err := validateAiPanoramaPublicVolumeConsumers(ctx, mountpoint, renderContainerID); err != nil {
		return nil, err
	}
	return &aiPanoramaRuntimeObservation{
		DockerRoot: dockerRoot, ImageID: imageID,
		ControlRootDevice: uint64(controlMetadata.Dev), ControlRootInode: controlMetadata.Ino,
		PublicVolumeMountpoint: mountpoint, PublicVolumeDevice: uint64(volumeMetadata.Dev),
		PublicVolumeInode: volumeMetadata.Ino, PublicVolumeUID: volumeMetadata.Uid,
		PublicVolumeGID: volumeMetadata.Gid, PublicVolumeMode: uint32(volumeInfo.Mode().Perm()),
		PublicVolumeNeedsInitialization: volumeMetadata.Uid == 0,
		DatabaseContainerID:             database.ID, DatabaseContainerName: database.Name,
		DatabaseImageID:       database.ImageID,
		APIRuntimeContainerID: api.ID, APIRuntimeContainerName: api.Name,
		APIRuntimeImageID:    api.ImageID,
		SchedulerContainerID: scheduler.ID, SchedulerContainerName: scheduler.Name,
		SchedulerImageID: scheduler.ImageID,
		RenderContainerID: func() string {
			if render == nil {
				return ""
			}
			return render.ID
		}(),
		RenderContainerName: func() string {
			if render == nil {
				return ""
			}
			return render.Name
		}(),
		RenderImageID: func() string {
			if render == nil {
				return ""
			}
			return render.ImageID
		}(),
	}, nil
}

func aiPanoramaStringArrayContains(values []any, expected string) bool {
	found := 0
	for _, value := range values {
		text, ok := exactString(value)
		if !ok {
			return false
		}
		if text == expected {
			found++
		}
	}
	return found == 1
}

func aiPanoramaImageEntrypointValid(entrypoint []any) bool {
	if len(entrypoint) != 4 {
		return false
	}
	expected := []string{aiPanoramaControllerPython, "-I", "-S", "/usr/local/libexec/property_web_entrypoint.py"}
	for index, value := range entrypoint {
		text, ok := exactString(value)
		if !ok || text != expected[index] {
			return false
		}
	}
	return true
}

type aiPanoramaContainerObservation struct {
	ID              string
	Name            string
	ComposeProject  string
	ComposeService  string
	ImageID         string
	ConfiguredImage string
	Running         bool
}

func aiPanoramaComposeServiceValid(service string) bool {
	return service == aiPanoramaDatabaseService || service == aiPanoramaAPIRuntimeService ||
		service == aiPanoramaSchedulerService || service == aiPanoramaRenderService
}

func observeAiPanoramaComposeService(ctx context.Context, service string) (*aiPanoramaContainerObservation, error) {
	if ctx == nil || !aiPanoramaComposeServiceValid(service) {
		return nil, fmt.Errorf("ai-panorama-compose-service-invalid")
	}
	ids, err := aiPanoramaComposeServiceIDs(ctx, service)
	if err != nil {
		return nil, err
	}
	if len(ids) != 1 {
		return nil, fmt.Errorf("ai-panorama-compose-service-cardinality-invalid")
	}
	return observeAiPanoramaContainer(ctx, ids[0], service)
}

func observeAiPanoramaOptionalComposeService(ctx context.Context, service string) (*aiPanoramaContainerObservation, error) {
	if ctx == nil || service != aiPanoramaRenderService {
		return nil, fmt.Errorf("ai-panorama-optional-compose-service-invalid")
	}
	ids, err := aiPanoramaComposeServiceIDs(ctx, service)
	if err != nil {
		return nil, err
	}
	if len(ids) == 0 {
		return nil, nil
	}
	if len(ids) != 1 {
		return nil, fmt.Errorf("ai-panorama-compose-service-cardinality-invalid")
	}
	return observeAiPanoramaContainer(ctx, ids[0], service)
}

func aiPanoramaComposeServiceIDs(ctx context.Context, service string) ([]string, error) {
	if ctx == nil || !aiPanoramaComposeServiceValid(service) {
		return nil, fmt.Errorf("ai-panorama-compose-service-invalid")
	}
	raw, err := executeAiPanoramaDocker(
		ctx, DockerExecutablePath, "container", "ls", "--all", "--no-trunc",
		"--filter", "label=com.docker.compose.project="+ProjectName,
		"--filter", "label=com.docker.compose.service="+service,
		"--format", "{{.ID}}",
	)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-compose-service-observation-failed")
	}
	ids := strings.Fields(string(raw))
	zero(raw)
	for _, id := range ids {
		if !runtimeContainerIDPattern.MatchString(id) {
			return nil, fmt.Errorf("ai-panorama-compose-service-id-invalid")
		}
	}
	return ids, nil
}

func observeAiPanoramaContainer(ctx context.Context, reference, expectedService string) (*aiPanoramaContainerObservation, error) {
	if ctx == nil || !runtimeContainerIDPattern.MatchString(reference) ||
		!aiPanoramaComposeServiceValid(expectedService) {
		return nil, fmt.Errorf("ai-panorama-container-reference-invalid")
	}
	raw, err := executeAiPanoramaDocker(
		ctx, DockerExecutablePath, "container", "inspect", "--format",
		`{{.Id}}|{{.Name}}|{{.Image}}|{{.Config.Image}}|{{.State.Running}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}`,
		reference,
	)
	if err != nil {
		return nil, err
	}
	parts := strings.Split(strings.TrimSpace(string(raw)), "|")
	zero(raw)
	if len(parts) != 7 {
		return nil, fmt.Errorf("ai-panorama-container-observation-invalid")
	}
	name := strings.TrimPrefix(parts[1], "/")
	if parts[0] != reference ||
		!runtimeContainerNamePattern.MatchString(name) ||
		!digestPattern.MatchString(parts[2]) || parts[3] == "" ||
		(parts[4] != "true" && parts[4] != "false") ||
		parts[5] != ProjectName || parts[6] != expectedService {
		return nil, fmt.Errorf("ai-panorama-container-observation-invalid")
	}
	return &aiPanoramaContainerObservation{
		ID: parts[0], Name: name, ImageID: parts[2], ConfiguredImage: parts[3],
		Running: parts[4] == "true", ComposeProject: parts[5], ComposeService: parts[6],
	}, nil
}

func aiPanoramaRuntimeObservationValue(observation *aiPanoramaRuntimeObservation) map[string]any {
	return map[string]any{
		"api_runtime_container_id":           observation.APIRuntimeContainerID,
		"api_runtime_container_name":         observation.APIRuntimeContainerName,
		"api_runtime_image_id":               observation.APIRuntimeImageID,
		"database_container_id":              observation.DatabaseContainerID,
		"database_container_name":            observation.DatabaseContainerName,
		"database_image_id":                  observation.DatabaseImageID,
		"docker_root":                        observation.DockerRoot,
		"control_root_device":                json.Number(strconv.FormatUint(observation.ControlRootDevice, 10)),
		"control_root_inode":                 json.Number(strconv.FormatUint(observation.ControlRootInode, 10)),
		"public_volume_device":               json.Number(strconv.FormatUint(observation.PublicVolumeDevice, 10)),
		"public_volume_gid":                  json.Number(strconv.FormatUint(uint64(observation.PublicVolumeGID), 10)),
		"public_volume_inode":                json.Number(strconv.FormatUint(observation.PublicVolumeInode, 10)),
		"public_volume_mode":                 json.Number(strconv.FormatUint(uint64(observation.PublicVolumeMode), 10)),
		"public_volume_mountpoint":           observation.PublicVolumeMountpoint,
		"public_volume_name":                 aiPanoramaPublicVolumeName,
		"public_volume_uid":                  json.Number(strconv.FormatUint(uint64(observation.PublicVolumeUID), 10)),
		"public_volume_needs_initialization": observation.PublicVolumeNeedsInitialization,
		"render_container_id":                observation.RenderContainerID,
		"render_container_name":              observation.RenderContainerName,
		"render_image_id":                    observation.RenderImageID,
		"scheduler_container_id":             observation.SchedulerContainerID,
		"scheduler_container_name":           observation.SchedulerContainerName,
		"scheduler_image_id":                 observation.SchedulerImageID,
		"web_image_id":                       observation.ImageID,
	}
}

func parseAiPanoramaRuntimeObservationValue(
	value map[string]any,
) (*aiPanoramaRuntimeObservation, error) {
	if value == nil || !hasKeys(
		value,
		"api_runtime_container_id", "api_runtime_container_name",
		"api_runtime_image_id", "database_container_id",
		"database_container_name", "database_image_id", "docker_root",
		"control_root_device", "control_root_inode", "public_volume_device",
		"public_volume_gid", "public_volume_inode", "public_volume_mode",
		"public_volume_mountpoint", "public_volume_name", "public_volume_uid",
		"public_volume_needs_initialization", "render_container_id",
		"render_container_name", "render_image_id", "scheduler_container_id",
		"scheduler_container_name", "scheduler_image_id", "web_image_id",
	) {
		return nil, fmt.Errorf("ai-panorama-runtime-value-shape-invalid")
	}
	stringField := func(name string) (string, bool) {
		text, ok := value[name].(string)
		return text, ok && len(text) <= 8192 &&
			!strings.ContainsAny(text, "\x00\r\n")
	}
	apiID, apiIDOK := stringField("api_runtime_container_id")
	apiName, apiNameOK := stringField("api_runtime_container_name")
	apiImageID, apiImageOK := stringField("api_runtime_image_id")
	databaseID, databaseIDOK := stringField("database_container_id")
	databaseName, databaseNameOK := stringField("database_container_name")
	databaseImageID, databaseImageOK := stringField("database_image_id")
	dockerRoot, dockerRootOK := stringField("docker_root")
	mountpoint, mountpointOK := stringField("public_volume_mountpoint")
	renderID, renderIDOK := stringField("render_container_id")
	renderName, renderNameOK := stringField("render_container_name")
	renderImageID, renderImageOK := stringField("render_image_id")
	schedulerID, schedulerIDOK := stringField("scheduler_container_id")
	schedulerName, schedulerNameOK := stringField("scheduler_container_name")
	schedulerImageID, schedulerImageOK :=
		stringField("scheduler_image_id")
	webImageID, webImageOK := stringField("web_image_id")
	controlDevice, controlDeviceOK :=
		exactInt(value["control_root_device"], 0, 1<<62)
	controlInode, controlInodeOK :=
		exactInt(value["control_root_inode"], 0, 1<<62)
	volumeDevice, volumeDeviceOK :=
		exactInt(value["public_volume_device"], 1, 1<<62)
	volumeInode, volumeInodeOK :=
		exactInt(value["public_volume_inode"], 1, 1<<62)
	volumeUID, volumeUIDOK :=
		exactInt(value["public_volume_uid"], 0, 1<<32-1)
	volumeGID, volumeGIDOK :=
		exactInt(value["public_volume_gid"], 0, 1<<32-1)
	volumeMode, volumeModeOK :=
		exactInt(value["public_volume_mode"], 0, 1<<32-1)
	needsInitialization, needsOK :=
		value["public_volume_needs_initialization"].(bool)
	if !apiIDOK || !apiNameOK || !apiImageOK ||
		!databaseIDOK || !databaseNameOK || !databaseImageOK ||
		!dockerRootOK || !filepath.IsAbs(dockerRoot) ||
		filepath.Clean(dockerRoot) != dockerRoot || dockerRoot == "/" ||
		!mountpointOK || !filepath.IsAbs(mountpoint) ||
		filepath.Clean(mountpoint) != mountpoint ||
		!renderIDOK || !renderNameOK || !renderImageOK ||
		!schedulerIDOK || !schedulerNameOK || !schedulerImageOK ||
		!webImageOK || !digestPattern.MatchString(webImageID) ||
		!controlDeviceOK || !controlInodeOK ||
		!volumeDeviceOK || !volumeInodeOK ||
		!volumeUIDOK || !volumeGIDOK || !volumeModeOK || !needsOK ||
		value["public_volume_name"] != aiPanoramaPublicVolumeName {
		return nil, fmt.Errorf("ai-panorama-runtime-value-invalid")
	}
	validIdentity := func(id, name, image string) bool {
		if id == "" && name == "" && image == "" {
			return true
		}
		return runtimeContainerIDPattern.MatchString(id) &&
			runtimeContainerNamePattern.MatchString(name) &&
			digestPattern.MatchString(image)
	}
	if !validIdentity(apiID, apiName, apiImageID) ||
		!validIdentity(databaseID, databaseName, databaseImageID) ||
		!validIdentity(renderID, renderName, renderImageID) ||
		!validIdentity(schedulerID, schedulerName, schedulerImageID) {
		return nil, fmt.Errorf("ai-panorama-runtime-value-container-invalid")
	}
	observation := &aiPanoramaRuntimeObservation{
		DockerRoot: dockerRoot, ImageID: webImageID,
		ControlRootDevice:               uint64(controlDevice),
		ControlRootInode:                uint64(controlInode),
		PublicVolumeMountpoint:          mountpoint,
		PublicVolumeDevice:              uint64(volumeDevice),
		PublicVolumeInode:               uint64(volumeInode),
		PublicVolumeUID:                 uint32(volumeUID),
		PublicVolumeGID:                 uint32(volumeGID),
		PublicVolumeMode:                uint32(volumeMode),
		PublicVolumeNeedsInitialization: needsInitialization,
		DatabaseContainerID:             databaseID,
		DatabaseContainerName:           databaseName,
		DatabaseImageID:                 databaseImageID,
		APIRuntimeContainerID:           apiID,
		APIRuntimeContainerName:         apiName,
		APIRuntimeImageID:               apiImageID,
		SchedulerContainerID:            schedulerID,
		SchedulerContainerName:          schedulerName,
		SchedulerImageID:                schedulerImageID,
		RenderContainerID:               renderID,
		RenderContainerName:             renderName,
		RenderImageID:                   renderImageID,
	}
	if !canonicalValuesEqual(
		value, aiPanoramaRuntimeObservationValue(observation),
	) {
		return nil, fmt.Errorf("ai-panorama-runtime-value-noncanonical")
	}
	return observation, nil
}

func validateAiPanoramaPublicVolumeConsumers(
	ctx context.Context,
	mountpoint string,
	expectedRenderContainerID string,
) error {
	raw, err := executeAiPanoramaDocker(
		ctx, DockerExecutablePath, "container", "ls", "--all", "--no-trunc",
		"--filter", "volume="+aiPanoramaPublicVolumeName,
		"--format", `{{.ID}}|{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.service"}}`,
	)
	if err != nil {
		return fmt.Errorf("ai-panorama-public-volume-consumers-unavailable")
	}
	rows := strings.Fields(string(raw))
	zero(raw)
	allowed := []string{aiPanoramaAPIRuntimeService, aiPanoramaRenderService, aiPanoramaSchedulerService}
	consumers := make(map[string]string, len(rows))
	for _, row := range rows {
		parts := strings.Split(row, "|")
		if len(parts) != 3 || !runtimeContainerIDPattern.MatchString(parts[0]) ||
			parts[1] != ProjectName || !containsString(allowed, parts[2]) ||
			consumers[parts[2]] != "" {
			return fmt.Errorf("ai-panorama-public-volume-consumer-set-invalid")
		}
		consumers[parts[2]] = parts[0]
	}
	if consumers[aiPanoramaAPIRuntimeService] == "" ||
		consumers[aiPanoramaSchedulerService] == "" ||
		(expectedRenderContainerID != "" &&
			consumers[aiPanoramaRenderService] != expectedRenderContainerID) ||
		(expectedRenderContainerID == "" &&
			consumers[aiPanoramaRenderService] != "") ||
		len(consumers) < 2 || len(consumers) > len(allowed) {
		return fmt.Errorf("ai-panorama-public-volume-consumer-set-invalid")
	}
	services := []string{aiPanoramaAPIRuntimeService, aiPanoramaSchedulerService}
	if consumers[aiPanoramaRenderService] != "" {
		services = append(services, aiPanoramaRenderService)
	}
	for _, service := range services {
		id := consumers[service]
		observation, err := observeAiPanoramaContainer(ctx, id, service)
		if err != nil ||
			(service != aiPanoramaRenderService && !observation.Running) {
			return fmt.Errorf("ai-panorama-public-volume-consumer-set-invalid")
		}
		raw, err := executeAiPanoramaDocker(
			ctx, DockerExecutablePath, "container", "inspect", "--format",
			"{{range .Mounts}}{{json .}}{{println}}{{end}}", id,
		)
		if err != nil {
			return fmt.Errorf("ai-panorama-public-volume-mount-observation-failed")
		}
		rows := bytes.Split(bytes.TrimSpace(raw), []byte{'\n'})
		found := 0
		for _, row := range rows {
			if len(bytes.TrimSpace(row)) == 0 {
				continue
			}
			mount, err := strictJSON(bytes.TrimSpace(row), 64*1024)
			if err != nil {
				zero(raw)
				return fmt.Errorf("ai-panorama-public-volume-mount-invalid")
			}
			if mount["Name"] != aiPanoramaPublicVolumeName {
				continue
			}
			if mount["Type"] != "volume" || mount["Source"] != mountpoint ||
				mount["Destination"] != aiPanoramaPublicMountTarget ||
				mount["Driver"] != "local" || mount["RW"] != false {
				zero(raw)
				return fmt.Errorf("ai-panorama-public-volume-mount-not-read-only")
			}
			found++
		}
		zero(raw)
		if found != 1 {
			return fmt.Errorf("ai-panorama-public-volume-mount-invalid")
		}
		if service == aiPanoramaAPIRuntimeService {
			expected := "EA_GOVERNED_PUBLIC_TOUR_DIR=" + aiPanoramaPublicMountTarget
			raw, err := executeAiPanoramaDocker(
				ctx, DockerExecutablePath, "container", "inspect", "--format",
				`{{range .Config.Env}}{{if eq (index (split . "=") 0) "EA_GOVERNED_PUBLIC_TOUR_DIR"}}{{println .}}{{end}}{{end}}`, id,
			)
			if err != nil {
				return fmt.Errorf("ai-panorama-api-governed-path-observation-failed")
			}
			observed := strings.TrimSpace(string(raw))
			zero(raw)
			if observed != expected {
				return fmt.Errorf("ai-panorama-api-governed-path-invalid")
			}
		}
	}
	return nil
}

func aiPanoramaNetworkName(config *Config) (string, error) {
	if config == nil || !deploymentIDPattern.MatchString(config.DeploymentID) ||
		!shaPattern.MatchString(config.RuntimeSHA) {
		return "", fmt.Errorf("ai-panorama-network-binding-invalid")
	}
	name := "pq-ai-panorama-prater-" + config.DeploymentID[:16]
	if !runtimeContainerNamePattern.MatchString(name) {
		return "", fmt.Errorf("ai-panorama-network-name-invalid")
	}
	return name, nil
}

type aiPanoramaNetworkObservation struct {
	Name       string
	ID         string
	DBAttached bool
}

func aiPanoramaNetworkExists(ctx context.Context, name string) (bool, error) {
	raw, err := executeAiPanoramaDocker(ctx, DockerExecutablePath, "network", "ls", "--filter", "name=^"+name+"$", "--format", "{{.Name}}")
	if err != nil {
		return false, err
	}
	defer zero(raw)
	trimmed := strings.TrimSpace(string(raw))
	if trimmed == "" {
		return false, nil
	}
	if trimmed != name {
		return false, fmt.Errorf("ai-panorama-network-list-ambiguous")
	}
	return true, nil
}

func observeAiPanoramaNetwork(ctx context.Context, config *Config, runtime *aiPanoramaRuntimeObservation) (*aiPanoramaNetworkObservation, error) {
	if ctx == nil || config == nil || runtime == nil {
		return nil, fmt.Errorf("ai-panorama-network-observation-input-invalid")
	}
	name, err := aiPanoramaNetworkName(config)
	if err != nil {
		return nil, err
	}
	raw, err := executeAiPanoramaDocker(ctx, DockerExecutablePath, "network", "inspect", "--format", "{{json .}}", name)
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-network-unavailable")
	}
	value, err := aiPanoramaDockerObject(raw)
	zero(raw)
	if err != nil {
		return nil, err
	}
	id, idOK := exactString(value["Id"])
	labels, labelsOK := value["Labels"].(map[string]any)
	containers, containersOK := value["Containers"].(map[string]any)
	if !idOK || !runtimeContainerIDPattern.MatchString(id) ||
		value["Name"] != name || value["Driver"] != "bridge" || value["Internal"] != true ||
		!labelsOK || !containersOK ||
		!canonicalValuesEqual(labels, aiPanoramaNetworkLabels(config)) {
		return nil, fmt.Errorf("ai-panorama-network-binding-invalid")
	}
	databaseAttached := false
	for containerID, rawEndpoint := range containers {
		endpoint, ok := rawEndpoint.(map[string]any)
		if !ok || containerID != runtime.DatabaseContainerID || endpoint["Name"] != runtime.DatabaseContainerName {
			return nil, fmt.Errorf("ai-panorama-network-unexpected-endpoint")
		}
		if databaseAttached {
			return nil, fmt.Errorf("ai-panorama-network-unexpected-endpoint")
		}
		databaseAttached = true
	}
	return &aiPanoramaNetworkObservation{Name: name, ID: id, DBAttached: databaseAttached}, nil
}

func aiPanoramaNetworkLabels(config *Config) map[string]any {
	return map[string]any{
		aiPanoramaNetworkLabel:                         "v1",
		"propertyquarry.release-control.config-digest": config.Digest,
		"propertyquarry.release-control.deployment-id": config.DeploymentID,
		"propertyquarry.release-control.runtime-sha":   config.RuntimeSHA,
	}
}

func ensureAiPanoramaNetwork(ctx context.Context, config *Config, runtime *aiPanoramaRuntimeObservation) (*aiPanoramaNetworkObservation, error) {
	name, err := aiPanoramaNetworkName(config)
	if err != nil {
		return nil, err
	}
	exists, err := aiPanoramaNetworkExists(ctx, name)
	if err != nil {
		return nil, err
	}
	if !exists {
		arguments := []string{"network", "create", "--driver", "bridge", "--internal"}
		labels := aiPanoramaNetworkLabels(config)
		keys := make([]string, 0, len(labels))
		for key := range labels {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		for _, key := range keys {
			arguments = append(arguments, "--label", key+"="+stringValue(labels[key]))
		}
		arguments = append(arguments, name)
		raw, err := executeAiPanoramaDocker(ctx, DockerExecutablePath, arguments...)
		if err != nil {
			return nil, fmt.Errorf("ai-panorama-network-create-failed")
		}
		networkID := strings.TrimSpace(string(raw))
		zero(raw)
		if !runtimeContainerIDPattern.MatchString(networkID) {
			return nil, fmt.Errorf("ai-panorama-network-create-result-invalid")
		}
	}
	observation, err := observeAiPanoramaNetwork(ctx, config, runtime)
	if err != nil {
		return nil, err
	}
	if !observation.DBAttached {
		if _, err := executeAiPanoramaDocker(
			ctx, DockerExecutablePath, "network", "connect", "--alias",
			aiPanoramaDatabaseService, observation.Name, runtime.DatabaseContainerID,
		); err != nil {
			return nil, fmt.Errorf("ai-panorama-network-connect-failed")
		}
		observation, err = observeAiPanoramaNetwork(ctx, config, runtime)
		if err != nil || !observation.DBAttached {
			return nil, fmt.Errorf("ai-panorama-network-connect-unverified")
		}
	}
	return observation, nil
}

func cleanupAiPanoramaNetwork(ctx context.Context, config *Config, runtime *aiPanoramaRuntimeObservation) error {
	name, err := aiPanoramaNetworkName(config)
	if err != nil {
		return err
	}
	exists, err := aiPanoramaNetworkExists(ctx, name)
	if err != nil || !exists {
		return err
	}
	observation, err := observeAiPanoramaNetwork(ctx, config, runtime)
	if err != nil {
		return err
	}
	if observation.DBAttached {
		if _, err := executeAiPanoramaDocker(ctx, DockerExecutablePath, "network", "disconnect", observation.Name, runtime.DatabaseContainerID); err != nil {
			return fmt.Errorf("ai-panorama-network-disconnect-failed")
		}
		observation, err = observeAiPanoramaNetwork(ctx, config, runtime)
		if err != nil || observation.DBAttached {
			return fmt.Errorf("ai-panorama-network-disconnect-unverified")
		}
	}
	if _, err := executeAiPanoramaDocker(ctx, DockerExecutablePath, "network", "rm", observation.Name); err != nil {
		return fmt.Errorf("ai-panorama-network-remove-failed")
	}
	exists, err = aiPanoramaNetworkExists(ctx, name)
	if err != nil || exists {
		return fmt.Errorf("ai-panorama-network-remove-unverified")
	}
	return nil
}

func aiPanoramaContainerName(config *Config, phase string) (string, error) {
	if config == nil || !deploymentIDPattern.MatchString(config.DeploymentID) ||
		!aiPanoramaPhaseValid(phase) {
		return "", fmt.Errorf("ai-panorama-container-name-input-invalid")
	}
	name := "pq-ai-panorama-prater-" + phase + "-" + config.DeploymentID[:12]
	if !runtimeContainerNamePattern.MatchString(name) {
		return "", fmt.Errorf("ai-panorama-container-name-invalid")
	}
	return name, nil
}

func aiPanoramaPhaseValid(phase string) bool {
	return phase == "bootstrap" || phase == "discover" ||
		phase == "preflight" || phase == "apply" || phase == "closeout"
}

func aiPanoramaOperationForPhase(phase string) string {
	if phase == "closeout" {
		return aiPanoramaCloseoutOperation
	}
	if aiPanoramaPhaseValid(phase) {
		return aiPanoramaInstallOperation
	}
	return ""
}

func aiPanoramaContainerArguments(
	config *Config,
	runtime *aiPanoramaRuntimeObservation,
	sealed *aiPanoramaSealedArtifactObservation,
	network *aiPanoramaNetworkObservation,
	phase string,
	databaseSecretSource string,
) ([]string, error) {
	if config == nil || runtime == nil || !aiPanoramaPhaseValid(phase) {
		return nil, fmt.Errorf("ai-panorama-container-contract-invalid")
	}
	requiresDatabase := phase == "discover" || phase == "apply"
	switch phase {
	case "discover":
		if network == nil || network.Name == "" || network.ID == "" ||
			!filepath.IsAbs(databaseSecretSource) ||
			filepath.Clean(databaseSecretSource) != databaseSecretSource ||
			sealed != nil {
			return nil, fmt.Errorf("ai-panorama-container-database-contract-invalid")
		}
	case "apply":
		if network == nil || network.Name == "" || network.ID == "" ||
			!filepath.IsAbs(databaseSecretSource) ||
			filepath.Clean(databaseSecretSource) != databaseSecretSource ||
			sealed == nil {
			return nil, fmt.Errorf("ai-panorama-container-database-contract-invalid")
		}
	case "preflight":
		if network != nil || databaseSecretSource != "" || sealed == nil {
			return nil, fmt.Errorf("ai-panorama-container-preflight-contract-invalid")
		}
	case "bootstrap", "closeout":
		if network != nil || databaseSecretSource != "" || sealed != nil {
			return nil, fmt.Errorf("ai-panorama-container-isolated-contract-invalid")
		}
	}
	name, err := aiPanoramaContainerName(config, phase)
	if err != nil {
		return nil, err
	}
	arguments := []string{
		"run", "--rm", "--pull", "never", "--name", name,
		"--log-driver", "none",
		"--read-only", "--user", "0:0",
		"--cap-drop", "ALL",
		"--security-opt", "no-new-privileges:true",
		"--pids-limit", "64", "--memory", "512m", "--memory-swap", "512m",
		"--cpus", "1.0", "--stop-timeout", "30", "--no-healthcheck",
		"--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=67108864,mode=1777",
		"--label", aiPanoramaNetworkLabel + "=v1",
		"--label", "propertyquarry.release-control.config-digest=" + config.Digest,
		"--label", "propertyquarry.release-control.deployment-id=" + config.DeploymentID,
		"--label", "propertyquarry.release-control.operation=" + aiPanoramaOperationForPhase(phase),
		"--label", "propertyquarry.release-control.phase=" + phase,
		"--env", "HOME=/nonexistent",
		"--env", "LANG=C.UTF-8",
		"--env", "PYTHONDONTWRITEBYTECODE=1",
		"--env", "PYTHONUNBUFFERED=1",
	}
	if requiresDatabase {
		arguments = append(arguments, "--network", network.Name)
	} else {
		arguments = append(arguments, "--network", "none")
	}
	if phase == "apply" {
		arguments = append(arguments,
			"--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER",
		)
	} else if phase == "bootstrap" {
		arguments = append(arguments, "--cap-add", "CHOWN")
	} else if phase == "closeout" {
		arguments = append(arguments, "--cap-add", "DAC_OVERRIDE")
	}
	entrypoint := aiPanoramaControllerEntrypoint
	switch phase {
	case "bootstrap":
		entrypoint = aiPanoramaBootstrapEntrypoint
		arguments = append(arguments,
			"--mount", aiPanoramaVolumeMount(
				aiPanoramaPublicVolumeName, aiPanoramaPublicMountTarget, false,
			),
		)
	case "discover":
		entrypoint = aiPanoramaDiscoveryEntrypoint
		arguments = append(arguments,
			"--mount", aiPanoramaBindMount(aiPanoramaControlRoot, aiPanoramaControlRoot, true),
			"--mount", aiPanoramaBindMount(databaseSecretSource, aiPanoramaDatabaseSecretMount, true),
		)
	case "preflight":
		arguments = append(arguments,
			"--mount", aiPanoramaBindMount(aiPanoramaControlRoot, aiPanoramaControlRoot, true),
			"--mount", aiPanoramaBindMount(aiPanoramaSealedArtifactRoot, aiPanoramaSealedArtifactRoot, true),
			"--mount", aiPanoramaBindMount(aiPanoramaVolumeProfilePath, aiPanoramaVolumeProfilePath, true),
			"--mount", aiPanoramaBindMount(aiPanoramaComposePlanPath, aiPanoramaComposePlanPath, true),
			"--mount", aiPanoramaBindMount(aiPanoramaTrustAssertionPath, aiPanoramaTrustAssertionPath, true),
			"--mount", aiPanoramaBindMount(aiPanoramaPurposeKeyringPath, aiPanoramaPurposeKeyringPath, true),
			"--mount", aiPanoramaVolumeMount(aiPanoramaPublicVolumeName, aiPanoramaPublicMountTarget, true),
		)
		entrypoint = aiPanoramaPreflightEntrypoint
	case "apply":
		arguments = append(arguments,
			"--mount", aiPanoramaBindMount(aiPanoramaControlRoot, aiPanoramaControlRoot, false),
			"--mount", aiPanoramaBindMount(aiPanoramaSealedArtifactRoot, aiPanoramaSealedArtifactRoot, true),
			"--mount", aiPanoramaBindMount(aiPanoramaVolumeProfilePath, aiPanoramaVolumeProfilePath, true),
			"--mount", aiPanoramaBindMount(aiPanoramaComposePlanPath, aiPanoramaComposePlanPath, true),
			"--mount", aiPanoramaBindMount(aiPanoramaTrustAssertionPath, aiPanoramaTrustAssertionPath, true),
			"--mount", aiPanoramaBindMount(aiPanoramaPurposeKeyringPath, aiPanoramaPurposeKeyringPath, true),
			"--mount", aiPanoramaBindMount(databaseSecretSource, aiPanoramaDatabaseSecretMount, true),
			"--mount", aiPanoramaVolumeMount(aiPanoramaPublicVolumeName, aiPanoramaPublicMountTarget, false),
		)
	case "closeout":
		entrypoint = aiPanoramaCloseoutEntrypoint
		arguments = append(arguments,
			"--mount", aiPanoramaBindMount(
				aiPanoramaCloseoutRequestPath, aiPanoramaCloseoutRequestPath, true,
			),
			"--mount", aiPanoramaVolumeMount(
				aiPanoramaPublicVolumeName, aiPanoramaPublicMountTarget, false,
			),
		)
	}
	arguments = append(arguments,
		"--entrypoint", aiPanoramaControllerPython,
		config.WebImage, "-I", "-B", entrypoint,
	)
	return arguments, nil
}

func aiPanoramaBindMount(source, target string, readOnly bool) string {
	value := "type=bind,src=" + source + ",dst=" + target + ",bind-propagation=rprivate"
	if readOnly {
		value += ",readonly"
	}
	return value
}

func aiPanoramaVolumeMount(source, target string, readOnly bool) string {
	value := "type=volume,src=" + source + ",dst=" + target + ",volume-nocopy"
	if readOnly {
		value += ",readonly"
	}
	return value
}

func aiPanoramaPhaseContainerExists(ctx context.Context, config *Config, phase string) (bool, error) {
	name, err := aiPanoramaContainerName(config, phase)
	if err != nil {
		return false, err
	}
	raw, err := executeAiPanoramaDocker(
		ctx, DockerExecutablePath, "container", "ls", "--all", "--no-trunc",
		"--filter", "name=^/"+name+"$", "--format", "{{.Names}}",
	)
	if err != nil {
		return false, fmt.Errorf("ai-panorama-phase-container-list-failed")
	}
	defer zero(raw)
	observed := strings.TrimSpace(string(raw))
	if observed == "" {
		return false, nil
	}
	if observed != name {
		return false, fmt.Errorf("ai-panorama-phase-container-list-ambiguous")
	}
	return true, nil
}

func cleanupAiPanoramaPhaseContainer(
	ctx context.Context,
	config *Config,
	runtime *aiPanoramaRuntimeObservation,
	network *aiPanoramaNetworkObservation,
	phase string,
) error {
	exists, err := aiPanoramaPhaseContainerExists(ctx, config, phase)
	if err != nil {
		return err
	}
	if exists {
		name, _ := aiPanoramaContainerName(config, phase)
		raw, err := executeAiPanoramaDocker(
			ctx, DockerExecutablePath, "container", "inspect", "--format",
			`{{.Name}}|{{.Config.Image}}|{{.HostConfig.NetworkMode}}|{{index .Config.Labels "propertyquarry.release-control.config-digest"}}|{{index .Config.Labels "propertyquarry.release-control.deployment-id"}}|{{index .Config.Labels "propertyquarry.release-control.operation"}}|{{index .Config.Labels "propertyquarry.release-control.phase"}}`,
			name,
		)
		if err != nil {
			return fmt.Errorf("ai-panorama-phase-container-inspect-failed")
		}
		parts := strings.Split(strings.TrimSpace(string(raw)), "|")
		zero(raw)
		expectedNetworkMode := "none"
		if network != nil {
			expectedNetworkMode = network.Name
		}
		if len(parts) != 7 || strings.TrimPrefix(parts[0], "/") != name ||
			parts[1] != config.WebImage || parts[2] != expectedNetworkMode ||
			parts[3] != config.Digest || parts[4] != config.DeploymentID ||
			parts[5] != aiPanoramaOperationForPhase(phase) || parts[6] != phase {
			return fmt.Errorf("ai-panorama-phase-container-binding-invalid")
		}
		if _, err := executeAiPanoramaDocker(
			ctx, DockerExecutablePath, "container", "rm", "--force", name,
		); err != nil {
			return fmt.Errorf("ai-panorama-phase-container-remove-failed")
		}
	}
	exists, err = aiPanoramaPhaseContainerExists(ctx, config, phase)
	if err != nil || exists {
		return fmt.Errorf("ai-panorama-phase-container-remove-unverified")
	}
	if network != nil {
		observation, err := observeAiPanoramaNetwork(ctx, config, runtime)
		if err != nil || observation.ID != network.ID || !observation.DBAttached {
			return fmt.Errorf("ai-panorama-phase-network-membership-unverified")
		}
	}
	return nil
}

func aiPanoramaJSONStrings(value any) ([]string, bool) {
	if value == nil {
		return nil, true
	}
	items, ok := value.([]any)
	if !ok {
		return nil, false
	}
	result := make([]string, 0, len(items))
	for _, item := range items {
		text, ok := exactString(item)
		if !ok {
			return nil, false
		}
		result = append(result, text)
	}
	return result, true
}

func aiPanoramaNormalizedCapabilities(value any) ([]string, bool) {
	values, ok := aiPanoramaJSONStrings(value)
	if !ok {
		return nil, false
	}
	for index := range values {
		values[index] = strings.TrimPrefix(values[index], "CAP_")
	}
	sort.Strings(values)
	return values, true
}

type aiPanoramaRecoveryMount struct {
	Type        string
	Source      string
	Destination string
	Name        string
	ReadWrite   bool
}

func aiPanoramaRecoveryMountContract(
	runtime *aiPanoramaRuntimeObservation,
	phase string,
) ([]aiPanoramaRecoveryMount, error) {
	if runtime == nil || runtime.PublicVolumeMountpoint == "" {
		return nil, fmt.Errorf("ai-panorama-recovery-mount-input-invalid")
	}
	bind := func(source string, readWrite bool) aiPanoramaRecoveryMount {
		return aiPanoramaRecoveryMount{
			Type: "bind", Source: source, Destination: source,
			ReadWrite: readWrite,
		}
	}
	volume := func(readWrite bool) aiPanoramaRecoveryMount {
		return aiPanoramaRecoveryMount{
			Type: "volume", Source: runtime.PublicVolumeMountpoint,
			Destination: aiPanoramaPublicMountTarget,
			Name:        aiPanoramaPublicVolumeName, ReadWrite: readWrite,
		}
	}
	switch phase {
	case "discover":
		return []aiPanoramaRecoveryMount{
			bind(aiPanoramaControlRoot, false),
			bind(aiPanoramaDatabaseSecretMount, false),
		}, nil
	case "preflight":
		return []aiPanoramaRecoveryMount{
			bind(aiPanoramaControlRoot, false),
			bind(aiPanoramaSealedArtifactRoot, false),
			bind(aiPanoramaVolumeProfilePath, false),
			bind(aiPanoramaComposePlanPath, false),
			bind(aiPanoramaTrustAssertionPath, false),
			bind(aiPanoramaPurposeKeyringPath, false),
			volume(false),
		}, nil
	case "apply":
		return []aiPanoramaRecoveryMount{
			bind(aiPanoramaControlRoot, true),
			bind(aiPanoramaSealedArtifactRoot, false),
			bind(aiPanoramaVolumeProfilePath, false),
			bind(aiPanoramaComposePlanPath, false),
			bind(aiPanoramaTrustAssertionPath, false),
			bind(aiPanoramaPurposeKeyringPath, false),
			bind(aiPanoramaDatabaseSecretMount, false),
			volume(true),
		}, nil
	case "closeout":
		return []aiPanoramaRecoveryMount{
			bind(aiPanoramaCloseoutRequestPath, false),
			volume(true),
		}, nil
	default:
		return nil, fmt.Errorf("ai-panorama-recovery-mount-phase-invalid")
	}
}

func validateAiPanoramaRecoveryMounts(
	value any,
	expected []aiPanoramaRecoveryMount,
) bool {
	items, ok := value.([]any)
	if !ok || len(items) != len(expected) {
		return false
	}
	byDestination := make(map[string]aiPanoramaRecoveryMount, len(expected))
	for _, mount := range expected {
		if _, duplicate := byDestination[mount.Destination]; duplicate {
			return false
		}
		byDestination[mount.Destination] = mount
	}
	seen := make(map[string]bool, len(expected))
	for _, item := range items {
		mount, ok := item.(map[string]any)
		if !ok {
			return false
		}
		destination, destinationOK := exactString(mount["Destination"])
		actualType, typeOK := exactString(mount["Type"])
		source, sourceOK := exactString(mount["Source"])
		readWrite, readWriteOK := mount["RW"].(bool)
		contract, expectedOK := byDestination[destination]
		if !destinationOK || !typeOK || !sourceOK || !readWriteOK ||
			!expectedOK || seen[destination] ||
			actualType != contract.Type || source != contract.Source ||
			readWrite != contract.ReadWrite {
			return false
		}
		if contract.Type == "bind" {
			propagation, _ := exactString(mount["Propagation"])
			if propagation != "rprivate" {
				return false
			}
		} else {
			name, nameOK := exactString(mount["Name"])
			driver, driverOK := exactString(mount["Driver"])
			if !nameOK || !driverOK || name != contract.Name ||
				driver != "local" {
				return false
			}
		}
		seen[destination] = true
	}
	return len(seen) == len(expected)
}

func validateAiPanoramaRecoveryContainerObject(
	value map[string]any,
	config *Config,
	runtime *aiPanoramaRuntimeObservation,
	phase string,
	expectedNetworkMode string,
	expectedName string,
) bool {
	if value == nil || config == nil || runtime == nil {
		return false
	}
	configValue, configOK := value["Config"].(map[string]any)
	host, hostOK := value["HostConfig"].(map[string]any)
	labels, labelsOK := configValue["Labels"].(map[string]any)
	logConfig, logOK := host["LogConfig"].(map[string]any)
	restart, restartOK := host["RestartPolicy"].(map[string]any)
	tmpfs, tmpfsOK := host["Tmpfs"].(map[string]any)
	args, argsOK := aiPanoramaJSONStrings(value["Args"])
	capAdd, capAddOK := aiPanoramaNormalizedCapabilities(host["CapAdd"])
	capDrop, capDropOK := aiPanoramaNormalizedCapabilities(host["CapDrop"])
	securityOptions, securityOK :=
		aiPanoramaJSONStrings(host["SecurityOpt"])
	pids, pidsOK := exactInt(host["PidsLimit"], 64, 64)
	memory, memoryOK := exactInt(host["Memory"], 536870912, 536870912)
	memorySwap, memorySwapOK :=
		exactInt(host["MemorySwap"], 536870912, 536870912)
	nanoCPUs, nanoCPUsOK :=
		exactInt(host["NanoCpus"], 1000000000, 1000000000)
	name, nameOK := exactString(value["Name"])
	containerID, idOK := exactString(value["Id"])
	imageID, imageIDOK := exactString(value["Image"])
	configuredImage, imageOK := exactString(configValue["Image"])
	user, userOK := exactString(configValue["User"])
	path, pathOK := exactString(value["Path"])
	networkMode, networkOK := exactString(host["NetworkMode"])
	pidMode, pidOK := host["PidMode"].(string)
	logType, logTypeOK := exactString(logConfig["Type"])
	restartName, restartNameOK := exactString(restart["Name"])
	expectedEntrypoint := aiPanoramaControllerEntrypoint
	expectedCapabilities := []string{}
	switch phase {
	case "discover":
		expectedEntrypoint = aiPanoramaDiscoveryEntrypoint
	case "preflight":
		expectedEntrypoint = aiPanoramaPreflightEntrypoint
	case "apply":
		expectedCapabilities = []string{"CHOWN", "DAC_OVERRIDE", "FOWNER"}
	case "closeout":
		expectedEntrypoint = aiPanoramaCloseoutEntrypoint
		expectedCapabilities = []string{"DAC_OVERRIDE"}
	default:
		return false
	}
	sort.Strings(expectedCapabilities)
	mounts, mountErr := aiPanoramaRecoveryMountContract(runtime, phase)
	if !configOK || !hostOK || !labelsOK || !logOK || !restartOK ||
		!tmpfsOK || !argsOK || !capAddOK || !capDropOK || !securityOK ||
		!pidsOK || pids != 64 || !memoryOK || memory != 536870912 ||
		!memorySwapOK || memorySwap != 536870912 ||
		!nanoCPUsOK || nanoCPUs != 1000000000 ||
		!nameOK || strings.TrimPrefix(name, "/") != expectedName ||
		!idOK || !runtimeContainerIDPattern.MatchString(containerID) ||
		!imageIDOK || imageID != runtime.ImageID ||
		!imageOK || configuredImage != config.WebImage ||
		!userOK || user != "0:0" ||
		!pathOK || path != aiPanoramaControllerPython ||
		!equalStrings(args, []string{"-I", "-B", expectedEntrypoint}) ||
		!networkOK || networkMode != expectedNetworkMode ||
		host["Privileged"] != false || host["ReadonlyRootfs"] != true ||
		!pidOK || pidMode != "" || host["AutoRemove"] != true ||
		!logTypeOK || logType != "none" ||
		!restartNameOK || restartName != "no" ||
		!equalStrings(capAdd, expectedCapabilities) ||
		!equalStrings(capDrop, []string{"ALL"}) ||
		!equalStrings(
			securityOptions, []string{"no-new-privileges:true"},
		) ||
		len(tmpfs) != 1 ||
		tmpfs["/tmp"] !=
			"rw,nosuid,nodev,noexec,size=67108864,mode=1777" ||
		mountErr != nil ||
		!validateAiPanoramaRecoveryMounts(value["Mounts"], mounts) {
		return false
	}
	requiredLabels := map[string]string{
		aiPanoramaNetworkLabel:                         "v1",
		"propertyquarry.release-control.config-digest": config.Digest,
		"propertyquarry.release-control.deployment-id": config.DeploymentID,
		"propertyquarry.release-control.operation":     aiPanoramaOperationForPhase(phase),
		"propertyquarry.release-control.phase":         phase,
	}
	for key, expected := range requiredLabels {
		if labels[key] != expected {
			return false
		}
	}
	for key := range labels {
		if strings.HasPrefix(key, "propertyquarry.release-control.") {
			if _, allowed := requiredLabels[key]; !allowed {
				return false
			}
		}
	}
	return true
}

func cleanupAiPanoramaRecoveryPhaseContainer(
	ctx context.Context,
	config *Config,
	runtime *aiPanoramaRuntimeObservation,
	phase string,
	expectedNetworkMode string,
) error {
	if ctx == nil || config == nil || runtime == nil ||
		(phase != "discover" && phase != "preflight" &&
			phase != "apply" && phase != "closeout") {
		return fmt.Errorf("ai-panorama-recovery-container-input-invalid")
	}
	exists, err := aiPanoramaPhaseContainerExists(ctx, config, phase)
	if err != nil || !exists {
		return err
	}
	name, err := aiPanoramaContainerName(config, phase)
	if err != nil {
		return err
	}
	raw, err := executeAiPanoramaDocker(
		ctx, DockerExecutablePath, "container", "inspect", "--format",
		"{{json .}}",
		name,
	)
	if err != nil {
		return fmt.Errorf("ai-panorama-recovery-container-inspect-failed")
	}
	value, decodeErr := aiPanoramaDockerObject(raw)
	zero(raw)
	if decodeErr != nil || !validateAiPanoramaRecoveryContainerObject(
		value, config, runtime, phase, expectedNetworkMode, name,
	) {
		return fmt.Errorf("ai-panorama-recovery-container-binding-invalid")
	}
	if _, err := executeAiPanoramaDocker(
		ctx, DockerExecutablePath, "container", "rm", "--force", name,
	); err != nil {
		return fmt.Errorf("ai-panorama-recovery-container-remove-failed")
	}
	exists, err = aiPanoramaPhaseContainerExists(ctx, config, phase)
	if err != nil || exists {
		return fmt.Errorf("ai-panorama-recovery-container-remove-unverified")
	}
	return nil
}

func runAiPanoramaContainerRaw(
	parent context.Context,
	config *Config,
	runtime *aiPanoramaRuntimeObservation,
	sealed *aiPanoramaSealedArtifactObservation,
	network *aiPanoramaNetworkObservation,
	phase string,
	databaseSecretSource string,
) ([]byte, error) {
	timeout := time.Duration(0)
	switch phase {
	case "bootstrap":
		timeout = aiPanoramaBootstrapPhaseTimeout
	case "discover":
		timeout = aiPanoramaDiscoveryPhaseTimeout
	case "preflight":
		timeout = aiPanoramaPreflightPhaseTimeout
	case "apply":
		timeout = aiPanoramaApplyPhaseTimeout
	case "closeout":
		timeout = aiPanoramaCloseoutPhaseTimeout
	default:
		return nil, fmt.Errorf("ai-panorama-phase-timeout-invalid")
	}
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()
	exists, err := aiPanoramaPhaseContainerExists(ctx, config, phase)
	if err != nil || exists {
		return nil, fmt.Errorf("ai-panorama-phase-container-preexisting")
	}
	arguments, err := aiPanoramaContainerArguments(
		config, runtime, sealed, network, phase, databaseSecretSource,
	)
	if err != nil {
		return nil, err
	}
	raw, commandErr := executeAiPanoramaDocker(ctx, DockerExecutablePath, arguments...)
	cleanupContext, cleanupCancel := context.WithTimeout(
		context.WithoutCancel(parent), aiPanoramaCleanupTimeout,
	)
	defer cleanupCancel()
	cleanupErr := cleanupAiPanoramaPhaseContainer(cleanupContext, config, runtime, network, phase)
	if commandErr != nil || cleanupErr != nil {
		zero(raw)
		if cleanupErr != nil {
			return nil, fmt.Errorf("ai-panorama-phase-container-cleanup-unverified")
		}
		return nil, commandErr
	}
	return raw, nil
}

func aiPanoramaSealedEvidenceDigest(observation *aiPanoramaSealedArtifactObservation) (string, error) {
	if observation == nil {
		return "", fmt.Errorf("ai-panorama-sealed-evidence-missing")
	}
	raw, err := canonicalJSON(aiPanoramaSealedArtifactValue(observation))
	if err != nil {
		return "", fmt.Errorf("ai-panorama-sealed-evidence-invalid")
	}
	defer zero(raw)
	return digest(raw), nil
}

func aiPanoramaReleasePrerequisite(journal *Journal, config *Config, request *workflowRequest, identity *Identity) (string, error) {
	if journal == nil || config == nil || request == nil || identity == nil {
		return "", fmt.Errorf("ai-panorama-release-prerequisite-input-invalid")
	}
	for index := len(journal.events) - 1; index >= 0; index-- {
		event := &journal.events[index]
		if event.EventType != "run-succeeded" {
			continue
		}
		if event.RunID != identity.RunID || event.RunAttempt != identity.RunAttempt ||
			event.Payload["config_digest"] != config.Digest ||
			event.Payload["plan_digest"] != config.PlanDigest ||
			event.Payload["runtime_sha"] != config.RuntimeSHA ||
			event.Payload["workflow_sha"] != config.WorkflowSHA ||
			event.Payload["deployment_id"] != config.DeploymentID ||
			event.Payload["web_image"] != config.WebImage ||
			event.Payload["production_ready"] != true ||
			event.Payload["runtime_deploy_verified"] != true ||
			event.Payload["runner_label"] != identity.RunnerLabel ||
			event.Payload["runner_dispatch_ticket_sha256"] != request.RunnerTicketDigest ||
			event.Payload["runner_launch_ticket_sha256"] != request.RunnerLaunchTicketDigest {
			return "", fmt.Errorf("ai-panorama-release-prerequisite-mismatch")
		}
		return event.ReceiptDigest, nil
	}
	return "", fmt.Errorf("ai-panorama-release-prerequisite-missing")
}

func aiPanoramaBaseFields(config *Config, request *workflowRequest, identity *Identity, releaseReceipt string, runtime *aiPanoramaRuntimeObservation, sealed *aiPanoramaSealedArtifactObservation, sealedDigest string, before *aiPanoramaRelatedManifest) map[string]any {
	fields := authorityFields(config, request, identity)
	fields["ai_panorama_slug"] = aiPanoramaPraterSlug
	fields["ai_panorama_control_url"] = aiPanoramaPraterControlURL
	fields["ai_panorama_public_mount_target"] = aiPanoramaPublicMountTarget
	fields["ai_panorama_public_volume_name"] = aiPanoramaPublicVolumeName
	fields["ai_panorama_runtime_observation"] = aiPanoramaRuntimeObservationValue(runtime)
	fields["ai_panorama_sealed_artifact"] = aiPanoramaSealedArtifactValue(sealed)
	fields["ai_panorama_sealed_artifact_evidence_sha256"] = sealedDigest
	fields["ai_panorama_before_manifest"] = aiPanoramaManifestValue(before)
	fields["ai_panorama_before_manifest_sha256"] = before.Digest
	fields["release_run_receipt_digest"] = releaseReceipt
	fields["ready"] = false
	fields["production_ready"] = false
	fields["release_effects_authorized"] = true
	fields["release_effects_performed"] = false
	fields["ai_panorama_install_verified"] = false
	fields["rollback_performed"] = false
	fields["recovery"] = false
	return fields
}

func aiPanoramaPublishedManifestValid(manifest *aiPanoramaRelatedManifest) bool {
	if manifest == nil || len(manifest.Entries) < 2 ||
		manifest.Entries[0].Path != aiPanoramaPraterSlug ||
		manifest.Entries[0].Kind != "directory" {
		return false
	}
	privateReceiptFound := false
	for _, entry := range manifest.Entries {
		if entry.Path != aiPanoramaPraterSlug &&
			!strings.HasPrefix(entry.Path, aiPanoramaPraterSlug+"/") {
			return false
		}
		if entry.Kind == "file" && entry.Path == aiPanoramaPraterSlug+"/tour.private.json" {
			if entry.Mode != 0o600 || privateReceiptFound {
				return false
			}
			privateReceiptFound = true
		}
	}
	return privateReceiptFound
}

func aiPanoramaRecordRecoveryRequired(journal *Journal, base map[string]any, disposition string) error {
	fields := cloneFields(base)
	fields["disposition"] = disposition
	fields["production_ready"] = false
	fields["rollback_performed"] = false
	fields["recovery_required"] = true
	fields["observed_at"] = json.Number(strconv.FormatInt(authorityNow().UTC().Unix(), 10))
	wire, appendErr := journal.Append(aiPanoramaInstallRecoveryRequiredEvent, fields)
	zero(wire)
	if appendErr != nil {
		return appendErr
	}
	return fmt.Errorf("ai-panorama-install-recovery-required")
}
