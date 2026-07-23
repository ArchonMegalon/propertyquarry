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
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

const (
	tourV4ManifestSchema                       = "propertyquarry.generated-reconstruction-publication-manifest.v4"
	tourV4InspectionSchema                     = "propertyquarry.generated-reconstruction-publication-inspection.v4"
	tourV4PreparedSchema                       = "propertyquarry.generated-reconstruction-publication-prepared.v4"
	tourV4TerminalSchema                       = "propertyquarry.generated-reconstruction-publication-terminal.v4"
	tourV4RollbackSchema                       = "propertyquarry.generated-reconstruction-publication-rollback.v4"
	tourV4InstalledBinary                      = "/usr/libexec/propertyquarry-release-control/propertyquarry-release-single-host-v2"
	tourV4LiveVolumeRoot                       = "/var/lib/docker/volumes/property_propertyquarry_public_tours/_data"
	tourV4ReceiptRoot                          = "/var/lib/propertyquarry-release-single-host-v2/tour-publication-receipts"
	tourV4ControlRelpath                       = ".propertyquarry-publisher-v4"
	tourV4AbsentSentinel                       = "absent"
	tourV4MaximumFiles                         = 128
	tourV4MaximumDirectories                   = 64
	tourV4MaximumTreeBytes                     = 64 * 1024 * 1024
	tourV4MaximumFileBytes                     = 32 * 1024 * 1024
	tourV4RenameExchange                       = 2
	tourV4DetachedProfile                      = "single-host-tour-publication-v4"
	tourV4DetachedMaterializationSchema        = "propertyquarry.release-control.single-host-tour-publication-materialization.v4"
	tourV4DetachedMaterializationDomain        = "propertyquarry.release-control.single-host-tour-publication-materialization-signature.v4\x00"
	tourV4DetachedBootstrapSchema              = "propertyquarry.release-control.single-host-production-authority-bootstrap.v2"
	tourV4DetachedBootstrapDomain              = "propertyquarry.release-control.single-host-production-authority-bootstrap.v2\x00"
	tourV4DetachedCanonicalAuthorityRoot       = "/docker/property/state/runtime/propertyquarry-release-authority-v2.private/authority-static-canonical"
	tourV4DetachedCanonicalPrivateDigest       = "sha256:8b9106db85e8ce423d454bb14c863b6c0d481b061eaae0bd4b584d7071cbc2e1"
	tourV4DetachedBundlePath                   = "/tmp/property-f7-tour-final-v4.HUQw8lU4/ab-1-8-modern-and-fully-furnited-loft-apartment-top-moderne-und-voll-mblierte-loft-wohnung-nas-layout-first-d07edad7af3fc379574d"
	tourV4DetachedTTLSeconds             int64 = 3600
)

var (
	tourV4SlugPattern        = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$`)
	tourV4TransactionPattern = regexp.MustCompile(`^[0-9a-f]{32}$`)
	tourV4SHA256Pattern      = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	tourV4ForbiddenJSONKeys  = map[string]struct{}{
		"candidate_ref": {}, "coordinates": {}, "external_id": {},
		"lat": {}, "latitude": {}, "listing_url": {}, "lng": {},
		"lon": {}, "longitude": {}, "matterport_url": {},
		"principal_id": {}, "private_exact_location": {},
		"property_url": {}, "recipient_email": {}, "search_run_id": {},
		"source_virtual_tour_url": {}, "three_d_vista_url": {},
	}
	tourV4Now                       = time.Now
	tourV4BeforeControlBindingCheck func()
	tourV4AfterExchangeHook         func() error
)

var tourV4DetachedOperations = []string{
	"tour-v4-authority-info",
	"tour-inspect-v4",
	"tour-publish-v4",
	"tour-recover-v4",
	"tour-rollback-v4",
}

type tourV4PermitFile struct {
	Path   string
	Mode   uint32
	Size   int64
	SHA256 string
	Public bool
}

type tourV4Permit struct {
	Slug                      string
	ReconstructionKind        string
	Provider                  string
	ViewerVersion             string
	Disclosure                string
	ArtifactTreeSHA256        string
	PublicTreeSHA256          string
	BrowserReceiptSHA256      string
	BrowserEvidenceTreeSHA256 string
	QualityReceiptSHA256      string
	WalkthroughSHA256         string
	TourManifestSHA256        string
	ReconstructionSHA256      string
	RenderCommitSHA256        string
	Files                     []tourV4PermitFile
}

type tourV4FileSnapshot struct {
	Path    string
	Mode    uint32
	Size    int64
	SHA256  string
	Public  bool
	Device  uint64
	Inode   uint64
	MtimeNS int64
	CtimeNS int64
	Content []byte
}

type tourV4DirectorySnapshot struct {
	Path string
	Mode uint32
}

type tourV4TreeSnapshot struct {
	Root        string
	Device      uint64
	Inode       uint64
	Mode        uint32
	UID         uint32
	GID         uint32
	MtimeNS     int64
	CtimeNS     int64
	Directories []tourV4DirectorySnapshot
	Files       []tourV4FileSnapshot
	TreeSHA256  string
	TotalBytes  int64
}

func (snapshot *tourV4TreeSnapshot) release() {
	if snapshot == nil {
		return
	}
	for index := range snapshot.Files {
		zero(snapshot.Files[index].Content)
	}
	*snapshot = tourV4TreeSnapshot{}
}

var tourV4AuthorizedPermits = []tourV4Permit{
	{
		Slug:                      "ab-1-8-modern-and-fully-furnited-loft-apartment-top-moderne-und-voll-mblierte-loft-wohnung-nas-layout-first-d07edad7af3fc379574d",
		ReconstructionKind:        "layout_preview",
		Provider:                  "propertyquarry_generated_reconstruction",
		ViewerVersion:             "propertyquarry_3d_tour_viewer_v3",
		Disclosure:                "Planning preview built from the floor plan and listing photos. Use it as a layout aid, not as a captured tour.",
		ArtifactTreeSHA256:        "sha256:862b282b297d6d16df8b715770934101a8afed77f50829f35890270ba9e364d7",
		PublicTreeSHA256:          "sha256:5cc6098565d79549089db385e133671c6de8748b17a171c699a997004412efb1",
		BrowserReceiptSHA256:      "sha256:800b2ba29a7c33ec64651db26ef23a1e0756d0223286eb43d742fc23c6bb34f8",
		BrowserEvidenceTreeSHA256: "sha256:838ff0b8e236dbd5e5695c6bc20a862182d42075bba4202cd1a63083bfcb0d08",
		QualityReceiptSHA256:      "sha256:0fc7dc5b0a63a49bfd3d63a9c748a0a04d7397a46d5f81d3b517d85ca753b721",
		WalkthroughSHA256:         "sha256:16197ec466ed41eab0ed05034e8e0132a1f15cd2387263d792b7b4492c6d7aed",
		TourManifestSHA256:        "sha256:794db94b7bcccd9b33e5dcbed4c1b0bbe5ed7d221fc0b07733b95a3f61fb9c47",
		ReconstructionSHA256:      "sha256:6d8d967e3447e783c1e9cb9e81bcb6a88b2d08cd85f0104edad2172b522c1e1b",
		RenderCommitSHA256:        "sha256:d45597b5fb4f789e02507dec5cca75c0323c2f58ad4b9a30c23b55f62074bfb3",
		Files: []tourV4PermitFile{
			{Path: ".propertyquarry-render-commit.json", Mode: 0o600, Size: 339, SHA256: "sha256:d45597b5fb4f789e02507dec5cca75c0323c2f58ad4b9a30c23b55f62074bfb3", Public: false},
			{Path: "diorama-preview.png", Mode: 0o644, Size: 799183, SHA256: "sha256:c7e9de76097173dfea1a3fd1f79bdc58cc9cc5cc0efe662dc3479b857faf9bfc", Public: true},
			{Path: "generated-reconstruction/generated-walkthrough.mp4", Mode: 0o644, Size: 1984340, SHA256: "sha256:16197ec466ed41eab0ed05034e8e0132a1f15cd2387263d792b7b4492c6d7aed", Public: true},
			{Path: "generated-reconstruction/generated-walkthrough.quality.json", Mode: 0o644, Size: 3096, SHA256: "sha256:0fc7dc5b0a63a49bfd3d63a9c748a0a04d7397a46d5f81d3b517d85ca753b721", Public: true},
			{Path: "generated-reconstruction/model.glb", Mode: 0o644, Size: 6408, SHA256: "sha256:f65215c8271f2c025f04a82a89061523bbcce21f5e40ac650a7b97bcaec08e5d", Public: true},
			{Path: "generated-reconstruction/model.mtl", Mode: 0o644, Size: 157, SHA256: "sha256:7ba0b529389c4d1074b0e5d0f2ad342fa112174114f90a24d38847b00060bcfc", Public: true},
			{Path: "generated-reconstruction/model.obj", Mode: 0o644, Size: 2840, SHA256: "sha256:73723d7e5e1255597bc6fd679aba796275de75e77012879657a765adf8681735", Public: true},
			{Path: "generated-reconstruction/photo-01.webp", Mode: 0o644, Size: 49570, SHA256: "sha256:34af1f092b95327290846d582180c6ebf1ca54f75490cde2db3b20485e62d2cc", Public: true},
			{Path: "generated-reconstruction/photo-02.webp", Mode: 0o644, Size: 40412, SHA256: "sha256:12d98f4a7cd3a93bbe77cf6a9747324195a03130394e9672f384e3c985964c5a", Public: true},
			{Path: "generated-reconstruction/photo-03.webp", Mode: 0o644, Size: 71186, SHA256: "sha256:33172eb3f9b557a3730b241ccc5aa64250e6a2169cbd7227bccd25b31fd259d4", Public: true},
			{Path: "generated-reconstruction/photo-04.webp", Mode: 0o644, Size: 49860, SHA256: "sha256:19d486904a4bf7d5d86bf597f18c25e16cb553625ce5b2d3648858df5e53e737", Public: true},
			{Path: "generated-reconstruction/photo-05.webp", Mode: 0o644, Size: 62846, SHA256: "sha256:52ca12a2119ba47295897558d51554581eccd20460ce612e939d3649daf33c2e", Public: true},
			{Path: "generated-reconstruction/photo-06.webp", Mode: 0o644, Size: 74916, SHA256: "sha256:e6c7246e32c77920aea6dd07c3922896340cbe1f6a09bc9d8dc4f48a6d54dd67", Public: true},
			{Path: "generated-reconstruction/photo-07.webp", Mode: 0o644, Size: 128502, SHA256: "sha256:791e87217f02f7cea13fe8090f1a86febf6dc289fc81a15774e1e81a2820bcfd", Public: true},
			{Path: "generated-reconstruction/photo-08.webp", Mode: 0o644, Size: 130076, SHA256: "sha256:3757bc5ca1a9d27bc01375c0ffc528d0242302da1060e1273909dee31ed8402e", Public: true},
			{Path: "generated-reconstruction/reconstruction.json", Mode: 0o644, Size: 20142, SHA256: "sha256:6d8d967e3447e783c1e9cb9e81bcb6a88b2d08cd85f0104edad2172b522c1e1b", Public: true},
			{Path: "generated-reconstruction/source-floorplan.webp", Mode: 0o644, Size: 65230, SHA256: "sha256:09b3a1b7c07a8985ed6e45f8b3248ee263daf836181e2447385384f9fc05304f", Public: true},
			{Path: "generated-reconstruction/vendor/examples/jsm/controls/OrbitControls.js", Mode: 0o644, Size: 33357, SHA256: "sha256:b15a310c930ed4ba3e26cae34931f145a9d3fb82741339563dcb623d1eedd18b", Public: true},
			{Path: "generated-reconstruction/vendor/three.module.js", Mode: 0o644, Size: 1296547, SHA256: "sha256:2fdbd590b5a285d9a9b1aa39dcba2d41fd8b7749361a84fcef1fc422696996ed", Public: true},
			{Path: "generated-reconstruction/viewer.html", Mode: 0o644, Size: 87416, SHA256: "sha256:81d1e33205835cca27d69d389a8b848803503fdad3bece53c532870dc7a472e5", Public: true},
			{Path: "telegram-preview.png", Mode: 0o644, Size: 761300, SHA256: "sha256:8e298b1c9643bd30252c986f2c9ca68ad16b20f84b695b34fdec6896ed093d67", Public: true},
			{Path: "tour.json", Mode: 0o644, Size: 11440, SHA256: "sha256:794db94b7bcccd9b33e5dcbed4c1b0bbe5ed7d221fc0b07733b95a3f61fb9c47", Public: true},
			{Path: "tour.private.json", Mode: 0o600, Size: 2875, SHA256: "sha256:12a9fd7fae6f1bafd4b1b78f7d18242840adf3aba29064811ce3593337c4a7c9", Public: false},
		},
	},
}

func tourV4PermitManifest(permit *tourV4Permit) (map[string]any, string, error) {
	if permit == nil || !tourV4SlugPattern.MatchString(permit.Slug) ||
		permit.ReconstructionKind != "layout_preview" ||
		permit.Provider != "propertyquarry_generated_reconstruction" ||
		permit.ViewerVersion != "propertyquarry_3d_tour_viewer_v3" ||
		len(permit.Files) < 1 || len(permit.Files) > tourV4MaximumFiles {
		return nil, "", fmt.Errorf("tour-v4-permit-invalid")
	}
	files := make([]any, 0, len(permit.Files))
	previous := ""
	var total int64
	for _, file := range permit.Files {
		if !tourV4SafeRelativePath(file.Path) || file.Path <= previous ||
			(file.Mode != 0o600 && file.Mode != 0o644) ||
			file.Size < 1 || file.Size > tourV4MaximumFileBytes ||
			!tourV4SHA256Pattern.MatchString(file.SHA256) ||
			(file.Public && file.Mode != 0o644) ||
			(!file.Public && file.Mode != 0o600) {
			return nil, "", fmt.Errorf("tour-v4-permit-file-invalid:%s", file.Path)
		}
		previous = file.Path
		total += file.Size
		files = append(files, map[string]any{
			"mode": json.Number(strconv.FormatUint(uint64(file.Mode), 10)),
			"path": file.Path, "public": file.Public,
			"sha256":     file.SHA256,
			"size_bytes": json.Number(strconv.FormatInt(file.Size, 10)),
		})
	}
	if total > tourV4MaximumTreeBytes {
		return nil, "", fmt.Errorf("tour-v4-permit-size-invalid")
	}
	for _, value := range []string{
		permit.ArtifactTreeSHA256, permit.PublicTreeSHA256,
		permit.BrowserReceiptSHA256, permit.BrowserEvidenceTreeSHA256,
		permit.QualityReceiptSHA256, permit.WalkthroughSHA256,
		permit.TourManifestSHA256, permit.ReconstructionSHA256,
		permit.RenderCommitSHA256,
	} {
		if !tourV4SHA256Pattern.MatchString(value) {
			return nil, "", fmt.Errorf("tour-v4-permit-digest-invalid")
		}
	}
	manifest := map[string]any{
		"artifact_tree_sha256": permit.ArtifactTreeSHA256,
		"audit_bindings": map[string]any{
			"browser_evidence_tree_sha256": permit.BrowserEvidenceTreeSHA256,
			"browser_receipt_sha256":       permit.BrowserReceiptSHA256,
			"quality_receipt_sha256":       permit.QualityReceiptSHA256,
			"render_commit_sha256":         permit.RenderCommitSHA256,
			"tour_manifest_sha256":         permit.TourManifestSHA256,
			"walkthrough_sha256":           permit.WalkthroughSHA256,
		},
		"canonical_live_root": tourV4LiveVolumeRoot,
		"disclosure":          permit.Disclosure,
		"files":               files,
		"privacy_contract": map[string]any{
			"private_paths": []any{".propertyquarry-render-commit.json", "tour.private.json"},
			"public_geographic_coordinates_forbidden":  true,
			"private_values_in_public_files_forbidden": true,
		},
		"provider":                       permit.Provider,
		"public_origin":                  PublicOrigin,
		"public_tree_sha256":             permit.PublicTreeSHA256,
		"reconstruction_kind":            permit.ReconstructionKind,
		"reconstruction_manifest_sha256": permit.ReconstructionSHA256,
		"schema":                         tourV4ManifestSchema,
		"slug":                           permit.Slug,
		"version":                        json.Number("4"),
		"viewer_version":                 permit.ViewerVersion,
	}
	raw, err := canonicalJSON(manifest)
	if err != nil {
		return nil, "", fmt.Errorf("tour-v4-permit-manifest-invalid")
	}
	return manifest, digest(raw), nil
}

func tourV4PermitByManifestDigest(expected string) (*tourV4Permit, string, error) {
	if !tourV4SHA256Pattern.MatchString(expected) {
		return nil, "", fmt.Errorf("tour-v4-manifest-digest-invalid")
	}
	for index := range tourV4AuthorizedPermits {
		_, observed, err := tourV4PermitManifest(&tourV4AuthorizedPermits[index])
		if err != nil {
			return nil, "", err
		}
		if observed == expected {
			return &tourV4AuthorizedPermits[index], observed, nil
		}
	}
	return nil, "", fmt.Errorf("tour-v4-manifest-not-authorized")
}

func tourV4SafeRelativePath(value string) bool {
	if value == "" || filepath.IsAbs(value) || strings.Contains(value, "\\") ||
		strings.ContainsRune(value, '\x00') || !utf8Printable(value) {
		return false
	}
	clean := filepath.Clean(value)
	return clean == value && clean != "." && clean != ".." &&
		!strings.HasPrefix(clean, "../") && len([]byte(value)) <= 512
}

func utf8Printable(value string) bool {
	for _, character := range value {
		if character < 0x20 || character == 0x7f {
			return false
		}
	}
	return true
}

func tourV4OpenDirectoryAbsolute(path string) (*os.File, error) {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || path == "/" {
		return nil, fmt.Errorf("tour-v4-directory-path-invalid")
	}
	rootFD, err := syscall.Open("/", syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, fmt.Errorf("tour-v4-directory-root-unavailable")
	}
	current := os.NewFile(uintptr(rootFD), "/")
	for _, component := range strings.Split(strings.TrimPrefix(path, "/"), "/") {
		if component == "" || component == "." || component == ".." || len(component) > 255 {
			current.Close()
			return nil, fmt.Errorf("tour-v4-directory-component-invalid")
		}
		childFD, openErr := syscall.Openat(
			int(current.Fd()),
			component,
			syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW,
			0,
		)
		if openErr != nil {
			current.Close()
			return nil, fmt.Errorf("tour-v4-directory-unavailable")
		}
		child := os.NewFile(uintptr(childFD), component)
		info, statErr := child.Stat()
		current.Close()
		if statErr != nil || !info.IsDir() {
			child.Close()
			return nil, fmt.Errorf("tour-v4-directory-invalid")
		}
		current = child
	}
	return current, nil
}

func tourV4OpenDirectoryAt(parent *os.File, name string) (*os.File, error) {
	if parent == nil || !tourV4SafeEntryName(name) {
		return nil, fmt.Errorf("tour-v4-directory-entry-invalid")
	}
	fd, err := syscall.Openat(
		int(parent.Fd()),
		name,
		syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW,
		0,
	)
	if err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(fd), name)
	info, err := file.Stat()
	if err != nil || !info.IsDir() {
		file.Close()
		return nil, fmt.Errorf("tour-v4-directory-entry-invalid")
	}
	return file, nil
}

func tourV4SafeEntryName(name string) bool {
	return name != "" && name != "." && name != ".." &&
		!strings.Contains(name, "/") && !strings.Contains(name, "\\") &&
		!strings.ContainsRune(name, '\x00') && utf8Printable(name) &&
		len([]byte(name)) <= 255
}

func tourV4StatMetadata(info os.FileInfo) (*syscall.Stat_t, error) {
	metadata, ok := infoSys(info)
	if !ok || metadata == nil {
		return nil, fmt.Errorf("tour-v4-stat-invalid")
	}
	return metadata, nil
}

func tourV4StatTimes(metadata *syscall.Stat_t) (int64, int64) {
	if metadata == nil {
		return 0, 0
	}
	return metadata.Mtim.Sec*1_000_000_000 + metadata.Mtim.Nsec,
		metadata.Ctim.Sec*1_000_000_000 + metadata.Ctim.Nsec
}

func tourV4SameFingerprint(left, right os.FileInfo) bool {
	if left == nil || right == nil || left.Mode() != right.Mode() ||
		left.Size() != right.Size() {
		return false
	}
	leftMeta, leftOK := infoSys(left)
	rightMeta, rightOK := infoSys(right)
	if !leftOK || !rightOK {
		return false
	}
	leftMtime, leftCtime := tourV4StatTimes(leftMeta)
	rightMtime, rightCtime := tourV4StatTimes(rightMeta)
	return leftMeta.Dev == rightMeta.Dev && leftMeta.Ino == rightMeta.Ino &&
		leftMeta.Nlink == rightMeta.Nlink &&
		leftMtime == rightMtime && leftCtime == rightCtime
}

func tourV4ReadRegularAt(parent *os.File, name, relpath string, maximum int64) (tourV4FileSnapshot, error) {
	if parent == nil || !tourV4SafeEntryName(name) || !tourV4SafeRelativePath(relpath) ||
		maximum < 1 || maximum > tourV4MaximumFileBytes {
		return tourV4FileSnapshot{}, fmt.Errorf("tour-v4-file-input-invalid")
	}
	fd, err := syscall.Openat(
		int(parent.Fd()),
		name,
		syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW,
		0,
	)
	if err != nil {
		return tourV4FileSnapshot{}, fmt.Errorf("tour-v4-file-unavailable")
	}
	file := os.NewFile(uintptr(fd), name)
	defer file.Close()
	before, err := file.Stat()
	metadata, metaErr := tourV4StatMetadata(before)
	if err != nil || metaErr != nil || !before.Mode().IsRegular() ||
		metadata.Nlink != 1 || before.Size() < 1 || before.Size() > maximum {
		return tourV4FileSnapshot{}, fmt.Errorf("tour-v4-file-metadata-invalid")
	}
	content := make([]byte, before.Size())
	if _, err := io.ReadFull(file, content); err != nil {
		zero(content)
		return tourV4FileSnapshot{}, fmt.Errorf("tour-v4-file-read-failed")
	}
	extra := make([]byte, 1)
	count, readErr := file.Read(extra)
	zero(extra)
	if count != 0 || (readErr != nil && readErr != io.EOF) {
		zero(content)
		return tourV4FileSnapshot{}, fmt.Errorf("tour-v4-file-size-changed")
	}
	after, err := file.Stat()
	if err != nil || !tourV4SameFingerprint(before, after) {
		zero(content)
		return tourV4FileSnapshot{}, fmt.Errorf("tour-v4-file-race-detected")
	}
	sum := sha256.Sum256(content)
	mtime, ctime := tourV4StatTimes(metadata)
	return tourV4FileSnapshot{
		Path: relpath, Mode: uint32(before.Mode().Perm()), Size: before.Size(),
		SHA256: "sha256:" + hex.EncodeToString(sum[:]),
		Device: uint64(metadata.Dev), Inode: uint64(metadata.Ino),
		MtimeNS: mtime, CtimeNS: ctime, Content: content,
	}, nil
}

func tourV4SnapshotTree(path string, permit *tourV4Permit, publicOnly bool) (*tourV4TreeSnapshot, error) {
	root, err := tourV4OpenDirectoryAbsolute(path)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	return tourV4SnapshotTreeFromOpenRoot(root, path, permit, publicOnly, true)
}

func tourV4SnapshotTreeAt(parent *os.File, name, displayPath string, permit *tourV4Permit, publicOnly bool) (*tourV4TreeSnapshot, error) {
	root, err := tourV4OpenDirectoryAt(parent, name)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	return tourV4SnapshotTreeFromOpenRoot(root, displayPath, permit, publicOnly, false)
}

func tourV4SnapshotTreeFromOpenRoot(root *os.File, displayPath string, permit *tourV4Permit, publicOnly, retainContent bool) (*tourV4TreeSnapshot, error) {
	if root == nil || !filepath.IsAbs(displayPath) {
		return nil, fmt.Errorf("tour-v4-tree-input-invalid")
	}
	rootBefore, err := root.Stat()
	rootMetadata, metaErr := tourV4StatMetadata(rootBefore)
	if err != nil || metaErr != nil || !rootBefore.IsDir() ||
		rootBefore.Mode().Perm() != 0o755 {
		return nil, fmt.Errorf("tour-v4-tree-root-invalid")
	}
	rootMtime, rootCtime := tourV4StatTimes(rootMetadata)
	snapshot := &tourV4TreeSnapshot{
		Root: displayPath, Device: uint64(rootMetadata.Dev),
		Inode: uint64(rootMetadata.Ino), Mode: uint32(rootBefore.Mode().Perm()),
		UID: rootMetadata.Uid, GID: rootMetadata.Gid,
		MtimeNS: rootMtime, CtimeNS: rootCtime,
		Directories: []tourV4DirectorySnapshot{{Path: ".", Mode: 0o755}},
	}
	expected := map[string]tourV4PermitFile{}
	if permit != nil {
		for _, file := range permit.Files {
			if !publicOnly || file.Public {
				expected[file.Path] = file
			}
		}
	}
	var walk func(*os.File, string) error
	walk = func(directory *os.File, prefix string) error {
		duplicateFD, err := syscall.Dup(int(directory.Fd()))
		if err != nil {
			return fmt.Errorf("tour-v4-directory-dup-failed")
		}
		reader := os.NewFile(uintptr(duplicateFD), "tour-v4-directory-reader")
		entries, err := reader.ReadDir(-1)
		reader.Close()
		if err != nil {
			return fmt.Errorf("tour-v4-directory-read-failed")
		}
		sort.Slice(entries, func(left, right int) bool { return entries[left].Name() < entries[right].Name() })
		for _, entry := range entries {
			name := entry.Name()
			if !tourV4SafeEntryName(name) {
				return fmt.Errorf("tour-v4-entry-name-invalid")
			}
			relpath := name
			if prefix != "" {
				relpath = prefix + "/" + name
			}
			if !tourV4SafeRelativePath(relpath) {
				return fmt.Errorf("tour-v4-entry-path-invalid")
			}
			child, openErr := tourV4OpenDirectoryAt(directory, name)
			if openErr == nil {
				before, err := child.Stat()
				if err != nil || before.Mode().Perm() != 0o755 {
					child.Close()
					return fmt.Errorf("tour-v4-directory-mode-invalid")
				}
				snapshot.Directories = append(snapshot.Directories, tourV4DirectorySnapshot{Path: relpath, Mode: 0o755})
				if len(snapshot.Directories) > tourV4MaximumDirectories {
					child.Close()
					return fmt.Errorf("tour-v4-tree-too-large")
				}
				if err := walk(child, relpath); err != nil {
					child.Close()
					return err
				}
				after, err := child.Stat()
				child.Close()
				if err != nil || !tourV4SameFingerprint(before, after) {
					return fmt.Errorf("tour-v4-directory-race-detected")
				}
				continue
			}
			file, err := tourV4ReadRegularAt(directory, name, relpath, tourV4MaximumFileBytes)
			if err != nil {
				return err
			}
			if permitFile, ok := expected[relpath]; ok {
				file.Public = permitFile.Public
				if file.Mode != permitFile.Mode || file.Size != permitFile.Size ||
					file.SHA256 != permitFile.SHA256 {
					zero(file.Content)
					return fmt.Errorf("tour-v4-file-contract-mismatch")
				}
			} else if permit != nil {
				zero(file.Content)
				return fmt.Errorf("tour-v4-extra-file")
			} else if file.Mode != 0o600 && file.Mode != 0o644 {
				// Uncontracted snapshots are only used to hash pre-existing
				// live/retained transaction trees for CAS and rollback. Legacy
				// trees can contain private control files at 0600. Contracted
				// source and published-tree snapshots still enforce every
				// permit mode, and staging copies public files as 0644 only.
				zero(file.Content)
				return fmt.Errorf("tour-v4-live-file-mode-invalid")
			}
			snapshot.Files = append(snapshot.Files, file)
			snapshot.TotalBytes += file.Size
			if len(snapshot.Files) > tourV4MaximumFiles ||
				snapshot.TotalBytes > tourV4MaximumTreeBytes {
				return fmt.Errorf("tour-v4-tree-too-large")
			}
		}
		return nil
	}
	if err := walk(root, ""); err != nil {
		snapshot.release()
		return nil, err
	}
	rootAfter, err := root.Stat()
	if err != nil || !tourV4SameFingerprint(rootBefore, rootAfter) {
		snapshot.release()
		return nil, fmt.Errorf("tour-v4-root-race-detected")
	}
	if permit != nil && len(snapshot.Files) != len(expected) {
		snapshot.release()
		return nil, fmt.Errorf("tour-v4-missing-file")
	}
	sort.Slice(snapshot.Directories, func(left, right int) bool { return snapshot.Directories[left].Path < snapshot.Directories[right].Path })
	sort.Slice(snapshot.Files, func(left, right int) bool { return snapshot.Files[left].Path < snapshot.Files[right].Path })
	if permit != nil {
		expectedDirectories := tourV4ExpectedDirectories(expected)
		if len(snapshot.Directories) != len(expectedDirectories) {
			snapshot.release()
			return nil, fmt.Errorf("tour-v4-directory-set-mismatch")
		}
		for index := range expectedDirectories {
			if snapshot.Directories[index].Path != expectedDirectories[index] ||
				snapshot.Directories[index].Mode != 0o755 {
				snapshot.release()
				return nil, fmt.Errorf("tour-v4-directory-set-mismatch")
			}
		}
	}
	treeDigest, err := tourV4TreeDigest(snapshot.Directories, snapshot.Files)
	if err != nil {
		snapshot.release()
		return nil, err
	}
	snapshot.TreeSHA256 = treeDigest
	if permit != nil {
		expectedDigest := permit.ArtifactTreeSHA256
		if publicOnly {
			expectedDigest = permit.PublicTreeSHA256
		}
		if treeDigest != expectedDigest {
			snapshot.release()
			return nil, fmt.Errorf("tour-v4-tree-digest-mismatch")
		}
	}
	if !retainContent {
		for index := range snapshot.Files {
			zero(snapshot.Files[index].Content)
			snapshot.Files[index].Content = nil
		}
	}
	return snapshot, nil
}

func tourV4TreeDigest(directories []tourV4DirectorySnapshot, files []tourV4FileSnapshot) (string, error) {
	directoryRows := make([]any, 0, len(directories))
	for _, directory := range directories {
		directoryRows = append(directoryRows, map[string]any{
			"mode": json.Number(strconv.FormatUint(uint64(directory.Mode), 10)),
			"path": directory.Path,
		})
	}
	fileRows := make([]any, 0, len(files))
	for _, file := range files {
		fileRows = append(fileRows, map[string]any{
			"mode": json.Number(strconv.FormatUint(uint64(file.Mode), 10)),
			"path": file.Path, "sha256": strings.TrimPrefix(file.SHA256, "sha256:"),
			"size_bytes": json.Number(strconv.FormatInt(file.Size, 10)),
		})
	}
	raw, err := canonicalJSON(map[string]any{"directories": directoryRows, "files": fileRows})
	if err != nil {
		return "", fmt.Errorf("tour-v4-tree-canonicalization-failed")
	}
	return digest(raw), nil
}

func tourV4ExpectedDirectories(files map[string]tourV4PermitFile) []string {
	set := map[string]struct{}{".": {}}
	for path := range files {
		directory := filepath.Dir(path)
		for directory != "." {
			set[filepath.ToSlash(directory)] = struct{}{}
			directory = filepath.Dir(directory)
		}
	}
	result := make([]string, 0, len(set))
	for path := range set {
		result = append(result, path)
	}
	sort.Strings(result)
	return result
}

func tourV4String(value map[string]any, key string) (string, bool) {
	text, ok := value[key].(string)
	return text, ok && text != ""
}

func tourV4Object(value map[string]any, key string) (map[string]any, bool) {
	object, ok := value[key].(map[string]any)
	return object, ok
}

func tourV4Bool(value map[string]any, key string) (bool, bool) {
	item, ok := value[key].(bool)
	return item, ok
}

func tourV4Number(value map[string]any, key string) (float64, bool) {
	number, ok := value[key].(json.Number)
	if !ok {
		return 0, false
	}
	parsed, err := strconv.ParseFloat(number.String(), 64)
	return parsed, err == nil
}

func tourV4FileMap(snapshot *tourV4TreeSnapshot) map[string]*tourV4FileSnapshot {
	result := make(map[string]*tourV4FileSnapshot, len(snapshot.Files))
	for index := range snapshot.Files {
		result[snapshot.Files[index].Path] = &snapshot.Files[index]
	}
	return result
}

func tourV4DecodeObject(raw []byte, code string) (map[string]any, error) {
	value, err := decodedJSONObject(raw, tourV4MaximumFileBytes)
	if err != nil {
		return nil, fmt.Errorf("%s", code)
	}
	return value, nil
}

func tourV4RejectForbiddenJSONKeys(value any) error {
	switch item := value.(type) {
	case map[string]any:
		for key, child := range item {
			if _, forbidden := tourV4ForbiddenJSONKeys[strings.ToLower(key)]; forbidden {
				return fmt.Errorf("tour-v4-public-coordinate-or-identity-leak")
			}
			if err := tourV4RejectForbiddenJSONKeys(child); err != nil {
				return err
			}
		}
	case []any:
		for _, child := range item {
			if err := tourV4RejectForbiddenJSONKeys(child); err != nil {
				return err
			}
		}
	}
	return nil
}

func tourV4CollectAllStrings(value any, output map[string]struct{}) {
	switch item := value.(type) {
	case map[string]any:
		for _, child := range item {
			tourV4CollectAllStrings(child, output)
		}
	case []any:
		for _, child := range item {
			tourV4CollectAllStrings(child, output)
		}
	case string:
		trimmed := strings.TrimSpace(item)
		if len([]byte(trimmed)) >= 5 {
			output[trimmed] = struct{}{}
		}
	}
}

func tourV4CollectPrivateStrings(value map[string]any, output map[string]struct{}) {
	sensitive := map[string]struct{}{
		"candidate_ref": {}, "crezlo_public_url": {}, "external_id": {},
		"listing_url": {}, "matterport_url": {}, "principal_id": {},
		"property_url": {}, "recipient_email": {},
		"search_run_id": {}, "source_ref": {}, "source_virtual_tour_url": {},
		"three_d_vista_url": {},
	}
	for key, child := range value {
		if _, ok := sensitive[strings.ToLower(key)]; ok {
			tourV4CollectAllStrings(child, output)
		}
	}
	if exactLocation, ok := value["private_exact_location"].(map[string]any); ok {
		facts, _ := exactLocation["facts"].(map[string]any)
		for _, key := range []string{
			"address_lines", "house_number", "postal_code", "raw_address", "street",
		} {
			if child, exists := facts[key]; exists {
				tourV4CollectAllStrings(child, output)
			}
		}
	}
}

func tourV4StringArray(value any) ([]string, bool) {
	items, ok := value.([]any)
	if !ok || len(items) == 0 || len(items) > 64 {
		return nil, false
	}
	result := make([]string, 0, len(items))
	for _, item := range items {
		text, ok := item.(string)
		if !ok || text == "" {
			return nil, false
		}
		result = append(result, text)
	}
	return result, true
}

func tourV4ValidateCoverage(value any) error {
	proof, ok := value.(map[string]any)
	if !ok || proof["status"] != "pass" ||
		proof["source"] != "propertyquarry_generated_reconstruction_viewer_capture" {
		return fmt.Errorf("tour-v4-walkthrough-coverage-invalid")
	}
	expected, expectedOK := tourV4StringArray(proof["segments_expected"])
	visited, visitedOK := tourV4StringArray(proof["segments_visited"])
	segments, segmentsOK := proof["coverage_segments"].([]any)
	if !expectedOK || !visitedOK || !segmentsOK || len(expected) != 8 ||
		len(visited) != len(expected) || len(segments) != len(expected) {
		return fmt.Errorf("tour-v4-walkthrough-coverage-invalid")
	}
	for index := range expected {
		if visited[index] != expected[index] {
			return fmt.Errorf("tour-v4-walkthrough-coverage-invalid")
		}
		segment, ok := segments[index].(map[string]any)
		if !ok || segment["segment"] != expected[index] {
			return fmt.Errorf("tour-v4-walkthrough-coverage-invalid")
		}
		number, numberOK := exactInt(segment["index"], int64(index+1), int64(index+1))
		start, startOK := tourV4Number(segment, "start")
		end, endOK := tourV4Number(segment, "end")
		if !numberOK || number != int64(index+1) || !startOK || !endOK ||
			start != float64(index)*6.0 || end != float64(index+1)*6.0 ||
			end <= start {
			return fmt.Errorf("tour-v4-walkthrough-coverage-invalid")
		}
	}
	return nil
}

func tourV4ValidateArtifact(snapshot *tourV4TreeSnapshot, permit *tourV4Permit) error {
	if snapshot == nil || permit == nil || snapshot.TreeSHA256 != permit.ArtifactTreeSHA256 ||
		len(snapshot.Files) != len(permit.Files) {
		return fmt.Errorf("tour-v4-artifact-binding-invalid")
	}
	files := tourV4FileMap(snapshot)
	for _, required := range []string{
		".propertyquarry-render-commit.json",
		"tour.private.json",
		"tour.json",
		"generated-reconstruction/reconstruction.json",
		"generated-reconstruction/generated-walkthrough.quality.json",
		"generated-reconstruction/generated-walkthrough.mp4",
	} {
		if files[required] == nil {
			return fmt.Errorf("tour-v4-artifact-required-file-missing")
		}
	}
	commit, err := tourV4DecodeObject(files[".propertyquarry-render-commit.json"].Content, "tour-v4-render-commit-invalid")
	if err != nil || !hasKeys(commit, "schema", "slug", "tour_manifest_sha256", "transaction_id") ||
		commit["schema"] != "propertyquarry.render_bundle_commit.v1" ||
		commit["slug"] != permit.Slug ||
		commit["tour_manifest_sha256"] != strings.TrimPrefix(permit.TourManifestSHA256, "sha256:") {
		return fmt.Errorf("tour-v4-render-commit-invalid")
	}
	transactionID, transactionOK := tourV4String(commit, "transaction_id")
	if !transactionOK || !tourV4TransactionPattern.MatchString(transactionID) {
		return fmt.Errorf("tour-v4-render-commit-invalid")
	}
	private, err := tourV4DecodeObject(files["tour.private.json"].Content, "tour-v4-private-manifest-invalid")
	if err != nil {
		return err
	}
	for _, key := range []string{"candidate_ref", "external_id", "principal_id", "search_run_id"} {
		if _, ok := tourV4String(private, key); !ok {
			return fmt.Errorf("tour-v4-private-identity-invalid")
		}
	}
	privateStrings := map[string]struct{}{}
	tourV4CollectPrivateStrings(private, privateStrings)

	tour, err := tourV4DecodeObject(files["tour.json"].Content, "tour-v4-tour-manifest-invalid")
	if err != nil || tour["slug"] != permit.Slug ||
		tour["tour_privacy_mode"] != "anonymous_public" ||
		tour["publication_status"] != "ready" ||
		tour["creation_mode"] != "generated_reconstruction_tour" ||
		tour["public_url"] != "/tours/"+permit.Slug ||
		tour["hosted_url"] != "/tours/"+permit.Slug ||
		tour["video_provider_key"] != permit.Provider ||
		tour["video_relpath"] != "generated-reconstruction/generated-walkthrough.mp4" ||
		tour["video_sidecar_relpath"] != "generated-reconstruction/generated-walkthrough.quality.json" {
		return fmt.Errorf("tour-v4-tour-manifest-invalid")
	}
	generated, generatedOK := tourV4Object(tour, "generated_reconstruction")
	verified, verifiedOK := tourV4Bool(generated, "verified_provider_capture")
	satisfies, satisfiesOK := tourV4Bool(generated, "satisfies_verified_tour_gate")
	stops, stopsOK := exactInt(generated["walkthrough_stop_count"], 8, 8)
	if !generatedOK || generated["provider"] != permit.Provider ||
		generated["viewer_version"] != permit.ViewerVersion ||
		generated["disclosure"] != permit.Disclosure ||
		!verifiedOK || verified || !satisfiesOK || satisfies ||
		!stopsOK || stops != 8 ||
		generated["walkthrough_video_relpath"] != "generated-reconstruction/generated-walkthrough.mp4" ||
		generated["walkthrough_sidecar_relpath"] != "generated-reconstruction/generated-walkthrough.quality.json" ||
		tourV4ValidateCoverage(generated["walkthrough_coverage_proof"]) != nil {
		return fmt.Errorf("tour-v4-generated-reconstruction-contract-invalid")
	}
	if err := tourV4RejectForbiddenJSONKeys(tour); err != nil {
		return err
	}

	reconstruction, err := tourV4DecodeObject(files["generated-reconstruction/reconstruction.json"].Content, "tour-v4-reconstruction-manifest-invalid")
	if err != nil || reconstruction["slug"] != permit.Slug ||
		reconstruction["provider"] != permit.Provider ||
		reconstruction["disclosure"] != permit.Disclosure {
		return fmt.Errorf("tour-v4-reconstruction-manifest-invalid")
	}
	verified, verifiedOK = tourV4Bool(reconstruction, "verified_provider_capture")
	satisfies, satisfiesOK = tourV4Bool(reconstruction, "satisfies_verified_tour_gate")
	viewer, viewerOK := tourV4Object(reconstruction, "viewer")
	walkthrough, walkthroughOK := tourV4Object(reconstruction, "walkthrough")
	duration, durationOK := tourV4Number(walkthrough, "duration_seconds")
	size, sizeOK := exactInt(walkthrough["size_bytes"], 1, tourV4MaximumFileBytes)
	if !verifiedOK || verified || !satisfiesOK || satisfies ||
		!viewerOK || viewer["version"] != permit.ViewerVersion ||
		viewer["sha256"] != strings.TrimPrefix(files["generated-reconstruction/viewer.html"].SHA256, "sha256:") ||
		!walkthroughOK || walkthrough["status"] != "generated" ||
		walkthrough["relpath"] != "generated-walkthrough.mp4" ||
		walkthrough["sidecar_relpath"] != "generated-walkthrough.quality.json" ||
		walkthrough["sha256"] != strings.TrimPrefix(permit.WalkthroughSHA256, "sha256:") ||
		walkthrough["sidecar_sha256"] != strings.TrimPrefix(permit.QualityReceiptSHA256, "sha256:") ||
		!durationOK || duration != 48.0 || !sizeOK ||
		size != files["generated-reconstruction/generated-walkthrough.mp4"].Size ||
		tourV4ValidateCoverage(walkthrough["coverage_proof"]) != nil {
		return fmt.Errorf("tour-v4-reconstruction-contract-invalid")
	}
	if err := tourV4RejectForbiddenJSONKeys(reconstruction); err != nil {
		return err
	}

	quality, err := tourV4DecodeObject(files["generated-reconstruction/generated-walkthrough.quality.json"].Content, "tour-v4-quality-receipt-invalid")
	qualityDuration, qualityDurationOK := tourV4Number(quality, "duration_seconds")
	qualityStops, qualityStopsOK := exactInt(quality["room_stop_count"], 8, 8)
	if err != nil || quality["provider_key"] != permit.Provider ||
		quality["viewer_capture_mode"] != true || !qualityDurationOK ||
		qualityDuration != 48.0 || !qualityStopsOK || qualityStops != 8 ||
		tourV4ValidateCoverage(quality["walkthrough_coverage_proof"]) != nil {
		return fmt.Errorf("tour-v4-quality-receipt-invalid")
	}
	if err := tourV4RejectForbiddenJSONKeys(quality); err != nil {
		return err
	}

	for _, file := range snapshot.Files {
		if !file.Public {
			continue
		}
		if strings.HasSuffix(file.Path, ".json") {
			publicValue, err := tourV4DecodeObject(file.Content, "tour-v4-public-json-invalid")
			if err != nil {
				return err
			}
			if err := tourV4RejectForbiddenJSONKeys(publicValue); err != nil {
				return err
			}
		}
		for secret := range privateStrings {
			if bytes.Contains(file.Content, []byte(secret)) {
				return fmt.Errorf("tour-v4-private-value-leaked")
			}
		}
		lower := bytes.ToLower(file.Content)
		if bytes.Contains(lower, []byte("geo:")) ||
			bytes.Contains(lower, []byte("maps.google.")) ||
			bytes.Contains(lower, []byte("openstreetmap.org/?mlat=")) {
			return fmt.Errorf("tour-v4-public-coordinate-url-leak")
		}
	}
	return nil
}

type tourV4AuthorityBinding struct {
	Profile                   string
	ConfigDigest              string
	RuntimeSHA                string
	WorkflowSHA               string
	DeploymentID              string
	PackageAuthorityKeyID     string
	TourMaterializationDigest string
	SourceManifestDigest      string
	HostMachineIDDigest       string
	ReceiptKeyID              string
	MaterializedAt            int64
	ValidUntil                int64
}

// TourV4DetachedMaterials are the signed package members needed by the
// self-contained, attested installer-image dispatcher. They are validated
// again by the authority after the dispatcher chroots onto the host.
type TourV4DetachedMaterials struct {
	AuthorityBootstrap          []byte
	AuthorityBootstrapSignature []byte
	Materialization             []byte
	MaterializationSignature    []byte
	PackageAnchor               []byte
	ReceiptKey                  []byte
	ReceiptAnchor               []byte
}

type tourV4PublishInput struct {
	BundlePath             string
	ExpectedOldTreeSHA256  string
	ExpectedManifestSHA256 string
	TransactionID          string
}

type tourV4Prepared struct {
	TransactionID         string
	ManifestSHA256        string
	PreparedReceiptSHA256 string
	ExpectedOldTreeSHA256 string
	ObservedOldTreeSHA256 string
	CandidateTreeSHA256   string
	ArtifactTreeSHA256    string
	Slug                  string
	StageRelpath          string
	RollbackRelpath       string
	OldDevice             uint64
	OldInode              uint64
	CandidateDevice       uint64
	CandidateInode        uint64
	OldWasAbsent          bool
	Raw                   []byte
}

func (prepared *tourV4Prepared) release() {
	if prepared == nil {
		return
	}
	zero(prepared.Raw)
	*prepared = tourV4Prepared{}
}

func tourV4AuthorityBindingFor(config *Config, key ed25519.PrivateKey) (tourV4AuthorityBinding, error) {
	if config == nil || len(key) != ed25519.PrivateKeySize {
		return tourV4AuthorityBinding{}, fmt.Errorf("tour-v4-authority-binding-invalid")
	}
	keyID, err := publicKeyID(key.Public().(ed25519.PublicKey))
	if err != nil || keyID != config.ReceiptAuthorityKeyID ||
		!digestPattern.MatchString(config.Digest) ||
		!shaPattern.MatchString(config.RuntimeSHA) ||
		!shaPattern.MatchString(config.WorkflowSHA) ||
		!deploymentIDPattern.MatchString(config.DeploymentID) {
		return tourV4AuthorityBinding{}, fmt.Errorf("tour-v4-authority-binding-invalid")
	}
	return tourV4AuthorityBinding{
		Profile:      "single-host-production-v2",
		ConfigDigest: config.Digest, RuntimeSHA: config.RuntimeSHA,
		WorkflowSHA: config.WorkflowSHA, DeploymentID: config.DeploymentID,
		ReceiptKeyID: keyID,
	}, nil
}

func tourV4DetachedAuthority(
	root string,
	materials TourV4DetachedMaterials,
) (tourV4AuthorityBinding, ed25519.PrivateKey, error) {
	var empty tourV4AuthorityBinding
	if root == "" || !filepath.IsAbs(root) || filepath.Clean(root) != root ||
		len(materials.AuthorityBootstrap) < 2 ||
		len(materials.AuthorityBootstrapSignature) != ed25519.SignatureSize ||
		len(materials.Materialization) < 2 ||
		len(materials.MaterializationSignature) != ed25519.SignatureSize ||
		len(materials.PackageAnchor) < 2 || len(materials.PackageAnchor) > 4096 ||
		len(materials.ReceiptKey) < 2 ||
		len(materials.ReceiptKey) > 4096 || len(materials.ReceiptAnchor) < 2 ||
		len(materials.ReceiptAnchor) > 4096 {
		return empty, nil, fmt.Errorf("tour-v4-detached-material-invalid")
	}
	packagePublic, packageKeyID, err := parsePublicKey(materials.PackageAnchor)
	if err != nil {
		zero(packagePublic)
		return empty, nil, fmt.Errorf("tour-v4-detached-package-anchor-invalid")
	}
	defer zero(packagePublic)
	if !ed25519.Verify(
		packagePublic,
		framed(tourV4DetachedBootstrapDomain, materials.AuthorityBootstrap),
		materials.AuthorityBootstrapSignature,
	) {
		return empty, nil, fmt.Errorf("tour-v4-detached-bootstrap-authentication-failed")
	}
	receiptPublic, receiptKeyID, err := parsePublicKey(materials.ReceiptAnchor)
	if err != nil {
		zero(receiptPublic)
		return empty, nil, fmt.Errorf("tour-v4-detached-receipt-anchor-invalid")
	}
	defer zero(receiptPublic)
	if err := tourV4ValidateDetachedBootstrap(
		materials.AuthorityBootstrap,
		materials.PackageAnchor,
		materials.ReceiptAnchor,
		packageKeyID,
		receiptKeyID,
	); err != nil {
		return empty, nil, err
	}
	if !ed25519.Verify(
		packagePublic,
		framed(tourV4DetachedMaterializationDomain, materials.Materialization),
		materials.MaterializationSignature,
	) {
		return empty, nil, fmt.Errorf("tour-v4-detached-materialization-authentication-failed")
	}
	binding, err := tourV4ParseDetachedMaterialization(
		materials.Materialization,
		digest(materials.AuthorityBootstrap),
		digest(materials.ReceiptAnchor),
		packageKeyID,
		receiptKeyID,
	)
	if err != nil {
		return empty, nil, err
	}
	ownerUID, ownerGID := secureOwner(root)
	machineIDRaw, err := secureRead(
		root, "/etc/machine-id", 0o444, ownerUID, ownerGID, 64,
	)
	if err != nil {
		return empty, nil, fmt.Errorf("tour-v4-detached-host-binding-unavailable")
	}
	machineID := strings.TrimSpace(string(machineIDRaw))
	zero(machineIDRaw)
	if !regexp.MustCompile(`^[0-9a-f]{32}$`).MatchString(machineID) ||
		digest([]byte(machineID)) != binding.HostMachineIDDigest {
		return empty, nil, fmt.Errorf("tour-v4-detached-host-binding-invalid")
	}
	receiptKey, err := parsePrivateKey(materials.ReceiptKey)
	if err != nil {
		return empty, nil, fmt.Errorf("tour-v4-detached-receipt-key-invalid")
	}
	if !bytes.Equal(
		receiptPublic, receiptKey.Public().(ed25519.PublicKey),
	) || receiptKeyID != binding.ReceiptKeyID {
		zero(receiptKey)
		return empty, nil, fmt.Errorf("tour-v4-detached-receipt-binding-invalid")
	}
	return binding, receiptKey, nil
}

func tourV4ValidateDetachedBootstrap(
	raw, packageAnchor, receiptAnchor []byte,
	packageKeyID, receiptKeyID string,
) error {
	value, err := strictJSON(raw, maximumConfigBytes)
	if err != nil || !hasKeys(
		value,
		"created_at_epoch", "package_authority_key_id",
		"package_authority_private_sha256", "package_authority_public_sha256",
		"package_authority_source", "receipt_authority_key_id",
		"receipt_authority_public_sha256", "schema", "version",
	) {
		return fmt.Errorf("tour-v4-detached-bootstrap-shape-invalid")
	}
	schema, _ := exactString(value["schema"])
	source, _ := exactString(value["package_authority_source"])
	configuredPackageID, _ := exactString(value["package_authority_key_id"])
	privateDigest, _ := exactString(value["package_authority_private_sha256"])
	publicDigest, _ := exactString(value["package_authority_public_sha256"])
	configuredReceiptID, _ := exactString(value["receipt_authority_key_id"])
	receiptDigest, _ := exactString(value["receipt_authority_public_sha256"])
	created, createdOK := exactInt(value["created_at_epoch"], 1, 1<<62)
	version, versionOK := exactInt(value["version"], 2, 2)
	if schema != tourV4DetachedBootstrapSchema ||
		source != tourV4DetachedCanonicalAuthorityRoot ||
		configuredPackageID != packageKeyID ||
		privateDigest != tourV4DetachedCanonicalPrivateDigest ||
		publicDigest != digest(packageAnchor) ||
		configuredReceiptID != receiptKeyID ||
		receiptDigest != digest(receiptAnchor) ||
		!createdOK || created < 1 || !versionOK || version != 2 {
		return fmt.Errorf("tour-v4-detached-bootstrap-binding-invalid")
	}
	return nil
}

func tourV4ParseDetachedMaterialization(
	raw []byte,
	bootstrapDigest, receiptAnchorDigest, packageKeyID, receiptKeyID string,
) (tourV4AuthorityBinding, error) {
	var empty tourV4AuthorityBinding
	value, err := strictJSON(raw, maximumConfigBytes)
	if err != nil || !hasKeys(
		value,
		"accepted_installer_mode", "allowed_operations", "artifact_bundle_path",
		"artifact_manifest_sha256", "artifact_public_tree_sha256", "artifact_slug",
		"authoritative", "authority_bootstrap_sha256", "host_install_permitted",
		"host_machine_id_digest", "materialized_at_epoch",
		"native_build_receipt_sha256", "network_required",
		"package_authority_key_id", "performs_release_effects",
		"persistent_credential_installation_permitted", "production_ready",
		"publication_dispatch_authorized", "publication_target_root",
		"receipt_authority_key_id", "receipt_authority_public_sha256",
		"root_helper_authorization_required", "runtime_deployment_permitted",
		"schema", "source_manifest_digest", "valid_until_epoch", "version",
	) {
		return empty, fmt.Errorf("tour-v4-detached-materialization-shape-invalid")
	}
	schema, _ := exactString(value["schema"])
	installerMode, _ := exactString(value["accepted_installer_mode"])
	bundlePath, _ := exactString(value["artifact_bundle_path"])
	manifestSHA, _ := exactString(value["artifact_manifest_sha256"])
	publicTreeSHA, _ := exactString(value["artifact_public_tree_sha256"])
	slug, _ := exactString(value["artifact_slug"])
	targetRoot, _ := exactString(value["publication_target_root"])
	configuredBootstrap, _ := exactString(value["authority_bootstrap_sha256"])
	buildReceiptDigest, _ := exactString(value["native_build_receipt_sha256"])
	configuredPackageID, _ := exactString(value["package_authority_key_id"])
	configuredReceiptID, _ := exactString(value["receipt_authority_key_id"])
	configuredReceiptAnchor, _ := exactString(value["receipt_authority_public_sha256"])
	sourceDigest, _ := exactString(value["source_manifest_digest"])
	hostDigest, _ := exactString(value["host_machine_id_digest"])
	materialized, materializedOK := exactInt(value["materialized_at_epoch"], 1, 1<<62)
	validUntil, validOK := exactInt(value["valid_until_epoch"], 1, 1<<62)
	version, versionOK := exactInt(value["version"], 4, 4)
	for _, field := range []string{
		"authoritative", "host_install_permitted", "network_required",
		"performs_release_effects", "persistent_credential_installation_permitted",
		"production_ready", "runtime_deployment_permitted",
	} {
		flag, ok := value[field].(bool)
		if !ok || flag {
			return empty, fmt.Errorf("tour-v4-detached-materialization-claim-invalid")
		}
	}
	for _, field := range []string{
		"publication_dispatch_authorized", "root_helper_authorization_required",
	} {
		flag, ok := value[field].(bool)
		if !ok || !flag {
			return empty, fmt.Errorf("tour-v4-detached-materialization-claim-invalid")
		}
	}
	operations, ok := value["allowed_operations"].([]any)
	if !ok || len(operations) != len(tourV4DetachedOperations) {
		return empty, fmt.Errorf("tour-v4-detached-materialization-operations-invalid")
	}
	for index, expected := range tourV4DetachedOperations {
		observed, ok := operations[index].(string)
		if !ok || observed != expected {
			return empty, fmt.Errorf("tour-v4-detached-materialization-operations-invalid")
		}
	}
	permit, observedManifestSHA, err := tourV4PermitByManifestDigest(manifestSHA)
	if err != nil {
		return empty, fmt.Errorf("tour-v4-detached-materialization-artifact-invalid")
	}
	if schema != tourV4DetachedMaterializationSchema ||
		installerMode != "dispatch-tour-v4" ||
		bundlePath != tourV4DetachedBundlePath ||
		observedManifestSHA != manifestSHA ||
		publicTreeSHA != permit.PublicTreeSHA256 || slug != permit.Slug ||
		targetRoot != tourV4LiveVolumeRoot ||
		configuredBootstrap != bootstrapDigest ||
		!digestPattern.MatchString(buildReceiptDigest) ||
		configuredPackageID != packageKeyID ||
		configuredReceiptID != receiptKeyID ||
		configuredReceiptAnchor != receiptAnchorDigest ||
		!digestPattern.MatchString(sourceDigest) ||
		!digestPattern.MatchString(hostDigest) ||
		!materializedOK || !validOK ||
		validUntil != materialized+tourV4DetachedTTLSeconds ||
		!versionOK || version != 4 {
		return empty, fmt.Errorf("tour-v4-detached-materialization-binding-invalid")
	}
	binding := tourV4AuthorityBinding{
		Profile:                   tourV4DetachedProfile,
		PackageAuthorityKeyID:     packageKeyID,
		TourMaterializationDigest: digest(raw),
		SourceManifestDigest:      sourceDigest,
		HostMachineIDDigest:       hostDigest,
		ReceiptKeyID:              receiptKeyID,
		MaterializedAt:            materialized,
		ValidUntil:                validUntil,
	}
	if err := tourV4ValidateAuthorityBinding(binding); err != nil {
		return empty, err
	}
	return binding, nil
}

func tourV4ValidateAuthorityBinding(binding tourV4AuthorityBinding) error {
	switch binding.Profile {
	case "single-host-production-v2":
		if !digestPattern.MatchString(binding.ConfigDigest) ||
			!shaPattern.MatchString(binding.RuntimeSHA) ||
			!shaPattern.MatchString(binding.WorkflowSHA) ||
			!deploymentIDPattern.MatchString(binding.DeploymentID) ||
			!digestPattern.MatchString(binding.ReceiptKeyID) ||
			binding.PackageAuthorityKeyID != "" ||
			binding.TourMaterializationDigest != "" ||
			binding.SourceManifestDigest != "" ||
			binding.HostMachineIDDigest != "" ||
			binding.MaterializedAt != 0 || binding.ValidUntil != 0 {
			return fmt.Errorf("tour-v4-authority-binding-invalid")
		}
	case tourV4DetachedProfile:
		if binding.ConfigDigest != "" ||
			!digestPattern.MatchString(binding.PackageAuthorityKeyID) ||
			!digestPattern.MatchString(binding.TourMaterializationDigest) ||
			!digestPattern.MatchString(binding.SourceManifestDigest) ||
			!digestPattern.MatchString(binding.HostMachineIDDigest) ||
			!digestPattern.MatchString(binding.ReceiptKeyID) ||
			binding.PackageAuthorityKeyID == binding.ReceiptKeyID ||
			binding.MaterializedAt < 1 ||
			binding.ValidUntil != binding.MaterializedAt+tourV4DetachedTTLSeconds ||
			binding.RuntimeSHA != "" || binding.WorkflowSHA != "" ||
			binding.DeploymentID != "" {
			return fmt.Errorf("tour-v4-authority-binding-invalid")
		}
	default:
		return fmt.Errorf("tour-v4-authority-binding-invalid")
	}
	return nil
}

func tourV4RootPath(root, absolute string) string {
	if root == "" || root == "/" {
		return absolute
	}
	return filepath.Join(root, strings.TrimPrefix(absolute, "/"))
}

func tourV4OpenFixedRoot(root, absolute string, expectedMode os.FileMode, ownerRequired bool) (*os.File, error) {
	path := tourV4RootPath(root, absolute)
	directory, err := tourV4OpenDirectoryAbsolute(path)
	if err != nil {
		return nil, err
	}
	info, err := directory.Stat()
	metadata, metaErr := tourV4StatMetadata(info)
	ownerUID, ownerGID := secureOwner(root)
	if err != nil || metaErr != nil || info.Mode().Perm() != expectedMode ||
		(ownerRequired && (metadata.Uid != ownerUID || metadata.Gid != ownerGID)) {
		directory.Close()
		return nil, fmt.Errorf("tour-v4-fixed-root-invalid")
	}
	return directory, nil
}

func tourV4OpenReceiptRoot(root string) (*os.File, error) {
	return tourV4OpenFixedRoot(root, tourV4ReceiptRoot, 0o700, true)
}

func tourV4EnsureDirectoryAt(
	parent *os.File,
	name string,
	mode os.FileMode,
	ownerUID, ownerGID uint32,
) (*os.File, error) {
	if parent == nil || !tourV4SafeEntryName(name) || mode != 0o700 {
		return nil, fmt.Errorf("tour-v4-state-directory-input-invalid")
	}
	directory, err := tourV4OpenDirectoryAt(parent, name)
	created := false
	if errors.Is(err, syscall.ENOENT) {
		if err := syscall.Mkdirat(int(parent.Fd()), name, uint32(mode)); err != nil {
			return nil, fmt.Errorf("tour-v4-state-directory-create-failed")
		}
		created = true
		directory, err = tourV4OpenDirectoryAt(parent, name)
	}
	if err != nil {
		return nil, fmt.Errorf("tour-v4-state-directory-unavailable")
	}
	if created {
		if err := syscall.Fchown(
			int(directory.Fd()), int(ownerUID), int(ownerGID),
		); err != nil || syscall.Fchmod(int(directory.Fd()), uint32(mode)) != nil ||
			directory.Sync() != nil || parent.Sync() != nil {
			directory.Close()
			return nil, fmt.Errorf("tour-v4-state-directory-initialize-failed")
		}
	}
	info, err := directory.Stat()
	metadata, metadataErr := tourV4StatMetadata(info)
	if err != nil || metadataErr != nil || !info.IsDir() ||
		info.Mode().Perm() != mode || metadata.Uid != ownerUID ||
		metadata.Gid != ownerGID {
		directory.Close()
		return nil, fmt.Errorf("tour-v4-state-directory-invalid")
	}
	return directory, nil
}

func tourV4EnsureDetachedStateRoot() error {
	varRoot, err := tourV4OpenDirectoryAbsolute("/var/lib")
	if err != nil {
		return fmt.Errorf("tour-v4-state-parent-unavailable")
	}
	defer varRoot.Close()
	info, err := varRoot.Stat()
	metadata, metadataErr := tourV4StatMetadata(info)
	if err != nil || metadataErr != nil || !info.IsDir() ||
		info.Mode().Perm()&0o022 != 0 || metadata.Uid != 0 || metadata.Gid != 0 {
		return fmt.Errorf("tour-v4-state-parent-invalid")
	}
	releaseRoot, err := tourV4EnsureDirectoryAt(
		varRoot, "propertyquarry-release-single-host-v2", 0o700, 0, 0,
	)
	if err != nil {
		return err
	}
	defer releaseRoot.Close()
	receipts, err := tourV4EnsureDirectoryAt(
		releaseRoot, "tour-publication-receipts", 0o700, 0, 0,
	)
	if err != nil {
		return err
	}
	return receipts.Close()
}

func tourV4OpenLiveRoot(root string) (*os.File, error) {
	return tourV4OpenFixedRoot(root, tourV4LiveVolumeRoot, 0o755, false)
}

func tourV4OpenControlRoot(root string, live *os.File) (*os.File, error) {
	if live == nil {
		return nil, fmt.Errorf("tour-v4-control-root-input-invalid")
	}
	ownerUID, ownerGID := secureOwner(root)
	control, err := tourV4OpenDirectoryAt(live, tourV4ControlRelpath)
	if errors.Is(err, syscall.ENOENT) {
		if err := syscall.Mkdirat(int(live.Fd()), tourV4ControlRelpath, 0o700); err != nil {
			return nil, fmt.Errorf("tour-v4-control-root-create-failed")
		}
		control, err = tourV4OpenDirectoryAt(live, tourV4ControlRelpath)
		if err != nil {
			return nil, fmt.Errorf("tour-v4-control-root-open-failed")
		}
		if err := syscall.Fchown(int(control.Fd()), int(ownerUID), int(ownerGID)); err != nil ||
			syscall.Fchmod(int(control.Fd()), 0o700) != nil ||
			control.Sync() != nil || live.Sync() != nil {
			control.Close()
			return nil, fmt.Errorf("tour-v4-control-root-initialize-failed")
		}
	} else if err != nil {
		return nil, fmt.Errorf("tour-v4-control-root-unavailable")
	}
	info, err := control.Stat()
	metadata, metaErr := tourV4StatMetadata(info)
	if err != nil || metaErr != nil || !info.IsDir() ||
		info.Mode().Perm() != 0o700 ||
		metadata.Uid != ownerUID || metadata.Gid != ownerGID {
		control.Close()
		return nil, fmt.Errorf("tour-v4-control-root-invalid")
	}
	return control, nil
}

func tourV4ControlRootStillBound(live, control *os.File) bool {
	if live == nil || control == nil {
		return false
	}
	expected, err := control.Stat()
	if err != nil {
		return false
	}
	observed, err := tourV4OpenDirectoryAt(live, tourV4ControlRelpath)
	if err != nil {
		return false
	}
	defer observed.Close()
	actual, err := observed.Stat()
	return err == nil && os.SameFile(expected, actual) &&
		actual.Mode().Perm() == 0o700
}

func tourV4AcquireLock(receipts *os.File, root string) (*os.File, error) {
	if receipts == nil {
		return nil, fmt.Errorf("tour-v4-lock-root-invalid")
	}
	fd, err := syscall.Openat(
		int(receipts.Fd()), ".tour-publish-v4.lock",
		syscall.O_CREAT|syscall.O_RDWR|syscall.O_CLOEXEC|syscall.O_NOFOLLOW,
		0o600,
	)
	if err != nil {
		return nil, fmt.Errorf("tour-v4-lock-unavailable")
	}
	lock := os.NewFile(uintptr(fd), ".tour-publish-v4.lock")
	info, err := lock.Stat()
	metadata, metaErr := tourV4StatMetadata(info)
	ownerUID, ownerGID := secureOwner(root)
	if err != nil || metaErr != nil || !info.Mode().IsRegular() ||
		info.Mode().Perm() != 0o600 || metadata.Nlink != 1 ||
		metadata.Uid != ownerUID || metadata.Gid != ownerGID {
		lock.Close()
		return nil, fmt.Errorf("tour-v4-lock-invalid")
	}
	if err := syscall.Flock(fd, syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		lock.Close()
		return nil, fmt.Errorf("tour-v4-publication-busy")
	}
	return lock, nil
}

func tourV4ReleaseLock(lock *os.File) {
	if lock != nil {
		_ = syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
		_ = lock.Close()
	}
}

func tourV4RandomPendingName() (string, error) {
	random := make([]byte, 16)
	if _, err := io.ReadFull(rand.Reader, random); err != nil {
		return "", fmt.Errorf("tour-v4-random-unavailable")
	}
	name := ".tour-v4-pending-" + hex.EncodeToString(random)
	zero(random)
	return name, nil
}

func tourV4WriteNoReplace(directory *os.File, name string, raw []byte, mode uint32, uid, gid uint32) error {
	if directory == nil || !tourV4SafeEntryName(name) || len(raw) < 1 ||
		len(raw) > maximumJournalBytes || (mode != 0o600 && mode != 0o644) {
		return fmt.Errorf("tour-v4-receipt-input-invalid")
	}
	pending, err := tourV4RandomPendingName()
	if err != nil {
		return err
	}
	fd, err := syscall.Openat(
		int(directory.Fd()), pending,
		syscall.O_WRONLY|syscall.O_CREAT|syscall.O_EXCL|syscall.O_CLOEXEC|syscall.O_NOFOLLOW,
		mode,
	)
	if err != nil {
		return fmt.Errorf("tour-v4-receipt-create-failed")
	}
	file := os.NewFile(uintptr(fd), pending)
	published := false
	defer func() {
		if file != nil {
			_ = file.Close()
		}
		if !published {
			_ = syscall.Unlinkat(int(directory.Fd()), pending)
		}
	}()
	if err := syscall.Fchown(fd, int(uid), int(gid)); err != nil ||
		writeAll(file, raw) != nil || file.Sync() != nil || file.Close() != nil {
		file = nil
		return fmt.Errorf("tour-v4-receipt-write-failed")
	}
	file = nil
	if err := renameAtNoReplace(int(directory.Fd()), pending, name); err != nil {
		if errors.Is(err, syscall.EEXIST) {
			return fmt.Errorf("tour-v4-receipt-already-exists")
		}
		return fmt.Errorf("tour-v4-receipt-publish-failed")
	}
	published = true
	if err := directory.Sync(); err != nil {
		return fmt.Errorf("tour-v4-receipt-durability-unknown")
	}
	return nil
}

func tourV4WriteSignedReceipt(root string, directory *os.File, name string, payload map[string]any, key ed25519.PrivateKey) ([]byte, string, error) {
	if len(key) != ed25519.PrivateKeySize || payload == nil {
		return nil, "", fmt.Errorf("tour-v4-receipt-signing-input-invalid")
	}
	wire, err := signReceipt(payload, key)
	if err != nil {
		return nil, "", err
	}
	if _, canonical, err := verifySignedReceiptPayload(wire, key.Public().(ed25519.PublicKey)); err != nil {
		zero(wire)
		return nil, "", fmt.Errorf("tour-v4-receipt-self-verification-failed")
	} else {
		zero(canonical)
	}
	uid, gid := secureOwner(root)
	if err := tourV4WriteNoReplace(directory, name, wire, 0o600, uid, gid); err != nil {
		zero(wire)
		return nil, "", err
	}
	return wire, digest(wire), nil
}

func tourV4ReadSignedReceipt(root string, directory *os.File, name string, public ed25519.PublicKey) (map[string]any, []byte, string, error) {
	if directory == nil || !tourV4SafeEntryName(name) || len(public) != ed25519.PublicKeySize {
		return nil, nil, "", fmt.Errorf("tour-v4-receipt-read-input-invalid")
	}
	fd, err := syscall.Openat(
		int(directory.Fd()), name,
		syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0,
	)
	if err != nil {
		return nil, nil, "", err
	}
	file := os.NewFile(uintptr(fd), name)
	defer file.Close()
	info, err := file.Stat()
	metadata, metaErr := tourV4StatMetadata(info)
	uid, gid := secureOwner(root)
	if err != nil || metaErr != nil || !info.Mode().IsRegular() ||
		info.Mode().Perm() != 0o600 || metadata.Nlink != 1 ||
		metadata.Uid != uid || metadata.Gid != gid ||
		info.Size() < 1 || info.Size() > maximumJournalBytes {
		return nil, nil, "", fmt.Errorf("tour-v4-receipt-metadata-invalid")
	}
	raw := make([]byte, info.Size())
	if _, err := io.ReadFull(file, raw); err != nil {
		zero(raw)
		return nil, nil, "", fmt.Errorf("tour-v4-receipt-read-failed")
	}
	after, err := file.Stat()
	if err != nil || !tourV4SameFingerprint(info, after) {
		zero(raw)
		return nil, nil, "", fmt.Errorf("tour-v4-receipt-race-detected")
	}
	payload, canonical, err := verifySignedReceiptPayload(raw, public)
	zero(canonical)
	if err != nil {
		zero(raw)
		return nil, nil, "", fmt.Errorf("tour-v4-receipt-authentication-failed")
	}
	return payload, raw, digest(raw), nil
}

func tourV4ReceiptNames(transactionID string) (string, string, string, error) {
	if !tourV4TransactionPattern.MatchString(transactionID) {
		return "", "", "", fmt.Errorf("tour-v4-transaction-id-invalid")
	}
	base := "tour-v4-" + transactionID
	return base + ".prepared.json", base + ".terminal.json", base + ".rollback.json", nil
}

func tourV4OpenRelativeDirectory(root *os.File, relpath string) (*os.File, error) {
	if root == nil || (relpath != "." && !tourV4SafeRelativePath(relpath)) {
		return nil, fmt.Errorf("tour-v4-relative-directory-invalid")
	}
	duplicate, err := syscall.Dup(int(root.Fd()))
	if err != nil {
		return nil, fmt.Errorf("tour-v4-relative-directory-dup-failed")
	}
	current := os.NewFile(uintptr(duplicate), ".")
	if relpath == "." {
		return current, nil
	}
	for _, component := range strings.Split(relpath, "/") {
		child, err := tourV4OpenDirectoryAt(current, component)
		current.Close()
		if err != nil {
			return nil, fmt.Errorf("tour-v4-relative-directory-unavailable")
		}
		current = child
	}
	return current, nil
}

func tourV4CreateStage(root string, control *os.File, controlPath, stageName string, source *tourV4TreeSnapshot, permit *tourV4Permit) (*tourV4TreeSnapshot, error) {
	if control == nil || source == nil || permit == nil || !tourV4SafeEntryName(stageName) {
		return nil, fmt.Errorf("tour-v4-stage-input-invalid")
	}
	ownerUID, ownerGID := secureOwner(root)
	if err := syscall.Mkdirat(int(control.Fd()), stageName, 0o755); err != nil {
		if errors.Is(err, syscall.EEXIST) {
			return nil, fmt.Errorf("tour-v4-stage-already-exists")
		}
		return nil, fmt.Errorf("tour-v4-stage-create-failed")
	}
	stage, err := tourV4OpenDirectoryAt(control, stageName)
	if err != nil {
		return nil, fmt.Errorf("tour-v4-stage-open-failed")
	}
	cleanup := true
	defer func() {
		stage.Close()
		if cleanup {
			_ = tourV4RemoveTreeAt(control, stageName)
			_ = control.Sync()
		}
	}()
	if err := syscall.Fchown(int(stage.Fd()), int(ownerUID), int(ownerGID)); err != nil ||
		syscall.Fchmod(int(stage.Fd()), 0o755) != nil {
		return nil, fmt.Errorf("tour-v4-stage-metadata-failed")
	}
	publicFiles := map[string]tourV4PermitFile{}
	for _, file := range permit.Files {
		if file.Public {
			publicFiles[file.Path] = file
		}
	}
	directories := tourV4ExpectedDirectories(publicFiles)
	for _, relpath := range directories {
		if relpath == "." {
			continue
		}
		parentPath := filepath.ToSlash(filepath.Dir(relpath))
		parent, err := tourV4OpenRelativeDirectory(stage, parentPath)
		if err != nil {
			return nil, err
		}
		name := filepath.Base(relpath)
		mkdirErr := syscall.Mkdirat(int(parent.Fd()), name, 0o755)
		parent.Close()
		if mkdirErr != nil {
			return nil, fmt.Errorf("tour-v4-stage-directory-create-failed")
		}
		child, err := tourV4OpenRelativeDirectory(stage, relpath)
		if err != nil {
			return nil, err
		}
		if err := syscall.Fchown(int(child.Fd()), int(ownerUID), int(ownerGID)); err != nil ||
			syscall.Fchmod(int(child.Fd()), 0o755) != nil {
			child.Close()
			return nil, fmt.Errorf("tour-v4-stage-directory-metadata-failed")
		}
		child.Close()
	}
	sourceFiles := tourV4FileMap(source)
	for _, permitFile := range permit.Files {
		if !permitFile.Public {
			continue
		}
		sourceFile := sourceFiles[permitFile.Path]
		if sourceFile == nil || sourceFile.SHA256 != permitFile.SHA256 ||
			sourceFile.Size != permitFile.Size || sourceFile.Mode != permitFile.Mode {
			return nil, fmt.Errorf("tour-v4-stage-source-mismatch")
		}
		parent, err := tourV4OpenRelativeDirectory(stage, filepath.ToSlash(filepath.Dir(permitFile.Path)))
		if err != nil {
			return nil, err
		}
		name := filepath.Base(permitFile.Path)
		fd, err := syscall.Openat(
			int(parent.Fd()), name,
			syscall.O_WRONLY|syscall.O_CREAT|syscall.O_EXCL|syscall.O_CLOEXEC|syscall.O_NOFOLLOW,
			0o644,
		)
		if err != nil {
			parent.Close()
			return nil, fmt.Errorf("tour-v4-stage-file-create-failed")
		}
		file := os.NewFile(uintptr(fd), name)
		writeErr := writeAll(file, sourceFile.Content)
		metadataErr := syscall.Fchown(fd, int(ownerUID), int(ownerGID))
		modeErr := syscall.Fchmod(fd, 0o644)
		syncErr := file.Sync()
		closeErr := file.Close()
		parent.Close()
		if writeErr != nil || metadataErr != nil || modeErr != nil || syncErr != nil || closeErr != nil {
			return nil, fmt.Errorf("tour-v4-stage-file-write-failed")
		}
	}
	for index := len(directories) - 1; index >= 0; index-- {
		directory, err := tourV4OpenRelativeDirectory(stage, directories[index])
		if err != nil {
			return nil, err
		}
		syncErr := directory.Sync()
		directory.Close()
		if syncErr != nil {
			return nil, fmt.Errorf("tour-v4-stage-directory-sync-failed")
		}
	}
	if err := control.Sync(); err != nil {
		return nil, fmt.Errorf("tour-v4-stage-parent-sync-failed")
	}
	staged, err := tourV4SnapshotTreeAt(control, stageName, filepath.Join(controlPath, stageName), permit, true)
	if err != nil {
		return nil, err
	}
	cleanup = false
	return staged, nil
}

func tourV4RemoveTreeAt(parent *os.File, name string) error {
	if parent == nil || !tourV4SafeEntryName(name) {
		return fmt.Errorf("tour-v4-remove-input-invalid")
	}
	directory, err := tourV4OpenDirectoryAt(parent, name)
	if err != nil {
		if errors.Is(err, syscall.ENOENT) {
			return nil
		}
		return fmt.Errorf("tour-v4-remove-tree-invalid")
	}
	entries, err := directory.ReadDir(-1)
	if err != nil {
		directory.Close()
		return fmt.Errorf("tour-v4-remove-read-failed")
	}
	for _, entry := range entries {
		childName := entry.Name()
		if !tourV4SafeEntryName(childName) {
			directory.Close()
			return fmt.Errorf("tour-v4-remove-name-invalid")
		}
		child, childErr := tourV4OpenDirectoryAt(directory, childName)
		if childErr == nil {
			child.Close()
			if err := tourV4RemoveTreeAt(directory, childName); err != nil {
				directory.Close()
				return err
			}
			continue
		}
		file, err := tourV4ReadRegularAt(directory, childName, childName, tourV4MaximumFileBytes)
		if err != nil {
			directory.Close()
			return fmt.Errorf("tour-v4-remove-entry-invalid")
		}
		zero(file.Content)
		if err := syscall.Unlinkat(int(directory.Fd()), childName); err != nil {
			directory.Close()
			return fmt.Errorf("tour-v4-remove-file-failed")
		}
	}
	if err := directory.Sync(); err != nil {
		directory.Close()
		return fmt.Errorf("tour-v4-remove-directory-sync-failed")
	}
	directory.Close()
	if err := tourV4UnlinkDirectoryAt(parent, name); err != nil {
		return fmt.Errorf("tour-v4-remove-directory-failed")
	}
	return nil
}

func tourV4UnlinkDirectoryAt(parent *os.File, name string) error {
	if parent == nil || !tourV4SafeEntryName(name) {
		return fmt.Errorf("tour-v4-unlink-directory-input-invalid")
	}
	pointer, err := syscall.BytePtrFromString(name)
	if err != nil {
		return err
	}
	_, _, errno := syscall.Syscall(
		syscall.SYS_UNLINKAT,
		uintptr(parent.Fd()),
		uintptr(unsafe.Pointer(pointer)),
		uintptr(0x200), // AT_REMOVEDIR
	)
	if errno != 0 {
		return errno
	}
	return nil
}

func tourV4RenameAt2(directory *os.File, oldName, newName string, flags uintptr) error {
	return tourV4RenameAt2Between(directory, oldName, directory, newName, flags)
}

func tourV4RenameAt2Between(oldDirectory *os.File, oldName string, newDirectory *os.File, newName string, flags uintptr) error {
	if oldDirectory == nil || newDirectory == nil ||
		!tourV4SafeEntryName(oldName) || !tourV4SafeEntryName(newName) ||
		(flags != renameNoReplace && flags != tourV4RenameExchange) {
		return fmt.Errorf("tour-v4-rename-input-invalid")
	}
	oldPointer, err := syscall.BytePtrFromString(oldName)
	if err != nil {
		return err
	}
	newPointer, err := syscall.BytePtrFromString(newName)
	if err != nil {
		return err
	}
	_, _, errno := syscall.Syscall6(
		sysRenameat2,
		uintptr(oldDirectory.Fd()), uintptr(unsafe.Pointer(oldPointer)),
		uintptr(newDirectory.Fd()), uintptr(unsafe.Pointer(newPointer)),
		flags, 0,
	)
	if errno != 0 {
		return errno
	}
	return nil
}

func tourV4OptionalTreeAt(live *os.File, livePath, name string, permit *tourV4Permit, publicOnly bool) (*tourV4TreeSnapshot, bool, error) {
	snapshot, err := tourV4SnapshotTreeAt(live, name, filepath.Join(livePath, name), permit, publicOnly)
	if err == nil {
		return snapshot, true, nil
	}
	if errors.Is(err, syscall.ENOENT) {
		return nil, false, nil
	}
	// tourV4OpenDirectoryAt preserves ENOENT, but snapshot validation wraps all
	// other states. A symlink, special file, or unreadable tree is never absence.
	fd, openErr := syscall.Openat(
		int(live.Fd()), name,
		syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0,
	)
	if openErr == syscall.ENOENT {
		return nil, false, nil
	}
	if openErr == nil {
		syscall.Close(fd)
	}
	return nil, false, err
}

func tourV4IdentityValue(snapshot *tourV4TreeSnapshot) any {
	if snapshot == nil {
		return nil
	}
	return map[string]any{
		"device": json.Number(strconv.FormatUint(snapshot.Device, 10)),
		"inode":  json.Number(strconv.FormatUint(snapshot.Inode, 10)),
	}
}

func tourV4TreeReceiptValue(snapshot *tourV4TreeSnapshot) any {
	if snapshot == nil {
		return nil
	}
	return map[string]any{
		"file_count":       json.Number(strconv.Itoa(len(snapshot.Files))),
		"root_identity":    tourV4IdentityValue(snapshot),
		"total_size_bytes": json.Number(strconv.FormatInt(snapshot.TotalBytes, 10)),
		"tree_sha256":      snapshot.TreeSHA256,
	}
}

func tourV4AuditFields(permit *tourV4Permit, manifestSHA string) map[string]any {
	return map[string]any{
		"artifact_tree_sha256":           permit.ArtifactTreeSHA256,
		"browser_evidence_tree_sha256":   permit.BrowserEvidenceTreeSHA256,
		"browser_receipt_sha256":         permit.BrowserReceiptSHA256,
		"manifest_sha256":                manifestSHA,
		"quality_receipt_sha256":         permit.QualityReceiptSHA256,
		"reconstruction_manifest_sha256": permit.ReconstructionSHA256,
		"render_commit_sha256":           permit.RenderCommitSHA256,
		"tour_manifest_sha256":           permit.TourManifestSHA256,
		"walkthrough_sha256":             permit.WalkthroughSHA256,
	}
}

func tourV4AuthorityFields(binding tourV4AuthorityBinding) map[string]any {
	if binding.Profile == tourV4DetachedProfile {
		return map[string]any{
			"authority_profile":           tourV4DetachedProfile,
			"package_authority_key_id":    binding.PackageAuthorityKeyID,
			"receipt_key_id":              binding.ReceiptKeyID,
			"source_manifest_digest":      binding.SourceManifestDigest,
			"tour_materialization_sha256": binding.TourMaterializationDigest,
		}
	}
	return map[string]any{
		"authority_profile": binding.Profile,
		"config_digest":     binding.ConfigDigest,
		"deployment_id":     binding.DeploymentID,
		"receipt_key_id":    binding.ReceiptKeyID,
		"runtime_sha":       binding.RuntimeSHA,
		"workflow_sha":      binding.WorkflowSHA,
	}
}

func tourV4AuthorityPayloadMatches(
	payload map[string]any,
	binding tourV4AuthorityBinding,
) bool {
	if payload == nil || tourV4ValidateAuthorityBinding(binding) != nil {
		return false
	}
	expected := tourV4AuthorityFields(binding)
	for key, value := range expected {
		if payload[key] != value {
			return false
		}
	}
	known := []string{
		"authority_profile", "config_digest", "deployment_id",
		"package_authority_key_id", "receipt_key_id", "runtime_sha",
		"source_manifest_digest", "tour_materialization_sha256", "workflow_sha",
	}
	for _, key := range known {
		if _, required := expected[key]; required {
			continue
		}
		if _, present := payload[key]; present {
			return false
		}
	}
	return true
}

func tourV4MergeFields(target map[string]any, source map[string]any) {
	for key, value := range source {
		target[key] = value
	}
}

func tourV4PreparedPayload(
	binding tourV4AuthorityBinding,
	input tourV4PublishInput,
	permit *tourV4Permit,
	manifestSHA string,
	source *tourV4TreeSnapshot,
	old *tourV4TreeSnapshot,
	stage *tourV4TreeSnapshot,
	stageName, rollbackName string,
) map[string]any {
	payload := map[string]any{
		"atomic_operation": func() string {
			if old == nil {
				return "renameat2-RENAME_NOREPLACE"
			}
			return "renameat2-RENAME_EXCHANGE"
		}(),
		"candidate_tree":                 tourV4TreeReceiptValue(stage),
		"canonical_live_root":            tourV4LiveVolumeRoot,
		"control_relpath":                tourV4ControlRelpath,
		"expected_old_tree_sha256":       input.ExpectedOldTreeSHA256,
		"observed_old_tree":              tourV4TreeReceiptValue(old),
		"operation":                      "publish-generated-reconstruction-v4",
		"prepared_at_epoch":              json.Number(strconv.FormatInt(tourV4Now().UTC().Unix(), 10)),
		"private_source_files_published": false,
		"public_file_count":              json.Number("21"),
		"reconstruction_kind":            permit.ReconstructionKind,
		"rollback_relpath":               rollbackName,
		"schema":                         tourV4PreparedSchema,
		"slug":                           permit.Slug,
		"source_bundle_path_sha256":      digest([]byte(source.Root)),
		"source_tree":                    tourV4TreeReceiptValue(source),
		"stage_relpath":                  stageName,
		"status":                         "prepared",
		"transaction_id":                 input.TransactionID,
		"version":                        json.Number("4"),
	}
	tourV4MergeFields(payload, tourV4AuditFields(permit, manifestSHA))
	tourV4MergeFields(payload, tourV4AuthorityFields(binding))
	return payload
}

func tourV4ParseIdentity(value any, allowNil bool) (uint64, uint64, bool, error) {
	if value == nil && allowNil {
		return 0, 0, false, nil
	}
	object, ok := value.(map[string]any)
	if !ok || !hasKeys(object, "device", "inode") {
		return 0, 0, false, fmt.Errorf("tour-v4-receipt-identity-invalid")
	}
	device, deviceOK := exactInt(object["device"], 0, 1<<62)
	inode, inodeOK := exactInt(object["inode"], 1, 1<<62)
	if !deviceOK || !inodeOK {
		return 0, 0, false, fmt.Errorf("tour-v4-receipt-identity-invalid")
	}
	return uint64(device), uint64(inode), true, nil
}

func tourV4ParseTreeReceipt(value any, allowNil bool) (string, uint64, uint64, bool, error) {
	if value == nil && allowNil {
		return "", 0, 0, false, nil
	}
	object, ok := value.(map[string]any)
	if !ok || !hasKeys(object, "file_count", "root_identity", "total_size_bytes", "tree_sha256") {
		return "", 0, 0, false, fmt.Errorf("tour-v4-receipt-tree-invalid")
	}
	treeSHA, shaOK := tourV4String(object, "tree_sha256")
	_, filesOK := exactInt(object["file_count"], 1, tourV4MaximumFiles)
	_, bytesOK := exactInt(object["total_size_bytes"], 1, tourV4MaximumTreeBytes)
	device, inode, present, identityErr := tourV4ParseIdentity(object["root_identity"], false)
	if !shaOK || !tourV4SHA256Pattern.MatchString(treeSHA) || !filesOK || !bytesOK ||
		identityErr != nil || !present {
		return "", 0, 0, false, fmt.Errorf("tour-v4-receipt-tree-invalid")
	}
	return treeSHA, device, inode, true, nil
}

func tourV4ParsePrepared(
	payload map[string]any,
	raw []byte,
	rawDigest string,
	binding tourV4AuthorityBinding,
	input tourV4PublishInput,
	permit *tourV4Permit,
	manifestSHA string,
) (*tourV4Prepared, error) {
	expectedKeys := []string{
		"artifact_tree_sha256", "atomic_operation",
		"browser_evidence_tree_sha256", "browser_receipt_sha256",
		"candidate_tree", "canonical_live_root", "control_relpath",
		"expected_old_tree_sha256", "manifest_sha256",
		"observed_old_tree", "operation", "prepared_at_epoch",
		"private_source_files_published", "public_file_count",
		"quality_receipt_sha256",
		"reconstruction_kind", "reconstruction_manifest_sha256",
		"render_commit_sha256", "rollback_relpath",
		"schema", "slug", "source_bundle_path_sha256", "source_tree",
		"stage_relpath", "status", "tour_manifest_sha256",
		"transaction_id", "version", "walkthrough_sha256",
	}
	for key := range tourV4AuthorityFields(binding) {
		expectedKeys = append(expectedKeys, key)
	}
	if payload == nil || !hasKeys(payload, expectedKeys...) ||
		payload["schema"] != tourV4PreparedSchema || payload["version"] != json.Number("4") ||
		payload["status"] != "prepared" ||
		payload["operation"] != "publish-generated-reconstruction-v4" ||
		!tourV4AuthorityPayloadMatches(payload, binding) ||
		payload["canonical_live_root"] != tourV4LiveVolumeRoot ||
		payload["control_relpath"] != tourV4ControlRelpath ||
		payload["transaction_id"] != input.TransactionID ||
		payload["expected_old_tree_sha256"] != input.ExpectedOldTreeSHA256 ||
		payload["manifest_sha256"] != manifestSHA ||
		payload["slug"] != permit.Slug ||
		payload["reconstruction_kind"] != permit.ReconstructionKind ||
		payload["artifact_tree_sha256"] != permit.ArtifactTreeSHA256 ||
		payload["browser_receipt_sha256"] != permit.BrowserReceiptSHA256 ||
		payload["browser_evidence_tree_sha256"] != permit.BrowserEvidenceTreeSHA256 ||
		payload["quality_receipt_sha256"] != permit.QualityReceiptSHA256 ||
		payload["walkthrough_sha256"] != permit.WalkthroughSHA256 ||
		payload["tour_manifest_sha256"] != permit.TourManifestSHA256 ||
		payload["reconstruction_manifest_sha256"] != permit.ReconstructionSHA256 ||
		payload["render_commit_sha256"] != permit.RenderCommitSHA256 ||
		payload["private_source_files_published"] != false {
		return nil, fmt.Errorf("tour-v4-prepared-receipt-binding-invalid")
	}
	publicCount, publicOK := exactInt(payload["public_file_count"], 21, 21)
	preparedAt, preparedOK := exactInt(payload["prepared_at_epoch"], 1, 1<<62)
	sourceSHA, _, _, sourcePresent, sourceErr := tourV4ParseTreeReceipt(payload["source_tree"], false)
	candidateSHA, candidateDevice, candidateInode, candidatePresent, candidateErr := tourV4ParseTreeReceipt(payload["candidate_tree"], false)
	oldSHA, oldDevice, oldInode, oldPresent, oldErr := tourV4ParseTreeReceipt(payload["observed_old_tree"], true)
	stageName, stageOK := tourV4String(payload, "stage_relpath")
	rollbackName, rollbackOK := tourV4String(payload, "rollback_relpath")
	sourcePathDigest, sourcePathOK := tourV4String(payload, "source_bundle_path_sha256")
	atomicOperation, atomicOK := tourV4String(payload, "atomic_operation")
	if !publicOK || publicCount != 21 || !preparedOK || preparedAt < 1 ||
		sourceErr != nil || !sourcePresent || sourceSHA != permit.ArtifactTreeSHA256 ||
		candidateErr != nil || !candidatePresent || candidateSHA != permit.PublicTreeSHA256 ||
		oldErr != nil || !stageOK || !rollbackOK ||
		!tourV4SafeEntryName(stageName) || !tourV4SafeEntryName(rollbackName) ||
		!sourcePathOK || !digestPattern.MatchString(sourcePathDigest) ||
		!atomicOK {
		return nil, fmt.Errorf("tour-v4-prepared-receipt-content-invalid")
	}
	oldWasAbsent := input.ExpectedOldTreeSHA256 == tourV4AbsentSentinel
	if oldWasAbsent {
		if oldPresent || oldSHA != "" || atomicOperation != "renameat2-RENAME_NOREPLACE" {
			return nil, fmt.Errorf("tour-v4-prepared-receipt-old-tree-invalid")
		}
	} else if !oldPresent || oldSHA != input.ExpectedOldTreeSHA256 ||
		atomicOperation != "renameat2-RENAME_EXCHANGE" {
		return nil, fmt.Errorf("tour-v4-prepared-receipt-old-tree-invalid")
	}
	return &tourV4Prepared{
		TransactionID: input.TransactionID, ManifestSHA256: manifestSHA,
		PreparedReceiptSHA256: rawDigest,
		ExpectedOldTreeSHA256: input.ExpectedOldTreeSHA256,
		ObservedOldTreeSHA256: oldSHA,
		CandidateTreeSHA256:   candidateSHA,
		ArtifactTreeSHA256:    sourceSHA,
		Slug:                  permit.Slug, StageRelpath: stageName, RollbackRelpath: rollbackName,
		OldDevice: oldDevice, OldInode: oldInode,
		CandidateDevice: candidateDevice, CandidateInode: candidateInode,
		OldWasAbsent: oldWasAbsent, Raw: append([]byte(nil), raw...),
	}, nil
}

func tourV4SnapshotMatches(snapshot *tourV4TreeSnapshot, sha string, device, inode uint64) bool {
	return snapshot != nil && snapshot.TreeSHA256 == sha &&
		snapshot.Device == device && snapshot.Inode == inode
}

func tourV4TerminalPayload(
	binding tourV4AuthorityBinding,
	permit *tourV4Permit,
	prepared *tourV4Prepared,
	live, retained *tourV4TreeSnapshot,
) map[string]any {
	payload := map[string]any{
		"atomic_operation": func() string {
			if prepared.OldWasAbsent {
				return "renameat2-RENAME_NOREPLACE"
			}
			return "renameat2-RENAME_EXCHANGE"
		}(),
		"canonical_live_root":            tourV4LiveVolumeRoot,
		"expected_old_tree_sha256":       prepared.ExpectedOldTreeSHA256,
		"live_tree":                      tourV4TreeReceiptValue(live),
		"operation":                      "publish-generated-reconstruction-v4",
		"prepared_receipt_sha256":        prepared.PreparedReceiptSHA256,
		"private_source_files_published": false,
		"public_file_count":              json.Number("21"),
		"reconstruction_kind":            permit.ReconstructionKind,
		"retained_rollback_relpath": func() any {
			if retained == nil {
				return nil
			}
			return tourV4ControlRelpath + "/" + prepared.RollbackRelpath
		}(),
		"retained_rollback_tree": tourV4TreeReceiptValue(retained),
		"schema":                 tourV4TerminalSchema,
		"slug":                   permit.Slug,
		"status":                 "succeeded",
		"terminal_at_epoch":      json.Number(strconv.FormatInt(tourV4Now().UTC().Unix(), 10)),
		"transaction_id":         prepared.TransactionID,
		"version":                json.Number("4"),
	}
	tourV4MergeFields(payload, tourV4AuditFields(permit, prepared.ManifestSHA256))
	tourV4MergeFields(payload, tourV4AuthorityFields(binding))
	return payload
}

func tourV4ValidateTerminalPayload(
	payload map[string]any,
	binding tourV4AuthorityBinding,
	permit *tourV4Permit,
	prepared *tourV4Prepared,
) error {
	if payload == nil || payload["schema"] != tourV4TerminalSchema ||
		payload["version"] != json.Number("4") || payload["status"] != "succeeded" ||
		payload["operation"] != "publish-generated-reconstruction-v4" ||
		payload["transaction_id"] != prepared.TransactionID ||
		payload["slug"] != permit.Slug ||
		payload["manifest_sha256"] != prepared.ManifestSHA256 ||
		payload["prepared_receipt_sha256"] != prepared.PreparedReceiptSHA256 ||
		payload["expected_old_tree_sha256"] != prepared.ExpectedOldTreeSHA256 ||
		!tourV4AuthorityPayloadMatches(payload, binding) ||
		payload["artifact_tree_sha256"] != permit.ArtifactTreeSHA256 ||
		payload["browser_receipt_sha256"] != permit.BrowserReceiptSHA256 ||
		payload["browser_evidence_tree_sha256"] != permit.BrowserEvidenceTreeSHA256 ||
		payload["quality_receipt_sha256"] != permit.QualityReceiptSHA256 ||
		payload["walkthrough_sha256"] != permit.WalkthroughSHA256 ||
		payload["private_source_files_published"] != false {
		return fmt.Errorf("tour-v4-terminal-receipt-binding-invalid")
	}
	liveSHA, liveDevice, liveInode, livePresent, err := tourV4ParseTreeReceipt(payload["live_tree"], false)
	if err != nil || !livePresent || liveSHA != permit.PublicTreeSHA256 ||
		liveDevice != prepared.CandidateDevice || liveInode != prepared.CandidateInode {
		return fmt.Errorf("tour-v4-terminal-live-tree-invalid")
	}
	retainedSHA, retainedDevice, retainedInode, retainedPresent, retainedErr := tourV4ParseTreeReceipt(payload["retained_rollback_tree"], true)
	if prepared.OldWasAbsent {
		if retainedErr != nil || retainedPresent || payload["retained_rollback_relpath"] != nil {
			return fmt.Errorf("tour-v4-terminal-rollback-tree-invalid")
		}
	} else if retainedErr != nil || !retainedPresent ||
		retainedSHA != prepared.ObservedOldTreeSHA256 ||
		retainedDevice != prepared.OldDevice || retainedInode != prepared.OldInode ||
		payload["retained_rollback_relpath"] != tourV4ControlRelpath+"/"+prepared.RollbackRelpath {
		return fmt.Errorf("tour-v4-terminal-rollback-tree-invalid")
	}
	return nil
}

func tourV4ReadPrepared(
	root string,
	receipts *os.File,
	public ed25519.PublicKey,
	binding tourV4AuthorityBinding,
	input tourV4PublishInput,
	permit *tourV4Permit,
	manifestSHA string,
) (*tourV4Prepared, error) {
	preparedName, _, _, err := tourV4ReceiptNames(input.TransactionID)
	if err != nil {
		return nil, err
	}
	payload, raw, rawDigest, err := tourV4ReadSignedReceipt(root, receipts, preparedName, public)
	if err != nil {
		return nil, err
	}
	prepared, err := tourV4ParsePrepared(payload, raw, rawDigest, binding, input, permit, manifestSHA)
	zero(raw)
	return prepared, err
}

func tourV4ReadTerminal(
	root string,
	receipts *os.File,
	public ed25519.PublicKey,
	binding tourV4AuthorityBinding,
	permit *tourV4Permit,
	prepared *tourV4Prepared,
) ([]byte, bool, error) {
	_, terminalName, _, err := tourV4ReceiptNames(prepared.TransactionID)
	if err != nil {
		return nil, false, err
	}
	payload, raw, _, err := tourV4ReadSignedReceipt(root, receipts, terminalName, public)
	if errors.Is(err, syscall.ENOENT) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	if err := tourV4ValidateTerminalPayload(payload, binding, permit, prepared); err != nil {
		zero(raw)
		return nil, false, err
	}
	return raw, true, nil
}

func tourV4Finalize(
	root string,
	receipts *os.File,
	binding tourV4AuthorityBinding,
	key ed25519.PrivateKey,
	permit *tourV4Permit,
	prepared *tourV4Prepared,
	live, retained *tourV4TreeSnapshot,
) ([]byte, error) {
	if raw, exists, err := tourV4ReadTerminal(
		root, receipts, key.Public().(ed25519.PublicKey), binding, permit, prepared,
	); err != nil {
		return nil, err
	} else if exists {
		return raw, nil
	}
	_, terminalName, _, err := tourV4ReceiptNames(prepared.TransactionID)
	if err != nil {
		return nil, err
	}
	payload := tourV4TerminalPayload(binding, permit, prepared, live, retained)
	wire, _, err := tourV4WriteSignedReceipt(root, receipts, terminalName, payload, key)
	if err != nil {
		return nil, err
	}
	storedPayload, storedRaw, _, err := tourV4ReadSignedReceipt(
		root, receipts, terminalName, key.Public().(ed25519.PublicKey),
	)
	if err != nil || !bytes.Equal(storedRaw, wire) {
		zero(wire)
		zero(storedRaw)
		return nil, fmt.Errorf("tour-v4-terminal-receipt-postpublish-invalid")
	}
	zero(wire)
	if err := tourV4ValidateTerminalPayload(storedPayload, binding, permit, prepared); err != nil {
		zero(storedRaw)
		return nil, err
	}
	return storedRaw, nil
}

func tourV4ResumePrepared(
	root string,
	live *os.File,
	livePath string,
	control *os.File,
	controlPath string,
	receipts *os.File,
	binding tourV4AuthorityBinding,
	key ed25519.PrivateKey,
	permit *tourV4Permit,
	prepared *tourV4Prepared,
) ([]byte, error) {
	if !tourV4ControlRootStillBound(live, control) {
		return nil, fmt.Errorf("tour-v4-control-root-drift-detected")
	}
	if raw, exists, err := tourV4ReadTerminal(
		root, receipts, key.Public().(ed25519.PublicKey), binding, permit, prepared,
	); err != nil {
		return nil, err
	} else if exists {
		return raw, nil
	}
	current, currentExists, currentErr := tourV4OptionalTreeAt(live, livePath, permit.Slug, nil, false)
	if currentErr != nil {
		return nil, currentErr
	}
	if current != nil {
		defer current.release()
	}
	stage, stageExists, stageErr := tourV4OptionalTreeAt(control, controlPath, prepared.StageRelpath, nil, false)
	if stageErr != nil {
		return nil, stageErr
	}
	if stage != nil {
		defer stage.release()
	}
	rollback, rollbackExists, rollbackErr := tourV4OptionalTreeAt(control, controlPath, prepared.RollbackRelpath, nil, false)
	if rollbackErr != nil {
		return nil, rollbackErr
	}
	if rollback != nil {
		defer rollback.release()
	}
	currentCandidate := currentExists && tourV4SnapshotMatches(
		current, prepared.CandidateTreeSHA256, prepared.CandidateDevice, prepared.CandidateInode,
	)
	stageCandidate := stageExists && tourV4SnapshotMatches(
		stage, prepared.CandidateTreeSHA256, prepared.CandidateDevice, prepared.CandidateInode,
	)
	currentOld := currentExists && !prepared.OldWasAbsent && tourV4SnapshotMatches(
		current, prepared.ObservedOldTreeSHA256, prepared.OldDevice, prepared.OldInode,
	)
	stageOld := stageExists && !prepared.OldWasAbsent && tourV4SnapshotMatches(
		stage, prepared.ObservedOldTreeSHA256, prepared.OldDevice, prepared.OldInode,
	)
	rollbackOld := rollbackExists && !prepared.OldWasAbsent && tourV4SnapshotMatches(
		rollback, prepared.ObservedOldTreeSHA256, prepared.OldDevice, prepared.OldInode,
	)

	exchangePerformed := false
	switch {
	case prepared.OldWasAbsent && !currentExists && stageCandidate && !rollbackExists:
		if err := tourV4RenameAt2Between(control, prepared.StageRelpath, live, permit.Slug, renameNoReplace); err != nil {
			return nil, fmt.Errorf("tour-v4-first-publication-exchange-failed")
		}
		if err := control.Sync(); err != nil || live.Sync() != nil {
			return nil, fmt.Errorf("tour-v4-first-publication-durability-unknown")
		}
		exchangePerformed = true
	case prepared.OldWasAbsent && currentCandidate && !stageExists && !rollbackExists:
		// The no-replace publication happened before an interruption.
	case !prepared.OldWasAbsent && currentOld && stageCandidate && !rollbackExists:
		if err := tourV4RenameAt2Between(control, prepared.StageRelpath, live, permit.Slug, tourV4RenameExchange); err != nil {
			return nil, fmt.Errorf("tour-v4-replacement-exchange-failed")
		}
		if err := control.Sync(); err != nil || live.Sync() != nil {
			return nil, fmt.Errorf("tour-v4-replacement-durability-unknown")
		}
		exchangePerformed = true
	case !prepared.OldWasAbsent && currentCandidate && stageOld && !rollbackExists:
		// The exchange happened; normalize the retained old tree below.
	case !prepared.OldWasAbsent && currentCandidate && !stageExists && rollbackOld:
		// The exchange and rollback-name normalization both happened.
	default:
		return nil, fmt.Errorf("tour-v4-recovery-state-ambiguous")
	}
	if exchangePerformed && tourV4BeforeControlBindingCheck != nil {
		tourV4BeforeControlBindingCheck()
	}
	if exchangePerformed && !tourV4ControlRootStillBound(live, control) {
		var rollbackErr error
		if prepared.OldWasAbsent {
			rollbackErr = tourV4RenameAt2Between(live, permit.Slug, control, prepared.StageRelpath, renameNoReplace)
		} else {
			rollbackErr = tourV4RenameAt2Between(control, prepared.StageRelpath, live, permit.Slug, tourV4RenameExchange)
		}
		if rollbackErr != nil || control.Sync() != nil || live.Sync() != nil {
			return nil, fmt.Errorf("tour-v4-control-root-drift-rollback-ambiguous")
		}
		return nil, fmt.Errorf("tour-v4-control-root-drift-detected")
	}
	if exchangePerformed && tourV4AfterExchangeHook != nil {
		if err := tourV4AfterExchangeHook(); err != nil {
			return nil, fmt.Errorf("tour-v4-post-exchange-interrupted")
		}
	}

	// Refresh all paths after a possible exchange.
	if current != nil {
		current.release()
		current = nil
	}
	if stage != nil {
		stage.release()
		stage = nil
	}
	if rollback != nil {
		rollback.release()
		rollback = nil
	}
	current, currentExists, currentErr = tourV4OptionalTreeAt(live, livePath, permit.Slug, permit, true)
	if currentErr != nil || !currentExists ||
		!tourV4SnapshotMatches(current, prepared.CandidateTreeSHA256, prepared.CandidateDevice, prepared.CandidateInode) {
		if current != nil {
			current.release()
		}
		return nil, fmt.Errorf("tour-v4-live-postcondition-invalid")
	}
	defer current.release()

	if !prepared.OldWasAbsent {
		stage, stageExists, stageErr = tourV4OptionalTreeAt(control, controlPath, prepared.StageRelpath, nil, false)
		if stageErr != nil {
			return nil, stageErr
		}
		if stageExists {
			if !tourV4SnapshotMatches(stage, prepared.ObservedOldTreeSHA256, prepared.OldDevice, prepared.OldInode) {
				stage.release()
				return nil, fmt.Errorf("tour-v4-retained-old-tree-invalid")
			}
			stage.release()
			if err := tourV4RenameAt2(control, prepared.StageRelpath, prepared.RollbackRelpath, renameNoReplace); err != nil {
				return nil, fmt.Errorf("tour-v4-rollback-retention-publish-failed")
			}
			if err := control.Sync(); err != nil {
				return nil, fmt.Errorf("tour-v4-rollback-retention-durability-unknown")
			}
		}
		rollback, rollbackExists, rollbackErr = tourV4OptionalTreeAt(control, controlPath, prepared.RollbackRelpath, nil, false)
		if rollbackErr != nil || !rollbackExists ||
			!tourV4SnapshotMatches(rollback, prepared.ObservedOldTreeSHA256, prepared.OldDevice, prepared.OldInode) {
			if rollback != nil {
				rollback.release()
			}
			return nil, fmt.Errorf("tour-v4-rollback-retention-invalid")
		}
		defer rollback.release()
	} else {
		_, stageExists, stageErr = tourV4OptionalTreeAt(control, controlPath, prepared.StageRelpath, nil, false)
		_, rollbackExists, rollbackErr = tourV4OptionalTreeAt(control, controlPath, prepared.RollbackRelpath, nil, false)
		if stageErr != nil || rollbackErr != nil || stageExists || rollbackExists {
			return nil, fmt.Errorf("tour-v4-first-publication-residue-invalid")
		}
	}
	if !tourV4ControlRootStillBound(live, control) {
		return nil, fmt.Errorf("tour-v4-control-root-drift-detected")
	}
	return tourV4Finalize(root, receipts, binding, key, permit, prepared, current, rollback)
}

func tourV4ValidatePublishInput(input tourV4PublishInput) error {
	if !filepath.IsAbs(input.BundlePath) || filepath.Clean(input.BundlePath) != input.BundlePath ||
		!tourV4TransactionPattern.MatchString(input.TransactionID) ||
		!tourV4SHA256Pattern.MatchString(input.ExpectedManifestSHA256) ||
		(input.ExpectedOldTreeSHA256 != tourV4AbsentSentinel &&
			!tourV4SHA256Pattern.MatchString(input.ExpectedOldTreeSHA256)) {
		return fmt.Errorf("tour-v4-publish-input-invalid")
	}
	return nil
}

func tourV4Publish(
	root string,
	binding tourV4AuthorityBinding,
	key ed25519.PrivateKey,
	input tourV4PublishInput,
) ([]byte, error) {
	if err := tourV4ValidateAuthorityBinding(binding); err != nil {
		return nil, err
	}
	if len(key) != ed25519.PrivateKeySize {
		return nil, fmt.Errorf("tour-v4-receipt-key-invalid")
	}
	if err := tourV4ValidatePublishInput(input); err != nil {
		return nil, err
	}
	permit, manifestSHA, err := tourV4PermitByManifestDigest(input.ExpectedManifestSHA256)
	if err != nil {
		return nil, err
	}
	source, err := tourV4SnapshotTree(input.BundlePath, permit, false)
	if err != nil {
		return nil, err
	}
	defer source.release()
	if err := tourV4ValidateArtifact(source, permit); err != nil {
		return nil, err
	}

	receipts, err := tourV4OpenReceiptRoot(root)
	if err != nil {
		return nil, err
	}
	defer receipts.Close()
	lock, err := tourV4AcquireLock(receipts, root)
	if err != nil {
		return nil, err
	}
	defer tourV4ReleaseLock(lock)
	live, err := tourV4OpenLiveRoot(root)
	if err != nil {
		return nil, err
	}
	defer live.Close()
	livePath := tourV4RootPath(root, tourV4LiveVolumeRoot)
	control, err := tourV4OpenControlRoot(root, live)
	if err != nil {
		return nil, err
	}
	defer control.Close()
	controlPath := filepath.Join(livePath, tourV4ControlRelpath)

	preparedName, _, _, err := tourV4ReceiptNames(input.TransactionID)
	if err != nil {
		return nil, err
	}
	if prepared, readErr := tourV4ReadPrepared(
		root, receipts, key.Public().(ed25519.PublicKey), binding,
		input, permit, manifestSHA,
	); readErr == nil {
		defer prepared.release()
		return tourV4ResumePrepared(root, live, livePath, control, controlPath, receipts, binding, key, permit, prepared)
	} else if !errors.Is(readErr, syscall.ENOENT) {
		return nil, readErr
	}

	stageName := "stage-v4-" + input.TransactionID
	rollbackName := "rollback-v4-" + input.TransactionID
	if !tourV4SafeEntryName(stageName) || !tourV4SafeEntryName(rollbackName) {
		return nil, fmt.Errorf("tour-v4-control-name-invalid")
	}
	stage, err := tourV4CreateStage(root, control, controlPath, stageName, source, permit)
	if err != nil {
		return nil, err
	}
	defer stage.release()

	sourceAgain, err := tourV4SnapshotTree(input.BundlePath, permit, false)
	if err != nil {
		_ = tourV4RemoveTreeAt(control, stageName)
		_ = control.Sync()
		return nil, err
	}
	if sourceAgain.Device != source.Device || sourceAgain.Inode != source.Inode ||
		sourceAgain.TreeSHA256 != source.TreeSHA256 ||
		sourceAgain.MtimeNS != source.MtimeNS || sourceAgain.CtimeNS != source.CtimeNS {
		sourceAgain.release()
		_ = tourV4RemoveTreeAt(control, stageName)
		_ = control.Sync()
		return nil, fmt.Errorf("tour-v4-source-drift-detected")
	}
	sourceAgain.release()

	old, oldExists, err := tourV4OptionalTreeAt(live, livePath, permit.Slug, nil, false)
	if err != nil {
		_ = tourV4RemoveTreeAt(control, stageName)
		_ = control.Sync()
		return nil, err
	}
	if old != nil {
		defer old.release()
	}
	if input.ExpectedOldTreeSHA256 == tourV4AbsentSentinel {
		if oldExists {
			_ = tourV4RemoveTreeAt(control, stageName)
			_ = control.Sync()
			return nil, fmt.Errorf("tour-v4-cas-expected-absent")
		}
	} else if !oldExists || old.TreeSHA256 != input.ExpectedOldTreeSHA256 {
		_ = tourV4RemoveTreeAt(control, stageName)
		_ = control.Sync()
		return nil, fmt.Errorf("tour-v4-cas-old-tree-mismatch")
	}
	payload := tourV4PreparedPayload(
		binding, input, permit, manifestSHA, source, old, stage, stageName, rollbackName,
	)
	wire, _, err := tourV4WriteSignedReceipt(root, receipts, preparedName, payload, key)
	if err != nil {
		_ = tourV4RemoveTreeAt(control, stageName)
		_ = control.Sync()
		return nil, err
	}
	zero(wire)
	prepared, err := tourV4ReadPrepared(
		root, receipts, key.Public().(ed25519.PublicKey), binding,
		input, permit, manifestSHA,
	)
	if err != nil {
		return nil, err
	}
	defer prepared.release()
	return tourV4ResumePrepared(root, live, livePath, control, controlPath, receipts, binding, key, permit, prepared)
}

func tourV4Inspect(
	root string,
	binding tourV4AuthorityBinding,
	key ed25519.PrivateKey,
	expectedManifest string,
) ([]byte, error) {
	if err := tourV4ValidateAuthorityBinding(binding); err != nil {
		return nil, err
	}
	if len(key) != ed25519.PrivateKeySize ||
		!tourV4SHA256Pattern.MatchString(expectedManifest) {
		return nil, fmt.Errorf("tour-v4-inspection-input-invalid")
	}
	permit, manifestSHA, err := tourV4PermitByManifestDigest(expectedManifest)
	if err != nil {
		return nil, err
	}
	live, err := tourV4OpenLiveRoot(root)
	if err != nil {
		return nil, err
	}
	defer live.Close()
	livePath := tourV4RootPath(root, tourV4LiveVolumeRoot)
	current, exists, err := tourV4OptionalTreeAt(live, livePath, permit.Slug, nil, false)
	if err != nil {
		return nil, err
	}
	if current != nil {
		defer current.release()
	}
	expectedOld := tourV4AbsentSentinel
	if exists {
		expectedOld = current.TreeSHA256
	}
	payload := map[string]any{
		"authoritative":              true,
		"canonical_live_root":        tourV4LiveVolumeRoot,
		"expected_old_tree_argument": expectedOld,
		"inspected_at_epoch":         json.Number(strconv.FormatInt(tourV4Now().UTC().Unix(), 10)),
		"live_tree":                  tourV4TreeReceiptValue(current),
		"operation":                  "inspect-generated-reconstruction-v4",
		"performs_release_effects":   false,
		"schema":                     tourV4InspectionSchema,
		"slug":                       permit.Slug,
		"version":                    json.Number("4"),
	}
	tourV4MergeFields(payload, tourV4AuditFields(permit, manifestSHA))
	tourV4MergeFields(payload, tourV4AuthorityFields(binding))
	wire, err := signReceipt(payload, key)
	if err != nil {
		return nil, err
	}
	verified, canonical, err := verifySignedReceiptPayload(wire, key.Public().(ed25519.PublicKey))
	zero(canonical)
	if err != nil || verified["expected_old_tree_argument"] != expectedOld ||
		verified["manifest_sha256"] != manifestSHA {
		zero(wire)
		return nil, fmt.Errorf("tour-v4-inspection-self-verification-failed")
	}
	return wire, nil
}

func tourV4Recover(
	root string,
	binding tourV4AuthorityBinding,
	key ed25519.PrivateKey,
	expectedManifest, expectedOld, transactionID string,
) ([]byte, error) {
	input := tourV4PublishInput{
		BundlePath: "/", ExpectedOldTreeSHA256: expectedOld,
		ExpectedManifestSHA256: expectedManifest, TransactionID: transactionID,
	}
	if err := tourV4ValidateAuthorityBinding(binding); err != nil {
		return nil, err
	}
	if len(key) != ed25519.PrivateKeySize ||
		!tourV4TransactionPattern.MatchString(transactionID) ||
		!tourV4SHA256Pattern.MatchString(expectedManifest) ||
		(expectedOld != tourV4AbsentSentinel && !tourV4SHA256Pattern.MatchString(expectedOld)) {
		return nil, fmt.Errorf("tour-v4-recovery-input-invalid")
	}
	permit, manifestSHA, err := tourV4PermitByManifestDigest(expectedManifest)
	if err != nil {
		return nil, err
	}
	receipts, err := tourV4OpenReceiptRoot(root)
	if err != nil {
		return nil, err
	}
	defer receipts.Close()
	lock, err := tourV4AcquireLock(receipts, root)
	if err != nil {
		return nil, err
	}
	defer tourV4ReleaseLock(lock)
	live, err := tourV4OpenLiveRoot(root)
	if err != nil {
		return nil, err
	}
	defer live.Close()
	control, err := tourV4OpenControlRoot(root, live)
	if err != nil {
		return nil, err
	}
	defer control.Close()
	livePath := tourV4RootPath(root, tourV4LiveVolumeRoot)
	prepared, err := tourV4ReadPrepared(
		root, receipts, key.Public().(ed25519.PublicKey), binding,
		input, permit, manifestSHA,
	)
	if err != nil {
		return nil, err
	}
	defer prepared.release()
	return tourV4ResumePrepared(
		root, live, livePath, control, filepath.Join(livePath, tourV4ControlRelpath),
		receipts, binding, key, permit, prepared,
	)
}

func tourV4RollbackReceiptNames(transactionID string) (string, string, error) {
	if !tourV4TransactionPattern.MatchString(transactionID) {
		return "", "", fmt.Errorf("tour-v4-transaction-id-invalid")
	}
	base := "tour-v4-" + transactionID
	return base + ".rollback-prepared.json", base + ".rollback-terminal.json", nil
}

func tourV4RollbackPayload(
	status string,
	binding tourV4AuthorityBinding,
	permit *tourV4Permit,
	prepared *tourV4Prepared,
	publicationReceiptSHA string,
	live, retained *tourV4TreeSnapshot,
) map[string]any {
	payload := map[string]any{
		"atomic_operation":                    "renameat2-RENAME_EXCHANGE",
		"canonical_live_root":                 tourV4LiveVolumeRoot,
		"expected_current_tree_sha256":        permit.PublicTreeSHA256,
		"live_tree":                           tourV4TreeReceiptValue(live),
		"operation":                           "rollback-generated-reconstruction-v4",
		"prepared_publication_receipt_sha256": prepared.PreparedReceiptSHA256,
		"publication_receipt_sha256":          publicationReceiptSHA,
		"published_tree_retained_relpath":     tourV4ControlRelpath + "/" + prepared.RollbackRelpath,
		"retained_tree":                       tourV4TreeReceiptValue(retained),
		"schema":                              tourV4RollbackSchema,
		"slug":                                permit.Slug,
		"status":                              status,
		"transaction_id":                      prepared.TransactionID,
		"version":                             json.Number("4"),
	}
	if status == "prepared" {
		payload["prepared_at_epoch"] = json.Number(strconv.FormatInt(tourV4Now().UTC().Unix(), 10))
	} else {
		payload["terminal_at_epoch"] = json.Number(strconv.FormatInt(tourV4Now().UTC().Unix(), 10))
	}
	tourV4MergeFields(payload, tourV4AuditFields(permit, prepared.ManifestSHA256))
	tourV4MergeFields(payload, tourV4AuthorityFields(binding))
	return payload
}

func tourV4ValidateRollbackPayload(
	payload map[string]any,
	status string,
	binding tourV4AuthorityBinding,
	permit *tourV4Permit,
	prepared *tourV4Prepared,
	publicationReceiptSHA string,
) error {
	if payload == nil || payload["schema"] != tourV4RollbackSchema ||
		payload["version"] != json.Number("4") || payload["status"] != status ||
		payload["operation"] != "rollback-generated-reconstruction-v4" ||
		payload["transaction_id"] != prepared.TransactionID ||
		payload["slug"] != permit.Slug ||
		payload["manifest_sha256"] != prepared.ManifestSHA256 ||
		payload["prepared_publication_receipt_sha256"] != prepared.PreparedReceiptSHA256 ||
		payload["publication_receipt_sha256"] != publicationReceiptSHA ||
		payload["expected_current_tree_sha256"] != permit.PublicTreeSHA256 ||
		payload["published_tree_retained_relpath"] != tourV4ControlRelpath+"/"+prepared.RollbackRelpath ||
		!tourV4AuthorityPayloadMatches(payload, binding) {
		return fmt.Errorf("tour-v4-rollback-receipt-binding-invalid")
	}
	return nil
}

func tourV4Rollback(
	root string,
	binding tourV4AuthorityBinding,
	key ed25519.PrivateKey,
	expectedManifest, expectedOld, expectedCurrent, transactionID string,
) ([]byte, error) {
	if err := tourV4ValidateAuthorityBinding(binding); err != nil {
		return nil, err
	}
	if len(key) != ed25519.PrivateKeySize ||
		!tourV4TransactionPattern.MatchString(transactionID) ||
		expectedOld == tourV4AbsentSentinel ||
		!tourV4SHA256Pattern.MatchString(expectedOld) ||
		!tourV4SHA256Pattern.MatchString(expectedManifest) ||
		!tourV4SHA256Pattern.MatchString(expectedCurrent) {
		return nil, fmt.Errorf("tour-v4-rollback-input-invalid")
	}
	permit, manifestSHA, err := tourV4PermitByManifestDigest(expectedManifest)
	if err != nil {
		return nil, err
	}
	if expectedCurrent != permit.PublicTreeSHA256 {
		return nil, fmt.Errorf("tour-v4-rollback-current-cas-invalid")
	}
	input := tourV4PublishInput{
		BundlePath: "/", ExpectedOldTreeSHA256: expectedOld,
		ExpectedManifestSHA256: expectedManifest, TransactionID: transactionID,
	}
	receipts, err := tourV4OpenReceiptRoot(root)
	if err != nil {
		return nil, err
	}
	defer receipts.Close()
	lock, err := tourV4AcquireLock(receipts, root)
	if err != nil {
		return nil, err
	}
	defer tourV4ReleaseLock(lock)
	live, err := tourV4OpenLiveRoot(root)
	if err != nil {
		return nil, err
	}
	defer live.Close()
	livePath := tourV4RootPath(root, tourV4LiveVolumeRoot)
	control, err := tourV4OpenControlRoot(root, live)
	if err != nil {
		return nil, err
	}
	defer control.Close()
	controlPath := filepath.Join(livePath, tourV4ControlRelpath)
	if !tourV4ControlRootStillBound(live, control) {
		return nil, fmt.Errorf("tour-v4-control-root-drift-detected")
	}
	prepared, err := tourV4ReadPrepared(
		root, receipts, key.Public().(ed25519.PublicKey), binding,
		input, permit, manifestSHA,
	)
	if err != nil {
		return nil, err
	}
	defer prepared.release()
	terminalPayload, terminalRaw, publicationSHA, err := func() (map[string]any, []byte, string, error) {
		_, terminalName, _, nameErr := tourV4ReceiptNames(transactionID)
		if nameErr != nil {
			return nil, nil, "", nameErr
		}
		return tourV4ReadSignedReceipt(root, receipts, terminalName, key.Public().(ed25519.PublicKey))
	}()
	if err != nil {
		return nil, err
	}
	defer zero(terminalRaw)
	if err := tourV4ValidateTerminalPayload(terminalPayload, binding, permit, prepared); err != nil {
		return nil, err
	}
	rollbackPreparedName, rollbackTerminalName, err := tourV4RollbackReceiptNames(transactionID)
	if err != nil {
		return nil, err
	}
	if payload, raw, _, readErr := tourV4ReadSignedReceipt(
		root, receipts, rollbackTerminalName, key.Public().(ed25519.PublicKey),
	); readErr == nil {
		defer zero(raw)
		if err := tourV4ValidateRollbackPayload(payload, "succeeded", binding, permit, prepared, publicationSHA); err != nil {
			return nil, err
		}
		return append([]byte(nil), raw...), nil
	} else if !errors.Is(readErr, syscall.ENOENT) {
		return nil, readErr
	}

	current, currentExists, err := tourV4OptionalTreeAt(live, livePath, permit.Slug, nil, false)
	if err != nil {
		return nil, err
	}
	if current != nil {
		defer current.release()
	}
	retained, retainedExists, err := tourV4OptionalTreeAt(control, controlPath, prepared.RollbackRelpath, nil, false)
	if err != nil {
		return nil, err
	}
	if retained != nil {
		defer retained.release()
	}
	currentCandidate := currentExists && tourV4SnapshotMatches(
		current, permit.PublicTreeSHA256, prepared.CandidateDevice, prepared.CandidateInode,
	)
	retainedOld := retainedExists && tourV4SnapshotMatches(
		retained, prepared.ObservedOldTreeSHA256, prepared.OldDevice, prepared.OldInode,
	)
	currentOld := currentExists && tourV4SnapshotMatches(
		current, prepared.ObservedOldTreeSHA256, prepared.OldDevice, prepared.OldInode,
	)
	retainedCandidate := retainedExists && tourV4SnapshotMatches(
		retained, permit.PublicTreeSHA256, prepared.CandidateDevice, prepared.CandidateInode,
	)
	if !((currentCandidate && retainedOld) || (currentOld && retainedCandidate)) {
		return nil, fmt.Errorf("tour-v4-rollback-state-ambiguous")
	}
	if payload, raw, _, readErr := tourV4ReadSignedReceipt(
		root, receipts, rollbackPreparedName, key.Public().(ed25519.PublicKey),
	); readErr == nil {
		defer zero(raw)
		if err := tourV4ValidateRollbackPayload(payload, "prepared", binding, permit, prepared, publicationSHA); err != nil {
			return nil, err
		}
	} else if errors.Is(readErr, syscall.ENOENT) {
		payload := tourV4RollbackPayload(
			"prepared", binding, permit, prepared, publicationSHA, current, retained,
		)
		wire, _, err := tourV4WriteSignedReceipt(root, receipts, rollbackPreparedName, payload, key)
		zero(wire)
		if err != nil {
			return nil, err
		}
	} else {
		return nil, readErr
	}
	if currentCandidate && retainedOld {
		if err := tourV4RenameAt2Between(
			live, permit.Slug, control, prepared.RollbackRelpath, tourV4RenameExchange,
		); err != nil {
			return nil, fmt.Errorf("tour-v4-rollback-exchange-failed")
		}
		if err := live.Sync(); err != nil || control.Sync() != nil {
			return nil, fmt.Errorf("tour-v4-rollback-durability-unknown")
		}
		if tourV4BeforeControlBindingCheck != nil {
			tourV4BeforeControlBindingCheck()
		}
		if !tourV4ControlRootStillBound(live, control) {
			reverseErr := tourV4RenameAt2Between(
				live, permit.Slug, control, prepared.RollbackRelpath, tourV4RenameExchange,
			)
			if reverseErr != nil || live.Sync() != nil || control.Sync() != nil {
				return nil, fmt.Errorf("tour-v4-control-root-drift-rollback-ambiguous")
			}
			return nil, fmt.Errorf("tour-v4-control-root-drift-detected")
		}
	}
	if current != nil {
		current.release()
		current = nil
	}
	if retained != nil {
		retained.release()
		retained = nil
	}
	current, currentExists, err = tourV4OptionalTreeAt(live, livePath, permit.Slug, nil, false)
	if err != nil || !currentExists ||
		!tourV4SnapshotMatches(current, prepared.ObservedOldTreeSHA256, prepared.OldDevice, prepared.OldInode) {
		if current != nil {
			current.release()
		}
		return nil, fmt.Errorf("tour-v4-rollback-live-postcondition-invalid")
	}
	defer current.release()
	retained, retainedExists, err = tourV4OptionalTreeAt(control, controlPath, prepared.RollbackRelpath, permit, true)
	if err != nil || !retainedExists ||
		!tourV4SnapshotMatches(retained, permit.PublicTreeSHA256, prepared.CandidateDevice, prepared.CandidateInode) {
		if retained != nil {
			retained.release()
		}
		return nil, fmt.Errorf("tour-v4-rollback-retained-postcondition-invalid")
	}
	defer retained.release()
	if !tourV4ControlRootStillBound(live, control) {
		return nil, fmt.Errorf("tour-v4-control-root-drift-detected")
	}
	payload := tourV4RollbackPayload(
		"succeeded", binding, permit, prepared, publicationSHA, current, retained,
	)
	wire, _, err := tourV4WriteSignedReceipt(root, receipts, rollbackTerminalName, payload, key)
	if err != nil {
		return nil, err
	}
	storedPayload, storedRaw, _, err := tourV4ReadSignedReceipt(
		root, receipts, rollbackTerminalName, key.Public().(ed25519.PublicKey),
	)
	zero(wire)
	if err != nil {
		return nil, err
	}
	if err := tourV4ValidateRollbackPayload(storedPayload, "succeeded", binding, permit, prepared, publicationSHA); err != nil {
		zero(storedRaw)
		return nil, err
	}
	return storedRaw, nil
}

func tourV4RequireInstalledAuthority() error {
	if os.Geteuid() != 0 || SourceManifestDigest == "sha256:unbound" ||
		!digestPattern.MatchString(SourceManifestDigest) ||
		ScratchExecutionContract == "" || ScratchExecutionContract == "unbound" {
		return fmt.Errorf("tour-v4-installed-authority-required")
	}
	executable, err := os.Executable()
	if err != nil {
		return fmt.Errorf("tour-v4-executable-unavailable")
	}
	executable, err = filepath.EvalSymlinks(executable)
	if err != nil || executable != tourV4InstalledBinary {
		return fmt.Errorf("tour-v4-installed-authority-required")
	}
	info, err := os.Lstat(tourV4InstalledBinary)
	metadata, metaErr := tourV4StatMetadata(info)
	if err != nil || metaErr != nil || !info.Mode().IsRegular() ||
		info.Mode().Perm() != 0o755 || metadata.Uid != 0 || metadata.Gid != 0 ||
		metadata.Nlink != 1 {
		return fmt.Errorf("tour-v4-installed-authority-invalid")
	}
	return nil
}

func tourV4WriteOutput(stdout io.Writer, raw []byte) error {
	if stdout == nil || len(raw) < 1 || len(raw) > maximumJournalBytes {
		return fmt.Errorf("tour-v4-output-invalid")
	}
	wire := append(append([]byte(nil), raw...), '\n')
	written, err := stdout.Write(wire)
	zero(wire)
	if err != nil || written != len(raw)+1 {
		return fmt.Errorf("tour-v4-output-write-failed")
	}
	return nil
}

func tourV4AuthorityInfo(stdout io.Writer) error {
	permits := make([]any, 0, len(tourV4AuthorizedPermits))
	for index := range tourV4AuthorizedPermits {
		manifest, manifestSHA, err := tourV4PermitManifest(&tourV4AuthorizedPermits[index])
		if err != nil {
			return err
		}
		permits = append(permits, map[string]any{
			"artifact_tree_sha256":         tourV4AuthorizedPermits[index].ArtifactTreeSHA256,
			"browser_evidence_tree_sha256": tourV4AuthorizedPermits[index].BrowserEvidenceTreeSHA256,
			"browser_receipt_sha256":       tourV4AuthorizedPermits[index].BrowserReceiptSHA256,
			"manifest":                     manifest,
			"manifest_sha256":              manifestSHA,
			"public_tree_sha256":           tourV4AuthorizedPermits[index].PublicTreeSHA256,
			"quality_receipt_sha256":       tourV4AuthorizedPermits[index].QualityReceiptSHA256,
			"slug":                         tourV4AuthorizedPermits[index].Slug,
			"walkthrough_sha256":           tourV4AuthorizedPermits[index].WalkthroughSHA256,
		})
	}
	raw, err := canonicalJSON(map[string]any{
		"authoritative":            false,
		"component":                "propertyquarry-release-single-host-v2",
		"performs_release_effects": false,
		"permits":                  permits,
		"production_ready":         false,
		"schema":                   "propertyquarry.generated-reconstruction-publication-authority-info.v4",
		"version":                  json.Number("4"),
	})
	if err != nil {
		return err
	}
	return tourV4WriteOutput(stdout, raw)
}

func tourV4Command(operation string, args []string, stdout io.Writer) error {
	if operation == "tour-v4-authority-info" {
		if len(args) != 0 {
			return fmt.Errorf("tour-v4-authority-info-arguments-invalid")
		}
		return tourV4AuthorityInfo(stdout)
	}
	if err := tourV4RequireInstalledAuthority(); err != nil {
		return err
	}
	config, key, err := LoadConfig("/")
	if err != nil {
		return err
	}
	defer config.release()
	defer zero(key)
	binding, err := tourV4AuthorityBindingFor(config, key)
	if err != nil {
		return err
	}
	return tourV4ExecuteBoundCommand("/", operation, args, binding, key, stdout)
}

func tourV4ExecuteBoundCommand(
	root string,
	operation string,
	args []string,
	binding tourV4AuthorityBinding,
	key ed25519.PrivateKey,
	stdout io.Writer,
) error {
	if root == "" || !filepath.IsAbs(root) || filepath.Clean(root) != root ||
		len(key) != ed25519.PrivateKeySize ||
		tourV4ValidateAuthorityBinding(binding) != nil {
		return fmt.Errorf("tour-v4-bound-command-input-invalid")
	}
	flags := flag.NewFlagSet(operation, flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	expectedManifest := flags.String("expected-manifest-sha256", "", "")
	expectedOld := flags.String("expected-old-tree", "", "")
	transactionID := flags.String("transaction-id", "", "")
	bundle := flags.String("bundle", "", "")
	expectedCurrent := flags.String("expected-current-tree", "", "")
	if err := flags.Parse(args); err != nil || len(flags.Args()) != 0 {
		return fmt.Errorf("tour-v4-command-arguments-invalid")
	}
	var (
		raw []byte
		err error
	)
	switch operation {
	case "tour-inspect-v4":
		if *bundle != "" || *expectedCurrent != "" || *expectedOld != "" ||
			*transactionID != "" {
			return fmt.Errorf("tour-v4-inspection-arguments-invalid")
		}
		raw, err = tourV4Inspect(root, binding, key, *expectedManifest)
	case "tour-publish-v4":
		if *bundle == "" || *expectedCurrent != "" {
			return fmt.Errorf("tour-v4-publish-arguments-invalid")
		}
		raw, err = tourV4Publish(root, binding, key, tourV4PublishInput{
			BundlePath: *bundle, ExpectedOldTreeSHA256: *expectedOld,
			ExpectedManifestSHA256: *expectedManifest,
			TransactionID:          *transactionID,
		})
	case "tour-recover-v4":
		if *bundle != "" || *expectedCurrent != "" {
			return fmt.Errorf("tour-v4-recovery-arguments-invalid")
		}
		raw, err = tourV4Recover(
			root, binding, key, *expectedManifest, *expectedOld, *transactionID,
		)
	case "tour-rollback-v4":
		if *bundle != "" || *expectedCurrent == "" {
			return fmt.Errorf("tour-v4-rollback-arguments-invalid")
		}
		raw, err = tourV4Rollback(
			root, binding, key, *expectedManifest, *expectedOld,
			*expectedCurrent, *transactionID,
		)
	default:
		return fmt.Errorf("tour-v4-operation-invalid")
	}
	if err != nil {
		zero(raw)
		return err
	}
	defer zero(raw)
	return tourV4WriteOutput(stdout, raw)
}

// RunAttestedTourV4 is the sole non-installed authority entry point. The
// caller must be the package/self-bound scratch installer; the source digest
// linkage is checked again here before detached signed material is accepted.
func RunAttestedTourV4(
	command []string,
	materials TourV4DetachedMaterials,
	sourceManifestDigest string,
	stdout io.Writer,
) error {
	if len(command) < 1 || len(command) > 10 || stdout == nil ||
		os.Geteuid() != 0 || os.Getegid() != 0 ||
		SourceManifestDigest == "sha256:unbound" ||
		sourceManifestDigest != SourceManifestDigest ||
		!digestPattern.MatchString(sourceManifestDigest) ||
		ScratchExecutionContract != "linux-amd64-static-et-exec-v1" {
		return fmt.Errorf("tour-v4-attested-dispatch-binding-invalid")
	}
	binding, key, err := tourV4DetachedAuthority("/", materials)
	if err != nil {
		return err
	}
	defer zero(key)
	operation := command[0]
	if binding.SourceManifestDigest != sourceManifestDigest {
		return fmt.Errorf("tour-v4-attested-source-binding-invalid")
	}
	if operation == "tour-publish-v4" {
		now := tourV4Now().UTC().Unix()
		if now < binding.MaterializedAt-60 || now > binding.ValidUntil {
			return fmt.Errorf("tour-v4-attested-materialization-expired")
		}
	}
	if operation == "tour-v4-authority-info" {
		if len(command) != 1 {
			return fmt.Errorf("tour-v4-authority-info-arguments-invalid")
		}
		return tourV4AuthorityInfo(stdout)
	}
	if operation == "tour-publish-v4" ||
		operation == "tour-recover-v4" ||
		operation == "tour-rollback-v4" {
		if err := tourV4EnsureDetachedStateRoot(); err != nil {
			return err
		}
	}
	return tourV4ExecuteBoundCommand(
		"/", operation, command[1:], binding, key, stdout,
	)
}
