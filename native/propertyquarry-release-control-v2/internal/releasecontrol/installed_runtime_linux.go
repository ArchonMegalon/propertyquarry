//go:build linux

package releasecontrol

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"path"
	"strconv"
	"syscall"
	"time"
)

const (
	installedAcceptPoll    = 250 * time.Millisecond
	installedChildDeadline = 5 * time.Second
	installedHealthPoll    = 5 * time.Second
	installedRuntimePoll   = 100 * time.Millisecond
	installedSocketMode    = 0o660
)

const installedRequestSmokePayload = `{"envelope":{"expires_at":1100,"identity":{"audience":"propertyquarry-release-v2","candidate_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","environment":"propertyquarry-production","job":"propertyquarry-release-v2","ref":"refs/heads/main","repository":"owner/property","run_attempt":1,"run_id":"424242","workflow_ref":"owner/property/.github/workflows/propertyquarry-release-v2.yml@refs/heads/main","workflow_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},"issued_at":1000,"nonce":"nonce-socket-request-1","operation":"release-preflight","request_id":"socket-request-1"},"envelope_digest":"sha256:f9c9160c494309599e9a8c0c768fee086dcc2e5a81f4d91735b630281085211b","request_signature":"sig:transport-conformance-test","schema":"propertyquarry.release-request.v2"}`

func runInstalledSupervisor(stderr io.Writer) int {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	restartSignal := make(chan os.Signal, 1)
	restartRequested := make(chan struct{})
	signal.Notify(restartSignal, syscall.SIGUSR2)
	defer signal.Stop(restartSignal)
	go func() {
		select {
		case <-restartSignal:
			close(restartRequested)
			stop()
		case <-ctx.Done():
		}
	}()
	if err := serveInstalledSupervisor(ctx, defaultInstalledRuntimePaths()); err != nil {
		return refuse(stderr)
	}
	select {
	case <-restartRequested:
		return refuse(stderr)
	default:
	}
	return 0
}

func serveInstalledSupervisor(ctx context.Context, paths installedRuntimePaths) error {
	verification, err := validateInstalledLocalAuthority(Supervisor, paths)
	if err != nil {
		return err
	}
	if _, err := validateInstalledAuthorityState(paths); err != nil {
		return err
	}
	before, err := prepareInstalledRuntimeDirectory(paths)
	if err != nil {
		return err
	}
	socketPath, err := rootedRuntimePath(paths.Root, paths.RequestSocket)
	if err != nil || len(socketPath) >= 108 {
		return fmt.Errorf("installed request socket path invalid")
	}
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: socketPath, Net: "unix"})
	if err != nil {
		return err
	}
	listener.SetUnlinkOnClose(true)
	defer listener.Close()
	if err := enableInstalledPassCredentials(listener); err != nil {
		return err
	}
	if err := os.Chmod(socketPath, installedSocketMode); err != nil {
		return err
	}
	after, err := inspectStableRootedDirectory(
		paths.Root,
		paths.RuntimeRoot,
		expectedFileMetadata{Mode: 0o750, UID: paths.SocketUID, GID: paths.SocketGID},
		false,
	)
	if err != nil || !sameInstalledDirectoryObject(before, after) {
		return fmt.Errorf("installed runtime directory changed")
	}
	if _, err := validateInstalledSocket(paths, false); err != nil {
		return err
	}

	for {
		if err := ctx.Err(); err != nil {
			return nil
		}
		if err := listener.SetDeadline(time.Now().Add(installedAcceptPoll)); err != nil {
			return err
		}
		connection, err := listener.AcceptUnix()
		if err != nil {
			if timeout, ok := err.(net.Error); ok && timeout.Timeout() {
				continue
			}
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		if err := handleInstalledConnection(connection, paths, verification); err != nil {
			// Malformed or unauthenticated local transports are isolated to one
			// connection. Installation drift and controller failures are terminal
			// and are returned by the handler as typed terminal errors.
			var terminal installedTerminalError
			if errors.As(err, &terminal) {
				return err
			}
		}
	}
}

func prepareInstalledRuntimeDirectory(paths installedRuntimePaths) (stableIdentity, error) {
	if err := validateInstalledPrincipalContract(paths); err != nil {
		return stableIdentity{}, err
	}
	expected := expectedFileMetadata{Mode: 0o750, UID: paths.SocketUID, GID: paths.SocketGID}
	before, err := inspectStableRootedDirectory(paths.Root, paths.RuntimeRoot, expected, false)
	if err != nil {
		return stableIdentity{}, err
	}
	runtimeFD, err := openRootedAbsolute(paths.Root, paths.RuntimeRoot, syscall.O_DIRECTORY)
	if err != nil {
		return stableIdentity{}, err
	}
	names, namesErr := directoryNames(runtimeFD)
	_ = syscall.Close(runtimeFD)
	if namesErr != nil {
		return stableIdentity{}, namesErr
	}
	if len(names) == 0 {
		return before, nil
	}
	if len(names) != 1 || names[0] != path.Base(paths.RequestSocket) {
		return stableIdentity{}, fmt.Errorf("installed runtime entries invalid")
	}
	staleBefore, err := validateInstalledSocket(paths, false)
	if err != nil {
		return stableIdentity{}, err
	}
	socketPath, err := rootedRuntimePath(paths.Root, paths.RequestSocket)
	if err != nil {
		return stableIdentity{}, err
	}
	connection, dialErr := net.DialTimeout("unix", socketPath, installedAcceptPoll)
	if dialErr == nil {
		_ = connection.Close()
		return stableIdentity{}, fmt.Errorf("installed request socket is already accepting")
	}
	if !errors.Is(dialErr, syscall.ECONNREFUSED) {
		return stableIdentity{}, fmt.Errorf("installed request socket state is indeterminate")
	}
	staleAfter, err := validateInstalledSocket(paths, false)
	if err != nil || staleAfter != staleBefore {
		return stableIdentity{}, fmt.Errorf("installed stale request socket changed")
	}
	runtimeFD, err = openRootedAbsolute(paths.Root, paths.RuntimeRoot, syscall.O_DIRECTORY)
	if err != nil {
		return stableIdentity{}, err
	}
	var runtimeStat syscall.Stat_t
	if err := syscall.Fstat(runtimeFD, &runtimeStat); err != nil ||
		!sameInstalledDirectoryObject(before, identityFromStat(runtimeStat)) {
		_ = syscall.Close(runtimeFD)
		return stableIdentity{}, fmt.Errorf("installed runtime directory changed")
	}
	unlinkErr := syscall.Unlinkat(runtimeFD, path.Base(paths.RequestSocket))
	_ = syscall.Close(runtimeFD)
	if unlinkErr != nil {
		return stableIdentity{}, unlinkErr
	}
	after, err := inspectStableRootedDirectory(paths.Root, paths.RuntimeRoot, expected, true)
	if err != nil || !sameInstalledDirectoryObject(before, after) {
		return stableIdentity{}, fmt.Errorf("installed runtime directory changed")
	}
	return after, nil
}

type installedRuntimeWatch struct {
	fd int
}

func openInstalledRuntimeWatch(paths installedRuntimePaths) (*installedRuntimeWatch, error) {
	if err := validateInstalledPrincipalContract(paths); err != nil {
		return nil, err
	}
	expected := expectedFileMetadata{Mode: 0o750, UID: paths.SocketUID, GID: paths.SocketGID}
	before, err := inspectStableRootedDirectory(paths.Root, paths.RuntimeRoot, expected, false)
	if err != nil {
		return nil, err
	}
	runtimePath, err := rootedRuntimePath(paths.Root, paths.RuntimeRoot)
	if err != nil {
		return nil, err
	}
	fd, err := syscall.InotifyInit1(syscall.IN_CLOEXEC | syscall.IN_NONBLOCK)
	if err != nil {
		return nil, err
	}
	mask := uint32(
		syscall.IN_ATTRIB |
			syscall.IN_CREATE |
			syscall.IN_DELETE |
			syscall.IN_DELETE_SELF |
			syscall.IN_MOVE_SELF |
			syscall.IN_MOVED_FROM |
			syscall.IN_MOVED_TO |
			syscall.IN_UNMOUNT |
			syscall.IN_DONT_FOLLOW |
			syscall.IN_ONLYDIR,
	)
	if _, err := syscall.InotifyAddWatch(fd, runtimePath, mask); err != nil {
		_ = syscall.Close(fd)
		return nil, err
	}
	after, err := inspectStableRootedDirectory(paths.Root, paths.RuntimeRoot, expected, false)
	if err != nil || before != after {
		_ = syscall.Close(fd)
		return nil, fmt.Errorf("installed runtime directory changed while watching")
	}
	return &installedRuntimeWatch{fd: fd}, nil
}

func (watch *installedRuntimeWatch) close() {
	if watch != nil && watch.fd >= 0 {
		_ = syscall.Close(watch.fd)
		watch.fd = -1
	}
}

func (watch *installedRuntimeWatch) changed() (bool, error) {
	if watch == nil || watch.fd < 0 {
		return false, fmt.Errorf("installed runtime watch unavailable")
	}
	buffer := make([]byte, 4096)
	defer zero(buffer)
	for {
		count, err := syscall.Read(watch.fd, buffer)
		if count > 0 {
			return true, nil
		}
		if err == syscall.EINTR {
			continue
		}
		if err == syscall.EAGAIN || err == syscall.EWOULDBLOCK {
			return false, nil
		}
		if err != nil {
			return false, err
		}
		return false, fmt.Errorf("installed runtime watch closed")
	}
}

func runInstalledRequestSmoke(stderr io.Writer) int {
	if err := runInstalledRequestSmokeWithPaths(defaultInstalledRuntimePaths()); err != nil {
		return refuse(stderr)
	}
	return 0
}

func runInstalledSupervisorRestartStimulus(stderr io.Writer) int {
	if os.Getpid() == 1 {
		return refuse(stderr)
	}
	if err := signalInstalledSupervisorRestart(
		defaultInstalledRuntimePaths(),
		"/proc/1/exe",
		1,
		syscall.Kill,
	); err != nil {
		return refuse(stderr)
	}
	// PID 1 handles the dedicated signal by exiting with protocol failure, after
	// which Docker terminates this exec process too. Remain silent and bounded
	// if a broken runtime fails to do so.
	time.Sleep(installedChildDeadline)
	return refuse(stderr)
}

func signalInstalledSupervisorRestart(
	paths installedRuntimePaths,
	targetExecutable string,
	targetPID int,
	signalProcess func(int, syscall.Signal) error,
) error {
	if targetExecutable == "" || targetPID <= 0 || targetPID == os.Getpid() || signalProcess == nil {
		return fmt.Errorf("installed restart stimulus invalid")
	}
	verification, err := validateInstalledLocalAuthority(Supervisor, paths)
	if err != nil {
		return err
	}
	if _, err := validateInstalledAuthorityState(paths); err != nil {
		return err
	}
	socketIdentity, err := validateInstalledSocket(paths, true)
	if err != nil {
		return err
	}
	role, ok := verification.Roles["supervisor-executable"]
	if !ok || validateRunningExecutable(targetExecutable, role) != nil {
		return fmt.Errorf("installed supervisor PID 1 invalid")
	}
	revalidated, err := validateInstalledLocalAuthority(Supervisor, paths)
	if err != nil || !sameInstalledAuthority(verification, revalidated) {
		return fmt.Errorf("installed authority changed during restart stimulus")
	}
	currentSocket, err := validateInstalledSocket(paths, true)
	if err != nil || currentSocket != socketIdentity {
		return fmt.Errorf("installed socket changed during restart stimulus")
	}
	if err := signalProcess(targetPID, syscall.SIGUSR2); err != nil {
		return fmt.Errorf("installed supervisor restart signal failed")
	}
	return nil
}

func runInstalledRequestSmokeWithPaths(paths installedRuntimePaths) error {
	verification, err := validateInstalledLocalAuthority(Supervisor, paths)
	if err != nil {
		return err
	}
	if _, err := validateInstalledSocket(paths, true); err != nil {
		return err
	}
	socketPath, err := rootedRuntimePath(paths.Root, paths.RequestSocket)
	if err != nil {
		return err
	}
	connection, err := net.DialUnix("unix", nil, &net.UnixAddr{Name: socketPath, Net: "unix"})
	if err != nil {
		return err
	}
	defer connection.Close()
	if err := connection.SetDeadline(time.Now().Add(installedChildDeadline + time.Second)); err != nil {
		return err
	}
	reader, writer, err := os.Pipe()
	if err != nil {
		return err
	}
	defer reader.Close()
	writerOpen := true
	defer func() {
		if writerOpen {
			_ = writer.Close()
		}
	}()
	payload := []byte(installedRequestSmokePayload)
	frame := make([]byte, 4+len(payload))
	binary.BigEndian.PutUint32(frame[:4], uint32(len(payload)))
	copy(frame[4:], payload)
	defer zero(frame)
	rights := syscall.UnixRights(int(writer.Fd()))
	rightsLength := len(rights)
	first, rightsCount, err := connection.WriteMsgUnix(frame[:1], rights, nil)
	zero(rights)
	if err != nil || first != 1 || rightsCount != rightsLength {
		return fmt.Errorf("installed request smoke first frame failed")
	}
	for offset := 1; offset < len(frame); {
		count, writeErr := connection.Write(frame[offset:])
		if writeErr != nil || count < 1 || count > len(frame)-offset {
			return fmt.Errorf("installed request smoke frame failed")
		}
		offset += count
	}
	if err := connection.CloseWrite(); err != nil {
		return err
	}
	if err := writer.Close(); err != nil {
		return err
	}
	writerOpen = false
	if err := awaitInstalledResponseEOF(reader, installedChildDeadline+time.Second); err != nil {
		return err
	}
	revalidated, err := validateInstalledLocalAuthority(Supervisor, paths)
	if err != nil || !sameInstalledAuthority(verification, revalidated) {
		return fmt.Errorf("installed authority changed during request smoke")
	}
	if _, err := validateInstalledSocket(paths, true); err != nil {
		return err
	}
	return nil
}

func awaitInstalledResponseEOF(reader *os.File, timeout time.Duration) error {
	if reader == nil || timeout <= 0 {
		return fmt.Errorf("installed response smoke invalid")
	}
	fd := int(reader.Fd())
	if err := syscall.SetNonblock(fd, true); err != nil {
		return err
	}
	deadline := time.Now().Add(timeout)
	buffer := make([]byte, 1)
	for {
		count, err := syscall.Read(fd, buffer)
		if count > 0 {
			zero(buffer)
			return fmt.Errorf("installed controller emitted a response")
		}
		if count == 0 && err == nil {
			zero(buffer)
			return nil
		}
		if err != syscall.EAGAIN && err != syscall.EWOULDBLOCK && err != syscall.EINTR {
			zero(buffer)
			return err
		}
		if time.Until(deadline) <= 0 {
			zero(buffer)
			return fmt.Errorf("installed controller response remained open")
		}
		time.Sleep(time.Millisecond)
	}
}

func enableInstalledPassCredentials(listener *net.UnixListener) error {
	raw, err := listener.SyscallConn()
	if err != nil {
		return err
	}
	var operationError error
	if err := raw.Control(func(fd uintptr) {
		operationError = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_PASSCRED, 1)
	}); err != nil {
		return err
	}
	return operationError
}

func sameInstalledDirectoryObject(left, right stableIdentity) bool {
	return left.Device == right.Device &&
		left.Inode == right.Inode &&
		left.Rdevice == right.Rdevice &&
		left.Mode == right.Mode &&
		left.Links == right.Links &&
		left.UID == right.UID &&
		left.GID == right.GID
}

type installedTerminalError struct{ cause error }

func (failure installedTerminalError) Error() string {
	return "installed local authority terminal failure"
}
func (failure installedTerminalError) Unwrap() error { return failure.cause }

func handleInstalledConnection(
	connection *net.UnixConn,
	paths installedRuntimePaths,
	startup *installedAuthorityVerification,
) error {
	fd, err := duplicateInstalledConnection(connection)
	_ = connection.Close()
	if err != nil {
		return err
	}
	intake, err := receiveBrokerControllerIntake(fd, brokerReadTimeout)
	if err != nil {
		return err
	}
	defer intake.release()
	if !installedPeerAuthorized(intake.peer.UID, intake.peer.GID, paths) ||
		intake.request == nil || !intake.request.envelopeDigestMatches {
		return fmt.Errorf("installed local request rejected")
	}
	revalidated, err := validateInstalledLocalAuthority(Supervisor, paths)
	if err != nil || !sameInstalledAuthority(startup, revalidated) {
		return installedTerminalError{cause: fmt.Errorf("installed authority changed")}
	}
	requestBindings, err := authenticateInstalledRequestBindings(
		paths,
		revalidated,
		intake.request,
		time.Now(),
	)
	if err != nil {
		return fmt.Errorf("installed local request authentication failed")
	}
	authenticatedAuthority, err := validateInstalledLocalAuthority(Supervisor, paths)
	if err != nil || !sameInstalledAuthority(revalidated, authenticatedAuthority) {
		return installedTerminalError{cause: fmt.Errorf("installed authority changed during request authentication")}
	}
	revalidated = authenticatedAuthority
	stateGeneration, err := validateInstalledAuthorityState(paths)
	if err != nil {
		return installedTerminalError{
			cause: fmt.Errorf("installed authority state changed during request authentication"),
		}
	}
	role, ok := revalidated.Roles["controller-executable"]
	if !ok {
		return installedTerminalError{cause: fmt.Errorf("installed controller role missing")}
	}
	controller, err := openPinnedInstalledController(paths, role)
	if err != nil {
		return installedTerminalError{cause: err}
	}
	if err := revalidateBrokerResponsePipe(intake); err != nil {
		_ = controller.Close()
		return fmt.Errorf("installed response pipe changed")
	}
	controllerResponse, controllerWriter, err := os.Pipe()
	if err != nil {
		_ = controller.Close()
		return installedTerminalError{cause: fmt.Errorf("private controller response pipe failed")}
	}
	if err := validateReadPipe(int(controllerResponse.Fd())); err != nil {
		_ = controllerResponse.Close()
		_ = controllerWriter.Close()
		_ = controller.Close()
		return installedTerminalError{cause: fmt.Errorf("private controller response reader invalid")}
	}
	if err := validateWritePipe(int(controllerWriter.Fd())); err != nil {
		_ = controllerResponse.Close()
		_ = controllerWriter.Close()
		_ = controller.Close()
		return installedTerminalError{cause: fmt.Errorf("private controller response writer invalid")}
	}
	controllerRequest, requestWriter, err := os.Pipe()
	if err != nil {
		_ = controllerResponse.Close()
		_ = controllerWriter.Close()
		_ = controller.Close()
		return installedTerminalError{cause: fmt.Errorf("private authenticated request pipe failed")}
	}
	if err := validateReadPipe(int(controllerRequest.Fd())); err != nil {
		_ = controllerRequest.Close()
		_ = requestWriter.Close()
		_ = controllerResponse.Close()
		_ = controllerWriter.Close()
		_ = controller.Close()
		return installedTerminalError{cause: fmt.Errorf("private authenticated request reader invalid")}
	}
	if err := validateWritePipe(int(requestWriter.Fd())); err != nil {
		_ = controllerRequest.Close()
		_ = requestWriter.Close()
		_ = controllerResponse.Close()
		_ = controllerWriter.Close()
		_ = controller.Close()
		return installedTerminalError{cause: fmt.Errorf("private authenticated request writer invalid")}
	}
	request := intake.request
	eventID := "local-" + request.rawBodyDigest[len("sha256:"):]
	args := []string{
		ControllerExecutable,
		"--config", ControllerConfig,
		"--operation", request.envelope.Operation,
		"--response-fd", "3",
		"--event-id", eventID,
		"--request-transport-digest", request.rawBodyDigest,
		"--installed-local-authority-executable-fd", "4",
		"--authenticated-request-fd", "5",
	}
	command := &exec.Cmd{
		Path:       "/proc/self/fd/4",
		Args:       args,
		Env:        []string{},
		Dir:        rootedUnchecked(paths.Root, paths.StateRoot),
		ExtraFiles: []*os.File{controllerWriter, controller, controllerRequest},
		SysProcAttr: &syscall.SysProcAttr{
			Setpgid:   true,
			Pdeathsig: syscall.SIGKILL,
		},
	}
	if err := claimInstalledRequestReplay(
		paths,
		request,
		requestBindings.rootPolicyDigest,
		revalidated,
		stateGeneration,
	); err != nil {
		_ = controllerRequest.Close()
		_ = requestWriter.Close()
		_ = controllerResponse.Close()
		_ = controllerWriter.Close()
		_ = controller.Close()
		var replay installedReplayRejectedError
		if errors.As(err, &replay) {
			return fmt.Errorf("installed request replay rejected")
		}
		return installedTerminalError{
			cause: fmt.Errorf("installed replay claim failed: %w", err),
		}
	}
	if err := command.Start(); err != nil {
		_ = controllerRequest.Close()
		_ = requestWriter.Close()
		_ = controllerResponse.Close()
		_ = controllerWriter.Close()
		_ = controller.Close()
		return installedTerminalError{cause: err}
	}
	_ = controllerRequest.Close()
	_ = controllerWriter.Close()
	_ = controller.Close()
	childResult := make(chan error, 1)
	responseResult := make(chan error, 1)
	requestResult := make(chan error, 1)
	go func() { childResult <- command.Wait() }()
	go func() {
		responseResult <- awaitInstalledResponseEOF(
			controllerResponse,
			installedChildDeadline,
		)
	}()
	go func() {
		requestResult <- writeAuthenticatedRequestPipe(
			requestWriter,
			request.rawBody,
			installedChildDeadline,
		)
	}()
	timer := time.NewTimer(installedChildDeadline)
	defer timer.Stop()
	childFinished := false
	responseFinished := false
	requestFinished := false
	for !childFinished || !responseFinished || !requestFinished {
		select {
		case err := <-childResult:
			childFinished = true
			if installedExitCode(err) != ExitProtocolFailure {
				_ = syscall.Kill(-command.Process.Pid, syscall.SIGKILL)
				_ = controllerResponse.Close()
				_ = requestWriter.Close()
				if !responseFinished {
					<-responseResult
				}
				if !requestFinished {
					<-requestResult
				}
				return installedTerminalError{cause: fmt.Errorf("fixed controller exit invalid")}
			}
		case err := <-responseResult:
			responseFinished = true
			if err != nil {
				_ = syscall.Kill(-command.Process.Pid, syscall.SIGKILL)
				_ = controllerResponse.Close()
				_ = requestWriter.Close()
				if !childFinished {
					<-childResult
				}
				if !requestFinished {
					<-requestResult
				}
				return installedTerminalError{cause: fmt.Errorf("fixed controller response invalid")}
			}
		case err := <-requestResult:
			requestFinished = true
			if err != nil {
				_ = syscall.Kill(-command.Process.Pid, syscall.SIGKILL)
				_ = controllerResponse.Close()
				if !childFinished {
					<-childResult
				}
				if !responseFinished {
					<-responseResult
				}
				return installedTerminalError{cause: fmt.Errorf("fixed controller request transfer invalid")}
			}
		case <-timer.C:
			_ = syscall.Kill(-command.Process.Pid, syscall.SIGKILL)
			_ = controllerResponse.Close()
			_ = requestWriter.Close()
			if !childFinished {
				<-childResult
			}
			if !responseFinished {
				<-responseResult
			}
			if !requestFinished {
				<-requestResult
			}
			return installedTerminalError{cause: fmt.Errorf("fixed controller timed out")}
		}
	}
	_ = controllerResponse.Close()
	return nil
}

func installedPeerAuthorized(uid, gid uint32, paths installedRuntimePaths) bool {
	return uid == paths.CallerUID && gid == paths.CallerGID
}

func duplicateInstalledConnection(connection *net.UnixConn) (int, error) {
	raw, err := connection.SyscallConn()
	if err != nil {
		return -1, err
	}
	duplicate := -1
	var operationError error
	if err := raw.Control(func(fd uintptr) {
		if setErr := syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_PASSCRED, 1); setErr != nil {
			operationError = setErr
			return
		}
		duplicate, operationError = fcntl(int(fd), syscall.F_DUPFD_CLOEXEC, 3)
	}); err != nil {
		return -1, err
	}
	if operationError != nil || duplicate < 0 {
		if duplicate >= 0 {
			_ = syscall.Close(duplicate)
		}
		return -1, operationError
	}
	return duplicate, nil
}

func sameInstalledAuthority(left, right *installedAuthorityVerification) bool {
	if left == nil || right == nil ||
		left.AuthenticationDigest != right.AuthenticationDigest ||
		left.PayloadTreeDigest != right.PayloadTreeDigest ||
		left.AuthorityKeyID != right.AuthorityKeyID ||
		left.ManifestDigest != right.ManifestDigest ||
		left.NativeBuildDigest != right.NativeBuildDigest ||
		len(left.Roles) != len(right.Roles) {
		return false
	}
	for role, expected := range left.Roles {
		if right.Roles[role] != expected {
			return false
		}
	}
	return true
}

func installedExitCode(err error) int {
	if err == nil {
		return 0
	}
	var exitError *exec.ExitError
	if errors.As(err, &exitError) {
		return exitError.ExitCode()
	}
	return -1
}

func runInstalledWatchdog(args []string, stdout, stderr io.Writer) int {
	paths := defaultInstalledRuntimePaths()
	switch {
	case len(args) == 2 && args[0] == "--installed-local-authority" && args[1] == "--health-json":
		if err := writeInstalledHealth(paths, stdout); err != nil {
			return refuse(stderr)
		}
		return 0
	case len(args) == 2 && args[0] == "--installed-local-authority" && args[1] == "--docker-watchdog":
		ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
		defer stop()
		if err := watchInstalledAuthority(ctx, paths, installedHealthPoll, stdout); err != nil {
			return refuse(stderr)
		}
		return 0
	default:
		return refuse(stderr)
	}
}

func writeInstalledHealth(paths installedRuntimePaths, stdout io.Writer) error {
	verification, err := validateInstalledLocalAuthority(Watchdog, paths)
	if err != nil {
		return err
	}
	if _, err := validateInstalledSocket(paths, true); err != nil {
		return err
	}
	payload, err := installedHealthJSON(verification)
	if err != nil {
		return err
	}
	defer zero(payload)
	written, err := stdout.Write(payload)
	if err != nil || written != len(payload) {
		return fmt.Errorf("installed health write failed")
	}
	return nil
}

func watchInstalledAuthority(
	ctx context.Context,
	paths installedRuntimePaths,
	interval time.Duration,
	stdout io.Writer,
) error {
	if interval <= 0 {
		return fmt.Errorf("watchdog interval invalid")
	}
	verification, err := validateInstalledLocalAuthority(Watchdog, paths)
	if err != nil {
		return err
	}
	runtimeWatch, err := openInstalledRuntimeWatch(paths)
	if err != nil {
		return err
	}
	defer runtimeWatch.close()
	socketIdentity, err := validateInstalledSocket(paths, true)
	if err != nil {
		return err
	}
	if changed, err := runtimeWatch.changed(); err != nil || changed {
		return fmt.Errorf("installed runtime changed during watchdog startup")
	}
	payload, err := installedHealthJSON(verification)
	if err != nil {
		return err
	}
	written, err := stdout.Write(payload)
	if err != nil || written != len(payload) {
		zero(payload)
		return fmt.Errorf("installed health write failed")
	}
	zero(payload)
	authorityTicker := time.NewTicker(interval)
	defer authorityTicker.Stop()
	runtimeTicker := time.NewTicker(installedRuntimePoll)
	defer runtimeTicker.Stop()
	for {
		if ctx.Err() != nil {
			return nil
		}
		select {
		case <-ctx.Done():
			return nil
		case <-runtimeTicker.C:
			if ctx.Err() != nil {
				return nil
			}
			if changed, err := runtimeWatch.changed(); err != nil || changed {
				return fmt.Errorf("installed runtime socket generation changed")
			}
		case <-authorityTicker.C:
			if ctx.Err() != nil {
				return nil
			}
			current, err := validateInstalledLocalAuthority(Watchdog, paths)
			if err != nil || !sameInstalledAuthority(verification, current) {
				return fmt.Errorf("installed authority became indeterminate")
			}
			currentSocket, err := validateInstalledSocket(paths, true)
			if err != nil {
				return err
			}
			if currentSocket != socketIdentity {
				return fmt.Errorf("installed runtime socket identity changed")
			}
			if changed, err := runtimeWatch.changed(); err != nil || changed {
				return fmt.Errorf("installed runtime socket generation changed")
			}
		}
	}
}

func installedHealthJSON(verification *installedAuthorityVerification) ([]byte, error) {
	if verification == nil {
		return nil, fmt.Errorf("installed health unavailable")
	}
	return readCanonicalHealth(map[string]any{
		"schema":                             localHealthSchema,
		"version":                            json.Number("2"),
		"component":                          string(Watchdog),
		"ready":                              true,
		"installed_local_authority_verified": true,
		"socket_accepting":                   true,
		"authoritative_for_package_authentication": true,
		"authoritative_for_release_effects":        false,
		"production_ready":                         false,
		"performs_release_effects":                 false,
		"authentication_digest":                    verification.AuthenticationDigest,
		"payload_tree_digest":                      verification.PayloadTreeDigest,
		"authority_key_id":                         verification.AuthorityKeyID,
		"source_manifest_digest":                   SourceManifestDigest,
	})
}

func validateInstalledSocket(paths installedRuntimePaths, connect bool) (stableIdentity, error) {
	if err := validateInstalledPrincipalContract(paths); err != nil {
		return stableIdentity{}, err
	}
	parentBefore, err := inspectStableRootedDirectory(
		paths.Root,
		paths.RuntimeRoot,
		expectedFileMetadata{Mode: 0o750, UID: paths.SocketUID, GID: paths.SocketGID},
		false,
	)
	if err != nil {
		return stableIdentity{}, err
	}
	runtimeFD, err := openRootedAbsolute(paths.Root, paths.RuntimeRoot, syscall.O_DIRECTORY)
	if err != nil {
		return stableIdentity{}, err
	}
	names, namesErr := directoryNames(runtimeFD)
	_ = syscall.Close(runtimeFD)
	if namesErr != nil || len(names) != 1 || names[0] != path.Base(paths.RequestSocket) {
		return stableIdentity{}, fmt.Errorf("installed runtime entries invalid")
	}
	socketPath, err := rootedRuntimePath(paths.Root, paths.RequestSocket)
	if err != nil {
		return stableIdentity{}, err
	}
	info, err := os.Lstat(socketPath)
	if err != nil {
		return stableIdentity{}, err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return stableIdentity{}, fmt.Errorf("installed socket metadata unavailable")
	}
	identity := identityFromStat(*stat)
	if identity.Mode&syscall.S_IFMT != syscall.S_IFSOCK || identity.Links != 1 ||
		identity.Mode&0o7777 != installedSocketMode ||
		identity.UID != paths.SocketUID || identity.GID != paths.SocketGID {
		return stableIdentity{}, fmt.Errorf("installed socket metadata invalid")
	}
	if connect {
		connection, err := net.DialTimeout("unix", socketPath, time.Second)
		if err != nil {
			return stableIdentity{}, err
		}
		_ = connection.Close()
	}
	infoAfter, err := os.Lstat(socketPath)
	if err != nil {
		return stableIdentity{}, err
	}
	statAfter, ok := infoAfter.Sys().(*syscall.Stat_t)
	if !ok || identityFromStat(*statAfter) != identity {
		return stableIdentity{}, fmt.Errorf("installed socket changed")
	}
	parentAfter, err := inspectStableRootedDirectory(
		paths.Root,
		paths.RuntimeRoot,
		expectedFileMetadata{Mode: 0o750, UID: paths.SocketUID, GID: paths.SocketGID},
		false,
	)
	if err != nil || parentAfter != parentBefore {
		return stableIdentity{}, fmt.Errorf("installed runtime directory changed")
	}
	return identity, nil
}

func rootedRuntimePath(root, absolute string) (string, error) {
	if root == "" || !path.IsAbs(absolute) || path.Clean(absolute) != absolute {
		return "", fmt.Errorf("runtime path invalid")
	}
	return rootedUnchecked(root, absolute), nil
}

func rootedUnchecked(root, absolute string) string {
	if root == "/" {
		return absolute
	}
	return path.Join(root, absolute)
}

func installedExecutableFDAtFixedPosition(args []string) (int, bool) {
	if len(args) < 12 || args[10] != "--installed-local-authority-executable-fd" {
		return 0, false
	}
	fd, err := strconv.Atoi(args[11])
	return fd, err == nil && fd >= 3 && strconv.Itoa(fd) == args[11]
}
