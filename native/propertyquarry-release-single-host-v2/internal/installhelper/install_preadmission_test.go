package installhelper

import (
	"crypto/ed25519"
	"encoding/gob"
	"encoding/hex"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

const (
	installerSIGKILLHelperEnvironment = "PROPERTYQUARRY_INSTALL_SIGKILL_HELPER"
	installerSIGKILLPointEnvironment  = "PROPERTYQUARRY_INSTALL_SIGKILL_POINT"
	installerSIGKILLWireEnvironment   = "PROPERTYQUARRY_INSTALL_SIGKILL_WIRE"
)

type installerSIGKILLWire struct {
	HostRoot                  string
	PackageAuthorityDERBase64 string
	ReceiptKey                []byte
	Verified                  VerifiedPackage
}

func writeInstallerSIGKILLWire(t *testing.T, fixture *installFixture, verified *VerifiedPackage) string {
	t.Helper()
	path := filepath.Join(fixture.root, "installer-sigkill-wire.gob")
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	wire := installerSIGKILLWire{
		HostRoot:                  fixture.root,
		PackageAuthorityDERBase64: EmbeddedPackageAuthorityDERBase64,
		ReceiptKey:                append([]byte(nil), fixture.receiptKey...),
		Verified:                  *verified,
	}
	encodeErr := gob.NewEncoder(file).Encode(&wire)
	syncErr := file.Sync()
	closeErr := file.Close()
	zero(wire.ReceiptKey)
	if encodeErr != nil || syncErr != nil || closeErr != nil {
		t.Fatalf("encode SIGKILL fixture: encode=%v sync=%v close=%v", encodeErr, syncErr, closeErr)
	}
	return path
}

func runInstallerSIGKILLChild(t *testing.T, wirePath, point string) {
	t.Helper()
	command := exec.Command(os.Args[0], "-test.run=^TestInstallerSIGKILLHelperProcess$", "-test.count=1")
	command.Env = append(os.Environ(),
		installerSIGKILLHelperEnvironment+"=1",
		installerSIGKILLPointEnvironment+"="+point,
		installerSIGKILLWireEnvironment+"="+wirePath,
	)
	output, err := command.CombinedOutput()
	exit, ok := err.(*exec.ExitError)
	status, statusOK := exit.Sys().(syscall.WaitStatus)
	if !ok || !statusOK || !status.Signaled() || status.Signal() != syscall.SIGKILL {
		t.Fatalf("child did not die by SIGKILL at %q: err=%v output=%s", point, err, output)
	}
}

func TestInstallerSIGKILLHelperProcess(t *testing.T) {
	if os.Getenv(installerSIGKILLHelperEnvironment) != "1" {
		return
	}
	wirePath := os.Getenv(installerSIGKILLWireEnvironment)
	point := os.Getenv(installerSIGKILLPointEnvironment)
	if wirePath == "" || point == "" {
		t.Fatal("SIGKILL helper environment incomplete")
	}
	file, err := os.Open(wirePath)
	if err != nil {
		t.Fatal(err)
	}
	var wire installerSIGKILLWire
	decodeErr := gob.NewDecoder(file).Decode(&wire)
	closeErr := file.Close()
	if decodeErr != nil || closeErr != nil {
		t.Fatalf("decode SIGKILL fixture: decode=%v close=%v", decodeErr, closeErr)
	}
	defer zero(wire.ReceiptKey)
	EmbeddedPackageAuthorityDERBase64 = wire.PackageAuthorityDERBase64
	fixture := &installFixture{root: wire.HostRoot, receiptKey: ed25519.PrivateKey(wire.ReceiptKey)}
	installer := freshFixtureInstaller(fixture, t)
	installer.Interrupt = func(candidate string) bool {
		if candidate != point {
			return false
		}
		_ = syscall.Kill(os.Getpid(), syscall.SIGKILL)
		select {}
	}
	previousUmask := syscall.Umask(0o777)
	defer syscall.Umask(previousUmask)
	if _, err := installer.Install(&wire.Verified); err != nil {
		t.Fatalf("SIGKILL point %q was not reached: %v", point, err)
	}
	t.Fatalf("SIGKILL point %q was not reached", point)
}

func freshFixtureInstaller(fixture *installFixture, t *testing.T) *Installer {
	t.Helper()
	return &Installer{
		HostRoot:   fixture.root,
		OwnerUID:   uint32(os.Geteuid()),
		OwnerGID:   uint32(os.Getegid()),
		Activate:   fixture.activate(t),
		Deactivate: func() error { return nil },
	}
}

func persistedPreAdmissionKeyID(t *testing.T, fixture *installFixture) string {
	t.Helper()
	for _, absolute := range []string{preAdmissionPath, preAdmissionPendingPath} {
		path := filepath.Join(fixture.root, strings.TrimPrefix(absolute, "/"))
		raw, err := os.ReadFile(path)
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			t.Fatal(err)
		}
		wrapper, err := strictJSON(raw, maximumManifestBytes)
		if err != nil {
			t.Fatal(err)
		}
		payload, ok := wrapper["payload"].(map[string]any)
		if !ok {
			t.Fatal("pre-admission payload missing")
		}
		keyID, ok := exactString(payload["backup_encryption_key_id"])
		if !ok || !digestPattern.MatchString(keyID) {
			t.Fatal("pre-admission backup key id invalid")
		}
		return keyID
	}
	keyPath := filepath.Join(fixture.root, strings.TrimPrefix(backupEncryptionKeyPath, "/"))
	raw, err := os.ReadFile(keyPath)
	if err != nil || len(raw) != 65 {
		t.Fatalf("persisted backup key unavailable: %v", err)
	}
	decoded := make([]byte, 32)
	if count, err := hex.Decode(decoded, raw[:64]); err != nil || count != len(decoded) {
		t.Fatalf("persisted backup key invalid: %v", err)
	}
	defer zero(decoded)
	return digest(decoded)
}

func durableWriteSIGKILLPoints(scope string) []string {
	points := make([]string, 0, 8)
	for _, operation := range []string{"create", "chown", "chmod", "partial-write", "write", "file-fsync", "close", "parent-fsync"} {
		points = append(points, scope+"-after-"+operation)
	}
	return points
}

func TestGenesisRealSIGKILLConvergesAcrossEveryInnerDurabilityBoundary(t *testing.T) {
	points := []string{
		"pre-admission-publish-after-rename",
		"pre-admission-publish-after-parent-fsync",
		"backup-key-directory-after-mkdir",
		"backup-key-directory-after-chown",
		"backup-key-directory-after-chmod",
		"backup-key-directory-after-parent-fsync",
		"backup-key-publish-after-rename",
		"backup-key-publish-after-parent-fsync",
		"install-directory-1-after-mkdir",
		"install-directory-1-after-chown",
		"install-directory-1-after-chmod",
		"install-directory-1-after-parent-fsync",
		"journal-admitted-publish-after-rename",
		"journal-admitted-publish-after-parent-fsync",
		"install-0-after-rename",
		"install-0-after-parent-fsync",
	}
	points = append(points, durableWriteSIGKILLPoints("pre-admission-stage")...)
	points = append(points, durableWriteSIGKILLPoints("backup-key-stage")...)
	points = append(points, durableWriteSIGKILLPoints("journal-admitted")...)
	points = append(points, durableWriteSIGKILLPoints("target-stage-0")...)
	for _, point := range points {
		t.Run(point, func(t *testing.T) {
			fixture := newInstallFixture(t)
			verified := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
			defer verified.Release()
			wirePath := writeInstallerSIGKILLWire(t, fixture, verified)
			runInstallerSIGKILLChild(t, wirePath, point)

			installer := freshFixtureInstaller(fixture, t)
			receipt, err := installer.Install(verified)
			if err != nil {
				t.Fatalf("fresh recovery after %q failed: %v", point, err)
			}
			assertSignedInstallReceipt(t, receipt, fixture.receiptKey, "installed-and-active")
			if !digestPattern.MatchString(persistedPreAdmissionKeyID(t, fixture)) {
				t.Fatal("stable backup key missing after recovery")
			}
			for _, absolute := range []string{preAdmissionPath, preAdmissionPendingPath, backupEncryptionKeyPath + ".pending"} {
				path := filepath.Join(fixture.root, strings.TrimPrefix(absolute, "/"))
				if _, err := os.Lstat(path); !os.IsNotExist(err) {
					t.Fatalf("fixed-name stage survived at %s: %v", path, err)
				}
			}
			assertNoTransactionArtifacts(t, installer, verified)
			assertContiguousSignedJournal(t, installer, verified, fixture.receiptKey)
		})
	}
}

func TestUpgradeRealSIGKILLConvergesAcrossEverySwapAndRestoreIndex(t *testing.T) {
	runCase := func(t *testing.T, swapPoint, recoveryPoint string) {
		t.Helper()
		fixture := newInstallFixture(t)
		first := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
		defer first.Release()
		if _, err := freshFixtureInstaller(fixture, t).Install(first); err != nil {
			t.Fatalf("install genesis: %v", err)
		}
		successor := fixture.verifiedPackage(t, 2, strings.Repeat("b", 40), strings.Repeat("a", 40))
		defer successor.Release()
		wirePath := writeInstallerSIGKILLWire(t, fixture, successor)
		runInstallerSIGKILLChild(t, wirePath, swapPoint)
		if recoveryPoint != "" {
			runInstallerSIGKILLChild(t, wirePath, recoveryPoint)
		}
		installer := freshFixtureInstaller(fixture, t)
		receipt, err := installer.Install(successor)
		if err != nil {
			t.Fatalf("fresh upgrade recovery after %q/%q failed: %v", swapPoint, recoveryPoint, err)
		}
		assertSignedInstallReceipt(t, receipt, fixture.receiptKey, "installed-and-active")
		manifestPath := filepath.Join(fixture.root, "etc/propertyquarry-release-single-host-v2/package-manifest.v2.json")
		manifest, err := os.ReadFile(manifestPath)
		if err != nil || string(manifest) != string(successor.ManifestRaw) {
			t.Fatalf("successor manifest not installed after %q/%q: err=%v", swapPoint, recoveryPoint, err)
		}
		assertNoTransactionArtifacts(t, installer, successor)
		assertContiguousSignedJournal(t, installer, successor, fixture.receiptKey)
	}

	countFixture := newInstallFixture(t)
	countFirst := countFixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	if _, err := freshFixtureInstaller(countFixture, t).Install(countFirst); err != nil {
		t.Fatal(err)
	}
	countSuccessor := countFixture.verifiedPackage(t, 2, strings.Repeat("b", 40), strings.Repeat("a", 40))
	txID := strings.TrimPrefix(countSuccessor.ArchiveDigest, "sha256:")[:32]
	targets, err := freshFixtureInstaller(countFixture, t).candidateTargets(countSuccessor, txID)
	if err != nil {
		t.Fatal(err)
	}
	targetCount := len(targets)
	countSuccessor.Release()
	countFirst.Release()
	if targetCount < 3 {
		t.Fatalf("upgrade target set too small: %d", targetCount)
	}

	for index := 0; index < targetCount; index++ {
		for _, operation := range []string{"backup-after-rename", "backup-after-parent-fsync", "install-after-rename", "install-after-parent-fsync"} {
			point := strings.Replace(operation, "backup-", "backup-"+strconv.Itoa(index)+"-", 1)
			point = strings.Replace(point, "install-", "install-"+strconv.Itoa(index)+"-", 1)
			t.Run(fmt.Sprintf("swap-%03d-%s", index, operation), func(t *testing.T) {
				runCase(t, point, "")
			})
		}
	}
	lastInstallPoint := fmt.Sprintf("install-%d-after-parent-fsync", targetCount-1)
	for index := targetCount - 1; index >= 0; index-- {
		for _, suffix := range []string{"after-remove", "after-remove-parent-fsync", "after-rename", "after-parent-fsync"} {
			recoveryPoint := fmt.Sprintf("restore-%d-%s", index, suffix)
			t.Run(fmt.Sprintf("restore-%03d-%s", index, suffix), func(t *testing.T) {
				runCase(t, lastInstallPoint, recoveryPoint)
			})
		}
	}
}

func TestGenesisPreAdmissionResumesEveryPublishedBoundaryInFreshProcess(t *testing.T) {
	points := []string{
		"after-pre-admission-pending",
		"after-pre-admission",
		"after-backup-key-directory",
		"after-backup-key-stage",
		"after-backup-key-publish",
		"after-install-directory-0",
		"after-install-directory-1",
		"after-install-directory-2",
		"after-install-directory-3",
		"after-install-directory-4",
		"after-admitted-before-pre-admission-clear",
		"after-admitted",
	}
	for _, point := range points {
		t.Run(point, func(t *testing.T) {
			fixture := newInstallFixture(t)
			verified := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
			defer verified.Release()
			first := freshFixtureInstaller(fixture, t)
			first.Interrupt = func(candidate string) bool { return candidate == point }
			if receipt, err := first.Install(verified); err != errInstallInterrupted || len(receipt) != 0 {
				t.Fatalf("boundary %q not interrupted: receipt=%s err=%v", point, receipt, err)
			}
			keyID := persistedPreAdmissionKeyID(t, fixture)
			second := freshFixtureInstaller(fixture, t)
			receipt, err := second.Install(verified)
			if err != nil {
				t.Fatal(err)
			}
			assertSignedInstallReceipt(t, receipt, fixture.receiptKey, "installed-and-active")
			if finalKeyID := persistedPreAdmissionKeyID(t, fixture); finalKeyID != keyID {
				t.Fatalf("backup key rotated across %q: got %s want %s", point, finalKeyID, keyID)
			}
			for _, absolute := range []string{preAdmissionPath, preAdmissionPendingPath} {
				path := filepath.Join(fixture.root, strings.TrimPrefix(absolute, "/"))
				if _, err := os.Lstat(path); !os.IsNotExist(err) {
					t.Fatalf("pre-admission artifact survived at %s: %v", path, err)
				}
			}
			assertNoTransactionArtifacts(t, second, verified)
			assertContiguousSignedJournal(t, second, verified, fixture.receiptKey)
		})
	}
}

func TestGenesisPreAdmissionRepairsTornFixedStages(t *testing.T) {
	for _, testCase := range []struct {
		name      string
		point     string
		stagePath string
	}{
		{name: "pre-admission", point: "after-pre-admission-pending", stagePath: preAdmissionPendingPath},
		{name: "backup-key", point: "after-backup-key-stage", stagePath: backupEncryptionKeyPath + ".pending"},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			fixture := newInstallFixture(t)
			verified := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
			defer verified.Release()
			first := freshFixtureInstaller(fixture, t)
			first.Interrupt = func(candidate string) bool { return candidate == testCase.point }
			if _, err := first.Install(verified); err != errInstallInterrupted {
				t.Fatal(err)
			}
			var expectedKeyID string
			if testCase.name == "backup-key" {
				expectedKeyID = persistedPreAdmissionKeyID(t, fixture)
			}
			stage := filepath.Join(fixture.root, strings.TrimPrefix(testCase.stagePath, "/"))
			if err := os.Chmod(stage, 0o600); err != nil {
				t.Fatal(err)
			}
			if err := os.Truncate(stage, 7); err != nil {
				t.Fatal(err)
			}
			if err := os.Chmod(stage, map[bool]os.FileMode{true: 0o400, false: 0o600}[testCase.name == "pre-admission"]); err != nil {
				t.Fatal(err)
			}
			second := freshFixtureInstaller(fixture, t)
			if _, err := second.Install(verified); err != nil {
				t.Fatal(err)
			}
			if expectedKeyID != "" && persistedPreAdmissionKeyID(t, fixture) != expectedKeyID {
				t.Fatal("authenticated staged backup key rotated")
			}
		})
	}
}

func TestInterruptedGenesisRecoversBeforeExpiredAndRotatedAdmissionInputs(t *testing.T) {
	fixture := newInstallFixture(t)
	verified := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer verified.Release()
	first := freshFixtureInstaller(fixture, t)
	first.Interrupt = func(point string) bool { return point == "after-install-2" }
	if _, err := first.Install(verified); err != errInstallInterrupted {
		t.Fatal(err)
	}
	verified.MaterializationValidUntil = time.Now().UTC().Unix() - 1
	scenePath := filepath.Join(fixture.root, "docker/property/state/runtime/property_scene_video_shared.env")
	writeTestFile(t, scenePath, []byte("PROPERTYQUARRY_RENDER_BRIDGE_TOKEN=rotated\n"), 0o600)
	second := freshFixtureInstaller(fixture, t)
	receipt, err := second.Install(verified)
	if err != nil {
		t.Fatal(err)
	}
	payload := signedInstallReceiptPayload(t, receipt, fixture.receiptKey)
	if payload["disposition"] != "recovered-prior-readmission-required" || payload["recovery_performed"] != true || payload["recovery_succeeded"] != true || payload["candidate_authority_installed"] != false {
		t.Fatalf("expired recovery receipt invalid: %#v", payload)
	}
	assertNoTransactionArtifacts(t, second, verified)
}

func TestSucceededJournalCleansUpBeforeExpiredReadmissionGate(t *testing.T) {
	fixture := newInstallFixture(t)
	verified := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer verified.Release()
	activations := 0
	first := freshFixtureInstaller(fixture, t)
	first.Activate = func() (*activationAttempt, error) {
		activations++
		return fixture.activationAttempt(t)
	}
	first.Interrupt = func(point string) bool { return point == "after-succeeded" }
	if _, err := first.Install(verified); err != errInstallInterrupted {
		t.Fatal(err)
	}
	verified.MaterializationValidUntil = time.Now().UTC().Unix() - 1
	second := freshFixtureInstaller(fixture, t)
	receipt, err := second.Install(verified)
	if err != nil {
		t.Fatal(err)
	}
	payload := signedInstallReceiptPayload(t, receipt, fixture.receiptKey)
	if payload["disposition"] != "recovery-complete-readmission-required" || payload["candidate_authority_installed"] != true || activations != 1 {
		t.Fatalf("succeeded cleanup crossed readmission gate: activations=%d payload=%#v", activations, payload)
	}
	assertNoTransactionArtifacts(t, second, verified)
}
