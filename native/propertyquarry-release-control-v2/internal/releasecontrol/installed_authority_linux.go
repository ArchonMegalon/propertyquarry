//go:build linux

package releasecontrol

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"os"
	"path"
	"strconv"
	"strings"
	"syscall"
)

const (
	InstalledAuthenticationJSON = "/usr/share/propertyquarry-release-control-v2/local-authority/authentication.v2.json"
	InstalledAuthenticationSig  = "/usr/share/propertyquarry-release-control-v2/local-authority/authentication.v2.sig"
	InstalledPayloadRoot        = "/usr/share/propertyquarry-release-control-v2/local-authority/payload"
	ExternalPackageAuthority    = "/run/secrets/propertyquarry-package-authority-v2.pem"
	InstalledStateRoot          = "/var/lib/propertyquarry-release-control-v2"
	InstalledRuntimeRoot        = "/run/propertyquarry-release-control-v2"
	InstalledRequestSocket      = "/run/propertyquarry-release-control-v2/request.sock"
	SupervisorExecutable        = "/usr/libexec/propertyquarry-release-control/propertyquarry-release-supervisor-v2"
	ControllerExecutable        = "/usr/libexec/propertyquarry-release-control/propertyquarry-release-controller-v2"
	WatchdogExecutable          = "/usr/libexec/propertyquarry-release-control/propertyquarry-release-watchdog-v2"

	localAuthenticationSchema = "propertyquarry.release-control.local-package-authentication.v2"
	localAuthenticationDomain = "propertyquarry.release-control.local-package-authentication.v2\x00"
	localHealthSchema         = "propertyquarry.release-control.local-runtime-health.v2"
	installedRoleCount        = 19
	installedPayloadFiles     = 21
	installedAuthorityUID     = 65532
	installedCallerUID        = 65534
)

type installedRuntimePaths struct {
	Root              string
	Authentication    string
	Signature         string
	PayloadRoot       string
	ExternalAnchor    string
	StateRoot         string
	RuntimeRoot       string
	RequestSocket     string
	Controller        string
	RunningExecutable string
	PackageUID        uint32
	PackageGID        uint32
	PrivateConfigGID  uint32
	AuthorityUID      uint32
	AuthorityGID      uint32
	SocketUID         uint32
	SocketGID         uint32
	CallerUID         uint32
	CallerGID         uint32
}

func defaultInstalledRuntimePaths() installedRuntimePaths {
	serviceGID := uint32(os.Getegid())
	return installedRuntimePaths{
		Root:              "/",
		Authentication:    InstalledAuthenticationJSON,
		Signature:         InstalledAuthenticationSig,
		PayloadRoot:       InstalledPayloadRoot,
		ExternalAnchor:    ExternalPackageAuthority,
		StateRoot:         InstalledStateRoot,
		RuntimeRoot:       InstalledRuntimeRoot,
		RequestSocket:     InstalledRequestSocket,
		Controller:        ControllerExecutable,
		RunningExecutable: "/proc/self/exe",
		PackageUID:        0,
		PackageGID:        0,
		PrivateConfigGID:  serviceGID,
		AuthorityUID:      installedAuthorityUID,
		AuthorityGID:      serviceGID,
		SocketUID:         installedAuthorityUID,
		SocketGID:         serviceGID,
		CallerUID:         installedCallerUID,
		CallerGID:         serviceGID,
	}
}

func validateInstalledPrincipalContract(paths installedRuntimePaths) error {
	if paths.Root == "" ||
		paths.SocketUID != paths.AuthorityUID ||
		paths.SocketGID != paths.AuthorityGID ||
		paths.PrivateConfigGID != paths.AuthorityGID ||
		paths.CallerGID != paths.AuthorityGID {
		return fmt.Errorf("installed principal contract invalid")
	}
	// A non-root prefix is used only by native fixtures, which must project
	// ownership to the unprivileged test process. Installed production paths
	// are rooted at "/" and have one exact, non-collapsible principal split.
	if paths.Root != "/" {
		return nil
	}
	if paths.PackageUID != 0 ||
		paths.PackageGID != 0 ||
		paths.AuthorityUID != installedAuthorityUID ||
		paths.CallerUID != installedCallerUID ||
		paths.AuthorityUID == paths.CallerUID {
		return fmt.Errorf("installed principal contract invalid")
	}
	return nil
}

func validateEmptyInstalledAuthorityState(paths installedRuntimePaths) (stableIdentity, error) {
	if err := validateInstalledPrincipalContract(paths); err != nil {
		return stableIdentity{}, err
	}
	return inspectStableRootedDirectory(
		paths.Root,
		paths.StateRoot,
		expectedFileMetadata{
			Mode: 0o700,
			UID:  paths.AuthorityUID,
			GID:  paths.AuthorityGID,
		},
		true,
	)
}

type authenticatedPayloadClaim struct {
	TreeDigest                 string
	FileCount                  int64
	DirectoryCount             int64
	RoleCount                  int64
	InstallationManifestDigest string
	PayloadReceiptDigest       string
	NativeBuildReceiptDigest   string
}

type localPackageAuthentication struct {
	Canonical []byte
	Digest    string
	KeyID     string
	Payload   authenticatedPayloadClaim
}

func (authentication *localPackageAuthentication) release() {
	if authentication == nil {
		return
	}
	zero(authentication.Canonical)
	authentication.Canonical = nil
	authentication.Digest = ""
	authentication.KeyID = ""
	authentication.Payload = authenticatedPayloadClaim{}
}

type installedRoleContract struct {
	Role    string
	Path    string
	Mode    uint32
	Private bool
}

type installedRole struct {
	Contract installedRoleContract
	Digest   string
	Size     int64
	UID      uint32
	GID      uint32
}

type installedAuthorityVerification struct {
	AuthenticationDigest string
	PayloadTreeDigest    string
	AuthorityKeyID       string
	ManifestDigest       string
	NativeBuildDigest    string
	Roles                map[string]installedRole
}

var installedRoleContracts = []installedRoleContract{
	{Role: "supervisor-executable", Path: SupervisorExecutable, Mode: 0o755},
	{Role: "controller-executable", Path: ControllerExecutable, Mode: 0o755},
	{Role: "watchdog-executable", Path: WatchdogExecutable, Mode: 0o755},
	{Role: "systemd-socket-unit", Path: "/usr/lib/systemd/system/propertyquarry-release-control-v2.socket", Mode: 0o644},
	{Role: "systemd-controller-template-unit", Path: "/usr/lib/systemd/system/propertyquarry-release-control-v2@.service", Mode: 0o644},
	{Role: "systemd-watchdog-unit", Path: "/usr/lib/systemd/system/propertyquarry-release-watchdog-v2.service", Mode: 0o644},
	{Role: "sysusers-config", Path: "/usr/lib/sysusers.d/propertyquarry-release-control-v2.conf", Mode: 0o644},
	{Role: "tmpfiles-config", Path: "/usr/lib/tmpfiles.d/propertyquarry-release-control-v2.conf", Mode: 0o644},
	{Role: "controller-schema", Path: "/usr/share/propertyquarry-release-control-v2/schema/controller-v2.schema.json", Mode: 0o644},
	{Role: "watchdog-schema", Path: "/usr/share/propertyquarry-release-control-v2/schema/watchdog-v2.schema.json", Mode: 0o644},
	{Role: "controller-config", Path: ControllerConfig, Mode: 0o640, Private: true},
	{Role: "watchdog-config", Path: WatchdogConfig, Mode: 0o640, Private: true},
	{Role: "root-policy", Path: "/etc/propertyquarry-release-control/policy-v2.json", Mode: 0o640, Private: true},
	{Role: "request-trust-root", Path: "/etc/propertyquarry-release-control/trust.d/request-authority-v2.pem", Mode: 0o640, Private: true},
	{Role: "response-trust-root", Path: "/etc/propertyquarry-release-control/trust.d/response-authority-v2.pem", Mode: 0o640, Private: true},
	{Role: "lifecycle-cas-trust-root", Path: "/etc/propertyquarry-release-control/trust.d/lifecycle-cas-v2.pem", Mode: 0o640, Private: true},
	{Role: "evidence-trust-root", Path: "/etc/propertyquarry-release-control/trust.d/evidence-authority-v2.pem", Mode: 0o640, Private: true},
	{Role: "resource-mediator-trust-root", Path: "/etc/propertyquarry-release-control/trust.d/resource-mediator-v2.pem", Mode: 0o640, Private: true},
	{Role: "package-trust-root", Path: "/etc/propertyquarry-release-control/trust.d/package-authority-v2.pem", Mode: 0o640, Private: true},
}

func validateInstalledLocalAuthority(
	component Component,
	paths installedRuntimePaths,
) (*installedAuthorityVerification, error) {
	if !validComponent(component) || validateInstalledPrincipalContract(paths) != nil {
		return nil, fmt.Errorf("installed authority invocation invalid")
	}
	rootMetadata := expectedFileMetadata{
		Mode: 0o644,
		UID:  paths.PackageUID,
		GID:  paths.PackageGID,
	}
	anchorBytes, _, err := readStableRootedFile(
		paths.Root,
		paths.ExternalAnchor,
		4096,
		expectedFileMetadata{
			Mode: 0o444,
			UID:  paths.PackageUID,
			GID:  paths.PackageGID,
		},
	)
	if err != nil {
		return nil, err
	}
	externalKey, externalKeyID, err := parseEd25519PublicAnchor(anchorBytes)
	zero(anchorBytes)
	if err != nil {
		return nil, err
	}

	authenticationBytes, _, err := readStableRootedFile(
		paths.Root,
		paths.Authentication,
		maxInstalledMetadataBytes,
		rootMetadata,
	)
	if err != nil {
		return nil, err
	}
	signature, _, err := readStableRootedFile(paths.Root, paths.Signature, ed25519.SignatureSize, rootMetadata)
	if err != nil {
		zero(authenticationBytes)
		return nil, err
	}
	if len(signature) != ed25519.SignatureSize {
		zero(authenticationBytes)
		zero(signature)
		return nil, fmt.Errorf("installed authentication signature invalid")
	}
	authentication, err := parseLocalPackageAuthentication(authenticationBytes)
	zero(authenticationBytes)
	if err != nil {
		zero(signature)
		return nil, err
	}
	defer authentication.release()
	if authentication.KeyID != externalKeyID {
		zero(signature)
		return nil, fmt.Errorf("installed authentication anchor mismatch")
	}
	message := domainSeparatedMessage([]byte(localAuthenticationDomain), authentication.Canonical)
	verified := ed25519.Verify(externalKey, message, signature)
	zero(message)
	zero(signature)
	if !verified {
		return nil, fmt.Errorf("installed authentication signature invalid")
	}

	tree, err := collectPayloadTree(
		paths.Root,
		paths.PayloadRoot,
		expectedFileMetadata{
			Mode: 0o755,
			UID:  paths.PackageUID,
			GID:  paths.PackageGID,
		},
	)
	if err != nil {
		return nil, err
	}
	defer tree.release()
	if tree.Digest != authentication.Payload.TreeDigest ||
		tree.FileCount != authentication.Payload.FileCount ||
		tree.DirectoryCount != authentication.Payload.DirectoryCount ||
		tree.FileCount != installedPayloadFiles ||
		authentication.Payload.RoleCount != installedRoleCount {
		return nil, fmt.Errorf("authenticated payload tree mismatch")
	}
	if err := validateClosedRetainedPayload(tree); err != nil {
		return nil, err
	}
	manifestBytes, ok := tree.Files["installation-manifest.v2.json"]
	if !ok || sha256Digest(manifestBytes) != authentication.Payload.InstallationManifestDigest {
		return nil, fmt.Errorf("authenticated manifest mismatch")
	}
	receiptBytes, ok := tree.Files["package-payload-receipt.v2.json"]
	if !ok || sha256Digest(receiptBytes) != authentication.Payload.PayloadReceiptDigest {
		return nil, fmt.Errorf("authenticated payload receipt mismatch")
	}
	if nativeBuildDigestFromReceipt(receiptBytes) != authentication.Payload.NativeBuildReceiptDigest {
		return nil, fmt.Errorf("authenticated native build receipt mismatch")
	}

	roles, err := parseAndAuditInstalledRoles(manifestBytes, tree, paths)
	if err != nil {
		return nil, err
	}
	packageRole, ok := roles["package-trust-root"]
	if !ok {
		return nil, fmt.Errorf("installed package authority missing")
	}
	packageAnchor, _, err := readStableRootedFile(
		paths.Root,
		packageRole.Contract.Path,
		packageRole.Size,
		expectedFileMetadata{Mode: packageRole.Contract.Mode, UID: packageRole.UID, GID: packageRole.GID},
	)
	if err != nil {
		return nil, err
	}
	_, packageKeyID, err := parseEd25519PublicAnchor(packageAnchor)
	zero(packageAnchor)
	if err != nil || packageKeyID != externalKeyID {
		return nil, fmt.Errorf("installed package authority key mismatch")
	}
	if paths.RunningExecutable != "" {
		roleName := map[Component]string{
			Supervisor: "supervisor-executable",
			Controller: "controller-executable",
			Watchdog:   "watchdog-executable",
		}[component]
		if err := validateRunningExecutable(paths.RunningExecutable, roles[roleName]); err != nil {
			return nil, err
		}
	}
	return &installedAuthorityVerification{
		AuthenticationDigest: authentication.Digest,
		PayloadTreeDigest:    tree.Digest,
		AuthorityKeyID:       externalKeyID,
		ManifestDigest:       authentication.Payload.InstallationManifestDigest,
		NativeBuildDigest:    authentication.Payload.NativeBuildReceiptDigest,
		Roles:                roles,
	}, nil
}

func validateClosedRetainedPayload(tree *payloadTreeSnapshot) error {
	if tree == nil {
		return fmt.Errorf("retained payload unavailable")
	}
	expectedFiles := map[string]uint32{
		"installation-manifest.v2.json":   0o644,
		"package-payload-receipt.v2.json": 0o644,
	}
	expectedDirectories := make(map[string]uint32)
	for _, contract := range installedRoleContracts {
		retained := "rootfs" + contract.Path
		expectedFiles[retained] = contract.Mode
		for directory := path.Dir(retained); directory != "." && directory != "/"; directory = path.Dir(directory) {
			mode := uint32(0o755)
			if directory == "rootfs/etc/propertyquarry-release-control" ||
				strings.HasPrefix(directory, "rootfs/etc/propertyquarry-release-control/") {
				mode = 0o750
			}
			expectedDirectories[directory] = mode
		}
	}
	for _, entry := range tree.Entries {
		switch entry.Type {
		case "file":
			mode, ok := expectedFiles[entry.Path]
			if !ok || entry.Mode != mode || entry.Size < 1 || !requestDigestPattern.MatchString(entry.Digest) {
				return fmt.Errorf("retained payload file contract changed")
			}
			delete(expectedFiles, entry.Path)
		case "directory":
			mode, ok := expectedDirectories[entry.Path]
			if !ok || entry.Mode != mode {
				return fmt.Errorf("retained payload directory contract changed")
			}
			delete(expectedDirectories, entry.Path)
		default:
			return fmt.Errorf("retained payload entry type invalid")
		}
	}
	if len(expectedFiles) != 0 || len(expectedDirectories) != 0 {
		return fmt.Errorf("retained payload contract incomplete")
	}
	return nil
}

func parseLocalPackageAuthentication(raw []byte) (*localPackageAuthentication, error) {
	value, err := decodeStrictJSON(raw)
	if err != nil {
		return nil, fmt.Errorf("installed authentication invalid")
	}
	outer, ok := value.(map[string]any)
	if !ok || !hasExactKeys(outer, "schema", "version", "signature_profile", "authority_scope", "payload") {
		return nil, fmt.Errorf("installed authentication invalid")
	}
	if schema, ok := exactString(outer["schema"]); !ok || schema != localAuthenticationSchema {
		return nil, fmt.Errorf("installed authentication schema invalid")
	}
	if version, ok := exactBoundedInt(outer["version"], 2); !ok || version != 2 {
		return nil, fmt.Errorf("installed authentication version invalid")
	}
	profile, ok := outer["signature_profile"].(map[string]any)
	if !ok || !hasExactKeys(profile, "algorithm", "encoding", "key_id", "signed_message") ||
		!exactStringEquals(profile["algorithm"], "ed25519") ||
		!exactStringEquals(profile["encoding"], "raw-64-byte") ||
		!exactStringEquals(profile["signed_message"], "domain-separated-uint64be-length-prefixed-canonical-json") {
		return nil, fmt.Errorf("installed authentication signature profile invalid")
	}
	keyID, ok := exactString(profile["key_id"])
	if !ok || !requestDigestPattern.MatchString(keyID) {
		return nil, fmt.Errorf("installed authentication key id invalid")
	}
	scope, ok := outer["authority_scope"].(map[string]any)
	if !ok || !hasExactKeys(
		scope,
		"kind",
		"scope_id",
		"authoritative_for_package_authentication",
		"external_production_authority",
		"public_launch_authority",
		"performs_release_effects",
	) ||
		!exactStringEquals(scope["kind"], "local-docker") ||
		!exactStringEquals(scope["scope_id"], "propertyquarry-local-docker") ||
		!exactBoolEquals(scope["authoritative_for_package_authentication"], true) ||
		!exactBoolEquals(scope["external_production_authority"], false) ||
		!exactBoolEquals(scope["public_launch_authority"], false) ||
		!exactBoolEquals(scope["performs_release_effects"], false) {
		return nil, fmt.Errorf("installed authentication authority scope invalid")
	}
	payload, ok := outer["payload"].(map[string]any)
	if !ok || !hasExactKeys(
		payload,
		"tree_digest",
		"file_count",
		"directory_count",
		"role_count",
		"installation_manifest_sha256",
		"package_payload_receipt_sha256",
		"native_build_receipt_sha256",
	) {
		return nil, fmt.Errorf("installed authentication payload invalid")
	}
	claim := authenticatedPayloadClaim{}
	for key, target := range map[string]*string{
		"tree_digest":                    &claim.TreeDigest,
		"installation_manifest_sha256":   &claim.InstallationManifestDigest,
		"package_payload_receipt_sha256": &claim.PayloadReceiptDigest,
		"native_build_receipt_sha256":    &claim.NativeBuildReceiptDigest,
	} {
		value, ok := exactString(payload[key])
		if !ok || !requestDigestPattern.MatchString(value) {
			return nil, fmt.Errorf("installed authentication digest invalid")
		}
		*target = value
	}
	if claim.FileCount, ok = exactBoundedInt(payload["file_count"], 1); !ok || claim.FileCount != installedPayloadFiles {
		return nil, fmt.Errorf("installed authentication file count invalid")
	}
	if claim.DirectoryCount, ok = exactBoundedInt(payload["directory_count"], 1); !ok || claim.DirectoryCount > maxInstalledTreeEntries {
		return nil, fmt.Errorf("installed authentication directory count invalid")
	}
	if claim.RoleCount, ok = exactBoundedInt(payload["role_count"], 1); !ok || claim.RoleCount != installedRoleCount {
		return nil, fmt.Errorf("installed authentication role count invalid")
	}
	canonical, err := canonicalJSON(outer)
	if err != nil || !bytes.Equal(raw, canonical) {
		zero(canonical)
		return nil, fmt.Errorf("installed authentication is not canonical")
	}
	return &localPackageAuthentication{
		Canonical: canonical,
		Digest:    sha256Digest(canonical),
		KeyID:     keyID,
		Payload:   claim,
	}, nil
}

func exactStringEquals(value any, expected string) bool {
	observed, ok := exactString(value)
	return ok && observed == expected
}

func exactBoolEquals(value any, expected bool) bool {
	observed, ok := value.(bool)
	return ok && observed == expected
}

func parseEd25519PublicAnchor(raw []byte) (ed25519.PublicKey, string, error) {
	block, rest := pem.Decode(raw)
	if block == nil || len(rest) != 0 || block.Type != "PUBLIC KEY" || len(block.Headers) != 0 {
		return nil, "", fmt.Errorf("package authority PEM invalid")
	}
	parsed, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		return nil, "", fmt.Errorf("package authority key invalid")
	}
	publicKey, ok := parsed.(ed25519.PublicKey)
	if !ok || len(publicKey) != ed25519.PublicKeySize {
		return nil, "", fmt.Errorf("package authority algorithm invalid")
	}
	der, err := x509.MarshalPKIXPublicKey(publicKey)
	if err != nil || !bytes.Equal(der, block.Bytes) {
		return nil, "", fmt.Errorf("package authority encoding invalid")
	}
	digest := sha256.Sum256(der)
	return append(ed25519.PublicKey(nil), publicKey...), "sha256:" + hex.EncodeToString(digest[:]), nil
}

func nativeBuildDigestFromReceipt(raw []byte) string {
	value, err := decodeStrictJSON(raw)
	if err != nil {
		return ""
	}
	outer, ok := value.(map[string]any)
	if !ok {
		return ""
	}
	integrity, ok := outer["input_integrity"].(map[string]any)
	if !ok {
		return ""
	}
	digest, ok := exactString(integrity["native_build_receipt_sha256"])
	if !ok || !requestDigestPattern.MatchString(digest) {
		return ""
	}
	return digest
}

func parseAndAuditInstalledRoles(
	raw []byte,
	tree *payloadTreeSnapshot,
	paths installedRuntimePaths,
) (map[string]installedRole, error) {
	value, err := decodeStrictJSON(raw)
	if err != nil {
		return nil, fmt.Errorf("installation manifest invalid")
	}
	outer, ok := value.(map[string]any)
	if !ok || !hasExactKeys(outer, "schema", "version", "roles") ||
		!exactStringEquals(outer["schema"], "propertyquarry.release-installation-manifest.v2") {
		return nil, fmt.Errorf("installation manifest schema invalid")
	}
	if version, ok := exactBoundedInt(outer["version"], 2); !ok || version != 2 {
		return nil, fmt.Errorf("installation manifest version invalid")
	}
	items, ok := outer["roles"].([]any)
	if !ok || len(items) != len(installedRoleContracts) {
		return nil, fmt.Errorf("installation manifest role count invalid")
	}
	roles := make(map[string]installedRole, len(items))
	for index, item := range items {
		object, ok := item.(map[string]any)
		if !ok || !hasExactKeys(object, "role", "path", "sha256", "size", "mode", "uid", "gid") {
			return nil, fmt.Errorf("installation manifest role invalid")
		}
		contract := installedRoleContracts[index]
		if !exactStringEquals(object["role"], contract.Role) || !exactStringEquals(object["path"], contract.Path) {
			return nil, fmt.Errorf("installation manifest role contract changed")
		}
		digest, ok := exactString(object["sha256"])
		if !ok || !requestDigestPattern.MatchString(digest) {
			return nil, fmt.Errorf("installation manifest role digest invalid")
		}
		size, ok := exactBoundedInt(object["size"], 1)
		if !ok || size > maxInstalledRoleBytes {
			return nil, fmt.Errorf("installation manifest role size invalid")
		}
		mode, ok := exactBoundedInt(object["mode"], 0)
		if !ok || uint32(mode) != contract.Mode {
			return nil, fmt.Errorf("installation manifest role mode invalid")
		}
		uid, ok := exactBoundedInt(object["uid"], 0)
		if !ok || uint64(uid) != uint64(paths.PackageUID) {
			return nil, fmt.Errorf("installation manifest role uid invalid")
		}
		gid, ok := exactBoundedInt(object["gid"], 0)
		expectedGID := paths.PackageGID
		if contract.Private {
			expectedGID = paths.PrivateConfigGID
		}
		if !ok || uint64(gid) != uint64(expectedGID) {
			return nil, fmt.Errorf("installation manifest role gid invalid")
		}
		retainedPath := "rootfs" + contract.Path
		retained, exists := tree.Files[retainedPath]
		if !exists || int64(len(retained)) != size || sha256Digest(retained) != digest {
			return nil, fmt.Errorf("retained role does not match manifest")
		}
		if !treeEntryHasMode(tree.Entries, retainedPath, contract.Mode) {
			return nil, fmt.Errorf("retained role mode does not match manifest")
		}
		active, _, err := readStableRootedFile(
			paths.Root,
			contract.Path,
			size,
			expectedFileMetadata{Mode: contract.Mode, UID: uint32(uid), GID: uint32(gid)},
		)
		if err != nil || int64(len(active)) != size || sha256Digest(active) != digest {
			zero(active)
			return nil, fmt.Errorf("active role does not match manifest")
		}
		zero(active)
		roles[contract.Role] = installedRole{
			Contract: contract,
			Digest:   digest,
			Size:     size,
			UID:      uint32(uid),
			GID:      uint32(gid),
		}
	}
	return roles, nil
}

func treeEntryHasMode(entries []payloadTreeEntry, target string, mode uint32) bool {
	for _, entry := range entries {
		if entry.Path == target {
			return entry.Type == "file" && entry.Mode == mode
		}
	}
	return false
}

func validateRunningExecutable(path string, role installedRole) error {
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()
	stat, err := file.Stat()
	if err != nil || !stat.Mode().IsRegular() || stat.Size() != role.Size || stat.Size() > maxInstalledRoleBytes {
		return fmt.Errorf("running executable metadata invalid")
	}
	value, err := io.ReadAll(io.LimitReader(file, maxInstalledRoleBytes+1))
	if err != nil || int64(len(value)) != role.Size || sha256Digest(value) != role.Digest {
		zero(value)
		return fmt.Errorf("running executable is not installed executable")
	}
	zero(value)
	return nil
}

func openPinnedInstalledController(
	paths installedRuntimePaths,
	role installedRole,
) (*os.File, error) {
	fd, err := openRootedAbsolute(paths.Root, paths.Controller, 0)
	if err != nil {
		return nil, err
	}
	value, _, err := readStableFD(fd, role.Size, expectedFileMetadata{
		Mode: role.Contract.Mode,
		UID:  role.UID,
		GID:  role.GID,
	})
	if err != nil || int64(len(value)) != role.Size || sha256Digest(value) != role.Digest {
		zero(value)
		_ = syscall.Close(fd)
		return nil, fmt.Errorf("fixed controller validation failed")
	}
	zero(value)
	file := os.NewFile(uintptr(fd), "authenticated-controller")
	if file == nil {
		_ = syscall.Close(fd)
		return nil, fmt.Errorf("fixed controller wrapper failed")
	}
	return file, nil
}

func closeInstalledExecutableFD(args []string) bool {
	if len(args) < 12 || args[10] != "--installed-local-authority-executable-fd" {
		return false
	}
	fd, err := strconv.Atoi(args[11])
	if err != nil || fd < 3 || strconv.Itoa(fd) != args[11] {
		return false
	}
	var stat syscall.Stat_t
	if syscall.Fstat(fd, &stat) != nil || stat.Mode&syscall.S_IFMT != syscall.S_IFREG {
		_ = syscall.Close(fd)
		return false
	}
	_ = syscall.Close(fd)
	return true
}

func readCanonicalHealth(value map[string]any) ([]byte, error) {
	encoded, err := canonicalJSON(value)
	if err != nil || len(encoded) > 4095 {
		zero(encoded)
		return nil, fmt.Errorf("local health encoding failed")
	}
	return append(encoded, '\n'), nil
}

// Keep encoding/json imported in this Linux file as an explicit compile-time
// assertion that strict integer parsing remains json.Number based.
var _ = json.Number("0")
