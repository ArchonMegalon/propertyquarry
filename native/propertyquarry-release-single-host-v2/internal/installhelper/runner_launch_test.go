package installhelper

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"syscall"
	"testing"
)

func validRunnerLaunchPackage() *VerifiedPackage {
	files := make(map[string]*FileRecord, 3)
	for path, contract := range map[string]requiredPackageFile{
		FixedRunnerLifecyclePath: {
			mode:    0o555,
			purpose: "ephemeral-runner-root-lifecycle",
		},
		fixedControllerPath: {
			mode:    0o755,
			purpose: "controller-binary",
		},
		fixedRunnerLauncherPath: {
			mode:    0o555,
			purpose: "ephemeral-runner-launcher",
		},
	} {
		raw := []byte("signed:" + path + "\n")
		files[path] = &FileRecord{
			InstallPath: path,
			PackagePath: "payload" + path,
			Purpose:     contract.purpose,
			Mode:        contract.mode,
			Size:        int64(len(raw)),
			Digest:      digest(raw),
			Data:        raw,
		}
	}
	return &VerifiedPackage{Files: files}
}

func TestValidateRunnerLaunchPackageRequiresExactSignedBindings(t *testing.T) {
	if err := validateRunnerLaunchPackage(validRunnerLaunchPackage()); err != nil {
		t.Fatalf("valid runner launch package rejected: %v", err)
	}
	if err := validateRunnerLaunchPackage(nil); err == nil {
		t.Fatal("nil runner launch package accepted")
	}
	tests := map[string]func(*VerifiedPackage){
		"missing-lifecycle": func(packageValue *VerifiedPackage) {
			delete(packageValue.Files, FixedRunnerLifecyclePath)
		},
		"substituted-path": func(packageValue *VerifiedPackage) {
			packageValue.Files[FixedRunnerLifecyclePath].InstallPath = "/tmp/lifecycle"
		},
		"mutable-mode": func(packageValue *VerifiedPackage) {
			packageValue.Files[FixedRunnerLifecyclePath].Mode = 0o755
		},
		"generic-purpose": func(packageValue *VerifiedPackage) {
			packageValue.Files[FixedRunnerLifecyclePath].Purpose = "generic-root-exec"
		},
		"empty-member": func(packageValue *VerifiedPackage) {
			file := packageValue.Files[FixedRunnerLifecyclePath]
			file.Data = nil
			file.Size = 0
			file.Digest = digest(nil)
		},
		"wrong-size": func(packageValue *VerifiedPackage) {
			packageValue.Files[FixedRunnerLifecyclePath].Size++
		},
		"wrong-digest": func(packageValue *VerifiedPackage) {
			packageValue.Files[FixedRunnerLifecyclePath].Digest =
				"sha256:" + strings.Repeat("0", 64)
		},
		"mutated-bytes": func(packageValue *VerifiedPackage) {
			packageValue.Files[FixedRunnerLifecyclePath].Data[0] ^= 1
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			packageValue := validRunnerLaunchPackage()
			mutate(packageValue)
			if err := validateRunnerLaunchPackage(packageValue); err == nil {
				t.Fatal("invalid runner launch package accepted")
			}
		})
	}
}

func TestRequireRunnerLaunchFIFORejectsBadDescriptors(t *testing.T) {
	readEnd, writeEnd, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer readEnd.Close()
	defer writeEnd.Close()
	if err := requireRunnerLaunchFIFO(int(readEnd.Fd())); err != nil {
		t.Fatalf("read-only FIFO rejected: %v", err)
	}
	if err := requireRunnerLaunchFIFO(int(writeEnd.Fd())); err == nil {
		t.Fatal("write-only FIFO accepted")
	}
	regular, err := os.CreateTemp(t.TempDir(), "regular")
	if err != nil {
		t.Fatal(err)
	}
	defer regular.Close()
	if err := requireRunnerLaunchFIFO(int(regular.Fd())); err == nil {
		t.Fatal("regular file accepted")
	}
	if err := requireRunnerLaunchFIFO(-1); err == nil {
		t.Fatal("negative descriptor accepted")
	}
}

func TestValidateRunnerLaunchResolverRequiresExactReadOnlyOverlay(t *testing.T) {
	hostRoot := t.TempDir()
	resolverDirectory := filepath.Join(hostRoot, "run/systemd/resolve")
	etcDirectory := filepath.Join(hostRoot, "etc")
	if err := os.MkdirAll(resolverDirectory, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(etcDirectory, 0o755); err != nil {
		t.Fatal(err)
	}
	resolverPath := filepath.Join(resolverDirectory, "stub-resolv.conf")
	if err := os.WriteFile(
		resolverPath,
		[]byte(fixedRunnerResolverBytes),
		0o444,
	); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(resolverPath, 0o444); err != nil {
		t.Fatal(err)
	}
	linkPath := filepath.Join(etcDirectory, "resolv.conf")
	if err := os.Symlink(fixedRunnerResolverLink, linkPath); err != nil {
		t.Fatal(err)
	}
	owner := uint32(os.Geteuid())
	if os.Getegid() != os.Geteuid() {
		t.Skip("resolver fixture requires matching effective UID/GID")
	}
	if err := validateRunnerLaunchResolver(
		hostRoot,
		owner,
		owner,
	); err != nil {
		t.Fatalf("exact resolver overlay rejected: %v", err)
	}

	if err := os.Chmod(resolverPath, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(
		resolverPath,
		[]byte("nameserver 127.0.0.53\noptions ndots:0\n"),
		0o644,
	); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(resolverPath, 0o444); err != nil {
		t.Fatal(err)
	}
	if err := validateRunnerLaunchResolver(
		hostRoot,
		owner,
		owner,
	); err == nil {
		t.Fatal("host stub resolver accepted")
	}
	if err := os.Chmod(resolverPath, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(
		resolverPath,
		[]byte(fixedRunnerResolverBytes),
		0o644,
	); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(resolverPath, 0o444); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(resolverDirectory, 0o775); err != nil {
		t.Fatal(err)
	}
	if err := validateRunnerLaunchResolver(
		hostRoot,
		owner,
		owner,
	); err == nil {
		t.Fatal("group-writable resolver directory accepted")
	}
}

func TestExecuteFixedRunnerLifecycleUsesOnlyFixedChrootExec(t *testing.T) {
	originalDup2 := runnerLaunchDup2
	originalClose := runnerLaunchClose
	originalChroot := runnerLaunchChroot
	originalChdir := runnerLaunchChdir
	originalExec := runnerLaunchExec
	originalFstat := runnerLaunchFstat
	originalFcntl := runnerLaunchFcntl
	originalVerify := runnerLaunchVerifyExecutables
	originalValidateHostRoot := runnerLaunchValidateHostRoot
	t.Cleanup(func() {
		runnerLaunchDup2 = originalDup2
		runnerLaunchClose = originalClose
		runnerLaunchChroot = originalChroot
		runnerLaunchChdir = originalChdir
		runnerLaunchExec = originalExec
		runnerLaunchFstat = originalFstat
		runnerLaunchFcntl = originalFcntl
		runnerLaunchVerifyExecutables = originalVerify
		runnerLaunchValidateHostRoot = originalValidateHostRoot
	})

	var calls []string
	runnerLaunchValidateHostRoot = func() error {
		calls = append(calls, "validate-host-root")
		return nil
	}
	runnerLaunchDup2 = func(oldDescriptor, newDescriptor int) error {
		if oldDescriptor != 0 || newDescriptor != runnerAdminTokenFD {
			t.Fatalf("unexpected dup2: %d -> %d", oldDescriptor, newDescriptor)
		}
		calls = append(calls, "dup2")
		return nil
	}
	runnerLaunchFstat = func(descriptor int, metadata *syscall.Stat_t) error {
		if descriptor != runnerAdminTokenFD {
			t.Fatalf("unexpected fstat descriptor: %d", descriptor)
		}
		metadata.Mode = syscall.S_IFIFO
		metadata.Nlink = 1
		calls = append(calls, "fstat")
		return nil
	}
	runnerLaunchFcntl = func(descriptor, command, argument int) (int, error) {
		if descriptor != runnerAdminTokenFD {
			t.Fatalf("unexpected fcntl descriptor: %d", descriptor)
		}
		switch command {
		case syscall.F_GETFD:
			calls = append(calls, "getfd")
			return syscall.FD_CLOEXEC, nil
		case syscall.F_SETFD:
			if argument != 0 {
				t.Fatalf("close-on-exec flag not cleared: %d", argument)
			}
			calls = append(calls, "setfd")
			return 0, nil
		case syscall.F_GETFL:
			calls = append(calls, "getfl")
			return syscall.O_RDONLY, nil
		default:
			t.Fatalf("unexpected fcntl command: %d", command)
			return 0, nil
		}
	}
	runnerLaunchClose = func(descriptor int) error {
		if descriptor != 0 {
			t.Fatalf("unexpected close descriptor: %d", descriptor)
		}
		calls = append(calls, "close-stdin")
		return nil
	}
	runnerLaunchChroot = func(path string) error {
		if path != FixedHostRoot {
			t.Fatalf("unexpected chroot: %q", path)
		}
		calls = append(calls, "chroot")
		return nil
	}
	runnerLaunchChdir = func(path string) error {
		if path != "/" {
			t.Fatalf("unexpected chdir: %q", path)
		}
		calls = append(calls, "chdir")
		return nil
	}
	packageValue := validRunnerLaunchPackage()
	runnerLaunchVerifyExecutables = func(observed *VerifiedPackage) error {
		if observed != packageValue {
			t.Fatal("post-chroot verifier received substituted package")
		}
		calls = append(calls, "verify")
		return nil
	}
	execFailure := errors.New("test exec stop")
	runnerLaunchExec = func(path string, arguments, environment []string) error {
		if path != FixedRunnerLifecyclePath {
			t.Fatalf("unexpected exec path: %q", path)
		}
		if !reflect.DeepEqual(arguments, []string{FixedRunnerLifecyclePath}) {
			t.Fatalf("unexpected exec arguments: %#v", arguments)
		}
		expectedEnvironment := []string{
			"HOME=/root",
			"LANG=C",
			"LC_ALL=C",
			"PATH=/usr/sbin:/usr/bin:/sbin:/bin",
			"TZ=UTC",
		}
		if !reflect.DeepEqual(environment, expectedEnvironment) {
			t.Fatalf("unexpected exec environment: %#v", environment)
		}
		for _, value := range environment {
			if strings.HasPrefix(
				value,
				"PROPERTYQUARRY_RUNNER_ADMIN_TOKEN_FD=",
			) ||
				strings.Contains(value, "github_pat_") ||
				strings.Contains(value, "ghp_") {
				t.Fatal("token capability entered lifecycle environment")
			}
		}
		calls = append(calls, "exec")
		return execFailure
	}

	err := executeFixedRunnerLifecycle(packageValue)
	if err == nil || err.Error() != "runner-launch-exec-failed" {
		t.Fatalf("unexpected execution result: %v", err)
	}
	expectedCalls := []string{
		"validate-host-root", "dup2", "getfd", "setfd", "fstat", "getfl",
		"close-stdin", "chroot", "chdir", "verify", "exec",
	}
	if !reflect.DeepEqual(calls, expectedCalls) {
		t.Fatalf("unexpected execution order: %#v", calls)
	}
}

func TestExecuteFixedRunnerLifecycleFailsClosedOnDescriptorErrors(t *testing.T) {
	originalDup2 := runnerLaunchDup2
	originalValidateHostRoot := runnerLaunchValidateHostRoot
	runnerLaunchValidateHostRoot = func() error { return nil }
	runnerLaunchDup2 = func(int, int) error { return errors.New("dup rejected") }
	t.Cleanup(func() {
		runnerLaunchDup2 = originalDup2
		runnerLaunchValidateHostRoot = originalValidateHostRoot
	})
	if err := executeFixedRunnerLifecycle(validRunnerLaunchPackage()); err == nil ||
		err.Error() != "runner-launch-token-dup-failed" {
		t.Fatalf("unexpected descriptor failure: %v", err)
	}
}

func TestRunRejectsGenericRunnerLaunchCommands(t *testing.T) {
	for _, arguments := range [][]string{
		{"launch-ephemeral-runner", "/bin/sh"},
		{"launch-ephemeral-runner", "bash", "-c", "id"},
		{"exec", FixedRunnerLifecyclePath},
		{"/bin/sh"},
	} {
		var stdout bytes.Buffer
		var stderr bytes.Buffer
		if code := Run(arguments, &stdout, &stderr); code != ExitFailure {
			t.Fatalf("generic arguments returned %d: %#v", code, arguments)
		}
		if stdout.Len() != 0 ||
			stderr.String() != "propertyquarry-single-host-installer-rejected\n" {
			t.Fatalf(
				"generic arguments did not fail closed: %#v, %q, %q",
				arguments, stdout.String(), stderr.String(),
			)
		}
	}
}
