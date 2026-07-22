//go:build linux

package releasecontrol

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

const (
	fixtureNativeBuildDigest = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
	localAuthVectorSeedHex   = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
	localAuthVectorKeyID     = "sha256:a050837d85070582ccf7394b0988847cc312cb88259b894899f6f239cf1791a5"
	localAuthVectorTreeJSON  = `{"entries":[{"mode":420,"path":"installation-manifest.v2.json","sha256":"sha256:96a491ee16265cae3d9c2592bd0de0e345fa8a2c3792f555a59ef9601b5494c6","size":4460,"type":"file"},{"mode":420,"path":"package-payload-receipt.v2.json","sha256":"sha256:9361a60248905443a11039856190c03db64695777f363bf7e7c6e3d72c6e8db6","size":1568,"type":"file"},{"mode":493,"path":"rootfs","type":"directory"},{"mode":493,"path":"rootfs/etc","type":"directory"},{"mode":488,"path":"rootfs/etc/propertyquarry-release-control","type":"directory"},{"mode":416,"path":"rootfs/etc/propertyquarry-release-control/controller-v2.json","sha256":"sha256:9f2b4d0075b998b9ebe3850e292d85e39393e6b7a140ddf0a643d49f81cb3652","size":3606,"type":"file"},{"mode":416,"path":"rootfs/etc/propertyquarry-release-control/policy-v2.json","sha256":"sha256:0a3655d702d82060cc825adf47955b0b72df4755c863d565caee550a3c3309f7","size":1367,"type":"file"},{"mode":488,"path":"rootfs/etc/propertyquarry-release-control/trust.d","type":"directory"},{"mode":416,"path":"rootfs/etc/propertyquarry-release-control/trust.d/evidence-authority-v2.pem","sha256":"sha256:a284dcecd00e98802681345623e354e793d4b4a36353b6104c7ee3ae55b17b7c","size":113,"type":"file"},{"mode":416,"path":"rootfs/etc/propertyquarry-release-control/trust.d/lifecycle-cas-v2.pem","sha256":"sha256:55f7c6dfa1c440ccf3136606786b62208da7ae0fe30115b3ae363c630326d5dd","size":113,"type":"file"},{"mode":416,"path":"rootfs/etc/propertyquarry-release-control/trust.d/package-authority-v2.pem","sha256":"sha256:f0ff50dacf109dea7f716d7c03eb7a821aa3a283fd3fa02f4c14dbba7d3c3322","size":113,"type":"file"},{"mode":416,"path":"rootfs/etc/propertyquarry-release-control/trust.d/request-authority-v2.pem","sha256":"sha256:e09939a27210d627ff8f99ed638f8a9427300828750a1a99834ac6eebac6696d","size":113,"type":"file"},{"mode":416,"path":"rootfs/etc/propertyquarry-release-control/trust.d/resource-mediator-v2.pem","sha256":"sha256:28809799947151145ddd2933c334ddb16d0c2074cc53f91f29cc9976cc02a5d5","size":113,"type":"file"},{"mode":416,"path":"rootfs/etc/propertyquarry-release-control/trust.d/response-authority-v2.pem","sha256":"sha256:2ed116ed161dd76155eece777ab5dc15467f74c2e30fd7ecde560aa3a68d47a6","size":113,"type":"file"},{"mode":416,"path":"rootfs/etc/propertyquarry-release-control/watchdog-v2.json","sha256":"sha256:8555a1406e62652b37f11b5bf3a97fceb8edb5dae08a6a1127de356e6d01e040","size":962,"type":"file"},{"mode":493,"path":"rootfs/usr","type":"directory"},{"mode":493,"path":"rootfs/usr/lib","type":"directory"},{"mode":493,"path":"rootfs/usr/lib/systemd","type":"directory"},{"mode":493,"path":"rootfs/usr/lib/systemd/system","type":"directory"},{"mode":420,"path":"rootfs/usr/lib/systemd/system/propertyquarry-release-control-v2.socket","sha256":"sha256:4790e1886b933a54f19f2bb046518fa66716acee1a7d187a92316a55feb163fc","size":457,"type":"file"},{"mode":420,"path":"rootfs/usr/lib/systemd/system/propertyquarry-release-control-v2@.service","sha256":"sha256:dd51606be55edf757eb268a617a198db6b5077e1f25e7a4a8a6be8b6344782f3","size":2971,"type":"file"},{"mode":420,"path":"rootfs/usr/lib/systemd/system/propertyquarry-release-watchdog-v2.service","sha256":"sha256:cf3a51a4b0a46d1d44050486a0eaca172b5626abf1703cc76477f9efc288b161","size":2859,"type":"file"},{"mode":493,"path":"rootfs/usr/lib/sysusers.d","type":"directory"},{"mode":420,"path":"rootfs/usr/lib/sysusers.d/propertyquarry-release-control-v2.conf","sha256":"sha256:21bedf39bf9917efb35329c0eb667aac7e6b7c9129f28ed77af9bd2fb6d9f4b0","size":299,"type":"file"},{"mode":493,"path":"rootfs/usr/lib/tmpfiles.d","type":"directory"},{"mode":420,"path":"rootfs/usr/lib/tmpfiles.d/propertyquarry-release-control-v2.conf","sha256":"sha256:945d1c9a34a9463356a8b6b923035a4d1d8054ce6f3b54cb6770db1222a092c2","size":991,"type":"file"},{"mode":493,"path":"rootfs/usr/libexec","type":"directory"},{"mode":493,"path":"rootfs/usr/libexec/propertyquarry-release-control","type":"directory"},{"mode":493,"path":"rootfs/usr/libexec/propertyquarry-release-control/propertyquarry-release-controller-v2","sha256":"sha256:91701cb5cae2b95bdee7b60d6c3d835fdd0b656b2a87e0d43f9b5fd332cac9ac","size":213,"type":"file"},{"mode":493,"path":"rootfs/usr/libexec/propertyquarry-release-control/propertyquarry-release-supervisor-v2","sha256":"sha256:bbfe99a87120c006cbac5a71e4762c8e2f23b66c2429120cb8ad0d33e7916d12","size":213,"type":"file"},{"mode":493,"path":"rootfs/usr/libexec/propertyquarry-release-control/propertyquarry-release-watchdog-v2","sha256":"sha256:2c523ca473543691711cfcd358be23b5b28d0a7a179a5d5f004faebfcf53cf6a","size":211,"type":"file"},{"mode":493,"path":"rootfs/usr/share","type":"directory"},{"mode":493,"path":"rootfs/usr/share/propertyquarry-release-control-v2","type":"directory"},{"mode":493,"path":"rootfs/usr/share/propertyquarry-release-control-v2/schema","type":"directory"},{"mode":420,"path":"rootfs/usr/share/propertyquarry-release-control-v2/schema/controller-v2.schema.json","sha256":"sha256:1fbad13305775bd50eb0a6540ef62165dc5a4d674891b35eb5174b9cfaa77c68","size":6097,"type":"file"},{"mode":420,"path":"rootfs/usr/share/propertyquarry-release-control-v2/schema/watchdog-v2.schema.json","sha256":"sha256:96db0457b9390de7360f2b23b1dd2716ec42036f97157fbdc51543b8bc09f6f3","size":2643,"type":"file"}],"schema":"propertyquarry.release-control.payload-tree.v2"}`
	localAuthVectorAuthJSON  = `{"authority_scope":{"authoritative_for_package_authentication":true,"external_production_authority":false,"kind":"local-docker","performs_release_effects":false,"public_launch_authority":false,"scope_id":"propertyquarry-local-docker"},"payload":{"directory_count":15,"file_count":21,"installation_manifest_sha256":"sha256:96a491ee16265cae3d9c2592bd0de0e345fa8a2c3792f555a59ef9601b5494c6","native_build_receipt_sha256":"sha256:cdd3ce09ab91ae315138d3ab516de5ceb6cc6c7ec0cc13e1f6e99dd564d16300","package_payload_receipt_sha256":"sha256:9361a60248905443a11039856190c03db64695777f363bf7e7c6e3d72c6e8db6","role_count":19,"tree_digest":"sha256:884dba36f555f872d1cecc6af09bdd9c20b44d5122e9eb4f385eb059e88e1ac0"},"schema":"propertyquarry.release-control.local-package-authentication.v2","signature_profile":{"algorithm":"ed25519","encoding":"raw-64-byte","key_id":"sha256:a050837d85070582ccf7394b0988847cc312cb88259b894899f6f239cf1791a5","signed_message":"domain-separated-uint64be-length-prefixed-canonical-json"},"version":2}`
	localAuthVectorSigHex    = "8f1049151ecce7c67b031828c8d9452841a07f38d3d5e37a8a8ede6421a08584fa060e8a5be29486e61d6e57319c0eabefc6d1b246a85e304497b77b339fc603"
)

type installedAuthorityFixture struct {
	paths          installedRuntimePaths
	authentication []byte
	signature      []byte
	publicPEM      []byte
	keyID          string
	privateKey     ed25519.PrivateKey
}

type installedHealthReady chan struct{}

func (ready installedHealthReady) Write(value []byte) (int, error) {
	select {
	case ready <- struct{}{}:
	default:
	}
	return len(value), nil
}

func TestInstalledRuntimeDefaultPathsAreFixed(t *testing.T) {
	paths := defaultInstalledRuntimePaths()
	if paths.Root != "/" ||
		paths.Authentication != "/usr/share/propertyquarry-release-control-v2/local-authority/authentication.v2.json" ||
		paths.Signature != "/usr/share/propertyquarry-release-control-v2/local-authority/authentication.v2.sig" ||
		paths.PayloadRoot != "/usr/share/propertyquarry-release-control-v2/local-authority/payload" ||
		paths.ExternalAnchor != "/run/secrets/propertyquarry-package-authority-v2.pem" ||
		paths.StateRoot != "/var/lib/propertyquarry-release-control-v2" ||
		paths.RuntimeRoot != "/run/propertyquarry-release-control-v2" ||
		paths.RequestSocket != "/run/propertyquarry-release-control-v2/request.sock" ||
		paths.Controller != "/usr/libexec/propertyquarry-release-control/propertyquarry-release-controller-v2" ||
		paths.RunningExecutable != "/proc/self/exe" ||
		paths.RootUID != 0 || paths.RootGID != 0 ||
		paths.RuntimeUID != uint32(os.Geteuid()) || paths.RuntimeGID != uint32(os.Getegid()) {
		t.Fatalf("installed runtime path contract changed: %#v", paths)
	}
}

func TestInstalledAuthenticationFixedSeedCrossLanguageVector(t *testing.T) {
	seed, err := hex.DecodeString(localAuthVectorSeedHex)
	if err != nil || len(seed) != ed25519.SeedSize {
		t.Fatal("fixed-seed vector is invalid")
	}
	privateKey := ed25519.NewKeyFromSeed(seed)
	zero(seed)
	publicKey := privateKey.Public().(ed25519.PublicKey)
	der, err := x509.MarshalPKIXPublicKey(publicKey)
	if err != nil {
		t.Fatal(err)
	}
	publicPEM := pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der})
	parsedPublicKey, keyID, err := parseEd25519PublicAnchor(publicPEM)
	if err != nil || keyID != localAuthVectorKeyID || !bytes.Equal(parsedPublicKey, publicKey) {
		t.Fatalf("fixed-seed SPKI mismatch: %s, %v", keyID, err)
	}

	tree := []byte(localAuthVectorTreeJSON)
	if sha256Digest(tree) != "sha256:9bcf048c3bd0e7197ba3a33fd24df085d4140adcf327c09e14870ebff8e854e9" {
		t.Fatal("canonical payload-tree JSON mismatch")
	}
	if domainSeparatedDigest([]byte("propertyquarry.release-control.payload-tree.v2\x00"), tree) !=
		"sha256:884dba36f555f872d1cecc6af09bdd9c20b44d5122e9eb4f385eb059e88e1ac0" {
		t.Fatal("framed payload-tree digest mismatch")
	}

	authentication := []byte(localAuthVectorAuthJSON)
	if sha256Digest(authentication) != "sha256:88c05d520ecd85db0eeb6f8823f5c034e528c2bf3a7363df5ab7a12f14db7282" {
		t.Fatal("canonical authentication JSON mismatch")
	}
	parsed, err := parseLocalPackageAuthentication(authentication)
	if err != nil {
		t.Fatal(err)
	}
	defer parsed.release()
	if parsed.KeyID != localAuthVectorKeyID || parsed.Payload.TreeDigest !=
		"sha256:884dba36f555f872d1cecc6af09bdd9c20b44d5122e9eb4f385eb059e88e1ac0" {
		t.Fatal("authentication vector contract mismatch")
	}

	message := domainSeparatedMessage([]byte(localAuthenticationDomain), authentication)
	defer zero(message)
	if sha256Digest(message) != "sha256:e99b6278dd9520d8106b9e60e23d07ce4b93fa04a8a3a107dcd6eb4978993314" {
		t.Fatal("framed authentication message mismatch")
	}
	signature := ed25519.Sign(privateKey, message)
	expectedSignature, err := hex.DecodeString(localAuthVectorSigHex)
	if err != nil || !bytes.Equal(signature, expectedSignature) || !ed25519.Verify(parsedPublicKey, message, signature) {
		t.Fatal("fixed-seed authentication signature mismatch")
	}
}

func newInstalledAuthorityFixture(t *testing.T) *installedAuthorityFixture {
	t.Helper()
	root, err := os.MkdirTemp("/tmp", "pqra.")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(root) })
	uid := uint32(os.Getuid())
	gid := uint32(os.Getgid())
	paths := defaultInstalledRuntimePaths()
	paths.Root = root
	paths.RootUID = uid
	paths.RootGID = gid
	paths.RuntimeUID = uid
	paths.RuntimeGID = gid
	paths.RuntimeRoot = "/r"
	paths.RequestSocket = "/r/request.sock"
	paths.RunningExecutable = ""

	seed := make([]byte, ed25519.SeedSize)
	for index := range seed {
		seed[index] = byte(index)
	}
	privateKey := ed25519.NewKeyFromSeed(seed)
	publicKey := privateKey.Public().(ed25519.PublicKey)
	der, err := x509.MarshalPKIXPublicKey(publicKey)
	if err != nil {
		t.Fatal(err)
	}
	publicPEM := pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der})
	_, keyID, err := parseEd25519PublicAnchor(publicPEM)
	if err != nil {
		t.Fatal(err)
	}

	fixtureMkdir(t, rootedUnchecked(root, paths.StateRoot), 0o700)
	fixtureMkdir(t, rootedUnchecked(root, paths.RuntimeRoot), 0o700)
	fixtureWrite(t, rootedUnchecked(root, paths.ExternalAnchor), publicPEM, 0o444)

	roleValues := make(map[string][]byte, len(installedRoleContracts))
	for _, contract := range installedRoleContracts {
		value := []byte("role:" + contract.Role + "\n")
		if contract.Role == "controller-executable" {
			value = []byte("#!/bin/sh\nexit 50\n")
		}
		if contract.Role == "package-trust-root" {
			value = append([]byte(nil), publicPEM...)
		}
		roleValues[contract.Role] = value
		fixtureWrite(t, rootedUnchecked(root, contract.Path), value, os.FileMode(contract.Mode))
		retained := filepath.Join(rootedUnchecked(root, paths.PayloadRoot), "rootfs", strings.TrimPrefix(contract.Path, "/"))
		fixtureWrite(t, retained, value, os.FileMode(contract.Mode))
	}
	fixtureNormalizePayloadDirectories(t, rootedUnchecked(root, paths.PayloadRoot))

	manifestRoles := make([]any, 0, len(installedRoleContracts))
	for _, contract := range installedRoleContracts {
		gidValue := paths.RootGID
		if contract.Private {
			gidValue = paths.RuntimeGID
		}
		value := roleValues[contract.Role]
		manifestRoles = append(manifestRoles, map[string]any{
			"role":   contract.Role,
			"path":   contract.Path,
			"sha256": sha256Digest(value),
			"size":   json.Number(strconv.Itoa(len(value))),
			"mode":   json.Number(strconv.FormatUint(uint64(contract.Mode), 10)),
			"uid":    json.Number(strconv.FormatUint(uint64(paths.RootUID), 10)),
			"gid":    json.Number(strconv.FormatUint(uint64(gidValue), 10)),
		})
	}
	manifest, err := canonicalJSON(map[string]any{
		"schema":  "propertyquarry.release-installation-manifest.v2",
		"version": json.Number("2"),
		"roles":   manifestRoles,
	})
	if err != nil {
		t.Fatal(err)
	}
	receipt, err := canonicalJSON(map[string]any{
		"schema": "propertyquarry.release-control.package-payload-receipt.v2",
		"input_integrity": map[string]any{
			"native_build_receipt_sha256": fixtureNativeBuildDigest,
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	fixtureWrite(t, filepath.Join(rootedUnchecked(root, paths.PayloadRoot), "installation-manifest.v2.json"), manifest, 0o644)
	fixtureWrite(t, filepath.Join(rootedUnchecked(root, paths.PayloadRoot), "package-payload-receipt.v2.json"), receipt, 0o644)
	fixtureNormalizePayloadDirectories(t, rootedUnchecked(root, paths.PayloadRoot))

	tree, err := collectPayloadTree(
		paths.Root,
		paths.PayloadRoot,
		expectedFileMetadata{Mode: 0o755, UID: uid, GID: gid},
	)
	if err != nil {
		t.Fatal(err)
	}
	authentication, err := canonicalJSON(map[string]any{
		"schema":  localAuthenticationSchema,
		"version": json.Number("2"),
		"signature_profile": map[string]any{
			"algorithm":      "ed25519",
			"encoding":       "raw-64-byte",
			"key_id":         keyID,
			"signed_message": "domain-separated-uint64be-length-prefixed-canonical-json",
		},
		"authority_scope": map[string]any{
			"kind":     "local-docker",
			"scope_id": "propertyquarry-local-docker",
			"authoritative_for_package_authentication": true,
			"external_production_authority":            false,
			"public_launch_authority":                  false,
			"performs_release_effects":                 false,
		},
		"payload": map[string]any{
			"tree_digest":                    tree.Digest,
			"file_count":                     json.Number(strconv.FormatInt(tree.FileCount, 10)),
			"directory_count":                json.Number(strconv.FormatInt(tree.DirectoryCount, 10)),
			"role_count":                     json.Number(strconv.Itoa(installedRoleCount)),
			"installation_manifest_sha256":   sha256Digest(manifest),
			"package_payload_receipt_sha256": sha256Digest(receipt),
			"native_build_receipt_sha256":    fixtureNativeBuildDigest,
		},
	})
	tree.release()
	if err != nil {
		t.Fatal(err)
	}
	message := domainSeparatedMessage([]byte(localAuthenticationDomain), authentication)
	signature := ed25519.Sign(privateKey, message)
	zero(message)
	fixtureWrite(t, rootedUnchecked(root, paths.Authentication), authentication, 0o644)
	fixtureWrite(t, rootedUnchecked(root, paths.Signature), signature, 0o644)
	return &installedAuthorityFixture{
		paths:          paths,
		authentication: authentication,
		signature:      signature,
		publicPEM:      publicPEM,
		keyID:          keyID,
		privateKey:     privateKey,
	}
}

func fixtureMkdir(t *testing.T, target string, mode os.FileMode) {
	t.Helper()
	if err := os.MkdirAll(target, mode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(target, mode); err != nil {
		t.Fatal(err)
	}
}

func fixtureWrite(t *testing.T, target string, value []byte, mode os.FileMode) {
	t.Helper()
	fixtureMkdir(t, filepath.Dir(target), 0o755)
	if err := os.WriteFile(target, value, mode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(target, mode); err != nil {
		t.Fatal(err)
	}
}

func fixtureNormalizePayloadDirectories(t *testing.T, root string) {
	t.Helper()
	err := filepath.WalkDir(root, func(current string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !entry.IsDir() {
			return nil
		}
		mode := os.FileMode(0o755)
		relative, err := filepath.Rel(root, current)
		if err != nil {
			return err
		}
		if relative != "." && strings.HasPrefix(filepath.ToSlash(relative), "rootfs/etc/propertyquarry-release-control") {
			mode = 0o750
		}
		return os.Chmod(current, mode)
	})
	if err != nil {
		t.Fatal(err)
	}
}

func fixtureResignPayloadTree(t *testing.T, fixture *installedAuthorityFixture) {
	t.Helper()
	tree, err := collectPayloadTree(
		fixture.paths.Root,
		fixture.paths.PayloadRoot,
		expectedFileMetadata{Mode: 0o755, UID: fixture.paths.RootUID, GID: fixture.paths.RootGID},
	)
	if err != nil {
		t.Fatal(err)
	}
	defer tree.release()
	value, err := decodeStrictJSON(fixture.authentication)
	if err != nil {
		t.Fatal(err)
	}
	outer, ok := value.(map[string]any)
	if !ok {
		t.Fatal("authentication fixture invalid")
	}
	payload, ok := outer["payload"].(map[string]any)
	if !ok {
		t.Fatal("authentication payload fixture invalid")
	}
	payload["tree_digest"] = tree.Digest
	payload["file_count"] = json.Number(strconv.FormatInt(tree.FileCount, 10))
	payload["directory_count"] = json.Number(strconv.FormatInt(tree.DirectoryCount, 10))
	authentication, err := canonicalJSON(outer)
	if err != nil {
		t.Fatal(err)
	}
	message := domainSeparatedMessage([]byte(localAuthenticationDomain), authentication)
	signature := ed25519.Sign(fixture.privateKey, message)
	zero(message)
	fixtureWrite(t, rootedUnchecked(fixture.paths.Root, fixture.paths.Authentication), authentication, 0o644)
	fixtureWrite(t, rootedUnchecked(fixture.paths.Root, fixture.paths.Signature), signature, 0o644)
	fixture.authentication = authentication
	fixture.signature = signature
}

func TestInstalledAuthorityAuthenticatesRetainedAndActivePayload(t *testing.T) {
	fixture := newInstalledAuthorityFixture(t)
	verification, err := validateInstalledLocalAuthority(Supervisor, fixture.paths)
	if err != nil {
		t.Fatal(err)
	}
	if verification.AuthorityKeyID != fixture.keyID ||
		verification.AuthenticationDigest != sha256Digest(fixture.authentication) ||
		verification.NativeBuildDigest != fixtureNativeBuildDigest ||
		len(verification.Roles) != installedRoleCount {
		t.Fatalf("verification mismatch: %#v", verification)
	}
}

func TestInstalledAuthorityRejectsAuthenticationAndFilesystemAttacks(t *testing.T) {
	tests := map[string]func(*testing.T, *installedAuthorityFixture){
		"authentication-signature": func(t *testing.T, fixture *installedAuthorityFixture) {
			value := append([]byte(nil), fixture.signature...)
			value[0] ^= 0x80
			fixtureWrite(t, rootedUnchecked(fixture.paths.Root, fixture.paths.Signature), value, 0o644)
		},
		"authentication-noncanonical": func(t *testing.T, fixture *installedAuthorityFixture) {
			value := append(append([]byte(nil), fixture.authentication...), '\n')
			fixtureWrite(t, rootedUnchecked(fixture.paths.Root, fixture.paths.Authentication), value, 0o644)
		},
		"external-anchor": func(t *testing.T, fixture *installedAuthorityFixture) {
			seed := bytes.Repeat([]byte{0x55}, ed25519.SeedSize)
			key := ed25519.NewKeyFromSeed(seed).Public().(ed25519.PublicKey)
			der, err := x509.MarshalPKIXPublicKey(key)
			if err != nil {
				t.Fatal(err)
			}
			target := rootedUnchecked(fixture.paths.Root, fixture.paths.ExternalAnchor)
			if err := os.Chmod(target, 0o600); err != nil {
				t.Fatal(err)
			}
			fixtureWrite(t, target, pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der}), 0o444)
		},
		"external-anchor-mode": func(t *testing.T, fixture *installedAuthorityFixture) {
			if err := os.Chmod(rootedUnchecked(fixture.paths.Root, fixture.paths.ExternalAnchor), 0o644); err != nil {
				t.Fatal(err)
			}
		},
		"external-anchor-hardlink": func(t *testing.T, fixture *installedAuthorityFixture) {
			target := rootedUnchecked(fixture.paths.Root, fixture.paths.ExternalAnchor)
			if err := os.Link(target, target+".alias"); err != nil {
				t.Fatal(err)
			}
		},
		"retained-extra": func(t *testing.T, fixture *installedAuthorityFixture) {
			fixtureWrite(t, filepath.Join(rootedUnchecked(fixture.paths.Root, fixture.paths.PayloadRoot), "extra"), []byte("x"), 0o644)
		},
		"retained-symlink": func(t *testing.T, fixture *installedAuthorityFixture) {
			target := filepath.Join(rootedUnchecked(fixture.paths.Root, fixture.paths.PayloadRoot), "installation-manifest.v2.json")
			if err := os.Remove(target); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink("package-payload-receipt.v2.json", target); err != nil {
				t.Fatal(err)
			}
		},
		"retained-mode": func(t *testing.T, fixture *installedAuthorityFixture) {
			target := filepath.Join(rootedUnchecked(fixture.paths.Root, fixture.paths.PayloadRoot), "installation-manifest.v2.json")
			if err := os.Chmod(target, 0o600); err != nil {
				t.Fatal(err)
			}
		},
		"active-content": func(t *testing.T, fixture *installedAuthorityFixture) {
			target := rootedUnchecked(fixture.paths.Root, ControllerConfig)
			info, err := os.Stat(target)
			if err != nil {
				t.Fatal(err)
			}
			fixtureWrite(t, target, bytes.Repeat([]byte{'x'}, int(info.Size())), 0o640)
		},
		"active-hardlink": func(t *testing.T, fixture *installedAuthorityFixture) {
			target := rootedUnchecked(fixture.paths.Root, ControllerConfig)
			link := target + ".second-link"
			if err := os.Link(target, link); err != nil {
				t.Fatal(err)
			}
		},
		"state-entry": func(t *testing.T, fixture *installedAuthorityFixture) {
			fixtureWrite(t, filepath.Join(rootedUnchecked(fixture.paths.Root, fixture.paths.StateRoot), "unsigned-state"), []byte("x"), 0o600)
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			fixture := newInstalledAuthorityFixture(t)
			mutate(t, fixture)
			if verification, err := validateInstalledLocalAuthority(Supervisor, fixture.paths); err == nil {
				t.Fatalf("attack accepted: %#v", verification)
			}
		})
	}
}

func TestInstalledAuthorityRejectsSignerAuthorizedUnexpectedRetainedDirectory(t *testing.T) {
	fixture := newInstalledAuthorityFixture(t)
	fixtureMkdir(
		t,
		filepath.Join(rootedUnchecked(fixture.paths.Root, fixture.paths.PayloadRoot), "rootfs", "unexpected"),
		0o755,
	)
	fixtureResignPayloadTree(t, fixture)
	if verification, err := validateInstalledLocalAuthority(Supervisor, fixture.paths); err == nil {
		t.Fatalf("signed open-ended payload shape accepted: %#v", verification)
	}
}

func TestInstalledSupervisorOwnsSocketAndForksPinnedController(t *testing.T) {
	fixture := newInstalledAuthorityFixture(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	served := make(chan error, 1)
	go func() { served <- serveInstalledSupervisor(ctx, fixture.paths) }()
	socketPath := rootedUnchecked(fixture.paths.Root, fixture.paths.RequestSocket)
	waitForInstalledSocket(t, socketPath, served)

	client, err := net.DialUnix("unix", nil, &net.UnixAddr{Name: socketPath, Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	clientFD := duplicateUnixFD(t, client)
	_ = client.Close()
	response := brokerPipe(t)
	defer syscall.Close(response[0])
	if err := sendBrokerWire(clientFD, []byte(crossLanguageGoldenRequest), brokerWireOptions{
		responseFDs: []int{response[1]},
		halfClose:   true,
	}); err != nil {
		t.Fatal(err)
	}
	awaitPipeEOF(t, response[0])
	cancel()
	select {
	case err := <-served:
		if err != nil {
			t.Fatalf("%v: %v", err, errors.Unwrap(err))
		}
	case <-time.After(2 * time.Second):
		t.Fatal("installed supervisor did not stop")
	}
}

func TestInstalledPersistentLifecycleHealthAndRequestSmoke(t *testing.T) {
	fixture := newInstalledAuthorityFixture(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	served := make(chan error, 1)
	go func() { served <- serveInstalledSupervisor(ctx, fixture.paths) }()
	socketPath := rootedUnchecked(fixture.paths.Root, fixture.paths.RequestSocket)
	waitForInstalledSocket(t, socketPath, served)

	var oneShot bytes.Buffer
	if err := writeInstalledHealth(fixture.paths, &oneShot); err != nil {
		t.Fatal(err)
	}
	if oneShot.Len() < 2 || oneShot.Bytes()[oneShot.Len()-1] != '\n' ||
		bytes.Count(oneShot.Bytes(), []byte{'\n'}) != 1 {
		t.Fatalf("one-shot health framing invalid: %q", oneShot.Bytes())
	}
	var persistent bytes.Buffer
	watchContext, stopWatch := context.WithCancel(context.Background())
	watchResult := make(chan error, 1)
	go func() {
		watchResult <- watchInstalledAuthority(
			watchContext,
			fixture.paths,
			10*time.Millisecond,
			&persistent,
		)
	}()
	time.Sleep(25 * time.Millisecond)
	stopWatch()
	if err := <-watchResult; err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(persistent.Bytes(), oneShot.Bytes()) {
		t.Fatalf("persistent initial health differs:\n got %q\nwant %q", persistent.Bytes(), oneShot.Bytes())
	}
	if err := runInstalledRequestSmokeWithPaths(fixture.paths); err != nil {
		t.Fatal(err)
	}
	if _, err := validateInstalledSocket(fixture.paths, true); err != nil {
		t.Fatal(err)
	}
	if _, err := inspectStableRootedDirectory(
		fixture.paths.Root,
		fixture.paths.StateRoot,
		expectedFileMetadata{Mode: 0o700, UID: fixture.paths.RuntimeUID, GID: fixture.paths.RuntimeGID},
		true,
	); err != nil {
		t.Fatalf("request smoke changed phase-A state: %v", err)
	}
	cancel()
	if err := <-served; err != nil {
		t.Fatal(err)
	}
}

func TestInstalledRestartStimulusAuthenticatesLiveSupervisorBeforeSignal(t *testing.T) {
	fixture := newInstalledAuthorityFixture(t)
	ctx, cancel := context.WithCancel(context.Background())
	served := make(chan error, 1)
	go func() { served <- serveInstalledSupervisor(ctx, fixture.paths) }()
	waitForInstalledSocket(t, rootedUnchecked(fixture.paths.Root, fixture.paths.RequestSocket), served)
	target := rootedUnchecked(fixture.paths.Root, SupervisorExecutable)
	called := false
	err := signalInstalledSupervisorRestart(
		fixture.paths,
		target,
		4242,
		func(pid int, signal syscall.Signal) error {
			called = true
			if pid != 4242 || signal != syscall.SIGUSR2 {
				t.Fatalf("restart stimulus changed: pid=%d signal=%d", pid, signal)
			}
			return nil
		},
	)
	if err != nil || !called {
		cancel()
		t.Fatalf("authenticated restart stimulus failed: called=%v err=%v", called, err)
	}
	called = false
	if err := signalInstalledSupervisorRestart(
		fixture.paths,
		rootedUnchecked(fixture.paths.Root, ControllerExecutable),
		4242,
		func(_ int, _ syscall.Signal) error { called = true; return nil },
	); err == nil || called {
		cancel()
		t.Fatal("restart stimulus signaled an unauthenticated target")
	}
	cancel()
	if err := <-served; err != nil {
		t.Fatal(err)
	}
}

func TestInstalledRequestSmokeRejectsResponseBytesOrOpenPipe(t *testing.T) {
	t.Run("response-byte", func(t *testing.T) {
		reader, writer, err := os.Pipe()
		if err != nil {
			t.Fatal(err)
		}
		defer reader.Close()
		if _, err := writer.Write([]byte{'x'}); err != nil {
			t.Fatal(err)
		}
		if err := writer.Close(); err != nil {
			t.Fatal(err)
		}
		if err := awaitInstalledResponseEOF(reader, time.Second); err == nil {
			t.Fatal("request smoke accepted a response byte")
		}
	})
	t.Run("open-pipe", func(t *testing.T) {
		reader, writer, err := os.Pipe()
		if err != nil {
			t.Fatal(err)
		}
		defer reader.Close()
		defer writer.Close()
		if err := awaitInstalledResponseEOF(reader, 10*time.Millisecond); err == nil {
			t.Fatal("request smoke accepted an open response pipe")
		}
	})
}

func TestInstalledSupervisorRecoversOnlyAValidatedStaleSocket(t *testing.T) {
	fixture := newInstalledAuthorityFixture(t)
	socketPath := rootedUnchecked(fixture.paths.Root, fixture.paths.RequestSocket)
	stale, err := net.ListenUnix("unix", &net.UnixAddr{Name: socketPath, Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	stale.SetUnlinkOnClose(false)
	if err := os.Chmod(socketPath, installedSocketMode); err != nil {
		t.Fatal(err)
	}
	if err := stale.Close(); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	served := make(chan error, 1)
	go func() { served <- serveInstalledSupervisor(ctx, fixture.paths) }()
	waitForInstalledSocket(t, socketPath, served)
	if err := runInstalledRequestSmokeWithPaths(fixture.paths); err != nil {
		cancel()
		t.Fatal(err)
	}
	cancel()
	if err := <-served; err != nil {
		t.Fatal(err)
	}
}

func TestInstalledSupervisorRefusesLiveSocketTakeover(t *testing.T) {
	fixture := newInstalledAuthorityFixture(t)
	socketPath := rootedUnchecked(fixture.paths.Root, fixture.paths.RequestSocket)
	live, err := net.ListenUnix("unix", &net.UnixAddr{Name: socketPath, Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	defer live.Close()
	if err := os.Chmod(socketPath, installedSocketMode); err != nil {
		t.Fatal(err)
	}
	if _, err := prepareInstalledRuntimeDirectory(fixture.paths); err == nil {
		t.Fatal("live request socket was taken over")
	}
	if _, err := os.Lstat(socketPath); err != nil {
		t.Fatalf("live request socket was removed: %v", err)
	}
}

func TestInstalledWatchdogReportsReadyOnlyForVerifiedRuntime(t *testing.T) {
	fixture := newInstalledAuthorityFixture(t)
	ctx, cancel := context.WithCancel(context.Background())
	served := make(chan error, 1)
	go func() { served <- serveInstalledSupervisor(ctx, fixture.paths) }()
	waitForInstalledSocket(t, rootedUnchecked(fixture.paths.Root, fixture.paths.RequestSocket), served)
	var output bytes.Buffer
	if err := writeInstalledHealth(fixture.paths, &output); err != nil {
		cancel()
		t.Fatal(err)
	}
	var health map[string]any
	if err := json.Unmarshal(output.Bytes(), &health); err != nil {
		cancel()
		t.Fatal(err)
	}
	if health["ready"] != true || health["installed_local_authority_verified"] != true ||
		health["authoritative_for_package_authentication"] != true ||
		health["authoritative_for_release_effects"] != false ||
		health["performs_release_effects"] != false || output.Len() > 4096 {
		cancel()
		t.Fatalf("health claim invalid: %s", output.Bytes())
	}
	canonicalValue, err := decodeStrictJSON(bytes.TrimSuffix(output.Bytes(), []byte{'\n'}))
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	canonicalHealth, ok := canonicalValue.(map[string]any)
	if !ok || !hasExactKeys(
		canonicalHealth,
		"authentication_digest",
		"authoritative_for_package_authentication",
		"authoritative_for_release_effects",
		"authority_key_id",
		"component",
		"installed_local_authority_verified",
		"payload_tree_digest",
		"performs_release_effects",
		"production_ready",
		"ready",
		"schema",
		"socket_accepting",
		"source_manifest_digest",
		"version",
	) || !exactStringEquals(canonicalHealth["schema"], localHealthSchema) ||
		!exactStringEquals(canonicalHealth["component"], string(Watchdog)) ||
		!exactStringEquals(canonicalHealth["source_manifest_digest"], SourceManifestDigest) {
		cancel()
		t.Fatalf("health canonical contract invalid: %#v", canonicalValue)
	}
	if err := writeInstalledHealth(fixture.paths, shortWriter{}); err == nil {
		cancel()
		t.Fatal("short health write was accepted")
	}
	cancel()
	if err := <-served; err != nil {
		t.Fatal(err)
	}
	if err := writeInstalledHealth(fixture.paths, &bytes.Buffer{}); err == nil {
		t.Fatal("watchdog reported ready without a live socket")
	}
}

func TestInstalledSupervisorExitsWhenActiveControllerDrifts(t *testing.T) {
	fixture := newInstalledAuthorityFixture(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	served := make(chan error, 1)
	go func() { served <- serveInstalledSupervisor(ctx, fixture.paths) }()
	socketPath := rootedUnchecked(fixture.paths.Root, fixture.paths.RequestSocket)
	waitForInstalledSocket(t, socketPath, served)
	fixtureWrite(t, rootedUnchecked(fixture.paths.Root, ControllerExecutable), []byte("#!/bin/sh\nexit 51\n"), 0o755)

	client, err := net.DialUnix("unix", nil, &net.UnixAddr{Name: socketPath, Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	clientFD := duplicateUnixFD(t, client)
	_ = client.Close()
	response := brokerPipe(t)
	defer syscall.Close(response[0])
	if err := sendBrokerWire(clientFD, []byte(crossLanguageGoldenRequest), brokerWireOptions{
		responseFDs: []int{response[1]},
		halfClose:   true,
	}); err != nil && err != syscall.EPIPE {
		t.Fatal(err)
	}
	awaitPipeEOF(t, response[0])
	select {
	case err := <-served:
		var terminal installedTerminalError
		if !errors.As(err, &terminal) {
			t.Fatalf("controller drift was not terminal: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("supervisor stayed alive after controller drift")
	}
}

func TestInstalledWatchdogExitsOnAnchorPackageOrStateDrift(t *testing.T) {
	mutations := map[string]func(*testing.T, *installedAuthorityFixture){
		"external-anchor": func(t *testing.T, fixture *installedAuthorityFixture) {
			target := rootedUnchecked(fixture.paths.Root, fixture.paths.ExternalAnchor)
			if err := os.Chmod(target, 0o600); err != nil {
				t.Fatal(err)
			}
			fixtureWrite(t, target, []byte("not-an-anchor\n"), 0o444)
		},
		"retained-package": func(t *testing.T, fixture *installedAuthorityFixture) {
			target := filepath.Join(
				rootedUnchecked(fixture.paths.Root, fixture.paths.PayloadRoot),
				"installation-manifest.v2.json",
			)
			value, err := os.ReadFile(target)
			if err != nil {
				t.Fatal(err)
			}
			fixtureWrite(t, target, append(value, '\n'), 0o644)
		},
		"state": func(t *testing.T, fixture *installedAuthorityFixture) {
			fixtureWrite(
				t,
				filepath.Join(rootedUnchecked(fixture.paths.Root, fixture.paths.StateRoot), "unsigned-state"),
				[]byte("x"),
				0o600,
			)
		},
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			fixture := newInstalledAuthorityFixture(t)
			serverContext, stopServer := context.WithCancel(context.Background())
			served := make(chan error, 1)
			go func() { served <- serveInstalledSupervisor(serverContext, fixture.paths) }()
			waitForInstalledSocket(t, rootedUnchecked(fixture.paths.Root, fixture.paths.RequestSocket), served)
			watchContext, stopWatch := context.WithCancel(context.Background())
			defer stopWatch()
			result := make(chan error, 1)
			go func() {
				result <- watchInstalledAuthority(watchContext, fixture.paths, 10*time.Millisecond, &bytes.Buffer{})
			}()
			time.Sleep(20 * time.Millisecond)
			mutate(t, fixture)
			select {
			case err := <-result:
				if err == nil {
					t.Fatal("watchdog accepted installed-authority drift")
				}
			case <-time.After(2 * time.Second):
				t.Fatal("watchdog did not exit on installed-authority drift")
			}
			stopServer()
			if err := <-served; err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestInstalledWatchdogPinsSocketGenerationAcrossFastSupervisorRestart(t *testing.T) {
	fixture := newInstalledAuthorityFixture(t)
	firstContext, stopFirst := context.WithCancel(context.Background())
	firstResult := make(chan error, 1)
	go func() { firstResult <- serveInstalledSupervisor(firstContext, fixture.paths) }()
	waitForInstalledSocket(
		t,
		rootedUnchecked(fixture.paths.Root, fixture.paths.RequestSocket),
		firstResult,
	)

	watchContext, stopWatch := context.WithCancel(context.Background())
	defer stopWatch()
	watchResult := make(chan error, 1)
	ready := make(installedHealthReady, 1)
	go func() {
		watchResult <- watchInstalledAuthority(
			watchContext,
			fixture.paths,
			time.Hour,
			ready,
		)
	}()
	select {
	case <-ready:
	case <-time.After(2 * time.Second):
		t.Fatal("persistent watchdog did not become ready")
	}

	stopFirst()
	if err := <-firstResult; err != nil {
		t.Fatal(err)
	}
	secondContext, stopSecond := context.WithCancel(context.Background())
	secondResult := make(chan error, 1)
	go func() { secondResult <- serveInstalledSupervisor(secondContext, fixture.paths) }()
	waitForInstalledSocket(
		t,
		rootedUnchecked(fixture.paths.Root, fixture.paths.RequestSocket),
		secondResult,
	)
	select {
	case err := <-watchResult:
		if err == nil {
			stopSecond()
			t.Fatal("watchdog accepted a replacement socket generation")
		}
	case <-time.After(2 * time.Second):
		stopSecond()
		t.Fatal("watchdog missed a fast supervisor replacement")
	}
	stopSecond()
	if err := <-secondResult; err != nil {
		t.Fatal(err)
	}
}

func TestInstalledControllerExtensionClosesPinnedExecutableAndStillRefuses(t *testing.T) {
	response := brokerPipe(t)
	defer syscall.Close(response[0])
	executablePath := filepath.Join(t.TempDir(), "controller")
	fixtureWrite(t, executablePath, []byte("fixed-controller"), 0o755)
	executable, err := syscall.Open(executablePath, syscall.O_RDONLY|syscall.O_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	code := Run(Controller, []string{
		"--config", ControllerConfig,
		"--operation", "release-preflight",
		"--response-fd", strconv.Itoa(response[1]),
		"--event-id", "local-event",
		"--request-transport-digest", "sha256:" + strings.Repeat("a", 64),
		"--installed-local-authority-executable-fd", strconv.Itoa(executable),
	}, &bytes.Buffer{}, &bytes.Buffer{})
	if code != ExitProtocolFailure {
		t.Fatalf("installed controller returned %d", code)
	}
	var stat syscall.Stat_t
	if err := syscall.Fstat(executable, &stat); err != syscall.EBADF {
		t.Fatalf("executable descriptor retained: %v", err)
	}
	awaitPipeEOF(t, response[0])
}

func waitForInstalledSocket(t *testing.T, target string, served <-chan error) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for {
		select {
		case err := <-served:
			t.Fatalf("installed supervisor exited before readiness: %v", err)
		default:
		}
		info, err := os.Lstat(target)
		if err == nil && info.Mode()&os.ModeSocket != 0 {
			connection, dialErr := net.DialTimeout("unix", target, 20*time.Millisecond)
			if dialErr == nil {
				_ = connection.Close()
				return
			}
			err = dialErr
		}
		if time.Until(deadline) <= 0 {
			t.Fatalf("installed socket unavailable: %v", err)
		}
		time.Sleep(time.Millisecond)
	}
}

func duplicateUnixFD(t *testing.T, connection *net.UnixConn) int {
	t.Helper()
	raw, err := connection.SyscallConn()
	if err != nil {
		t.Fatal(err)
	}
	duplicate := -1
	var operationError error
	if err := raw.Control(func(fd uintptr) {
		duplicate, operationError = fcntl(int(fd), syscall.F_DUPFD_CLOEXEC, 3)
	}); err != nil {
		t.Fatal(err)
	}
	if operationError != nil || duplicate < 0 {
		t.Fatalf("socket duplication failed: %v", operationError)
	}
	return duplicate
}
