package installhelper

import (
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

const (
	FixedRunnerLifecyclePath = "/usr/libexec/propertyquarry-release-control/run-propertyquarry-ephemeral-runner-lifecycle-v2"
	fixedControllerPath      = "/usr/libexec/propertyquarry-release-control/propertyquarry-release-single-host-v2"
	fixedRunnerLauncherPath  = "/usr/libexec/propertyquarry-release-control/run-propertyquarry-ephemeral-runner-v2"
	fixedRunnerResolverPath  = "/run/systemd/resolve/stub-resolv.conf"
	fixedRunnerResolverLink  = "../run/systemd/resolve/stub-resolv.conf"
	fixedRunnerResolverBytes = "nameserver 127.0.0.11\noptions ndots:0\n"
	runnerAdminTokenFD       = 8
)

var (
	runnerLaunchGeteuid = os.Geteuid
	runnerLaunchGetegid = os.Getegid
	runnerLaunchDup2    = syscall.Dup2
	runnerLaunchClose   = syscall.Close
	runnerLaunchChroot  = syscall.Chroot
	runnerLaunchChdir   = os.Chdir
	runnerLaunchExec    = syscall.Exec
	runnerLaunchFstat   = syscall.Fstat
	runnerLaunchFcntl   = func(fd int, command int, argument int) (int, error) {
		value, _, errno := syscall.Syscall(
			syscall.SYS_FCNTL,
			uintptr(fd),
			uintptr(command),
			uintptr(argument),
		)
		if errno != 0 {
			return 0, errno
		}
		return int(value), nil
	}
	runnerLaunchVerifyExecutables = func(verified *VerifiedPackage) error {
		for _, path := range []string{
			FixedRunnerLifecyclePath,
			fixedControllerPath,
			fixedRunnerLauncherPath,
		} {
			if err := verifyRunnerLaunchFile(path, verified.Files[path]); err != nil {
				return err
			}
		}
		return nil
	}
	runnerLaunchValidateHostRoot = func() error {
		if err := validateDirectoryChain(FixedHostRoot, FixedHostRoot, 0); err != nil {
			return fmt.Errorf("runner-launch-host-root-invalid")
		}
		if err := validateRunnerLaunchResolver(FixedHostRoot, 0, 1000); err != nil {
			return err
		}
		return nil
	}
)

func LaunchFixedEphemeralRunner() error {
	if runnerLaunchGeteuid() != 0 || runnerLaunchGetegid() != 0 {
		return fmt.Errorf("runner-launch-root-required")
	}
	for _, name := range []string{
		"ACTIONS_RUNNER_INPUT_TOKEN",
		"GH_TOKEN",
		"GITHUB_TOKEN",
		"PROPERTYQUARRY_RUNNER_ADMIN_TOKEN",
		"PROPERTYQUARRY_RUNNER_ADMIN_TOKEN_FD",
	} {
		if os.Getenv(name) != "" {
			return fmt.Errorf("runner-launch-environment-invalid")
		}
	}
	if err := disableCredentialDumps(); err != nil {
		return err
	}
	if err := requireRunnerLaunchFIFO(0); err != nil {
		return err
	}
	verified, err := authorizeFixedRunnerLaunch()
	if err != nil {
		return err
	}
	defer verified.Release()
	return executeFixedRunnerLifecycle(verified)
}

func validateRunnerLaunchPackage(verified *VerifiedPackage) error {
	if verified == nil {
		return fmt.Errorf("runner-launch-package-invalid")
	}
	expected := map[string]requiredPackageFile{
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
	}
	for path, contract := range expected {
		file := verified.Files[path]
		if file == nil ||
			file.InstallPath != path ||
			file.Mode != contract.mode ||
			file.Purpose != contract.purpose ||
			file.Size < 1 ||
			int64(len(file.Data)) != file.Size ||
			!digestPattern.MatchString(file.Digest) ||
			digest(file.Data) != file.Digest {
			return fmt.Errorf("runner-launch-package-binding-invalid")
		}
	}
	return nil
}

func authorizeFixedRunnerLaunch() (*VerifiedPackage, error) {
	packageKey, packageKeyID, err := EmbeddedPackageAuthority()
	if err != nil {
		return nil, err
	}
	defer zero(packageKey)
	verified, err := VerifyPackageFile(FixedPackagePath, packageKey, packageKeyID)
	if err != nil {
		return nil, err
	}
	succeeded := false
	defer func() {
		if !succeeded {
			verified.Release()
		}
	}()
	if err := validateInstallerSelfBinding(verified); err != nil {
		return nil, err
	}
	if err := validateRunnerLaunchPackage(verified); err != nil {
		return nil, err
	}
	installer := &Installer{HostRoot: FixedHostRoot, OwnerUID: 0, OwnerGID: 0}
	matched, present, err := installer.enforceInstalledGeneration(verified)
	if err != nil || !matched || !present {
		return nil, fmt.Errorf("runner-launch-installed-generation-invalid")
	}
	if err := installer.verifyInstalledFiles(verified); err != nil {
		return nil, fmt.Errorf("runner-launch-installed-files-invalid")
	}
	succeeded = true
	return verified, nil
}

func requireRunnerLaunchFIFO(descriptor int) error {
	var metadata syscall.Stat_t
	if descriptor < 0 || runnerLaunchFstat(descriptor, &metadata) != nil ||
		metadata.Mode&syscall.S_IFMT != syscall.S_IFIFO ||
		metadata.Nlink != 1 {
		return fmt.Errorf("runner-launch-token-fd-invalid")
	}
	flags, err := runnerLaunchFcntl(descriptor, syscall.F_GETFL, 0)
	if err != nil || flags&syscall.O_ACCMODE != syscall.O_RDONLY {
		return fmt.Errorf("runner-launch-token-fd-invalid")
	}
	return nil
}

func validateRunnerResolverDirectoryChain(
	root string,
	path string,
	rootOwner uint32,
) error {
	root = filepath.Clean(root)
	current := filepath.Clean(path)
	relative, err := filepath.Rel(root, current)
	if err != nil || relative == ".." ||
		len(relative) > 3 && relative[:3] == "../" {
		return fmt.Errorf("runner-launch-resolver-directory-invalid")
	}
	for {
		info, err := os.Lstat(current)
		if err != nil || !info.IsDir() ||
			info.Mode()&os.ModeSymlink != 0 ||
			info.Mode().Perm()&0o022 != 0 {
			return fmt.Errorf("runner-launch-resolver-directory-invalid")
		}
		metadata, ok := info.Sys().(*syscall.Stat_t)
		if !ok || current == root && metadata.Uid != rootOwner {
			return fmt.Errorf("runner-launch-resolver-directory-invalid")
		}
		if current == root {
			return nil
		}
		next := filepath.Dir(current)
		if next == current {
			return fmt.Errorf("runner-launch-resolver-directory-invalid")
		}
		current = next
	}
}

func validateRunnerLaunchResolver(
	hostRoot string,
	hostOwner uint32,
	resolverOwner uint32,
) error {
	resolverPath := filepath.Join(hostRoot, fixedRunnerResolverPath[1:])
	if err := validateRunnerResolverDirectoryChain(
		hostRoot,
		filepath.Dir(resolverPath),
		hostOwner,
	); err != nil {
		return err
	}
	linkPath := filepath.Join(hostRoot, "etc/resolv.conf")
	if err := validateRunnerResolverDirectoryChain(
		hostRoot,
		filepath.Dir(linkPath),
		hostOwner,
	); err != nil {
		return err
	}
	linkInfo, err := os.Lstat(linkPath)
	if err != nil || linkInfo.Mode()&os.ModeSymlink == 0 {
		return fmt.Errorf("runner-launch-resolver-link-invalid")
	}
	linkMetadata, ok := linkInfo.Sys().(*syscall.Stat_t)
	if !ok || linkMetadata.Uid != hostOwner ||
		linkMetadata.Gid != hostOwner || linkMetadata.Nlink != 1 {
		return fmt.Errorf("runner-launch-resolver-link-invalid")
	}
	link, err := os.Readlink(linkPath)
	if err != nil || link != fixedRunnerResolverLink {
		return fmt.Errorf("runner-launch-resolver-link-invalid")
	}
	raw, err := readExactFile(
		resolverPath,
		0o444,
		resolverOwner,
		resolverOwner,
		len(fixedRunnerResolverBytes),
	)
	if err != nil {
		return fmt.Errorf("runner-launch-resolver-file-invalid")
	}
	defer zero(raw)
	if string(raw) != fixedRunnerResolverBytes {
		return fmt.Errorf("runner-launch-resolver-file-invalid")
	}
	return nil
}

func verifyRunnerLaunchFile(path string, file *FileRecord) error {
	if file == nil || file.InstallPath != path || file.Size < 1 ||
		file.Size > maximumMemberBytes || !digestPattern.MatchString(file.Digest) {
		return fmt.Errorf("runner-launch-executable-contract-invalid")
	}
	raw, err := readExactFile(path, file.Mode, 0, 0, int(file.Size))
	if err != nil {
		return fmt.Errorf("runner-launch-executable-invalid")
	}
	defer zero(raw)
	if int64(len(raw)) != file.Size || digest(raw) != file.Digest {
		return fmt.Errorf("runner-launch-executable-invalid")
	}
	return nil
}

func executeFixedRunnerLifecycle(verified *VerifiedPackage) error {
	if err := validateRunnerLaunchPackage(verified); err != nil {
		return err
	}
	if err := runnerLaunchValidateHostRoot(); err != nil {
		return err
	}
	if err := runnerLaunchDup2(0, runnerAdminTokenFD); err != nil {
		return fmt.Errorf("runner-launch-token-dup-failed")
	}
	flags, err := runnerLaunchFcntl(runnerAdminTokenFD, syscall.F_GETFD, 0)
	if err != nil {
		return fmt.Errorf("runner-launch-token-flags-invalid")
	}
	if _, err := runnerLaunchFcntl(
		runnerAdminTokenFD,
		syscall.F_SETFD,
		flags&^syscall.FD_CLOEXEC,
	); err != nil {
		return fmt.Errorf("runner-launch-token-flags-invalid")
	}
	if err := requireRunnerLaunchFIFO(runnerAdminTokenFD); err != nil {
		return err
	}
	if err := runnerLaunchClose(0); err != nil {
		return fmt.Errorf("runner-launch-stdin-close-failed")
	}
	if err := runnerLaunchChroot(FixedHostRoot); err != nil {
		return fmt.Errorf("runner-launch-chroot-failed")
	}
	if err := runnerLaunchChdir("/"); err != nil {
		return fmt.Errorf("runner-launch-chdir-failed")
	}
	if err := runnerLaunchVerifyExecutables(verified); err != nil {
		return err
	}
	environment := []string{
		"HOME=/root",
		"LANG=C",
		"LC_ALL=C",
		"PATH=/usr/sbin:/usr/bin:/sbin:/bin",
		"TZ=UTC",
	}
	if err := runnerLaunchExec(
		FixedRunnerLifecyclePath,
		[]string{FixedRunnerLifecyclePath},
		environment,
	); err != nil {
		return fmt.Errorf("runner-launch-exec-failed")
	}
	return fmt.Errorf("runner-launch-exec-returned")
}
